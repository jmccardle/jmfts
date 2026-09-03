"""Regression tests for API endpoints (Kanban #255).

Tests: BM25 search endpoint, hybrid search endpoint, triples query
endpoint (predicate filter + offset), and pipeline run endpoint.

Uses FastAPI TestClient with real DB (mocked embedding + LLM).
"""

import numpy as np
import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient
from sqlalchemy import text as sa_text

# ---------------------------------------------------------------------------
# DB availability check
# ---------------------------------------------------------------------------

try:
    from jmfts_core.database import get_engine, get_db
    from jmfts_core.repositories.document import DocumentRepository
    from jmfts_core.repositories.search import SearchRepository
    from jmfts_core.repositories.triple import TripleRepository
    from jmfts_core.embedding import EmbeddingResult, TokenEmbeddingResult
    from jmfts_core.rest.main import app

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


# db_session is provided centrally by tests/conftest.py (connection-level
# transaction + create_savepoint), so endpoint commits can't leak.


@pytest.fixture
def mock_embedding_all():
    """Patch embedding service in both document and search repositories."""
    if not _DB_AVAILABLE:
        pytest.skip("Database not available")
    svc = MockEmbeddingService()
    with (
        patch("jmfts_core.repositories.document.get_embedder", return_value=svc),
        patch("jmfts_core.repositories.search.get_embedding_service", return_value=svc),
    ):
        yield svc


@pytest.fixture
def client_with_db(db_session, mock_embedding_all):
    """TestClient that uses the savepoint-wrapped session."""

    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    # CR-4: the app now carries an app-level shared-bearer dependency, so the
    # TestClient must present the token pinned by tests/conftest.py.
    from tests.conftest import AUTH_HEADERS

    client = TestClient(app, headers=AUTH_HEADERS)
    yield client
    app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# Helper: seed data for search API tests
# ---------------------------------------------------------------------------


