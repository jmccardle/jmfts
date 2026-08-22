#!/usr/bin/env python3
"""
Two-Stage MaxSim Benchmark for JMFTS

Compares full MaxSim vs two-stage (centroid filter → MaxSim rerank)
on the complete MultiHop-RAG dataset.

Usage:
    python scripts/benchmark_twostage.py
    python scripts/benchmark_twostage.py --quick  # 200 queries
    python scripts/benchmark_twostage.py --candidates 500  # test specific N
"""

import json
import time
import sys
import argparse
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, "/home/john/Development/jmfts")

from sqlalchemy import text
from jmfts_core.database import get_session
from jmfts_core.repositories.search import SearchRepository
from jmfts_core.models.document import Document
from jmfts_core.models.token_embedding import TokenEmbedding

QUERIES_FILE = Path("/storage/jmfts_data/MultiHop-RAG/dataset/MultiHopRAG.json")
OUTPUT_FILE = Path("/storage/jmfts_data/twostage_benchmark_results.json")


def load_queries():
    with open(QUERIES_FILE) as f:
        return json.load(f)


def normalize_text(text: str) -> str:
    return text.replace(" ", "").replace("\n", "").lower()


def evaluate_method(search_repo, method, query_text, root_id, n_candidates=500):
    """Run a single search method"""
    start = time.time()

    try:
        if method == "vector":
            results = search_repo.vector_search_text(query_text, limit=10, parent_id=root_id)
        elif method == "maxsim":
            results = search_repo.maxsim_search(
                query_text, limit=10,
                embed_dim=256,
                parent_id=root_id,
                max_tier=50,
            )
        elif method == "maxsim_twostage":
            results = search_repo.maxsim_search_twostage(
                query_text, limit=10,
                n_candidates=n_candidates,
                embed_dim=256,
                parent_id=root_id,
                max_tier=50,
            )
        elif method == "fulltext":
            results = search_repo.fulltext_search(query_text, limit=10, parent_id=root_id)
        else:
            results = []
    except Exception as e:
        print(f"    Error in {method}: {e}", flush=True)
        results = []

    elapsed_ms = (time.time() - start) * 1000
    texts = [r.document.content or "" for r in results]
    return texts, elapsed_ms


def calculate_metrics(retrieved_all, gold_facts_all):
    """Calculate standard retrieval metrics"""
    hits_at_4 = 0
    hits_at_10 = 0
    map_scores = []
    mrr_scores = []

    for retrieved, gold in zip(retrieved_all, gold_facts_all):
        gold_norm = [normalize_text(g) for g in gold]
        retrieved_norm = [normalize_text(r) for r in retrieved]

        found = []
        first_rank = None
        ap_sum = 0

        for rank, ret in enumerate(retrieved_norm[:10], 1):
            matched = False
            for g in gold_norm:
                if g in ret and g not in found:
                    matched = True
                    found.append(g)
                    break

            if matched:
                if first_rank is None:
                    first_rank = rank
                if rank <= 4:
                    hits_at_4 += 1
                precision = len(found) / rank
                ap_sum += precision

        if found:
            hits_at_10 += 1

        map_scores.append(ap_sum / min(len(gold), 10) if gold else 0)
        mrr_scores.append(1 / first_rank if first_rank else 0)

    n = len(gold_facts_all)
    return {
        "hits_at_4": hits_at_4 / n if n else 0,
        "hits_at_10": hits_at_10 / n if n else 0,
        "map_at_10": sum(map_scores) / n if n else 0,
        "mrr_at_10": sum(mrr_scores) / n if n else 0,
    }


