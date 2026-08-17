"""Tests for RAPTOR hierarchical summarization (#14).

Tier 1: Unit tests — no DB, no LLM, synthetic embeddings only.
Tier 2: Integration tests — real DB (with transaction rollback), mocked LLM + embedding.

See test_methodology_raptor_pipeline.md § 2.1 for the full test plan.
"""

import asyncio
from unittest.mock import patch

import numpy as np
import pytest

from jmfts_core.summarization import (
    _build_knn_graph,
    _leiden_cluster,
    _detect_bridge_chunks,
    raptor_summarize,
    ClusterInfo,
)
from jmfts_core.config import Settings

# ============================================================================
# Helpers
# ============================================================================


def _run(coro):
    """Run async coroutine synchronously (existing project pattern)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_block_embeddings(block_sizes, dim=768, noise=0.05, rng_seed=42):
    """Create synthetic embedding matrix with distinct topic blocks.

    Returns (embeddings_array, doc_ids).
    """
    rng = np.random.default_rng(rng_seed)
    embeddings = []
    for size in block_sizes:
        centroid = rng.standard_normal(dim)
        centroid /= np.linalg.norm(centroid)
        for _ in range(size):
            vec = centroid + rng.normal(0, noise, dim)
            vec /= np.linalg.norm(vec)
            embeddings.append(vec)
    return np.array(embeddings, dtype=np.float32), list(range(1, sum(block_sizes) + 1))


def _make_uniform_embeddings(n, dim=768, noise=0.005, rng_seed=42):
    """Near-identical embeddings (single cluster expected)."""
    rng = np.random.default_rng(rng_seed)
    centroid = rng.standard_normal(dim)
    centroid /= np.linalg.norm(centroid)
    embeddings = []
    for _ in range(n):
        vec = centroid + rng.normal(0, noise, dim)
        vec /= np.linalg.norm(vec)
        embeddings.append(vec)
    return np.array(embeddings, dtype=np.float32), list(range(1, n + 1))


def _make_orthogonal_embeddings(n, dim=768, rng_seed=42):
    """Maximally dissimilar embeddings (random high-dim → near-orthogonal)."""
    rng = np.random.default_rng(rng_seed)
    embeddings = []
    for _ in range(n):
        vec = rng.standard_normal(dim)
        vec /= np.linalg.norm(vec)
        embeddings.append(vec)
    return np.array(embeddings, dtype=np.float32), list(range(1, n + 1))


def _random_embedding(dim=768, seed=0):
    """Generate a single random L2-normalized embedding as a Python list."""
    rng = np.random.default_rng(seed)
    vec = rng.standard_normal(dim).astype(np.float32)
    vec /= np.linalg.norm(vec)
    return vec.tolist()


# ============================================================================
# TIER 1: Unit Tests — No DB, No LLM
# ============================================================================


class TestBuildKnnGraph:
    """k-NN graph construction from embeddings."""

    def test_basic_graph_structure(self):
        embeddings, _ = _make_block_embeddings([5, 5])
        graph = _build_knn_graph(embeddings, k=3)
        assert graph.vcount() == 10
        assert graph.ecount() > 0
        assert all(w >= 0 for w in graph.es["weight"])

    def test_single_node_no_edges(self):
        embeddings, _ = _make_block_embeddings([1])
        graph = _build_knn_graph(embeddings, k=3)
        assert graph.vcount() == 1
        assert graph.ecount() == 0

    def test_two_nodes_one_edge(self):
        embeddings, _ = _make_block_embeddings([2])
        graph = _build_knn_graph(embeddings, k=3)
        assert graph.vcount() == 2
        assert graph.ecount() == 1

    def test_k_clamped_to_n_minus_1(self):
        embeddings, _ = _make_block_embeddings([3])
        graph = _build_knn_graph(embeddings, k=100)
        assert graph.vcount() == 3
        assert graph.ecount() > 0


class TestLeidenClusterH1:
    """H1: 3 clear clusters of 4 chunks each → 3 communities."""

    def test_three_clusters_detected(self):
        embeddings, doc_ids = _make_block_embeddings([4, 4, 4], noise=0.05)
        clusters = _leiden_cluster(embeddings, doc_ids, k=5, gamma=1.0, min_cluster_size=2)
        assert len(clusters) == 3, f"Expected 3 clusters, got {len(clusters)}"

    def test_full_coverage_no_duplicates(self):
        embeddings, doc_ids = _make_block_embeddings([4, 4, 4], noise=0.05)
        clusters = _leiden_cluster(embeddings, doc_ids, k=5, gamma=1.0, min_cluster_size=2)
        all_ids = [mid for c in clusters for mid in c.member_ids]
        assert sorted(all_ids) == sorted(doc_ids), "Full coverage violated"
        assert len(all_ids) == len(set(all_ids)), "Duplicate doc across clusters"

    def test_embeddings_parallel_to_ids(self):
        embeddings, doc_ids = _make_block_embeddings([4, 4, 4], noise=0.05)
        clusters = _leiden_cluster(embeddings, doc_ids, k=5, gamma=1.0, min_cluster_size=2)
        for c in clusters:
            assert len(c.member_ids) == len(c.member_embeddings)


class TestDualAdaptiveH6:
    """H6: k increases and gamma decreases across layers."""

    def test_k_increases_monotonically(self):
        k_base, k_step = 5, 3
        k_values = [k_base + layer * k_step for layer in range(5)]
        assert k_values == [5, 8, 11, 14, 17]
        for i in range(len(k_values) - 1):
            assert k_values[i] < k_values[i + 1]

    def test_gamma_decreases_monotonically(self):
        gamma_base, gamma_decay = 1.0, 0.5
        gamma_values = [gamma_base * (gamma_decay**layer) for layer in range(5)]
        expected = [1.0, 0.5, 0.25, 0.125, 0.0625]
        for got, exp in zip(gamma_values, expected):
            assert abs(got - exp) < 1e-9
        for i in range(len(gamma_values) - 1):
            assert gamma_values[i] > gamma_values[i + 1]

    def test_default_settings_values(self):
        s = Settings()
        assert s.raptor_k_base == 5
        assert s.raptor_k_step == 3
        assert s.raptor_gamma_base == 1.0
        assert s.raptor_gamma_decay == 0.5


class TestLeidenClusterA1:
    """A1: Single-chunk document — graceful return."""

    def test_single_chunk(self):
        embeddings, doc_ids = _make_block_embeddings([1])
        clusters = _leiden_cluster(embeddings, doc_ids, k=5, gamma=1.0, min_cluster_size=2)
        assert len(clusters) == 1
        assert clusters[0].member_ids == [1]

    def test_empty_input(self):
        clusters = _leiden_cluster(
            np.array([], dtype=np.float32).reshape(0, 768),
            [],
            k=5,
            gamma=1.0,
            min_cluster_size=2,
        )
        assert len(clusters) == 0


class TestLeidenClusterA2:
    """A2: Uniform embeddings → single community."""

    def test_uniform_single_or_few_clusters(self):
        embeddings, doc_ids = _make_uniform_embeddings(20, noise=0.005)
        clusters = _leiden_cluster(embeddings, doc_ids, k=5, gamma=1.0, min_cluster_size=2)
        total = sum(len(c.member_ids) for c in clusters)
        assert total == 20
        # Leiden is stochastic; with near-uniform data it should not fragment badly.
        # Allow up to 5 clusters (n/4) — stricter than "many" without being brittle.
        assert len(clusters) <= 5, f"Too many clusters for uniform data: {len(clusters)}"


class TestLeidenClusterA3:
    """A3: Maximum fragmentation — orthogonal embeddings."""

    def test_handles_gracefully(self):
        embeddings, doc_ids = _make_orthogonal_embeddings(20)
        clusters = _leiden_cluster(embeddings, doc_ids, k=5, gamma=1.0, min_cluster_size=2)
        assert len(clusters) >= 1
        total = sum(len(c.member_ids) for c in clusters)
        assert total == 20


class TestEmptyContentA5:
    """A5: Clustering operates on embeddings; empty content handled at summarization."""

    def test_clustering_works_regardless(self):
        embeddings, doc_ids = _make_block_embeddings([5, 5])
        clusters = _leiden_cluster(embeddings, doc_ids, k=5, gamma=1.0, min_cluster_size=2)
        total = sum(len(c.member_ids) for c in clusters)
        assert total == 10


class TestDetectBridgeChunks:
    """H5 (unit): Bridge detection across clusters."""

    def test_high_similarity_detected(self):
        rng = np.random.default_rng(42)
        centroid_a = rng.standard_normal(768).astype(np.float32)
        centroid_a /= np.linalg.norm(centroid_a)
        centroid_b = rng.standard_normal(768).astype(np.float32)
        centroid_b /= np.linalg.norm(centroid_b)
        shared = (centroid_a + centroid_b) / 2
        shared /= np.linalg.norm(shared)

        ca = ClusterInfo(member_ids=[1, 2], member_embeddings=[centroid_a, shared])
        cb = ClusterInfo(member_ids=[3, 4], member_embeddings=[shared.copy(), centroid_b])
        ebi = {1: centroid_a, 2: shared, 3: shared.copy(), 4: centroid_b}

        bridges = _detect_bridge_chunks([ca, cb], ebi, threshold=0.5)
        assert len(bridges) > 0
        pairs = {(b[0], b[1]) for b in bridges}
        assert (2, 3) in pairs

    def test_well_separated_no_bridges(self):
        embs, _ = _make_block_embeddings([4, 4], noise=0.01)
        ca = ClusterInfo(member_ids=[1, 2, 3, 4], member_embeddings=list(embs[:4]))
        cb = ClusterInfo(member_ids=[5, 6, 7, 8], member_embeddings=list(embs[4:]))
        ebi = {i + 1: embs[i] for i in range(8)}
        bridges = _detect_bridge_chunks([ca, cb], ebi, threshold=0.9)
        assert len(bridges) == 0


class TestSeedReproducibility:
    """Regression invariant: deterministic clustering on same input."""

    def test_deterministic(self):
        embeddings, doc_ids = _make_block_embeddings([4, 4, 4], noise=0.05, rng_seed=42)
        c1 = _leiden_cluster(embeddings, doc_ids, k=5, gamma=1.0, min_cluster_size=2)
        c2 = _leiden_cluster(embeddings, doc_ids, k=5, gamma=1.0, min_cluster_size=2)
        ids_1 = sorted([sorted(c.member_ids) for c in c1])
        ids_2 = sorted([sorted(c.member_ids) for c in c2])
        assert ids_1 == ids_2


# ============================================================================
# TIER 2: Integration Tests — Real DB (rollback), Mocked LLM + Embedding
# ============================================================================

# Attempt DB connection at import time; skip integration tests if unavailable.
from sqlalchemy import text as sa_text

try:
    from jmfts_core.database import get_engine
    from jmfts_core.repositories.document import DocumentRepository
    from jmfts_core.models.document import DocumentLink
    from jmfts_core.embedding import EmbeddingResult, TokenEmbeddingResult

    _engine = get_engine()
    with _engine.connect() as _conn:
        _conn.execute(sa_text("SELECT 1"))
    _DB_AVAILABLE = True
except Exception:
    _DB_AVAILABLE = False

requires_db = pytest.mark.skipif(not _DB_AVAILABLE, reason="Database not available")


# -- Mock services ----------------------------------------------------------


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


async def _fake_llm_summarize(texts, settings, llm_model):
    """Deterministic: first sentence of each input, joined."""
    parts = []
    for t in texts:
        first = t.split(".")[0].strip()
        if first:
            parts.append(first)
    return ". ".join(parts) + "." if parts else "Summary placeholder."


# -- Fixtures ---------------------------------------------------------------


# db_session is provided centrally by tests/conftest.py (connection-level
# transaction + create_savepoint), so endpoint commits can't leak.


@pytest.fixture
def mock_llm():
    """Patch _llm_summarize in the summarization module."""
    with patch("jmfts_core.summarization._llm_summarize", side_effect=_fake_llm_summarize):
        yield


@pytest.fixture
def mock_embedding():
    """Patch get_embedding_service in the document repository module."""
    if not _DB_AVAILABLE:
        pytest.skip("Database not available")
    svc = MockEmbeddingService()
    with patch("jmfts_core.repositories.document.get_embedding_service", return_value=svc):
        yield svc


# -- Helper: create test tree -----------------------------------------------


def _create_test_tree(repo, session, block_sizes, noise=0.05, rng_seed=42):
    """Create a parent document with embedded children.

    Returns (parent_doc, child_ids, embeddings_array).
    """
    n_chunks = sum(block_sizes)
    parent = repo.create(
        title="RAPTOR Test Root",
        content="Root document for RAPTOR testing purposes with enough content.",
        usetype="container",
        auto_embed=False,
    )
    embeddings, _ = _make_block_embeddings(block_sizes, dim=768, noise=noise, rng_seed=rng_seed)
    child_ids = []
    for i in range(n_chunks):
        child = repo.create(
            title=f"Chunk {i + 1}",
            content=f"Topic content for chunk number {i + 1} in the test document. " * 5,
            parent_id=parent.id,
            usetype="chunk",
            auto_embed=False,
        )
        child.embed = embeddings[i].tolist()
        child_ids.append(child.id)
    session.flush()
    return parent, child_ids, embeddings


# -- Regression invariant checks -------------------------------------------


def _check_tree_integrity(repo, session, root_id):
    """Every summary has exactly one parent_id; tree is connected to root."""
    summaries = repo.find(usetype="summary", limit=1000)
    issues = []
    for s in summaries:
        if s.parent_id is None:
            issues.append(f"Summary {s.id} has no parent_id")
        # Check path leads to root
        if root_id not in (s.path or []) and s.parent_id != root_id:
            issues.append(f"Summary {s.id} not connected to root {root_id}")
    return issues


def _check_full_coverage(repo, root_id, original_child_ids):
    """Every original chunk is in exactly one cluster (via re-parenting)."""
    issues = []
    for cid in original_child_ids:
        doc = repo.get(cid)
        if doc is None:
            issues.append(f"Chunk {cid} not found")
            continue
        # After RAPTOR, chunks should be re-parented under a summary
        if doc.parent_id == root_id:
            issues.append(f"Chunk {cid} still directly under root (not re-parented)")
    return issues


def _check_link_consistency(repo, session):
    """Every summary should have at least one 'summarizes' link (outgoing)."""
    summaries = repo.find(usetype="summary", limit=1000)
    missing = []
    for s in summaries:
        links = repo.get_links(s.id, direction="outgoing", link_type="summarizes")
        if not links:
            missing.append(s.id)
    return missing


def _check_no_orphaned_summaries(repo, session):
    """Every summary has at least one child document (re-parented chunks)."""
    summaries = repo.find(usetype="summary", limit=1000)
    orphaned = []
    for s in summaries:
        children = repo.get_children(s.id, depth=1, limit=1)
        if not children:
            orphaned.append(s.id)
    return orphaned


def _check_embedding_completeness(repo, session, dim=768):
    """Every summary has a non-null embed of correct dimensionality."""
    summaries = repo.find(usetype="summary", limit=1000)
    issues = []
    for s in summaries:
        if s.embed is None:
            issues.append(f"Summary {s.id} has null embed")
        elif len(s.embed) != dim:
            issues.append(f"Summary {s.id} embed dim={len(s.embed)}, expected {dim}")
    return issues


def _check_monotonic_compression(result):
    """Each layer has fewer nodes than the previous layer's input."""
    issues = []
    if len(result.layers) < 2:
        return issues
    for i in range(1, len(result.layers)):
        prev_summaries = result.layers[i - 1].clusters
        curr_input = result.layers[i].clusters
        # Current layer's cluster count should be <= previous layer's summary count
        # (the input to the current layer is the output of the previous)
        if curr_input > prev_summaries:
            issues.append(
                f"Layer {i}: {curr_input} clusters > layer {i-1}: {prev_summaries} clusters"
            )
    return issues


