"""``EXPLAIN`` and ``ANALYZE`` for file ingestion — the wire forms of ``INGEST_SPEC.md``
11.2's two modes.

A NEW module rather than an addition to ``contracts/ingest.py``, and the separation is the
signal. ``contracts/ingest.py`` is path A's contract set — ``IngestRequest``,
``PipelineStageInfo``, the synchronous pipeline 11.1 deprecates and says will be deleted
when path B reaches parity. A path-B contract filed alongside it would say the two are one
surface, which is the opposite of what 11.1 decided.

Both modes live here because they are one answer with two ways of reaching it:
:class:`AnalyzeIngestResponse` CONTAINS an :class:`ExplainIngestResponse`, because
``ANALYZE`` is ``EXPLAIN`` with the third input measured rather than assumed. Splitting
them across two modules would put the containing shape and the contained one in different
files for no gain.

The scheduler's dataclasses are the answer; these are the shapes it travels in. They are
kept apart for the reason every contract module here is: an in-process caller gets the
dataclass with tuples and enum-ish string constants, and only a request that crossed HTTP
pays for the Pydantic round trip.

The functions that BUILD these models from those dataclasses live server-side, in
``jmfts_core/explain_wire.py``. They cannot live here: they take scheduler and prober
types, and this package is installed by consumers who have neither.
"""

from typing import Optional

from pydantic import BaseModel, Field


class ExplainIngestRequest(BaseModel):
    """What to explain: a format, the options to explain it under, and — optionally — the
    patterns to assume ``probe`` would report.

    ``patterns`` is what turns a conditional answer into a concrete one. It is a HYPOTHESIS
    the caller supplies, so it is never invented on their behalf: omitting it for a format
    with a prober gets an answer that says which conditions it could not decide, rather
    than one built on a guess about the file.
    """

    format: str = Field(
        min_length=1,
        description=(
            "The format `probe` would report — the short name `detect_format` produces "
            "(`pdf`, `docx`, `pptx`, `zip`, `text`, ...), not a mime type and not a "
            "filename. An unknown name is a legal question and gets a real answer: every "
            "condition that names a format-specific pattern is reported as impossible."
        ),
    )
    options: Optional[dict] = Field(
        default=None,
        description=(
            "Ingest option overrides, in the shape `POST /ingest/file` takes them — "
            '`{"structure": {"max_tokens": 60}}`. Resolved against the task defaults and '
            "the format's profile exactly as an upload resolves them, so an option that "
            "does not exist is a 400 here for the same reason it is a 400 there."
        ),
    )
    patterns: Optional[dict] = Field(
        default=None,
        description=(
            'The content patterns to assume, e.g. `{"has_text_layer": true}`. A real '
            "node's `matched.patterns` block can be pasted in whole: keys no condition "
            "reads are reported in `patterns_ignored` rather than rejected."
        ),
    )


class CountResponse(BaseModel):
    """An interval with an open top. ``SPRINT_JOBS.md`` 7.1 and 7.2.

    Every accumulator in the cost fold returns one, because a fan-out atom declares a
    BOUND rather than a number — so the answer is ``[low, high]`` and never a floor.

    ``high: null`` is not "unbounded" and not "very large". It means the measurement that
    would close this interval has not been taken, because ``EXPLAIN`` is given no bytes and
    the atom that writes it has not run. ``ExplainIngestResponse.cost.unmeasured`` names
    which measurement and who would write it. A sentinel integer here would be
    indistinguishable from a measured large number, which is the wrong answer 7.2 refuses.
    """

    low: int = Field(description="The floor. Always known.")
    high: Optional[int] = Field(
        description="The ceiling, or null when nothing in this answer bounds it"
    )


