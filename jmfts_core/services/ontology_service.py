"""OntologyService — Turtle in, and the shapes a scope of documents is held to.

``docs/SPRINT_0_3_0.md`` 5.2 and 4.5. ``POST /ontologies`` takes ``text/turtle``, ``rdflib``
parses it, predicates land in the registry with their IRIs, shapes land in ``ontologies``,
and a binding is created against a scope.

Three things this deliberately does not do.

**It does not validate anything.** 5.1: no reasoner, and validation waits for the shapes to
have data to run against. Storing a shape and binding it is the whole of 0.3.0. That is
also what makes uploading safe — until a shape is bound it constrains nothing, and even
bound it changes no existing row.

**It does not rebind an existing predicate to an incoming IRI.** ``predicates.name`` is
UNIQUE and so is ``predicates.iri``. A vocabulary term named ``knows`` arriving into a
store that already has a local ``knows`` is a genuine ambiguity: rebinding would
retroactively claim every fact recorded under the local predicate was about the
vocabulary's property, and minting a second row would fail on the name. So it is reported
as a ``PredicateImportConflict`` and the row is left alone —
``TripleRepository.get_or_create_predicate`` already says that this is where that decision
lives.

**It does not guess a base IRI.** ``ontologies.base_iri`` is NOT NULL and is a parameter,
not a reading of ``@base`` or of the first prefix. An ontology may declare neither, and a
locally-minted IRI resting on a guess is exactly the failure Part 12 item 5 is still open
about.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from jmfts_client.contracts.rdf import (
    OntologyImportResponse,
    OntologyResponse,
    PredicateImportConflict,
    RuleDerivationRequest,
    RuleDerivationRunResponse,
    ShapeBindingCreate,
    ShapeBindingResponse,
    ShapeProperty,
    ShapeSummary,
    ShapeValidationRequest,
    ShapeValidationRunResponse,
    TurtleDocument,
)
from jmfts_core.access import AccessKey, access_key, acr_ids, require_write
from jmfts_core.config import get_settings
from jmfts_core.derive_tasks import (
    DERIVATION_BLOCK,
    DERIVATION_PROVENANCE,
    DERIVATION_REPORT_USETYPE,
    PARAM_BASE_IRI as DERIVE_PARAM_BASE_IRI,
    PARAM_BINDING_ID as DERIVE_PARAM_BINDING_ID,
    PARAM_DOCUMENT_IDS as DERIVE_PARAM_DOCUMENT_IDS,
    PARAM_RULE as DERIVE_PARAM_RULE,
    REPORT_PARENT_REASON as DERIVATION_PARENT_REASON,
)
from jmfts_core.ingest_tasks import TASK_DERIVE_RULE, TASK_VALIDATE_SHAPE
from jmfts_core.models.document import SETTLED_IN_FLIGHT, Document
from jmfts_core.models.ontology import Ontology, ShapeBinding
from jmfts_core.models.principal import AccessGrant
from jmfts_core.models.task_queue import WRITE_SELF
from jmfts_core.rdf import RdfStackNotInstalled
from jmfts_core.rdf.names import DEFAULT_BASE_IRI
from jmfts_core.registry import expose, register_service
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.ontology import OntologyRepository
from jmfts_core.repositories.task_queue import TaskQueueRepository
from jmfts_core.repositories.triple import TripleRepository
from jmfts_core.shacl_rules import rule_identity
from jmfts_core.validate_tasks import (
    PARAM_BASE_IRI,
    PARAM_BINDING_ID,
    PARAM_DOCUMENT_IDS,
    PARAM_PROVENANCE,
    REPORT_PARENT_REASON,
    VALIDATION_BLOCK,
    VALIDATION_REPORT_USETYPE,
)


class ShapeNotDeclaredError(LookupError):
    """A binding named a shape the ontology does not declare (→ 404).

    Checked at BIND time and not at use time, which is what ``models/ontology.py`` says
    ``shape_bindings.shape_iri`` being TEXT rather than a foreign key buys: the answer can
    be reported to the caller who typed the IRI, instead of surfacing later as a rule that
    matches nothing.
    """


class BindingConflictError(Exception):
    """This shape is already bound to this exact scope (→ 409).

    ``uq_shape_binding`` says so too; raising first means the caller gets the name of its
    own mistake rather than an IntegrityError that has poisoned the transaction.
    """


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ScopeEmptyError(ValueError):
    """The binding's scope resolves to no document this caller can read (→ 422).

    Refused rather than run, and the reason is what a run would produce: an empty data graph
    conforms to every shape, so a report over an empty scope is indistinguishable from a
    report over data that passed. A binding whose usetype pattern has a typo would come back
    clean forever. The two ways to reach this state are worth telling apart in the message —
    the scope names nothing, or it names nothing THIS principal may read.
    """


class ScopeAccessNotUniformError(ValueError):
    """The scope's documents are not governed identically, so one report cannot hold them.

    ``SPRINT_0_5_0.md`` 3.4: a derived node inherits the access of its SOURCES. A report over
    documents with two different access keys has no correct access of its own — the union
    over-shares (a principal who may read one source reads a report about all of them) and
    the intersection cannot be expressed, because the empty access key already means
    UNGOVERNED, which is public, rather than "nobody" (``jmfts_core/access.py``).

    So it is refused, and the caller narrows the binding or aligns the grants. In the default
    deployment — no grants anywhere — every key is empty, every scope is uniform, and this is
    unreachable.
    """


def _pin_scope(db: Session, binding: ShapeBinding, *, action: str) -> tuple[list[int], AccessKey]:
    """The binding's scope, resolved and access-checked once, for the run that is about to be
    enqueued. Returns ``(document_ids, access key)``.

    **ONE function for both runs, and that is deliberate rather than tidy.** ``validate:shape``
    and ``derive:rule`` make the same four decisions in the request and for the same reasons,
    and three of the four are access decisions. Two copies of an access check are two things
    that can come to disagree, and the one that would be strengthened is whichever the next
    reader happened to open.

    *The scope, resolved under the CALLER'S access filter.* A worker holds no principal and
    therefore bypasses every check (``jmfts_core/access.py``), so a scope re-resolved there
    would be every document in the appliance. The ids are resolved once, here, and pinned onto
    the task row — which also fixes what a re-run means: the same task row is the same set of
    documents.

    *Write access to every one of them.* Open question 6.4's recorded default: a run requires
    WRITE access to the binding's scope. For ``derive:rule`` that needs no further argument —
    it writes facts about those documents. For ``validate:shape`` the argument is the node:
    a run MINTS a retrievable, full-text searchable digest of every document in the scope, and
    a read-only principal who could do that would have a way to manufacture one for data they
    may only read (``SPRINT_0_3_0.md`` 13.9 by another route).

    *That the scope is governed uniformly.* See :class:`ScopeAccessNotUniformError`.

    *That the scope is not empty.* See :class:`ScopeEmptyError`.

    *That the scope is not larger than the operator's bound.* Open question 6.3:
    ``Settings.shacl_max_scope_documents``, 512 documents by default, refused as
    ``rdf.shacl.ScopeTooLargeError``. It is a ``ValueError`` and both endpoints map
    ``ValueError`` to 422, so it needs no entry of its own in their error maps — and it is not
    imported at module scope here for the reason the import below gives.

    Raises:
        ScopeEmptyError, ScopeAccessNotUniformError, ScopeTooLargeError, AccessDeniedError.
    """
    # At the point of use, as the validation lane wrote it: `rdf/shacl.py` pulls in
    # `rdf/serialize.py`, and this module is on the import path of an appliance that may hold
    # triples without being able to speak Turtle at all.
    from jmfts_core.rdf.shacl import resolve_scope_document_ids

    document_ids = resolve_scope_document_ids(
        db,
        scope_type=binding.scope_type,
        scope=binding.scope or {},
        # Open question 6.3's bound, taken 2026-09-10: `Settings.shacl_max_scope_documents`,
        # 512 documents by default and `None` for unbounded. THIS IS THE ONLY PLACE IT IS
        # READ, which is what makes one edit bind both runs — the handlers pass None on
        # purpose, because the request has already applied the operator's bound and applying
        # it a second time in the worker would refuse a run the request accepted.
        max_scope_documents=get_settings().shacl_max_scope_documents,
    )
    if not document_ids:
        raise ScopeEmptyError(
            f"binding {binding.id} names a {binding.scope_type} scope that resolves to no "
            f"document you can read, so there is nothing to {action}. An empty graph conforms "
            "to every shape and gives every rule nothing to fire on, and a report saying so "
            "would be indistinguishable from one over data that passed"
        )

    key: AccessKey = ()
    # One query, and it short-circuits the whole block in the default deployment: with no
    # grants anywhere, nothing is governed, every access key is empty and every document is
    # writable — which is exactly what `can_write` and `access_key` each conclude on their
    # own, per document. Asking once is the same answer for less.
    if acr_ids(db):
        keys = set()
        for doc_id in document_ids:
            document = db.get(Document, doc_id)
            require_write(db, document, action=f"{action} over")
            keys.add(access_key(db, document))
        if len(keys) > 1:
            raise ScopeAccessNotUniformError(
                f"binding {binding.id} names {len(document_ids)} documents governed "
                f"{len(keys)} different ways, and one report node cannot carry more than one "
                "access. Narrow the binding's scope, or align the grants over it"
            )
        key = keys.pop()
    return document_ids, key


def _mirror_grants(db: Session, document_id: int, key: AccessKey) -> None:
    """Give a derived node exactly the access of the documents it was derived from.

    The grants ARE the access control: ``access.py`` defines an access-control root as a
    document with at least one grant, so these rows are what make a report governed exactly as
    its sources are. An empty key writes none, and the report is public because the documents
    it is about are — the same rule, not an exception to it.
    ``jmfts_core/derived_roots.py`` mints a root's grants the same way.
    """
    for principal_id, level in key:
        db.add(AccessGrant(document_id=document_id, principal_id=principal_id, level=level))


def _shape_summary(shape: dict) -> ShapeSummary:
    """One stored shape digest as its contract. The digest is the parser's ``to_dict``."""
    return ShapeSummary(
        iri=shape["iri"],
        target_class=shape.get("target_class"),
        properties=[ShapeProperty(**prop) for prop in shape.get("properties", [])],
    )


