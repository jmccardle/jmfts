"""The cost fold — ``SPRINT_JOBS.md`` Part 7, phase 5.

``EXPLAIN`` said WHICH rungs would run and could not say HOW MANY nodes or how much work.
Part 7 is one traversal of the plan with a choice of accumulator: counting tasks, child
nodes, embeddings and LLM calls are the same fold, and every accumulator returns an
interval because a fan-out atom declares a bound rather than a number (2.4).

Three properties are what this file is for, and each is a way the answer could be
confidently wrong instead of merely absent:

* **Cardinality composes without reading the tree.** A parent rule's fan-out IS the child
  rule's multiplicity (:class:`~jmfts_core.ingest_tasks.Scope`'s docstring), so a
  three-sheet workbook's ``profile:sheet`` is three tasks and ``EXPLAIN`` can say so from
  the declarations alone.
* **An unmeasurable count is reported, never guessed** (7.2). ``extract:sheet``'s ceiling
  is a function of ``sheet.measurements.rows``, which ``profile:sheet`` has not written
  yet, so the top of the interval is open and the answer names what would close it.
* **What the fold does NOT count is stated.** Three shipped tasks write into another tree
  and the locus vocabulary has no term for it (``INGEST_SPEC.md`` 5.3), and twelve task
  types are not scheduled from Part 4's table at all. A total that quietly omitted either
  would be wrong by exactly the amount nobody could see.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jmfts_core.atoms import ATOMS, COST_CPU, COST_LLM, COST_MODEL
from jmfts_core.database import get_db
from jmfts_core.rest.main import app
from jmfts_core.ingest_tasks import (
    ONE,
    TASK_EMBED,
    TASK_EXTRACT_FACTS,
    TASK_EXTRACT_SHEET,
    TASK_PROBE,
    TASK_PROFILE_SHEET,
    TASK_ROWS,
    TASK_STRUCTURE_CONVERSATION,
    TASK_STRUCTURE_SHEETS,
    ZERO,
    Count,
    explain_plan,
)
from jmfts_core.models.task_queue import WRITE_CHILDREN

#: A three-sheet workbook as `probe` would report it. `sheet_count` is what
#: `structure:sheets`' bound divides, and `has_sheets` is what schedules it.
WORKBOOK = {"has_sheets": True, "sheet_count": 3}

#: A transcript as the `text` prober reports one. `line_count` is a measurement no
#: condition reads and no bound reads either — see the conversation test below.
TRANSCRIPT = {
    "has_text_layer": True,
    "is_conversation": True,
    "char_count": 4_000,
    "line_count": 40,
}


def _task(plan, name):
    for task in plan.tasks:
        if task.task == name:
            return task
    raise AssertionError(f"{name} is not in the plan")


class TestCountArithmetic:
    """An interval with an open top, because 7.2's answer has one."""

    def test_an_open_top_survives_addition_and_multiplication(self):
        assert Count(1, 2) + Count(3, 4) == Count(4, 6)
        assert Count(1, 2) * Count(3, 4) == Count(3, 8)
        assert Count(1, None) + Count(3, 4) == Count(4, None)
        assert Count(2, 3) * Count(0, None) == Count(0, None)

    def test_zero_annihilates_an_open_top(self):
        # A task that cannot run writes nothing, and "nothing" is closed however open the
        # per-instance bound is. Without this a not-applicable rung would put a `?` on the
        # whole plan's node count.
        assert ZERO * Count(0, None) == ZERO