class UnmeasuredResponse(BaseModel):
    """A count the fold needed and could not take. 7.1's `?` line.

    7.1 prints `? rows — produced by profile:sheet` under the totals, and says the line
    naming what would collapse the interval "is the part that carries the value". This is
    that line: an interval with an open top is only actionable beside the name of the thing
    that would close it.
    """

    evidence: str = Field(
        description=(
            "The registry name the bound reads — `sheet.measurements.rows`, "
            "`extraction.characters`. From the atom's own `Fanout.reads`."
        )
    )
    needed_by: str = Field(description="The task whose fan-out bound declared it")
    written_by: str = Field(
        description=(
            "Who writes it, read out of the evidence registry rather than from a table — "
            "`profile:sheet writes it (measurements.rows)`. Run that task and the interval "
            "closes."
        )
    )


class PlanCostResponse(BaseModel):
    """How much, beside `EXPLAIN`'s what. ``SPRINT_JOBS.md`` Part 7.

    One traversal of the plan with a choice of accumulator: counting tasks, child nodes,
    embeddings and LLM calls are the same fold. What makes it a fold rather than a query is
    that a parent rule's fan-out IS the child rule's multiplicity, so cardinality composes
    without anything reading the tree — this costs no I/O and is computed for every plan.
    """

    tasks: CountResponse = Field(
        description=(
            "Task INSTANCES, not table rows. A forty-sheet workbook's `profile:sheet` is "
            "forty of them, and a plan that reported one would be the reading `scope` "
            "exists to prevent."
        )
    )
    nodes: CountResponse = Field(
        description="Child nodes the plan's rules write, from their declared fan-out bounds"
    )
    by_cost_class: dict[str, CountResponse] = Field(
        description=(
            "Instances by what the task's most expensive step is: `cpu` counts, parses and "
            "reads bytes; `model` is an embedding forward pass; `llm` is a call to the "
            "configured endpoint. A budget is sized on the last."
        )
    )
    unmeasured: list[UnmeasuredResponse] = Field(
        description="Every open top in this answer, and what would close each one"
    )
    not_counted: list[str] = Field(
        description=(
            "What these totals leave out, in sentences. Derived from the declarations, not "
            "a list kept here: work scheduled outside Part 4's table (the settling walk, a "
            "fetch, a binding), tasks that write into another tree the locus vocabulary "
            "cannot name, and any rung that writes children without declaring a bound."
        )
    )


class ExplainedTaskResponse(BaseModel):
    """One task's outcome. ``INGEST_SPEC.md`` Part 4's table, read as a forecast."""

    task: str
    outcome: str = Field(
        description=(
            "enqueued | skipped | deferred | not_applicable | impossible | conditional. "
            "`impossible` is stronger than `not_applicable`: the condition cannot hold for "
            "this format whatever the bytes are. `deferred` means the condition holds (or "
            "could) and no handler is registered, so nothing is queued."
        )
    )
    if_condition_holds: Optional[str] = Field(
        default=None,
        description=(
            "What the task becomes if its condition holds — enqueued, skipped or deferred. "
            "Present only when `outcome` is `conditional`."
        ),
    )
    reason: Optional[str] = Field(
        default=None,
        description=(
            "The skip reason, the deferral reason, or the condition that was false — the "
            "same sentence the run would write into the node's attempt log."
        ),
    )
    write_mode: Optional[str] = Field(
        default=None,
        description=(
            "The region this task claims (spec 5.3): `self` or `children`. Null for a task "
            "that is always recorded rather than queued, which claims nothing."
        ),
    )
    scope: str = Field(
        default="@root",
        description=(
            "Which nodes this task runs on. `@root` is the uploaded file node — one task "
            "per file. `@children_of(rule|rule):usetype|usetype` is one task per child a "
            "named rule wrote, so a 40-sheet workbook's `profile:sheet` is 40 tasks and "
            "not one. Without it a plan listing `structure:sheets` beside `profile:sheet` "
            "reads as two tasks on one node."
        ),
    )
    after: list[str] = Field(
        description=(
            "Tasks that must ALL be eligible before this one is. Within one scope: an "
            "ordering is between two tasks on ONE node. What orders a child-scoped task "
            "after the rule that made its node is the scope, not this."
        )
    )
    after_any: list[str] = Field(
        default_factory=list,
        description=(
            "Tasks of which AT LEAST ONE must be eligible; this task is ordered after "
            "whichever of them are. Kept apart from `after` because they are alternatives "
            "to each other — the two structure rungs, of which exactly one ever fires — so "
            "a reader who saw them in `after` would conclude the task can never run."
        ),
    )
    requires: list[str] = Field(
        description=(
            "Patterns that must be true, RESOLVED for this format — the declared-structure "
            "sentinel replaced by the pattern it names for this format. A sentinel with no "
            "pattern here is dropped from the list and is what makes the task impossible; "
            "`reason` says so."
        )
    )
    forbids: list[str] = Field(description="Patterns that must be false, resolved the same way")
    params: dict = Field(description="The resolved options this task's queue row would carry")
    multiplicity: CountResponse = Field(
        description=(
            "HOW MANY instances of this task the plan holds — `scope`'s cardinality times "
            "whether `outcome` lets it run at all. `@root` is one node, so a root-scoped "
            "task is 1 or 0; a child-scoped one is however many nodes the rules above it "
            "write, which is where a three-sheet workbook's three `profile:sheet` come "
            "from. `[0, 1]` for a conditional task: the patterns that decide it were not "
            "supplied and the fold refuses to pick."
        )
    )
    writes: CountResponse = Field(
        description=(
            "Child nodes all of those instances write together, from the atom's declared "
            "fan-out bound. Zero for a task that writes no children; an open top when the "
            "bound's evidence has not been measured yet."
        )
    )
    writes_usetype: Optional[str] = Field(
        default=None,
        description=(
            "What `writes` counts — `chunk`, `sheet`, `record`. Null when nothing is "
            'written. Present because "wrote 5" has to say five WHAT before it can be '
            "held against a ceiling."
        ),
    )
    writes_basis: Optional[str] = Field(
        default=None,
        description=(
            "The bound in words — `⌊characters / min_chunk_length⌋ chunks`. The atom's own "
            "sentence, not a paraphrase of it."
        ),
    )


