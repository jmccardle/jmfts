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

8.6's ``propose:links`` lives here too, and it is the first task in this module scoped to the
WORKBOOK rather than to one sheet: it reads every sheet's profile and proposes the columns
whose value sets nest. The search itself is :mod:`jmfts_core.column_links`, which holds no
session; what is here is the half that needs one — gathering the profiles, verifying the
survivors in SQL, and writing the proposals down.

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

from typing import Optional, Sequence

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from jmfts_client.contracts.anchor import ANCHOR_UNRESOLVED_ROW, CellsAnchor
from jmfts_core.atoms import (
    COST_CPU,
    EV_BLOB,
    EV_CELL,
    EV_COLUMN_LINKS,
    EV_MATCHED,
    EV_PROFILE,
    EV_SHEET,
    EV_SHEET_MEASUREMENTS,
    EV_RECORD,
    EV_SHEET_SHAPE,
    EV_SOURCE_ANCHOR,
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
from jmfts_core.column_links import (
    DEFAULT_CONTAINMENT_THRESHOLD,
    DEFAULT_LSH_PARTITIONS,
    DEFAULT_MAX_CANDIDATES,
    DEFAULT_MIN_DISTINCT_VALUES,
    METHOD_LSH_ENSEMBLE,
    SKETCH_RESOLUTION,
    THRESHOLD_UNCALIBRATED,
    VERIFIED_NONE,
    VERIFIED_SQL_EXACT,
    Containment,
    Verification,
    candidate_record,
    census,
    collapse_mutual,
    columns_of,
    find_containments,
    passes_threshold,
    recipe_of,
    sort_candidates,
    unverifiable_reason,
)
from jmfts_core.config import get_settings
from jmfts_core.embedding import get_embedding_service
from jmfts_core.ingest_options import STRUCTURE_CHUNK_PARAMS
from jmfts_core.ingest_tasks import (
    DEFERRED_REASON,
    TASK_EXTRACT_SHEET,
    TASK_PROFILE_SHEET,
    TASK_PROPOSE_LINKS,
    TASK_REVIEW_LINK,
    TASK_STRUCTURE_SHEETS,
    Frontier,
    TaskOutcome,
    enqueue_frontier,
    plan_frontier,
    register_task_handler,
    without_token_vectors,
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
from jmfts_core.models.task_queue import TaskQueue, WRITE_CHILDREN, WRITE_SELF
from jmfts_core.office.cells import CellRange, json_value, read_rows, used_range
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
from jmfts_core.sketch import require_datasketch
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

#: 8.6's workbook-level task, and it is not a row of ``TASK_ROWS`` — see
#: :data:`~jmfts_core.ingest_tasks.TASK_PROPOSE_LINKS` for the dependency a row cannot
#: express. ``self``, because it writes the workbook node's own ``column_links`` evidence and
#: creates no node. Enqueuing is not a write to the tree (see ``PROBE_WRITE_MODE``), and
#: today it enqueues nothing at all — see
#: :data:`~jmfts_core.ingest_tasks.DEFERRED_REASON` for ``review:link``.
PROPOSE_LINKS_WRITE_MODE = WRITE_SELF

#: Every knob 8.6's containment search reads, in the task row rather than in configuration.
#: Two reasons, both the ones the ``sheet_profile`` option group gives for ``sketch_columns``:
#: a re-ingest under a different threshold is a different request and its
#: ``param_fingerprint`` has to say so (6.1), and a workbook profiled last month has to record
#: which numbers it was searched under.
#:
#: NOT AN ``ingest_options`` GROUP, which is where every row's parameters come from since
#: Phase 3, and the reason is the one above: a group is resolved per format when ``probe``
#: finishes and handed to a ROW, and this task has no row. Making it one would mean a
#: ``params_key`` on a row that cannot state its own dependency, which buys a settable knob
#: at the price of a task that runs before the profiles exist. The knobs are settable per
#: task here instead; :func:`_links_params` is what validates them, and it validates rather
#: than clamps for the reason ``ingest_options`` does.
#:
#: **None of them is calibrated.** See :data:`~jmfts_core.column_links.THRESHOLD_UNCALIBRATED`,
#: which is written into every workbook beside the numbers themselves.
PROPOSE_LINKS_PARAMS: dict = {
    "containment_threshold": DEFAULT_CONTAINMENT_THRESHOLD,
    "lsh_partitions": DEFAULT_LSH_PARTITIONS,
    "min_distinct_values": DEFAULT_MIN_DISTINCT_VALUES,
    "max_candidates": DEFAULT_MAX_CANDIDATES,
}

#: Why a workbook whose sheets were profiled without sketches gets no ``propose:links``.
#: Scheduling reads the parameter the profile task was planned WITH, so the two cannot
#: disagree: the containment search compares sketches, and a profile that wrote none leaves
#: it nothing to compare. Recorded rather than discovered at claim time, because a task
#: enqueued to fail is a failed workbook and a task not enqueued for a stated reason is a
#: line in the attempt log.
NO_SKETCHES_REASON = (
    "INGEST_SPEC.md 8.6's containment search compares the per-column sketches "
    "`profile:sheet` writes, and this workbook's profiles were planned with "
    "`sketch_columns` false, so there is nothing for it to compare. Re-ingest with "
    "sketching on (see jmfts_core/sketch.py) to get the proposals"
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
    profile_task_ids: list[int] = []
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
            queued = enqueue_frontier(tasks, node, frontier)
            # `enqueue_batch` returns one id per spec, in the order it enqueued them, so the
            # zip is positional and not a lookup by name. Which of them is the profile is
            # read off the spec rather than off a position, because the batch's membership is
            # `plan_frontier`'s answer and it is allowed to change.
            profile_task_ids.extend(
                task_id
                for spec, task_id in zip(frontier.specs, queued)
                if spec.task_type == TASK_PROFILE_SHEET
            )
        child_ids.append(node.id)

    # 8.6's workbook-level task, and this is the only moment its dependencies are known: one
    # `profile:sheet` per sheet, on nodes that did not exist when this handler started. It
    # goes on the FILE node, so the workbook does not settle until the proposals are made.
    links_task_id = _enqueue_propose_links(tasks, doc.id, frontier, profile_task_ids)

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
        # And the one that is not per sheet. Its id rather than a boolean, because what a
        # reader wants next is the row it waits on (8.6).
        "propose_links_task_id": links_task_id,
        # 3.4: a rung that ran and stopped where the spec says to stop must not look like
        # one that never ran.
        #
        # A copy, not `frontier.deferred` itself: the frontier is the SHEET nodes' batch and
        # `propose:links` is not in it, so the workbook's own un-queued task is added here
        # and never written back into a plan that is about a different scope.
        "deferred": (
            frontier.deferred
            if links_task_id is not None or not profile_task_ids
            else {**frontier.deferred, TASK_PROPOSE_LINKS: NO_SKETCHES_REASON}
        ),
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


def _enqueue_propose_links(
    tasks: TaskQueueRepository,
    file_node_id: int,
    frontier: Frontier,
    profile_task_ids: Sequence[int],
) -> Optional[int]:
    """One ``propose:links`` for the workbook, or ``None`` with the reason left to the caller.

    **The condition is read off the planned spec, not off configuration.**
    ``sketch_columns`` is a ``profile:sheet`` parameter — a key of the ``sheet_profile``
    option group, resolved per ingest since Phase 3 — so what decides whether a containment
    search has anything to compare is what those tasks were queued WITH. Reading it from
    ``get_settings()`` or from the group's defaults would let the two disagree the moment a
    caller sets it, which is exactly what Phase 3 made possible.
    """
    if not profile_task_ids:
        return None
    sketching = any(
        spec.task_type == TASK_PROFILE_SHEET and spec.params.get("sketch_columns")
        for spec in frontier.specs
    )
    if not sketching:
        return None
    return tasks.enqueue(
        TASK_PROPOSE_LINKS,
        file_node_id,
        PROPOSE_LINKS_WRITE_MODE,
        params=dict(PROPOSE_LINKS_PARAMS),
        # Every profile, not the last one. 8.6: "This is the first workbook-level task after
        # the per-sheet fan-out, which is why it needs all sheets profiled first."
        dependencies=list(profile_task_ids),
    ).id


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
    produces=(
        f"{EV_SHEET_MEASUREMENTS}@self",
        f"{EV_PROFILE}@children",
        f"{EV_TEXT}@children",
        f"{EV_SOURCE_ANCHOR}@children",
    ),
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
        evidence={
            "profile": dict(profile_record, sheet_name=name),
            # The profile describes the whole sheet, so it is addressed at what it measured.
            **_cells_anchor(name, used_range(measurement.rows, measurement.cols)),
        },
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

    NOT the library's bare defaults, which is what the deleted
    ``EmbeddingService.chunk_to_fit`` passed: ``ChunkStrategy.sentence``, unpacked, when
    this was measured. ``sentence`` is the strategy
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
            max_words=int(STRUCTURE_CHUNK_PARAMS["max_words"]),
            min_chunk_length=int(STRUCTURE_CHUNK_PARAMS["min_chunk_length"]),
            max_chars=get_settings().chunk_max_chars,
            fits=service.fits_token_window,
        )
    ]


#: The ``source_anchor.unresolved`` code for a node made from a sheet that holds nothing.
UNRESOLVED_EMPTY_SHEET = "empty_sheet"


def _cells_anchor(sheet_name: str, bounds: Optional[CellRange]) -> dict:
    """The ``source_anchor`` row for a node made from a rectangle of one worksheet.

    ``OFFICE_SPEC.md`` Part 5's ``{"kind": "cells", "sheet": ..., "ref": ...}``, built
    through the contract model that READS it (``parse_anchor``), so the writer cannot spell
    a row the reader refuses. Returned as an evidence fragment to spread into ``evidence=``.

    With no rectangle — an empty sheet has no used range, and an anchor at ``A1:A1`` over
    nothing would be an address invented here (``used_range``) — it is the UNRESOLVED row
    instead, which is present exactly when the anchor is not (``evidence.py``). "Nothing
    placed this node" and "this node has no region to place" are different answers.
    """
    if bounds is None:
        return {
            ANCHOR_UNRESOLVED_ROW: {
                "code": UNRESOLVED_EMPTY_SHEET,
                "reason": f"sheet {sheet_name!r} was measured to hold no cells, so it has "
                "no used range to address",
            }
        }
    return {EV_SOURCE_ANCHOR: CellsAnchor(sheet=sheet_name, ref=bounds.ref).model_dump()}


def _row_range(row_index: int, measurements: dict) -> Optional[CellRange]:
    """One worksheet row, across the sheet's measured width. What a record node came from.

    The width is ``profile:sheet``'s, the same one the table and the profile are anchored
    at, so a record's region is a slice of its sheet's region and never wider than it. Column
    A and not the record's first filled column: the row is the unit a person goes back to,
    and Excel's row header selects all of it.
    """
    cols = int(measurements.get("cols") or 0)
    if cols < 1:
        return None
    return CellRange(min_row=row_index, min_col=1, max_row=row_index, max_col=cols)


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
        frontier = without_token_vectors(frontier)
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
            },
            # The markdown is the rendering of the measured used range, so that is the
            # region this node is the whole of.
            **_cells_anchor(
                sheet_name,
                used_range(int(measurements.get("rows") or 0), int(measurements.get("cols") or 0)),
            ),
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
        f"{EV_SOURCE_ANCHOR}@children",
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
                **_cells_anchor(name, _row_range(record.row_index, measurements)),
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


