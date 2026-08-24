"""Document contract — the one DocumentResponse, plus the one ORM→response converter.

Before unification there were FOUR hand-written ``Document -> DocumentResponse``
converters (documents.py, search.py, triples.py, templates.py). Three of them dropped
``position`` and ``event_time``, so the same document serialised differently depending
on which endpoint returned it — the field-drop bug. ``from_document`` is now the single
source; every surface calls it, so the shape cannot drift again.

Why an explicit converter and not ``model_validate(doc)`` with ``from_attributes``:
``Document.embed`` is a raw pgvector ``Vector(768)`` that comes back as a numpy array of
``numpy.float32``. ``model_validate`` would (a) pull that heavy vector into every response
and (b) fail pydantic's ``list[float]`` validation on the numpy scalars. The converter
keeps ``embed`` opt-in and coerces with ``float(x)``.
"""

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class DocumentResponse(BaseModel):
    """Document response"""

    id: int
    parent_id: Optional[int]
    title: Optional[str]
    content: Optional[str]
    structured_content: dict
    path: list
    depth: int
    usetype: Optional[str]
    position: Optional[int] = None  # CR-1: NULL for unordered documents
    created_at: Optional[datetime]
    updated_at: Optional[datetime]
    event_time: Optional[datetime] = None  # domain time; NULL = authored in place
    content_hash: Optional[str]
    # Ingest lifecycle: 'in_flight' | 'settled' | 'failed'. Read-only on this surface —
    # there is no API verb that sets it yet, and retrieval already excludes everything
    # that is not 'settled', so a caller who sees a document in a search result sees
    # 'settled'. It is still worth serialising: a direct GET of a node mid-ingestion is
    # the one place a client can tell "not ready" from "this is the answer".
    settled: str = "settled"
    embed: Optional[list[float]] = None

    class Config:
        from_attributes = True

    @classmethod
    def from_document(cls, doc, *, include_embed: bool = False) -> "DocumentResponse":
        """Convert a ``Document`` ORM row to a response.

        The single source of truth for this mapping. ``include_embed`` gates the
        768-dim vector (default off); when on, values are coerced with ``float(x)``
        because pgvector hands back numpy ``float32`` that pydantic rejects for
        ``Optional[list[float]]``.
        """
        return cls(
            id=doc.id,
            parent_id=doc.parent_id,
            title=doc.title,
            content=doc.content,
            structured_content=doc.structured_content or {},
            path=doc.path or [],
            depth=doc.depth,
            usetype=doc.usetype,
            position=doc.position,
            created_at=doc.created_at,
            updated_at=doc.updated_at,
            event_time=doc.event_time,
            content_hash=doc.content_hash,
            settled=doc.settled,
            embed=(
                [float(x) for x in doc.embed] if include_embed and doc.embed is not None else None
            ),
        )


# ============================================================================
# Document CRUD Schemas
# ============================================================================


class DocumentCreate(BaseModel):
    """Request to create a document"""

    title: Optional[str] = None
    content: Optional[str] = None
    parent_id: Optional[int] = None
    usetype: Optional[str] = None
    structured_content: Optional[dict] = None
    auto_embed: bool = True
    # Whether auto_embed also generates token-level (maxsim) vectors. True (default)
    # embeds both the document vector (8192-token window) and token vectors, which
    # are bounded far lower (embedding_token_window, 512) by attention memory — so a
    # document over ~512 tokens would raise TextTooLongError with no way to opt out.
    # Pass False for a container that holds a whole over-window source text: it still
    # gets a full document vector, it just skips the token vectors it cannot fit
    # (mirrors DocumentRepository.create's embed_tokens). See KNOWN-DEFECTS D1.
    embed_tokens: bool = True
    # CR-1 sibling ordering. None (default) inherits ordered-ness from the parent;
    # True auto-assigns the next sibling position; False leaves position NULL.
    sequential: Optional[bool] = None
    # Domain time: when the thing this document records actually happened, as opposed
    # to when the row was created. Set it when importing content whose ingest time
    # carries no signal — transcript turns, backfills, benchmark corpora — where every
    # row would otherwise share one created_at and any recency ordering would just be
    # reading ingest order back. None (default) for content authored in place; readers
    # use COALESCE(event_time, created_at), so None keeps the legacy behaviour.
    event_time: Optional[datetime] = None


