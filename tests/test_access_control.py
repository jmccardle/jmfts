"""Subtree RBAC — enforcement engine and the read gate (stage 1).

The security core is proven at the engine level (no embeddings, fast, deterministic):
the `readable_filter` predicate and the `can_read`/`can_write` point checks, over a
real Postgres tree so the `path @> [acr]` containment and max-over-path semantics are
exercised against the actual GIN index.

Then each of the four search leaves (vector / full-text / BM25 / MaxSim) is checked
end-to-end: a non-owner principal without a grant must not see a governed document that
the owner sees, while an ungoverned document stays visible to everyone. Enforcement is
driven by binding the request principal in the contextvar, exactly as `api/auth.py` will.

Tree fixture:

    U   (ungoverned)          -- no ACR anywhere on its path
    └── U1
    A   (ACR)                 -- grant: P=read
    ├── A1
    │   └── A2
    └── B   (ACR, under A)    -- grant: Q=read
        └── B1

    P (read on A) sees: A, A1, A2, B, B1   (max-over-path: B1 is governed by {A,B},
                                            and P's grant on the ancestor A suffices)
    Q (read on B) sees: B, B1              (isolation: no grant on A → A/A1/A2 hidden)
    Z (no grants)  sees: nothing governed  (only U, U1)
    owner / unbound sees: everything       (bypass)
"""

from contextlib import contextmanager

from sqlalchemy import select

from jmfts_core.access import can_read, can_write, readable_filter
from jmfts_core.models.document import Document
from jmfts_core.models.principal import AccessGrant, Principal as PrincipalModel
from jmfts_core.principal_context import (
    OWNER,
    CurrentPrincipal,
    reset_principal,
    set_principal,
)
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.search import SearchRepository


@contextmanager
def _as(principal):
    """Bind `principal` as the current request principal for the block."""
    token = set_principal(principal)
    try:
        yield
    finally:
        reset_principal(token)


def _principal(session, name: str, *, is_owner: bool = False) -> CurrentPrincipal:
    row = PrincipalModel(name=name, is_owner=is_owner)
    session.add(row)
    session.flush()
    return CurrentPrincipal(id=row.id, name=name, is_owner=is_owner)


def _grant(session, doc_id: int, principal: CurrentPrincipal, level: str) -> None:
    session.add(AccessGrant(document_id=doc_id, principal_id=principal.id, level=level))
    session.flush()


def _tree(session):
    """Build the fixture tree (no embeddings) and return ids + principals."""
    repo = DocumentRepository(session)

    def mk(title, parent=None):
        d = repo.create(title=title, content=f"body of {title}", parent_id=parent, auto_embed=False)
        session.flush()
        return d.id

    u = mk("U")
    u1 = mk("U1", u)
    a = mk("A")
    a1 = mk("A1", a)
    a2 = mk("A2", a1)
    b = mk("B", a)  # nested ACR under A
    b1 = mk("B1", b)

    p = _principal(session, "P")
    q = _principal(session, "Q")
    z = _principal(session, "Z")
    _grant(session, a, p, "read")
    _grant(session, b, q, "read")

    ids = dict(U=u, U1=u1, A=a, A1=a1, A2=a2, B=b, B1=b1)
    return ids, dict(P=p, Q=q, Z=z)


def _visible_ids(session, principal) -> set[int]:
    pred = readable_filter(session, principal)
    q = select(Document.id)
    if pred is not None:
        q = q.where(pred)
    return set(session.execute(q).scalars())


# ── engine: readable_filter over the tree ───────────────────────────────────


def test_owner_and_unbound_see_everything(db_session):
    ids, _ = _tree(db_session)
    everything = set(ids.values())
    assert _visible_ids(db_session, OWNER) == everything
    assert _visible_ids(db_session, None) == everything  # unbound in-process caller


def test_granted_principal_sees_own_subtree_via_max_over_path(db_session):
    ids, pr = _tree(db_session)
    # P holds read on A → the whole A-subtree, including B/B1 which are ALSO under the
    # nested ACR B (governed by {A, B}); the ancestor grant is enough (max-over-path).
    assert _visible_ids(db_session, pr["P"]) == {
        ids["U"],
        ids["U1"],
        ids["A"],
        ids["A1"],
        ids["A2"],
        ids["B"],
        ids["B1"],
    }


def test_nested_grant_is_isolated_from_the_ancestor(db_session):
    ids, pr = _tree(db_session)
    # Q holds read only on B → sees B and B1, but NOT A/A1/A2 (governed by {A}, no grant).
    assert _visible_ids(db_session, pr["Q"]) == {ids["U"], ids["U1"], ids["B"], ids["B1"]}


