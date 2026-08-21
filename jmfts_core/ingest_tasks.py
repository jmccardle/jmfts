"""Queued ingest tasks: the handler registry, and ``probe``. ``INGEST_SPEC.md`` Part 4.

Two things live here and they are deliberately separate.

**The registry** maps a ``task_queue.task_type`` to the function that runs it. The worker
(``jmfts_core/ingest_worker.py``) knows nothing about any particular task; it claims a
row, looks the name up here, and records whatever comes back. A name with no handler is a
hard failure, not a no-op — see :func:`get_task_handler`.

**The Part 4 conditions** are :func:`plan_after_probe`. The spec's table is the whole
scheduling decision for file ingestion:

    ``probe`` is cheap, needs no model, and always runs. Everything downstream is decided
    by what it wrote into ``matched.patterns``.

So the conditions are DECLARED in one place — :data:`TASK_ROWS`, a row per task naming the
patterns it needs and the tasks it comes after — and evaluated by one loop over the
patterns probe measured. Spec 11.2 is the reason it is a table rather than a branch
ladder: an ``EXPLAIN`` for ingestion can only answer "what would this format with these
options do?" if every enqueue decision is a pure function of ``(format, patterns,
options)``, and a table makes that structural instead of a property somebody has to keep
re-establishing. The third of those inputs lives in ``jmfts_core.ingest_options``: a row
names WHICH parameters its task takes, and the task's defaults, the format's profile and
the caller's overrides resolve to what they are — so what a queue row carries is decided by
the same declaration the schedule is. The result is recorded in probe's own attempt detail — every downstream
task appears there either as enqueued, as skipped with a reason, or as not-applicable with
the pattern that was false. A task whose condition holds but whose *implementation* has
not shipped is reported as deferred and **is not enqueued**: enqueuing it would put a row
in the queue that no worker can run, and stubbing a handler that returns a plausible empty
result is exactly the anti-pattern the project's Fail Early rule forbids. When the handler
lands, registering it is the only change needed — the condition is already written.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional, Protocol

from sqlalchemy.orm import Session

from jmfts_core.contracts.attempt import AttemptRecord, param_fingerprint
from jmfts_core.ingest_options import resolve_options
from jmfts_core.models.document import Document
from jmfts_core.models.task_queue import WRITE_CHILDREN, WRITE_SELF, TaskQueue
from jmfts_core.probe import PROBERS_AVAILABLE, detect_format, probe_patterns
from jmfts_core.repositories.blob import BlobRepository
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.task_queue import TaskQueueRepository
from jmfts_core.settling import TaskSpec, enqueue_batch

#: Task names from spec Part 4, spelled once. Not an enum and not a CHECK constraint —
#: ``task_type`` stays an open string so an application can queue its own work — but the
#: tasks this spec names are referenced from several modules and must agree.
TASK_PROBE = "probe"
TASK_EXTRACT_TEXT = "extract:text"
TASK_OCR = "ocr"
TASK_STRUCTURE_DECLARED = "structure:declared"
TASK_STRUCTURE_INFERRED = "structure:inferred"
TASK_EXTRACT_TABLES = "extract:tables"
TASK_EXTRACT_IMAGES = "extract:images"

#: The two rollup tasks, which are NOT in :data:`TASK_ROWS`. Part 4 lists
#: ``structure:semantic`` under structuring, and 11.4 moves it here for the reason 5.4
#: gives: its input is the child sequence and their embeddings, which do not exist until
#: the rung above has finished and its nodes have settled. Nothing probe measures could
#: decide it. Both are planned by ``jmfts_core.rollup_tasks.IngestRollupPlanner`` at the
#: settling boundary instead.
TASK_STRUCTURE_SEMANTIC = "structure:semantic"
TASK_SUMMARIZE = "summarize"

#: The LLM half of ``summarize``, split out so the two can be routed at different pools.
#:
#: Every ``summarize`` needs the embedding model. Only the ones whose concatenated children
#: overflow the embedding window need an LLM — and which ones those are is decided by
#: ``check_fit`` INSIDE the handler, after the claim, so the queue cannot know it at enqueue
#: time. That left no honest badge for ``summarize``: routed at an LLM pool, embedding-only
#: work occupies a scarce resource; routed at an embedding pool, every embedding worker
#: needs an LLM endpoint and LLM cost stops being separately routable.
#:
#: So ``summarize`` now decides and defers. It does the fit check, and either stores the
#: concatenation itself or enqueues one of these, which carries its own badge. The shape is
#: the one ``structure:semantic`` already uses — a task whose product is more tasks.
TASK_SUMMARIZE_LLM = "summarize:llm"

#: Give one node the vectors that make it retrievable. Also not in :data:`TASK_ROWS`, and
#: for the opposite reason to the two above: nothing probe measures decides it because
#: EVERY leaf gets one. The structure rungs enqueue it per chunk as they write the chunk.
#:
#: It used to not exist. ``DocumentRepository.create`` took ``auto_embed=True`` and ran the
#: model inline, so the transformer forward pass for every chunk in a document happened
#: inside the one ``structure:declared`` task that created them — which is why that task
#: measured at 54% of ingest wall clock while claiming to be a text-splitting task
#: (:mod:`jmfts_core.task_routing`). Three things follow from splitting it out:
#:
#: * the chunks of one file embed CONCURRENTLY, across the fleet, instead of in a loop;
#: * the expensive work is one task type, so routing it is one badge on one row rather
#:   than a badge on a handler that also parses text;
#: * a worker can run everything EXCEPT this — see ``--runner-url`` in
#:   :mod:`jmfts_core.worker` — which is what lets a fixed set of GPUs be shared between
#:   embedding and summarization instead of pinned to whichever pool ingests.
#:
#: The ordering that used to be free now has to be real: rollup reads its children's
#: vectors, and it stopped being true that a chunk has one the moment it exists. The
#: settling walk already carries that (5.4) and no dependency array does — a chunk is
#: created ``in_flight`` holding this task, so its parent cannot settle and the rollup
#: planner is not called until every chunk under it has finished. See
#: :func:`jmfts_core.structure_tasks._TreeWriter._write_chunks`.
TASK_EMBED = "embed"

#: The write mode ``probe`` declares (spec 5.3). ``self``, not ``children``: probe writes
#: this node's own ``structured_content`` and creates no nodes. Enqueuing follow-on work
#: is not a write to the tree, and declaring ``children`` would reserve a region probe
#: never touches — which would block a real structuring task for no reason.
PROBE_WRITE_MODE = WRITE_SELF

#: Where a node's ingest options are kept between the upload that chose them and the
#: ``probe`` that reads them. ``probe`` runs in a worker, long after the request that
#: accepted the file has returned, so the options cannot be an argument — they have to be
#: on the row. Owned by the pipeline (:data:`~jmfts_core.repositories.document
#: .INGEST_OWNED_KEYS`), so a metadata PATCH cannot rewrite what a re-run will do.
OPTIONS_KEY = "options"


@dataclass(frozen=True)
class TaskOutcome:
    """What a handler did. Everything here lands in the attempt record (spec 3.4).

    ``status`` is ``'completed'`` or ``'skipped'``. A handler that *failed* raises; it
    does not return a status saying so, because the worker has to classify the exception
    to decide about a retry and a returned string carries no exception to classify.
    """

    detail: dict = field(default_factory=dict)
    produced: Optional[dict] = None
    rung: Optional[str] = None
    status: str = "completed"


class TaskHandler(Protocol):
    """Runs one claimed task inside the worker's transaction.

    The session is the worker's own, one per task, never a request-scoped one. The
    handler writes whatever the task writes and returns; the worker commits, or rolls
    back and records the failure.
    """

    def __call__(self, session: Session, task: TaskQueue) -> TaskOutcome: ...


TASK_HANDLERS: dict[str, TaskHandler] = {}


class UnknownTaskError(LookupError):
    """No handler is registered for a queued task type.

    Deliberately not survivable. The alternatives are worse in both directions: silently
    completing the row publishes a node whose work never ran, and silently leaving it
    pending stalls the ingestion with nothing saying why. The worker fails the task with
    a PERMANENT classification, which puts the reason in the node's attempt log and stops
    the retry budget being spent on a name that will not appear by itself.
    """


def register_task_handler(task_type: str) -> Callable[[TaskHandler], TaskHandler]:
    """Register the handler for ``task_type``. Duplicate registration is an error."""

    def decorate(handler: TaskHandler) -> TaskHandler:
        existing = TASK_HANDLERS.get(task_type)
        # Re-registering the SAME function is how a module re-imported under pytest or
        # uvicorn --reload behaves, and it is harmless. Two different functions under one
        # name is a real collision, and whichever import happened to run last would win.
        if existing is not None and existing is not handler:
            raise ValueError(
                f"task type {task_type!r} already has a handler ({existing!r}); "
                "two handlers under one name would be resolved by import order"
            )
        TASK_HANDLERS[task_type] = handler
        return handler

    return decorate


def has_task_handler(task_type: str) -> bool:
    return task_type in TASK_HANDLERS


def get_task_handler(task_type: str) -> TaskHandler:
    handler = TASK_HANDLERS.get(task_type)
    if handler is None:
        raise UnknownTaskError(
            f"no handler is registered for task type {task_type!r}; "
            f"known types are {sorted(TASK_HANDLERS)}"
        )
    return handler


# ---------------------------------------------------------------------------
# Part 4 — the enqueue conditions
# ---------------------------------------------------------------------------

#: Which pattern, per format, means "this file declares its own structure" (spec 3.5's
#: `declared` rung: PDF/EPUB outline, DOCX heading styles, PPTX slide list, XLSX sheets,
#: Markdown ATX, HTML h1-h6). The PDF and text probers exist today; every other entry
#: names a pattern probe does not yet report — and the condition is then simply false,
#: recorded as such, rather than assumed either way.
#:
#: There is no ``markdown`` key, and 11.3 records the measurement behind that: nothing in
#: the bytes distinguishes authored markdown from a ``.txt`` file that opens with a ``#``,
#: so ``detect_format`` reports both as ``text`` and one entry covers them. The ``html``
#: entry is kept even though HTML also arrives as ``text`` today — it is a true statement
#: about the format, and the pattern that actually keeps HTML out of the prose path is
#: ``has_markup`` on the ``extract:text`` row below.
DECLARED_STRUCTURE_PATTERN: dict[str, str] = {
    "pdf": "has_outline",
    "epub": "has_outline",
    "docx": "has_heading_styles",
    "pptx": "has_slides",
    "xlsx": "has_sheets",
    "text": "has_headings",
    "html": "has_headings",
}

#: Stands in a row's ``requires``/``forbids`` for "whatever pattern THIS format uses to
#: declare its own structure", resolved through the dict above. Written as a sentinel
#: rather than as one row per format because the rule is one rule — the declared rung runs
#: when the file declares something — and thirteen near-identical rows would let the copies
#: drift apart. A format with no entry at all resolves to nothing, and that asymmetry is
#: the point: as a requirement it can never be satisfied, as a prohibition it is always
#: satisfied, so an unknown format gets the inferred rung rather than neither rung.
DECLARED_STRUCTURE = "@declared_structure"

#: Why a task whose Part 4 condition HOLDS is nevertheless not enqueued: its handler
#: belongs to a later phasing step. Kept as data rather than as a comment because it is
#: written into the node's attempt log, where it is the answer to "why did nothing happen
#: to this file after probe".
DEFERRED_REASON: dict[str, str] = {
    TASK_EXTRACT_TABLES: (
        "tables are extracted inline into the markdown by `extract:text`; a task that "
        "gives each table its own node is INGEST_SPEC.md phasing step 5's remainder and "
        "has no handler registered"
    ),
    TASK_EXTRACT_IMAGES: "image handling is INGEST_SPEC.md phasing step 7; no handler registered",
}


@dataclass(frozen=True)
class SkippedTask:
    """A task the spec says to record as never-attempted, with the reason 3.4 demands."""

    task_type: str
    reason: str


@dataclass(frozen=True)
class TaskRow:
    """One row of Part 4's table, as data the planner reads rather than code it runs.

    A row's condition is deliberately restricted to three things — named patterns that
    must be true, named patterns that must be false, and rows it comes after. That
    restriction is the whole point of the shape: spec 11.2 wants an ``EXPLAIN`` that can
    answer "what would a ``.pptx`` with these options do?" from a format and a set of
    options, without bytes and without running anything, and that only holds if every
    enqueue decision is a pure function of ``(format, patterns, options)``. A predicate
    written as a lambda would be pure too, but it would not be *inspectable*: the reason a
    row did not fire could then only be discovered by running it, and the hand-written
    ``not_applicable`` string it needed would be one more thing to keep in step with the
    condition. Naming the patterns lets both the decision and its explanation come from
    the same declaration.
    """

    task: str
    #: Spec 5.3's declared write mode. Optional only because a row that is always recorded
    #: as skipped never reaches the queue and so has no region to declare; ``__post_init__``
    #: refuses a row that could be enqueued without one, rather than defaulting to a mode
    #: that would reserve a region the task never writes.
    write_mode: Optional[str] = None
    #: Rows — earlier in :data:`TASK_ROWS` — that must be eligible before this one is.
    #: Resolved to real queue dependencies by ``enqueue_batch`` (spec 5.5).
    after: tuple[str, ...] = ()
    #: Every named pattern must be truthy. :data:`DECLARED_STRUCTURE` resolves per format.
    #:
    #: A row may name a pattern its predecessor already requires — ``has_text_layer`` on
    #: the rows that come after ``extract:text`` — and that repetition is deliberate. It
    #: decides nothing (the dependency check reaches it first, and says so more precisely),
    #: but it keeps a row's condition readable as Part 4 writes it, without following
    #: ``after`` up the table.
    requires: tuple[str, ...] = ()
    #: Every named pattern must be falsy.
    forbids: tuple[str, ...] = ()
    #: Which group of the format's resolved options this row's queue row carries. The row
    #: names WHICH parameters its task takes; ``jmfts_core.ingest_options`` says what they
    #: are for the format being planned. A row with no key takes no parameters at all,
    #: which is a different fact from taking them and leaving them at their defaults — the
    #: empty params dict is what tells 6.1's re-run diff that nothing about this task can
    #: be tuned into needing a second run.
    params_key: Optional[str] = None
    #: If set, a row whose condition HOLDS is recorded as skipped rather than enqueued —
    #: Part 4's ``ocr``. Not the same fact as a deferred task (:data:`DEFERRED_REASON`),
    #: which is a condition that holds and an implementation that has not shipped.
    skip_reason: Optional[str] = None

    def __post_init__(self) -> None:
        if self.write_mode is None and self.skip_reason is None:
            raise ValueError(
                f"task row {self.task!r} can be enqueued but declares no write mode; "
                "spec 5.3 requires one before a task may claim a region"
            )


#: Part 4's table. Order is significant twice over: ``after`` may only name a row above,
#: and the eligible list comes out in this order, which is the order the batch is enqueued
#: in.
TASK_ROWS: tuple[TaskRow, ...] = (
    # `self`, for the same reason probe is (see PROBE_WRITE_MODE): extraction writes this
    # node's own `content` and `extraction` block and creates nothing. The nodes are made
    # by the structure task that follows it, which is the one that declares `children`.
    #
    # `has_markup` is a statement about what the extractors this appliance has can do, and
    # it is a `forbids` rather than a deferral because it is decided per FILE and not per
    # format: HTML arrives as `text` (11.3's measurement), so the row that handles prose
    # and the row that would handle markup are the same row seen from two files. The text
    # extractor is a decoder; decoding HTML yields HTML, which would then be chunked with
    # its tags intact and settle looking exactly like a success. No format reports the
    # pattern except `text`, so this constrains nothing else — an absent pattern is falsy
    # and the prohibition is satisfied.
    TaskRow(
        TASK_EXTRACT_TEXT,
        write_mode=WRITE_SELF,
        requires=("has_text_layer",),
        forbids=("has_markup",),
    ),
    # Part 4 marks ocr "out of scope for v1, recorded as skipped", so it is the one task
    # the spec itself says to log as never-attempted rather than to enqueue.
    TaskRow(
        TASK_OCR,
        requires=("is_scanned",),
        skip_reason=(
            "OCR is out of scope for v1 (INGEST_SPEC.md Part 4); the document has page "
            "images and no usable text layer, so its text is not recoverable by this "
            "appliance"
        ),
    ),
    # The two structure rungs are ONE decision read both ways, which is why they sit next
    # to each other reading the same sentinel with opposite signs. Part 4 predicates the
    # inferred rung on "coverage gap remains" after the declared rung, and a document that
    # declares no structure at all is the total case of that: the gap is the whole
    # document and it is already known here, from the one pattern the sentinel names.
    # Waiting for structure:declared to say so would mean enqueuing a task whose only
    # possible finding is that it has nothing to read. The PARTIAL case — an outline that
    # names eight chapters and nothing below — is not decidable from the patterns and is
    # not handled by this pass; the declared handler measures the remaining gap and
    # records it.
    TaskRow(
        TASK_STRUCTURE_DECLARED,
        write_mode=WRITE_CHILDREN,
        after=(TASK_EXTRACT_TEXT,),
        requires=("has_text_layer", DECLARED_STRUCTURE),
        params_key="structure",
    ),
    TaskRow(
        TASK_STRUCTURE_INFERRED,
        write_mode=WRITE_CHILDREN,
        after=(TASK_EXTRACT_TEXT,),
        requires=("has_text_layer",),
        forbids=(DECLARED_STRUCTURE,),
        params_key="structure",
    ),
    TaskRow(
        TASK_EXTRACT_TABLES,
        write_mode=WRITE_CHILDREN,
        after=(TASK_EXTRACT_TEXT,),
        requires=("has_text_layer", "has_tables"),
    ),
    TaskRow(TASK_EXTRACT_IMAGES, write_mode=WRITE_CHILDREN, requires=("has_images",)),
)


def _resolve_pattern(name: str, fmt: str) -> Optional[str]:
    """The pattern a row's condition actually tests, for this format.

    Plain names pass through unchanged, so ``None`` can only ever mean "the sentinel has
    no pattern for this format" — which is what lets the caller phrase that case as the
    different thing it is, rather than as a pattern that happened to be false.
    """
    if name != DECLARED_STRUCTURE:
        return name
    return DECLARED_STRUCTURE_PATTERN.get(fmt)


def _dependency_reason(task: str, dependency: str) -> str:
    """Why a row whose predecessor did not come out eligible is not applicable either.

    Spelled once because :func:`explain_plan` reports the same fact for the same rows and
    a second wording would read as a second, different finding.
    """
    return f"{dependency} is not eligible, and {task} depends on it"


def _no_declared_structure_reason(fmt: str) -> str:
    """Why a row requiring :data:`DECLARED_STRUCTURE` can never fire for this format.

    Not "a pattern that was false" — there is no pattern. :data:`DECLARED_STRUCTURE_PATTERN`
    has no entry for the format, so no bytes of it could ever satisfy the requirement, which
    is why :func:`explain_plan` reports the row as ``impossible`` and reuses this exact
    sentence for the reason.
    """
    return f"format {fmt!r} declares no structure pattern this spec knows about"


def _blocking_reason(row: TaskRow, fmt: str, patterns: dict, eligible: set[str]) -> Optional[str]:
    """Why ``row``'s condition does not hold, or ``None`` if it does.

    Dependencies are checked before patterns on purpose. A row that comes after a row that
    is not eligible has one honest reason, and naming a pattern its predecessor already
    accounts for would read as a second, independent problem — ``structure:declared`` on a
    file with no text layer is not applicable because there will be no text, not because
    of anything about its outline.
    """
    for dependency in row.after:
        if dependency not in eligible:
            return _dependency_reason(row.task, dependency)

    for name in row.requires:
        pattern = _resolve_pattern(name, fmt)
        if pattern is None:
            return _no_declared_structure_reason(fmt)
        if not patterns.get(pattern):
            return f"patterns.{pattern} is false or absent"

    for name in row.forbids:
        pattern = _resolve_pattern(name, fmt)
        # A sentinel with no pattern for this format prohibits nothing: there is no
        # declared structure to be in the way.
        if pattern is not None and patterns.get(pattern):
            return f"patterns.{pattern} is true, and {row.task} runs only when it is false"

    return None


@dataclass(frozen=True)
class DownstreamPlan:
    """What Part 4's table decides, given one node's ``matched.patterns``.

    Three lists rather than one, because they are three different facts and 3.4 insists
    they stay apart: work to do, work deliberately not done, and work whose condition was
    false.
    """

    #: Conditions that hold. Ordered; ``after`` names refer to earlier entries.
    eligible: tuple[TaskSpec, ...] = ()
    #: Conditions that hold but which the spec says to record as skipped rather than run.
    skipped: tuple[SkippedTask, ...] = ()
    #: task -> the condition that was false.
    not_applicable: dict[str, str] = field(default_factory=dict)


def plan_after_probe(fmt: str, patterns: dict, options: Optional[dict] = None) -> DownstreamPlan:
    """Evaluate spec Part 4's enqueue conditions from what ``probe`` measured.

    Only the rows Part 4 predicates on ``probe``'s output are decidable here. The lower
    structure rungs (``structure:inferred`` / ``semantic`` / ``flat``) are predicated on
    "coverage gap remains", which is a measurement the rung above produces — they are
    enqueued by whichever structuring task leaves a gap, not by probe. ``describe:images``
    and ``embed:images`` follow ``extract:images`` for the same reason. ``summarize`` and
    ``extract_facts`` belong to rollup (5.4) and are the settling walk's business.

    One pass over :data:`TASK_ROWS`, in order, with no branch per task: a row is either
    eligible, recorded as skipped, or recorded with the condition that was false. Adding a
    task is then a row, not a branch and a hand-typed string — which is what makes the
    thirteen tasks Part 4 lists and Part 8's two per worksheet a table rather than ninety
    more lines, and what keeps 11.2's ``EXPLAIN`` honest: the plan is derived from the same
    declaration the run is.

    ``options`` are OVERRIDES, not a finished set: they are resolved here against the
    format's profile and the tasks' own defaults, so the params that reach a queue row are
    always complete and always validated. Every group resolves for every format — the
    defaults belong to the task that reads them — so a row that names a ``params_key``
    always has parameters, and there is no case here to handle. Omitting the argument means
    those defaults, which is the documented meaning of "no overrides" rather than a
    stand-in for something missing. Resolving is idempotent on a complete set, which is what
    lets ``run_probe`` hand back the options the upload recorded on the node and get the
    same plan the upload was promised. A bad override raises rather than being ignored (see
    :func:`~jmfts_core.ingest_options.resolve_options`); by the time ``probe`` runs, the
    upload has already validated them, so a raise here means the node's stored options were
    tampered with rather than that a caller mistyped.
    """
    resolved = resolve_options(fmt, options)
    eligible: list[TaskSpec] = []
    skipped: list[SkippedTask] = []
    not_applicable: dict[str, str] = {}
    # Names of rows that came out eligible. A skipped row is NOT in here: nothing may be
    # ordered after work that will never be queued.
    eligible_names: set[str] = set()

    for row in TASK_ROWS:
        reason = _blocking_reason(row, fmt, patterns, eligible_names)
        if reason is not None:
            not_applicable[row.task] = reason
            continue
        if row.skip_reason is not None:
            skipped.append(SkippedTask(row.task, row.skip_reason))
            continue
        eligible.append(
            TaskSpec(
                task_type=row.task,
                write_mode=row.write_mode,
                # Copied, not shared: `resolved` is about to be read again by the next row
                # naming the same group, and this dict is going onto a queue row's params.
                params=dict(resolved[row.params_key]) if row.params_key else {},
                after=row.after,
            )
        )
        eligible_names.add(row.task)

    return DownstreamPlan(
        eligible=tuple(eligible), skipped=tuple(skipped), not_applicable=not_applicable
    )


def _split_by_handler(specs: tuple[TaskSpec, ...]) -> tuple[list[TaskSpec], dict[str, str]]:
    """Partition eligible tasks into runnable now, and deferred with a stated reason.

    A spec whose ``after`` names a deferred task is deferred too: enqueuing it with an
    unresolvable dependency would either block forever (if the dependency is never
    created) or run out of order.
    """
    runnable: list[TaskSpec] = []
    deferred: dict[str, str] = {}
    for spec in specs:
        if not has_task_handler(spec.task_type):
            deferred[spec.task_type] = DEFERRED_REASON.get(
                spec.task_type, "no handler is registered for this task type"
            )
            continue
        blocking = [name for name in spec.after if name in deferred]
        if blocking:
            deferred[spec.task_type] = (
                f"depends on {blocking!r}, which is deferred; a task cannot be ordered "
                "after work that was never queued"
            )
            continue
        runnable.append(spec)
    return runnable, deferred


# ---------------------------------------------------------------------------
# EXPLAIN — spec 11.2
# ---------------------------------------------------------------------------

#: A task will be queued, and a worker can run it.
OUTCOME_ENQUEUED = "enqueued"
#: Its condition holds and the spec says to record it as never-attempted (``ocr``).
OUTCOME_SKIPPED = "skipped"
#: Its condition holds — or could — and no handler is registered, so it is NOT enqueued.
OUTCOME_DEFERRED = "deferred"
#: Its condition is false for the patterns that are known.
OUTCOME_NOT_APPLICABLE = "not_applicable"
#: Its condition can never hold for this format, whatever the bytes turn out to be.
OUTCOME_IMPOSSIBLE = "impossible"
#: Undecidable without patterns; ``if_condition_holds`` says what it would become.
OUTCOME_CONDITIONAL = "conditional"

#: The caller handed us patterns — a hypothesis, or a real node's ``matched.patterns``.
PATTERNS_SUPPLIED = "supplied"
#: ``probe`` was RUN, over real bytes the caller sent, and these are what it measured.
#: ``ANALYZE``'s source, and the only one of the four that is evidence about a FILE rather
#: than about a format: ``supplied`` is a hypothesis, ``no_prober`` and ``unknown`` are
#: facts about this appliance.
PATTERNS_PROBED = "probed"
#: The format has no prober, so ``probe_patterns`` returns ``{}``. Known, not assumed.
PATTERNS_NO_PROBER = "no_prober"
#: The format has a prober and nobody said what it would find.
PATTERNS_UNKNOWN = "unknown"


@dataclass(frozen=True)
class ExplainedTask:
    """One row of Part 4's table as an ``EXPLAIN`` answer. Transport-neutral, like
    :class:`DownstreamPlan`; ``jmfts_core.contracts.explain`` is the wire form.

    ``requires``/``forbids`` are RESOLVED for the format being explained — the
    :data:`DECLARED_STRUCTURE` sentinel is replaced by the pattern it names, and a sentinel
    with no entry for the format is dropped from the list rather than emitted as a null.
    Dropping it loses nothing: as a requirement its absence is what makes the row
    ``impossible`` and :attr:`reason` says so, and as a prohibition it prohibits nothing.
    """

    task: str
    outcome: str
    #: Set ONLY when :attr:`outcome` is ``conditional`` — what the row becomes if its
    #: condition holds. ``deferred`` is legal here for a row whose own handler exists but
    #: whose predecessor is conditional and unimplemented; no row in the shipped table is
    #: in that position today.
    if_condition_holds: Optional[str] = None
    #: The skip reason, the deferral reason, or the condition that was false — whichever
    #: applies. Taken from the same strings the run records, never re-worded here.
    reason: Optional[str] = None
    write_mode: Optional[str] = None
    after: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    forbids: tuple[str, ...] = ()
    #: The resolved options for the row's ``params_key``; ``{}`` for a row that takes none.
    params: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ExplainedPlan:
    """What a format with these options would do, and on what basis it was decided.

    :attr:`patterns_source` is not decoration. ``EXPLAIN`` has no bytes and therefore no
    patterns, so an answer that did not say where its patterns came from would be
    indistinguishable from one that made them up.
    """

    format: str
    #: Whether ``probe`` can look inside this format at all (``probe.PROBERS_AVAILABLE``).
    prober_available: bool
    #: Whether every task's outcome is decided. False means some rows are ``conditional``.
    patterns_known: bool
    patterns_source: str
    #: Supplied pattern keys no row's condition consults for this format. Reported, never
    #: rejected: pasting a real ``matched.patterns`` block is the obvious use and it
    #: carries measurements (``page_count``, ``outline_depth``) no condition reads. What
    #: the list is FOR is making a typo — ``has_text_lyer`` — visible as a key that decided
    #: nothing, instead of silently planning as though the pattern were false.
    patterns_ignored: tuple[str, ...]
    #: The RESOLVED options, all groups — what the queue rows would actually carry.
    options: dict
    #: ``probe`` first (it always runs), then every row of :data:`TASK_ROWS` in table
    #: order. Every row appears: a task missing from a plan is a wrong answer, not a short
    #: one.
    tasks: tuple[ExplainedTask, ...]


def _resolved_names(names: tuple[str, ...], fmt: str) -> tuple[str, ...]:
    """A row's ``requires``/``forbids`` with the sentinel replaced by the real pattern."""
    resolved = (_resolve_pattern(name, fmt) for name in names)
    return tuple(pattern for pattern in resolved if pattern is not None)


