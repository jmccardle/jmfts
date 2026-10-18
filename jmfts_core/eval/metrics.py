"""IR metrics over graded qrels. Pure functions; no database, no model.

Ported verbatim in substance from ``benchmarks/beir/evaluate.py`` on the pre-0.1.0 history,
including its ``200c5fe`` fix ("honest graded-qrel metrics"): a document is relevant iff its
judgment is > 0. BEIR's graded qrels (TREC-COVID, NFCorpus) list explicitly-judged
NON-relevant documents at 0, and counting membership instead of grade inflated every binary
metric on those two datasets. nDCG was never affected — a 0 contributes 0 gain.

Every function takes ``retrieved`` (BEIR document ids, best first) and ``qrel``
(``{doc_id: grade}`` for one query) and looks at the first ``k`` of ``retrieved``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


def dcg_at_k(relevances: list[int], k: int) -> float:
    """Discounted cumulative gain, linear gain: ``sum(rel_i / log2(i + 2))`` over the top k.

    Linear rather than exponential (``2**rel - 1``) gain, which is what ``pytrec_eval``'s
    ``ndcg_cut`` computes and therefore what published BEIR numbers use.
    """
    return sum(rel / math.log2(i + 2) for i, rel in enumerate(relevances[:k]))


def ndcg_at_k(retrieved: list[str], qrel: dict[str, int], k: int = 10) -> float:
    """Normalised DCG at k; 0.0 for a query whose judgments hold no positive grade."""
    gains = [qrel.get(doc_id, 0) for doc_id in retrieved[:k]]
    ideal = sorted(qrel.values(), reverse=True)[:k]
    idcg = dcg_at_k(ideal, k)
    return dcg_at_k(gains, k) / idcg if idcg > 0 else 0.0


def is_relevant(qrel: dict[str, int], doc_id: str) -> bool:
    """Relevant iff the graded judgment is > 0. See the module docstring for why."""
    return qrel.get(doc_id, 0) > 0


def num_relevant(qrel: dict[str, int]) -> int:
    return sum(1 for grade in qrel.values() if grade > 0)


def precision_at_k(retrieved: list[str], qrel: dict[str, int], k: int = 10) -> float:
    """Relevant hits in the top k, over k (not over how many were retrieved)."""
    return sum(1 for d in retrieved[:k] if is_relevant(qrel, d)) / k


def recall_at_k(retrieved: list[str], qrel: dict[str, int], k: int = 10) -> float:
    total = num_relevant(qrel)
    if total == 0:
        return 0.0
    return sum(1 for d in retrieved[:k] if is_relevant(qrel, d)) / total


def average_precision(retrieved: list[str], qrel: dict[str, int], k: int = 10) -> float:
    """AP@k, normalised by ``min(#relevant, k)`` — the BEIR / pytrec_eval ``map_cut`` form."""
    hits = 0
    total = 0.0
    for i, doc_id in enumerate(retrieved[:k]):
        if is_relevant(qrel, doc_id):
            hits += 1
            total += hits / (i + 1)
    denom = min(num_relevant(qrel), k)
    return total / denom if denom > 0 else 0.0


def reciprocal_rank(retrieved: list[str], qrel: dict[str, int], k: int = 10) -> float:
    for i, doc_id in enumerate(retrieved[:k]):
        if is_relevant(qrel, doc_id):
            return 1.0 / (i + 1)
    return 0.0


@dataclass
class MethodMetrics:
    """Mean metrics for one method over the evaluated queries.

    ``recall_at_100`` is None when the run retrieved fewer than 100 per query: a recall@100
    computed over a shorter list is a different number wearing the same name.
    """

    ndcg_at_10: float
    map_at_10: float
    recall_at_10: float
    recall_at_100: Optional[float]
    mrr_at_10: float
    precision_at_10: float
    avg_latency_ms: float
    queries_evaluated: int

    def as_dict(self) -> dict:
        return {
            "nDCG@10": self.ndcg_at_10,
            "MAP@10": self.map_at_10,
            "Recall@10": self.recall_at_10,
            "Recall@100": self.recall_at_100,
            "MRR@10": self.mrr_at_10,
            "P@10": self.precision_at_10,
            "Latency_ms": self.avg_latency_ms,
            "Queries": self.queries_evaluated,
        }


class MetricAccumulator:
    """Per-query scores in, means out. One per method."""

    def __init__(self, depth: int):
        self.depth = depth
        self._rows: list[tuple[float, float, float, float, float, float]] = []
        self._latencies: list[float] = []

    def add(self, retrieved: list[str], qrel: dict[str, int], latency_ms: float) -> None:
        self._rows.append(
            (
                ndcg_at_k(retrieved, qrel, 10),
                average_precision(retrieved, qrel, 10),
                recall_at_k(retrieved, qrel, 10),
                recall_at_k(retrieved, qrel, 100),
                reciprocal_rank(retrieved, qrel, 10),
                precision_at_k(retrieved, qrel, 10),
            )
        )
        self._latencies.append(latency_ms)

    @property
    def count(self) -> int:
        return len(self._rows)

    def running_ndcg(self) -> float:
        return sum(r[0] for r in self._rows) / len(self._rows) if self._rows else 0.0

    def result(self) -> MethodMetrics:
        n = len(self._rows)
        if n == 0:
            raise ValueError("no queries were evaluated; there is nothing to average")
        means = [sum(col) / n for col in zip(*self._rows)]
        return MethodMetrics(
            ndcg_at_10=means[0],
            map_at_10=means[1],
            recall_at_10=means[2],
            recall_at_100=means[3] if self.depth >= 100 else None,
            mrr_at_10=means[4],
            precision_at_10=means[5],
            avg_latency_ms=sum(self._latencies) / n,
            queries_evaluated=n,
        )
