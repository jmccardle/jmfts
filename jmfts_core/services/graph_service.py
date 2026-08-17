"""GraphService — graph-analytics operations, transport-neutral.

Logic lifted verbatim from ``api/routers/graph.py`` so the behaviour is identical;
the seven read-only analytics endpoints (centrality, subtree-authority, spines,
communities, diff, stats, lint) now live as ``@expose``-decorated service methods,
callable directly in-process by Tau and served over REST by the generated adapter.

These endpoints do not serialise ORM documents (they build their own aggregate item
models from the analysis results), so no ``Document→DocumentResponse`` converter is
involved on this surface.

Domain → HTTP mapping is declared per-op in ``@expose(errors=...)`` and keyed by
EXCEPTION TYPE. The one hand-written status the router raised — the ``/spines`` 404
when a root has no paths — is reproduced by raising ``LookupError`` (→ 404), with the
detail string ``"No paths from root <id>"`` preserved verbatim. All seven are reads;
none commits.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from jmfts_core.contracts.graph import (
    CentralityResponse,
    CentralityScoreItem,
    CommunityItem,
    CommunityMember,
    CommunityResponse,
    GraphDiffResponse,
    GraphStatsResponse,
    LintFinding,
    LintRequest,
    LintResponse,
    NeighborItem,
    NeighborsResponse,
    SpineBranchAlternative,
    SpineBranchPoint,
    SpineItem,
    SpineResponse,
    SubtreeAuthorityItem,
    SubtreeAuthorityResponse,
    TopDescendantItem,
)
from jmfts_core.graph_analysis import (
    build_graph,
    compute_centrality,
    compute_communities,
    compute_diff,
    compute_neighbors,
    compute_spines,
    compute_stats,
    compute_subtree_authority,
)
from jmfts_core.access import can_read
from jmfts_core.models.document import Document
from jmfts_core.lint import lint_corpus
from jmfts_core.registry import expose, register_service


def _parse_csv(value: Optional[str]) -> Optional[list[str]]:
    if not value:
        return None
    return [s.strip() for s in value.split(",") if s.strip()]


@register_service
class GraphService:
    """Graph-analytics operations over a single database session."""

    def __init__(self, session: Session):
        self.session = session

    @expose(
        "GET",
        "/graph/centrality",
        response_model=CentralityResponse,
        tags=["graph"],
        summary="Flat top-N centrality scores",
    )
    def get_centrality(
        self,
        *,
        metric: str = "pagerank",
        scope: str = "links",
        parent_id: Optional[int] = None,
        exclude_usetypes: Optional[str] = None,
        top: int = 20,
    ) -> CentralityResponse:
        """Flat top-N centrality scores."""
        excludes = _parse_csv(exclude_usetypes)
        build = build_graph(
            self.session, scope=scope, parent_id=parent_id, exclude_usetypes=excludes
        )
        scores = compute_centrality(build, metric=metric, top=top)
        return CentralityResponse(
            metric=metric,
            scope=scope,
            parent_id=parent_id,
            total_vertices=build.graph.vcount(),
            total_edges=build.graph.ecount(),
            results=[
                CentralityScoreItem(
                    document_id=s.document_id,
                    title=s.title,
                    usetype=s.usetype,
                    depth=s.depth,
                    score=s.score,
                    in_degree=s.in_degree,
                    out_degree=s.out_degree,
                )
                for s in scores
            ],
        )

    @expose(
        "GET",
        "/graph/subtree-authority",
        response_model=SubtreeAuthorityResponse,
        tags=["graph"],
        summary="Hierarchical roll-up: descendant centrality flows up the tree with decay",
    )
    def get_subtree_authority(
        self,
        *,
        metric: str = "pagerank",
        scope: str = "links",
        decay: float = 0.7,
        min_subtree_size: int = 3,
        parent_id: Optional[int] = None,
        exclude_usetypes: Optional[str] = None,
        top: int = 20,
    ) -> SubtreeAuthorityResponse:
        """Hierarchical roll-up: descendant centrality flows up the tree with decay."""
        excludes = _parse_csv(exclude_usetypes)
        results = compute_subtree_authority(
            self.session,
            scope=scope,
            parent_id=parent_id,
            exclude_usetypes=excludes,
            metric=metric,  # type: ignore[arg-type]
            decay=decay,
            min_subtree_size=min_subtree_size,
            top=top,
        )
        return SubtreeAuthorityResponse(
            metric=metric,
            scope=scope,
            decay=decay,
            min_subtree_size=min_subtree_size,
            parent_id=parent_id,
            results=[
                SubtreeAuthorityItem(
                    document_id=r.document_id,
                    title=r.title,
                    usetype=r.usetype,
                    depth=r.depth,
                    subtree_size=r.subtree_size,
                    own_centrality=r.own_centrality,
                    descendant_authority=r.descendant_authority,
                    spread=r.spread,
                    spread_bonus=r.spread_bonus,
                    authority=r.authority,
                    top_descendants=[TopDescendantItem(**d) for d in r.top_descendants],
                )
                for r in results
            ],
        )

    @expose(
        "GET",
        "/graph/spines",
        response_model=SpineResponse,
        errors={LookupError: 404},
        tags=["graph"],
        summary="Top reading paths through the subtree of root_id",
    )
    def get_spines(
        self,
        *,
        root_id: int,
        metric: str = "pagerank",
        scope: str = "links",
        branching_threshold: float = 0.85,
        max_paths: int = 5,
        max_depth: Optional[int] = None,
        exclude_usetypes: Optional[str] = None,
    ) -> SpineResponse:
        """Top reading paths through the subtree of root_id."""
        excludes = _parse_csv(exclude_usetypes)
        paths = compute_spines(
            self.session,
            root_id,
            scope=scope,
            exclude_usetypes=excludes,
            metric=metric,  # type: ignore[arg-type]
            branching_threshold=branching_threshold,
            max_paths=max_paths,
            max_depth=max_depth,
        )
        if not paths:
            # Document might not exist or have no descendants — surface a 404 so callers
            # know whether to retry with another root.
            raise LookupError(f"No paths from root {root_id}")
        return SpineResponse(
            root_id=root_id,
            metric=metric,
            scope=scope,
            branching_threshold=branching_threshold,
            paths=[
                SpineItem(
                    path=p.path,
                    titles=p.titles,
                    usetypes=p.usetypes,
                    total_score=p.total_score,
                    branch_points=[
                        SpineBranchPoint(
                            at_document_id=bp["at_document_id"],
                            chosen=bp["chosen"],
                            alternatives=[
                                SpineBranchAlternative(**alt) for alt in bp["alternatives"]
                            ],
                        )
                        for bp in p.branch_points
                    ],
                )
                for p in paths
            ],
        )

    @expose(
        "GET",
        "/graph/neighbors",
        response_model=NeighborsResponse,
        errors={LookupError: 404},
        tags=["graph"],
        summary="Link-graph neighbors of a root document (bounded BFS)",
    )
    def get_neighbors(
        self,
        *,
        root_id: int,
        max_depth: int = 2,
        direction: str = "both",
        link_types: Optional[str] = None,
        limit: int = 200,
    ) -> NeighborsResponse:
        """Link-graph neighbors of a root document (bounded BFS).

        The transitive counterpart to per-document ``get_links``: everything reachable
        from ``root_id`` within ``max_depth`` link hops, cycle-guarded and ``limit``-capped.
        ``link_types`` is an optional comma-separated allow-list of edge types.
        """
        # Subtree RBAC: an unreadable root is indistinguishable from missing (404);
        # compute_neighbors additionally hides unreadable nodes reached during the walk.
        root = self.session.get(Document, root_id)
        if root is None or not can_read(self.session, root):
            raise LookupError(f"Document {root_id} not found")
        types = _parse_csv(link_types)
        nodes = compute_neighbors(
            self.session,
            root_id,
            max_depth=max_depth,
            direction=direction,
            link_types=types,
            limit=limit,
        )
        return NeighborsResponse(
            root_id=root_id,
            max_depth=max_depth,
            direction=direction,
            link_types=types,
            total=len(nodes),
            truncated=len(nodes) >= limit,
            neighbors=[
                NeighborItem(
                    document_id=n.document_id,
                    title=n.title,
                    usetype=n.usetype,
                    depth=n.depth,
                    parent_document_id=n.parent_document_id,
                    via_link_id=n.via_link_id,
                    via_link_type=n.via_link_type,
                    direction=n.direction,
                )
                for n in nodes
            ],
        )

    @expose(
        "GET",
        "/graph/communities",
        response_model=CommunityResponse,
        tags=["graph"],
        summary="Leiden communities over the chosen edge graph",
    )
    def get_communities(
        self,
        *,
        scope: str = "links",
        parent_id: Optional[int] = None,
        exclude_usetypes: Optional[str] = None,
        resolution: float = 1.0,
        min_size: int = 2,
    ) -> CommunityResponse:
        """Leiden communities over the chosen edge graph."""
        excludes = _parse_csv(exclude_usetypes)
        build = build_graph(
            self.session, scope=scope, parent_id=parent_id, exclude_usetypes=excludes
        )
        communities = compute_communities(build, resolution=resolution, min_size=min_size)
        return CommunityResponse(
            scope=scope,
            resolution=resolution,
            total_communities=len(communities),
            results=[
                CommunityItem(
                    community_id=c.community_id,
                    size=c.size,
                    cohesion=c.cohesion,
                    members=[CommunityMember(**m) for m in c.members],
                )
                for c in communities
            ],
        )

    @expose(
        "GET",
        "/graph/diff",
        response_model=GraphDiffResponse,
        tags=["graph"],
        summary="Counts of new/changed/superseded entities in the time window",
    )
    def get_diff(
        self,
        *,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> GraphDiffResponse:
        """Counts of new/changed/superseded entities in the time window."""
        diff = compute_diff(self.session, since=since, until=until)
        return GraphDiffResponse(
            since=diff.since,
            until=diff.until,
            new_documents=diff.new_documents,
            new_links=diff.new_links,
            new_triples=diff.new_triples,
            superseded_triples=diff.superseded_triples,
        )

    @expose(
        "GET",
        "/graph/stats",
        response_model=GraphStatsResponse,
        tags=["graph"],
        summary="Corpus-wide aggregate counts",
    )
    def get_stats(self) -> GraphStatsResponse:
        """Corpus-wide aggregate counts."""
        s = compute_stats(self.session)
        return GraphStatsResponse(
            total_documents=s.total_documents,
            total_links=s.total_links,
            total_triples=s.total_triples,
            invalidated_triples=s.invalidated_triples,
            by_usetype=s.by_usetype,
            by_link_type=s.by_link_type,
            by_fact_type=s.by_fact_type,
        )

    @expose(
        "POST",
        "/graph/lint",
        response_model=LintResponse,
        tags=["graph"],
        summary="Run orphan + contradiction + stale + coverage audits in one transaction",
    )
    def post_lint(self, request: LintRequest) -> LintResponse:
        """Run orphan + contradiction + stale + coverage audits in one transaction."""
        report = lint_corpus(
            self.session,
            scope=request.scope,
            parent_id=request.parent_id,
            exclude_usetypes=request.exclude_usetypes,
            orphan_threshold=request.orphan_threshold,
            stale_threshold_days=request.stale_threshold_days,
            coverage_top_k=request.coverage_top_k,
            coverage_summary_usetypes=request.include_summaries_usetype,
        )
        return LintResponse(
            scope=report.scope,
            parent_id=report.parent_id,
            findings=[
                LintFinding(
                    category=f.category,
                    severity=f.severity,
                    document_ids=f.document_ids,
                    triple_ids=f.triple_ids,
                    message=f.message,
                    detail=f.detail,
                )
                for f in report.findings
            ],
            counts=report.counts,
        )
