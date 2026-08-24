"""Turtle in. ``docs/SPRINT_0_3_0.md`` 5.1, 5.2 and 4.5.

Three layers, and the middle one is the point of the sprint.

1. The parser, against the SHACL subset 5.1 fixes as a CEILING — which means the tests
   that matter most are the ones asserting a term is NOT read and IS reported.
2. The importer: what lands in ``predicates``, what lands in ``ontologies``, and what it
   refuses to do to a predicate that already exists.
3. The wire, because ``text/turtle`` is the one request shape in this tree that is not
   JSON and not multipart, and the adapter had to be taught it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jmfts_client.contracts.rdf import ShapeBindingCreate
from jmfts_core.database import get_db
from jmfts_core.rest.main import app
from jmfts_core.services.ontology_service import (
    BindingConflictError,
    OntologyService,
    ShapeNotDeclaredError,
)

rdflib = pytest.importorskip("rdflib", reason="the rdf extra is not installed")

from jmfts_core.rdf.parse import (  # noqa: E402  (after the importorskip, deliberately)
    TurtleParseError,
    parse_ontology,
)

EX = "https://example.org/vendors#"

VENDOR_TTL = f"""
@prefix ex:  <{EX}> .
@prefix sh:  <http://www.w3.org/ns/shacl#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .

ex:name    a owl:DatatypeProperty .
ex:country a owl:DatatypeProperty .
ex:parent  a owl:ObjectProperty .

ex:VendorShape
    a sh:NodeShape ;
    sh:targetClass ex:Vendor ;
    sh:property [
        sh:path ex:name ;
        sh:datatype xsd:string ;
        sh:minCount 1 ;
        sh:maxCount 1 ;
        sh:name "Vendor name"
    ] ;
    sh:property [
        sh:path ex:country ;
        sh:in ( "US" "CA" "MX" )
    ] ;
    sh:property [
        sh:path ex:parent ;
        sh:class ex:Vendor ;
        sh:maxCount 1
    ] .
