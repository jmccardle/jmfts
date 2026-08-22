#!/usr/bin/env python3
"""
Batch Summarization Benchmark on XSum Dataset

Runs multiple local LLMs on XSum test set, saves outputs per-model,
and computes ROUGE scores against reference summaries.

Usage:
    python scripts/batch_summarize_xsum.py --models phi gemma4b gemma27b
    python scripts/batch_summarize_xsum.py --models phi --samples 100  # quick test
    python scripts/batch_summarize_xsum.py --evaluate-only  # score existing outputs
"""

import os
import re
import sys
import json
import time
import argparse
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Optional

# Set HuggingFace cache
os.environ["HF_HOME"] = "/storage/jmfts_data/huggingface_cache"
os.environ["HF_DATASETS_CACHE"] = "/storage/jmfts_data/huggingface_cache/datasets"

sys.stdout.reconfigure(line_buffering=True)

from datasets import load_dataset
from rouge_score import rouge_scorer

# Configuration
LLAMA_CLI = "/storage/THUDM_GLM-4-32B-0414-GGUF/llama.cpp/build/bin/llama-cli"
SPIRITBUUN_COMPLETION = (
    "/home/john/Development/turboquant_experiments/repos/"
    "spiritbuun-llama-cpp/build/bin/llama-completion"
)
OUTPUT_BASE = Path("/storage/jmfts_data/xsum_summaries")

# Model configurations: (path, name, context_size, gpu_layers, prompt_template)
MODEL_CONFIGS = {
    "phi": {
        "path": "/fast/model/Phi-3.5-mini-instruct-Q6_K_L.gguf",
        "name": "Phi-3.5-mini-Q6",
        "context": 4096,
        "gpu_layers": 99,
        "template": "phi",
    },
    "gemma4b": {
        "path": "/fast/google_gemma-3-4b-it-Q8_0.gguf",
        "name": "Gemma-3-4B-Q8",
        "context": 4096,
        "gpu_layers": 99,
        "template": "gemma",
    },
    "gemma27b": {
        "path": "/fast/model/google_gemma-3-27b-it-Q6_K_L.gguf",
        "name": "Gemma-3-27B-Q6",
        "context": 1024,  # Reduced for VRAM
        "gpu_layers": 99,
        "template": "gemma",
    },
    "glm4": {
        "path": "/storage/THUDM_GLM-4-32B-0414-GGUF/THUDM_GLM-4-32B-0414-Q4_K_M.gguf",
        "name": "GLM-4-32B-Q4",
        "context": 2048,
        "gpu_layers": 50,  # Partial offload
        "template": "plain",
    },
    "ministral": {
        "path": "/fast/model/Ministral-8B-Instruct-2410-Q8_0.gguf",
        "name": "Ministral-8B-Q8",
        "context": 4096,
        "gpu_layers": 99,
        "template": "mistral",
    },
    "qwen3b": {
        "path": "/fast/model/qwen2.5-3b-instruct-q6_k.gguf",
        "name": "Qwen2.5-3B-Q6",
        "context": 4096,
        "gpu_layers": 99,
        "template": "plain",
    },
    "qwen32b": {
        "path": "/fast/Qwen_Qwen3-32B-Q4_K_M.gguf",
        "name": "Qwen3-32B-Q4KM",
        "context": 4096,
        "gpu_layers": 99,
        "template": "qwen3",
        "binary": "spiritbuun",
    },
}


def format_prompt(document: str, template: str) -> str:
    """Format summarization prompt for different model types."""
    instruction = "Summarize this news article in one sentence:"

    if template == "qwen3":
        return (
            f"<|im_start|>system\nYou are a helpful assistant. /no_think<|im_end|>\n"
            f"<|im_start|>user\n{instruction}\n\n{document}<|im_end|>\n"
            f"<|im_start|>assistant\n"
        )
    elif template == "gemma":
        return f"""<start_of_turn>user
{instruction}

{document}
<end_of_turn>
<start_of_turn>model
"""
    elif template == "phi":
        return f"""<|user|>
{instruction}

{document}<|end|>
<|assistant|>
"""
    elif template == "mistral":
        return f"""[INST] {instruction}

{document} [/INST]"""
    else:  # plain
        return f"""{instruction}

{document}

Summary:"""