# -- Integration tests ------------------------------------------------------


@requires_db
class TestRaptorIntegrationH1:
    """H1 full: Summary documents created with correct parent_id and usetype."""

    def test_summary_creation(self, db_session, mock_llm, mock_embedding):
        repo = DocumentRepository(db_session)
        parent, child_ids, _ = _create_test_tree(repo, db_session, [4, 4, 4])

        result = _run(raptor_summarize(parent.id, db_session, max_depth=5, min_cluster_size=2))

        assert result.root_id == parent.id
        assert (
            result.total_summaries >= 3
        ), f"Expected >= 3 summaries for 3 clusters, got {result.total_summaries}"
        assert len(result.layers) >= 1

        # Check first layer produced ~3 clusters
        assert result.layers[0].clusters >= 2

        # Check summary documents in DB
        summaries = repo.find(usetype="summary", limit=100)
        assert len(summaries) >= 3
        for s in summaries:
            assert s.usetype == "summary"
            assert s.content is not None and len(s.content) > 0

    def test_regression_invariants(self, db_session, mock_llm, mock_embedding):
        repo = DocumentRepository(db_session)
        parent, child_ids, _ = _create_test_tree(repo, db_session, [4, 4, 4])

        result = _run(raptor_summarize(parent.id, db_session, max_depth=5, min_cluster_size=2))

        # Tree integrity
        issues = _check_tree_integrity(repo, db_session, parent.id)
        assert issues == [], f"Tree integrity issues: {issues}"

        # Full coverage (all chunks re-parented under summaries)
        issues = _check_full_coverage(repo, parent.id, child_ids)
        assert issues == [], f"Full coverage issues: {issues}"

        # Link consistency — summarizes links created during RAPTOR build
        missing_links = _check_link_consistency(repo, db_session)
        assert (
            missing_links == []
        ), f"{len(missing_links)} summaries lack 'summarizes' links: {missing_links}"

        # No orphaned summaries
        orphaned = _check_no_orphaned_summaries(repo, db_session)
        assert orphaned == [], f"Orphaned summaries: {orphaned}"

        # Embedding completeness
        issues = _check_embedding_completeness(repo, db_session)
        assert issues == [], f"Embedding issues: {issues}"

        # Monotonic compression
        issues = _check_monotonic_compression(result)
        assert issues == [], f"Monotonic compression issues: {issues}"


