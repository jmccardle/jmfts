#!/usr/bin/env python3
"""
MultiHop-RAG Benchmark for JMFTS

Benchmarks retrieval performance with:
- Proper chunking (~256 tokens per chunk, matching reference implementation)
- Tiered token storage for efficient percentage sweeps
- Fact-based evaluation (matching against evidence facts, not just titles)
- Standard metrics: Hits@k, MAP@k, MRR@k

Usage:
    # Quick test (50 docs, 100 queries)
    python benchmark_multihop.py --quick

    # Full benchmark with all tier sweeps
    python benchmark_multihop.py

    # Single configuration
    python benchmark_multihop.py --dim 256 --tier 10
"""

import json
import time
import sys
import re
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
from collections import defaultdict
import argparse

# Add project to path
sys.path.insert(0, "/home/john/Development/jmfts")

from jmfts_core.database import get_session
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.search import SearchRepository
from jmfts_core.models.document import Document
from jmfts_core.models.token_embedding import TokenEmbedding
from jmfts_core.models.search_index import SearchIndex, SearchIndexEntry, SearchTermPosting, SearchTermStats
from jmfts_core.embedding import EmbeddingService

DATASET_DIR = Path("/storage/jmfts_data/MultiHop-RAG/dataset")
CORPUS_FILE = DATASET_DIR / "corpus.json"
QUERIES_FILE = DATASET_DIR / "MultiHopRAG.json"

# Chunking config (matching reference implementation)
CHUNK_SIZE = 256  # tokens
CHUNK_OVERLAP = 25  # ~10% overlap


@dataclass
class BenchmarkConfig:
    """Configuration for a benchmark run"""
    embed_dim: int = 256
    max_tier: int = 10  # 5, 10, 15, or 20
    limit_corpus: Optional[int] = None
    limit_queries: Optional[int] = None


@dataclass
class BenchmarkMetrics:
    """Standard retrieval metrics"""
    hits_at_4: float = 0.0
    hits_at_10: float = 0.0
    map_at_10: float = 0.0
    mrr_at_10: float = 0.0
    avg_latency_ms: float = 0.0
    queries_evaluated: int = 0


@dataclass
class BenchmarkResult:
    """Results from a benchmark run"""
    config: BenchmarkConfig
    import_time_sec: float = 0.0
    total_docs: int = 0
    total_chunks: int = 0
    total_tokens: int = 0

    vector_metrics: BenchmarkMetrics = field(default_factory=BenchmarkMetrics)
    maxsim_metrics: BenchmarkMetrics = field(default_factory=BenchmarkMetrics)
    fulltext_metrics: BenchmarkMetrics = field(default_factory=BenchmarkMetrics)


def load_corpus() -> list[dict]:
    """Load MultiHop-RAG corpus"""
    with open(CORPUS_FILE) as f:
        return json.load(f)


def load_queries() -> list[dict]:
    """Load MultiHop-RAG queries with evidence"""
    with open(QUERIES_FILE) as f:
        return json.load(f)


def simple_tokenize(text: str) -> list[str]:
    """Simple whitespace tokenization for chunking"""
    return text.split()


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """
    Split text into overlapping chunks.

    Args:
        text: Text to chunk
        chunk_size: Target tokens per chunk
        overlap: Token overlap between chunks

    Returns:
        List of chunk strings
    """
    words = simple_tokenize(text)
    if len(words) <= chunk_size:
        return [text]

    chunks = []
    start = 0
    while start < len(words):
        end = start + chunk_size
        chunk_words = words[start:end]
        chunks.append(" ".join(chunk_words))
        start = end - overlap
        if start >= len(words) - overlap:
            break

    return chunks


def wipe_database(session):
    """Clear all data"""
    print("  Wiping database...")
    session.query(TokenEmbedding).delete()
    session.query(SearchTermPosting).delete()
    session.query(SearchTermStats).delete()
    session.query(SearchIndexEntry).delete()
    session.query(SearchIndex).delete()
    session.query(Document).delete()
    session.commit()


