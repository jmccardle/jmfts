"""Knowledge graph triple and predicate models."""

import enum
from datetime import datetime, timezone
from typing import Optional, Any
from sqlalchemy import (
    String,
    Text,
    Integer,
    ForeignKey,
    DateTime,
    CheckConstraint,
    UniqueConstraint,
    Index,
    Enum,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from jmfts_core.database import Base


class FactType(str, enum.Enum):
    """Fact classification per Zep/Graphiti taxonomy.

    - atemporal: universally true (e.g. "Paris is-capital-of France")
    - static: true for a long period, rarely changes (e.g. "John works-at Acme")
    - dynamic: frequently changing (e.g. "Server CPU-usage 87%")
    """

    atemporal = "atemporal"
    static = "static"
    dynamic = "dynamic"


class Predicate(Base):
    """Named predicate for knowledge graph relationships.

    A predicate carrying an ``iri`` is one a published vocabulary names; one without is
    local to this appliance. Nothing else about the row changes — a local predicate and a
    vocabulary predicate are the same kind of thing, and the IRI is the only fact that
    says which of the two it is.
    """

    __tablename__ = "predicates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)

    # WAS `domain`, renamed in migration 013 (SPRINT_0_3_0.md 4.4). `rdfs:domain` means
    # "the class a subject must belong to", and this column has never meant that — it is
    # the group a predicate belongs to. The two readings were harmless while nothing in
    # this tree spoke RDF; once ontologies land, the wrong one becomes the natural one.
    # `rdfs:domain` is expressed in the ontology, not here.
    namespace: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # The IRI a published vocabulary knows this predicate by, e.g.
    # "http://xmlns.com/foaf/0.1/knows". UNIQUE: two predicate rows claiming one IRI would
    # make "which predicate does this vocabulary term mean" ambiguous at exactly the point
    # where an ontology import has to answer it. NULL for a locally-minted predicate, and
    # NULLs do not conflict, so any number of local predicates coexist.
    iri: Mapped[Optional[str]] = mapped_column(Text, unique=True, nullable=True)

    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    # Relationships
    triples: Mapped[list["Triple"]] = relationship(
        "Triple", back_populates="predicate", cascade="all, delete-orphan"
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "namespace": self.namespace,
            "iri": self.iri,
            "description": self.description,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self):
        return f"<Predicate(id={self.id}, name='{self.name}')>"


class Triple(Base):
    """Knowledge graph triple: subject --predicate--> object.

    The object is EITHER another document node (``object_id``) OR a literal value
    (``object_literal``, optionally typed by ``object_datatype``). Exactly one, enforced by
    ``ck_triples_object_exactly_one`` below.

    Before migration 013 there was no literal: ``object_id`` was NOT NULL, so every object
    had to be a document. That is why ``fact_extraction.resolve_entity("128000")`` created
    a document node whose title and content were both the string ``128000``, embedded it at
    768 dimensions and hung it in the tree, where it is a retrieval hit. The literal column
    is the fix for that defect; RDF compliance is a consequence, not the motive.
    """

    __tablename__ = "triples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subject_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    predicate_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("predicates.id", ondelete="CASCADE"), nullable=False
    )
    # NULLABLE since migration 013: null here means the object is the literal below.
    object_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=True
    )
    # The literal object's lexical form, verbatim, and the `xsd:` IRI that types it
    # (e.g. "http://www.w3.org/2001/XMLSchema#integer"). The datatype is nullable because
    # an untyped literal is a plain string in RDF — a NULL here is `xsd:string`, not
    # "unknown". Storing the LEXICAL form rather than a parsed value is deliberate: "1.50"
    # and "1.5" are the same decimal and different literals, and a store that cannot tell
    # them apart cannot round-trip the document it read them from.
    object_literal: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    object_datatype: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    source_document_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("documents.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    # Temporal validity window
    valid_from: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    # Fact classification (Zep/Graphiti taxonomy)
    fact_type: Mapped[FactType] = mapped_column(
        Enum(FactType, name="fact_type", create_type=False),
        nullable=False,
        default=FactType.atemporal,
    )

    # Edge invalidation
    invalidated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    invalidated_by: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("triples.id", ondelete="SET NULL"), nullable=True
    )
    invalidation_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # WHICH RULE PRODUCED THIS ROW. NULL means asserted — a human, an upload, or an
    # extractor working from a source document. Anything else names the rule that derived
    # it, and "asserted only" is then `WHERE derived_by IS NULL`.
    #
    # SPRINT_0_3_0.md 4.2: this is the one column here that is expensive to add later.
    # Validation runs against raw data; inference materialises into a separate layer; the
    # materialised layer is never validated. Without this column the two layers are the
    # same rows and cannot be separated without re-deriving everything — and the failure it
    # prevents is silent: a rule invents the missing triple, the shape starts passing, and
    # nothing reports that it passed on a fact nobody asserted.
    #
    # Nothing writes it yet. There is no reasoner in this sprint (5.1) and there is not
    # meant to be one; the column exists so that the first rule to land cannot be
    # indistinguishable from an assertion.
    derived_by: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    # Relationships
    subject: Mapped["Document"] = relationship("Document", foreign_keys=[subject_id])
    predicate: Mapped["Predicate"] = relationship("Predicate", back_populates="triples")
    object: Mapped[Optional["Document"]] = relationship("Document", foreign_keys=[object_id])
    source_document: Mapped[Optional["Document"]] = relationship(
        "Document", foreign_keys=[source_document_id]
    )
    superseded_by: Mapped[Optional["Triple"]] = relationship(
        "Triple", remote_side=[id], foreign_keys=[invalidated_by]
    )

    __table_args__ = (
        # Exactly one object. A datatype belongs to a literal, so it is refused on a
        # resource object rather than being ignored there: a row saying "this document
        # node is an xsd:integer" is not a fact anything in this system can act on.
        CheckConstraint(
            "(object_id IS NOT NULL AND object_literal IS NULL AND object_datatype IS NULL)"
            " OR (object_literal IS NOT NULL AND object_id IS NULL)",
            name="ck_triples_object_exactly_one",
        ),
        # Resource objects only: NULLs are distinct in a UNIQUE constraint, so a literal
        # row (object_id NULL) never conflicts here. uq_triple_literal below is its
        # counterpart.
        UniqueConstraint("subject_id", "predicate_id", "object_id", name="uq_triple"),
        # THE LITERAL HALF OF uq_triple. Without it "Acme founded_in 1999" could be
        # asserted any number of times, because the constraint above cannot see it.
        #
        # md5(object_literal), not the literal itself: a btree entry is capped near 2704
        # bytes and object_literal is unbounded TEXT, so indexing it directly would turn a
        # long literal into an index-size error at INSERT. COALESCE on the datatype for the
        # same reason as above — NULL means xsd:string here, and NULLs that do not compare
        # equal would let the same untyped literal in twice.
        #
        # The cost is stated rather than hidden: two literals that share a subject, a
        # predicate, a datatype AND an md5 collision would be deduplicated as one fact.
        Index(
            "uq_triple_literal",
            "subject_id",
            "predicate_id",
            text("md5(object_literal)"),
            text("COALESCE(object_datatype, '')"),
            unique=True,
            postgresql_where=text("object_literal IS NOT NULL"),
        ),
        Index("ix_triples_subject", "subject_id"),
        Index("ix_triples_object", "object_id"),
        Index("ix_triples_predicate", "predicate_id"),
        Index("ix_triples_valid_range", "valid_from", "valid_until"),
        Index("ix_triples_fact_type", "fact_type"),
        # "Asserted only" and "derived by this rule" are both single-column lookups on a
        # column that is NULL for every row today. Partial, so the index holds only the
        # derived rows — the asserted majority is served by not being in it.
        Index(
            "ix_triples_derived_by", "derived_by", postgresql_where=text("derived_by IS NOT NULL")
        ),
    )

    @property
    def is_valid(self) -> bool:
        """Check if this triple is currently valid (not invalidated)."""
        return self.invalidated_at is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "subject_id": self.subject_id,
            "predicate_id": self.predicate_id,
            "object_id": self.object_id,
            "object_literal": self.object_literal,
            "object_datatype": self.object_datatype,
            "derived_by": self.derived_by,
            "source_document_id": self.source_document_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "valid_from": self.valid_from.isoformat() if self.valid_from else None,
            "valid_until": self.valid_until.isoformat() if self.valid_until else None,
            "recorded_at": self.recorded_at.isoformat() if self.recorded_at else None,
            "fact_type": self.fact_type.value if self.fact_type else None,
            "invalidated_at": self.invalidated_at.isoformat() if self.invalidated_at else None,
            "invalidated_by": self.invalidated_by,
            "invalidation_reason": self.invalidation_reason,
        }

    def __repr__(self):
        valid = "valid" if self.is_valid else "invalidated"
        obj = self.object_id if self.object_id is not None else repr(self.object_literal)
        return (
            f"<Triple(id={self.id}, s={self.subject_id}, p={self.predicate_id}, "
            f"o={obj}, {valid})>"
        )


# Avoid circular import: document imports this module, so this cannot move to the top.
from jmfts_core.models.document import Document  # noqa: E402
