"""The in-process worker and the asynchronous file pipeline. ``INGEST_SPEC.md`` 5.7, 5.8, Part 4.

What each group is really guarding:

* **5.7** — ``POST /ingest/file`` returns *before* extraction. The proof is not that it is
  fast; it is that the queue row exists, is still ``pending``, and the response's attempt
  log says so. A regression that quietly made the upload synchronous again would still
  return 201 with plausible content, and only the pending status catches it.
* **5.8** — the worker loop. Every behavioural test drives ``IngestWorker.run_once`` /
  ``drain`` synchronously through ``tests.conftest.drain_ingest_queue``: a background
  thread polled with sleeps turns an ordering bug into an intermittent one. The *thread*
  is tested separately and only for the property a thread has that a function does not —
  that it starts and joins.
* **Part 4** — the enqueue conditions are a pure function of ``matched.patterns``, so most
  of them are asserted without a database at all.
* **Retry** — the two halves that must not be confused: a PERMANENT failure ends the node
  (spec 2.1's ``failed``), and a RETRYABLE one waits out its backoff before running again.
  Both are checked through the real queue rather than by calling ``fail`` directly, since
  the thing being tested is whether the CLAIM honours them.
"""

from __future__ import annotations

import time
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from jmfts_core.rest.main import app
from jmfts_core.contracts.ingest import IngestRequest
from jmfts_core.contracts.upload import UploadedFile
from jmfts_core.database import get_db, get_session
from jmfts_core.ingest_options import OPTION_CHECKS, STRUCTURE_CHUNK_PARAMS
from jmfts_core.ingest_tasks import (
    DECLARED_STRUCTURE,
    TASK_EMBED,
    TASK_EXTRACT_IMAGES,
    TASK_EXTRACT_TABLES,
    TASK_EXTRACT_TEXT,
    TASK_OCR,
    TASK_PROBE,
    TASK_ROWS,
    TASK_STRUCTURE_DECLARED,
    TASK_STRUCTURE_INFERRED,
    TASK_HANDLERS,
    TaskOutcome,
    TaskRow,
    UnknownTaskError,
    get_task_handler,
    plan_after_probe,
    register_task_handler,
)
from jmfts_core.ingest_worker import IngestWorker
from jmfts_core.models.document import (
    Document,
    SETTLED_FAILED,
    SETTLED_IN_FLIGHT,
    SETTLED_SETTLED,
)
from jmfts_core.models.task_queue import (
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_PENDING,
    TASK_RUNNING,
)
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.task_queue import TaskQueueRepository
from jmfts_core.services.ingest_service import IngestService
from jmfts_core.structure_tasks import USETYPE_CHUNK
from tests.conftest import AUTH_HEADERS, DB_READY, _borrowed_session, drain_ingest_queue

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_fixture_pdf() -> bytes:
    """A 1-page PDF with a text layer and a 1-entry outline. Small on purpose."""
    pymupdf = pytest.importorskip("pymupdf")

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 60), "Chapter One", fontsize=24)
    page.insert_text(
        (50, 120),
        "Body text long enough that the average characters per page is comfortably "
        "above the scanned-document threshold and has_text_layer is unambiguous.",
        fontsize=12,
    )
    doc.set_toc([[1, "Chapter One", 1]])
    return doc.tobytes()


@pytest.fixture(scope="module")
def pdf_bytes() -> bytes:
    return _make_fixture_pdf()


@pytest.fixture
def client_with_db(db_session):
    """TestClient bound to the savepoint-wrapped session (the house pattern)."""

    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    client = TestClient(app, headers=AUTH_HEADERS)
    yield client
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def temp_handler():
    """Register a task handler for the duration of one test, then take it back out.

    ``TASK_HANDLERS`` is a module-global registry, so a test that added to it and left it
    there would change what every later test's worker is willing to claim.
    """
    registered: list[str] = []

    def _register(task_type, handler):
        register_task_handler(task_type)(handler)
        registered.append(task_type)
        return handler

    yield _register
    for task_type in registered:
        TASK_HANDLERS.pop(task_type, None)


def _upload(session, data, filename="annual.pdf", mime="application/pdf", parent_id=None):
    return IngestService(session).upload_file(
        UploadedFile(data=data, filename=filename, content_type=mime), parent_id=parent_id
    )


def _run_coroutine(coro):
    """Drive one coroutine without touching the process-wide event loop policy.

    `asyncio.run` sets the current loop to None on the way out, which makes a LATER
    module's `asyncio.get_event_loop()` raise — `tests/test_synthesis.py` does exactly
    that, and it collects after this file. A private loop leaves the policy untouched.
    """
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _node(docs, parent=None, title="n", settled=SETTLED_IN_FLIGHT):
    return docs.create(
        title=title,
        content=None,
        parent_id=parent.id if parent is not None else None,
        auto_embed=False,
        settled=settled,
    )


# ---------------------------------------------------------------------------
# 1. Part 4 — the enqueue conditions, as a pure function of the patterns
# ---------------------------------------------------------------------------


