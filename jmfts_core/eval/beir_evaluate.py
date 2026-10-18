"""The method grammar and the query loop.

Ported from ``benchmarks/beir/evaluate.py`` on the pre-0.1.0 history. Method names:

    vector | bm25 | fulltext | maxsim     one retrieval method
    hybrid                                 vector+bm25 RRF at the tuned production weights
    rrf                                    vector+bm25 RRF, equal weight (the tuning-free baseline)
    hybrid_v70b30                          vector+bm25 RRF at vector=0.70, bm25=0.30
    <stage1>→maxsim@200                    top-200 from stage 1, exact MaxSim rerank
    <stage1>→maxsim@200:t15                ... over tokens up to tier 15 only
    <stage1>→crossenc@100                  top-100 from stage 1, cross-encoder rerank

``->`` is accepted for ``→`` (it is easier to type) and the name is recorded with ``→``.

Three behaviours changed in the port, each because the old one could report a number that
did not mean what its label said:

- **A query that raises stops the run.** The old loop printed the error and ``continue``d,
  so a method that failed on a tenth of its queries reported the mean of the other nine
  tenths under its own name.
- **A result outside the corpus stops the run.** The old mapper dropped any hit it could not
  map back to a BEIR id, which silently shortened the ranked list. Every search here is
  scoped to the corpus subtree and the root has no content, so an unmappable hit is a
  scoping defect, and it is reported as one.
- **The grammar is checked before anything runs.** ``maxsim@200junk`` used to parse as
  ``maxsim@200``, and an unknown stage 1 failed per query; ``:tN`` on ``crossenc`` was
  documented as ignored. All three are errors at parse time now, which the CLI reaches
  before it downloads or ingests anything.
"""

from __future__ import annotations

import re
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Optional

from sqlalchemy.orm import Session

from jmfts_core.eval.beir_loader import BEIRFiles, IngestedDataset
from jmfts_core.eval.metrics import MetricAccumulator, MethodMetrics

STAGE1_METHODS = ("vector", "bm25", "fulltext", "maxsim", "hybrid", "rrf")
DEFAULT_RERANK_DEPTH = 100

_HYBRID_WEIGHTED = re.compile(r"hybrid_v(\d+)b(\d+)")
_STAGE2 = re.compile(r"(maxsim|crossenc)(?:@(\d+))?(?::t(\d+))?")


@dataclass(frozen=True)
class MethodConfig:
    """One parsed method name."""

    name: str  # canonical, with →
    stage1: str  # "vector" | "bm25" | "fulltext" | "maxsim" | "hybrid"
    hybrid_methods: Optional[tuple[str, ...]] = None  # None: hybrid_search's default
    hybrid_weights: Optional[dict[str, float]] = None  # None: tuned; {}: equal-weight RRF
    rerank_method: Optional[str] = None  # "maxsim" | "crossenc"
    rerank_depth: int = DEFAULT_RERANK_DEPTH
    rerank_tier: Optional[int] = None  # maxsim only

    @property
    def rerank(self) -> bool:
        return self.rerank_method is not None

    def describe(self) -> dict:
        """What this arm ran, for the result file."""
        return {
            "stage1": self.stage1,
            "hybrid_methods": list(self.hybrid_methods) if self.hybrid_methods else None,
            "hybrid_weights": self.hybrid_weights,
            "rerank_method": self.rerank_method,
            "rerank_depth": self.rerank_depth if self.rerank else None,
            "rerank_tier": self.rerank_tier,
        }


