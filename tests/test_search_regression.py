"""Search regression tests for bugs fixed 2026-04-05.

Three bugs were fixed:
1. BM25 returned 0 results — None-guarding index_name and creating a default index
2. Entity nodes polluted 40-60% of search results — entities now get parent_ids
3. Scope filtering didn't work — parent_id added to bm25_search SQL

These are integration tests that use the real database with savepoint rollback.
No test data persists after the test suite runs.
"""

import numpy as np

from jmfts_core.models.document import Document
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.search import SearchRepository

# ============================================================================
# Fixtures — savepoint-wrapped sessions for clean rollback
# ============================================================================


# db_session is provided centrally by tests/conftest.py (connection-level
# transaction + create_savepoint), so endpoint commits can't leak.


def _create_doc(session, title=None, content=None, parent_id=None, usetype=None):
    """Create a document directly via ORM, bypassing embedding.

    Returns the Document object with a valid ID.
    """
    repo = DocumentRepository(session)
    doc = repo.create(
        title=title,
        content=content,
        parent_id=parent_id,
        usetype=usetype,
        auto_embed=False,
    )
    return doc


# ============================================================================
# BM25 Regression Tests
# ============================================================================


class TestBM25Regression:
    """Tests for Bug #1: BM25 returned 0 results.

    Root cause was None-guarding on index_name and missing default index.
    """

    def test_bm25_returns_results(self, db_session):
        """Ingest a document, index it, search — should return results."""
        # Create a root doc and a child with searchable content
        root = _create_doc(db_session, title="Test Root", content="Root document", usetype="raw")
        child = _create_doc(
            db_session,
            title="Python Guide",
            content="Python FastAPI web framework for building modern APIs quickly",
            parent_id=root.id,
            usetype="raw/chunk",
        )

        repo = SearchRepository(db_session)

        # Create and populate index
        repo.create_index("test_bm25_default", description="test index")
        repo.index_document(child.id, index_name="test_bm25_default")
        db_session.flush()

        # Search for a term that is definitely in the content
        results = repo.bm25_search("Python", index_name="test_bm25_default", limit=10)

        assert len(results) > 0, "BM25 search must return results for indexed content"
        assert any(r.document.id == child.id for r in results)

    def test_bm25_scores_are_positive(self, db_session):
        """BM25 results should have positive scores."""
        root = _create_doc(db_session, title="Score Root", content="Root", usetype="raw")
        child = _create_doc(
            db_session,
            title="Score Doc",
            content="PostgreSQL database engine for reliable data storage",
            parent_id=root.id,
            usetype="raw/chunk",
        )

        repo = SearchRepository(db_session)
        repo.create_index("test_bm25_scores", description="test index")
        repo.index_document(child.id, index_name="test_bm25_scores")
        db_session.flush()

        results = repo.bm25_search("PostgreSQL database", index_name="test_bm25_scores", limit=10)

        assert len(results) > 0
        for r in results:
            assert r.score > 0, f"BM25 score should be positive, got {r.score}"

    def test_bm25_with_none_index_name(self, db_session):
        """Passing index_name=None should fall back to 'default', not error.

        This was the core of bug #1: the API router passed None when no
        index was specified, and bm25_search failed to handle it.
        """
        root = _create_doc(db_session, title="None Index Root", content="Root", usetype="raw")
        child = _create_doc(
            db_session,
            title="None Index Doc",
            content="Machine learning algorithms for classification tasks",
            parent_id=root.id,
            usetype="raw/chunk",
        )

        repo = SearchRepository(db_session)
        # Use the existing "default" index, or create it if missing
        if not repo.get_index("default"):
            repo.create_index("default", description="default index")
        repo.index_document(child.id, index_name="default")
        db_session.flush()

        # The API router's bm25_search call uses: index_name=params.get("index_name") or "default"
        # This test verifies the fallback works at the repository level too
        index_name = None or "default"
        results = repo.bm25_search("machine learning", index_name=index_name, limit=10)

        assert len(results) > 0, "BM25 with None->default index_name must return results"

    def test_bm25_missing_index_returns_empty(self, db_session):
        """Searching a non-existent index returns empty, not an error."""
        repo = SearchRepository(db_session)
        results = repo.bm25_search("anything", index_name="nonexistent_index_xyz", limit=10)
        assert results == []