# ---------------------------------------------------------------------------
# propose:links — INGEST_SPEC.md 8.6, SPRINT_0_3_0.md 6.4
# ---------------------------------------------------------------------------

#: Where the proposals land on the workbook node: an evidence row of their own, beside
#: ``structure`` and ``matched`` rather than inside either. It is not a rung — no boundary is
#: drawn and no node is written — and it is not something probe measured.
LINKS_KEY = EV_COLUMN_LINKS

#: Said once for the whole workbook rather than on each of Part 7.6's payloads, which carry
#: the number itself. A rebuild estimate under an undecided representation is worth having
#: and is worth being honest about: 8.4 has four shapes, ``extract:sheet`` now runs two of
#: them, and only ``records`` writes a node per row.
REBUILD_ESTIMATE_ASSUMES = (
    "`estimated_rebuild_nodes` on each review payload is the contained column's row count, "
    "which assumes INGEST_SPEC.md 8.4's `records` shape — one node per row. `extract:sheet` "
    "runs that shape and `small_table` beside it, and which of the two a given sheet got is "
    "on that sheet's node (`sheet.shape`); the other two shapes are unbuilt, because 8.8 "
    "leaves the thresholds they read unset"
)

#: Exact verification, in the database, over the value sets 8.5 stored. One statement for
#: every survivor rather than one per pair, and the value lists never leave Postgres — a
#: pair of thousand-element arrays pulled into Python per candidate is the cost 6.4's "in
#: SQL" is avoiding, not a detail of where the comparison happens to be written.
#:
#: **It reads ``document_evidence`` and not ``documents``.** Phase 2b moved every named fact
#: out of ``Document.structured_content`` into a row per name, so the sheet block is the
#: ``sheet`` row's ``value`` and the join is on ``(document_id, name)``. The row name is a
#: bind parameter rather than a literal, so the statement and every Python reader of the
#: same block name it with one constant (:data:`~jmfts_core.atoms.EV_SHEET`).
#:
#: The columns are addressed by their ARRAY POSITION in the stored profile
#: (``ColumnRef.position``), which is what a JSONB path can follow; the worksheet's own
#: 1-based column index is a different number and is carried separately.
_VERIFY_SQL = text("""
    WITH pair(ord, contained_node, contained_pos, container_node, container_pos) AS (
        SELECT * FROM unnest(
            CAST(:ords AS int[]),
            CAST(:contained_nodes AS int[]),
            CAST(:contained_positions AS int[]),
            CAST(:container_nodes AS int[]),
            CAST(:container_positions AS int[])
        )
    ),
    contained_values AS (
        SELECT p.ord, v.value AS val
        FROM pair p
        JOIN document_evidence e
          ON e.document_id = p.contained_node AND e.name = :sheet_row
        CROSS JOIN LATERAL jsonb_array_elements_text(
            e.value -> 'columns' -> p.contained_pos -> 'values'
        ) AS v(value)
    ),
    container_values AS (
        SELECT p.ord, v.value AS val
        FROM pair p
        JOIN document_evidence e
          ON e.document_id = p.container_node AND e.name = :sheet_row
        CROSS JOIN LATERAL jsonb_array_elements_text(
            e.value -> 'columns' -> p.container_pos -> 'values'
        ) AS v(value)
    )
    SELECT p.ord,
           (SELECT count(*) FROM contained_values c WHERE c.ord = p.ord) AS contained_size,
           (SELECT count(*) FROM container_values c WHERE c.ord = p.ord) AS container_size,
           (SELECT count(*) FROM contained_values c
             WHERE c.ord = p.ord
               AND EXISTS (SELECT 1 FROM container_values x
                            WHERE x.ord = p.ord AND x.val = c.val)) AS overlap
    FROM pair p
    """)


