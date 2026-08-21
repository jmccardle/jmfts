"""``embed`` as a queued task, and the ordering that used to be free.

Before this, ``DocumentRepository.create(auto_embed=True)`` ran the model inline, so a
chunk had its vectors at the instant it existed and rollup could read a child's embedding
without anyone having to think about when it was written. Splitting the model call into its
own task removes that guarantee and replaces it with a different one — a chunk is created
``in_flight`` holding an ``embed``, so ``settle_node`` will not settle it, and its parent is
blocked on ``children`` until it does.

That substitution is what these tests are about. The vectors themselves are the embedding
service's business and are tested where it is; what is asserted here is WHEN they exist
relative to everything that reads them.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from jmfts_core.contracts.upload import UploadedFile
from jmfts_core.ingest_tasks import TASK_EMBED, TASK_SUMMARIZE
from jmfts_core.ingest_worker import IngestWorker
from jmfts_core.models.document import (
    Document,
    SETTLED_IN_FLIGHT,
    SETTLED_SETTLED,
)
from jmfts_core.models.task_queue import TASK_PENDING, WRITE_SELF, TaskQueue
from jmfts_core.models.token_embedding import TokenEmbedding
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.rollup_tasks import IngestRollupPlanner
from jmfts_core.services.ingest_service import IngestService
from jmfts_core.settling import settle_node
from jmfts_core.structure_tasks import EMBED_CHUNK_SPEC, USETYPE_CHUNK, USETYPE_SECTION
from tests.conftest import _borrowed_session, drain_ingest_queue

MARKDOWN = b"""# Retrieval

Late interaction scores each query token against every document token and sums the
maxima over the query. That is more expensive than a single dot product and it is why
the token vectors are stored at 256 dimensions rather than at the model's full width.

# Segmentation

