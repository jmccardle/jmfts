"""RAPTOR Quality Benchmark — Test Report 002.

Ingests a test document, chunks it with real embeddings, runs RAPTOR
hierarchical summarization against GLM-4-32B, then measures:
  1. Embedding similarity (summary vs source centroid)
  2. Compression ratio
  3. Summary coherence (prints first 3 for manual inspection)
  4. Tree structure analysis (clusters, depth, bridge links)
"""

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Must set env vars BEFORE importing jmfts_core (lru_cache on get_settings) ──
os.environ["JMFTS_LLM_BASE_URL"] = "http://localhost:8101/v1"
os.environ["JMFTS_LLM_MODEL"] = "THUDM_GLM-4-32B-0414-Q4_K_M.gguf"
os.environ["JMFTS_LLM_TIMEOUT"] = "300"  # GLM-4-32B can be slow

import numpy as np

from jmfts_core.config import get_settings
from jmfts_core.chunking import chunk_text, ChunkStrategy
from jmfts_core.database import get_session
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.summarization import raptor_summarize

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEST_ARTICLE = PROJECT_ROOT / "test_docs" / "articles" / "wiki0-3D printing.txt"
REPORT_DIR = PROJECT_ROOT / "test_reports"


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    a = a / (np.linalg.norm(a) + 1e-9)
    b = b / (np.linalg.norm(b) + 1e-9)
    return float(np.dot(a, b))


def print_header(msg: str) -> None:
    print(f"\n{'='*70}")
    print(f"  {msg}")
    print(f"{'='*70}")


