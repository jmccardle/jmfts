"""Regression tests for search correctness (Kanban #255).

Tests: BM25 keyword search returns matching chunks (not entities/summaries),
parent_id scoping filters to subtree, hybrid search combines BM25 + vector.

Tier 2: Integration tests — real DB (savepoint rollback), mocked embedding.
"""

import numpy as np
import pytest
from unittest.mock import patch
from sqlalchemy import text as sa_text

# ---------------------------------------------------------------------------
# DB availability check
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
# Mock embedding service
# ---------------------------------------------------------------------------


class MockEmbeddingService:
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
    session.begin_nested()
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


@pytest.fixture
def mock_search_embedding(mock_embedding):
    """Also patch embedding service in search repository for vector_search_text."""
    with patch("jmfts_core.repositories.search.get_embedding_service", return_value=mock_embedding):
        yield mock_embedding


# ---------------------------------------------------------------------------
# Helper: create an indexed document tree
# ---------------------------------------------------------------------------


def _create_indexed_tree(
    db_session, mock_embedding, docs, root_title="Test Root", root_usetype="raw"
):
    """Create a root with children and index them in BM25.

    Args:
        docs: list of (title, content, usetype) tuples for children.

    Returns:
        (root_doc, child_docs)
    """
    repo = DocumentRepository(db_session)
    search_repo = SearchRepository(db_session)

    root = repo.create(
        title=root_title,
        content=f"Root document: {root_title}",
        usetype=root_usetype,
        auto_embed=True,
    )

    children = []
    for title, content, usetype in docs:
        child = repo.create(
            title=title,
            content=content,
            parent_id=root.id,
            usetype=usetype,
            auto_embed=True,
        )
        children.append(child)

    db_session.flush()

    # Index everything
    if not search_repo.get_index("default"):
        search_repo.create_index(name="default", description="Test BM25 index")
    search_repo.add_root_to_index("default", root.id)
    for doc in repo.get_subtree(root.id):
        if doc.content:
            search_repo.index_document(doc.id, "default")
    db_session.flush()

    return root, children


# ---------------------------------------------------------------------------
# Tests: BM25 keyword search correctness
# ---------------------------------------------------------------------------


@requires_db
class TestBM25SearchCorrectness:
    """BM25 keyword search returns matching chunks, not entities or summaries."""

    def test_keyword_returns_matching_chunks(self, db_session, mock_embedding):
        """Searching for a keyword returns the chunk containing it."""
        root, children = _create_indexed_tree(
            db_session,
            mock_embedding,
            [
                ("Chunk A", "Mitochondria are the powerhouse of the cell.", "chunk"),
                ("Chunk B", "Photosynthesis converts sunlight into energy.", "chunk"),
                ("Chunk C", "DNA replication occurs during cell division.", "chunk"),
            ],
        )

        search_repo = SearchRepository(db_session)
        results = search_repo.bm25_search("mitochondria powerhouse", index_name="default", limit=10)

        assert len(results) >= 1, "BM25 should find at least one result"
        top_result = results[0]
        assert (
            "mitochondria" in top_result.document.content.lower()
            or "powerhouse" in top_result.document.content.lower()
        ), f"Top result should contain query terms, got: {top_result.document.content[:80]}"

    def test_bm25_excludes_entity_usetype(self, db_session, mock_embedding):
        """BM25 results should not include entity documents."""
        root, children = _create_indexed_tree(
            db_session,
            mock_embedding,
            [
                (
                    "Chunk About Ribosomes",
                    "Ribosomes synthesize proteins from messenger RNA.",
                    "chunk",
                ),
                (
                    "ribosome",
                    "Ribosome entity description with protein synthesis keywords.",
                    "entity",
                ),
            ],
        )

        search_repo = SearchRepository(db_session)
        results = search_repo.bm25_search("ribosome protein", index_name="default", limit=10)

        result_usetypes = {r.document.usetype for r in results}
        assert (
            "entity" not in result_usetypes
        ), f"BM25 should not return entity documents, got usetypes: {result_usetypes}"

    def test_bm25_returns_zero_for_absent_term(self, db_session, mock_embedding):
        """Searching for a term not in any document returns empty results."""
        root, children = _create_indexed_tree(
            db_session,
            mock_embedding,
            [("Chunk", "Normal scientific text about biology.", "chunk")],
        )

        search_repo = SearchRepository(db_session)
        results = search_repo.bm25_search(
            "zyxwvutsrqponmlkjihgfedcba", index_name="default", limit=10
        )
        assert len(results) == 0


