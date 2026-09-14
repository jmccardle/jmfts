"""Regression tests for the ingestion pipeline (Kanban #255).

Tests: parent_id assignment, usetype correctness, BM25 auto-indexing after
ingest, and entity exclusion from BM25 indexes.

Tier 2: Integration tests — real DB (savepoint rollback), mocked LLM + embedding.
"""

import asyncio
from unittest.mock import patch

import numpy as np
import pytest
from sqlalchemy import select
from sqlalchemy import text as sa_text

from jmfts_client.contracts.ingest import IngestRequest
from jmfts_core.ingest_tasks import TASK_INDEX_BM25
from jmfts_core.repositories.task_queue import TaskQueueRepository
from jmfts_core.models.document import USETYPE_FILE
from jmfts_core.services.ingest_service import IngestService

# ---------------------------------------------------------------------------
# DB availability check (same pattern as test_raptor.py)
# ---------------------------------------------------------------------------

try:
    from jmfts_core.database import get_engine
    from jmfts_core.repositories.document import DocumentRepository
    from jmfts_core.repositories.search import SearchRepository
    from jmfts_core.models.search_index import SearchIndexEntry
    from jmfts_core.embedding import EmbeddingResult, TokenEmbeddingResult

    _engine = get_engine()
    with _engine.connect() as _conn:
        _conn.execute(sa_text("SELECT 1"))
    _DB_AVAILABLE = True
except Exception:
    _DB_AVAILABLE = False

requires_db = pytest.mark.skipif(not _DB_AVAILABLE, reason="Database not available")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _attempt_detail(session, node_id, task):
    """The handler's own detail for ``task``, from the node's durable attempt log."""
    repo = DocumentRepository(session)
    entries = [e for e in repo.attempt_log(repo.get(node_id)) if e["task"] == task]
    assert len(entries) == 1, f"{task}: {entries}"
    return entries[0]["detail"]


def _ingest(session, content, usetype, **kwargs):
    """One ingest through the real service. ``SPRINT_JOBS.md`` 15.4 S5.

    These tests used to call ``execute_pipeline`` directly. ``markdown`` and ``raw`` are
    served by the ingest queue now, so the entry point is the service — which stores the
    content as a file node, drains that document's tasks, and returns the same response.
    The claims below are unchanged: chunks parent onto the root, carry ``usetype='chunk'``,
    and have the root in their ``path``.
    """
    return _run(
        IngestService(session).ingest_content(
            IngestRequest(content=content, usetype=usetype, **kwargs)
        )
    )


class MockEmbeddingService:
    """Deterministic mock: hashes text to produce a reproducible embedding."""

    def __init__(self, dim=768):
        self.dim = dim

    def embed_text(self, text, normalize=True, prefix=""):
        rng = np.random.default_rng(hash(text) % (2**31))
        vec = rng.standard_normal(self.dim).astype(np.float32)
        vec /= np.linalg.norm(vec)
        return vec

    def embed_with_tokens(self, text, top_percent=0.35, token_selector=None, prefix=""):
        doc_emb = self.embed_text(text)
        words = (text.split() or ["empty"])[:3]
        token_embs = []
        for i, w in enumerate(words):
            rng = np.random.default_rng(hash(f"{text}_{i}") % (2**31))
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


# THE LOCAL `db_session` FIXTURE WAS HERE, and it leaked. It was a plain session with a
# nested SAVEPOINT, so `session.commit()` committed for real — which nothing in this file
# used to do, because it called `execute_pipeline` and that only flushes. SPRINT_JOBS.md
# 15.4 S5 routed these tests through `IngestService`, which commits the file node before
# the queue can see it, and the committed rows then outlived the test and were claimed by
# whatever ran next.
#
# `tests/conftest.py`'s `db_session` is the one that contains a commit: it binds the
# session to a connection-level transaction with `join_transaction_mode="create_savepoint"`,
# so an endpoint's commit releases a savepoint inside a transaction the fixture rolls back.


@pytest.fixture
def mock_embedding():
    if not _DB_AVAILABLE:
        pytest.skip("Database not available")
    svc = MockEmbeddingService()
    with patch("jmfts_core.repositories.document.get_embedder", return_value=svc):
        yield svc