def _consulted_patterns(fmt: str) -> set[str]:
    """Every pattern name any row's condition reads for this format."""
    consulted: set[str] = set()
    for row in TASK_ROWS:
        consulted.update(_resolved_names(row.requires, fmt))
        consulted.update(_resolved_names(row.forbids, fmt))
    return consulted


def _is_impossible(row: TaskRow, fmt: str) -> bool:
    """Whether ``row``'s condition can never hold for ``fmt``, whatever the bytes are.

    Only ``requires`` can make a row impossible. A :data:`DECLARED_STRUCTURE` sentinel with
    no entry for the format names a pattern that does not exist, so no probe of any file of
    this format can report it true; the same sentinel in ``forbids`` prohibits nothing and
    is always satisfied (see :data:`DECLARED_STRUCTURE`).
    """
    return any(_resolve_pattern(name, fmt) is None for name in row.requires)


def _explained_row(row: TaskRow, fmt: str, resolved: dict, outcome: str, **kwargs) -> ExplainedTask:
    """One :class:`ExplainedTask` from a row, with the parts that never vary filled in."""
    return ExplainedTask(
        task=row.task,
        outcome=outcome,
        write_mode=row.write_mode,
        after=row.after,
        requires=_resolved_names(row.requires, fmt),
        forbids=_resolved_names(row.forbids, fmt),
        params=dict(resolved[row.params_key]) if row.params_key else {},
        **kwargs,
    )


