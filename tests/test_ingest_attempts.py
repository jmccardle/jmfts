"""Tests for the durable attempt log — INGEST_SPEC.md Part 3.3 / 3.4.

Tier 1: unit tests over the contract and the fingerprint (no DB).
Tier 2: integration tests — real DB (savepoint rollback), mocked embedding, LLM stages
off, asserting that ``execute_pipeline`` actually writes the log to the row rather than
into a mock that proves nothing.
"""

import asyncio
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from pydantic import ValidationError
from sqlalchemy import text as sa_text

from jmfts_core.contracts.attempt import AttemptRecord, param_fingerprint
from jmfts_core.pipeline import execute_pipeline

# ---------------------------------------------------------------------------
# DB availability check (same pattern as test_regression_ingestion.py)
# ---------------------------------------------------------------------------

try:
    from jmfts_core.database import get_engine, get_session_factory
    from jmfts_core.embedding import EmbeddingResult, TokenEmbeddingResult
    from jmfts_core.repositories.document import DocumentRepository

    _engine = get_engine()
    with _engine.connect() as _conn:
        _conn.execute(sa_text("SELECT 1"))
    _DB_AVAILABLE = True
except Exception:
    _DB_AVAILABLE = False

requires_db = pytest.mark.skipif(not _DB_AVAILABLE, reason="Database not available")

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _utc(offset_seconds: int = 0) -> datetime:
    return datetime(2026, 8, 15, 14, 21, 3, tzinfo=timezone.utc) + timedelta(seconds=offset_seconds)


def _record(**overrides) -> AttemptRecord:
    """A minimal valid record; override one field per test to isolate the rule."""
    fields = dict(
        task="chunk",
        status="completed",
        scope_document_id=1000,
        params={},
        param_fingerprint=param_fingerprint({}),
        started_at=_utc(),
        finished_at=_utc(3),
    )
    fields.update(overrides)
    return AttemptRecord(**fields)


# ============================================================================
# Tier 1 — param_fingerprint
# ============================================================================


class TestParamFingerprint:
    def test_insensitive_to_dict_ordering(self):
        a = {"min_section_chars": 200, "exclude_patterns": ["^Page \\d+$"], "enabled": True}
        b = {"enabled": True, "exclude_patterns": ["^Page \\d+$"], "min_section_chars": 200}
        assert param_fingerprint(a) == param_fingerprint(b)

    def test_insensitive_to_nested_dict_ordering(self):
        a = {"chunk": {"strategy": "paragraph", "max_tokens": 200}}
        b = {"chunk": {"max_tokens": 200, "strategy": "paragraph"}}
        assert param_fingerprint(a) == param_fingerprint(b)

    def test_changes_when_a_value_changes(self):
        base = {"strategy": "paragraph", "max_tokens": 200}
        assert param_fingerprint(base) != param_fingerprint({**base, "max_tokens": 500})

    def test_changes_when_a_key_is_added(self):
        base = {"strategy": "paragraph"}
        assert param_fingerprint(base) != param_fingerprint({**base, "min_chunk_length": 20})

    def test_list_order_is_significant(self):
        # Lists are ordered data (exclusion patterns are applied in order), so unlike dict
        # keys their order MUST change the fingerprint.
        assert param_fingerprint({"p": [1, 2]}) != param_fingerprint({"p": [2, 1]})

    def test_empty_params_are_hashable(self):
        fp = param_fingerprint({})
        assert fp.startswith("sha256:")
        assert len(fp) == len("sha256:") + 64

    def test_distinguishes_none_from_missing(self):
        # `llm_model: None` means "not overridden"; an absent key means the stage has no
        # such parameter at all. Spec 6.1 diffs on this value, so they cannot collide.
        assert param_fingerprint({"llm_model": None}) != param_fingerprint({})

    def test_rejects_values_json_cannot_represent(self):
        with pytest.raises(TypeError):
            param_fingerprint({"when": datetime(2026, 8, 15, tzinfo=timezone.utc)})

    def test_stable_across_processes(self):
        """Deterministic under different PYTHONHASHSEEDs — spec 6.1 compares across runs.

        Two subprocesses with different hash seeds must agree with this process. This is
        the property that rules out any implementation resting on ``hash()`` or on dict
        iteration order.
        """
        expected = param_fingerprint({"b": 1, "a": {"z": [1, 2], "y": "x"}, "c": None})
        code = (
            "from jmfts_core.contracts.attempt import param_fingerprint;"
            "print(param_fingerprint({'a': {'y': 'x', 'z': [1, 2]}, 'c': None, 'b': 1}))"
        )
        for seed in ("1", "12345"):
            env = {**os.environ, "PYTHONHASHSEED": seed}
            out = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True,
                text=True,
                check=True,
                env=env,
                cwd=REPO_ROOT,
            )
            assert out.stdout.strip() == expected


