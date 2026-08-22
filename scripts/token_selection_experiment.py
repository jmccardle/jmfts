#!/usr/bin/env python3
"""
Token Selection Experiment - Extended Version

Compare different semantic methods for selecting important tokens.
Uses real documents from test_docs/, split into paragraphs.
Selects top 10% of tokens per document.
"""

import sys
sys.path.insert(0, "/home/john/Development/jmfts")

import os
import re
import torch
import torch.nn.functional as F
import numpy as np
from pathlib import Path
from sentence_transformers import SentenceTransformer

TEST_DOCS_DIR = Path("/home/john/Development/jmfts/test_docs")


def load_paragraphs_from_docs(docs_dir: Path, min_chars: int = 50) -> list[tuple[str, str]]:
    """
    Load documents and split into paragraphs.
    Returns list of (source_file, paragraph_text) tuples.
    """
    paragraphs = []

    # Patterns to filter out
    skip_patterns = [
        r'^https?://',           # URLs
        r'^Category:',           # Wiki categories
        r'^\s*#+ ',              # Markdown headers (keep content, skip titles)
        r'^---+$',               # Horizontal rules
        r'^\s*\*\*[^*]+\*\*:?\s*$',  # Bold-only lines (headers)
        r'^thumb\|',             # Wiki image refs
        r'^\d+\.\s*$',           # Just a number
        r'^References\s*$',      # Reference section headers
        r'^External links\s*$',
        r'^Videos:\s*$',
        r'^Websites\s*$',
    ]
    skip_re = [re.compile(p, re.IGNORECASE) for p in skip_patterns]

    for root, dirs, files in os.walk(docs_dir):
        for fname in files:
            if not (fname.endswith('.txt') or fname.endswith('.md')):
                continue

            fpath = Path(root) / fname
            try:
                content = fpath.read_text(encoding='utf-8')
            except Exception as e:
                print(f"Warning: Could not read {fpath}: {e}")
                continue

            # Split on double newline
            raw_paragraphs = re.split(r'\n\n+', content)

            for para in raw_paragraphs:
                # Clean up
                para = para.strip()

                # Skip if too short
                if len(para) < min_chars:
                    continue

                # Skip if matches filter patterns
                if any(p.search(para) for p in skip_re):
                    continue

                # Skip if mostly non-alphanumeric (URLs, references, etc.)
                alpha_ratio = sum(c.isalpha() for c in para) / len(para) if para else 0
                if alpha_ratio < 0.5:
                    continue

                paragraphs.append((fname, para))

    return paragraphs


