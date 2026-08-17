"""Regression tests for the ingestion pipeline (Kanban #255).

Tests: parent_id assignment, usetype correctness, BM25 auto-indexing after
ingest, and entity exclusion from BM25 indexes.

Tier 2: Integration tests — real DB (savepoint rollback), mocked LLM + embedding.
"""

import asyncio
from unittest.mock import patch

import numpy as np
import pytest
from sqlalchemy import text as sa_text

from jmfts_core.pipeline import execute_pipeline

# ---------------------------------------------------------------------------
# DB availability check (same pattern as test_raptor.py)
# ---------------------------------------------------------------------------

try:
    from jmfts_core.database import get_session_factory, get_engine
    from jmfts_core.repositories.document import DocumentRepository
    from jmfts_core.repositories.search import SearchRepository
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
    with patch("jmfts_core.repositories.document.get_embedding_service", return_value=svc):
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

        result = _run(
            execute_pipeline(
                db_session,
                content,
                "markdown",
                title="Parent ID Test",
                pipeline_config={"summarize": False, "extract_facts": False},
            )
        )

        root_id = result.source_document_id
        repo = DocumentRepository(db_session)
        children = repo.get_children(root_id, depth=1, limit=100)

        assert len(children) >= 3, f"Expected >= 3 children, got {len(children)}"
        for child in children:
            assert (
                child.parent_id == root_id
            ), f"Child {child.id} has parent_id={child.parent_id}, expected {root_id}"

    def test_markdown_chunks_have_chunk_usetype(self, db_session, mock_embedding):
        """Markdown chunks should have usetype='chunk'."""
        content = (
            "# Alpha\n\nAlpha section with enough content to chunk properly.\n\n"
            "# Beta\n\nBeta section with enough content to chunk properly.\n"
        )

        result = _run(
            execute_pipeline(
                db_session,
                content,
                "markdown",
                title="Usetype Test",
                pipeline_config={"summarize": False, "extract_facts": False},
            )
        )

        repo = DocumentRepository(db_session)
        children = repo.get_children(result.source_document_id, depth=1, limit=100)
        for child in children:
            assert (
                child.usetype == "chunk"
            ), f"Child {child.id} has usetype='{child.usetype}', expected 'chunk'"

    def test_raw_root_has_correct_usetype(self, db_session, mock_embedding):
        """Raw pipeline root document should have usetype='raw'."""
        content = (
            "The quick brown fox jumped over the lazy dog. "
            "This is a test of the raw text ingestion pipeline. "
            "It should create a root document with usetype raw."
        )

        result = _run(
            execute_pipeline(
                db_session,
                content,
                "raw",
                title="Usetype Raw Test",
                pipeline_config={"summarize": False, "extract_facts": False},
            )
        )

        repo = DocumentRepository(db_session)
        root = repo.get(result.source_document_id)
        assert root.usetype == "raw"

    def test_chunk_path_includes_root(self, db_session, mock_embedding):
        """Chunk documents should have root_id in their path array."""
        content = "# Heading\n\n" "Some paragraph text long enough to be ingested as a chunk.\n"

        result = _run(
            execute_pipeline(
                db_session,
                content,
                "markdown",
                title="Path Test",
                pipeline_config={"summarize": False, "extract_facts": False},
            )
        )

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
class TestBM25AutoIndexing:
    """After ingest, the document subtree should be searchable via BM25."""

    def test_ingest_creates_bm25_index(self, db_session, mock_embedding):
        """Pipeline creates a 'default' BM25 index and populates it."""
        content = (
            "PostgreSQL vector search with pgvector extension is powerful. "
            "It supports HNSW and IVFFlat index types for similarity search. "
            "Combined with BM25, it provides hybrid retrieval capabilities."
        )

        result = _run(
            execute_pipeline(
                db_session,
                content,
                "raw",
                title="BM25 Auto-Index Test",
                pipeline_config={"summarize": False, "extract_facts": False},
            )
        )

        # Check that the bm25_index stage ran successfully
        bm25_stage = [s for s in result.stages if s.stage == "bm25_index"]
        assert len(bm25_stage) == 1
        assert bm25_stage[0].status == "completed"
        assert bm25_stage[0].detail["documents_indexed"] >= 1

    def test_ingest_then_bm25_search_finds_document(self, db_session, mock_embedding):
        """After ingesting a document, BM25 search for a keyword returns it."""
        unique_keyword = "xylophoneRegression"
        content = (
            f"The {unique_keyword} is a very specific term used only in this test. "
            "This document should be findable after BM25 indexing completes. "
            "No other document in the database contains this unique term."
        )

        result = _run(
            execute_pipeline(
                db_session,
                content,
                "raw",
                title="BM25 Search After Ingest",
                pipeline_config={"summarize": False, "extract_facts": False},
            )
        )
        db_session.flush()

        # Search for the unique keyword
        search_repo = SearchRepository(db_session)
        results = search_repo.bm25_search(unique_keyword, index_name="default", limit=10)

        assert len(results) >= 1, "BM25 search should find the ingested document"
        found_ids = {r.document.id for r in results}
        # The result should include the root or one of its chunks
        root_id = result.source_document_id
        repo = DocumentRepository(db_session)
        subtree_ids = {d.id for d in repo.get_subtree(root_id)}
        assert (
            found_ids & subtree_ids
        ), f"BM25 results {found_ids} should overlap with subtree {subtree_ids}"


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