def _ontology_response(row: Ontology) -> OntologyResponse:
    shapes = row.shapes or {}
    return OntologyResponse(
        name=row.name,
        base_iri=row.base_iri,
        source_turtle=row.source_turtle,
        prefixes=row.prefixes or {},
        shapes=[_shape_summary(shapes[iri]) for iri in sorted(shapes)],
        description=row.description,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _binding_response(row: ShapeBinding) -> ShapeBindingResponse:
    return ShapeBindingResponse(
        id=row.id,
        ontology_name=row.ontology_name,
        shape_iri=row.shape_iri,
        scope_type=row.scope_type,
        scope=row.scope or {},
        description=row.description,
        created_at=row.created_at,
    )


def _scope_of(request: ShapeBindingCreate) -> tuple[str, dict]:
    """Which of the three scopes was asked for, refusing zero and refusing two.

    A binding with no scope would match nothing and a binding with two would match by
    whichever query ran first. Both are silent, so both are a 422 here rather than a
    surprise the first time a rule iterates bindings.
    """
    chosen = [
        ("usetype", {"pattern": request.usetype_pattern}, request.usetype_pattern is not None),
        ("subtree", {"parent_id": request.parent_id}, request.parent_id is not None),
        ("documents", {"document_ids": request.document_ids}, request.document_ids is not None),
    ]
    given = [(kind, scope) for kind, scope, present in chosen if present]
    if len(given) != 1:
        raise ValueError(
            "a binding names exactly one scope — usetype_pattern, parent_id or "
            f"document_ids; got {len(given)}"
        )
    return given[0]


@register_service
class OntologyService:
    """Ontologies and shape bindings — Turtle in, and which documents a shape is about."""

    def __init__(self, session: Session):
        self.session = session

    @expose(
        "POST",
        "/ontologies",
        response_model=OntologyImportResponse,
        status_code=201,
        errors={RdfStackNotInstalled: 501, ValueError: 422},
        tags=["ontologies"],
        summary="Import a vocabulary from a text/turtle document",
    )
    def import_ontology(
        self,
        turtle: TurtleDocument,
        *,
        name: str,
        base_iri: str,
        description: Optional[str] = None,
    ) -> OntologyImportResponse:
        """Import a vocabulary. The body is the Turtle document itself, ``text/turtle``.

        ``name`` is the key it is stored under and a second upload under the same name
        REPLACES that vocabulary — the bytes are the record, so replacing them is how a
        vocabulary is corrected. ``base_iri`` is what terms in the document are relative to
        and is required: an ontology may declare neither ``@base`` nor a matching prefix,
        and guessing one would make every locally-minted IRI rest on a guess.

        What lands where. Every ``sh:path`` in a shape, and everything typed as
        ``rdf:Property`` / ``owl:ObjectProperty`` / ``owl:DatatypeProperty``, becomes a row
        in ``predicates`` carrying its IRI. The shapes become the ontology's parsed digest.
        Nothing is bound to anything: a shape constrains no document until
        ``POST /ontologies/{name}/bindings`` says which.

        **The SHACL subset is a ceiling** (5.1): ``sh:NodeShape``, ``sh:targetClass``,
        ``sh:property``, ``sh:path``, ``sh:minCount``, ``sh:maxCount``, ``sh:datatype``,
        ``sh:in``, ``sh:class``. Anything else in the document is stored verbatim and
        reported in ``unsupported_terms`` — it is NOT read and NOT enforced. A validator
        that is right about eight constraints and silent about the ninth is worse than
        none, because the silence looks like a pass.

        Answers 501 where this install has no ``rdflib``: holding triples without being able
        to read a vocabulary is a supported deployment (``jmfts_core/rdf/__init__.py``).
        """
        from jmfts_core.rdf.parse import parse_ontology

        parsed = parse_ontology(turtle)

        ontologies = OntologyRepository(self.session)
        row, replaced = ontologies.upsert(
            name=name,
            base_iri=base_iri,
            source_turtle=turtle,
            prefixes=parsed.prefixes,
            shapes={shape.iri: shape.to_dict() for shape in parsed.shapes},
            description=description,
        )

        triples = TripleRepository(self.session)
        created: list[int] = []
        reused: list[int] = []
        conflicts: list[PredicateImportConflict] = []
        for iri in sorted(parsed.predicate_terms):
            local = parsed.predicate_terms[iri]
            by_iri = triples.get_predicate_by_iri(iri)
            if by_iri is not None:
                reused.append(by_iri.id)
                continue
            by_name = triples.get_predicate_by_name(local)
            if by_name is not None:
                conflicts.append(
                    PredicateImportConflict(
                        iri=iri,
                        name=local,
                        existing_predicate_id=by_name.id,
                        existing_iri=by_name.iri,
                    )
                )
                continue
            predicate = triples.create_predicate(
                name=local,
                # The vocabulary's own name, so `GET /triples/predicates?namespace=` answers
                # "which predicates came from this ontology". `namespace` is NOT rdfs:domain
                # — that collision is exactly what the 4.4 rename was for.
                namespace=name,
                iri=iri,
            )
            created.append(predicate.id)

        response = OntologyImportResponse(
            ontology=_ontology_response(row),
            replaced=replaced,
            predicates_created=created,
            predicates_reused=reused,
            predicate_conflicts=conflicts,
            unsupported_terms=parsed.unsupported_terms,
        )
        # Commit before responding: get_db's teardown commit runs after the response is
        # sent, so a client acting on the returned ids would race it.
        self.session.commit()
        return response

    @expose(
        "GET",
        "/ontologies",
        response_model=list[OntologyResponse],
        tags=["ontologies"],
        summary="List stored vocabularies",
    )
    def list_ontologies(self) -> list[OntologyResponse]:
        """List stored vocabularies, with their source Turtle and parsed shapes."""
        return [_ontology_response(row) for row in OntologyRepository(self.session).list_all()]

    @expose(
        "GET",
        "/ontologies/{name}",
        response_model=OntologyResponse,
        errors={LookupError: 404},
        tags=["ontologies"],
        summary="Get one vocabulary by name",
    )
    def get_ontology(self, name: str) -> OntologyResponse:
        """Get one vocabulary by name."""
        row = OntologyRepository(self.session).get(name)
        if row is None:
            raise LookupError(f"Ontology {name!r} not found")
        return _ontology_response(row)

    @expose(
        "DELETE",
        "/ontologies/{name}",
        errors={LookupError: 404},
        tags=["ontologies"],
        summary="Delete a vocabulary and its bindings",
    )
    def delete_ontology(self, name: str) -> dict:
        """Delete a vocabulary and every binding that names it.

        The predicates it minted are NOT deleted. They are rows facts point at, and
        ``triples.predicate_id`` is ``ON DELETE CASCADE`` — dropping a predicate here would
        delete the facts recorded with it, which is not what removing a vocabulary means.
        """
        if not OntologyRepository(self.session).delete(name):
            raise LookupError(f"Ontology {name!r} not found")
        self.session.commit()
        return {"deleted": name}

    @expose(
        "POST",
        "/ontologies/{name}/bindings",
        response_model=ShapeBindingResponse,
        status_code=201,
        errors={
            ShapeNotDeclaredError: 404,
            LookupError: 404,
            BindingConflictError: 409,
            ValueError: 422,
        },
        tags=["ontologies"],
        summary="Bind one of this vocabulary's shapes to a scope of documents",
    )
    def create_binding(self, name: str, request: ShapeBindingCreate) -> ShapeBindingResponse:
        """Bind one of this vocabulary's shapes to a scope of documents.

        A scope is a usetype pattern, a subtree ``parent_id``, or an explicit document set —
        exactly one of the three (4.5). This is what "giving documents specific scopes for
        fact extraction" means concretely, and it is what will constrain the predicates an
        extractor may use (8.2).

        Binding changes no document and validates nothing. It records which claim is about
        which documents; running the claim against them is the step after this sprint.
        """
        repo = OntologyRepository(self.session)
        ontology = repo.get(name)
        if ontology is None:
            raise LookupError(f"Ontology {name!r} not found")
        if request.shape_iri not in (ontology.shapes or {}):
            declared = ", ".join(sorted(ontology.shapes or {})) or "none"
            raise ShapeNotDeclaredError(
                f"Ontology {name!r} declares no shape {request.shape_iri!r}. It declares: "
                f"{declared}"
            )
        scope_type, scope = _scope_of(request)
        if repo.find_binding(
            ontology_name=name, shape_iri=request.shape_iri, scope_type=scope_type, scope=scope
        ):
            raise BindingConflictError(
                f"shape {request.shape_iri!r} is already bound to that {scope_type} scope"
            )
        binding = repo.create_binding(
            ontology_name=name,
            shape_iri=request.shape_iri,
            scope_type=scope_type,
            scope=scope,
            description=request.description,
        )
        response = _binding_response(binding)
        self.session.commit()
        return response

    @expose(
        "GET",
        "/shape-bindings",
        response_model=list[ShapeBindingResponse],
        tags=["ontologies"],
        summary="List shape bindings",
    )
    def list_bindings(self, *, ontology: Optional[str] = None) -> list[ShapeBindingResponse]:
        """List shape bindings, optionally for one vocabulary."""
        rows = OntologyRepository(self.session).list_bindings(ontology_name=ontology)
        return [_binding_response(row) for row in rows]

    @expose(
        "DELETE",
        "/shape-bindings/{binding_id}",
        errors={LookupError: 404},
        tags=["ontologies"],
        summary="Delete a shape binding",
    )
    def delete_binding(self, binding_id: int) -> dict:
        """Delete a shape binding. The vocabulary and its shapes are untouched."""
        if not OntologyRepository(self.session).delete_binding(binding_id):
            raise LookupError(f"Shape binding {binding_id} not found")
        self.session.commit()
        return {"deleted": binding_id}

    @expose(
        "POST",
        "/ontologies/{name}/validate",
        response_model=ShapeValidationRunResponse,
        status_code=202,
        errors={
            ShapeNotDeclaredError: 404,
            LookupError: 404,
            ScopeEmptyError: 422,
            ScopeAccessNotUniformError: 422,
            ValueError: 422,
        },
        tags=["ontologies"],
        summary="Enqueue a validation run for one of this vocabulary's bindings",
    )
    def validate_binding(
        self, name: str, request: ShapeValidationRequest
    ) -> ShapeValidationRunResponse:
        """Run a bound shape against the data it is bound to. ``SPRINT_0_5_0.md`` Block A step 4.

        **It enqueues and returns; it does not validate inline.** A run over a bound scope is
        the same shape of work as ``profile:sheet`` — bounded, restartable, worth retrying —
        so it is a ``validate:shape`` task (:mod:`jmfts_core.validate_tasks`) and this returns
        the task and the node the report will land on. 202, because the answer is not ready.

        **What is decided here rather than in the worker, and why each one has to be.**

        *The scope, resolved under the CALLER'S access filter.* A worker holds no principal
        and therefore bypasses every check (``jmfts_core/access.py``), so a scope re-resolved
        there would be every document in the appliance. The ids are resolved once, here, and
        pinned onto the task row. That also fixes what a re-run means: the same task row is
        the same set of documents.

        *Write access to every one of them.* Open question 6.4's recorded default is that
        running a derivation requires WRITE access to the binding's scope, and this takes it
        for validation too although Block A derives nothing. The reason is not the reading —
        it is that a run MINTS A NODE whose content is derived from every document in the
        scope and which is retrievable and full-text searchable. A read-only principal who
        could do that would have a way to manufacture a durable, searchable digest of data
        they may only read, which is ``SPRINT_0_3_0.md`` 13.9's hazard reached by yet another
        route. 6.4 also calls write "the narrowest defensible rule and the easiest to widen
        later"; if a read-only run is wanted, the thing to widen is this line, and the report
        node's access is not what changes.

        *That the scope is governed uniformly.* See :class:`ScopeAccessNotUniformError`.

        *That the scope is not empty.* See :class:`ScopeEmptyError`.

        **This does not check that ``pyshacl`` is installed, deliberately.** The API process
        is not necessarily the process that will run the task — a badged worker fleet is the
        normal deployment — so a 501 from here would refuse a run that a worker could have
        completed. An install with no validator fails the task PERMANENT with the extra named,
        and the node ends ``failed`` with that in its attempt log. ``GET /capabilities`` is
        where a caller asks the question in advance.
        """
        db = self.session
        binding = self._binding_for(name, request.binding_id)
        document_ids, key = _pin_scope(db, binding, action="run a validation")

        requested_at = _utc_now()
        node = DocumentRepository(db).create(
            title=f"SHACL validation — {binding.shape_iri}",
            # No content until the run writes the report. A node whose content said
            # "pending" would be a document that retrieves and says nothing.
            content=None,
            # Top-level, and REPORT_PARENT_REASON is why — the settling walk must stop here.
            parent_id=None,
            usetype=VALIDATION_REPORT_USETYPE,
            structured_content={
                VALIDATION_BLOCK: {
                    "binding_id": binding.id,
                    "ontology_name": name,
                    "shape_iri": binding.shape_iri,
                    "scope_type": binding.scope_type,
                    "scope": binding.scope or {},
                    "provenance": request.provenance,
                    "base_iri": DEFAULT_BASE_IRI,
                    "requested_at": requested_at.isoformat(),
                    "parent_reason": REPORT_PARENT_REASON,
                }
            },
            # No model runs for a report, here or in the task. It is retrievable and
            # full-text searchable; it is not a vector-search result, and giving it one
            # would put violation lists in the neighbourhood of the documents they are
            # about.
            auto_embed=False,
            embed_tokens=False,
            settled=SETTLED_IN_FLIGHT,
            produced_by=TASK_VALIDATE_SHAPE,
        )
        _mirror_grants(db, node.id, key)
        db.flush()

        task = TaskQueueRepository(db).enqueue(
            TASK_VALIDATE_SHAPE,
            node.id,
            WRITE_SELF,
            params={
                PARAM_BINDING_ID: binding.id,
                PARAM_DOCUMENT_IDS: document_ids,
                PARAM_PROVENANCE: request.provenance,
                # Recorded rather than defaulted at run time: a stored report has to read
                # back against the names it was validated under, and the default can move.
                PARAM_BASE_IRI: DEFAULT_BASE_IRI,
            },
        )
        response = ShapeValidationRunResponse(
            task_id=task.id,
            report_document_id=node.id,
            binding_id=binding.id,
            ontology_name=name,
            shape_iri=binding.shape_iri,
            scope_type=binding.scope_type,
            scope_document_count=len(document_ids),
            provenance=request.provenance,
            governed=bool(key),
            requested_at=requested_at,
            status=task.status,
        )
        # Commit before responding, for `import_ontology`'s reason: a client that polls the
        # returned document id would otherwise race `get_db`'s teardown commit.
        db.commit()
        return response

    def _binding_for(self, name: str, binding_id: int) -> ShapeBinding:
        """The binding this run is about, checked against the vocabulary in the path.

        Shared by the two runs for the same reason :func:`_pin_scope` is: three refusals, all
        of which decide what a run is allowed to be about.

        Raises:
            LookupError: no such vocabulary, or it has no such binding (404). A binding under
                ANOTHER vocabulary is a 404 and not a 422 — the path says which vocabulary
                this operation is about, so a caller who named the wrong one is asking about
                a binding that does not exist here.
            ShapeNotDeclaredError: the vocabulary was replaced by an upload that no longer
                declares the shape the binding names (404).
        """
        repo = OntologyRepository(self.session)
        ontology = repo.get(name)
        if ontology is None:
            raise LookupError(f"Ontology {name!r} not found")
        binding = repo.get_binding(binding_id)
        if binding is None or binding.ontology_name != name:
            raise LookupError(f"Ontology {name!r} has no shape binding {binding_id}")
        if binding.shape_iri not in (ontology.shapes or {}):
            declared = ", ".join(sorted(ontology.shapes or {})) or "none"
            raise ShapeNotDeclaredError(
                f"Ontology {name!r} no longer declares the shape {binding.shape_iri!r} that "
                f"binding {binding.id} names. It declares: {declared}"
            )
        return binding

    @expose(
        "POST",
        "/ontologies/{name}/derive",
        response_model=RuleDerivationRunResponse,
        status_code=202,
        errors={
            ShapeNotDeclaredError: 404,
            LookupError: 404,
            ScopeEmptyError: 422,
            ScopeAccessNotUniformError: 422,
            ValueError: 422,
        },
        tags=["ontologies"],
        summary="Enqueue a rule-derivation run for one of this vocabulary's bindings",
    )
    def derive_binding(
        self, name: str, request: RuleDerivationRequest
    ) -> RuleDerivationRunResponse:
        """Run a bound shape's ``sh:rule`` set and write what it concludes.
        ``SPRINT_0_5_0.md`` Block B steps 6, 7 and 8.

        **This is the first thing in this appliance that writes** ``triples.derived_by``.
        ``models/triple.py`` has carried the column since migration ``013`` with "nothing
        writes it yet" against it, precisely so that the first rule to land could not be
        indistinguishable from an assertion. Every row this run writes carries the ``rule``
        the response returns, and ``WHERE derived_by = <rule>`` is the complete description of
        what it produced — which is what makes a re-run a delete-then-insert rather than a
        reconciliation.

        **It enqueues and returns; it does not derive inline.** A rule pass over a bound scope
        is bounded, restartable work — ``validate:shape``'s argument, and here it carries the
        extra weight of being a write path: the task's own delete-then-insert is what makes a
        retry after a crashed attempt converge instead of doubling.

        **What is decided here rather than in the worker** is :func:`_pin_scope`'s four
        decisions, unchanged from a validation run except that open question 6.4's default
        needs no supporting argument for this one: the run writes facts about the scope's
        documents, so it takes write access to them.

        **The scope is also the bound on what may be WRITTEN, and that is step 9's mechanism.**
        A derived triple's visibility follows its endpoints, not its rule, so a rule that could
        name a document outside the scope could mint a fact about a document whose access
        nobody checked. Both endpoints of every derived row must be in this pinned set;
        anything else fails the run with :class:`~jmfts_core.shacl_rules.TermOutOfScopeError`
        and writes nothing at all.

        **No ``provenance`` parameter.** The graph a rule reads is the ASSERTED layer, always.
        See :class:`~jmfts_client.contracts.rdf.RuleDerivationRequest`: a field here
        would be the way to ask a rule to read another rule's output, which is the fixpoint
        Block B declined.

        **This does not check that ``pyshacl`` is installed, and it does not check that the
        shape carries a rule, for the same reason in both cases.** The API process is not
        necessarily the process that runs the task — a badged worker fleet is the normal
        deployment — so a refusal from here would refuse a run a worker could complete, and
        reading ``sh:rule`` out of the stored Turtle needs ``rdflib`` in THIS process. A shape
        that declares no rule fails the task PERMANENT with
        :class:`~jmfts_core.shacl_rules.ShapeDeclaresNoRuleError`, which is a better answer
        than a report saying "0 derived" — that number is indistinguishable from rules that
        ran and matched nothing.
        """
        db = self.session
        binding = self._binding_for(name, request.binding_id)
        document_ids, key = _pin_scope(db, binding, action="run a derivation")

        # Computed HERE and pinned onto the task row, not recomputed in the worker: it is the
        # DELETE set, and the response is where the caller learns it. The handler recomputes
        # it anyway and refuses a mismatch — see `derive_tasks.run_derive_rule`.
        rule = rule_identity(
            ontology_name=binding.ontology_name,
            shape_iri=binding.shape_iri,
            scope_type=binding.scope_type,
            scope=binding.scope or {},
        )

        requested_at = _utc_now()
        node = DocumentRepository(db).create(
            title=f"SHACL derivation — {binding.shape_iri}",
            # No content until the run writes the report, for the reason a validation report
            # has none: a node whose content said "pending" would retrieve and say nothing.
            content=None,
            # Top-level, and DERIVATION_PARENT_REASON is why — the settling walk must stop
            # here rather than offering a shared root a summarize.
            parent_id=None,
            usetype=DERIVATION_REPORT_USETYPE,
            structured_content={
                DERIVATION_BLOCK: {
                    "binding_id": binding.id,
                    "ontology_name": name,
                    "shape_iri": binding.shape_iri,
                    "scope_type": binding.scope_type,
                    "scope": binding.scope or {},
                    "provenance": DERIVATION_PROVENANCE,
                    "base_iri": DEFAULT_BASE_IRI,
                    "rule": rule,
                    "requested_at": requested_at.isoformat(),
                    "parent_reason": DERIVATION_PARENT_REASON,
                }
            },
            # No model runs for a report, here or in the task.
            auto_embed=False,
            embed_tokens=False,
            settled=SETTLED_IN_FLIGHT,
            produced_by=TASK_DERIVE_RULE,
        )
        _mirror_grants(db, node.id, key)
        db.flush()

        task = TaskQueueRepository(db).enqueue(
            TASK_DERIVE_RULE,
            node.id,
            WRITE_SELF,
            params={
                DERIVE_PARAM_BINDING_ID: binding.id,
                DERIVE_PARAM_DOCUMENT_IDS: document_ids,
                # Recorded rather than defaulted at run time: the derived rows are read back
                # against the names they were minted under, and the default can move.
                DERIVE_PARAM_BASE_IRI: DEFAULT_BASE_IRI,
                DERIVE_PARAM_RULE: rule,
            },
        )
        response = RuleDerivationRunResponse(
            task_id=task.id,
            report_document_id=node.id,
            binding_id=binding.id,
            ontology_name=name,
            shape_iri=binding.shape_iri,
            scope_type=binding.scope_type,
            rule=rule,
            scope_document_count=len(document_ids),
            provenance=DERIVATION_PROVENANCE,
            governed=bool(key),
            requested_at=requested_at,
            status=task.status,
        )
        # Commit before responding, for `import_ontology`'s reason.
        db.commit()
        return response
