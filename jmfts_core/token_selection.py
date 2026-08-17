"""
JMFTS Token Selection Module

Modular, weighted token selection for MaxSim late interaction.
Combines multiple semantic techniques with configurable weights.

Techniques:
- MMR: Maximum Marginal Relevance (diversity + document relevance)
- Attention Variance: Tokens with consistent attention across layers/heads
- Stopword Penalty: Hard penalty for function words and punctuation
"""

import threading

import torch
import torch.nn.functional as F
import numpy as np
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Optional

# NLTK stopwords - embedded to avoid runtime dependency
ENGLISH_STOPWORDS = frozenset({
    'i', 'me', 'my', 'myself', 'we', 'our', 'ours', 'ourselves', 'you', "you're",
    "you've", "you'll", "you'd", 'your', 'yours', 'yourself', 'yourselves', 'he',
    'him', 'his', 'himself', 'she', "she's", 'her', 'hers', 'herself', 'it', "it's",
    'its', 'itself', 'they', 'them', 'their', 'theirs', 'themselves', 'what', 'which',
    'who', 'whom', 'this', 'that', "that'll", 'these', 'those', 'am', 'is', 'are',
    'was', 'were', 'be', 'been', 'being', 'have', 'has', 'had', 'having', 'do',
    'does', 'did', 'doing', 'a', 'an', 'the', 'and', 'but', 'if', 'or', 'because',
    'as', 'until', 'while', 'of', 'at', 'by', 'for', 'with', 'about', 'against',
    'between', 'into', 'through', 'during', 'before', 'after', 'above', 'below',
    'to', 'from', 'up', 'down', 'in', 'out', 'on', 'off', 'over', 'under', 'again',
    'further', 'then', 'once', 'here', 'there', 'when', 'where', 'why', 'how', 'all',
    'each', 'few', 'more', 'most', 'other', 'some', 'such', 'no', 'nor', 'not',
    'only', 'own', 'same', 'so', 'than', 'too', 'very', 's', 't', 'can', 'will',
    'just', 'don', "don't", 'should', "should've", 'now', 'd', 'll', 'm', 'o', 're',
    've', 'y', 'ain', 'aren', "aren't", 'couldn', "couldn't", 'didn', "didn't",
    'doesn', "doesn't", 'hadn', "hadn't", 'hasn', "hasn't", 'haven', "haven't",
    'isn', "isn't", 'ma', 'mightn', "mightn't", 'mustn', "mustn't", 'needn',
    "needn't", 'shan', "shan't", 'shouldn', "shouldn't", 'wasn', "wasn't", 'weren',
    "weren't", 'won', "won't", 'wouldn', "wouldn't"
})


@dataclass
class TokenSelectionConfig:
    """Configuration for token selection weights"""
    # Method weights (will be normalized)
    mmr_weight: float = 1.0
    attention_variance_weight: float = 0.5
    stopword_penalty_weight: float = 1.0  # Applied as penalty, not combined

    # MMR parameters
    mmr_lambda: float = 0.5  # Balance relevance vs diversity

    # Stopword penalty value (large negative to effectively filter)
    stopword_penalty: float = -100.0

    # Selection parameters
    top_percent: float = 0.10


@dataclass
class TokenScores:
    """Intermediate scores for each technique"""
    tokens: list[str]
    attention_mask: np.ndarray
    mmr_scores: np.ndarray
    variance_scores: np.ndarray
    stopword_mask: np.ndarray  # 1 for content, 0 for stopword/punct


