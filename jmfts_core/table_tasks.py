"""``extract:tables``: a PDF's inline tables as first-class ``table`` nodes. ``INGEST_SPEC.md``
Part 4, and phasing step 5's remainder.

**The table is not rendered here.** ``extract:text`` already rendered each table once, in
reading order, into the file node's markdown (``pdf_extraction._process_page``), and it
recorded where: ``extraction.tables`` holds a ``{page, index, bbox, start, end, rows, cols}``
span per rendered table. This task slices ``content[start:end]`` and writes it as a child.
That is the rule ``sheet_tasks._write_table`` follows — "the markdown is the profile's, byte
for byte" — and for the same reason: a second rendering would be a second answer to what
the table looks like, free to disagree with the one the chunks were cut from. Re-opening the
blob would also re-run ``find_tables``, which is 96.5% of a PDF's extraction wall clock.

**It is reached by a re-plan, not by probe.** ``pages_with_tables`` is measured by
``extract:text``, so probe cannot decide this row; ``run_extract_text`` calls
``plan_after_measurement`` once the measurement exists, and that is what enqueues this task
on the file node (spec 3.3, "enqueued by the task that measures it rather than by probe").

**The table text is in the chunks too, and that is accepted for now.** The table stays
inline in ``doc.content``, so whatever chunk the structure rung cuts around it holds the same
cells. The chunk keeps the table's prose context; the table node is the precise retrieval
unit. Removing the duplicate means the rungs cutting table spans out of their chunks, which
couples them to this task and changes chunk boundaries for every table-bearing PDF, so it
waits for a measurement of what the duplicate costs BM25.

**Every check here is a fail-early check, not a filter.** The row fired, so
``pages_with_tables`` was non-empty, so the record must carry spans for exactly those pages,
and each span must still be a table in ``content``. Any disagreement means the offsets and
the text came from different extractions, and writing nodes from them would put the wrong
characters under a table's name.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from jmfts_core.atoms import (
    COST_CPU,
    EV_EXTRACTION,
    EV_SOURCE_SPAN,
    EV_TABLE,
    EV_TEXT,
    KEY_NATURAL,
    ChildKey,
    Fanout,
)
from jmfts_core.config import get_settings
from jmfts_core.embedding import get_embedding_service
from jmfts_core.ingest_tasks import (
    TASK_EXTRACT_TABLES,
    TaskOutcome,
    enqueue_frontier,
    plan_frontier,
    register_task_handler,
    without_token_vectors,
)
from jmfts_core.models.document import SETTLED_IN_FLIGHT, USETYPE_TABLE, Document
from jmfts_core.models.task_queue import WRITE_CHILDREN, TaskQueue
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.evidence import EvidenceRepository
from jmfts_core.repositories.task_queue import TaskQueueRepository
from jmfts_core.structure_tasks import _scope_node


def table_key(page: int, index: int) -> str:
    """A table's natural key: its page and its position among that page's tables.

    Natural rather than positional (``SPRINT_JOBS.md`` 9.2): a re-run matches the same table
    even if an earlier page gained one, which a document-wide ordinal would not survive.
    """
    return f"p{page}.t{index}"


def _table_ceiling(evidence: dict, params: dict) -> tuple[int, int]:
    """Exactly one node per span ``extract:text`` recorded. The handler writes all or none."""
    n = int(evidence["extraction.tables_found"])
    return (n, n)


_TABLE_FANOUT = Fanout(
    bound=_table_ceiling,
    reads=("extraction.tables_found",),
    basis="one node per table extract:text rendered inline",
    counts=USETYPE_TABLE,
)


def _checked_spans(doc: Document, extraction: dict) -> list[dict]:
    """``extraction.tables``, after proving each span still is the table it claims to be.

    Raises on the first disagreement; see the module docstring for why each is a defect and
    not a case to skip.
    """
    tables = extraction.get("tables")
    if not isinstance(tables, list):
        raise ValueError(
            f"extract:tables is scoped to document {doc.id}, whose extraction record carries "
            f"no `tables` spans (source {extraction.get('source')!r}); the row fired on "
            "`pages_with_tables`, and the reader that measures it records a span per table"
        )
    if len(tables) != extraction.get("tables_found"):
        raise ValueError(
            f"document {doc.id}'s extraction record counts {extraction.get('tables_found')} "
            f"tables and carries {len(tables)} spans; the two were written by one pass"
        )
    pages = extraction.get("pages_with_tables") or []
    spanned = {t["page"] for t in tables}
    if spanned != set(pages):
        raise ValueError(
            f"document {doc.id}'s extraction record names tables on pages {sorted(pages)} "
            f"and carries spans for pages {sorted(spanned)}; the two were written by one "
            "pass and cannot disagree"
        )

    content = doc.content or ""
    seen: set[str] = set()
    for t in tables:
        key = table_key(t["page"], t["index"])
        if key in seen:
            raise ValueError(f"document {doc.id} records table {key} twice")
        seen.add(key)
        start, end = t["start"], t["end"]
        if not 0 <= start < end <= len(content):
            raise ValueError(
                f"document {doc.id} table {key} spans [{start}, {end}), outside content of "
                f"{len(content)} characters; the offsets are not this text's"
            )
        lines = content[start:end].split("\n")
        # `_table_to_markdown`: a header row, a separator, then the data rows, one line each,
        # every one opening and closing with a pipe.
        if (
            len(lines) != t["rows"] + 1
            or not all(line.startswith("|") and line.endswith("|") for line in lines)
            or not lines[1].startswith("| --- ")
        ):
            raise ValueError(
                f"document {doc.id} table {key} spans [{start}, {end}), and that text is not "
                f"a {t['rows']}-row markdown table; the offsets are stale"
            )
    return tables


# `extraction@self` is the derived edge: the spans are `extract:text`'s, and `text@self` is
# the string they index. Both are on the file node, which is why `after` names it.
@register_task_handler(
    TASK_EXTRACT_TABLES,
    consumes=(f"{EV_TEXT}@self", f"{EV_EXTRACTION}@self"),
    produces=(f"{EV_TABLE}@children", f"{EV_TEXT}@children", f"{EV_SOURCE_SPAN}@children"),
    write_mode=WRITE_CHILDREN,
    cost_class=COST_CPU,
    fanout=_TABLE_FANOUT,
    child_key=ChildKey(KEY_NATURAL, path="table.key"),
)
def run_extract_tables(session: Session, task: TaskQueue) -> TaskOutcome:
    """Write one ``table`` child per span ``extract:text`` recorded, and plan its ``embed``.

    Each child holds ``content[start:end]`` exactly, with ``source_span`` set to that span
    and the page and bbox on its ``table`` evidence — a citation anchor the ``citation``
    pass never has to compute, and does not try to (it annotates ``chunk`` nodes only).

    ``with_tokens`` is decided per table from the tokenizer, as ``_write_table`` decides it
    per sheet. A table past the DOCUMENT window raises: no ``embed`` could take it, and a
    node nobody can retrieve is the failure, not a shape to write.
    """
    doc = _scope_node(session, task, TASK_EXTRACT_TABLES)
    extraction = EvidenceRepository(session).read(doc.id, "extraction") or {}
    tables = _checked_spans(doc, extraction)

    frontier = plan_frontier(session, doc, produced_by=TASK_EXTRACT_TABLES, usetype=USETYPE_TABLE)
    if not frontier.in_flight:
        raise ValueError(
            f"document {doc.id} has {len(tables)} tables and no task is planned for a "
            f"{USETYPE_TABLE!r} child of {TASK_EXTRACT_TABLES}; the nodes would be written "
            "with nothing to give them a vector, which is a retrievable shape no query can reach"
        )

    settings = get_settings()
    service = get_embedding_service()
    repo = DocumentRepository(session)
    queue = TaskQueueRepository(session)
    content = doc.content or ""
    measured: list[tuple[dict, str, int]] = []
    for t in tables:
        text = content[t["start"] : t["end"]]
        # `check_fit`, so the count includes the retrieval prefix and special tokens the
        # embedder will add — the same count `profile:sheet` takes for the same decision.
        tokens = service.check_fit(text, with_tokens=True).token_count
        if tokens > settings.embedding_doc_window:
            raise ValueError(
                f"document {doc.id} table {table_key(t['page'], t['index'])} is {tokens} "
                f"tokens, past the {settings.embedding_doc_window}-token document window; "
                "no embed path can take it"
            )
        measured.append((t, text, tokens))

    written: list[int] = []
    document_vector_only: list[str] = []
    for t, text, tokens in measured:
        key = table_key(t["page"], t["index"])
        fits_token_window = tokens <= settings.embedding_token_window
        child_frontier = frontier if fits_token_window else without_token_vectors(frontier)
        if not fits_token_window:
            document_vector_only.append(key)
        child = repo.create(
            title=f"Table {t['index'] + 1} (page {t['page'] + 1})",
            content=text,
            parent_id=doc.id,
            usetype=USETYPE_TABLE,
            produced_by=TASK_EXTRACT_TABLES,
            evidence={
                "table": {
                    "key": key,
                    "page": t["page"],
                    "index": t["index"],
                    "bbox": t["bbox"],
                    "rows": t["rows"],
                    "cols": t["cols"],
                    "rendered_tokens": tokens,
                    "fits_token_window": fits_token_window,
                },
                "source_span": [t["start"], t["end"]],
            },
            # The model does not run here; `embed` is its own task and the node is not
            # retrievable until it has run, which is what `in_flight` states.
            auto_embed=False,
            sequential=True,
            settled=SETTLED_IN_FLIGHT,
        )
        enqueue_frontier(queue, child, child_frontier)
        written.append(child.id)

    return TaskOutcome(
        detail={
            "tables_written": len(written),
            "pages": sorted({t["page"] for t in tables}),
            "document_vector_only": document_vector_only,
            "deferred": frontier.deferred,
        }
    )
