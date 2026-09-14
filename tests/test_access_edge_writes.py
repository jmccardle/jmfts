"""Subtree RBAC — the WRITE gate on document links (``SPRINT_0_6_0.md`` Block A step 1).

``tests/test_access_edges.py`` covers the READ half: an edge pointing at a document the
principal cannot read does not surface. This file covers the half that was missing —
``DocumentService.create_link`` performed no access check of any kind, and ``delete_link``
did not either, in a class whose adjacent ``get_links`` calls ``can_read``.

**Write on the source, read on the target** (Part 4 question 4.1, ANSWERED 2026-09-13).
Write-on-both refuses a legitimate citation of a document you may read and not modify.
Write-on-source-only lets a principal attach an edge to a document it cannot see, and that
row then reaches ``/graph/neighbors`` and ``/graph/centrality`` — edge injection into a
graph the injector cannot read.

Fixture: three governed documents and three principals, so both halves of the rule can fail
independently.

    OWNED   — `author` has write, `reader` has read
    SHARED  — `author` has read, `reader` has read   (a legitimate citation target)
    SECRET  — only `reader` has read                 (invisible to `author`)
    OPEN    — under no ACR at all                    (ungoverned stays open)
"""

from contextlib import contextmanager

import pytest

from jmfts_core.access import AccessDeniedError
from jmfts_core.models.principal import AccessGrant, Principal as PrincipalModel
from jmfts_core.principal_context import CurrentPrincipal, reset_principal, set_principal
from jmfts_core.repositories.document import DocumentRepository
from jmfts_client.contracts.document import LinkCreate
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


def _fixture(session):
    """Build the four documents and two principals described in the module docstring."""
    docs = DocumentRepository(session)
    owned = docs.create(title="OWNED", content="owned " * 3, auto_embed=False)
    shared = docs.create(title="SHARED", content="shared " * 3, auto_embed=False)
    secret = docs.create(title="SECRET", content="secret " * 3, auto_embed=False)
    open_doc = docs.create(title="OPEN", content="open " * 3, auto_embed=False)
    session.flush()

    author = _principal(session, "author")
    reader = _principal(session, "reader")
    session.add_all(
        [
            AccessGrant(document_id=owned.id, principal_id=author.id, level="write"),
            AccessGrant(document_id=owned.id, principal_id=reader.id, level="read"),
            AccessGrant(document_id=shared.id, principal_id=author.id, level="read"),
            AccessGrant(document_id=shared.id, principal_id=reader.id, level="read"),
            AccessGrant(document_id=secret.id, principal_id=reader.id, level="read"),
        ]
    )
    session.flush()
    ids = dict(OWNED=owned.id, SHARED=shared.id, SECRET=secret.id, OPEN=open_doc.id)
    return ids, author, reader


def _link(service, source_id, target_id, link_type="cites"):
    return service.create_link(
        source_id,
        LinkCreate(source_id=source_id, target_id=target_id, link_type=link_type),
    )


# ── create: write on the source ─────────────────────────────────────────────


def test_write_on_the_source_is_required(db_session):
    """The entry condition in the step table, inverted: read on the source is not enough."""
    ids, _author, reader = _fixture(db_session)
    service = DocumentService(db_session)
    with _as(reader), pytest.raises(AccessDeniedError):
        _link(service, ids["OWNED"], ids["SHARED"])


def test_write_on_the_source_and_read_on_the_target_succeeds(db_session):
    """Citation across an access boundary is the case 4.1 chose this rule to permit."""
    ids, author, _reader = _fixture(db_session)
    service = DocumentService(db_session)
    with _as(author):
        link = _link(service, ids["OWNED"], ids["SHARED"])
    assert (link.source_id, link.target_id) == (ids["OWNED"], ids["SHARED"])


def test_write_on_the_target_is_not_required(db_session):
    """`author` holds READ on SHARED and never write; the edge is still legitimate."""
    ids, author, _reader = _fixture(db_session)
    repo = DocumentRepository(db_session)
    with _as(author):
        link = repo.create_link(source_id=ids["OWNED"], target_id=ids["SHARED"], link_type="cites")
    assert link.id is not None


# ── create: read on the target ──────────────────────────────────────────────


def test_an_unreadable_target_is_spelled_as_missing(db_session):
    """The entry condition in the step table: the edge to a document the caller cannot see.

    404, not 403 — ``require_add_child``'s house style. A 403 would confirm that SECRET
    exists to a principal for whom it is 404 everywhere else.
    """
    ids, author, _reader = _fixture(db_session)
    service = DocumentService(db_session)
    with _as(author), pytest.raises(LookupError):
        _link(service, ids["OWNED"], ids["SECRET"])


