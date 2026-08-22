#!/usr/bin/env python3
"""
Centroid Filtering Recall Benchmark for JMFTS

Tests how well centroid-based pre-filtering recovers full MaxSim ground truth.
Compares weighted centroid vs truncated mean-pool approaches.

Usage:
    python scripts/benchmark_centroid_recall.py
    python scripts/benchmark_centroid_recall.py --quick  # 100 queries
    python scripts/benchmark_centroid_recall.py --queries 500
"""

import json
import time
import sys
import argparse
import numpy as np
from pathlib import Path
from collections import defaultdict

sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, "/home/john/Development/jmfts")

from sqlalchemy import text
from jmfts_core.database import get_session
from jmfts_core.repositories.search import SearchRepository
from jmfts_core.models.document import Document
from jmfts_core.embedding import get_embedding_service

QUERIES_FILE = Path("/storage/jmfts_data/MultiHop-RAG/dataset/MultiHopRAG.json")
OUTPUT_FILE = Path("/storage/jmfts_data/centroid_recall_results.json")


def load_queries():
    with open(QUERIES_FILE) as f:
        return json.load(f)


def get_maxsim_ground_truth(search_repo, query_text, parent_id, k=10):
    """Get ground truth top-k documents from full MaxSim"""
    results = search_repo.maxsim_search(
        query_text, limit=k,
        embed_dim=256,
        parent_id=parent_id,
        max_tier=50,
    )
    return [r.document.id for r in results]


def centroid_filter_weighted(session, query_embeddings, parent_id, n_candidates):
    """
    Filter documents by weighted centroid similarity.
    Uses Option C: each query token vs centroids, aggregate scores.
    """
    doc_scores = defaultdict(float)

    for q_embed in query_embeddings:
        embed_str = "[" + ",".join(str(x) for x in q_embed) + "]"

        # Find top docs by centroid similarity for this query token
        result = session.execute(text("""
            SELECT d.id, 1 - (d.centroid_256 <=> CAST(:query_vec AS vector)) as similarity
            FROM documents d
            WHERE d.centroid_256 IS NOT NULL
              AND d.path @> jsonb_build_array(:parent_id)
            ORDER BY d.centroid_256 <=> CAST(:query_vec AS vector)
            LIMIT :n
        """), {"query_vec": embed_str, "parent_id": parent_id, "n": n_candidates}).fetchall()

        for doc_id, sim in result:
            # Track max similarity per doc for this query token (like MaxSim)
            if sim > doc_scores.get(doc_id, 0):
                doc_scores[doc_id] = max(doc_scores.get(doc_id, 0), sim)

    # Sort by aggregated score
    sorted_docs = sorted(doc_scores.items(), key=lambda x: x[1], reverse=True)
    return [doc_id for doc_id, _ in sorted_docs[:n_candidates]]


def centroid_filter_meanpool(session, query_embeddings, parent_id, n_candidates):
    """
    Filter documents by truncated mean-pool similarity.
    Uses embed[:256] from the documents table.
    """
    doc_scores = defaultdict(float)

    for q_embed in query_embeddings:
        embed_str = "[" + ",".join(str(x) for x in q_embed) + "]"

        # Find top docs by truncated mean-pool similarity
        # Note: embed is 768-dim, we need to truncate to 256 for comparison
        # pgvector doesn't support slicing, so we do this comparison in a subquery
        result = session.execute(text("""
            WITH truncated AS (
                SELECT id,
                       (string_to_array(trim(both '[]' from embed::text), ',')::float8[])[1:256] as embed_256_arr
                FROM documents
                WHERE embed IS NOT NULL
                  AND path @> jsonb_build_array(:parent_id)
            )
            SELECT t.id,
                   1 - (
                       (SELECT sqrt(sum(power(a - b, 2)))
                        FROM unnest(t.embed_256_arr, (string_to_array(trim(both '[]' from :query_vec), ',')::float8[])) as x(a, b))
                       / nullif(
                           (SELECT sqrt(sum(power(a, 2))) FROM unnest(t.embed_256_arr) as x(a)) *
                           (SELECT sqrt(sum(power(b, 2))) FROM unnest((string_to_array(trim(both '[]' from :query_vec), ',')::float8[])) as x(b)),
                           0
                       )
                   ) as similarity
            FROM truncated t
            ORDER BY similarity DESC
            LIMIT :n
        """), {"query_vec": embed_str, "parent_id": parent_id, "n": n_candidates}).fetchall()

        for doc_id, sim in result:
            if sim and sim > doc_scores.get(doc_id, 0):
                doc_scores[doc_id] = max(doc_scores.get(doc_id, 0), sim)

    # Sort by aggregated score
    sorted_docs = sorted(doc_scores.items(), key=lambda x: x[1], reverse=True)
    return [doc_id for doc_id, _ in sorted_docs[:n_candidates]]


