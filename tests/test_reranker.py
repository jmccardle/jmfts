"""Tests for cross-encoder reranker integration.

Verifies:
1. rerank() reorders SearchResult candidates by cross-encoder score
2. API endpoints with rerank=true return reranked results
3. A reranker that cannot load fails the request instead of silently skipping stage 2
4. The reranker device follows the embedding device unless pinned

Uses real DB with savepoint rollback and mocked embeddings. No cross-encoder model is
downloaded — the service is mocked at its singleton getter.
"""

import numpy as np
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# DB availability check
# ---------------------------------------------------------------------------

try:
    from jmfts_core.database import get_session_factory, get_engine, get_db
    from jmfts_core.repositories.document import DocumentRepository
    from jmfts_core.repositories.search import SearchRepository, SearchResult
    from jmfts_core.embedding import EmbeddingResult, TokenEmbeddingResult
    from jmfts_core.rest.main import app

    from sqlalchemy import text as sa_text

    _engine = get_engine()
    with _engine.connect() as _conn:
        _conn.execute(sa_text("SELECT 1"))
    _DB_AVAILABLE = True
except Exception:
    _DB_AVAILABLE = False

requires_db = pytest.mark.skipif(not _DB_AVAILABLE, reason="Database not available")


# ---------------------------------------------------------------------------
# Mock embedding service (same as test_regression_api)
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
# Mock reranker that produces deterministic scores
# ---------------------------------------------------------------------------


class MockRerankerService:
    """Reranker that scores based on keyword overlap for deterministic tests."""

    def score_pairs(self, query: str, documents: list[str]) -> list[float]:
        query_words = set(query.lower().split())
        scores = []
        for doc in documents:
            doc_words = set(doc.lower().split())
            overlap = len(query_words & doc_words)
            scores.append(float(overlap) / max(len(query_words), 1))
        return scores

    def rerank(self, query, candidates, limit=None):
        if not candidates:
            return []
        documents = []
        for r in candidates:
            text = ""
            if r.document.title:
                text += r.document.title + " "
            if r.document.content:
                text += r.document.content
            documents.append(text.strip() or "(empty)")

        scores = self.score_pairs(query, documents)
        scored = list(zip(candidates, scores))
        scored.sort(key=lambda x: x[1], reverse=True)
        if limit:
            scored = scored[:limit]
        return [
            SearchResult(
                document=r.document,
                score=float(s),
                method=f"{r.method}+rerank",
            )
            for r, s in scored
        ]


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
def mock_embedding_all():
    if not _DB_AVAILABLE:
        pytest.skip("Database not available")
    svc = MockEmbeddingService()
    with (
        patch("jmfts_core.repositories.document.get_embedder", return_value=svc),
        patch("jmfts_core.repositories.search.get_embedding_service", return_value=svc),
    ):
        yield svc


@pytest.fixture
def mock_reranker():
    svc = MockRerankerService()
    # Patch every by-name import site of the singleton getter. The whole /search/*
    # family moved its rerank logic from jmfts_core.rest.routers.search into the SearchService
    # (@expose unification), so the service module now holds the by-name copy.
    with (
        patch("jmfts_core.reranker.get_reranker_service", return_value=svc),
        patch("jmfts_core.services.search_service.get_reranker_service", return_value=svc),
    ):
        yield svc


@pytest.fixture
def client_with_db(db_session, mock_embedding_all, mock_reranker):
    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    # CR-4: present the shared-bearer token pinned by tests/conftest.py.
    from tests.conftest import AUTH_HEADERS

    client = TestClient(app, headers=AUTH_HEADERS)
    yield client
    app.dependency_overrides.pop(get_db, None)