def test_a_caller_with_no_write_cannot_probe_target_existence(db_session):
    """Source first, and completely.

    `reader` may not write OWNED. Whether the target is SECRET (readable to it), OPEN, or
    an id that does not exist at all, the answer must be the same 403 — otherwise the verb
    is an existence oracle for a call that was never going to succeed.
    """
    ids, _author, reader = _fixture(db_session)
    service = DocumentService(db_session)
    for target in (ids["SECRET"], ids["OPEN"], 9_999_999):
        with _as(reader), pytest.raises(AccessDeniedError):
            _link(service, ids["OWNED"], target)


def test_an_unreadable_source_is_spelled_as_missing(db_session):
    """A source the caller cannot read at all is 404, before ``source_id`` is compared."""
    ids, author, _reader = _fixture(db_session)
    service = DocumentService(db_session)
    with _as(author), pytest.raises(LookupError):
        _link(service, ids["SECRET"], ids["OWNED"])


# ── ungoverned stays open ───────────────────────────────────────────────────


def test_ungoverned_documents_stay_writable_by_anyone(db_session):
    """``access.py:172`` — ungoverned → open. Access control here is opt-in and stays so.

    A gate that closed by default would be a different product, and it would break the
    single-user deployment this appliance is normally run as.
    """
    ids, _author, reader = _fixture(db_session)
    repo = DocumentRepository(db_session)
    with _as(reader):
        link = repo.create_link(source_id=ids["OPEN"], target_id=ids["SHARED"], link_type="cites")
    assert link.id is not None


def test_an_unbound_caller_bypasses_the_gate(db_session):
    """Workers and scripts bind no principal. ``summarization`` mints bridge edges this way."""
    ids, _author, _reader = _fixture(db_session)
    repo = DocumentRepository(db_session)
    link = repo.create_link(source_id=ids["SECRET"], target_id=ids["OWNED"], link_type="bridge")
    assert link.id is not None


# ── delete ──────────────────────────────────────────────────────────────────


def test_delete_needs_write_on_one_end(db_session):
    ids, author, reader = _fixture(db_session)
    repo = DocumentRepository(db_session)
    link = repo.create_link(source_id=ids["OWNED"], target_id=ids["SHARED"], link_type="cites")
    db_session.flush()
    service = DocumentService(db_session)
    with _as(reader), pytest.raises(AccessDeniedError):
        service.delete_link(ids["OWNED"], link.id)
    with _as(author):
        assert service.delete_link(ids["OWNED"], link.id)["deleted"] is True


def test_delete_of_an_asserted_edge_accepts_write_on_either_end(db_session):
    """``derived_by IS NULL``: nothing recorded who asserted it, so either end will do.

    The edge below runs SHARED → OWNED, so `author` has READ on the source and WRITE on
    the target. Guessing that the source asserted it would refuse this delete with no way
    to tell whether the guess was right.
    """
    ids, author, _reader = _fixture(db_session)
    repo = DocumentRepository(db_session)
    link = repo.create_link(source_id=ids["SHARED"], target_id=ids["OWNED"], link_type="cites")
    db_session.flush()
    with _as(author):
        assert repo.delete_link(link.id, incident_to=ids["OWNED"]) == "cites"


def test_delete_of_a_derived_edge_needs_write_on_the_source(db_session):
    """``derived_by IS NOT NULL``: the rule derived it FROM the source, so that is the end.

    Write on the target is refused because the rule would put the row back on its next
    run — a verb that reports success and changes nothing is the swallowed failure this
    codebase does not ship.
    """
    ids, author, _reader = _fixture(db_session)
    repo = DocumentRepository(db_session)
    link = repo.create_link(source_id=ids["SHARED"], target_id=ids["OWNED"], link_type="cites")
    link.derived_by = "a-rule"
    db_session.flush()
    with _as(author), pytest.raises(AccessDeniedError):
        repo.delete_link(link.id, incident_to=ids["OWNED"])


def test_delete_through_an_unreadable_document_is_missing(db_session):
    """The service's own 404, matching ``get_links``: an unreadable incident document."""
    ids, author, _reader = _fixture(db_session)
    repo = DocumentRepository(db_session)
    link = repo.create_link(source_id=ids["SECRET"], target_id=ids["OPEN"], link_type="cites")
    db_session.flush()
    service = DocumentService(db_session)
    with _as(author), pytest.raises(LookupError):
        service.delete_link(ids["SECRET"], link.id)