def main():
    parser = argparse.ArgumentParser(description="Two-Stage MaxSim Benchmark")
    parser.add_argument("--quick", action="store_true", help="Quick test with 200 queries")
    parser.add_argument("--candidates", type=int, nargs="+", default=[200, 500, 1000],
                        help="Candidate set sizes to test")
    args = parser.parse_args()

    print("=" * 70, flush=True)
    print("JMFTS Two-Stage MaxSim Benchmark", flush=True)
    print("=" * 70, flush=True)

    queries = load_queries()

    # Filter to valid queries
    valid_queries = []
    for q in queries:
        if q.get("question_type") == "null_query":
            continue
        evidence_list = q.get("evidence_list", [])
        gold_facts = [ev["fact"] for ev in evidence_list if isinstance(ev, dict) and "fact" in ev]
        if gold_facts:
            valid_queries.append((q, gold_facts))

    print(f"Loaded {len(valid_queries)} valid queries", flush=True)

    if args.quick:
        valid_queries = valid_queries[:200]
        print(f"Quick mode: using first 200 queries", flush=True)

    # Methods to test
    methods = ["vector", "maxsim"] + [f"maxsim_twostage_{n}" for n in args.candidates]

    print(f"Testing methods: {methods}", flush=True)
    print(f"Candidate sizes: {args.candidates}", flush=True)

    with get_session() as session:
        # Set IVF probes
        session.execute(text("SET ivfflat.probes = 10"))

        # Find corpus root
        root = session.query(Document).filter(Document.usetype == "multihop:corpus").first()
        if not root:
            print("ERROR: No corpus found!", flush=True)
            return

        root_id = root.id
        total_chunks = session.query(Document).filter(Document.usetype == "multihop:chunk").count()
        total_tokens = session.query(TokenEmbedding).count()
        print(f"Corpus: root={root_id}, {total_chunks} chunks, {total_tokens} tokens", flush=True)

        search_repo = SearchRepository(session)

        # Initialize result storage
        method_data = {m: {"retrieved": [], "latencies": []} for m in methods}
        gold_facts_all = []

        start_time = time.time()

        for i, (q, gold_facts) in enumerate(valid_queries):
            gold_facts_all.append(gold_facts)
            query_text = q["query"]

            # Run each method
            for method in methods:
                if method.startswith("maxsim_twostage_"):
                    n_candidates = int(method.split("_")[-1])
                    texts, latency = evaluate_method(
                        search_repo, "maxsim_twostage", query_text, root_id, n_candidates
                    )
                else:
                    texts, latency = evaluate_method(search_repo, method, query_text, root_id)

                method_data[method]["retrieved"].append(texts)
                method_data[method]["latencies"].append(latency)

            if (i + 1) % 100 == 0:
                elapsed = time.time() - start_time
                qps = (i + 1) / elapsed
                eta = (len(valid_queries) - (i + 1)) / qps if qps > 0 else 0
                print(f"  {i+1}/{len(valid_queries)} queries ({qps:.1f} q/s, ETA: {eta:.0f}s)", flush=True)

        print(f"\nCompleted {len(valid_queries)} queries", flush=True)

        # Calculate metrics for each method
        results = {"queries_evaluated": len(valid_queries)}

        print("\n" + "=" * 80, flush=True)
        print("RESULTS", flush=True)
        print("=" * 80, flush=True)
        print(f"{'Method':<25} {'Hits@4':>8} {'Hits@10':>8} {'MAP@10':>8} {'MRR@10':>8} {'Latency':>10}", flush=True)
        print("-" * 80, flush=True)

        for method in methods:
            metrics = calculate_metrics(method_data[method]["retrieved"], gold_facts_all)
            avg_latency = sum(method_data[method]["latencies"]) / len(method_data[method]["latencies"])

            results[method] = {
                **metrics,
                "avg_latency_ms": avg_latency,
            }

            print(f"{method:<25} {metrics['hits_at_4']:>7.1%} {metrics['hits_at_10']:>7.1%} "
                  f"{metrics['map_at_10']:>8.3f} {metrics['mrr_at_10']:>8.3f} {avg_latency:>9.0f}ms", flush=True)

        print("-" * 80, flush=True)

        # Calculate speedups
        if "maxsim" in results:
            baseline_latency = results["maxsim"]["avg_latency_ms"]
            print(f"\nSpeedups vs full MaxSim ({baseline_latency:.0f}ms):", flush=True)
            for method in methods:
                if method != "maxsim":
                    speedup = baseline_latency / results[method]["avg_latency_ms"]
                    quality_diff = results[method]["hits_at_4"] - results["maxsim"]["hits_at_4"]
                    print(f"  {method}: {speedup:.1f}x faster, {quality_diff:+.1%} H@4", flush=True)

        # Save results
        with open(OUTPUT_FILE, "w") as f:
            json.dump(results, f, indent=2)
        print(f"\nResults saved to: {OUTPUT_FILE}", flush=True)


if __name__ == "__main__":
    main()
