"""Subtree RBAC — the five ``IndexService`` verbs that name a document take a gate.

They were ROADMAP known gap 12's ``IndexService`` share (``test_expose_principal_audit``):
a service that reached ``jmfts_core.access`` nowhere. The gates live in
``SearchRepository``, following ``DocumentRepository.embed_document`` (517f2a8):

    index_single_document   write on the document; hidden -> the same ValueError (400) a
                            missing document gets, read-only -> AccessDeniedError (403)
    add / remove root       write on the root; hidden or missing -> LookupError (404),
                            read-only -> AccessDeniedError (403)
    get_index_roots         roots the caller cannot read are left out
    refresh_index           write on EVERY root, checked before anything is cleared;
                            hidden or read-only -> IndexRefreshDeniedError (403), naming
                            no root

Owner and unbound (worker) callers bypass, as everywhere.
"""

import pytest
from sqlalchemy import func, select

from jmfts_client.contracts.index import IndexCreate
from jmfts_core.access import AccessDeniedError
from jmfts_core.models.document import Document
from jmfts_core.models.search_index import SearchIndex, SearchIndexEntry, SearchIndexMember
from jmfts_core.repositories.search import IndexRefreshDeniedError
from jmfts_core.services.index_service import IndexService
from tests.test_access_write_gate import _as, _fixture

INDEX = "rbac-index-verbs"


def _setup(session):
    _, ids, pr = _fixture(session)
    IndexService(session).create_index(IndexCreate(name=INDEX))
    return ids, pr


def _entries(session, doc_id):
    session.expire_all()
    return session.execute(
        select(func.count())
        .select_from(SearchIndexEntry)
        .join(SearchIndex, SearchIndex.id == SearchIndexEntry.index_id)
        .where(SearchIndex.name == INDEX, SearchIndexEntry.document_id == doc_id)
    ).scalar_one()


def _members(session):
    session.expire_all()
    return set(
        session.execute(
            select(SearchIndexMember.root_document_id)
            .join(SearchIndex, SearchIndex.id == SearchIndexMember.index_id)
            .where(SearchIndex.name == INDEX)
        ).scalars()
    )


def _missing_id(session):
    return (session.execute(select(func.max(Document.id))).scalar_one() or 0) + 1_000_000


# ── index_single_document ────────────────────────────────────────────────────


def test_index_document_read_only_is_denied_and_writes_nothing(db_session):
    ids, pr = _setup(db_session)
    with _as(pr["RO"]), pytest.raises(AccessDeniedError):
        IndexService(db_session).index_single_document(INDEX, ids["A1"])
    assert _entries(db_session, ids["A1"]) == 0


def test_index_document_non_reader_sees_missing_document(db_session):
    ids, pr = _setup(db_session)
    with _as(pr["NA"]), pytest.raises(ValueError) as hidden:
        IndexService(db_session).index_single_document(INDEX, ids["A1"])
    assert _entries(db_session, ids["A1"]) == 0

    missing = _missing_id(db_session)
    with _as(pr["NA"]), pytest.raises(ValueError) as absent:
        IndexService(db_session).index_single_document(INDEX, missing)
    # Same exception, same wording: hidden and absent differ only by the id echoed back.
    assert str(hidden.value).replace(str(ids["A1"]), "N") == str(absent.value).replace(
        str(missing), "N"
    )


def test_index_document_write_holder_allowed(db_session):
    ids, pr = _setup(db_session)
    with _as(pr["WR"]):
        out = IndexService(db_session).index_single_document(INDEX, ids["A2"])
    assert out["indexed_document"] == ids["A2"]
    assert _entries(db_session, ids["A2"]) == 1


def test_index_document_unbound_caller_allowed(db_session):
    ids, _ = _setup(db_session)
    IndexService(db_session).index_single_document(INDEX, ids["A1"])
    assert _entries(db_session, ids["A1"]) == 1


# ── add / remove root ────────────────────────────────────────────────────────


def test_add_root_read_only_is_denied(db_session):
    ids, pr = _setup(db_session)
    with _as(pr["RO"]), pytest.raises(AccessDeniedError):
        IndexService(db_session).add_root_to_index(INDEX, root_document_id=ids["A"])
    assert ids["A"] not in _members(db_session)


