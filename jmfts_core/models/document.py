"""Document and DocumentLink models"""

from datetime import datetime
from typing import Optional, List, Any
from sqlalchemy import (
    CheckConstraint,
    String,
    Text,
    Integer,
    Float,
    ForeignKey,
    DateTime,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import JSONB
from pgvector.sqlalchemy import Vector

from jmfts_core.database import Base

#: The closed set of ingest-lifecycle states `Document.settled` may hold. Mirrors the
#: `ck_documents_settled` CHECK constraint in schema.sql / migration 008; the database is
#: the authority, this tuple exists so Python can reject a bad value with a useful
#: message instead of surfacing an IntegrityError at flush time.
SETTLED_IN_FLIGHT = "in_flight"
SETTLED_SETTLED = "settled"
SETTLED_FAILED = "failed"
SETTLED_STATES: tuple[str, ...] = (SETTLED_IN_FLIGHT, SETTLED_SETTLED, SETTLED_FAILED)

#: The usetypes the ingest pipeline itself writes (INGEST_SPEC.md Part 9). Usetype stays an
#: OPEN string — there is no enum, no CHECK constraint and no validation list to extend;
#: these constants exist so the several places that mean the same node spell it the same
#: way, not to close the set. An application built on JMFTS defines its own usetypes freely.
#:
#: THEY LIVE ON THE MODEL, and each one is here for the reason `USETYPE_FILE` was here
#: alone: something BELOW the handler that writes the node has to recognise it. The settling
#: walk is below the service layer and needs `file`. Part 4's rule table
#: (:data:`~jmfts_core.ingest_tasks.TASK_ROWS`) is below every handler module and, since a
#: rule's scope names the KIND of child it applies to (`SPRINT_JOBS.md` 4.1), needs the
#: other six. Defining them in the handler modules and importing them into the table would
#: close an import cycle: every one of those modules imports the table's own module.
#:
#: ONE OF THEM WAS ALREADY WRITTEN TWICE — `sheet_tasks` and `services/document_service`
#: each declared `USETYPE_SHEET = "sheet"`, which is precisely the copy-drift Part 14
#: forbids. Consolidating them is what removes the second spelling rather than adding a
#: seventh.
USETYPE_FILE = "file"

#: A titled region a structure rung found: a chapter, a heading's span. Holds no text of
#: its own — its chunks do — and gets `effective_content` from the rollup.
USETYPE_SECTION = "section"

#: A leaf carrying prose: a piece of a region, or one turn of a transcript. The node the
#: token/maxsim path exists for.
USETYPE_CHUNK = "chunk"

#: A container PELT created over a span of siblings. Not a `section`: a section is a span
#: the DOCUMENT named, and this is a span this appliance found.
USETYPE_SEGMENT = "segment"

#: One worksheet of a workbook (8.1). The whole of a workbook's declared rung.
USETYPE_SHEET = "sheet"

#: A measured summary node — today, the profile `profile:sheet` writes under a sheet (8.5).
#: The same string the RAPTOR summaries use, and deliberately: a profile is a summary of the
#: node above it, it is retrieved the same way, and a reader filtering `usetype='summary'`
#: wants both.
USETYPE_SUMMARY = "summary"

#: One row of a worksheet, as typed JSON (8.4's `records` shape). `record` and not `row`:
#: what the node holds is one instance of whatever the sheet is a table of, and its row
#: NUMBER is a fact about where it was found.
USETYPE_RECORD = "record"


class Document(Base):
    """Core document model with hierarchical structure and vector embeddings"""

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("documents.id", ondelete="SET NULL"), nullable=True
    )

    # Content
    title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    structured_content: Mapped[dict] = mapped_column(JSONB, default=dict)

    # Matryoshka embedding (768 dimensions for modernbert-embed-base)
    embed: Mapped[Optional[List[float]]] = mapped_column(Vector(768), nullable=True)

    # Tree navigation path (array of ancestor IDs)
    path: Mapped[list] = mapped_column(JSONB, default=list)

    # Classification
    usetype: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # WHICH RULE PRODUCED THIS NODE. NULL means asserted — a person, an importer, or an
    # upload created it, not a rule. Anything else names the rule, which today is the task
    # type of the atom that wrote the node ('structure:declared', 'extract:sheet', ...).
    #
    # SPRINT_JOBS.md 4.2: a multiplicity gives a number and a scope needs an IDENTITY. A
    # rule scoped to "the children another rule produced" is only answerable if each child
    # records what made it — a node with children from `chunk` and children from `partition`
    # cannot be told apart any other way, and the manually rearranged tree is exactly the
    # case that misattributes without it. `usetype` does not answer it: that says what a
    # node IS, not what made it, and one structure rung writes both `section` and `chunk`.
    #
    # The symmetry with `Triple.derived_by` is deliberate and so is the NULL convention:
    # a derived thing names the rule that produced it, and "asserted only" is
    # `WHERE produced_by IS NULL`. 9.4 is the other half — a person who edits a produced
    # node clears the stamp, so a later re-run creates a sibling rather than silently
    # overwriting the edit. See migration 016.
    produced_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # Explicit sibling ordering (CR-1). Sparse: NULL for unordered subtrees.
    # Ordering contract: position ASC NULLS LAST, created_at ASC, id ASC.
    position: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Timestamps — SYSTEM time: when this row entered the store / last changed in it.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow
    )

    # DOMAIN time (sparse, nullable): when the thing this document records actually
    # happened. Set it for imported content whose ingest time carries no signal — a
    # transcript turn, a backfilled note, a benchmark session — where every row would
    # otherwise share one `created_at` and any recency ordering would just be reading
    # ingest order. NULL for documents authored in place; read as
    # `COALESCE(event_time, created_at)`, so NULL keeps the legacy behaviour exactly.
    #
    # Mirrors the split `Triple` already makes (valid_from/valid_until = domain time
    # vs created_at/recorded_at = system time); documents lacked the domain half.
    #
    # NOT `updated_at`: that carries `onupdate` and re-stamps on every write (the embed
    # pass included, since it is an UPDATE), so it can never hold a backdated value —
    # and it means "content changed", so an edit would clobber whatever else lived there.
    # NOT an access clock either: "last retrieved" is a separate axis that needs its own
    # column when something actually consumes it. See migration 005.
    event_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Content deduplication
    content_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # INGEST LIFECYCLE — one of SETTLED_STATES: 'in_flight' | 'settled' | 'failed'.
    # A node being shuffled between pipeline stages and a node that is finished,
    # embedded and searchable are not the same kind of record; this is the separation.
    # `settled` is recursive: a node is settled when its own work is done AND every
    # child is settled (a childless node settles on its own work alone). `failed` marks
    # a permanent task failure with no retry scheduled — it exists so that a dead node
    # is distinguishable from one still in progress, which a sweeper would otherwise
    # eventually publish.
    #
    # TEXT + CHECK, not a Postgres ENUM: migrations here run inside BEGIN/COMMIT and
    # `ALTER TYPE ... ADD VALUE` cannot, so a fourth state would be disproportionately
    # expensive. Unlike `usetype` (deliberately an open string) this is a closed set, so
    # it is constrained in the database rather than left to convention.
    #
    # BOTH defaults are load-bearing and they are not redundant. `server_default` covers
    # rows written by raw SQL and the ADD COLUMN backfill of every existing row;
    # `default` covers ORM inserts, so a Document constructed without an opinion lands
    # as 'settled' without a round trip to read the server default back.
    # Note neither one is an ATTRIBUTE default: both fire at INSERT, so an in-memory
    # Document that has never been flushed reads `settled is None`, exactly like
    # `created_at`. Anything building a synthetic Document (the DocumentResponse
    # coverage guards do) must set it, and DocumentResponse deliberately rejects None
    # rather than substituting 'settled' for a row whose real state is unknown.
    # See migration 008.
    settled: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="settled", default="settled"
    )

    __table_args__ = (
        CheckConstraint(
            "settled IN ('in_flight', 'settled', 'failed')", name="ck_documents_settled"
        ),
    )

    # Relationships
    parent: Mapped[Optional["Document"]] = relationship(
        "Document", remote_side=[id], back_populates="children"
    )
    children: Mapped[List["Document"]] = relationship(
        "Document", back_populates="parent", cascade="all, delete-orphan"
    )
    token_embeddings: Mapped[List["TokenEmbedding"]] = relationship(
        "TokenEmbedding", back_populates="document", cascade="all, delete-orphan"
    )
    outgoing_links: Mapped[List["DocumentLink"]] = relationship(
        "DocumentLink",
        foreign_keys="DocumentLink.source_id",
        back_populates="source",
        cascade="all, delete-orphan",
    )
    incoming_links: Mapped[List["DocumentLink"]] = relationship(
        "DocumentLink",
        foreign_keys="DocumentLink.target_id",
        back_populates="target",
        cascade="all, delete-orphan",
    )

    @property
    def depth(self) -> int:
        """Tree depth (0 for root documents)"""
        return len(self.path) if self.path else 0

    def to_dict(self, include_embed: bool = False) -> dict[str, Any]:
        """Convert to dictionary representation"""
        result = {
            "id": self.id,
            "parent_id": self.parent_id,
            "title": self.title,
            "content": self.content,
            "structured_content": self.structured_content,
            "path": self.path,
            "depth": self.depth,
            "usetype": self.usetype,
            "produced_by": self.produced_by,
            "position": self.position,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "event_time": self.event_time.isoformat() if self.event_time else None,
            "content_hash": self.content_hash,
            "settled": self.settled,
        }
        if include_embed and self.embed is not None:
            result["embed"] = list(self.embed)
        return result


class DocumentLink(Base):
    """Graph edges between documents"""

    __tablename__ = "document_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    target_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    link_type: Mapped[str] = mapped_column(String(50), nullable=False)
    score: Mapped[float] = mapped_column(Float, default=1.0)
    link_metadata: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    # Relationships
    source: Mapped["Document"] = relationship(
        "Document", foreign_keys=[source_id], back_populates="outgoing_links"
    )
    target: Mapped["Document"] = relationship(
        "Document", foreign_keys=[target_id], back_populates="incoming_links"
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "link_type": self.link_type,
            "score": self.score,
            "metadata": self.link_metadata,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# Import here to avoid circular import: token_embedding imports Document for its
# relationship, so this cannot move to the top of the file.
from jmfts_core.models.token_embedding import TokenEmbedding  # noqa: E402