# ============================================================================
# Tier 1 — AttemptRecord invariants
# ============================================================================


class TestAttemptRecordStatus:
    def test_skipped_without_a_reason_is_rejected(self):
        with pytest.raises(ValidationError, match="detail.reason"):
            _record(status="skipped", detail={})

    def test_skipped_with_a_blank_reason_is_rejected(self):
        with pytest.raises(ValidationError, match="detail.reason"):
            _record(status="skipped", detail={"reason": "   "})

    def test_skipped_with_a_reason_is_accepted(self):
        rec = _record(status="skipped", detail={"reason": "no vision model configured"})
        assert rec.status == "skipped"

    def test_completed_finding_nothing_needs_no_reason(self):
        """Spec 3.4: a heuristic that ran and matched nothing is `completed`, not skipped.

        The record must not force a reason onto it — that is precisely the fact the two
        statuses exist to keep apart.
        """
        rec = _record(status="completed", detail={"looked_for": "outline", "sections": 0})
        assert rec.status == "completed"
        assert "reason" not in rec.detail

    def test_unknown_status_is_rejected(self):
        with pytest.raises(ValidationError):
            _record(status="done")

    def test_all_five_statuses_are_accepted(self):
        assert _record(status="completed").status == "completed"
        assert _record(status="failed", error="boom").status == "failed"
        assert _record(status="skipped", detail={"reason": "disabled"}).status == "skipped"
        assert _record(status="running", finished_at=None).status == "running"
        assert _record(status="pending", started_at=None, finished_at=None).status == "pending"


class TestAttemptRecordTiming:
    def test_naive_started_at_is_rejected(self):
        with pytest.raises(ValidationError, match="timezone-aware"):
            _record(started_at=datetime(2026, 8, 15, 14, 21, 3))

    def test_non_utc_offset_is_normalised(self):
        eastern = timezone(timedelta(hours=-4))
        rec = _record(
            started_at=datetime(2026, 8, 15, 10, 21, 3, tzinfo=eastern),
            finished_at=_utc(3),
        )
        assert rec.started_at == _utc()
        assert rec.started_at.utcoffset() == timedelta(0)

    def test_terminal_status_requires_finished_at(self):
        with pytest.raises(ValidationError, match="finished_at"):
            _record(status="completed", finished_at=None)

    def test_non_terminal_status_requires_started_at(self):
        with pytest.raises(ValidationError, match="started_at"):
            _record(status="running", started_at=None, finished_at=None)

    def test_pending_carries_no_timestamps(self):
        with pytest.raises(ValidationError, match="pending"):
            _record(status="pending", started_at=_utc(), finished_at=_utc(1))

    def test_finished_before_started_is_rejected(self):
        with pytest.raises(ValidationError, match="precedes"):
            _record(started_at=_utc(5), finished_at=_utc(1))


class TestAttemptRecordSerialisation:
    def test_to_jsonb_is_json_native(self):
        entry = _record(
            rung="declared",
            produced={"node_count": 2, "child_ids": [7, 8]},
            detail={"sections": 2},
        ).to_jsonb()
        assert datetime.fromisoformat(entry["started_at"]) == _utc()
        assert entry["produced"] == {"node_count": 2, "child_ids": [7, 8]}
        # The not-yet-built machinery is explicitly null, not absent and not invented.
        assert entry["task_id"] is None
        assert entry["write_mode"] is None
        assert entry["superseded_by"] is None
        assert entry["error_type"] is None

    def test_every_spec_3_4_field_is_present(self):
        entry = _record().to_jsonb()
        assert set(entry) == {
            "task",
            "task_id",
            "status",
            "attempt",
            "rung",
            "scope_document_id",
            "write_mode",
            "params",
            "param_fingerprint",
            "started_at",
            "finished_at",
            "detail",
            "produced",
            "superseded_by",
            "error",
            "error_type",
        }


# ============================================================================
# Tier 2 — the pipeline actually writes it
# ============================================================================