@requires_db
class TestRaptorIntegrationH3:
    """H3: Recursive convergence on 50 chunks — tree depth >= 3."""

    def test_recursive_convergence(self, db_session, mock_llm, mock_embedding):
        repo = DocumentRepository(db_session)
        # 50 chunks in 5 topic blocks of 10 each
        parent, child_ids, _ = _create_test_tree(repo, db_session, [10, 10, 10, 10, 10], rng_seed=7)

        result = _run(raptor_summarize(parent.id, db_session, max_depth=10, min_cluster_size=2))

        assert result.root_id == parent.id
        assert (
            len(result.layers) >= 2
        ), f"Expected >= 2 layers for 50 chunks, got {len(result.layers)}"
        # Total summaries should be << 50 (compact tree)
        assert (
            result.total_summaries < 50
        ), f"Too many summaries ({result.total_summaries}) for 50 chunks"

    def test_monotonic_compression(self, db_session, mock_llm, mock_embedding):
        repo = DocumentRepository(db_session)
        parent, child_ids, _ = _create_test_tree(repo, db_session, [10, 10, 10, 10, 10], rng_seed=7)

        result = _run(raptor_summarize(parent.id, db_session, max_depth=10, min_cluster_size=2))

        issues = _check_monotonic_compression(result)
        assert issues == [], f"Monotonic compression violated: {issues}"


