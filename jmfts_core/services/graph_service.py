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

**Subtree RBAC on the analytics verbs is applied to the GRAPH, not to the answer**, and
it is applied one layer down — in ``graph_analysis._candidate_doc_query`` and
``_build_tree_index``, which are where every vertex and every title come from. There is
therefore no ``can_read`` call in ``get_centrality``, ``get_subtree_authority``,
``get_spines`` or ``get_communities``: a score computed over rows the caller cannot see
would be a number about a different graph, so the filter belongs where the graph is
built rather than where its result is serialised. ``SPRINT_0_6_0.md`` Block A step 2,
Part 4 question 4.2. ``get_neighbors`` is the exception that looks like an inconsistency
and is not: its walk is bounded by a root the caller names, so the root gets its own
``can_read`` 404 and the walk hides nodes as it reaches them.

``get_diff`` and ``get_stats`` are NOT filtered. They return counts and no document
identity, and Block A does not scope them; ``tests/test_expose_principal_audit.py``
carries that as a listed decision rather than leaving it to be rediscovered.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Optional

from pydantic import Field
from sqlalchemy.orm import Session

from jmfts_client.contracts.graph import (
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
    compute_spines,
    compute_stats,
    compute_subtree_authority,
    walk_neighbors,
)
from jmfts_core.access import can_read
from jmfts_core.models.document import Document
from jmfts_core.lint import lint_corpus
from jmfts_core.registry import expose, register_service


def _parse_csv(value: Optional[str]) -> Optional[list[str]]:
    if not value:
        return None
    return [s.strip() for s in value.split(",") if s.strip()]


# --- ceilings on the neighbour walk — SPRINT_0_5_0.md Block D finding 7 -------------
#
# The walk took both bounds from the wire with no upper limit, so a token holder could ask
# for ten million nodes: MEASURED at 17.4–20.8 s of server time in one request, with the
# whole 36,823-node graph materialised as `NeighborNode` objects (`MEASURE_TYPED_WALK.md`,
# "Where the time actually goes").
#
# `limit` is the bound that matters, because it is the only one that bounds WORK.
# `next_frontier` is built inside the loop that fills `reached`, so every frontier node is
# also a reached node and the sum of all frontier sizes is at most `limit`; total edges
# iterated is therefore at most `limit × degree` however deep the walk runs. That makes
# `max_depth` nearly free while `limit` binds, and makes `limit` the whole ceiling.
#
# 1024 is 5× the only value anything in this tree passes (200, from both in-process
# callers) and is a safety ceiling rather than a tuning knob. MEASURED 2026-09-10, uniform
# fixture, 20,000 documents and 400,000 edges, `direction="both"`, best of three:
#
#     limit         depth 2     depth 6    nodes at depth 6
#     10             37 ms       37 ms          10
#     200            93 ms       93 ms         200
#     1024          100 ms      100 ms        1024
#     4096          104 ms      370 ms        4096
#     10,000,000    107 ms     2626 ms      19,999  ← of 20,000: the whole graph
#
# Three things that sweep settles. **1024 costs 7% more than 200, not 5×** — `limit × degree`
# is an upper bound that does not bind, because both caps fire inside the same hop whose
# edges were fetched once. **Depth is free exactly while `limit` binds**: at 1024 it is
# 100 ms whether the walk may run 2 hops or 6, and only at 4096 does depth start to cost.
# And **the ceiling is doing real work** — removing it costs 26× here and reaches all but
# one document, which is the same shape `MEASURE_TYPED_WALK.md` measured at 17.4–20.8 s on
# a 3.68M-edge fixture reaching 36,822 of 36,823. 1024 sits at the knee, below where depth
# starts to matter again.
#
# The SQL side agrees independently: a frontier capped at 1024 stays well inside the
# planner's index/seq-scan crossover — in that sweep the smallest frontier already on a
# sequential scan was 8,892 and the largest still index-served was 14,144 — so the walk
# keeps `document_links_source_id_target_id_link_type_key`. That argument is sound but it
# is not the binding one: 87–89% of a slow walk is the per-edge Python loop, not the
# database.
MAX_NEIGHBOR_LIMIT = 1024

