"""Guards with operators, and the vocabulary they read. ``SPRINT_JOBS.md`` Phase 4.

Three things land together and the third is what keeps the first two honest.

**A term** (4.4) is ``name op value``, and ``op`` may be absent — which is TRUTHINESS and is
not ``= True``. Every guard in ``TASK_ROWS`` was a bare name before this phase and most of
them still are; the operators exist for the counts probe was already measuring and nothing
was reading.

**An option reference** on a term's right side is what makes a threshold a caller's rather
than a source edit. ``enabled_by`` folded into a term when that arrived, because a boolean
option that must be true is the degenerate case of one.

**The vocabulary** is :data:`GUARDABLE_PATTERNS`, and it is closed. ``matched.patterns`` is
an open namespace by design, and a guard is the one reader for which an unknown name and a
false one are indistinguishable: ``has_hedings`` would type-check, plan, and stand its row
down forever with a sentence naming a pattern nobody measures.

WHAT IS DELIBERATELY NOT HERE is a test of ``requires`` and ``forbids`` as one expression.
They are two unknown policies rather than a weak boolean algebra, and the class below that
asserts the asymmetry is the reason: flattened, ``structure:inferred``'s guard would read
``not (has_heading_styles = true)``, and every ``.docx`` would ingest to nothing.
"""

from __future__ import annotations

import dataclasses

import pytest

from jmfts_core.ingest_options import TASK_PARAM_DEFAULTS, resolve_options
from jmfts_core.ingest_tasks import (
    CHAR_COUNT,
    GUARDABLE_PATTERNS,
    HAS_HEADING_STYLES,
    HAS_TEXT_LAYER,
    IS_DAMAGED,
    OP_EQ,
    OP_GT,
    OP_LT,
    PATTERNS_NOT_PROBED,
    TASK_EXTRACT_FACTS,
    TASK_EXTRACT_TEXT,
    TASK_ROWS,
    TASK_STRUCTURE_DECLARED,
    TASK_STRUCTURE_INFERRED,
    Term,
    _check_task_rows,
    option,
    plan_after_probe,
    term,
)
from tests.corpus.vocabulary import specimens

FACTS_ON = {"facts": {"enabled": True}}


def _row(task: str):
    return next(row for row in TASK_ROWS if row.task == task)


def _plan(fmt: str, patterns: dict, overrides: dict | None = None):
    return plan_after_probe(fmt, patterns, resolve_options(fmt, overrides))


def _eligible(fmt: str, patterns: dict, overrides: dict | None = None) -> list[str]:
    return [spec.task_type for spec in _plan(fmt, patterns, overrides).eligible]


def _replace(task: str, **fields):
    """The shipped table with one row replaced. For the audit tests, which need a bad row."""
    broken = dataclasses.replace(_row(task), **fields)
    return [broken if r.task == task else r for r in TASK_ROWS]


# ---------------------------------------------------------------------------
# 1. The vocabulary — what a guard may name, and what it holds
# ---------------------------------------------------------------------------


