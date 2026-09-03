"""BM25 membership, derived from the tree. ``INGEST_SPEC.md`` 11.5.

The last row of 11.1's parity table: *"BM25 index entry — A: yes, unconditionally into
`default`. B: no."* Nothing ingested through the queue reached a BM25 index, so a queued
ingestion produced a tree that vector search could find and full-text search could not.

**The obvious port is wrong, and 11.5 says why.** ``_index_subtree_bm25`` indexed into
``"default"`` unconditionally, creating that index if it was absent, which made every
ingestion a member of one corpus whether or not anybody asked for it. ``search_index_members``
keys on ``(index_id, root_document_id)``, so membership is a property of a SUBTREE ROOT,
and a node's ancestors are already in its ``path``. So this reads the file node's ``path``
and indexes into every BM25 index whose root is the node itself or one of its ancestors.

**A file with no ancestors joins nothing, and that is the deliberate part.** It is
invisible to full-text search and visible to vector search until somebody indexes
something above it. That is the same shape as the rest of the appliance — 1.3: a subtree
"does not appear in BM25 results by default" — and creating a corpus as a side effect of an
upload is the behaviour being removed, not preserved. The task reports that outcome as
``skipped`` with the candidate roots it looked for, so "nothing indexed this" is a fact on
the record rather than a silence.

**A ``TASK_ROWS`` row, not the rollup planner, and that is a divergence from 11.5's
wording.** 11.5 puts it at the settling boundary because "the index has to see the whole
subtree, and the subtree does not exist until structuring finishes". A row with
``after_any`` on the two structure rungs gets the identical guarantee out of the queue's
own dependency gate — the rung must be ``completed``, and a completed rung has written
every chunk with its ``content`` — and it gets two things the planner cannot give:

* **Exactly one task per file.** Part 4's table is evaluated once per uploaded file.
  ``IngestRollupPlanner`` is called at EVERY node's settling boundary, so the same walk
  that reaches the file node also reaches each ``section`` above the chunks, and each of
  those would offer its own subtree index task — the same documents, indexed again.
* **A leaf is not skipped.** The planner returns nothing for a node with no children,
  which is every chunk — and chunks are precisely what BM25 needs.

The deeper nodes the rollup DOES create (``segment``) carry ``effective_content`` rather
than ``content``, which ``index_document`` does not read (11.4: a summary must not skew the
BM25 statistics of the corpus it summarizes), so nothing is missed by running before them.

NOT BUILT HERE, and named so it is not mistaken for done: 11.5's "an explicit index name on
the upload remains available and overrules the walk" — there is no such upload parameter
yet — and its second consequence, that reparenting a file into or out of an indexed folder
should change its membership. That is a ``reparent`` consequence rather than an ingest one.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from jmfts_core.atoms import COST_CPU, EV_TEXT
from jmfts_core.ingest_tasks import TASK_INDEX_BM25, TaskOutcome, register_task_handler
from jmfts_core.models.document import Document
from jmfts_core.models.search_index import SearchIndex, SearchIndexMember
from jmfts_core.models.task_queue import TaskQueue, WRITE_SELF
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.search import SearchRepository

logger = logging.getLogger(__name__)


def covering_indexes(session: Session, node: Document) -> list[str]:
    """The BM25 indexes whose registered root is ``node`` or one of its ancestors.

    Closest ancestor first, which is the order ``_find_covering_index`` already prefers
    when it resolves a search scope — the two answer the same question from the same table
    and should not disagree about which index is the most specific.
    """
    candidates = [node.id] + list(node.path or [])
    rows = session.execute(
        select(SearchIndex.name, SearchIndexMember.root_document_id)
        .join(SearchIndexMember, SearchIndexMember.index_id == SearchIndex.id)
        .where(SearchIndexMember.root_document_id.in_(candidates))
    ).all()
    position = {doc_id: i for i, doc_id in enumerate(candidates)}
    ordered = sorted(rows, key=lambda row: position[row[1]])
    # A single index may name two of this node's ancestors as roots; it is still one index.
    seen: list[str] = []
    for name, _root in ordered:
        if name not in seen:
            seen.append(name)
    return seen


@register_task_handler(
    TASK_INDEX_BM25,
    # The tree's text, not this node's. The file node's own `content` is the whole
    # extracted markdown and every chunk carries its share of it, and both are indexed —
    # which is the shape path A had too, root plus chunks.
    consumes=(f"{EV_TEXT}@self", f"{EV_TEXT}@subtree"),
    # Nothing. The postings, the term statistics and the entries are rows in the search
    # tables, and no node gains a block. An atom that claimed to produce evidence here
    # would put this task in the audit's derivation for a key nobody could read back.
    produces=(),
    write_mode=WRITE_SELF,
    cost_class=COST_CPU,
)
def run_index_bm25(session: Session, task: TaskQueue) -> TaskOutcome:
    """Add this file's subtree to every BM25 index that already covers it. 11.5."""
    node = session.get(Document, task.scope_document_id)
    if node is None:
        raise LookupError(f"document {task.scope_document_id} is gone; nothing to index")

    names = covering_indexes(session, node)
    if not names:
        return TaskOutcome(
            status="skipped",
            detail={
                "reason": (
                    "no BM25 index names this document or any of its ancestors as a root "
                    "(INGEST_SPEC.md 11.5); it is searchable by vector and not by BM25 "
                    "until an index is given a root above it"
                ),
                "candidate_roots": [node.id] + list(node.path or []),
            },
        )

    # `include_in_flight=True`: this task runs INSIDE the ingestion that built the tree, so
    # it is the code responsible for those nodes and must see them. The settled-only
    # default exists to stop OUTSIDE readers getting a partial tree with no error; here it
    # would silently index half of what was just written.
    subtree = DocumentRepository(session).get_subtree(node.id, include_in_flight=True)
    repo = SearchRepository(session)
    indexed = {name: 0 for name in names}
    for name in names:
        for doc in subtree:
            if doc.content and repo.index_document(doc.id, name):
                indexed[name] += 1
    session.flush()

    return TaskOutcome(
        detail={
            "indexes": names,
            "documents_indexed": indexed,
            "subtree_size": len(subtree),
        }
    )