class TestMultiplicityComposes:
    """4.1's scope, folded: the producing rule's fan-out is the child rule's multiplicity."""

    def test_a_three_sheet_workbook_runs_the_sheet_rungs_three_times(self):
        plan = explain_plan("xlsx", None, WORKBOOK)
        assert _task(plan, TASK_STRUCTURE_SHEETS).multiplicity == ONE
        assert _task(plan, TASK_PROFILE_SHEET).multiplicity == Count(3, 3)
        assert _task(plan, TASK_EXTRACT_SHEET).multiplicity == Count(3, 3)

    def test_the_sheet_rung_writes_exactly_three_nodes(self):
        plan = explain_plan("xlsx", None, WORKBOOK)
        rung = _task(plan, TASK_STRUCTURE_SHEETS)
        assert rung.writes == Count(3, 3)
        assert rung.writes_usetype == "sheet"
        assert rung.writes_basis == "one node per sheet"

    def test_the_profile_rung_writes_one_node_per_sheet(self):
        # `fixed(1)`: one profile node per sheet node, so three tasks and three nodes.
        assert _task(explain_plan("xlsx", None, WORKBOOK), TASK_PROFILE_SHEET).writes == Count(3, 3)

    def test_a_task_that_cannot_run_has_no_instances(self):
        plan = explain_plan("xlsx", None, WORKBOOK)
        for task in plan.tasks:
            if task.outcome != "enqueued":
                assert task.multiplicity == ZERO, task.task
                assert task.writes == ZERO, task.task

    def test_probe_runs_once_and_writes_no_nodes(self):
        probe = _task(explain_plan("xlsx", None, WORKBOOK), TASK_PROBE)
        assert probe.multiplicity == ONE
        assert probe.writes == ZERO

    def test_embed_is_cast_at_every_leaf_the_rules_beneath_it_wrote(self):
        # `embed`'s scope names five producing rules and five usetypes. On a workbook the
        # profile nodes are three and the record nodes are unmeasurable, so the low bound
        # is the three profiles and the top is open. A plan that reported `embed` as ONE
        # task is the reading Part 0 recorded as `EXPLAIN` "stopping at the sheet list".
        embed = _task(explain_plan("xlsx", None, WORKBOOK), TASK_EMBED)
        assert embed.multiplicity.low == 3
        assert embed.multiplicity.high is None


class TestAnUnmeasurableCountIsReported:
    """7.2. The fold has probe's patterns and the resolved options, and nothing else."""

    def test_the_row_ceiling_is_open_and_names_what_would_close_it(self):
        plan = explain_plan("xlsx", None, WORKBOOK)
        assert _task(plan, TASK_EXTRACT_SHEET).writes == Count(0, None)
        wanted = {u.evidence: u for u in plan.cost.unmeasured}
        assert "sheet.measurements.rows" in wanted
        entry = wanted["sheet.measurements.rows"]
        assert entry.needed_by == TASK_EXTRACT_SHEET
        # The `?` line of 7.1's worked example: the atom that would resolve it, named from
        # the registry rather than from a table this test keeps.
        assert TASK_PROFILE_SHEET in entry.written_by

    def test_a_measured_bound_is_not_reported_as_unmeasured(self):
        plan = explain_plan("xlsx", None, WORKBOOK)
        assert "matched.patterns.sheet_count" not in {u.evidence for u in plan.cost.unmeasured}

    def test_a_conditional_plan_measures_nothing_and_says_so(self):
        # No patterns at all: every bound that reads one is open, and the plan's totals
        # are open with it. This is the mode that must not invent a document.
        plan = explain_plan("pdf")
        assert not plan.patterns_known
        assert plan.cost.nodes.high is None
        assert {u.evidence for u in plan.cost.unmeasured} >= {"extraction.characters"}


class TestTheTotals:
    """One traversal, four accumulators."""

    def test_the_task_total_counts_every_instance_not_every_row(self):
        plan = explain_plan("xlsx", None, WORKBOOK)
        enqueued = [t for t in plan.tasks if t.outcome == "enqueued"]
        # probe, structure:sheets, render:pdf, three profiles, three extracts, and an
        # embed per leaf. More instances than rows, which is the whole point.
        assert plan.cost.tasks.low > len(enqueued)
        assert plan.cost.tasks.low == sum(t.multiplicity.low for t in plan.tasks)

    def test_the_node_total_is_the_sum_of_what_the_rules_write(self):
        plan = explain_plan("xlsx", None, WORKBOOK)
        assert plan.cost.nodes.low == sum(t.writes.low for t in plan.tasks)

    def test_the_cost_classes_are_the_declared_ones_and_all_three_are_present(self):
        plan = explain_plan("xlsx", None, WORKBOOK)
        assert set(plan.cost.by_cost_class) == {COST_CPU, COST_MODEL, COST_LLM}

    def test_an_embedding_is_a_model_call_and_a_workbook_makes_at_least_three(self):
        plan = explain_plan("xlsx", None, WORKBOOK)
        assert plan.cost.by_cost_class[COST_MODEL].low == 3

    def test_a_workbook_makes_no_llm_calls(self):
        # `extract:facts` is the only `llm` row and a workbook has no text layer, so the
        # honest answer is exactly zero rather than an open interval.
        plan = explain_plan("xlsx", None, WORKBOOK)
        assert plan.cost.by_cost_class[COST_LLM] == ZERO

    def test_turning_fact_extraction_off_takes_the_llm_call_out_of_the_forecast(self):
        patterns = {"has_text_layer": True, "char_count": 20_000, "page_count": 4}
        on = explain_plan("pdf", {"facts": {"enabled": True}}, patterns)
        off = explain_plan("pdf", {"facts": {"enabled": False}}, patterns)
        assert _task(on, TASK_EXTRACT_FACTS).multiplicity == ONE
        assert on.cost.by_cost_class[COST_LLM] == ONE
        assert off.cost.by_cost_class[COST_LLM] == ZERO


