"""Turtle out. ``docs/SPRINT_0_3_0.md`` 5.3.

The serialiser is built before the importer and before the extractor because it is how
their output gets REVIEWED, so these tests are mostly about whether the document that comes
out is READABLE and TRUE, not about whether rdflib works.

Three of them are really tests of migration 013's schema, asked from the outside: a literal
object with no datatype has to come back as a plain literal, a typed one has to keep its
type, and an invalidated fact must not be written at all. A store that could not answer
those would be one whose ``object_literal`` column did not carry enough to reconstruct what
was asserted.
"""

import pytest

from jmfts_core.graph_analysis import SAME_AS_LINK_TYPE
from jmfts_core.rdf.names import (
    DEFAULT_BASE_IRI,
    DOCUMENT_PREFIX,
    PREDICATE_PREFIX,
    document_iri,
    document_namespace,
    iri_problem,
    local_name,
    predicate_iri,
    predicate_namespace,
)
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.triple import TripleRepository

rdflib = pytest.importorskip("rdflib", reason="the rdf extra is not installed")

from jmfts_core.rdf.serialize import (  # noqa: E402  (after the importorskip, deliberately)
    TurtleSerializationError,
    triples_to_turtle,
)

XSD_INTEGER = "http://www.w3.org/2001/XMLSchema#integer"


# ---------------------------------------------------------------------------
# Naming. No database, no rdflib.
# ---------------------------------------------------------------------------


class TestNames:
    def test_a_minted_iri_is_a_urn_and_says_so(self):
        """An http:// IRI would claim something answers at that address. Nothing does."""
        assert document_iri(42) == "urn:jmfts:document:42"
        assert document_iri(42, "https://kb.example/") == "https://kb.example/document:42"

    def test_a_predicate_name_is_percent_encoded(self):
        """``predicates.name`` is free text and a URN admits no spaces."""
        assert predicate_iri("works_at") == "urn:jmfts:predicate:works_at"
        assert predicate_iri("works at") == "urn:jmfts:predicate:works%20at"

    def test_two_namespaces_are_what_make_the_output_readable(self):
        """The claim `names.py` measures, asserted against rdflib rather than argued.

        Binding ONE prefix on the base IRI does not work: rdflib splits an IRI at its own
        trailing-name boundary and will not use a prefix that stops short of it, so every
        subject comes out as `<urn:jmfts:document:1>`. Binding the two namespaces the
        module defines gives `doc:1`.
        """
        subject, predicate, obj = (
            rdflib.URIRef(document_iri(1)),
            rdflib.URIRef(predicate_iri("knows")),
            rdflib.URIRef(document_iri(2)),
        )

        one = rdflib.Graph()
        one.bind("jmfts", rdflib.Namespace(DEFAULT_BASE_IRI))
        one.add((subject, predicate, obj))
        assert "<urn:jmfts:document:1>" in one.serialize(format="turtle")

        two = rdflib.Graph()
        two.bind(DOCUMENT_PREFIX, rdflib.Namespace(document_namespace()))
        two.bind(PREDICATE_PREFIX, rdflib.Namespace(predicate_namespace()))
        two.add((subject, predicate, obj))
        text = two.serialize(format="turtle")
        assert "doc:1 prop:knows doc:2 ." in text
        # The only angle brackets left are the two @prefix declarations at the top.
        assert "<urn:jmfts:document:1>" not in text

    def test_local_name_splits_on_the_rightmost_separator(self):
        assert local_name("http://xmlns.com/foaf/0.1/knows") == "knows"
        assert local_name("http://www.w3.org/2000/01/rdf-schema#label") == "label"

    def test_local_name_of_an_iri_with_no_separator_is_the_whole_iri(self):
        """Never an empty string: ``predicates.name`` is UNIQUE, so two nameless terms
        would collide with each other rather than being reported as distinct."""
        assert local_name("urn:isbn:0451450523") == "urn:isbn:0451450523"
        assert local_name("http://example.org/vocab#") == "http://example.org/vocab#"

    def test_iri_problem_passes_an_iri_and_names_what_is_wrong_with_the_rest(self):
        assert iri_problem("http://www.w3.org/2001/XMLSchema#integer") is None
        assert iri_problem("urn:jmfts:document:1") is None
        assert iri_problem("urn:isbn:0451450523") is None
        # A CURIE: one prefix, one local name, nothing else.
        assert "CURIE" in iri_problem("xsd:integer")
        # A relative reference resolves against the READER's base, not the writer's.
        assert "RELATIVE" in iri_problem("integer")
        assert iri_problem("") is not None
        assert iri_problem("http://example.org/a b") is not None


