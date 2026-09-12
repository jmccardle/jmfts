"""RAPTOR Hierarchical Summarization Service.

Leiden community detection on document embeddings → LLM summarization → recursive tree.
Dual-adaptive k/gamma: k (neighbors) increases and gamma (resolution) decreases per layer,
producing fine-grained clusters at the leaves and broad clusters near the root.
"""

import logging
from dataclasses import dataclass, field

import igraph as ig
import leidenalg
import numpy as np
from sqlalchemy.orm import Session

from jmfts_core.config import get_settings, Settings
from jmfts_core.llm_client import complete
from jmfts_core.models.document import SUMMARIZES_LINK_TYPE
from jmfts_core.repositories.document import DocumentRepository

logger = logging.getLogger(__name__)

SUMMARIZE_SYSTEM_PROMPT = (
    "You are a concise summarization engine. Given a set of text passages that belong "
    "to the same topic cluster, produce a single coherent summary. "
    "First, create a bulleted outline of the key points across the passages. "
    "Then, using that outline, write a cohesive summary paragraph. "
    "Output only the summary paragraph — do not include the outline in your final output. "
    "Be factual and concise. Do not add opinions or external knowledge."
)


@dataclass
class ClusterInfo:
    """A cluster of document IDs produced by Leiden."""

    member_ids: list[int]
    member_embeddings: list[np.ndarray]


@dataclass
class RaptorLayerResult:
    """Result of one layer of RAPTOR clustering + summarization."""

    layer: int
    clusters: int
    summary_ids: list[int]
    bridge_links_created: int


@dataclass
class RaptorResult:
    """Full result of recursive RAPTOR summarization."""

    root_id: int
    layers: list[RaptorLayerResult] = field(default_factory=list)
    total_summaries: int = 0
    total_bridge_links: int = 0


def _build_knn_graph(embeddings: np.ndarray, k: int) -> ig.Graph:
    """Build a k-NN graph from L2-normalized embeddings using cosine similarity.

    Args:
        embeddings: (n, d) array of L2-normalized embeddings.
        k: Number of nearest neighbors per node.

    Returns:
        Weighted undirected igraph Graph.
    """
    n = len(embeddings)
    k = min(k, n - 1)
    if k < 1:
        # Degenerate: single node or empty
        g = ig.Graph(n)
        return g

    # Cosine similarity matrix (embeddings already L2-normalized)
    sim_matrix = embeddings @ embeddings.T
    np.fill_diagonal(sim_matrix, -1.0)  # Exclude self

    edges = []
    weights = []
    seen = set()

    for i in range(n):
        neighbors = np.argsort(sim_matrix[i])[-k:]
        for j in neighbors:
            if j == i:
                continue
            edge = (min(i, j), max(i, j))
            if edge not in seen:
                seen.add(edge)
                # Shift similarity to positive range for Leiden (weights must be >= 0)
                w = max(0.0, float(sim_matrix[i, j]))
                edges.append(edge)
                weights.append(w)

    g = ig.Graph(n, edges, directed=False)
    g.es["weight"] = weights
    return g