async def run_benchmark():
    settings = get_settings()
    print(f"LLM endpoint: {settings.llm_base_url}")
    print(f"LLM model:    {settings.llm_model}")
    print(f"LLM timeout:  {settings.llm_timeout}s")

    # ── 1. Read test article ──
    print_header("1. Ingesting test document")
    article_text = TEST_ARTICLE.read_text(encoding="utf-8")
    word_count = len(article_text.split())
    print(f"Article: {TEST_ARTICLE.name} ({word_count} words, {len(article_text)} chars)")

    # ── 2. Chunk it ──
    chunks = chunk_text(article_text, strategy=ChunkStrategy.paragraph)
    print(f"Chunked into {len(chunks)} paragraphs")

    # ── 3. Create parent + children with embeddings in DB ──
    with get_session() as session:
        repo = DocumentRepository(session)

        parent = repo.create(
            title="[BENCHMARK] 3D Printing — RAPTOR Quality Test",
            content=article_text[:500],  # store excerpt only
            usetype="source",
            auto_embed=False,
        )
        parent_id = parent.id
        print(f"Created parent document: id={parent_id}")

        child_ids = []
        for chunk in chunks:
            if len(chunk.text.strip()) < 20:
                continue  # skip trivial chunks
            child = repo.create(
                title=f"Chunk {chunk.index}",
                content=chunk.text,
                parent_id=parent_id,
                usetype="chunk",
                auto_embed=True,  # generates real embeddings
            )
            child_ids.append(child.id)

        embedded_count = sum(
            1 for cid in child_ids if repo.get(cid).embed is not None
        )
        print(f"Created {len(child_ids)} chunk children, {embedded_count} with embeddings")

        # Collect source embeddings for later metric computation
        source_data = []  # (id, content, embedding)
        for cid in child_ids:
            doc = repo.get(cid)
            if doc and doc.embed is not None:
                source_data.append((cid, doc.content, np.array(doc.embed, dtype=np.float32)))

    # ── 4. Run RAPTOR ──
    print_header("2. Running RAPTOR summarization with GLM-4-32B")
    t0 = time.time()

    with get_session() as session:
        result = await raptor_summarize(
            document_id=parent_id,
            session=session,
            max_depth=5,
            min_cluster_size=2,
            llm_model=settings.llm_model,
            max_summary_tokens=512,
        )
        elapsed = time.time() - t0
        print(f"RAPTOR completed in {elapsed:.1f}s")
        print(f"  Layers:       {len(result.layers)}")
        print(f"  Summaries:    {result.total_summaries}")
        print(f"  Bridge links: {result.total_bridge_links}")

        for lr in result.layers:
            print(f"    Layer {lr.layer}: {lr.clusters} clusters, "
                  f"{len(lr.summary_ids)} summaries, {lr.bridge_links_created} bridges")

        # ── 5. Collect summary data ──
        repo = DocumentRepository(session)
        summary_rows = []  # dicts for report table

        all_summary_ids = []
        for lr in result.layers:
            all_summary_ids.extend(lr.summary_ids)

        for sid in all_summary_ids:
            sdoc = repo.get(sid)
            if not sdoc:
                continue

            summary_embed = np.array(sdoc.embed, dtype=np.float32) if sdoc.embed is not None else None
            sc = sdoc.structured_content or {}
            member_ids = sc.get("member_ids", [])
            layer = sc.get("raptor_layer", -1)

            # Get member embeddings & text
            member_embeds = []
            member_texts = []
            for mid in member_ids:
                mdoc = repo.get(mid)
                if mdoc:
                    if mdoc.embed is not None:
                        member_embeds.append(np.array(mdoc.embed, dtype=np.float32))
                    if mdoc.content:
                        member_texts.append(mdoc.content)

            # Compute centroid
            if member_embeds:
                centroid = np.mean(member_embeds, axis=0)
            else:
                centroid = None

            # Embedding similarity
            if summary_embed is not None and centroid is not None:
                sim = cosine_similarity(summary_embed, centroid)
            else:
                sim = None

            # Compression ratio
            summary_len = len(sdoc.content) if sdoc.content else 0
            source_len = sum(len(t) for t in member_texts)
            compression = summary_len / source_len if source_len > 0 else None

            # Get links
            links = repo.get_links(sid, direction="outgoing", link_type="summarizes")

            summary_rows.append({
                "id": sid,
                "layer": layer,
                "member_count": len(member_ids),
                "summary_len": summary_len,
                "source_len": source_len,
                "compression_ratio": compression,
                "embedding_similarity": sim,
                "link_count": len(links),
                "content": sdoc.content,
                "member_texts": member_texts,
            })

    # ── 6. Compute aggregate metrics ──
    print_header("3. Quality Metrics")

    sims = [r["embedding_similarity"] for r in summary_rows if r["embedding_similarity"] is not None]
    comps = [r["compression_ratio"] for r in summary_rows if r["compression_ratio"] is not None]

    if sims:
        mean_sim = np.mean(sims)
        min_sim = np.min(sims)
        max_sim = np.max(sims)
        sim_pass = mean_sim > 0.7
        print(f"Embedding Similarity:  mean={mean_sim:.3f}  min={min_sim:.3f}  max={max_sim:.3f}")
        print(f"  Threshold: > 0.7  →  {'PASS' if sim_pass else 'FAIL'}")
    else:
        mean_sim = min_sim = max_sim = None
        sim_pass = False
        print("Embedding Similarity:  NO DATA")

    if comps:
        mean_comp = np.mean(comps)
        min_comp = np.min(comps)
        max_comp = np.max(comps)
        comp_pass = 0.15 <= mean_comp <= 0.40
        print(f"Compression Ratio:     mean={mean_comp:.3f}  min={min_comp:.3f}  max={max_comp:.3f}")
        print(f"  Threshold: 0.15-0.40  →  {'PASS' if comp_pass else 'FAIL'}")
    else:
        mean_comp = min_comp = max_comp = None
        comp_pass = False
        print("Compression Ratio:  NO DATA")

    # Tree structure
    tree_depth = len(result.layers)
    total_clusters = sum(lr.clusters for lr in result.layers)
    total_bridges = result.total_bridge_links

    print(f"\nTree Structure:")
    print(f"  Depth:          {tree_depth} layers")
    print(f"  Total clusters: {total_clusters}")
    print(f"  Total summaries:{result.total_summaries}")
    print(f"  Bridge links:   {total_bridges}")
    print(f"  Source chunks:  {embedded_count}")
    compactness = result.total_summaries / embedded_count if embedded_count > 0 else 0
    compact_pass = compactness < 0.50
    print(f"  Compactness:    {compactness:.2f} (summaries/chunks, threshold < 0.50) → {'PASS' if compact_pass else 'FAIL'}")

    # ── 7. Print sample summaries ──
    print_header("4. Sample Summaries (first 3)")
    for i, row in enumerate(summary_rows[:3]):
        print(f"\n--- Summary {i+1} (ID {row['id']}, Layer {row['layer']}, "
              f"{row['member_count']} members) ---")
        print(f"Embedding sim: {row['embedding_similarity']:.3f}" if row["embedding_similarity"] else "Embedding sim: N/A")
        print(f"Compression:   {row['compression_ratio']:.3f}" if row["compression_ratio"] else "Compression: N/A")
        print(f"\nSUMMARY:\n{row['content'][:800]}")
        print(f"\nSOURCE EXCERPTS:")
        for j, mt in enumerate(row["member_texts"][:3]):
            print(f"  [{j+1}] {mt[:200]}...")

    # ── 8. Overall verdict ──
    overall = sim_pass and comp_pass and compact_pass
    print_header("5. Overall Verdict")
    print(f"  Embedding Similarity: {'PASS' if sim_pass else 'FAIL'}")
    print(f"  Compression Ratio:    {'PASS' if comp_pass else 'FAIL'}")
    print(f"  Tree Compactness:     {'PASS' if compact_pass else 'FAIL'}")
    print(f"  OVERALL:              {'PASS' if overall else 'FAIL'}")

    # ── 9. Generate report ──
    print_header("6. Generating report")
    REPORT_DIR.mkdir(exist_ok=True)
    report = generate_report(
        summary_rows=summary_rows,
        result=result,
        embedded_count=embedded_count,
        parent_id=parent_id,
        word_count=word_count,
        elapsed=elapsed,
        sims=sims,
        comps=comps,
        mean_sim=mean_sim,
        min_sim=min_sim,
        max_sim=max_sim,
        sim_pass=sim_pass,
        mean_comp=mean_comp,
        min_comp=min_comp,
        max_comp=max_comp,
        comp_pass=comp_pass,
        compact_pass=compact_pass,
        compactness=compactness,
        total_bridges=total_bridges,
        tree_depth=tree_depth,
        overall=overall,
    )
    report_path = REPORT_DIR / "002_raptor_quality.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"Report written to {report_path}")

    # Return data for Kanboard posting
    return {
        "overall": overall,
        "mean_sim": mean_sim,
        "mean_comp": mean_comp,
        "compactness": compactness,
        "tree_depth": tree_depth,
        "total_summaries": result.total_summaries,
        "total_bridges": total_bridges,
        "elapsed": elapsed,
        "sim_pass": sim_pass,
        "comp_pass": comp_pass,
        "compact_pass": compact_pass,
    }


