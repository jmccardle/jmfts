"""What one ingested tree turned out to be. ``SPRINT_JOBS.md`` 15.4 S4.

``IngestResponse``'s six counts come out of ``execute_pipeline``'s own stage results
today: the pipeline built the tree and counted as it went, so the report and the run were
the same pass. On the queue they cannot be. The chunks are written by one task, embedded
by another, segmented by a third, and a fourth may run an hour later in a different
process — so the only place the numbers can come from afterwards is the tree and the
attempt log the tasks left behind.

**This reads. It never writes and never decides.** It is called after
:meth:`~jmfts_core.ingest_worker.IngestWorker.drain_document` has established that the
root's tree owes no more work, and its whole job is to describe what is there. A count
that is zero here is a count that is zero in the database.

**Three of the six numbers mean something slightly different than they did**, and the
difference is the migration rather than a defect:

* ``summary_count`` counted RAPTOR summary NODES. Path B's ``summarize`` gives an existing
  container node its ``effective_content`` instead of adding a node beside it, so what is
  counted is nodes that carry one. A tree of the same shape reports a smaller number, and
  the smaller number is the true one — the old tree really did contain extra nodes.
* ``segment_count`` counted PELT segments from a stage that was off by default. Path B
  segments every node wider than ``rollup.max_children``, so this is now usually non-zero.
* ``tree_depth`` was the RAPTOR LAYER COUNT, which was 1 for every ingest that did not
  summarize — including ones with a root and two hundred chunks under it. Here it is the
  depth of the tree that exists: a root on its own is 1, a root with chunks is 2.

``message_count`` and ``triple_count`` are unchanged in meaning. ``stages`` is a rollup —
see :func:`stage_results`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jmfts_client.contracts.ingest import IngestStageResult
from jmfts_core.models.document import Document
from jmfts_core.models.document_evidence import DocumentEvidence
from jmfts_core.models.triple import Triple

#: The evidence row ``summarize`` writes. Named here rather than imported
#: from :mod:`jmfts_core.evidence` so this module states what it counts; the two are
#: pinned together by ``tests/test_ingest_summary.py``.
EFFECTIVE_CONTENT_KEY = "effective_content"

#: Worst-first. A rolled-up stage reports the most serious status any of its attempts
#: reached, so a document whose four hundred ``embed`` tasks include one failure does not
#: report ``embed`` as completed.
_STATUS_SEVERITY = {
    "failed": 5,
    "running": 4,
    "pending": 3,
    "skipped": 2,
    "completed": 1,
}


@dataclass(frozen=True)
class TreeSummary:
    """The counts one ingested tree yields, read back from the database."""

    title: str
    message_count: int
    segment_count: int
    summary_count: int
    triple_count: int
    tree_depth: int
    stages: list[IngestStageResult]


def stage_results(attempts_by_node: list[list[dict]]) -> list[IngestStageResult]:
    """One entry per distinct task, rolled up over every node that ran it.

    NOT one entry per attempt. A five-hundred-chunk document has five hundred ``embed``
    attempts, and a response that listed them all would be a log rather than a summary.
    So each task appears once, its ``status`` is the most serious any attempt reached, and
    ``detail`` carries the per-status counts that were rolled up — which is what makes the
    rollup readable AS a rollup rather than as a single mysterious verdict.

    Order is first appearance, walking the nodes in the order given and each node's log in
    the order it was written. That is ingest order for the root's own tasks, which is the
    order path A reported its stages in.

    ``error`` carries the FIRST error string seen for that task. One is a diagnosis and n
    is a wall of text; the node-level attempt logs hold every one of them, and a caller who
    needs them all reads the nodes.
    """
    order: list[str] = []
    seen: dict[str, dict] = {}
    for attempts in attempts_by_node:
        for entry in attempts:
            if not isinstance(entry, dict):
                continue
            task = entry.get("task")
            status = entry.get("status")
            if not isinstance(task, str) or status not in _STATUS_SEVERITY:
                continue
            if task not in seen:
                order.append(task)
                seen[task] = {"counts": {}, "status": status, "error": None}
            record = seen[task]
            record["counts"][status] = record["counts"].get(status, 0) + 1
            if _STATUS_SEVERITY[status] > _STATUS_SEVERITY[record["status"]]:
                record["status"] = status
            if record["error"] is None and entry.get("error"):
                record["error"] = str(entry["error"])

    results = []
    for task in order:
        record = seen[task]
        counts = record["counts"]
        results.append(
            IngestStageResult(
                stage=task,
                status=record["status"],
                detail={"attempts": sum(counts.values()), **counts},
                error=record["error"],
            )
        )
    return results


def summarize_tree(session: Session, root_id: int) -> Optional[TreeSummary]:
    """Read back what the queue built under ``root_id``. ``None`` if the node is gone.

    One pass over the subtree for the node facts, one for the two evidence names it reads,
    and one count query for the triples.

    THE JSONB PROJECTION IS GONE, AND THAT IS PHASE 2b. This used to select
    ``structured_content -> 'effective_content'`` and ``-> 'attempts'`` out of the column,
    because a document with four hundred chunks would otherwise pull four hundred whole
    JSONB blobs across to count two things. Evidence is rows now, so asking for two names is
    a ``WHERE name IN (...)`` and the projection has nothing left to work around.
    """
    # Imported here, not at module scope. `structure_tasks` and `citation_tasks` import
    # each other through `ingest_tasks`, and that cycle resolves only when `ingest_tasks`
    # is the module entered first. A module-scope import of `structure_tasks` from here
    # would make THIS module the entry point and break the cycle open — and pinning the
    # import order at the top of the file would be undone by the first formatter run.
    from jmfts_core.models.document import USETYPE_CHUNK, USETYPE_SEGMENT

    root = session.get(Document, root_id)
    if root is None:
        return None

    root_depth = len(root.path or [])
    rows = session.execute(
        select(Document.id, Document.usetype, Document.path).where(
            (Document.id == root_id) | (Document.path.op("@>")(func.jsonb_build_array(root_id)))
        )
        # Document order, so the root's own attempts come first and the stage rollup below
        # reports them in the order the tasks ran.
        .order_by(Document.path, Document.position.asc().nullslast(), Document.id.asc())
    ).all()

    subtree_ids = [row[0] for row in rows]
    # The two names, over the whole subtree, in one query. The id list is resolved first
    # rather than joined to the subtree predicate: 13.1 measured that with both in one
    # statement the planner stops using `idx_documents_path` and sequentially scans
    # `documents`, which costs more than the join it was avoiding.
    evidence = session.execute(
        select(DocumentEvidence.document_id, DocumentEvidence.name, DocumentEvidence.value).where(
            DocumentEvidence.document_id.in_(subtree_ids),
            DocumentEvidence.name.in_((EFFECTIVE_CONTENT_KEY, "attempts")),
        )
    ).all()
    summarized: set[int] = set()
    attempts_of: dict[int, list] = {}
    for node_id, name, value in evidence:
        if name == EFFECTIVE_CONTENT_KEY:
            summarized.add(node_id)
        elif isinstance(value, list):
            attempts_of[node_id] = value

    message_count = 0
    segment_count = 0
    summary_count = 0
    depth = 1
    attempts_by_node: list[list[dict]] = []
    for node_id, usetype, path in rows:
        if usetype == USETYPE_CHUNK:
            message_count += 1
        elif usetype == USETYPE_SEGMENT:
            segment_count += 1
        if node_id in summarized:
            summary_count += 1
        depth = max(depth, len(path or []) - root_depth + 1)
        found = attempts_of.get(node_id)
        if found is not None:
            attempts_by_node.append(found)
    triple_count = session.execute(
        select(func.count()).select_from(Triple).where(Triple.source_document_id.in_(subtree_ids))
    ).scalar_one()

    return TreeSummary(
        title=root.title,
        message_count=message_count,
        segment_count=segment_count,
        summary_count=summary_count,
        triple_count=triple_count,
        tree_depth=depth,
        stages=stage_results(attempts_by_node),
    )
