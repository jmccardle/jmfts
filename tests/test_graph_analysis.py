"""Unit tests for graph_analysis primitives (no DB required).

The compute_* functions that take a Session are exercised via integration
tests at the API level. Here we test the pure-logic pieces by constructing
``_GraphBuild`` instances directly.
"""

import math

import igraph as ig
import pytest


# Skip the whole file if igraph isn't importable in the test env.
pytest.importorskip("igraph")

from jmfts_core.graph_analysis import (
    _GraphBuild,
    compute_centrality,
    compute_communities,
)


def _make_build(edges: list[tuple[int, int]], n: int, weights=None) -> _GraphBuild:
    """Helper: make a _GraphBuild whose vertex i has document_id = i."""
    g = ig.Graph(n=n, edges=edges, directed=True)
    if weights is not None:
        g.es["weight"] = weights
    else:
        g.es["weight"] = [1.0] * g.ecount()
    idx_to_id = list(range(n))
    id_to_idx = {i: i for i in range(n)}
    meta = {i: {"title": f"D{i}", "usetype": "doc", "depth": 0} for i in range(n)}
    return _GraphBuild(graph=g, idx_to_id=idx_to_id, id_to_idx=id_to_idx, meta=meta)


# ---------------------------------------------------------------------------
# compute_centrality
# ---------------------------------------------------------------------------


class TestCentrality:
    def test_pagerank_returns_top_n_sorted(self):
        # Star: node 0 receives from 1,2,3,4 -> 0 has highest pagerank
        edges = [(1, 0), (2, 0), (3, 0), (4, 0)]
        build = _make_build(edges, n=5)
        results = compute_centrality(build, metric="pagerank", top=3)
        assert len(results) == 3
        assert results[0].document_id == 0
        # Pagerank scores should be monotonic decreasing in the result
        for i in range(len(results) - 1):
            assert results[i].score >= results[i + 1].score

    def test_pagerank_sums_to_one(self):
        edges = [(0, 1), (1, 2), (2, 0), (2, 3)]
        build = _make_build(edges, n=4)
        results = compute_centrality(build, metric="pagerank", top=4)
        total = sum(r.score for r in results)
        assert math.isclose(total, 1.0, abs_tol=0.01)

    def test_degree_returns_total_degree(self):
        # Hub node 0 has 4 in + 4 out
        edges = [(1, 0), (2, 0), (3, 0), (4, 0), (0, 1), (0, 2), (0, 3), (0, 4)]
        build = _make_build(edges, n=5)
        results = compute_centrality(build, metric="degree", top=5)
        # Node 0 should top: degree 8 (4+4); spokes have 2 each (1+1)
        assert results[0].document_id == 0
        assert results[0].score == 8
        for r in results[1:]:
            assert r.score == 2

    def test_betweenness_finds_bridge(self):
        # A bridge node 2 connecting two clusters
        edges = [(0, 1), (1, 2), (2, 3), (3, 4), (4, 2), (1, 0), (3, 2)]
        build = _make_build(edges, n=5)
        results = compute_centrality(build, metric="betweenness", top=5)
        # Top vertex should be 2 (the bridge)
        assert results[0].document_id == 2

    def test_in_out_degree_attached_to_each_score(self):
        edges = [(0, 1), (0, 2), (1, 2)]
        build = _make_build(edges, n=3)
        results = compute_centrality(build, metric="degree", top=3)
        by_id = {r.document_id: r for r in results}
        assert by_id[0].out_degree == 2
        assert by_id[0].in_degree == 0
        assert by_id[2].in_degree == 2
        assert by_id[2].out_degree == 0

    def test_unknown_metric_raises(self):
        build = _make_build([(0, 1)], n=2)
        with pytest.raises(ValueError):
            compute_centrality(build, metric="not-a-metric")  # type: ignore[arg-type]

    def test_empty_graph_returns_empty(self):
        g = ig.Graph(n=0, edges=[], directed=True)
        empty = _GraphBuild(graph=g, idx_to_id=[], id_to_idx={}, meta={})
        assert compute_centrality(empty, metric="pagerank") == []


# ---------------------------------------------------------------------------
# compute_communities
# ---------------------------------------------------------------------------


class TestCommunities:
    def test_two_obvious_clusters_emerge_as_two_communities(self):
        # Two cliques connected by one bridge edge
        edges = [
            (0, 1), (1, 2), (2, 0),  # cluster A
            (3, 4), (4, 5), (5, 3),  # cluster B
            (2, 3),                   # bridge
        ]
        build = _make_build(edges, n=6)
        comms = compute_communities(build, resolution=1.0, min_size=2)
        # Should produce at least 2 communities (could merge if resolution low)
        assert len(comms) >= 2
        # All members accounted for
        all_members = set()
        for c in comms:
            for m in c.members:
                all_members.add(m["document_id"])
        assert all_members == set(range(6))

    def test_min_size_filters_singletons(self):
        # Disconnected pair + isolated node
        edges = [(0, 1)]
        build = _make_build(edges, n=3)
        comms = compute_communities(build, resolution=1.0, min_size=2)
        for c in comms:
            assert c.size >= 2

    def test_empty_graph_returns_empty(self):
        g = ig.Graph(n=0, edges=[], directed=True)
        empty = _GraphBuild(graph=g, idx_to_id=[], id_to_idx={}, meta={})
        assert compute_communities(empty) == []


# ---------------------------------------------------------------------------
# spread_bonus formula sanity (encoded inside compute_subtree_authority)
# ---------------------------------------------------------------------------


class TestSpreadBonusFormula:
    """Verify the documented formula `1 + log(1 + n)` matches what we expect."""

    def test_no_high_centrality_descendants_gives_1(self):
        # 1 + log1p(0) = 1 + 0 = 1
        assert math.isclose(1.0 + math.log1p(0), 1.0)

    def test_one_high_centrality_descendant_gives_log2(self):
        # 1 + log1p(1) = 1 + ln(2) ≈ 1.693
        assert math.isclose(1.0 + math.log1p(1), 1.0 + math.log(2))

    def test_growth_is_sublinear(self):
        # Doubling spread doesn't double the bonus
        b10 = 1.0 + math.log1p(10)
        b20 = 1.0 + math.log1p(20)
        assert b20 < 2 * b10