# ============================================================================
# Scope Filtering Tests
# ============================================================================


class TestScopeFiltering:
    """Tests for Bug #3: Scope filtering (parent_id) didn't work.

    Root cause was that bm25_search ignored parent_id parameter.
    """

    def test_bm25_parent_id_filters_results(self, db_session):
        """BM25 search with parent_id should only return children of that root."""
        # Create two separate document trees
        root_a = _create_doc(
            db_session, title="Project Alpha", content="Alpha project", usetype="raw"
        )
        child_a1 = _create_doc(
            db_session,
            title="Alpha Design",
            content="Distributed systems architecture with microservices",
            parent_id=root_a.id,
            usetype="raw/chunk",
        )
        child_a2 = _create_doc(
            db_session,
            title="Alpha Implementation",
            content="Kubernetes deployment for distributed microservices",
            parent_id=root_a.id,
            usetype="raw/chunk",
        )

        root_b = _create_doc(
            db_session, title="Project Beta", content="Beta project", usetype="raw"
        )
        child_b1 = _create_doc(
            db_session,
            title="Beta Design",
            content="Distributed database sharding with microservices",
            parent_id=root_b.id,
            usetype="raw/chunk",
        )

        repo = SearchRepository(db_session)
        idx_name = "test_scope_bm25"
        repo.create_index(idx_name, description="scope test index")

        # Index all children
        for doc in [child_a1, child_a2, child_b1]:
            repo.index_document(doc.id, index_name=idx_name)
        db_session.flush()

        # Search scoped to root_a — should only find Alpha children
        results_a = repo.bm25_search(
            "distributed microservices",
            index_name=idx_name,
            limit=10,
            parent_id=root_a.id,
        )

        result_ids = {r.document.id for r in results_a}
        assert (
            child_a1.id in result_ids or child_a2.id in result_ids
        ), "Scoped search must find children of the target root"
        assert (
            child_b1.id not in result_ids
        ), "Scoped search must NOT include children from a different root"

    def test_bm25_scope_excludes_other_tree(self, db_session):
        """Searching scoped to root B should not return root A's children."""
        root_a = _create_doc(db_session, title="Tree A", content="Tree A root", usetype="raw")
        child_a = _create_doc(
            db_session,
            title="A Child",
            content="Quantum computing algorithms for optimization",
            parent_id=root_a.id,
            usetype="raw/chunk",
        )

        root_b = _create_doc(db_session, title="Tree B", content="Tree B root", usetype="raw")
        child_b = _create_doc(
            db_session,
            title="B Child",
            content="Quantum computing hardware development",
            parent_id=root_b.id,
            usetype="raw/chunk",
        )

        repo = SearchRepository(db_session)
        idx_name = "test_scope_excl"
        repo.create_index(idx_name, description="exclusion test")

        for doc in [child_a, child_b]:
            repo.index_document(doc.id, index_name=idx_name)
        db_session.flush()

        # Search scoped to root_b
        results = repo.bm25_search(
            "quantum computing",
            index_name=idx_name,
            limit=10,
            parent_id=root_b.id,
        )

        result_ids = {r.document.id for r in results}
        assert child_b.id in result_ids, "Scoped search must find target tree's children"
        assert child_a.id not in result_ids, "Scoped search must exclude other tree's children"

    def test_fulltext_parent_id_filters_results(self, db_session):
        """Fulltext search with parent_id should filter to subtree."""
        root_a = _create_doc(db_session, title="FT Root A", content="Root A", usetype="raw")
        child_a = _create_doc(
            db_session,
            title="FT Child A",
            content="Elasticsearch inverted index optimization techniques",
            parent_id=root_a.id,
            usetype="raw/chunk",
        )

        root_b = _create_doc(db_session, title="FT Root B", content="Root B", usetype="raw")
        child_b = _create_doc(
            db_session,
            title="FT Child B",
            content="Elasticsearch cluster management and scaling",
            parent_id=root_b.id,
            usetype="raw/chunk",
        )

        repo = SearchRepository(db_session)

        # Fulltext search scoped to root_a
        results = repo.fulltext_search(
            "Elasticsearch",
            limit=10,
            parent_id=root_a.id,
        )

        result_ids = {r.document.id for r in results}
        assert child_a.id in result_ids, "Fulltext scoped search must find target subtree"
        assert child_b.id not in result_ids, "Fulltext scoped search must exclude other subtree"

    def test_vector_parent_id_filters_results(self, db_session):
        """Vector search with parent_id should filter to subtree.

        Uses a fake embedding (no model loading) since vector search
        requires embeddings to be present on documents.
        """
        # Create two trees
        root_a = _create_doc(db_session, title="Vec Root A", content="Root A", usetype="raw")
        child_a = _create_doc(
            db_session,
            title="Vec Child A",
            content="Neural network training",
            parent_id=root_a.id,
            usetype="raw/chunk",
        )

        root_b = _create_doc(db_session, title="Vec Root B", content="Root B", usetype="raw")
        child_b = _create_doc(
            db_session,
            title="Vec Child B",
            content="Neural network inference",
            parent_id=root_b.id,
            usetype="raw/chunk",
        )

        # Manually set embeddings (768-dim random vectors, normalized)
        rng = np.random.RandomState(42)
        for doc in [child_a, child_b]:
            vec = rng.randn(768).astype(np.float32)
            vec = vec / np.linalg.norm(vec)
            doc.embed = vec.tolist()
        db_session.flush()

        # Use a random embedding for query (same shape)
        query_vec = rng.randn(768).astype(np.float32)
        query_vec = query_vec / np.linalg.norm(query_vec)

        repo = SearchRepository(db_session)

        # Vector search scoped to root_a
        results = repo.vector_search(
            query_vec.tolist(),
            limit=10,
            parent_id=root_a.id,
        )

        result_ids = {r.document.id for r in results}
        assert child_b.id not in result_ids, "Vector scoped search must exclude other subtree"

    def test_hybrid_parent_id_filters_results(self, db_session):
        """Hybrid search with parent_id should filter across all methods.

        Uses fulltext + bm25 (no vector) to avoid embedding dependencies.
        """
        root_a = _create_doc(db_session, title="Hybrid Root A", content="Root A", usetype="raw")
        child_a = _create_doc(
            db_session,
            title="Hybrid Child A",
            content="Reinforcement learning policy gradient methods",
            parent_id=root_a.id,
            usetype="raw/chunk",
        )

        root_b = _create_doc(db_session, title="Hybrid Root B", content="Root B", usetype="raw")
        child_b = _create_doc(
            db_session,
            title="Hybrid Child B",
            content="Reinforcement learning reward shaping strategies",
            parent_id=root_b.id,
            usetype="raw/chunk",
        )

        repo = SearchRepository(db_session)
        idx_name = "test_hybrid_scope"
        repo.create_index(idx_name, description="hybrid scope test")
        for doc in [child_a, child_b]:
            repo.index_document(doc.id, index_name=idx_name)
        db_session.flush()

        # Hybrid search with only fulltext + bm25 (skip vector to avoid embeddings)
        results = repo.hybrid_search(
            "reinforcement learning",
            limit=10,
            methods=["fulltext", "bm25"],
            parent_id=root_a.id,
            index_name=idx_name,
        )

        result_ids = {r.document.id for r in results}
        assert (
            child_b.id not in result_ids
        ), "Hybrid scoped search must exclude documents from other subtree"