# `sheet.measurements@children` and not `@self`: this task is scoped to the FILE node and
# every measurement it reads is one level down, on the sheet nodes `structure:sheets` wrote.
# That locus derives no within-node edge (2.3) and is not meant to — what orders this task
# after the profiles is the dependency array `_enqueue_propose_links` builds, which names the
# rows rather than the types and is therefore a stronger statement than an edge.
#
# No `fanout` and no `child_key`, because it writes no node at all: 8.6 proposes, and 7.3
# gives a person the decision. `Atom.__post_init__` requires both together or neither.
@register_task_handler(
    TASK_PROPOSE_LINKS,
    consumes=(f"{EV_SHEET_MEASUREMENTS}@children",),
    produces=(f"{EV_COLUMN_LINKS}@self",),
    write_mode=PROPOSE_LINKS_WRITE_MODE,
    cost_class=COST_CPU,
)
def run_propose_links(session: Session, task: TaskQueue) -> TaskOutcome:
    """Propose the columns of this workbook whose value sets nest. 8.6, by 6.4's mechanism.

    **The LSH pass is recall and the SQL pass is the answer.** ``MinHashLSHEnsemble`` over
    the per-column sketches finds pairs whose containment is plausibly at or above the
    threshold — a MinHash carries real error and the ensemble's own guarantee is
    probabilistic — and what survives it is counted exactly against the stored value sets.
    Where 8.5 kept no values to count, the candidate says so rather than presenting an
    estimate as a count; see :func:`~jmfts_core.column_links.unverifiable_reason`, which is
    the one part of 6.4 this pass cannot finish and why.

    **It fails by name on an install that cannot sketch.** ``require_datasketch()`` runs
    first, before the sheets are read and before anything decides there is nothing to do: an
    appliance built without the ``sketch`` extra is a supported deployment, and the failure
    it deserves says which deployment it is. Silently proposing nothing would be
    indistinguishable from a workbook whose columns genuinely share no values.

    **It proposes and stops.** 8.6 gives the meaning to a reviewer, so every candidate
    carries Part 7.6's payload and nothing here writes a relationship. The payloads are
    written to the node rather than queued as review tasks — see
    :data:`~jmfts_core.ingest_tasks.DEFERRED_REASON` for ``review:link``, which is the one
    thing 8.6 names that this pass does not do and the reason it does not.
    """
    node = session.get(Document, task.scope_document_id)
    if node is None:
        raise ValueError(
            f"{TASK_PROPOSE_LINKS} is scoped to document {task.scope_document_id}, "
            "which does not exist"
        )
    # First, and deliberately ahead of every cheaper check. See the docstring.
    require_datasketch()
    options = _links_params(task)

    sheet_nodes = list(
        session.execute(
            select(Document)
            .where(Document.parent_id == node.id, Document.usetype == USETYPE_SHEET)
            .order_by(Document.position.asc().nullslast(), Document.id.asc())
        )
        .scalars()
        .all()
    )
    if not sheet_nodes:
        raise ValueError(
            f"{TASK_PROPOSE_LINKS} is scoped to document {node.id}, which has no "
            f"{USETYPE_SHEET!r} children; this task is enqueued by {TASK_STRUCTURE_SHEETS} "
            "in the same transaction that creates them, so an empty list means the tree was "
            "rearranged underneath a queued task"
        )

    blocks: list[dict] = []
    unprofiled: list[str] = []
    columns: list = []
    for sheet_node in sheet_nodes:
        block = evidence_value(session, sheet_node.id, EV_SHEET) or {}
        # `columns` is written by `sheet_evidence_block` for every sheet, empty ones
        # included, so its ABSENCE means this sheet was never profiled — not that it holds
        # no columns. The dependency array should make that impossible; searching the rest
        # anyway would answer a question about part of the workbook while looking like an
        # answer about the workbook.
        if "columns" not in block:
            unprofiled.append(f"{sheet_node.id}:{block.get('name')!r}")
            continue
        blocks.append(block)
        columns.extend(columns_of(sheet_node.id, block))
    if unprofiled:
        raise ValueError(
            f"{TASK_PROPOSE_LINKS} on document {node.id} found sheets with no profile "
            f"({', '.join(unprofiled)}); it is enqueued with a dependency on every "
            f"{TASK_PROFILE_SHEET} of this workbook, so either a sheet node was added after "
            "the dependency array was built or the profiles were rewritten"
        )

    measured_columns = sum(len(block.get("columns") or ()) for block in blocks)
    if measured_columns and not columns:
        raise ValueError(
            f"{TASK_PROPOSE_LINKS} on document {node.id} found {measured_columns} "
            "measured columns and not one sketch. INGEST_SPEC.md 8.6's containment search "
            "compares sketches, so there is nothing here to compare: these sheets were "
            "profiled with `sketch_columns` false. Re-run profile:sheet with sketching on"
        )

    recipe = recipe_of(columns) if columns else None
    eligible = [column for column in columns if column.sketch_size >= options["min_distinct"]]
    counted = census(blocks, columns, len(eligible))

    # One column cannot be contained in another one, and `find_containments` would build an
    # ensemble to discover it. Two is the smallest workbook this question means anything for.
    containments: list[Containment] = []
    if len(eligible) > 1:
        containments = find_containments(
            eligible,
            threshold=options["threshold"],
            partitions=options["partitions"],
        )
    # SPRINT_0_3_0.md 6.6's rule, applied to this task's own fan-out: a named limit that
    # fails, not a silent truncation. Eight hundred columns whose values all nest is three
    # hundred thousand proposals, which is a JSONB block nobody reads and a review queue
    # nobody works. The message carries the number, so raising the limit is an informed
    # decision rather than a guess.
    #
    # It bites on what the LSH pass PROPOSED rather than on what survives verification, and
    # deliberately: the verification is one statement over every pair, and the point of a
    # limit is to stop before the expensive thing rather than after it.
    if len(containments) > options["max_candidates"]:
        raise ValueError(
            f"{TASK_PROPOSE_LINKS} on document {node.id} proposed {len(containments):,} "
            "candidate pairs, over the `max_candidates` limit of "
            f"{options['max_candidates']:,}. Nothing was written and nothing was verified: "
            "truncating would hide which proposals were dropped. Re-run with a higher "
            "`max_candidates`, or a higher `containment_threshold` / `min_distinct_values` "
            "to propose fewer (all three are uncalibrated — INGEST_SPEC.md 8.8)"
        )

    # Pass 2: measure every proposal, exactly where the values are stored. Pass 3: collapse
    # the pairs that survive in both directions. Both AFTER the counting, because the
    # ensemble returns both directions for pairs that nest only one way.
    verifications = _verify_in_sql(session, containments)
    confirmed = [
        containment
        for containment in containments
        if passes_threshold(
            containment, verifications[_pair_key(containment)], options["threshold"]
        )
    ]
    candidates = sort_candidates(
        [
            candidate_record(containment, verifications[_pair_key(containment)])
            for containment in collapse_mutual(confirmed)
        ]
    )

    record = {
        "spec": "INGEST_SPEC.md 8.6; SPRINT_0_3_0.md 6.4",
        "method": METHOD_LSH_ENSEMBLE,
        "sheets": len(blocks),
        **counted,
        # The knobs it ran under, and the recipe the sketches were built under. The first is
        # what a threshold sweep varies; the second is what says whether two workbooks'
        # sketches may be compared at all (jmfts_core/sketch.py).
        "parameters": {
            "containment_threshold": options["threshold"],
            "lsh_partitions": options["partitions"],
            "min_distinct_values": options["min_distinct"],
            "max_candidates": options["max_candidates"],
        },
        "sketch_recipe": recipe,
        # How many the ensemble put forward and how many the measurement kept. A reader who
        # sees two candidates from four hundred proposals is looking at a threshold that is
        # doing its job; one who sees two from two is looking at a recall pass that found
        # nothing to reject, which is a different fact about the workbook.
        "proposed_pairs": len(containments),
        "confirmed_pairs": len(confirmed),
        "thresholds": THRESHOLD_UNCALIBRATED,
        "resolution": SKETCH_RESOLUTION,
        "rebuild_estimate": REBUILD_ESTIMATE_ASSUMES,
        "review": {"task": TASK_REVIEW_LINK, "deferred": DEFERRED_REASON[TASK_REVIEW_LINK]},
        "candidates": candidates,
    }
    EvidenceRepository(session).write(node.id, LINKS_KEY, record)
    session.flush()

    verified = sum(
        1 for candidate in candidates if candidate["verification"]["method"] == VERIFIED_SQL_EXACT
    )
    return TaskOutcome(
        # No rung, for the reason `profile:sheet` claims none: a rung is a statement about
        # the evidence a BOUNDARY was drawn from, and this task draws none.
        rung=None,
        detail={
            **counted,
            "sheets": len(blocks),
            "proposed_pairs": len(containments),
            "confirmed_pairs": len(confirmed),
            "candidates": len(candidates),
            "verified_in_sql": verified,
            "unverified": len(candidates) - verified,
            "mutual": sum(1 for candidate in candidates if candidate["mutual"]),
            "with_unique_container": sum(1 for c in candidates if c["implications"]),
            "parameters": record["parameters"],
            "thresholds": THRESHOLD_UNCALIBRATED,
            "deferred": {TASK_REVIEW_LINK: DEFERRED_REASON[TASK_REVIEW_LINK]},
        },
        produced={"node_count": 0, "child_ids": []},
    )