def import_corpus_chunked(
    session,
    corpus: list[dict],
) -> tuple[int, dict[str, list[int]]]:
    """
    Import MultiHop-RAG corpus with chunking.

    Structure:
        corpus_root
          └── article_doc (metadata only)
                └── chunk_1 (content + embeddings)
                └── chunk_2 (content + embeddings)
                └── ...

    Returns (root_id, title_to_chunk_ids mapping)
    """
    doc_repo = DocumentRepository(session)

    # Create root document
    root = doc_repo.create(
        title="MultiHop-RAG Corpus",
        content=f"News articles from MultiHop-RAG benchmark. {len(corpus)} documents.",
        usetype="multihop:corpus",
        auto_embed=False,
    )
    print(f"  Created corpus root: {root.id}")

    title_to_chunk_ids: dict[str, list[int]] = defaultdict(list)
    total_chunks = 0

    for i, article in enumerate(corpus):
        title = article.get("title", f"Article {i}")
        body = article.get("body", "")
        metadata = {
            "author": article.get("author"),
            "source": article.get("source"),
            "published_at": article.get("published_at"),
            "category": article.get("category"),
            "url": article.get("url"),
        }

        # Create article document (metadata container)
        article_doc = doc_repo.create(
            title=title[:200],
            content=f"Article from {metadata.get('source', 'unknown')}",
            parent_id=root.id,
            usetype="multihop:article",
            structured_content=metadata,
            auto_embed=False,
        )

        # Chunk the body and create chunk documents
        chunks = chunk_text(body)
        for j, chunk_text_content in enumerate(chunks):
            chunk_doc = doc_repo.create(
                title=f"{title[:50]}... [chunk {j+1}/{len(chunks)}]",
                content=chunk_text_content,
                parent_id=article_doc.id,
                usetype="multihop:chunk",
                structured_content={"chunk_idx": j, "article_title": title},
                auto_embed=True,  # This triggers embedding with tiered tokens
            )
            title_to_chunk_ids[title].append(chunk_doc.id)
            total_chunks += 1

        if (i + 1) % 50 == 0:
            session.commit()
            print(f"    Imported {i+1}/{len(corpus)} articles ({total_chunks} chunks)...")

    session.commit()
    print(f"  Total: {len(corpus)} articles, {total_chunks} chunks")
    return root.id, dict(title_to_chunk_ids)


def create_bm25_index(session, root_id: int) -> str:
    """Create BM25 index for the corpus"""
    search_repo = SearchRepository(session)

    index_name = "multihop"
    try:
        search_repo.create_index(index_name, "MultiHop-RAG corpus index")
    except:
        pass  # Index might already exist
    search_repo.add_root_to_index(index_name, root_id)
    session.flush()
    result = search_repo.refresh_index(index_name)
    session.commit()
    print(f"  Created BM25 index: {result}")
    return index_name


def normalize_text(text: str) -> str:
    """Normalize text for comparison"""
    return text.replace(" ", "").replace("\n", "").lower()


