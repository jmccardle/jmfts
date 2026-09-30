"""Projection: match on leaves, return a related node.

A retrieval method scores the nodes its filters admit. Projection runs after it and changes
only which node each hit is reported as — today, its parent. The ranking that produced the
hits is untouched, so any method, fusion or rerank composes with it.

The motivating shape is a branch with small synthetic children carrying keywords: match the
keyword leaves, and the answer is the branch they hang from. Several leaves under one branch
are the common case rather than the edge case, which is why grouping and an aggregate are
part of the step rather than a later refinement.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from jmfts_client.contracts.search import PROJECTION_AGGREGATES
from jmfts_core.access import readable_filter
from jmfts_core.models.document import SETTLED_SETTLED, Document


@dataclass
class ProjectedHit:
    """A parent standing in for the leaves that matched under it.

    Duck-types the repository's ``SearchResult`` (``document``, ``score``, ``method``) so the
    service's page assembly serves both. ``via`` holds the leaf results, best first.
    """

    document: Document
    score: float
    method: str
    via: list


@dataclass
class ProjectionCounts:
    """Where every matched leaf went. ``ProjectionApplied`` on the wire."""

    matched: int
    parents: int
    no_parent: int
    withheld: int


def project_to_parents(
    session: Session,
    results: Sequence,
    *,
    aggregate: str,
    limit: int,
) -> tuple[list[ProjectedHit], ProjectionCounts]:
    """Group ``results`` by parent, score each parent by ``aggregate``, cut to ``limit``.

    A parent is returned only if the caller may read it and it is ``settled`` — the same two
    gates every retrieval method puts on what it scores. A leaf whose parent fails either is
    counted in ``withheld`` rather than dropped without a trace; a root leaf is counted in
    ``no_parent``. Usetype filters are NOT applied to the parent: they chose what was
    matched, and the parent is where the caller asked to be taken.

    ONE query for the parents of the whole page.
    """
    if aggregate not in PROJECTION_AGGREGATES:
        raise ValueError(
            f"unknown projection aggregate {aggregate!r}; expected one of "
            f"{list(PROJECTION_AGGREGATES)}"
        )

    groups: dict[int, list] = {}
    no_parent = 0
    for result in results:
        parent_id = result.document.parent_id
        if parent_id is None:
            no_parent += 1
            continue
        groups.setdefault(parent_id, []).append(result)

    parents: dict[int, Document] = {}
    if groups:
        stmt = select(Document).where(
            Document.id.in_(list(groups)), Document.settled == SETTLED_SETTLED
        )
        pred = readable_filter(session)
        if pred is not None:
            stmt = stmt.where(pred)
        parents = {doc.id: doc for doc in session.scalars(stmt)}

    withheld = 0
    hits: list[tuple[float, float, ProjectedHit]] = []
    for parent_id, leaves in groups.items():
        if parent_id not in parents:
            withheld += len(leaves)
            continue
        leaves = sorted(leaves, key=lambda r: (-r.score, r.document.id))
        best = leaves[0].score
        if aggregate == "max":
            score = best
        elif aggregate == "sum":
            score = sum(r.score for r in leaves)
        else:
            score = float(len(leaves))
        hits.append(
            (score, best, ProjectedHit(parents[parent_id], score, leaves[0].method, leaves))
        )

    # Score, then the best leaf (the tiebreak `count` needs), then id for a stable page.
    hits.sort(key=lambda h: (-h[0], -h[1], h[2].document.id))
    counts = ProjectionCounts(
        matched=len(results),
        parents=len(hits),
        no_parent=no_parent,
        withheld=withheld,
    )
    return [h[2] for h in hits[:limit]], counts
