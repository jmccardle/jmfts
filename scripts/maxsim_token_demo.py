#!/usr/bin/env python3
"""
MaxSim Token Matching Demo

Show which query tokens match which document tokens during MaxSim search.
Compares different token selection methods.
"""

import sys
sys.path.insert(0, "/home/john/Development/jmfts")

import torch
import torch.nn.functional as F
import numpy as np
from sentence_transformers import SentenceTransformer


def get_token_embeddings_with_methods(text: str, model: SentenceTransformer, device: str = "cpu"):
    """Get token embeddings and scores for multiple selection methods."""
    transformer = model[0].auto_model
    tokenizer = model.tokenizer

    inputs = tokenizer(
        text,
        return_tensors="pt",
        truncation=True,
        max_length=512,
        padding=True,
    )
    inputs = {k: v.to(device) for k, v in inputs.items()}

    with torch.no_grad():
        outputs = transformer(
            **inputs,
            output_attentions=True,
            output_hidden_states=True,
        )

    last_hidden = outputs.last_hidden_state[0]  # (seq_len, hidden_dim)
    attentions = outputs.attentions
    attention_mask = inputs["attention_mask"][0].cpu()

    token_ids = inputs["input_ids"][0].cpu().tolist()
    tokens = tokenizer.convert_ids_to_tokens(token_ids)
    seq_len = len(tokens)

    # Method A: Attention-only (current broken)
    attention_stack = torch.stack(attentions)
    avg_attention = attention_stack.mean(dim=(0, 1, 2)).sum(dim=0)
    method_a = avg_attention.cpu().numpy()

    # Method H: MMR
    mask_expanded = attention_mask.unsqueeze(-1).float().to(device)
    doc_embedding = (last_hidden * mask_expanded).sum(dim=0) / mask_expanded.sum()
    doc_embedding = F.normalize(doc_embedding, p=2, dim=0)
    token_norms = F.normalize(last_hidden, p=2, dim=-1)
    keybert_scores = torch.matmul(token_norms, doc_embedding).cpu().numpy()

    token_similarities = torch.matmul(token_norms, token_norms.T).cpu().numpy()
    lambda_param = 0.5
    selected_indices = []
    remaining = list(range(seq_len))

    for step in range(min(seq_len, 50)):
        if not remaining:
            break
        best_score = -float('inf')
        best_idx = remaining[0]
        for idx in remaining:
            if tokens[idx] in ['[CLS]', '[SEP]', '[PAD]']:
                continue
            rel = float(keybert_scores[idx])
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

    return {
        "tokens": tokens,
        "embeddings": last_hidden.cpu(),  # (seq_len, hidden_dim)
        "attention_mask": attention_mask.numpy(),
        "A_attention": method_a,
        "H_mmr": method_h,
    }


def select_top_tokens(results, method_key, top_pct=0.10):
    """Select top tokens by a method, return indices and tokens."""
    tokens = results["tokens"]
    scores = results[method_key]
    mask = results["attention_mask"]

    valid = []
    for i, (tok, score, m) in enumerate(zip(tokens, scores, mask)):
        if m == 1 and tok not in ['[CLS]', '[SEP]', '[PAD]']:
            valid.append((i, tok, float(score)))

    valid.sort(key=lambda x: -x[2])
    top_k = max(3, int(len(valid) * top_pct))
    return [(idx, tok) for idx, tok, _ in valid[:top_k]]


def maxsim_search(query_results, doc_results, doc_token_indices):
    """
    Compute MaxSim score and show which query tokens matched which doc tokens.

    Returns: (score, matches) where matches is list of (query_token, doc_token, similarity)
    """
    query_embeddings = query_results["embeddings"]
    query_tokens = query_results["tokens"]
    query_mask = query_results["attention_mask"]

    doc_embeddings = doc_results["embeddings"]
    doc_tokens = doc_results["tokens"]

    # Get doc token embeddings (only selected ones)
    doc_selected_embeddings = doc_embeddings[doc_token_indices]  # (k, hidden_dim)
    doc_selected_tokens = [doc_tokens[i] for i in doc_token_indices]

    # Normalize
    query_normed = F.normalize(query_embeddings, p=2, dim=-1)
    doc_normed = F.normalize(doc_selected_embeddings, p=2, dim=-1)

    # Compute all pairwise similarities: (query_len, doc_k)
    similarities = torch.matmul(query_normed, doc_normed.T)

    # MaxSim: for each query token, find max similarity to any doc token
    matches = []
    total_score = 0.0

    for q_idx, (q_tok, q_mask) in enumerate(zip(query_tokens, query_mask)):
        if q_mask == 0 or q_tok in ['[CLS]', '[SEP]', '[PAD]']:
            continue

        # Find best matching doc token for this query token
        sims = similarities[q_idx]
        best_doc_idx = sims.argmax().item()
        best_sim = sims[best_doc_idx].item()

        matches.append((q_tok, doc_selected_tokens[best_doc_idx], best_sim))
        total_score += best_sim

    return total_score, matches