def _explain_concrete(
    fmt: str, patterns: dict, options: Optional[dict], resolved: dict
) -> tuple[ExplainedTask, ...]:
    """Every row's outcome, when the patterns are known.

    The plan is taken from :func:`plan_after_probe` and :func:`_split_by_handler`
    UNCHANGED — the same two calls ``run_probe`` makes — rather than re-derived. A second
    evaluator of the same table would be free to disagree with the first, and the disagreement
    would surface as an ``EXPLAIN`` that confidently describes a run that does something else.
    """
    plan = plan_after_probe(fmt, patterns, options)
    runnable, deferred = _split_by_handler(plan.eligible)
    enqueued = {spec.task_type for spec in runnable}
    skipped = {entry.task_type: entry.reason for entry in plan.skipped}

    impossible: set[str] = set()
    tasks: list[ExplainedTask] = []
    for row in TASK_ROWS:
        blocked = plan.not_applicable.get(row.task)
        if blocked is not None:
            # `impossible` REFINES not-applicable rather than competing with it: the run
            # records the same row as not-applicable, and the outcome adds the fact that no
            # bytes of this format could have changed that.
            #
            # The row's OWN unsatisfiable requirement is reported ahead of a blocked
            # dependency, which is the one place this deliberately departs from
            # `_blocking_reason`'s order. Both are true of `structure:declared` on a format
            # with no declared-structure pattern and no text layer; only one of them is why
            # the answer is `impossible` rather than "not this time".
            if _is_impossible(row, fmt):
                impossible.add(row.task)
                reason = _no_declared_structure_reason(fmt)
            elif any(name in impossible for name in row.after):
                impossible.add(row.task)
                reason = blocked
            else:
                tasks.append(
                    _explained_row(row, fmt, resolved, OUTCOME_NOT_APPLICABLE, reason=blocked)
                )
                continue
            tasks.append(_explained_row(row, fmt, resolved, OUTCOME_IMPOSSIBLE, reason=reason))
        elif row.task in skipped:
            tasks.append(
                _explained_row(row, fmt, resolved, OUTCOME_SKIPPED, reason=skipped[row.task])
            )
        elif row.task in deferred:
            tasks.append(
                _explained_row(row, fmt, resolved, OUTCOME_DEFERRED, reason=deferred[row.task])
            )
        elif row.task in enqueued:
            tasks.append(_explained_row(row, fmt, resolved, OUTCOME_ENQUEUED))
        else:
            # Unreachable: the three lists of a DownstreamPlan partition TASK_ROWS, and
            # `_split_by_handler` partitions the eligible list. Raised rather than assumed
            # because the alternative is reporting some default outcome for a row the plan
            # placed somewhere this function does not know about.
            raise RuntimeError(
                f"task {row.task!r} appears in no part of the plan for format {fmt!r}; "
                "DownstreamPlan no longer partitions TASK_ROWS"
            )
    return tuple(tasks)