def _seed_search_data(db_session):
    """Create documents indexed for BM25 + vector search via API tests."""
    repo = DocumentRepository(db_session)
    search_repo = SearchRepository(db_session)

    root = repo.create(
        title="API Test Root",
        content="Root document about astrophysics and stellar evolution.",
        usetype="raw",
        auto_embed=True,
    )
    chunks = []
    for i, content in enumerate(
        [
            "Stellar nucleosynthesis produces heavier elements in star cores.",
            "Supernova explosions scatter elements across the interstellar medium.",
            "White dwarfs are the remnants of low-mass stellar evolution.",
            "Neutron stars form when massive stars collapse after supernova.",
        ]
    ):
        c = repo.create(
            title=f"Astro Chunk {i}",
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


def _seed_triple_data(db_session):
    """Create entities, predicates, and triples for API tests."""
    doc_repo = DocumentRepository(db_session)
    triple_repo = TripleRepository(db_session)

    entities = {}
    for name in ["Sun", "Solar System", "Milky Way", "Earth", "Moon"]:
        entities[name] = doc_repo.create(
            title=name, content=f"{name} entity.", usetype="entity", auto_embed=False
        )
    db_session.flush()

    predicates = {}
    for pname in ["part_of", "orbits", "contains", "has_satellite"]:
        predicates[pname] = triple_repo.create_predicate(name=pname)
    db_session.flush()

    triples = []
    triple_specs = [
        ("Sun", "part_of", "Solar System"),
        ("Solar System", "part_of", "Milky Way"),
        ("Earth", "orbits", "Sun"),
        ("Moon", "orbits", "Earth"),
        ("Solar System", "contains", "Earth"),
        ("Solar System", "contains", "Sun"),
        ("Milky Way", "contains", "Solar System"),
        ("Earth", "has_satellite", "Moon"),
    ]
    for subj, pred, obj in triple_specs:
        t = triple_repo.create_triple(
            subject_id=entities[subj].id,
            predicate_id=predicates[pred].id,
            object_id=entities[obj].id,
        )
        triples.append(t)
    db_session.flush()

    return entities, predicates, triples


# ---------------------------------------------------------------------------
# Tests: BM25 search endpoint
# ---------------------------------------------------------------------------


@requires_db
class TestBM25SearchEndpoint:
    """POST /search/bm25 returns matching results."""

    def test_bm25_endpoint_returns_results(self, client_with_db, db_session):
        _seed_search_data(db_session)

        resp = client_with_db.post(
            "/search/bm25",
            json={"query": "supernova stellar", "limit": 10},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert "results" in data
        assert "total" in data
        assert "latency_ms" in data
        assert data["total"] >= 1
        assert data["results"][0]["method"] == "bm25"

    def test_bm25_endpoint_empty_query(self, client_with_db, db_session):
        _seed_search_data(db_session)

        resp = client_with_db.post(
            "/search/bm25",
            json={"query": "", "limit": 10},
        )
        # Empty query should return 200 with 0 results (no terms to match)
        assert resp.status_code == 200

    def test_bm25_endpoint_with_usetype_filter(self, client_with_db, db_session):
        _seed_search_data(db_session)

        resp = client_with_db.post(
            "/search/bm25",
            json={"query": "supernova", "limit": 10, "usetype": "chunk"},
        )

        assert resp.status_code == 200
        data = resp.json()
        for result in data["results"]:
            assert result["document"]["usetype"] == "chunk"


# ---------------------------------------------------------------------------
# Tests: Hybrid search endpoint
# ---------------------------------------------------------------------------


@requires_db
class TestHybridSearchEndpoint:
    """POST /search/hybrid returns fused results."""

    def test_hybrid_endpoint_returns_results(self, client_with_db, db_session):
        _seed_search_data(db_session)

        resp = client_with_db.post(
            "/search/hybrid",
            json={
                "query": "stellar evolution supernova",
                "limit": 5,
                "methods": ["vector", "bm25"],
            },
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        assert all(r["method"] == "hybrid" for r in data["results"])
        assert data["latency_ms"] >= 0

    def test_hybrid_endpoint_with_parent_id(self, client_with_db, db_session):
        root, chunks = _seed_search_data(db_session)

        resp = client_with_db.post(
            "/search/hybrid",
            json={
                "query": "stellar",
                "limit": 10,
                "methods": ["vector", "bm25"],
                "parent_id": root.id,
            },
        )

        assert resp.status_code == 200
        data = resp.json()
        # All results should be within the root's subtree
        for result in data["results"]:
            doc = result["document"]
            # Should either be a child (parent_id == root.id) or the root itself
            assert (
                doc["parent_id"] == root.id or doc["id"] == root.id
            ), f"Doc {doc['id']} with parent_id={doc['parent_id']} not in subtree of {root.id}"


# ---------------------------------------------------------------------------
# Tests: Triples query endpoint
# ---------------------------------------------------------------------------


@requires_db
class TestTriplesQueryEndpoint:
    """GET /triples/query respects predicate and offset parameters."""

    def test_query_returns_all_triples(self, client_with_db, db_session):
        entities, predicates, triples = _seed_triple_data(db_session)

        resp = client_with_db.get("/triples/query", params={"limit": 50})

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 8, f"Expected 8 triples, got {len(data)}"

    def test_query_with_predicate_filter(self, client_with_db, db_session):
        entities, predicates, triples = _seed_triple_data(db_session)

        resp = client_with_db.get(
            "/triples/query",
            params={"predicate": "orbits", "limit": 50},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2, f"Expected 2 'orbits' triples, got {len(data)}"
        for t in data:
            assert t["predicate"]["name"] == "orbits"

    def test_query_with_offset_pagination(self, client_with_db, db_session):
        entities, predicates, triples = _seed_triple_data(db_session)

        # Page 1
        resp1 = client_with_db.get("/triples/query", params={"limit": 3, "offset": 0})
        assert resp1.status_code == 200
        page1 = resp1.json()

        # Page 2
        resp2 = client_with_db.get("/triples/query", params={"limit": 3, "offset": 3})
        assert resp2.status_code == 200
        page2 = resp2.json()

        page1_ids = {t["id"] for t in page1}
        page2_ids = {t["id"] for t in page2}

        assert not (
            page1_ids & page2_ids
        ), f"Pages should not overlap: page1={page1_ids}, page2={page2_ids}"

    def test_query_with_entity_and_direction(self, client_with_db, db_session):
        entities, predicates, triples = _seed_triple_data(db_session)

        earth_id = entities["Earth"].id
        resp = client_with_db.get(
            "/triples/query",
            params={"entity_id": earth_id, "direction": "outgoing", "limit": 50},
        )

        assert resp.status_code == 200
        data = resp.json()
        # Earth is subject in: Earth orbits Sun
        assert len(data) >= 1
        for t in data:
            assert t["subject"]["id"] == earth_id

    def test_query_with_each_seeded_predicate(self, client_with_db, db_session):
        """Predicate filter works for each of the seeded predicates (part_of, orbits, contains)."""
        entities, predicates, triples = _seed_triple_data(db_session)

        expected = {
            "part_of": 2,  # Sun part_of Solar System; Solar System part_of Milky Way
            "orbits": 2,  # Earth orbits Sun; Moon orbits Earth
            "contains": 3,  # Solar System contains Earth/Sun; Milky Way contains Solar System
            "has_satellite": 1,  # Earth has_satellite Moon
        }
        for pred_name, expected_count in expected.items():
            resp = client_with_db.get(
                "/triples/query",
                params={"predicate": pred_name, "limit": 50},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert (
                len(data) == expected_count
            ), f"predicate='{pred_name}': expected {expected_count}, got {len(data)}"
            for t in data:
                assert t["predicate"]["name"] == pred_name

    def test_query_predicate_and_offset_combined(self, client_with_db, db_session):
        """Predicate filter + offset work together."""
        entities, predicates, triples = _seed_triple_data(db_session)

        # "contains" has 3 triples; get page 1 (limit=2) and page 2 (offset=2)
        resp1 = client_with_db.get(
            "/triples/query",
            params={"predicate": "contains", "limit": 2, "offset": 0},
        )
        resp2 = client_with_db.get(
            "/triples/query",
            params={"predicate": "contains", "limit": 2, "offset": 2},
        )

        assert resp1.status_code == 200
        assert resp2.status_code == 200

        page1 = resp1.json()
        page2 = resp2.json()

        assert len(page1) == 2
        assert len(page2) == 1  # 3 total, offset=2 leaves 1

        all_ids = {t["id"] for t in page1} | {t["id"] for t in page2}
        assert len(all_ids) == 3  # No overlap, full coverage


# ---------------------------------------------------------------------------
# Tests: Predicates list endpoint — ghost predicate filtering
# ---------------------------------------------------------------------------


@requires_db
class TestPredicatesListEndpoint:
    """GET /triples/predicates returns only active predicates by default."""

    def test_predicates_default_excludes_ghosts(self, client_with_db, db_session):
        """Default /triples/predicates omits predicates with 0 triples."""
        triple_repo = TripleRepository(db_session)

        # Create a ghost predicate (no triples)
        triple_repo.create_predicate(name="ghost_for_api_test_no_triples")
        db_session.flush()

        resp = client_with_db.get("/triples/predicates")
        assert resp.status_code == 200
        data = resp.json()

        names = {p["name"] for p in data}
        assert (
            "ghost_for_api_test_no_triples" not in names
        ), "Ghost predicate must not appear in default /triples/predicates response"

    def test_predicates_with_triples_only_false_includes_ghosts(self, client_with_db, db_session):
        """?with_triples_only=false includes ghost predicates."""
        triple_repo = TripleRepository(db_session)

        triple_repo.create_predicate(name="ghost_for_api_all_test")
        db_session.flush()

        resp = client_with_db.get("/triples/predicates", params={"with_triples_only": False})
        assert resp.status_code == 200
        data = resp.json()

        names = {p["name"] for p in data}
        assert (
            "ghost_for_api_all_test" in names
        ), "Ghost predicate must appear when with_triples_only=false"

    def test_predicates_active_predicates_present(self, client_with_db, db_session):
        """Predicates that have triples appear in the default filtered list."""
        entities, predicates, triples = _seed_triple_data(db_session)

        resp = client_with_db.get("/triples/predicates")
        assert resp.status_code == 200
        data = resp.json()
        names = {p["name"] for p in data}

        for pred_name in ["part_of", "orbits", "contains"]:
            assert (
                pred_name in names
            ), f"Active predicate '{pred_name}' must appear in /triples/predicates"


# ---------------------------------------------------------------------------
# Tests: Pipeline run endpoint
# ---------------------------------------------------------------------------


@requires_db
class TestPipelineRunEndpoint:
    """``POST /ingest`` over HTTP. ``SPRINT_JOBS.md`` 15.4 S5.

    The three usetypes here run on the ingest queue now, so the assertions moved from
    path A's stage list (``parse``, ``chunk``, ``bm25_index``) to the tasks that really
    ran. The wire itself did not move: same request, same response model, same fields.
    """

    def test_pipeline_run_raw(self, client_with_db, db_session):
        resp = client_with_db.post(
            "/ingest",
            json={
                "content": (
                    "Neural networks learn representations through backpropagation. "
                    "Gradient descent optimizes the loss function iteratively. "
                    "Convolutional layers extract spatial features from images."
                ),
                "usetype": "raw",
                "title": "Pipeline Run Test",
            },
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["source_document_id"] >= 1
        assert data["usetype"] == "raw"
        assert data["title"] == "Pipeline Run Test"
        assert data["message_count"] >= 1

        stage_names = [s["stage"] for s in data["stages"]]
        assert stage_names[0] == "probe"
        assert "extract:text" in stage_names
        assert "structure:inferred" in stage_names, stage_names
        assert "embed" in stage_names, stage_names
        for s in data["stages"]:
            assert s["status"] in ("completed", "skipped"), s

    def test_pipeline_run_markdown(self, client_with_db, db_session):
        """Headings still decide the tree — measured by probe now, not claimed by name."""
        resp = client_with_db.post(
            "/ingest",
            json={
                "content": (
                    "# Introduction\n\n"
                    "This document covers machine learning basics.\n\n"
                    "# Supervised Learning\n\n"
                    "Supervised learning uses labeled training data.\n\n"
                    "# Unsupervised Learning\n\n"
                    "Unsupervised learning finds patterns without labels.\n"
                ),
                "usetype": "markdown",
                "title": "ML Basics",
            },
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["usetype"] == "markdown"
        stage_names = [s["stage"] for s in data["stages"]]
        assert "structure:declared" in stage_names, stage_names
        assert "structure:inferred" not in stage_names, stage_names
        # Three declared headings become three sections, and the chunks sit under them —
        # so the tree is deeper than the two levels a flat chunking would give.
        assert data["tree_depth"] >= 3

    def test_pipeline_run_joins_no_bm25_index_by_default(self, client_with_db, db_session):
        """WAS `test_pipeline_run_creates_searchable_content`, and the behaviour it
        asserted is the one INGEST_SPEC.md 11.5 removes.

        Path A indexed every ingest into `default` unconditionally, so the content was
        immediately findable by BM25. The queue's `index:bm25` task joins only indexes
        whose registered root is the document or one of its ancestors, and a document
        posted with no parent has neither. It is findable by vector search and not by BM25
        until an operator indexes something above it — 1.3's stated default, arrived at
        rather than worked around.
        """
        unique_term = "zygomorphicFlowerSymmetry"
        resp = client_with_db.post(
            "/ingest",
            json={
                "content": (
                    f"The concept of {unique_term} describes bilateral symmetry in flowers. "
                    "This is a botanical classification term used in plant morphology. "
                    "It distinguishes radial from bilateral flower structures."
                ),
                "usetype": "raw",
                "title": "Searchable Pipeline Test",
            },
        )
        assert resp.status_code == 200
        index_stage = [s for s in resp.json()["stages"] if s["stage"] == "index:bm25"]
        assert [s["status"] for s in index_stage] == ["skipped"]

        search_resp = client_with_db.post(
            "/search/bm25",
            json={"query": unique_term.lower(), "limit": 10},
        )
        assert search_resp.status_code == 200
        assert search_resp.json()["total"] == 0
