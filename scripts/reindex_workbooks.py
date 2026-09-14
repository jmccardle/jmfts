#!/usr/bin/env python3
"""Index workbook subtrees into the BM25 indexes that already cover them.

``docs/STRESS_CORPUS.md`` 4.4: no ``index:bm25`` task was ever enqueued for an xlsx, so
every spreadsheet in a corpus is absent from its own index and from ``universal``. That is
a scheduling defect and it is not fixed here — this script fixes the CORPUS, not the
pipeline.

**THE PIPELINE IS FIXED NOW** (``SPRINT_0_6_0.md`` Block B step 7): ``index:bm25`` is a rule
at the settling boundary rather than a ``TASK_ROWS`` row, so a workbook uploaded after that
lands joins its covering indexes on its own. ``docs/MEASURE_BM25_BOUNDARY.md`` §5.5 is why
this script stays anyway — it repairs a corpus ingested BEFORE that, and the boundary rule
does not fire retroactively over a tree that has already settled. Re-ingesting is the other
way to get the same postings and it costs every embedding again.

Why it needs no pipeline fix to be correct: the defect is one of ORDER, and an already
ingested workbook has no order left. Its tree is complete on disk, so walking it now and
indexing every content-bearing node produces exactly the postings a correctly scheduled
``index:bm25`` would have written. The same script run mid-ingest would not, which is why
it refuses a subtree that is still in flight.

Membership follows ``INGEST_SPEC.md`` 11.5 and is read, not chosen: a node belongs to every
index whose registered root is that node or one of its ancestors. This creates no index and
adds no root. A workbook nothing covers is reported and skipped, which is the same answer
``run_index_bm25`` gives.

    python -m scripts.reindex_workbooks              # say what would be indexed
    python -m scripts.reindex_workbooks --apply      # write the postings
    python -m scripts.reindex_workbooks --root 2     # only under document 2

Reads ``JMFTS_DB_*`` from the environment like everything else; there is no path in this
file. Run it where the appliance's ``.env`` is.
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

from jmfts_core.database import get_session
from jmfts_core.index_tasks import covering_indexes
from jmfts_core.models.document import Document, SETTLED_SETTLED, USETYPE_SHEET
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.search import SearchRepository


def workbook_file_nodes(session, root_id: int | None) -> list[Document]:
    """Every ``file`` node with a ``sheet`` child — which is what ``structure:sheets`` wrote.

    The format column is not consulted. A workbook that reached ``structure:sheets`` has
    sheet children, and one that did not has no rows to index whatever its bytes say; asking
    the tree keeps this in step with what the pipeline actually built.
    """
    sheets = select(Document.parent_id).where(Document.usetype == USETYPE_SHEET)
    query = select(Document).where(Document.id.in_(sheets))
    if root_id is not None:
        query = query.where(Document.path.contains([root_id]))
    return list(session.execute(query.order_by(Document.id)).scalars().all())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the postings; without it nothing is committed",
    )
    parser.add_argument(
        "--root",
        type=int,
        default=None,
        help="only workbooks with this document in their path",
    )
    args = parser.parse_args()

    with get_session() as session:
        workbooks = workbook_file_nodes(session, args.root)
        if not workbooks:
            print("no workbook has sheet children; nothing to index")
            return 0

        repo = DocumentRepository(session)
        search = SearchRepository(session)
        total = {"workbooks": 0, "documents": 0, "uncovered": 0, "in_flight": 0}

        for node in workbooks:
            names = covering_indexes(session, node)
            if not names:
                total["uncovered"] += 1
                print(f"{node.id} {node.title!r}: no index covers it — skipped")
                continue

            # `include_in_flight=True` so the walk RETURNS rather than raises, and the
            # settledness test below is this script's own. `get_subtree` refuses an
            # unsettled ROOT outright, which would abort the run on the first such workbook
            # and leave every later one unexamined — the check has to see the tree to report
            # which nodes are unsettled, and it cannot see it through the exception.
            subtree = repo.get_subtree(node.id, include_in_flight=True)
            unsettled = [d.id for d in subtree if d.settled != SETTLED_SETTLED]
            if unsettled:
                # FAIL EARLY, and this is the one case where the script's answer would
                # differ from the pipeline's. A subtree still being written has nodes whose
                # `content` is not final, and indexing it now would write postings for text
                # that is about to change with nothing scheduled to correct them.
                total["in_flight"] += 1
                print(
                    f"{node.id} {node.title!r}: {len(unsettled)} of {len(subtree)} node(s) "
                    f"not settled ({unsettled[:5]}) — skipped, re-run when the queue is dry"
                )
                continue

            with_text = [d for d in subtree if d.content]
            written = 0
            for name in names:
                for doc in with_text:
                    if args.apply and search.index_document(doc.id, name):
                        written += 1
                    elif not args.apply:
                        written += 1
            total["workbooks"] += 1
            total["documents"] += written
            verb = "indexed" if args.apply else "would index"
            # `written` counts DOCUMENT-INDEX PAIRS, not documents: a workbook under two
            # covering indexes is indexed into both, so the number is deliberately larger
            # than the node count and the message says which is which.
            print(
                f"{node.id} {node.title!r}: {verb} {len(with_text)} node(s) with text "
                f"into {len(names)} index(es) {names} — {written} row(s)"
            )

        if args.apply:
            session.commit()

        print(
            f"\n{total['workbooks']} workbook(s), {total['documents']} document-index "
            f"row(s), {total['uncovered']} uncovered, {total['in_flight']} in flight"
        )
        if not args.apply:
            print("nothing was written; re-run with --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
