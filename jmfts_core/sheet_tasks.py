"""A workbook's sheets, and what each one measures to. ``INGEST_SPEC.md`` 8.1–8.5.

    file node (usetype="file")
    └── sheet node (usetype="sheet")        <- declared, guaranteed (8.1)
        └── profile node (usetype="summary")<- measured, one per sheet (8.5)

Two of 8.2's three tasks live here. ``structure:sheets`` writes the sheet list a workbook
declares about itself, which is the whole of its declared rung; ``profile:sheet`` measures
one sheet and writes the profile node. The third, ``extract:sheet``, materialises the cells
according to a representation 8.4 chooses — and **8.8 leaves every threshold that choice
reads unset**, so it is declared in the batch and has no handler. See
:data:`EXTRACT_SHEET_SPEC`.

**No shape is chosen anywhere in this module.** What ``profile:sheet`` writes is 8.3's
measurements plus the exact values a shape decision would consume, so that calibrating the
thresholds is a query over stored profiles instead of a re-ingest (``SPRINT_0_3_0.md``
6.2). A number invented here would look settled when it was a guess.

**Why this is not ``structure:declared``, which is what 8.2 names as the dependency.**
The two structure rungs in :mod:`jmfts_core.structure_tasks` consume TEXT: that module's
docstring states the invariant plainly — "they consume text, an outline and page offsets,
and nothing in them knows what produced those" — and Part 4's row for the declared rung
therefore requires ``has_text_layer`` and comes ``after`` ``extract:text``. A workbook
satisfies neither. It has no text layer, and it cannot acquire one without first choosing
a rendering for its cells, which is 8.4's decision and is exactly what 8.1 says is NOT
part of the declared rung. So the evidence is different in kind, and the row that reads it
is a different row.

What is preserved is the RUNG, which is the part 8.1 actually asserts and the part 3.5
gives meaning to: the nodes below carry ``primary_rung = "declared"``, because the sheets
are a fact the file states about itself. A reader asking "did the declared rung run for
this workbook" gets a yes from the node, which is the question 8.2's dependency column is
really asking. **8.2's task name is the thing this pass diverges from, and it is recorded
here rather than quietly reconciled.**

**The scheduling input is ``probe``'s ``has_sheets`` and this module does not recompute
it.** ``jmfts_core.probe._probe_xlsx`` reads the sheet list from the workbook part with
the standard library, in a base install, before any of this is reachable. What this module
adds is the reader (:mod:`jmfts_core.office.workbook`) — tier 2, behind
``require_openpyxl()``, at the point of use.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from jmfts_core.config import get_settings
from jmfts_core.embedding import get_embedding_service
from jmfts_core.ingest_tasks import (
    TASK_EXTRACT_SHEET,
    TASK_PROFILE_SHEET,
    TASK_STRUCTURE_SHEETS,
    TaskOutcome,
    register_task_handler,
    # Private, and imported anyway — the same import `tests/test_ingest_worker.py` and
    # `tests/test_ingest_explain.py` already make. It is the one implementation of "a
    # planned task whose handler is not registered is DEFERRED with a stated reason, and
    # so is anything ordered after it", and a second copy of that rule here could differ
    # from the one `run_probe` applies.
    _split_by_handler,
)
from jmfts_core.models.document import Document, SETTLED_IN_FLIGHT, SETTLED_SETTLED
from jmfts_core.models.task_queue import TaskQueue, WRITE_CHILDREN
from jmfts_core.office.cells import read_rows
from jmfts_core.office.sheets import measure_sheet
from jmfts_core.office.workbook import read_sheets
from jmfts_core.repositories.blob import BlobRepository
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.task_queue import TaskQueueRepository
from jmfts_core.settling import TaskSpec, enqueue_batch
from jmfts_core.sheet_profile import (
    USETYPE_SUMMARY,
    build_profile_content,
    sheet_structured_content,
)
from jmfts_core.sheet_records import (
    NO_HEADER_REASON,
    SHAPE_BASIS,
    SHAPE_RECORDS,
    USETYPE_RECORD,
    build_records,
    header_labels,
)
from jmfts_core.structure_tasks import EMBED_CHUNK_SPEC, RUNG_DECLARED, RUNG_INFERRED

#: The usetype 8.1 names. An open string like every usetype (spec Part 9), spelled here so
#: the tasks 8.2 brings — which select their scope by it — agree with what wrote it.
USETYPE_SHEET = "sheet"

#: What produced these boundaries, recorded beside the rung exactly as the text rungs
#: record theirs. It names what was READ: the workbook part's own ``<sheets>`` list, not a
#: count of ``xl/worksheets/`` parts and not anything about the cells.
SOURCE_WORKBOOK_SHEETS = "workbook_sheet_list"

#: ``INGEST_SPEC.md`` 8.2's first per-sheet task, as it is enqueued beneath every sheet.
#:
#: **``children``, where 8.2's table says ``self``, and this is a deliberate divergence.**
#: A write mode is a concurrency declaration that the claim query acts on (spec 5.3): a
#: ``self`` task conflicts only with another ``self`` on the same node. But 8.5 makes the
#: profile a CHILD of the sheet node, so this task writes children, and declaring
#: otherwise would tell the queue it is safe to run a children-writer beside it. Today the
#: only other children-writer on a sheet node is ``extract:sheet``, which is ordered after
#: this one, so the lie would not yet cost anything — which is exactly why it would still
#: be there when it did.
#:
#: ``sketch_columns`` is in the params rather than read from configuration so that it lands
#: in the row's ``param_fingerprint`` (6.1): a re-ingest that turns sketching on is a
#: different request from the one that ran without it, and the attempt diff has to see
#: that. ``true`` is the default because a column with no sketch is invisible to 8.6's
#: containment search whatever its cardinality — see :mod:`jmfts_core.sketch`.
PROFILE_SHEET_SPEC = TaskSpec(
    task_type=TASK_PROFILE_SHEET,
    write_mode=WRITE_CHILDREN,
    params={"sketch_columns": True},
)

#: How many rows one ``extract:sheet`` turns into nodes before it FAILS the task. 6.6 asks
#: for exactly this — "a named limit that fails the task, not a silent truncation" — because
#: a sheet whose first ten thousand rows became nodes is indistinguishable, from anywhere
#: downstream, from a sheet that had ten thousand rows.
#:
#: The number is a CEILING, not a measurement. It is round on purpose so that it does not
#: read as something that was counted, and it clears 8.4's own worked example (1,284 rows)
#: by an order of magnitude. Raising it is a task parameter; what it costs is one node and
#: one embedding per row.
DEFAULT_MAX_ROW_NODES = 10_000

#: 8.2's second per-sheet task: the cells themselves, one node per row.
#:
#: **It runs 8.4's ``records`` shape only, and the rule it branches on is not one of 8.8's
#: thresholds.** See :data:`jmfts_core.sheet_records.SHAPE_BASIS`. A sheet whose
#: ``header_row`` is false gets no records and says why; the shapes that would cover it are
#: still waiting on the calibration corpus.
#:
#: ``max_rows`` and ``with_cell_notes`` are parameters rather than configuration for the
#: reason ``sketch_columns`` is: they land in the row's ``param_fingerprint`` (6.1), so a
#: re-ingest that raises the ceiling is a different request from the one that failed on it.
EXTRACT_SHEET_SPEC = TaskSpec(
    task_type=TASK_EXTRACT_SHEET,
    write_mode=WRITE_CHILDREN,
    after=(TASK_PROFILE_SHEET,),
    params={"max_rows": DEFAULT_MAX_ROW_NODES, "with_cell_notes": True},
)

#: The batch every sheet node gets. In dependency order, because ``enqueue_batch`` resolves
#: an ``after`` name to the id of a spec EARLIER in the same batch.
SHEET_TASK_SPECS: tuple[TaskSpec, ...] = (PROFILE_SHEET_SPEC, EXTRACT_SHEET_SPEC)

#: Why the sheet node carries no ``rung`` for what is below it. 8.7 gives the sheet block a
#: ``rung`` that depends on the shape — ``inferred`` for every one except ``unstructured``,
#: which is ``flat`` — so a pass that chooses no shape cannot name a rung either, and
#: naming one anyway would claim evidence for boundaries that were never drawn.
NO_RUNG_REASON = (
    "INGEST_SPEC.md 8.7 makes the sheet block's `rung` a function of the shape chosen in "
    "8.4, and no shape was chosen: 8.8 leaves the thresholds unset. `profile:sheet` draws "
    "no boundary below a sheet, so it claims no rung for one"
)


@register_task_handler(TASK_STRUCTURE_SHEETS)
def run_structure_sheets(session: Session, task: TaskQueue) -> TaskOutcome:
    """Write one node per sheet the workbook names, under the file node.

    **A sheet node is created IN FLIGHT when work was queued beneath it, and SETTLED when
    none was.** That is the same rule the text rungs apply to their containers, and it is
    a derived state rather than a constant: ``settled`` must not claim a subtree is
    finished while its profile is still embedding, and equally a node with nothing queued
    beneath it and left in flight would park the whole workbook forever with no task able
    to release it. ``_split_by_handler`` decides which case this is, from what is
    registered — so the day ``extract:sheet`` gains a handler, nothing here changes.

    **An empty sheet list raises.** ``has_sheets`` is what schedules this task, probe read
    it from the same ``xl/workbook.xml`` this reader does, and the two disagreeing means
    the bytes changed underneath or the two readers do not agree about what a sheet is.
    Writing zero nodes and completing would settle a workbook as structured when nothing
    structured it.
    """
    doc = session.get(Document, task.scope_document_id)
    if doc is None:
        raise ValueError(
            f"{TASK_STRUCTURE_SHEETS} is scoped to document {task.scope_document_id}, "
            "which does not exist"
        )

    data = BlobRepository(session).read_bytes(doc.id)
    if data is None:
        raise ValueError(
            f"document {doc.id} has no stored blob; {TASK_STRUCTURE_SHEETS} was enqueued "
            "for bytes that are no longer there"
        )

    sheets = read_sheets(data)
    probed = ((doc.structured_content or {}).get("matched") or {}).get("patterns") or {}
    if not sheets:
        raise ValueError(
            f"{TASK_STRUCTURE_SHEETS} is scoped to document {doc.id}, whose workbook names "
            "no sheets; this task is enqueued only when probe reported has_sheets, so "
            "either the stored bytes are not the probed bytes or the two readers disagree "
            f"about what a sheet is (probe counted {probed.get('sheet_count')})"
        )

    repo = DocumentRepository(session)
    tasks = TaskQueueRepository(session)
    # 8.2's two tasks are scoped to a SHEET, which is a node this loop is about to create —
    # so they cannot be rows of Part 4's table, which is evaluated once per uploaded file
    # from probe's patterns, for the same reason `embed` is not a row. What 8.2's "depends
    # on the declared rung" means in this codebase's terms is that the node the task is
    # scoped to does not exist until that rung created it, which is the relationship
    # `EMBED_CHUNK_SPEC` already has with the chunker.
    runnable, deferred_tasks = _split_by_handler(SHEET_TASK_SPECS)
    child_ids: list[int] = []
    for sheet in sheets:
        node = repo.create(
            title=sheet.name,
            # No content, for the reason a section container has none: a sheet's cells are
            # not its own text, and the nodes that hold them are 8.2's to write. A markdown
            # rendering here would ALSO be a representation decision (8.4) taken by the one
            # task the spec says makes no representation decisions.
            content=None,
            parent_id=doc.id,
            usetype=USETYPE_SHEET,
            structured_content={
                "structure": {
                    "primary_rung": RUNG_DECLARED,
                    "source": SOURCE_WORKBOOK_SHEETS,
                },
                # 8.3 stores `profile:sheet`'s signals under `sheet.measurements`, so these
                # three sit beside them rather than in a block of their own.
                "sheet": {
                    "index": sheet.index,
                    "name": sheet.name,
                    "state": sheet.state,
                },
            },
            auto_embed=False,
            sequential=True,
            settled=SETTLED_IN_FLIGHT if runnable else SETTLED_SETTLED,
        )
        if runnable:
            enqueue_batch(tasks, node.id, runnable)
        child_ids.append(node.id)

    structured = dict(doc.structured_content or {})
    structured["structure"] = {
        "primary_rung": RUNG_DECLARED,
        "source": SOURCE_WORKBOOK_SHEETS,
        "node_count": len(child_ids),
        "max_depth": 1,
    }
    doc.structured_content = structured
    session.flush()

    detail = {
        "sheets": len(sheets),
        # The paired measurement, in the same spirit as `headings_probed` beside the text
        # rungs' section count: probe counted `<sheet>` elements with the standard library
        # and openpyxl resolved each one to a part, so two numbers that should agree and do
        # not are the signal that the workbook holds something other than worksheets — a
        # chartsheet, a dialog sheet, a legacy macro sheet.
        "sheets_probed": probed.get("sheet_count"),
        "hidden_sheets": sum(1 for sheet in sheets if sheet.state != "visible"),
        # Per sheet, not in total — the numbers below are what one sheet node got, and a
        # reader multiplying by `sheets` gets the workbook's fan-out.
        "queued_per_sheet": [spec.task_type for spec in runnable],
        # 3.4: a rung that ran and stopped where the spec says to stop must not look like
        # one that never ran.
        "deferred": deferred_tasks,
    }

    return TaskOutcome(
        rung=RUNG_DECLARED,
        detail=detail,
        produced={"node_count": len(child_ids), "child_ids": child_ids},
    )


# ---------------------------------------------------------------------------
# What 8.2's two per-sheet tasks both have to check first
# ---------------------------------------------------------------------------


def _scoped_sheet(session: Session, task: TaskQueue, *, task_type: str, purpose: str) -> tuple:
    """``(node, structured_content, sheet_block, sheet_name)`` for a per-sheet task.

    Both of 8.2's per-sheet tasks are scoped to a sheet node and both need the same four
    things to be true before they can do anything. Shared rather than written twice because
    the checks are the same checks — a second copy would be free to stop agreeing about
    what a sheet node is.

    ``purpose`` completes the sentence "this task ..." in the usetype error, which is the
    one place the two genuinely differ: they fail on the same condition and a reader is
    owed the reason THIS task cannot proceed.
    """
    node = session.get(Document, task.scope_document_id)
    if node is None:
        raise ValueError(
            f"{task_type} is scoped to document {task.scope_document_id}, which does not exist"
        )
    if node.usetype != USETYPE_SHEET:
        raise ValueError(
            f"{task_type} is scoped to document {node.id}, whose usetype is "
            f"{node.usetype!r} and not {USETYPE_SHEET!r}; this task {purpose} "
            "and has no meaning anywhere else in the tree"
        )
    structured = dict(node.structured_content or {})
    sheet = dict(structured.get("sheet") or {})
    name = sheet.get("name")
    if not name:
        raise ValueError(
            f"document {node.id} carries usetype {USETYPE_SHEET!r} with no `sheet.name`; "
            f"{TASK_STRUCTURE_SHEETS} writes that name and it is the only thing that says "
            "WHICH sheet of the workbook this node is"
        )
    return node, structured, sheet, name


def _sheet_blob(session: Session, node: Document, *, task_type: str, name: str) -> bytes:
    """The workbook bytes, which are on the sheet node's PARENT.

    A per-sheet task is scoped to the sheet node, which is where its output goes; the bytes
    belong to the file node above it. A sheet node with no parent is a tree that was
    rearranged underneath a queued task, and it raises.
    """
    if node.parent_id is None:
        raise ValueError(
            f"sheet node {node.id} has no parent, so there is no file node holding the "
            f"workbook it was measured from; {task_type} cannot run"
        )
    data = BlobRepository(session).read_bytes(node.parent_id)
    if data is None:
        raise ValueError(
            f"document {node.parent_id} has no stored blob; {task_type} was enqueued for "
            f"sheet {name!r} of bytes that are no longer there"
        )
    return data


# ---------------------------------------------------------------------------
# profile:sheet — INGEST_SPEC.md 8.2, 8.3, 8.5, 8.7
# ---------------------------------------------------------------------------


@register_task_handler(TASK_PROFILE_SHEET)
def run_profile_sheet(session: Session, task: TaskQueue) -> TaskOutcome:
    """Measure one sheet, write the measurements, and write the profile node. 8.3 and 8.5.

    **It does not choose a representation, and that is the whole shape of this pass.** 8.2
    says ``profile:sheet`` "measures the sheet, writes the measurements, chooses a
    representation, and writes a profile node", and 8.8 says every threshold the choice
    reads is unset pending calibration against real workbooks. Numbers picked here would
    look settled when they were guesses, so the sheet node records the measured values the
    branch consumes (``shape_decision.inputs``) and states that the branch was not taken.
    Calibration is then a query over stored profiles rather than a re-ingest —
    ``SPRINT_0_3_0.md`` 6.2.

    **No model runs.** 8.2's last column is "uses an LLM: no", and every signal here is
    counted. The tokenizer is not a model in that sense — it loads no weights and touches
    no GPU (:meth:`~jmfts_core.embedding.EmbeddingService.tokenizer`) — and it is needed
    for exactly one measurement, ``rendered_tokens``, which is a token count and cannot be
    taken any other way.

    **The blob is on the PARENT.** The task is scoped to the sheet node, which is where its
    measurements go; the bytes belong to the file node above it. A sheet node with no
    parent is a tree that was rearranged underneath a queued task, and it raises.
    """
    node, structured, sheet, name = _scoped_sheet(
        session, task, task_type=TASK_PROFILE_SHEET, purpose="measures one worksheet"
    )
    data = _sheet_blob(session, node, task_type=TASK_PROFILE_SHEET, name=name)

    settings = get_settings()
    params = task.params or {}
    with_sketches = bool(params.get("sketch_columns", True))
    # The document window, because it is the model's own limit and therefore the largest
    # value 8.4's `small_table` test could ever be calibrated to. See `measure_sheet` for
    # why that makes it a derivation rather than a chosen size.
    render_cell_budget = settings.embedding_doc_window
    measurement = measure_sheet(
        data,
        name,
        render_cell_budget=render_cell_budget,
        with_sketches=with_sketches,
    )

    service = get_embedding_service()
    rendered_tokens = None
    if measurement.rendered_markdown is not None:
        # `check_fit`, so the count is the one the embedder would take: the retrieval
        # prefix and the special tokens are part of what has to fit, and a bare count of
        # the table's own tokens would say a table fits that does not.
        rendered_tokens = service.check_fit(
            measurement.rendered_markdown, with_tokens=True
        ).token_count

    sheet.update(
        sheet_structured_content(
            measurement,
            rendered_tokens=rendered_tokens,
            token_window=settings.embedding_token_window,
            doc_window=settings.embedding_doc_window,
            embedding_model=settings.embedding_model,
            render_cell_budget=render_cell_budget,
        )
    )

    content, profile_record = build_profile_content(measurement, fits=service.fits_token_window)
    repo = DocumentRepository(session)
    profile = repo.create(
        title=f"{name} — sheet profile",
        content=content,
        parent_id=node.id,
        usetype=USETYPE_SUMMARY,
        structured_content={"profile": dict(profile_record, sheet_name=name)},
        # The model does not run here. `embed` is its own task and this node is not
        # retrievable until it has run, which is what `in_flight` states — the same
        # contract every chunk has.
        auto_embed=False,
        sequential=True,
        settled=SETTLED_IN_FLIGHT,
    )
    enqueue_batch(TaskQueueRepository(session), profile.id, (EMBED_CHUNK_SPEC,))

    sheet["profile_node_id"] = profile.id
    structured["sheet"] = sheet
    node.structured_content = structured
    session.flush()

    return TaskOutcome(
        # No rung. See NO_RUNG_REASON: 8.7 makes it a function of the shape, and no shape
        # was chosen.
        rung=None,
        detail={
            "sheet": name,
            "rows": measurement.rows,
            "cols": measurement.cols,
            "non_empty_cells": measurement.non_empty_cells,
            "merged_cells": measurement.merged_cells,
            "columns": len(measurement.columns),
            "rendered_tokens": rendered_tokens,
            "sketched_columns": sum(1 for column in measurement.columns if column.sketch),
            # Which measurements came back as a floor rather than a number, so that a
            # calibration run can exclude them instead of averaging over them.
            "bounded": {
                "interior_cardinality": not measurement.interior_cardinality_exact,
                "columns_without_exact_distinct": sum(
                    1 for column in measurement.columns if not column.distinct_exact
                ),
                "rendered": measurement.rendered_markdown is None,
            },
            "profile": profile_record,
            "no_rung": NO_RUNG_REASON,
            # COMPUTED, not stated. This read `DEFERRED_REASON[TASK_EXTRACT_SHEET]`
            # unconditionally while that task had no handler, and would have gone on
            # reporting it as deferred the day it got one. `_split_by_handler` is the one
            # implementation of "what in this batch cannot run", and `structure:sheets`
            # decides what to enqueue from the same call.
            "deferred": _split_by_handler((EXTRACT_SHEET_SPEC,))[1],
        },
        produced={"node_count": 1, "child_ids": [profile.id]},
    )


# ---------------------------------------------------------------------------
# extract:sheet — INGEST_SPEC.md 8.2, 8.4, 8.7
# ---------------------------------------------------------------------------


@register_task_handler(TASK_EXTRACT_SHEET)
def run_extract_sheet(session: Session, task: TaskQueue) -> TaskOutcome:
    """One node per row, holding the row as typed JSON. 8.4's ``records`` shape.

    **It runs one of 8.4's four shapes and says so.** The four-way branch reads thresholds
    8.8 leaves unset, and this task does not set them. What it branches on is ``header_row``
    — a measured boolean, already on the node — and that decides only whether records are
    POSSIBLE: a sheet whose first row names every column has keys, and a sheet whose first
    row does not have none. See :data:`jmfts_core.sheet_records.SHAPE_BASIS`.

    **It reads the header from the profile, not from the sheet.** ``profile:sheet`` decided
    what the header row was and recorded a label per column. Re-deriving it here could
    disagree, and then the profile and the records would describe different sheets. That is
    why this task is ordered after that one, and why a node with no ``sheet.measurements``
    raises rather than measuring for itself.

    **A row whose prose does not fit the embedding window still becomes a node.** The JSON
    record is the point of the node and it is complete either way; what will not complete is
    that node's ``embed`` task, which raises rather than embedding a truncated prefix
    (``KNOWN-DEFECTS.md`` D1). The count is in the attempt detail so an operator sees the
    condition here rather than as N failing tasks with no common cause.
    """
    node, structured, sheet, name = _scoped_sheet(
        session, task, task_type=TASK_EXTRACT_SHEET, purpose="materialises one worksheet's cells"
    )
    measurements = sheet.get("measurements")
    if not measurements:
        raise ValueError(
            f"sheet node {node.id} carries no `sheet.measurements`, so {TASK_PROFILE_SHEET} "
            f"has not run; {TASK_EXTRACT_SHEET} reads the header verdict and the column "
            "names from the profile rather than deriving them a second time, and is "
            "ordered after it for that reason"
        )

    params = task.params or {}
    max_rows = int(params.get("max_rows", DEFAULT_MAX_ROW_NODES))
    with_notes = bool(params.get("with_cell_notes", True))
    # Preserved, not rebuilt: `profile:sheet` put 8.4's branch INPUTS here and they are what
    # a calibration sweep replays against. This task adds a verdict; it does not get to
    # forget the evidence.
    decision = dict(sheet.get("shape_decision") or {})

    if not measurements.get("header_row"):
        decision.update(
            {"decided": False, "shape": None, "basis": "header_row", "reason": NO_HEADER_REASON}
        )
        sheet["shape"] = None
        sheet["shape_decision"] = decision
        structured["sheet"] = sheet
        node.structured_content = structured
        session.flush()
        return TaskOutcome(
            # 8.7 makes the rung a function of the shape and no shape was chosen.
            rung=None,
            detail={"sheet": name, "records": 0, "no_records": NO_HEADER_REASON},
            produced={"node_count": 0, "child_ids": []},
        )

    data = _sheet_blob(session, node, task_type=TASK_EXTRACT_SHEET, name=name)
    rows = read_rows(data, name, max_rows=max_rows, with_notes=with_notes)
    records = build_records(rows, header=header_labels(sheet.get("columns") or []))

    repo = DocumentRepository(session)
    tasks = TaskQueueRepository(session)
    service = get_embedding_service()
    child_ids: list[int] = []
    over_window = 0
    with_formula = 0
    text_forced = 0
    for record in records:
        if not service.fits_token_window(record.content):
            over_window += 1
        for cell in record.cells.values():
            with_formula += 1 if "formula" in cell else 0
            text_forced += 1 if cell.get("text_forced") else 0
        child = repo.create(
            # The row NUMBER, not a position in the result. A person going back to the
            # workbook to check needs the number Excel shows them down the left edge.
            title=f"{name} row {record.row_index}",
            content=record.content,
            parent_id=node.id,
            usetype=USETYPE_RECORD,
            structured_content={
                "record": record.record,
                "row_index": record.row_index,
                "sheet_name": name,
                # Sparse, and absent entirely on a row of plain values. See
                # `jmfts_core.office.cells.CellNote` for what earns a cell an entry.
                **({"cells": record.cells} if record.cells else {}),
            },
            # The model does not run here. `embed` is its own task and this node is not
            # retrievable until it has run, which is what `in_flight` states.
            auto_embed=False,
            sequential=True,
            settled=SETTLED_IN_FLIGHT,
        )
        enqueue_batch(tasks, child.id, (EMBED_CHUNK_SPEC,))
        child_ids.append(child.id)

    decision.update(
        {
            "decided": True,
            "shape": SHAPE_RECORDS,
            "basis": "header_row",
            "reason": SHAPE_BASIS,
        }
    )
    sheet["shape"] = SHAPE_RECORDS
    # 8.7's second key. `null` and not "clear": a margin says how close the decision was to
    # a boundary, and a boolean has no boundary to be close to. The reason is in
    # `shape_decision`, where a reader finds it beside the verdict.
    sheet["shape_margin"] = None
    sheet["rung"] = RUNG_INFERRED
    sheet["shape_decision"] = decision
    sheet["record_count"] = len(child_ids)
    structured["sheet"] = sheet
    node.structured_content = structured
    session.flush()

    return TaskOutcome(
        # 8.7: `inferred` for every shape except `unstructured`. The sheet node itself was
        # produced at the declared rung; the shape below it was not.
        rung=RUNG_INFERRED,
        detail={
            "sheet": name,
            "shape": SHAPE_RECORDS,
            "records": len(child_ids),
            "rows_read": len(rows.rows),
            "cols": rows.cols,
            "max_rows": max_rows,
            # What the standard-library pass found that openpyxl's read-only mode cannot
            # report. Zero with `cell_notes_read` false means nothing looked.
            "cell_notes_read": rows.notes_read,
            "cells_with_formula": with_formula,
            "cells_text_forced": text_forced,
            # A condition, not a failure, and not this task's to resolve — see the
            # docstring. Their `embed` tasks are the ones that will raise.
            "records_over_token_window": over_window,
        },
        produced={"node_count": len(child_ids), "child_ids": child_ids},
    )