class TestWhatItDoesNotCount:
    """The debt phase 5 inherits and does not pay, stated rather than hidden."""

    def test_the_table_is_not_the_whole_appliance_and_the_plan_says_so(self):
        plan = explain_plan("xlsx", None, WORKBOOK)
        outside = set(ATOMS) - {row.task for row in TASK_ROWS} - {TASK_PROBE}
        assert outside, "the derivation is vacuous if every atom is a row"
        said = " ".join(plan.cost.not_counted)
        for name in sorted(outside):
            assert name in said, name

    def test_a_task_that_writes_outside_this_tree_is_named(self):
        # `extract:facts` declares `produces=()` — it writes triples, predicates and entity
        # nodes, none of which is evidence on a node of this tree. `SPRINT_0_5_0.md` 6.1.
        patterns = {"has_text_layer": True, "char_count": 20_000}
        plan = explain_plan("pdf", {"facts": {"enabled": True}}, patterns)
        assert any(TASK_EXTRACT_FACTS in line for line in plan.cost.not_counted)

    def test_a_rung_that_writes_children_and_declares_no_bound_is_named(self):
        # `structure:conversation` writes one chunk per turn and declares no `Fanout`,
        # which 2.4 says a fan-out atom must. Until it does, its node count is open and
        # the plan says which rung opened it rather than reporting a number short by every
        # turn in the transcript.
        plan = explain_plan("text", None, TRANSCRIPT)
        rung = _task(plan, TASK_STRUCTURE_CONVERSATION)
        assert rung.outcome == "enqueued"
        assert rung.writes == Count(0, None)
        assert any(TASK_STRUCTURE_CONVERSATION in line for line in plan.cost.not_counted)

    def test_the_undeclared_bound_is_the_only_one_of_its_kind(self):
        # A guard on the test above: the sentence is derived from a rule — an atom whose
        # write mode is `children` and which declares no fan-out — and the day a second
        # atom is in that position this says so instead of the rule quietly widening.
        undeclared = {
            name
            for name, atom in ATOMS.items()
            if atom.write_mode == WRITE_CHILDREN and atom.fanout is None
        }
        assert undeclared == {TASK_STRUCTURE_CONVERSATION}


class TestPatternsTheFoldReadsAreNotReportedAsIgnored:
    """``patterns_ignored`` says a key decided nothing, and now a key can decide a count."""

    def test_sheet_count_decides_the_multiplicity_and_is_not_ignored(self):
        plan = explain_plan("xlsx", None, WORKBOOK)
        assert "sheet_count" not in plan.patterns_ignored

    def test_a_key_nothing_reads_is_still_reported(self):
        plan = explain_plan("xlsx", None, {**WORKBOOK, "has_text_lyer": True})
        assert "has_text_lyer" in plan.patterns_ignored


@pytest.fixture
def client_with_db(db_session):
    """TestClient bound to the savepoint-wrapped session (the house pattern)."""

    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    from tests.conftest import AUTH_HEADERS

    client = TestClient(app, headers=AUTH_HEADERS)
    yield client
    app.dependency_overrides.pop(get_db, None)


class TestTheWire:
    """The fold crosses HTTP, because the upload view's `EXPLAIN` panel is where it goes."""

    def test_the_counts_survive_the_round_trip(self, client_with_db):
        response = client_with_db.post(
            "/ingest/explain", json={"format": "xlsx", "patterns": WORKBOOK}
        )
        assert response.status_code == 200
        body = response.json()
        rows = {row["task"]: row for row in body["tasks"]}
        assert rows[TASK_PROFILE_SHEET]["multiplicity"] == {"low": 3, "high": 3}
        assert rows[TASK_EXTRACT_SHEET]["writes"] == {"low": 0, "high": None}
        cost = body["cost"]
        assert cost["nodes"]["high"] is None
        assert cost["by_cost_class"]["llm"] == {"low": 0, "high": 0}
        assert any(u["evidence"] == "sheet.measurements.rows" for u in cost["unmeasured"])
        assert cost["not_counted"]