class ExplainIngestResponse(BaseModel):
    """The plan, and the basis it was decided on. ``INGEST_SPEC.md`` 11.2.

    `patterns_source` is load-bearing, not metadata. `EXPLAIN` is given no bytes and
    therefore has no patterns; an answer that did not say where its patterns came from
    would be indistinguishable from one that made them up.
    """

    format: str
    prober_available: bool = Field(
        description="Whether `probe` can look inside this format at all today"
    )
    patterns_known: bool = Field(
        description=(
            "True when every task's outcome is decided. False means the format has a "
            "prober, no patterns were supplied, and some tasks are `conditional`."
        )
    )
    patterns_source: str = Field(
        description=(
            "probed — `probe` ran over real bytes and measured them (`ANALYZE`); supplied "
            "— the caller gave them; no_prober — this format has no prober, so `probe` "
            "reports no patterns and the empty set is a fact rather than an assumption; "
            "unknown — nobody said, and the answer is conditional."
        )
    )
    patterns_ignored: list[str] = Field(
        description=(
            "Supplied keys no condition consults for this format. Reported, not rejected: "
            "a real `matched.patterns` block carries measurements (`page_count`, "
            "`outline_depth`) nothing schedules on. It is what makes a misspelled pattern "
            "visible instead of silently planning as though it were false."
        )
    )
    options: dict = Field(description="The RESOLVED options, all groups — what the run would use")
    tasks: list[ExplainedTaskResponse] = Field(
        description=(
            "`probe` first, then every Part 4 row in table order — at EVERY scope, so a "
            "workbook's forecast reaches the two tasks per sheet rather than stopping at "
            "the sheet list. None is omitted; read `scope` to know which node each runs on."
        )
    )
    cost: PlanCostResponse = Field(
        description=(
            "How much, folded over `tasks` — how many task instances, how many nodes, how "
            "many embeddings and LLM calls. Always present: the fold is a walk over "
            "declarations with no I/O in it, and splitting it into a second request would "
            "be two answers about one plan that a caller could receive out of step."
        )
    )