def calculate_metrics(
    retrieved_texts: list[list[str]],
    gold_facts: list[list[str]],
) -> BenchmarkMetrics:
    """
    Calculate retrieval metrics.

    Uses substring matching: a retrieved chunk matches if any gold fact
    appears within it (after normalization).
    """
    metrics = BenchmarkMetrics()

    hits_at_10_count = 0
    hits_at_4_count = 0
    map_at_10_list = []
    mrr_list = []

    for retrieved, gold in zip(retrieved_texts, gold_facts):
        hits_at_10_flag = False
        hits_at_4_flag = False
        average_precision_sum = 0
        first_relevant_rank = None
        found_gold = []

        # Normalize gold facts
        gold_normalized = [normalize_text(g) for g in gold]
        retrieved_normalized = [normalize_text(r) for r in retrieved]

        for rank, retrieved_item in enumerate(retrieved_normalized[:11], start=1):
            # Check if any gold fact is contained in retrieved chunk
            matched = False
            for gold_item in gold_normalized:
                if gold_item in retrieved_item and gold_item not in found_gold:
                    matched = True
                    found_gold.append(gold_item)

            if matched:
                if rank <= 10:
                    hits_at_10_flag = True
                    if first_relevant_rank is None:
                        first_relevant_rank = rank
                    if rank <= 4:
                        hits_at_4_flag = True
                    precision_at_rank = len(found_gold) / rank
                    average_precision_sum += precision_at_rank

        hits_at_10_count += int(hits_at_10_flag)
        hits_at_4_count += int(hits_at_4_flag)
        map_at_10_list.append(average_precision_sum / min(len(gold), 10) if gold else 0)
        mrr_list.append(1 / first_relevant_rank if first_relevant_rank else 0)

    n = len(gold_facts)
    if n > 0:
        metrics.hits_at_4 = hits_at_4_count / n
        metrics.hits_at_10 = hits_at_10_count / n
        metrics.map_at_10 = sum(map_at_10_list) / n
        metrics.mrr_at_10 = sum(mrr_list) / n
        metrics.queries_evaluated = n

    return metrics


def evaluate_retrieval(
    session,
    queries: list[dict],
    root_id: int,
    config: BenchmarkConfig,
) -> BenchmarkResult:
    """
    Evaluate retrieval methods on queries.

    Matches retrieved chunks against evidence facts using substring matching.
    """
    search_repo = SearchRepository(session)
    result = BenchmarkResult(config=config)

    # Collect retrieved texts and gold facts for each method
    methods = {
        "vector": {"retrieved": [], "latencies": []},
        "maxsim": {"retrieved": [], "latencies": []},
        "fulltext": {"retrieved": [], "latencies": []},
    }
    gold_facts_all = []

    for i, q in enumerate(queries):
        query_text = q["query"]
        question_type = q.get("question_type", "")

        # Skip null queries (no answer expected)
        if question_type == "null_query":
            continue

        # Extract gold facts from evidence
        evidence_list = q.get("evidence_list", [])
        gold_facts = []
        for ev in evidence_list:
            if isinstance(ev, dict) and "fact" in ev:
                gold_facts.append(ev["fact"])

        if not gold_facts:
            continue

        gold_facts_all.append(gold_facts)

        # Test each method
        for method_name in methods:
            start = time.time()

            try:
                if method_name == "vector":
                    results = search_repo.vector_search_text(
                        query_text, limit=10, parent_id=root_id
                    )
                elif method_name == "maxsim":
                    results = search_repo.maxsim_search(
                        query_text, limit=10,
                        embed_dim=config.embed_dim,
                        parent_id=root_id,
                        max_tier=config.max_tier,
                    )
                elif method_name == "fulltext":
                    results = search_repo.fulltext_search(
                        query_text, limit=10, parent_id=root_id
                    )
                else:
                    results = []
            except Exception as e:
                print(f"    Error in {method_name}: {e}")
                results = []

            elapsed_ms = (time.time() - start) * 1000
            methods[method_name]["latencies"].append(elapsed_ms)

            # Extract retrieved content
            retrieved_texts = [r.document.content or "" for r in results]
            methods[method_name]["retrieved"].append(retrieved_texts)

        if (i + 1) % 200 == 0:
            print(f"    Evaluated {i+1}/{len(queries)} queries...")

    # Calculate metrics for each method
    for method_name, data in methods.items():
        metrics = calculate_metrics(data["retrieved"], gold_facts_all)
        metrics.avg_latency_ms = sum(data["latencies"]) / len(data["latencies"]) if data["latencies"] else 0

        if method_name == "vector":
            result.vector_metrics = metrics
        elif method_name == "maxsim":
            result.maxsim_metrics = metrics
        elif method_name == "fulltext":
            result.fulltext_metrics = metrics

    return result


