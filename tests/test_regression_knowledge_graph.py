"""Regression tests for knowledge graph operations (Kanban #255).

Tests: triple creation, predicate filtering, pagination (offset), and
entity resolution producing correct document links.

Tier 2: Integration tests — real DB (savepoint rollback).
"""

import pytest
from sqlalchemy import text as sa_text
from unittest.mock import patch
import numpy as np

# ---------------------------------------------------------------------------
# DB availability check
# ---------------------------------------------------------------------------

try:
    from jmfts_core.database import get_engine
    from jmfts_core.repositories.document import DocumentRepository
    from jmfts_core.repositories.triple import TripleRepository
    from jmfts_core.models.triple import FactType
    from jmfts_core.embedding import EmbeddingResult, TokenEmbeddingResult

    _engine = get_engine()
    with _engine.connect() as _conn:
        _conn.execute(sa_text("SELECT 1"))
    _DB_AVAILABLE = True
except Exception:
    _DB_AVAILABLE = False

requires_db = pytest.mark.skipif(not _DB_AVAILABLE, reason="Database not available")


# ---------------------------------------------------------------------------
# Mock embedding service (needed for auto_embed on entity creation)
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
def mock_embedding():
    if not _DB_AVAILABLE:
        pytest.skip("Database not available")
    svc = MockEmbeddingService()
    with patch("jmfts_core.repositories.document.get_embedder", return_value=svc):
        yield svc


# ---------------------------------------------------------------------------
# Helper: create entities and predicates for graph tests
# ---------------------------------------------------------------------------


def _setup_graph(db_session, mock_embedding):
    """Create a small knowledge graph with entities, predicates, and triples.

    Graph:
        Paris --capital_of--> France
        France --located_in--> Europe
        Berlin --capital_of--> Germany
        Germany --located_in--> Europe
        Paris --has_landmark--> Eiffel Tower

    Returns: dict with entity docs, predicates, and triples.
    """
    doc_repo = DocumentRepository(db_session)
    triple_repo = TripleRepository(db_session)

    # Create entity documents
    entities = {}
    for name in ["Paris", "France", "Europe", "Berlin", "Germany", "Eiffel Tower"]:
        entities[name] = doc_repo.create(
            title=name, content=f"{name} entity node.", usetype="entity", auto_embed=False
        )
    db_session.flush()

    # Create predicates
    predicates = {}
    for pred_name in ["capital_of", "located_in", "has_landmark"]:
        predicates[pred_name] = triple_repo.create_predicate(name=pred_name)
    db_session.flush()

    # Create a source document
    source = doc_repo.create(
        title="Geography Source",
        content="Source document for geography facts.",
        usetype="chunk",
        auto_embed=False,
    )
    db_session.flush()

    # Create triples
    triples = {}
    triple_data = [
        ("Paris_capital_France", "Paris", "capital_of", "France"),
        ("France_in_Europe", "France", "located_in", "Europe"),
        ("Berlin_capital_Germany", "Berlin", "capital_of", "Germany"),
        ("Germany_in_Europe", "Germany", "located_in", "Europe"),
        ("Paris_has_Eiffel", "Paris", "has_landmark", "Eiffel Tower"),
    ]
    for key, subj, pred, obj in triple_data:
        triples[key] = triple_repo.create_triple(
            subject_id=entities[subj].id,
            predicate_id=predicates[pred].id,
            object_id=entities[obj].id,
            source_document_id=source.id,
        )
    db_session.flush()

    return {
        "entities": entities,
        "predicates": predicates,
        "triples": triples,
        "source": source,
    }


# ---------------------------------------------------------------------------
# Tests: Triple creation during fact extraction
# ---------------------------------------------------------------------------