class TestTheGuardVocabulary:
    """A closed list over an open namespace, and the audit that keeps it true."""

    def test_every_name_probe_does_not_emit_says_who_writes_it(self):
        """The one direction worth auditing. Probe measuring something no guard reads is
        not an error; a guard reading something NOTHING writes is one, unless the table
        says which task writes it instead."""
        from jmfts_core.probe import detect_format, probe_patterns

        probed: set[str] = set()
        for blobs in specimens().values():
            for data in blobs if isinstance(blobs, (list, tuple)) else [blobs]:
                patterns, _detail = probe_patterns(data, detect_format(data))
                probed.update(patterns)

        unexplained = sorted(set(GUARDABLE_PATTERNS) - probed - set(PATTERNS_NOT_PROBED))
        assert not unexplained, (
            f"{unexplained} are guardable and the installed probe emits none of them; a "
            "guard on a name nothing writes stands its row down forever, so PATTERNS_NOT_"
            "PROBED has to say who writes it instead"
        )

    def test_every_unprobed_name_is_guardable(self):
        """The other direction: an entry excusing a name no guard may read is describing
        a table that does not exist."""
        stale = sorted(set(PATTERNS_NOT_PROBED) - set(GUARDABLE_PATTERNS))
        assert not stale, f"{stale} are excused from probing and are not guardable"

    def test_the_two_unprobed_names_are_the_two_expected(self):
        """Named rather than counted, because they are two different facts. One is planned
        and unbuilt (OFFICE_SPEC Part 2); the other is written by `extract:text`, which
        means `extract:tables` is unreachable until something re-plans a node."""
        assert sorted(PATTERNS_NOT_PROBED) == ["has_heading_styles", "pages_with_tables"]

    def test_pages_with_tables_is_typed_as_the_list_it_is(self):
        """The case that proves a defaulted type wrong. `evidence.pattern_type` calls it a
        flag, because `matched.patterns` is a flag unless PATTERN_TYPES names it and probe
        never emits this one for the rule to be checked against."""
        from jmfts_core.evidence import TYPE_BOOL, TYPE_LIST, pattern_type

        assert GUARDABLE_PATTERNS["pages_with_tables"] == TYPE_LIST
        assert pattern_type("pages_with_tables") == TYPE_BOOL


# ---------------------------------------------------------------------------
# 2. The table audit — check 5, which is `_check_term`
# ---------------------------------------------------------------------------


class TestTheTermAudit:
    """Each of these is a row that would plan wrongly or raise inside planning.

    The check runs at import, so the shipped table already passes; what these assert is
    that it REFUSES the four mistakes, which a passing import cannot demonstrate.
    """

    def _with(self, rows, monkeypatch):
        monkeypatch.setattr("jmfts_core.ingest_tasks.TASK_ROWS", tuple(rows))
        return _check_task_rows

    def test_the_shipped_table_passes(self):
        _check_task_rows()

    def test_a_name_outside_the_vocabulary_is_refused(self, monkeypatch):
        """The typo. Under `matched.patterns`' own rule it is a well-typed boolean that
        never fires, which is the failure this list exists to make loud."""
        check = self._with(_replace(TASK_EXTRACT_TEXT, requires=("has_text_lyer",)), monkeypatch)
        with pytest.raises(ValueError, match="GUARDABLE_PATTERNS does not name"):
            check()

    def test_comparing_two_kinds_of_thing_is_refused(self, monkeypatch):
        """`char_count = true` plans cleanly and is never satisfied."""
        rows = _replace(TASK_EXTRACT_TEXT, requires=(term(CHAR_COUNT, OP_EQ, True),))
        check = self._with(rows, monkeypatch)
        with pytest.raises(ValueError, match="the same kind of thing"):
            check()

    def test_ordering_a_flag_is_refused(self, monkeypatch):
        """`has_text_layer > 3` would raise a TypeError inside planning, on the first real
        document of a format that measures it."""
        rows = _replace(TASK_EXTRACT_TEXT, requires=(term(HAS_TEXT_LAYER, OP_GT, True),))
        check = self._with(rows, monkeypatch)
        with pytest.raises(ValueError, match="the same kind of thing|are for counts"):
            check()

    def test_an_option_nobody_declares_is_refused(self, monkeypatch):
        """A guard's option reference is resolved by `resolve_options`, and a name nothing
        declares resolves to nothing."""
        rows = _replace(TASK_EXTRACT_TEXT, requires=(term(option("facts", "min_chars")),))
        check = self._with(rows, monkeypatch)
        with pytest.raises(ValueError, match="not a declared option"):
            check()

    def test_a_sentinel_whose_patterns_disagree_on_type_is_refused(self, monkeypatch):
        """A sentinel resolves per format and a comparison is checked once, so a sentinel
        holding a flag for one format and a count for another type-checks against whichever
        the audit looked at and fails on documents of the other."""
        monkeypatch.setitem(
            __import__("jmfts_core.ingest_tasks", fromlist=["x"]).SENTINEL_PATTERNS,
            "@declared_structure",
            {"docx": HAS_HEADING_STYLES, "text": CHAR_COUNT},
        )
        with pytest.raises(ValueError, match="agree on one type"):
            _check_task_rows()