def _explain_conditional(fmt: str, resolved: dict) -> tuple[ExplainedTask, ...]:
    """Every row's outcome, when the format has a prober and nobody said what it found.

    Most rows come out ``conditional`` — the resolved patterns that decide them are on the
    row, and ``if_condition_holds`` says what they would become. Three kinds are decided
    anyway, and reporting them as undecided would be a worse answer than no answer: a row
    whose requirement names a pattern this format has no name for can never fire, a row
    after such a row can never fire either, and a row with no registered handler is not
    enqueued whatever the bytes say.
    """
    impossible: set[str] = set()
    deferred: set[str] = set()
    tasks: list[ExplainedTask] = []

    for row in TASK_ROWS:
        # Same precedence as `_explain_concrete`: the row's own unsatisfiable requirement
        # first, a dependency that can never fire second.
        if _is_impossible(row, fmt):
            impossible.add(row.task)
            reason = _no_declared_structure_reason(fmt)
            tasks.append(_explained_row(row, fmt, resolved, OUTCOME_IMPOSSIBLE, reason=reason))
            continue
        blocking = next((name for name in row.after if name in impossible), None)
        if blocking is not None:
            impossible.add(row.task)
            reason = _dependency_reason(row.task, blocking)
            tasks.append(_explained_row(row, fmt, resolved, OUTCOME_IMPOSSIBLE, reason=reason))
            continue

        if row.skip_reason is not None:
            # `ocr` is the one row the spec itself says to record rather than run, so what
            # is unknown about it is only whether the condition fires at all.
            tasks.append(
                _explained_row(
                    row,
                    fmt,
                    resolved,
                    OUTCOME_CONDITIONAL,
                    if_condition_holds=OUTCOME_SKIPPED,
                    reason=row.skip_reason,
                )
            )
            continue

        if not has_task_handler(row.task):
            deferred.add(row.task)
            reason = DEFERRED_REASON.get(row.task, "no handler is registered for this task type")
            tasks.append(_explained_row(row, fmt, resolved, OUTCOME_DEFERRED, reason=reason))
            continue
        blocking = next((name for name in row.after if name in deferred), None)
        if blocking is not None:
            deferred.add(row.task)
            tasks.append(
                _explained_row(
                    row,
                    fmt,
                    resolved,
                    OUTCOME_DEFERRED,
                    reason=(
                        f"depends on {blocking!r}, which is deferred; a task cannot be "
                        "ordered after work that was never queued"
                    ),
                )
            )
            continue

        tasks.append(
            _explained_row(
                row, fmt, resolved, OUTCOME_CONDITIONAL, if_condition_holds=OUTCOME_ENQUEUED
            )
        )
    return tuple(tasks)


