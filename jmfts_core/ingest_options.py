"""Ingest options for the queued pipeline: task defaults, per-format profiles, and the one
merge that resolves them. ``INGEST_SPEC.md`` 11.2.

The queued pipeline — upload, ``probe``, and everything Part 4's table schedules after it —
had no configuration surface at all. What a document got was decided entirely by its bytes,
and the one set of tunable numbers in it was a module constant. The synchronous pipeline
11.1 deprecated had been configurable per call since it shipped. The newer pipeline being
the less controllable one is what this module closed.

It was deliberately shaped like the deleted ``PipelineDefinition``: a registry of named
defaults, and one merge of caller overrides over them. That was so path A's callers had
something familiar to migrate onto at cut-over rather than a second model to learn. Part 15
finished that migration and there is only this one now.

**Three layers, and which is which is the design.**

::

    TASK_PARAM_DEFAULTS[group]        the task's parameters — valid wherever it runs
      <- INGEST_PROFILES[fmt][group]  this format deviates
        <- INGEST_USETYPES[name]      this ENTRY POINT deviates (only when one is named)
          <- caller overrides         this request deviates

Three of the four for an upload, which names no entry point. The usetype layer exists for
``POST /ingest``, whose caller selects one by name; see :class:`Usetype` for what a name is
still allowed to mean once ``probe`` decides the format.

A parameter belongs to the TASK that reads it, not to the format that fed it. The structure
handlers consume text, an outline and page offsets and know nothing about what produced
them, which is exactly what lets 11.3 add entry points without rewriting them — so their
chunking parameters cannot be a property of ``pdf``. What a format gets to say is where it
DEVIATES, and today no format does, so :data:`INGEST_PROFILES` is empty.

The keying of that middle layer is nonetheless the one thing that differs from path A, and
it matters: a profile is keyed by FORMAT, not by usetype. Path A asks the caller to declare
what the content is and runs the pipeline registered under that name. Path B never asks —
``probe`` reads the bytes and reports a format, and every decision after that is made from
what was measured rather than from what was claimed.

**Options are namespaced, not free-form.** Every layer maps a GROUP to that group's
parameters, where a group names the parameters one kind of task takes and these tables say
what they are. Six groups: ``structure`` (the chunking parameters the two structure rungs
share), ``facts``, ``embed``, ``sheet_profile`` and ``sheet_records``, each named by a
row's ``TaskRow.params_key``; and ``rollup``, which ``jmfts_core.rollup_tasks`` reads at
the settling boundary. A rollup task has no ``TaskRow`` — nothing probe measures decides it
(5.4) — so that one group is not reachable from Part 4's table, and the asymmetry is the
shape of the pipeline rather than a gap. A task that gains parameters gains a group;
nothing else changes.

**Three of those six are new in ``SPRINT_JOBS.md`` Phase 3, and they are not new knobs.**
``embed``, ``sheet_profile`` and ``sheet_records`` were literal ``params`` dicts on module
constants — ``EMBED_CHUNK_SPEC``, ``PROFILE_SHEET_SPEC``, ``EXTRACT_SHEET_SPEC`` — because
the tasks they belonged to were scoped to a node that does not exist at probe time and so
could not be rows of Part 4's table. Phase 3 gave a rule a scope, the three became rows,
and their parameters arrived here because that is where a row's parameters come from. Part
0 measured what the old shape cost: ``max_rows``, ``with_cell_notes`` and ``sketch_columns``
were not reachable from any caller, so ``sheet_tasks``' claim that "a re-ingest that raises
the ceiling is a different request" described a fingerprint no request could move.

Because the defaults belong to the group, **every group resolves for every format**. There
is no document that can reach a task whose parameters nobody set, and therefore nothing
anywhere has to decide what to do about one.

**An override naming something that does not exist RAISES.** ``_resolve_stages``, in the
pipeline Part 15 deleted, silently ignored an override naming a stage it did not have, and
that was the one thing here that deliberately departed from it. A caller who writes ``max_token``
and gets 120 has been told nothing: the run does something other than what was asked and
reports success, which is precisely the swallowed failure the project's Fail Early rule
exists to prevent. It is also what 11.2's ``EXPLAIN`` rests on — a plan that plausibly
reports options the run will not use is a wrong answer, not a partial one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from jmfts_core.chunking import ChunkStrategy

#: The parameters the structure rungs chunk their leaves with — the ``structure`` group's
#: defaults. A named constant rather than a literal inside :data:`TASK_PARAM_DEFAULTS`
#: because ``structure_tasks`` reads it too, and because the measurement below is the reason
#: for these particular numbers and has to travel with the value it justifies.
#:
#: ``plan_after_probe`` copies the resolved group onto the queue row, which is what makes
#: these part of the task's ``param_fingerprint`` and therefore what makes 6.1's "enqueue
#: only the difference" able to notice that they changed.
#:
#: ``sentence_packed`` at 120 words is a measurement, not a preference. Over 25 extracted
#: papers through the shipped ``chunk_text``, the share of leaf nodes too short to carry a
#: retrievable idea (under 8 words) is 21.4% for ``paragraph``, 9.7% for ``sentence``,
#: 0.3% at 60 words packed and 0.1% at 120. Node counts move the other way — 8,887 /
#: 10,196 / 5,253 / 2,784 — so 120 is where the short-node tail has flattened and the node
#: count is still falling.
STRUCTURE_CHUNK_PARAMS: dict = {
    "chunk_strategy": "sentence_packed",
    "max_tokens": 120,
    "min_chunk_length": 20,
}


#: The ``rollup`` group: what ``jmfts_core.rollup_tasks`` segments and summarizes with.
#:
#: ``max_children`` is a BOUND, not a measurement, and it is the one number here that is
#: not derived from anything. It says how wide a node may be before PELT is asked to deepen
#: it. The obvious alternative trigger — "the children no longer fit one LLM call" — was
#: rejected once the appliance's model was measured at ~190k tokens of context: almost no
#: single document reaches that, so a context-driven trigger would never fire and the
#: hierarchy 11.4 describes would never be built. Fan-out is the real reason to segment,
#: because a node with a hundred flat children gives retrieval no mid-scale representation
#: whatever the model could swallow. 16 is chosen so that a node must be meaningfully wider
#: than ``max_segment`` before it is split at all; the measurement that would replace it is
#: the fan-out distribution over an ingested corpus against retrieval quality per level.
#:
#: ``penalty`` and ``min_segment`` are ``jmfts_core.segmentation``'s own parameters,
#: exposed because they decide the shape of every tree this produces. There is deliberately
#: no maximum segment size: ``enforce_segment_bounds`` can divide an oversized segment into
#: equal parts, and an equal part is a boundary the document does not have. An oversized
#: segment is handled by recursion instead — it becomes a container and its own settling
#: walk segments it again.
#:
#: ``llm_model`` empty means the configured default (``Settings.effective_llm_model``).
ROLLUP_PARAMS: dict = {
    "max_children": 16,
    "penalty": 1.0,
    "min_segment": 3,
    "llm_model": "",
}


#: The ``facts`` group: whether to extract knowledge triples from a document's leaves, and
#: which model does it. ``SPRINT_JOBS.md`` 15.4 S6, ``INGEST_SPEC.md`` 11.4.
#:
#: **``enabled`` is False here and True on the four text usetypes, and the split is the
#: design.** Path A's ``extract_facts`` stage was on by default for ``conversation``,
#: ``markdown``, ``raw`` and ``transcript``, and off for the three ``wiki:`` entry points.
#: An UPLOAD names no usetype and has never had fact extraction at all, so a default of
#: True here would put an LLM call over every chunk of every file anybody uploads — a cost
#: nobody asked for, arrived at by porting a per-usetype default into a group default.
#:
#: ``llm_model`` is the ``facts`` task's own, not a second spelling of ``rollup``'s. A
#: parameter belongs to the task that reads it (see the module docstring), and summarizing
#: and extracting are different tasks that a deployment may well want on different models.
#: ``POST /ingest``'s single ``llm_model`` field sets both, because that field predates the
#: split and means "whichever model answers".
#: ``max_facts``, ``confidence_threshold`` and ``include_summaries`` are
#: ``extract_facts``'s own arguments, exposed because ``POST /conversations/ingest`` has
#: carried all three on its request since it shipped and they have to land somewhere real.
#: ``None`` means the appliance default (``JMFTS_EXTRACTION_MAX_FACTS`` and
#: ``JMFTS_EXTRACTION_CONFIDENCE_THRESHOLD``), which is a documented meaning rather than a
#: missing value: a caller who names neither is declining to deviate from what the
#: deployment configured.
#: ``min_characters`` is a GUARD's right side, not a parameter the handler reads, and it is
#: the first option that is one. ``TASK_ROWS``' ``extract:facts`` row excludes itself on
#: ``char_count < options.facts.min_characters``, so a caller sets the floor rather than
#: waiting for a literal in that table to be edited (``SPRINT_JOBS.md`` 4.4 point 2, 14.2).
#:
#: **0 is the default and it means every document**, which is the behaviour this option
#: replaced nothing to get: before it, no size decided whether facts were extracted. A
#: default above zero would be this appliance inventing a threshold nobody measured, on a
#: pattern only the ``text`` prober even emits.
FACTS_PARAMS: dict = {
    "enabled": False,
    "llm_model": "",
    "max_facts": None,
    "confidence_threshold": None,
    "include_summaries": True,
    "min_characters": 0,
}


#: The ``embed`` group: which vector path one leaf's ``embed`` task takes.
#:
#: It was a literal ``params`` dict on a module constant (``EMBED_CHUNK_SPEC``) until
#: Phase 3, which is to say it was not reachable from any caller. ``embed`` is a rule now
#: (``SPRINT_JOBS.md`` 4.1), and a rule's parameters come from a group like every other
#: task's — so this is what the constant's dict became rather than a knob added on top.
#:
#: ``with_tokens`` true is the default because a chunk is exactly the node the token/maxsim
#: path exists for: it is a leaf, its text is its own, and the chunker bounded it to the
#: token window. Setting it false is a real request with a real cost — the leaves of that
#: document get a document vector and no token vectors, so late interaction has nothing to
#: score them with — and it is now a request somebody can make and see in ``EXPLAIN``,
#: instead of a number only the source could change.
EMBED_PARAMS: dict = {"with_tokens": True}


#: How many rows one ``extract:sheet`` turns into nodes before it FAILS the task.
#: ``INGEST_SPEC.md`` 6.6 asks for exactly this — "a named limit that fails the task, not a
#: silent truncation" — because a sheet whose first ten thousand rows became nodes is
#: indistinguishable, from anywhere downstream, from a sheet that had ten thousand rows.
#:
#: The number is a CEILING, not a measurement. It is round on purpose so that it does not
#: read as something that was counted, and it clears 8.4's own worked example (1,284 rows)
#: by an order of magnitude. Raising it is a task parameter; what it costs is one node and
#: one embedding per row.
DEFAULT_MAX_ROW_NODES = 10_000


#: The ``sheet_profile`` group: what ``profile:sheet`` measures with. ``INGEST_SPEC.md``
#: 8.3.
#:
#: ``true`` is the default because a column with no sketch is invisible to 8.6's containment
#: search whatever its cardinality — see :mod:`jmfts_core.sketch`.
SHEET_PROFILE_PARAMS: dict = {"sketch_columns": True}


#: The ``sheet_records`` group: what ``extract:sheet`` materialises cells with.
#: ``INGEST_SPEC.md`` 8.4.
#:
#: **TWO GROUPS RATHER THAN ONE ``sheet`` GROUP, and the reason is 6.1's diff.** Both
#: per-sheet tasks are rules now, and a rule's resolved group lands in its queue row's
#: ``param_fingerprint``. Sharing one group would move ``profile:sheet``'s fingerprint every
#: time a caller changed ``max_rows`` — so a re-ingest that only raises the row ceiling
#: would re-measure every sheet as well, which is the opposite of what
#: ``sheet_tasks.py``'s own claim about that fingerprint says. The two names match the two
#: modules that read them, :mod:`jmfts_core.sheet_profile` and :mod:`jmfts_core.sheet_records`.
SHEET_RECORDS_PARAMS: dict = {
    "max_rows": DEFAULT_MAX_ROW_NODES,
    "with_cell_notes": True,
}


def _positive_int(value: Any) -> Optional[str]:
    """A whole number of tokens or characters. ``bool`` is rejected on purpose.

    ``isinstance(True, int)`` is true in Python, so a caller who sent ``max_tokens: true``
    through JSON would otherwise get a chunker packing to one token. Zero and negatives
    are refused for the same reason a misspelled key is: ``chunk_text`` would return
    something, and what it returned would not be what anybody asked for.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return f"expected an integer, got {type(value).__name__}"
    if value < 1:
        return f"expected a positive integer, got {value}"
    return None


