"""TripleService — knowledge-graph triple & predicate operations, transport-neutral.

Logic extracted verbatim from ``api/routers/triples.py`` so the behaviour is identical;
the only intentional change is that document serialisation now goes through the single
``DocumentResponse.from_document`` converter (deleting ``triples.py``'s local
``_doc_to_response``, converter 3 of 4), which fixes the ``position``/``event_time``
field-drop on ``TripleDetailResponse.subject``/``object``.

Domain → HTTP mapping is declared per-op in ``@expose(errors=...)`` and keyed by
EXCEPTION TYPE, so the three hand-written statuses the router raised are reproduced
without a call-site check:

- ``LookupError``          → 404 (predicate/triple not found)
- ``PredicateConflictError`` → 409 (predicate name already exists)
- ``InvalidFactTypeError`` → 422 (fact_type not in the allowed set)

The 404 detail strings ("Predicate not found" / "Triple not found"), the 409 string,
and the 422 string are all preserved verbatim.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from jmfts_client.contracts.document import DocumentResponse
from jmfts_client.contracts.triple import (
    PathResponse,
    PathStep,
    PredicateCreate,
    PredicateResponse,
    TripleCreate,
    TripleDetailResponse,
    TripleInvalidateRequest,
    TripleResponse,
    TripleSupersedRequest,
)
from jmfts_core.access import readable_id_subset
from jmfts_core.models.triple import FactType
from jmfts_core.registry import expose, register_service
from jmfts_core.repositories.triple import TripleAlreadyInvalidatedError, TripleRepository


class PredicateConflictError(Exception):
    """A predicate with the requested name already exists (→ HTTP 409)."""


class InvalidFactTypeError(Exception):
    """A fact_type value outside the allowed set was supplied (→ HTTP 422)."""


def _parse_fact_type(value: str) -> FactType:
    """Coerce a fact_type string to the enum, raising the 422-mapped error.

    Preserves the router's exact detail string so the wire error is byte-identical.
    """
    try:
        return FactType(value)
    except ValueError:
        raise InvalidFactTypeError(
            f"Invalid fact_type '{value}'. Must be: atemporal, static, dynamic"
        )


@register_service
class TripleService:
    """Triple & predicate operations over a single database session."""

    def __init__(self, session: Session):
        self.session = session

    # -- Predicate endpoints ------------------------------------------------------

    @expose(
        "GET",
        "/triples/predicates",
        response_model=list[PredicateResponse],
        tags=["triples"],
        summary="List predicates",
    )
    def list_predicates(
        self,
        *,
        domain: Optional[str] = None,
        with_triples_only: bool = True,
    ) -> list[PredicateResponse]:
        """List predicates. By default returns only predicates with at least one triple."""
        repo = TripleRepository(self.session)
        return [
            PredicateResponse(**p.to_dict())
            for p in repo.list_predicates(domain=domain, with_triples_only=with_triples_only)
        ]

    @expose(
        "POST",
        "/triples/predicates",
        response_model=PredicateResponse,
        status_code=201,
        errors={PredicateConflictError: 409},
        tags=["triples"],
        summary="Create a new predicate",
    )
    def create_predicate(self, request: PredicateCreate) -> PredicateResponse:
        """Create a new predicate."""
        repo = TripleRepository(self.session)
        existing = repo.get_predicate_by_name(request.name)
        if existing:
            raise PredicateConflictError(f"Predicate '{request.name}' already exists")
        pred = repo.create_predicate(
            name=request.name, domain=request.domain, description=request.description
        )
        response = PredicateResponse(**pred.to_dict())
        # Commit before responding: get_db's teardown commit runs after the
        # response is sent, so a client acting on the returned id would race it.
        self.session.commit()
        return response

    @expose(
        "GET",
        "/triples/predicates/{predicate_id}",
        response_model=PredicateResponse,
        errors={LookupError: 404},
        tags=["triples"],
        summary="Get a predicate by ID",
    )
    def get_predicate(self, predicate_id: int) -> PredicateResponse:
        """Get a predicate by ID."""
        repo = TripleRepository(self.session)
        pred = repo.get_predicate(predicate_id)
        if not pred:
            raise LookupError("Predicate not found")
        return PredicateResponse(**pred.to_dict())

    @expose(
        "DELETE",
        "/triples/predicates/{predicate_id}",
        errors={LookupError: 404},
        tags=["triples"],
        summary="Delete a predicate and all its triples",
    )
    def delete_predicate(self, predicate_id: int) -> dict:
        """Delete a predicate and all its triples."""
        repo = TripleRepository(self.session)
        if not repo.delete_predicate(predicate_id):
            raise LookupError("Predicate not found")
        self.session.commit()
        return {"deleted": predicate_id}

    # -- Triple endpoints (route order: /query and /path before /{triple_id}) -----

    @expose(
        "POST",
        "/triples",
        response_model=TripleResponse,
        status_code=201,
        errors={InvalidFactTypeError: 422},
        tags=["triples"],
        summary="Create a new triple",
    )
    def create_triple(self, request: TripleCreate, *, dedup: bool = False) -> TripleResponse:
        """Create a new triple.

        With ``dedup=true`` this is the idempotent "assert this fact" verb: the
        ``(subject, predicate, object)`` unique key is honoured via ``INSERT ... ON
        CONFLICT DO NOTHING``, and an already-asserted fact is returned unchanged rather
        than raising ``IntegrityError``. Pairs with the exposed ``supersede``/
        ``invalidate`` legs to form a complete assert/retract/supersede fact API. Without
        the flag the plain insert still raises on a duplicate fact (unchanged behaviour).
        """
        fact_type = _parse_fact_type(request.fact_type)
        repo = TripleRepository(self.session)
        if dedup:
            triple, _created = repo.upsert_triple(
                subject_id=request.subject_id,
                predicate_id=request.predicate_id,
                object_id=request.object_id,
                source_document_id=request.source_document_id,
                valid_from=request.valid_from,
                valid_until=request.valid_until,
                fact_type=fact_type,
            )
        else:
            triple = repo.create_triple(
                subject_id=request.subject_id,
                predicate_id=request.predicate_id,
                object_id=request.object_id,
                source_document_id=request.source_document_id,
                valid_from=request.valid_from,
                valid_until=request.valid_until,
                fact_type=fact_type,
            )
        response = TripleResponse(**triple.to_dict())
        self.session.commit()
        return response

    @expose(
        "PUT",
        "/triples",
        response_model=TripleResponse,
        errors={InvalidFactTypeError: 422},
        tags=["triples"],
        summary="Assert a fact idempotently (insert-or-return the existing triple)",
    )
    def upsert_triple(self, request: TripleCreate) -> TripleResponse:
        """Assert a ``(subject, predicate, object)`` fact idempotently — the first-class
        "remember this fact" verb.

        ``PUT`` because it is idempotent: the unique ``(subject, predicate, object)`` key is
        honoured via ``INSERT ... ON CONFLICT DO NOTHING``, so re-asserting an existing fact
        returns it unchanged (HTTP 200) instead of raising ``IntegrityError`` the way a plain
        ``POST /triples`` does. This is the named, discoverable form of ``create_triple(dedup=
        true)``; both share ``repo.upsert_triple``.

        **On-conflict is DO NOTHING, not DO UPDATE — deliberately.** An existing fact's metadata
        (``source_document_id``, validity window, ``fact_type``) is *not* overwritten by a
        re-assert. Under the bitemporal model, changing a fact's validity or superseding its
        object is ``supersede_triple`` / ``invalidate_triple``, which preserves the version chain;
        a silent metadata overwrite here would fork that history. Re-assert = "this fact still
        holds", not "rewrite this fact".
        """
        fact_type = _parse_fact_type(request.fact_type)
        repo = TripleRepository(self.session)
        triple, _created = repo.upsert_triple(
            subject_id=request.subject_id,
            predicate_id=request.predicate_id,
            object_id=request.object_id,
            source_document_id=request.source_document_id,
            valid_from=request.valid_from,
            valid_until=request.valid_until,
            fact_type=fact_type,
        )
        response = TripleResponse(**triple.to_dict())
        self.session.commit()
        return response

    @expose(
        "GET",
        "/triples/query",
        response_model=list[TripleDetailResponse],
        errors={InvalidFactTypeError: 422},
        tags=["triples"],
        summary="Query triples with optional filters including temporal filtering",
    )
    def query_triples(
        self,
        *,
        entity_id: Optional[int] = None,
        predicate_id: Optional[int] = None,
        predicate: Optional[str] = None,
        direction: str = "both",
        limit: int = 50,
        offset: int = 0,
        valid_only: bool = False,
        valid_at: Optional[datetime] = None,
        fact_type: Optional[str] = None,
        include_invalidated: bool = True,
        coreferent: bool = False,
    ) -> list[TripleDetailResponse]:
        """Query triples with optional filters including temporal filtering.

        With ``coreferent=true`` and an ``entity_id``, the query expands to the entity's
        ``same_as`` cluster: facts recorded under any alias of the entity are returned as
        one set. Off by default, so a plain single-entity query is byte-identical — the
        coreference leg is strictly opt-in.
        """
        ft = None
        if fact_type:
            ft = _parse_fact_type(fact_type)
        repo = TripleRepository(self.session)
        entity_ids = None
        if coreferent and entity_id is not None:
            from jmfts_core.graph_analysis import resolve_coreferent_ids

            entity_ids = resolve_coreferent_ids(self.session, entity_id)
        triples = repo.query_triples(
            entity_id=entity_id,
            predicate_id=predicate_id,
            predicate_name=predicate,
            direction=direction,
            limit=limit,
            offset=offset,
            valid_only=valid_only,
            valid_at=valid_at,
            fact_type=ft,
            include_invalidated=include_invalidated,
            entity_ids=entity_ids,
        )
        results = []
        for t in triples:
            results.append(
                TripleDetailResponse(
                    id=t.id,
                    subject=DocumentResponse.from_document(t.subject),
                    predicate=PredicateResponse(**t.predicate.to_dict()),
                    object=DocumentResponse.from_document(t.object),
                    source_document_id=t.source_document_id,
                    created_at=t.created_at,
                    valid_from=t.valid_from,
                    valid_until=t.valid_until,
                    recorded_at=t.recorded_at,
                    fact_type=t.fact_type.value if t.fact_type else None,
                    invalidated_at=t.invalidated_at,
                    invalidated_by=t.invalidated_by,
                    invalidation_reason=t.invalidation_reason,
                )
            )
        return results

    @expose(
        "GET",
        "/triples/path",
        response_model=PathResponse,
        tags=["triples"],
        summary="Find paths between two entities via triples",
    )
    def find_path(
        self,
        *,
        from_id: int,
        to_id: int,
        max_depth: int = 5,
    ) -> PathResponse:
        """Find paths between two entities via triples."""
        repo = TripleRepository(self.session)
        paths = repo.find_path(from_id=from_id, to_id=to_id, max_depth=max_depth)
        result_paths = []
        for path in paths:
            steps = []
            for triple in path:
                steps.append(
                    PathStep(
                        triple_id=triple.id,
                        subject_id=triple.subject_id,
                        predicate_name=(
                            triple.predicate.name if triple.predicate else str(triple.predicate_id)
                        ),
                        object_id=triple.object_id,
                    )
                )
            result_paths.append(steps)
        return PathResponse(paths=result_paths, total_paths=len(result_paths))

    @expose(
        "POST",
        "/triples/{triple_id}/invalidate",
        response_model=TripleResponse,
        errors={LookupError: 404},
        tags=["triples"],
        summary="Invalidate a triple (soft-delete for contradicted facts)",
    )
    def invalidate_triple(self, triple_id: int, request: TripleInvalidateRequest) -> TripleResponse:
        """Invalidate a triple (soft-delete for contradicted facts)."""
        repo = TripleRepository(self.session)
        triple = repo.invalidate_triple(
            triple_id=triple_id,
            reason=request.reason,
            superseding_triple_id=request.superseding_triple_id,
        )
        if not triple:
            raise LookupError("Triple not found")
        response = TripleResponse(**triple.to_dict())
        self.session.commit()
        return response

    @expose(
        "POST",
        "/triples/{triple_id}/supersede",
        response_model=TripleResponse,
        status_code=201,
        errors={LookupError: 404, InvalidFactTypeError: 422, TripleAlreadyInvalidatedError: 409},
        tags=["triples"],
        summary="Create a new triple that supersedes (invalidates) an existing one",
    )
    def supersede_triple(self, triple_id: int, request: TripleSupersedRequest) -> TripleResponse:
        """Create a new triple that supersedes (invalidates) an existing one.

        Serialised against concurrent supersessions of the same triple: if it was already
        superseded, this raises ``TripleAlreadyInvalidatedError`` (→ 409) rather than
        forking the version chain. The caller should re-read the live head and supersede
        that instead.
        """
        repo = TripleRepository(self.session)
        old_triple = repo.get_triple(triple_id)
        if not old_triple:
            raise LookupError("Triple not found")
        fact_type = _parse_fact_type(request.fact_type)
        new_triple, _ = repo.supersede_triple(
            old_triple_id=triple_id,
            subject_id=request.subject_id,
            predicate_id=request.predicate_id,
            object_id=request.object_id,
            source_document_id=request.source_document_id,
            valid_from=request.valid_from,
            valid_until=request.valid_until,
            fact_type=fact_type,
            reason=request.reason,
        )
        if new_triple is None:
            # Raced with a delete between the existence check above and the locked read.
            raise LookupError("Triple not found")
        response = TripleResponse(**new_triple.to_dict())
        self.session.commit()
        return response

    @expose(
        "GET",
        "/triples/{triple_id}",
        response_model=TripleResponse,
        errors={LookupError: 404},
        tags=["triples"],
        summary="Get a triple by ID",
    )
    def get_triple(self, triple_id: int) -> TripleResponse:
        """Get a triple by ID."""
        repo = TripleRepository(self.session)
        triple = repo.get_triple(triple_id)
        if not triple:
            raise LookupError("Triple not found")
        # Subtree RBAC: a triple whose subject or object the principal cannot read is hidden
        # whole (existence-hiding, 404) — the same rule query_triples applies in bulk.
        endpoints = {triple.subject_id, triple.object_id}
        if readable_id_subset(self.session, endpoints) != endpoints:
            raise LookupError("Triple not found")
        return TripleResponse(**triple.to_dict())

    @expose(
        "DELETE",
        "/triples/{triple_id}",
        errors={LookupError: 404},
        tags=["triples"],
        summary="Delete a triple",
    )
    def delete_triple(self, triple_id: int) -> dict:
        """Delete a triple."""
        repo = TripleRepository(self.session)
        if not repo.delete_triple(triple_id):
            raise LookupError("Triple not found")
        self.session.commit()
        return {"deleted": triple_id}