class TestEnqueueConditions:
    """``probe`` is cheap and always runs; everything downstream is decided by what it
    wrote into ``matched.patterns`` (spec Part 4)."""

    def test_a_text_pdf_with_an_outline_makes_both_text_and_declared_eligible(self):
        plan = plan_after_probe("pdf", {"has_text_layer": True, "has_outline": True})

        names = [spec.task_type for spec in plan.eligible]
        assert names == [TASK_EXTRACT_TEXT, TASK_STRUCTURE_DECLARED]
        # 5.5's within-node ordering: structure:declared depends on extract:text.
        assert dict((s.task_type, s.after) for s in plan.eligible)[TASK_STRUCTURE_DECLARED] == (
            TASK_EXTRACT_TEXT,
        )

    def test_no_text_layer_makes_extract_text_not_applicable(self):
        plan = plan_after_probe("pdf", {"has_text_layer": False, "has_outline": True})

        assert TASK_EXTRACT_TEXT not in [s.task_type for s in plan.eligible]
        assert "has_text_layer" in plan.not_applicable[TASK_EXTRACT_TEXT]

    def test_an_outline_without_a_text_layer_does_not_enqueue_declared_structure(self):
        """The row's *dependency* is extract:text; there is nothing to structure."""
        plan = plan_after_probe("pdf", {"has_text_layer": False, "has_outline": True})

        assert TASK_STRUCTURE_DECLARED not in [s.task_type for s in plan.eligible]
        assert "depends on it" in plan.not_applicable[TASK_STRUCTURE_DECLARED]

    def test_tables_and_images_follow_their_own_patterns(self):
        plan = plan_after_probe(
            "pdf", {"has_text_layer": True, "has_tables": True, "has_images": True}
        )

        names = [spec.task_type for spec in plan.eligible]
        assert TASK_EXTRACT_TABLES in names and TASK_EXTRACT_IMAGES in names

    def test_a_scanned_document_records_ocr_as_skipped_with_a_reason(self):
        """Part 4 marks `ocr` out of scope for v1 and says to record it as skipped."""
        plan = plan_after_probe("pdf", {"is_scanned": True, "has_images": True})

        assert [entry.task_type for entry in plan.skipped] == [TASK_OCR]
        assert "out of scope" in plan.skipped[0].reason
        assert TASK_OCR not in [spec.task_type for spec in plan.eligible]

    def test_a_format_with_no_patterns_makes_everything_not_applicable(self):
        """A .docx today: probe named the format and could not look inside it."""
        plan = plan_after_probe("docx", {})

        assert plan.eligible == ()
        assert plan.skipped == ()
        assert set(plan.not_applicable) == {
            TASK_EXTRACT_TEXT,
            TASK_OCR,
            TASK_STRUCTURE_DECLARED,
            TASK_STRUCTURE_INFERRED,
            TASK_EXTRACT_TABLES,
            TASK_EXTRACT_IMAGES,
        }

    def test_an_unknown_format_says_it_declares_no_structure_pattern(self):
        """The sentinel's asymmetry: unsatisfiable as a requirement, satisfied as a
        prohibition, so a format nothing knows how to read still gets the inferred rung
        rather than no structuring at all.

        With the parameters the structure task chunks by, which no format has to be listed
        anywhere to receive — they belong to the task, not to whatever produced the text."""
        plan = plan_after_probe("wat", {"has_text_layer": True})

        assert "declares no structure pattern" in plan.not_applicable[TASK_STRUCTURE_DECLARED]
        eligible = {spec.task_type: spec.params for spec in plan.eligible}
        assert TASK_STRUCTURE_INFERRED in eligible
        assert eligible[TASK_STRUCTURE_INFERRED] == dict(STRUCTURE_CHUNK_PARAMS)


class TestTaskTable:
    """The table itself (spec 11.2). Its whole value is that a decision can be read off it
    without running anything, so the properties that make that true are asserted directly
    rather than inferred from a handful of planned documents."""

    #: Nothing here is a realistic document; the point is coverage of the sentinel's three
    #: states and of both sides of every pattern the rows name.
    SAMPLES = [
        ("pdf", {}),
        ("pdf", {"has_text_layer": True}),
        ("pdf", {"has_text_layer": True, "has_outline": True}),
        ("pdf", {"has_text_layer": True, "has_tables": True, "has_images": True}),
        ("pdf", {"is_scanned": True}),
        ("docx", {"has_text_layer": True, "has_heading_styles": True}),
        ("wat", {"has_text_layer": True}),
        ("wat", {}),
    ]

    def test_every_row_is_decided_exactly_once(self):
        """Eligible, skipped and not-applicable partition the table — no row falls through
        the loop unrecorded, which is what makes the plan an answer rather than a sample."""
        for fmt, patterns in self.SAMPLES:
            plan = plan_after_probe(fmt, patterns)
            decided = (
                [spec.task_type for spec in plan.eligible]
                + [entry.task_type for entry in plan.skipped]
                + list(plan.not_applicable)
            )
            assert sorted(decided) == sorted(row.task for row in TASK_ROWS), (fmt, patterns)

    def test_every_condition_is_decidable_from_the_patterns_alone(self):
        """A condition names patterns and rows above it — never a measurement only the task
        itself could make. This is 11.2's constraint, and it is checkable structurally."""
        seen: set[str] = set()
        for row in TASK_ROWS:
            for dependency in row.after:
                assert dependency in seen, f"{row.task} comes after a row below it"
            for name in row.requires + row.forbids:
                # Either a pattern probe writes, or the one sentinel the planner resolves.
                # A second, unresolvable "@..." would be evaluated as a literal pattern
                # name, which is always absent — a condition that silently never holds.
                assert name and (name == DECLARED_STRUCTURE or not name.startswith("@")), name
            seen.add(row.task)
        assert len(seen) == len(TASK_ROWS), "a task is declared twice"

    def test_every_params_key_names_a_declared_option_group(self):
        """A row's ``params_key`` names an option group, and a group is DECLARED in
        ``OPTION_CHECKS``. A key naming no declared group would resolve to nothing and put
        an empty params dict on the queue row, which reads in the attempt log exactly like
        a task that genuinely takes no parameters."""
        for row in TASK_ROWS:
            if row.params_key is not None:
                assert row.params_key in OPTION_CHECKS, row.task

    def test_a_row_that_could_be_enqueued_must_declare_a_write_mode(self):
        """Spec 5.3. A row with no write mode is only legal when it is never enqueued."""
        TaskRow("never:runs", skip_reason="out of scope")  # legal: recorded as skipped

        with pytest.raises(ValueError, match="declares no write mode"):
            TaskRow("would:run", requires=("has_text_layer",))


class TestHandlerRegistry:
    def test_probe_is_registered(self):
        assert get_task_handler(TASK_PROBE) is not None

    def test_an_unregistered_task_type_raises_rather_than_no_opping(self):
        with pytest.raises(UnknownTaskError, match="no handler is registered"):
            get_task_handler("nothing:registers-this")

    def test_two_different_handlers_under_one_name_is_a_collision(self, temp_handler):
        temp_handler("test:collide", lambda session, task: TaskOutcome())

        with pytest.raises(ValueError, match="already has a handler"):
            register_task_handler("test:collide")(lambda session, task: TaskOutcome())


