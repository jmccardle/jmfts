"""`EXPLAIN` for file ingestion — `INGEST_SPEC.md` 11.2, first mode.

The load-bearing test here is :class:`TestExplainAgreesWithThePlanner`. `EXPLAIN` is only
worth having if it describes the run that would actually happen, and the one way it can
stop doing that is by becoming a second evaluator of Part 4's table — one that answers
plausibly and independently. So for a grid of (format, patterns, options), the explanation
is asserted against `plan_after_probe` + `_split_by_handler`: the same two calls `run_probe`
makes, on the same inputs. Same enqueued tasks in the same order, same params, same skip,
deferral and not-applicable reasons, spelled the same way.

Everything else here guards the part that is genuinely new — that an answer with no bytes
says on what basis it was decided, and never invents the patterns it lacks.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jmfts_core.rest.main import app
from jmfts_core.database import get_db
from jmfts_core.ingest_options import ROLLUP_PARAMS, STRUCTURE_CHUNK_PARAMS
from jmfts_core.ingest_tasks import (
    DECLARED_STRUCTURE_PATTERN,
    DEFERRED_REASON,
    OUTCOME_CONDITIONAL,
    OUTCOME_DEFERRED,
    OUTCOME_ENQUEUED,
    OUTCOME_IMPOSSIBLE,
    OUTCOME_NOT_APPLICABLE,
    OUTCOME_SKIPPED,
    PATTERNS_NO_PROBER,
    PATTERNS_SUPPLIED,
    PATTERNS_UNKNOWN,
    PROBE_WRITE_MODE,
    SENTINEL_REASONS,
    TASK_EXTRACT_IMAGES,
    TASK_EXTRACT_TABLES,
    TASK_EXTRACT_TEXT,
    TASK_OCR,
    TASK_PROBE,
    TASK_ROWS,
    TASK_STRUCTURE_DECLARED,
    TASK_STRUCTURE_INFERRED,
    _no_declared_structure_reason,
    _split_by_handler,
    explain_plan,
    plan_after_probe,
)
from jmfts_core.probe import PROBERS_AVAILABLE
from jmfts_core.services.ingest_service import IngestService

# A real PDF pattern set as a CALLER would paste one back: `_probe_pdf`'s keys plus the
# one `extract:text` measures. Pasting a node's patterns back is the obvious way to ask
# "what would this node have done", and after extraction has run the node knows all of
# them — so this is the shape under test.
#
# `probe` alone reports every key here EXCEPT `pages_with_tables`; that one is measured by
# `extract:text` (INGEST_SPEC.md 3.3) and reaches the node in its `extraction` record. The
# probe-time set is `PROBE_ONLY_PDF_PATTERNS` below, and the difference between the two is
# exactly the window in which `extract:tables` is undecidable.
REAL_PDF_PATTERNS = {
    "has_text_layer": True,
    "has_outline": True,
    "outline_depth": 2,
    # Empty, not absent: this document was measured and has no tables. It is the falsy
    # half of the list pattern, and it must block `extract:tables` exactly as the
    # `has_tables: false` it replaces did.
    "pages_with_tables": [],
    "has_images": True,
    "image_count": 4,
    "page_count": 12,
    "is_scanned": False,
    "is_damaged": False,
}

#: What `_probe_pdf` ACTUALLY writes today: the set above minus `pages_with_tables`. This
#: is the pattern set `run_probe` plans from on every real PDF, so `explain` and the queue
#: have to agree on it — including on the fact that `extract:tables` is undecidable here.
PROBE_ONLY_PDF_PATTERNS = {k: v for k, v in REAL_PDF_PATTERNS.items() if k != "pages_with_tables"}

#: Formats and pattern sets whose explanation must match what the queue would really do.
#: `pdf` several ways because it is the one format with a prober; `pptx` and `zip` because
#: a format with no prober is the case where the empty pattern set is a fact.
AGREEMENT_GRID = [
    ("pdf", {}),
    ("pdf", {"has_text_layer": True}),
    ("pdf", {"has_text_layer": True, "has_outline": True}),
    ("pdf", {"has_text_layer": True, "pages_with_tables": [0, 4], "has_images": True}),
    # The empty list beside the populated one above: same key, opposite truthiness, and
    # `explain` must agree with the queue on both.
    ("pdf", {"has_text_layer": True, "pages_with_tables": [], "has_images": True}),
    ("pdf", {"is_scanned": True, "has_images": True}),
    ("pdf", REAL_PDF_PATTERNS),
    ("pdf", PROBE_ONLY_PDF_PATTERNS),
    ("pptx", {}),
    ("pptx", {"has_text_layer": True, "has_slides": True}),
    ("pptx", {"has_text_layer": True}),
    ("zip", {}),
    ("zip", {"has_text_layer": True}),
    ("unknown", {"has_text_layer": True, "has_images": True}),
]

OPTIONS_GRID = [None, {"structure": {"max_tokens": 60}}]


def _by_task(plan) -> dict:
    return {task.task: task for task in plan.tasks}


# ---------------------------------------------------------------------------
# 1. The agreement test — EXPLAIN must not become a second, lying copy of the table
# ---------------------------------------------------------------------------


class TestExplainAgreesWithThePlanner:
    @pytest.mark.parametrize("fmt,patterns", AGREEMENT_GRID)
    @pytest.mark.parametrize("options", OPTIONS_GRID)
    def test_explanation_matches_what_the_queue_would_do(self, fmt, patterns, options):
        plan = plan_after_probe(fmt, patterns, options)
        runnable, deferred = _split_by_handler(plan.eligible)
        explained = explain_plan(fmt, options, patterns)
        tasks = _by_task(explained)

        # Same tasks enqueued, in the same order — `enqueue_batch` resolves `after` by
        # position, so an explanation that reordered them would describe a different run.
        assert [
            task.task
            for task in explained.tasks
            if task.outcome == OUTCOME_ENQUEUED and task.task != TASK_PROBE
        ] == [spec.task_type for spec in runnable]

        # Same params on each. These are what `param_fingerprint` is taken over (6.1), so
        # a plan that reported different ones would misdescribe re-ingest as well as ingest.
        for spec in runnable:
            assert tasks[spec.task_type].params == spec.params
            assert tasks[spec.task_type].write_mode == spec.write_mode

        for entry in plan.skipped:
            assert tasks[entry.task_type].outcome == OUTCOME_SKIPPED
            assert tasks[entry.task_type].reason == entry.reason

        for task_type, reason in deferred.items():
            assert tasks[task_type].outcome == OUTCOME_DEFERRED
            assert tasks[task_type].reason == reason

        for task_type, reason in plan.not_applicable.items():
            task = tasks[task_type]
            assert task.outcome in {OUTCOME_NOT_APPLICABLE, OUTCOME_IMPOSSIBLE}
            if task.outcome == OUTCOME_NOT_APPLICABLE:
                assert task.reason == reason
            else:
                # `impossible` refines not-applicable. Its reason is either the one the run
                # records or the sentence naming the requirement no file of this format can
                # satisfy — never a third wording invented by the explainer.
                #
                # Built from SENTINEL_REASONS rather than from a hand-written list, so a
                # new sentinel arrives here with its own sentence instead of failing this
                # test into a wider allowance.
                sentinel_reasons = {builder(fmt) for builder in SENTINEL_REASONS.values()}
                assert task.reason in {reason} | sentinel_reasons

    @pytest.mark.parametrize("fmt,patterns", AGREEMENT_GRID)
    def test_no_task_is_explained_as_something_the_plan_did_not_decide(self, fmt, patterns):
        """The converse direction: every claim in the explanation has a source in the plan."""
        plan = plan_after_probe(fmt, patterns, None)
        runnable, deferred = _split_by_handler(plan.eligible)
        enqueued = {spec.task_type for spec in runnable}
        skipped = {entry.task_type for entry in plan.skipped}

        for task in explain_plan(fmt, None, patterns).tasks:
            if task.task == TASK_PROBE:
                continue
            if task.outcome == OUTCOME_ENQUEUED:
                assert task.task in enqueued
            elif task.outcome == OUTCOME_SKIPPED:
                assert task.task in skipped
            elif task.outcome == OUTCOME_DEFERRED:
                assert task.task in deferred
            else:
                assert task.outcome in {OUTCOME_NOT_APPLICABLE, OUTCOME_IMPOSSIBLE}
                assert task.task in plan.not_applicable

    @pytest.mark.parametrize("fmt,patterns", AGREEMENT_GRID)
    @pytest.mark.parametrize("options", OPTIONS_GRID)
    def test_every_task_row_appears(self, fmt, patterns, options):
        """A task omitted from a plan is a wrong answer, not a short one (spec 11.2)."""
        tasks = _by_task(explain_plan(fmt, options, patterns))
        for row in TASK_ROWS:
            assert row.task in tasks, f"{row.task} missing from the explanation for {fmt}"
        assert TASK_PROBE in tasks

    def test_every_task_row_appears_when_the_answer_is_conditional(self):
        tasks = _by_task(explain_plan("pdf"))
        for row in TASK_ROWS:
            assert row.task in tasks


# ---------------------------------------------------------------------------
# 2. probe leads, and always runs
# ---------------------------------------------------------------------------


class TestProbe:
    @pytest.mark.parametrize("fmt", ["pdf", "pptx", "zip", "unknown"])
    def test_probe_is_first_and_enqueued_for_every_format(self, fmt):
        """Part 4: probe depends on nothing, needs no model, and always runs."""
        plan = explain_plan(fmt)
        assert plan.tasks[0].task == TASK_PROBE
        assert plan.tasks[0].outcome == OUTCOME_ENQUEUED
        assert plan.tasks[0].write_mode == PROBE_WRITE_MODE
        assert plan.tasks[0].params == {}
        assert plan.tasks[0].requires == () and plan.tasks[0].forbids == ()

    def test_the_rest_of_the_list_is_the_table_in_table_order(self):
        plan = explain_plan("pdf")
        assert [task.task for task in plan.tasks[1:]] == [row.task for row in TASK_ROWS]


# ---------------------------------------------------------------------------
# 3. The conditional answer — a prober exists and nobody said what it found
# ---------------------------------------------------------------------------


class TestConditionalAnswer:
    def test_pdf_without_patterns_is_conditional_and_says_so(self):
        plan = explain_plan("pdf")
        assert "pdf" in PROBERS_AVAILABLE
        assert plan.prober_available is True
        assert plan.patterns_known is False
        assert plan.patterns_source == PATTERNS_UNKNOWN

    def test_conditional_tasks_name_the_patterns_that_decide_them(self):
        tasks = _by_task(explain_plan("pdf"))

        assert tasks[TASK_EXTRACT_TEXT].outcome == OUTCOME_CONDITIONAL
        assert tasks[TASK_EXTRACT_TEXT].if_condition_holds == OUTCOME_ENQUEUED
        assert tasks[TASK_EXTRACT_TEXT].requires == ("has_text_layer",)

        declared = tasks[TASK_STRUCTURE_DECLARED]
        assert declared.outcome == OUTCOME_CONDITIONAL
        assert declared.if_condition_holds == OUTCOME_ENQUEUED
        # The sentinel is RESOLVED: `has_outline` is what PDF declares structure with.
        assert declared.requires == ("has_text_layer", "has_outline")
        assert declared.after == (TASK_EXTRACT_TEXT,)

        inferred = tasks[TASK_STRUCTURE_INFERRED]
        assert inferred.outcome == OUTCOME_CONDITIONAL
        assert inferred.forbids == ("has_outline",)

    def test_ocr_is_conditional_but_would_be_skipped_with_the_specs_reason(self):
        """The one row the spec says to record rather than run. What is unknown about it
        is only whether the condition fires at all."""
        task = _by_task(explain_plan("pdf"))[TASK_OCR]
        assert task.outcome == OUTCOME_CONDITIONAL
        assert task.if_condition_holds == OUTCOME_SKIPPED
        assert task.requires == ("is_scanned",)
        assert "out of scope for v1" in task.reason
        # A row that is always recorded rather than queued claims no region (spec 5.3).
        assert task.write_mode is None

    def test_a_task_with_no_handler_is_decided_without_patterns(self):
        """Whether or not the condition fires, an unimplemented task is not enqueued —
        so it is reported as decided rather than as conditional."""
        tasks = _by_task(explain_plan("pdf"))
        for task_type in (TASK_EXTRACT_TABLES, TASK_EXTRACT_IMAGES):
            assert tasks[task_type].outcome == OUTCOME_DEFERRED
            assert tasks[task_type].if_condition_holds is None
            assert tasks[task_type].reason == DEFERRED_REASON[task_type]

    def test_if_condition_holds_is_set_only_on_conditional_tasks(self):
        for plan in (explain_plan("pdf"), explain_plan("pptx"), explain_plan("zip")):
            for task in plan.tasks:
                if task.outcome == OUTCOME_CONDITIONAL:
                    assert task.if_condition_holds in {
                        OUTCOME_ENQUEUED,
                        OUTCOME_SKIPPED,
                        OUTCOME_DEFERRED,
                    }
                else:
                    assert task.if_condition_holds is None


# ---------------------------------------------------------------------------
# 4. The no-prober answer — concrete, because the empty pattern set is a fact
# ---------------------------------------------------------------------------


class TestNoProberIsConcrete:
    def test_epub_is_answered_concretely_and_nothing_but_probe_is_enqueued(self):
        """11.2's own example, moved to the format it is still true of.

        It was written about `.pptx`. OFFICE_SPEC.md phasing step 4 gave `pptx` a prober,
        so its answer is now conditional (see below) — EPUB is the remaining ZIP container
        probe identifies and cannot look inside.
        """
        assert "epub" not in PROBERS_AVAILABLE
        plan = explain_plan("epub")

        assert plan.prober_available is False
        assert plan.patterns_known is True
        assert plan.patterns_source == PATTERNS_NO_PROBER

        enqueued = [task.task for task in plan.tasks if task.outcome == OUTCOME_ENQUEUED]
        assert enqueued == [TASK_PROBE]
        assert not any(task.outcome == OUTCOME_CONDITIONAL for task in plan.tasks)

    def test_epub_declared_structure_keeps_its_real_condition(self):
        """`epub` HAS a declared-structure pattern — its outline. It is not applicable
        because nothing measures it today, which is a different fact from impossible."""
        assert DECLARED_STRUCTURE_PATTERN["epub"] == "has_outline"
        task = _by_task(explain_plan("epub"))[TASK_STRUCTURE_DECLARED]
        assert task.outcome == OUTCOME_NOT_APPLICABLE
        assert "has_outline" in task.requires

    def test_pptx_now_has_a_prober_and_so_answers_conditionally(self):
        """The state change phasing step 4 made, from EXPLAIN's side.

        `pptx` moved out of this section: a format with a prober cannot be answered
        concretely without bytes, because what the prober will find is exactly what is
        unknown. `has_slides` is now a condition that MIGHT hold rather than one nothing
        will ever report.
        """
        assert "pptx" in PROBERS_AVAILABLE
        plan = explain_plan("pptx")

        assert plan.prober_available is True
        assert plan.patterns_known is False
        assert plan.patterns_source == PATTERNS_UNKNOWN

        declared = _by_task(plan)[TASK_STRUCTURE_DECLARED]
        assert declared.outcome == OUTCOME_CONDITIONAL
        assert declared.requires == ("has_text_layer", "has_slides")

    def test_a_format_with_no_declared_structure_pattern_is_impossible_not_merely_false(self):
        assert "zip" not in DECLARED_STRUCTURE_PATTERN
        tasks = _by_task(explain_plan("zip"))

        declared = tasks[TASK_STRUCTURE_DECLARED]
        assert declared.outcome == OUTCOME_IMPOSSIBLE
        assert declared.reason == _no_declared_structure_reason("zip")
        # The unresolvable sentinel is DROPPED rather than emitted as a null in the list.
        assert declared.requires == ("has_text_layer",)
        assert None not in declared.requires

        # The same sentinel in `forbids` prohibits nothing, so the inferred rung is
        # perfectly possible: an unknown format gets a rung rather than neither rung.
        inferred = tasks[TASK_STRUCTURE_INFERRED]
        assert inferred.outcome != OUTCOME_IMPOSSIBLE
        assert inferred.forbids == ()

    def test_impossible_survives_patterns_that_would_otherwise_satisfy_the_row(self):
        """Impossible means whatever the bytes are — including bytes somebody asserts."""
        tasks = _by_task(explain_plan("zip", patterns={"has_text_layer": True}))
        assert tasks[TASK_EXTRACT_TEXT].outcome == OUTCOME_ENQUEUED
        assert tasks[TASK_STRUCTURE_DECLARED].outcome == OUTCOME_IMPOSSIBLE
        assert tasks[TASK_STRUCTURE_INFERRED].outcome == OUTCOME_ENQUEUED


# ---------------------------------------------------------------------------
# 5. Supplied patterns
# ---------------------------------------------------------------------------


class TestSuppliedPatterns:
    def test_supplied_patterns_make_the_answer_concrete(self):
        plan = explain_plan("pdf", patterns={"has_text_layer": True, "has_outline": True})
        assert plan.patterns_known is True
        assert plan.patterns_source == PATTERNS_SUPPLIED
        tasks = _by_task(plan)
        assert tasks[TASK_EXTRACT_TEXT].outcome == OUTCOME_ENQUEUED
        assert tasks[TASK_STRUCTURE_DECLARED].outcome == OUTCOME_ENQUEUED
        assert tasks[TASK_STRUCTURE_INFERRED].outcome == OUTCOME_NOT_APPLICABLE

    def test_a_real_pattern_block_is_accepted_and_its_unread_keys_are_listed(self):
        """Pasting a node's `matched.patterns` back is the obvious use, so the keys no
        condition consults are REPORTED rather than rejected — rejecting them would
        manufacture a failure out of a perfectly good question."""
        plan = explain_plan("pdf", patterns=REAL_PDF_PATTERNS)
        assert plan.patterns_source == PATTERNS_SUPPLIED
        assert "page_count" in plan.patterns_ignored
        assert set(plan.patterns_ignored) == {
            "page_count",
            "outline_depth",
            "image_count",
            "is_damaged",
        }
        # And the keys that DO decide something are not in the list.
        assert "has_text_layer" not in plan.patterns_ignored
        assert "has_outline" not in plan.patterns_ignored

    def test_a_misspelled_pattern_shows_up_as_a_key_that_decided_nothing(self):
        plan = explain_plan("pdf", patterns={"has_text_lyer": True})
        assert plan.patterns_ignored == ("has_text_lyer",)
        assert _by_task(plan)[TASK_EXTRACT_TEXT].outcome == OUTCOME_NOT_APPLICABLE

    def test_nothing_is_ignored_when_no_patterns_were_supplied(self):
        assert explain_plan("pdf").patterns_ignored == ()
        assert explain_plan("pptx").patterns_ignored == ()

    def test_a_formats_own_pattern_name_is_not_reported_as_ignored(self):
        """`has_slides` decides nothing for a PDF and everything for a pptx."""
        assert "has_slides" in explain_plan("pdf", patterns={"has_slides": True}).patterns_ignored
        assert explain_plan("pptx", patterns={"has_slides": True}).patterns_ignored == ()


# ---------------------------------------------------------------------------
# 6. Options
# ---------------------------------------------------------------------------


class TestOptions:
    def test_resolved_options_are_reported_in_full(self):
        plan = explain_plan("pdf")
        assert plan.options == {
            "structure": dict(STRUCTURE_CHUNK_PARAMS),
            "rollup": dict(ROLLUP_PARAMS),
        }

    def test_overrides_reach_the_structure_rows_params(self):
        plan = explain_plan(
            "pdf",
            options={"structure": {"max_tokens": 60}},
            patterns={"has_text_layer": True, "has_outline": True},
        )
        assert plan.options["structure"]["max_tokens"] == 60
        tasks = _by_task(plan)
        assert tasks[TASK_STRUCTURE_DECLARED].params["max_tokens"] == 60
        # ...and the rest of the group is still complete, not a diff.
        assert (
            tasks[TASK_STRUCTURE_DECLARED].params["chunk_strategy"]
            == STRUCTURE_CHUNK_PARAMS["chunk_strategy"]
        )
        # A row with no params_key takes no parameters at all, which is a different fact
        # from taking them and leaving them at their defaults (spec 6.1's re-run diff).
        assert tasks[TASK_EXTRACT_TEXT].params == {}

    def test_params_are_reported_even_for_a_row_that_will_not_run(self):
        """What the queue row WOULD carry is part of the answer either way."""
        tasks = _by_task(explain_plan("epub", options={"structure": {"max_tokens": 60}}))
        assert tasks[TASK_STRUCTURE_DECLARED].outcome == OUTCOME_NOT_APPLICABLE
        assert tasks[TASK_STRUCTURE_DECLARED].params["max_tokens"] == 60

    def test_params_are_copies_not_shared_state(self):
        plan = explain_plan("pdf", patterns={"has_text_layer": True})
        tasks = _by_task(plan)
        tasks[TASK_STRUCTURE_INFERRED].params["max_tokens"] = 9999
        assert plan.options["structure"]["max_tokens"] == STRUCTURE_CHUNK_PARAMS["max_tokens"]

    @pytest.mark.parametrize(
        "options,message",
        [
            ({"structure": {"max_token": 60}}, "unknown option structure.max_token"),
            ({"chunking": {"max_tokens": 60}}, "unknown option group 'chunking'"),
            ({"structure": {"max_tokens": 0}}, "expected a positive integer"),
            ({"structure": {"chunk_strategy": "vibes"}}, "expected one of"),
        ],
    )
    def test_a_bad_option_raises_and_names_it(self, options, message):
        """Explaining a plan under options the run would reject is the wrong answer this
        endpoint exists to prevent."""
        with pytest.raises(ValueError, match=message):
            explain_plan("pdf", options=options)


# ---------------------------------------------------------------------------
# 7. The endpoint
# ---------------------------------------------------------------------------


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


class TestExplainEndpoint:
    def test_the_route_is_registered_once(self):
        from jmfts_core.registry import REGISTRY

        hits = [spec for spec in REGISTRY if spec.path == "/ingest/explain"]
        assert len(hits) == 1
        assert hits[0].method == "POST"
        assert hits[0].service_cls is IngestService

    def test_the_route_returns_what_the_in_process_call_returns(self, client_with_db):
        body = {
            "format": "pdf",
            "options": {"structure": {"max_tokens": 60}},
            "patterns": REAL_PDF_PATTERNS,
        }
        response = client_with_db.post("/ingest/explain", json=body)
        assert response.status_code == 200, response.text
        over_http = response.json()

        from jmfts_core.explain_wire import explain_response_from_plan

        in_process = explain_response_from_plan(
            explain_plan(body["format"], body["options"], body["patterns"])
        )
        assert over_http == in_process.model_dump()

    def test_a_conditional_answer_survives_the_wire(self, client_with_db):
        response = client_with_db.post("/ingest/explain", json={"format": "pdf"})
        assert response.status_code == 200, response.text
        payload = response.json()

        assert payload["patterns_known"] is False
        assert payload["patterns_source"] == PATTERNS_UNKNOWN
        assert payload["patterns_ignored"] == []
        by_task = {task["task"]: task for task in payload["tasks"]}
        assert [task["task"] for task in payload["tasks"]] == [TASK_PROBE] + [
            row.task for row in TASK_ROWS
        ]
        assert by_task[TASK_EXTRACT_TEXT]["outcome"] == OUTCOME_CONDITIONAL
        assert by_task[TASK_EXTRACT_TEXT]["if_condition_holds"] == OUTCOME_ENQUEUED
        assert by_task[TASK_EXTRACT_TEXT]["requires"] == ["has_text_layer"]

    def test_a_no_prober_format_answers_concretely_over_http(self, client_with_db):
        response = client_with_db.post("/ingest/explain", json={"format": "epub"})
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["patterns_known"] is True
        assert payload["patterns_source"] == PATTERNS_NO_PROBER
        enqueued = [t["task"] for t in payload["tasks"] if t["outcome"] == OUTCOME_ENQUEUED]
        assert enqueued == [TASK_PROBE]

    def test_a_bad_option_is_a_400_naming_the_option(self, client_with_db):
        response = client_with_db.post(
            "/ingest/explain",
            json={"format": "pdf", "options": {"structure": {"max_token": 60}}},
        )
        assert response.status_code == 400, response.text
        assert "max_token" in response.json()["detail"]

    def test_an_empty_format_is_refused(self, client_with_db):
        response = client_with_db.post("/ingest/explain", json={"format": ""})
        assert response.status_code == 422, response.text

    def test_explaining_writes_nothing(self, client_with_db, db_session):
        """Read-only in the strongest sense: no document, no queue row, no attempt."""
        from jmfts_core.models.document import Document
        from jmfts_core.models.task_queue import TaskQueue

        before = (
            db_session.query(Document).count(),
            db_session.query(TaskQueue).count(),
        )
        assert (
            client_with_db.post(
                "/ingest/explain", json={"format": "pdf", "patterns": REAL_PDF_PATTERNS}
            ).status_code
            == 200
        )
        assert (
            db_session.query(Document).count(),
            db_session.query(TaskQueue).count(),
        ) == before
