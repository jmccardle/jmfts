"""Search contracts — request/response models for the search surface.

Moved out of ``api/schemas.py`` so the service layer (``jmfts_core.services``) can
depend on them without importing FastAPI. ``api/schemas.py`` re-exports these names.
"""

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

from jmfts_client.contracts.document import DocumentResponse


class HybridSearchRequest(BaseModel):
    """Hybrid search request"""

    query: str
    limit: int = Field(default=10, ge=1, le=100)
    methods: list[str] = Field(default=["vector", "fulltext", "bm25"])
    weights: Optional[dict[str, float]] = None
    usetype: Optional[str] = None
    exclude_types: Optional[list[str]] = Field(
        default=None,
        description="Exclude documents with these usetypes. When omitted, the server default applies (entity, summary). Pass [] to disable all exclusion.",
    )
    parent_id: Optional[int] = None
    index_name: str = "default"
    # Generative-Agents scoring terms, applied multiplicatively after RRF fusion.
    # Both default to 0.0 = off, leaving plain RRF untouched; callers opt in.
    recency_weight: float = Field(
        default=0.0,
        ge=0.0,
        description="Strength of the recency term (0.0 = off). Decays on COALESCE(event_time, created_at).",
    )
    importance_weight: float = Field(
        default=0.0,
        ge=0.0,
        description="Strength of the importance term (0.0 = off). Reads structured_content['importance'] on the 1-10 scale.",
    )
    recency_halflife_days: float = Field(
        default=7.0,
        gt=0.0,
        description="Age at which the recency factor halves. Only consulted when recency_weight is non-zero.",
    )
    now: Optional[datetime] = Field(
        default=None,
        description="Reference time for recency decay; defaults to the current UTC time. Pass it explicitly when a corpus has its own timeline, so results are reproducible.",
    )
    as_of: Optional[datetime] = Field(
        default=None,
        description="Point-in-time retrieval cutoff: only return documents whose domain clock COALESCE(event_time, created_at) <= as_of. Naive datetimes are read as UTC. Orthogonal to `now` (which reweights by recency); `as_of` filters visibility. Off when omitted.",
    )


class SearchResultItem(BaseModel):
    """Single search result"""

    document: DocumentResponse
    score: float
    method: str


class SearchResponse(BaseModel):
    """Search response"""

    results: list[SearchResultItem]
    total: int
    latency_ms: float


class SearchRequest(BaseModel):
    """Search request"""

    query: str
    limit: int = Field(default=10, ge=1, le=100)
    usetype: Optional[str] = None
    exclude_types: Optional[list[str]] = Field(
        default=None,
        description="Exclude documents with these usetypes. When omitted, the server default applies (entity, summary). Pass [] to disable all exclusion.",
    )
    parent_id: Optional[int] = None
    threshold: float = Field(default=0.0, ge=0.0, le=1.0)
    as_of: Optional[datetime] = Field(
        default=None,
        description="Point-in-time retrieval cutoff: only return documents whose domain clock COALESCE(event_time, created_at) <= as_of. Naive datetimes are read as UTC. Off when omitted.",
    )


class AutoSearchRequest(BaseModel):
    """Auto search request — heuristic router picks the best method"""

    query: str
    limit: int = Field(default=10, ge=1, le=100)
    usetype: Optional[str] = None
    exclude_types: Optional[list[str]] = Field(
        default=None,
        description="Exclude documents with these usetypes. When omitted, the server default applies (entity, summary). Pass [] to disable all exclusion.",
    )
    parent_id: Optional[int] = None


class RoutingMetadata(BaseModel):
    """Metadata about which search method was selected and why"""

    method: str
    reason: str
    signals: dict[str, Any]


class AutoSearchResponse(BaseModel):
    """Auto search response with routing metadata"""

    results: list[SearchResultItem]
    total: int
    latency_ms: float
    routing: RoutingMetadata


class SynthesizeRequest(BaseModel):
    """Request to synthesize an answer from search results"""

    query: str
    search_method: str = Field(
        default="auto",
        description="Search method: auto, hybrid, maxsim, bm25, vector, fulltext",
    )
    top_k: int = Field(default=5, ge=1, le=20)
    max_context_tokens: int = Field(default=4096, ge=256, le=32768)
    llm_model: Optional[str] = Field(default=None, description="Override the default LLM model")
    usetype: Optional[str] = None
    parent_id: Optional[int] = None


class SourceReference(BaseModel):
    """A source document referenced in the synthesis"""

    document_id: int
    title: Optional[str]
    score: float
    method: str


class SynthesizeResponse(BaseModel):
    """Synthesis response with generated text and source references"""

    synthesis: str
    sources: list[SourceReference]
    llm_model: str
    search_latency_ms: float
    total_latency_ms: float
    llm_available: bool = True
