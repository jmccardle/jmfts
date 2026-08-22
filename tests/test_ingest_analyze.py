"""`ANALYZE` for file ingestion — `INGEST_SPEC.md` 11.2, second mode.

`EXPLAIN` reasons about a format. `ANALYZE` is handed the bytes, so the load-bearing test
here is a different one from `tests/test_ingest_explain.py`'s: not "does the explanation
agree with the planner" but **does the forecast agree with the run**. So
:class:`TestAnalyzeForecastsTheRealRun` analyses a PDF, then uploads the same bytes and
drains the queue, and asserts the tasks `analyze` said would be enqueued are exactly the
ones `probe` enqueued, with the patterns it said would be measured.

The second property, asserted just as hard, is that analysing writes NOTHING: 11.2 says
`ANALYZE` runs `probe` and stops, and an endpoint that quietly left a node, a blob or a
queue row behind would make "analyse the corpus first" an expensive mistake rather than
the cheap check it is meant to be.
"""

from __future__ import annotations

import hashlib
import io
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from jmfts_core.rest.main import app
from jmfts_client.contracts.upload import UploadedFile
from jmfts_core.database import get_db
from jmfts_core.ingest_options import STRUCTURE_CHUNK_PARAMS
from jmfts_core.ingest_tasks import (
    OUTCOME_ENQUEUED,
    PATTERNS_PROBED,
    TASK_EXTRACT_TEXT,
    TASK_PROBE,
    TASK_STRUCTURE_DECLARED,
    TASK_STRUCTURE_INFERRED,
    explain_plan,
)
from jmfts_core.models.document import Document
from jmfts_core.models.document_blob import DocumentBlob
from jmfts_core.models.task_queue import TaskQueue
from jmfts_core.probe import detect_format, probe_patterns
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.services.ingest_service import IngestService
from jmfts_core.task_errors import ErrorType
from tests.conftest import drain_ingest_queue

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_fixture_pdf() -> bytes:
    """A 2-page PDF with a real text layer and a 2-level, 3-entry outline.

    The same fixture `tests/test_file_upload.py` builds, and deliberately so: the forecast
    test below compares this file's analysis against this file's real ingestion, and both
    halves have to be looking at a document whose structure the test states.
    """
    pymupdf = pytest.importorskip("pymupdf")

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 60), "Chapter One", fontsize=24)
    page.insert_text(
        (50, 120),
        "Body text on the first page, long enough that the average characters "
        "per page is comfortably above the scanned-document threshold.",
        fontsize=12,
    )
    page2 = doc.new_page()
    page2.insert_text((50, 60), "Chapter Two", fontsize=24)
    page2.insert_text((50, 120), "More body text on the second page of the fixture.", fontsize=12)
    doc.set_toc([[1, "Chapter One", 1], [2, "Section 1.1", 1], [1, "Chapter Two", 2]])
    return doc.tobytes()


@pytest.fixture(scope="module")
def pdf_bytes() -> bytes:
    return _make_fixture_pdf()


#: A .md file, as an author writes one. `detect_format` sniffs it as `text` — there is no
#: markdown magic number and the extension is not evidence — and the text prober reads it
#: from there.
MARKDOWN_BYTES = b"""# Title

An opening paragraph before any subheading.

## First section

Some prose under the first heading.
"""


def _make_docx_like_zip() -> bytes:
    """A ZIP carrying `word/document.xml`: detected as docx by manifest, and now probed."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<document/>")
    return buffer.getvalue()


def _make_epub_like_zip() -> bytes:
    """A ZIP EPUB's `mimetype` member identifies, and which no prober can look inside.

    The office probers arrived with OFFICE_SPEC.md phasing step 4, so `.docx` is no longer
    an example of a format probe cannot open. EPUB is: its outline is an `.ncx` or a nav
    document, and nothing reads either yet.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", "<container/>")
    return buffer.getvalue()


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


def _analyze(session, data: bytes, filename: str, mime=None, options=None, private=False):
    return IngestService(session).analyze_ingest(
        UploadedFile(data=data, filename=filename, content_type=mime),
        options=options,
        private=private,
    )


def _outcomes(response) -> dict[str, str]:
    return {task.task: task.outcome for task in response.plan.tasks}


def _counts(session) -> tuple[int, int]:
    documents = session.execute(select(func.count()).select_from(Document)).scalar_one()
    tasks = session.execute(select(func.count()).select_from(TaskQueue)).scalar_one()
    return documents, tasks