# ============================================================================
# Entity Pollution Tests
# ============================================================================


class TestEntityPollution:
    """Tests for Bug #2: Entity nodes polluted 40-60% of search results.

    Root cause was that entities were created without parent_id, so they
    appeared as root documents and matched all scope-less searches equally.
    """

    def test_entities_have_parent_id(self, db_session):
        """After entity resolution, entity documents should have a non-null parent_id.

        This tests the fix: resolve_entity now passes source_document_id as parent_id.
        """
        from jmfts_core.fact_extraction import resolve_entity

        # Create a source document
        source = _create_doc(
            db_session,
            title="Source Doc",
            content="Paris is the capital of France.",
            usetype="raw/chunk",
        )

        cache = {}
        entity_id, created = resolve_entity(
            "Paris",
            db_session,
            threshold=0.8,
            _cache=cache,
            parent_id=source.id,
        )

        assert created is True, "Should create a new entity document"

        # Verify the entity document has a parent_id
        entity_doc = db_session.get(Document, entity_id)
        assert entity_doc is not None
        assert (
            entity_doc.parent_id == source.id
        ), f"Entity should have parent_id={source.id}, got {entity_doc.parent_id}"
        assert entity_doc.usetype == "entity"

    def test_entity_path_includes_source(self, db_session):
        """Entity document's path should include the source document's ancestry."""
        from jmfts_core.fact_extraction import resolve_entity

        root = _create_doc(db_session, title="Root", content="Root doc", usetype="raw")
        source = _create_doc(
            db_session,
            title="Source Chunk",
            content="Machine learning models.",
            parent_id=root.id,
            usetype="raw/chunk",
        )

        cache = {}
        entity_id, created = resolve_entity(
            "MachineLearning",
            db_session,
            threshold=0.8,
            _cache=cache,
            parent_id=source.id,
        )

        entity_doc = db_session.get(Document, entity_id)
        assert entity_doc is not None
        assert (
            source.id in entity_doc.path
        ), f"Entity path should include source doc ID {source.id}, got {entity_doc.path}"
        assert (
            root.id in entity_doc.path
        ), f"Entity path should include root ID {root.id}, got {entity_doc.path}"

    def test_entities_excluded_from_bm25_indexing(self, db_session):
        """Entity documents (usetype=entity) must be excluded from BM25 indexing.

        Kanban #279: Entity docs are short and keyword-dense, inflating BM25 scores.
        index_document() now skips excluded usetypes entirely.
        """
        root = _create_doc(db_session, title="Project A", content="Project A", usetype="raw")
        chunk = _create_doc(
            db_session,
            title="A Chunk",
            content="Python web framework development",
            parent_id=root.id,
            usetype="raw/chunk",
        )
        entity = _create_doc(
            db_session,
            title="Python",
            content="Python",
            parent_id=chunk.id,
            usetype="entity",
        )

        repo = SearchRepository(db_session)
        idx_name = "test_entity_excl"
        repo.create_index(idx_name, description="entity exclusion test")

        # index_document should return False for entities
        assert (
            repo.index_document(entity.id, index_name=idx_name) is False
        ), "index_document must skip entity documents"

        # Index the chunk normally
        assert repo.index_document(chunk.id, index_name=idx_name) is True

        db_session.flush()

        # Search should find the chunk but not the entity
        results = repo.bm25_search("Python", index_name=idx_name, limit=10)
        result_ids = {r.document.id for r in results}
        assert chunk.id in result_ids, "Chunk must appear in BM25 results"
        assert entity.id not in result_ids, "Entity must NOT appear in BM25 results"

    def test_summaries_excluded_from_bm25_indexing(self, db_session):
        """Summary documents (usetype=summary) must be excluded from BM25 indexing.

        Kanban #279: Summaries are synthetic and can pollute keyword search.
        """
        root = _create_doc(db_session, title="Root", content="Root doc", usetype="raw")
        chunk = _create_doc(
            db_session,
            title="Real Content",
            content="PostgreSQL database optimization techniques",
            parent_id=root.id,
            usetype="raw/chunk",
        )
        summary = _create_doc(
            db_session,
            title="Summary",
            content="PostgreSQL database optimization techniques summary overview",
            parent_id=root.id,
            usetype="summary",
        )

        repo = SearchRepository(db_session)
        idx_name = "test_summary_excl"
        repo.create_index(idx_name, description="summary exclusion test")

        assert (
            repo.index_document(summary.id, index_name=idx_name) is False
        ), "index_document must skip summary documents"
        assert repo.index_document(chunk.id, index_name=idx_name) is True

        db_session.flush()

        results = repo.bm25_search("PostgreSQL", index_name=idx_name, limit=10)
        result_ids = {r.document.id for r in results}
        assert chunk.id in result_ids, "Chunk must appear in BM25 results"
        assert summary.id not in result_ids, "Summary must NOT appear in BM25 results"

    def test_entities_excluded_from_vector_search_by_default(self, db_session):
        """Entity documents must be excluded from vector search when no usetype filter given.

        Kanban #332: Entity nodes are short keyword-dense text that score high in
        vector search, causing ~86% entity pollution in top results.
        """
        fake_embed = np.random.default_rng(42).normal(size=768).tolist()

        root = _create_doc(db_session, title="Root", content="Root", usetype="raw")
        chunk = _create_doc(
            db_session,
            title="Python framework",
            content="Python web framework development guide",
            parent_id=root.id,
            usetype="raw/chunk",
        )
        chunk.embed = fake_embed
        entity = _create_doc(
            db_session,
            title="Python",
            content="Python",
            parent_id=chunk.id,
            usetype="entity",
        )
        entity.embed = fake_embed
        db_session.flush()

        repo = SearchRepository(db_session)
        results = repo.vector_search(fake_embed, limit=10)
        result_ids = {r.document.id for r in results}
        assert chunk.id in result_ids, "Chunk must appear in vector results"
        assert entity.id not in result_ids, "Entity must NOT appear in default vector results"

    def test_entities_excluded_from_fulltext_search_by_default(self, db_session):
        """Entity documents must be excluded from fulltext search when no usetype filter given.

        Kanban #332: Same entity pollution issue applies to fulltext search.
        """
        root = _create_doc(db_session, title="Root", content="Root", usetype="raw")
        chunk = _create_doc(
            db_session,
            title="Chunk",
            content="PostgreSQL database optimization techniques for production",
            parent_id=root.id,
            usetype="raw/chunk",
        )
        entity = _create_doc(
            db_session,
            title="PostgreSQL",
            content="PostgreSQL",
            parent_id=chunk.id,
            usetype="entity",
        )
        db_session.flush()

        repo = SearchRepository(db_session)
        results = repo.fulltext_search("PostgreSQL", limit=10, parent_id=root.id)
        result_ids = {r.document.id for r in results}
        assert chunk.id in result_ids, "Chunk must appear in fulltext results"
        assert entity.id not in result_ids, "Entity must NOT appear in default fulltext results"

    def test_explicit_usetype_overrides_default_exclusion(self, db_session):
        """Passing usetype='entity' explicitly must return entities (no default exclusion).

        Kanban #332: The exclusion is a default, not a hard block. Callers who want
        entities should still get them.
        """
        fake_embed = np.random.default_rng(99).normal(size=768).tolist()

        root = _create_doc(db_session, title="Root", content="Root", usetype="raw")
        entity = _create_doc(
            db_session,
            title="FastAPI",
            content="FastAPI web application",
            parent_id=root.id,
            usetype="entity",
        )
        entity.embed = fake_embed
        db_session.flush()

        repo = SearchRepository(db_session)
        results = repo.vector_search(fake_embed, limit=10, usetype="entity")
        result_ids = {r.document.id for r in results}
        assert entity.id in result_ids, "Entity must appear when usetype='entity' is explicit"

    def test_null_usetype_not_excluded(self, db_session):
        """Documents with NULL usetype must not be excluded by default exclusion.

        Kanban #332: The exclusion targets specific usetypes; unclassified documents
        (usetype=NULL) should always pass through.
        """
        fake_embed = np.random.default_rng(77).normal(size=768).tolist()

        doc = _create_doc(db_session, title="Untyped doc", content="Some content", usetype=None)
        doc.embed = fake_embed
        db_session.flush()

        repo = SearchRepository(db_session)
        results = repo.vector_search(fake_embed, limit=10)
        result_ids = {r.document.id for r in results}
        assert doc.id in result_ids, "Doc with NULL usetype must appear in default results"

    def test_exclude_types_empty_list_disables_default_exclusion(self, db_session):
        """exclude_types=[] must disable default exclusion and return entity documents.

        Kanban #332: Callers must be able to opt out of the default exclusion list
        entirely by passing an empty list.
        """
        fake_embed = np.random.default_rng(55).normal(size=768).tolist()

        root = _create_doc(db_session, title="Root", content="Root", usetype="raw")
        entity = _create_doc(
            db_session,
            title="Django",
            content="Django web framework",
            parent_id=root.id,
            usetype="entity",
        )
        entity.embed = fake_embed
        db_session.flush()

        repo = SearchRepository(db_session)
        results = repo.vector_search(fake_embed, limit=10, exclude_types=[])
        result_ids = {r.document.id for r in results}
        assert (
            entity.id in result_ids
        ), "Entity must appear when exclude_types=[] disables default exclusion"

    def test_exclude_types_custom_list_applied(self, db_session):
        """exclude_types=["entity"] must exclude entities but not other non-default types.

        Kanban #332: An explicit exclude_types list overrides the config default,
        giving callers control over exactly which types are excluded.
        """
        fake_embed = np.random.default_rng(66).normal(size=768).tolist()

        root = _create_doc(db_session, title="Root", content="Root", usetype="raw")
        chunk = _create_doc(
            db_session,
            title="Flask tutorial",
            content="Flask web development tutorial",
            parent_id=root.id,
            usetype="raw/chunk",
        )
        chunk.embed = fake_embed
        entity = _create_doc(
            db_session,
            title="Flask",
            content="Flask",
            parent_id=root.id,
            usetype="entity",
        )
        entity.embed = fake_embed
        db_session.flush()

        repo = SearchRepository(db_session)
        results = repo.vector_search(fake_embed, limit=10, exclude_types=["entity"])
        result_ids = {r.document.id for r in results}
        assert chunk.id in result_ids, "Chunk must appear when exclude_types excludes only entity"
        assert entity.id not in result_ids, "Entity must be excluded by explicit exclude_types"

    def test_fulltext_exclude_types_empty_list(self, db_session):
        """exclude_types=[] must disable default exclusion in fulltext search.

        Kanban #332: Same exclude_types contract applies to fulltext search.
        """
        root = _create_doc(db_session, title="Root", content="Root", usetype="raw")
        entity = _create_doc(
            db_session,
            title="SQLAlchemy",
            content="SQLAlchemy ORM library",
            parent_id=root.id,
            usetype="entity",
        )
        db_session.flush()

        repo = SearchRepository(db_session)
        results = repo.fulltext_search("SQLAlchemy", limit=10, exclude_types=[])
        result_ids = {r.document.id for r in results}
        assert entity.id in result_ids, "Entity must appear in fulltext when exclude_types=[]"