# ---------------------------------------------------------------------------
# 2. 5.7 — the upload returns before extraction
# ---------------------------------------------------------------------------


class TestUploadIsAsynchronous:
    def test_the_response_carries_exactly_one_pending_probe_attempt(self, db_session, pdf_bytes):
        response = _upload(db_session, pdf_bytes)

        assert len(response.attempts) == 1
        probe = response.attempts[0]
        assert probe.task == TASK_PROBE
        assert probe.status == "pending"
        assert probe.task_id is not None
        # `pending` is the one status with nothing measured yet.
        assert probe.started_at is None and probe.finished_at is None

    def test_probe_is_queued_and_not_run(self, db_session, pdf_bytes):
        response = _upload(db_session, pdf_bytes)

        node = DocumentRepository(db_session).get(response.document_id)
        assert node.settled == SETTLED_IN_FLIGHT
        # The decisive assertion: nothing opened the document.
        assert "matched" not in (node.structured_content or {})

        queued = TaskQueueRepository(db_session).unfinished_tasks_for(node.id)
        assert [(t.task_type, t.status, t.write_mode) for t in queued] == [
            (TASK_PROBE, TASK_PENDING, "self")
        ]

    def test_the_text_pipeline_stays_synchronous(self, db_session):
        """Spec 5.7: the file pipeline is the FIRST asynchronous one; the others are not.

        `POST /ingest` still runs to completion inside the call and hands back a finished,
        settled tree with no queue rows behind it.
        """
        result = _run_coroutine(
            IngestService(db_session).ingest_content(
                IngestRequest(
                    content="# Heading\n\nA paragraph of prose for the markdown pipeline.",
                    usetype="markdown",
                    title="sync",
                    pipeline_config={"summarize": False, "extract_facts": False},
                )
            )
        )

        node = DocumentRepository(db_session).get(result.source_document_id)
        assert node.settled == SETTLED_SETTLED
        assert TaskQueueRepository(db_session).unfinished_tasks_for(node.id) == []


# ---------------------------------------------------------------------------
# 3. 5.8 — the worker drains the queue
# ---------------------------------------------------------------------------


class TestWorkerDrainsTheQueue:
    def test_draining_runs_the_whole_file_pipeline_and_settles_the_file_node(
        self, db_session, pdf_bytes
    ):
        """probe, extract:text, the rung probe's patterns chose, and an embed per chunk.

        The count is derived from the tree rather than written down: this fixture is one
        page and produces one chunk today, and a chunking change that made it two would
        turn a hardcoded number into a failure about arithmetic instead of about ingestion.
        """
        response = _upload(db_session, pdf_bytes)

        ran = drain_ingest_queue(db_session)

        node = DocumentRepository(db_session).get(response.document_id)
        assert node.structured_content["matched"]["patterns"]["has_outline"] is True
        # The file node's OWN attempts are unchanged by the embed split: `embed` is scoped
        # to each chunk, so its attempt records land there.
        assert [e["task"] for e in node.structured_content["attempts"]] == [
            TASK_PROBE,
            TASK_EXTRACT_TEXT,
            TASK_STRUCTURE_DECLARED,
        ]

        chunks = (
            db_session.execute(
                select(Document)
                .where(Document.path.contains([node.id]))
                .where(Document.usetype == USETYPE_CHUNK)
            )
            .scalars()
            .all()
        )
        assert chunks, "the declared rung wrote no chunks"
        assert ran == 3 + len(chunks)
        for chunk in chunks:
            assert [e["task"] for e in chunk.structured_content["attempts"]] == [TASK_EMBED]
            assert chunk.embed is not None, "the embed task did not write a vector"

        # Every chunk's own task drained, so each settled, so the walk reached the file
        # node and settled that. Before `embed` was its own task this held for a different
        # reason — the chunks were born settled — and the walk never had to travel.
        assert node.settled == SETTLED_SETTLED

    def test_the_pending_entry_becomes_the_outcome_rather_than_gaining_a_sibling(
        self, db_session, pdf_bytes
    ):
        """Spec 3.4: one entry per task ATTEMPT. Pending → completed is one attempt."""
        response = _upload(db_session, pdf_bytes)
        task_id = response.attempts[0].task_id

        drain_ingest_queue(db_session)

        node = DocumentRepository(db_session).get(response.document_id)
        probes = [e for e in node.structured_content["attempts"] if e["task"] == TASK_PROBE]
        assert len(probes) == 1
        assert probes[0]["status"] == "completed"
        assert probes[0]["task_id"] == task_id
        assert probes[0]["attempt"] == 1

    def test_probe_records_what_it_decided_about_every_downstream_task(self, db_session, pdf_bytes):
        """Part 4's decision is auditable from the node, not only from the code."""
        response = _upload(db_session, pdf_bytes)
        drain_ingest_queue(db_session)

        node = DocumentRepository(db_session).get(response.document_id)
        detail = node.structured_content["attempts"][0]["detail"]
        # What it enqueued, in order, with the real queue ids.
        assert list(detail["enqueued"]) == [TASK_EXTRACT_TEXT, TASK_STRUCTURE_DECLARED]
        # And what it decided NOT to enqueue, with the pattern that was false. This
        # document has no images, and the inferred rung is not the one that runs when the
        # file declares an outline.
        assert TASK_EXTRACT_IMAGES in detail["not_applicable"]
        assert TASK_STRUCTURE_INFERRED in detail["not_applicable"]

    def test_a_scanned_pdf_writes_the_skipped_ocr_attempt(self, db_session, temp_handler):
        """Part 4's one spec-mandated `skipped`, written to the node's durable log."""
        pymupdf = pytest.importorskip("pymupdf")
        doc = pymupdf.open()
        page = doc.new_page()
        # A page image and no text: probe's is_scanned heuristic.
        pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40))
        pixmap.set_rect(pixmap.irect, (255, 255, 255))
        page.insert_image(pymupdf.Rect(0, 0, 200, 200), pixmap=pixmap)
        scanned = doc.tobytes()

        response = _upload(db_session, scanned, "scan.pdf")
        drain_ingest_queue(db_session)

        node = DocumentRepository(db_session).get(response.document_id)
        assert node.structured_content["matched"]["patterns"]["is_scanned"] is True
        ocr = [e for e in node.structured_content["attempts"] if e["task"] == TASK_OCR]
        assert len(ocr) == 1
        assert ocr[0]["status"] == "skipped"
        # 3.4: a skip without a reason is an unexplained gap and is rejected outright.
        assert "out of scope" in ocr[0]["detail"]["reason"]

    def test_an_empty_queue_reports_no_work(self, db_session):
        assert drain_ingest_queue(db_session) == 0

    def test_a_handler_that_enqueues_work_is_drained_too(self, db_session, temp_handler):
        """The queue is a frontier: a task may create the next one."""
        docs = DocumentRepository(db_session)
        node = _node(docs, title="root")
        db_session.flush()
        calls: list[str] = []

        def first(session, task):
            calls.append("first")
            TaskQueueRepository(session).enqueue("test:second", task.scope_document_id, "self")
            return TaskOutcome(detail={"chained": True})

        def second(session, task):
            calls.append("second")
            return TaskOutcome()

        temp_handler("test:first", first)
        temp_handler("test:second", second)
        TaskQueueRepository(db_session).enqueue("test:first", node.id, "self")

        assert drain_ingest_queue(db_session) == 2
        assert calls == ["first", "second"]

    def test_the_recorded_duration_is_the_work_and_not_the_transaction(
        self, db_session, temp_handler
    ):
        """``started_at``/``completed_at`` must measure the handler, not two BEGINs.

        Postgres' ``now()`` is the transaction's start time. The worker claims in one
        transaction and completes in another, so a duration computed from ``now()`` is
        the gap between two transaction starts — a few milliseconds for every task in
        the appliance, including LLM calls that took half a minute. Both columns are
        ``clock_timestamp()`` for that reason, and this asserts the property rather than
        the function: a handler that demonstrably took 250 ms must not record 0.
        """
        docs = DocumentRepository(db_session)
        node = _node(docs, title="slow")
        db_session.flush()

        def slow(session, task):
            time.sleep(0.25)
            return TaskOutcome()

        temp_handler("test:slow", slow)
        task_id = TaskQueueRepository(db_session).enqueue("test:slow", node.id, "self").id

        assert drain_ingest_queue(db_session) == 1

        row = db_session.execute(
            text("SELECT started_at, completed_at FROM task_queue WHERE id = :id"),
            {"id": task_id},
        ).one()
        assert (row.completed_at - row.started_at).total_seconds() >= 0.25

        attempt = (DocumentRepository(db_session).get(node.id).structured_content or {})[
            "attempts"
        ][-1]
        started = datetime.fromisoformat(attempt["started_at"])
        finished = datetime.fromisoformat(attempt["finished_at"])
        assert (finished - started).total_seconds() >= 0.25

    def test_the_drain_bound_raises_rather_than_reporting_an_empty_queue(
        self, db_session, temp_handler
    ):
        docs = DocumentRepository(db_session)
        node = _node(docs)
        db_session.flush()

        def forever(session, task):
            TaskQueueRepository(session).enqueue("test:forever", task.scope_document_id, "self")
            return TaskOutcome()

        temp_handler("test:forever", forever)
        TaskQueueRepository(db_session).enqueue("test:forever", node.id, "self")

        with pytest.raises(RuntimeError, match="without the queue going quiet"):
            drain_ingest_queue(db_session, max_tasks=3)


