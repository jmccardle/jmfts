#!/usr/bin/env python3
"""
Overnight MultiHop-RAG Benchmark for JMFTS

Uses ANN-accelerated MaxSim search (via IVF-Flat indexes).
Runs full benchmark on all tier/dimension configurations.

Usage:
    python scripts/benchmark_overnight.py

    # Quick validation (200 queries)
    python scripts/benchmark_overnight.py --quick

    # Single config test
    python scripts/benchmark_overnight.py --dim 256 --tier 10

Output saved to: /storage/jmfts_data/benchmark_results.json
"""

import json
import time
import sys
import argparse
from pathlib import Path
from dataclasses import dataclass
from collections import defaultdict

# Force unbuffered output
sys.stdout.reconfigure(line_buffering=True)

sys.path.insert(0, "/home/john/Development/jmfts")

from jmfts_core.database import get_session
from jmfts_core.repositories.search import SearchRepository
from jmfts_core.models.document import Document
from jmfts_core.models.token_embedding import TokenEmbedding

QUERIES_FILE = Path("/storage/jmfts_data/MultiHop-RAG/dataset/MultiHopRAG.json")
OUTPUT_FILE = Path("/storage/jmfts_data/benchmark_results.json")


def load_queries():
    with open(QUERIES_FILE) as f:
        return json.load(f)


def normalize_text(text: str) -> str:
    return text.replace(" ", "").replace("\n", "").lower()


@dataclass
class Config:
    embed_dim: int
    max_tier: int

    def __str__(self):
        return f"dim={self.embed_dim}/tier={self.max_tier}"


