"""Projection: match leaves, return their parents.

The method scores leaves exactly as it would without a projection; only what comes back
changes. These tests drive ``fulltext`` because it needs no embedding model and computes
its ``tsvector`` at query time, so a document created in the test is searchable at once.
The projection step is method-agnostic — it runs on whatever list the method returns — so
one method exercises it; ``test_absent_projection_is_byte_identical`` holds the other side,
that a request without one is untouched.

Fixture: two branches of keyword leaves and one keyword at the root.

    ROOT
    ├── CHARGERS   (content matches nothing)
    │   ├── k1  "zephyr zephyr zephyr"
    │   ├── k2  "zephyr"
    │   └── k3  "zephyr"
    ├── ROADTRIP   (content matches nothing)
    │   └── k4  "zephyr zephyr zephyr zephyr zephyr"
    └── (root-level) LONE "zephyr", parent None
"""

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from jmfts_client import RemoteJmftsClient
from jmfts_client.contracts import Projection, SearchRequest
from jmfts_core.database import get_db
from jmfts_core.models.principal import AccessGrant, Principal as PrincipalModel
from jmfts_core.principal_context import CurrentPrincipal, reset_principal, set_principal
from jmfts_core.projection import project_to_parents
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.rest.main import app
from jmfts_core.services.search_service import SearchService

TERM = "zephyr"


def _fixture(session):
    docs = DocumentRepository(session)

    def make(title, content, parent=None, usetype=None):
        doc = docs.create(
            title=title,
            content=content,
            parent_id=parent.id if parent else None,
            usetype=usetype,
            auto_embed=False,
        )
        session.flush()
        return doc

    root = make("ROOT", "nothing to see")
    chargers = make("CHARGERS", "branch summary about plugs", root)
    roadtrip = make("ROADTRIP", "branch summary about a drive", root)
    k1 = make("k1", f"{TERM} {TERM} {TERM}", chargers, "keyword")
    k2 = make("k2", TERM, chargers, "keyword")
    k3 = make("k3", TERM, chargers, "keyword")
    k4 = make("k4", " ".join([TERM] * 5), roadtrip, "keyword")
    lone = make("LONE", TERM, None, "keyword")
    return dict(
        ROOT=root, CHARGERS=chargers, ROADTRIP=roadtrip, k1=k1, k2=k2, k3=k3, k4=k4, LONE=lone
    )


def _search(session, project=None, limit=10):
    request = SearchRequest(query=TERM, limit=limit, usetype="keyword", project=project)
    return SearchService(session).fulltext_search(request)


def _titles(response):
    return [r.document.title for r in response.results]


def test_leaves_under_one_parent_become_one_result(db_session):
    ids = _fixture(db_session)
    response = _search(db_session, Projection())

    assert sorted(_titles(response)) == ["CHARGERS", "ROADTRIP"]
    chargers = next(r for r in response.results if r.document.title == "CHARGERS")
    assert {m.document_id for m in chargers.via} == {ids["k1"].id, ids["k2"].id, ids["k3"].id}
    assert [m.score for m in chargers.via] == sorted((m.score for m in chargers.via), reverse=True)


def test_max_scores_the_parent_by_its_best_leaf(db_session):
    """k4 outscores every CHARGERS leaf, so ROADTRIP leads under `max`."""
    _fixture(db_session)
    response = _search(db_session, Projection(aggregate="max"))

    assert _titles(response)[0] == "ROADTRIP"
    for result in response.results:
        assert result.score == result.via[0].score


def test_count_scores_the_parent_by_how_many_leaves_matched(db_session):
    """Three weaker handles outrank one strong one: the question log's aggregation."""
    _fixture(db_session)
    response = _search(db_session, Projection(aggregate="count"))

    assert _titles(response) == ["CHARGERS", "ROADTRIP"]
    assert [r.score for r in response.results] == [3.0, 1.0]