def explain_plan(
    fmt: str,
    options: Optional[dict] = None,
    patterns: Optional[dict] = None,
    *,
    patterns_source: Optional[str] = None,
) -> ExplainedPlan:
    """``EXPLAIN`` for file ingestion: what this format with these options would do.

    Spec 11.2's first mode — format and options, NO BYTES. This function never opens a
    file. ``ANALYZE``, 11.2's second mode, is ``IngestService.analyze_ingest``: it runs
    :func:`~jmfts_core.probe.detect_format` and :func:`~jmfts_core.probe.probe_patterns`
    over real bytes and then calls THIS, passing what probe measured as ``patterns`` and
    :data:`PATTERNS_PROBED` as ``patterns_source``. One planner answers both modes, which
    is what stops the two from ever describing different runs.

    ``patterns_source`` is therefore an override of the provenance label, not of the plan:
    the rows are decided by ``patterns`` either way, and this only changes what the answer
    says about where those patterns came from. It is refused without ``patterns``, because
    a conditional plan has no provenance to state.

    :func:`plan_after_probe` is already a pure function of ``(format, patterns, options)``,
    and ``EXPLAIN`` has two of those three. It therefore answers in one of two ways, and
    :attr:`ExplainedPlan.patterns_source` states which, because an answer that did not say
    would be indistinguishable from one that invented the third input:

    **Concrete** — the patterns are known and every row is decided. Either the caller
    supplied them (a hypothesis, or a real node's ``matched.patterns`` pasted back), or the
    format has no prober, in which case ``probe_patterns`` returns ``{}`` at runtime and the
    empty pattern set is a measured fact about this appliance rather than an assumption
    about the file. The second is the honest answer to 11.2's own example question, "what
    would a ``.pptx`` do?" — today: probe identifies it, and nothing downstream can become
    eligible, because nothing can report a pattern.

    **Conditional** — the format has a prober and the caller said nothing about what it
    would find. Each row then carries the resolved patterns that decide it and what it
    would become if they held. Rows that are decidable anyway are still decided; see
    :func:`_explain_conditional`.

    Patterns are NEVER fabricated. There is no "assume a text layer" default, because a
    plan built on a guessed input is a wrong answer with a confident shape — and 11.2's
    whole claim is that the plan is a first-class answer.

    ``options`` are overrides, resolved here exactly as an upload resolves them, so a
    misspelled option raises ``ValueError`` naming it (a 400 at the endpoint) rather than
    being explained away as a plan the run would not produce.
    """
    if patterns_source is not None and patterns is None:
        raise ValueError(
            "patterns_source says where `patterns` came from and there are none; labelling "
            "a conditional plan with a provenance would claim an input it does not have"
        )

    resolved = resolve_options(fmt, options)
    prober_available = fmt in PROBERS_AVAILABLE

    if patterns is not None:
        known: Optional[dict] = patterns
        source = patterns_source or PATTERNS_SUPPLIED
    elif not prober_available:
        known, source = {}, PATTERNS_NO_PROBER
    else:
        known, source = None, PATTERNS_UNKNOWN

    if known is not None:
        rows = _explain_concrete(fmt, known, options, resolved)
    else:
        rows = _explain_conditional(fmt, resolved)

    return ExplainedPlan(
        format=fmt,
        prober_available=prober_available,
        patterns_known=known is not None,
        patterns_source=source,
        patterns_ignored=tuple(sorted(set(patterns or {}) - _consulted_patterns(fmt))),
        options=resolved,
        tasks=(
            # probe is not a TASK_ROWS entry because nothing decides it: Part 4 specifies it
            # as depending on nothing and always running, which is exactly why it has no
            # condition to declare. It leads the list for the same reason.
            ExplainedTask(task=TASK_PROBE, outcome=OUTCOME_ENQUEUED, write_mode=PROBE_WRITE_MODE),
        )
        + rows,
    )


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------


