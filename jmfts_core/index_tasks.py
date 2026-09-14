"""BM25 membership, derived from the tree. ``INGEST_SPEC.md`` 11.5.

The last row of 11.1's parity table: *"BM25 index entry — A: yes, unconditionally into
`default`. B: no."* Nothing ingested through the queue reached a BM25 index, so a queued
ingestion produced a tree that vector search could find and full-text search could not.

**The obvious port is wrong, and 11.5 says why.** ``_index_subtree_bm25`` indexed into
``"default"`` unconditionally, creating that index if it was absent, which made every
ingestion a member of one corpus whether or not anybody asked for it. ``search_index_members``
keys on ``(index_id, root_document_id)``, so membership is a property of a SUBTREE ROOT,
and a node's ancestors are already in its ``path``. So this reads a node's ``path``
and indexes into every BM25 index whose root is the node itself or one of its ancestors.

**A ROLLUP RULE AND NOT A ``TASK_ROWS`` ROW, SINCE ``SPRINT_0_6_0.md`` BLOCK B STEP 7.**
It was a row on the file node with ``after_any`` on the three prose structure rungs, and
that row could not reach a spreadsheet at all. ``STRESS_CORPUS.md`` 4.4 measured the
consequence on a real corpus — zero ``index:bm25`` tasks anywhere in a 21-workbook subtree,
an ``xlsx`` index holding 0 documents and 0 postings, while 4,601 ``record`` nodes below it
carried text averaging 457 characters. Three causes, and the third is the one that decided
the shape of the repair:

1. ``after_any`` named ``structure:declared``, ``structure:inferred`` and
   ``structure:conversation`` and not ``structure:sheets``;
2. ``requires=(HAS_TEXT_LAYER,)`` is a PDF property no workbook can satisfy;
3. **a workbook's leaves are not written by its structure rung, and every other format's
   are.** ``structure:sheets`` writes the ``sheet`` containers; ``profile:sheet`` and
   ``extract:sheet`` are scoped to those children, one rung further down. 4.4c repaired
   causes 1 and 2 alone and measured the result: *"index:bm25 added 0 documents while the
   subtree holds 3 record nodes; it saw a subtree of 2"* — a ``completed`` attempt with
   ``documents_indexed: 0`` that consumed the one chance the task gets, which is worse than
   the visible absence it replaced.

The row cannot express the missing dependency. ``after``/``after_any`` order two rows on
ONE node (``ingest_tasks._check_task_rows`` rule 2) so a file-node row cannot name
``extract:sheet``, which is sheet-scoped; a second row under the same name is refused by
rule 1. ``docs/MEASURE_BM25_BOUNDARY.md`` priced five shapes against that and §5.2
recommends this one, Option I: **the settling boundary, over a set difference.**

**WHAT THE TASK'S WORK IS, AND IT IS NOT "MY SUBTREE".** :func:`outstanding_documents` asks
for the documents a covering index *should* hold and does not — settled, content-bearing, not
held out by ``bm25_exclude_usetypes``, with no ``search_index_entries`` row. Three properties
follow and none of them follows from "walk my subtree":

* **A stuck straggler withholds itself and nothing else.** ``STRESS_CORPUS.md`` 4.4d
  measured the alternative: three workbooks, ~2,240 finished ``record`` nodes withheld from
  the index by about 70 unsettled siblings, because a subtree walk refuses a subtree.
  ``settled`` is per NODE — ``models/document.py``'s own definition — and so is a BM25
  write: one ``search_index_entries`` row, one posting set.
* **It catches up.** A boundary that fires with nothing outstanding does one anti-join and
  exits (measured at ~1.7 ms, §2.6, and it costs the same whether or not it finds rows). A
  boundary that fires after four more sheets settled writes those four sheets.
* **It is format-blind.** The set difference reads ``documents.path`` and
  ``search_index_members``. It names no rung, no usetype and no format, so "a BM25 index
  per folder" is a property of the tree rather than of the table of rungs.

**THE BOUNDARY NODE ITSELF IS IN THE SET, AND ITS ``settled`` COLUMN IS THE ONE EXCEPTION.**
``settle_node`` calls the planner only when the node has no unfinished task and every child
is settled, and marks the column in the same transaction once the planner returns nothing —
so at the instant this is planned the node is settled in fact and not yet in the column.
Excluding it would drop a prose file node's own ``content``, which is the whole extracted
markdown and which the old row DID index, and would leave it indexed only if some ancestor
happened to settle later. ``docs/MEASURE_BM25_BOUNDARY.md`` §5.2 writes the locus as
``text@children`` for that reason; the atom below declares ``text@self`` and ``text@subtree``
instead, because that is what this reads.

**A LEAF IS STILL NEVER OFFERED THIS**, and it is ``IngestRollupPlanner``'s existing leaf
guard that says so rather than anything here — a node with no children returns before any
rung is considered. That is what keeps the shape at one task per container with outstanding
work (Option I) instead of one task per document (Option B, priced at 0.6–3.8% of the rung
at reference scale and rejected on the strength of the anti-join rather than on cost).

**WHAT MOVING IT COSTS, named rather than discovered later.** Two things:

* ``index:bm25`` leaves ``EXPLAIN``'s answer. ``explain_plan`` reads ``TASK_ROWS`` and
  nothing else, deliberately (``ingest_tasks.TASK_SUMMARIZE_TREE``'s note says why), so the
  four rollup task types do not appear in it. ``SPRINT_JOBS.md`` Phase 7 is where that ends:
  *"After 7, ``IngestRollupPlanner`` is rules consuming ``@children`` and the rollup's
  special case is gone."*
* The old row's ``skipped`` outcome — *"no BM25 index names this document or any of its
  ancestors as a root"*, with the candidate roots on the record — is no longer written for
  an ordinary uncovered upload, because no task is created for one. ``IngestRollupPlanner``
  refuses to enqueue a row whose only possible outcome is ``skipped`` in exactly those words,
  and the question it answered is one query against ``search_index_members``. The branch
  survives below for the case that is genuinely a race: an index dropped between the
  boundary that planned the task and the worker that claimed it.

NOT BUILT HERE, and named so it is not mistaken for done: 11.5's "an explicit index name on
the upload remains available and overrules the walk" — there is no such upload parameter
yet — and its second consequence, that reparenting a file into or out of an indexed folder
should change its membership. Option I makes the second CHEAPER to solve later (a reparent
invalidates entries and the next boundary catches up) without solving it.
"""