# ---------------------------------------------------------------------------
# 3. Two unknown policies, pointed in opposite directions
# ---------------------------------------------------------------------------


class TestTheTwoUnknownPolicies:
    """4.4's table, asserted on the rows that read it both ways.

    `requires` on an unmeasured name blocks: the precondition cannot be confirmed.
    `forbids` on one does not: there is no evidence of a blocker. The same name, read by
    two rows with opposite polarity, is what makes a `.docx` ingest at all.
    """

    def test_the_same_unmeasured_name_stands_one_rung_down_and_lets_the_other_run(self):
        """`has_heading_styles` is `@declared_structure` for docx and nothing emits it."""
        plan = _plan("docx", {HAS_TEXT_LAYER: True})
        eligible = [spec.task_type for spec in plan.eligible]

        assert TASK_STRUCTURE_DECLARED not in eligible
        assert plan.not_applicable[TASK_STRUCTURE_DECLARED] == (
            "patterns.has_heading_styles was not measured"
        )
        assert TASK_STRUCTURE_INFERRED in eligible

    def test_a_docx_with_a_text_layer_still_ingests(self):
        """The regression this phase came closest to shipping. Flattening the two fields
        into one expression empties every .docx."""
        # `index:bm25` was the third name here until `SPRINT_0_6_0.md` Block B step 7 took
        # it out of `TASK_ROWS` and gave it to `IngestRollupPlanner`. `explain_plan` reads
        # that table and nothing else, so a rollup rung is not in the answer — see
        # `jmfts_core.index_tasks` for what that costs and `SPRINT_JOBS.md` Phase 7 for
        # where it ends. What this test is about is untouched: the two `.docx` fields.
        assert _eligible("docx", {HAS_TEXT_LAYER: True}) == [
            TASK_EXTRACT_TEXT,
            TASK_STRUCTURE_INFERRED,
        ]


# ---------------------------------------------------------------------------
# 4. The appliance's own guard — a literal, and a caller may not turn it off
# ---------------------------------------------------------------------------


class TestTheAppliancesOwnGuard:
    """`is_damaged` on `extract:text`, and the cascade that follows from it."""

    def test_a_damaged_pdf_extracts_nothing_and_says_why(self):
        plan = _plan("pdf", {HAS_TEXT_LAYER: True, "has_outline": True, IS_DAMAGED: True})

        assert plan.eligible == ()
        assert plan.not_applicable[TASK_EXTRACT_TEXT] == (
            "patterns.is_damaged is True, and extract:text does not run when it is True"
        )

    def test_the_rest_of_the_tree_reports_the_dependency_and_not_the_damage(self):
        """A cascade of no-ops is the correct outcome, and each rung's reason is its own.
        `structure:inferred` did not stand down because the file is damaged; it stood down
        because there will be no text."""
        plan = _plan("pdf", {HAS_TEXT_LAYER: True, "has_outline": True, IS_DAMAGED: True})

        assert plan.not_applicable[TASK_STRUCTURE_INFERRED] == (
            "extract:text is not eligible, and structure:inferred depends on it"
        )

    def test_an_undamaged_pdf_is_untouched(self):
        assert TASK_EXTRACT_TEXT in _eligible("pdf", {HAS_TEXT_LAYER: True})

    def test_a_format_that_does_not_measure_damage_is_untouched(self):
        """`is_damaged` is PDF-only. `forbids`' unknown policy is what keeps every .docx
        out of the way of a guard written for PDFs."""
        assert IS_DAMAGED not in _plan("docx", {HAS_TEXT_LAYER: True}).not_applicable
        assert TASK_EXTRACT_TEXT in _eligible("docx", {HAS_TEXT_LAYER: True})