# ---------------------------------------------------------------------------
# Tests: parent_id and usetype assignment
# ---------------------------------------------------------------------------


@requires_db
class TestIngestionParentIdAndUsetype:
    """Verify that chunks created during ingestion have correct parent_id and usetype."""

    def test_markdown_chunks_have_correct_parent_id(self, db_session, mock_embedding):
        """Markdown ingest produces chunks whose parent_id is the root document."""
        content = (
            "# Section One\n\n"
            "First section has enough text to be a meaningful chunk.\n\n"
            "# Section Two\n\n"
            "Second section also has enough text to be a meaningful chunk.\n\n"
            "# Section Three\n\n"
            "Third section with additional text for chunking purposes.\n"
        )

        result = _ingest(db_session, content, "markdown", title="Parent ID Test")

        root_id = result.source_document_id
        repo = DocumentRepository(db_session)
        children = repo.get_children(root_id, depth=1, limit=100)

        assert len(children) >= 3, f"Expected >= 3 children, got {len(children)}"
        for child in children:
            assert (
                child.parent_id == root_id
            ), f"Child {child.id} has parent_id={child.parent_id}, expected {root_id}"

    def test_markdown_leaves_have_chunk_usetype(self, db_session, mock_embedding):
        """The LEAVES carry `usetype='chunk'`.

        WAS "the root's direct children". Path A chunked a markdown document flat under
        its root; the queue's `structure:declared` rung builds the tree the headings
        describe — a `section` per declared heading, with the chunks under it — so the
        children of the root are sections and the chunks are one level further down.
        `USETYPE_CHUNK` is still what a retrievable leaf is.
        """
        content = (
            "# Alpha\n\nAlpha section with enough content to chunk properly.\n\n"
            "# Beta\n\nBeta section with enough content to chunk properly.\n"
        )

        result = _ingest(db_session, content, "markdown", title="Usetype Test")

        repo = DocumentRepository(db_session)
        subtree = [d for d in repo.get_subtree(result.source_document_id, include_in_flight=True)]
        parents = {d.parent_id for d in subtree}
        leaves = [d for d in subtree if d.id not in parents and d.id != result.source_document_id]
        assert leaves, "the rung produced no leaves"
        for leaf in leaves:
            assert (
                leaf.usetype == "chunk"
            ), f"Leaf {leaf.id} has usetype='{leaf.usetype}', expected 'chunk'"

    def test_raw_root_is_a_file_node(self, db_session, mock_embedding):
        """WAS `usetype == "raw"`. SPRINT_JOBS.md 15.4 S5 moved the usetype onto the ingest
        queue, where the content string is stored as bytes and the root is the `file` node
        that holds them — which is what it really is. The response still reports `raw` as
        the usetype that was ASKED for."""
        content = (
            "The quick brown fox jumped over the lazy dog. "
            "This is a test of the raw text ingestion pipeline. "
            "It should create a root document holding the bytes it was sent."
        )

        result = _ingest(db_session, content, "raw", title="Usetype Raw Test")

        repo = DocumentRepository(db_session)
        root = repo.get(result.source_document_id)
        assert root.usetype == USETYPE_FILE
        assert result.usetype == "raw"

    def test_chunk_path_includes_root(self, db_session, mock_embedding):
        """Chunk documents should have root_id in their path array."""
        content = "# Heading\n\n" "Some paragraph text long enough to be ingested as a chunk.\n"

        result = _ingest(db_session, content, "markdown", title="Path Test")

        root_id = result.source_document_id
        repo = DocumentRepository(db_session)
        children = repo.get_children(root_id, depth=1, limit=100)
        assert len(children) >= 1
        for child in children:
            assert root_id in (
                child.path or []
            ), f"Child {child.id} path={child.path} does not contain root_id={root_id}"


# ---------------------------------------------------------------------------
# Tests: BM25 auto-indexing after ingestion
# ---------------------------------------------------------------------------


