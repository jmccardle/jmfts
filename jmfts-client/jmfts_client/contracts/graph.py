"""Graph-analytics contracts — request/response models for the graph surface.

Moved out of ``api/schemas.py`` so the service layer (``jmfts_core.services``) can
depend on them without importing FastAPI. ``api/schemas.py`` re-exports these names.

These are all plain aggregate/analytics shapes (centrality, subtree-authority, spines,
communities, diff, stats, lint) — none of them carries an ORM document, so there is no
``Document→DocumentResponse`` converter involved on this surface.
"""

from typing import Literal, Optional

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


# -- seeded rank ----------------------------------------------------------------


class Seed(BaseModel):
    """A document the walk starts from, and how much of the restart mass it gets."""

    document_id: int
    weight: float = Field(
        default=1.0,
        gt=0,
        description="Relative share of the restart mass. Pass a search score here.",
    )


class SeededRankRequest(BaseModel):
    """Personalized PageRank from a handful of documents, over the few hops around them.

    Global centrality answers "what is central in this corpus". This answers "what is
    central to THESE documents": the walk restarts at the seeds, so a node scores by what
    points at it from near them. Seeds are ids rather than a query, so the verb composes
    with any search method — search, then pass the hits and their scores.
    """

    seeds: list[Seed] = Field(min_length=1, max_length=100)
    scope: Literal["links", "triples", "both"] = Field(
        default="links",
        description=(
            "Which edges the walk follows. `both` sums a link's score with 1.0 per triple, "
            "a weight with no unit, so prefer one table unless both are wanted."
        ),
    )
    relations: Optional[list[str]] = Field(
        default=None,
        min_length=1,
        description=(
            "Walk only these relations: `link_type` values for links, predicate names for "
            "triples, both for `both`. A name that is neither is refused, not ignored. "
            "Omit to walk every relation."
        ),
    )
    hops: int = Field(
        default=2,
        ge=0,
        le=4,
        description="How far from a seed, ignoring edge direction, the subgraph reaches.",
    )
    damping: float = Field(
        default=0.85,
        gt=0,
        lt=1,
        description="Probability of following an edge rather than restarting at a seed.",
    )
    parent_id: Optional[int] = Field(
        default=None, description="Confine the graph to this subtree before seeding."
    )
    exclude_usetypes: Optional[list[str]] = Field(
        default=None, description="Documents of these usetypes are not vertices."
    )
    top: int = Field(default=20, ge=1, le=200)


class SeededRankItem(BaseModel):
    document_id: int
    title: Optional[str]
    usetype: Optional[str]
    depth: int = Field(description="Depth in the document tree, as on centrality.")
    score: float
    is_seed: bool
    hops: int = Field(description="Undirected edge distance to the nearest seed.")
    in_degree: int = Field(description="Within the seeded subgraph.")
    out_degree: int = Field(description="Within the seeded subgraph.")


class SeededRankResponse(BaseModel):
    """Response for /graph/seeded-rank.

    ``subgraph_vertices`` and ``subgraph_edges`` describe the graph the scores were computed
    on — the seeds' neighbourhood, not the corpus.
    """

    scope: str
    relations: Optional[list[str]]
    hops: int
    parent_id: Optional[int]
    subgraph_vertices: int
    subgraph_edges: int
    results: list[SeededRankItem]


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
    """Response for GET /graph/neighbors.

    ``truncated`` is a property of the GRAPH, not of the request. It says the walk stopped
    with reachable nodes still behind it, which is the one thing a caller cannot work out
    from the page in front of them — and it is measured by the walk overshooting its node
    cap by exactly one, not inferred from ``total``. An over-large ``limit`` or
    ``max_depth`` is refused at the wire (422) rather than clamped, so this flag never has
    to double as "your bound was overruled".

    Depth is not truncation. A caller who asked for ``max_depth`` hops and received every
    node within ``max_depth`` hops was answered; depth is an ordinary predicate on the
    result set, the way ``link_types`` is.
    """

    root_id: int
    max_depth: int
    direction: str
    link_types: Optional[list[str]]
    total: int
    #: A ``limit + 1``th neighbour exists within ``max_depth`` — this page is a cut of the
    #: reachable set. Measured by the walk, never inferred from ``total``.
    truncated: bool
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