class DocumentUpdate(BaseModel):
    """Request to update a document"""

    title: Optional[str] = None
    content: Optional[str] = None
    usetype: Optional[str] = None
    structured_content: Optional[dict] = None
    re_embed: bool = True
    parent_id: Optional[int] = Field(
        default=None,
        description=(
            "Move the document (and its whole subtree) under this parent — the first-class "
            "reparent verb. Rewrites path/position and rejects cycles. None leaves the parent "
            "unchanged; there is no move-to-root via this field."
        ),
    )


# ============================================================================
# Tree Schemas
# ============================================================================


class SubtreeResponse(BaseModel):
    """Subtree response"""

    root: DocumentResponse
    descendants: list[DocumentResponse]
    total: int


# ============================================================================
# Link Schemas
# ============================================================================


class LinkCreate(BaseModel):
    """Request to create a document link"""

    source_id: int
    target_id: int
    link_type: str
    score: float = 1.0
    metadata: Optional[dict] = None


class LinkResponse(BaseModel):
    """Link response"""

    id: int
    source_id: int
    target_id: int
    link_type: str
    score: float
    metadata: dict
    created_at: Optional[datetime]

    class Config:
        from_attributes = True


# ============================================================================
# Token Embedding Schemas
# ============================================================================


class TokenEmbeddingResponse(BaseModel):
    """Token embedding info"""

    token_idx: int
    token_text: str
    importance_score: float
    has_embed_256: bool
    # has_embed_384 / has_embed_512 removed: settings.token_embed_dims is [256] and the
    # 384/512 columns were dropped from TokenEmbedding. Advertising them meant the router
    # read attributes that do not exist, 500-ing this endpoint for every document that had
    # token embeddings at all.


class DocumentTokensResponse(BaseModel):
    """Document with token embeddings"""

    document_id: int
    title: Optional[str]
    token_count: int
    tokens: list[TokenEmbeddingResponse]


# ============================================================================
# Spreadsheet regions — OFFICE_SPEC.md Part 7, `GET /documents/{id}/cells`
# ============================================================================


class CellNoteResponse(BaseModel):
    """What one cell carries beyond its value. Sparse: most cells carry nothing.

    ``formula_shared`` marks a cell in a shared-formula group, where Excel writes the text
    once on the group's master cell. ``formula`` is then the MASTER's text with the master's
    references, not this cell's — the flag says so rather than letting a consumer read a
    translated formula that was never translated.

    ``text_forced`` is the leading apostrophe: the author declared this cell text. Nothing
    acts on it. It is here so that a consumer comparing ``"0012345"`` against the integer
    ``12345`` can see why the two do not match.
    """

    formula: Optional[str] = None
    formula_shared: bool = False
    text_forced: bool = False


class CellRowResponse(BaseModel):
    """One row of the region, at its worksheet row number.

    ``row`` is the number Excel shows down the left edge, not a position in ``rows``: the
    rows of a region that hold nothing are not returned, so positions are not contiguous and
    were never the address.

    ``values`` is positional across the REGION, left to right, one entry per entry of
    ``columns`` — ``values[0]`` is the region's first column, which is column A only when the
    region starts there. ``null`` is an empty cell.
    """

    row: int
    values: list[Any]