def _links_params(task: TaskQueue) -> dict:
    """The four knobs, validated. Out of range is a ValueError, which classifies PERMANENT.

    Validated rather than clamped: a threshold of 1.4 is a mistake somebody made, and a task
    that quietly searched at 1.0 would report candidates against a number nobody chose.

    ``.get`` over the defaults, where :func:`run_profile_sheet` and :func:`run_extract_sheet`
    both index their params and say why. The difference is real rather than a slip: their
    groups are completed by ``ingest_options`` on every row ``plan_frontier`` writes, so a
    default in those two would be a SECOND source of a value that already has one. This task
    has no row and no group (:data:`PROPOSE_LINKS_PARAMS`), so these are the only source
    there is.
    """
    params = dict(PROPOSE_LINKS_PARAMS)
    params.update(task.params or {})
    threshold = float(params["containment_threshold"])
    if not 0.0 < threshold <= 1.0:
        raise ValueError(
            f"`containment_threshold` is {threshold}; a containment is a fraction of the "
            "contained column's values and has to be in (0, 1]"
        )
    partitions = int(params["lsh_partitions"])
    minimum = int(params["min_distinct_values"])
    maximum = int(params["max_candidates"])
    for name, value in (
        ("lsh_partitions", partitions),
        ("min_distinct_values", minimum),
        ("max_candidates", maximum),
    ):
        if value < 1:
            raise ValueError(f"`{name}` is {value}; it counts things and has to be at least 1")
    return {
        "threshold": threshold,
        "partitions": partitions,
        "min_distinct": minimum,
        "max_candidates": maximum,
    }