def _positive_number(value: Any) -> Optional[str]:
    """A positive real. PELT's penalty is a BIC-style weight, not a count.

    ``bool`` is rejected for the same reason it is above, and zero with it: a penalty of
    zero puts a changepoint between every pair of children, which ``enforce_segment_bounds``
    would then merge back, so the run would be expensive and produce nothing.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return f"expected a number, got {type(value).__name__}"
    if value <= 0:
        return f"expected a positive number, got {value}"
    return None


def _non_negative_int(value: Any) -> Optional[str]:
    """A whole number of zero or more. A FLOOR, where :func:`_positive_int` counts things.

    Zero is the meaningful value here rather than the degenerate one — ``min_characters: 0``
    is "every document", which is what a floor nobody set has to mean. ``bool`` is rejected
    for the reason it is rejected above: ``min_characters: true`` is a mistake, and reading
    it as 1 would put a threshold on a caller who was trying to set a flag.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return f"expected a whole number, got {type(value).__name__}"
    if value < 0:
        return f"expected zero or more, got {value}"
    return None


def _optional_positive_int(value: Any) -> Optional[str]:
    """A positive whole number, or ``None`` for "whatever the deployment configured"."""
    if value is None:
        return None
    return _positive_int(value)


def _unit_interval(value: Any) -> Optional[str]:
    """A confidence in ``[0, 1]``, or ``None`` for the configured default.

    Zero is legal here and is not the "nothing would survive" case zero is elsewhere: a
    threshold of zero keeps every triple the model produced, which is a real thing to ask
    for when tuning extraction.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return f"expected a number, got {type(value).__name__}"
    if not 0.0 <= value <= 1.0:
        return f"expected a confidence between 0 and 1, got {value}"
    return None


def _boolean(value: Any) -> Optional[str]:
    """A real ``bool``. An integer is refused rather than read for truthiness.

    ``enabled: 1`` from a JSON caller is almost certainly a mistake, and reading it as True
    would run an LLM over a document because somebody typed the wrong literal. The other
    checks here reject ``bool`` where an integer is wanted, for the mirror-image reason.
    """
    if not isinstance(value, bool):
        return f"expected a boolean, got {type(value).__name__}"
    return None


def _model_name(value: Any) -> Optional[str]:
    """A model name, or the empty string meaning "whichever one is configured".

    The empty string is a real value here rather than a missing one: it says the caller
    declines to pin a model, which is a different statement from naming the current default
    and freezing it into the node's ``param_fingerprint``.
    """
    if not isinstance(value, str):
        return f"expected a string, got {type(value).__name__}"
    return None


def _chunk_strategy(value: Any) -> Optional[str]:
    """A member of :class:`~jmfts_core.chunking.ChunkStrategy`, by value.

    Checked here rather than left to ``ChunkStrategy(...)`` in the handler because the
    handler runs in a worker, minutes later: the same mistake is a 400 on the upload if it
    is caught here and a failed task on a node that already exists if it is not.
    """
    try:
        ChunkStrategy(value)
    except ValueError:
        legal = [s.value for s in ChunkStrategy]
        return f"expected one of {legal}, got {value!r}"
    return None


#: What a legal value is, per group, per option. This table is where an option is
#: DECLARED: a name absent from here is not an option, at any of the three layers. Each
#: check returns ``None`` for a value it accepts and a phrase describing the problem for
#: one it does not, so :func:`resolve_options` can name the option and say what was wrong
#: with it in a single sentence.
OPTION_CHECKS: dict[str, dict[str, Callable[[Any], Optional[str]]]] = {
    "structure": {
        "chunk_strategy": _chunk_strategy,
        "max_tokens": _positive_int,
        "min_chunk_length": _positive_int,
    },
    "rollup": {
        "max_children": _positive_int,
        "penalty": _positive_number,
        "min_segment": _positive_int,
        "llm_model": _model_name,
    },
    "facts": {
        "enabled": _boolean,
        "llm_model": _model_name,
        "max_facts": _optional_positive_int,
        "confidence_threshold": _unit_interval,
        "include_summaries": _boolean,
        "min_characters": _non_negative_int,
    },
    "embed": {"with_tokens": _boolean},
    "sheet_profile": {"sketch_columns": _boolean},
    # `_positive_int` is the check the four measured consequences in `SPRINT_JOBS.md` Part 0
    # named as absent: `max_rows` was read as `int(params.get("max_rows", ...))`, and
    # `int(True)` is 1, so `max_rows: true` from a JSON caller would have written one record
    # node for a whole sheet. It was LATENT rather than live only because nothing could feed
    # the task anything at all. Making the knob reachable is what makes the check load-bearing.
    "sheet_records": {"max_rows": _positive_int, "with_cell_notes": _boolean},
}

#: A group's own defaults — the TASK's parameters, valid wherever that task runs.
#:
#: This is the layer that decides what an option IS, and it is keyed by group rather than
#: by format because that is where the values belong. ``STRUCTURE_CHUNK_PARAMS`` describes
#: how to chunk prose; it does not describe PDF. The structure handlers consume text, an
#: outline and page offsets and know nothing about what produced them — that is what makes
#: them reusable across the entry points 11.3 adds — so their parameters cannot be a
#: property of the format that happened to feed them. The measurement behind the numbers
#: was taken on a PDF corpus because that is the corpus that existed, which is a fact about
#: the evidence rather than about the scope of the value.
#:
#: A group here is complete: every option :data:`OPTION_CHECKS` declares has a default,
#: enforced by :func:`_check_option_tables` at import. That completeness is what lets
#: ``plan_after_probe`` put full parameters on every queue row without asking whether the
#: format was one somebody remembered to list.
TASK_PARAM_DEFAULTS: dict[str, dict] = {
    "structure": STRUCTURE_CHUNK_PARAMS,
    "rollup": ROLLUP_PARAMS,
    "facts": FACTS_PARAMS,
    "embed": EMBED_PARAMS,
    "sheet_profile": SHEET_PROFILE_PARAMS,
    "sheet_records": SHEET_RECORDS_PARAMS,
}

#: Where a FORMAT deviates from the task defaults above: format -> group -> the options it
#: wants different. An override set, not a full profile — a format states its differences
#: and inherits everything it is silent about.
#:
#: **Empty, and that is the honest state of the appliance**, not a gap. No format currently
#: needs to chunk differently from the way the structure task chunks by default, so there
#: is nothing to write here; a format whose extractor emits much shorter regions than a
#: paper's would be the first entry, and it would be one line. Deliberately not "pdf: the
#: numbers the structure task already uses", which would say that PDFs are special when
#: what is true is that PDFs are what got measured.
#:
#: None of this is the fallback the Fail Early rule forbids. A fallback substitutes for a
#: value that went missing; a task default IS the value, and a profile is a format asking
#: for a different one. Nothing is guessed at either layer, and an option nobody declared
#: still raises at whichever layer names it.
INGEST_PROFILES: dict[str, dict[str, dict]] = {}


# ---------------------------------------------------------------------------
# Usetypes — path A's seven named pipelines, as data (SPRINT_JOBS.md 15.4 S1)
# ---------------------------------------------------------------------------

#: ``content`` is the document. The three usetypes below it name where to FETCH the
#: document instead, and the string a caller sends is an identifier rather than text.
#:
#: The distinction was a module-private set in the deprecated ``jmfts_core.pipeline``
#: (``_SOURCE_FETCH_PIPELINES``) that decided whether to record a source URL on the node.
#: It is here because it is a property of the entry point, and because 15.4 S8 turned each
#: of the three into a queue task that has to be selected from something.
SOURCE_CONTENT = "content"
SOURCE_URL = "url"
SOURCE_ARXIV = "arxiv"
SOURCE_PATH = "path"

SOURCE_KINDS = (SOURCE_CONTENT, SOURCE_URL, SOURCE_ARXIV, SOURCE_PATH)


@dataclass(frozen=True)
class Usetype:
    """One entry point a caller may name in ``POST /ingest``'s ``usetype`` field.

    **A usetype is not a format and does not claim to be one.** That is the whole
    difference between the two ingest paths and the reason this is a fourth table rather
    than seven more entries in :data:`INGEST_PROFILES`. Path A asks the caller to declare
    what the content is and runs the stage list registered under that name; path B hands
    the bytes to ``probe``, which measures a format and a pattern set, and every decision
    after that is made from what was measured. A usetype survives the migration as two
    facts probe cannot supply — where the bytes come from (:attr:`source`) and what the
    caller wants tuned differently (:attr:`options`) — and it contributes nothing else.

    That is why ``markdown`` and ``raw`` carry no statement about headings. Under path A
    the name chose the splitter. Under path B ``probe`` reports ``has_headings`` or does
    not, and Part 4's table sends the document to ``structure:declared`` or
    ``structure:inferred`` on that measurement, whatever the caller called it.
    """

    name: str
    description: str
    #: One of :data:`SOURCE_KINDS`. What ``IngestRequest.content`` holds for this usetype.
    source: str
    #: Overrides this entry point applies, group -> options, in exactly the shape a
    #: caller's own overrides take and validated to the same standard by
    #: :func:`_check_usetype_table` at import.
    options: dict[str, dict] = field(default_factory=dict)


#: The chunking every usetype below asked for under path A: 200 tokens, 20-character
#: floor, and a strategy that differs per entry point.
#:
#: **These are path A's historical numbers, carried across unchanged, and they are not the
#: measured ones.** :data:`STRUCTURE_CHUNK_PARAMS` is ``sentence_packed`` at 120 because
#: that is where the short-node tail flattened over 25 papers; these predate that
#: measurement. Preserving them is deliberate for the duration of the migration — S5 moves
#: callers onto the queue and a chunk size that moved in the same commit would make any
#: change in the resulting tree impossible to attribute. Whether a usetype should override
#: chunking AT ALL once path A is gone is a question for after S9, and the answer may well
#: be that these three overrides delete themselves.
_PATH_A_CHUNKING = {"max_tokens": 200, "min_chunk_length": 20}


def _chunking(strategy: str) -> dict[str, dict]:
    """One ``structure`` override set, spelled once per strategy rather than per usetype."""
    return {"structure": {"chunk_strategy": strategy, **_PATH_A_CHUNKING}}


#: Path A had ``extract_facts`` enabled for ``conversation``, ``markdown``, ``raw`` and
#: ``transcript``, and disabled for the three ``wiki:`` entry points. Carried across
#: unchanged (``SPRINT_JOBS.md`` 15.4 S6). On an appliance with no LLM configured the task
#: reports itself skipped with that reason rather than failing — a blank ``JMFTS_LLM_*`` is
#: a supported configuration, and failing an ingest over it would be manufacturing a
#: problem rather than reporting one.
_FACTS_ON = {"facts": {"enabled": True}}


#: The seven entry points, and the ONE list of them. There was a second — the deprecated
#: ``PipelineDefinition`` registry in ``jmfts_core/pipeline.py``, held in step by an
#: import-time check for the length of the migration — and ``SPRINT_JOBS.md`` 15.4 S9
#: deleted it.
#:
#: Four things path A's stage table said are deliberately NOT here:
#:
#: * ``parse`` and ``chunk`` as separate stages. Under path B ``extract:text`` and the
#:   structure rungs are tasks with their own handlers, and which of them runs is decided
#:   by ``plan_after_probe`` from the measured patterns, not by a name.
#: * ``summarize`` off for the three ``wiki:`` entries. Path A's ``summarize`` stage was
#:   RAPTOR clustering — expensive, LLM-driven, and optional. Path B's ``summarize`` task
#:   is what gives a container node its content and its vectors, and it calls an LLM only
#:   when the children overflow the embedding window. Turning it off would leave every
#:   container built by ``structure:semantic`` unretrievable, so the two switches are not
#:   the same switch and translating one into the other would be wrong.
#: * ``extract_facts`` off for the same three. That one IS translatable and is S6's, which
#:   adds the ``extract:facts`` task, the ``rollup.extract_facts`` option that gates it,
#:   and the four entries that want it on. Declaring the option here, before a task reads
#:   it, would accept ``rollup.extract_facts`` from a caller and do nothing with it.
#: * ``conversation``'s ``segment`` stage, disabled with its own ``min_segment`` and
#:   ``max_segment``. Path B segments every node wider than ``rollup.max_children``, for
#:   every format, so there is no per-usetype switch to carry — and ``min_segment`` is
#:   already a ``rollup`` option any caller can set.
INGEST_USETYPES: dict[str, Usetype] = {
    u.name: u
    for u in (
        Usetype(
            name="conversation",
            description=(
                "Conversation ingestion: parse JSONL/messages, chunk by turn, "
                "embed, optionally RAPTOR + fact extraction"
            ),
            source=SOURCE_CONTENT,
            # No chunking override: a conversation's leaves are its turns, and a turn is a
            # boundary the transcript states rather than one a strategy finds.
            options=_FACTS_ON,
        ),
        Usetype(
            name="markdown",
            description=(
                "Markdown ingestion: structural split on headings, chunk sections, "
                "embed, optionally RAPTOR + fact extraction"
            ),
            source=SOURCE_CONTENT,
            options={**_chunking("paragraph"), **_FACTS_ON},
        ),
        Usetype(
            name="raw",
            description=(
                "Raw text ingestion: sentence chunk, embed, optionally RAPTOR + fact extraction"
            ),
            source=SOURCE_CONTENT,
            options={**_chunking("sentence"), **_FACTS_ON},
        ),
        Usetype(
            name="transcript",
            description=(
                "Voice transcript ingestion: sentence chunk with transcript metadata, "
                "embed, optionally RAPTOR + fact extraction"
            ),
            source=SOURCE_CONTENT,
            options={**_chunking("sentence"), **_FACTS_ON},
        ),
        Usetype(
            name="wiki:url",
            description="Fetch a URL, convert HTML→markdown, then run markdown pipeline.",
            source=SOURCE_URL,
            options=_chunking("paragraph"),
        ),
        Usetype(
            name="wiki:arxiv",
            description="Fetch an arXiv paper (metadata + PDF), convert to markdown.",
            source=SOURCE_ARXIV,
            options=_chunking("paragraph"),
        ),
        Usetype(
            name="wiki:pdf",
            description="Convert a local PDF (file path) to markdown.",
            source=SOURCE_PATH,
            options=_chunking("paragraph"),
        ),
    )
}


def get_usetype(name: str) -> Optional[Usetype]:
    """The entry point registered under ``name``, or ``None``.

    Returns the declaration, NOT the options a request under it resolves to — that is
    :func:`resolve_usetype_options`, which merges this entry's overrides under the
    caller's.
    """
    return INGEST_USETYPES.get(name)


def resolve_usetype_options(
    name: str, fmt: str, overrides: Optional[dict] = None
) -> dict[str, dict]:
    """The options a document ingested through entry point ``name`` runs with.

    Four layers now, and the usetype sits between the format and the request::

        TASK_PARAM_DEFAULTS[group]         the task's parameters
          <- INGEST_PROFILES[fmt][group]   this format deviates
            <- INGEST_USETYPES[name]       this entry point deviates
              <- overrides                 this request deviates

    Under the entry point rather than over it, because a usetype is a default a caller
    selected by name and an override is one they typed. A request that names
    ``usetype=raw`` and ``structure.chunk_strategy=paragraph`` gets ``paragraph``.

    Raises ``ValueError`` for an unknown usetype, and for everything
    :func:`resolve_options` raises it for.
    """
    usetype = INGEST_USETYPES.get(name)
    if usetype is None:
        raise ValueError(
            f"unknown ingest usetype {name!r}; the registered entry points are "
            f"{sorted(INGEST_USETYPES)}"
        )
    resolved = resolve_options(fmt, usetype.options)
    if overrides:
        _apply(resolved, overrides, fmt=fmt, source="ingest options")
    return resolved


def get_profile(fmt: str) -> dict[str, dict]:
    """The options ``fmt`` wants different from the task defaults. Empty for most formats.

    Read-only, and NOT the options a document of this format is ingested with — that is
    :func:`resolve_options`, which starts from the task defaults this deviates from.
    """
    return INGEST_PROFILES.get(fmt, {})


def _apply(resolved: dict[str, dict], overrides: dict, *, fmt: str, source: str) -> None:
    """Merge one layer of ``overrides`` into ``resolved`` in place, validating as it goes.

    Shared by the two layers above the defaults so that a profile is held to exactly the
    standard a request is. A registry entry that names an option nobody declared is the
    same mistake as a caller who mistypes one, and finding it at import (where
    :func:`_check_option_tables` runs this over every profile) rather than on the first
    upload of that format is the whole reason to spend the indirection.
    """
    if not isinstance(overrides, dict):
        raise ValueError(
            f"{source} must be a mapping of option group to parameters, got "
            f"{type(overrides).__name__}"
        )

    for group, values in overrides.items():
        if group not in resolved:
            raise ValueError(
                f"unknown option group {group!r} in {source} for format {fmt!r}; the "
                f"declared groups are {sorted(resolved)}"
            )
        if not isinstance(values, dict):
            raise ValueError(
                f"option group {group!r} takes a mapping of option name to value, got "
                f"{type(values).__name__}"
            )
        checks = OPTION_CHECKS[group]
        for name, value in values.items():
            if name not in resolved[group]:
                raise ValueError(
                    f"unknown option {group}.{name}; options in {group!r} are "
                    f"{sorted(resolved[group])}"
                )
            problem = checks[name](value)
            if problem is not None:
                raise ValueError(f"option {group}.{name}: {problem}")
            resolved[group][name] = value


def resolve_options(fmt: str, overrides: Optional[dict] = None) -> dict[str, dict]:
    """The options one document is ingested with. Three layers, resolved in order::

        TASK_PARAM_DEFAULTS[group]        the task's parameters — valid wherever it runs
          <- INGEST_PROFILES[fmt][group]  this format deviates
            <- overrides                  this request deviates

    The ONE place options are merged. ``plan_after_probe`` calls it, so does the upload
    that records them, so will 11.2's ``EXPLAIN`` — and the result is always a COMPLETE set
    rather than a diff, which is what makes it safe to call twice. Resolving an
    already-resolved set is the identity, and that is what lets ``probe`` re-derive the plan
    from what the upload durably recorded on the node.

    Every group always resolves, for every format, because the task declares it. So there
    is no such thing as a document that reaches a task whose parameters nobody has set —
    an option is missing only if a group was declared with no defaults, and that cannot
    survive import.

    ``overrides=None`` means the layers below it, which is a documented meaning rather than
    a fallback: no override is a caller declining to deviate.

    Raises ``ValueError`` — mapped to a 400 by the service — for an unknown group, an
    unknown option, or a value of the wrong type. Never silently drops one: see the module
    docstring for why this is the deliberate difference from the deleted ``_resolve_stages``.
    """
    resolved = {group: dict(params) for group, params in TASK_PARAM_DEFAULTS.items()}
    profile = get_profile(fmt)
    if profile:
        _apply(resolved, profile, fmt=fmt, source="the format profile")
    if overrides:
        _apply(resolved, overrides, fmt=fmt, source="ingest options")
    return resolved


def _check_option_tables() -> None:
    """The three tables agree. Checked at import, because a disagreement is not survivable.

    A group declared in :data:`OPTION_CHECKS` with an incomplete set of defaults is the
    invariant that matters: it is what would let a task be planned with a parameter nobody
    set, and the handler would then supply its own number and the attempt log would record
    that number as the one that was asked for. Checking it here is what removes the
    question from every caller — ``plan_after_probe`` never has to ask whether this
    format's parameters exist.

    Profiles are then run through the real merge, so a registry entry naming an option that
    does not exist fails on import rather than on the first upload of that format.
    """
    for group, checks in OPTION_CHECKS.items():
        defaults = TASK_PARAM_DEFAULTS.get(group)
        if defaults is None:
            raise ValueError(
                f"option group {group!r} is declared in OPTION_CHECKS and has no entry in "
                "TASK_PARAM_DEFAULTS; a group's options must have defaults, or a task "
                "naming it would be planned with parameters nobody set"
            )
        undefaulted = sorted(set(checks) - set(defaults))
        undeclared = sorted(set(defaults) - set(checks))
        if undefaulted or undeclared:
            raise ValueError(
                f"option group {group!r} disagrees between its tables: declared options "
                f"with no default {undefaulted}, defaults for undeclared options "
                f"{undeclared}"
            )

    for fmt in INGEST_PROFILES:
        resolve_options(fmt)


def _check_usetype_table() -> None:
    """Every usetype names a real source kind and legal overrides. Checked at import.

    The overrides go through the real merge, against the empty format — which resolves no
    profile and so tests exactly the layer being checked. A usetype whose overrides name an
    option nobody declared would otherwise be found on the first request that selected it,
    and the caller would be told about a table they did not write.
    """
    for name, usetype in INGEST_USETYPES.items():
        if name != usetype.name:
            raise ValueError(
                f"usetype registered under {name!r} calls itself {usetype.name!r}; the key "
                "is what a caller sends and the two cannot differ"
            )
        if usetype.source not in SOURCE_KINDS:
            raise ValueError(
                f"usetype {name!r} declares source {usetype.source!r}; the kinds are "
                f"{list(SOURCE_KINDS)}"
            )
        resolved = {group: dict(params) for group, params in TASK_PARAM_DEFAULTS.items()}
        _apply(resolved, usetype.options, fmt="", source=f"the {name!r} usetype")


_check_option_tables()
_check_usetype_table()