# `max_depth` costs round trips, not expansion — two statements per hop at
# `direction="both"`, over a frontier `limit` already bounds, and the table above shows
# depth 6 costing what depth 2 costs at every limit up to 1024. It needs a ceiling anyway,
# because nothing else bounds how many statements one request may issue: the walk only
# stops early when the frontier empties, so `max_depth=10_000_000` with `limit=1024` is
# still up to ~2,050 round trips. 6 is past every named walk the appliance runs — the wire
# default is 2, no in-tree caller of `/graph/neighbors` passes more, and the deepest walk
# anywhere is the coreference cluster at `max_depth=5` (`graph_analysis.py`), which does
# not come through this endpoint.
MAX_NEIGHBOR_DEPTH = 6


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
        """Flat top-N centrality scores.

        Computed over the documents the caller may read — see the module docstring, and
        ``graph_analysis._candidate_doc_query`` for where the gate is. ``total_vertices``
        and ``total_edges`` therefore describe the caller's graph, which is the only graph
        the scores are about.
        """
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
        """Hierarchical roll-up: descendant centrality flows up the tree with decay.

        Both inputs are principal-scoped: the centrality graph and the tree index the
        roll-up walks. An unreadable descendant is not in ``subtree_size``, not in
        ``spread``, and not in ``top_descendants`` — see the module docstring.
        """
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
        """Top reading paths through the subtree of root_id.

        Principal-scoped through the same two builders as the rest of the analytics verbs.
        A root the caller cannot read yields no tree and therefore no paths, so it takes
        the 404 below — the same answer a root that does not exist gets, which is the
        existence-hiding this codebase spells everywhere else.
        """
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
        max_depth: Annotated[int, Field(ge=1, le=MAX_NEIGHBOR_DEPTH)] = 2,
        direction: str = "both",
        link_types: Optional[str] = None,
        limit: Annotated[int, Field(ge=1, le=MAX_NEIGHBOR_LIMIT)] = 200,
    ) -> NeighborsResponse:
        """Link-graph neighbors of a root document (bounded BFS).

        The transitive counterpart to per-document ``get_links``: everything reachable
        from ``root_id`` within ``max_depth`` link hops, cycle-guarded and ``limit``-capped.
        ``link_types`` is an optional comma-separated allow-list of edge types.

        **Both bounds have a ceiling and an over-large ask is refused, not clamped.**
        ``MAX_NEIGHBOR_LIMIT`` / ``MAX_NEIGHBOR_DEPTH`` above carry the numbers and the
        measurements behind them. Refusing is what keeps the ceiling readable: a clamped
        walk answers a question the caller did not ask, and ``truncated`` cannot say so —
        it reports a property of the GRAPH ("more lies behind this page"), so using it to
        also report a property of the REQUEST ("your bound was overruled") would make one
        bit mean two things. Refusing keeps the response about the graph.

        The ``Field`` bounds are declared on the signature so ``wiring.py`` publishes them
        in the OpenAPI document — a caller reads the ceiling without sending anything, the
        way ``/capabilities`` answers — and FastAPI refuses out-of-range values with 422
        before a session is touched. The explicit check below is not a duplicate of that:
        ``LocalJmftsClient`` and every in-process caller reach this method with no wire
        validation in front of them, and a bound only the HTTP transport enforces is not a
        bound. Both read the same two constants, so they cannot disagree.
        """
        if not 1 <= limit <= MAX_NEIGHBOR_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_NEIGHBOR_LIMIT}, got {limit}")
        if not 1 <= max_depth <= MAX_NEIGHBOR_DEPTH:
            raise ValueError(
                f"max_depth must be between 1 and {MAX_NEIGHBOR_DEPTH}, got {max_depth}"
            )
        # Subtree RBAC: an unreadable root is indistinguishable from missing (404);
        # walk_neighbors additionally hides unreadable nodes reached during the walk.
        root = self.session.get(Document, root_id)
        if root is None or not can_read(self.session, root):
            raise LookupError(f"Document {root_id} not found")
        types = _parse_csv(link_types)
        # `truncated` comes from the walk, which overshoots the node cap by one and reads
        # the answer back. It used to be inferred here as `len(nodes) >= limit`, which is
        # wrong in both directions — see `NeighborWalk`. Depth is deliberately not part of
        # it: a caller who asked for `max_depth` hops and got every node within `max_depth`
        # hops was answered, not cut, so the walk is not asked to measure that here.
        walk = walk_neighbors(
            self.session,
            root_id,
            max_depth=max_depth,
            direction=direction,
            link_types=types,
            limit=limit,
        )
        nodes = walk.nodes
        return NeighborsResponse(
            root_id=root_id,
            max_depth=max_depth,
            direction=direction,
            link_types=types,
            total=len(nodes),
            truncated=walk.truncated,
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
        """Leiden communities over the chosen edge graph.

        The partition is over the caller's graph — see the module docstring. A community
        is a property of the graph it was found in, so a member list assembled from rows
        the caller cannot read would name documents that are 404 everywhere else.
        """
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
        """Run orphan + contradiction + stale + coverage audits in one transaction.

        **Half of this is principal-scoped and half is not, as of Block A step 2.**
        ``lint_orphans`` and ``lint_coverage`` build their graph through
        ``graph_analysis.build_graph`` (``lint.py:66``, ``:246``) and so inherit step 2's
        vertex filter for free. ``lint_contradictions`` and ``lint_stale`` read ``triples``
        directly (``lint.py:111``, ``:175``) and report ``triple_ids`` with no gate. Step 2
        scopes to the four analytics verbs, so closing that is not this step's; it is
        written here rather than left to be rediscovered.
        """
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