class DocumentCellsResponse(BaseModel):
    """A spreadsheet region: its values, its formulas, and it rendered as a table.

    ``ref`` is the region that was actually SERVED, always in the two-ended form
    (``B4:B4`` for a single cell), so a caller comparing it against what it asked for does
    not have to normalise. ``ref_source`` says where it came from: ``request`` when the
    caller named it, ``anchor`` when it came from the node's own ``cells`` anchor
    (``OFFICE_SPEC.md`` Part 5), ``used_range`` when it is the whole of what the sheet was
    measured to hold.

    ``cells`` is keyed by A1 cell reference and holds only the cells that carry a formula or
    a forced-text flag. Absent is the normal case; a note per cell would be a second copy of
    the region made of mostly-empty records.

    ``cell_count`` is the region's AREA — rows times columns, the number the size limit is
    applied to — and not the number of cells that hold a value. It is the price of the
    request, so it is reported in the units the request was priced in.
    """

    document_id: int
    sheet: str
    ref: str
    ref_source: str
    #: Column letters, left to right: ``["B", "C", ..., "H"]``. One per entry of each row's
    #: ``values``.
    columns: list[str]
    rows: list[CellRowResponse]
    cells: dict[str, CellNoteResponse]
    #: The region as a markdown grid: the header row holds the column letters and the first
    #: column holds the worksheet row number. A region holding nothing is the header and
    #: separator alone — the columns that were asked for, and no row claiming to be a row.
    markdown: str
    row_count: int
    cell_count: int


# ============================================================================
# Structural Splitting Schemas
# ============================================================================


class StructuralSplitRequest(BaseModel):
    """Request to split a document on markdown heading boundaries"""

    auto_embed: bool = Field(
        default=True,
        description="Whether to auto-embed the created child documents",
    )
    usetype: Optional[str] = Field(
        default=None,
        description="Usetype to assign to child section documents",
    )


class StructuralSplitSectionItem(BaseModel):
    """A section produced by structural splitting"""

    document_id: int
    title: str
    level: int
    content_length: int
    source_line: int


class StructuralSplitResponse(BaseModel):
    """Result of structural splitting a document on headings"""

    parent_id: int
    total_sections: int
    had_headings: bool
    sections: list[StructuralSplitSectionItem]


# ============================================================================
# Segmentation Schemas
# ============================================================================


class SegmentRequest(BaseModel):
    """Request to segment a document's children by topic"""

    penalty: float = Field(
        default=1.0,
        gt=0.0,
        description="PELT penalty — higher values produce fewer, coarser segments",
    )
    min_size: int = Field(
        default=2,
        ge=1,
        description="Minimum segment length for PELT algorithm constraint",
    )
    constructive: bool = Field(
        default=False,
        description="If true, create interim container documents and re-parent chunks",
    )
    min_segment: int = Field(
        default=3,
        ge=1,
        description="Merge segments smaller than this (constructive mode)",
    )
    max_segment: int = Field(
        default=10,
        ge=2,
        description="Split segments larger than this (constructive mode)",
    )


class SegmentItem(BaseModel):
    """A single topic segment"""

    start: int
    end: int
    child_ids: list[int]
    size: int
    container_id: Optional[int] = Field(
        default=None,
        description="ID of the created container document (constructive mode only)",
    )


class SegmentResponse(BaseModel):
    """Segmentation result for a parent document"""

    document_id: int
    total_children: int
    num_segments: int
    constructive: bool = False
    segments: list[SegmentItem]


# ============================================================================
# Chunking Schemas
# ============================================================================


