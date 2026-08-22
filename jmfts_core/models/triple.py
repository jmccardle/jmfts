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
    UniqueConstraint,
    Index,
    Enum,
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
    """Named predicate for knowledge graph relationships."""

    __tablename__ = "predicates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    domain: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
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
            "domain": self.domain,
            "description": self.description,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self):
        return f"<Predicate(id={self.id}, name='{self.name}')>"


class Triple(Base):
    """Knowledge graph triple: subject --predicate--> object."""

    __tablename__ = "triples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subject_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    predicate_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("predicates.id", ondelete="CASCADE"), nullable=False
    )
    object_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
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

    # Relationships
    subject: Mapped["Document"] = relationship("Document", foreign_keys=[subject_id])
    predicate: Mapped["Predicate"] = relationship("Predicate", back_populates="triples")
    object: Mapped["Document"] = relationship("Document", foreign_keys=[object_id])
    source_document: Mapped[Optional["Document"]] = relationship(
        "Document", foreign_keys=[source_document_id]
    )
    superseded_by: Mapped[Optional["Triple"]] = relationship(
        "Triple", remote_side=[id], foreign_keys=[invalidated_by]
    )

    __table_args__ = (
        UniqueConstraint("subject_id", "predicate_id", "object_id", name="uq_triple"),
        Index("ix_triples_subject", "subject_id"),
        Index("ix_triples_object", "object_id"),
        Index("ix_triples_predicate", "predicate_id"),
        Index("ix_triples_valid_range", "valid_from", "valid_until"),
        Index("ix_triples_fact_type", "fact_type"),
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
        return (
            f"<Triple(id={self.id}, s={self.subject_id}, p={self.predicate_id}, "
            f"o={self.object_id}, {valid})>"
        )


# Avoid circular import: document imports this module, so this cannot move to the top.
from jmfts_core.models.document import Document  # noqa: E402
