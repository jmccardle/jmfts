"""A workbook's sheets, and what each one measures to. ``INGEST_SPEC.md`` 8.1–8.5.

    file node (usetype="file")
    └── sheet node (usetype="sheet")        <- declared, guaranteed (8.1)
        ├── profile node (usetype="profile")<- measured, one per sheet (8.5)
        ├── table node   (usetype="table")  <- 8.4's `small_table`, where it fits
        └── record nodes (usetype="record") <- 8.4's `records`, where there is a header

All three of 8.2's tasks live here. ``structure:sheets`` writes the sheet list a workbook
declares about itself, which is the whole of its declared rung; ``profile:sheet`` measures
one sheet and writes the profile node; ``extract:sheet`` materialises the cells according to
a representation 8.4 chooses — and it runs TWO of 8.4's four shapes and emits both where
both match, because **8.8 leaves every threshold the other two read unset** while neither of
these two reads one. See :func:`run_extract_sheet`.

**The two per-sheet tasks are declared in** :data:`~jmfts_core.ingest_tasks.TASK_ROWS`
**and not here.** They were literal ``TaskSpec`` values in this module until
``SPRINT_JOBS.md`` Phase 3 gave a rule a scope; they are now rows scoped to the children
``structure:sheets`` produces, which is what makes their parameters settable, their
fingerprints movable and their conditions visible to ``EXPLAIN``.

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

from dataclasses import replace

from sqlalchemy.orm import Session

from jmfts_core.atoms import (
    COST_CPU,
    EV_BLOB,
    EV_CELL,
    EV_MATCHED,
    EV_PROFILE,
    EV_SHEET,
    EV_SHEET_MEASUREMENTS,
    EV_RECORD,
    EV_SHEET_SHAPE,
    EV_STRUCTURE,
    EV_TABLE,
    EV_TEXT,
    KEY_NATURAL,
    KEY_POSITION,
    LOCUS_ANCESTOR,
    ChildKey,
    Fanout,
    fixed,
)
from jmfts_core.chunking import ChunkStrategy, chunk_text
from jmfts_core.config import get_settings
from jmfts_core.embedding import get_embedding_service
from jmfts_core.ingest_options import STRUCTURE_CHUNK_PARAMS
from jmfts_core.ingest_tasks import (
    TASK_EMBED,
    TASK_EXTRACT_SHEET,
    TASK_PROFILE_SHEET,
    TASK_STRUCTURE_SHEETS,
    TaskOutcome,
    enqueue_frontier,
    plan_frontier,
    register_task_handler,
)
from jmfts_core.models.document import (
    Document,
    SETTLED_IN_FLIGHT,
    SETTLED_SETTLED,
    USETYPE_CELL,
    USETYPE_CHUNK,
    USETYPE_RECORD,
    USETYPE_SHEET,
    USETYPE_PROFILE,
    USETYPE_TABLE,
)
from jmfts_core.models.task_queue import TaskQueue, WRITE_CHILDREN
from jmfts_core.office.cells import json_value, read_rows
from jmfts_core.office.sheets import measure_sheet
from jmfts_core.office.workbook import read_sheets
from jmfts_core.repositories.blob import BlobRepository
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.evidence import EvidenceRepository, evidence_value
from jmfts_core.repositories.task_queue import TaskQueueRepository
from jmfts_core.sheet_profile import build_profile_content, sheet_evidence_block
from jmfts_core.sheet_records import (
    BOTH_SHAPES_BASIS,
    NO_HEADER_REASON,
    NO_TABLE_REASON,
    SHAPE_BASIS,
    SHAPE_RECORDS,
    SHAPE_SMALL_TABLE,
    TABLE_SHAPE_BASIS,
    RecordPlan,
    build_records,
    header_labels,
    plan_record,
)
from jmfts_core.structure_tasks import RUNG_DECLARED, RUNG_INFERRED

#: What produced these boundaries, recorded beside the rung exactly as the text rungs
#: record theirs. It names what was READ: the workbook part's own ``<sheets>`` list, not a
#: count of ``xl/worksheets/`` parts and not anything about the cells.
SOURCE_WORKBOOK_SHEETS = "workbook_sheet_list"

# 8.2's TWO PER-SHEET TASKS ARE NOT DECLARED IN THIS MODULE ANY MORE. They were
# `PROFILE_SHEET_SPEC`, `EXTRACT_SHEET_SPEC` and `SHEET_TASK_SPECS` — literal `TaskSpec`
# values with literal `params` dicts, which is what `SPRINT_JOBS.md` Part 0 measured as the
# second of three planners and as the reason the sheet knobs could not be set by anybody.
# They are rows of `TASK_ROWS` now, scoped to the children `structure:sheets` produces, and
# `plan_frontier` is what reads them. Four things follow, and each one was a defect:
#
#   * `max_rows`, `with_cell_notes` and `sketch_columns` are reachable from a caller's
#     ingest options (`sheet_records` and `sheet_profile` in `jmfts_core.ingest_options`);
#   * so `sheet_tasks`' claim that they land in the `param_fingerprint` — "a re-ingest that
#     raises the ceiling is a different request from the one that failed on it" — describes
#     a fingerprint a request can now actually move;
#   * `EXPLAIN` reaches them, where before it stopped at the sheet list;
#   * `max_rows` goes through `ingest_options._positive_int`, which rejects `bool` —
#     `int(True)` is 1, and one record node for a whole sheet was a live outcome the moment
#     the knob became reachable.
#
# What did NOT change is where the enqueue happens. 6.4: the settling walk travels upward
# only, so a freshly created child is unreachable from below and its work must be enqueued
# where it is created. `run_structure_sheets` still does that, per sheet; what it no longer
# holds is the list of what to enqueue.

#: Why the sheet node carries no ``rung`` for what is below it. 8.7 gives the sheet block a
#: ``rung`` that depends on the shape — ``inferred`` for every one except ``unstructured``,
#: which is ``flat`` — so a pass that chooses no shape cannot name a rung either, and
#: naming one anyway would claim evidence for boundaries that were never drawn.
NO_RUNG_REASON = (
    "INGEST_SPEC.md 8.7 makes the sheet block's `rung` a function of the shape chosen in "
    "8.4, and no shape was chosen: 8.8 leaves the thresholds unset. `profile:sheet` draws "
    "no boundary below a sheet, so it claims no rung for one"
)


def _sheet_ceiling(evidence: dict, params: dict) -> tuple[int, int]:
    """One node per sheet the workbook names. Exact in both directions.

    ``sheet_count`` is what probe read out of ``xl/workbook.xml``, and the handler raises
    when openpyxl disagrees with it — so this bound is not an estimate that a run might
    exceed, it is a number the run asserts against.
    """
    count = int(evidence["matched.patterns.sheet_count"])
    return (count, count)


# The sheet's own name, which is the one natural key in the appliance today: the workbook
# names it, a re-ingest of an edited workbook names it the same, and it survives a sheet
# being moved. 9.2's strongest form, and the only atom that gets it for free.
@register_task_handler(
    TASK_STRUCTURE_SHEETS,
    consumes=(f"{EV_MATCHED}@self", f"{EV_BLOB}@self"),
    produces=(f"{EV_STRUCTURE}@self", f"{EV_SHEET}@children"),
    write_mode=WRITE_CHILDREN,
    cost_class=COST_CPU,
    fanout=Fanout(
        bound=_sheet_ceiling,
        reads=("matched.patterns.sheet_count",),
        basis="one node per sheet",
        counts=USETYPE_SHEET,
    ),
    child_key=ChildKey(KEY_NATURAL, path="sheet.name"),
)
def run_structure_sheets(session: Session, task: TaskQueue) -> TaskOutcome:
    """Write one node per sheet the workbook names, under the file node.

    **A sheet node is created IN FLIGHT when work was queued beneath it, and SETTLED when
    none was.** That is the same rule the text rungs apply to their containers, and it is
    a derived state rather than a constant: ``settled`` must not claim a subtree is
    finished while its profile is still embedding, and equally a node with nothing queued
    beneath it and left in flight would park the whole workbook forever with no task able
    to release it. :attr:`~jmfts_core.ingest_tasks.Frontier.in_flight` decides which case
    this is, from what the rows for this scope came out as — so the day a per-sheet row
    gains or loses a handler, nothing here changes.

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
    probed = evidence_value(session, doc.id, "matched.patterns") or {}
    if not sheets:
        raise ValueError(
            f"{TASK_STRUCTURE_SHEETS} is scoped to document {doc.id}, whose workbook names "
            "no sheets; this task is enqueued only when probe reported has_sheets, so "
            "either the stored bytes are not the probed bytes or the two readers disagree "
            f"about what a sheet is (probe counted {probed.get('sheet_count')})"
        )

    repo = DocumentRepository(session)
    tasks = TaskQueueRepository(session)
    # 8.2's two tasks are scoped to a SHEET, which is a node this loop is about to create.
    # That is what 8.2's "depends on the declared rung" means in this codebase's terms —
    # the node the task is scoped to does not exist until this rung creates it — and since
    # Phase 3 it is written down as the rows' scope rather than as a tuple here.
    #
    # Planned ONCE for the whole workbook and enqueued per sheet: the rows are decided by
    # `(format, patterns, options)`, and every sheet of one workbook shares all three. A
    # forty-sheet workbook is one evaluation and forty enqueues.
    frontier = plan_frontier(session, doc, produced_by=TASK_STRUCTURE_SHEETS, usetype=USETYPE_SHEET)
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
            # 4.2's stamp, and it is what makes "the children `structure:sheets` produced"
            # answerable — which is the scope both of 8.2's tasks are declared at.
            produced_by=TASK_STRUCTURE_SHEETS,
            evidence={
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
            settled=SETTLED_IN_FLIGHT if frontier.in_flight else SETTLED_SETTLED,
        )
        if frontier.in_flight:
            enqueue_frontier(tasks, node, frontier)
        child_ids.append(node.id)

    EvidenceRepository(session).write(
        doc.id,
        "structure",
        {
            "primary_rung": RUNG_DECLARED,
            "source": SOURCE_WORKBOOK_SHEETS,
            "node_count": len(child_ids),
            "max_depth": 1,
        },
    )
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
        "queued_per_sheet": [spec.task_type for spec in frontier.specs],
        # 3.4: a rung that ran and stopped where the spec says to stop must not look like
        # one that never ran.
        "deferred": frontier.deferred,
        # And a row whose CONDITION was false is a third thing again. It could not be
        # reported here before Phase 3, because the sheet tier was a literal tuple with no
        # condition to report; it is a row now, and a row that did not fire has a reason.
        "not_applicable": frontier.not_applicable,
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
    """``(node, sheet_block, sheet_name)`` for a per-sheet task.

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
    sheet = dict(EvidenceRepository(session).read(node.id, "sheet") or {})
    name = sheet.get("name")
    if not name:
        raise ValueError(
            f"document {node.id} carries usetype {USETYPE_SHEET!r} with no `sheet.name`; "
            f"{TASK_STRUCTURE_SHEETS} writes that name and it is the only thing that says "
            "WHICH sheet of the workbook this node is"
        )
    return node, sheet, name


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


# `cpu`, and the docstring below argues it: the tokenizer loads no weights and touches no
# GPU, so `rendered_tokens` costs a parse and not a forward pass. The profile node this
# writes carries an `embed`, and THAT is the model-class work — it is its own atom, on its
# own node, which is the whole reason `embed` was split out.
#
# `blob@ancestor` is the fourth locus, and this atom is one of the two that needed it. The
# bytes are on the file node above; `SPRINT_JOBS.md` 2.2 offered three loci and all three
# read downward or at the node itself.
@register_task_handler(
    TASK_PROFILE_SHEET,
    consumes=(f"{EV_SHEET}@self", f"{EV_BLOB}@{LOCUS_ANCESTOR}"),
    produces=(f"{EV_SHEET_MEASUREMENTS}@self", f"{EV_PROFILE}@children", f"{EV_TEXT}@children"),
    write_mode=WRITE_CHILDREN,
    cost_class=COST_CPU,
    fanout=fixed(1, counts=USETYPE_PROFILE),
    child_key=ChildKey(KEY_NATURAL, path="profile.sheet_name"),
)
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
    node, sheet, name = _scoped_sheet(
        session, task, task_type=TASK_PROFILE_SHEET, purpose="measures one worksheet"
    )
    data = _sheet_blob(session, node, task_type=TASK_PROFILE_SHEET, name=name)

    settings = get_settings()
    # `params[...]` and not `params.get(..., True)`, for the reason `run_extract_sheet`
    # gives about its own two: the `sheet_profile` group is complete on every queue row
    # `plan_frontier` writes, so a default here would be a second source of the value and
    # which one ran would depend on what enqueued the task.
    params = task.params or {}
    with_sketches = bool(params["sketch_columns"])
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
        sheet_evidence_block(
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
    frontier = plan_frontier(session, node, produced_by=TASK_PROFILE_SHEET, usetype=USETYPE_PROFILE)
    profile = repo.create(
        title=f"{name} — sheet profile",
        content=content,
        parent_id=node.id,
        usetype=USETYPE_PROFILE,
        produced_by=TASK_PROFILE_SHEET,
        evidence={"profile": dict(profile_record, sheet_name=name)},
        # The model does not run here. `embed` is its own task and this node is not
        # retrievable until it has run, which is what `in_flight` states — the same
        # contract every chunk has.
        auto_embed=False,
        sequential=True,
        settled=SETTLED_IN_FLIGHT if frontier.in_flight else SETTLED_SETTLED,
    )
    enqueue_frontier(TaskQueueRepository(session), profile, frontier)

    sheet["profile_node_id"] = profile.id
    EvidenceRepository(session).write(node.id, "sheet", sheet)
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
            # The PROFILE NODE's frontier, which is `embed`. It used to be
            # `_split_by_handler((EXTRACT_SHEET_SPEC,))[1]` — this task reporting whether
            # its SIBLING could run — because this module held the sheet batch and this was
            # the only place left to say so. `structure:sheets` reports that now, from the
            # frontier it planned, which is where the decision is actually taken.
            "deferred": frontier.deferred,
        },
        produced={"node_count": 1, "child_ids": [profile.id]},
    )


# ---------------------------------------------------------------------------
# extract:sheet — INGEST_SPEC.md 8.2, 8.4, 8.7
# ---------------------------------------------------------------------------


def chunk_prose(text: str) -> list:
    """A cell's prose, in pieces that each fit — with the parameters prose is chunked by.

    NOT ``EmbeddingService.chunk_to_fit``, which is the same call with the library's bare
    defaults: ``ChunkStrategy.sentence``, unpacked. That is the strategy
    :data:`~jmfts_core.ingest_options.STRUCTURE_CHUNK_PARAMS` was measured against and
    beat — over 25 extracted papers the share of leaves too short to carry a retrievable
    idea is 9.7% for ``sentence`` and 0.1% at 120 words packed. `ran` against the reference
    corpus's two document-bearing sheets, with this function and without it: 20 over-window
    cells became 311 chunks averaging 203 characters with 8 under eight words on the bare
    default, and 93 chunks averaging 683 with NONE under eight words here. Same cells, same
    predicate, a third as many nodes.

    A cell's prose is prose. ``STRUCTURE_CHUNK_PARAMS`` says how to chunk it and its own
    comment already records that ``structure_tasks`` reads it directly for the same reason
    — the values belong to the ACT of chunking, not to the format that fed it, which is
    what :mod:`jmfts_core.ingest_options` argues at length. A second copy under the
    ``sheet_records`` group would be two numbers for one decision.

    ``fits`` is the measurement rather than ``max_chars``' proxy (``KNOWN-DEFECTS`` D7), so
    every piece is checked against the tokenizer and not against an exchange rate.
    """
    service = get_embedding_service()
    return [
        chunk.text
        for chunk in chunk_text(
            text,
            strategy=ChunkStrategy(STRUCTURE_CHUNK_PARAMS["chunk_strategy"]),
            max_tokens=int(STRUCTURE_CHUNK_PARAMS["max_tokens"]),
            min_chunk_length=int(STRUCTURE_CHUNK_PARAMS["min_chunk_length"]),
            max_chars=get_settings().chunk_max_chars,
            fits=service.fits_token_window,
        )
    ]


def _born(frontier) -> str:
    """The ``settled`` a freshly created node takes, from the frontier its subtree gets.

    ``Frontier.in_flight`` says it for a leaf — a node created ``settled`` with work queued
    is a false claim its ancestors roll up over. A CONTAINER reads the frontier of the
    children it is about to get, because it has no work of its own: what will settle it is
    the walk arriving from below once those children are done, and that walk only happens
    if something down there was queued at all.
    """
    return SETTLED_IN_FLIGHT if frontier.in_flight else SETTLED_SETTLED


def _write_cells(
    repo: DocumentRepository,
    tasks: TaskQueueRepository,
    record_node: Document,
    plan: RecordPlan,
    *,
    record,
    sheet_name: str,
    cell_frontier,
    piece_frontier,
) -> tuple[int, int]:
    """One ``cell`` node per column of a row too long to embed whole, and its pieces.

    Returns ``(cells, pieces)``. The nodes are GRANDCHILDREN of the sheet and the atom
    declares ``text@subtree`` for them, which is the same asymmetry the two structure rungs
    already carry: a chunk under a titled section is a grandchild of the node the rung ran
    on, and the write mode stays ``children`` because the region reserved is the one this
    task creates from nothing.

    A container with an EMPTY frontier below it would park the tree forever — born
    ``in_flight``, nothing queued to settle it, no walk ever reaching it — so an empty one
    raises here rather than being written and discovered as a stall. It means the ``embed``
    row's scope stopped naming a usetype this rule writes.
    """
    if not cell_frontier.in_flight:
        raise ValueError(
            f"row {record.row_index} of {sheet_name!r} does not fit the embedding window and "
            f"has to become a container, but no task is planned for its {USETYPE_CELL!r} "
            "children; the container would be created in flight with nothing queued to "
            "settle it"
        )

    cells = 0
    pieces = 0
    for position, cell in enumerate(plan.cells):
        # `record` is what a row's typed values are called and this is one of them. The
        # column name is on the node because the prose alone cannot say which field it is:
        # a split row has lost the sentence its neighbours gave it.
        evidence = {
            "cell": {
                "column": cell.key,
                "value": json_value(cell.value),
                "row_index": record.row_index,
                "sheet_name": sheet_name,
                "position": position,
            }
        }
        child = repo.create(
            title=f"{record_node.title} · {cell.key}",
            content=cell.content,
            parent_id=record_node.id,
            usetype=USETYPE_CELL,
            produced_by=TASK_EXTRACT_SHEET,
            evidence=evidence,
            auto_embed=False,
            sequential=True,
            settled=_born(cell_frontier if cell.content is not None else piece_frontier),
        )
        cells += 1
        if cell.content is not None:
            enqueue_frontier(tasks, child, cell_frontier)
            continue

        if not piece_frontier.in_flight:
            raise ValueError(
                f"column {cell.key!r} of row {record.row_index} does not fit the embedding "
                f"window and has to become a container, but no task is planned for its "
                f"{USETYPE_CHUNK!r} children"
            )
        for index, piece in enumerate(cell.pieces):
            grandchild = repo.create(
                title=f"{child.title} ({index + 1} of {len(cell.pieces)})",
                content=piece,
                parent_id=child.id,
                usetype=USETYPE_CHUNK,
                produced_by=TASK_EXTRACT_SHEET,
                auto_embed=False,
                sequential=True,
                settled=_born(piece_frontier),
            )
            enqueue_frontier(tasks, grandchild, piece_frontier)
            pieces += 1
    return cells, pieces


def _write_table(
    session: Session,
    repo: DocumentRepository,
    tasks: TaskQueueRepository,
    node: Document,
    *,
    sheet_name: str,
    markdown: str,
    measurements: dict,
    fits_token_window: bool,
) -> int:
    """One node holding the whole sheet as a markdown table. 8.4's ``small_table``.

    **The markdown is the profile's, byte for byte.** ``profile:sheet`` rendered it while it
    was measuring the sheet and kept it on the node when it fits the document window; this
    task writes it out. A second rendering here would be a second answer to "what does this
    sheet look like as a table", and the token count that decided the shape would then be a
    count of a string nobody stored. It is the same rule this handler already follows for
    the header verdict and the column labels.

    **It asks for NO token vectors when the table does not fit the token window, and the
    measurement decides it.** This appliance has two windows — 512 on the token/MaxSim path,
    8192 on the document-vector path — and ``small_table`` is tested against the second,
    because that is the model's own limit and therefore the largest value 8.4's test could be
    calibrated to. It is also not close to the first: 52.0% of FUSE sheets fit 8192 and not
    512, and at 512 the shape does not earn its implementation on the open web. So a table
    node routinely holds text the token path cannot take, and ``embed_with_tokens`` refuses
    it rather than embedding a prefix — correctly, and the outcome would be a node with no
    vector and a permanently failed task, which is ``KNOWN-DEFECTS`` D1 in a third costume.
    ``with_tokens`` false is the same document-vector-only treatment every container in the
    tree gets from ``summarize``, and here it is a fact about this sheet's size written into
    the queue row where the attempt log and ``EXPLAIN`` can both see it — not a handler
    quietly deciding for itself, and not a shape emitted at a window nobody measured.
    """
    frontier = plan_frontier(session, node, produced_by=TASK_EXTRACT_SHEET, usetype=USETYPE_TABLE)
    if not frontier.in_flight:
        raise ValueError(
            f"sheet {sheet_name!r} matched {SHAPE_SMALL_TABLE} and no task is planned for "
            f"its {USETYPE_TABLE!r} child; the node would be written with nothing to give "
            "it a vector, which is a retrievable shape that no query can reach"
        )
    if not fits_token_window:
        frontier = replace(
            frontier,
            specs=tuple(
                (
                    replace(spec, params={**spec.params, "with_tokens": False})
                    if spec.task_type == TASK_EMBED
                    else spec
                )
                for spec in frontier.specs
            ),
        )
    child = repo.create(
        # Not "row N": this node IS the sheet, which is the one thing a reader has to be able
        # to tell it from its own siblings.
        title=f"{sheet_name} — whole sheet",
        content=markdown,
        parent_id=node.id,
        usetype=USETYPE_TABLE,
        produced_by=TASK_EXTRACT_SHEET,
        evidence={
            "table": {
                "sheet_name": sheet_name,
                "rows": measurements.get("rows"),
                "cols": measurements.get("cols"),
                "first_data_row": measurements.get("first_data_row"),
                "header_row_number": measurements.get("header_row_number"),
                "rendered_tokens": measurements.get("rendered_tokens"),
                "fits_token_window": fits_token_window,
                "fits_doc_window": measurements.get("rendered_fits_doc_window"),
            }
        },
        # The model does not run here. `embed` is its own task and this node is not
        # retrievable until it has run, which is what `in_flight` states.
        auto_embed=False,
        sequential=True,
        settled=_born(frontier),
    )
    enqueue_frontier(tasks, child, frontier)
    return child.id


def _row_ceiling(evidence: dict, params: dict) -> tuple[int, int]:
    """One node per data row, up to the ceiling that FAILS the task. 8.4 and 6.6.

    ``max_rows`` is not a truncation and the low bound says so: a sheet with more rows than
    the ceiling writes nothing at all, because it raises. So the interval is ``(0, rows)``
    for a sheet inside the ceiling and ``(0, 0)`` for one past it, and there is no value of
    ``rows`` for which this atom writes a partial sheet.

    **The header's ROW is read, where this used to subtract 1.** The header is looked for in
    rows 1 to 8, so a sheet whose header is at row 4 has three banner rows above it and
    ``rows - 1`` would over-count by three. ``null`` means no pass found a header, and then
    there are no records at all — the `table` node such a sheet may still get is a different
    usetype and is not what this interval counts (``Fanout.counts``).
    """
    rows = int(evidence["sheet.measurements.rows"])
    if rows > int(params["max_rows"]):
        return (0, 0)
    header_row = evidence["sheet.measurements.header_row_number"]
    if header_row is None:
        return (0, 0)
    # Neither the header row nor the banner above it becomes a node of its own: the header
    # names the keys the records carry, and the banner is a label for the table.
    return (0, max(rows - int(header_row), 0))


# `sheet.measurements@self` is the derived edge, and it is the one this atom's docstring
# argues for in prose: the header verdict and the column labels come from `profile:sheet`
# rather than being measured a second time. The `extract:sheet` ROW says the same thing by
# hand, in its `after`, which is what Part 2.3 claims is redundant.
@register_task_handler(
    TASK_EXTRACT_SHEET,
    consumes=(
        f"{EV_SHEET}@self",
        f"{EV_SHEET_MEASUREMENTS}@self",
        f"{EV_BLOB}@{LOCUS_ANCESTOR}",
    ),
    # `@subtree` AND NOT ONLY `@children`, for the asymmetry the two structure rungs already
    # carry: a row too long to embed becomes a container, its cells are grandchildren of the
    # sheet, and a cell too long to embed puts chunks one deeper again. The write mode stays
    # `children` — 9.3's escalation to `subtree` is for a re-run that DELETES an unmatched
    # node, and this run creates a region from nothing.
    produces=(
        f"{EV_SHEET_SHAPE}@self",
        f"{EV_TEXT}@subtree",
        f"{EV_RECORD}@children",
        f"{EV_CELL}@subtree",
        f"{EV_TABLE}@children",
    ),
    write_mode=WRITE_CHILDREN,
    cost_class=COST_CPU,
    fanout=Fanout(
        bound=_row_ceiling,
        reads=("sheet.measurements.rows", "sheet.measurements.header_row_number"),
        basis="one node per data row",
        # THE RECORDS, NOT THE LEAVES, and the two stopped being the same thing when a row
        # became splittable. `_RUNG_FANOUT` counts chunks for the opposite reason — a rung's
        # section count varies with the document's shape while its ceiling is a function of
        # length — and here it is the RECORD count that is exactly `rows - header_row`
        # whatever the columns hold. What varies is how many nodes each record needs, which
        # is a fact about the cells rather than about the row count this interval is over.
        #
        # NOT THE `table` NODE EITHER, and that is a second reason this names one usetype.
        # This rule writes at most one of those, it is a different kind of node, and an
        # interval over "records" that silently included it would be uncheckable against a
        # run — which is the whole reason `counts` exists.
        counts=USETYPE_RECORD,
    ),
    child_key=ChildKey(KEY_POSITION),
)
def run_extract_sheet(session: Session, task: TaskQueue) -> TaskOutcome:
    """One node per row, and one node for the whole sheet. 8.4's ``records`` and ``small_table``.

    **It runs TWO of 8.4's four shapes and it emits both where both match.** 8.4 says the
    first match wins; that sentence had never been executed, because only one shape was ever
    built. Both are now, and the ordering it asks for is what the measurement argues
    against: at the document window both shapes match 29.4% of git-corpora sheets and 12.5%
    of FUSE sheets, and those contested sheets are SMALL — a median of 7 and 13 data rows,
    39.9% of the git set at five rows or fewer — which is exactly the population 8.4's own
    argument for ``small_table`` is about ("a small table is often exactly the retrieval unit
    we want, and splitting it destroys it"). A first-match ordering either way throws the
    shape that argument is for away on the sheets it is strongest on. See
    :data:`jmfts_core.sheet_records.BOTH_SHAPES_BASIS`.

    Neither branch reads one of 8.8's unset thresholds. ``records`` branches on 8.3's
    ``header_row``, a measured boolean; ``small_table`` branches on a token count against a
    window, which is the model's own limit. ``matrix`` and ``unstructured`` are still
    unbuilt, and they are the two that need the calibration corpus.

    **It reads the header, its ROW and the rendered table from the profile, not from the
    sheet.** ``profile:sheet`` decided which row was the header — rows 1 to 8 are scanned, so
    it is not necessarily row 1 — recorded a label per column, and kept the markdown it
    rendered when that markdown fits. Re-deriving any of the three here could disagree, and
    then the profile and the nodes would describe different sheets. That is why this task is
    ordered after that one, and why a node with no ``sheet.measurements`` raises rather than
    measuring for itself.

    **A row whose prose does not fit the embedding window becomes a CONTAINER over its
    columns**, and its cells carry the text. That is the rule the rest of the tree follows —
    a ``section`` holds no prose and its chunks do — and ``summarize`` gives the container a
    document vector over the concatenation at the settling boundary, which is the same text
    the single node would have held. Before this, the row became one node whose ``embed``
    raised rather than embedding a truncated prefix (``KNOWN-DEFECTS.md`` D1), and the
    refusal was correct while the outcome was not: 66 of 4,601 record nodes on the reference
    corpus have no vector, no ancestor that settled, and a permanently failed task each.

    Splitting by COLUMN is what makes the pieces legible. The keys are measured, so a cell
    node says which field it is; a blind chunk of the row's prose would put a boundary in
    the middle of ``Discussion:`` and produce a node that names no column at all. A cell
    that is itself over the window gets pieces, which is the same rule once more and not a
    corner case — 41 of those 66 rows hold a single column that does not fit either.
    """
    node, sheet, name = _scoped_sheet(
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

    # `params[...]` and not `params.get(..., DEFAULT)`. The `sheet_records` group is
    # complete for every format (`ingest_options._check_option_tables` holds that at
    # import), so a queue row planned by `plan_frontier` always carries both values —
    # validated, and with `_positive_int` having already refused the `bool` that `int(True)`
    # would have turned into one record node for a whole sheet. A default here would be a
    # second source of the number, and the one that ran would depend on which path enqueued
    # the task.
    params = task.params or {}
    max_rows = int(params["max_rows"])
    with_notes = bool(params["with_cell_notes"])
    # Preserved, not rebuilt: `profile:sheet` put 8.4's branch INPUTS here and they are what
    # a calibration sweep replays against. This task adds a verdict; it does not get to
    # forget the evidence.
    decision = dict(sheet.get("shape_decision") or {})

    header_row = measurements.get("header_row_number")
    records_match = bool(measurements.get("header_row"))
    if records_match and not header_row:
        raise ValueError(
            f"sheet node {node.id} carries `header_row` true with no `header_row_number`; "
            f"{TASK_PROFILE_SHEET} writes the two together and the row number is what says "
            "which rows are the banner above it and which are instances below it"
        )
    table_markdown = measurements.get("rendered_markdown")
    table_match = bool(measurements.get("rendered_fits_doc_window")) and bool(table_markdown)

    repo = DocumentRepository(session)
    tasks = TaskQueueRepository(session)
    service = get_embedding_service()
    # FOUR FRONTIERS AND NOT ONE, because a fan-out plans per (producer, usetype) pair and
    # this rule writes four kinds of node. `enqueue_frontier` refuses a child stamped with
    # a pair its frontier was not planned for, which is what makes the four separate
    # rather than one reused — see `Frontier` for why that check exists.
    frontier = plan_frontier(session, node, produced_by=TASK_EXTRACT_SHEET, usetype=USETYPE_RECORD)
    cell_frontier = plan_frontier(
        session, node, produced_by=TASK_EXTRACT_SHEET, usetype=USETYPE_CELL
    )
    piece_frontier = plan_frontier(
        session, node, produced_by=TASK_EXTRACT_SHEET, usetype=USETYPE_CHUNK
    )
    child_ids: list[int] = []
    shapes: list[str] = []
    table_id = None

    # 8.4 LISTS `small_table` FIRST AND IT IS WRITTEN FIRST, so a sheet that gets both has
    # the whole table at position 0 and its rows after it. That is an ordering of siblings
    # and not a precedence between the shapes: both are written, and `BOTH_SHAPES_BASIS` is
    # why.
    if table_match:
        table_id = _write_table(
            session,
            repo,
            tasks,
            node,
            sheet_name=name,
            markdown=table_markdown,
            measurements=measurements,
            fits_token_window=bool(measurements.get("rendered_fits_token_window")),
        )
        child_ids.append(table_id)
        shapes.append(SHAPE_SMALL_TABLE)

    if not records_match:
        decision.update(
            {
                "decided": bool(shapes),
                "shape": list(shapes),
                "basis": "header_row, rendered_fits_doc_window",
                "reason": TABLE_SHAPE_BASIS if shapes else NO_HEADER_REASON,
                "no_records": NO_HEADER_REASON,
            }
        )
        sheet["shape"] = list(shapes)
        sheet["shape_margin"] = None
        sheet["shape_decision"] = decision
        # 8.7: `inferred` for every shape except `unstructured`, and no rung at all where no
        # shape was chosen — `NO_RUNG_REASON` says why a rung is a function of the shape.
        if shapes:
            sheet["rung"] = RUNG_INFERRED
        sheet["record_count"] = 0
        EvidenceRepository(session).write(node.id, "sheet", sheet)
        session.flush()
        return TaskOutcome(
            rung=RUNG_INFERRED if shapes else None,
            detail={
                "sheet": name,
                "shape": list(shapes),
                "records": 0,
                "no_records": NO_HEADER_REASON,
                "table_node_id": table_id,
                **({} if table_match else {"no_table": NO_TABLE_REASON}),
            },
            produced={"node_count": len(child_ids), "child_ids": list(child_ids)},
        )

    data = _sheet_blob(session, node, task_type=TASK_EXTRACT_SHEET, name=name)
    rows = read_rows(data, name, max_rows=max_rows, with_notes=with_notes)
    records = build_records(
        rows,
        header=header_labels(sheet.get("columns") or []),
        header_row=int(header_row),
    )
    shapes.append(SHAPE_RECORDS)

    over_window = 0
    cells_written = 0
    pieces_written = 0
    with_formula = 0
    text_forced = 0
    for record in records:
        for cell in record.cells.values():
            with_formula += 1 if "formula" in cell else 0
            text_forced += 1 if cell.get("text_forced") else 0
        # 8.4 plus the window, decided in `sheet_records` and not here: this task knows how
        # to write nodes and the shape of what to write is a pure function of the row and
        # the two callables below.
        plan = plan_record(record, fits=service.fits_token_window, chunk=chunk_prose)
        if plan.content is None:
            over_window += 1
        child = repo.create(
            # The row NUMBER, not a position in the result. A person going back to the
            # workbook to check needs the number Excel shows them down the left edge.
            title=f"{name} row {record.row_index}",
            content=plan.content,
            parent_id=node.id,
            usetype=USETYPE_RECORD,
            produced_by=TASK_EXTRACT_SHEET,
            evidence={
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
            settled=_born(frontier if plan.content is not None else cell_frontier),
        )
        if plan.content is not None:
            enqueue_frontier(tasks, child, frontier)
        else:
            # NO `embed` ON THE CONTAINER, and `run_embed` is why: it refuses a node with no
            # content, correctly. The vector arrives from `summarize` at the settling
            # boundary, over the concatenation of the cells below — which is
            # `store_effective_content`'s document-vector-only path, the same one every
            # `section` in the corpus takes.
            written = _write_cells(
                repo,
                tasks,
                child,
                plan,
                record=record,
                sheet_name=name,
                cell_frontier=cell_frontier,
                piece_frontier=piece_frontier,
            )
            cells_written += written[0]
            pieces_written += written[1]
        child_ids.append(child.id)

    records_written = len(child_ids) - (1 if table_id is not None else 0)
    decision.update(
        {
            "decided": True,
            "shape": list(shapes),
            "basis": "header_row, rendered_fits_doc_window",
            "reason": (BOTH_SHAPES_BASIS if len(shapes) > 1 else SHAPE_BASIS),
            "records_reason": SHAPE_BASIS,
            "table_reason": TABLE_SHAPE_BASIS if table_match else NO_TABLE_REASON,
        }
    )
    sheet["shape"] = list(shapes)
    # 8.7's second key. `null` and not "clear": a margin says how close the decision was to
    # a boundary, and neither input here has one — a boolean has no boundary to be close to,
    # and a token count against the model's own window is a hard edge rather than a
    # threshold somebody chose. The reasons are in `shape_decision`, where a reader finds
    # them beside the verdict.
    sheet["shape_margin"] = None
    sheet["rung"] = RUNG_INFERRED
    sheet["shape_decision"] = decision
    sheet["record_count"] = records_written
    EvidenceRepository(session).write(node.id, "sheet", sheet)
    session.flush()

    return TaskOutcome(
        # 8.7: `inferred` for every shape except `unstructured`. The sheet node itself was
        # produced at the declared rung; the shape below it was not.
        rung=RUNG_INFERRED,
        detail={
            "sheet": name,
            "shape": list(shapes),
            "header_row": int(header_row),
            "table_node_id": table_id,
            **({} if table_match else {"no_table": NO_TABLE_REASON}),
            "records": records_written,
            "rows_read": len(rows.rows),
            "cols": rows.cols,
            "max_rows": max_rows,
            # What the standard-library pass found that openpyxl's read-only mode cannot
            # report. Zero with `cell_notes_read` false means nothing looked.
            "cell_notes_read": rows.notes_read,
            "cells_with_formula": with_formula,
            "cells_text_forced": text_forced,
            # A condition, and now a resolved one: these are the rows that became
            # containers. The number stayed after the split because it is what an operator
            # reads to tell a sheet of values from a sheet of documents, and the two counts
            # below say what it cost — `records_over_token_window` rows produced
            # `cell_nodes` cells, of which the ones that did not fit either produced
            # `cell_piece_nodes` chunks.
            "records_over_token_window": over_window,
            "cell_nodes": cells_written,
            "cell_piece_nodes": pieces_written,
        },
        # `child_ids` is every CHILD of the sheet node — the record nodes, and the one
        # `table` node where the sheet got both shapes. `Fanout.counts` names `record` and
        # the ceiling is an interval over those alone, which is why `records` above is a
        # separate number: a plan's ceiling has to be checkable against the thing it
        # bounded. The cells and their pieces are below these and are counted in their own
        # two fields.
        produced={"node_count": len(child_ids), "child_ids": child_ids},
    )