class TokenSelector:
    """
    Modular token selector combining multiple scoring techniques.

    Usage:
        selector = TokenSelector(config)
        indices, scores = selector.select_tokens(
            tokens, embeddings, attentions, attention_mask, doc_embedding
        )
    """

    def __init__(self, config: Optional[TokenSelectionConfig] = None):
        self.config = config or TokenSelectionConfig()

    def _clean_token(self, token: str) -> str:
        """Remove tokenizer prefixes for text analysis"""
        # ModernBERT/RoBERTa style
        return token.replace("Ġ", "").replace("Ċ", "").lower()

    def _is_stopword_or_punct(self, token: str) -> bool:
        """Check if token is a stopword or punctuation"""
        clean = self._clean_token(token)

        # Empty or whitespace
        if not clean or clean.isspace():
            return True

        # Pure punctuation
        if all(not c.isalnum() for c in clean):
            return True

        # Stopword
        if clean in ENGLISH_STOPWORDS:
            return True

        # Single character (except meaningful ones)
        if len(clean) == 1 and clean not in {'i', 'a'}:
            return True

        return False

    def compute_mmr_scores(
        self,
        token_embeddings: torch.Tensor,  # (seq_len, hidden_dim)
        doc_embedding: torch.Tensor,      # (hidden_dim,)
        tokens: list[str],
        attention_mask: np.ndarray,
    ) -> np.ndarray:
        """
        Compute MMR (Maximum Marginal Relevance) scores.

        Iteratively selects tokens that are:
        1. Relevant to the document embedding
        2. Diverse from already-selected tokens
        """
        seq_len = len(tokens)

        # Normalize embeddings
        token_normed = F.normalize(token_embeddings, p=2, dim=-1)
        doc_normed = F.normalize(doc_embedding, p=2, dim=0)

        # Relevance: token similarity to document
        relevance = torch.matmul(token_normed, doc_normed).cpu().numpy()

        # Pairwise token similarities for diversity
        token_sims = torch.matmul(token_normed, token_normed.T).cpu().numpy()

        # MMR selection
        lambda_param = self.config.mmr_lambda
        selected = []
        remaining = [i for i in range(seq_len)
                    if attention_mask[i] == 1 and tokens[i] not in ['[CLS]', '[SEP]', '[PAD]']]

        # Select up to 50 tokens (more than we need, scores decrease with rank)
        for step in range(min(len(remaining), 50)):
            if not remaining:
                break

            best_score = -float('inf')
            best_idx = remaining[0]

            for idx in remaining:
                rel = float(relevance[idx])

                if selected:
                    max_sim = float(max(token_sims[idx, s] for s in selected))
                else:
                    max_sim = 0.0

                score = lambda_param * rel - (1 - lambda_param) * max_sim

                if score > best_score:
                    best_score = score
                    best_idx = idx

            selected.append(best_idx)
            remaining.remove(best_idx)

        # Convert selection order to scores (earlier = higher)
        scores = np.zeros(seq_len, dtype=np.float32)
        for rank, idx in enumerate(selected):
            scores[idx] = float(len(selected) - rank) / len(selected)  # Normalize to [0, 1]

        return scores

    def compute_variance_scores(
        self,
        attentions: tuple,  # tuple of (batch, heads, seq, seq)
        attention_mask: np.ndarray,
    ) -> np.ndarray:
        """
        Compute attention variance scores.

        Tokens that receive consistent attention across all layers/heads
        tend to be semantically important.
        """
        # Stack attention layers
        attention_stack = torch.stack(attentions)  # (layers, batch, heads, seq, seq)

        # Sum attention received by each token from all source positions
        attn_received = attention_stack[:, 0, :, :, :].sum(dim=2)  # (layers, heads, seq)

        # Compute variance across layers and heads
        variance = attn_received.var(dim=(0, 1)).cpu().numpy()  # (seq,)

        # Low variance = consistent = important
        # Add small epsilon to avoid division by zero
        scores = 1.0 / (variance + 0.01)

        # Normalize to [0, 1]
        valid_mask = attention_mask == 1
        if valid_mask.sum() > 0:
            valid_scores = scores[valid_mask]
            min_s, max_s = valid_scores.min(), valid_scores.max()
            if max_s > min_s:
                scores = (scores - min_s) / (max_s - min_s)
            else:
                scores = np.ones_like(scores) * 0.5

        return scores.astype(np.float32)

    def compute_stopword_mask(self, tokens: list[str]) -> np.ndarray:
        """
        Compute stopword/punctuation mask.

        Returns 1.0 for content tokens, 0.0 for stopwords/punctuation.
        """
        mask = np.array([
            0.0 if self._is_stopword_or_punct(tok) else 1.0
            for tok in tokens
        ], dtype=np.float32)
        return mask

    def compute_combined_scores(self, token_scores: TokenScores) -> np.ndarray:
        """
        Combine all scoring methods with configured weights.

        Stopword penalty is applied after combining other scores.
        """
        # Combine MMR and variance scores
        total_weight = self.config.mmr_weight + self.config.attention_variance_weight

        combined = (
            self.config.mmr_weight * token_scores.mmr_scores +
            self.config.attention_variance_weight * token_scores.variance_scores
        ) / total_weight

        # Apply stopword penalty
        # Where stopword_mask is 0, add large negative penalty
        penalty = (1.0 - token_scores.stopword_mask) * self.config.stopword_penalty
        combined = combined + penalty

        return combined

    def select_tokens(
        self,
        tokens: list[str],
        token_embeddings: torch.Tensor,  # (seq_len, hidden_dim)
        attentions: tuple,                # attention outputs
        attention_mask: torch.Tensor,     # (seq_len,)
        doc_embedding: torch.Tensor,      # (hidden_dim,)
        top_percent: Optional[float] = None,
    ) -> tuple[list[int], np.ndarray]:
        """
        Select top tokens using combined scoring methods.

        Args:
            tokens: List of token strings
            token_embeddings: Token embeddings from last hidden state
            attentions: Attention weights from transformer
            attention_mask: Mask for valid tokens
            doc_embedding: Document-level embedding
            top_percent: Fraction of tokens to select (default: config value)

        Returns:
            (selected_indices, scores) tuple
        """
        top_percent = top_percent or self.config.top_percent
        mask_np = attention_mask.cpu().numpy()

        # Compute individual scores
        mmr_scores = self.compute_mmr_scores(
            token_embeddings, doc_embedding, tokens, mask_np
        )
        variance_scores = self.compute_variance_scores(attentions, mask_np)
        stopword_mask = self.compute_stopword_mask(tokens)

        token_scores = TokenScores(
            tokens=tokens,
            attention_mask=mask_np,
            mmr_scores=mmr_scores,
            variance_scores=variance_scores,
            stopword_mask=stopword_mask,
        )

        # Combine scores
        combined = self.compute_combined_scores(token_scores)

        # Filter out special tokens and select top N%
        valid_indices = [
            i for i, tok in enumerate(tokens)
            if mask_np[i] == 1 and tok not in ['[CLS]', '[SEP]', '[PAD]']
        ]

        n_select = max(1, int(len(valid_indices) * top_percent))

        # Sort by combined score
        scored_indices = [(i, combined[i]) for i in valid_indices]
        scored_indices.sort(key=lambda x: -x[1])

        selected = scored_indices[:n_select]
        selected_indices = [i for i, _ in selected]
        scores = np.array([s for _, s in selected], dtype=np.float32)

        return selected_indices, scores