from __future__ import annotations

import logging

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from jmfts_core.atoms import COST_CPU, EV_TEXT
from jmfts_core.config import get_settings
from jmfts_core.ingest_tasks import TASK_INDEX_BM25, TaskOutcome, register_task_handler
from jmfts_core.models.document import Document, SETTLED_SETTLED
from jmfts_core.models.search_index import SearchIndex, SearchIndexEntry, SearchIndexMember
from jmfts_core.models.task_queue import TaskQueue, WRITE_SELF
from jmfts_core.repositories.search import SearchRepository

logger = logging.getLogger(__name__)

#: The params key carrying how much work the boundary saw. ``SPRINT_JOBS.md`` 6.1's attempt
#: diff is what terminates the walk, and it keys on ``(task, param_fingerprint)`` — so a
#: boundary that finds new work has to look different from one that found the same work
#: twice. ``child_count`` plays this role for the three summarize rungs and cannot play it
#: here: a sheet that settles adds no child to the file node and changes this number by
#: hundreds.
PARAM_OUTSTANDING = "outstanding"


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


def outstanding_documents(session: Session, node: Document, index_name: str) -> list[int]:
    """Documents under ``node`` that ``index_name`` should hold and does not.

    The four predicates are ``index_document``'s own refusals, stated ahead of it so that a
    document it would decline is never counted as outstanding. A mismatch between the two
    would be read by the planner as work that never gets done: the anti-join would keep
    returning the same rows, the boundary would keep offering a task, and the task would
    keep writing nothing.

    ``content`` is tested for emptiness as well as for NULL because ``index_document``
    returns ``False`` on a falsy one. What it cannot mirror is the tokenizer — a document
    whose content is punctuation alone tokenizes to nothing and gets no entry, so it stays
    outstanding. That costs one no-op task per boundary that sees it, bounded by 6.1's
    diff, and it is left visible rather than papered over: the handler reports what it was
    asked for beside what it wrote.
    """
    index_id = session.execute(
        select(SearchIndex.id).where(SearchIndex.name == index_name)
    ).scalar_one_or_none()
    if index_id is None:
        return []

    excluded = list(get_settings().bm25_exclude_usetypes or ())
    already = select(SearchIndexEntry.document_id).where(
        SearchIndexEntry.index_id == index_id,
        SearchIndexEntry.document_id == Document.id,
    )
    query = (
        select(Document.id)
        # `node` itself, plus its descendants. `path` is the JSONB ancestor list, which is
        # how 11.5 defines membership in the first place.
        .where(or_(Document.id == node.id, Document.path.contains([node.id])))
        # Settled, EXCEPT the boundary node — see the module docstring. Its own work is
        # done and its children are settled; the column is written after the planner runs.
        .where(or_(Document.settled == SETTLED_SETTLED, Document.id == node.id))
        .where(Document.content.isnot(None))
        .where(Document.content != "")
        .where(~already.exists())
        .order_by(Document.id)
    )
    if excluded:
        query = query.where(
            or_(Document.usetype.is_(None), Document.usetype.notin_(excluded)),
        )
    return list(session.execute(query).scalars().all())


