#!/usr/bin/env python3
"""
Compare embeddings of source texts, reference summaries, and generated summaries.
Measures semantic similarity rather than lexical overlap (ROUGE).

Usage:
    python scripts/embedding_similarity.py                     # compare all models found
    python scripts/embedding_similarity.py --models phi qwen32b
    python scripts/embedding_similarity.py --samples 200       # limit sample size
"""

import argparse
import json
import os
import random
from pathlib import Path

import numpy as np
from datasets import load_dataset
from sentence_transformers import SentenceTransformer

# Configuration
STORAGE_DIR = Path("/storage/jmfts_data")
RESULTS_DIR = STORAGE_DIR / "xsum_summaries"
SAMPLE_SIZE = 1000  # Number of samples to analyze
EMBEDDING_MODEL = "nomic-ai/modernbert-embed-base"  # Same as jmfts


def load_model_results(model_key: str, limit=None):
    """Load a model's generated summaries from JSONL results."""
    results_file = RESULTS_DIR / f"{model_key}_results.jsonl"
    if not results_file.exists():
        return {}
    results = {}
    with open(results_file) as f:
        for i, line in enumerate(f):
            if limit and i >= limit:
                break
            data = json.loads(line)
            if data.get("success") and data.get("generated"):
                results[data["id"]] = {
                    "reference": data["reference"],
                    "generated": data["generated"],
                }
    return results

def cosine_similarity(a, b):
    """Compute cosine similarity between two vectors."""
    return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))

def evaluate_model(model_key, model_results, xsum_by_id, embed_model, sample_size):
    """Evaluate embedding similarity for a single model's results."""
    matching_ids = [id for id in model_results.keys() if id in xsum_by_id]
    print(f"\n[{model_key}] Found {len(matching_ids)} matching documents")

    if not matching_ids:
        return None

    if len(matching_ids) > sample_size:
        random.seed(42)
        sample_ids = random.sample(matching_ids, sample_size)
    else:
        sample_ids = matching_ids

    print(f"[{model_key}] Analyzing {len(sample_ids)} samples...")

    sources = [xsum_by_id[doc_id] for doc_id in sample_ids]
    references = [model_results[doc_id]["reference"] for doc_id in sample_ids]
    generated = [model_results[doc_id]["generated"] for doc_id in sample_ids]

    print(f"[{model_key}] Embedding texts...")
    source_emb = embed_model.encode(sources, show_progress_bar=True)
    ref_emb = embed_model.encode(references, show_progress_bar=True)
    gen_emb = embed_model.encode(generated, show_progress_bar=True)

    source_ref = [cosine_similarity(source_emb[i], ref_emb[i]) for i in range(len(sample_ids))]
    source_gen = [cosine_similarity(source_emb[i], gen_emb[i]) for i in range(len(sample_ids))]
    ref_gen = [cosine_similarity(ref_emb[i], gen_emb[i]) for i in range(len(sample_ids))]

    return {
        "model": model_key,
        "sample_size": len(sample_ids),
        "source_reference": {"mean": float(np.mean(source_ref)), "std": float(np.std(source_ref))},
        "source_generated": {"mean": float(np.mean(source_gen)), "std": float(np.std(source_gen))},
        "reference_generated": {"mean": float(np.mean(ref_gen)), "std": float(np.std(ref_gen))},
    }


def main():
    parser = argparse.ArgumentParser(description="Embedding Similarity Benchmark")
    parser.add_argument("--models", nargs="+", default=None, help="Model keys to evaluate")
    parser.add_argument("--samples", type=int, default=SAMPLE_SIZE, help="Sample size per model")
    args = parser.parse_args()

    # Discover available models
    if args.models:
        model_keys = args.models
    else:
        model_keys = sorted(
            p.stem.replace("_results", "")
            for p in RESULTS_DIR.glob("*_results.jsonl")
        )

    print(f"Models to evaluate: {model_keys}")
    print(f"Loading embedding model: {EMBEDDING_MODEL}")
    embed_model = SentenceTransformer(EMBEDDING_MODEL, trust_remote_code=True)

    print("Loading XSum dataset...")
    os.environ["HF_HOME"] = str(STORAGE_DIR / "huggingface_cache")
    dataset = load_dataset("xsum", split="test", trust_remote_code=True)
    xsum_by_id = {item["id"]: item["document"] for item in dataset}

    all_results = []
    for model_key in model_keys:
        model_results = load_model_results(model_key)
        if not model_results:
            print(f"[{model_key}] No results found, skipping")
            continue
        print(f"[{model_key}] Loaded {len(model_results)} successful summaries")
        result = evaluate_model(model_key, model_results, xsum_by_id, embed_model, args.samples)
        if result:
            all_results.append(result)

    # Print comparison table
    print("\n" + "=" * 80)
    print("EMBEDDING SIMILARITY COMPARISON")
    print("=" * 80)
    print(f"{'Model':<20} {'N':>6} {'Src↔Gen':>10} {'Src↔Ref':>10} {'Ref↔Gen':>10}")
    print("-" * 80)
    for r in sorted(all_results, key=lambda x: x["source_generated"]["mean"], reverse=True):
        print(
            f"{r['model']:<20} {r['sample_size']:>6} "
            f"{r['source_generated']['mean']:>10.4f} "
            f"{r['source_reference']['mean']:>10.4f} "
            f"{r['reference_generated']['mean']:>10.4f}"
        )

    # Save results
    output_file = RESULTS_DIR / "embedding_similarity.json"
    with open(output_file, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\nResults saved to {output_file}")

if __name__ == "__main__":
    main()