# ---------------------------------------------------------------------------
# Serialising the store
# ---------------------------------------------------------------------------


def _entity(docs, name):
    return docs.create(title=name, content=name, usetype="entity", auto_embed=False)


def _parse(turtle):
    graph = rdflib.Graph()
    graph.parse(data=turtle, format="turtle")
    return graph


class TestTurtleOut:
    def test_a_resource_object_becomes_two_iris_and_two_labels(self, db_session):
        docs, triples = DocumentRepository(db_session), TripleRepository(db_session)
        acme, john = _entity(docs, "Acme"), _entity(docs, "John")
        db_session.flush()
        works_at = triples.create_predicate(name="works_at")
        db_session.flush()
        triples.create_triple(subject_id=john.id, predicate_id=works_at.id, object_id=acme.id)
        db_session.flush()

        export = triples_to_turtle(db_session, entity_id=john.id)
        graph = _parse(export.turtle)

        assert export.triple_count == 1
        assert (
            rdflib.URIRef(document_iri(john.id)),
            rdflib.URIRef(predicate_iri("works_at")),
            rdflib.URIRef(document_iri(acme.id)),
        ) in graph
        labels = {str(o) for _, _, o in graph.triples((None, rdflib.RDFS.label, None))}
        assert labels == {"Acme", "John"}

    def test_an_untyped_literal_comes_back_as_a_plain_literal(self, db_session):
        """A NULL ``object_datatype`` is ``xsd:string``, not "unknown" — so the literal
        must NOT acquire a type on the way out."""
        docs, triples = DocumentRepository(db_session), TripleRepository(db_session)
        acme = _entity(docs, "Acme")
        db_session.flush()
        founded = triples.create_predicate(name="founded_in")
        db_session.flush()
        triples.create_triple(subject_id=acme.id, predicate_id=founded.id, object_literal="1999")
        db_session.flush()

        graph = _parse(triples_to_turtle(db_session, entity_id=acme.id).turtle)
        objects = [o for _, p, o in graph if p == rdflib.URIRef(predicate_iri("founded_in"))]
        assert objects == [rdflib.Literal("1999")]
        assert objects[0].datatype is None

    def test_a_typed_literal_keeps_its_datatype(self, db_session):
        docs, triples = DocumentRepository(db_session), TripleRepository(db_session)
        acme = _entity(docs, "Acme")
        db_session.flush()
        revenue = triples.create_predicate(name="revenue")
        db_session.flush()
        triples.create_triple(
            subject_id=acme.id,
            predicate_id=revenue.id,
            object_literal="128000",
            object_datatype=XSD_INTEGER,
        )
        db_session.flush()

        export = triples_to_turtle(db_session, entity_id=acme.id)
        graph = _parse(export.turtle)
        objects = [o for _, p, o in graph if p == rdflib.URIRef(predicate_iri("revenue"))]
        assert objects == [rdflib.Literal("128000", datatype=rdflib.XSD.integer)]
        # And the lexical form survived: 128000 is what was asserted, not 128000.0.
        assert str(objects[0]) == "128000"

    def test_a_datatype_that_is_not_an_iri_is_refused_by_name(self, db_session):
        """``object_datatype`` is a VARCHAR(100) with no CHECK on its shape, so this is
        where the shape gets checked. It raises rather than dropping the datatype."""
        docs, triples = DocumentRepository(db_session), TripleRepository(db_session)
        acme = _entity(docs, "Acme")
        db_session.flush()
        revenue = triples.create_predicate(name="revenue")
        db_session.flush()
        bad = triples.create_triple(
            subject_id=acme.id,
            predicate_id=revenue.id,
            object_literal="128000",
            object_datatype="xsd:integer",
        )
        db_session.flush()

        with pytest.raises(TurtleSerializationError) as excinfo:
            triples_to_turtle(db_session, entity_id=acme.id)
        assert str(bad.id) in str(excinfo.value)

    def test_a_predicate_with_an_iri_is_called_by_it(self, db_session):
        """The ``iri`` column exists so an imported term keeps its vocabulary's name.
        Re-minting one under the local base would make one property into two."""
        docs, triples = DocumentRepository(db_session), TripleRepository(db_session)
        a, b = _entity(docs, "A"), _entity(docs, "B")
        db_session.flush()
        knows = triples.create_predicate(name="knows", iri="http://xmlns.com/foaf/0.1/knows")
        db_session.flush()
        triples.create_triple(subject_id=a.id, predicate_id=knows.id, object_id=b.id)
        db_session.flush()

        graph = _parse(triples_to_turtle(db_session, entity_id=a.id).turtle)
        predicates = {str(p) for _, p, _ in graph if p != rdflib.RDFS.label}
        assert predicates == {"http://xmlns.com/foaf/0.1/knows"}

    def test_an_invalidated_fact_is_never_written(self, db_session):
        """Turtle asserts its triples and the 5.1 subset cannot say "retracted", so an
        export is of the LIVE graph and there is no flag to ask for anything else."""
        docs, triples = DocumentRepository(db_session), TripleRepository(db_session)
        acme, nyc, sf = _entity(docs, "Acme"), _entity(docs, "NYC"), _entity(docs, "SF")
        db_session.flush()
        based_in = triples.create_predicate(name="based_in")
        db_session.flush()
        old = triples.create_triple(subject_id=acme.id, predicate_id=based_in.id, object_id=nyc.id)
        db_session.flush()
        triples.supersede_triple(
            old_triple_id=old.id,
            subject_id=acme.id,
            predicate_id=based_in.id,
            object_id=sf.id,
        )
        db_session.flush()

        export = triples_to_turtle(db_session, entity_id=acme.id)
        graph = _parse(export.turtle)
        assert export.triple_count == 1
        assert (None, None, rdflib.URIRef(document_iri(nyc.id))) not in graph
        assert (None, None, rdflib.URIRef(document_iri(sf.id))) in graph

    def test_provenance_splits_the_asserted_layer_from_a_derived_one(self, db_session):
        """4.2's whole point, exercised: with ``derived_by`` the split is a WHERE clause."""
        docs, triples = DocumentRepository(db_session), TripleRepository(db_session)
        a, b, c = _entity(docs, "A"), _entity(docs, "B"), _entity(docs, "C")
        db_session.flush()
        rel = triples.create_predicate(name="rel")
        db_session.flush()
        triples.create_triple(subject_id=a.id, predicate_id=rel.id, object_id=b.id)
        triples.create_triple(
            subject_id=a.id, predicate_id=rel.id, object_id=c.id, derived_by="ifp:closure"
        )
        db_session.flush()

        assert triples_to_turtle(db_session, entity_id=a.id).triple_count == 2
        asserted = triples_to_turtle(db_session, entity_id=a.id, provenance="asserted")
        derived = triples_to_turtle(db_session, entity_id=a.id, provenance="derived")
        assert asserted.triple_count == 1
        assert derived.triple_count == 1
        assert (None, None, rdflib.URIRef(document_iri(c.id))) in _parse(derived.turtle)
        assert (None, None, rdflib.URIRef(document_iri(c.id))) not in _parse(asserted.turtle)

    def test_an_unrecognised_provenance_raises_rather_than_widening(self, db_session):
        with pytest.raises(ValueError):
            triples_to_turtle(db_session, provenance="everything")

    def test_a_same_as_cluster_is_reported_and_not_collapsed(self, db_session):
        """The cluster's facts are unioned and the aliasing is written out as owl:sameAs.

        Rewriting every alias to a canonical id would read more tidily and would destroy
        the only evidence that two nodes were ever separate — which is the same argument
        SAME_AS_LINK_TYPE already makes for the edge being a link rather than a merge.
        """
        docs, triples = DocumentRepository(db_session), TripleRepository(db_session)
        john_a, john_b = _entity(docs, "John"), _entity(docs, "John McCardle")
        nyc, acme = _entity(docs, "NYC"), _entity(docs, "Acme")
        db_session.flush()
        lives_in = triples.create_predicate(name="lives_in")
        works_at = triples.create_predicate(name="works_at")
        db_session.flush()
        triples.create_triple(subject_id=john_a.id, predicate_id=lives_in.id, object_id=nyc.id)
        triples.create_triple(subject_id=john_b.id, predicate_id=works_at.id, object_id=acme.id)
        docs.create_link(john_a.id, john_b.id, link_type=SAME_AS_LINK_TYPE)
        db_session.flush()

        plain = triples_to_turtle(db_session, entity_id=john_a.id)
        assert plain.triple_count == 1
        assert plain.same_as_count == 0

        joined = triples_to_turtle(db_session, entity_id=john_a.id, coreferent=True)
        assert joined.triple_count == 2
        assert joined.same_as_count == 1
        assert set(joined.entity_ids) == {john_a.id, john_b.id}
        graph = _parse(joined.turtle)
        assert (
            rdflib.URIRef(document_iri(john_a.id)),
            rdflib.OWL.sameAs,
            rdflib.URIRef(document_iri(john_b.id)),
        ) in graph
        # Both aliases still exist as distinct subjects; nothing was rewritten.
        assert (rdflib.URIRef(document_iri(john_b.id)), None, None) in graph

    def test_labels_can_be_turned_off(self, db_session):
        docs, triples = DocumentRepository(db_session), TripleRepository(db_session)
        a, b = _entity(docs, "A"), _entity(docs, "B")
        db_session.flush()
        rel = triples.create_predicate(name="rel")
        db_session.flush()
        triples.create_triple(subject_id=a.id, predicate_id=rel.id, object_id=b.id)
        db_session.flush()

        graph = _parse(triples_to_turtle(db_session, entity_id=a.id, include_labels=False).turtle)
        assert (None, rdflib.RDFS.label, None) not in graph

    def test_the_base_iri_is_the_callers_to_choose(self, db_session):
        docs, triples = DocumentRepository(db_session), TripleRepository(db_session)
        a, b = _entity(docs, "A"), _entity(docs, "B")
        db_session.flush()
        rel = triples.create_predicate(name="rel")
        db_session.flush()
        triples.create_triple(subject_id=a.id, predicate_id=rel.id, object_id=b.id)
        db_session.flush()

        export = triples_to_turtle(db_session, entity_id=a.id, base_iri="https://kb.example/")
        assert "https://kb.example/document:" in export.turtle
        assert "urn:jmfts:" not in export.turtle

    def test_the_output_is_parseable_turtle(self, db_session):
        """The claim the whole step rests on: what comes out is a document, not a report."""
        docs, triples = DocumentRepository(db_session), TripleRepository(db_session)
        acme, john = _entity(docs, "Acme"), _entity(docs, "John")
        db_session.flush()
        works_at = triples.create_predicate(name="works_at")
        founded = triples.create_predicate(name="founded_in")
        db_session.flush()
        triples.create_triple(subject_id=john.id, predicate_id=works_at.id, object_id=acme.id)
        triples.create_triple(
            subject_id=acme.id,
            predicate_id=founded.id,
            object_literal="1999",
            object_datatype=XSD_INTEGER,
        )
        db_session.flush()

        export = triples_to_turtle(db_session, entity_id=acme.id, direction="both")
        graph = _parse(export.turtle)
        assert len(graph) == export.triple_count + 2  # two facts, two labels


