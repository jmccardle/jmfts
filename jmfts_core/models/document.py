"""Document and DocumentLink models"""

from datetime import datetime
from typing import Optional, List, Any
from sqlalchemy import (
    CheckConstraint,
    Index,
    String,
    Text,
    Integer,
    Float,
    ForeignKey,
    DateTime,
    text,
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

#: A summary written by a model, or a roll-up standing in for the nodes it names. Held out
#: of every default result set by `Settings.search_exclude_usetypes` and
#: `bm25_exclude_usetypes`, which is right for the two things that carry it: a
#: `summarize:tree` node's vector is byte-identical to its source node's (13,755 of 13,755
#: pairs at cosine distance 0.000000 on the reference corpus), so admitting them would
#: return every answer twice, and an LLM summary's text is reachable on the node it
#: summarises through `effective_content`.
USETYPE_SUMMARY = "summary"

#: What `profile:sheet` writes under a sheet — the measured description of a worksheet
#: (`INGEST_SPEC.md` 8.5): its columns, their cardinalities, which one identifies a row.
#:
#: SPLIT FROM `USETYPE_SUMMARY` on 2026-09-07 and this is a behaviour change. It used to be
#: `summary`, on the reasoning that "a profile is a summary of the node above it, it is
#: retrieved the same way". The second half was false: `summary` is in both exclusion lists,
#: so the profile was retrieved no way at all — while 8.5 says outright that it "is embedded
#: and retrievable like any other node" and that "a retrieval hit on the profile tells the
#: agent which columns exist". The spec and the shipped config disagreed and the config won.
#:
#: The two do not belong together. A profile is MEASURED — counted from cells, no model, no
#: inference about meaning (8.5's factoid table) — and it is the only text a header-less
#: sheet produces at all: 18 of 33 sheets on the reference corpus wrote zero records, and
#: their profile is what the appliance knows about them. A summary is authored by a model or
#: is a duplicate of something else's vector. Sharing one string made the first invisible
#: for the second's reasons.
USETYPE_PROFILE = "profile"

#: One row of a worksheet, as typed JSON (8.4's `records` shape). `record` and not `row`:
#: what the node holds is one instance of whatever the sheet is a table of, and its row
#: NUMBER is a fact about where it was found.
#:
#: A record is a LEAF while its prose fits the token/maxsim window and a CONTAINER when it
#: does not, which is the same rule `section` follows one level up. See
#: `jmfts_core.sheet_records.plan_record`.
USETYPE_RECORD = "record"

#: One column of one row, on the node it gets when the row it belongs to was too long to
#: embed whole. Like `record` it is a leaf while its own labelled text fits and a container
#: over `chunk` nodes when it does not.
#:
#: NOT `chunk`, though a cell that fits is a text leaf exactly as a chunk is. A chunk is a
#: piece of prose whose boundary this appliance chose; a cell is a field the SHEET named,
#: and its `cell` evidence carries the column name and the typed value that `record` keeps
#: for the whole row. Sharing one string would make "which column is this" underivable for
#: the one node kind that knows the answer — the mistake `USETYPE_PROFILE` was split out of
#: `USETYPE_SUMMARY` to undo.
USETYPE_CELL = "cell"

#: The whole worksheet as one markdown table, in one node (8.4's `small_table`). The shape
#: 8.4 lists FIRST and the one nothing had ever built: `run_extract_sheet` branched on
#: `header_row` alone, so a sheet either became rows or became nothing, and the markdown
#: `profile:sheet` rendered was counted, reported as a boolean and discarded.
#:
#: NOT `section` and not `chunk`, though it is a text leaf like a chunk. A chunk is a piece
#: of prose whose boundary this appliance chose and a section is a region the document
#: named; this is a whole sheet kept WHOLE, on the argument 8.4 makes for the shape — "a
#: small table is often exactly the retrieval unit we want, and splitting it destroys it" —
#: so the one thing a reader has to be able to ask of it is whether it is entire. Sharing a
#: string with either would make that unanswerable.
#:
#: A `table` node and the `record` nodes of the same sheet routinely BOTH exist, and neither
#: contains the other: they are two renderings of one sheet, they are separate documents, and
#: a query matching both returns both. See `jmfts_core.sheet_records.BOTH_SHAPES_BASIS`.
USETYPE_TABLE = "table"


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


#: The edge from a summary node down to one node it covers.
#:
#: ON THE MODEL FOR `USETYPE_SHEET`'s REASON, and it arrived here the same way: it was
#: written twice. `raptor_summarize` (`summarization.py`) spelled it as a literal and
#: `rollup_tasks.SUMMARIZES_LINK_TYPE` spelled it as a constant, which is the copy-drift
#: `USETYPE_SHEET` was consolidated to end. Two derivations write this ONE edge type
#: deliberately: `SPRINT_0_5_0.md` 3.1's leaf projection resolves a derived node to source
#: leaves by following one type, and a second spelling would make it ask which derivation
#: produced a node before it could follow anything. A constant in one writer cannot hold
#: that invariant, because the other writer is where it breaks.
SUMMARIZES_LINK_TYPE = "summarizes"


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

    # WHICH RULE PRODUCED THIS EDGE. NULL means asserted — a person, an importer or an
    # ingest handler wrote it — exactly as it does on `Triple.derived_by`
    # (`models/triple.py:167`), and "asserted only" is then `WHERE derived_by IS NULL`.
    # `VARCHAR(200)` matches the triple column rather than `Document.produced_by`'s 100: a
    # rule identity is one string, and a link and a triple that one rule produced must be
    # findable under the same name.
    #
    # What it buys is stated in `docs/SPRINT_0_5_0.md` Block D step 15: `WHERE derived_by =
    # :rule` is a complete description of what one rule produced, so re-derivation is a
    # delete-then-insert rather than a diff.
    #
    # ONE writer stamps it, and IT REBUILDS. `summarize:tree` (Block C step 11,
    # `rollup_tasks._rewrite_member_links`) sets `summarize:tree` on the `summarizes` edges a
    # roll-up mints, and a second run over a changed member set DELETES that node's edges and
    # writes them again. An earlier draft of this comment said the column had no rebuilder
    # yet and that Part 3.1's reprojection would be the first; that was true when it was
    # written and false by the end of the same pass, and Block D corrects it in its own text.
    #
    # The delete is scoped to one derived node's outgoing edges, NOT to the rule:
    # `DocumentRepository.rederive_links(rule, links)` deletes everything a rule produced
    # across the whole store, and `summarize:tree` produces edges for every container in it,
    # so a per-node rebuild through that method would delete every other node's edges. Block
    # C finding 5 records the mismatch; `rederive_links` is uncalled until a rule identity
    # carries a scope.
    #
    # EVERY OTHER WRITER LEAVES IT NULL, which is the asserted-edge case and is correct:
    # each writes once at ingest and never rebuilds, so there is no rule identity to record.
    # `bridge` (`summarization.py:354`, `:624`), `summarizes` from RAPTOR
    # (`summarization.py:472`), `LINK_CONTAINS` (`services/ingest_service.py:918`), and
    # `MENTIONS_LINK_TYPE` plus `RBAC_COREF_LINK_TYPE`, both through
    # `fact_extraction._upsert_link` (`:401`).
    #
    # THAT IS FIVE TYPES, NOT FOUR. Block D step 15 counts four and names `bridge`,
    # `summarizes`, `contains` and `mentions`; `rbac_coref` is a fifth, written by
    # `resolve_entity` when an entity gets a copy under a second entities root. It leaves
    # the column NULL for the same reason as the rest, so the count is the only thing wrong
    # and nothing follows from it — but the number is corrected here rather than copied.
    # See migration 019.
    derived_by: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    # Relationships
    source: Mapped["Document"] = relationship(
        "Document", foreign_keys=[source_id], back_populates="outgoing_links"
    )
    target: Mapped["Document"] = relationship(
        "Document", foreign_keys=[target_id], back_populates="incoming_links"
    )

    # ONE ENTRY, and the rest of this table's DDL is deliberately not mirrored here. The
    # class never declared its UNIQUE (source_id, target_id, link_type) or its source/target
    # indexes — `sql/schema.sql` is where this table is defined and the mapper is not the
    # authority — so adding them alongside the new index would be inventing constraint names
    # the database does not use. The partial index is declared because it is the half of
    # step 15 that has to exist for `WHERE derived_by = :rule` to be cheap, and because
    # `Triple` declares its counterpart at `models/triple.py:222` in exactly this shape.
    __table_args__ = (
        # Partial, for `ix_triples_derived_by`'s reason and NOT `documents.produced_by`'s:
        # derived edges are the minority of a link graph, so the index holds only them and
        # the asserted majority is served by not being in it.
        Index("ix_links_derived_by", "derived_by", postgresql_where=text("derived_by IS NOT NULL")),
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "link_type": self.link_type,
            "score": self.score,
            "metadata": self.link_metadata,
            "derived_by": self.derived_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# Import here to avoid circular import: token_embedding imports Document for its
# relationship, so this cannot move to the top of the file.
from jmfts_core.models.token_embedding import TokenEmbedding  # noqa: E402