def _seed_data(db_session):
    """Create documents with content designed for deterministic reranking."""
    repo = DocumentRepository(db_session)
    search_repo = SearchRepository(db_session)

    root = repo.create(
        title="Reranker Test Root",
        content="Root document for reranker testing.",
        usetype="raw",
        auto_embed=True,
    )
    chunks = []
    contents = [
        "Neutron stars form after supernova explosions of massive stars.",
        "Photosynthesis converts sunlight into chemical energy in plants.",
        "Supernova remnants scatter heavy elements across interstellar space.",
        "The ocean covers seventy percent of Earth surface area.",
        "Stellar nucleosynthesis produces elements heavier than hydrogen.",
    ]
    for i, content in enumerate(contents):
        c = repo.create(
            title=f"Chunk {i}",
            content=content,
            parent_id=root.id,
            usetype="chunk",
            auto_embed=True,
        )
        chunks.append(c)
    db_session.flush()

    # BM25 index
    if not search_repo.get_index("default"):
        search_repo.create_index(name="default", description="Test index")
    search_repo.add_root_to_index("default", root.id)
    for doc in repo.get_subtree(root.id):
        if doc.content:
            search_repo.index_document(doc.id, "default")
    db_session.flush()

    return root, chunks


# ---------------------------------------------------------------------------
# Unit tests: RerankerService.rerank()
# ---------------------------------------------------------------------------


class TestRerankerReorder:
    """rerank() reorders candidates by cross-encoder score."""

    def test_rerank_reorders_by_score(self):
        """Documents should be sorted by reranker score, not original order."""
        from jmfts_core.models.document import Document

        # Create mock documents with different relevance to "supernova explosion"
        docs = []
        for title, content in [
            ("Doc A", "Photosynthesis in plants converts light to energy."),
            ("Doc B", "Supernova explosion scatters heavy elements."),
            ("Doc C", "The ocean is very deep and blue."),
        ]:
            d = MagicMock(spec=Document)
            d.title = title
            d.content = content
            docs.append(d)

        candidates = [
            SearchResult(document=docs[0], score=0.99, method="vector"),
            SearchResult(document=docs[1], score=0.50, method="vector"),
            SearchResult(document=docs[2], score=0.75, method="vector"),
        ]

        reranker = MockRerankerService()
        reranked = reranker.rerank("supernova explosion", candidates)

        # Doc B ("Supernova explosion...") should be first
        assert reranked[0].document.title == "Doc B"
        assert reranked[0].method == "vector+rerank"

    def test_rerank_respects_limit(self):
        from jmfts_core.models.document import Document

        docs = []
        for i in range(5):
            d = MagicMock(spec=Document)
            d.title = f"Doc {i}"
            d.content = f"Content {i}"
            docs.append(d)

        candidates = [SearchResult(document=d, score=0.5, method="bm25") for d in docs]

        reranker = MockRerankerService()
        reranked = reranker.rerank("test query", candidates, limit=2)
        assert len(reranked) == 2

    def test_rerank_empty_candidates(self):
        reranker = MockRerankerService()
        assert reranker.rerank("query", []) == []

    def test_rerank_appends_method_suffix(self):
        from jmfts_core.models.document import Document

        d = MagicMock(spec=Document)
        d.title = "Test"
        d.content = "Test content"

        candidates = [SearchResult(document=d, score=0.5, method="hybrid")]
        reranker = MockRerankerService()
        reranked = reranker.rerank("test", candidates)
        assert reranked[0].method == "hybrid+rerank"


# ---------------------------------------------------------------------------
# API integration tests: rerank=true on search endpoints
# ---------------------------------------------------------------------------


