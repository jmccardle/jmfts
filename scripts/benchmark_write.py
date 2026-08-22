#!/usr/bin/env python3
"""Initial write-path benchmarks for JMFTS.

Every existing benchmark measures *retrieval* over a frozen corpus — the write
path has never been measured (the "write-path has no benchmark" gap). This is a
deliberately basic starting point: it times the core memory-write operations an
agent (Tau) actually drives, so we have a baseline to regress against.

Measured (per-op latency + throughput):
  - doc create, no embed      — pure INSERT + path trigger
  - doc create + embed        — the real ingest cost (embedding usually dominates)
  - triple upsert             — knowledge-graph write (ON CONFLICT path)

Scope/caveats:
  - Times work done inside one transaction and ROLLS BACK at the end, so the run
    never persists and never pollutes the target DB. pgvector HNSW/ivfflat index
    maintenance happens on INSERT (inside the txn), so it IS included; the final
    COMMIT's durability flush is NOT. Good enough for a relative baseline.
  - Embedding device follows JMFTS_EMBEDDING_DEVICE (cpu/cuda). Report which.
  - Run against whatever JMFTS_DB_* points at. For a clean, comparable number,
    point it at an empty DB (e.g. the docker pgvector stack).

Usage:
    python -m scripts.benchmark_write --quick
    python -m scripts.benchmark_write --docs 200 --triples 500
    JMFTS_EMBEDDING_DEVICE=cpu python -m scripts.benchmark_write --no-embed
"""

import argparse
import sys
import time

sys.path.insert(0, "/home/john/Development/jmfts")

from jmfts_core.config import get_settings  # noqa: E402
from jmfts_core.database import get_session  # noqa: E402
from jmfts_core.repositories.document import DocumentRepository  # noqa: E402
from jmfts_core.repositories.triple import TripleRepository  # noqa: E402


def _percentile(sorted_vals, q):
    """Nearest-rank percentile (q in [0,1]) over a pre-sorted list."""
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, int(round(q * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


def _summarize(name, latencies_ms):
    total_s = sum(latencies_ms) / 1000.0
    n = len(latencies_ms)
    ordered = sorted(latencies_ms)
    mean = sum(latencies_ms) / n if n else 0.0
    return {
        "op": name,
        "n": n,
        "ops_per_s": (n / total_s) if total_s > 0 else 0.0,
        "mean_ms": mean,
        "p50_ms": _percentile(ordered, 0.50),
        "p95_ms": _percentile(ordered, 0.95),
    }


def _content(i):
    """Distinct, realistic-length content so nothing dedups on content_hash."""
    return (
        f"Benchmark memory record number {i}. "
        "It records an agent turn with enough text to embed meaningfully — a "
        "tool call, its result, and a short reflection on what to do next. "
        f"Unique salt {i * 7919}."
    )


def bench_doc_create(session, root_id, n, embed):
    repo = DocumentRepository(session)
    latencies = []
    for i in range(n):
        t0 = time.perf_counter()
        repo.create(
            title=f"bench-{i}",
            content=_content(i),
            parent_id=root_id,
            usetype="raw",
            auto_embed=embed,
            embed_tokens=embed,
        )
        latencies.append((time.perf_counter() - t0) * 1000.0)
    return _summarize("doc_create+embed" if embed else "doc_create", latencies)


def bench_triple_upsert(session, root_id, n):
    repo = TripleRepository(session)
    doc_repo = DocumentRepository(session)
    # Entity pool (unembedded) + one predicate; time only the upserts.
    subject = doc_repo.create(
        title="subj",
        content="subject entity",
        parent_id=root_id,
        usetype="entity",
        auto_embed=False,
    )
    objects = [
        doc_repo.create(
            title=f"obj-{i}",
            content=f"object entity {i}",
            parent_id=root_id,
            usetype="entity",
            auto_embed=False,
        )
        for i in range(n)
    ]
    pred, _ = repo.get_or_create_predicate(name="benchmark_relates_to")
    session.flush()

    latencies = []
    for i in range(n):
        t0 = time.perf_counter()
        repo.upsert_triple(subject_id=subject.id, predicate_id=pred.id, object_id=objects[i].id)
        latencies.append((time.perf_counter() - t0) * 1000.0)
    return _summarize("triple_upsert", latencies)


def main():
    parser = argparse.ArgumentParser(description="JMFTS write-path benchmark")
    parser.add_argument("--docs", type=int, default=100, help="documents to create")
    parser.add_argument("--triples", type=int, default=200, help="triples to upsert")
    parser.add_argument("--no-embed", action="store_true", help="skip the embed pass")
    parser.add_argument("--quick", action="store_true", help="tiny run (20 docs / 40 triples)")
    args = parser.parse_args()

    if args.quick:
        args.docs, args.triples = 20, 40

    settings = get_settings()
    print(
        f"target db = {settings.db_host}:{settings.db_port}/{settings.db_name}  "
        f"embedding_device = {settings.embedding_device}  "
        f"model = {settings.embedding_model}"
    )
    print("(work is rolled back — nothing persists)\n")

    results = []
    with get_session() as session:
        try:
            root = DocumentRepository(session).create(
                title="__write_benchmark_root__", content="scratch root", auto_embed=False
            )
            session.flush()

            results.append(bench_doc_create(session, root.id, args.docs, embed=False))
            if not args.no_embed:
                results.append(bench_doc_create(session, root.id, args.docs, embed=True))
            results.append(bench_triple_upsert(session, root.id, args.triples))
        finally:
            # Never persist benchmark data.
            session.rollback()

    header = f"{'op':<20}{'n':>8}{'ops/s':>12}{'mean ms':>12}{'p50 ms':>12}{'p95 ms':>12}"
    print(header)
    print("-" * len(header))
    for r in results:
        print(
            f"{r['op']:<20}{r['n']:>8}{r['ops_per_s']:>12.1f}"
            f"{r['mean_ms']:>12.2f}{r['p50_ms']:>12.2f}{r['p95_ms']:>12.2f}"
        )


if __name__ == "__main__":
    main()