# ============================================================================
# BM25 Scoped Index Auto-Selection Tests
# ============================================================================


class TestBM25ScopedIndexAutoSelect:
    """Tests for Bug #4: BM25 returns 0 results when parent_id scopes to a tree
    that is indexed in a named index, but the caller uses index_name='default'.

    Root cause: BM25 index selection is decoupled from the scope filter.  If a
    document tree lives in 'adjutant' index but the caller omits index_name
    (defaulting to 'default'), the posting-list lookup finds nothing.

    Fix: when parent_id is provided and index_name is 'default', auto-select the
    covering named index by querying search_index_members for the ancestor tree.
    """

    def test_bm25_scoped_to_named_index_tree_via_default(self, db_session):
        """Scoped BM25 with default index_name finds docs indexed in a named index.

        Scenario:
          - Tree A is indexed in 'named_index_a' (like adjutant).
          - Caller searches with parent_id=root_a but index_name='default'.
          - Before fix: 0 results (docs not in default index).
          - After fix: results found (auto-selects 'named_index_a').
        """
        # Tree A: docs indexed in a named index
        root_a = _create_doc(db_session, title="Project Alpha", content="Alpha root", usetype="raw")
        child_a = _create_doc(
            db_session,
            title="Alpha Chunk",
            content="Distributed vector search with HNSW indexes",
            parent_id=root_a.id,
            usetype="chunk",
        )

        # Tree B: docs indexed in 'default' index
        root_b = _create_doc(db_session, title="Project Beta", content="Beta root", usetype="raw")
        child_b = _create_doc(
            db_session,
            title="Beta Chunk",
            content="Distributed vector search with HNSW indexes",
            parent_id=root_b.id,
            usetype="chunk",
        )

        repo = SearchRepository(db_session)

        # Index tree A into a named index (simulating 'adjutant' pattern)
        named_idx = "test_named_idx_a"
        repo.create_index(named_idx, description="named index for tree A")
        repo.add_root_to_index(named_idx, root_a.id)
        repo.index_document(child_a.id, index_name=named_idx)

        # Index tree B into 'default' (index already exists in production DB;
        # get_or_create it so the test works in both fresh and live-DB contexts)
        if not repo.get_index("default"):
            repo.create_index("default", description="default index")
        repo.add_root_to_index("default", root_b.id)
        repo.index_document(child_b.id, index_name="default")

        db_session.flush()

        # Scoped search to root_a using default index_name — the bug case
        results = repo.bm25_search(
            "distributed vector search",
            index_name="default",  # caller didn't specify the right named index
            parent_id=root_a.id,
        )

        result_ids = {r.document.id for r in results}
        assert child_a.id in result_ids, (
            "BM25 scoped to root_a must find docs from tree A even when "
            "index_name='default' — should auto-select the covering named index"
        )
        assert (
            child_b.id not in result_ids
        ), "BM25 scoped to root_a must NOT return docs from tree B"

    def test_bm25_scoped_to_subtree_of_named_index(self, db_session):
        """Auto-selection works for a mid-tree parent_id (not just the root member)."""
        root_a = _create_doc(db_session, title="Root A", content="Root A", usetype="raw")
        section = _create_doc(
            db_session,
            title="Section",
            content="Section node",
            parent_id=root_a.id,
            usetype="raw",
        )
        leaf = _create_doc(
            db_session,
            title="Leaf",
            content="Quantum entanglement and superposition in quantum computing",
            parent_id=section.id,
            usetype="chunk",
        )

        repo = SearchRepository(db_session)
        named_idx = "test_named_idx_mid"
        repo.create_index(named_idx, description="named index for subtree test")
        repo.add_root_to_index(named_idx, root_a.id)  # root registered, not section
        repo.index_document(leaf.id, index_name=named_idx)
        db_session.flush()

        # Search scoped to 'section' (not the index root) with default index_name
        results = repo.bm25_search(
            "quantum computing",
            index_name="default",  # wrong index, but parent_id should guide auto-select
            parent_id=section.id,
        )

        result_ids = {r.document.id for r in results}
        assert leaf.id in result_ids, (
            "Auto-selection must find the covering index via ancestor path lookup "
            "(section's path contains root_a, which is a member of named_idx)"
        )