class MockEmbeddingService:
    """Deterministic mock: hashes text to produce a reproducible embedding."""

    def __init__(self, dim=768):
        self.dim = dim

    def embed_text(self, text, normalize=True, prefix=""):
        rng = np.random.default_rng(abs(hash(text)) % (2**31))
        vec = rng.standard_normal(self.dim).astype(np.float32)
        vec /= np.linalg.norm(vec)
        return vec

    def embed_with_tokens(self, text, top_percent=0.35, token_selector=None, prefix=""):
        doc_emb = self.embed_text(text)
        words = (text.split() or ["empty"])[:3]
        token_embs = []
        for i, w in enumerate(words):
            rng = np.random.default_rng(abs(hash(f"{text}_{i}")) % (2**31))
            tok_emb = rng.standard_normal(self.dim).astype(np.float32)
            tok_emb /= np.linalg.norm(tok_emb)
            token_embs.append(
                TokenEmbeddingResult(
                    token_idx=i,
                    token_text=w,
                    importance_score=1.0 - i * 0.2,
                    embedding=tok_emb,
                )
            )
        return EmbeddingResult(document_embedding=doc_emb, token_embeddings=token_embs)

    def truncate_embedding(self, embedding, target_dim, normalize=True):
        trunc = embedding[:target_dim].copy()
        if normalize:
            n = np.linalg.norm(trunc)
            if n > 0:
                trunc /= n
        return trunc


@pytest.fixture
def db_session():
    if not _DB_AVAILABLE:
        pytest.skip("Database not available")
    SessionLocal = get_session_factory()
    session = SessionLocal()
    session.begin_nested()  # SAVEPOINT
    yield session
    session.rollback()
    session.close()


@pytest.fixture
def mock_embedding():
    if not _DB_AVAILABLE:
        pytest.skip("Database not available")
    svc = MockEmbeddingService()
    with patch("jmfts_core.repositories.document.get_embedder", return_value=svc):
        yield svc


MARKDOWN = (
    "# Introduction\n\n"
    "The introduction has enough prose to survive the minimum chunk length.\n\n"
    "# Methods\n\n"
    "The methods section also has enough prose to survive the minimum length.\n\n"
    "# Results\n\n"
    "The results section rounds out a three-section document for the test.\n"
)

NO_LLM = {"summarize": False, "extract_facts": False}


def _ingest(session, content=MARKDOWN, usetype="markdown", **kwargs):
    return _run(execute_pipeline(session, content, usetype, pipeline_config=NO_LLM, **kwargs))


