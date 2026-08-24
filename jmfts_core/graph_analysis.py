"""Graph analytics over the JMFTS link graph, triple graph, and tree.

Three primitives, one philosophy: replace the vague "god node" framing with
explicit, configurable centrality measures.

- ``build_link_graph`` / ``build_triple_graph`` / ``build_combined_graph``
  construct directed igraph instances over the chosen edge source.
- ``compute_centrality`` runs degree | pagerank | betweenness on the graph.
- ``compute_subtree_authority`` rolls up descendant centrality into ancestor
  authority with depth decay and a spread bonus that prevents a single hub
  from inflating its parent's score.
- ``compute_spines`` returns top-N reading paths through the tree, ranked by
  cumulative centrality of the path's documents.

The graphs are all directed; an undirected projection is used internally
when betweenness/community work needs it.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Iterable, Literal, Optional

import igraph as ig
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jmfts_core.access import readable_id_subset
from jmfts_core.models.document import Document, DocumentLink
from jmfts_core.models.triple import Triple

logger = logging.getLogger(__name__)


Metric = Literal["degree", "pagerank", "betweenness"]
Scope = Literal["links", "triples", "both"]


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------


@dataclass
class _GraphBuild:
    """A directed igraph plus parallel id <-> index maps and per-vertex metadata."""

    graph: ig.Graph
    # vertex index (0..n-1) -> document id
    idx_to_id: list[int]
    # document id -> vertex index
    id_to_idx: dict[int, int]
    # document id -> minimal metadata (title, usetype, depth)
    meta: dict[int, dict]


def _candidate_doc_query(
    session: Session,
    parent_id: Optional[int],
    exclude_usetypes: Optional[Iterable[str]],
):
    """Return a select() of Document rows matching scope filters."""
    stmt = select(Document.id, Document.title, Document.usetype, Document.path)
    if parent_id is not None:
        # Documents whose path contains parent_id (descendants) plus the parent itself
        stmt = stmt.where(
            (Document.id == parent_id) | (Document.path.op("@>")(func.jsonb_build_array(parent_id)))
        )
    if exclude_usetypes:
        stmt = stmt.where(
            (Document.usetype.is_(None)) | (~Document.usetype.in_(list(exclude_usetypes)))
        )
    return stmt


def _gather_vertices(
    session: Session,
    parent_id: Optional[int],
    exclude_usetypes: Optional[Iterable[str]],
) -> tuple[list[int], dict[int, int], dict[int, dict]]:
    """Resolve the vertex set: list of doc ids, id->idx map, id->meta map."""
    rows = session.execute(_candidate_doc_query(session, parent_id, exclude_usetypes)).all()
    idx_to_id: list[int] = []
    id_to_idx: dict[int, int] = {}
    meta: dict[int, dict] = {}
    for i, (doc_id, title, usetype, path) in enumerate(rows):
        idx_to_id.append(doc_id)
        id_to_idx[doc_id] = i
        meta[doc_id] = {
            "title": title,
            "usetype": usetype,
            "depth": len(path) if path else 0,
        }
    return idx_to_id, id_to_idx, meta


def build_link_graph(
    session: Session,
    *,
    parent_id: Optional[int] = None,
    exclude_usetypes: Optional[Iterable[str]] = None,
) -> _GraphBuild:
    """Build a directed graph from the ``document_links`` table."""
    idx_to_id, id_to_idx, meta = _gather_vertices(session, parent_id, exclude_usetypes)

    if not idx_to_id:
        return _GraphBuild(ig.Graph(directed=True), [], {}, {})

    link_stmt = select(DocumentLink.source_id, DocumentLink.target_id, DocumentLink.score).where(
        DocumentLink.source_id.in_(idx_to_id),
        DocumentLink.target_id.in_(idx_to_id),
    )
    rows = session.execute(link_stmt).all()

    edges: list[tuple[int, int]] = []
    weights: list[float] = []
    for src, tgt, score in rows:
        edges.append((id_to_idx[src], id_to_idx[tgt]))
        weights.append(float(score) if score is not None else 1.0)

    g = ig.Graph(n=len(idx_to_id), edges=edges, directed=True)
    if weights:
        g.es["weight"] = weights
    return _GraphBuild(g, idx_to_id, id_to_idx, meta)


def build_triple_graph(
    session: Session,
    *,
    parent_id: Optional[int] = None,
    exclude_usetypes: Optional[Iterable[str]] = None,
) -> _GraphBuild:
    """Build a directed graph from the ``triples`` table.

    Subject -> object edges, weighted uniformly. Excludes invalidated triples.
    """
    idx_to_id, id_to_idx, meta = _gather_vertices(session, parent_id, exclude_usetypes)
    if not idx_to_id:
        return _GraphBuild(ig.Graph(directed=True), [], {}, {})

    triple_stmt = select(Triple.subject_id, Triple.object_id).where(
        Triple.subject_id.in_(idx_to_id),
        Triple.object_id.in_(idx_to_id),
        Triple.invalidated_at.is_(None),
    )
    rows = session.execute(triple_stmt).all()
    edges = [(id_to_idx[s], id_to_idx[o]) for s, o in rows]

    g = ig.Graph(n=len(idx_to_id), edges=edges, directed=True)
    g.es["weight"] = [1.0] * g.ecount()
    return _GraphBuild(g, idx_to_id, id_to_idx, meta)


def build_combined_graph(
    session: Session,
    *,
    parent_id: Optional[int] = None,
    exclude_usetypes: Optional[Iterable[str]] = None,
) -> _GraphBuild:
    """Union of link and triple graphs. Edges from both sources, weights summed."""
    idx_to_id, id_to_idx, meta = _gather_vertices(session, parent_id, exclude_usetypes)
    if not idx_to_id:
        return _GraphBuild(ig.Graph(directed=True), [], {}, {})

    edge_weights: dict[tuple[int, int], float] = {}

    link_stmt = select(DocumentLink.source_id, DocumentLink.target_id, DocumentLink.score).where(
        DocumentLink.source_id.in_(idx_to_id),
        DocumentLink.target_id.in_(idx_to_id),
    )
    for src, tgt, score in session.execute(link_stmt).all():
        key = (id_to_idx[src], id_to_idx[tgt])
        edge_weights[key] = edge_weights.get(key, 0.0) + (float(score) if score else 1.0)

    triple_stmt = select(Triple.subject_id, Triple.object_id).where(
        Triple.subject_id.in_(idx_to_id),
        Triple.object_id.in_(idx_to_id),
        Triple.invalidated_at.is_(None),
    )
    for s, o in session.execute(triple_stmt).all():
        key = (id_to_idx[s], id_to_idx[o])
        edge_weights[key] = edge_weights.get(key, 0.0) + 1.0

    edges = list(edge_weights.keys())
    weights = [edge_weights[e] for e in edges]
    g = ig.Graph(n=len(idx_to_id), edges=edges, directed=True)
    if weights:
        g.es["weight"] = weights
    return _GraphBuild(g, idx_to_id, id_to_idx, meta)


def build_graph(
    session: Session,
    scope: Scope = "links",
    *,
    parent_id: Optional[int] = None,
    exclude_usetypes: Optional[Iterable[str]] = None,
) -> _GraphBuild:
    """Dispatch to the right builder based on ``scope``."""
    if scope == "links":
        return build_link_graph(session, parent_id=parent_id, exclude_usetypes=exclude_usetypes)
    if scope == "triples":
        return build_triple_graph(session, parent_id=parent_id, exclude_usetypes=exclude_usetypes)
    if scope == "both":
        return build_combined_graph(session, parent_id=parent_id, exclude_usetypes=exclude_usetypes)
    raise ValueError(f"Unknown scope: {scope!r}")


# ---------------------------------------------------------------------------
# Flat centrality
# ---------------------------------------------------------------------------


@dataclass
class CentralityScore:
    document_id: int
    title: Optional[str]
    usetype: Optional[str]
    depth: int
    score: float
    in_degree: int
    out_degree: int


def compute_centrality(
    build: _GraphBuild,
    metric: Metric = "pagerank",
    top: int = 20,
) -> list[CentralityScore]:
    """Score every vertex with the chosen metric, return top-N."""
    g = build.graph
    n = g.vcount()
    if n == 0:
        return []

    has_weights = "weight" in g.es.attribute_names() and g.ecount() > 0
    weights = g.es["weight"] if has_weights else None

    if metric == "pagerank":
        scores = g.pagerank(weights=weights, directed=True)
    elif metric == "degree":
        # Total degree (in+out); router uses in/out separately
        scores = g.degree(mode="all")
    elif metric == "betweenness":
        # Betweenness on undirected projection (more meaningful for navigation)
        und = g.as_undirected(combine_edges=dict(weight="sum"))
        und_weights = und.es["weight"] if "weight" in und.es.attribute_names() else None
        scores = und.betweenness(weights=und_weights)
    else:
        raise ValueError(f"Unknown metric: {metric!r}")

    in_deg = g.degree(mode="in")
    out_deg = g.degree(mode="out")

    scored = [
        CentralityScore(
            document_id=build.idx_to_id[i],
            title=build.meta[build.idx_to_id[i]]["title"],
            usetype=build.meta[build.idx_to_id[i]]["usetype"],
            depth=build.meta[build.idx_to_id[i]]["depth"],
            score=float(scores[i]),
            in_degree=int(in_deg[i]),
            out_degree=int(out_deg[i]),
        )
        for i in range(n)
    ]
    scored.sort(key=lambda r: r.score, reverse=True)
    return scored[:top]


# ---------------------------------------------------------------------------
# Subtree-aware authority
# ---------------------------------------------------------------------------


@dataclass
class SubtreeAuthority:
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
    top_descendants: list[dict] = field(default_factory=list)


def _build_tree_index(
    session: Session,
    *,
    parent_id: Optional[int] = None,
) -> tuple[dict[int, list[int]], dict[int, dict]]:
    """Pull the document tree into memory: id -> children_ids, id -> meta.

    Bounded by ``parent_id``: only the parent and its descendants are loaded.
    """
    stmt = select(Document.id, Document.parent_id, Document.title, Document.usetype, Document.path)
    if parent_id is not None:
        stmt = stmt.where(
            (Document.id == parent_id) | (Document.path.op("@>")(func.jsonb_build_array(parent_id)))
        )
    children: dict[int, list[int]] = {}
    meta: dict[int, dict] = {}
    for doc_id, par_id, title, usetype, path in session.execute(stmt).all():
        meta[doc_id] = {
            "title": title,
            "usetype": usetype,
            "depth": len(path) if path else 0,
            "parent_id": par_id,
        }
        children.setdefault(doc_id, [])
        if par_id is not None:
            children.setdefault(par_id, []).append(doc_id)
    return children, meta


def _descendants(children: dict[int, list[int]], root_id: int) -> list[int]:
    """All descendants of root_id (excluding root)."""
    out: list[int] = []
    stack = list(children.get(root_id, []))
    while stack:
        node = stack.pop()
        out.append(node)
        stack.extend(children.get(node, []))
    return out


def compute_subtree_authority(
    session: Session,
    *,
    scope: Scope = "links",
    parent_id: Optional[int] = None,
    exclude_usetypes: Optional[Iterable[str]] = None,
    metric: Metric = "pagerank",
    decay: float = 0.7,
    min_subtree_size: int = 3,
    top: int = 20,
    high_centrality_quantile: float = 0.75,
) -> list[SubtreeAuthority]:
    """Aggregate descendant centrality up the tree with depth decay + spread.

    ``authority(d) = (own + Σ centrality(d') * decay^(depth(d') - depth(d))) * spread_bonus(d)``
    where ``spread_bonus = 1 + log(1 + n_high_centrality_descendants)``.
    """
    build = build_graph(
        session, scope=scope, parent_id=parent_id, exclude_usetypes=exclude_usetypes
    )
    if build.graph.vcount() == 0:
        return []

    centrality_list = compute_centrality(build, metric=metric, top=build.graph.vcount())
    centrality_map = {row.document_id: row.score for row in centrality_list}

    children, tree_meta = _build_tree_index(session, parent_id=parent_id)

    # high-centrality threshold (quantile across the candidate set)
    nonzero_scores = sorted(s for s in centrality_map.values() if s > 0)
    if nonzero_scores:
        idx = int(high_centrality_quantile * (len(nonzero_scores) - 1))
        threshold = nonzero_scores[idx]
    else:
        threshold = 0.0

    results: list[SubtreeAuthority] = []
    for doc_id, mdata in tree_meta.items():
        descendants = _descendants(children, doc_id)
        subtree_size = 1 + len(descendants)
        if subtree_size < min_subtree_size:
            continue
        own = centrality_map.get(doc_id, 0.0)
        own_depth = mdata["depth"]
        descendant_authority = 0.0
        spread = 0
        descendant_scores: list[tuple[int, float]] = []
        for d in descendants:
            score = centrality_map.get(d, 0.0)
            if score <= 0:
                continue
            depth_diff = max(0, tree_meta[d]["depth"] - own_depth)
            contribution = score * (decay**depth_diff)
            descendant_authority += contribution
            descendant_scores.append((d, contribution))
            if score >= threshold and threshold > 0:
                spread += 1
        spread_bonus = 1.0 + math.log1p(spread)
        authority = (own + descendant_authority) * spread_bonus
        descendant_scores.sort(key=lambda x: x[1], reverse=True)
        top_desc = [
            {
                "document_id": d,
                "title": tree_meta.get(d, {}).get("title"),
                "usetype": tree_meta.get(d, {}).get("usetype"),
                "contribution": contrib,
            }
            for d, contrib in descendant_scores[:5]
        ]
        results.append(
            SubtreeAuthority(
                document_id=doc_id,
                title=mdata["title"],
                usetype=mdata["usetype"],
                depth=mdata["depth"],
                subtree_size=subtree_size,
                own_centrality=own,
                descendant_authority=descendant_authority,
                spread=spread,
                spread_bonus=spread_bonus,
                authority=authority,
                top_descendants=top_desc,
            )
        )
    results.sort(key=lambda r: r.authority, reverse=True)
    return results[:top]


# ---------------------------------------------------------------------------
# Spines (top reading paths through the tree)
# ---------------------------------------------------------------------------


@dataclass
class SpinePath:
    path: list[int]
    titles: list[Optional[str]]
    usetypes: list[Optional[str]]
    total_score: float
    branch_points: list[dict] = field(default_factory=list)


def compute_spines(
    session: Session,
    root_id: int,
    *,
    scope: Scope = "links",
    exclude_usetypes: Optional[Iterable[str]] = None,
    metric: Metric = "pagerank",
    branching_threshold: float = 0.85,
    max_paths: int = 5,
    max_depth: Optional[int] = None,
) -> list[SpinePath]:
    """Top reading paths from root_id to leaves, ranked by sum of centrality."""
    build = build_graph(session, scope=scope, parent_id=root_id, exclude_usetypes=exclude_usetypes)
    centrality_list = compute_centrality(build, metric=metric, top=build.graph.vcount() or 1)
    centrality_map = {row.document_id: row.score for row in centrality_list}

    children, tree_meta = _build_tree_index(session, parent_id=root_id)
    if root_id not in tree_meta:
        return []

    # Enumerate root-to-leaf paths (or root-to-max_depth) with running score.
    @dataclass
    class _Cand:
        path: list[int]
        score: float

    completed: list[_Cand] = []
    stack: list[_Cand] = [_Cand(path=[root_id], score=centrality_map.get(root_id, 0.0))]
    root_depth = tree_meta[root_id]["depth"]

    while stack:
        cand = stack.pop()
        last = cand.path[-1]
        kids = children.get(last, [])
        if max_depth is not None and tree_meta[last]["depth"] - root_depth >= max_depth:
            kids = []
        if not kids:
            completed.append(cand)
            continue
        # Sort kids by their own centrality score descending — gives stable best-first traversal
        kids_sorted = sorted(kids, key=lambda k: centrality_map.get(k, 0.0), reverse=True)
        for k in kids_sorted:
            stack.append(_Cand(path=cand.path + [k], score=cand.score + centrality_map.get(k, 0.0)))

    completed.sort(key=lambda c: c.score, reverse=True)
    if not completed:
        return []

    selected = completed[:max_paths]
    best_score = selected[0].score

    # Detect branch points: at each step in a path, were there sibling alternatives
    # whose own score (subtree-summed) was within branching_threshold of the chosen?
    def subtree_sum(node_id: int) -> float:
        total = centrality_map.get(node_id, 0.0)
        for d in _descendants(children, node_id):
            total += centrality_map.get(d, 0.0)
        return total

    out: list[SpinePath] = []
    for cand in selected:
        bps: list[dict] = []
        for i, node_id in enumerate(cand.path[:-1]):
            chosen = cand.path[i + 1]
            sibs = children.get(node_id, [])
            if len(sibs) <= 1:
                continue
            chosen_score = subtree_sum(chosen)
            alts = []
            for s in sibs:
                if s == chosen:
                    continue
                ss = subtree_sum(s)
                if chosen_score > 0 and ss / chosen_score >= branching_threshold:
                    alts.append(
                        {
                            "document_id": s,
                            "title": tree_meta.get(s, {}).get("title"),
                            "subtree_score": ss,
                        }
                    )
            if alts:
                bps.append(
                    {
                        "at_document_id": node_id,
                        "chosen": chosen,
                        "alternatives": alts,
                    }
                )
        out.append(
            SpinePath(
                path=cand.path,
                titles=[tree_meta.get(p, {}).get("title") for p in cand.path],
                usetypes=[tree_meta.get(p, {}).get("usetype") for p in cand.path],
                total_score=cand.score,
                branch_points=bps,
            )
        )
        # Stop early if path is no longer "best-ish"
        if best_score > 0 and cand.score / best_score < branching_threshold:
            break
    return out


# ---------------------------------------------------------------------------
# Communities (Leiden over the link / triple / combined graph)
# ---------------------------------------------------------------------------


@dataclass
class Community:
    community_id: int
    size: int
    members: list[dict]
    cohesion: float


def compute_communities(
    build: _GraphBuild,
    *,
    resolution: float = 1.0,
    min_size: int = 2,
) -> list[Community]:
    """Leiden community detection on the undirected projection of the graph."""
    import leidenalg

    g = build.graph
    if g.vcount() == 0:
        return []
    und = g.as_undirected(combine_edges=dict(weight="sum"))
    und_weights = und.es["weight"] if "weight" in und.es.attribute_names() else None
    partition = leidenalg.find_partition(
        und,
        leidenalg.RBConfigurationVertexPartition,
        weights=und_weights,
        resolution_parameter=resolution,
        n_iterations=-1,
    )

    out: list[Community] = []
    for ci, members in enumerate(partition):
        if len(members) < min_size:
            continue
        # Cohesion: edges-inside / max-possible-edges-inside
        subg = und.subgraph(members)
        possible = len(members) * (len(members) - 1) / 2 if len(members) > 1 else 1
        cohesion = subg.ecount() / possible if possible > 0 else 0.0
        member_dicts = [
            {
                "document_id": build.idx_to_id[i],
                "title": build.meta[build.idx_to_id[i]]["title"],
                "usetype": build.meta[build.idx_to_id[i]]["usetype"],
            }
            for i in members
        ]
        out.append(
            Community(
                community_id=ci,
                size=len(members),
                members=member_dicts,
                cohesion=cohesion,
            )
        )
    out.sort(key=lambda c: c.size, reverse=True)
    return out


# ---------------------------------------------------------------------------
# Diff (corpus delta over time)
# ---------------------------------------------------------------------------


@dataclass
class GraphDiff:
    since: Optional[str]
    until: Optional[str]
    new_documents: int
    new_links: int
    new_triples: int
    superseded_triples: int


def compute_diff(
    session: Session,
    since=None,
    until=None,
) -> GraphDiff:
    """Counts of new/changed entities within the time window."""
    new_docs_q = select(func.count(Document.id))
    new_links_q = select(func.count(DocumentLink.id))
    new_triples_q = select(func.count(Triple.id))
    superseded_q = select(func.count(Triple.id)).where(Triple.invalidated_at.is_not(None))

    if since is not None:
        new_docs_q = new_docs_q.where(Document.created_at >= since)
        new_links_q = new_links_q.where(DocumentLink.created_at >= since)
        new_triples_q = new_triples_q.where(Triple.created_at >= since)
        superseded_q = superseded_q.where(Triple.invalidated_at >= since)
    if until is not None:
        new_docs_q = new_docs_q.where(Document.created_at <= until)
        new_links_q = new_links_q.where(DocumentLink.created_at <= until)
        new_triples_q = new_triples_q.where(Triple.created_at <= until)
        superseded_q = superseded_q.where(Triple.invalidated_at <= until)

    return GraphDiff(
        since=since.isoformat() if since else None,
        until=until.isoformat() if until else None,
        new_documents=session.execute(new_docs_q).scalar() or 0,
        new_links=session.execute(new_links_q).scalar() or 0,
        new_triples=session.execute(new_triples_q).scalar() or 0,
        superseded_triples=session.execute(superseded_q).scalar() or 0,
    )


# ---------------------------------------------------------------------------
# Stats (corpus aggregates)
# ---------------------------------------------------------------------------


@dataclass
class GraphStats:
    total_documents: int
    total_links: int
    total_triples: int
    invalidated_triples: int
    by_usetype: dict[str, int]
    by_link_type: dict[str, int]
    by_fact_type: dict[str, int]


def compute_stats(session: Session) -> GraphStats:
    """Corpus-wide counts. Cheap one-shot SQL."""
    total_docs = session.execute(select(func.count(Document.id))).scalar() or 0
    total_links = session.execute(select(func.count(DocumentLink.id))).scalar() or 0
    total_triples = session.execute(select(func.count(Triple.id))).scalar() or 0
    invalidated = (
        session.execute(
            select(func.count(Triple.id)).where(Triple.invalidated_at.is_not(None))
        ).scalar()
        or 0
    )

    by_usetype = dict(
        session.execute(
            select(Document.usetype, func.count(Document.id)).group_by(Document.usetype)
        ).all()
    )
    by_usetype = {(k or "(none)"): v for k, v in by_usetype.items()}

    by_link_type = dict(
        session.execute(
            select(DocumentLink.link_type, func.count(DocumentLink.id)).group_by(
                DocumentLink.link_type
            )
        ).all()
    )

    by_fact_type = dict(
        session.execute(
            select(Triple.fact_type, func.count(Triple.id)).group_by(Triple.fact_type)
        ).all()
    )
    by_fact_type = {
        (k.value if hasattr(k, "value") else str(k)): v for k, v in by_fact_type.items()
    }

    return GraphStats(
        total_documents=total_docs,
        total_links=total_links,
        total_triples=total_triples,
        invalidated_triples=invalidated,
        by_usetype=by_usetype,
        by_link_type=by_link_type,
        by_fact_type=by_fact_type,
    )


# ---------------------------------------------------------------------------
# Link-graph traversal (bounded BFS from a root)
# ---------------------------------------------------------------------------


class GraphWalkTruncated(RuntimeError):
    """A bounded walk stopped with reachable nodes unvisited, for a caller needing all.

    The bounds on ``compute_neighbors`` are right for browsing and wrong for any caller
    whose question is "what is the WHOLE set". A cut cluster returns a partial fact set
    that reads exactly like a complete one, which is the failure the appliance is built
    not to have. Raising is the current answer; ``SPRINT_0_3_0.md`` 7.3 replaces it with a
    materialized component at step 10, and this error must become unreachable then.
    """


@dataclass
class NeighborNode:
    """One document reached while traversing the ``DocumentLink`` graph from a root.

    ``depth`` is the number of link hops from the root (root itself is depth 0 and is
    not emitted). ``via_link_*`` / ``parent_document_id`` describe the *first* edge that
    reached this node (BFS shortest hop), so the caller can reconstruct a path back to
    the root. ``direction`` is the sense of that edge relative to traversal: ``outgoing``
    means the edge pointed parent→node, ``incoming`` means node→parent.
    """

    document_id: int
    title: Optional[str]
    usetype: Optional[str]
    depth: int
    parent_document_id: int
    via_link_id: int
    via_link_type: str
    direction: str


def compute_neighbors(
    session: Session,
    root_id: int,
    *,
    max_depth: int = 2,
    direction: str = "both",
    link_types: Optional[Iterable[str]] = None,
    limit: int = 200,
    raise_on_truncation: bool = False,
) -> list[NeighborNode]:
    """Breadth-first walk of the ``DocumentLink`` graph outward from ``root_id``.

    The read-side counterpart to ``create_link``/``delete_link``: the link graph is a
    real graph but the only exposed reads were per-document ``get_links``. This gives the
    transitive picture — everything reachable within ``max_depth`` hops — cycle-guarded
    (each node is emitted once, at its shortest hop) and bounded by ``limit`` total nodes.

    Args:
        root_id: the document to start from (not itself emitted).
        max_depth: maximum number of link hops to follow (>= 1).
        direction: which edges to follow — ``outgoing`` (source→target), ``incoming``
            (target→source), or ``both``.
        link_types: if given, only traverse edges whose ``link_type`` is in this set.
        limit: stop once this many distinct neighbors have been collected.
        raise_on_truncation: raise :class:`GraphWalkTruncated` instead of returning a
            partial walk. A browse endpoint wants the partial list and a "there is more"
            flag; a caller that needs the WHOLE reachable set — coreference, per
            ``SPRINT_0_3_0.md`` 7.3 — cannot tell a complete cluster from a cut one and
            must not be handed the cut one silently.

    Returns:
        Neighbors ordered by (depth, discovery), each carrying the first edge that
        reached it. The root is never included; ``limit`` caps the result — the caller
        should surface truncation to the user rather than treat it as the whole graph.

    Raises:
        GraphWalkTruncated: only with ``raise_on_truncation``, when either bound cut the
            walk with reachable nodes still unvisited.
    """
    type_filter = list(link_types) if link_types else None
    follow_out = direction in ("outgoing", "both")
    follow_in = direction in ("incoming", "both")

    visited: set[int] = {root_id}
    reached: list[NeighborNode] = []
    # (document_id) currently on the frontier; start with just the root at depth 0.
    frontier: list[int] = [root_id]
    depth = 0

    # A walk that must know whether it saw everything overshoots each bound by exactly one
    # and then measures. Stopping AT a bound is ambiguous — a frontier is non-empty whether
    # or not expanding it would find anything new, and a walk that collected `limit` nodes
    # may or may not have had a 201st. One extra hop and one extra node settle both, and
    # cost nothing when nothing is truncated. Only `raise_on_truncation` pays for it.
    node_cap = limit + 1 if raise_on_truncation else limit
    depth_cap = max_depth + 1 if raise_on_truncation else max_depth

    while frontier and depth < depth_cap and len(reached) < node_cap:
        depth += 1
        # Pull every edge incident to the current frontier in the chosen direction(s),
        # one batched query per direction rather than per node.
        edges: list[tuple[int, int, int, str, str]] = []  # (link_id, from, to, type, dir)
        if follow_out:
            stmt = select(
                DocumentLink.id,
                DocumentLink.source_id,
                DocumentLink.target_id,
                DocumentLink.link_type,
            ).where(DocumentLink.source_id.in_(frontier))
            if type_filter is not None:
                stmt = stmt.where(DocumentLink.link_type.in_(type_filter))
            for lid, src, tgt, ltype in session.execute(stmt).all():
                edges.append((lid, src, tgt, ltype, "outgoing"))
        if follow_in:
            stmt = select(
                DocumentLink.id,
                DocumentLink.source_id,
                DocumentLink.target_id,
                DocumentLink.link_type,
            ).where(DocumentLink.target_id.in_(frontier))
            if type_filter is not None:
                stmt = stmt.where(DocumentLink.link_type.in_(type_filter))
            for lid, src, tgt, ltype in session.execute(stmt).all():
                edges.append((lid, tgt, src, ltype, "incoming"))

        # Subtree RBAC: treat documents the current principal cannot read as absent from
        # the graph. Filtering the frontier candidates BEFORE they are visited/emitted hides
        # them AND stops the walk from transiting through them to reveal what lies beyond
        # (owner/unbound callers see the whole graph — readable_id_subset returns all ids).
        candidate_ids = {to for (_, _, to, _, _) in edges if to not in visited}
        readable = readable_id_subset(session, candidate_ids)

        next_frontier: list[int] = []
        for link_id, from_id, to_id, link_type, edge_dir in edges:
            if to_id in visited or to_id not in readable:
                continue
            visited.add(to_id)
            reached.append(
                NeighborNode(
                    document_id=to_id,
                    title=None,
                    usetype=None,
                    depth=depth,
                    parent_document_id=from_id,
                    via_link_id=link_id,
                    via_link_type=link_type,
                    direction=edge_dir,
                )
            )
            next_frontier.append(to_id)
            if len(reached) >= node_cap:
                break
        frontier = next_frontier

    # The overshoot, read back. An extra node means there was a `limit + 1`th; an extra hop
    # that found anything means `max_depth` was cutting the walk short.
    if raise_on_truncation:
        if len(reached) > limit:
            raise GraphWalkTruncated(
                f"walk from document {root_id} hit its node cap of {limit}; the reachable "
                f"set is larger and this result is a cut of it"
            )
        beyond = sum(1 for n in reached if n.depth > max_depth)
        if beyond:
            raise GraphWalkTruncated(
                f"walk from document {root_id} hit its depth cap of {max_depth}; "
                f"{beyond} more node(s) lie past it and this result is a cut of the "
                f"reachable set"
            )

    # One query to hydrate titles/usetypes for every reached node.
    if reached:
        meta = {
            row.id: (row.title, row.usetype)
            for row in session.execute(
                select(Document.id, Document.title, Document.usetype).where(
                    Document.id.in_([n.document_id for n in reached])
                )
            ).all()
        }
        for node in reached:
            node.title, node.usetype = meta.get(node.document_id, (None, None))

    return reached


# ---------------------------------------------------------------------------
# Entity coreference (the non-destructive `same_as` leg)
# ---------------------------------------------------------------------------

#: Link type used to assert that two entity documents denote the same real-world thing.
#: A ``same_as`` edge is treated as *symmetric*: it is traversed in both directions at
#: read time (``compute_neighbors(direction="both")``), so a single directional edge
#: written by ``create_link`` suffices — there is no need to store the reverse. It is a
#: plain ``DocumentLink``, so it is asserted with ``create_link(a, b, "same_as")`` and
#: retracted with ``delete_link`` — non-destructive and reversible, never a merge.
SAME_AS_LINK_TYPE = "same_as"

#: Link type joining the copies of ONE real-world thing that exist because it was mentioned
#: under more than one access (``SPRINT_0_3_0.md`` 7.5). Same referent, different viewers.
#:
#: **It is not ``same_as``, and collapsing the two would be a mistake.** ``same_as``
#: asserts an INFERENCE that may be wrong, and retracting one is a correction;
#: ``rbac_coref`` asserts something the appliance KNOWS, because it created both nodes, and
#: retracting one would be a lie. They also mean different things to island analysis — an
#: access split is not evidence of coreference density.
#:
#: Symmetric at read time for the same reason ``same_as`` is, so ``resolve_entity`` writes
#: one directional edge from the copy it just created to each existing copy and the walk
#: finds it from either end.
RBAC_COREF_LINK_TYPE = "rbac_coref"


def resolve_coreferent_ids(
    session: Session,
    entity_id: int,
    *,
    max_depth: int = 5,
    limit: int = 200,
) -> list[int]:
    """Return ``entity_id`` together with every entity it is coreferent with.

    Follows ``same_as`` AND ``rbac_coref`` edges transitively in both directions
    (coreference is an equivalence relation — symmetric and transitive) via the bounded,
    cycle-guarded ``compute_neighbors`` walk, so ``A same_as B`` and ``B same_as C`` put A,
    B and C in one cluster. ``entity_id`` is always the first element, even if it has no
    coreferents.

    **Two link types, one cluster.** They are kept distinct as ASSERTIONS — one is an
    inference, the other is a fact about how the appliance stored a thing — and the
    distinction is not this function's business: asked "what else denotes this", both
    answer yes. ``SPRINT_0_3_0.md`` 7.5.

    This is the read-side of the coreference leg: pass the result to
    ``TripleRepository.query_triples(entity_ids=...)`` to union the facts recorded under
    every alias of an entity.

    **A cut cluster raises rather than returning.** The bounds used to truncate silently,
    which handed the caller a partial fact set indistinguishable from a complete one. They
    are generous for hand-asserted ``same_as`` edges and will not be once an
    inverse-functional column derives them over a whole workbook. ``SPRINT_0_3_0.md`` 7.3.

    Raises:
        GraphWalkTruncated: the cluster is larger than ``max_depth``/``limit`` admit.
    """
    cluster = [entity_id]
    seen = {entity_id}
    for node in compute_neighbors(
        session,
        entity_id,
        max_depth=max_depth,
        direction="both",
        link_types=[SAME_AS_LINK_TYPE, RBAC_COREF_LINK_TYPE],
        limit=limit,
        raise_on_truncation=True,
    ):
        if node.document_id not in seen:
            seen.add(node.document_id)
            cluster.append(node.document_id)

    # Subtree RBAC, asserted HERE and not left to the walk. `compute_neighbors` does filter
    # its frontier today, but its bounds and its filtering are tuned for browsing and it
    # has callers that legitimately want the whole graph; the guarantee this function makes
    # is its own. What an `rbac_coref` cluster leaks without it is CARDINALITY — a
    # principal learns "this entity has four copies and I can read one" — which is exactly
    # the shape of the access boundary the copies exist to respect.
    #
    # `entity_id` itself is never dropped: the caller supplied it, so returning it tells
    # them nothing they did not already have, and `[entity_id]` is the documented answer
    # for an entity with no coreferents.
    if len(cluster) > 1:
        readable = readable_id_subset(session, cluster[1:])
        cluster = [entity_id] + [cid for cid in cluster[1:] if cid in readable]
    return cluster
