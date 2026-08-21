"""PELT rollup: segmentation and `effective_content`. `INGEST_SPEC.md` 5.4 and 11.4.

Three layers, tested apart because they fail apart.

`TestThePlanner` is the scheduling: which of the two tasks a node is offered, and that
6.1's diff stops it being offered twice. `TestSegmentation` is the shape PELT produces, and
most of it is 11.4's termination rules — the degenerate cases are the whole risk of this
feature, so they get more tests than the happy path. `TestEffectiveContent` is the
represent-before-interpret rule.

The first two build their embeddings by hand. Synthetic orthogonal vectors make a
changepoint an arithmetic fact rather than something the embedding model has to be
persuaded to produce, so a termination rule that broke would fail here for its own reason
rather than because a fixture drifted. `TestEndToEnd` is the one that runs the real model
through the real queue.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from sqlalchemy import select

from jmfts_core.contracts.upload import UploadedFile
from jmfts_core.ingest_tasks import (
    TASK_STRUCTURE_SEMANTIC,
    TASK_SUMMARIZE,
    TASK_SUMMARIZE_LLM,
)
from jmfts_core.models.document import SETTLED_IN_FLIGHT, SETTLED_SETTLED, Document
from jmfts_core.models.task_queue import TaskQueue
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.rollup_tasks import (
    METHOD_CONCATENATED,
    METHOD_LLM_SUMMARY,
    RUNG_SEMANTIC,
    SOURCE_PELT,
    USETYPE_SEGMENT,
    IngestRollupPlanner,
    child_ids,
    effective_text,
    run_structure_semantic,
    run_summarize,
    run_summarize_llm,
)
from jmfts_core.services.ingest_service import IngestService
from tests.conftest import drain_ingest_queue

pytest.importorskip("ruptures")

EMBED_DIM = 768


@dataclass
class _Task:
    """A claimed queue row, without the queue. The handlers read only these three."""

    scope_document_id: int
    params: dict = field(default_factory=dict)
    task_type: str = TASK_STRUCTURE_SEMANTIC


def _vector(axis: int) -> list[float]:
    """A unit vector on one axis. Two different axes are a maximal changepoint."""
    vec = [0.0] * EMBED_DIM
    vec[axis % EMBED_DIM] = 1.0
    return vec


def _configure_llm(monkeypatch, module) -> None:
    """Point ``module``'s settings at an LLM endpoint.

    The shipped default names none — JMFTS does not include an LLM — so `run_summarize_llm`
    reports `skipped` before it reaches the model. A test that replaces `summarize_span` and
    expects it to be called has to configure an endpoint first, even though nothing will
    connect to it.
    """
    from jmfts_core.config import get_settings

    settings = get_settings().model_copy()
    settings.llm_base_url = "http://llm.invalid:8000"
    settings.llm_model = "a-model-that-is-never-called"
    monkeypatch.setattr(module, "get_settings", lambda: settings)


def _tree(session, axes: list[int], *, text: str = "some retrievable body text") -> Document:
    """A parent with one embedded child per entry in ``axes``, in that order."""
    repo = DocumentRepository(session)
    parent = repo.create(title="parent", content=None, auto_embed=False, settled=SETTLED_SETTLED)
    for index, axis in enumerate(axes):
        child = repo.create(
            title=f"child {index}",
            content=f"{text} {index}",
            parent_id=parent.id,
            auto_embed=False,
            sequential=True,
            settled=SETTLED_SETTLED,
        )
        child.embed = _vector(axis)
    session.flush()
    return parent


SEGMENT_PARAMS = {"penalty": 0.5, "min_segment": 2}


def _children(session, parent_id: int) -> list[Document]:
    return [session.get(Document, cid) for cid in child_ids(session, parent_id)]


# ---------------------------------------------------------------------------
# The planner
# ---------------------------------------------------------------------------


class TestThePlanner:
    def test_a_leaf_is_offered_nothing(self, db_session):
        repo = DocumentRepository(db_session)
        leaf = repo.create(title="leaf", content="text", auto_embed=False)
        db_session.flush()
        assert IngestRollupPlanner()(db_session, leaf) == ()

    def test_a_narrow_node_is_offered_effective_content(self, db_session):
        parent = _tree(db_session, [0, 1, 2])
        specs = IngestRollupPlanner()(db_session, parent)
        assert [s.task_type for s in specs] == [TASK_SUMMARIZE]
        assert specs[0].write_mode == "self"

    def test_a_wide_node_is_offered_segmentation_first(self, db_session):
        parent = _tree(db_session, list(range(20)))
        specs = IngestRollupPlanner()(db_session, parent)
        assert [s.task_type for s in specs] == [TASK_STRUCTURE_SEMANTIC]
        # `subtree`, not `children`: the upward direction moves containers that have
        # children of their own, and 5.3 reserves a path rewrite for `subtree`.
        assert specs[0].write_mode == "subtree"

    def test_the_trigger_measurement_is_in_the_params(self, db_session):
        """11.4's tension with 6.1's diff, and the resolution.

        PELT's real input is the child set. If the params did not carry a measurement of
        it, a node segmented once and then given more children would be over the limit
        again with an identical fingerprint, and the diff would refuse to re-run it.
        """
        parent = _tree(db_session, list(range(20)))
        specs = IngestRollupPlanner()(db_session, parent)
        assert specs[0].params["child_count"] == 20

    def test_a_wide_node_whose_segmentation_ran_falls_through(self, db_session):
        """The reason the diff is applied inside the planner rather than around it.

        A node that could not be segmented must still get `effective_content`. Offering it
        the same segmentation again would loop, and offering it nothing would leave it
        without an embedding for a reason that is no longer true.
        """
        parent = _tree(db_session, list(range(20)))
        planner = IngestRollupPlanner()
        segment = planner(db_session, parent)[0]
        _record_attempt(db_session, parent, TASK_STRUCTURE_SEMANTIC, segment.params)

        specs = planner(db_session, parent)
        assert [s.task_type for s in specs] == [TASK_SUMMARIZE]

    def test_a_node_that_has_done_both_settles(self, db_session):
        parent = _tree(db_session, list(range(20)))
        planner = IngestRollupPlanner()
        segment = planner(db_session, parent)[0]
        _record_attempt(db_session, parent, TASK_STRUCTURE_SEMANTIC, segment.params)
        summarize = planner(db_session, parent)[0]
        _record_attempt(db_session, parent, TASK_SUMMARIZE, summarize.params)

        assert planner(db_session, parent) == ()

    def test_options_are_inherited_from_the_file_node(self, db_session):
        """A segment created three levels down is rolled up the way the UPLOAD asked.

        Options live on the file node, so a node that records none reads the nearest
        ancestor that does — not the profile defaults as they stand today.
        """
        parent = _tree(db_session, [0, 1, 2])
        parent.structured_content = {"options": {"rollup": {"max_children": 2}}}
        db_session.flush()

        specs = IngestRollupPlanner()(db_session, parent)
        assert [s.task_type for s in specs] == [TASK_STRUCTURE_SEMANTIC]


def _record_attempt(session, node: Document, task: str, params: dict) -> None:
    """Append the attempt a completed task would have left, so the diff can see it."""
    from jmfts_core.contracts.attempt import param_fingerprint

    structured = dict(node.structured_content or {})
    attempts = list(structured.get("attempts", []))
    attempts.append(
        {"task": task, "status": "completed", "param_fingerprint": param_fingerprint(params)}
    )
    structured["attempts"] = attempts
    node.structured_content = structured
    session.flush()


# ---------------------------------------------------------------------------
# Segmentation, and 11.4's termination rules
# ---------------------------------------------------------------------------


class TestSegmentation:
    def test_it_creates_a_container_per_segment_and_reparents(self, db_session):
        # Two topics, six children: three on one axis, three on another.
        parent = _tree(db_session, [0, 0, 0, 1, 1, 1])
        outcome = run_structure_semantic(db_session, _Task(parent.id, SEGMENT_PARAMS))

        assert outcome.detail["segmented"] is True
        assert outcome.rung == RUNG_SEMANTIC
        containers = _children(db_session, parent.id)
        assert len(containers) == 2
        assert all(c.usetype == USETYPE_SEGMENT for c in containers)
        assert [len(child_ids(db_session, c.id)) for c in containers] == [3, 3]

    def test_the_containers_carry_the_rung_and_no_title(self, db_session):
        """The document does not name this span, so nothing here names it either."""
        parent = _tree(db_session, [0, 0, 0, 1, 1, 1])
        run_structure_semantic(db_session, _Task(parent.id, SEGMENT_PARAMS))

        container = _children(db_session, parent.id)[0]
        assert container.title is None
        assert container.content is None
        assert container.structured_content["structure"]["primary_rung"] == RUNG_SEMANTIC
        assert container.structured_content["structure"]["source"] == SOURCE_PELT

    def test_document_order_survives_the_move(self, db_session):
        parent = _tree(db_session, [0, 0, 0, 1, 1, 1])
        before = [d.title for d in _children(db_session, parent.id)]
        run_structure_semantic(db_session, _Task(parent.id, SEGMENT_PARAMS))

        after = []
        for container in _children(db_session, parent.id):
            after.extend(d.title for d in _children(db_session, container.id))
        assert after == before

    def test_each_container_is_in_flight_and_carries_its_own_rollup(self, db_session):
        """The walk only travels upward, so a container created settled and given no work
        would never be visited and would never get an embedding."""
        parent = _tree(db_session, [0, 0, 0, 1, 1, 1])
        run_structure_semantic(db_session, _Task(parent.id, SEGMENT_PARAMS))

        for container in _children(db_session, parent.id):
            assert container.settled == SETTLED_IN_FLIGHT
            queued = (
                db_session.execute(
                    select(TaskQueue).where(TaskQueue.scope_document_id == container.id)
                )
                .scalars()
                .all()
            )
            assert [t.task_type for t in queued] == [TASK_SUMMARIZE]

    # -- 11.4's termination rules -------------------------------------------

    def test_one_segment_covering_everything_creates_nothing(self, db_session):
        """Rule 1, and the reason the whole feature terminates.

        Every child on one axis has no changepoint in it. A container for the single
        segment would give the next walk the same children under a new parent, which
        segments the same way — depth growing forever, width never falling.
        """
        parent = _tree(db_session, [0] * 20)
        outcome = run_structure_semantic(db_session, _Task(parent.id, SEGMENT_PARAMS))

        assert outcome.detail["segmented"] is False
        assert "no changepoint" in outcome.detail["reason"]
        assert len(_children(db_session, parent.id)) == 20
        assert not any(c.usetype == USETYPE_SEGMENT for c in _children(db_session, parent.id))

    def test_a_single_child_segment_gets_no_container(self, db_session):
        """Rule 2. One child under a new node adds a level and no information."""
        parent = _tree(db_session, [0, 0, 0, 1, 2, 2, 2])
        outcome = run_structure_semantic(
            db_session, _Task(parent.id, {"penalty": 0.1, "min_segment": 1})
        )

        assert outcome.detail["segmented"] is True
        children = _children(db_session, parent.id)
        containers = [c for c in children if c.usetype == USETYPE_SEGMENT]
        loners = [c for c in children if c.usetype != USETYPE_SEGMENT]
        assert containers
        assert all(len(child_ids(db_session, c.id)) >= 2 for c in containers)
        # The child PELT put in a segment of its own stayed where it was.
        assert loners

    def test_a_node_with_one_child_is_not_a_sequence(self, db_session):
        parent = _tree(db_session, [0])
        outcome = run_structure_semantic(db_session, _Task(parent.id, SEGMENT_PARAMS))
        assert outcome.detail["segmented"] is False
        assert "fewer than two children" in outcome.detail["reason"]

    def test_unembedded_children_are_refused_with_a_count(self, db_session):
        """Rule 3, and the honest limit of this pass.

        Section containers written by the structure rungs settle at creation and are never
        embedded. Segmenting the embedded subset would build a tree over a different set of
        children than the node has, so the node stays wide and says why.
        """
        parent = _tree(db_session, [0, 0, 0, 1, 1, 1])
        _children(db_session, parent.id)[2].embed = None
        db_session.flush()

        outcome = run_structure_semantic(db_session, _Task(parent.id, SEGMENT_PARAMS))
        assert outcome.detail["segmented"] is False
        assert outcome.detail["unembedded"] == 1
        assert outcome.detail["children"] == 6
        assert len(_children(db_session, parent.id)) == 6

    def test_the_detail_records_the_shape_it_produced(self, db_session):
        parent = _tree(db_session, [0, 0, 0, 1, 1, 1])
        outcome = run_structure_semantic(db_session, _Task(parent.id, SEGMENT_PARAMS))

        assert outcome.detail["children_before"] == 6
        assert outcome.detail["children_after"] == 2
        assert outcome.detail["sizes"] == [3, 3]
        assert outcome.produced["node_count"] == 2


# ---------------------------------------------------------------------------
# effective_content
# ---------------------------------------------------------------------------


class TestEffectiveContent:
    def test_a_short_node_is_concatenated_not_summarized(self, db_session):
        """The represent-before-interpret rule, at the point it applies."""
        parent = _tree(db_session, [0, 1], text="a short passage about retrieval")
        outcome = run_summarize(db_session, _Task(parent.id, {}, TASK_SUMMARIZE))

        assert outcome.detail["method"] == METHOD_CONCATENATED
        record = db_session.get(Document, parent.id).structured_content["effective_content"]
        assert record["method"] == METHOD_CONCATENATED
        assert record["source_children"] == 2
        # A concatenation is derivable from the subtree, so it is not written down.
        assert "text" not in record

    def test_it_writes_an_embedding_and_no_content(self, db_session):
        parent = _tree(db_session, [0, 1])
        run_summarize(db_session, _Task(parent.id, {}, TASK_SUMMARIZE))

        node = db_session.get(Document, parent.id)
        assert node.embed is not None
        assert node.content is None

    def test_the_deciding_token_count_is_recorded(self, db_session):
        parent = _tree(db_session, [0, 1])
        outcome = run_summarize(db_session, _Task(parent.id, {}, TASK_SUMMARIZE))
        assert outcome.detail["tokens"] > 0
        assert outcome.detail["tokens"] < outcome.detail["window"]

    def test_a_leaf_records_a_skip_rather_than_a_failure(self, db_session):
        repo = DocumentRepository(db_session)
        leaf = repo.create(title="leaf", content="text", auto_embed=False)
        db_session.flush()

        outcome = run_summarize(db_session, _Task(leaf.id, {}, TASK_SUMMARIZE))
        assert outcome.status == "skipped"
        assert "no children" in outcome.detail["reason"]

    def test_children_with_no_text_are_a_skip_with_the_count(self, db_session):
        repo = DocumentRepository(db_session)
        parent = repo.create(title="parent", auto_embed=False)
        repo.create(title="empty", content=None, parent_id=parent.id, auto_embed=False)
        db_session.flush()

        outcome = run_summarize(db_session, _Task(parent.id, {}, TASK_SUMMARIZE))
        assert outcome.status == "skipped"
        assert outcome.detail["children"] == 1

    def test_effective_text_reads_down_to_the_leaves(self, db_session):
        """A container in the middle of the tree stands for the text below it."""
        repo = DocumentRepository(db_session)
        parent = repo.create(title="parent", auto_embed=False)
        middle = repo.create(
            title="middle", content=None, parent_id=parent.id, auto_embed=False, sequential=True
        )
        repo.create(
            title="leaf a",
            content="first thing said",
            parent_id=middle.id,
            auto_embed=False,
            sequential=True,
        )
        repo.create(
            title="leaf b",
            content="second thing said",
            parent_id=middle.id,
            auto_embed=False,
            sequential=True,
        )
        db_session.flush()

        text = effective_text(db_session, parent.id, own_content=False)
        assert text == "first thing said\n\nsecond thing said"

    def test_a_stored_summary_stands_in_for_the_subtree(self, db_session):
        """Which is why a summary IS written down where a concatenation is not."""
        repo = DocumentRepository(db_session)
        parent = repo.create(title="parent", auto_embed=False)
        middle = repo.create(title="middle", content=None, parent_id=parent.id, auto_embed=False)
        repo.create(
            title="leaf", content="the long original", parent_id=middle.id, auto_embed=False
        )
        middle.structured_content = {
            "effective_content": {"method": METHOD_LLM_SUMMARY, "text": "the short summary"}
        }
        db_session.flush()

        assert effective_text(db_session, parent.id, own_content=False) == "the short summary"

    def test_an_oversized_node_is_deferred_to_the_llm_task(self, db_session):
        """`summarize` does not call an LLM any more; it establishes that one is NEEDED and
        hands the node to `summarize:llm`, which carries the LLM pool's badge. The split
        exists because every summarize embeds but only some need a completion, and which
        ones is not knowable until the children are concatenated and tokenised."""
        long_text = "Retrieval quality is measured against a baseline. " * 900
        parent = _tree(db_session, [0, 1], text=long_text)

        outcome = run_summarize(db_session, _Task(parent.id, {}, TASK_SUMMARIZE))

        assert outcome.status == "completed"
        assert outcome.detail["deferred_to"] == TASK_SUMMARIZE_LLM
        assert outcome.detail["tokens"] > outcome.detail["window"]
        # The node has NOT been given effective_content yet — the deferral is the product.
        assert "effective_content" not in (
            db_session.get(Document, parent.id).structured_content or {}
        )
        queued = db_session.query(TaskQueue).filter_by(scope_document_id=parent.id).all()
        assert [t.task_type for t in queued] == [TASK_SUMMARIZE_LLM]

    def test_the_llm_task_summarizes_and_embeds(self, db_session, monkeypatch):
        """The path over the embedding window. The model is replaced, not called."""
        import jmfts_core.rollup_tasks as rollup

        _configure_llm(monkeypatch, rollup)
        long_text = "Retrieval quality is measured against a baseline. " * 900
        parent = _tree(db_session, [0, 1], text=long_text)

        calls = []

        def _fake(text, settings, model):
            calls.append((len(text), model))
            return "A short summary of a long span about retrieval quality."

        monkeypatch.setattr(rollup, "summarize_span", _fake)
        outcome = run_summarize_llm(db_session, _Task(parent.id, {}, TASK_SUMMARIZE_LLM))

        assert len(calls) == 1
        assert outcome.detail["method"] == METHOD_LLM_SUMMARY
        assert outcome.detail["input_tokens"] > outcome.detail["window"]
        record = db_session.get(Document, parent.id).structured_content["effective_content"]
        assert record["text"] == "A short summary of a long span about retrieval quality."

    def test_a_model_that_does_not_summarize_raises(self, db_session, monkeypatch):
        """Storing an unembeddable summary would leave the node claiming an
        `effective_content` that nothing can retrieve."""
        import jmfts_core.rollup_tasks as rollup

        _configure_llm(monkeypatch, rollup)
        long_text = "Retrieval quality is measured against a baseline. " * 900
        parent = _tree(db_session, [0, 1], text=long_text)
        monkeypatch.setattr(rollup, "summarize_span", lambda text, settings, model: long_text)

        with pytest.raises(ValueError, match="did not summarize"):
            run_summarize_llm(db_session, _Task(parent.id, {}, TASK_SUMMARIZE_LLM))

    def test_no_llm_configured_is_a_skip_with_the_measurement(self, db_session, monkeypatch):
        """11.4 §4: a missing summary is never silent."""
        import jmfts_core.rollup_tasks as rollup
        from jmfts_core.config import get_settings

        long_text = "Retrieval quality is measured against a baseline. " * 900
        parent = _tree(db_session, [0, 1], text=long_text)

        settings = get_settings().model_copy()
        settings.llm_base_url = ""
        settings.ensonet_url = ""
        monkeypatch.setattr(rollup, "get_settings", lambda: settings)

        outcome = run_summarize_llm(db_session, _Task(parent.id, {}, TASK_SUMMARIZE_LLM))
        assert outcome.status == "skipped"
        assert "no LLM is configured" in outcome.detail["reason"]
        assert outcome.detail["tokens"] > outcome.detail["window"]


# ---------------------------------------------------------------------------
# Through the real queue
# ---------------------------------------------------------------------------


#: Two topics with nothing in common, so the changepoint between them is in the embeddings
#: rather than in the fixture's hopes. Long enough, at the chunking options below, to make
#: the file node wider than `max_children`.
TWO_TOPIC_TEXT = (
    " ".join(
        f"Late interaction scores every query token against every document token, and "
        f"retrieval recall improved by {n} points against the sparse baseline."
        for n in range(1, 14)
    )
    + "\n\n"
    + " ".join(
        f"Braise the shallots in butter for {n} minutes, then deglaze the pan with white "
        f"wine and reduce the sauce until it coats the back of a spoon."
        for n in range(1, 14)
    )
)

INGEST_OPTIONS = {
    "structure": {"max_tokens": 20, "min_chunk_length": 5},
    "rollup": {"max_children": 4, "min_segment": 2, "penalty": 0.5},
}


class TestEndToEnd:
    @pytest.fixture
    def node(self, db_session) -> Document:
        response = IngestService(db_session).upload_file(
            UploadedFile(
                data=TWO_TOPIC_TEXT.encode("utf-8"),
                filename="notes.txt",
                content_type="text/plain",
            ),
            options=INGEST_OPTIONS,
        )
        drain_ingest_queue(db_session, planner=IngestRollupPlanner(), max_tasks=300)
        return DocumentRepository(db_session).get(response.document_id)

    def test_the_wide_flat_node_got_a_level_deeper(self, node, db_session):
        """The shape 11.3 hands 11.4: a `.txt` file is one untitled region of chunks."""
        children = _children(db_session, node.id)
        assert len(children) <= INGEST_OPTIONS["rollup"]["max_children"]
        assert any(c.usetype == USETYPE_SEGMENT for c in children)

    def test_every_node_settled(self, node, db_session):
        assert node.settled == SETTLED_SETTLED
        rows = (
            db_session.execute(
                select(Document).where(Document.path.contains([node.id]))  # type: ignore[arg-type]
            )
            .scalars()
            .all()
        )
        assert rows
        assert all(row.settled == SETTLED_SETTLED for row in rows)

    def test_the_segments_carry_effective_content(self, node, db_session):
        segments = [c for c in _children(db_session, node.id) if c.usetype == USETYPE_SEGMENT]
        assert segments
        for segment in segments:
            assert segment.embed is not None
            assert segment.content is None
            assert segment.structured_content["effective_content"]["method"] == METHOD_CONCATENATED

    def test_the_walk_stopped(self, node, db_session):
        """Termination, end to end: nothing is left pending anywhere under the file node."""
        pending = db_session.execute(
            select(TaskQueue).where(TaskQueue.status.in_(["pending", "claimed", "running"]))
        ).scalars()
        assert [t.task_type for t in pending] == []
