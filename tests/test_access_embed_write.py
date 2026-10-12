"""Subtree RBAC — ``DocumentService.embed_document`` takes the write gate.

It writes embeddings onto a document named by id and reaches ``DocumentRepository.
embed_document`` rather than ``.update``, so it once skipped the ``require_write`` every
sibling write verb performs (ROADMAP known gap 12; ``test_expose_principal_audit``).

    hidden (unreadable) document -> LookupError          (404, existence-hiding)
    readable but not writable    -> AccessDeniedError    (403)
    write holder / owner         -> allowed
"""

import pytest

from jmfts_core.access import AccessDeniedError
from jmfts_core.services.document_service import DocumentService
from tests.test_access_write_gate import _as, _fixture


def _embedded(session, doc_id):
    session.expire_all()
    from jmfts_core.repositories.document import DocumentRepository

    return DocumentRepository(session).get(doc_id).embed is not None


def test_embed_read_only_is_denied_and_writes_nothing(db_session):
    _, ids, pr = _fixture(db_session)
    with _as(pr["RO"]), pytest.raises(AccessDeniedError):
        DocumentService(db_session).embed_document(ids["A1"], with_tokens=False)
    assert not _embedded(db_session, ids["A1"])


def test_embed_non_reader_sees_missing_document(db_session):
    _, ids, pr = _fixture(db_session)
    with _as(pr["NA"]), pytest.raises(LookupError):
        DocumentService(db_session).embed_document(ids["A1"], with_tokens=False)
    assert not _embedded(db_session, ids["A1"])


def test_embed_write_holder_allowed(db_session):
    _, ids, pr = _fixture(db_session)
    with _as(pr["WR"]):
        out = DocumentService(db_session).embed_document(ids["A2"], with_tokens=False)
    assert out["embedded"] is True
    assert _embedded(db_session, ids["A2"])


def test_embed_unbound_caller_allowed(db_session):
    _, ids, _ = _fixture(db_session)
    out = DocumentService(db_session).embed_document(ids["U"], with_tokens=False)
    assert out["embedded"] is True