@requires_db
class TestTripleCreation:
    """Triple CRUD operations produce correct records."""

    def test_create_triple_with_all_fields(self, db_session, mock_embedding):
        """Creating a triple stores subject, predicate, object, and metadata."""
        graph = _setup_graph(db_session, mock_embedding)
        triple_repo = TripleRepository(db_session)

        t = graph["triples"]["Paris_capital_France"]
        fetched = triple_repo.get_triple(t.id)

        assert fetched is not None
        assert fetched.subject_id == graph["entities"]["Paris"].id
        assert fetched.predicate_id == graph["predicates"]["capital_of"].id
        assert fetched.object_id == graph["entities"]["France"].id
        assert fetched.source_document_id == graph["source"].id
        assert fetched.fact_type == FactType.atemporal
        assert fetched.invalidated_at is None

    def test_triple_links_to_correct_source_document(self, db_session, mock_embedding):
        """Triples should reference the source document they were extracted from."""
        graph = _setup_graph(db_session, mock_embedding)
        triple_repo = TripleRepository(db_session)

        for key, t in graph["triples"].items():
            fetched = triple_repo.get_triple(t.id)
            assert (
                fetched.source_document_id == graph["source"].id
            ), f"Triple {key} should reference source doc {graph['source'].id}"


# ---------------------------------------------------------------------------
# Tests: Predicate filter
# ---------------------------------------------------------------------------


@requires_db
class TestPredicateFilter:
    """Query triples by predicate returns only matching triples."""

    def test_filter_by_predicate_id(self, db_session, mock_embedding):
        """query_triples with predicate_id returns only triples with that predicate."""
        graph = _setup_graph(db_session, mock_embedding)
        triple_repo = TripleRepository(db_session)

        capital_pred_id = graph["predicates"]["capital_of"].id
        results = triple_repo.query_triples(predicate_id=capital_pred_id, limit=50)

        assert len(results) == 2, f"Expected 2 'capital_of' triples, got {len(results)}"
        for t in results:
            assert t.predicate_id == capital_pred_id

    def test_filter_by_predicate_name(self, db_session, mock_embedding):
        """query_triples with predicate_name returns only matching triples."""
        _setup_graph(db_session, mock_embedding)
        triple_repo = TripleRepository(db_session)

        results = triple_repo.query_triples(predicate_name="located_in", limit=50)

        assert len(results) == 2, f"Expected 2 'located_in' triples, got {len(results)}"
        for t in results:
            assert t.predicate.name == "located_in"

    def test_filter_by_predicate_returns_empty_for_nonexistent(self, db_session, mock_embedding):
        """Filtering by a predicate name that doesn't exist returns no results."""
        _setup_graph(db_session, mock_embedding)
        triple_repo = TripleRepository(db_session)

        results = triple_repo.query_triples(predicate_name="nonexistent_predicate", limit=50)
        assert len(results) == 0

    def test_filter_by_predicate_and_entity(self, db_session, mock_embedding):
        """Combining predicate and entity filters narrows results correctly."""
        graph = _setup_graph(db_session, mock_embedding)
        triple_repo = TripleRepository(db_session)

        paris_id = graph["entities"]["Paris"].id
        capital_pred_id = graph["predicates"]["capital_of"].id

        results = triple_repo.query_triples(
            entity_id=paris_id, predicate_id=capital_pred_id, direction="outgoing", limit=50
        )

        assert len(results) == 1
        assert results[0].subject_id == paris_id
        assert results[0].object_id == graph["entities"]["France"].id


# ---------------------------------------------------------------------------
# Tests: Pagination
# ---------------------------------------------------------------------------