"""


# ---------------------------------------------------------------------------
# 1. The parser, and the ceiling
# ---------------------------------------------------------------------------


class TestParser:
    def test_the_subset_is_read_in_full(self):
        parsed = parse_ontology(VENDOR_TTL)
        assert [s.iri for s in parsed.shapes] == [EX + "VendorShape"]
        shape = parsed.shapes[0]
        assert shape.target_class == EX + "Vendor"
        by_path = {p.path: p for p in shape.properties}
        assert set(by_path) == {EX + "name", EX + "country", EX + "parent"}

        name = by_path[EX + "name"]
        assert name.datatype == "http://www.w3.org/2001/XMLSchema#string"
        assert (name.min_count, name.max_count) == (1, 1)
        assert name.name == "Vendor name"

        assert by_path[EX + "country"].allowed_values == ["US", "CA", "MX"]
        assert by_path[EX + "parent"].class_iri == EX + "Vendor"
        assert by_path[EX + "parent"].max_count == 1

    def test_an_absent_constraint_is_null_and_not_a_permissive_default(self):
        """A null ``min_count`` is "unconstrained"; ``0`` is "explicitly optional". They
        are different statements and the parser must not turn the first into the second."""
        shape = parse_ontology(VENDOR_TTL).shapes[0]
        country = next(p for p in shape.properties if p.path == EX + "country")
        assert country.min_count is None
        assert country.max_count is None
        assert country.datatype is None

    def test_predicate_terms_come_from_paths_and_from_property_declarations(self):
        parsed = parse_ontology(VENDOR_TTL)
        assert set(parsed.predicate_terms) == {EX + "name", EX + "country", EX + "parent"}
        assert parsed.predicate_terms[EX + "name"] == "name"

    def test_a_term_outside_the_subset_is_reported_and_not_read(self):
        """The whole argument for the ceiling. ``sh:pattern`` is a real constraint; reading
        it as nothing while storing the document would give a caller a shape that passes on
        data the shape forbids, and the silence would look like a pass."""
        turtle = f"""
        @prefix ex: <{EX}> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .
        ex:S a sh:NodeShape ;
            sh:property [ sh:path ex:code ; sh:pattern "^[A-Z]{{3}}$" ; sh:minCount 1 ] .
        """
        parsed = parse_ontology(turtle)
        assert parsed.unsupported_terms == ["http://www.w3.org/ns/shacl#pattern"]
        prop = parsed.shapes[0].properties[0]
        assert prop.min_count == 1
        assert not hasattr(prop, "pattern")

    def test_owl_axioms_are_stored_but_reported_as_unread(self):
        """5.4 admits two OWL predicates as STRUCTURAL, and 5.1 says no reasoner. Storing
        an axiom as a triple is not acting on it, and this sprint does not act on it."""
        turtle = f"""
        @prefix ex:  <{EX}> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        ex:taxId a owl:InverseFunctionalProperty .
        ex:vendorName owl:equivalentProperty ex:name .
        """
        parsed = parse_ontology(turtle)
        assert "http://www.w3.org/2002/07/owl#equivalentProperty" in parsed.unsupported_terms
        assert "http://www.w3.org/2002/07/owl#InverseFunctionalProperty" in (
            parsed.unsupported_terms
        )

    def test_only_the_documents_own_prefixes_are_kept(self):
        """rdflib binds a table of its own into every Graph; storing it would make two
        uploads of unrelated vocabularies report near-identical prefix maps."""
        prefixes = parse_ontology(VENDOR_TTL).prefixes
        assert prefixes["ex"] == EX
        assert "rdflib" not in "".join(prefixes)
        # rdflib's own defaults bind names like `brick:` and `dcat:` that this document
        # never mentioned.
        assert set(prefixes) <= {"ex", "sh", "xsd", "owl"}

    def test_bad_turtle_is_a_named_error_carrying_the_parser_message(self):
        with pytest.raises(TurtleParseError) as caught:
            parse_ontology("@prefix ex: <https://example.org/> \nex:a ex:b")
        assert "Turtle" in str(caught.value)

    def test_a_property_shape_with_no_path_is_refused(self):
        turtle = f"""
        @prefix ex: <{EX}> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .
        ex:S a sh:NodeShape ; sh:property [ sh:minCount 1 ] .
        """
        with pytest.raises(TurtleParseError, match="sh:path"):
            parse_ontology(turtle)

    def test_a_blank_node_shape_is_refused_because_it_cannot_be_bound(self):
        """``shape_bindings.shape_iri`` is how a binding names a shape, and a blank node's
        id is an artefact of this parse rather than a name the document gave it."""
        turtle = f"""
        @prefix ex: <{EX}> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .
        [ a sh:NodeShape ; sh:targetClass ex:Vendor ] .
        """
        with pytest.raises(TurtleParseError, match="blank node"):
            parse_ontology(turtle)

    def test_a_property_path_expression_is_refused_rather_than_read_as_an_iri(self):
        """5.1 admits ``sh:path`` as a predicate IRI. A path expression read as if it were
        one would silently constrain the wrong property."""
        turtle = f"""
        @prefix ex: <{EX}> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .
        ex:S a sh:NodeShape ;
            sh:property [ sh:path [ sh:inversePath ex:parent ] ; sh:minCount 1 ] .
        """
        with pytest.raises(TurtleParseError, match="property-path"):
            parse_ontology(turtle)

    def test_one_constraint_asserted_twice_with_two_values_is_refused(self):
        """Picking one silently is how a shape ends up enforcing something nobody wrote."""
        turtle = f"""
        @prefix ex: <{EX}> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .
        ex:S a sh:NodeShape ;
            sh:property [ sh:path ex:code ; sh:minCount 1 ; sh:minCount 2 ] .
        """
        with pytest.raises(TurtleParseError):
            parse_ontology(turtle)


# ---------------------------------------------------------------------------
# 2. The importer
# ---------------------------------------------------------------------------


class TestImport:
    def _import(self, db_session, turtle=VENDOR_TTL, name="vendors"):
        return OntologyService(db_session).import_ontology(
            turtle, name=name, base_iri=EX, description="test vocabulary"
        )

    def test_predicates_land_in_the_registry_with_their_iris(self, db_session):
        result = self._import(db_session)
        assert len(result.predicates_created) == 3
        assert result.predicates_reused == []
        assert result.predicate_conflicts == []

        from jmfts_core.repositories.triple import TripleRepository

        repo = TripleRepository(db_session)
        term = repo.get_predicate_by_iri(EX + "name")
        assert term is not None
        assert term.name == "name"
        # The vocabulary's name, so `GET /triples/predicates?namespace=vendors` answers
        # "which predicates came from this ontology". NOT rdfs:domain — that collision is
        # what the 4.4 rename was for.
        assert term.namespace == "vendors"

    def test_shapes_land_in_the_ontologies_table_beside_the_source_bytes(self, db_session):
        result = self._import(db_session)
        assert result.replaced is False
        assert [s.iri for s in result.ontology.shapes] == [EX + "VendorShape"]
        # The bytes are the record; the digest is a cache of a parse.
        assert result.ontology.source_turtle == VENDOR_TTL
        assert result.ontology.base_iri == EX

    def test_reimporting_the_same_vocabulary_reuses_the_predicates(self, db_session):
        first = self._import(db_session)
        second = self._import(db_session)
        assert second.replaced is True
        assert second.predicates_created == []
        assert sorted(second.predicates_reused) == sorted(first.predicates_created)

    def test_a_local_predicate_of_the_same_name_is_reported_not_rebound(self, db_session):
        """The ambiguity the import refuses to resolve. Rebinding would retroactively claim
        every fact recorded under the local predicate was about the vocabulary's property."""
        from jmfts_core.repositories.triple import TripleRepository

        repo = TripleRepository(db_session)
        local = repo.create_predicate(name="name", description="minted locally, long ago")
        db_session.flush()

        result = self._import(db_session)
        conflicts = {c.name: c for c in result.predicate_conflicts}
        assert set(conflicts) == {"name"}
        assert conflicts["name"].iri == EX + "name"
        assert conflicts["name"].existing_predicate_id == local.id
        assert conflicts["name"].existing_iri is None
        # And the row was left alone.
        db_session.refresh(local)
        assert local.iri is None
        # The other two terms still landed; one conflict does not fail the import.
        assert len(result.predicates_created) == 2

    def test_an_unreadable_document_stores_nothing(self, db_session):
        with pytest.raises(TurtleParseError):
            self._import(db_session, turtle="this is not turtle {{{")
        from jmfts_core.repositories.ontology import OntologyRepository

        assert OntologyRepository(db_session).get("vendors") is None


