"""Graph-analytics contracts — request/response models for the graph surface.

Moved out of ``api/schemas.py`` so the service layer (``jmfts_core.services``) can
depend on them without importing FastAPI. ``api/schemas.py`` re-exports these names.

These are all plain aggregate/analytics shapes (centrality, subtree-authority, spines,
communities, diff, stats, lint) — none of them carries an ORM document, so there is no
``Document→DocumentResponse`` converter involved on this surface.
"""

from typing import Optional

from pydantic import BaseModel, Field

# -- centrality ---------------------------------------------------------------


class CentralityScoreItem(BaseModel):
    """A single document's centrality score."""

    document_id: int
    title: Optional[str]
    usetype: Optional[str]
    depth: int
    score: float
    in_degree: int
    out_degree: int


class CentralityResponse(BaseModel):
    """Response for /graph/centrality."""

    metric: str
    scope: str
    parent_id: Optional[int]
    total_vertices: int
    total_edges: int
    results: list[CentralityScoreItem]


# -- subtree authority --------------------------------------------------------


class TopDescendantItem(BaseModel):
    document_id: int
    title: Optional[str]
    usetype: Optional[str]
    contribution: float


class SubtreeAuthorityItem(BaseModel):
    document_id: int
    title: Optional[str]
    usetype: Optional[str]
    depth: int
    subtree_size: int
    own_centrality: float
    descendant_authority: float
    spread: int
    spread_bonus: float
    authority: float
    top_descendants: list[TopDescendantItem]


class SubtreeAuthorityResponse(BaseModel):
    metric: str
    scope: str
    decay: float
    min_subtree_size: int
    parent_id: Optional[int]
    results: list[SubtreeAuthorityItem]


# -- spines -------------------------------------------------------------------


class SpineBranchAlternative(BaseModel):
    document_id: int
    title: Optional[str]
    subtree_score: float


class SpineBranchPoint(BaseModel):
    at_document_id: int
    chosen: int
    alternatives: list[SpineBranchAlternative]


class SpineItem(BaseModel):
    path: list[int]
    titles: list[Optional[str]]
    usetypes: list[Optional[str]]
    total_score: float
    branch_points: list[SpineBranchPoint]


class SpineResponse(BaseModel):
    root_id: int
    metric: str
    scope: str
    branching_threshold: float
    paths: list[SpineItem]


# -- communities --------------------------------------------------------------


class CommunityMember(BaseModel):
    document_id: int
    title: Optional[str]
    usetype: Optional[str]


class CommunityItem(BaseModel):
    community_id: int
    size: int
    cohesion: float
    members: list[CommunityMember]


class CommunityResponse(BaseModel):
    scope: str
    resolution: float
    total_communities: int
    results: list[CommunityItem]


# -- neighbors (link-graph traversal) -----------------------------------------


class NeighborItem(BaseModel):
    """A document reached while traversing the link graph from a root."""

    document_id: int
    title: Optional[str]
    usetype: Optional[str]
    depth: int
    parent_document_id: int
    via_link_id: int
    via_link_type: str
    direction: str  # sense of the reaching edge: "outgoing" | "incoming"


class NeighborsResponse(BaseModel):
    """Response for GET /graph/neighbors."""

    root_id: int
    max_depth: int
    direction: str
    link_types: Optional[list[str]]
    total: int
    truncated: bool  # True if `limit` capped the walk before it ran dry
    neighbors: list[NeighborItem]


# -- diff / stats -------------------------------------------------------------


class GraphDiffResponse(BaseModel):
    since: Optional[str]
    until: Optional[str]
    new_documents: int
    new_links: int
    new_triples: int
    superseded_triples: int


class GraphStatsResponse(BaseModel):
    total_documents: int
    total_links: int
    total_triples: int
    invalidated_triples: int
    by_usetype: dict[str, int]
    by_link_type: dict[str, int]
    by_fact_type: dict[str, int]


# -- lint ---------------------------------------------------------------------


class LintRequest(BaseModel):
    """Request body for POST /graph/lint."""

    scope: str = Field(default="links", description="links | triples | both")
    parent_id: Optional[int] = None
    exclude_usetypes: Optional[list[str]] = None
    orphan_threshold: int = Field(default=1, ge=0, description="Degree <= this counts as orphan")
    stale_threshold_days: int = Field(default=90, ge=1)
    coverage_top_k: int = Field(default=20, ge=1, le=200)
    include_summaries_usetype: list[str] = Field(default_factory=lambda: ["summary"])


class LintFinding(BaseModel):
    category: str  # orphan | contradiction | stale | coverage
    severity: str  # info | warning | error
    document_ids: list[int] = Field(default_factory=list)
    triple_ids: list[int] = Field(default_factory=list)
    message: str
    detail: dict = Field(default_factory=dict)


class LintResponse(BaseModel):
    scope: str
    parent_id: Optional[int]
    findings: list[LintFinding]
    counts: dict[str, int]
