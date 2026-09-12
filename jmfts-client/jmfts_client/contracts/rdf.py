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
from typing import Any, Literal, NewType, Optional

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


class ShapeValidationRequest(BaseModel):
    """Ask for one binding to be run against the data it is bound to.

    ``docs/SPRINT_0_5_0.md`` Block A step 4. The binding is the whole request: it already
    names the ontology, the shape and the scope, and a request that re-stated any of them
    would be free to disagree with the row it names.
    """

    binding_id: int = Field(
        description=(
            "The binding to run. It must belong to the ontology in the path — a binding "
            "under another vocabulary is a 404 here rather than a run against the wrong "
            "shape."
        )
    )
    provenance: Literal["any", "asserted", "derived"] = Field(
        default="any",
        description=(
            "Which layer of the triple store the data graph is built from. 'any' is the "
            "live graph as it stands, which is what a validation run normally asks about; "
            "'asserted' is `derived_by IS NULL`, which is what a rule pass reads. "
            "Invalidated triples are never included at any layer."
        ),
    )


class ShapeValidationRunResponse(BaseModel):
    """What ``POST /ontologies/{name}/validate`` enqueued — a task and the node it fills in.

    The run has NOT happened when this returns. ``report_document_id`` is a document that
    exists, is in flight, and carries the request; poll it (``GET /documents/{id}``) and read
    ``structured_content['validation']`` for the report once its task completes. A run that
    cannot be completed — this install has no ``pyshacl``, the binding was deleted — leaves
    that node ``settled='failed'`` with the reason in its attempt log, which is more than a
    refused request would have left behind.
    """

    task_id: int = Field(description="The queued `validate:shape` task")
    report_document_id: int = Field(
        description="The node the report will be written to. In flight until the task runs."
    )
    binding_id: int
    ontology_name: str
    shape_iri: str
    scope_type: str
    scope_document_count: int = Field(
        description=(
            "Documents the binding's scope resolved to, AFTER the caller's access filter. "
            "This is the set the run is pinned to: the worker holds no principal, so the "
            "ids are recorded on the task row rather than re-resolved when it runs."
        )
    )
    provenance: str
    governed: bool = Field(
        description=(
            "Whether the report node carries access grants. It carries exactly the grants "
            "the scope's documents are governed by, so a report over restricted data is "
            "restricted identically; false means the scope is under no access-control root "
            "and the report is readable by every token, like the documents it reports on."
        )
    )
    requested_at: datetime
    status: str = Field(description="The queued task's status — 'pending' when it is created")


# ── the write side: a bound shape's sh:rule set ─────────────────────────────
#
# Here and not in a module of their own. ``SPRINT_0_5_0.md`` Block B finding 7: step 4 put
# ``ShapeValidationRequest``/``ShapeValidationRunResponse`` in this file and step 6 created a
# second module for these two, which are the same pair of shapes for the sibling operation.
# Two modules for one vocabulary is drift, so ``contracts/ontology.py`` was deleted and these
# moved here unchanged.


class RuleDerivationRequest(BaseModel):
    """Ask for one binding's ``sh:rule`` set to be run against the data it is bound to.

    The binding is the whole request, as it is for validation: it already names the ontology,
    the shape and the scope, and a request that re-stated any of them would be free to
    disagree with the row it names.

    **There is no ``provenance`` field and its absence is the design.**
    ``ShapeValidationRequest`` has one because reading the derived layer is a reasonable thing
    to ask for; a rule pass reads the ASSERTED layer and only that. ``SPRINT_0_5_0.md`` Block
    B: rules do not chain in this sprint, because a rule whose input is another rule's output
    is a fixpoint over a store that also accepts writes. A field here would be the way to ask
    for exactly that, so there is not one.
    """

    binding_id: int = Field(
        description=(
            "The binding to run. It must belong to the ontology in the path — a binding "
            "under another vocabulary is a 404 here rather than a run against the wrong "
            "shape. Its shape must declare at least one sh:rule; a shape that declares none "
            "is refused, because 'derived nothing' and 'rules ran and matched nothing' are "
            "opposite facts that read the same."
        )
    )


class RuleDerivationRunResponse(BaseModel):
    """What ``POST /ontologies/{name}/derive`` enqueued — a task, a report node, and a rule id.

    The run has NOT happened when this returns. ``report_document_id`` is a document that
    exists, is in flight, and carries the request; poll it (``GET /documents/{id}``) and read
    ``structured_content['derivation']`` once its task completes. A run that cannot be
    completed — no ``pyshacl`` on the worker, a rule that produced a triple this store cannot
    hold — leaves that node ``settled='failed'`` with the reason in its attempt log, and
    leaves the triple store exactly as it was.
    """

    task_id: int = Field(description="The queued `derive:rule` task")
    report_document_id: int = Field(
        description="The node the report will be written to. In flight until the task runs."
    )
    binding_id: int
    ontology_name: str
    shape_iri: str
    scope_type: str
    rule: str = Field(
        description=(
            "The identity this run's rows will carry in `triples.derived_by`, and the "
            "complete description of what this rule produced: `WHERE derived_by = <rule>` is "
            "the set a re-run deletes and rebuilds. It is a digest of the four fields that "
            "make a binding unique — vocabulary, shape, scope kind, scope — rather than the "
            "binding's row id, so a binding deleted and recreated identically re-derives the "
            "same set instead of stranding it. Returned here because the digest is otherwise "
            "only readable off the report node."
        )
    )
    scope_document_count: int = Field(
        description=(
            "Documents the binding's scope resolved to, AFTER the caller's access filter. "
            "This is the set the run is pinned to, and it is also the set a derived triple's "
            "endpoints must both be inside: the worker holds no principal, so the ids are "
            "recorded on the task row rather than re-resolved when it runs."
        )
    )
    provenance: str = Field(
        description=(
            "Always 'asserted'. Reported rather than accepted, so a caller reading a "
            "response can see which layer the conclusion was drawn from. See the request."
        )
    )
    governed: bool = Field(
        description=(
            "Whether the report node carries access grants. It carries exactly the grants "
            "the scope's documents are governed by; false means the scope is under no "
            "access-control root and the report is readable by every token, like the "
            "documents it derives from. The derived TRIPLES are not governed by this: a "
            "triple's visibility follows its endpoints, which are documents in this same "
            "scope."
        )
    )
    requested_at: datetime
    status: str = Field(description="The queued task's status — 'pending' when it is created")