def generate_report(
    summary_rows, result, embedded_count, parent_id, word_count, elapsed,
    sims, comps, mean_sim, min_sim, max_sim, sim_pass,
    mean_comp, min_comp, max_comp, comp_pass,
    compact_pass, compactness, total_bridges, tree_depth, overall,
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Per-summary table
    table_rows = []
    for r in summary_rows:
        sim_str = f"{r['embedding_similarity']:.3f}" if r["embedding_similarity"] is not None else "N/A"
        comp_str = f"{r['compression_ratio']:.3f}" if r["compression_ratio"] is not None else "N/A"
        table_rows.append(
            f"| {r['id']} | L{r['layer']} | {r['member_count']} | "
            f"{r['summary_len']} | {r['source_len']} | {comp_str} | {sim_str} |"
        )
    table = "\n".join(table_rows)

    # Sample summaries
    samples = []
    for i, r in enumerate(summary_rows[:3]):
        source_excerpts = "\n".join(
            f"> **[{j+1}]** {t[:300]}{'...' if len(t) > 300 else ''}"
            for j, t in enumerate(r["member_texts"][:3])
        )
        sim_str = f"{r['embedding_similarity']:.3f}" if r["embedding_similarity"] is not None else "N/A"
        comp_str = f"{r['compression_ratio']:.3f}" if r["compression_ratio"] is not None else "N/A"
        samples.append(f"""### Summary {i+1} (ID {r['id']}, Layer {r['layer']}, {r['member_count']} members)

- **Embedding similarity**: {sim_str}
- **Compression ratio**: {comp_str}

**Summary text:**

> {r['content'][:1000] if r['content'] else '(empty)'}

**Source chunk excerpts:**

{source_excerpts}
""")
    samples_text = "\n".join(samples)

    # Layer breakdown
    layer_rows = []
    for lr in result.layers:
        layer_rows.append(
            f"| {lr.layer} | {lr.clusters} | {len(lr.summary_ids)} | {lr.bridge_links_created} |"
        )
    layer_table = "\n".join(layer_rows)

    return f"""# Test Report 002: RAPTOR Quality Benchmarks

**Date**: {now}
**Commit**: 3bf7855 (RAPTOR hierarchical summarization)
**LLM**: GLM-4-32B (THUDM_GLM-4-32B-0414-Q4_K_M.gguf) via llama.cpp on port 8101
**Test Data**: wiki0-3D printing.txt ({word_count} words)
**Parent Document ID**: {parent_id}
**Execution Time**: {elapsed:.1f}s

## 1. Summary of Results

| Metric | Value | Threshold | Verdict |
|--------|-------|-----------|---------|
| Embedding similarity (mean) | {f'{mean_sim:.3f}' if mean_sim is not None else 'N/A'} | > 0.7 | **{'PASS' if sim_pass else 'FAIL'}** |
| Compression ratio (mean) | {f'{mean_comp:.3f}' if mean_comp is not None else 'N/A'} | 0.15-0.40 | **{'PASS' if comp_pass else 'FAIL'}** |
| Tree compactness | {compactness:.2f} | < 0.50 | **{'PASS' if compact_pass else 'FAIL'}** |
| **Overall** | | | **{'PASS' if overall else 'FAIL'}** |

### Embedding Similarity Distribution

- Mean: {f'{mean_sim:.3f}' if mean_sim is not None else 'N/A'}
- Min: {f'{min_sim:.3f}' if min_sim is not None else 'N/A'}
- Max: {f'{max_sim:.3f}' if max_sim is not None else 'N/A'}
- Threshold: > 0.7 (RAPTOR paper baseline with Phi: 0.863)

### Compression Ratio Distribution

- Mean: {f'{mean_comp:.3f}' if mean_comp is not None else 'N/A'}
- Min: {f'{min_comp:.3f}' if min_comp is not None else 'N/A'}
- Max: {f'{max_comp:.3f}' if max_comp is not None else 'N/A'}
- Threshold: 0.15-0.40 (RAPTOR paper avg: 0.28)

## 2. Per-Summary Scores

| ID | Layer | Members | Summary Len | Source Len | Compression | Similarity |
|----|-------|---------|-------------|------------|-------------|------------|
{table}

## 3. Tree Structure

| Layer | Clusters | Summaries | Bridge Links |
|-------|----------|-----------|--------------|
{layer_table}

- **Total depth**: {tree_depth} layers
- **Total summaries**: {result.total_summaries}
- **Total bridge links**: {total_bridges}
- **Source chunks**: {embedded_count}
- **Compactness ratio**: {compactness:.2f} (summaries / source chunks)

## 4. Sample Summaries with Source Text

{samples_text}

## 5. Coherence Assessment

Manual inspection of the {min(3, len(summary_rows))} sample summaries above. Assess:
- Does each summary capture the key information from its source chunks?
- Is the language coherent and well-structured?
- Are there any hallucinations (claims not supported by the source text)?

## 6. Verdict

| Metric | Verdict |
|--------|---------|
| Embedding Similarity | **{'PASS' if sim_pass else 'FAIL'}** |
| Compression Ratio | **{'PASS' if comp_pass else 'FAIL'}** |
| Tree Compactness | **{'PASS' if compact_pass else 'FAIL'}** |
| **Overall** | **{'PASS' if overall else 'FAIL'}** |
"""


if __name__ == "__main__":
    metrics = asyncio.run(run_benchmark())
    print(f"\nDone. Metrics: {json.dumps({k: round(v, 3) if isinstance(v, float) else bool(v) if isinstance(v, np.bool_) else v for k, v in metrics.items()})}")