class ChunkRequest(BaseModel):
    """Request to chunk a document into container + children"""

    strategy: str = Field(
        default="sentence",
        description="Chunking strategy: sentence, paragraph, or token_count",
    )
    max_tokens: int = Field(
        default=200,
        ge=1,
        description="Max words per chunk (token_count strategy only)",
    )
    overlap: int = Field(
        default=0,
        ge=0,
        description="Word overlap between chunks (token_count strategy only)",
    )
    min_chunk_length: int = Field(
        default=1,
        ge=1,
        description="Minimum character length per chunk; shorter chunks merge with previous",
    )
    max_chars: Optional[int] = Field(
        default=None,
        ge=1,
        description=(
            "Hard upper bound on chunk size, enforced for every strategy after "
            "splitting and after merging. Defaults to settings.chunk_max_chars. "
            "Chunks exist to be embedded, so this is what keeps them inside the "
            "embedder's window; max_tokens is a target, this is the guarantee."
        ),
    )
    child_usetype: str = Field(
        default="chunk",
        description="Usetype to assign to child chunk documents",
    )
    container_usetype: Optional[str] = Field(
        default=None,
        description="Usetype to set on the container document (None keeps existing)",
    )
    auto_embed: bool = Field(
        default=True,
        description="Whether to auto-embed each chunk",
    )


class ChunkItem(BaseModel):
    """A single created chunk"""

    document_id: int
    index: int
    char_start: int
    char_end: int
    length: int


class ChunkResponse(BaseModel):
    """Chunking result for a document"""

    container_id: int
    strategy: str
    num_chunks: int
    chunks: list[ChunkItem]


# ============================================================================
# RAPTOR Summarization Schemas
# ============================================================================


class RaptorRequest(BaseModel):
    """Request to run RAPTOR hierarchical summarization on a document's children"""

    max_depth: int = Field(default=5, ge=1, le=20, description="Maximum recursion depth")
    min_cluster_size: int = Field(
        default=2, ge=2, description="Minimum documents per Leiden cluster"
    )
    llm_model: Optional[str] = Field(
        default=None, description="Override the default LLM model for summarization"
    )
    max_summary_tokens: Optional[int] = Field(
        default=None, ge=64, le=4096, description="Max tokens per summary"
    )
    usetype_filter: Optional[str] = Field(
        default=None,
        description="Only cluster children with this usetype (e.g. 'summary')",
    )


class PortfolioRaptorRequest(BaseModel):
    """Request to run cross-document RAPTOR over report-level summaries in a portfolio"""

    max_depth: int = Field(default=5, ge=1, le=20, description="Maximum recursion depth")
    min_cluster_size: int = Field(
        default=2, ge=2, description="Minimum documents per Leiden cluster"
    )
    llm_model: Optional[str] = Field(
        default=None, description="Override the default LLM model for summarization"
    )
    max_summary_tokens: Optional[int] = Field(
        default=None, ge=64, le=4096, description="Max tokens per summary"
    )


class RaptorLayerItem(BaseModel):
    """Result of a single RAPTOR layer"""

    layer: int
    clusters: int
    summary_ids: list[int]
    bridge_links_created: int


class RaptorResponse(BaseModel):
    """Result of RAPTOR hierarchical summarization"""

    root_id: int
    layers: list[RaptorLayerItem]
    total_summaries: int
    total_bridge_links: int


# ============================================================================
# Fact Extraction Schemas (#58)
# ============================================================================


class FactExtractionRequest(BaseModel):
    """Request to extract knowledge triples from a document's segments"""

    llm_model: Optional[str] = Field(default=None, description="Override the extraction LLM model")
    max_facts: Optional[int] = Field(
        default=None, ge=1, le=20, description="Max triples per segment (default from settings)"
    )
    confidence_threshold: Optional[float] = Field(
        default=None, ge=0.0, le=1.0, description="Min confidence to keep a triple"
    )
    include_summaries: bool = Field(
        default=True, description="Also extract from RAPTOR summary documents"
    )


class SegmentExtractionItem(BaseModel):
    """Extraction result for one segment/chunk"""

    source_document_id: int
    triples_created: int
    skipped: int
    triple_ids: list[int]
    errors: list[str]


class FactExtractionResponse(BaseModel):
    """Result of fact extraction pipeline"""

    root_document_id: int
    documents_processed: int
    total_triples_created: int
    total_skipped: int
    entities_created: int
    entities_resolved: int
    predicates_created: int
    extractions: list[SegmentExtractionItem]
    errors: list[str]
