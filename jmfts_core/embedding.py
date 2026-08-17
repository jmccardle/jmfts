"""
JMFTS Embedding Service

Provides matryoshka embeddings using ModernBERT with support for:
- Full document embeddings (1024 dim)
- Token-level embeddings with importance scoring
- Truncation to various dimensions (128, 256, 384, 512)
"""

import threading

import torch
import torch.nn.functional as F
import numpy as np
from typing import Optional
from dataclasses import dataclass
from sentence_transformers import SentenceTransformer

from jmfts_core.chunking import ChunkStrategy, chunk_text
from jmfts_core.config import get_settings
from jmfts_core.token_selection import (
    TokenSelector,
    get_token_selector,
)


class TextTooLongError(ValueError):
    """Text exceeds the window the requested embedding path can actually cover.

    Raised instead of embedding a truncated prefix and reporting success.  The
    embedder used to pass `truncation=True, max_length=512` and return
    `{"embedded": true}` for a document of any length: the tail simply did not
    exist as far as vector or maxsim search was concerned, and a caller had no
    way to find out (docs/KNOWN-DEFECTS.md, D1).
    """

    def __init__(self, token_count: int, limit: int, chars_total: int, path: str):
        self.token_count = token_count
        self.limit = limit
        self.chars_total = chars_total
        self.path = path
        super().__init__(
            f"Text is {token_count} tokens ({chars_total} chars), over the "
            f"{limit}-token {path} window. Chunk it first — embedding it here "
            f"would silently discard {token_count - limit} tokens."
        )


@dataclass
class FitResult:
    """Whether a text fits the window of the embedding path that would handle it."""

    token_count: int
    limit: int
    chars_total: int
    truncated: bool

    @property
    def tokens_dropped(self) -> int:
        return max(0, self.token_count - self.limit)


@dataclass
class TokenEmbeddingResult:
    """Result of token-level embedding extraction"""

    token_idx: int
    token_text: str
    importance_score: float
    embedding: np.ndarray  # Full embedding, truncate as needed


@dataclass
class EmbeddingResult:
    """Result of document embedding"""

    document_embedding: np.ndarray  # Full 1024-dim embedding
    token_embeddings: list[TokenEmbeddingResult]  # Top N% tokens