@register_task_handler(TASK_PROBE)
def run_probe(session: Session, task: TaskQueue) -> TaskOutcome:
    """Identify the bytes, measure the content patterns, and schedule what follows.

    Spec 3.1 keeps file type and content pattern in separate fields, and this runs them
    in that order: :func:`~jmfts_core.probe.detect_format` says how to open the bytes,
    :func:`~jmfts_core.probe.probe_patterns` says what is inside. The node's ``matched``
    block gets both (3.3), then Part 4's conditions are evaluated over the patterns.

    Failure is NOT caught here. A corrupt PDF, a missing prober library, a node with no
    stored bytes — each raises, the worker classifies it, and the node's attempt log
    records the failure with its classification. The step-3 version of this code ran
    inline inside the upload request and had to swallow the exception, because raising
    would have rolled back and discarded bytes the client had already sent. The queue
    removes that constraint: the upload has committed long before this runs.
    """
    doc = session.get(Document, task.scope_document_id)
    if doc is None:
        raise ValueError(
            f"probe is scoped to document {task.scope_document_id}, which does not exist"
        )

    file_block = (doc.structured_content or {}).get("file")
    if not isinstance(file_block, dict):
        raise ValueError(
            f"document {doc.id} carries no `file` block; probe reads the uploaded bytes "
            "and the record of what was received, and this node was not created by an "
            "upload (INGEST_SPEC.md 3.3)"
        )

    data = BlobRepository(session).read_bytes(doc.id)
    if data is None:
        raise ValueError(
            f"document {doc.id} has a `file` block but no stored blob; the bytes it "
            "describes are gone and there is nothing to probe"
        )

    declared_mime = file_block.get("declared_mime")
    # Re-derived from the bytes rather than read back off the `file` block. Detection is
    # cheap and deterministic, and deriving it here means this handler works on any node
    # carrying bytes, not only on one this exact version of the uploader wrote.
    detection = detect_format(
        data, filename=file_block.get("filename"), declared_mime=declared_mime
    )

    detail: dict = {
        "format": detection.format,
        "declared_mime": declared_mime,
        "detected_mime": detection.detected_mime,
        "detected_by": detection.detected_by,
    }
    # Spec 3.1: when declared and detected disagree, record both. Both are already on the
    # `file` block; this makes the DISAGREEMENT itself a first-class entry in the log, so
    # nobody has to notice it by comparing fields.
    if detection.mime_agrees is False:
        detail["mime_conflict"] = {
            "declared": declared_mime,
            "detected": detection.detected_mime,
        }

    patterns, pattern_detail = probe_patterns(data, detection)
    detail.update(pattern_detail)

    sc = dict(doc.structured_content or {})
    sc["matched"] = {
        "format": detection.format,
        "patterns": patterns,
        "probed_at": utc_now_iso(),
    }
    doc.structured_content = sc
    session.flush()

    # The options the UPLOAD was made with, read back off the node rather than resolved
    # afresh. They were resolved and validated before this node existed, and freezing them
    # there is what makes a re-probe reproduce the run it is re-probing instead of picking
    # up whatever the profile defaults have become since. A node with no `options` block
    # was written by something older than this block and recorded no overrides, which
    # resolves to the same profile defaults it already ran with.
    plan = plan_after_probe(detection.format, patterns, sc.get(OPTIONS_KEY))
    runnable, deferred = _split_by_handler(plan.eligible)

    tasks = TaskQueueRepository(session)
    enqueued_ids = enqueue_batch(tasks, doc.id, runnable) if runnable else ()

    docs = DocumentRepository(session)
    for entry in plan.skipped:
        docs.upsert_attempt(doc, _skipped_record(doc, entry, docs.attempt_counts(doc)))

    detail["enqueued"] = {spec.task_type: task_id for spec, task_id in zip(runnable, enqueued_ids)}
    detail["deferred"] = deferred
    detail["skipped"] = {entry.task_type: entry.reason for entry in plan.skipped}
    detail["not_applicable"] = plan.not_applicable
    return TaskOutcome(detail=detail)