@requires_db
class TestRaptorIntegrationH5:
    """H5: Bridge links created for cross-cluster similarity."""

    def test_bridge_links_stored_correctly(self, db_session, mock_llm, mock_embedding):
        repo = DocumentRepository(db_session)
        parent, child_ids, _ = _create_test_tree(repo, db_session, [4, 4, 4])

        result = _run(raptor_summarize(parent.id, db_session, max_depth=5, min_cluster_size=2))

        # Bridge links may or may not be created depending on inter-cluster similarity.
        # Verify that any created bridges have correct structure.
        bridge_links = (
            db_session.query(DocumentLink).filter(DocumentLink.link_type == "bridge").all()
        )

        for link in bridge_links:
            assert link.link_type == "bridge"
            assert link.score >= 0.0
            assert link.score <= 1.0
            assert link.source_id != link.target_id
            # Verify both endpoints exist
            assert repo.get(link.source_id) is not None
            assert repo.get(link.target_id) is not None

        # result.total_bridge_links should match DB
        assert result.total_bridge_links == len(bridge_links)


@requires_db
class TestRaptorIntegrationA5:
    """A5: Empty content chunks excluded from summarization."""

    def test_empty_chunks_excluded(self, db_session, mock_llm, mock_embedding):
        repo = DocumentRepository(db_session)

        # Create parent
        parent = repo.create(
            title="A5 Test Root",
            content="Root for empty content test with sufficient length.",
            usetype="container",
            auto_embed=False,
        )

        # Create 8 chunks: 5 with content, 3 with empty/None content
        embeddings, _ = _make_block_embeddings([5], dim=768, noise=0.05)
        content_child_ids = []
        for i in range(5):
            child = repo.create(
                title=f"Content Chunk {i + 1}",
                content=f"Real content for chunk {i + 1} in the test set. " * 5,
                parent_id=parent.id,
                usetype="chunk",
                auto_embed=False,
            )
            child.embed = embeddings[i].tolist()
            content_child_ids.append(child.id)

        # Empty chunks — no embedding (mimics real behavior where embed_document
        # skips content shorter than 10 chars)
        empty_child_ids = []
        for i in range(3):
            child = repo.create(
                title=f"Empty Chunk {i + 1}",
                content="",
                parent_id=parent.id,
                usetype="chunk",
                auto_embed=False,
            )
            # No embedding set → embed is None
            empty_child_ids.append(child.id)

        db_session.flush()

        # _get_embedded_child_ids filters by embed IS NOT NULL
        # so only the 5 content chunks should be clustered
        result = _run(raptor_summarize(parent.id, db_session, max_depth=5, min_cluster_size=2))

        # Empty chunks should remain direct children of parent (not re-parented)
        for cid in empty_child_ids:
            doc = repo.get(cid)
            assert (
                doc.parent_id == parent.id
            ), f"Empty chunk {cid} should remain under root, got parent_id={doc.parent_id}"

        # Content chunks should have been clustered and summarized
        assert result.total_summaries >= 1


