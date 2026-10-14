"""Subtree RBAC — ``IngestService.file_frontier`` takes the read gate.

It reports settle counts under a document named by id. Without ``can_read`` it told "a
document with work under it" from "no such document" for a caller that may read neither
(ROADMAP known gap 12; ``test_expose_principal_audit``).

    hidden (unreadable) node -> LookupError, same message as a missing id   (404)
    reader                   -> allowed
"""

import pytest

from jmfts_core.services.ingest_service import IngestService
from tests.test_access_write_gate import _as, _fixture


def test_frontier_non_reader_sees_missing_document(db_session):
    _, ids, pr = _fixture(db_session)
    with _as(pr["NA"]), pytest.raises(LookupError) as hidden:
        IngestService(db_session).file_frontier(ids["A1"])
    with pytest.raises(LookupError) as missing:
        IngestService(db_session).file_frontier(10**9)
    # Existence-hiding: the two refusals read the same apart from the id.
    assert str(hidden.value).replace(str(ids["A1"]), "N") == str(missing.value).replace(
        str(10**9), "N"
    )


def test_frontier_read_only_holder_allowed(db_session):
    _, ids, pr = _fixture(db_session)
    with _as(pr["RO"]):
        out = IngestService(db_session).file_frontier(ids["A"])
    assert out.document_id == ids["A"]


def test_frontier_ungoverned_node_open_to_anyone(db_session):
    _, ids, pr = _fixture(db_session)
    with _as(pr["NA"]):
        assert IngestService(db_session).file_frontier(ids["U"]).document_id == ids["U"]
