"""Wiki lint — surface issues that wiki maintainers should resolve.

Four sub-audits, runnable individually or bundled by ``lint_corpus``:

- **orphan**: documents with degree ≤ threshold (link graph by default).
- **contradiction**: same (subject, predicate) with overlapping validity
  and different objects, neither superseded.
- **stale**: ``fact_type='dynamic'`` triples past a recency threshold
  with no superseding triple.
- **coverage**: high-centrality documents lacking summary descendants.

All four return ``LintFinding``-shaped dicts. The router converts to Pydantic.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from jmfts_core.graph_analysis import build_graph, compute_centrality
from jmfts_core.models.document import Document
from jmfts_core.models.triple import FactType, Triple

logger = logging.getLogger(__name__)


@dataclass
class LintFinding:
    category: str  # orphan | contradiction | stale | coverage
    severity: str  # info | warning | error
    document_ids: list[int] = field(default_factory=list)
    triple_ids: list[int] = field(default_factory=list)
    message: str = ""
    detail: dict = field(default_factory=dict)


@dataclass
class LintReport:
    scope: str
    parent_id: Optional[int]
    findings: list[LintFinding] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Sub-audits
# ---------------------------------------------------------------------------


def lint_orphans(
    session: Session,
    *,
    scope: str = "links",
    parent_id: Optional[int] = None,
    exclude_usetypes: Optional[Iterable[str]] = None,
    threshold: int = 1,
    max_findings: int = 200,
) -> list[LintFinding]:
    """Documents with total degree ≤ threshold within the scoped graph."""
    build = build_graph(
        session, scope=scope, parent_id=parent_id, exclude_usetypes=exclude_usetypes
    )
    if build.graph.vcount() == 0:
        return []
    in_deg = build.graph.degree(mode="in")
    out_deg = build.graph.degree(mode="out")

    findings: list[LintFinding] = []
    for i in range(build.graph.vcount()):
        deg = in_deg[i] + out_deg[i]
        if deg > threshold:
            continue
        doc_id = build.idx_to_id[i]
        m = build.meta[doc_id]
        findings.append(
            LintFinding(
                category="orphan",
                severity="warning" if deg == 0 else "info",
                document_ids=[doc_id],
                message=f"#{doc_id} has degree {deg} (≤ {threshold})",
                detail={
                    "in_degree": in_deg[i],
                    "out_degree": out_deg[i],
                    "title": m["title"],
                    "usetype": m["usetype"],
                },
            )
        )
        if len(findings) >= max_findings:
            break
    return findings


def lint_contradictions(
    session: Session, *, max_findings: int = 200
) -> list[LintFinding]:
    """Triples with same (subject, predicate) and overlapping validity but different objects."""
    stmt = (
        select(Triple)
        .where(Triple.invalidated_at.is_(None))
        .options(
            joinedload(Triple.subject),
            joinedload(Triple.object),
            joinedload(Triple.predicate),
        )
    )
    triples = list(session.execute(stmt).scalars().all())

    groups: dict[tuple[int, int], list[Triple]] = defaultdict(list)
    for t in triples:
        groups[(t.subject_id, t.predicate_id)].append(t)

    findings: list[LintFinding] = []
    for (sid, pid), members in groups.items():
        if len(members) < 2:
            continue
        if len({t.object_id for t in members}) < 2:
            continue
        # Check for any pair with overlapping validity
        overlapping_ids: list[int] = []
        for i, a in enumerate(members):
            for b in members[i + 1 :]:
                if a.object_id == b.object_id:
                    continue
                if _windows_overlap(a, b):
                    overlapping_ids.extend([a.id, b.id])
        if not overlapping_ids:
            continue
        unique_ids = sorted(set(overlapping_ids))
        first = members[0]
        subj_title = first.subject.title if first.subject else None
        pred_name = first.predicate.name if first.predicate else None
        findings.append(
            LintFinding(
                category="contradiction",
                severity="error",
                triple_ids=unique_ids,
                document_ids=[sid],
                message=(
                    f"subject #{sid} ({subj_title or '?'}) has overlapping "
                    f"contradictory triples for predicate '{pred_name or pid}'"
                ),
                detail={
                    "subject_id": sid,
                    "predicate_id": pid,
                    "predicate_name": pred_name,
                    "triple_count": len(members),
                    "object_ids": sorted({t.object_id for t in members}),
                },
            )
        )
        if len(findings) >= max_findings:
            break
    return findings


def lint_stale(
    session: Session,
    *,
    threshold_days: int = 90,
    max_findings: int = 500,
) -> list[LintFinding]:
    """Dynamic-fact triples past threshold without supersession."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=threshold_days)
    stmt = (
        select(Triple)
        .where(Triple.fact_type == FactType.dynamic)
        .where(Triple.invalidated_at.is_(None))
        .options(
            joinedload(Triple.subject),
            joinedload(Triple.object),
            joinedload(Triple.predicate),
        )
    )
    rows = list(session.execute(stmt).scalars().all())

    findings: list[LintFinding] = []
    for t in rows:
        ref = t.valid_from or t.recorded_at or t.created_at
        if ref is None or ref >= cutoff:
            continue
        age = (datetime.now(timezone.utc) - ref).days
        subj_title = t.subject.title if t.subject else None
        obj_title = t.object.title if t.object else None
        pred_name = t.predicate.name if t.predicate else None
        findings.append(
            LintFinding(
                category="stale",
                severity="warning",
                triple_ids=[t.id],
                document_ids=[t.subject_id, t.object_id],
                message=(
                    f"triple #{t.id}: dynamic claim {age}d old "
                    f"({subj_title or '?'} -[{pred_name or '?'}]-> {obj_title or '?'})"
                ),
                detail={
                    "age_days": age,
                    "subject_id": t.subject_id,
                    "object_id": t.object_id,
                    "predicate_id": t.predicate_id,
                    "valid_from": t.valid_from.isoformat() if t.valid_from else None,
                },
            )
        )
        if len(findings) >= max_findings:
            break
    findings.sort(key=lambda f: f.detail.get("age_days", 0), reverse=True)
    return findings