def get_token_importance_scores(text: str, model: SentenceTransformer, device: str = "cpu"):
    """
    Compute token importance using multiple methods.
    Returns dict with scores for each method.
    """
    transformer = model[0].auto_model
    tokenizer = model.tokenizer

    # Tokenize
    inputs = tokenizer(
        text,
        return_tensors="pt",
        truncation=True,
        max_length=512,
        padding=True,
    )
    inputs = {k: v.to(device) for k, v in inputs.items()}

    # Forward pass with attention and hidden states
    with torch.no_grad():
        outputs = transformer(
            **inputs,
            output_attentions=True,
            output_hidden_states=True,
        )

    # Get components
    last_hidden = outputs.last_hidden_state[0]  # (seq_len, hidden_dim)
    attentions = outputs.attentions  # tuple of (batch, heads, seq, seq)

    # Decode tokens
    token_ids = inputs["input_ids"][0].cpu().tolist()
    tokens = tokenizer.convert_ids_to_tokens(token_ids)
    attention_mask = inputs["attention_mask"][0].cpu()

    seq_len = len(tokens)

    # Method A: Current (attention-weighted)
    attention_stack = torch.stack(attentions)
    avg_attention_received = attention_stack.mean(dim=(0, 1, 2)).sum(dim=0)
    method_a = avg_attention_received.cpu().numpy()

    # Method B: Value-Aware (Attention × Value L1 Norm)
    value_norms = torch.norm(last_hidden, p=1, dim=-1)
    method_b = (avg_attention_received * value_norms).cpu().numpy()

    # Method C: Embedding Norm (L2)
    embedding_norms = torch.norm(last_hidden, p=2, dim=-1)
    method_c = embedding_norms.cpu().numpy()

    # Method D: CLS Attention
    cls_attention = attention_stack[:, :, :, 0, :].mean(dim=(0, 1, 2))
    method_d = cls_attention.cpu().numpy()

    # Method E: Combined (CLS attention × embedding norm)
    method_e = (cls_attention * embedding_norms).cpu().numpy()

    # Method F: Inverse attention variance
    attn_to_tokens = attention_stack[:, 0, :, :, :].sum(dim=2)
    attn_variance = attn_to_tokens.var(dim=(0, 1))
    method_f = (1.0 / (attn_variance + 0.01)).cpu().numpy()

    # Method G: KeyBERT-style (token similarity to document embedding)
    mask_expanded = attention_mask.unsqueeze(-1).float().to(device)
    doc_embedding = (last_hidden * mask_expanded).sum(dim=0) / mask_expanded.sum()
    doc_embedding = F.normalize(doc_embedding, p=2, dim=0)
    token_norms = F.normalize(last_hidden, p=2, dim=-1)
    keybert_scores = torch.matmul(token_norms, doc_embedding)
    method_g = keybert_scores.cpu().numpy()

    # Method H: MMR (Maximum Marginal Relevance)
    lambda_param = 0.5
    token_similarities = torch.matmul(token_norms, token_norms.T).cpu().numpy()
    relevance = keybert_scores.cpu().numpy()
    selected_indices = []
    remaining = list(range(seq_len))

    # Select more tokens for MMR (will be trimmed to 10% later)
    for step in range(min(seq_len, 50)):
        if not remaining:
            break

        best_score = -float('inf')
        best_idx = remaining[0]

        for idx in remaining:
            if tokens[idx] in ['[CLS]', '[SEP]', '[PAD]']:
                continue

            rel = float(relevance[idx])
            if selected_indices:
                max_sim = float(max(token_similarities[idx, s] for s in selected_indices))
            else:
                max_sim = 0.0

            score = lambda_param * rel - (1 - lambda_param) * max_sim

            if score > best_score:
                best_score = score
                best_idx = idx

        selected_indices.append(best_idx)
        remaining.remove(best_idx)

    method_h = np.zeros(seq_len)
    for rank, idx in enumerate(selected_indices):
        method_h[idx] = float(len(selected_indices) - rank)

    # Method I: N-gram keyphrases
    ngram_scores = np.zeros(seq_len, dtype=np.float64)

    for n in [2, 3, 4]:
        for start in range(seq_len - n + 1):
            end = start + n

            if any(tokens[i] in ['[CLS]', '[SEP]', '[PAD]'] for i in range(start, end)):
                continue

            ngram_embeds = token_norms[start:end]

            if n > 1:
                pairwise_sims = []
                for i in range(n):
                    for j in range(i+1, n):
                        sim = float(torch.dot(ngram_embeds[i], ngram_embeds[j]).item())
                        pairwise_sims.append(sim)
                cohesion = float(np.mean(pairwise_sims)) if pairwise_sims else 0.0
            else:
                cohesion = 1.0

            ngram_centroid = ngram_embeds.mean(dim=0)
            ngram_centroid = F.normalize(ngram_centroid, p=2, dim=0)
            relevance_score = float(torch.dot(ngram_centroid, doc_embedding).item())

            combined = float((cohesion * 0.3 + relevance_score * 0.7) * (1 + 0.1 * n))

            for i in range(start, end):
                ngram_scores[i] = max(ngram_scores[i], combined)

    method_i = ngram_scores

    return {
        "tokens": tokens,
        "attention_mask": attention_mask.numpy(),
        "A_attention_only": method_a,
        "B_value_aware": method_b,
        "C_embedding_norm": method_c,
        "D_cls_attention": method_d,
        "E_cls_x_norm": method_e,
        "F_low_variance": method_f,
        "G_keybert": method_g,
        "H_mmr": method_h,
        "I_ngram": method_i,
    }


def is_content_token(token: str) -> bool:
    """Check if token looks like meaningful content (not punctuation/whitespace)."""
    clean = token.replace("Ġ", "").replace("Ċ", "")
    return len(clean) > 1 and any(c.isalpha() for c in clean)


def evaluate_method(results, scores_key, top_k: int):
    """
    Evaluate how many of the top-k selected tokens are content tokens.
    Returns (content_count, total_selected).
    """
    tokens = results["tokens"]
    scores = results[scores_key]
    mask = results["attention_mask"]

    # Get valid tokens with scores
    valid = []
    for i, (tok, score, m) in enumerate(zip(tokens, scores, mask)):
        if m == 1 and tok not in ['[CLS]', '[SEP]', '[PAD]']:
            valid.append((tok, float(score)))

    valid.sort(key=lambda x: -x[1])
    selected = valid[:top_k]

    content_count = sum(1 for tok, _ in selected if is_content_token(tok))
    return content_count, len(selected)


def get_top_tokens(results, scores_key, top_k: int):
    """Get top-k tokens for display."""
    tokens = results["tokens"]
    scores = results[scores_key]
    mask = results["attention_mask"]

    valid = []
    for i, (tok, score, m) in enumerate(zip(tokens, scores, mask)):
        if m == 1 and tok not in ['[CLS]', '[SEP]', '[PAD]']:
            valid.append((tok, float(score)))

    valid.sort(key=lambda x: -x[1])
    return [tok for tok, _ in valid[:top_k]]


