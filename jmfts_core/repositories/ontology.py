"""Repository for ontologies and shape bindings.

``docs/SPRINT_0_3_0.md`` 4.5. Two tables, following ``usetype_presentations``: an open
string key, policy in JSONB, extended by inserting a row.

Nothing here imports ``rdflib``. That is not an accident of layering — it is the claim the
``rdf`` extra rests on. A vocabulary is a row: bytes, a prefix map, a parsed digest, and
the bindings that say which documents it is about. Reading and writing those rows is
storage, and a base install can do all of it. What needs the extra is turning the bytes
into the digest, which happens one layer up in ``OntologyService``.
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from jmfts_core.models.ontology import SCOPE_TYPES, Ontology, ShapeBinding


class OntologyRepository:
    """Ontology and shape-binding CRUD over a single database session."""

    def __init__(self, session: Session):
        self.session = session

    # -- ontologies ---------------------------------------------------------------

    def get(self, name: str) -> Optional[Ontology]:
        return self.session.get(Ontology, name)

    def list_all(self) -> list[Ontology]:
        return list(self.session.execute(select(Ontology).order_by(Ontology.name)).scalars().all())

    def upsert(
        self,
        *,
        name: str,
        base_iri: str,
        source_turtle: str,
        prefixes: dict[str, str],
        shapes: dict[str, Any],
        description: Optional[str] = None,
    ) -> tuple[Ontology, bool]:
        """Store this vocabulary under ``name``, replacing any vocabulary already there.

        Returns ``(ontology, replaced)``.

        A REPLACEMENT and not a second copy, because ``name`` is the primary key and a
        vocabulary is referred to by name everywhere a human writes one down. The bindings
        that point at the old rows are NOT dropped: ``shape_bindings.shape_iri`` is TEXT
        rather than a foreign key precisely so a re-upload that keeps a shape keeps its
        bindings, and one that drops a shape leaves a binding whose ``shape_iri`` the
        ontology no longer declares — visible, reportable, and not silently deleted policy.
        """
        existing = self.get(name)
        replaced = existing is not None
        if existing is None:
            existing = Ontology(name=name)
            self.session.add(existing)
        existing.base_iri = base_iri
        existing.source_turtle = source_turtle
        existing.prefixes = prefixes
        existing.shapes = shapes
        existing.description = description
        self.session.flush()
        return existing, replaced

    def delete(self, name: str) -> bool:
        ontology = self.get(name)
        if ontology is None:
            return False
        # The FK is ON DELETE CASCADE, so the bindings go with it — deleting a vocabulary
        # while leaving live bindings pointing at its shapes would leave rules that name
        # nothing.
        self.session.delete(ontology)
        self.session.flush()
        return True

    # -- shape bindings -----------------------------------------------------------

    def create_binding(
        self,
        *,
        ontology_name: str,
        shape_iri: str,
        scope_type: str,
        scope: dict[str, Any],
        description: Optional[str] = None,
    ) -> ShapeBinding:
        """Bind a shape to a scope.

        The database enforces the closed ``scope_type`` set too
        (``ck_shape_bindings_scope_type``); this raises first so the caller gets the name of
        its own mistake rather than an IntegrityError that has already poisoned the
        transaction it was running in — the same reason ``TripleRepository.create_triple``
        checks the object constraint in Python.
        """
        if scope_type not in SCOPE_TYPES:
            raise ValueError(
                f"scope_type must be one of {', '.join(SCOPE_TYPES)}; got {scope_type!r}"
            )
        binding = ShapeBinding(
            ontology_name=ontology_name,
            shape_iri=shape_iri,
            scope_type=scope_type,
            scope=scope,
            description=description,
        )
        self.session.add(binding)
        self.session.flush()
        return binding

    def get_binding(self, binding_id: int) -> Optional[ShapeBinding]:
        return self.session.get(ShapeBinding, binding_id)

    def list_bindings(self, ontology_name: Optional[str] = None) -> list[ShapeBinding]:
        query = select(ShapeBinding).order_by(ShapeBinding.id)
        if ontology_name is not None:
            query = query.where(ShapeBinding.ontology_name == ontology_name)
        return list(self.session.execute(query).scalars().all())

    def find_binding(
        self, *, ontology_name: str, shape_iri: str, scope_type: str, scope: dict[str, Any]
    ) -> Optional[ShapeBinding]:
        """The binding this one would collide with on ``uq_shape_binding``, if any.

        Looked up in Python rather than by a JSONB equality predicate: ``uq_shape_binding``
        compares the jsonb value, and a SQL ``=`` on jsonb is key-order-independent while a
        Python dict comparison is too — but the SQL form would need the scope round-tripped
        through the same normalisation the column applies, and the binding count per
        ontology is small enough that reading them is cheaper than getting that subtly wrong.
        """
        for binding in self.list_bindings(ontology_name):
            if (
                binding.shape_iri == shape_iri
                and binding.scope_type == scope_type
                and (binding.scope or {}) == scope
            ):
                return binding
        return None

    def delete_binding(self, binding_id: int) -> bool:
        binding = self.get_binding(binding_id)
        if binding is None:
            return False
        self.session.delete(binding)
        self.session.flush()
        return True