class EmbeddingService:
    """
    Embedding service with matryoshka support.

    Uses ModernBERT for embeddings with the ability to truncate to
    different dimensions while maintaining semantic quality.
    """

    def __init__(
        self,
        model_name: Optional[str] = None,
        device: Optional[str] = None,
        token_top_percent: Optional[float] = None,
    ):
        settings = get_settings()
        self.model_name = model_name or settings.embedding_model
        self.device = device or settings.embedding_device
        self.token_top_percent = token_top_percent or settings.token_top_percent

        self._model: Optional[SentenceTransformer] = None
        self._tokenizer = None
        # One reentrant lock guards BOTH the lazy model load (so a concurrent burst
        # can't load the weights twice) AND every model-forward pass (so two callers
        # don't run concurrent attention passes and double the VRAM peak — see the
        # embedding_token_window memory budget in config.py). It's an RLock because a
        # forward method holds it and then touches ``self.model``, which re-acquires.
        # The GIL already serialises the Python around encode(), so this costs no
        # throughput — it only makes the peak deterministic. See ROADMAP "Concurrency
        # & thread safety", Axis A #2.
        self._infer_lock = threading.RLock()

    @property
    def model(self) -> SentenceTransformer:
        """Lazy-load the embedding model (double-checked under the infer lock)."""
        if self._model is None:
            with self._infer_lock:
                if self._model is None:
                    self._model = SentenceTransformer(
                        self.model_name,
                        device=self.device,
                        trust_remote_code=True,
                        model_kwargs={"attn_implementation": "eager"},
                    )
        return self._model

    @property
    def tokenizer(self):
        """Lazy-load the tokenizer alone — no model weights, no GPU.

        Deciding whether a text fits is a tokenizer operation, so it must not
        require loading the model.  Callers (and the API's 400 path) need to
        answer "will this fit?" cheaply.
        """
        if self._tokenizer is None:
            from transformers import AutoTokenizer

            self._tokenizer = AutoTokenizer.from_pretrained(self.model_name)
        return self._tokenizer

    def check_fit(
        self, text: str, with_tokens: bool = True, prefix: str = "search_document: "
    ) -> FitResult:
        """Report whether `text` fits the window of the path that would embed it.

        The two paths have genuinely different limits: the document-vector path is
        bounded by the model (8192), the token/maxsim path by attention memory
        (512).  Which limit applies depends on what is being asked for.
        """
        settings = get_settings()
        limit = settings.embedding_token_window if with_tokens else settings.embedding_doc_window
        token_count = len(self.tokenizer(prefix + text, add_special_tokens=True)["input_ids"])
        return FitResult(
            token_count=token_count,
            limit=limit,
            chars_total=len(text),
            truncated=token_count > limit,
        )

    def fits_token_window(self, text: str, prefix: str = "search_document: ") -> bool:
        """Does `text` fit the token/maxsim window? The predicate `chunk_text` takes.

        A chunker cannot answer this: it counts characters, and the exchange rate to
        subword tokens is a property of the text, not a constant (KNOWN-DEFECTS D7).
        This is the measurement, small enough to hand to the chunker as a callable, and
        cheap enough to call per piece — `check_fit` loads the tokenizer alone, never
        the model weights.
        """
        return not self.check_fit(text, with_tokens=True, prefix=prefix).truncated

    def chunk_to_fit(
        self,
        text: str,
        strategy: Optional[ChunkStrategy] = None,
        max_chars: Optional[int] = None,
        prefix: str = "search_document: ",
    ) -> list[str]:
        """Chunk `text` into pieces that each *verifiably* fit the token/maxsim window.

        The measuring is `chunk_text`'s now — it takes the predicate and enforces it on
        every piece it returns, so the guarantee is the same one every other caller gets
        by passing `fits`, rather than a second implementation of it that lived here.
        """
        if strategy is None:
            strategy = ChunkStrategy.sentence
        if max_chars is None:
            max_chars = get_settings().chunk_max_chars

        return [
            chunk.text
            for chunk in chunk_text(
                text,
                strategy=strategy,
                max_chars=max_chars,
                fits=lambda piece: self.fits_token_window(piece, prefix),
            )
        ]

    def embed_text(self, text: str, normalize: bool = True, prefix: str = "") -> np.ndarray:
        """
        Generate full document embedding (1024 dimensions).

        Covers the model's full 8192-token window.  Raises rather than truncate.

        Args:
            text: Text to embed
            normalize: Whether to L2-normalize the embedding
            prefix: Task prefix for nomic models (e.g. "search_document: ", "search_query: ")

        Returns:
            1024-dimensional numpy array

        Raises:
            TextTooLongError: If text exceeds the model's document window.
        """
        fit = self.check_fit(text, with_tokens=False, prefix=prefix)
        if fit.truncated:
            raise TextTooLongError(fit.token_count, fit.limit, fit.chars_total, "document-vector")

        # Serialise the forward pass: one model call at a time bounds the VRAM peak.
        with self._infer_lock:
            embedding = self.model.encode(
                prefix + text,
                convert_to_numpy=True,
                normalize_embeddings=normalize,
            )
        return embedding

    def truncate_embedding(
        self,
        embedding: np.ndarray,
        target_dim: int,
        normalize: bool = True,
    ) -> np.ndarray:
        """
        Truncate matryoshka embedding to target dimension.

        Critical: Re-normalizes after truncation to maintain unit length.

        Args:
            embedding: Full embedding (1024-dim)
            target_dim: Target dimension (128, 256, 384, 512)
            normalize: Whether to re-normalize after truncation

        Returns:
            Truncated embedding of shape (target_dim,)
        """
        truncated = embedding[:target_dim]
        if normalize:
            norm = np.linalg.norm(truncated)
            if norm > 0:
                truncated = truncated / norm
        return truncated

    def embed_with_tokens(
        self,
        text: str,
        top_percent: Optional[float] = None,
        token_selector: Optional[TokenSelector] = None,
        prefix: str = "",
    ) -> EmbeddingResult:
        """
        Generate document embedding plus token-level embeddings for late interaction.

        Uses modular token selection combining:
        - MMR (Maximum Marginal Relevance) for diversity + relevance
        - Attention variance for semantic consistency
        - Stopword/punctuation penalty

        Args:
            text: Text to embed
            top_percent: Fraction of tokens to keep (default: self.token_top_percent)
            token_selector: Custom token selector (default: use global instance)

        Returns:
            EmbeddingResult with document and token embeddings
        """
        top_percent = top_percent or self.token_top_percent
        selector = token_selector or get_token_selector()
        window = get_settings().embedding_token_window

        # Refuse over-window text rather than embed a prefix that looks faithful.
        # This is the caller's cue to chunk (chunk_text bounds every chunk to
        # settings.chunk_max_chars, which is sized to land inside this window).
        fit = self.check_fit(text, with_tokens=True, prefix=prefix)
        if fit.truncated:
            raise TextTooLongError(fit.token_count, fit.limit, fit.chars_total, "token/maxsim")

        # Get the underlying transformer model
        transformer = self.model[0].auto_model
        tokenizer = self.model.tokenizer

        # Tokenize (with prefix for nomic task instruction)
        inputs = tokenizer(
            prefix + text,
            return_tensors="pt",
            truncation=True,
            max_length=window,
            padding=True,
        )
        inputs = {k: v.to(self.device) for k, v in inputs.items()}

        # Count prefix tokens so we can strip them from token-level results
        prefix_token_count = 0
        if prefix:
            prefix_ids = tokenizer(prefix, add_special_tokens=False)["input_ids"]
            prefix_token_count = len(prefix_ids)

        # Serialise the forward pass through token selection: output_attentions=True
        # materialises the full attention matrices (the config's memory budget), so two
        # concurrent passes would double the VRAM peak. The lock spans until select_tokens
        # has consumed the attentions; after that the big tensors can be freed.
        with self._infer_lock:
            # Get token embeddings and attention
            with torch.no_grad():
                outputs = transformer(**inputs, output_attentions=True)

                # Token embeddings from last hidden state
                token_embeddings = outputs.last_hidden_state[0]  # (seq_len, hidden_dim)

            # Decode tokens
            token_ids = inputs["input_ids"][0].cpu().tolist()
            tokens = tokenizer.convert_ids_to_tokens(token_ids)

            # Get document-level embedding (mean pooling with attention mask)
            attention_mask = inputs["attention_mask"][0]
            masked_embeddings = token_embeddings * attention_mask.unsqueeze(-1)
            doc_embedding = masked_embeddings.sum(dim=0) / attention_mask.sum()
            doc_embedding = F.normalize(doc_embedding, p=2, dim=0)

            # Select tokens using modular selector
            selected_indices, importance_scores = selector.select_tokens(
                tokens=tokens,
                token_embeddings=token_embeddings,
                attentions=outputs.attentions,
                attention_mask=attention_mask,
                doc_embedding=doc_embedding,
                top_percent=top_percent,
            )

        # Build token embedding results, skipping prefix tokens
        # prefix_token_count offset by 1 for [CLS] token
        skip_before = 1 + prefix_token_count  # [CLS] + prefix tokens
        token_results = []
        for idx, score in zip(selected_indices, importance_scores):
            if idx < skip_before:
                continue  # skip [CLS] and prefix tokens
            # Get token embedding and normalize
            tok_embed = token_embeddings[idx].cpu().numpy()
            tok_embed = tok_embed / np.linalg.norm(tok_embed)

            token_results.append(
                TokenEmbeddingResult(
                    token_idx=idx - prefix_token_count,  # adjust index to match unprefixed text
                    token_text=tokens[idx],
                    importance_score=float(score),
                    embedding=tok_embed,
                )
            )

        # Sort by importance (highest first)
        token_results.sort(key=lambda x: x.importance_score, reverse=True)

        # Convert doc embedding to numpy
        doc_embedding_np = doc_embedding.cpu().numpy()

        return EmbeddingResult(
            document_embedding=doc_embedding_np,
            token_embeddings=token_results,
        )

    def embed_batch_with_tokens(
        self,
        texts: list[str],
        top_percent: Optional[float] = None,
        token_selector: Optional[TokenSelector] = None,
        prefix: str = "",
    ) -> list[EmbeddingResult]:
        """
        Batch version of embed_with_tokens for GPU efficiency.

        Processes multiple documents in a single GPU pass.

        Args:
            texts: List of texts to embed
            top_percent: Fraction of tokens to keep (default: self.token_top_percent)
            token_selector: Custom token selector (default: use global instance)
            prefix: Task prefix for nomic models (e.g. "search_document: ", "search_query: ")

        Returns:
            List of EmbeddingResults, one per input text
        """
        if not texts:
            return []

        top_percent = top_percent or self.token_top_percent
        selector = token_selector or get_token_selector()
        window = get_settings().embedding_token_window

        # Same refusal as the single-text path — a batch must not be the way to
        # sneak an over-window document past the check.
        for i, t in enumerate(texts):
            fit = self.check_fit(t, with_tokens=True, prefix=prefix)
            if fit.truncated:
                raise TextTooLongError(
                    fit.token_count, fit.limit, fit.chars_total, f"token/maxsim (batch item {i})"
                )

        # Get the underlying transformer model
        transformer = self.model[0].auto_model
        tokenizer = self.model.tokenizer

        # Count prefix tokens so we can strip them from token-level results
        prefix_token_count = 0
        if prefix:
            prefix_ids = tokenizer(prefix, add_special_tokens=False)["input_ids"]
            prefix_token_count = len(prefix_ids)

        # Batch tokenize (with prefix for nomic task instruction)
        prefixed = [prefix + t for t in texts] if prefix else texts
        inputs = tokenizer(
            prefixed,
            return_tensors="pt",
            truncation=True,
            max_length=window,
            padding=True,
        )
        inputs = {k: v.to(self.device) for k, v in inputs.items()}

        # Serialise the whole batch pass: the single forward's attentions
        # (output_attentions=True) are consumed across the entire per-item loop, so the
        # lock must span forward + loop. Two concurrent batch passes would otherwise
        # double the (already large) attention-matrix VRAM peak. See config's memory budget.
        with self._infer_lock:
            # Batch forward pass
            with torch.no_grad():
                outputs = transformer(**inputs, output_attentions=True)
                # outputs.last_hidden_state: (batch, seq_len, hidden_dim)
                # outputs.attentions: tuple of (batch, heads, seq, seq)

            results = []
            batch_size = len(texts)
            skip_before = 1 + prefix_token_count  # [CLS] + prefix tokens

            for batch_idx in range(batch_size):
                # Extract this document's data
                token_embeddings = outputs.last_hidden_state[batch_idx]  # (seq_len, hidden_dim)
                attention_mask = inputs["attention_mask"][batch_idx]  # (seq_len,)

                # Extract per-document attentions (need to slice each layer)
                doc_attentions = tuple(
                    attn[batch_idx : batch_idx + 1]  # Keep batch dim for compatibility
                    for attn in outputs.attentions
                )

                # Decode tokens
                token_ids = inputs["input_ids"][batch_idx].cpu().tolist()
                tokens = tokenizer.convert_ids_to_tokens(token_ids)

                # Document embedding (mean pooling with attention mask)
                masked_embeddings = token_embeddings * attention_mask.unsqueeze(-1)
                doc_embedding = masked_embeddings.sum(dim=0) / attention_mask.sum()
                doc_embedding = F.normalize(doc_embedding, p=2, dim=0)

                # Select tokens
                selected_indices, importance_scores = selector.select_tokens(
                    tokens=tokens,
                    token_embeddings=token_embeddings,
                    attentions=doc_attentions,
                    attention_mask=attention_mask,
                    doc_embedding=doc_embedding,
                    top_percent=top_percent,
                )

                # Build token embedding results, skipping prefix tokens
                token_results = []
                for idx, score in zip(selected_indices, importance_scores):
                    if idx < skip_before:
                        continue  # skip [CLS] and prefix tokens
                    tok_embed = token_embeddings[idx].cpu().numpy()
                    tok_embed = tok_embed / np.linalg.norm(tok_embed)

                    token_results.append(
                        TokenEmbeddingResult(
                            token_idx=idx
                            - prefix_token_count,  # adjust index to match unprefixed text
                            token_text=tokens[idx],
                            importance_score=float(score),
                            embedding=tok_embed,
                        )
                    )

                token_results.sort(key=lambda x: x.importance_score, reverse=True)
                doc_embedding_np = doc_embedding.cpu().numpy()

                results.append(
                    EmbeddingResult(
                        document_embedding=doc_embedding_np,
                        token_embeddings=token_results,
                    )
                )

        return results


# Singleton instance. Double-checked under a lock so a concurrent first-touch can't
# construct two services (each of which would lazily load its own copy of the model).
_embedding_service: Optional[EmbeddingService] = None
_embedding_service_lock = threading.Lock()


def get_embedding_service() -> EmbeddingService:
    """Get or create the singleton embedding service"""
    global _embedding_service
    if _embedding_service is None:
        with _embedding_service_lock:
            if _embedding_service is None:
                _embedding_service = EmbeddingService()
    return _embedding_service
