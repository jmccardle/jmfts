"""IC-3: ``/view/{id}`` tells the caller whether it may write.

``docs/SPRINT_0_6_0.md`` Block F step 18, and the half of Block A step 1 that faces outward.
Until this landed no response reported the caller's own access level, so a page rendering an
"edit" or "create link" control had to fire the call and read a 403 to find out. A control
that lies is the user-facing half of the defect Block A closes on the server side.

Fixture shape is ``tests/test_access_write_gate.py``'s, deliberately: ACR A with a child,
plus an ungoverned root, and three principals — write, read-only, and no grant at all. Same
shape, different question. That file asks what the repository refuses; this one asks what the
view reports, and the point of IC-3 is that the two answers come from one function.
"""

from contextlib import contextmanager

import pytest

from jmfts_core.models.principal import AccessGrant, Principal as PrincipalModel
from jmfts_core.principal_context import CurrentPrincipal, reset_principal, set_principal
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.services.view_service import ViewService


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


@pytest.fixture
def tree(db_session):
    """ACR ``A`` with child ``A1``, an ungoverned root ``U``, three principals."""
    repo = DocumentRepository(db_session)

    def mk(title, parent=None):
        doc = repo.create(title=title, content=f"body {title}", parent_id=parent, auto_embed=False)
        db_session.flush()
        return doc.id

    a = mk("A")
    a1 = mk("A1", a)
    u = mk("U")

    writer = _principal(db_session, "WR")
    reader = _principal(db_session, "RO")
    stranger = _principal(db_session, "NA")
    db_session.add(AccessGrant(document_id=a, principal_id=writer.id, level="write"))
    db_session.add(AccessGrant(document_id=a, principal_id=reader.id, level="read"))
    db_session.flush()

    return dict(A=a, A1=a1, U=u), dict(WR=writer, RO=reader, NA=stranger)


def test_a_writer_reads_true(db_session, tree):
    docs, who = tree
    with _as(who["WR"]):
        assert ViewService(db_session).view_document(docs["A"]).can_write is True


def test_a_writer_reads_true_below_the_root_it_was_granted(db_session, tree):
    """A grant is on a subtree, so the answer has to follow the tree and not the node."""
    docs, who = tree
    with _as(who["WR"]):
        assert ViewService(db_session).view_document(docs["A1"]).can_write is True


def test_a_read_only_principal_reads_false(db_session, tree):
    """The case the field exists for: readable, so the page renders; not writable, so the
    page must not offer a control that would 403."""
    docs, who = tree
    with _as(who["RO"]):
        view = ViewService(db_session).view_document(docs["A"])
        assert view.id == docs["A"]  # it IS readable — this is not a hidden-document test
        assert view.can_write is False


def test_an_ungoverned_document_reads_true_for_everyone(db_session, tree):
    """Access control here is opt-in, and this asserts that rather than assuming it.

    ``can_write`` returns True when no access-control root governs the node
    (``jmfts_core/access.py:172``). A principal with no grant at all reads True on an
    ungoverned document, which is the default state of most of the corpus and is the
    behaviour ``GET /access/audit`` exists to report on.
    """
    docs, who = tree
    with _as(who["NA"]):
        assert ViewService(db_session).view_document(docs["U"]).can_write is True


def test_the_owner_reads_true(db_session, tree):
    """``_bypass`` exempts the owner and in-process callers from all of it."""
    docs, _ = tree
    assert ViewService(db_session).view_document(docs["A"]).can_write is True