PELT finds changepoints in a sequence rather than clusters in a set, so a container it
creates is a contiguous span of the document and the tree reads in the order the author
wrote it.
"""


def _upload(session) -> Document:
    response = IngestService(session).upload_file(
        UploadedFile(data=MARKDOWN, filename="notes.md", content_type="text/markdown")
    )
    return DocumentRepository(session).get(response.document_id)


def _worker(session, planner=None):
    from jmfts_core.settling import NO_ROLLUP

    session.commit()
    return IngestWorker(
        worker_id="test-embed-worker",
        session_factory=lambda: _borrowed_session(session),
        planner=planner if planner is not None else NO_ROLLUP,
    )


def _run_until_only_embed_is_left(session, worker) -> None:
    """Advance the queue to the instant after the structure rung and before any embedding.

    Written as a condition on the queue rather than as a fixed number of ``run_once``
    calls, so a change to how many tasks precede the rung does not turn this into a test
    about arithmetic.
    """
    for _ in range(20):
        pending = set(
            session.execute(
                select(TaskQueue.task_type).where(TaskQueue.status == TASK_PENDING)
            ).scalars()
        )
        if pending == {TASK_EMBED}:
            return
        assert worker.run_once(), "the queue went quiet before any embed task was enqueued"
    raise AssertionError("the queue never reached a state where only embed tasks remained")


def _nodes(session, root: Document, usetype: str) -> list[Document]:
    return list(
        session.execute(
            select(Document)
            .where(Document.path.contains([root.id]))
            .where(Document.usetype == usetype)
            .order_by(Document.id)
        )
        .scalars()
        .all()
    )


# ---------------------------------------------------------------------------
# What the structure rung now enqueues
# ---------------------------------------------------------------------------


class TestTheRungEnqueuesEmbedPerChunk:
    def test_one_embed_task_per_chunk_node(self, db_session):
        node = _upload(db_session)
        worker = _worker(db_session)
        _run_until_only_embed_is_left(db_session, worker)

        chunks = _nodes(db_session, node, USETYPE_CHUNK)
        queued = db_session.execute(
            select(TaskQueue).where(TaskQueue.task_type == TASK_EMBED)
        ).scalars()

        assert chunks
        assert sorted(t.scope_document_id for t in queued) == sorted(c.id for c in chunks)

    def test_the_write_mode_is_self_so_a_document_s_chunks_embed_in_parallel(self, db_session):
        """``claim_next`` conflicts a ``self`` task only with another ``self`` on the SAME
        node. Anything wider would serialise a document's chunks behind each other and give
        back the loop this split exists to remove."""
        _upload(db_session)
        worker = _worker(db_session)
        _run_until_only_embed_is_left(db_session, worker)

        modes = set(
            db_session.execute(
                select(TaskQueue.write_mode).where(TaskQueue.task_type == TASK_EMBED)
            ).scalars()
        )
        assert modes == {WRITE_SELF}

        # And they really are simultaneously claimable, which is the property the mode is
        # chosen FOR: two different workers hold two of this document's chunks at once.
        from jmfts_core.repositories.task_queue import TaskQueueRepository

        tasks = TaskQueueRepository(db_session)
        first = tasks.claim_next("worker-a")
        second = tasks.claim_next("worker-b")
        assert first is not None and second is not None
        assert first.id != second.id

    def test_the_chunk_is_in_flight_until_its_embed_runs(self, db_session):
        node = _upload(db_session)
        worker = _worker(db_session)
        _run_until_only_embed_is_left(db_session, worker)

        chunks = _nodes(db_session, node, USETYPE_CHUNK)
        assert chunks
        assert all(c.settled == SETTLED_IN_FLIGHT for c in chunks)
        assert all(c.embed is None for c in chunks)

    def test_a_section_holding_unembedded_chunks_is_in_flight_too(self, db_session):
        """``settled`` is recursive, so a container above in-flight chunks is not settled.

        This is not bookkeeping. ``settle_node`` reads a node's DIRECT children, so a
        section that claimed to be settled would let the file node above it roll up over
        grandchildren with no vectors — which is what the rollup test below measures.
        """
        node = _upload(db_session)
        worker = _worker(db_session)
        _run_until_only_embed_is_left(db_session, worker)

        sections = _nodes(db_session, node, USETYPE_SECTION)
        assert sections, "the markdown fixture declared no sections"
        assert all(s.settled == SETTLED_IN_FLIGHT for s in sections)

    def test_a_heading_with_nothing_under_it_is_settled_at_birth(self, db_session):
        """The case the always-settled rule used to protect, kept and made explicit.

        A container nothing will ever work on must not be left in flight: the walk only
        travels upward, so nothing would ever visit it, and the whole document would park.
        """
        empty_heading = b"# Nothing Follows\n\n# Something Follows\n\n" + b"Prose. " * 40
        response = IngestService(db_session).upload_file(
            UploadedFile(data=empty_heading, filename="sparse.md", content_type="text/markdown")
        )
        node = DocumentRepository(db_session).get(response.document_id)
        worker = _worker(db_session)
        _run_until_only_embed_is_left(db_session, worker)

        by_title = {s.title: s for s in _nodes(db_session, node, USETYPE_SECTION)}
        assert by_title["Nothing Follows"].settled == SETTLED_SETTLED
        assert by_title["Something Follows"].settled == SETTLED_IN_FLIGHT


# ---------------------------------------------------------------------------
# The ordering: nothing reads a vector before it is written
# ---------------------------------------------------------------------------


class TestPendingEmbedBlocksTheWalk:
    def test_the_file_node_cannot_settle_while_a_chunk_is_unembedded(self, db_session):
        """The whole substitution, in one assertion. `settle_node` is asked directly rather
        than through a drain, because what is under test is its VERDICT — `children`, the
        blocked-by that keeps the rollup planner from being called at all."""
        node = _upload(db_session)
        worker = _worker(db_session)
        _run_until_only_embed_is_left(db_session, worker)

        step = settle_node(db_session, node.id, IngestRollupPlanner())

        assert step.settled is False
        assert step.blocked_by == "children"
        assert step.enqueued_task_ids == (), "the rollup planner ran over unembedded children"

    def test_a_node_is_never_rolled_up_over_a_descendant_with_no_vector(self, db_session):
        """The invariant, checked after every single task the worker runs.

        It is per-NODE, not global. Two sections are independent subtrees, so the first
        one's ``summarize`` being queued while the second is still embedding is correct and
        is the parallelism this split is for. What must never happen is a ``summarize`` on
        a node that still has an unembedded chunk somewhere UNDER it — that task reads its
        children's text, and ``structure:semantic`` beside it reads their vectors.
        """
        _upload(db_session)
        worker = _worker(db_session, planner=IngestRollupPlanner())

        for _ in range(200):
            for task in db_session.execute(
                select(TaskQueue).where(TaskQueue.task_type == TASK_SUMMARIZE)
            ).scalars():
                blind = list(
                    db_session.execute(
                        select(Document.id)
                        .where(Document.path.contains([task.scope_document_id]))
                        .where(Document.usetype == USETYPE_CHUNK)
                        .where(Document.embed.is_(None))
                    ).scalars()
                )
                assert not blind, (
                    f"summarize is queued on {task.scope_document_id} while chunks "
                    f"{blind} under it have no vector"
                )
            if not worker.run_once():
                break
        else:
            raise AssertionError("the queue never went quiet")

    def test_everything_settles_once_the_queue_drains(self, db_session):
        node = _upload(db_session)
        drain_ingest_queue(db_session, planner=IngestRollupPlanner(), max_tasks=200)

        subtree = list(
            db_session.execute(select(Document).where(Document.path.contains([node.id])))
            .scalars()
            .all()
        )
        assert subtree
        assert all(d.settled == SETTLED_SETTLED for d in subtree)
        assert node.settled == SETTLED_SETTLED

    def test_every_chunk_ends_up_with_a_vector_and_token_rows(self, db_session):
        node = _upload(db_session)
        drain_ingest_queue(db_session, planner=IngestRollupPlanner(), max_tasks=200)

        chunks = _nodes(db_session, node, USETYPE_CHUNK)
        assert chunks
        for chunk in chunks:
            assert chunk.embed is not None
            stored = (
                db_session.execute(
                    select(TokenEmbedding).where(TokenEmbedding.document_id == chunk.id)
                )
                .scalars()
                .all()
            )
            assert stored, f"chunk {chunk.id} has a document vector and no token vectors"

    def test_a_section_now_gets_summarized(self, db_session):
        """A consequence of the chunks becoming in-flight, and a gap closed rather than a
        side effect tolerated.

        The settling walk only travels UPWARD. A section container is created settled with
        no work on it, so before this nothing ever visited one and no section ever got
        ``effective_content`` — the same defect ``run_structure_semantic`` names about its
        own containers. Its chunks now walk up through it.
        """
        node = _upload(db_session)
        drain_ingest_queue(db_session, planner=IngestRollupPlanner(), max_tasks=200)

        sections = _nodes(db_session, node, USETYPE_SECTION)
        assert sections, "the markdown fixture declared no sections"
        for section in sections:
            effective = (section.structured_content or {}).get("effective_content")
            assert effective, f"section {section.id} was never rolled up"
            assert section.embed is not None


# ---------------------------------------------------------------------------
# The handler itself
# ---------------------------------------------------------------------------


class TestTheEmbedHandler:
    def test_the_spec_the_rung_uses_asks_for_token_vectors(self):
        """A chunk is exactly the node the token/maxsim path exists for: a leaf, its text
        is its own, and the chunker bounded it to the token window."""
        assert EMBED_CHUNK_SPEC.task_type == TASK_EMBED
        assert EMBED_CHUNK_SPEC.write_mode == WRITE_SELF
        assert EMBED_CHUNK_SPEC.params == {"with_tokens": True}

    def test_a_node_with_no_content_raises_rather_than_completing(self, db_session):
        """Not a skip. The task was enqueued for text, and a node that has none is a chunk
        whose prose went missing — settling over it publishes a leaf no query can reach."""
        from jmfts_core.embed_tasks import run_embed

        docs = DocumentRepository(db_session)
        empty = docs.create(title="nothing", content=None, auto_embed=False)
        db_session.flush()

        task = TaskQueue(
            task_type=TASK_EMBED,
            scope_document_id=empty.id,
            write_mode=WRITE_SELF,
            params={"with_tokens": True},
        )
        with pytest.raises(ValueError, match="no content"):
            run_embed(db_session, task)

    def test_the_detail_records_what_produced_the_vectors(self, db_session):
        """``model`` and ``device`` in the attempt log are how a corpus embedded across a
        mixed fleet stays auditable — a remote embedder reports ``remote:<url>`` here."""
        node = _upload(db_session)
        drain_ingest_queue(db_session, max_tasks=200)

        chunk = _nodes(db_session, node, USETYPE_CHUNK)[0]
        attempt = next(e for e in chunk.structured_content["attempts"] if e["task"] == TASK_EMBED)
        assert attempt["status"] == "completed"
        assert attempt["detail"]["model"]
        assert attempt["detail"]["dims"] > 0
        assert attempt["detail"]["tokens_stored"] > 0
        assert attempt["detail"]["with_tokens"] is True