@requires_db
class TestAttemptsArePersisted:
    def test_one_attempt_per_stage_that_ran(self, db_session, mock_embedding):
        result = _ingest(db_session, title="Attempt log markdown")
        root = DocumentRepository(db_session).get(result.source_document_id)

        attempts = root.structured_content["attempts"]
        assert [a["task"] for a in attempts] == [s.stage for s in result.stages]
        assert [a["status"] for a in attempts] == [s.status for s in result.stages]
        assert all(a["scope_document_id"] == root.id for a in attempts)
        assert all(a["attempt"] == 1 for a in attempts)

    def test_response_body_is_unchanged(self, db_session, mock_embedding):
        """Behaviour must not otherwise change: the returned stage list is as it was."""
        result = _ingest(db_session)
        assert [s.stage for s in result.stages] == [
            "parse",
            "chunk",
            "summarize",
            "extract_facts",
            "bm25_index",
        ]
        assert result.stages[0].detail["sections"] == 3

    def test_existing_structured_content_keys_survive(self, db_session, mock_embedding):
        result = _ingest(db_session)
        root = DocumentRepository(db_session).get(result.source_document_id)
        assert root.structured_content["section_count"] == 3
        assert "attempts" in root.structured_content

    def test_timestamps_are_real_and_utc(self, db_session, mock_embedding):
        before = datetime.now(timezone.utc)
        result = _ingest(db_session)
        after = datetime.now(timezone.utc)
        root = DocumentRepository(db_session).get(result.source_document_id)

        for entry in root.structured_content["attempts"]:
            started = datetime.fromisoformat(entry["started_at"])
            finished = datetime.fromisoformat(entry["finished_at"])
            assert started.tzinfo is not None and finished.tzinfo is not None
            assert started.utcoffset() == timedelta(0)
            assert before <= started <= finished <= after

    def test_produced_records_the_child_ids(self, db_session, mock_embedding):
        result = _ingest(db_session)
        repo = DocumentRepository(db_session)
        root = repo.get(result.source_document_id)

        chunk = next(a for a in root.structured_content["attempts"] if a["task"] == "chunk")
        child_ids = sorted(c.id for c in repo.get_children(root.id, depth=1))
        assert sorted(chunk["produced"]["child_ids"]) == child_ids
        assert chunk["produced"]["node_count"] == len(child_ids)

        # A stage that creates no nodes gets a null, not an empty undo record.
        parse = next(a for a in root.structured_content["attempts"] if a["task"] == "parse")
        assert parse["produced"] is None

    def test_params_and_fingerprint_track_the_resolved_config(self, db_session, mock_embedding):
        default_run = _ingest(db_session)
        override_run = _run(
            execute_pipeline(
                db_session,
                MARKDOWN.replace("Introduction", "Preface"),
                "markdown",
                pipeline_config={**NO_LLM, "chunk": {"max_tokens": 500}},
            )
        )
        repo = DocumentRepository(db_session)

        def _chunk_attempt(result):
            root = repo.get(result.source_document_id)
            return next(a for a in root.structured_content["attempts"] if a["task"] == "chunk")

        default_chunk = _chunk_attempt(default_run)
        override_chunk = _chunk_attempt(override_run)
        assert default_chunk["params"]["max_tokens"] == 200
        assert override_chunk["params"]["max_tokens"] == 500
        assert default_chunk["param_fingerprint"] != override_chunk["param_fingerprint"]
        assert default_chunk["param_fingerprint"] == param_fingerprint(default_chunk["params"])

    def test_skipped_stages_carry_their_reason(self, db_session, mock_embedding):
        result = _ingest(db_session)
        root = DocumentRepository(db_session).get(result.source_document_id)
        skipped = [a for a in root.structured_content["attempts"] if a["status"] == "skipped"]
        assert skipped, "summarize and extract_facts were disabled and must be logged skipped"
        for entry in skipped:
            assert entry["detail"]["reason"]

    def test_structure_block_summarises_the_rung(self, db_session, mock_embedding):
        result = _ingest(db_session)
        root = DocumentRepository(db_session).get(result.source_document_id)
        structure = root.structured_content["structure"]

        # The document declares its own outline, so `declared` is the highest rung that
        # claimed anything, and node_count is what the structuring produced.
        assert structure["primary_rung"] == "declared"
        assert structure["source"] == "markdown_headings"
        assert structure["node_count"] >= 3
        # Not computable yet — omitted rather than written as a misleading zero.
        assert "coverage" not in structure
        assert "gap_regions" not in structure

    def test_headingless_text_is_not_called_declared(self, db_session, mock_embedding):
        result = _ingest(
            db_session,
            content=(
                "The quick brown fox jumped over the lazy dog. "
                "This paragraph has no headings anywhere in it at all. "
                "It should be recorded as flat chunking, not as a declared outline."
            ),
            usetype="raw",
        )
        root = DocumentRepository(db_session).get(result.source_document_id)
        structure = root.structured_content["structure"]
        assert structure["primary_rung"] == "flat"

        parse = next(a for a in root.structured_content["attempts"] if a["task"] == "parse")
        assert parse["rung"] is None


@requires_db
class TestReIngestAppends:
    def test_re_ingest_appends_rather_than_replaces(self, db_session, mock_embedding):
        first = _ingest(db_session, title="Appended log")
        repo = DocumentRepository(db_session)
        root = repo.get(first.source_document_id)
        first_log = list(root.structured_content["attempts"])
        assert first_log

        second = _ingest(db_session, title="Appended log")
        assert second.was_existing is True
        assert second.source_document_id == first.source_document_id

        db_session.refresh(root)
        second_log = root.structured_content["attempts"]
        assert len(second_log) == len(first_log) + 1
        assert second_log[: len(first_log)] == first_log
        assert second_log[-1]["task"] == "idempotency"
        assert second_log[-1]["status"] == "skipped"
        assert second_log[-1]["detail"]["reason"]

    def test_repeat_of_the_same_task_increments_the_attempt_counter(
        self, db_session, mock_embedding
    ):
        first = _ingest(db_session, title="Counted log")
        _ingest(db_session, title="Counted log")
        third = _ingest(db_session, title="Counted log")

        root = DocumentRepository(db_session).get(first.source_document_id)
        db_session.refresh(root)
        idempotency = [a for a in root.structured_content["attempts"] if a["task"] == "idempotency"]
        assert [a["attempt"] for a in idempotency] == [1, 2]
        assert third.was_existing is True

    def test_re_ingest_does_not_disturb_the_first_run_keys(self, db_session, mock_embedding):
        first = _ingest(db_session, title="Preserved keys")
        _ingest(db_session, title="Preserved keys")

        root = DocumentRepository(db_session).get(first.source_document_id)
        db_session.refresh(root)
        assert root.structured_content["section_count"] == 3
        assert root.structured_content["structure"]["primary_rung"] == "declared"
