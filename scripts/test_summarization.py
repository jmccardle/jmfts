#!/usr/bin/env python3
"""
Quick test: Load XSum sample, run Gemma 3 inference for summarization.
"""

import os
import subprocess
import json
import time
from pathlib import Path

# Set HuggingFace cache to our storage location
os.environ["HF_HOME"] = "/storage/jmfts_data/huggingface_cache"
os.environ["HF_DATASETS_CACHE"] = "/storage/jmfts_data/huggingface_cache/datasets"

from datasets import load_dataset

# Paths
LLAMA_CPP_BIN = "/storage/THUDM_GLM-4-32B-0414-GGUF/llama.cpp/build/bin"
GEMMA3_MODEL = "/fast/model/google_gemma-3-27b-it-Q6_K_L.gguf"
MINISTRAL_MODEL = "/fast/model/Ministral-8B-Instruct-2410-Q8_0.gguf"


def load_xsum_sample(n=1):
    """Load n samples from XSum test set."""
    print("Loading XSum dataset...")
    ds = load_dataset("EdinburghNLP/xsum", split="test", trust_remote_code=True)
    samples = []
    for i in range(min(n, len(ds))):
        samples.append({
            "id": ds[i]["id"],
            "document": ds[i]["document"],
            "reference_summary": ds[i]["summary"],
        })
    return samples


def run_inference(model_path: str, prompt: str, max_tokens: int = 150) -> tuple[str, float]:
    """Run llama.cpp inference and return (output, latency_ms)."""

    # Use gemma3-cli for gemma models, regular cli otherwise
    if "gemma" in model_path.lower():
        cli = f"{LLAMA_CPP_BIN}/llama-gemma3-cli"
    else:
        cli = f"{LLAMA_CPP_BIN}/llama-cli"

    cmd = [
        cli,
        "-m", model_path,
        "-p", prompt,
        "-n", str(max_tokens),
        "--temp", "0.3",
        "-ngl", "99",  # Offload all layers to GPU
        "--no-display-prompt",
        "-e",  # Escape sequences
    ]

    start = time.time()
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=300,
    )
    elapsed_ms = (time.time() - start) * 1000

    if result.returncode != 0:
        print(f"Error: {result.stderr[:500]}")
        return "", elapsed_ms

    # Extract just the generated text (after prompt)
    output = result.stdout.strip()
    return output, elapsed_ms


def format_prompt(document: str, model_type: str = "gemma") -> str:
    """Format summarization prompt for the model."""

    if model_type == "gemma":
        # Gemma 3 uses a specific chat format
        return f"""<start_of_turn>user
Summarize the following news article in one sentence:

{document}
<end_of_turn>
<start_of_turn>model
"""
    else:
        # Ministral/Mistral format
        return f"""[INST] Summarize the following news article in one sentence:

{document} [/INST]"""


def main():
    print("=" * 70)
    print("Summarization Test: XSum + Local LLM")
    print("=" * 70)

    # Load one sample
    samples = load_xsum_sample(1)
    sample = samples[0]

    print(f"\n--- Document (first 500 chars) ---")
    print(sample["document"][:500] + "...")
    print(f"\n--- Reference Summary ---")
    print(sample["reference_summary"])

    # Test with Gemma 3 27B
    print(f"\n--- Testing Gemma 3 27B ---")
    prompt = format_prompt(sample["document"], "gemma")

    output, latency = run_inference(GEMMA3_MODEL, prompt)

    print(f"Generated summary: {output}")
    print(f"Latency: {latency:.0f}ms")

    # Save result
    result = {
        "document_id": sample["id"],
        "document_preview": sample["document"][:200],
        "reference_summary": sample["reference_summary"],
        "generated_summary": output,
        "latency_ms": latency,
        "model": "gemma-3-27b-it-Q6_K_L",
    }

    output_path = Path("/storage/jmfts_data/summarization_test.json")
    with open(output_path, "w") as f:
        json.dump(result, f, indent=2)

    print(f"\nResult saved to: {output_path}")


if __name__ == "__main__":
    main()