# ---------------------------------------------------------------------------
# One row→RDF mapping, for the exporter and for the validator
# ---------------------------------------------------------------------------


class TestOneMapping:
    """``SPRINT_0_5_0.md`` Block A step 1's leftover, closed.

    ``rdf/shacl.triple_terms`` is the single mapping from a stored row to ``(subject,
    predicate, object)`` RDF terms, and ``triples_to_turtle`` calls it rather than repeating
    it. The reason is not tidiness: a validator that read a row differently from the exporter
    would report violations against a graph nobody can export, and the two readings would
    diverge exactly where the mapping is subtle — a predicate with no ``iri``, a NULL datatype,
    a name that has to be percent-encoded.
    """

    def test_the_exporter_and_the_validator_mint_identical_terms(self, db_session):
        from jmfts_core.rdf.shacl import triple_terms

        docs, triples = DocumentRepository(db_session), TripleRepository(db_session)
        acme, john = _entity(docs, "Acme"), _entity(docs, "John")
        db_session.flush()
        # One of each subtlety: a vocabulary predicate that keeps its own IRI, a local one
        # with no `iri` whose NAME carries a space (so the IRI is percent-encoded), a NULL
        # datatype (RDF's plain literal) and a typed one.
        knows = triples.create_predicate(name="knows", iri="http://xmlns.com/foaf/0.1/knows")
        trades_as = triples.create_predicate(name="trades as")
        founded = triples.create_predicate(name="founded_in")
        db_session.flush()
        rows = [
            triples.create_triple(subject_id=john.id, predicate_id=knows.id, object_id=acme.id),
            triples.create_triple(
                subject_id=acme.id, predicate_id=trades_as.id, object_literal="Acme Ltd"
            ),
            triples.create_triple(
                subject_id=acme.id,
                predicate_id=founded.id,
                object_literal="1999",
                object_datatype=XSD_INTEGER,
            ),
        ]
        db_session.flush()

        export = triples_to_turtle(db_session, include_labels=False)
        assert export.triple_count == len(rows)
        exported = set(_parse(export.turtle))
        # The validator's reading of the same rows, term for term.
        validated = {triple_terms(rdflib, row, DEFAULT_BASE_IRI) for row in rows}
        assert exported == validated

    def test_both_paths_refuse_the_same_unwritable_row(self, db_session):
        """The refusals are one implementation too. A CURIE in ``object_datatype`` resolves
        against the READER's prefixes, so writing it out would assert something the store
        never said — and a validator that quietly accepted it would validate a graph the
        exporter refuses."""
        from jmfts_core.rdf.shacl import triple_terms

        docs, triples = DocumentRepository(db_session), TripleRepository(db_session)
        acme = _entity(docs, "Acme")
        db_session.flush()
        revenue = triples.create_predicate(name="revenue")
        db_session.flush()
        bad = triples.create_triple(
            subject_id=acme.id,
            predicate_id=revenue.id,
            object_literal="128000",
            object_datatype="xsd:integer",
        )
        db_session.flush()

        with pytest.raises(TurtleSerializationError):
            triples_to_turtle(db_session, entity_id=acme.id)
        with pytest.raises(TurtleSerializationError) as caught:
            triple_terms(rdflib, bad, DEFAULT_BASE_IRI)
        assert str(bad.id) in str(caught.value)
