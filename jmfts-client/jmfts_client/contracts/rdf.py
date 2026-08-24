"""RDF contracts — Turtle out, Turtle in, and the shapes a scope of documents is held to.

``docs/SPRINT_0_3_0.md`` Part 5. Everything here is transport-neutral by the rule of the
house: no ``jmfts_core``, no web framework.

``TurtleDocument`` is the one name in this file that is not a Pydantic model, and it exists
for the same reason ``UploadedFile`` does — see ``contracts/upload.py``. A service method
may not import FastAPI, so it cannot write ``Annotated[str, Body(media_type="text/turtle")]``
itself; the adapter learns the marker instead. It is the second entry in a list of exactly
two, and a third should make somebody ask whether ``@expose`` wants a way to declare a
media type rather than the adapter growing another special case.
"""

from datetime import datetime
from typing import Any, NewType, Optional

from pydantic import BaseModel, Field

#: A request body that arrives as ``text/turtle`` rather than as JSON.
#:
#: ``NewType`` rather than a class: at runtime this IS a ``str`` and the service receives a
#: plain one, so a subclass would be a promise the adapter does not keep. ``rest/wiring.py``
#: matches on the annotation by identity and republishes it to FastAPI as
#: ``Annotated[str, Body(media_type="text/turtle")]``; ``scripts/generate_client`` reads the
#: media type back off the built route and emits a client that posts the bytes verbatim
#: instead of wrapping them in JSON.
#:
#: Why not a JSON body with a ``turtle`` field: a vocabulary is a FILE. ``curl
#: --data-binary @vocab.ttl -H 'Content-Type: text/turtle'`` is the whole upload, with no
#: escaping step between the file on disk and the bytes stored in
#: ``ontologies.source_turtle`` — and those bytes are the record (see
#: ``models/ontology.py``), so any transformation on the way in is a transformation of the
#: record.
TurtleDocument = NewType("TurtleDocument", str)


class TurtleExportResponse(BaseModel):
    """One Turtle document, plus what it was an export of.

    The counts travel beside the text because the text cannot carry them. An export is
    bounded by ``limit``, and a reader has no way to tell a graph that ended from a page
    that ran out; ``triple_count`` against the limit that was asked for is that difference.
    """

    turtle: str = Field(description="The serialised graph, in Turtle")
    triple_count: int = Field(
        description=(
            "Facts written. Excludes the owl:sameAs statements and the rdfs:labels, which "
            "are the export's commentary on the facts rather than facts."
        )
    )
    entity_ids: list[int] = Field(
        default_factory=list,
        description=(
            "Every entity whose facts were asked for, after coreference expansion and "
            "after subtree access control; the anchor first. Empty for an unfiltered export."
        ),
    )
    same_as_count: int = Field(
        default=0,
        description="owl:sameAs statements written. Zero unless coreferent was requested.",
    )


class ShapeProperty(BaseModel):
    """One ``sh:property`` constraint, in the subset 5.1 admits.

    Every field below ``path`` is Optional because SHACL says every one of them is: a
    property shape may declare a path and nothing else. A field that is null here was
    ABSENT from the Turtle, and that is a different statement from a constraint with a
    permissive value — a null ``min_count`` is "unconstrained", ``0`` is "explicitly
    optional".
    """

    path: str = Field(description="sh:path — the predicate IRI this constrains")
    min_count: Optional[int] = Field(default=None, description="sh:minCount")
    max_count: Optional[int] = Field(default=None, description="sh:maxCount")
    datatype: Optional[str] = Field(default=None, description="sh:datatype, an xsd: IRI")
    class_iri: Optional[str] = Field(
        default=None, description="sh:class — the class a resource-valued object must be"
    )
    allowed_values: Optional[list[str]] = Field(
        default=None,
        description=(
            "sh:in, as a list of lexical forms. A closed set — which is what a spreadsheet "
            "column with few distinct values becomes (SPRINT_0_3_0.md Part 3 step 1)."
        ),
    )
    name: Optional[str] = Field(default=None, description="sh:name, if the shape gave one")
    description: Optional[str] = Field(default=None, description="sh:description, if given")