@requires_db
class TestTriplePagination:
    """Offset and limit on triple queries produce non-overlapping pages."""

    def test_offset_zero_and_offset_n_non_overlapping(self, db_session, mock_embedding):
        """Two pages with different offsets return non-overlapping results."""
        _setup_graph(db_session, mock_embedding)
        triple_repo = TripleRepository(db_session)

        # Page 1: offset=0, limit=3
        page1 = triple_repo.query_triples(limit=3, offset=0)
        # Page 2: offset=3, limit=3
        page2 = triple_repo.query_triples(limit=3, offset=3)

        page1_ids = {t.id for t in page1}
        page2_ids = {t.id for t in page2}

        assert not (
            page1_ids & page2_ids
        ), f"Pages should not overlap: page1={page1_ids}, page2={page2_ids}"

    def test_all_triples_covered_by_pagination(self, db_session, mock_embedding):
        """Paginating through all results covers every triple."""
        _setup_graph(db_session, mock_embedding)
        triple_repo = TripleRepository(db_session)

        # Get all at once
        all_triples = triple_repo.query_triples(limit=100, offset=0)
        all_ids = {t.id for t in all_triples}

        # Get in pages of 2
        paginated_ids = set()
        offset = 0
        while True:
            page = triple_repo.query_triples(limit=2, offset=offset)
            if not page:
                break
            for t in page:
                paginated_ids.add(t.id)
            offset += 2

        assert (
            paginated_ids == all_ids
        ), f"Paginated IDs {paginated_ids} should equal all IDs {all_ids}"

    def test_limit_respected(self, db_session, mock_embedding):
        """query_triples never returns more than the requested limit."""
        _setup_graph(db_session, mock_embedding)
        triple_repo = TripleRepository(db_session)

        results = triple_repo.query_triples(limit=2, offset=0)
        assert len(results) <= 2

    def test_large_offset_returns_empty(self, db_session, mock_embedding):
        """Offset beyond total results returns empty list."""
        _setup_graph(db_session, mock_embedding)
        triple_repo = TripleRepository(db_session)

        results = triple_repo.query_triples(limit=10, offset=1000)
        assert len(results) == 0


# ---------------------------------------------------------------------------
# Tests: Entity resolution
# ---------------------------------------------------------------------------


@requires_db
class TestEntityResolution:
    """Entity resolution creates or reuses entity documents correctly."""

    def test_resolve_existing_entity_reuses_document(self, db_session, mock_embedding):
        """resolve_entity for an existing entity name returns the same doc ID."""
        from jmfts_core.fact_extraction import resolve_entity

        doc_repo = DocumentRepository(db_session)
        # Create an entity document first
        entity = doc_repo.create(title="Paris", content=None, usetype="entity", auto_embed=False)
        db_session.flush()

        cache = {}
        doc_id, created = resolve_entity("Paris", db_session, threshold=0.8, _cache=cache)

        assert doc_id == entity.id, "Should reuse existing entity document"
        assert created is False

    def test_resolve_new_entity_creates_document(self, db_session, mock_embedding):
        """resolve_entity for a new entity name creates a new entity document."""
        from jmfts_core.fact_extraction import resolve_entity

        cache = {}
        doc_id, created = resolve_entity(
            "BrandNewEntityXYZ", db_session, threshold=0.8, _cache=cache
        )

        assert doc_id is not None
        assert created is True

        # Verify the created document
        doc_repo = DocumentRepository(db_session)
        doc = doc_repo.get(doc_id)
        assert doc is not None
        assert doc.usetype == "entity"
        assert doc.title == "BrandNewEntityXYZ"

    def test_resolve_entity_caches_result(self, db_session, mock_embedding):
        """After resolving, the cache contains the entity for fast lookup."""
        from jmfts_core.fact_extraction import resolve_entity

        cache = {}
        doc_id1, created1 = resolve_entity("CachedEntity", db_session, threshold=0.8, _cache=cache)
        assert created1 is True
        assert "cachedentity" in cache

        # Second resolve should hit cache
        doc_id2, created2 = resolve_entity("CachedEntity", db_session, threshold=0.8, _cache=cache)
        assert doc_id2 == doc_id1
        assert created2 is False


# ---------------------------------------------------------------------------
# Tests: Direction filtering
# ---------------------------------------------------------------------------


