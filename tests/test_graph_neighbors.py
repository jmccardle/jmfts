"""/graph/neighbors — bounded BFS over the DocumentLink graph.

The transitive read the link graph never had (per-document ``get_links`` only saw one hop).
Tests exercise depth bounding, direction, link_type filtering, the cycle guard, the limit
cap (with its ``truncated`` flag), the ceilings on both bounds, and the service's
root-not-found 404. Small hand-built graph, no embedding model. See ROADMAP §A
"Link-edge lifecycle" and ``SPRINT_0_5_0.md`` Block D finding 7.
"""

import pytest
from fastapi.testclient import TestClient

from jmfts_core.graph_analysis import compute_neighbors, walk_neighbors
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.rest.main import app
from jmfts_core.services.graph_service import (
    MAX_NEIGHBOR_DEPTH,
    MAX_NEIGHBOR_LIMIT,
    GraphService,
)
from tests.conftest import AUTH_HEADERS


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


class TestWalkNeighborsMeasuresItsOwnCut:
    """``truncated`` is measured by overshooting the node cap, not inferred from length.

    ``SPRINT_0_3_0.md`` 7.3 built the overshoot for ``raise_on_truncation`` and left the
    browse flag as ``len(nodes) >= limit``, which is wrong in both directions: a walk that
    ran dry on exactly ``limit`` nodes reads as cut, and there is no reading at all of
    whether anything is left. One extra node settles it, and costs an extra hop only in
    the boundary case that is otherwise unreadable.
    """

    def test_a_walk_that_ran_dry_on_exactly_the_limit_is_not_truncated(self, db_session):
        # A's whole 2-hop neighbourhood is B, C, F, E, D — five nodes and no sixth.
        ids = _graph(db_session)
        walk = walk_neighbors(db_session, ids["A"], max_depth=2, direction="both", limit=5)
        assert len(walk.nodes) == 5
        assert walk.truncated is False

    def test_a_walk_with_one_more_node_behind_it_is_truncated(self, db_session):
        ids = _graph(db_session)
        walk = walk_neighbors(db_session, ids["A"], max_depth=2, direction="both", limit=4)
        assert len(walk.nodes) == 4  # the overshot node is measured, never returned
        assert walk.truncated is True

    def test_an_exhausted_neighbourhood_is_not_truncated(self, db_session):
        ids = _graph(db_session)
        walk = walk_neighbors(db_session, ids["A"], max_depth=2, direction="both", limit=100)
        assert len(walk.nodes) == 5
        assert walk.truncated is False


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

    def test_service_not_truncated_on_a_walk_that_used_its_whole_budget(self, db_session):
        """The flag the old inference got wrong: five reachable, five asked for, none left."""
        ids = _graph(db_session)
        svc = GraphService(db_session)
        resp = svc.get_neighbors(root_id=ids["A"], max_depth=2, direction="both", limit=5)
        assert resp.total == 5
        assert resp.truncated is False

    def test_service_404_on_missing_root(self, db_session):
        svc = GraphService(db_session)
        with pytest.raises(LookupError, match="not found"):
            svc.get_neighbors(root_id=9_999_999)


class TestNeighborBoundsAreRefused:
    """Both bounds have a ceiling, and an over-large ask is refused rather than clamped.

    ``SPRINT_0_5_0.md`` Block D finding 7: neither bound had an upper limit, and
    ``wiring.py`` builds the route from this signature, so a token holder could ask for ten
    million nodes — measured at 17.4–20.8 s of server time in one request
    (``MEASURE_TYPED_WALK.md``). Refusing rather than clamping is what makes the ceiling
    readable: a clamped walk answers a question the caller did not ask and nothing in the
    response says which one.
    """

    def test_limit_above_the_ceiling_is_refused(self, db_session):
        svc = GraphService(db_session)
        with pytest.raises(ValueError, match="limit"):
            svc.get_neighbors(root_id=1, limit=10_000_000)

    def test_max_depth_above_the_ceiling_is_refused(self, db_session):
        svc = GraphService(db_session)
        with pytest.raises(ValueError, match="max_depth"):
            svc.get_neighbors(root_id=1, max_depth=MAX_NEIGHBOR_DEPTH + 1)

    def test_a_non_positive_bound_is_refused(self, db_session):
        svc = GraphService(db_session)
        for kwargs in ({"limit": 0}, {"limit": -1}, {"max_depth": 0}, {"max_depth": -1}):
            with pytest.raises(ValueError):
                svc.get_neighbors(root_id=1, **kwargs)

    def test_the_ceilings_themselves_are_accepted(self, db_session):
        ids = _graph(db_session)
        svc = GraphService(db_session)
        resp = svc.get_neighbors(
            root_id=ids["A"], max_depth=MAX_NEIGHBOR_DEPTH, limit=MAX_NEIGHBOR_LIMIT
        )
        assert resp.truncated is False

    def test_the_bound_is_refused_at_the_wire_and_published_in_openapi(self):
        """The route carries the ceiling, so a caller reads it without sending anything."""
        client = TestClient(app)
        for params, field in (
            ({"root_id": 1, "limit": 10_000_000}, "limit"),
            ({"root_id": 1, "max_depth": 10_000_000}, "max_depth"),
        ):
            resp = client.get("/graph/neighbors", params=params, headers=AUTH_HEADERS)
            assert resp.status_code == 422, f"{field}={params[field]} was not refused"
            assert any(field in str(d.get("loc", ())) for d in resp.json()["detail"])

        schema = app.openapi()["paths"]["/graph/neighbors"]["get"]
        published = {p["name"]: p["schema"] for p in schema["parameters"]}
        assert published["limit"]["maximum"] == MAX_NEIGHBOR_LIMIT
        assert published["max_depth"]["maximum"] == MAX_NEIGHBOR_DEPTH