# ---------------------------------------------------------------------------
# 4. Failure: permanent ends it, retryable waits
# ---------------------------------------------------------------------------


class TestFailureClassification:
    def test_a_permanent_failure_marks_the_node_failed_and_does_not_retry(self, db_session):
        """Spec 2.1: without `failed`, a node whose ingestion died permanently is
        indistinguishable from one still in progress."""
        docs = DocumentRepository(db_session)
        # A node with no `file` block. probe raises ValueError → PERMANENT.
        node = _node(docs, title="not an upload")
        db_session.flush()
        task = TaskQueueRepository(db_session).enqueue(TASK_PROBE, node.id, "self")
        task_id = task.id

        assert drain_ingest_queue(db_session) == 1

        tasks = TaskQueueRepository(db_session)
        failed = tasks.get(task_id)
        assert failed.status == TASK_FAILED
        assert failed.error_type == "permanent"
        assert failed.retryable is False
        assert failed.retry_after is None
        assert failed.retry_count == 0
        assert docs.get(node.id).settled == SETTLED_FAILED
        # And the claim will not offer it again.
        assert drain_ingest_queue(db_session) == 0

    def test_the_permanent_failure_is_in_the_nodes_durable_log(self, db_session):
        docs = DocumentRepository(db_session)
        node = _node(docs)
        db_session.flush()
        TaskQueueRepository(db_session).enqueue(TASK_PROBE, node.id, "self")

        drain_ingest_queue(db_session)

        entries = docs.get(node.id).structured_content["attempts"]
        assert len(entries) == 1
        assert entries[0]["status"] == "failed"
        assert entries[0]["error_type"] == "permanent"
        assert "no `file` block" in entries[0]["error"]

    def test_an_unknown_task_type_fails_permanently(self, db_session):
        """A name nobody registered does not start working on the third attempt."""
        docs = DocumentRepository(db_session)
        node = _node(docs)
        db_session.flush()
        task = TaskQueueRepository(db_session).enqueue(
            "nothing:registers-this", node.id, "children"
        )

        drain_ingest_queue(db_session)

        failed = TaskQueueRepository(db_session).get(task.id)
        assert failed.status == TASK_FAILED
        assert failed.error_type == "permanent"
        assert "no handler is registered" in failed.error

    def test_a_retryable_failure_waits_out_its_backoff_then_runs_again(
        self, db_session, temp_handler
    ):
        docs = DocumentRepository(db_session)
        node = _node(docs)
        db_session.flush()
        attempts: list[int] = []

        def flaky(session, task):
            attempts.append(task.retry_count)
            if len(attempts) == 1:
                raise httpx.ConnectError("the model service is not answering")
            return TaskOutcome(detail={"attempt": len(attempts)})

        temp_handler("test:flaky", flaky)
        task = TaskQueueRepository(db_session).enqueue("test:flaky", node.id, "self")
        task_id = task.id

        assert drain_ingest_queue(db_session) == 1

        tasks = TaskQueueRepository(db_session)
        failed = tasks.get(task_id)
        assert failed.status == TASK_FAILED
        assert failed.error_type == "retryable"
        assert failed.retryable is True
        assert failed.retry_after is not None
        # The node is NOT failed: the work is going to run again.
        assert docs.get(node.id).settled == SETTLED_IN_FLIGHT

        # retry_after is in the future, so the claim refuses it. This is the assertion
        # that the backoff is enforced by the CLAIM rather than by a sleeping caller.
        assert drain_ingest_queue(db_session) == 0
        assert attempts == [0]

        # Wind the clock back rather than waiting a minute for it.
        db_session.execute(
            text("UPDATE task_queue SET retry_after = NOW() - INTERVAL '1 second' WHERE id = :i"),
            {"i": task_id},
        )
        db_session.flush()

        assert drain_ingest_queue(db_session) == 1
        succeeded = tasks.get(task_id)
        assert succeeded.status == TASK_COMPLETED
        assert succeeded.retry_count == 1
        assert attempts == [0, 1]

    def test_the_retry_does_not_erase_the_failure_it_followed(self, db_session, temp_handler):
        """Two attempts are two entries. Replacing by task_id alone would lose the first."""
        docs = DocumentRepository(db_session)
        node = _node(docs)
        db_session.flush()
        seen: list[int] = []

        def flaky(session, task):
            seen.append(1)
            if len(seen) == 1:
                raise httpx.ConnectError("transient")
            return TaskOutcome()

        temp_handler("test:flaky2", flaky)
        task = TaskQueueRepository(db_session).enqueue("test:flaky2", node.id, "self")
        drain_ingest_queue(db_session)
        db_session.execute(
            text("UPDATE task_queue SET retry_after = NOW() - INTERVAL '1 second' WHERE id = :i"),
            {"i": task.id},
        )
        db_session.flush()
        drain_ingest_queue(db_session)

        entries = docs.get(node.id).structured_content["attempts"]
        assert [(e["status"], e["attempt"]) for e in entries] == [
            ("failed", 1),
            ("completed", 2),
        ]