@requires_db
class TestBM25MembershipFollowsTheTree:
    """``INGEST_SPEC.md`` 11.5, arrived at by ``SPRINT_JOBS.md`` 15.4 S5.

    WAS ``TestBM25AutoIndexing``, and the rule it asserted is the one 11.5 removes. Path A
    ran ``_index_subtree_bm25`` at the end of every ingest and indexed unconditionally into
    ``"default"``, creating that index if it was absent — so every ingestion joined one
    corpus whether or not anybody asked. The queue's ``index:bm25`` task reads the file
    node's ``path`` instead and joins every index whose registered root is the node or one
    of its ancestors.

    IT IS A ROLLUP RULE AND NOT A ``TASK_ROWS`` ROW SINCE ``SPRINT_0_6_0.md`` Block B step
    7 (``jmfts_core.index_tasks``), which changes when it fires and not what it joins.
    Everything below is about what it joins.

    So a file uploaded into a folder somebody has indexed joins that index, and a file
    uploaded anywhere else joins nothing. The second half is the deliberate part: the
    document is findable by vector search and not by BM25 until an operator indexes
    something above it, which is the same shape as the rest of the appliance (1.3: a
    subtree "does not appear in BM25 results by default").
    """

    def test_an_ingest_with_no_indexed_ancestor_joins_nothing(self, db_session, mock_embedding):
        """AND NO LONGER "SAYS SO", WHICH IS A LOSS AND IS RECORDED RATHER THAN HIDDEN.

        Until ``SPRINT_0_6_0.md`` Block B step 7 this ingest produced an ``index:bm25``
        attempt with ``status='skipped'``, a reason, and the ``candidate_roots`` it had
        looked for. The task is planned by ``IngestRollupPlanner`` now, and the planner
        refuses to enqueue a row whose only possible outcome is ``skipped`` — its
        ``summarize:tree`` rung says so in those words, and offering one per settling
        boundary on an unindexed corpus is what Option I's anti-join exists to avoid
        (``docs/MEASURE_BM25_BOUNDARY.md`` §2.6). So the trace is gone and the guarantee is
        not: nothing joins any index, and the question the trace answered is one query
        against ``search_index_members``.
        """
        content = (
            "PostgreSQL vector search with pgvector extension is powerful. "
            "It supports HNSW and IVFFlat index types for similarity search. "
            "Combined with BM25, it provides hybrid retrieval capabilities."
        )

        result = _ingest(db_session, content, "raw", title="BM25 Membership Test")

        assert [s for s in result.stages if s.stage == TASK_INDEX_BM25] == []
        subtree = {
            d.id for d in DocumentRepository(db_session).get_subtree(result.source_document_id)
        }
        entries = (
            db_session.execute(
                select(SearchIndexEntry.document_id).where(
                    SearchIndexEntry.document_id.in_(subtree)
                )
            )
            .scalars()
            .all()
        )
        assert entries == []
        assert TaskQueueRepository(db_session).unfinished_tasks_for(result.source_document_id) == []

    def test_an_ingest_under_an_indexed_ancestor_joins_that_index_and_is_findable(
        self, db_session, mock_embedding
    ):
        unique_keyword = "xylophoneRegression"
        repo = DocumentRepository(db_session)
        folder = repo.create(title="An indexed folder", content=None, auto_embed=False)
        db_session.flush()

        search_repo = SearchRepository(db_session)
        search_repo.create_index("regression-corpus", description="11.5 membership test")
        assert search_repo.add_root_to_index("regression-corpus", folder.id)
        db_session.flush()

        content = (
            f"The {unique_keyword} is a very specific term used only in this test. "
            "This document should be findable after BM25 indexing completes. "
            "No other document in the database contains this unique term."
        )
        result = _ingest(db_session, content, "raw", title="Findable", parent_id=folder.id)
        db_session.flush()

        stage = [s for s in result.stages if s.stage == TASK_INDEX_BM25][0]
        assert stage.status == "completed"
        # The response's `stages` are a rollup — one entry per task with per-status counts
        # — so the handler's own detail is read from the node's attempt log, which is where
        # it durably lives (INGEST_SPEC.md 3.4).
        assert _attempt_detail(db_session, result.source_document_id, TASK_INDEX_BM25)[
            "indexes"
        ] == ["regression-corpus"]

        results = search_repo.bm25_search(unique_keyword, index_name="regression-corpus", limit=10)
        assert len(results) >= 1, "BM25 search should find the ingested document"
        found_ids = {r.document.id for r in results}
        subtree_ids = {d.id for d in repo.get_subtree(result.source_document_id)}
        assert (
            found_ids & subtree_ids
        ), f"BM25 results {found_ids} should overlap with subtree {subtree_ids}"

    def test_the_default_index_is_no_longer_created_as_a_side_effect(
        self, db_session, mock_embedding
    ):
        """The behaviour being removed, stated directly."""
        before = SearchRepository(db_session).get_index("default")

        _ingest(db_session, "Alpha beta gamma delta epsilon. " * 20, "raw")

        after = SearchRepository(db_session).get_index("default")
        assert (before is None) == (after is None)