def run_inference(model_config: dict, prompt: str, timeout: int = 120) -> tuple[str, float, bool]:
    """
    Run llama.cpp inference.
    Returns: (output_text, latency_ms, success)
    """
    prompt_file = Path("/tmp/summarize_prompt.txt")
    prompt_file.write_text(prompt)

    binary = LLAMA_CLI
    extra_args = ["-no-cnv"]
    if model_config.get("binary") == "spiritbuun":
        binary = SPIRITBUUN_COMPLETION
        extra_args = []

    cmd = [
        binary,
        "-m", model_config["path"],
        "-f", str(prompt_file),
        "-n", "150",  # Max tokens for summary
        "-ngl", str(model_config["gpu_layers"]),
        "-c", str(model_config["context"]),
        "--temp", "0.3",
        "--no-display-prompt",
        *extra_args,
    ]

    env = None
    if model_config.get("binary") == "spiritbuun":
        env = {**os.environ, "GGML_TURBO_DECODE_NATIVE": "1"}

    start = time.time()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=env,
        )
        elapsed_ms = (time.time() - start) * 1000

        if result.returncode != 0:
            return "", elapsed_ms, False

        # Extract generated text, clean up
        output = result.stdout.strip()
        # Strip Qwen3 thinking block if present
        output = re.sub(r"<think>[\s\S]*?</think>\s*", "", output).strip()
        # Remove common artifacts
        for marker in ["[end of text]", "<|end|>", "</s>", "<end_of_turn>", "<|im_end|>"]:
            if marker in output:
                output = output.split(marker)[0].strip()

        # Take first sentence/paragraph if multiple generated
        lines = [l.strip() for l in output.split("\n") if l.strip()]
        if lines:
            output = lines[0]

        return output, elapsed_ms, True

    except subprocess.TimeoutExpired:
        return "", timeout * 1000, False
    except Exception as e:
        return str(e), 0, False


def load_xsum_test(n_samples: Optional[int] = None) -> list[dict]:
    """Load XSum test set."""
    print("Loading XSum dataset...")
    ds = load_dataset("EdinburghNLP/xsum", split="test")

    samples = []
    limit = n_samples if n_samples else len(ds)

    for i in range(min(limit, len(ds))):
        samples.append({
            "id": ds[i]["id"],
            "document": ds[i]["document"],
            "reference": ds[i]["summary"],
        })

    print(f"Loaded {len(samples)} samples")
    return samples


def compute_rouge(predictions: list[str], references: list[str]) -> dict:
    """Compute ROUGE scores."""
    scorer = rouge_scorer.RougeScorer(['rouge1', 'rouge2', 'rougeL'], use_stemmer=True)

    scores = {"rouge1": [], "rouge2": [], "rougeL": []}

    for pred, ref in zip(predictions, references):
        if not pred:  # Skip failed generations
            continue
        result = scorer.score(ref, pred)
        scores["rouge1"].append(result["rouge1"].fmeasure)
        scores["rouge2"].append(result["rouge2"].fmeasure)
        scores["rougeL"].append(result["rougeL"].fmeasure)

    return {
        "rouge1": sum(scores["rouge1"]) / len(scores["rouge1"]) if scores["rouge1"] else 0,
        "rouge2": sum(scores["rouge2"]) / len(scores["rouge2"]) if scores["rouge2"] else 0,
        "rougeL": sum(scores["rougeL"]) / len(scores["rougeL"]) if scores["rougeL"] else 0,
        "n_scored": len(scores["rouge1"]),
    }