def evaluate_method(search_repo, method, query_text, root_id, config: Config):
    """Run a single search method"""
    start = time.time()

    try:
        if method == "vector":
            results = search_repo.vector_search_text(query_text, limit=10, parent_id=root_id)
        elif method == "maxsim":
            results = search_repo.maxsim_search(
                query_text, limit=10,
                embed_dim=config.embed_dim,
                parent_id=root_id,
                max_tier=config.max_tier,
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


def run_evaluation(session, search_repo, queries, root_id, config: Config):
    """Run evaluation for a single configuration"""
    print(f"\n{'='*70}", flush=True)
    print(f"Config: {config}", flush=True)
    print(f"{'='*70}", flush=True)

    methods = ["vector", "maxsim", "fulltext"]
    method_data = {m: {"retrieved": [], "latencies": []} for m in methods}
    gold_facts_all = []

    evaluated = 0
    start_time = time.time()

    for i, q in enumerate(queries):
        if q.get("question_type") == "null_query":
            continue

        evidence_list = q.get("evidence_list", [])
        gold_facts = [ev["fact"] for ev in evidence_list if isinstance(ev, dict) and "fact" in ev]

        if not gold_facts:
            continue

        gold_facts_all.append(gold_facts)
        query_text = q["query"]

        for method in methods:
            texts, latency = evaluate_method(search_repo, method, query_text, root_id, config)
            method_data[method]["retrieved"].append(texts)
            method_data[method]["latencies"].append(latency)

        evaluated += 1
        if evaluated % 100 == 0:
            elapsed = time.time() - start_time
            qps = evaluated / elapsed
            eta = (len(queries) - evaluated) / qps if qps > 0 else 0
            print(f"  {evaluated} queries evaluated ({qps:.1f} q/s, ETA: {eta:.0f}s)", flush=True)

    print(f"\n  Completed {evaluated} queries", flush=True)

    # Calculate metrics
    results = {"config": {"embed_dim": config.embed_dim, "max_tier": config.max_tier}}

    for method in methods:
        metrics = calculate_metrics(method_data[method]["retrieved"], gold_facts_all)
        avg_latency = sum(method_data[method]["latencies"]) / len(method_data[method]["latencies"]) if method_data[method]["latencies"] else 0

        print(f"  {method:10s}: H@4={metrics['hits_at_4']:.1%} H@10={metrics['hits_at_10']:.1%} "
              f"MAP={metrics['map_at_10']:.3f} MRR={metrics['mrr_at_10']:.3f} ({avg_latency:.0f}ms)", flush=True)

        results[method] = {**metrics, "avg_latency_ms": avg_latency, "queries_evaluated": evaluated}

    return results


def main():
    parser = argparse.ArgumentParser(description="Overnight MultiHop-RAG Benchmark")
    parser.add_argument("--quick", action="store_true", help="Quick test with 200 queries")
    parser.add_argument("--dim", type=int, help="Single dimension to test")
    parser.add_argument("--tier", type=int, help="Single tier to test")
    args = parser.parse_args()

    print("=" * 70, flush=True)
    print("JMFTS MultiHop-RAG Benchmark (ANN-accelerated)", flush=True)
    print("=" * 70, flush=True)

    queries = load_queries()
    print(f"Loaded {len(queries)} queries", flush=True)

    if args.quick:
        queries = queries[:200]
        print(f"Quick mode: using first 200 queries", flush=True)

    # Build configuration list
    if args.dim and args.tier:
        configs = [Config(args.dim, args.tier)]
    elif args.dim:
        configs = [Config(args.dim, t) for t in [35, 40, 45, 50]]
    elif args.tier:
        configs = [Config(256, args.tier)]  # Only 256 dim now
    else:
        # Test tiers 35-50% to find performance plateau
        # Only 256-dim (dropped 384)
        configs = [
            Config(256, tier)
            for tier in [35, 40, 45, 50]
        ]

    print(f"Running {len(configs)} configurations", flush=True)

    with get_session() as session:
        # Set IVF probes for better recall during search
        from sqlalchemy import text
        session.execute(text("SET ivfflat.probes = 10"))

        # Find corpus root
        root = session.query(Document).filter(Document.usetype == "multihop:corpus").first()
        if not root:
            print("ERROR: No corpus found! Run import first.", flush=True)
            return

        root_id = root.id
        total_chunks = session.query(Document).filter(Document.usetype == "multihop:chunk").count()
        total_tokens = session.query(TokenEmbedding).count()
        print(f"Corpus: root={root_id}, {total_chunks} chunks, {total_tokens} tokens", flush=True)

        search_repo = SearchRepository(session)
        all_results = []

        for i, config in enumerate(configs):
            print(f"\n[{i+1}/{len(configs)}] Running {config}...", flush=True)
            result = run_evaluation(session, search_repo, queries, root_id, config)
            all_results.append(result)

            # Save intermediate results
            with open(OUTPUT_FILE, "w") as f:
                json.dump(all_results, f, indent=2)
            print(f"  (saved to {OUTPUT_FILE})", flush=True)

    # Print summary table
    print("\n" + "=" * 80, flush=True)
    print("FINAL SUMMARY", flush=True)
    print("=" * 80, flush=True)
    print(f"{'Config':<20} {'Method':<10} {'Hits@4':>8} {'Hits@10':>8} {'MAP@10':>8} {'MRR@10':>8} {'Latency':>10}", flush=True)
    print("-" * 80, flush=True)

    for r in all_results:
        cfg = f"d{r['config']['embed_dim']}/t{r['config']['max_tier']}"
        for method in ["vector", "maxsim", "fulltext"]:
            m = r[method]
            label = cfg if method == "vector" else ""
            print(f"{label:<20} {method:<10} {m['hits_at_4']:>8.1%} {m['hits_at_10']:>8.1%} "
                  f"{m['map_at_10']:>8.3f} {m['mrr_at_10']:>8.3f} {m['avg_latency_ms']:>9.0f}ms", flush=True)
        print("-" * 80, flush=True)

    print(f"\nResults saved to: {OUTPUT_FILE}", flush=True)
    print("Benchmark complete!", flush=True)


if __name__ == "__main__":
    main()