def run_benchmark(config: BenchmarkConfig, session=None, root_id=None, skip_import=False) -> BenchmarkResult:
    """Run a single benchmark configuration"""
    print(f"\n{'='*60}")
    print(f"Benchmark: dim={config.embed_dim}, tier={config.max_tier} (top {config.max_tier}%)")
    print(f"{'='*60}")

    corpus = load_corpus()
    queries = load_queries()

    if config.limit_corpus:
        corpus = corpus[:config.limit_corpus]
    if config.limit_queries:
        queries = queries[:config.limit_queries]

    print(f"  Corpus: {len(corpus)} documents")
    print(f"  Queries: {len(queries)} queries")

    should_close = False
    if session is None:
        session = get_session().__enter__()
        should_close = True

    try:
        if not skip_import:
            # Wipe and import
            wipe_database(session)

            print("\n  [1] Importing corpus with chunking...")
            import_start = time.time()
            root_id, title_to_chunk_ids = import_corpus_chunked(session, corpus)
            import_time = time.time() - import_start

            # Get stats
            total_docs = session.query(Document).count()
            total_chunks = session.query(Document).filter(Document.usetype == "multihop:chunk").count()
            total_tokens = session.query(TokenEmbedding).count()
            print(f"  Imported {total_docs} docs ({total_chunks} chunks), {total_tokens} tokens in {import_time:.1f}s")

            print("\n  [2] Creating BM25 index...")
            create_bm25_index(session, root_id)
        else:
            import_time = 0
            total_docs = session.query(Document).count()
            total_chunks = session.query(Document).filter(Document.usetype == "multihop:chunk").count()
            total_tokens = session.query(TokenEmbedding).count()

        print(f"\n  [3] Evaluating retrieval (tier={config.max_tier})...")
        result = evaluate_retrieval(session, queries, root_id, config)

        result.import_time_sec = import_time
        result.total_docs = total_docs
        result.total_chunks = total_chunks
        result.total_tokens = total_tokens

        return result, root_id

    finally:
        if should_close:
            session.close()


def print_metrics(metrics: BenchmarkMetrics, method: str):
    """Print metrics for a single method"""
    print(f"    {method:10s}: H@4={metrics.hits_at_4:.1%} H@10={metrics.hits_at_10:.1%} "
          f"MAP={metrics.map_at_10:.3f} MRR={metrics.mrr_at_10:.3f} ({metrics.avg_latency_ms:.0f}ms)")


def print_results(results: list[BenchmarkResult]):
    """Print benchmark results summary"""
    print("\n" + "="*80)
    print("BENCHMARK RESULTS SUMMARY")
    print("="*80)

    print(f"\n{'Config':<15} {'Method':<10} {'Hits@4':>8} {'Hits@10':>8} {'MAP@10':>8} {'MRR@10':>8} {'Latency':>10}")
    print("-"*80)

    for r in results:
        config_str = f"d{r.config.embed_dim}/t{r.config.max_tier}"

        # Vector
        m = r.vector_metrics
        print(f"{config_str:<15} {'Vector':<10} {m.hits_at_4:>8.1%} {m.hits_at_10:>8.1%} "
              f"{m.map_at_10:>8.3f} {m.mrr_at_10:>8.3f} {m.avg_latency_ms:>9.0f}ms")

        # MaxSim
        m = r.maxsim_metrics
        print(f"{'':<15} {'MaxSim':<10} {m.hits_at_4:>8.1%} {m.hits_at_10:>8.1%} "
              f"{m.map_at_10:>8.3f} {m.mrr_at_10:>8.3f} {m.avg_latency_ms:>9.0f}ms")

        # Fulltext
        m = r.fulltext_metrics
        print(f"{'':<15} {'Fulltext':<10} {m.hits_at_4:>8.1%} {m.hits_at_10:>8.1%} "
              f"{m.map_at_10:>8.3f} {m.mrr_at_10:>8.3f} {m.avg_latency_ms:>9.0f}ms")

        print("-"*80)


