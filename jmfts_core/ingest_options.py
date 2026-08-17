"""Ingest options for the queued pipeline: task defaults, per-format profiles, and the one
merge that resolves them. ``INGEST_SPEC.md`` 11.2.

Path B — upload, ``probe``, and everything Part 4's table schedules after it — had no
configuration surface at all. What a document got was decided entirely by its bytes, and
the one set of tunable numbers in it was a module constant. Path A, the pipeline 11.1
deprecates, has been configurable per call since it shipped. The newer pipeline being the
less controllable one is what this module closes.

It is deliberately shaped like ``PipelineDefinition``: a registry of named defaults, and
one merge of caller overrides over them. That is so path A's callers have something to
migrate onto at cut-over rather than a second unfamiliar model to learn.

**Three layers, and which is which is the design.**

::

    TASK_PARAM_DEFAULTS[group]        the task's parameters — valid wherever it runs
      <- INGEST_PROFILES[fmt][group]  this format deviates
        <- caller overrides           this request deviates

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
what they are. Two groups today: ``structure``, the chunking parameters the two structure
rungs share, named by their ``TaskRow.params_key``; and ``rollup``, which
``jmfts_core.rollup_tasks`` reads at the settling boundary. A rollup task has no
``TaskRow`` — nothing probe measures decides it (5.4) — so the group is not reachable from
Part 4's table, and that asymmetry is the shape of the pipeline rather than a gap. A task
that gains parameters gains a group; nothing else changes.

Because the defaults belong to the group, **every group resolves for every format**. There
is no document that can reach a task whose parameters nobody set, and therefore nothing
anywhere has to decide what to do about one.

**An override naming something that does not exist RAISES.** ``_resolve_stages`` in the
deprecated pipeline silently ignores an override naming a stage it does not have, and that
is the one thing here that deliberately departs from it. A caller who writes ``max_token``
and gets 120 has been told nothing: the run does something other than what was asked and
reports success, which is precisely the swallowed failure the project's Fail Early rule
exists to prevent. It is also what 11.2's ``EXPLAIN`` rests on — a plan that plausibly
reports options the run will not use is a wrong answer, not a partial one.
"""

from __future__ import annotations

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
    docstring for why this is the deliberate difference from ``_resolve_stages``.
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


_check_option_tables()
