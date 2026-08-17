"""Subtree RBAC — the write gate and list read-filtering (stage 2).

Writes are gated at the repository choke points (create / update / delete / reparent), so
every higher-level mutator that funnels through them (split, chunk, segment, summarize,
extract) inherits the gate. The HTTP status each raise maps to is asserted by exception
TYPE here (the app-level handler that turns AccessDeniedError into 403 is exercised over
HTTP in stage 3, once non-owner tokens can authenticate):

    hidden (unreadable) target  -> LookupError / ValueError   (404 / 400: existence-hiding)
    readable but not writable   -> AccessDeniedError          (403)
    owner / unbound caller      -> allowed                    (bypass)

List/tree read verbs filter to what the principal may read.

Fixture: ACR A (A1 child, A2 grandchild) + an ungoverned root U. Principals: WR=write on A,
RO=read on A, NA=no grant.
"""

from contextlib import contextmanager

import pytest

from jmfts_core.access import AccessDeniedError
from jmfts_core.models.principal import AccessGrant, Principal as PrincipalModel
from jmfts_core.principal_context import OWNER, CurrentPrincipal, reset_principal, set_principal
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.services.document_service import DocumentService


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


def _grant(session, doc_id, principal, level):
    session.add(AccessGrant(document_id=doc_id, principal_id=principal.id, level=level))
    session.flush()


def _fixture(session):
    repo = DocumentRepository(session)

    def mk(title, parent=None):
        d = repo.create(title=title, content=f"body {title}", parent_id=parent, auto_embed=False)
        session.flush()
        return d.id

    a = mk("A")
    a1 = mk("A1", a)
    a2 = mk("A2", a1)
    u = mk("U")  # ungoverned

    wr = _principal(session, "WR")
    ro = _principal(session, "RO")
    na = _principal(session, "NA")
    _grant(session, a, wr, "write")
    _grant(session, a, ro, "read")

    return repo, dict(A=a, A1=a1, A2=a2, U=u), dict(WR=wr, RO=ro, NA=na)


# ── create (add child) ──────────────────────────────────────────────────────


def test_create_child_write_holder_allowed(db_session):
    repo, ids, pr = _fixture(db_session)
    with _as(pr["WR"]):
        child = repo.create(title="new", content="x" * 20, parent_id=ids["A1"], auto_embed=False)
    assert child.id is not None


def test_create_child_read_only_is_denied(db_session):
    repo, ids, pr = _fixture(db_session)
    with _as(pr["RO"]), pytest.raises(AccessDeniedError):
        repo.create(title="new", content="x" * 20, parent_id=ids["A1"], auto_embed=False)


def test_create_child_non_reader_sees_missing_parent(db_session):
    # Existence-hiding: a parent NA can't read reads as "does not exist" (ValueError→400),
    # not a 403 that would confirm the parent is there.
    repo, ids, pr = _fixture(db_session)
    with _as(pr["NA"]), pytest.raises(ValueError, match="does not exist"):
        repo.create(title="new", content="x" * 20, parent_id=ids["A1"], auto_embed=False)


def test_create_under_ungoverned_parent_is_open(db_session):
    repo, ids, pr = _fixture(db_session)
    with _as(pr["NA"]):
        child = repo.create(title="ok", content="x" * 20, parent_id=ids["U"], auto_embed=False)
    assert child.id is not None


def test_create_bypasses_for_owner_and_unbound(db_session):
    repo, ids, _ = _fixture(db_session)
    with _as(OWNER):
        assert repo.create(title="o", content="x" * 20, parent_id=ids["A1"], auto_embed=False)
    # unbound (no contextvar) — in-process caller
    assert repo.create(title="u", content="x" * 20, parent_id=ids["A1"], auto_embed=False)


# ── update / delete ─────────────────────────────────────────────────────────


def test_update_write_read_hidden(db_session):
    repo, ids, pr = _fixture(db_session)
    with _as(pr["WR"]):
        assert repo.update(ids["A2"], title="renamed", re_embed=False) is not None
    with _as(pr["RO"]), pytest.raises(AccessDeniedError):
        repo.update(ids["A2"], title="nope", re_embed=False)
    with _as(pr["NA"]), pytest.raises(LookupError):
        repo.update(ids["A2"], title="nope", re_embed=False)


def test_delete_write_read_hidden(db_session):
    repo, ids, pr = _fixture(db_session)
    with _as(pr["RO"]), pytest.raises(AccessDeniedError):
        repo.delete(ids["A2"])
    with _as(pr["NA"]), pytest.raises(LookupError):
        repo.delete(ids["A2"])
    with _as(pr["WR"]):
        assert repo.delete(ids["A2"]) is True


# ── reparent (needs write on the node AND the destination) ──────────────────


def test_reparent_requires_write_on_both_ends(db_session):
    repo, ids, pr = _fixture(db_session)
    # WR holds write on all of A's subtree and U is ungoverned → allowed.
    with _as(pr["WR"]):
        moved = repo.reparent(ids["A2"], ids["U"])
    assert moved.parent_id == ids["U"]


def test_reparent_read_only_is_denied(db_session):
    repo, ids, pr = _fixture(db_session)
    with _as(pr["RO"]), pytest.raises(AccessDeniedError):
        repo.reparent(ids["A2"], ids["U"])


# ── list/tree read verbs filter to the readable set ─────────────────────────


def test_get_children_filters_for_non_reader(db_session):
    repo, ids, pr = _fixture(db_session)
    service = DocumentService(db_session)
    # A1 is governed by {A}; NA has no grant → the child is hidden.
    with _as(pr["NA"]):
        assert service.get_children(ids["A"]) == []
    # RO reads A → A1 visible.
    with _as(pr["RO"]):
        assert {c.id for c in service.get_children(ids["A"])} == {ids["A1"]}


def test_list_documents_filters_to_ungoverned_for_non_reader(db_session):
    repo, ids, pr = _fixture(db_session)
    service = DocumentService(db_session)
    with _as(pr["NA"]):
        visible = {d.id for d in service.list_documents(limit=100)}
    assert ids["U"] in visible
    assert not ({ids["A"], ids["A1"], ids["A2"]} & visible)


def test_get_subtree_hides_unreadable_root(db_session):
    repo, ids, pr = _fixture(db_session)
    service = DocumentService(db_session)
    with _as(pr["NA"]), pytest.raises(LookupError):
        service.get_subtree(ids["A"])
    # RO sees the whole A-subtree (governed only by {A}, which RO reads).
    with _as(pr["RO"]):
        sub = service.get_subtree(ids["A"])
    assert sub.total == 3
