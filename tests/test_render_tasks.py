"""``render:pdf`` and ``convert:ooxml``. ``OFFICE_SPEC.md`` Part 6 and Part 11 step 7.

Four things are guarded here, and they fail in different ways:

* **The appliance that cannot convert says so.** This is the entry condition for the step.
  A base install has no LibreOffice, no ``JMFTS_CONVERT_URL`` and — because ``dev`` does not
  imply ``convert`` — not even the unoserver client. Every one of those has to come back as a
  PERMANENT failure naming what is missing and how to get it, the way
  ``OfficeStackNotInstalled`` does for tier 2. A rendition task that completed having
  produced nothing would be indistinguishable from a document LibreOffice cannot paginate.

* **The rows fire for the formats that have no pages and for no others.** A ``.docx`` has no
  page until something paginates it; a PDF already is one and a rendition of it would be a
  copy. The sentinel is what expresses that, and a row that fired for ``pdf`` would double
  every PDF in blob storage for nothing.

* **A rendition is derived, so it is replaceable and it is evictable.** The uploaded blob
  refuses a second write (``BlobRepository.store``); this one requires it, because
  re-running ``render:pdf`` after a LibreOffice upgrade is the whole of
  ``SPRINT_0_6_0.md`` 4.9's answer. And deleting the document has to take the large object
  with it — the leak ``_OID_SOURCES`` exists to prevent.

* **Step 33 can find the PDF.** :func:`pdf_for` is the seam that lane forks on: a PDF
  resolves to its own bytes, an office document to its rendition, and an office document
  with no rendition to a named refusal rather than to ``None``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from jmfts_client.contracts.upload import UploadedFile
from jmfts_core import evidence as ev
from jmfts_core.convert import (
    CONVERT_INSTALL_HINT,
    ConversionFailed,
    ConverterNotConfigured,
    ConvertStackNotInstalled,
    ConverterTarget,
    converter_target,
    require_unoserver,
)
from jmfts_core.ingest_tasks import (
    TASK_CONVERT_OOXML,
    TASK_EXTRACT_TEXT,
    TASK_HANDLERS,
    TASK_PROBE,
    TASK_RENDER_PDF,
    OUTCOME_ENQUEUED,
    plan_after_probe,
)
from jmfts_core.atoms import ATOMS
from jmfts_core.models.document import Document
from jmfts_core.models.task_queue import ADVISORY_TASK_TYPES, TaskQueue, WRITE_SELF
from jmfts_core.render_tasks import (
    RENDITION_NAME,
    CONVERSION_NAME,
    run_convert_ooxml,
    run_render_pdf,
)
from jmfts_core.repositories.blob import BlobRepository
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.evidence import EvidenceRepository
from jmfts_core.repositories.task_queue import TaskQueueRepository
from jmfts_core.repositories.rendition import (
    KIND_OOXML,
    KIND_PDF,
    NoRenditionError,
    RenditionRepository,
    pdf_for,
)
from jmfts_core.services.ingest_service import IngestService
from jmfts_core.task_errors import ErrorType, classify_exception
from tests.conftest import drain_ingest_queue

# ---------------------------------------------------------------------------
# Fixtures — bytes that need no LibreOffice
# ---------------------------------------------------------------------------


def _one_page_pdf(pages: int = 2) -> bytes:
    """What a converter would hand back. Built with pymupdf, which is a base dependency."""
    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    for index in range(pages):
        page = doc.new_page()
        page.insert_text((72, 96), f"page {index}", fontsize=18)
    return doc.tobytes()


@pytest.fixture(scope="module")
def rendered_pdf() -> bytes:
    return _one_page_pdf()


def _minimal_docx() -> bytes:
    """A ZIP holding ``word/document.xml``, which is what ``detect_format`` looks for.

    Written here rather than taken from ``tests/corpus`` because what is under test is the
    handler's CHECK that the converter returned the format it asked for, and the smallest
    thing that passes that check is the right fixture for it — a real package would add
    ``python-docx`` to a test that does not open one.
    """
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", "<w:document/>")
    return buffer.getvalue()


def _office_node(session, *, fmt: str, data: bytes = b"PK\x03\x04 not really a docx"):
    """A file node that probe has already run on, without running probe.

    The handlers under test read the blob and the ``matched`` block and nothing else, so a
    real ``.docx`` would only be testing ``python-docx``'s ability to write one.
    """
    node = DocumentRepository(session).create(
        title=f"a {fmt}", content="", usetype="file", auto_embed=False
    )
    session.flush()
    BlobRepository(session).store(
        node.id, data, mime_type="application/octet-stream", content_hash="0" * 64
    )
    EvidenceRepository(session).write(node.id, "matched", {"format": fmt, "patterns": {}})
    session.flush()
    return node


def _task(session, node_id: int, task_type: str) -> TaskQueue:
    """The queue row a worker would hand the handler.

    Enqueued through the repository rather than constructed, because the row carries
    machinery a handler test has no business reproducing — `param_fingerprint` is NOT NULL
    and is computed there — and because the badge the policy would apply is part of what a
    real row looks like.
    """
    return TaskQueueRepository(session).enqueue(task_type, node_id, WRITE_SELF)


# ---------------------------------------------------------------------------
# 1. The appliance that cannot convert — the entry condition
# ---------------------------------------------------------------------------


class TestTheWayOutIsNamed:
    def test_a_blank_convert_url_refuses_and_names_the_setting(self, monkeypatch):
        monkeypatch.setenv("JMFTS_CONVERT_URL", "")
        from jmfts_core import config

        config.get_settings.cache_clear()
        with pytest.raises(ConverterNotConfigured) as caught:
            converter_target()
        message = str(caught.value)
        assert "JMFTS_CONVERT_URL" in message
        config.get_settings.cache_clear()

    def test_that_refusal_is_permanent(self):
        """Three attempts do not make a setting appear. Same argument as
        ``ModelStackNotInstalled``, which reaches PERMANENT through the ImportError arm;
        this one reaches it through the ValueError arm and the base is chosen for that."""
        assert classify_exception(ConverterNotConfigured("x")) is ErrorType.PERMANENT
        assert classify_exception(ConvertStackNotInstalled("x")) is ErrorType.PERMANENT
        assert classify_exception(ConversionFailed("x")) is ErrorType.PERMANENT

    def test_the_client_half_is_reported_separately_from_the_url(self):
        """Two different missing things, two different messages. A worker with the extra
        and no URL is a different deployment from one with a URL and no extra."""
        try:
            import unoserver.client  # noqa: F401
        except ImportError:
            with pytest.raises(ConvertStackNotInstalled) as caught:
                require_unoserver()
            assert "jmfts[convert]" in str(caught.value)
        else:
            assert require_unoserver() is not None
        assert "jmfts[convert]" in CONVERT_INSTALL_HINT

    def test_a_url_is_parsed_into_a_host_and_a_port(self, monkeypatch):
        from jmfts_core import config

        monkeypatch.setenv("JMFTS_CONVERT_URL", "http://libreoffice.jmfts.svc:2003")
        config.get_settings.cache_clear()
        assert converter_target() == ConverterTarget("libreoffice.jmfts.svc", 2003, "http")

        monkeypatch.setenv("JMFTS_CONVERT_URL", "http://127.0.0.1")
        config.get_settings.cache_clear()
        assert converter_target().port == 2003

        monkeypatch.setenv("JMFTS_CONVERT_URL", "libreoffice:2003")
        config.get_settings.cache_clear()
        with pytest.raises(ConverterNotConfigured):
            converter_target()
        config.get_settings.cache_clear()


# ---------------------------------------------------------------------------
# 2. The declarations
# ---------------------------------------------------------------------------


class TestDeclarations:
    def test_both_task_types_have_a_handler_and_an_atom(self):
        for task_type in (TASK_RENDER_PDF, TASK_CONVERT_OOXML):
            assert task_type in TASK_HANDLERS
            assert task_type in ATOMS

    def test_a_rendition_is_advisory_and_a_conversion_is_not(self):
        """Part 6: "Rendering degrades; retrieval does not" — a ``.docx`` with no rendition
        is still fully searchable, so its failure must not fail the node. A legacy binary
        with no conversion has no text at all, which is a failed document and must say so."""
        assert TASK_RENDER_PDF in ADVISORY_TASK_TYPES
        assert TASK_CONVERT_OOXML not in ADVISORY_TASK_TYPES

    @pytest.mark.parametrize(
        "fmt,patterns",
        [
            ("docx", {"has_text_layer": True}),
            ("pptx", {"has_text_layer": True}),
            ("xlsx", {"has_sheets": True}),
            ("ole2", {"is_legacy_binary": True, "is_encrypted": False}),
        ],
    )
    def test_a_format_with_no_pages_of_its_own_is_rendered(self, fmt, patterns):
        plan = plan_after_probe(fmt, patterns, None)
        assert TASK_RENDER_PDF in [spec.task_type for spec in plan.eligible]

    def test_a_pdf_is_not_rendered_because_it_already_is_one(self):
        plan = plan_after_probe("pdf", {"has_text_layer": True, "has_outline": True}, None)
        assert TASK_RENDER_PDF not in [spec.task_type for spec in plan.eligible]
        assert plan.not_applicable[TASK_RENDER_PDF]

    def test_only_a_legacy_binary_is_converted(self):
        legacy = plan_after_probe("ole2", {"is_legacy_binary": True}, None)
        assert TASK_CONVERT_OOXML in [spec.task_type for spec in legacy.eligible]

        modern = plan_after_probe("docx", {"has_text_layer": True}, None)
        assert TASK_CONVERT_OOXML not in [spec.task_type for spec in modern.eligible]

    def test_explain_calls_a_pdf_rendition_impossible_and_says_why(self):
        """IMPOSSIBLE and not merely not-applicable, which is the sentinel earning its
        keep: a condition that no bytes of this format could ever satisfy is a different
        answer from one that happened to be false, and an operator reading the second about
        every PDF they own would reasonably conclude renditions are broken."""
        from jmfts_core.ingest_tasks import explain_plan

        rows = {row.task: row for row in explain_plan("pdf", {}, None).tasks}
        assert rows[TASK_RENDER_PDF].outcome == "impossible"
        assert "already has pages" in rows[TASK_RENDER_PDF].reason
        assert rows[TASK_PROBE].outcome == OUTCOME_ENQUEUED

        docx = {row.task: row for row in explain_plan("docx", {}, None).tasks}
        assert docx[TASK_RENDER_PDF].outcome != "impossible"
        assert docx[TASK_EXTRACT_TEXT] is not None


# ---------------------------------------------------------------------------
# 3. The handlers, against a converter that is not LibreOffice
# ---------------------------------------------------------------------------


class TestRenderPdf:
    def test_the_rendition_is_stored_and_described(self, db_session, monkeypatch, rendered_pdf):
        node = _office_node(db_session, fmt="docx")
        monkeypatch.setattr(
            "jmfts_core.render_tasks.convert_bytes", lambda data, *, to: rendered_pdf
        )

        outcome = run_render_pdf(db_session, _task(db_session, node.id, TASK_RENDER_PDF))

        assert RenditionRepository(db_session).read_bytes(node.id, KIND_PDF) == rendered_pdf
        record = EvidenceRepository(db_session).read(node.id, RENDITION_NAME)
        assert record["kind"] == KIND_PDF
        assert record["page_count"] == 2
        assert len(record["pages"]) == 2
        assert record["pages"][0]["width"] > 0
        assert record["source_format"] == "docx"
        assert outcome.detail["page_count"] == 2

    def test_a_second_run_replaces_the_first_rather_than_colliding(
        self, db_session, monkeypatch, rendered_pdf
    ):
        node = _office_node(db_session, fmt="docx")
        monkeypatch.setattr(
            "jmfts_core.render_tasks.convert_bytes", lambda data, *, to: rendered_pdf
        )
        run_render_pdf(db_session, _task(db_session, node.id, TASK_RENDER_PDF))
        first = RenditionRepository(db_session).get(node.id, KIND_PDF).lob_oid

        three_pages = _one_page_pdf(3)
        monkeypatch.setattr(
            "jmfts_core.render_tasks.convert_bytes", lambda data, *, to: three_pages
        )
        run_render_pdf(db_session, _task(db_session, node.id, TASK_RENDER_PDF))

        repo = RenditionRepository(db_session)
        assert repo.read_bytes(node.id, KIND_PDF) == three_pages
        assert (
            db_session.execute(
                text("SELECT count(*) FROM document_renditions WHERE document_id = :d"),
                {"d": node.id},
            ).scalar_one()
            == 1
        )
        assert (
            db_session.execute(
                text("SELECT 1 FROM pg_largeobject_metadata WHERE oid = :o"), {"o": first}
            ).scalar()
            is None
        )

    def test_bytes_that_are_not_a_pdf_are_refused_rather_than_stored(self, db_session, monkeypatch):
        """A converter that answers 200 with an HTML error page is the failure this
        catches. Stored, it would be a rendition every later read had to discover was not
        one — and step 33 would report "no rectangle" for a document that has pages."""
        node = _office_node(db_session, fmt="docx")
        monkeypatch.setattr(
            "jmfts_core.render_tasks.convert_bytes", lambda data, *, to: b"<html>no</html>"
        )
        with pytest.raises(ConversionFailed):
            run_render_pdf(db_session, _task(db_session, node.id, TASK_RENDER_PDF))
        assert RenditionRepository(db_session).get(node.id, KIND_PDF) is None

    def test_evicting_a_rendition_retracts_the_row_that_described_it(
        self, db_session, monkeypatch, rendered_pdf
    ):
        """Eviction is Block G's answer to Part 12 question 1, and a reader asks the evidence
        row whether there is a page to show. A row left standing after the bytes go says
        "yes" to a question the appliance can no longer answer."""
        node = _office_node(db_session, fmt="docx")
        monkeypatch.setattr(
            "jmfts_core.render_tasks.convert_bytes", lambda data, *, to: rendered_pdf
        )
        run_render_pdf(db_session, _task(db_session, node.id, TASK_RENDER_PDF))
        assert EvidenceRepository(db_session).read(node.id, RENDITION_NAME) is not ev.ABSENT

        assert RenditionRepository(db_session).delete(node.id, KIND_PDF) is True

        # ABSENT and not a null: 3.2's "never attempted" is what a retracted row has to read
        # as, because "there is no rendition" and "a rendition was made and is empty" are the
        # two answers a reader has to tell apart.
        assert EvidenceRepository(db_session).read(node.id, RENDITION_NAME) is ev.ABSENT
        with pytest.raises(NoRenditionError):
            pdf_for(db_session, node)

    def test_deleting_the_document_takes_the_large_object_with_it(
        self, db_session, monkeypatch, rendered_pdf
    ):
        node = _office_node(db_session, fmt="docx")
        monkeypatch.setattr(
            "jmfts_core.render_tasks.convert_bytes", lambda data, *, to: rendered_pdf
        )
        run_render_pdf(db_session, _task(db_session, node.id, TASK_RENDER_PDF))
        oid = RenditionRepository(db_session).get(node.id, KIND_PDF).lob_oid

        DocumentRepository(db_session).delete(node.id)
        db_session.flush()

        assert (
            db_session.execute(
                text("SELECT 1 FROM pg_largeobject_metadata WHERE oid = :o"), {"o": oid}
            ).scalar()
            is None
        )
        assert oid not in BlobRepository(db_session).find_orphaned_lobs()


class TestConvertOoxml:
    def test_a_legacy_binary_becomes_a_package(self, db_session, monkeypatch):
        node = _office_node(db_session, fmt="ole2", data=b"\xd0\xcf\x11\xe0not really")
        EvidenceRepository(db_session).write(
            node.id,
            "matched",
            {
                "format": "ole2",
                "patterns": {"is_legacy_binary": True, "legacy_application": "word"},
            },
        )
        db_session.flush()
        package = _minimal_docx()
        monkeypatch.setattr("jmfts_core.render_tasks.convert_bytes", lambda data, *, to: package)

        outcome = run_convert_ooxml(db_session, _task(db_session, node.id, TASK_CONVERT_OOXML))

        assert RenditionRepository(db_session).read_bytes(node.id, KIND_OOXML) == package
        record = EvidenceRepository(db_session).read(node.id, CONVERSION_NAME)
        assert record["to"] == "docx"
        assert record["from_format"] == "ole2"
        assert record["from_application"] == "word"
        assert outcome.detail["byte_size"] == len(package)

    def test_a_package_that_is_not_what_was_asked_for_is_refused(self, db_session, monkeypatch):
        """A converter that answers with a ZIP of something else. Stored, it would be an
        artefact named `docx` that no docx reader can open."""
        node = _office_node(db_session, fmt="ole2", data=b"\xd0\xcf\x11\xe0not really")
        EvidenceRepository(db_session).write(
            node.id,
            "matched",
            {
                "format": "ole2",
                "patterns": {"is_legacy_binary": True, "legacy_application": "word"},
            },
        )
        db_session.flush()
        monkeypatch.setattr(
            "jmfts_core.render_tasks.convert_bytes", lambda data, *, to: b"PK\x03\x04nothing"
        )
        with pytest.raises(ConversionFailed, match="detect as"):
            run_convert_ooxml(db_session, _task(db_session, node.id, TASK_CONVERT_OOXML))
        assert RenditionRepository(db_session).get(node.id, KIND_OOXML) is None

    def test_an_application_nobody_named_is_refused_rather_than_guessed(
        self, db_session, monkeypatch
    ):
        node = _office_node(db_session, fmt="ole2", data=b"\xd0\xcf\x11\xe0not really")
        monkeypatch.setattr(
            "jmfts_core.render_tasks.convert_bytes", lambda data, *, to: b"PK\x03\x04package"
        )
        with pytest.raises(ValueError, match="legacy_application"):
            run_convert_ooxml(db_session, _task(db_session, node.id, TASK_CONVERT_OOXML))


# ---------------------------------------------------------------------------
# 4. What step 33 forks on
# ---------------------------------------------------------------------------


class TestPdfFor:
    def test_a_pdf_resolves_to_its_own_bytes(self, db_session, rendered_pdf):
        uploaded = IngestService(db_session).upload_file(
            UploadedFile(data=rendered_pdf, filename="a.pdf", content_type="application/pdf")
        )
        db_session.flush()
        resolved = pdf_for(db_session, db_session.get(Document, uploaded.document_id))
        assert resolved.data == rendered_pdf
        assert resolved.origin == "source"

    def test_an_office_document_resolves_to_its_rendition(
        self, db_session, monkeypatch, rendered_pdf
    ):
        node = _office_node(db_session, fmt="docx")
        monkeypatch.setattr(
            "jmfts_core.render_tasks.convert_bytes", lambda data, *, to: rendered_pdf
        )
        run_render_pdf(db_session, _task(db_session, node.id, TASK_RENDER_PDF))

        resolved = pdf_for(db_session, node)
        assert resolved.data == rendered_pdf
        assert resolved.origin == "rendition"

    def test_an_office_document_with_no_rendition_says_so(self, db_session):
        node = _office_node(db_session, fmt="docx")
        with pytest.raises(NoRenditionError) as caught:
            pdf_for(db_session, node)
        assert "render:pdf" in str(caught.value)


class TestWhyThereIsNoRendition:
    """0.7.1, preview audit #23. 0.7.0 told every node with no rendition that `render:pdf`
    needs a LibreOffice, which was wrong advice for a document still rendering, one whose
    rendition was evicted, and one no converter could paginate."""

    def test_the_task_name_is_the_one_the_queue_uses(self):
        from jmfts_core.repositories.rendition import RENDER_PDF_TASK

        assert RENDER_PDF_TASK == TASK_RENDER_PDF

    def test_a_document_still_rendering_is_told_to_wait(self, db_session):
        node = _office_node(db_session, fmt="docx")
        _task(db_session, node.id, TASK_RENDER_PDF)
        with pytest.raises(NoRenditionError, match=r"YET: `render:pdf` is pending") as caught:
            pdf_for(db_session, node)
        assert "LibreOffice" not in str(caught.value)

    def test_a_failed_render_quotes_the_failure(self, db_session):
        node = _office_node(db_session, fmt="docx")
        row = _task(db_session, node.id, TASK_RENDER_PDF)
        row.status = "failed"
        row.error_type = "permanent"
        row.error = "JMFTS_CONVERT_URL is blank"
        db_session.flush()
        with pytest.raises(NoRenditionError, match="failed for it") as caught:
            pdf_for(db_session, node)
        assert "JMFTS_CONVERT_URL is blank" in str(caught.value)
        assert "LibreOffice" in str(caught.value)

    def test_an_evicted_rendition_asks_for_a_re_run(self, db_session, monkeypatch, rendered_pdf):
        node = _office_node(db_session, fmt="docx")
        monkeypatch.setattr(
            "jmfts_core.render_tasks.convert_bytes", lambda data, *, to: rendered_pdf
        )
        row = _task(db_session, node.id, TASK_RENDER_PDF)
        run_render_pdf(db_session, row)
        row.status = "completed"
        db_session.flush()
        RenditionRepository(db_session).delete(node.id, KIND_PDF)
        with pytest.raises(NoRenditionError, match="evicted"):
            pdf_for(db_session, node)

    def test_a_format_nothing_paginates_says_no_converter_would_help(self, db_session):
        uploaded = IngestService(db_session).upload_file(
            UploadedFile(data=b"plain words\n", filename="notes.txt", content_type="text/plain")
        )
        drain_ingest_queue(db_session)
        node = db_session.get(Document, uploaded.document_id)
        with pytest.raises(NoRenditionError, match="does not paginate") as caught:
            pdf_for(db_session, node)
        assert "no converter would change that" in str(caught.value)