def outstanding_by_index(session: Session, node: Document) -> dict[str, list[int]]:
    """:func:`outstanding_documents` for every index :func:`covering_indexes` returns.

    Empty when nothing covers this node, which is the same answer as "nothing is
    outstanding" and is deliberately not distinguished from it here: an uncovered subtree
    has no index to be behind.
    """
    return {
        name: outstanding_documents(session, node, name) for name in covering_indexes(session, node)
    }


@register_task_handler(
    TASK_INDEX_BM25,
    # The tree's text, and this node's. The boundary node's own `content` is in the set
    # difference — for a file node that is the whole extracted markdown — and every settled
    # descendant's is too, which is `@self` and `@subtree` and not `@children`.
    # `SPRINT_JOBS.md` 2.2 routes both of the latter to the settling walk, which is now
    # where this task is planned, so the divergence `tests/test_atom_declarations.py`
    # recorded for it is gone rather than restated.
    consumes=(f"{EV_TEXT}@self", f"{EV_TEXT}@subtree"),
    # Nothing. The postings, the term statistics and the entries are rows in the search
    # tables, and no node gains a block. An atom that claimed to produce evidence here
    # would put this task in the audit's derivation for a key nobody could read back.
    produces=(),
    write_mode=WRITE_SELF,
    cost_class=COST_CPU,
)
def run_index_bm25(session: Session, task: TaskQueue) -> TaskOutcome:
    """Write the documents every covering BM25 index is missing. 11.5."""
    node = session.get(Document, task.scope_document_id)
    if node is None:
        raise LookupError(f"document {task.scope_document_id} is gone; nothing to index")

    names = covering_indexes(session, node)
    if not names:
        # A RACE AND NOT THE ORDINARY CASE. The planner does not offer this task where
        # nothing covers the node, so reaching here means an index was dropped, or a root
        # removed from one, between the boundary and the claim. Reported rather than
        # raised: the correct state after such a drop is exactly "no postings".
        return TaskOutcome(
            status="skipped",
            detail={
                "reason": (
                    "no BM25 index names this document or any of its ancestors as a root "
                    "(INGEST_SPEC.md 11.5); one covered it when this task was planned, so "
                    "an index or a root was removed in between"
                ),
                "candidate_roots": [node.id] + list(node.path or []),
            },
        )

    repo = SearchRepository(session)
    asked = {}
    indexed = {}
    for name in names:
        outstanding = outstanding_documents(session, node, name)
        asked[name] = len(outstanding)
        indexed[name] = sum(1 for doc_id in outstanding if repo.index_document(doc_id, name))
    session.flush()

    return TaskOutcome(
        detail={
            "indexes": names,
            # Both numbers, because they are allowed to differ and the gap is the finding:
            # a document the anti-join offered and `index_document` declined tokenized to
            # nothing. Reporting only the second would make that invisible.
            "documents_outstanding": asked,
            "documents_indexed": indexed,
        }
    )