def main():
    print("=" * 80)
    print("Token Selection Experiment - Extended")
    print("=" * 80)

    # Load paragraphs
    print(f"\nLoading paragraphs from {TEST_DOCS_DIR}...")
    paragraphs = load_paragraphs_from_docs(TEST_DOCS_DIR)
    print(f"Loaded {len(paragraphs)} paragraphs from {len(set(f for f, _ in paragraphs))} files")

    if len(paragraphs) == 0:
        print("ERROR: No paragraphs found!")
        return

    # Sample some paragraphs for display
    print("\nSample paragraphs:")
    for i, (fname, para) in enumerate(paragraphs[:3]):
        print(f"  [{fname}] {para[:80]}...")

    print("\nLoading model...")
    device = "cpu"  # CUDA has FPE with attention outputs
    model = SentenceTransformer(
        "nomic-ai/modernbert-embed-base",
        device=device,
        trust_remote_code=True,
    )
    print(f"Model loaded on {device}")

    methods = [
        ("A_attention_only", "A. Attention Only (current)"),
        ("B_value_aware", "B. Value-Aware (attn × L1 norm)"),
        ("C_embedding_norm", "C. Embedding Norm (L2)"),
        ("D_cls_attention", "D. CLS Attention"),
        ("E_cls_x_norm", "E. CLS × Embedding Norm"),
        ("F_low_variance", "F. Low Attention Variance"),
        ("G_keybert", "G. KeyBERT (token→doc similarity)"),
        ("H_mmr", "H. MMR (diverse + relevant)"),
        ("I_ngram", "I. N-gram Keyphrases"),
    ]

    # Track quality scores: list of (content_count, total_selected) per method
    quality_scores = {key: [] for key, _ in methods}

    # Process all paragraphs
    print(f"\nProcessing {len(paragraphs)} paragraphs...")

    for para_idx, (fname, text) in enumerate(paragraphs):
        if (para_idx + 1) % 10 == 0 or para_idx == 0:
            print(f"  Processing paragraph {para_idx + 1}/{len(paragraphs)}...")

        try:
            results = get_token_importance_scores(text, model, device)
        except Exception as e:
            print(f"    Error processing paragraph from {fname}: {e}")
            continue

        # Count valid tokens (excluding special tokens)
        valid_token_count = sum(
            1 for tok, m in zip(results["tokens"], results["attention_mask"])
            if m == 1 and tok not in ['[CLS]', '[SEP]', '[PAD]']
        )

        # Select top 10% of tokens
        top_k = max(3, int(valid_token_count * 0.10))

        for key, name in methods:
            content_count, total = evaluate_method(results, key, top_k)
            quality_scores[key].append((content_count, total))

    # Summary statistics
    print("\n" + "=" * 80)
    print("SUMMARY - Content Token Selection Quality")
    print("=" * 80)
    print(f"Evaluated {len(paragraphs)} paragraphs")
    print(f"Selecting top 10% of tokens per document\n")

    summary = []
    for key, name in methods:
        scores = quality_scores[key]
        if not scores:
            continue

        # Calculate percentage of selected tokens that are content
        total_content = sum(c for c, t in scores)
        total_selected = sum(t for c, t in scores)
        pct = (total_content / total_selected * 100) if total_selected > 0 else 0

        summary.append((pct, key, name, total_content, total_selected))

    summary.sort(key=lambda x: -x[0])

    print("Method                                   Content%   Content/Total")
    print("-" * 70)
    for pct, key, name, content, total in summary:
        bar = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))
        print(f"{bar} {pct:5.1f}%   {name:<35} {content:4}/{total}")

    print("\n" + "=" * 80)
    print("DETAILED EXAMPLES")
    print("=" * 80)

    # Show examples from different document types
    examples = [p for p in paragraphs if "wiki" in p[0].lower()][:2]
    examples += [p for p in paragraphs if "transcript" in p[0].lower()][:2]
    examples += [p for p in paragraphs if "librarian" in p[0].lower() or "prose" in p[0].lower()][:2]

    if not examples:
        examples = paragraphs[:6]

    for fname, text in examples[:6]:
        print(f"\n--- [{fname}] ---")
        print(f"Text: {text[:100]}...")

        results = get_token_importance_scores(text, model, device)
        valid_count = sum(
            1 for tok, m in zip(results["tokens"], results["attention_mask"])
            if m == 1 and tok not in ['[CLS]', '[SEP]', '[PAD]']
        )
        top_k = max(3, int(valid_count * 0.10))

        print(f"Tokens: {valid_count}, selecting top {top_k} (10%)")

        # Show top 3 methods
        for key, name in [("A_attention_only", "A. Attention"), ("H_mmr", "H. MMR"), ("C_embedding_norm", "C. Embed Norm")]:
            top_toks = get_top_tokens(results, key, top_k)
            content, total = evaluate_method(results, key, top_k)
            print(f"  {name}: {content}/{total} content | {top_toks[:8]}")

    print("\n" + "=" * 80)
    print("RECOMMENDATION")
    print("=" * 80)
    best_pct, best_key, best_name, _, _ = summary[0]
    print(f"Best method: {best_name}")
    print(f"Content token rate: {best_pct:.1f}%")


if __name__ == "__main__":
    main()