def run_model_benchmark(model_key: str, samples: list[dict], output_dir: Path) -> dict:
    """Run benchmark for a single model."""
    config = MODEL_CONFIGS[model_key]
    model_name = config["name"]

    print(f"\n{'='*60}")
    print(f"Running: {model_name}")
    print(f"{'='*60}")

    # Check model exists
    if not Path(config["path"]).exists():
        print(f"ERROR: Model file not found: {config['path']}")
        return {"error": "Model file not found"}

    output_dir.mkdir(parents=True, exist_ok=True)
    results_file = output_dir / f"{model_key}_results.jsonl"

    # Check for existing progress
    existing_ids = set()
    if results_file.exists():
        with open(results_file) as f:
            for line in f:
                try:
                    existing_ids.add(json.loads(line)["id"])
                except:
                    pass
        print(f"Resuming: {len(existing_ids)} samples already processed")

    predictions = []
    references = []
    latencies = []
    failures = 0

    start_time = time.time()

    with open(results_file, "a") as f:
        for i, sample in enumerate(samples):
            # Skip already processed
            if sample["id"] in existing_ids:
                # Load existing result for scoring
                continue

            # Progress update
            if (i + 1) % 10 == 0 or i == 0:
                elapsed = time.time() - start_time
                rate = (i + 1 - len(existing_ids)) / elapsed if elapsed > 0 else 0
                eta = (len(samples) - i - 1) / rate if rate > 0 else 0
                print(f"  [{i+1}/{len(samples)}] {rate:.2f} samples/s, ETA: {eta/60:.1f}m", flush=True)

            # Format and run
            prompt = format_prompt(sample["document"], config["template"])
            output, latency, success = run_inference(config, prompt)

            result = {
                "id": sample["id"],
                "reference": sample["reference"],
                "generated": output,
                "latency_ms": latency,
                "success": success,
                "timestamp": datetime.now().isoformat(),
            }

            f.write(json.dumps(result) + "\n")
            f.flush()

            if success:
                predictions.append(output)
                references.append(sample["reference"])
                latencies.append(latency)
            else:
                failures += 1

    # Load all results for scoring (including previously processed)
    all_predictions = []
    all_references = []
    all_latencies = []

    with open(results_file) as f:
        for line in f:
            try:
                r = json.loads(line)
                if r.get("success", True) and r.get("generated"):
                    all_predictions.append(r["generated"])
                    all_references.append(r["reference"])
                    all_latencies.append(r.get("latency_ms", 0))
            except:
                pass

    # Compute metrics
    rouge_scores = compute_rouge(all_predictions, all_references)
    avg_latency = sum(all_latencies) / len(all_latencies) if all_latencies else 0

    summary = {
        "model": model_name,
        "model_key": model_key,
        "samples_total": len(samples),
        "samples_successful": len(all_predictions),
        "failures": failures,
        "avg_latency_ms": avg_latency,
        **rouge_scores,
    }

    # Save summary
    summary_file = output_dir / f"{model_key}_summary.json"
    with open(summary_file, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\n{model_name} Results:")
    print(f"  Samples: {summary['samples_successful']}/{summary['samples_total']}")
    print(f"  ROUGE-1: {rouge_scores['rouge1']:.4f}")
    print(f"  ROUGE-2: {rouge_scores['rouge2']:.4f}")
    print(f"  ROUGE-L: {rouge_scores['rougeL']:.4f}")
    print(f"  Avg Latency: {avg_latency:.0f}ms")

    return summary


def evaluate_existing(output_dir: Path) -> None:
    """Evaluate all existing model outputs in directory."""
    print("Evaluating existing outputs...")

    results = []
    for results_file in output_dir.glob("*_results.jsonl"):
        model_key = results_file.stem.replace("_results", "")

        predictions = []
        references = []
        latencies = []

        with open(results_file) as f:
            for line in f:
                try:
                    r = json.loads(line)
                    if r.get("success", True) and r.get("generated"):
                        predictions.append(r["generated"])
                        references.append(r["reference"])
                        latencies.append(r.get("latency_ms", 0))
                except:
                    pass

        if not predictions:
            continue

        rouge_scores = compute_rouge(predictions, references)
        avg_latency = sum(latencies) / len(latencies) if latencies else 0

        results.append({
            "model": model_key,
            "n_samples": len(predictions),
            "avg_latency_ms": avg_latency,
            **rouge_scores,
        })

    # Print comparison table
    print("\n" + "="*80)
    print("EVALUATION RESULTS")
    print("="*80)
    print(f"{'Model':<25} {'N':>6} {'ROUGE-1':>10} {'ROUGE-2':>10} {'ROUGE-L':>10} {'Latency':>10}")
    print("-"*80)

    for r in sorted(results, key=lambda x: x["rougeL"], reverse=True):
        print(f"{r['model']:<25} {r['n_samples']:>6} {r['rouge1']:>10.4f} {r['rouge2']:>10.4f} "
              f"{r['rougeL']:>10.4f} {r['avg_latency_ms']:>9.0f}ms")

    # Save combined results
    combined_file = output_dir / "evaluation_summary.json"
    with open(combined_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to: {combined_file}")


def main():
    parser = argparse.ArgumentParser(description="Batch Summarization Benchmark")
    parser.add_argument("--models", nargs="+", choices=list(MODEL_CONFIGS.keys()),
                        default=["phi", "gemma4b", "gemma27b"],
                        help="Models to run")
    parser.add_argument("--samples", type=int, default=None,
                        help="Number of samples (default: full test set ~11K)")
    parser.add_argument("--evaluate-only", action="store_true",
                        help="Only evaluate existing outputs")
    args = parser.parse_args()

    output_dir = OUTPUT_BASE
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.evaluate_only:
        evaluate_existing(output_dir)
        return

    # Load dataset
    samples = load_xsum_test(args.samples)

    print(f"\nModels to run: {args.models}")
    print(f"Samples: {len(samples)}")
    print(f"Output directory: {output_dir}")

    # Run each model
    all_results = []
    for model_key in args.models:
        try:
            result = run_model_benchmark(model_key, samples, output_dir)
            all_results.append(result)
        except Exception as e:
            print(f"ERROR running {model_key}: {e}")
            all_results.append({"model": model_key, "error": str(e)})

    # Final summary
    print("\n" + "="*80)
    print("FINAL SUMMARY")
    print("="*80)

    for r in all_results:
        if "error" in r:
            print(f"{r['model']}: ERROR - {r['error']}")
        else:
            print(f"{r['model']}: ROUGE-L={r['rougeL']:.4f}, Latency={r['avg_latency_ms']:.0f}ms")

    # Save combined results
    combined_file = output_dir / "benchmark_results.json"
    with open(combined_file, "w") as f:
        json.dump({
            "timestamp": datetime.now().isoformat(),
            "n_samples": len(samples),
            "models": args.models,
            "results": all_results,
        }, f, indent=2)

    print(f"\nAll results saved to: {output_dir}")


if __name__ == "__main__":
    main()