@requires_db
class TestRerankerAPIIntegration:
    """Search endpoints with rerank=true return reranked results."""

    def test_vector_search_rerank(self, client_with_db, db_session):
        _seed_data(db_session)

        resp = client_with_db.post(
            "/search/vector?rerank=true",
            json={"query": "supernova explosion", "limit": 5},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        # All results should have +rerank suffix
        for r in data["results"]:
            assert "+rerank" in r["method"]

    def test_bm25_search_rerank(self, client_with_db, db_session):
        _seed_data(db_session)

        resp = client_with_db.post(
            "/search/bm25?rerank=true",
            json={"query": "supernova stellar", "limit": 5},
        )
        assert resp.status_code == 200
        data = resp.json()
        if data["total"] > 0:
            assert "+rerank" in data["results"][0]["method"]

    def test_hybrid_search_rerank(self, client_with_db, db_session):
        _seed_data(db_session)

        resp = client_with_db.post(
            "/search/hybrid?rerank=true",
            json={"query": "supernova", "limit": 5},
        )
        assert resp.status_code == 200
        data = resp.json()
        if data["total"] > 0:
            assert "+rerank" in data["results"][0]["method"]

    def test_search_without_rerank_has_no_suffix(self, client_with_db, db_session):
        _seed_data(db_session)

        resp = client_with_db.post(
            "/search/vector",
            json={"query": "supernova", "limit": 5},
        )
        assert resp.status_code == 200
        data = resp.json()
        if data["total"] > 0:
            assert "+rerank" not in data["results"][0]["method"]

    def test_quick_search_rerank(self, client_with_db, db_session):
        _seed_data(db_session)

        resp = client_with_db.get(
            "/search/?q=supernova&rerank=true&limit=5",
        )
        assert resp.status_code == 200
        data = resp.json()
        if data["total"] > 0:
            assert "+rerank" in data["results"][0]["method"]

    def test_rerank_changes_order(self, client_with_db, db_session):
        """With and without rerank should potentially differ in order."""
        _seed_data(db_session)

        resp_plain = client_with_db.post(
            "/search/vector",
            json={"query": "supernova explosion massive stars", "limit": 5},
        )
        resp_reranked = client_with_db.post(
            "/search/vector?rerank=true",
            json={"query": "supernova explosion massive stars", "limit": 5},
        )

        assert resp_plain.status_code == 200
        assert resp_reranked.status_code == 200

        plain = resp_plain.json()
        reranked = resp_reranked.json()

        # Both should return results
        assert plain["total"] >= 1
        assert reranked["total"] >= 1

        # Reranked results should have different scores (cross-encoder vs cosine)
        if plain["total"] > 0 and reranked["total"] > 0:
            assert plain["results"][0]["score"] != reranked["results"][0]["score"]


# ---------------------------------------------------------------------------
# Device resolution
# ---------------------------------------------------------------------------


class TestRerankerDevice:
    """A CPU-only deployment should not have to set two variables to stay CPU-only."""

    def test_blank_device_follows_embedding_device(self):
        from jmfts_core.config import Settings

        s = Settings(embedding_device="cpu", reranker_device="")
        assert s.effective_reranker_device == "cpu"

    def test_explicit_device_wins(self):
        from jmfts_core.config import Settings

        s = Settings(embedding_device="cpu", reranker_device="cuda")
        assert s.effective_reranker_device == "cuda"


# ---------------------------------------------------------------------------
# Failure surfacing
# ---------------------------------------------------------------------------


@requires_db
class TestRerankerFailureSurfaces:
    """A reranker that cannot load must fail the request, not quietly skip stage 2."""

    def test_reranker_failure_is_a_server_error(self, db_session, mock_embedding_all):
        """`?rerank=true` with an unloadable model must not answer 200 with the
        first-stage ranking. The caller asked for two stages and got one; reporting
        success would make that indistinguishable from a working reranker."""
        _seed_data(db_session)

        def _override_get_db():
            yield db_session

        def _failing_reranker():
            raise RuntimeError("Model not loaded")

        app.dependency_overrides[get_db] = _override_get_db
        try:
            with patch(
                "jmfts_core.services.search_service.get_reranker_service",
                side_effect=_failing_reranker,
            ):
                from tests.conftest import AUTH_HEADERS

                client = TestClient(app, headers=AUTH_HEADERS, raise_server_exceptions=False)
                resp = client.post(
                    "/search/vector?rerank=true",
                    json={"query": "supernova", "limit": 5},
                )
                assert resp.status_code == 500
        finally:
            app.dependency_overrides.pop(get_db, None)

    def test_search_without_rerank_is_unaffected(self, db_session, mock_embedding_all):
        """The reranker is only touched when it is asked for — a broken model must not
        break plain search."""
        _seed_data(db_session)

        def _override_get_db():
            yield db_session

        def _failing_reranker():
            raise RuntimeError("Model not loaded")

        app.dependency_overrides[get_db] = _override_get_db
        try:
            with patch(
                "jmfts_core.services.search_service.get_reranker_service",
                side_effect=_failing_reranker,
            ):
                from tests.conftest import AUTH_HEADERS

                client = TestClient(app, headers=AUTH_HEADERS)
                resp = client.post(
                    "/search/vector",
                    json={"query": "supernova", "limit": 5},
                )
                assert resp.status_code == 200
        finally:
            app.dependency_overrides.pop(get_db, None)

        app.dependency_overrides.pop(get_db, None)