def parse_method(method_name: str) -> MethodConfig:
    """Parse a method name (module docstring); ValueError on anything it does not cover."""
    text = method_name.strip().replace("->", "→")
    stage1_str, sep, stage2_str = text.partition("→")
    if sep and not stage2_str:
        raise ValueError(f"{method_name!r}: nothing after the arrow")

    hybrid_methods: Optional[tuple[str, ...]] = None
    hybrid_weights: Optional[dict[str, float]] = None
    weighted = _HYBRID_WEIGHTED.fullmatch(stage1_str)
    if weighted:
        stage1 = "hybrid"
        hybrid_methods = ("vector", "bm25")
        hybrid_weights = {
            "vector": int(weighted.group(1)) / 100.0,
            "bm25": int(weighted.group(2)) / 100.0,
        }
    elif stage1_str == "rrf":
        # An EXPLICIT empty dict: hybrid_search gives every method 1.0. None would select
        # the tuned default, which is what "hybrid" is for.
        stage1 = "hybrid"
        hybrid_methods = ("vector", "bm25")
        hybrid_weights = {}
    elif stage1_str in STAGE1_METHODS:
        stage1 = stage1_str
    else:
        raise ValueError(
            f"{method_name!r}: unknown first stage {stage1_str!r}; expected one of "
            f"{', '.join(STAGE1_METHODS)} or hybrid_v<NN>b<NN>"
        )

    if not sep:
        return MethodConfig(
            name=text,
            stage1=stage1,
            hybrid_methods=hybrid_methods,
            hybrid_weights=hybrid_weights,
        )

    stage2 = _STAGE2.fullmatch(stage2_str)
    if not stage2:
        raise ValueError(
            f"{method_name!r}: rerank stage {stage2_str!r} is not "
            "maxsim[@DEPTH][:tTIER] or crossenc[@DEPTH]"
        )
    rerank_method = stage2.group(1)
    depth = int(stage2.group(2)) if stage2.group(2) else DEFAULT_RERANK_DEPTH
    tier = int(stage2.group(3)) if stage2.group(3) else None
    if depth < 1:
        raise ValueError(f"{method_name!r}: rerank depth must be at least 1")
    if tier is not None and rerank_method != "maxsim":
        raise ValueError(f"{method_name!r}: a :tN tier applies to maxsim only")
    return MethodConfig(
        name=text,
        stage1=stage1,
        hybrid_methods=hybrid_methods,
        hybrid_weights=hybrid_weights,
        rerank_method=rerank_method,
        rerank_depth=depth,
        rerank_tier=tier,
    )


class ResultOutsideCorpus(RuntimeError):
    """A search returned a document that is not one of this dataset's ingested documents."""


def _stage1(search_repo, config: MethodConfig, query: str, limit: int, data: IngestedDataset):
    scope = {"parent_id": data.root_id}
    if config.stage1 == "vector":
        return search_repo.vector_search_text(query, limit=limit, **scope)
    if config.stage1 == "bm25":
        return search_repo.bm25_search(query, index_name=data.index_name, limit=limit, **scope)
    if config.stage1 == "fulltext":
        return search_repo.fulltext_search(query, limit=limit, **scope)
    if config.stage1 == "maxsim":
        return search_repo.maxsim_search(query, limit=limit, **scope)
    if config.stage1 == "hybrid":
        return search_repo.hybrid_search(
            query,
            limit=limit,
            methods=list(config.hybrid_methods) if config.hybrid_methods else None,
            # `None` and `{}` mean different things to hybrid_search; pass it through as is.
            weights=config.hybrid_weights,
            index_name=data.index_name,
            **scope,
        )
    raise AssertionError(f"parse_method admitted stage1 {config.stage1!r}")


@dataclass
class MethodOutcome:
    metrics: MethodMetrics
    reranker_model: Optional[str]
    ann_short_queries: int  # queries whose ANN scan returned fewer rows than it asked for

    def as_dict(self, config: MethodConfig) -> dict:
        out = self.metrics.as_dict()
        out["config"] = config.describe()
        out["reranker_model"] = self.reranker_model
        out["ann_short_queries"] = self.ann_short_queries
        return out