def clean_token(tok):
    """Clean token for display."""
    return tok.replace("Ġ", " ").replace("Ċ", "\\n")


def main():
    print("=" * 80)
    print("MaxSim Token Matching Demo")
    print("=" * 80)

    # Sample documents
    docs = [
        ("3D Printing Wiki", "3D printing, or additive manufacturing, is the construction of a three-dimensional object from a CAD model or a digital 3D model. It can be done in a variety of processes in which material is deposited, joined or solidified under computer control."),
        ("Nylon Wiki", "Nylon is a generic designation for a family of synthetic polymers composed of polyamides. Nylon is a thermoplastic silky material that can be melt-processed into fibers, films, or shapes."),
        ("Librarian Poem", "There was once a library that contained every book ever written. One day it noticed it was reading itself. Not a librarian - the library. The building, the shelves, the accumulated weight of every word."),
        ("PID Controllers", "PID controllers minimize error between desired and actual state. Tune proportional gain first, then add integral and derivative terms. The controller continuously calculates an error value."),
    ]

    queries = [
        "What is 3D printing used for?",
        "How do you make nylon fibers?",
        "What is a self-aware library?",
        "How do you tune a PID controller?",
    ]

    print("\nLoading model...")
    device = "cpu"
    model = SentenceTransformer(
        "nomic-ai/modernbert-embed-base",
        device=device,
        trust_remote_code=True,
    )
    print("Model loaded.\n")

    # Process documents
    print("=" * 80)
    print("DOCUMENT TOKEN SELECTION")
    print("=" * 80)

    doc_data = []
    for name, text in docs:
        results = get_token_embeddings_with_methods(text, model, device)
        doc_data.append((name, text, results))

        # Show selected tokens for each method
        print(f"\n📄 {name}")
        print(f"   Text: {text[:80]}...")

        valid_count = sum(1 for m in results["attention_mask"] if m == 1) - 2  # exclude CLS/SEP
        top_k = max(3, int(valid_count * 0.10))
        print(f"   Tokens: {valid_count}, selecting {top_k} (10%)")

        attn_selected = select_top_tokens(results, "A_attention", 0.10)
        mmr_selected = select_top_tokens(results, "H_mmr", 0.10)

        print(f"   Attention: {[clean_token(t) for _, t in attn_selected]}")
        print(f"   MMR:       {[clean_token(t) for _, t in mmr_selected]}")

    # Search queries
    print("\n" + "=" * 80)
    print("MAXSIM SEARCH - Query Token → Document Token Matching")
    print("=" * 80)

    for query in queries:
        print(f"\n🔍 Query: \"{query}\"")

        query_results = get_token_embeddings_with_methods(query, model, device)
        query_tokens = [clean_token(t) for t in query_results["tokens"]
                       if t not in ['[CLS]', '[SEP]', '[PAD]']]
        print(f"   Query tokens: {query_tokens}")

        print("\n   Method A (Attention-only) vs Method H (MMR):")
        print("   " + "-" * 70)

        for name, text, doc_results in doc_data:
            # Get selected token indices for each method
            attn_indices = [idx for idx, _ in select_top_tokens(doc_results, "A_attention", 0.10)]
            mmr_indices = [idx for idx, _ in select_top_tokens(doc_results, "H_mmr", 0.10)]

            # Compute MaxSim scores
            attn_score, attn_matches = maxsim_search(query_results, doc_results, attn_indices)
            mmr_score, mmr_matches = maxsim_search(query_results, doc_results, mmr_indices)

            print(f"\n   📄 {name}")
            print(f"      Attention score: {attn_score:.2f}")
            print(f"      MMR score:       {mmr_score:.2f}")

            # Show top token matches for MMR
            top_matches = sorted(mmr_matches, key=lambda x: -x[2])[:5]
            print(f"      Top MMR matches:")
            for q_tok, d_tok, sim in top_matches:
                print(f"         \"{clean_token(q_tok)}\" → \"{clean_token(d_tok)}\" (sim={sim:.3f})")

    print("\n" + "=" * 80)
    print("ANALYSIS")
    print("=" * 80)


if __name__ == "__main__":
    main()
