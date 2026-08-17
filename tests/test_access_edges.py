"""Subtree RBAC — hiding edges that point to unreadable documents (stage 4).

An edge (a document link, a graph neighbor hop, a triple's subject/object) that references a
document the current principal cannot read must not surface — otherwise the edge leaks the
existence of the hidden document even though the document itself is 404/filtered everywhere
else. Owner/unbound callers see the whole graph (bypass).

Fixture: a public document P and a public P2 (ungoverned), and a secret S under an ACR
granted only to `insider`. P links to and has triples with both S and P2. `outsider` (no
grant) must see the P↔P2 edges but never the P↔S edges, and S itself is 404.
"""

from contextlib import contextmanager

import pytest

from jmfts_core.graph_analysis import compute_neighbors
from jmfts_core.models.principal import AccessGrant, Principal as PrincipalModel
from jmfts_core.principal_context import OWNER, CurrentPrincipal, reset_principal, set_principal
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.triple import TripleRepository
from jmfts_core.repositories.view import ViewRepository
from jmfts_core.services.document_service import DocumentService
from jmfts_core.services.triple_service import TripleService


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
    docs = DocumentRepository(session)
    triples = TripleRepository(session)

    p = docs.create(title="P", content="public root " * 3, auto_embed=False)
    p2 = docs.create(title="P2", content="public other " * 3, auto_embed=False)
    s = docs.create(title="S", content="secret entity " * 3, auto_embed=False)
    session.flush()

    docs.create_link(source_id=p.id, target_id=s.id, link_type="mentions")
    docs.create_link(source_id=p.id, target_id=p2.id, link_type="mentions")
    pred = triples.create_predicate(name="knows")
    t_ps = triples.create_triple(subject_id=p.id, predicate_id=pred.id, object_id=s.id)
    t_pp2 = triples.create_triple(subject_id=p.id, predicate_id=pred.id, object_id=p2.id)
    session.flush()

    insider = _principal(session, "insider")
    outsider = _principal(session, "outsider")
    session.add(AccessGrant(document_id=s.id, principal_id=insider.id, level="read"))
    session.flush()

    ids = dict(P=p.id, P2=p2.id, S=s.id, T_PS=t_ps.id, T_PP2=t_pp2.id)
    return ids, outsider


# ── document links ──────────────────────────────────────────────────────────


def test_get_links_hides_edges_to_unreadable(db_session):
    ids, outsider = _fixture(db_session)
    repo = DocumentRepository(db_session)
    with _as(OWNER):
        assert {lk.target_id for lk in repo.get_links(ids["P"])} == {ids["S"], ids["P2"]}
    with _as(outsider):
        # The edge to S is gone; the edge to the public P2 remains.
        assert {lk.target_id for lk in repo.get_links(ids["P"])} == {ids["P2"]}


def test_get_links_service_404s_on_unreadable_incident_doc(db_session):
    ids, outsider = _fixture(db_session)
    service = DocumentService(db_session)
    with _as(outsider), pytest.raises(LookupError):
        service.get_links(ids["S"])


# ── graph neighbors (transit-hiding) ────────────────────────────────────────


def test_compute_neighbors_hides_unreadable_nodes(db_session):
    ids, outsider = _fixture(db_session)
    with _as(OWNER):
        assert {n.document_id for n in compute_neighbors(db_session, ids["P"])} == {
            ids["S"],
            ids["P2"],
        }
    with _as(outsider):
        assert {n.document_id for n in compute_neighbors(db_session, ids["P"])} == {ids["P2"]}


# ── triples ─────────────────────────────────────────────────────────────────


def test_query_triples_hides_facts_touching_unreadable(db_session):
    ids, outsider = _fixture(db_session)
    repo = TripleRepository(db_session)
    with _as(OWNER):
        assert {t.id for t in repo.query_triples(entity_id=ids["P"])} == {ids["T_PS"], ids["T_PP2"]}
    with _as(outsider):
        # The (P, knows, S) fact is hidden because S is unreadable; (P, knows, P2) stays.
        assert {t.id for t in repo.query_triples(entity_id=ids["P"])} == {ids["T_PP2"]}


def test_get_triple_404s_when_an_endpoint_is_unreadable(db_session):
    ids, outsider = _fixture(db_session)
    service = TripleService(db_session)
    with _as(outsider):
        # The all-public fact is retrievable...
        assert service.get_triple(ids["T_PP2"]).id == ids["T_PP2"]
        # ...but the one whose object is the secret is hidden whole.
        with pytest.raises(LookupError):
            service.get_triple(ids["T_PS"])


# ── /view bundle ────────────────────────────────────────────────────────────


def test_view_bundle_filters_links_and_triples(db_session):
    ids, outsider = _fixture(db_session)
    repo = ViewRepository(db_session)
    with _as(outsider):
        bundle = repo.get_bundle(ids["P"])
        assert {lk["_other_id"] for lk in bundle.outbound_links} == {ids["P2"]}
        object_ids = {t["object_id"] for t in bundle.triples}
        assert ids["S"] not in object_ids and ids["P2"] in object_ids
        # The secret document's own view is indistinguishable from missing.
        assert repo.get_bundle(ids["S"]) is None
