"""Subtree RBAC — ``OntologyService.create_binding`` checks the documents its scope names.

A binding's scope may be a ``parent_id`` or an explicit ``document_ids`` set — a document
reference that was written with no check (ROADMAP known gap 12;
``test_expose_principal_audit``). A governed caller may now name only documents it can read.

    scope names a hidden document  -> LookupError        (404, same as a missing id)
    scope names readable documents -> allowed
    usetype scope                  -> no document named, not checked
    unbound / owner caller         -> unchanged, may name an id that does not exist yet
"""

import pytest

from jmfts_client.contracts.rdf import ShapeBindingCreate
from jmfts_core.services.ontology_service import OntologyService
from tests.test_access_write_gate import _as, _fixture

pytest.importorskip("rdflib", reason="the rdf extra is not installed")

from tests.test_ontology_import import EX, VENDOR_TTL  # noqa: E402

SHAPE = EX + "VendorShape"


def _service(session):
    service = OntologyService(session)
    service.import_ontology(VENDOR_TTL, name="vendors", base_iri=EX)
    return service


def test_subtree_scope_on_hidden_document_is_missing(db_session):
    _, ids, pr = _fixture(db_session)
    service = _service(db_session)
    with _as(pr["NA"]), pytest.raises(LookupError, match=f"Document {ids['A1']} not found"):
        service.create_binding("vendors", ShapeBindingCreate(shape_iri=SHAPE, parent_id=ids["A1"]))
    assert service.list_bindings(ontology="vendors") == []


def test_document_set_with_one_hidden_member_is_refused(db_session):
    _, ids, pr = _fixture(db_session)
    service = _service(db_session)
    request = ShapeBindingCreate(shape_iri=SHAPE, document_ids=[ids["U"], ids["A2"]])
    with _as(pr["NA"]), pytest.raises(LookupError, match=f"Document {ids['A2']} not found"):
        service.create_binding("vendors", request)
    assert service.list_bindings(ontology="vendors") == []


def test_governed_caller_naming_a_missing_id_gets_the_same_refusal(db_session):
    _, _, pr = _fixture(db_session)
    service = _service(db_session)
    with _as(pr["NA"]), pytest.raises(LookupError, match="not found"):
        service.create_binding("vendors", ShapeBindingCreate(shape_iri=SHAPE, parent_id=10**9))


def test_reader_may_bind_readable_documents(db_session):
    _, ids, pr = _fixture(db_session)
    service = _service(db_session)
    request = ShapeBindingCreate(shape_iri=SHAPE, document_ids=[ids["A1"], ids["U"]])
    with _as(pr["RO"]):
        binding = service.create_binding("vendors", request)
    assert binding.scope == {"document_ids": [ids["A1"], ids["U"]]}


def test_usetype_scope_is_not_checked(db_session):
    _, _, pr = _fixture(db_session)
    service = _service(db_session)
    with _as(pr["NA"]):
        binding = service.create_binding(
            "vendors", ShapeBindingCreate(shape_iri=SHAPE, usetype_pattern="profile:sheet")
        )
    assert binding.scope_type == "usetype"


def test_unbound_caller_unchanged(db_session):
    _, ids, _ = _fixture(db_session)
    service = _service(db_session)
    binding = service.create_binding(
        "vendors", ShapeBindingCreate(shape_iri=SHAPE, parent_id=ids["A1"])
    )
    assert binding.scope == {"parent_id": ids["A1"]}
