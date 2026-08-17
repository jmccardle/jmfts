"""PELT → ingest: constructive topic segmentation wired into conversation ingest.

Two layers of test:

1. ``segment_conversation`` construction — deterministic, embeddings injected by hand
   (two orthogonal clusters) so the tree surgery (containers created, turns reparented,
   bounds/penalty honoured) is proven without depending on the embedding model or on PELT
   tuning.
2. Orchestrator + pipeline wiring — that ``segment`` is opt-in (default off → flat tree,
   canary rule), routes to the stage, and flows ``segment_count`` back; the conversation
   pipeline exposes it as a default-disabled stage. See ROADMAP "PELT → ingest".
"""

import asyncio

import numpy as np

from jmfts_core.conversation_ingest import (
    ParsedMessage,
    SegmentationOutcome,
    ingest_conversation,
    segment_conversation,
)
from jmfts_core.pipeline import _resolve_stages, get_pipeline
from jmfts_core.repositories.document import DocumentRepository

DIM = 768


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _unit(axis: int) -> list:
    v = np.zeros(DIM, dtype=float)
    v[axis] = 1.0
    return v.tolist()


def _root_with_turns(db_session, embeds):
    """Create a conversation root with one 'chunk' child per entry in ``embeds`` (each a
    hand-set embedding), returning (root_id, [child_ids in order])."""
    repo = DocumentRepository(db_session)
    root = repo.create(title="conv", content="root", usetype="conversation", auto_embed=False)
    child_ids = []
    for i, emb in enumerate(embeds):
        c = repo.create(
            title=f"turn {i}",
            content=f"turn {i} content",
            parent_id=root.id,
            usetype="chunk",
            auto_embed=False,
        )
        c.embed = emb
        child_ids.append(c.id)
    db_session.flush()
    return root.id, child_ids


class TestSegmentConversationConstruction:
    def test_two_clusters_build_two_containers_and_reparent(self, db_session):
        # 5 turns on axis 0, then 5 on axis 1 — a clean topic shift at index 5.
        embeds = [_unit(0)] * 5 + [_unit(1)] * 5
        root_id, child_ids = _root_with_turns(db_session, embeds)

        outcome = segment_conversation(db_session, root_id, child_ids)
        containers = outcome.container_ids

        assert outcome.ran is True
        assert len(containers) == 2
        repo = DocumentRepository(db_session)
        # Root's direct children are now the segment containers, not the raw turns.
        root_children = repo.get_children(root_id, depth=1, limit=100)
        assert {c.id for c in root_children} == set(containers)
        assert all(c.usetype == "segment" for c in root_children)
        # Each turn was reparented under a container (5 + 5), path updated.
        for cont_id in containers:
            members = repo.get_children(cont_id, depth=1, limit=100)
            assert len(members) == 5
            assert all(m.parent_id == cont_id for m in members)
            assert all(cont_id in (m.path or []) for m in members)

    def test_single_topic_creates_nothing_but_reports_that_pelt_ran(self, db_session):
        """Spec 3.4: a heuristic that ran and matched nothing must not look like one that
        never ran. One topic run is a MEASUREMENT — `ran` is True and the detail says what
        was looked at — and a different penalty could legitimately overturn it."""
        embeds = [_unit(0)] * 8  # one topic → one segment → no interim layer
        root_id, child_ids = _root_with_turns(db_session, embeds)

        outcome = segment_conversation(db_session, root_id, child_ids)
        assert outcome.container_ids == []
        assert outcome.ran is True
        assert outcome.detail["embedded_turns"] == 8
        assert outcome.detail["segments_found"] <= 1
        assert "penalty" in outcome.detail
        repo = DocumentRepository(db_session)
        # Turns are still the root's direct children — untouched.
        assert {c.id for c in repo.get_children(root_id, depth=1, limit=100)} == set(child_ids)

    def test_too_few_turns_never_runs_pelt_and_says_so(self, db_session):
        embeds = [_unit(0), _unit(1), _unit(0), _unit(1)]  # 4 < 2*min_segment(=6)
        root_id, child_ids = _root_with_turns(db_session, embeds)

        outcome = segment_conversation(db_session, root_id, child_ids)
        assert outcome.container_ids == []
        assert outcome.ran is False
        # The one status that has to carry a reason (attempt-record contract, spec 3.4).
        assert "4" in outcome.detail["reason"] and "6" in outcome.detail["reason"]

    def test_high_penalty_override_suppresses_splitting(self, db_session):
        embeds = [_unit(0)] * 5 + [_unit(1)] * 5
        root_id, child_ids = _root_with_turns(db_session, embeds)
        # A huge penalty makes even a clean boundary not worth a breakpoint — but PELT ran.
        suppressed = segment_conversation(db_session, root_id, child_ids, penalty=1e9)
        assert suppressed.container_ids == [] and suppressed.ran is True
        assert suppressed.detail["penalty"] == 1e9
        # ...while the default log(n) penalty does split (sanity re-assert).
        assert len(segment_conversation(db_session, root_id, child_ids).container_ids) == 2