# ---------------------------------------------------------------------------
# 3. Bindings
# ---------------------------------------------------------------------------


class TestBindings:
    def _imported(self, db_session):
        service = OntologyService(db_session)
        service.import_ontology(VENDOR_TTL, name="vendors", base_iri=EX)
        return service

    def test_a_shape_binds_to_a_usetype_scope(self, db_session):
        service = self._imported(db_session)
        binding = service.create_binding(
            "vendors",
            ShapeBindingCreate(shape_iri=EX + "VendorShape", usetype_pattern="profile:sheet"),
        )
        assert binding.scope_type == "usetype"
        assert binding.scope == {"pattern": "profile:sheet"}
        assert [b.id for b in service.list_bindings(ontology="vendors")] == [binding.id]

    def test_a_shape_binds_to_a_subtree_and_to_a_document_set(self, db_session):
        service = self._imported(db_session)
        subtree = service.create_binding(
            "vendors", ShapeBindingCreate(shape_iri=EX + "VendorShape", parent_id=7)
        )
        explicit = service.create_binding(
            "vendors", ShapeBindingCreate(shape_iri=EX + "VendorShape", document_ids=[1, 2, 3])
        )
        assert subtree.scope_type == "subtree" and subtree.scope == {"parent_id": 7}
        assert explicit.scope_type == "documents"
        assert explicit.scope == {"document_ids": [1, 2, 3]}

    def test_a_binding_names_exactly_one_scope(self, db_session):
        """Zero would match nothing; two would match by whichever query ran first. Both are
        silent, so both are refused."""
        service = self._imported(db_session)
        with pytest.raises(ValueError):
            service.create_binding("vendors", ShapeBindingCreate(shape_iri=EX + "VendorShape"))
        with pytest.raises(ValueError):
            service.create_binding(
                "vendors",
                ShapeBindingCreate(shape_iri=EX + "VendorShape", parent_id=1, document_ids=[2]),
            )

    def test_binding_a_shape_the_ontology_does_not_declare_is_a_404(self, db_session):
        """Checked at BIND time, where the answer can be reported to whoever typed the IRI,
        rather than surfacing later as a rule that matches nothing."""
        service = self._imported(db_session)
        with pytest.raises(ShapeNotDeclaredError) as caught:
            service.create_binding(
                "vendors", ShapeBindingCreate(shape_iri=EX + "NoSuchShape", parent_id=1)
            )
        assert EX + "VendorShape" in str(caught.value)

    def test_the_same_binding_twice_is_a_conflict(self, db_session):
        service = self._imported(db_session)
        request = ShapeBindingCreate(shape_iri=EX + "VendorShape", parent_id=7)
        service.create_binding("vendors", request)
        with pytest.raises(BindingConflictError):
            service.create_binding("vendors", request)

    def test_deleting_a_vocabulary_takes_its_bindings_and_leaves_its_predicates(self, db_session):
        """Predicates are rows facts point at, and ``triples.predicate_id`` cascades —
        dropping one here would delete the facts recorded with it."""
        service = self._imported(db_session)
        service.create_binding(
            "vendors", ShapeBindingCreate(shape_iri=EX + "VendorShape", parent_id=7)
        )
        service.delete_ontology("vendors")
        assert service.list_bindings() == []

        from jmfts_core.repositories.triple import TripleRepository

        assert TripleRepository(db_session).get_predicate_by_iri(EX + "name") is not None


