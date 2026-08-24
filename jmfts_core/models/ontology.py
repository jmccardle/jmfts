"""Ontologies and shape bindings — the vocabulary a scope of documents is held to.

``docs/SPRINT_0_3_0.md`` Part 4.5 and Part 5. Two tables, following the
:class:`~jmfts_core.models.usetype_presentation.UsetypePresentation` pattern: keyed on an
open classification string, carrying policy in JSONB, extended by inserting a row rather
than by altering the schema.

**Why the source Turtle is stored verbatim.** ``shapes`` below is a parsed digest of the
SHACL subset this sprint supports (5.1), and a digest is lossy by construction — an
ontology may declare more than the subset reads. Keeping the bytes that were POSTed means
the digest can be rebuilt when the subset widens, without asking whoever uploaded it to
find the file again. The digest is a cache of a parse; the Turtle is the record.

**What a binding is for.** A shape is a claim about a kind of record. A binding says which
documents that claim is about — a usetype pattern, a subtree, or an explicit set. Until a
shape is bound it constrains nothing, which is what makes uploading an ontology a safe
operation: it cannot change how anything already in the tree is validated or extracted.
"""

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from jmfts_core.database import Base

#: What a binding's ``scope_type`` may say. A closed set — unlike ``usetype``, which is
#: deliberately open — because each value here names a different query the binding
#: resolver has to run, and a value with no query behind it would silently match nothing.
#:
#:   usetype    scope = {"pattern": "profile:sheet"} — matched against documents.usetype
#:   subtree    scope = {"parent_id": 42}            — the node and everything below it
#:   documents  scope = {"document_ids": [1, 2, 3]}  — exactly these nodes
SCOPE_TYPES = ("usetype", "subtree", "documents")


class Ontology(Base):
    """One uploaded vocabulary: its Turtle, its prefixes, and the shapes read out of it."""

    __tablename__ = "ontologies"

    #: The open classification key, as ``usetype_presentations.usetype`` is. A name rather
    #: than a serial id because an ontology is referred to by name everywhere a human
    #: writes one down, and a second upload under the same name is a REPLACEMENT of that
    #: vocabulary rather than a second copy of it.
    name: Mapped[str] = mapped_column(String(200), primary_key=True)

    #: The IRI that terms in this document are relative to. Not derived from ``@base`` or
    #: from the first prefix: an ontology may declare neither, and guessing one would make
    #: every locally-minted IRI depend on a guess.
    base_iri: Mapped[str] = mapped_column(Text, nullable=False)

    #: Exactly the bytes that were uploaded, decoded. See the module docstring.
    source_turtle: Mapped[str] = mapped_column(Text, nullable=False)

    #: ``{prefix: namespace IRI}`` as the document declared them. Used to render Turtle
    #: back out with the names the author chose, instead of rdflib's invented ``ns1:``.
    prefixes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    #: ``{shape IRI: {targetClass, properties: [...]}}`` — the parsed SHACL subset (5.1).
    #: A digest of the Turtle above, never the authority over it.
    shapes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "base_iri": self.base_iri,
            "source_turtle": self.source_turtle,
            "prefixes": self.prefixes or {},
            "shapes": self.shapes or {},
            "description": self.description,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self):
        return f"<Ontology(name='{self.name}', shapes={len(self.shapes or {})})>"


class ShapeBinding(Base):
    """Which shape applies to which documents."""

    __tablename__ = "shape_bindings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    ontology_name: Mapped[str] = mapped_column(
        String(200), ForeignKey("ontologies.name", ondelete="CASCADE"), nullable=False
    )

    #: The ``sh:NodeShape`` this binding names. Text and not a foreign key: shapes live
    #: inside the ontology's JSONB digest, and promoting them to their own table would make
    #: the digest the authority over the Turtle, which the module docstring refuses.
    #: Whether the IRI names a shape the ontology actually declares is checked at bind
    #: time, where the answer can be reported to the caller.
    shape_iri: Mapped[str] = mapped_column(Text, nullable=False)

    scope_type: Mapped[str] = mapped_column(String(20), nullable=False)
    scope: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        CheckConstraint(
            "scope_type IN ('usetype', 'subtree', 'documents')",
            name="ck_shape_bindings_scope_type",
        ),
        # The same shape bound to the same scope twice is one binding asserted twice, and a
        # second row would double every rule that iterates bindings.
        UniqueConstraint(
            "ontology_name", "shape_iri", "scope_type", "scope", name="uq_shape_binding"
        ),
        Index("ix_shape_bindings_ontology", "ontology_name"),
        # "Which bindings mention this usetype / this parent / this document" is a
        # containment query against the scope object, which is what GIN answers.
        Index("ix_shape_bindings_scope", "scope", postgresql_using="gin"),
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ontology_name": self.ontology_name,
            "shape_iri": self.shape_iri,
            "scope_type": self.scope_type,
            "scope": self.scope or {},
            "description": self.description,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self):
        return (
            f"<ShapeBinding(id={self.id}, shape='{self.shape_iri}', "
            f"scope_type='{self.scope_type}')>"
        )