# ---------------------------------------------------------------------------
# 4b. 7.3 — a claim left behind by a dead worker
# ---------------------------------------------------------------------------


class TestStaleClaimRecovery:
    """Spec 7.3, machine-task row: *"Stale claim | worker died, requeue"*.

    A row left `claimed` or `running` by a killed process is invisible to everything else
    here: the claim admits only `pending` and retryable `failed`, the unfinished-task
    predicate counts it forever, so its node and every ancestor stay out of the retrieval
    indexes, and the conflict predicate treats it as a live reservation so a re-enqueued
    task on the same node cannot be claimed either. Nothing times out.
    """

    def _abandoned(self, db_session, temp_handler, worker_id="dead-worker"):
        """A task this worker claimed and marked running, with the worker then gone."""
        docs = DocumentRepository(db_session)
        node = _node(docs)
        db_session.flush()
        ran: list[int] = []
        temp_handler("test:revived", lambda session, task: ran.append(task.id) or TaskOutcome())
        tasks = TaskQueueRepository(db_session)
        task = tasks.enqueue("test:revived", node.id, "self")
        db_session.flush()
        claimed = tasks.claim_next(worker_id)
        tasks.mark_running(claimed)
        db_session.flush()
        return docs, node, task.id, ran

    def test_the_zombie_row_blocks_everything_until_it_is_recovered(self, db_session, temp_handler):
        docs, node, task_id, ran = self._abandoned(db_session, temp_handler)

        # Before recovery: unclaimable, and the node can never settle.
        assert drain_ingest_queue(db_session) == 0
        assert TaskQueueRepository(db_session).structuring_complete(node.id) is False
        assert ran == []

        recovered = TaskQueueRepository(db_session).requeue_stale_claims("dead-worker")

        assert recovered == [task_id]
        assert drain_ingest_queue(db_session) == 1
        assert ran == [task_id]
        assert TaskQueueRepository(db_session).get(task_id).status == TASK_COMPLETED
        assert docs.get(node.id).settled == SETTLED_SETTLED

    def test_the_lost_run_is_in_the_durable_log_and_costs_a_retry(self, db_session, temp_handler):
        """Recorded as a failure rather than silently flipped back to `pending`: spec 5.6
        wants the durable record, and the retry cap is what stops a task that kills the
        process from crash-looping the appliance forever."""
        docs, node, task_id, _ = self._abandoned(db_session, temp_handler)

        TaskQueueRepository(db_session).requeue_stale_claims("dead-worker")

        entries = docs.get(node.id).structured_content["attempts"]
        assert [e["status"] for e in entries] == ["failed"]
        assert "was holding this task" in entries[0]["error"]
        assert entries[0]["detail"]["requeued_from"] == "running"
        assert TaskQueueRepository(db_session).get(task_id).retryable is True

    def test_a_task_that_keeps_killing_the_worker_ends_as_a_failed_node(
        self, db_session, temp_handler
    ):
        """The cap bites: when the retries run out the node goes to `failed` (2.1) rather
        than hanging in flight forever."""
        docs, node, task_id, _ = self._abandoned(db_session, temp_handler)
        tasks = TaskQueueRepository(db_session)
        db_session.execute(
            text("UPDATE task_queue SET retry_count = max_retries WHERE id = :i"), {"i": task_id}
        )
        db_session.expire_all()

        tasks.requeue_stale_claims("dead-worker")

        assert tasks.get(task_id).retryable is True  # the classification is unchanged...
        assert tasks.get(task_id).retry_after is None  # ...but nothing more is scheduled
        assert docs.get(node.id).settled == SETTLED_FAILED

    def test_another_workers_claim_is_left_alone(self, db_session, temp_handler):
        """Scoped to this worker's own id, which is what makes it safe without a lease: a
        worker that is starting cannot also be running the task its previous incarnation
        claimed. Another worker's live claim is exactly what it looks like."""
        _, _, task_id, _ = self._abandoned(db_session, temp_handler, worker_id="other-worker")

        assert TaskQueueRepository(db_session).requeue_stale_claims("this-worker") == []
        assert TaskQueueRepository(db_session).get(task_id).status == "running"

    def test_the_worker_recovers_its_own_claims_without_starting_a_thread(
        self, db_session, temp_handler
    ):
        docs, node, task_id, ran = self._abandoned(db_session, temp_handler, worker_id="restarted")
        worker = IngestWorker(
            worker_id="restarted", session_factory=lambda: _borrowed_session(db_session)
        )

        assert worker.recover_own_claims() == [task_id]
        assert worker.drain() == 1
        assert ran == [task_id]