def lint_coverage(
    session: Session,
    *,
    scope: str = "links",
    parent_id: Optional[int] = None,
    exclude_usetypes: Optional[Iterable[str]] = None,
    top_k: int = 20,
    summary_usetypes: Optional[Iterable[str]] = None,
) -> list[LintFinding]:
    """High-centrality documents that lack summary descendants.

    "Summary descendants" = any child whose ``usetype`` is in
    ``summary_usetypes`` (default: {'summary'}). The intent is to flag
    important-but-unsummarized hubs so a maintainer can run RAPTOR on them.
    """
    summary_usetypes = set(summary_usetypes or {"summary"})
    build = build_graph(
        session, scope=scope, parent_id=parent_id, exclude_usetypes=exclude_usetypes
    )
    top = compute_centrality(build, metric="pagerank", top=top_k)
    if not top:
        return []
    top_ids = [r.document_id for r in top]

    # Single query: which top docs have at least one child with summary_usetypes?
    has_summary_stmt = (
        select(Document.parent_id)
        .where(Document.parent_id.in_(top_ids))
        .where(Document.usetype.in_(list(summary_usetypes)))
        .distinct()
    )
    summarized = {r[0] for r in session.execute(has_summary_stmt).all()}

    findings: list[LintFinding] = []
    for r in top:
        if r.document_id in summarized:
            continue
        findings.append(
            LintFinding(
                category="coverage",
                severity="info",
                document_ids=[r.document_id],
                message=(
                    f"high-centrality doc #{r.document_id} has no summary descendant"
                ),
                detail={
                    "title": r.title,
                    "usetype": r.usetype,
                    "centrality": r.score,
                    "rank": top_ids.index(r.document_id) + 1,
                },
            )
        )
    return findings


# ---------------------------------------------------------------------------
# Bundled entry point
# ---------------------------------------------------------------------------


def lint_corpus(
    session: Session,
    *,
    scope: str = "links",
    parent_id: Optional[int] = None,
    exclude_usetypes: Optional[Iterable[str]] = None,
    orphan_threshold: int = 1,
    stale_threshold_days: int = 90,
    coverage_top_k: int = 20,
    coverage_summary_usetypes: Optional[Iterable[str]] = None,
) -> LintReport:
    """Run all four audits in one transaction."""
    findings: list[LintFinding] = []

    findings += lint_orphans(
        session,
        scope=scope,
        parent_id=parent_id,
        exclude_usetypes=exclude_usetypes,
        threshold=orphan_threshold,
    )
    findings += lint_contradictions(session)
    findings += lint_stale(session, threshold_days=stale_threshold_days)
    findings += lint_coverage(
        session,
        scope=scope,
        parent_id=parent_id,
        exclude_usetypes=exclude_usetypes,
        top_k=coverage_top_k,
        summary_usetypes=coverage_summary_usetypes,
    )

    counts: dict[str, int] = defaultdict(int)
    for f in findings:
        counts[f.category] += 1

    # Sort: errors first, then warnings, then infos
    severity_order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: severity_order.get(f.severity, 3))

    return LintReport(
        scope=scope,
        parent_id=parent_id,
        findings=findings,
        counts=dict(counts),
    )


def _windows_overlap(a: Triple, b: Triple) -> bool:
    """Treat None bounds as ±∞."""
    if a.valid_until is not None and b.valid_from is not None and a.valid_until < b.valid_from:
        return False
    if b.valid_until is not None and a.valid_from is not None and b.valid_until < a.valid_from:
        return False
    return True