# ---------------------------------------------------------------------------
# 4. The wire
# ---------------------------------------------------------------------------


@pytest.fixture
def client(db_session):
    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    from tests.conftest import AUTH_HEADERS

    with TestClient(app, headers=AUTH_HEADERS) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_db, None)


class TestWire:
    def test_the_body_is_the_turtle_document_itself(self, client):
        """``text/turtle``, not a JSON envelope. ``curl --data-binary @vocab.ttl`` is the
        whole upload, and the bytes stored are the bytes that were sent."""
        response = client.post(
            "/ontologies",
            params={"name": "vendors", "base_iri": EX},
            content=VENDOR_TTL,
            headers={"Content-Type": "text/turtle"},
        )
        assert response.status_code == 201, response.text
        payload = response.json()
        assert payload["ontology"]["source_turtle"] == VENDOR_TTL
        assert len(payload["predicates_created"]) == 3

    def test_the_openapi_document_declares_the_media_type(self, client):
        """What the adapter had to be taught. A route documented as ``text/turtle`` and
        served as JSON would be a document nobody could write a client from."""
        schema = client.get("/openapi.json").json()
        content = schema["paths"]["/ontologies"]["post"]["requestBody"]["content"]
        assert list(content) == ["text/turtle"]

    def test_unparseable_turtle_is_a_422(self, client):
        response = client.post(
            "/ontologies",
            params={"name": "broken", "base_iri": EX},
            content="not turtle {{{",
            headers={"Content-Type": "text/turtle"},
        )
        assert response.status_code == 422, response.text

    def test_the_generated_client_posts_the_bytes_verbatim(self, db_session):
        """The generated verb sends ``content`` under ``text/turtle`` rather than wrapping
        the document in JSON — which is what ``scripts/generate_client`` learned to emit."""
        from jmfts_client import RemoteJmftsClient

        def _override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = _override_get_db
        from tests.conftest import AUTH_HEADERS

        transport = TestClient(app, headers=AUTH_HEADERS)
        try:
            jmfts = RemoteJmftsClient("http://testserver", client=transport)
            result = jmfts.import_ontology(VENDOR_TTL, name="vendors", base_iri=EX)
            assert result.ontology.source_turtle == VENDOR_TTL
            assert type(result).__name__ == "OntologyImportResponse"
            binding = jmfts.create_binding(
                "vendors",
                ShapeBindingCreate(shape_iri=EX + "VendorShape", usetype_pattern="vendor"),
            )
            assert binding.scope == {"pattern": "vendor"}
        finally:
            app.dependency_overrides.pop(get_db, None)