# ---------------------------------------------------------------------------
# 5. The thread
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not DB_READY, reason="test database not provisioned")
class TestWorkerThread:
    """The only property a thread has that ``run_once`` does not: it starts and it joins.

    Deliberately NOT taking ``db_session``: the thread opens its own connection and would
    not see a fixture's uncommitted rows anyway. It runs against an idle queue.
    """

    def test_it_starts_and_stops_cleanly(self):
        worker = IngestWorker(
            worker_id="test-thread-worker", session_factory=get_session, poll_seconds=0.01
        )
        worker.start()
        try:
            assert worker.running
        finally:
            worker.stop(timeout=10)
        assert not worker.running

    def test_stopping_a_worker_that_never_started_is_harmless(self):
        IngestWorker(worker_id="never-started").stop()

    def test_starting_twice_is_an_error_rather_than_a_second_loop(self):
        worker = IngestWorker(
            worker_id="test-double-start", session_factory=get_session, poll_seconds=0.01
        )
        worker.start()
        try:
            with pytest.raises(RuntimeError, match="already running"):
                worker.start()
        finally:
            worker.stop(timeout=10)

    def test_the_suite_runs_with_the_worker_disabled(self):
        """Explicit, not implicit: conftest pins JMFTS_INGEST_WORKER_ENABLED=0, so the
        lifespan in `with TestClient(app)` does not spin a real poll loop under an
        unrelated test."""
        from jmfts_core.config import get_settings

        assert get_settings().ingest_worker_enabled is False


# ---------------------------------------------------------------------------
# 6. 2.4 — the frontier
# ---------------------------------------------------------------------------


class TestFrontier:
    def test_it_reports_the_frontier_mid_run_and_after(self, client_with_db, db_session, pdf_bytes):
        response = _upload(db_session, pdf_bytes)
        db_session.flush()

        mid = client_with_db.get(f"/ingest/file/{response.document_id}/frontier")
        assert mid.status_code == 200, mid.text
        body = mid.json()
        assert body["document_id"] == response.document_id
        assert body["settled"] == SETTLED_IN_FLIGHT
        assert body["nodes_in_flight"] == 1
        assert body["nodes_settled"] == 0
        assert body["nodes_failed"] == 0
        assert body["nodes_total"] == 1
        assert body["tasks_unfinished"] == 1

        drain_ingest_queue(db_session)

        after = client_with_db.get(f"/ingest/file/{response.document_id}/frontier").json()
        assert after["settled"] == SETTLED_SETTLED
        # The subtree extraction built is counted too, and all of it settled — the point
        # of the endpoint is the whole region, not the one row the upload created.
        assert after["nodes_total"] > 1
        assert after["nodes_settled"] == after["nodes_total"]
        assert after["nodes_in_flight"] == 0
        assert after["tasks_unfinished"] == 0

    def test_it_counts_the_whole_subtree_by_state(self, client_with_db, db_session):
        docs = DocumentRepository(db_session)
        root = _node(docs, title="file", settled=SETTLED_IN_FLIGHT)
        _node(docs, parent=root, title="done", settled=SETTLED_SETTLED)
        _node(docs, parent=root, title="also done", settled=SETTLED_SETTLED)
        _node(docs, parent=root, title="working", settled=SETTLED_IN_FLIGHT)
        _node(docs, parent=root, title="broken", settled=SETTLED_FAILED)
        db_session.flush()

        body = client_with_db.get(f"/ingest/file/{root.id}/frontier").json()

        assert body["nodes_settled"] == 2
        assert body["nodes_in_flight"] == 2  # the root and the working child
        assert body["nodes_failed"] == 1
        assert body["nodes_total"] == 5
        # No queue rows exist for this hand-built tree.
        assert body["tasks_unfinished"] == 0

    def test_unfinished_tasks_count_the_whole_subtree(self, client_with_db, db_session):
        docs = DocumentRepository(db_session)
        root = _node(docs, title="file")
        child = _node(docs, parent=root, title="section")
        db_session.flush()
        tasks = TaskQueueRepository(db_session)
        tasks.enqueue(TASK_PROBE, root.id, "self")
        tasks.enqueue("test:whatever", child.id, "children")
        db_session.flush()

        body = client_with_db.get(f"/ingest/file/{root.id}/frontier").json()

        assert body["tasks_unfinished"] == 2

    def test_an_unknown_document_is_a_404_not_a_row_of_zeros(self, client_with_db):
        """A caller polling for progress would read all-zeros as 'finished'."""
        assert client_with_db.get("/ingest/file/10000000/frontier").status_code == 404


# ---------------------------------------------------------------------------
# The lease — migration 011, and the fleet the in-process worker grew into
# ---------------------------------------------------------------------------