def _leiden_cluster(
    embeddings: np.ndarray,
    doc_ids: list[int],
    k: int,
    gamma: float,
    min_cluster_size: int,
) -> list[ClusterInfo]:
    """Run Leiden community detection on k-NN graph of embeddings.

    Args:
        embeddings: (n, d) array.
        doc_ids: Parallel list of document IDs.
        k: Number of neighbors for k-NN graph.
        gamma: Leiden resolution parameter (higher = more clusters).
        min_cluster_size: Clusters smaller than this are merged with nearest neighbor.

    Returns:
        List of ClusterInfo objects.
    """
    n = len(embeddings)
    if n <= 1:
        if n == 1:
            return [ClusterInfo(member_ids=[doc_ids[0]], member_embeddings=[embeddings[0]])]
        return []

    graph = _build_knn_graph(embeddings, k)

    # Run Leiden with RBConfiguration for resolution parameter
    partition = leidenalg.find_partition(
        graph,
        leidenalg.RBConfigurationVertexPartition,
        weights="weight",
        resolution_parameter=gamma,
        n_iterations=-1,  # Iterate until convergence
    )

    # Group by community
    community_map: dict[int, list[int]] = {}
    for node_idx, comm_id in enumerate(partition.membership):
        community_map.setdefault(comm_id, []).append(node_idx)

    # Build ClusterInfo objects, merging undersized clusters
    clusters: list[ClusterInfo] = []
    orphans: list[int] = []  # Node indices from undersized clusters

    for comm_id, node_indices in community_map.items():
        if len(node_indices) < min_cluster_size:
            orphans.extend(node_indices)
        else:
            clusters.append(
                ClusterInfo(
                    member_ids=[doc_ids[i] for i in node_indices],
                    member_embeddings=[embeddings[i] for i in node_indices],
                )
            )

    # Merge orphans into nearest cluster by centroid similarity
    if orphans and clusters:
        centroids = np.array([np.mean(c.member_embeddings, axis=0) for c in clusters])
        centroids = centroids / (np.linalg.norm(centroids, axis=1, keepdims=True) + 1e-9)

        for orphan_idx in orphans:
            orphan_emb = embeddings[orphan_idx]
            sims = centroids @ orphan_emb
            best_cluster = int(np.argmax(sims))
            clusters[best_cluster].member_ids.append(doc_ids[orphan_idx])
            clusters[best_cluster].member_embeddings.append(embeddings[orphan_idx])
    elif orphans and not clusters:
        # All clusters were undersized — make one big cluster
        clusters.append(
            ClusterInfo(
                member_ids=[doc_ids[i] for i in orphans],
                member_embeddings=[embeddings[i] for i in orphans],
            )
        )

    return clusters


def _detect_bridge_chunks(
    clusters: list[ClusterInfo],
    embeddings_by_id: dict[int, np.ndarray],
    threshold: float,
) -> list[tuple[int, int, float]]:
    """Find documents that have high similarity to documents in other clusters.

    Returns list of (source_id, target_id, similarity) tuples for bridge links.
    """
    bridges: list[tuple[int, int, float]] = []

    for i, cluster_a in enumerate(clusters):
        for j, cluster_b in enumerate(clusters):
            if j <= i:
                continue
            # Compare every member of cluster_a against every member of cluster_b
            for doc_id_a in cluster_a.member_ids:
                emb_a = embeddings_by_id[doc_id_a]
                for doc_id_b in cluster_b.member_ids:
                    emb_b = embeddings_by_id[doc_id_b]
                    sim = float(np.dot(emb_a, emb_b))
                    if sim >= threshold:
                        bridges.append((doc_id_a, doc_id_b, sim))

    return bridges