def main():
    parser = argparse.ArgumentParser(description="MultiHop-RAG Benchmark")
    parser.add_argument("--quick", action="store_true",
                       help="Quick test with limited data")
    parser.add_argument("--dim", type=int, default=None,
                       help="Single dimension to test (256, 384, 512)")
    parser.add_argument("--tier", type=int, default=None,
                       help="Single tier to test (5, 10, 15, 20)")
    parser.add_argument("--sweep-tiers", action="store_true",
                       help="Sweep tiers without re-importing (requires prior import)")
    args = parser.parse_args()

    print("MultiHop-RAG Benchmark for JMFTS")
    print(f"Dataset: {CORPUS_FILE}")

    # Build configuration list
    if args.dim and args.tier:
        configs = [BenchmarkConfig(embed_dim=args.dim, max_tier=args.tier)]
    elif args.dim:
        configs = [BenchmarkConfig(embed_dim=args.dim, max_tier=t) for t in [5, 10, 15, 20]]
    elif args.tier:
        configs = [BenchmarkConfig(embed_dim=d, max_tier=args.tier) for d in [256, 384, 512]]
    else:
        # Default: test all tiers at 256 dim (one import, multiple evaluations)
        configs = [BenchmarkConfig(embed_dim=256, max_tier=t) for t in [5, 10, 15, 20]]

    if args.quick:
        for c in configs:
            c.limit_corpus = 50
            c.limit_queries = 200

    print(f"Running {len(configs)} configurations...")

    results = []

    if args.sweep_tiers or len(configs) > 1:
        # Efficient mode: import once, sweep tiers
        with get_session() as session:
            # Run first config with import
            first_config = configs[0]
            result, root_id = run_benchmark(first_config)
            results.append(result)

            # Run remaining configs without re-importing
            for config in configs[1:]:
                print(f"\n  [Tier sweep] Evaluating tier={config.max_tier}...")
                result, _ = run_benchmark(config, session=session, root_id=root_id, skip_import=True)
                results.append(result)
    else:
        # Single config
        result, _ = run_benchmark(configs[0])
        results.append(result)

    print_results(results)

    # Save results
    output_file = Path("/storage/jmfts_data/benchmark_results.json")
    with open(output_file, "w") as f:
        json.dump([{
            "embed_dim": r.config.embed_dim,
            "max_tier": r.config.max_tier,
            "import_time_sec": r.import_time_sec,
            "total_docs": r.total_docs,
            "total_chunks": r.total_chunks,
            "total_tokens": r.total_tokens,
            "vector": {
                "hits_at_4": r.vector_metrics.hits_at_4,
                "hits_at_10": r.vector_metrics.hits_at_10,
                "map_at_10": r.vector_metrics.map_at_10,
                "mrr_at_10": r.vector_metrics.mrr_at_10,
                "avg_latency_ms": r.vector_metrics.avg_latency_ms,
            },
            "maxsim": {
                "hits_at_4": r.maxsim_metrics.hits_at_4,
                "hits_at_10": r.maxsim_metrics.hits_at_10,
                "map_at_10": r.maxsim_metrics.map_at_10,
                "mrr_at_10": r.maxsim_metrics.mrr_at_10,
                "avg_latency_ms": r.maxsim_metrics.avg_latency_ms,
            },
            "fulltext": {
                "hits_at_4": r.fulltext_metrics.hits_at_4,
                "hits_at_10": r.fulltext_metrics.hits_at_10,
                "map_at_10": r.fulltext_metrics.map_at_10,
                "mrr_at_10": r.fulltext_metrics.mrr_at_10,
                "avg_latency_ms": r.fulltext_metrics.avg_latency_ms,
            },
            "queries_evaluated": r.vector_metrics.queries_evaluated,
        } for r in results], f, indent=2)
    print(f"\nResults saved to {output_file}")


if __name__ == "__main__":
    main()