@requires_db
class TestDirectionFiltering:
    """query_triples direction parameter filters correctly."""

    def test_outgoing_only(self, db_session, mock_embedding):
        """direction='outgoing' returns only triples where entity is subject."""
        graph = _setup_graph(db_session, mock_embedding)
        triple_repo = TripleRepository(db_session)

        paris_id = graph["entities"]["Paris"].id
        results = triple_repo.query_triples(entity_id=paris_id, direction="outgoing", limit=50)

        for t in results:
            assert (
                t.subject_id == paris_id
            ), f"Outgoing triple should have Paris as subject, got subject_id={t.subject_id}"
        # Paris has 2 outgoing: capital_of France, has_landmark Eiffel Tower
        assert len(results) == 2

    def test_incoming_only(self, db_session, mock_embedding):
        """direction='incoming' returns only triples where entity is object."""
        graph = _setup_graph(db_session, mock_embedding)
        triple_repo = TripleRepository(db_session)

        europe_id = graph["entities"]["Europe"].id
        results = triple_repo.query_triples(entity_id=europe_id, direction="incoming", limit=50)

        for t in results:
            assert t.object_id == europe_id
        # Europe has 2 incoming: France located_in, Germany located_in
        assert len(results) == 2

    def test_both_directions(self, db_session, mock_embedding):
        """direction='both' returns triples where entity is either subject or object."""
        graph = _setup_graph(db_session, mock_embedding)
        triple_repo = TripleRepository(db_session)

        france_id = graph["entities"]["France"].id
        results = triple_repo.query_triples(entity_id=france_id, direction="both", limit=50)

        # France is: subject in (located_in Europe), object in (Paris capital_of France)
        assert len(results) == 2
        result_ids = {t.id for t in results}
        assert graph["triples"]["Paris_capital_France"].id in result_ids
        assert graph["triples"]["France_in_Europe"].id in result_ids


# ---------------------------------------------------------------------------
# Tests: Ghost predicate filtering
# ---------------------------------------------------------------------------


@requires_db
class TestGhostPredicateFilter:
    """list_predicates with_triples_only omits predicates that have no triples."""

    def test_with_triples_only_excludes_ghost_predicates(self, db_session, mock_embedding):
        """with_triples_only=True omits predicates that have no triples."""
        triple_repo = TripleRepository(db_session)

        # Create a ghost predicate (no triples)
        ghost = triple_repo.create_predicate(name="ghost_pred_no_triples")
        db_session.flush()

        # Create an active predicate with a triple
        from jmfts_core.repositories.document import DocumentRepository

        doc_repo = DocumentRepository(db_session)
        subj = doc_repo.create(title="SubjGhost", content=None, usetype="entity", auto_embed=False)
        obj = doc_repo.create(title="ObjGhost", content=None, usetype="entity", auto_embed=False)
        db_session.flush()
        active = triple_repo.create_predicate(name="active_pred_with_triple")
        db_session.flush()
        triple_repo.create_triple(subject_id=subj.id, predicate_id=active.id, object_id=obj.id)
        db_session.flush()

        results_active = triple_repo.list_predicates(with_triples_only=True)
        result_names = {p.name for p in results_active}

        assert "active_pred_with_triple" in result_names, "Active predicate must be returned"
        assert "ghost_pred_no_triples" not in result_names, "Ghost predicate must be excluded"

    def test_without_filter_includes_ghost_predicates(self, db_session, mock_embedding):
        """with_triples_only=False (default) returns all predicates including ghosts."""
        triple_repo = TripleRepository(db_session)

        ghost = triple_repo.create_predicate(name="ghost_pred_no_triples_2")
        db_session.flush()

        results_all = triple_repo.list_predicates(with_triples_only=False)
        result_names = {p.name for p in results_all}

        assert (
            "ghost_pred_no_triples_2" in result_names
        ), "Ghost predicate must appear without filter"

    def test_with_triples_only_returns_all_active_predicates(self, db_session, mock_embedding):
        """with_triples_only=True returns every predicate that has ≥1 triple."""
        graph = _setup_graph(db_session, mock_embedding)
        triple_repo = TripleRepository(db_session)

        # Add a ghost predicate that should not appear
        triple_repo.create_predicate(name="dangling_pred_should_not_appear")
        db_session.flush()

        active_results = triple_repo.list_predicates(with_triples_only=True)
        active_names = {p.name for p in active_results}

        for pred_name in ["capital_of", "located_in", "has_landmark"]:
            assert (
                pred_name in active_names
            ), f"Active predicate '{pred_name}' missing from filtered results"
        assert "dangling_pred_should_not_appear" not in active_names