# ---------------------------------------------------------------------------
# 5. The caller's guard — an option reference, and the phase's first customer
# ---------------------------------------------------------------------------


class TestTheCallersGuard:
    """`facts.min_characters`, which is what makes 14.2's promise true.

    A literal threshold in `TASK_ROWS` moves by a code change. This one moves through the
    three layers `resolve_options` already had, and the plan reports both the knob and the
    value it resolved to.
    """

    TEXT = {HAS_TEXT_LAYER: True, "has_headings": True}

    def test_a_short_document_is_excluded_and_the_reason_names_the_knob(self):
        plan = _plan(
            "text",
            {**self.TEXT, CHAR_COUNT: 120},
            {"facts": {"enabled": True, "min_characters": 5000}},
        )

        assert TASK_EXTRACT_FACTS not in [s.task_type for s in plan.eligible]
        assert plan.not_applicable[TASK_EXTRACT_FACTS] == (
            "patterns.char_count is 120, and extract:facts does not run when it is less "
            "than options.facts.min_characters (5000)"
        )

    def test_the_same_document_over_the_floor_runs(self):
        eligible = _eligible(
            "text",
            {**self.TEXT, CHAR_COUNT: 9000},
            {"facts": {"enabled": True, "min_characters": 5000}},
        )
        assert TASK_EXTRACT_FACTS in eligible

    def test_the_default_floor_excludes_nothing(self):
        """0 means every document, which is the behaviour this option replaced nothing to
        get: before it, no size decided whether facts were extracted."""
        assert TASK_PARAM_DEFAULTS["facts"]["min_characters"] == 0
        assert TASK_EXTRACT_FACTS in _eligible("text", {**self.TEXT, CHAR_COUNT: 1}, FACTS_ON)

    def test_a_format_that_measures_no_size_still_extracts_facts(self):
        """THE REASON THIS GUARD IS A `forbids`. Probe emits `char_count` for `text` alone,
        so required, the same threshold would stop fact extraction on every PDF, .docx and
        .pptx — 4.4's .docx regression in a second costume."""
        eligible = _eligible(
            "pdf",
            {HAS_TEXT_LAYER: True, "has_outline": True},
            {"facts": {"enabled": True, "min_characters": 5000}},
        )
        assert TASK_EXTRACT_FACTS in eligible


# ---------------------------------------------------------------------------
# 6. `enabled_by` as a term, and the ordering that survived the fold
# ---------------------------------------------------------------------------


class TestTheRequestIsAnsweredFirst:
    """A term about the REQUEST is answered ahead of the dependency gate.

    That ordering was `_disabled_reason`'s, and it survives `enabled_by` becoming an
    ordinary term because it was never about the field: it is about which kind of question
    a term asks.
    """

    def test_facts_off_says_so_rather_than_naming_the_rung_it_comes_after(self):
        """Both facts are true and only one of them is why. A caller who turned fact
        extraction off is owed the one they can act on."""
        plan = _plan("docx", {})

        assert plan.not_applicable[TASK_EXTRACT_FACTS] == (
            "options.facts.enabled is false, and extract:facts runs only when it is true"
        )

    def test_the_option_term_is_where_enabled_by_went(self):
        row = _row(TASK_EXTRACT_FACTS)
        assert not hasattr(row, "enabled_by")
        assert Term(option("facts", "enabled")) in row.requires


# ---------------------------------------------------------------------------
# 7. A value that cannot be compared blocks in both fields
# ---------------------------------------------------------------------------