def test_add_root_non_reader_and_missing_root_are_the_same_404(db_session):
    ids, pr = _setup(db_session)
    with _as(pr["NA"]), pytest.raises(LookupError) as hidden:
        IndexService(db_session).add_root_to_index(INDEX, root_document_id=ids["A"])
    assert ids["A"] not in _members(db_session)

    missing = _missing_id(db_session)
    with _as(pr["NA"]), pytest.raises(LookupError) as absent:
        IndexService(db_session).add_root_to_index(INDEX, root_document_id=missing)
    assert str(hidden.value) == f"Document {ids['A']} not found"
    assert str(absent.value) == f"Document {missing} not found"


def test_add_root_write_holder_allowed(db_session):
    ids, pr = _setup(db_session)
    with _as(pr["WR"]):
        IndexService(db_session).add_root_to_index(INDEX, root_document_id=ids["A1"])
    assert ids["A1"] in _members(db_session)


def test_add_root_unknown_index_is_still_index_not_found(db_session):
    ids, pr = _setup(db_session)
    with _as(pr["NA"]), pytest.raises(LookupError, match="Index 'no-such-index' not found"):
        IndexService(db_session).add_root_to_index("no-such-index", root_document_id=ids["A"])


def test_remove_root_read_only_is_denied(db_session):
    ids, pr = _setup(db_session)
    IndexService(db_session).add_root_to_index(INDEX, root_document_id=ids["A"])
    with _as(pr["RO"]), pytest.raises(AccessDeniedError):
        IndexService(db_session).remove_root_from_index(INDEX, ids["A"])
    assert ids["A"] in _members(db_session)


def test_remove_root_non_reader_sees_missing_document(db_session):
    ids, pr = _setup(db_session)
    IndexService(db_session).add_root_to_index(INDEX, root_document_id=ids["A"])
    with _as(pr["NA"]), pytest.raises(LookupError, match=f"Document {ids['A']} not found"):
        IndexService(db_session).remove_root_from_index(INDEX, ids["A"])
    assert ids["A"] in _members(db_session)


def test_remove_root_write_holder_allowed(db_session):
    ids, pr = _setup(db_session)
    IndexService(db_session).add_root_to_index(INDEX, root_document_id=ids["A"])
    with _as(pr["WR"]):
        IndexService(db_session).remove_root_from_index(INDEX, ids["A"])
    assert ids["A"] not in _members(db_session)


# ── get_index_roots ──────────────────────────────────────────────────────────


def test_get_roots_hides_roots_the_caller_cannot_read(db_session):
    ids, pr = _setup(db_session)
    svc = IndexService(db_session)
    svc.add_root_to_index(INDEX, root_document_id=ids["A"])
    svc.add_root_to_index(INDEX, root_document_id=ids["U"])

    with _as(pr["NA"]):
        assert set(svc.get_index_roots(INDEX)["roots"]) == {ids["U"]}
    with _as(pr["RO"]):
        assert set(svc.get_index_roots(INDEX)["roots"]) == {ids["A"], ids["U"]}
    assert set(svc.get_index_roots(INDEX)["roots"]) == {ids["A"], ids["U"]}


# ── refresh_index ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("who", ["RO", "NA"])
def test_refresh_without_write_on_every_root_is_denied_and_clears_nothing(db_session, who):
    ids, pr = _setup(db_session)
    svc = IndexService(db_session)
    svc.add_root_to_index(INDEX, root_document_id=ids["A"])
    svc.add_root_to_index(INDEX, root_document_id=ids["U"])
    svc.refresh_index(INDEX)
    before = _entries(db_session, ids["A1"])
    assert before == 1

    with _as(pr[who]), pytest.raises(IndexRefreshDeniedError) as denied:
        svc.refresh_index(INDEX)
    assert isinstance(denied.value, AccessDeniedError)  # -> the app-level 403 handler
    # Names the index, not the root: a non-reader learns nothing about which root it was.
    assert str(ids["A"]) not in str(denied.value)
    assert _entries(db_session, ids["A1"]) == before


def test_refresh_write_holder_on_every_root_allowed(db_session):
    ids, pr = _setup(db_session)
    svc = IndexService(db_session)
    svc.add_root_to_index(INDEX, root_document_id=ids["A"])
    svc.add_root_to_index(INDEX, root_document_id=ids["U"])  # ungoverned: open to write
    with _as(pr["WR"]):
        out = svc.refresh_index(INDEX)
    assert out["indexed"] >= 4  # A, A1, A2, U
    assert _entries(db_session, ids["A2"]) == 1