def test_ungranted_principal_sees_only_ungoverned(db_session):
    ids, pr = _tree(db_session)
    assert _visible_ids(db_session, pr["Z"]) == {ids["U"], ids["U1"]}


def test_grant_on_the_root_makes_the_root_itself_readable(db_session):
    # `path` excludes self, so a grant on ACR A must still make A visible — the engine
    # includes `Document.id IN (roots)` alongside the containment.
    ids, pr = _tree(db_session)
    assert ids["A"] in _visible_ids(db_session, pr["P"])
    assert ids["A"] not in _visible_ids(db_session, pr["Z"])


# ── engine: point checks (can_read / can_write) ─────────────────────────────


def test_point_checks_match_the_filter(db_session):
    ids, pr = _tree(db_session)
    a2 = db_session.get(Document, ids["A2"])
    u1 = db_session.get(Document, ids["U1"])
    b1 = db_session.get(Document, ids["B1"])

    # P: read on A → can_read the A-subtree; a plain read grant is NOT write.
    assert can_read(db_session, a2, pr["P"]) is True
    assert can_write(db_session, a2, pr["P"]) is False
    # Z: only ungoverned.
    assert can_read(db_session, u1, pr["Z"]) is True
    assert can_read(db_session, a2, pr["Z"]) is False
    # Q: B-subtree only.
    assert can_read(db_session, b1, pr["Q"]) is True
    assert can_read(db_session, a2, pr["Q"]) is False
    # Owner bypasses everything.
    assert can_write(db_session, a2, OWNER) is True


def test_write_grant_confers_write_and_read(db_session):
    ids, pr = _tree(db_session)
    w = _principal(db_session, "W")
    _grant(db_session, ids["A"], w, "write")
    a1 = db_session.get(Document, ids["A1"])
    assert can_read(db_session, a1, w) is True
    assert can_write(db_session, a1, w) is True


# ── read gate through each of the four search leaves ────────────────────────


def _seed_searchable(session):
    """One governed doc (ACR granted to nobody-but-owner) + one ungoverned doc, both
    embedded and indexed so every search leaf can retrieve them."""
    repo = DocumentRepository(session)
    search = SearchRepository(session)

    secret = repo.create(
        title="Confidential",
        content="the pangolin protocol authorizes zebra maneuvers",
        auto_embed=True,
    )
    session.flush()
    public = repo.create(
        title="Public",
        content="the pangolin protocol is discussed openly at zebra meetings",
        auto_embed=True,
    )
    session.flush()
    for d in (secret.id, public.id):
        search.index_document(d, "default")
    session.flush()

    outsider = _principal(session, "outsider")
    _grant(session, secret.id, _principal(session, "insider"), "read")  # ACR, outsider not on it
    session.flush()
    return search, secret.id, public.id, outsider


def _ids(results):
    return {r.document.id for r in results}


def test_vector_search_enforces_read_gate(db_session):
    search, secret, public, outsider = _seed_searchable(db_session)
    q = "pangolin protocol zebra"
    with _as(OWNER):
        assert secret in _ids(search.vector_search_text(q, limit=10))
    with _as(outsider):
        got = _ids(search.vector_search_text(q, limit=10))
    assert secret not in got and public in got


def test_fulltext_search_enforces_read_gate(db_session):
    search, secret, public, outsider = _seed_searchable(db_session)
    q = "pangolin protocol"
    with _as(OWNER):
        assert secret in _ids(search.fulltext_search(q, limit=10))
    with _as(outsider):
        got = _ids(search.fulltext_search(q, limit=10))
    assert secret not in got and public in got


def test_bm25_search_enforces_read_gate(db_session):
    search, secret, public, outsider = _seed_searchable(db_session)
    q = "pangolin protocol zebra"
    with _as(OWNER):
        assert secret in _ids(search.bm25_search(q, limit=10))
    with _as(outsider):
        got = _ids(search.bm25_search(q, limit=10))
    assert secret not in got and public in got


def test_maxsim_search_enforces_read_gate(db_session):
    search, secret, public, outsider = _seed_searchable(db_session)
    q = "pangolin protocol zebra"
    with _as(OWNER):
        assert secret in _ids(search.maxsim_search(q, limit=10))
    with _as(outsider):
        got = _ids(search.maxsim_search(q, limit=10))
    assert secret not in got and public in got