# ---------------------------------------------------------------------------
# The forecast is the run
# ---------------------------------------------------------------------------


class TestAnalyzeForecastsTheRealRun:
    """What `analyze` says will happen is what happens when the same bytes are uploaded."""

    def test_the_enqueued_tasks_are_the_ones_probe_enqueues(self, db_session, pdf_bytes):
        forecast = _analyze(db_session, pdf_bytes, "annual.pdf", "application/pdf")
        predicted = [t.task for t in forecast.plan.tasks if t.outcome == OUTCOME_ENQUEUED]

        uploaded = IngestService(db_session).upload_file(
            UploadedFile(data=pdf_bytes, filename="annual.pdf", content_type="application/pdf")
        )
        drain_ingest_queue(db_session)
        node = DocumentRepository(db_session).get(uploaded.document_id)
        attempts = (node.structured_content or {}).get("attempts") or []
        probe_attempt = next(a for a in attempts if a["task"] == TASK_PROBE)

        # `probe` itself is enqueued by the upload, not by probe, so it leads the forecast
        # and is absent from probe's own `enqueued` detail.
        #
        # Compared as SETS, because the right-hand side came back out of a `jsonb` column
        # and `jsonb` stores object keys sorted by length and then by bytes — the sequence
        # there is Postgres's, not the planner's. What this test is for is that `analyze`
        # names the same TASKS the run queues; the order the batch goes in is asserted
        # against `plan_after_probe` directly, in tests/test_ingest_worker.py.
        assert predicted[0] == TASK_PROBE
        assert set(predicted[1:]) == set(probe_attempt["detail"]["enqueued"])

    def test_the_measured_patterns_are_the_ones_probe_writes(self, db_session, pdf_bytes):
        forecast = _analyze(db_session, pdf_bytes, "annual.pdf", "application/pdf")

        uploaded = IngestService(db_session).upload_file(
            UploadedFile(data=pdf_bytes, filename="annual.pdf", content_type="application/pdf")
        )
        drain_ingest_queue(db_session)
        node = DocumentRepository(db_session).get(uploaded.document_id)

        assert forecast.patterns == node.structured_content["matched"]["patterns"]
        assert forecast.format == node.structured_content["matched"]["format"]

    def test_the_skip_and_not_applicable_reasons_are_spelled_the_same(self, db_session, pdf_bytes):
        forecast = _analyze(db_session, pdf_bytes, "annual.pdf", "application/pdf")

        uploaded = IngestService(db_session).upload_file(
            UploadedFile(data=pdf_bytes, filename="annual.pdf", content_type="application/pdf")
        )
        drain_ingest_queue(db_session)
        node = DocumentRepository(db_session).get(uploaded.document_id)
        attempts = (node.structured_content or {}).get("attempts") or []
        detail = next(a for a in attempts if a["task"] == TASK_PROBE)["detail"]

        by_task = {t.task: t for t in forecast.plan.tasks}
        for task, reason in detail["not_applicable"].items():
            assert by_task[task].reason == reason
        for task, reason in detail["skipped"].items():
            assert by_task[task].reason == reason
        for task, reason in detail["deferred"].items():
            assert by_task[task].reason == reason

    def test_the_plan_is_explain_plan_with_the_probed_patterns(self, db_session, pdf_bytes):
        """`analyze` measures the third input; it does not re-derive the other two."""
        forecast = _analyze(db_session, pdf_bytes, "annual.pdf", "application/pdf")
        detection = detect_format(pdf_bytes, filename="annual.pdf")
        patterns, _ = probe_patterns(pdf_bytes, detection)

        direct = explain_plan(detection.format, None, patterns, patterns_source=PATTERNS_PROBED)
        assert [(t.task, t.outcome, t.reason) for t in forecast.plan.tasks] == [
            (t.task, t.outcome, t.reason) for t in direct.tasks
        ]


# ---------------------------------------------------------------------------
# It runs probe and stops
# ---------------------------------------------------------------------------