def evaluate_method(
    session: Session,
    config: MethodConfig,
    files: BEIRFiles,
    data: IngestedDataset,
    *,
    depth: int = 100,
    reranker=None,
    progress_every: int = 50,
    log: Callable[[str], None] = print,
) -> MethodOutcome:
    """Run every evaluable query through one method and average the metrics.

    ``depth`` is how many results are kept per query (100 gives Recall@100). A rerank arm
    retrieves ``rerank_depth`` in stage 1 and keeps ``depth`` after reranking.
    ``reranker`` is a loaded ``RerankerService``, required for a ``crossenc`` arm.
    """
    from jmfts_core.repositories.search import SearchRepository

    if config.rerank_method == "crossenc" and reranker is None:
        raise ValueError(f"{config.name}: a crossenc arm needs a loaded reranker")
    reverse = {doc_id: beir_id for beir_id, doc_id in data.id_map.items()}
    acc = MetricAccumulator(depth)
    ann_short = 0
    queries = files.evaluated_queries

    for i, (qid, query) in enumerate(queries.items(), 1):
        qrel = files.qrels[qid]
        repo = SearchRepository(session)
        t0 = time.perf_counter()
        results = _stage1(
            repo, config, query, config.rerank_depth if config.rerank else depth, data
        )
        if config.rerank_method == "crossenc" and results:
            results = reranker.rerank(query, results, limit=depth)
        elif config.rerank_method == "maxsim" and results:
            results = repo.maxsim_rerank(results, query, limit=depth, max_tier=config.rerank_tier)
        latency_ms = (time.perf_counter() - t0) * 1000
        if repo.scan_truncated:
            ann_short += 1

        retrieved = []
        for r in results:
            beir_id = reverse.get(r.document.id)
            if beir_id is None:
                raise ResultOutsideCorpus(
                    f"{config.name} returned document {r.document.id} for query {qid}, which "
                    f"is not in the {files.name} corpus under root {data.root_id}"
                )
            retrieved.append(beir_id)
        acc.add(retrieved, qrel, latency_ms)

        if progress_every and i % progress_every == 0:
            log(
                f"    [{config.name}] {i}/{len(queries)} queries "
                f"(nDCG@10={acc.running_ndcg():.4f})"
            )

    return MethodOutcome(
        metrics=acc.result(),
        reranker_model=reranker.model_name if config.rerank_method == "crossenc" else None,
        ann_short_queries=ann_short,
    )


# =============================================================================
# Display
# =============================================================================


def format_dataset_table(dataset: str, outcomes: dict[str, MethodOutcome]) -> str:
    lines = [
        f"  {dataset}",
        f"  {'Method':<30} {'nDCG@10':>8} {'MAP@10':>8} {'R@10':>8} "
        f"{'R@100':>8} {'MRR@10':>8} {'P@10':>8} {'Latency':>10}",
        "  " + "-" * 96,
    ]
    for name, o in outcomes.items():
        m = o.metrics
        r100 = f"{m.recall_at_100:>8.4f}" if m.recall_at_100 is not None else f"{'-':>8}"
        lines.append(
            f"  {name:<30} {m.ndcg_at_10:>8.4f} {m.map_at_10:>8.4f} {m.recall_at_10:>8.4f} "
            f"{r100} {m.mrr_at_10:>8.4f} {m.precision_at_10:>8.4f} {m.avg_latency_ms:>8.0f}ms"
        )
    return "\n".join(lines)


def format_summary_table(per_dataset: dict[str, dict[str, MethodOutcome]]) -> str:
    """nDCG@10 per dataset and method, with the mean over datasets that ran it."""
    methods: list[str] = []
    for outcomes in per_dataset.values():
        methods.extend(m for m in outcomes if m not in methods)
    width = max([13] + [len(m) + 2 for m in methods])
    rule = "  " + "-" * (16 + (width + 1) * len(methods))
    lines = ["  nDCG@10", f"  {'Dataset':<16}" + "".join(f" {m:>{width}}" for m in methods), rule]
    columns: dict[str, list[float]] = defaultdict(list)
    for dataset, outcomes in per_dataset.items():
        row = f"  {dataset:<16}"
        for m in methods:
            if m in outcomes:
                value = outcomes[m].metrics.ndcg_at_10
                columns[m].append(value)
                row += f" {value:>{width}.4f}"
            else:
                row += f" {'-':>{width}}"
        lines.append(row)
    lines.append(rule)
    lines.append(
        f"  {'Average':<16}"
        + "".join(
            (
                f" {sum(columns[m]) / len(columns[m]):>{width}.4f}"
                if columns[m]
                else f" {'-':>{width}}"
            )
            for m in methods
        )
    )
    return "\n".join(lines)