class ShapeSummary(BaseModel):
    """One ``sh:NodeShape``, reduced to the subset this appliance reads."""

    iri: str
    target_class: Optional[str] = Field(
        default=None,
        description=(
            "sh:targetClass. Null means the shape declares no target, so nothing selects it "
            "automatically — it can still be bound to a scope explicitly."
        ),
    )
    properties: list[ShapeProperty] = Field(default_factory=list)


class OntologyResponse(BaseModel):
    """A stored vocabulary. ``source_turtle`` is the record; ``shapes`` is a parse of it."""

    name: str
    base_iri: str
    source_turtle: str
    prefixes: dict[str, str] = Field(default_factory=dict)
    shapes: list[ShapeSummary] = Field(default_factory=list)
    description: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class PredicateImportConflict(BaseModel):
    """A vocabulary term whose local name is already taken by a different predicate.

    Reported, never resolved. ``predicates.name`` is UNIQUE and ``predicates.iri`` is
    UNIQUE, so a term named ``knows`` arriving into a store that already has a local
    ``knows`` is a genuine ambiguity: binding the existing row to the incoming IRI would
    retroactively claim every fact recorded under it was about the vocabulary's property,
    and minting a second row would fail on the name. The import stores the ontology and
    leaves the predicate alone.
    """

    iri: str = Field(description="The vocabulary term that could not be registered")
    name: str = Field(description="The local name it would have taken")
    existing_predicate_id: int
    existing_iri: Optional[str] = Field(
        default=None, description="What the existing row is already called, if anything"
    )


class OntologyImportResponse(BaseModel):
    """What ``POST /ontologies`` did: the vocabulary, and what it changed in the registry."""

    ontology: OntologyResponse
    replaced: bool = Field(
        default=False,
        description=(
            "True when a vocabulary of this name already existed and these bytes replaced "
            "it. The name is the primary key (models/ontology.py), so a re-upload is a "
            "replacement rather than a second copy."
        ),
    )
    predicates_created: list[int] = Field(
        default_factory=list, description="Predicate rows minted for terms new to this store"
    )
    predicates_reused: list[int] = Field(
        default_factory=list, description="Predicate rows already carrying one of these IRIs"
    )
    predicate_conflicts: list[PredicateImportConflict] = Field(
        default_factory=list,
        description="Terms left unregistered because their local name is taken. See the type.",
    )
    unsupported_terms: list[str] = Field(
        default_factory=list,
        description=(
            "SHACL and OWL terms present in the Turtle that 5.1's subset does not read. The "
            "source is stored whole, so nothing is lost — but a constraint listed here is "
            "NOT enforced, and a caller that assumed it was needs to know."
        ),
    )


class ShapeBindingCreate(BaseModel):
    """Bind a shape to a scope of documents.

    Exactly one of the three scope fields is set, and which one is set decides
    ``scope_type``. They are three named fields rather than one free JSONB object because
    the set is CLOSED (``models/ontology.py``): each names a different query the resolver
    runs, and a scope with no query behind it would silently match nothing.
    """

    shape_iri: str
    usetype_pattern: Optional[str] = Field(
        default=None, description="scope_type 'usetype' — matched against documents.usetype"
    )
    parent_id: Optional[int] = Field(
        default=None, description="scope_type 'subtree' — the node and everything below it"
    )
    document_ids: Optional[list[int]] = Field(
        default=None, description="scope_type 'documents' — exactly these nodes"
    )
    description: Optional[str] = None


class ShapeBindingResponse(BaseModel):
    """A stored binding."""

    id: int
    ontology_name: str
    shape_iri: str
    scope_type: str
    scope: dict[str, Any] = Field(default_factory=dict)
    description: Optional[str] = None
    created_at: Optional[datetime] = None