class TestAnalyzeWritesNothing:
    def test_no_document_and_no_queue_row(self, db_session, pdf_bytes):
        before = _counts(db_session)
        _analyze(db_session, pdf_bytes, "annual.pdf", "application/pdf")
        assert _counts(db_session) == before

    def test_analysing_twice_is_the_same_answer(self, db_session, pdf_bytes):
        """No state accumulates, so the second call cannot see anything the first left.

        `probe_detail.elapsed_ms` is excluded, and it is the one field that has to be: it
        measures how long the probe TOOK, which is a fact about this machine at this moment
        rather than about the file. Everything else — the patterns, the plan, the file
        block — is a function of the bytes and must not move between two calls.
        """
        first = _analyze(db_session, pdf_bytes, "annual.pdf", "application/pdf").model_dump()
        second = _analyze(db_session, pdf_bytes, "annual.pdf", "application/pdf").model_dump()

        assert first["probe_detail"].pop("elapsed_ms") >= 0
        assert second["probe_detail"].pop("elapsed_ms") >= 0
        assert first == second

    def test_no_blob_is_stored(self, db_session, pdf_bytes):
        """A delta, not an absolute count: the analysed bytes must add no blob row.

        `document_blobs` is the row that owns a Postgres large object, so a leaked one
        here would be 40 MB on disk per corpus file somebody asked a question about.
        """
        before = db_session.execute(select(func.count()).select_from(DocumentBlob)).scalar_one()
        _analyze(db_session, pdf_bytes, "annual.pdf", "application/pdf")
        after = db_session.execute(select(func.count()).select_from(DocumentBlob)).scalar_one()
        assert after == before


# ---------------------------------------------------------------------------
# Where the patterns came from
# ---------------------------------------------------------------------------


class TestProvenance:
    def test_a_probed_plan_says_so_and_is_decided(self, db_session, pdf_bytes):
        response = _analyze(db_session, pdf_bytes, "annual.pdf", "application/pdf")
        assert response.plan.patterns_source == PATTERNS_PROBED
        assert response.plan.patterns_known is True
        assert all(task.outcome != "conditional" for task in response.plan.tasks)

    def test_a_format_with_no_prober_is_still_probed(self, db_session):
        """`probe` RAN on these bytes and measured nothing, because it has no prober.

        Not `no_prober`: that is `EXPLAIN`'s answer, where nothing was opened. Here the
        bytes were read and identified, and `probe_detail` carries the reason the pattern
        set is empty — which is the fact a caller needs in order to tell "this file has no
        text layer" from "we cannot look inside this format".
        """
        response = _analyze(db_session, _make_epub_like_zip(), "book.epub")

        assert response.format == "epub"
        assert response.patterns == {}
        assert response.plan.patterns_source == PATTERNS_PROBED
        assert response.probe_detail["no_prober_for_format"] == "epub"
        assert "pdf" in response.probe_detail["probers_available"]

    def test_markdown_reaches_the_declared_rung(self, db_session):
        """The state 11.3 was written to change, now recorded the other way round.

        A `.md` file used to get `probe` and nothing else. It now gets the text prober's
        `has_headings`, which is spec 3.5's DECLARED rung for markdown — the author typed
        those markers — so the same three tasks a PDF with an outline gets.
        """
        response = _analyze(db_session, MARKDOWN_BYTES, "notes.md", "text/markdown")
        outcomes = _outcomes(response)

        assert response.format == "text"
        assert response.patterns["has_text_layer"] is True
        assert response.patterns["has_headings"] is True
        assert response.patterns["heading_count"] == 2
        assert response.patterns["has_markup"] is False
        assert outcomes[TASK_PROBE] == OUTCOME_ENQUEUED
        assert outcomes[TASK_EXTRACT_TEXT] == OUTCOME_ENQUEUED
        assert outcomes[TASK_STRUCTURE_DECLARED] == OUTCOME_ENQUEUED
        assert outcomes[TASK_STRUCTURE_INFERRED] == "not_applicable"

    def test_a_docx_is_identified_from_the_archive_not_the_name(self, db_session):
        response = _analyze(db_session, _make_docx_like_zip(), "report.txt", "text/plain")
        assert response.format == "docx"
        assert response.file.detected_by == "zip_manifest"


# ---------------------------------------------------------------------------
# The file block
# ---------------------------------------------------------------------------