# ---------------------------------------------------------------------------
# Tests: parent_id scoping
# ---------------------------------------------------------------------------


@requires_db
class TestParentIdScoping:
    """Search with parent_id only returns documents in that subtree."""

    def test_bm25_parent_id_filters_to_subtree(self, db_session, mock_embedding):
        """BM25 search with parent_id returns only docs under that parent."""
        repo = DocumentRepository(db_session)
        search_repo = SearchRepository(db_session)

        # Create two separate document trees sharing a keyword
        root_a = repo.create(
            title="Root A", content="Root A about electrochemistry.", usetype="raw", auto_embed=True
        )
        repo.create(
            title="Child A",
            content="Electrochemistry involves oxidation and reduction reactions.",
            parent_id=root_a.id,
            usetype="chunk",
            auto_embed=True,
        )

        root_b = repo.create(
            title="Root B", content="Root B about electrochemistry.", usetype="raw", auto_embed=True
        )
        repo.create(
            title="Child B",
            content="Electrochemistry is used in battery technology and fuel cells.",
            parent_id=root_b.id,
            usetype="chunk",
            auto_embed=True,
        )
        db_session.flush()

        # Index both trees
        if not search_repo.get_index("default"):
            search_repo.create_index(name="default", description="Test index")
        for root in [root_a, root_b]:
            search_repo.add_root_to_index("default", root.id)
            for doc in repo.get_subtree(root.id):
                if doc.content:
                    search_repo.index_document(doc.id, "default")
        db_session.flush()

        # Search scoped to root_a
        results_a = search_repo.bm25_search(
            "electrochemistry", index_name="default", limit=10, parent_id=root_a.id
        )
        result_ids_a = {r.document.id for r in results_a}

        # Should find child_a (and possibly root_a), but NOT child_b or root_b
        subtree_a_ids = {d.id for d in repo.get_subtree(root_a.id)}
        subtree_b_ids = {d.id for d in repo.get_subtree(root_b.id)}

        assert (
            result_ids_a <= subtree_a_ids
        ), f"Results {result_ids_a} should be subset of subtree A {subtree_a_ids}"
        assert not (result_ids_a & subtree_b_ids), "Results should not include docs from subtree B"

    def test_vector_search_parent_id_filters_to_subtree(self, db_session, mock_search_embedding):
        """Vector search with parent_id returns only docs under that parent."""
        repo = DocumentRepository(db_session)
        search_repo = SearchRepository(db_session)

        root_a = repo.create(
            title="Vec Root A",
            content="Machine learning algorithms for natural language processing.",
            usetype="raw",
            auto_embed=True,
        )
        repo.create(
            title="Vec Child A",
            content="Neural networks are fundamental to deep learning systems.",
            parent_id=root_a.id,
            usetype="chunk",
            auto_embed=True,
        )

        root_b = repo.create(
            title="Vec Root B",
            content="Cooking recipes for traditional Italian cuisine.",
            usetype="raw",
            auto_embed=True,
        )
        repo.create(
            title="Vec Child B",
            content="Pasta dishes require fresh ingredients and careful preparation.",
            parent_id=root_b.id,
            usetype="chunk",
            auto_embed=True,
        )
        db_session.flush()

        # Vector search scoped to root_a
        results = search_repo.vector_search_text("deep learning", limit=10, parent_id=root_a.id)

        result_ids = {r.document.id for r in results}
        subtree_a_ids = {d.id for d in repo.get_subtree(root_a.id)}
        subtree_b_ids = {d.id for d in repo.get_subtree(root_b.id)}

        # All results should be within subtree_a
        assert (
            result_ids <= subtree_a_ids
        ), f"Vector results {result_ids} should be within subtree A {subtree_a_ids}"
        assert not (result_ids & subtree_b_ids), "Should not include docs from subtree B"


# ---------------------------------------------------------------------------
# Tests: Hybrid search
# ---------------------------------------------------------------------------


