"""Subtree RBAC on the graph-analytics verbs (``SPRINT_0_6_0.md`` Block A step 2).

The entry condition in the step table: *a principal with no grant calls
``GET /graph/centrality`` and reads back ids and titles of documents it cannot read*. Four
verbs did that — ``get_centrality``, ``get_subtree_authority``, ``get_spines`` and
``get_communities`` — because each builds a graph over the whole corpus and then returns
ids, while ``get_neighbors`` next to them gates on ``can_read`` and hides nodes during the
walk.

Part 4 question 4.2 is the choice between filtering the RESULT (leaks nothing, reports
numbers derived from rows the caller cannot see) and filtering the GRAPH (honest, more
expensive, moves every score). Its stated default is to filter the graph, and these tests
are written against that: the assertions are not only "the secret id is absent" but "the
score changed", because a result-filter would pass the first and fail the second.

Fixture: a public root with three public children in a chain, plus one SECRET child that is
the most-linked node in the corpus. An outsider must see neither the id nor its pull on
everybody else's centrality.
"""

from contextlib import contextmanager

from jmfts_core.models.principal import AccessGrant, Principal as PrincipalModel
from jmfts_core.principal_context import OWNER, CurrentPrincipal, reset_principal, set_principal
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.services.graph_service import GraphService


@contextmanager
def _as(principal):
    token = set_principal(principal)
    try:
        yield
    finally:
        reset_principal(token)


def _principal(session, name):
    row = PrincipalModel(name=name)
    session.add(row)
    session.flush()
    return CurrentPrincipal(id=row.id, name=name, is_owner=False)


def _fixture(session):
    """ROOT with A, B, C public and S secret. Everyone links to S; S links to nobody.

    S is therefore the corpus's authority under any centrality metric, which is what makes
    the "the score moved" assertions below sharp: drop S and the ranking has to change.
    """
    docs = DocumentRepository(session)
    root = docs.create(title="ROOT", content="root " * 3, auto_embed=False)
    session.flush()
    a = docs.create(title="A", content="alpha " * 3, parent_id=root.id, auto_embed=False)
    b = docs.create(title="B", content="beta " * 3, parent_id=root.id, auto_embed=False)
    c = docs.create(title="C", content="gamma " * 3, parent_id=root.id, auto_embed=False)
    s = docs.create(title="SECRET", content="secret " * 3, parent_id=root.id, auto_embed=False)
    session.flush()

    for src in (a, b, c):
        docs.create_link(source_id=src.id, target_id=s.id, link_type="cites")
    docs.create_link(source_id=a.id, target_id=b.id, link_type="cites")
    docs.create_link(source_id=b.id, target_id=c.id, link_type="cites")
    session.flush()

    insider = _principal(session, "insider")
    outsider = _principal(session, "outsider")
    session.add(AccessGrant(document_id=s.id, principal_id=insider.id, level="read"))
    session.flush()

    ids = dict(ROOT=root.id, A=a.id, B=b.id, C=c.id, S=s.id)
    return ids, insider, outsider


def _centrality(session, **kwargs):
    return GraphService(session).get_centrality(top=50, **kwargs)


# ── centrality ──────────────────────────────────────────────────────────────


def test_centrality_hides_unreadable_documents(db_session):
    """The entry condition. The outsider must not read S's id or title off this verb."""
    ids, insider, outsider = _fixture(db_session)
    with _as(OWNER):
        assert ids["S"] in {r.document_id for r in _centrality(db_session).results}
    with _as(insider):
        assert ids["S"] in {r.document_id for r in _centrality(db_session).results}
    with _as(outsider):
        response = _centrality(db_session)
        assert ids["S"] not in {r.document_id for r in response.results}
        assert "SECRET" not in {r.title for r in response.results}


def test_centrality_filters_the_graph_and_not_the_result(db_session):
    """4.2's default, asserted as a number rather than as an absence.

    A result-filter would leave B's pagerank exactly where it was and merely drop S's row.
    A graph-filter removes S's three inbound edges from the computation, so the whole
    vertex and edge count moves and B's score moves with it.
    """
    ids, _insider, outsider = _fixture(db_session)
    with _as(OWNER):
        full = _centrality(db_session)
    with _as(outsider):
        scoped = _centrality(db_session)

    assert scoped.total_vertices < full.total_vertices
    assert scoped.total_edges < full.total_edges

    full_scores = {r.document_id: r.score for r in full.results}
    scoped_scores = {r.document_id: r.score for r in scoped.results}
    assert full_scores[ids["B"]] != scoped_scores[ids["B"]]


def test_centrality_is_unchanged_where_nothing_is_governed(db_session):
    """No grants anywhere → ``readable_filter`` returns None → the statement is unchanged.

    This is the property that makes the behaviour break survivable: a single-principal
    appliance with no access-control roots reads the same numbers it always did.
    """
    docs = DocumentRepository(db_session)
    x = docs.create(title="X", content="ex " * 3, auto_embed=False)
    y = docs.create(title="Y", content="why " * 3, auto_embed=False)
    db_session.flush()
    docs.create_link(source_id=x.id, target_id=y.id, link_type="cites")
    db_session.flush()
    nobody = _principal(db_session, "nobody")

    with _as(OWNER):
        full = _centrality(db_session)
    with _as(nobody):
        scoped = _centrality(db_session)
    assert {r.document_id: r.score for r in full.results} == {
        r.document_id: r.score for r in scoped.results
    }


# ── communities, subtree authority, spines ──────────────────────────────────


def test_communities_hide_unreadable_members(db_session):
    ids, _insider, outsider = _fixture(db_session)
    service = GraphService(db_session)
    with _as(outsider):
        response = service.get_communities(min_size=1)
    members = {m.document_id for c in response.results for m in c.members}
    assert ids["S"] not in members


def test_subtree_authority_excludes_unreadable_descendants(db_session):
    """The roll-up reads its tree from ``_build_tree_index``, which is filtered too.

    Filtering only the edge graph would have closed the score and left the labels: S would
    still have arrived as a ``top_descendants`` entry carrying its title.
    """
    ids, _insider, outsider = _fixture(db_session)
    service = GraphService(db_session)
    with _as(OWNER):
        full = {r.document_id: r for r in service.get_subtree_authority(min_subtree_size=1).results}
    with _as(outsider):
        scoped = {
            r.document_id: r for r in service.get_subtree_authority(min_subtree_size=1).results
        }

    assert ids["S"] in full and ids["S"] not in scoped
    assert full[ids["ROOT"]].subtree_size == 5
    assert scoped[ids["ROOT"]].subtree_size == 4
    named = {d.document_id for d in scoped[ids["ROOT"]].top_descendants}
    assert ids["S"] not in named


def test_spines_do_not_route_through_unreadable_documents(db_session):
    ids, _insider, outsider = _fixture(db_session)
    service = GraphService(db_session)
    with _as(outsider):
        response = service.get_spines(root_id=ids["ROOT"])
    visited = {doc_id for path in response.paths for doc_id in path.path}
    assert ids["S"] not in visited
    assert "SECRET" not in {title for path in response.paths for title in path.titles}