class TestTheFileBlock:
    def test_the_hash_and_size_are_the_bytes_that_were_sent(self, db_session, pdf_bytes):
        response = _analyze(db_session, pdf_bytes, "annual.pdf", "application/pdf")
        assert response.file.content_hash == f"sha256:{hashlib.sha256(pdf_bytes).hexdigest()}"
        assert response.file.byte_size == len(pdf_bytes)

    def test_the_bytes_overrule_the_name(self, db_session, pdf_bytes):
        response = _analyze(db_session, pdf_bytes, "annual.txt", "text/plain")
        assert response.format == "pdf"
        assert response.file.detected_by == "magic_bytes"

    def test_a_mime_disagreement_is_visible_before_anything_is_stored(self, db_session, pdf_bytes):
        response = _analyze(db_session, pdf_bytes, "annual.pdf", "text/plain")
        assert response.file.declared_mime == "text/plain"
        assert response.file.detected_mime == "application/pdf"
        assert response.file.mime_agrees is False

    def test_unrecognised_bytes_report_no_evidence_rather_than_a_guess(self, db_session):
        response = _analyze(db_session, b"\x00\x01\x02\x03binary", "thing.bin", None)
        assert response.file.detected_mime is None
        assert response.file.detected_by is None
        assert response.file.mime_agrees is None
        assert response.format == "bin"


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


class TestOptions:
    def test_resolved_options_land_on_the_tasks_that_take_them(self, db_session, pdf_bytes):
        response = _analyze(
            db_session,
            pdf_bytes,
            "annual.pdf",
            "application/pdf",
            options={"structure": {"max_tokens": 60}},
        )
        by_task = {t.task: t for t in response.plan.tasks}
        assert by_task[TASK_STRUCTURE_DECLARED].params["max_tokens"] == 60
        assert response.plan.options["structure"]["max_tokens"] == 60

    def test_the_defaults_are_the_shipped_ones(self, db_session, pdf_bytes):
        response = _analyze(db_session, pdf_bytes, "annual.pdf", "application/pdf")
        assert response.plan.options["structure"] == STRUCTURE_CHUNK_PARAMS

    def test_an_unknown_option_is_refused_here_as_it_is_on_upload(self, db_session, pdf_bytes):
        with pytest.raises(ValueError, match="max_token"):
            _analyze(
                db_session,
                pdf_bytes,
                "annual.pdf",
                "application/pdf",
                options={"structure": {"max_token": 60}},
            )

    def test_options_are_checked_against_the_detected_format(self, db_session, pdf_bytes):
        """Named for the property, not the current table: today no profile deviates."""
        response = _analyze(
            db_session, pdf_bytes, "annual.txt", "text/plain", options={"structure": {}}
        )
        assert response.plan.format == "pdf"


# ---------------------------------------------------------------------------
# Refusals and failures
# ---------------------------------------------------------------------------


class TestRefusals:
    def test_an_empty_file_is_refused_exactly_as_on_upload(self, db_session):
        with pytest.raises(ValueError, match="empty"):
            _analyze(db_session, b"", "empty.pdf", "application/pdf")

    def test_a_nameless_upload_is_refused(self, db_session, pdf_bytes):
        with pytest.raises(ValueError, match="filename"):
            _analyze(db_session, pdf_bytes, "", "application/pdf")


class TestAProbeThatWouldFail:
    def test_broken_pdf_bytes_report_the_failure_and_no_plan(self, db_session):
        """Bytes wearing a PDF header that PyMuPDF will not open.

        The run would raise here too. So the forecast is the failure and its
        classification — the same one the worker would record — and NOT a plan built on an
        empty pattern set, which would read as "nothing applies to this file" when the
        truth is that nothing could be measured.
        """
        pytest.importorskip("pymupdf")
        response = _analyze(db_session, b"%PDF-1.4\nthis is not a PDF", "broken.pdf", None)

        assert response.plan is None
        assert response.probe_failed is not None
        assert response.probe_failed.error
        assert response.probe_failed.error_type in {e.value for e in ErrorType}
        assert response.format == "pdf"
        assert response.file.detected_by == "magic_bytes"

    def test_a_zero_page_pdf_is_damaged_rather_than_failed(self, db_session):
        """PyMuPDF opens this without complaint, so probe succeeds and reports `is_damaged`.

        The distinction the endpoint has to keep: a file that cannot be opened has no plan,
        a file that opens and contains nothing has one, and it says nothing is applicable.
        """
        pytest.importorskip("pymupdf")
        empty_pdf = (
            b"%PDF-1.4\n"
            b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Kids[]/Count 0>>endobj\n"
            b"trailer<</Root 1 0 R>>\n"
            b"%%EOF\n"
        )
        response = _analyze(db_session, empty_pdf, "zero.pdf", "application/pdf")

        if response.probe_failed is not None:
            pytest.skip("this PyMuPDF build refuses to open a zero-page PDF at all")
        assert response.patterns["is_damaged"] is True
        assert response.plan is not None
        assert _outcomes(response)[TASK_EXTRACT_TEXT] == "not_applicable"


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


