"""
Cross-Encoder Reranker Service for JMFTS

Second-stage reranking. A cross-encoder jointly encodes each (query, document)
pair and emits a relevance score directly, which is strictly more informative
than the independent bi-encoder embeddings the first stage ranks with -- at the
cost of one forward pass per candidate, so it only runs over an over-fetched
candidate set, never over the corpus.

The backend is a standard sentence-transformers ``CrossEncoder``. The default
model (``cross-encoder/ms-marco-MiniLM-L-6-v2``, ~22M params) is trained on
MS MARCO relevance judgments, downloads from the Hub on first use, and is small
enough to run on CPU. Point ``JMFTS_RERANKER_MODEL`` at any Hub cross-encoder to
swap it.

History: an earlier version of this file used a local NLI checkpoint's
entailment probability as the relevance signal. That was wrong on three counts
and is documented in ``docs/RERANKER_CRITIQUE.md``. It was removed in favor of a
model trained for the task JMFTS actually asks of it.
"""

import logging
from typing import Optional

from jmfts_core.config import get_settings

logger = logging.getLogger(__name__)


class RerankerService:
    """Cross-encoder reranking service.

    Loads one cross-encoder model and scores (query, document) pairs. Loading is
    eager and unguarded: if the model cannot be fetched or placed on the
    requested device, construction raises rather than yielding a service that
    silently returns the input order.
    """

    def __init__(
        self,
        model: Optional[str] = None,
        device: Optional[str] = None,
        max_length: Optional[int] = None,
        batch_size: Optional[int] = None,
    ):
        settings = get_settings()
        self.model_name = model or settings.reranker_model
        self.device = device or settings.effective_reranker_device
        self.max_length = max_length or settings.reranker_max_length
        self.batch_size = batch_size or settings.reranker_batch_size

        self._model = None
        self._load_model()

    def _load_model(self):
        """Load the cross-encoder. Raises if the model is unavailable."""
        from sentence_transformers import CrossEncoder

        self._model = CrossEncoder(
            self.model_name,
            device=self.device,
            max_length=self.max_length,
        )
        logger.info("Loaded cross-encoder reranker %s on %s", self.model_name, self.device)

    def score_pairs(self, query: str, documents: list[str]) -> list[float]:
        """Score a list of (query, document) pairs.

        Args:
            query: The search query.
            documents: List of document texts.

        Returns:
            List of relevance scores (higher = more relevant), one per document.
            The scale is model-specific and is not comparable across models or
            across queries -- it is an ordering signal, not a calibrated
            probability.
        """
        if not documents:
            return []

        pairs = [[query, doc] for doc in documents]
        scores = self._model.predict(pairs, batch_size=self.batch_size)
        return [float(s) for s in scores]

    def rerank(
        self,
        query: str,
        candidates: list,
        limit: Optional[int] = None,
    ) -> list:
        """Rerank SearchResult candidates by cross-encoder score.

        Args:
            query: The search query.
            candidates: List of SearchResult objects from initial retrieval.
            limit: Max results to return after reranking.

        Returns:
            Reranked list of SearchResult objects with updated scores and method.
        """
        from jmfts_core.repositories.search import SearchResult

        if not candidates:
            return []

        # Extract document text for scoring
        documents = []
        for r in candidates:
            text = ""
            if r.document.title:
                text += r.document.title + " "
            if r.document.content:
                text += r.document.content
            documents.append(text.strip() or "(empty)")

        # Score all pairs
        scores = self.score_pairs(query, documents)

        # Build reranked results
        scored = list(zip(candidates, scores))
        scored.sort(key=lambda x: x[1], reverse=True)

        if limit:
            scored = scored[:limit]

        return [
            SearchResult(
                document=r.document,
                score=float(s),
                method=f"{r.method}+rerank",
            )
            for r, s in scored
        ]


# Singleton instance
_reranker_service: Optional[RerankerService] = None


def get_reranker_service() -> RerankerService:
    """Get or create the singleton reranker service."""
    global _reranker_service
    if _reranker_service is None:
        _reranker_service = RerankerService()
    return _reranker_service
