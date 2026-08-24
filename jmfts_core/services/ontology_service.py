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

from typing import Optional

from sqlalchemy.orm import Session

from jmfts_client.contracts.rdf import (
    OntologyImportResponse,
    OntologyResponse,
    PredicateImportConflict,
    ShapeBindingCreate,
    ShapeBindingResponse,
    ShapeProperty,
    ShapeSummary,
    TurtleDocument,
)
from jmfts_core.models.ontology import Ontology, ShapeBinding
from jmfts_core.rdf import RdfStackNotInstalled
from jmfts_core.registry import expose, register_service
from jmfts_core.repositories.ontology import OntologyRepository
from jmfts_core.repositories.triple import TripleRepository


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