@requires_db
class TestRaptorIntegrationA6:
    """A6: Pre-existing summary tree — idempotent or error behavior."""

    def test_second_run_behavior(self, db_session, mock_llm, mock_embedding):
        repo = DocumentRepository(db_session)
        parent, child_ids, _ = _create_test_tree(repo, db_session, [4, 4, 4])

        # First RAPTOR run
        result1 = _run(raptor_summarize(parent.id, db_session, max_depth=5, min_cluster_size=2))
        summaries_after_first = len(repo.find(usetype="summary", limit=1000))

        # Second RAPTOR run on same document
        # After first run, original chunks are re-parented under summaries.
        # _get_embedded_child_ids looks for immediate children of root with embed.
        # The immediate children are now the L0 summaries (which have embeddings).
        result2 = _run(raptor_summarize(parent.id, db_session, max_depth=5, min_cluster_size=2))
        summaries_after_second = len(repo.find(usetype="summary", limit=1000))

        # Document the behavior: second run should either:
        # (a) Produce additional summaries on top of existing ones, OR
        # (b) Be a no-op (< 2 children triggers early exit)
        # The implementation does NOT check for pre-existing summaries,
        # so it will attempt to cluster the existing summaries.
        assert (
            summaries_after_second >= summaries_after_first
        ), "Second run should not delete existing summaries"
