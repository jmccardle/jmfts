"""/graph/neighbors — bounded BFS over the DocumentLink graph.

The transitive read the link graph never had (per-document ``get_links`` only saw one hop).
Tests exercise depth bounding, direction, link_type filtering, the cycle guard, the limit
cap (with its ``truncated`` flag), and the service's root-not-found 404. Small hand-built
graph, no embedding model. See ROADMAP §A "Link-edge lifecycle".
"""

import pytest

from jmfts_core.graph_analysis import compute_neighbors
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.services.graph_service import GraphService


def _graph(db_session):
    """Build:  E -> A ,  A -> B ,  A -> C ,  B -> D ,  D -> B (cycle) ,  A -> F [bridge].

    D->B loops back to an already-visited node (not the root), so D stays a genuine 2-hop
    node while still exercising the cycle guard. All 'ref' edges except A->F ('bridge').
    Returns the id map.
    """
    repo = DocumentRepository(db_session)
    ids = {}
    for name in "ABCDEF":
        ids[name] = repo.create(
            title=name, content=f"node {name}", usetype="raw", auto_embed=False
        ).id
    db_session.flush()

    def link(src, tgt, ltype="ref"):
        repo.create_link(source_id=ids[src], target_id=ids[tgt], link_type=ltype)

    link("E", "A")
    link("A", "B")
    link("A", "C")
    link("B", "D")
    link("D", "B")  # cycle back to an already-visited node (not the root)
    link("A", "F", "bridge")
    db_session.flush()
    return ids


def _by_id(nodes):
    return {n.document_id: n for n in nodes}


class TestComputeNeighbors:
    def test_depth_one_both_directions(self, db_session):
        ids = _graph(db_session)
        nodes = compute_neighbors(db_session, ids["A"], max_depth=1, direction="both")
        got = _by_id(nodes)
        # Out: B, C, F ; In: E. Not D (that's 2 hops).
        assert set(got) == {ids["B"], ids["C"], ids["F"], ids["E"]}
        assert all(n.depth == 1 for n in nodes)
        assert got[ids["E"]].direction == "incoming"
        assert got[ids["B"]].direction == "outgoing"

    def test_depth_two_reaches_further_and_guards_cycle(self, db_session):
        ids = _graph(db_session)
        nodes = compute_neighbors(db_session, ids["A"], max_depth=2, direction="both")
        got = _by_id(nodes)
        # D reached via B at depth 2; A is the root and its cycle edge must not re-add it.
        assert ids["D"] in got
        assert got[ids["D"]].depth == 2
        assert ids["A"] not in got  # cycle guard: root never re-emitted

    def test_direction_outgoing_excludes_incoming(self, db_session):
        ids = _graph(db_session)
        nodes = compute_neighbors(db_session, ids["A"], max_depth=2, direction="outgoing")
        got = set(_by_id(nodes))
        assert ids["E"] not in got  # E->A is incoming
        assert {ids["B"], ids["C"], ids["F"], ids["D"]} <= got

    def test_link_type_filter(self, db_session):
        ids = _graph(db_session)
        nodes = compute_neighbors(
            db_session, ids["A"], max_depth=1, direction="outgoing", link_types=["ref"]
        )
        got = set(_by_id(nodes))
        assert ids["F"] not in got  # A->F is a 'bridge', filtered out
        assert {ids["B"], ids["C"]} <= got

    def test_limit_caps_results(self, db_session):
        ids = _graph(db_session)
        nodes = compute_neighbors(db_session, ids["A"], max_depth=2, direction="both", limit=2)
        assert len(nodes) == 2

    def test_hydrates_title_and_usetype(self, db_session):
        ids = _graph(db_session)
        nodes = compute_neighbors(db_session, ids["A"], max_depth=1, direction="outgoing")
        b = _by_id(nodes)[ids["B"]]
        assert b.title == "B"
        assert b.usetype == "raw"


class TestGraphServiceNeighbors:
    def test_service_shapes_response_and_flags_truncation(self, db_session):
        ids = _graph(db_session)
        svc = GraphService(db_session)
        resp = svc.get_neighbors(root_id=ids["A"], max_depth=2, direction="both", limit=2)
        assert resp.root_id == ids["A"]
        assert resp.total == 2
        assert resp.truncated is True
        assert len(resp.neighbors) == 2

    def test_service_not_truncated_when_under_limit(self, db_session):
        ids = _graph(db_session)
        svc = GraphService(db_session)
        resp = svc.get_neighbors(root_id=ids["A"], max_depth=1, direction="outgoing", limit=100)
        assert resp.truncated is False

    def test_service_404_on_missing_root(self, db_session):
        svc = GraphService(db_session)
        with pytest.raises(LookupError, match="not found"):
            svc.get_neighbors(root_id=9_999_999)
