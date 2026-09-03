"""Reading back what the queue built. ``SPRINT_JOBS.md`` 15.4 S4.

Two halves, tested differently. :func:`stage_results` is a pure function over attempt logs
and is tested directly, exhaustively, with no database. :func:`summarize_tree` is tested
against a real ingest — upload, drain, count — because what it is really asserting is that
the numbers a caller reads match the nodes that exist, and a hand-built tree would only
assert that the function can add up.
"""

from __future__ import annotations

from sqlalchemy import select

from jmfts_core.evidence import REGISTRY
from jmfts_core.ingest_summary import EFFECTIVE_CONTENT_KEY, stage_results, summarize_tree
from jmfts_core.models.document import Document
from jmfts_core.models.triple import Triple
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.evidence import EvidenceRepository
from jmfts_core.models.document import USETYPE_CHUNK, USETYPE_SEGMENT
from jmfts_core.rollup_tasks import IngestRollupPlanner
from jmfts_core.services.ingest_service import IngestService
from tests.conftest import drain_ingest_queue

# ---------------------------------------------------------------------------
# The stage rollup
# ---------------------------------------------------------------------------


class TestStageResults:
    def test_one_entry_per_task_not_per_attempt(self):
        """The reason this is a rollup: a five-hundred-chunk document has five hundred
        `embed` attempts and a response that listed them all would be a log."""
        results = stage_results(
            [
                [{"task": "probe", "status": "completed"}],
                [{"task": "embed", "status": "completed"}],
                [{"task": "embed", "status": "completed"}],
                [{"task": "embed", "status": "completed"}],
            ]
        )

        assert [r.stage for r in results] == ["probe", "embed"]
        assert results[1].detail == {"attempts": 3, "completed": 3}

    def test_the_worst_status_wins(self):
        """One failure among four hundred successes must not report as completed."""
        results = stage_results(
            [
                [{"task": "embed", "status": "completed"}],
                [{"task": "embed", "status": "failed", "error": "no model"}],
                [{"task": "embed", "status": "completed"}],
            ]
        )

        assert results[0].status == "failed"
        assert results[0].error == "no model"
        assert results[0].detail == {"attempts": 3, "completed": 2, "failed": 1}

    def test_a_skipped_task_outranks_a_completed_one_and_not_a_failed_one(self):
        assert stage_results([[{"task": "ocr", "status": "skipped"}]])[0].status == "skipped"
        assert (
            stage_results(
                [[{"task": "t", "status": "skipped"}, {"task": "t", "status": "completed"}]]
            )[0].status
            == "skipped"
        )
        assert (
            stage_results(
                [[{"task": "t", "status": "failed"}, {"task": "t", "status": "skipped"}]]
            )[0].status
            == "failed"
        )

    def test_order_is_first_appearance(self):
        results = stage_results(
            [
                [
                    {"task": "probe", "status": "completed"},
                    {"task": "extract:text", "status": "completed"},
                ]
            ]
        )
        assert [r.stage for r in results] == ["probe", "extract:text"]

    def test_the_first_error_is_the_one_reported(self):
        results = stage_results(
            [
                [{"task": "t", "status": "failed", "error": "first"}],
                [{"task": "t", "status": "failed", "error": "second"}],
            ]
        )
        assert results[0].error == "first"

    def test_a_malformed_entry_is_skipped_rather_than_crashing_the_response(self):
        """The attempt log is a JSONB blob. A row written by an older version, or by hand,
        must not make the whole report unreadable — but it contributes no count either."""
        results = stage_results(
            [["not a dict", {"task": 7, "status": "completed"}, {"task": "t", "status": "wat"}]]
        )
        assert results == []

    def test_no_attempts_is_no_stages(self):
        assert stage_results([]) == []
        assert stage_results([[]]) == []


# ---------------------------------------------------------------------------
# The tree
# ---------------------------------------------------------------------------