class AnalyzedFile(BaseModel):
    """What the bytes are, what the client said they were, and how we know.

    The same four fields an upload writes into the node's ``file`` block (spec 3.3),
    produced by the same :func:`~jmfts_core.probe.detect_format`. A caller can compare this
    against what it expected to send BEFORE a node exists — a ``.docx`` that is really a
    ZIP with no OOXML parts is visible here, where after an upload it is an unexplained
    extraction failure two tasks later.
    """

    filename: str
    byte_size: int
    content_hash: str = Field(description="sha256:<hex> of the analysed bytes")
    declared_mime: Optional[str] = Field(description="What the client said; null if nothing")
    detected_mime: Optional[str] = Field(description="What the bytes are; null if unrecognised")
    detected_by: Optional[str] = Field(description="magic_bytes | zip_manifest | content_sniff")
    mime_agrees: Optional[bool] = Field(
        description=(
            "Whether declared and detected agree. Null when the comparison cannot be made "
            "because one of them is unknown — which is a different answer from `false`."
        )
    )


class ProbeFailure(BaseModel):
    """``probe`` would raise on these bytes, so there is no plan to give.

    Not an error swallowed into a plausible answer: the run would fail here too, and this
    reports the failure the worker would record — the exception, and the SAME
    classification :func:`~jmfts_core.task_errors.classify_exception` would give it, which
    is what decides whether the real task retries. A file that cannot be probed has no
    downstream schedule, because Part 4's conditions are evaluated over patterns that were
    never measured; describing one anyway would be the wrong answer this endpoint exists
    to prevent.
    """

    error: str = Field(description="`ExceptionType: message`, as the attempt log records it")
    error_type: str = Field(
        description=(
            "retryable | permanent | timeout | dependency — what the worker would classify "
            "this as, and therefore whether the real task would be retried"
        )
    )


class AlreadyStored(BaseModel):
    """These bytes are already here, so an upload would deduplicate to this node.

    Load-bearing rather than informational. ``POST /ingest/file`` resolves an upload whose
    sha256 already exists to the existing node and runs NO plan (spec 6.1) — so a forecast
    that omitted this would describe a pipeline the upload is not going to run. It also
    warns about the one refusal a caller cannot otherwise predict: an upload whose resolved
    options differ from those recorded on the existing node is a 400.
    """

    document_id: int
    settled: str = Field(description="in_flight | settled | failed — where that node got to")
    options: Optional[dict] = Field(
        description=(
            "The resolved options the existing node was ingested with, or null for a node "
            "that predates them. An upload carrying anything else is refused (spec 6.1)."
        )
    )


class AnalyzeIngestResponse(BaseModel):
    """``ANALYZE``: what THESE bytes would do. ``INGEST_SPEC.md`` 11.2's second mode.

    ``probe`` is run and nothing else. Exactly one of :attr:`plan` and :attr:`probe_failed`
    is set, and callers must check the failure first: a file probe cannot open has no
    schedule, and a null plan is that fact rather than an empty one.
    """

    file: AnalyzedFile
    format: str = Field(description="The short format name every Part 4 condition is keyed by")
    patterns: dict = Field(
        description=(
            "What `probe` MEASURED — the block that would land in `matched.patterns`. Empty "
            "for a format with no prober, which is a measured fact about this appliance and "
            "not a claim about the file."
        )
    )
    probe_detail: dict = Field(
        description=(
            "`probe`'s own attempt detail: the raw numbers its derived patterns come from "
            "(`text_chars`, `chars_per_page`, the scanned threshold), or the name of the "
            "format it has no prober for. Verbatim — this is what the node's log would hold."
        )
    )
    probe_failed: Optional[ProbeFailure] = Field(
        default=None, description="Set when `probe` would raise; `plan` is then null"
    )
    plan: Optional[ExplainIngestResponse] = Field(
        default=None,
        description=(
            "The plan, from the same `explain_plan` `POST /ingest/explain` calls, with "
            "`patterns_source: probed`. Null only when `probe_failed` is set."
        ),
    )
    already_stored: Optional[AlreadyStored] = Field(
        default=None,
        description=(
            "Set when these bytes are already stored in a file node this caller may read. "
            "An upload would resolve to it and run no plan at all."
        ),
    )