# ---------------------------------------------------------------------------
# Tests: Entity exclusion from BM25
# ---------------------------------------------------------------------------


@requires_db
class TestBM25EntityExclusion:
    """Documents with usetype='entity' or 'summary' should not be indexed in BM25."""

    def test_entity_documents_not_indexed(self, db_session, mock_embedding):
        """Calling index_document on an entity document should return False."""
        repo = DocumentRepository(db_session)
        search_repo = SearchRepository(db_session)

        # Create an entity document
        entity = repo.create(
            title="Test Entity",
            content="This entity has some content that could be indexed.",
            usetype="entity",
            auto_embed=False,
        )
        db_session.flush()

        # Ensure a default index exists
        if not search_repo.get_index("default"):
            search_repo.create_index(name="default", description="Test index")

        # Attempt to index it
        indexed = search_repo.index_document(entity.id, "default")
        assert indexed is False, "Entity documents should not be indexed in BM25"

    def test_summary_documents_not_indexed(self, db_session, mock_embedding):
        """Calling index_document on a summary document should return False."""
        repo = DocumentRepository(db_session)
        search_repo = SearchRepository(db_session)

        summary = repo.create(
            title="Test Summary",
            content="This summary has some content that could be indexed.",
            usetype="summary",
            auto_embed=False,
        )
        db_session.flush()

        if not search_repo.get_index("default"):
            search_repo.create_index(name="default", description="Test index")

        indexed = search_repo.index_document(summary.id, "default")
        assert indexed is False, "Summary documents should not be indexed in BM25"

    def test_chunk_documents_are_indexed(self, db_session, mock_embedding):
        """Calling index_document on a chunk document should return True."""
        repo = DocumentRepository(db_session)
        search_repo = SearchRepository(db_session)

        chunk = repo.create(
            title="Test Chunk",
            content="This chunk has content that should be indexed in BM25.",
            usetype="chunk",
            auto_embed=False,
        )
        db_session.flush()

        if not search_repo.get_index("default"):
            search_repo.create_index(name="default", description="Test index")

        indexed = search_repo.index_document(chunk.id, "default")
        assert indexed is True, "Chunk documents should be indexed in BM25"

    def test_pipeline_auto_index_excludes_entities(self, db_session, mock_embedding):
        """After pipeline ingest, BM25 search should not return entity documents."""
        # Create a root doc with an entity child manually, then index
        repo = DocumentRepository(db_session)
        search_repo = SearchRepository(db_session)

        root = repo.create(
            title="Entity Exclusion Root",
            content="Root document about quantum chromodynamics for testing.",
            usetype="raw",
            auto_embed=False,
        )
        repo.create(
            title="Chunk Child",
            content="Quantum chromodynamics is the theory of the strong interaction.",
            parent_id=root.id,
            usetype="chunk",
            auto_embed=False,
        )
        entity = repo.create(
            title="quantum chromodynamics",
            content="Quantum chromodynamics entity node with descriptive content.",
            parent_id=root.id,
            usetype="entity",
            auto_embed=False,
        )
        db_session.flush()

        # Index the subtree
        if not search_repo.get_index("default"):
            search_repo.create_index(name="default", description="Test index")
        search_repo.add_root_to_index("default", root.id)
        for doc in repo.get_subtree(root.id):
            if doc.content:
                search_repo.index_document(doc.id, "default")
        db_session.flush()

        # BM25 search for "quantum"
        results = search_repo.bm25_search("quantum chromodynamics", index_name="default", limit=20)
        result_ids = {r.document.id for r in results}
        result_usetypes = {r.document.usetype for r in results}

        # Entity should not appear in results
        assert entity.id not in result_ids, "Entity document should not appear in BM25 results"
        assert "entity" not in result_usetypes, "No entity usetype should appear in BM25 results"