def _skipped_record(doc: Document, entry: SkippedTask, counts: dict[str, int]) -> AttemptRecord:
    """A spec-3.4 ``skipped`` entry for a task that will never be attempted.

    ``skipped`` means the task was NEVER attempted, and 3.4 requires it to carry
    ``detail.reason``. There is no queue row and therefore no ``task_id``: inventing one
    would point the log at a row that does not exist.

    The timestamps are the instant the decision was taken, not a duration of work — the
    record contract requires a terminal status to carry both, and the honest reading of
    them for a skip is "this is when we decided not to". The same convention is already
    what ``execute_pipeline`` writes for a disabled stage.
    """
    params: dict = {}
    decided_at = datetime.now(timezone.utc)
    return AttemptRecord(
        task=entry.task_type,
        status="skipped",
        attempt=counts.get(entry.task_type, 0) + 1,
        scope_document_id=doc.id,
        params=params,
        param_fingerprint=param_fingerprint(params),
        started_at=decided_at,
        finished_at=decided_at,
        detail={"reason": entry.reason},
    )


def utc_now_iso() -> str:
    """Timezone-aware UTC, ISO-8601 — the format spec 3.3's `uploaded_at`/`probed_at` show."""
    return datetime.now(timezone.utc).isoformat()


# Registration of the handlers that live in other modules, at the BOTTOM of this one and
# on purpose. `_split_by_handler` asks `has_task_handler` whether a task can run, so what
# is registered decides what `plan_after_probe` enqueues — and if registration depended on
# some caller having imported the handler module first, the same patterns would schedule
# differently depending on import order. Importing them here means anyone who can see
# `TASK_HANDLERS` sees every handler that has shipped.
#
# The cycle is real and it resolves: `structure_tasks` imports names from this module, and
# by the time this line runs every one of them is defined.
from jmfts_core import embed_tasks  # noqa: E402,F401  (side effect: registration)
from jmfts_core import rollup_tasks  # noqa: E402,F401  (side effect: registration)
from jmfts_core import structure_tasks  # noqa: E402,F401  (side effect: registration)