class TestHeartbeatLease:
    """Recovering a worker that never comes back.

    ``requeue_stale_claims`` recovers a RESTARTING worker's own rows and is provably safe
    with no clock, because a process that is starting cannot also be running the task its
    previous incarnation claimed. The cases it structurally cannot reach are the ones a
    fleet produces: a pod rescheduled under a new id (nothing matches the dead
    ``claimed_by``) and a host that stays down (no worker ever starts to do the matching).

    The property that separates this design from "requeue anything claimed longer than N"
    is asserted directly in
    :meth:`test_a_slow_task_that_is_still_beating_is_not_reaped`. A lease keyed on elapsed
    RUNTIME would reap a long `summarize` and run it a second time, concurrently, against
    the same tree region.
    """

    def _held(self, db_session, temp_handler, worker_id="dead-worker"):
        """A task claimed and marked running by ``worker_id``."""
        docs = DocumentRepository(db_session)
        node = _node(docs)
        db_session.flush()
        ran: list[int] = []
        temp_handler("test:leased", lambda session, task: ran.append(task.id) or TaskOutcome())
        tasks = TaskQueueRepository(db_session)
        task = tasks.enqueue("test:leased", node.id, "self")
        db_session.flush()
        claimed = tasks.claim_next(worker_id)
        tasks.mark_running(claimed)
        db_session.flush()
        return docs, node, task.id, ran

    def _age_heartbeat(self, db_session, task_id, seconds):
        """Backdate the beat, so the test does not have to wait out a real lease."""
        db_session.execute(
            text(
                "UPDATE task_queue SET heartbeat_at = clock_timestamp() "
                "- make_interval(secs => :s) WHERE id = :i"
            ),
            {"s": seconds, "i": task_id},
        )
        db_session.expire_all()

    def test_the_claim_stamps_a_first_beat(self, db_session, temp_handler):
        """So a worker that dies between claiming and starting its heartbeat thread still
        leaves a timestamp, and the reaper never has to guess what NULL meant."""
        _, _, task_id, _ = self._held(db_session, temp_handler)
        assert TaskQueueRepository(db_session).get(task_id).heartbeat_at is not None

    def test_a_worker_that_stops_beating_loses_the_task_to_another_worker(
        self, db_session, temp_handler
    ):
        """The case `requeue_stale_claims` cannot reach: the dead worker's id is never
        seen again, so recovery must not depend on matching it."""
        docs, node, task_id, ran = self._held(db_session, temp_handler)
        tasks = TaskQueueRepository(db_session)

        # A differently-named worker recovers nothing by the old mechanism.
        assert tasks.requeue_stale_claims("some-other-worker") == []
        assert drain_ingest_queue(db_session) == 0
        assert tasks.structuring_complete(node.id) is False

        self._age_heartbeat(db_session, task_id, 120)
        assert tasks.requeue_expired_claims(90) == [task_id]

        assert drain_ingest_queue(db_session) == 1
        assert ran == [task_id]
        assert tasks.get(task_id).status == TASK_COMPLETED
        assert docs.get(node.id).settled == SETTLED_SETTLED

    def test_a_slow_task_that_is_still_beating_is_not_reaped(self, db_session, temp_handler):
        """THE point of a heartbeat. This task has been running far longer than the lease;
        what keeps it is that its worker is still reporting in. A lease measured against
        `started_at` — the "requeue anything claimed longer than N" design this replaced —
        would take it away and run it twice."""
        _, _, task_id, _ = self._held(db_session, temp_handler)
        tasks = TaskQueueRepository(db_session)

        db_session.execute(
            text(
                "UPDATE task_queue SET started_at = clock_timestamp() "
                "- make_interval(secs => 3600) WHERE id = :i"
            ),
            {"i": task_id},
        )
        assert tasks.touch_heartbeat(task_id) is True
        db_session.expire_all()

        assert tasks.requeue_expired_claims(90) == []
        assert tasks.get(task_id).status == TASK_RUNNING

    def test_the_reaped_run_is_in_the_durable_log_and_costs_a_retry(self, db_session, temp_handler):
        """Through `fail()`, exactly as `requeue_stale_claims` goes: spec 5.6 wants the
        record, and the retry cap is what stops a task that reliably kills its worker from
        crash-looping the whole fleet."""
        docs, node, task_id, _ = self._held(db_session, temp_handler)
        self._age_heartbeat(db_session, task_id, 120)

        TaskQueueRepository(db_session).requeue_expired_claims(90)

        entries = docs.get(node.id).structured_content["attempts"]
        assert [e["status"] for e in entries] == ["failed"]
        assert "stopped reporting in" in entries[0]["error"]
        assert entries[0]["detail"]["reason"] == "lease expired"
        assert entries[0]["detail"]["worker_id"] == "dead-worker"
        assert TaskQueueRepository(db_session).get(task_id).retryable is True

    def test_the_retry_is_immediate_not_backed_off(self, db_session, temp_handler):
        """The backoff decorrelates tasks that failed against one overloaded dependency. A
        worker that stopped beating is not that, and the work should move to a live worker
        now."""
        _, _, task_id, _ = self._held(db_session, temp_handler)
        self._age_heartbeat(db_session, task_id, 120)

        tasks = TaskQueueRepository(db_session)
        tasks.requeue_expired_claims(90)

        assert tasks.claim_next("live-worker") is not None

    def test_a_task_that_keeps_killing_its_worker_ends_as_a_failed_node(
        self, db_session, temp_handler
    ):
        docs, node, task_id, _ = self._held(db_session, temp_handler)
        tasks = TaskQueueRepository(db_session)
        db_session.execute(
            text("UPDATE task_queue SET retry_count = max_retries WHERE id = :i"), {"i": task_id}
        )
        self._age_heartbeat(db_session, task_id, 120)

        tasks.requeue_expired_claims(90)

        assert tasks.get(task_id).retry_after is None
        assert docs.get(node.id).settled == SETTLED_FAILED

    def test_a_beat_cannot_resurrect_a_row_somebody_else_already_reaped(
        self, db_session, temp_handler
    ):
        """The beat is scoped to the active statuses. Once the row is 'failed', the worker
        that was holding it no longer owns it, and a late beat must not make it look live —
        which is also how the beating thread learns to stop."""
        _, _, task_id, _ = self._held(db_session, temp_handler)
        tasks = TaskQueueRepository(db_session)
        self._age_heartbeat(db_session, task_id, 120)
        tasks.requeue_expired_claims(90)

        assert tasks.touch_heartbeat(task_id) is False

    def test_only_one_worker_reaps_per_round(self, db_session, temp_handler):
        """`with_reaper_lock` is what keeps every worker running the sweep on its own timer
        from meaning N workers reap the same rows at once. Losing it is the normal case."""
        tasks = TaskQueueRepository(db_session)
        assert tasks.with_reaper_lock() is True

        from jmfts_core.database import get_session

        with get_session() as other:
            assert TaskQueueRepository(other).with_reaper_lock() is False