async def _llm_summarize(texts: list[str], settings: Settings, llm_model: str | None) -> str:
    """Call the LLM to summarize a list of text passages.

    Uses the same OpenAI-compatible endpoint as the synthesis service.
    """
    base_url, model = settings.require_llm("RAPTOR summarization", llm_model)

    # Format passages
    passages = []
    for i, text in enumerate(texts, 1):
        passages.append(f"[Passage {i}]\n{text}")
    context = "\n\n".join(passages)

    # Truncate context to fit within budget
    char_budget = settings.summarization_context * 4  # rough chars-per-token
    if len(context) > char_budget:
        context = context[:char_budget]

    user_message = (
        f"Summarize the following {len(texts)} passages into a single cohesive summary:\n\n"
        f"{context}"
    )

    extra_body = {}
    if settings.summarization_disable_thinking:
        extra_body["chat_template_kwargs"] = {"enable_thinking": False}

    result = await complete(
        settings=settings,
        base_url=base_url,
        model=model,
        messages=[
            {"role": "system", "content": SUMMARIZE_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        max_tokens=settings.raptor_max_summary_tokens,
        temperature=settings.summarization_temperature,
        extra_body=extra_body,
    )
    return result.text


async def raptor_summarize(
    document_id: int,
    session: Session,
    max_depth: int | None = None,
    min_cluster_size: int | None = None,
    llm_model: str | None = None,
    max_summary_tokens: int | None = None,
    usetype_filter: str | None = None,
) -> RaptorResult:
    """Run RAPTOR hierarchical summarization on a document's children.

    Takes a parent document with embedded children (e.g., output of PELT segmentation),
    recursively clusters them using Leiden community detection, summarizes each cluster
    via LLM, and builds a summary tree. Bridge links are created for cross-cluster context.

    Args:
        document_id: Parent document whose children will be clustered and summarized.
        session: SQLAlchemy session.
        max_depth: Maximum recursion depth (default from settings).
        min_cluster_size: Minimum docs per cluster (default from settings).
        llm_model: Override the default LLM model.
        max_summary_tokens: Override max tokens for summaries.
        usetype_filter: If set, only cluster children with this usetype (e.g. 'summary').

    Returns:
        RaptorResult with layer-by-layer details.
    """
    settings = get_settings()
    max_depth = max_depth if max_depth is not None else settings.raptor_max_depth
    min_cluster_size = (
        min_cluster_size if min_cluster_size is not None else settings.raptor_min_cluster_size
    )
    if max_summary_tokens is not None:
        settings = settings.model_copy()
        settings.raptor_max_summary_tokens = max_summary_tokens

    repo = DocumentRepository(session)
    result = RaptorResult(root_id=document_id)

    # Current layer of document IDs to cluster
    # usetype_filter only applies to the first layer (selecting initial children);
    # subsequent layers always cluster the summaries produced by the previous layer.
    current_ids = _get_embedded_child_ids(repo, document_id, usetype_filter=usetype_filter)
    if len(current_ids) < 2:
        logger.info(
            "Document %d has fewer than 2 embedded children, nothing to cluster", document_id
        )
        return result

    for layer in range(max_depth):
        # Collect embeddings for current layer
        docs_with_embeddings = []
        for doc_id in current_ids:
            doc = repo.get(doc_id)
            if doc and doc.embed is not None:
                docs_with_embeddings.append((doc_id, np.array(doc.embed, dtype=np.float32)))

        if len(docs_with_embeddings) < 2:
            logger.info("Layer %d: fewer than 2 docs with embeddings, stopping", layer)
            break

        doc_ids = [d[0] for d in docs_with_embeddings]
        embeddings = np.array([d[1] for d in docs_with_embeddings])

        # Normalize embeddings
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        embeddings = embeddings / (norms + 1e-9)

        # Dual-adaptive parameters
        k = settings.raptor_k_base + layer * settings.raptor_k_step
        gamma = settings.raptor_gamma_base * (settings.raptor_gamma_decay**layer)
        logger.info("Layer %d: %d docs, k=%d, gamma=%.3f", layer, len(doc_ids), k, gamma)

        # Leiden clustering
        clusters = _leiden_cluster(embeddings, doc_ids, k, gamma, min_cluster_size)

        if len(clusters) <= 1 and layer > 0:
            # Convergence: everything in one cluster
            logger.info("Layer %d: single cluster, converged", layer)
            if len(clusters) == 1:
                # Create final root summary
                summary_id = await _summarize_cluster(
                    repo, clusters[0], document_id, layer, settings, llm_model
                )
                result.layers.append(
                    RaptorLayerResult(
                        layer=layer, clusters=1, summary_ids=[summary_id], bridge_links_created=0
                    )
                )
                result.total_summaries += 1
            break

        # Detect bridge chunks before summarizing
        embeddings_by_id = dict(zip(doc_ids, embeddings))
        bridges = _detect_bridge_chunks(
            clusters, embeddings_by_id, settings.raptor_bridge_threshold
        )
        bridge_count = 0
        for src_id, tgt_id, sim in bridges:
            repo.create_link(
                source_id=src_id,
                target_id=tgt_id,
                link_type="bridge",
                score=sim,
                metadata={"raptor_layer": layer},
            )
            bridge_count += 1

        # Summarize each cluster
        summary_ids = []
        for cluster in clusters:
            summary_id = await _summarize_cluster(
                repo, cluster, document_id, layer, settings, llm_model
            )
            summary_ids.append(summary_id)

        result.layers.append(
            RaptorLayerResult(
                layer=layer,
                clusters=len(clusters),
                summary_ids=summary_ids,
                bridge_links_created=bridge_count,
            )
        )
        result.total_summaries += len(summary_ids)
        result.total_bridge_links += bridge_count

        # Next layer: cluster the summaries. This is the ONE list layer N+1 clusters over,
        # and it is carried in memory from layer N's return values — the tree is not
        # re-read between layers. `_get_embedded_child_ids` is called exactly once, before
        # the loop (`:293`), to seed layer 0. That is why deleting the reparent from
        # `_summarize_cluster` (`docs/SPRINT_0_5_0.md` Block C step 12) does not touch
        # multi-layer roll-up: the reparent never fed the clustering, only the tree.
        current_ids = summary_ids

    session.flush()
    return result


async def _summarize_cluster(
    repo: DocumentRepository,
    cluster: ClusterInfo,
    root_parent_id: int,
    layer: int,
    settings: Settings,
    llm_model: str | None,
) -> int:
    """Summarize a single cluster: collect text, call LLM, create the summary, link down.

    It does NOT re-parent the members. `docs/SPRINT_0_5_0.md` Block C step 12 deleted the
    ``repo.reparent(doc_id, summary_doc.id)`` that used to run here; see the loop below.
    """
    # Collect text from cluster members
    texts = []
    for doc_id in cluster.member_ids:
        doc = repo.get(doc_id)
        if doc and doc.content:
            texts.append(doc.content)

    if not texts:
        # Cluster has no textual content — create placeholder
        summary_text = "(No content available for summarization)"
    else:
        summary_text = await _llm_summarize(texts, settings, llm_model)

    # Create summary document as child of root parent
    summary_doc = repo.create(
        title=f"RAPTOR Summary L{layer} ({len(cluster.member_ids)} docs)",
        content=summary_text,
        parent_id=root_parent_id,
        usetype="summary",
        structured_content={
            "raptor_layer": layer,
            "member_count": len(cluster.member_ids),
            "member_ids": cluster.member_ids,
        },
        auto_embed=True,
    )

    # Link the summary down to each member. The link is the ONLY record of the relation:
    # `docs/SPRINT_0_5_0.md` Block C step 12 deleted the ``repo.reparent(doc_id,
    # summary_doc.id)`` that used to run on this same iteration, and nothing replaces it.
    # Part 1.1 states the property the whole of Part 3 rests on — a derived tree LINKS to
    # the source leaves and does not OWN them — and the reparent was the ownership. The
    # two lines recorded the same relation and only this one can record it correctly:
    # `reparent` "Move[s] a document under a new parent, updating path for it and all
    # descendants" (`repositories/document.py:847`-`:850`), so it is single-valued and it
    # is destructive.
    #
    # This repository already made exactly this fix once, for entities: until
    # `SPRINT_0_3_0.md` 7.5 an entity was a CHILD of the document that mentioned it, and
    # "an entity mentioned by two documents could only be a child of one of them. As a
    # link it is many-to-many" (`fact_extraction.py:35`-`:41`, MENTIONS_LINK_TYPE). RAPTOR
    # had the identical defect and never got the migration.
    #
    # Three things change, and each is an improvement (Block C, step 12):
    #
    #   * a leaf keeps its PELT parent, so subtree search over a chapter finds its own
    #     leaves — `Document.path @> jsonb_build_array(parent_id)`
    #     (`repositories/search.py:187`, `:271`) is read off a path the roll-up no longer
    #     rewrites (`tests/test_raptor_structure.py`, test 2);
    #   * a leaf that Leiden placed in two clusters gets two ``summarizes`` links instead
    #     of one arbitrary parent — ``structured_content.member_ids`` above already
    #     records the true membership the tree could not represent;
    #   * a leaf keeps the ACR it was ingested under, because subtree RBAC resolves a
    #     principal's readable set with the same containment (`access.py:78`)
    #     (`tests/test_raptor_structure.py`, test 3).
    #
    # Removing the reparent loses no edge — the link carries everything the parent edge
    # carried, plus provenance, plus many-to-many. What it does NOT yet do is put the
    # summary somewhere of its own: `summary_doc` is still created under
    # ``root_parent_id`` above, and the derived root minted by
    # `sql/migrations/018_derived_roots.sql` has no writer until Block C step 11, which is
    # held out of this pass. Block C states that interim state rather than leaving it to
    # be discovered.
    #
    # ONE SPELLING, AND IT IS NOT THIS MODULE'S. `SPRINT_0_5_0.md` Block C finding 6: this
    # site and `rollup_tasks` both write the edge 3.1's leaf projection follows, so the name
    # lives on the model where neither writer can drift from the other.
    for doc_id in cluster.member_ids:
        repo.create_link(source_id=summary_doc.id, target_id=doc_id, link_type=SUMMARIZES_LINK_TYPE)

    return summary_doc.id


def _get_embedded_child_ids(
    repo: DocumentRepository, parent_id: int, usetype_filter: str | None = None
) -> list[int]:
    """Get IDs of immediate children that have embeddings.

    Args:
        repo: Document repository.
        parent_id: Parent document ID.
        usetype_filter: If set, only include children with this usetype.
    """
    children = repo.get_children(parent_id, usetype=usetype_filter, depth=1, limit=10000)
    return [c.id for c in children if c.embed is not None]


def _collect_report_summaries(repo: DocumentRepository, portfolio_id: int) -> list[int]:
    """Gather report-level summary document IDs from all child reports under a portfolio.

    Walks each immediate child (report) of the portfolio and collects its
    usetype='summary' children — these are the RAPTOR summaries produced by
    per-document RAPTOR. Only summaries with embeddings are returned.

    **What this returns changed with `docs/SPRINT_0_5_0.md` Block C step 12, and the
    change is not hidden here.** Every summary `_summarize_cluster` writes is created
    under ``root_parent_id`` — the report — at EVERY layer, so all of them are immediate
    children of the report. While the reparent existed, layer N+1 pulled layer N's
    summaries down underneath itself, and this ``depth=1`` walk therefore saw only the
    TOP layer. Without it they all stay siblings, so this now returns every layer's
    summaries and the portfolio roll-up clusters an L0 summary alongside the L1 summary
    that already covers it.

    That is left as observed rather than filtered on ``structured_content.raptor_layer``,
    because picking the top layer is a decision about what a portfolio tree summarises
    and Block C step 11 — the handler that writes into the derived root — is the pass
    that makes it. No test asserts against this path today (nothing under `tests/` calls
    `portfolio_raptor_summarize`; its only caller is
    `services/document_service.py:1190`), so under `SPRINT_0_4_0.md` Part 0's rule this
    is a recorded consequence and not an open defect.
    """
    report_docs = repo.get_children(portfolio_id, depth=1, limit=10000)
    summary_ids: list[int] = []
    for report in report_docs:
        summaries = repo.get_children(report.id, usetype="summary", depth=1, limit=10000)
        summary_ids.extend(s.id for s in summaries if s.embed is not None)
    return summary_ids


async def portfolio_raptor_summarize(
    portfolio_id: int,
    session: Session,
    max_depth: int | None = None,
    min_cluster_size: int | None = None,
    llm_model: str | None = None,
    max_summary_tokens: int | None = None,
) -> RaptorResult:
    """Run cross-document RAPTOR over report-level summaries under a portfolio root.

    Instead of clustering raw chunks from a single document, this gathers the
    top-level RAPTOR summaries from each report document under the portfolio,
    then clusters and summarizes them into portfolio-level themes.

    Assumes per-document RAPTOR has already been run on each report, producing
    usetype='summary' children.

    Args:
        portfolio_id: Portfolio root document whose child reports contain summaries.
        session: SQLAlchemy session.
        max_depth: Maximum recursion depth (default from settings).
        min_cluster_size: Minimum docs per cluster (default from settings).
        llm_model: Override the default LLM model.
        max_summary_tokens: Override max tokens for summaries.

    Returns:
        RaptorResult with layer-by-layer details.
    """
    settings = get_settings()
    max_depth = max_depth if max_depth is not None else settings.raptor_max_depth
    min_cluster_size = (
        min_cluster_size if min_cluster_size is not None else settings.raptor_min_cluster_size
    )
    if max_summary_tokens is not None:
        settings = settings.model_copy()
        settings.raptor_max_summary_tokens = max_summary_tokens

    repo = DocumentRepository(session)
    result = RaptorResult(root_id=portfolio_id)

    # Collect report-level summaries across all child reports
    current_ids = _collect_report_summaries(repo, portfolio_id)
    if len(current_ids) < 2:
        logger.info(
            "Portfolio %d has fewer than 2 report-level summaries, nothing to cluster",
            portfolio_id,
        )
        return result

    for layer in range(max_depth):
        docs_with_embeddings = []
        for doc_id in current_ids:
            doc = repo.get(doc_id)
            if doc and doc.embed is not None:
                docs_with_embeddings.append((doc_id, np.array(doc.embed, dtype=np.float32)))

        if len(docs_with_embeddings) < 2:
            logger.info("Portfolio layer %d: fewer than 2 docs with embeddings, stopping", layer)
            break

        doc_ids = [d[0] for d in docs_with_embeddings]
        embeddings = np.array([d[1] for d in docs_with_embeddings])

        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        embeddings = embeddings / (norms + 1e-9)

        k = settings.raptor_k_base + layer * settings.raptor_k_step
        gamma = settings.raptor_gamma_base * (settings.raptor_gamma_decay**layer)
        logger.info(
            "Portfolio layer %d: %d summaries, k=%d, gamma=%.3f",
            layer,
            len(doc_ids),
            k,
            gamma,
        )

        clusters = _leiden_cluster(embeddings, doc_ids, k, gamma, min_cluster_size)

        if len(clusters) <= 1 and layer > 0:
            logger.info("Portfolio layer %d: single cluster, converged", layer)
            if len(clusters) == 1:
                summary_id = await _summarize_cluster(
                    repo, clusters[0], portfolio_id, layer, settings, llm_model
                )
                result.layers.append(
                    RaptorLayerResult(
                        layer=layer, clusters=1, summary_ids=[summary_id], bridge_links_created=0
                    )
                )
                result.total_summaries += 1
            break

        embeddings_by_id = dict(zip(doc_ids, embeddings))
        bridges = _detect_bridge_chunks(
            clusters, embeddings_by_id, settings.raptor_bridge_threshold
        )
        bridge_count = 0
        for src_id, tgt_id, sim in bridges:
            repo.create_link(
                source_id=src_id,
                target_id=tgt_id,
                link_type="bridge",
                score=sim,
                metadata={"raptor_layer": layer, "portfolio_id": portfolio_id},
            )
            bridge_count += 1

        summary_ids = []
        for cluster in clusters:
            summary_id = await _summarize_cluster(
                repo, cluster, portfolio_id, layer, settings, llm_model
            )
            summary_ids.append(summary_id)

        result.layers.append(
            RaptorLayerResult(
                layer=layer,
                clusters=len(clusters),
                summary_ids=summary_ids,
                bridge_links_created=bridge_count,
            )
        )
        result.total_summaries += len(summary_ids)
        result.total_bridge_links += bridge_count

        current_ids = summary_ids

    session.flush()
    return result