class TestAnUncomparableValue:
    """A third answer, and not a false one.

    Absent means nobody looked, and the two fields read that in opposite directions.
    A value that cannot be compared means somebody looked and wrote something unusable,
    and reading THAT as "no blocker" would be the swallowed error the two policies exist to
    prevent. So it blocks in both.
    """

    def test_a_wrongly_typed_pattern_blocks_a_forbids_rather_than_being_ignored(self):
        """EXPLAIN takes a caller's hypothetical patterns, so this is reachable."""
        plan = _plan(
            "text",
            {HAS_TEXT_LAYER: True, "has_headings": True, CHAR_COUNT: "lots"},
            FACTS_ON,
        )

        assert TASK_EXTRACT_FACTS not in [s.task_type for s in plan.eligible]
        assert "cannot be compared" in plan.not_applicable[TASK_EXTRACT_FACTS]

    def test_it_does_not_raise(self):
        """A plan is computed for a request; a caller's bad hypothesis is an answer, not a
        500."""
        _plan("text", {HAS_TEXT_LAYER: True, CHAR_COUNT: None}, FACTS_ON)


# ---------------------------------------------------------------------------
# 8. What EXPLAIN reports, and what it deliberately still reports as strings
# ---------------------------------------------------------------------------


class TestTheReportedCondition:
    """Operators reached the table without changing what a row that has none says.

    Whether the wire wants the STRUCTURE of a term is a separate question from whether the
    table has it, and answering it here would spend a client-visible break on a shape
    nobody has asked for.
    """

    def test_a_truthiness_term_still_renders_as_the_bare_resolved_pattern(self):
        from jmfts_core.ingest_tasks import explain_plan

        declared = {t.task: t for t in explain_plan("pdf").tasks}[TASK_STRUCTURE_DECLARED]
        assert declared.requires == ("has_text_layer", "has_outline")

    def test_a_comparison_renders_as_the_comparison(self):
        from jmfts_core.ingest_tasks import explain_plan

        facts = {t.task: t for t in explain_plan("text").tasks}[TASK_EXTRACT_FACTS]
        assert facts.forbids == ("char_count < options.facts.min_characters",)
        assert facts.requires == ("options.facts.enabled", "has_text_layer")

    def test_an_option_term_is_not_a_consulted_pattern(self):
        """`patterns_ignored` answers "which measurements decide anything here", and
        `options.facts.enabled` is not a key a caller's matched.patterns could supply."""
        from jmfts_core.ingest_tasks import explain_plan

        plan = explain_plan("text", patterns={HAS_TEXT_LAYER: True, "nonsense": 1})
        assert plan.patterns_ignored == ("nonsense",)


# ---------------------------------------------------------------------------
# 9. The operators, exercised one at a time
# ---------------------------------------------------------------------------


class TestEachOperator:
    """One row, one comparison, over the shipped evaluator rather than a reimplementation."""

    @pytest.mark.parametrize(
        "op,threshold,value,holds",
        [
            (OP_EQ, 10, 10, True),
            (OP_EQ, 10, 11, False),
            ("!=", 10, 11, True),
            (OP_LT, 10, 9, True),
            (OP_LT, 10, 10, False),
            ("<=", 10, 10, True),
            (OP_GT, 10, 11, True),
            (">=", 10, 10, True),
            (">=", 10, 9, False),
        ],
    )
    def test_a_requires_term_holds_exactly_when_the_comparison_does(
        self, op, threshold, value, holds, monkeypatch
    ):
        rows = _replace(
            TASK_EXTRACT_TEXT,
            requires=(HAS_TEXT_LAYER, term(CHAR_COUNT, op, threshold)),
            forbids=(),
        )
        monkeypatch.setattr("jmfts_core.ingest_tasks.TASK_ROWS", tuple(rows))

        eligible = _eligible("text", {HAS_TEXT_LAYER: True, CHAR_COUNT: value})
        assert (TASK_EXTRACT_TEXT in eligible) is holds