class TestHeartbeatThread:
    """The beating thread itself: it must use a session of its OWN.

    The handler runs inside one long transaction. An UPDATE issued on that session is
    invisible to every other connection until it commits — which is exactly when the beat
    stops being needed — so a beat sharing the work's transaction reports nothing to
    anyone. These tests assert the mechanics without a database, because what is being
    tested is the threading, not the SQL.
    """

    def _worker(self):
        from contextlib import contextmanager

        import jmfts_core.ingest_worker as worker_module

        # The session is never used: touch_heartbeat is monkeypatched in every test here.
        @contextmanager
        def factory():
            yield object()

        return worker_module.IngestWorker(
            worker_id="beater",
            session_factory=factory,
            heartbeat_seconds=0.02,
            lease_seconds=90.0,
        )

    def test_it_beats_while_the_block_runs_and_stops_after(self, monkeypatch):
        calls: list[int] = []
        monkeypatch.setattr(
            TaskQueueRepository,
            "touch_heartbeat",
            lambda self, task_id: calls.append(task_id) or True,
        )
        worker = self._worker()

        with worker._heartbeat(7):
            time.sleep(0.15)
        beats_during = len(calls)

        assert beats_during >= 2, f"expected repeated beats, got {beats_during}"
        time.sleep(0.1)
        assert len(calls) == beats_during, "the thread kept beating after the block exited"

    def test_it_stops_when_the_row_is_no_longer_active(self, monkeypatch):
        """Another worker has reaped the task. Continuing to beat would be claiming
        liveness for a row somebody else now owns."""
        calls: list[int] = []
        monkeypatch.setattr(
            TaskQueueRepository,
            "touch_heartbeat",
            lambda self, task_id: calls.append(task_id) or False,
        )
        worker = self._worker()

        with worker._heartbeat(7):
            time.sleep(0.15)

        assert len(calls) == 1, f"expected one beat then a stop, got {len(calls)}"

    def test_a_failing_beat_does_not_kill_the_task(self, monkeypatch):
        """The thread cannot interrupt the handler, and a database blip is what the lease
        exists to ride out. Raising here would turn a blip into a failed task."""
        calls: list[int] = []

        def explode(self, task_id):
            calls.append(task_id)
            raise RuntimeError("database went away")

        monkeypatch.setattr(TaskQueueRepository, "touch_heartbeat", explode)
        worker = self._worker()

        with worker._heartbeat(7):
            time.sleep(0.15)

        assert len(calls) >= 2, "a failed beat should be retried, not fatal"


class TestWorkerProcessShutdown:
    """``python -m jmfts_core.worker``'s signal handling.

    Found in a live run rather than by this suite: the first version logged and called
    ``worker.stop()`` from inside the signal handler. A SIGTERM that landed while the main
    thread was inside logging's ``stream.flush()`` raised a reentrancy error out of the
    shutdown path — a Python signal handler runs on the main thread between bytecodes, so
    it can interrupt code holding a lock and must not try to take that lock again.
    ``stop()`` is worse still: it joins a thread, and blocking in a handler blocks the
    interpreter that has to run the thread being waited for.
    """

    def test_the_handler_only_sets_a_flag(self):
        """Asserted on the source, because the failure needs a signal to land inside a
        lock the handler then re-enters — reproducing that reliably in a test is harder
        than stating the rule the handler has to obey."""
        import ast
        import inspect

        import jmfts_core.worker as worker_module

        tree = ast.parse(inspect.getsource(worker_module))
        handlers = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "request_stop"
        ]
        assert len(handlers) == 1, "expected exactly one signal handler in jmfts_core.worker"

        called = {
            node.func.attr
            for node in ast.walk(handlers[0])
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        forbidden = called & {"info", "warning", "error", "exception", "debug", "stop", "join"}
        assert not forbidden, (
            f"the signal handler calls {sorted(forbidden)}; it must only set an Event. "
            "Logging from a handler can re-enter logging's own lock, and stop()/join() "
            "block the interpreter that has to run the thread being waited for."
        )

    def test_build_worker_carries_the_rollup_planner(self):
        """With NO_ROLLUP every tree settles without ever being summarised — which looks
        like success and produces a tree nothing can retrieve."""
        from jmfts_core.rollup_tasks import IngestRollupPlanner
        from jmfts_core.worker import build_parser, build_worker

        worker = build_worker(build_parser().parse_args(["--badge", "embed"]))

        assert isinstance(worker.planner, IngestRollupPlanner)
        assert worker.service_badges == ["embed"]

    def test_repeated_badge_flags_accumulate(self):
        """A host running a local LLM on a GPU answers both pools; one badge per worker
        would leave it idle whenever the other kind of work was queued."""
        from jmfts_core.worker import build_parser, build_worker

        worker = build_worker(build_parser().parse_args(["--badge", "embed", "--badge", "llm"]))

        assert worker.service_badges == ["embed", "llm"]

    def test_the_env_form_of_the_badge_list_is_comma_separated(self, monkeypatch):
        """A container sets one variable, not a repeated flag."""
        from jmfts_core.worker import build_parser, build_worker

        monkeypatch.setenv("JMFTS_WORKER_BADGE", "llm, embed ,")

        worker = build_worker(build_parser().parse_args([]))

        # Blank entries dropped: a badge named "" matches nothing and would silently
        # reduce the worker to un-badged work only.
        assert worker.service_badges == ["llm", "embed"]

    def test_a_lease_shorter_than_the_beat_is_refused_at_construction(self):
        """The ratio is checked in Settings AND here, because a caller can build a worker
        directly and the failure it prevents — a live worker's task reaped and run twice,
        concurrently — does not show up as a crash."""
        from jmfts_core.worker import build_parser, build_worker

        args = build_parser().parse_args(["--heartbeat-seconds", "10", "--lease-seconds", "20"])

        with pytest.raises(ValueError, match="at least"):
            build_worker(args)