class TestOrchestratorWiring:
    def _msgs(self, n=6):
        return [
            ParsedMessage(role="user", content=f"message number {i}", turn_index=i)
            for i in range(n)
        ]

    def test_segment_default_off_is_flat_tree(self, db_session):
        """Canary: without segment=True the tree shape is unchanged and count is 0."""
        result = _run(
            ingest_conversation(db_session, self._msgs(), summarize=False, extract_triples=False)
        )
        assert result.segment_count == 0
        repo = DocumentRepository(db_session)
        kids = repo.get_children(result.source_document_id, depth=1, limit=100)
        assert kids and all(k.usetype == "chunk" for k in kids)  # turns, not segments
        seg_stage = next(s for s in result.stages if s.stage == "segment")
        assert seg_stage.status == "skipped" and seg_stage.detail.get("reason") == "disabled"

    def test_segment_true_routes_and_reports_count(self, db_session, monkeypatch):
        """segment=True calls the constructor and flows its count back — proven without
        depending on real-embedding separability by stubbing the constructor."""
        called = {}

        def fake_segment(session, root_id, child_ids, **kwargs):
            called["args"] = (root_id, list(child_ids), kwargs)
            # pretend two containers were built
            return SegmentationOutcome(container_ids=[111, 222], ran=True, detail={})

        monkeypatch.setattr("jmfts_core.conversation_ingest.segment_conversation", fake_segment)
        result = _run(
            ingest_conversation(
                db_session,
                self._msgs(),
                segment=True,
                segment_penalty=7.0,
                summarize=False,
                extract_triples=False,
            )
        )
        assert result.segment_count == 2
        assert called["args"][2]["penalty"] == 7.0  # override threaded through
        seg_stage = next(s for s in result.stages if s.stage == "segment")
        assert seg_stage.status == "completed"
        assert seg_stage.detail["segments_created"] == 2

    def test_pelt_that_ran_and_matched_nothing_is_completed_not_skipped(
        self, db_session, monkeypatch
    ):
        """INGEST_SPEC 5.1: "the divider task matching nothing is `completed`, not
        `skipped`. It ran." The stage result becomes a durable attempt record, where a
        `skipped` entry means "never attempted" and is the key spec 6.1 re-runs on."""

        def fake_segment(session, root_id, child_ids, **kwargs):
            return SegmentationOutcome(
                container_ids=[], ran=True, detail={"segments_found": 1, "penalty": 1.79}
            )

        monkeypatch.setattr("jmfts_core.conversation_ingest.segment_conversation", fake_segment)
        result = _run(
            ingest_conversation(
                db_session, self._msgs(), segment=True, summarize=False, extract_triples=False
            )
        )
        seg_stage = next(s for s in result.stages if s.stage == "segment")
        assert seg_stage.status == "completed"
        assert seg_stage.detail["segments_created"] == 0
        assert seg_stage.detail["segments_found"] == 1  # what it looked for, and found
        assert "reason" not in seg_stage.detail  # `reason` belongs to `skipped` alone

    def test_pelt_that_never_ran_is_skipped_with_a_reason(self, db_session, monkeypatch):
        def fake_segment(session, root_id, child_ids, **kwargs):
            return SegmentationOutcome(
                container_ids=[], ran=False, detail={"reason": "2 embedded turns is below 6"}
            )

        monkeypatch.setattr("jmfts_core.conversation_ingest.segment_conversation", fake_segment)
        result = _run(
            ingest_conversation(
                db_session, self._msgs(), segment=True, summarize=False, extract_triples=False
            )
        )
        seg_stage = next(s for s in result.stages if s.stage == "segment")
        assert seg_stage.status == "skipped"
        assert seg_stage.detail["reason"] == "2 embedded turns is below 6"


class TestPipelineExposure:
    def test_conversation_pipeline_exposes_segment_stage_disabled(self):
        seg = get_pipeline("conversation").default_stages["segment"]
        assert seg.enabled is False  # opt-in
        assert seg.params["min_segment"] == 3 and seg.params["max_segment"] == 10

    def test_pipeline_config_can_enable_and_tune_segment(self):
        stages = _resolve_stages(
            get_pipeline("conversation"),
            {"segment": {"enabled": True, "penalty": 5.0}},
        )
        assert stages["segment"].enabled is True
        assert stages["segment"].params["penalty"] == 5.0