def test_sum_adds_the_leaf_scores(db_session):
    _fixture(db_session)
    response = _search(db_session, Projection(aggregate="sum"))
    for result in response.results:
        assert result.score == pytest.approx(sum(m.score for m in result.via))


def test_root_hit_is_counted_not_returned(db_session):
    ids = _fixture(db_session)
    response = _search(db_session, Projection())

    assert ids["LONE"].id not in {r.document.id for r in response.results}
    applied = response.applied.projection
    assert applied.no_parent == 1
    assert applied.matched == 5
    assert applied.parents == 2
    assert applied.withheld == 0
    assert applied.match_limit == 50


def test_projection_cuts_to_limit_after_grouping(db_session):
    _fixture(db_session)
    response = _search(db_session, Projection(aggregate="count"), limit=1)
    assert _titles(response) == ["CHARGERS"]
    assert response.applied.projection.parents == 2


def test_absent_projection_is_byte_identical(db_session):
    """No `project` → the leaves themselves, no `via`, no `applied.projection`."""
    _fixture(db_session)
    response = _search(db_session)

    assert sorted(_titles(response)) == ["LONE", "k1", "k2", "k3", "k4"]
    assert all(r.via is None for r in response.results)
    assert response.applied.projection is None


def test_usetype_filter_chooses_the_match_not_the_parent(db_session):
    """The parents are not `keyword` and are returned anyway: the filter scoped the match."""
    _fixture(db_session)
    response = _search(db_session, Projection())
    assert {r.document.usetype for r in response.results} == {None}


def test_unsettled_parent_is_withheld(db_session):
    ids = _fixture(db_session)
    ids["ROADTRIP"].settled = "in_flight"
    db_session.flush()

    response = _search(db_session, Projection())
    assert _titles(response) == ["CHARGERS"]
    assert response.applied.projection.withheld == 1


@contextmanager
def _as(principal):
    token = set_principal(principal)
    try:
        yield
    finally:
        reset_principal(token)


def test_unreadable_parent_is_withheld(db_session):
    """A leaf readable through its own grant does not carry its parent out with it.

    ROADTRIP becomes an access-control root the outsider holds no grant on; k4 is made
    readable to them by a grant of its own. The leaf matches, the parent is withheld and
    counted, and nothing about the parent reaches the page.
    """
    ids = _fixture(db_session)
    row = PrincipalModel(name="outsider")
    db_session.add(row)
    db_session.flush()
    outsider = CurrentPrincipal(id=row.id, name="outsider", is_owner=False)
    other = PrincipalModel(name="owner-of-roadtrip")
    db_session.add(other)
    db_session.flush()
    db_session.add(AccessGrant(document_id=ids["ROADTRIP"].id, principal_id=other.id, level="read"))
    db_session.add(AccessGrant(document_id=ids["k4"].id, principal_id=outsider.id, level="read"))
    db_session.flush()

    with _as(outsider):
        leaves = _search(db_session)
        assert "k4" in _titles(leaves)

        response = _search(db_session, Projection())
    assert "ROADTRIP" not in _titles(response)
    assert response.applied.projection.withheld == 1


def test_unknown_aggregate_is_refused(db_session):
    with pytest.raises(ValueError, match="unknown projection aggregate"):
        project_to_parents(db_session, [], aggregate="median", limit=10)


def test_projection_round_trips_the_generated_client(db_session):
    """The field travels in the body and `via` and `applied.projection` parse back."""
    _fixture(db_session)

    def _override_get_db():
        yield db_session

    from tests.conftest import AUTH_HEADERS

    app.dependency_overrides[get_db] = _override_get_db
    try:
        client = RemoteJmftsClient(
            "http://testserver", client=TestClient(app, headers=AUTH_HEADERS)
        )
        response = client.fulltext_search(
            SearchRequest(query=TERM, usetype="keyword", project=Projection(aggregate="count"))
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert [r.document.title for r in response.results] == ["CHARGERS", "ROADTRIP"]
    assert len(response.results[0].via) == 3
    assert response.applied.projection.aggregate == "count"