@requires_db
class TestHybridSearch:
    """Hybrid search combines BM25 + vector results via RRF."""

    def test_hybrid_returns_results(self, db_session, mock_search_embedding):
        """Hybrid search produces results that combine multiple methods."""
        repo = DocumentRepository(db_session)
        search_repo = SearchRepository(db_session)

        root = repo.create(
            title="Hybrid Root",
            content="Root about thermodynamics and heat transfer.",
            usetype="raw",
            auto_embed=True,
        )
        for i in range(5):
            repo.create(
                title=f"Thermo Chunk {i}",
                content=f"Thermodynamics law {i} describes energy conservation and entropy. "
                f"Heat flows from hot to cold objects naturally. "
                f"This is chunk number {i} in the thermodynamics series.",
                parent_id=root.id,
                usetype="chunk",
                auto_embed=True,
            )
        db_session.flush()

        # Index for BM25
        if not search_repo.get_index("default"):
            search_repo.create_index(name="default", description="Test index")
        search_repo.add_root_to_index("default", root.id)
        for doc in repo.get_subtree(root.id):
            if doc.content:
                search_repo.index_document(doc.id, "default")
        db_session.flush()

        results = search_repo.hybrid_search(
            query_text="thermodynamics entropy",
            limit=5,
            methods=["vector", "bm25"],
            weights={"vector": 0.86, "bm25": 0.14},
        )

        assert len(results) >= 1, "Hybrid search should return at least one result"
        assert all(r.method == "hybrid" for r in results)
        # Scores should be positive (RRF produces small but positive values)
        assert all(r.score > 0 for r in results)

    def test_hybrid_respects_parent_id(self, db_session, mock_search_embedding):
        """Hybrid search with parent_id filters to the specified subtree."""
        repo = DocumentRepository(db_session)
        search_repo = SearchRepository(db_session)

        # Two trees with overlapping content
        root_a = repo.create(
            title="Hybrid A",
            content="Genetics and DNA sequencing techniques.",
            usetype="raw",
            auto_embed=True,
        )
        repo.create(
            title="Genetics Chunk",
            content="Genetics involves studying genes and heredity in organisms.",
            parent_id=root_a.id,
            usetype="chunk",
            auto_embed=True,
        )

        root_b = repo.create(
            title="Hybrid B",
            content="Genetics of crop improvement.",
            usetype="raw",
            auto_embed=True,
        )
        repo.create(
            title="Crop Genetics Chunk",
            content="Genetics is applied to crop improvement through selective breeding.",
            parent_id=root_b.id,
            usetype="chunk",
            auto_embed=True,
        )
        db_session.flush()

        # Index both
        if not search_repo.get_index("default"):
            search_repo.create_index(name="default", description="Test index")
        for root in [root_a, root_b]:
            search_repo.add_root_to_index("default", root.id)
            for doc in repo.get_subtree(root.id):
                if doc.content:
                    search_repo.index_document(doc.id, "default")
        db_session.flush()

        results = search_repo.hybrid_search(
            query_text="genetics",
            limit=10,
            methods=["vector", "bm25"],
            parent_id=root_a.id,
        )

        result_ids = {r.document.id for r in results}
        subtree_b_ids = {d.id for d in repo.get_subtree(root_b.id)}
        assert not (
            result_ids & subtree_b_ids
        ), "Hybrid search scoped to root_a should not include root_b's subtree"

    def test_hybrid_scores_decrease(self, db_session, mock_search_embedding):
        """Hybrid search results should be sorted by descending score."""
        repo = DocumentRepository(db_session)
        search_repo = SearchRepository(db_session)

        root = repo.create(
            title="Score Order Root",
            content="Testing score ordering.",
            usetype="raw",
            auto_embed=True,
        )
        for i in range(5):
            repo.create(
                title=f"Score Chunk {i}",
                content=f"Chemistry involves the study of matter and reactions. "
                f"Chemical bonding determines molecular properties. Chunk {i}.",
                parent_id=root.id,
                usetype="chunk",
                auto_embed=True,
            )
        db_session.flush()

        if not search_repo.get_index("default"):
            search_repo.create_index(name="default", description="Test index")
        search_repo.add_root_to_index("default", root.id)
        for doc in repo.get_subtree(root.id):
            if doc.content:
                search_repo.index_document(doc.id, "default")
        db_session.flush()

        results = search_repo.hybrid_search(
            query_text="chemistry bonding",
            limit=10,
            methods=["vector", "bm25"],
        )

        if len(results) >= 2:
            scores = [r.score for r in results]
            for i in range(len(scores) - 1):
                assert (
                    scores[i] >= scores[i + 1]
                ), f"Results not sorted: score[{i}]={scores[i]} < score[{i+1}]={scores[i+1]}"