def importance_from_salience(importance_scores: Sequence[float]) -> float:
    """Derive a 1–10 document importance from its token salience scores.

    This is the WRITE side of the importance axis whose READ side is
    ``SearchRepository._importance_factor`` (which maps the stored 1–10 value to a
    [0,1] rerank multiplier). It replaces Generative-Agents' *LLM-rating-per-memory*
    with an intrinsic signal already produced at ingest: the combined MMR-relevance +
    attention-variance salience that ``TokenSelector.select_tokens`` assigns each
    stored token. No LLM call, no extra forward pass.

    The aggregate is the mean of the content-token salience scores (scores > 0;
    stopwords/punctuation carry a large negative penalty and are excluded), mapped
    linearly onto the 1–10 scale the read side expects.

    IMPORTANT CAVEAT — this is a *relative-within-document* signal. ``select_tokens``
    normalises both MMR (rank-derived) and attention-variance (min-max) per document,
    so the top token of every document scores ~1.0 and the absolute mean is only
    weakly comparable across documents (its cross-document signal is the *shape* of a
    document's salience distribution, not an absolute magnitude). It is a principled,
    deterministic, zero-cost importance prior — not a calibrated cross-corpus ranking.
    A corpus-percentile calibration would be the stronger v2 and needs the whole
    corpus in hand, so it does not belong at single-document ingest. Until then this
    stays opt-in (``embed_document(..., write_importance=True)``) precisely because it
    is a prior, not ground truth.

    Args:
        importance_scores: Per-token salience scores from an EmbeddingResult's
            ``token_embeddings`` (each ``TokenEmbeddingResult.importance_score``).

    Returns:
        A float in [1.0, 10.0], rounded to 2 decimals. A document with no positive
        content-token salience returns 1.0 (the scale's floor — "no salient content"),
        which the read side maps to a 0.0 importance factor (neutral).
    """
    content = [float(s) for s in importance_scores if s is not None and s > 0.0]
    if not content:
        return 1.0
    mean = sum(content) / len(content)
    # Salience combined-scores are ~[0,1]; clamp defensively before the affine map so
    # a stray out-of-range score can never push importance outside the read scale.
    mean = min(1.0, max(0.0, mean))
    return round(1.0 + 9.0 * mean, 2)


# Default selector instance. Double-checked under a lock so a concurrent first-touch
# can't construct two default selectors.
_default_selector: Optional[TokenSelector] = None
_default_selector_lock = threading.Lock()


def get_token_selector(config: Optional[TokenSelectionConfig] = None) -> TokenSelector:
    """Get or create the token selector"""
    global _default_selector
    # An explicit config always yields a fresh, caller-owned selector (never the shared one).
    if config is not None:
        return TokenSelector(config)
    if _default_selector is None:
        with _default_selector_lock:
            if _default_selector is None:
                _default_selector = TokenSelector()
    return _default_selector