def centroid_filter_weighted_simple(session, query_embedding_mean, parent_id, n_candidates):
    """
    Simpler filter: query mean-pool vs doc centroids (single vector comparison).
    Faster but potentially less accurate for multi-hop.
    """
    embed_str = "[" + ",".join(str(x) for x in query_embedding_mean) + "]"

    result = session.execute(text("""
        SELECT d.id
        FROM documents d
        WHERE d.centroid_256 IS NOT NULL
          AND d.path @> jsonb_build_array(:parent_id)
        ORDER BY d.centroid_256 <=> CAST(:query_vec AS vector)
        LIMIT :n
    """), {"query_vec": embed_str, "parent_id": parent_id, "n": n_candidates}).fetchall()

    return [row[0] for row in result]


def calculate_recall(ground_truth, candidates):
    """Calculate what fraction of ground truth docs are in candidates"""
    if not ground_truth:
        return 0.0
    gt_set = set(ground_truth)
    candidate_set = set(candidates)
    found = len(gt_set & candidate_set)
    return found / len(gt_set)


def main():
    parser = argparse.ArgumentParser(description="Centroid Filtering Recall Benchmark")
    parser.add_argument("--quick", action="store_true", help="Quick test with 100 queries")
    parser.add_argument("--queries", type=int, default=500, help="Number of queries to test")
    args = parser.parse_args()

    print("=" * 70, flush=True)
    print("JMFTS Centroid Filtering Recall Benchmark", flush=True)
    print("=" * 70, flush=True)

    queries = load_queries()

    # Filter to valid queries with evidence
    valid_queries = []
    for q in queries:
        if q.get("question_type") == "null_query":
            continue
        evidence_list = q.get("evidence_list", [])
        gold_facts = [ev["fact"] for ev in evidence_list if isinstance(ev, dict) and "fact" in ev]
        if gold_facts:
            valid_queries.append(q)

    print(f"Loaded {len(valid_queries)} valid queries", flush=True)

    n_queries = 100 if args.quick else min(args.queries, len(valid_queries))
    test_queries = valid_queries[:n_queries]
    print(f"Testing with {n_queries} queries", flush=True)

    # Candidate set sizes to test
    n_candidates_list = [50, 100, 200, 500, 1000, 2000]

    embedding_service = get_embedding_service()

    results = {
        "n_queries": n_queries,
        "ground_truth_k": 10,
        "methods": {}
    }

    with get_session() as session:
        # Set IVF probes
        session.execute(text("SET ivfflat.probes = 10"))

        # Find corpus root
        root = session.query(Document).filter(Document.usetype == "multihop:corpus").first()
        if not root:
            print("ERROR: No corpus found!", flush=True)
            return

        parent_id = root.id
        total_chunks = session.query(Document).filter(Document.usetype == "multihop:chunk").count()
        print(f"Corpus: {total_chunks} chunks", flush=True)

        search_repo = SearchRepository(session)

        # Test each method and candidate size
        for method_name in ["weighted_centroid_multi", "weighted_centroid_simple"]:
            print(f"\n{'='*60}", flush=True)
            print(f"Testing: {method_name}", flush=True)
            print(f"{'='*60}", flush=True)

            results["methods"][method_name] = {}

            for n_candidates in n_candidates_list:
                recalls = []
                latencies_gt = []
                latencies_filter = []

                print(f"\n  N={n_candidates}:", flush=True)

                for i, q in enumerate(test_queries):
                    query_text = q["query"]

                    # Get query token embeddings
                    embed_result = embedding_service.embed_with_tokens(
                        query_text, top_percent=1.0, prefix="search_query: "
                    )
                    content_tokens = [t for t in embed_result.token_embeddings if t.importance_score > 0]
                    tokens_to_use = content_tokens if content_tokens else embed_result.token_embeddings

                    query_embeddings = [
                        embedding_service.truncate_embedding(tok.embedding, 256).tolist()
                        for tok in tokens_to_use
                    ]
                    query_mean = np.mean(query_embeddings, axis=0).tolist()

                    # Ground truth from full MaxSim
                    t0 = time.time()
                    gt_docs = get_maxsim_ground_truth(search_repo, query_text, parent_id, k=10)
                    latencies_gt.append((time.time() - t0) * 1000)

                    # Centroid filtering
                    t0 = time.time()
                    if method_name == "weighted_centroid_multi":
                        candidate_docs = centroid_filter_weighted(session, query_embeddings, parent_id, n_candidates)
                    elif method_name == "weighted_centroid_simple":
                        candidate_docs = centroid_filter_weighted_simple(session, query_mean, parent_id, n_candidates)
                    latencies_filter.append((time.time() - t0) * 1000)

                    # Calculate recall
                    recall = calculate_recall(gt_docs, candidate_docs)
                    recalls.append(recall)

                    if (i + 1) % 50 == 0:
                        avg_recall = np.mean(recalls)
                        print(f"    {i+1}/{n_queries}: avg recall = {avg_recall:.1%}", flush=True)

                avg_recall = np.mean(recalls)
                p95_recall = np.percentile(recalls, 5)  # 5th percentile = worst 5%
                avg_gt_latency = np.mean(latencies_gt)
                avg_filter_latency = np.mean(latencies_filter)
                speedup = avg_gt_latency / avg_filter_latency if avg_filter_latency > 0 else 0

                results["methods"][method_name][n_candidates] = {
                    "avg_recall": float(avg_recall),
                    "p95_recall": float(p95_recall),
                    "avg_gt_latency_ms": float(avg_gt_latency),
                    "avg_filter_latency_ms": float(avg_filter_latency),
                    "speedup": float(speedup),
                }

                print(f"    Results: recall={avg_recall:.1%} (p95={p95_recall:.1%}), "
                      f"filter={avg_filter_latency:.0f}ms vs GT={avg_gt_latency:.0f}ms, "
                      f"speedup={speedup:.1f}x", flush=True)

    # Save results
    with open(OUTPUT_FILE, "w") as f:
        json.dump(results, f, indent=2)

    # Print summary table
    print("\n" + "=" * 80, flush=True)
    print("SUMMARY: Recall @ various candidate set sizes", flush=True)
    print("=" * 80, flush=True)
    print(f"{'Method':<30} {'N':>6} {'Recall':>8} {'P95':>8} {'Filter':>10} {'Speedup':>8}", flush=True)
    print("-" * 80, flush=True)

    for method_name, method_results in results["methods"].items():
        for n_candidates, metrics in method_results.items():
            print(f"{method_name:<30} {n_candidates:>6} {metrics['avg_recall']:>7.1%} "
                  f"{metrics['p95_recall']:>7.1%} {metrics['avg_filter_latency_ms']:>9.0f}ms "
                  f"{metrics['speedup']:>7.1f}x", flush=True)
        print("-" * 80, flush=True)

    print(f"\nResults saved to: {OUTPUT_FILE}", flush=True)


if __name__ == "__main__":
    main()