def _pair_key(containment: Containment) -> tuple:
    return (containment.contained.key, containment.container.key)


def _verify_in_sql(session: Session, containments: Sequence[Containment]) -> dict:
    """Count the two value sets for every survivor that has two value sets to count.

    The pairs 8.5 kept no values for are not dropped and are not estimated into the same
    field — they come back as :data:`~jmfts_core.column_links.VERIFIED_NONE` carrying the
    reason, which is what stops a reader taking a MinHash estimate for a count.
    """
    verifications: dict = {}
    countable: list[Containment] = []
    for containment in containments:
        reason = unverifiable_reason(containment)
        if reason is None:
            countable.append(containment)
        else:
            verifications[_pair_key(containment)] = Verification(
                method=VERIFIED_NONE, reason=reason
            )
    if not countable:
        return verifications

    rows = session.execute(
        _VERIFY_SQL,
        {
            "ords": list(range(len(countable))),
            "contained_nodes": [pair.contained.node_id for pair in countable],
            "contained_positions": [pair.contained.position for pair in countable],
            "container_nodes": [pair.container.node_id for pair in countable],
            "container_positions": [pair.container.position for pair in countable],
            "sheet_row": EV_SHEET,
        },
    ).all()
    measured = {int(row[0]): (int(row[1]), int(row[2]), int(row[3])) for row in rows}
    for ordinal, containment in enumerate(countable):
        if ordinal not in measured:
            raise ValueError(
                "the exact verification returned no row for candidate "
                f"{_pair_key(containment)}; one row per pair is what the query's `pair` CTE "
                "produces, so a missing one means the statement was rewritten"
            )
        contained_size, container_size, overlap = measured[ordinal]
        verifications[_pair_key(containment)] = Verification(
            method=VERIFIED_SQL_EXACT,
            contained_size=contained_size,
            container_size=container_size,
            overlap=overlap,
        )
    return verifications