class TestSummarizeTree:
    def test_a_missing_node_is_none_rather_than_an_empty_summary(self, db_session):
        """A zero count means "the database says zero". A deleted root is a different
        answer and must not come back looking like an ingest that produced nothing."""
        assert summarize_tree(db_session, 10**9) is None

    def test_a_root_on_its_own_is_depth_one(self, db_session):
        node = DocumentRepository(db_session).create(title="alone", content="x", auto_embed=False)
        db_session.flush()

        summary = summarize_tree(db_session, node.id)

        assert summary.tree_depth == 1
        assert summary.message_count == 0
        assert summary.segment_count == 0

    def test_the_counts_match_the_nodes_a_real_ingest_created(self, db_session):
        """The assertion that matters. Every number is compared against a query for the
        nodes it claims to count, so a rename of a usetype fails here rather than silently
        reporting zero."""
        content = (
            "# One\n\n" + ("Alpha beta gamma delta. " * 40) + "\n\n# Two\n\nMore prose here.\n"
        )
        response = IngestService(db_session).store_text_as_file(content, filename="note")
        drain_ingest_queue(db_session, planner=IngestRollupPlanner())

        summary = summarize_tree(db_session, response.document_id)

        subtree = (
            db_session.execute(
                select(Document).where(Document.path.contains([response.document_id]))
            )
            .scalars()
            .all()
        )
        assert summary.message_count == sum(1 for n in subtree if n.usetype == USETYPE_CHUNK)
        assert summary.message_count > 0, "the ingest produced no chunks to count"
        assert summary.segment_count == sum(1 for n in subtree if n.usetype == USETYPE_SEGMENT)
        root = DocumentRepository(db_session).get(response.document_id)
        found = EvidenceRepository(db_session).read_many([n.id for n in [root, *subtree]])
        assert summary.summary_count == sum(
            1 for values in found.values() if values.get(EFFECTIVE_CONTENT_KEY) is not None
        )
        assert summary.title == root.title

    def test_depth_is_the_tree_that_exists_not_a_raptor_layer_count(self, db_session):
        """Path A reported 1 for a root with two hundred chunks under it, because the
        number was the RAPTOR layer count. Here a root with children is 2."""
        content = "Alpha beta gamma delta epsilon. " * 60
        response = IngestService(db_session).store_text_as_file(content, filename="note")
        drain_ingest_queue(db_session)

        summary = summarize_tree(db_session, response.document_id)

        assert summary.tree_depth >= 2

    def test_the_stages_are_the_tasks_that_really_ran(self, db_session):
        response = IngestService(db_session).store_text_as_file(
            "Alpha beta gamma. " * 30, filename="note"
        )
        drain_ingest_queue(db_session)

        summary = summarize_tree(db_session, response.document_id)

        names = [s.stage for s in summary.stages]
        assert names[0] == "probe"
        assert "extract:text" in names
        assert all(s.status in ("completed", "skipped") for s in summary.stages), names

    def test_triples_are_counted_by_source_document_across_the_subtree(self, db_session):
        """`extract:facts` does not exist yet (S6), so the triples are written directly.
        What is being asserted is the JOIN — a triple whose source is a CHUNK counts for
        the file node above it."""
        response = IngestService(db_session).store_text_as_file(
            "Alpha beta gamma. " * 30, filename="note"
        )
        drain_ingest_queue(db_session)
        chunk = (
            db_session.execute(
                select(Document)
                .where(Document.path.contains([response.document_id]))
                .where(Document.usetype == USETYPE_CHUNK)
                .limit(1)
            )
            .scalars()
            .one()
        )
        assert summarize_tree(db_session, response.document_id).triple_count == 0

        from jmfts_core.models.triple import FactType, Predicate

        predicate = Predicate(name="mentions")
        db_session.add(predicate)
        db_session.flush()
        db_session.add(
            Triple(
                subject_id=chunk.id,
                predicate_id=predicate.id,
                object_literal="beta",
                source_document_id=chunk.id,
                fact_type=FactType.static,
            )
        )
        db_session.flush()

        assert summarize_tree(db_session, response.document_id).triple_count == 1

    def test_the_effective_content_key_is_the_one_the_registry_declares(self):
        """The two-lists rule. This module names the key it counts; the evidence registry
        is where it is declared, and a rename must not leave this reporting zero."""
        assert EFFECTIVE_CONTENT_KEY in REGISTRY
        # The ROW name, since Phase 2b. This module queries `document_evidence` by name,
        # so what has to agree is the row it selects and the row the registry declares.
        assert REGISTRY[EFFECTIVE_CONTENT_KEY].store.row == EFFECTIVE_CONTENT_KEY