class TestAlreadyStored:
    def test_unknown_bytes_report_nothing(self, db_session, pdf_bytes):
        assert (
            _analyze(db_session, pdf_bytes, "annual.pdf", "application/pdf").already_stored is None
        )

    def test_stored_bytes_name_the_node_an_upload_would_resolve_to(self, db_session, pdf_bytes):
        uploaded = IngestService(db_session).upload_file(
            UploadedFile(data=pdf_bytes, filename="annual.pdf", content_type="application/pdf")
        )
        response = _analyze(db_session, pdf_bytes, "again.pdf", "application/pdf")

        assert response.already_stored is not None
        assert response.already_stored.document_id == uploaded.document_id
        # The plan is still reported: it describes what this FORMAT does, and the dedup
        # block is what says this particular upload will not run it.
        assert response.plan is not None

    def test_the_recorded_options_come_back_so_a_conflict_is_predictable(
        self, db_session, pdf_bytes
    ):
        """An upload whose options differ from the stored node's is a 400 (spec 6.1)."""
        IngestService(db_session).upload_file(
            UploadedFile(data=pdf_bytes, filename="annual.pdf", content_type="application/pdf"),
            options={"structure": {"max_tokens": 60}},
        )
        response = _analyze(db_session, pdf_bytes, "again.pdf", "application/pdf")
        assert response.already_stored.options["structure"]["max_tokens"] == 60


# ---------------------------------------------------------------------------
# The planner's own guard
# ---------------------------------------------------------------------------


class TestExplainPlanProvenanceGuard:
    def test_a_source_without_patterns_is_refused(self):
        with pytest.raises(ValueError, match="patterns_source"):
            explain_plan("pdf", None, None, patterns_source=PATTERNS_PROBED)

    def test_supplied_patterns_keep_their_own_label_by_default(self):
        plan = explain_plan("pdf", None, {"has_text_layer": True})
        assert plan.patterns_source == "supplied"


# ---------------------------------------------------------------------------
# Over HTTP
# ---------------------------------------------------------------------------


class TestOverHttp:
    def test_a_multipart_post_returns_the_analysis(self, client_with_db, pdf_bytes):
        response = client_with_db.post(
            "/ingest/analyze",
            files={"file": ("annual.pdf", pdf_bytes, "application/pdf")},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["format"] == "pdf"
        assert body["plan"]["patterns_source"] == PATTERNS_PROBED
        assert body["patterns"]["has_outline"] is True
        assert [t["task"] for t in body["plan"]["tasks"]][0] == TASK_PROBE

    def test_options_arrive_as_a_form_field(self, client_with_db, pdf_bytes):
        response = client_with_db.post(
            "/ingest/analyze",
            files={"file": ("annual.pdf", pdf_bytes, "application/pdf")},
            data={"options": '{"structure": {"max_tokens": 60}}'},
        )

        assert response.status_code == 200, response.text
        assert response.json()["plan"]["options"]["structure"]["max_tokens"] == 60

    def test_an_unknown_option_is_a_400(self, client_with_db, pdf_bytes):
        response = client_with_db.post(
            "/ingest/analyze",
            files={"file": ("annual.pdf", pdf_bytes, "application/pdf")},
            data={"options": '{"structure": {"max_token": 60}}'},
        )
        assert response.status_code == 400, response.text

    def test_nothing_was_created(self, client_with_db, db_session, pdf_bytes):
        before = _counts(db_session)
        client_with_db.post(
            "/ingest/analyze",
            files={"file": ("annual.pdf", pdf_bytes, "application/pdf")},
        )
        assert _counts(db_session) == before

    def test_a_markdown_file_over_http_says_nothing_applies(self, client_with_db):
        response = client_with_db.post(
            "/ingest/analyze",
            files={"file": ("notes.md", MARKDOWN_BYTES, "text/markdown")},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["format"] == "text"
        outcomes = {t["task"]: t["outcome"] for t in body["plan"]["tasks"]}
        assert outcomes[TASK_PROBE] == OUTCOME_ENQUEUED
        assert outcomes[TASK_STRUCTURE_INFERRED] == "not_applicable"
