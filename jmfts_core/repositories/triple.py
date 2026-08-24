"""Repository for knowledge graph operations."""

from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import select, and_, or_, exists
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, joinedload

from jmfts_core.access import readable_id_subset
from jmfts_core.models.triple import Triple, Predicate, FactType


class TripleAlreadyInvalidatedError(Exception):
    """A supersede targeted a triple that was already invalidated/superseded (→ 409).

    Carries the id of the successor that already invalidated it, so the caller can
    re-read the live head of the chain and supersede *that* instead of forking it.
    """

    def __init__(self, triple_id: int, invalidated_by: Optional[int]):
        self.triple_id = triple_id
        self.invalidated_by = invalidated_by
        msg = f"Triple {triple_id} was already superseded"
        if invalidated_by is not None:
            msg += f" by triple {invalidated_by}"
        super().__init__(msg)


class TripleRepository:
    """Repository for triple and predicate CRUD and graph queries."""

    def __init__(self, session: Session):
        self.session = session

    # =========================================================================
    # Predicate CRUD
    # =========================================================================

    def create_predicate(
        self,
        name: str,
        namespace: Optional[str] = None,
        description: Optional[str] = None,
        iri: Optional[str] = None,
    ) -> Predicate:
        pred = Predicate(name=name, namespace=namespace, description=description, iri=iri)
        self.session.add(pred)
        self.session.flush()
        return pred

    def get_or_create_predicate(
        self,
        name: str,
        namespace: Optional[str] = None,
        description: Optional[str] = None,
        iri: Optional[str] = None,
    ) -> tuple[Predicate, bool]:
        """Resolve a predicate by name, creating it atomically if absent.

        ``predicates.name`` is UNIQUE, so a plain get-then-``create_predicate``
        races: two concurrent transactions minting the same name both see "not
        found" and the loser aborts on ``predicates_name_key`` — which, inside a
        fact-extraction batch, rolls back the whole batch. INSERT ... ON CONFLICT
        DO NOTHING makes the mint idempotent under concurrency (mirrors
        ``upsert_triple``); ``create_predicate`` keeps raising for the explicit
        REST create path that wants a 409.

        ``iri`` is only ever WRITTEN by this call, never used to resolve: the conflict key
        is ``name``, so importing a vocabulary term whose local name already exists returns
        the existing predicate and leaves its ``iri`` alone rather than silently rebinding
        a local predicate to somebody else's IRI. ``OntologyService`` reports that case to
        the caller instead of resolving it here.

        Returns:
            (predicate, created) — created is False if it already existed.
        """
        stmt = (
            pg_insert(Predicate.__table__)
            .values(name=name, namespace=namespace, description=description, iri=iri)
            .on_conflict_do_nothing(index_elements=["name"])
            .returning(Predicate.__table__.c.id)
        )
        created = self.session.execute(stmt).first() is not None
        # A concurrent inserter's row is visible to our SELECT once committed; if
        # it is still in-flight, ON CONFLICT blocked us until it committed, so by
        # here the row exists either way.
        pred = self.get_predicate_by_name(name)
        return pred, created

    def get_predicate(self, predicate_id: int) -> Optional[Predicate]:
        return self.session.get(Predicate, predicate_id)

    def get_predicate_by_name(self, name: str) -> Optional[Predicate]:
        result = self.session.execute(select(Predicate).where(Predicate.name == name))
        return result.scalar_one_or_none()

    def get_predicate_by_iri(self, iri: str) -> Optional[Predicate]:
        result = self.session.execute(select(Predicate).where(Predicate.iri == iri))
        return result.scalar_one_or_none()

    def list_predicates(
        self, namespace: Optional[str] = None, with_triples_only: bool = False
    ) -> list[Predicate]:
        query = select(Predicate).order_by(Predicate.name)
        if namespace:
            query = query.where(Predicate.namespace == namespace)
        if with_triples_only:
            query = query.where(exists().where(Triple.predicate_id == Predicate.id))
        result = self.session.execute(query)
        return list(result.scalars().all())

    def delete_predicate(self, predicate_id: int) -> bool:
        pred = self.get_predicate(predicate_id)
        if pred:
            self.session.delete(pred)
            return True
        return False

    # =========================================================================
    # Triple CRUD
    # =========================================================================

    def create_triple(
        self,
        subject_id: int,
        predicate_id: int,
        object_id: Optional[int] = None,
        source_document_id: Optional[int] = None,
        valid_from: Optional[datetime] = None,
        valid_until: Optional[datetime] = None,
        fact_type: FactType = FactType.atemporal,
        object_literal: Optional[str] = None,
        object_datatype: Optional[str] = None,
        derived_by: Optional[str] = None,
    ) -> Triple:
        """Write one triple. The object is a document node OR a literal, never both.

        The database enforces that too (``ck_triples_object_exactly_one``); this raises
        first so the caller gets the name of its own mistake rather than an IntegrityError
        that has already poisoned the transaction it was running in.
        """
        if (object_id is None) == (object_literal is None):
            raise ValueError(
                "a triple's object is exactly one of object_id and object_literal; "
                f"got object_id={object_id!r}, object_literal={object_literal!r}"
            )
        if object_datatype is not None and object_literal is None:
            raise ValueError("object_datatype types a literal; it cannot type a document node")
        triple = Triple(
            subject_id=subject_id,
            predicate_id=predicate_id,
            object_id=object_id,
            object_literal=object_literal,
            object_datatype=object_datatype,
            derived_by=derived_by,
            source_document_id=source_document_id,
            valid_from=valid_from,
            valid_until=valid_until,
            fact_type=fact_type,
        )
        self.session.add(triple)
        self.session.flush()
        return triple

    def upsert_triple(
        self,
        subject_id: int,
        predicate_id: int,
        object_id: int,
        source_document_id: Optional[int] = None,
        valid_from: Optional[datetime] = None,
        valid_until: Optional[datetime] = None,
        fact_type: FactType = FactType.atemporal,
    ) -> tuple[Triple, bool]:
        """Insert a triple, returning the existing one if it already exists.

        Uses INSERT ... ON CONFLICT DO NOTHING to avoid IntegrityError and
        savepoint rollback issues during bulk ingestion.

        Returns:
            (triple, created) — created is False if the triple already existed.
        """
        stmt = (
            pg_insert(Triple.__table__)
            .values(
                subject_id=subject_id,
                predicate_id=predicate_id,
                object_id=object_id,
                source_document_id=source_document_id,
                valid_from=valid_from,
                valid_until=valid_until,
                fact_type=fact_type,
            )
            .on_conflict_do_nothing(index_elements=["subject_id", "predicate_id", "object_id"])
        )
        result = self.session.execute(stmt)
        created = result.rowcount > 0

        triple = self.session.execute(
            select(Triple).where(
                Triple.subject_id == subject_id,
                Triple.predicate_id == predicate_id,
                Triple.object_id == object_id,
            )
        ).scalar_one()

        return triple, created

    def get_triple(self, triple_id: int) -> Optional[Triple]:
        return self.session.get(Triple, triple_id)

    def delete_triple(self, triple_id: int) -> bool:
        triple = self.get_triple(triple_id)
        if triple:
            self.session.delete(triple)
            return True
        return False

    def query_triples(
        self,
        entity_id: Optional[int] = None,
        predicate_id: Optional[int] = None,
        predicate_name: Optional[str] = None,
        direction: str = "both",
        limit: int = 50,
        offset: int = 0,
        valid_only: bool = False,
        valid_at: Optional[datetime] = None,
        fact_type: Optional[FactType] = None,
        include_invalidated: bool = True,
        entity_ids: Optional[list[int]] = None,
        provenance: str = "any",
    ) -> list[Triple]:
        """Query triples, optionally scoped to one entity or a set of entities.

        ``entity_ids`` generalises ``entity_id`` to a set — the union of facts touching
        any member, in one query. It is how coreference (a ``same_as`` cluster) is served:
        resolve the cluster, then pass every member here so a query on one alias sees the
        facts recorded under all of them. When both are given, ``entity_ids`` wins;
        ``entity_id`` alone behaves exactly as before (single-id filter).

        **Neither given means unscoped, and that is deliberate** — ``GET /triples`` and the
        Turtle export both page the whole store through here. What is not allowed is a
        scope that WAS asked for and came out empty widening to unscoped; see the comment
        on the filter below.

        ``provenance`` splits the asserted layer from a derived one (``derived_by``):
        ``"asserted"`` is ``derived_by IS NULL``, ``"derived"`` its complement, ``"any"``
        both. An unrecognised value raises rather than being read as ``"any"`` — a filter
        that silently widens is how an unvalidated derived row reaches an answer that asked
        for asserted facts.
        """
        if provenance not in ("any", "asserted", "derived"):
            raise ValueError(
                f"provenance must be 'any', 'asserted' or 'derived'; got {provenance!r}"
            )
        query = select(Triple).options(
            joinedload(Triple.subject),
            joinedload(Triple.predicate),
            joinedload(Triple.object),
        )

        conditions = []
        if predicate_id:
            conditions.append(Triple.predicate_id == predicate_id)
        if predicate_name:
            query = query.join(Predicate)
            conditions.append(Predicate.name == predicate_name)
        # Three states, not two: no scope asked for (None), a scope asked for and empty
        # ([]), a scope asked for and populated. An empty `entity_ids` — a coreference
        # cluster whose every member the caller may not read, say — must return NOTHING;
        # under `if ids:` it fell through to the unscoped query and returned the whole
        # store instead. Same family of defect as SPRINT_0_3_0.md 13.1: a filter built by
        # truthiness widens exactly where it should close.
        if entity_ids is not None:
            ids: Optional[list[int]] = entity_ids
        elif entity_id is not None:
            ids = [entity_id]
        else:
            ids = None
        if ids is not None:
            if direction == "outgoing":
                conditions.append(Triple.subject_id.in_(ids))
            elif direction == "incoming":
                conditions.append(Triple.object_id.in_(ids))
            else:
                conditions.append(or_(Triple.subject_id.in_(ids), Triple.object_id.in_(ids)))

        # Temporal filtering
        if valid_only or not include_invalidated:
            conditions.append(Triple.invalidated_at.is_(None))

        if valid_at is not None:
            conditions.append(or_(Triple.valid_from.is_(None), Triple.valid_from <= valid_at))
            conditions.append(or_(Triple.valid_until.is_(None), Triple.valid_until >= valid_at))

        if fact_type is not None:
            conditions.append(Triple.fact_type == fact_type)

        if provenance == "asserted":
            conditions.append(Triple.derived_by.is_(None))
        elif provenance == "derived":
            conditions.append(Triple.derived_by.is_not(None))

        if conditions:
            query = query.where(and_(*conditions))

        query = query.order_by(Triple.id).offset(offset).limit(limit)
        result = self.session.execute(query)
        triples = list(result.unique().scalars().all())

        # Subtree RBAC: hide any triple whose subject OR object is a document the current
        # principal cannot read — a fact about a hidden entity must not leak its existence.
        # Single query; a no-op for owner/unbound callers and when no ACRs exist. Because
        # find_path() walks the graph through this method, dropping edges to unreadable
        # nodes here also prevents paths from traversing them.
        # A literal object has no document id and therefore no access rule of its own; the
        # subject's is the whole check for it. `None` must not reach readable_id_subset.
        endpoint_ids = {t.subject_id for t in triples} | {
            t.object_id for t in triples if t.object_id is not None
        }
        readable = readable_id_subset(self.session, endpoint_ids)
        if len(readable) != len(endpoint_ids):
            triples = [
                t
                for t in triples
                if t.subject_id in readable and (t.object_id is None or t.object_id in readable)
            ]
        return triples

    # =========================================================================
    # Edge Invalidation
    # =========================================================================

    def invalidate_triple(
        self,
        triple_id: int,
        reason: Optional[str] = None,
        superseding_triple_id: Optional[int] = None,
    ) -> Optional[Triple]:
        """Mark a triple as invalidated (soft-delete for contradicted facts)."""
        triple = self.get_triple(triple_id)
        if not triple:
            return None
        triple.invalidated_at = datetime.now(timezone.utc)
        triple.invalidation_reason = reason
        triple.invalidated_by = superseding_triple_id
        self.session.flush()
        return triple

    def supersede_triple(
        self,
        old_triple_id: int,
        subject_id: int,
        predicate_id: int,
        object_id: int,
        source_document_id: Optional[int] = None,
        valid_from: Optional[datetime] = None,
        valid_until: Optional[datetime] = None,
        fact_type: FactType = FactType.atemporal,
        reason: Optional[str] = None,
    ) -> tuple[Optional[Triple], Optional[Triple]]:
        """Create a new triple that supersedes (invalidates) an existing one.

        Returns (new_triple, old_triple), or (None, None) if the old triple no longer
        exists (raced with a delete).

        Axis-B (concurrent supersessions of the SAME triple): under READ COMMITTED two
        callers both read the old triple as live, both mint a successor, and both set
        ``invalidated_by`` — last writer wins, so the version chain forks (two live
        successors, one lost back-pointer). Locking the old row with ``FOR UPDATE`` before
        the invalidation serialises them: the second caller blocks, then observes
        ``invalidated_at`` set and raises ``TripleAlreadyInvalidatedError`` instead of
        forking. The lock is taken BEFORE minting the successor, so the loser never
        creates an orphan.

        Raises:
            TripleAlreadyInvalidatedError: the old triple was already superseded/
                invalidated by the time the lock was acquired.
        """
        old_triple = self.session.execute(
            select(Triple).where(Triple.id == old_triple_id).with_for_update()
        ).scalar_one_or_none()
        if old_triple is None:
            return None, None
        if old_triple.invalidated_at is not None:
            raise TripleAlreadyInvalidatedError(old_triple_id, old_triple.invalidated_by)

        new_triple = self.create_triple(
            subject_id=subject_id,
            predicate_id=predicate_id,
            object_id=object_id,
            source_document_id=source_document_id,
            valid_from=valid_from,
            valid_until=valid_until,
            fact_type=fact_type,
        )
        old_triple.invalidated_at = datetime.now(timezone.utc)
        old_triple.invalidation_reason = reason or "Superseded by newer triple"
        old_triple.invalidated_by = new_triple.id
        self.session.flush()
        return new_triple, old_triple

    # =========================================================================
    # Graph Queries
    # =========================================================================

    def find_path(
        self,
        from_id: int,
        to_id: int,
        max_depth: int = 5,
        valid_only: bool = False,
    ) -> list[list[Triple]]:
        """BFS path finding between two entities via triples.

        A path is between entities, so only resource edges are traversable. A triple whose
        object is a LITERAL is a leaf — a value has no far side to step to — and is skipped
        here rather than walked (``SPRINT_0_3_0.md`` 13.1). Before that skip existed a
        literal fact produced ``next_id = None``, which was queued and then passed to
        ``query_triples(entity_id=None)``; that filter is built by truthiness, so the round
        ran UNSCOPED and every triple in the store was treated as adjacent to the literal's
        subject. Two edges that do not connect came back as a path.
        """
        visited = {from_id}
        queue: list[tuple[int, list[Triple]]] = [(from_id, [])]
        paths: list[list[Triple]] = []

        for _ in range(max_depth):
            next_queue: list[tuple[int, list[Triple]]] = []
            for current_id, path in queue:
                triples = self.query_triples(
                    entity_id=current_id,
                    direction="both",
                    limit=200,
                    valid_only=valid_only,
                )
                for triple in triples:
                    if triple.object_id is None:
                        continue  # literal object: a leaf, not an edge to walk through
                    next_id = (
                        triple.object_id if triple.subject_id == current_id else triple.subject_id
                    )
                    if next_id == to_id:
                        paths.append(path + [triple])
                        if len(paths) >= 5:
                            return paths
                    elif next_id not in visited:
                        visited.add(next_id)
                        next_queue.append((next_id, path + [triple]))
            queue = next_queue
            if not queue:
                break

        return paths
