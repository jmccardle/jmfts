"""Upload and probe — `INGEST_SPEC.md` Part 10 step 3.

Four things are guarded here, and they fail in different ways:

1. **The bytes survive and are the bytes that were sent.** A large object roundtrips and
   its sha256 matches, so a truncated or re-encoded upload cannot pass silently.
2. **Deleting the node unlinks the large object.** This is the one failure the storage
   decision (Part 9) explicitly warns about: `ON DELETE CASCADE` removes the
   `document_blobs` row and leaves the object behind, orphaned, forever. Nothing in the
   database catches it, so it is asserted here directly against
   `pg_largeobject_metadata`.
3. **Probe reports what is really in the file.** The PDF fixture is built with a known
   text layer and a known three-entry, two-level outline, so `has_text_layer`,
   `has_outline` and `outline_depth` are checked against ground truth rather than against
   "some plausible value".
4. **A declared/detected mime disagreement is recorded, not resolved.** Spec 3.1: the
   extension says how to open the bytes, it is not a structure guarantee, and when the
   two disagree BOTH are written down.

DB tests run on the shared savepoint-rollback `db_session` fixture, so nothing here
commits — including the large objects, which are transactional and disappear with the
rollback.
"""

import hashlib
import io
import zipfile
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from api.main import app
from jmfts_core.access import AccessDeniedError, can_read, can_write
from jmfts_core.contracts.attempt import AttemptRecord
from jmfts_core.contracts.document import DocumentUpdate
from jmfts_core.contracts.upload import UploadedFile
from jmfts_core.database import get_db
from jmfts_core.models.document import SETTLED_IN_FLIGHT, DocumentLink
from jmfts_core.models.principal import AccessGrant, Principal as PrincipalModel
from jmfts_core.principal_context import OWNER, CurrentPrincipal, reset_principal, set_principal
from jmfts_core.probe import detect_format, probe_patterns
from jmfts_core.repositories.blob import BlobLeakError, BlobRepository
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.services.document_service import DocumentService
from jmfts_core.services.ingest_service import LINK_CONTAINS, USETYPE_FILE, IngestService
from tests.conftest import drain_ingest_queue

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_fixture_pdf() -> bytes:
    """A 2-page PDF with a real text layer and a 2-level, 3-entry outline.

    Built with PyMuPDF exactly as `tests/test_pdf_extraction.py` does, so the probe
    assertions below compare against a document whose structure this file states rather
    than against whatever happened to be on disk.
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
    # (level, title, page) — a level-2 entry is what makes outline_depth 2 rather than 1.
    doc.set_toc([[1, "Chapter One", 1], [2, "Section 1.1", 1], [1, "Chapter Two", 2]])
    return doc.tobytes()


@pytest.fixture(scope="module")
def pdf_bytes() -> bytes:
    return _make_fixture_pdf()


def _make_docx_like_zip() -> bytes:
    """A ZIP carrying `word/document.xml` — enough for the manifest refinement.

    Not a valid .docx (no prober opens it; that is phasing step 6). It exists to prove
    that ZIP-container detection reads the archive's own directory rather than the name.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<document/>")
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


def _lob_exists(session, oid: int) -> bool:
    return (
        session.execute(
            text("SELECT 1 FROM pg_largeobject_metadata WHERE oid = :oid"), {"oid": oid}
        ).scalar()
        is not None
    )


@contextmanager
def _as(principal):
    """Run a block as a bound principal — the house pattern from `tests/test_access_*`."""
    token = set_principal(principal)
    try:
        yield
    finally:
        reset_principal(token)


def _principal(session, name):
    row = PrincipalModel(name=name)
    session.add(row)
    session.flush()
    return CurrentPrincipal(id=row.id, name=name, is_owner=False)


def _new_file_node(session, title="upload.bin"):
    return DocumentRepository(session).create(
        title=title, content=None, usetype=USETYPE_FILE, auto_embed=False, settled=SETTLED_IN_FLIGHT
    )


# ---------------------------------------------------------------------------
# 1. Blob storage (spec Part 9)
# ---------------------------------------------------------------------------


class TestBlobStorage:
    def test_bytes_roundtrip_unchanged(self, db_session):
        data = b"\x00\x01binary payload with a NUL and \xff high bytes\x00" * 64
        node = _new_file_node(db_session)
        repo = BlobRepository(db_session)
        repo.store(node.id, data, mime_type="application/octet-stream", content_hash=_sha256(data))

        assert repo.read_bytes(node.id) == data

    def test_recorded_hash_and_size_describe_the_stored_bytes(self, db_session):
        data = b"the quick brown fox" * 100
        node = _new_file_node(db_session)
        repo = BlobRepository(db_session)
        blob = repo.store(node.id, data, mime_type="text/plain", content_hash=_sha256(data))

        assert blob.byte_size == len(data)
        assert blob.content_hash == hashlib.sha256(data).hexdigest()
        # And the hash describes what actually came back, not what was passed in.
        assert hashlib.sha256(repo.read_bytes(node.id)).hexdigest() == blob.content_hash

    def test_second_store_for_one_document_is_rejected(self, db_session):
        node = _new_file_node(db_session)
        repo = BlobRepository(db_session)
        repo.store(node.id, b"first", mime_type="text/plain", content_hash=_sha256(b"first"))

        with pytest.raises(ValueError, match="already has a blob"):
            repo.store(node.id, b"second", mime_type="text/plain", content_hash=_sha256(b"second"))

    def test_read_bytes_is_none_when_there_is_no_row(self, db_session):
        node = _new_file_node(db_session)
        assert BlobRepository(db_session).read_bytes(node.id) is None

    def test_read_bytes_raises_when_the_row_outlives_the_object(self, db_session):
        """A row asserts its bytes exist. Its being wrong is corruption, not a None."""
        data = b"payload"
        node = _new_file_node(db_session)
        repo = BlobRepository(db_session)
        blob = repo.store(node.id, data, mime_type="text/plain", content_hash=_sha256(data))
        db_session.execute(text("SELECT lo_unlink(:oid)"), {"oid": blob.lob_oid})

        with pytest.raises(BlobLeakError):
            repo.read_bytes(node.id)

    def test_delete_unlinks_the_large_object(self, db_session):
        data = b"bytes that must not outlive their row"
        node = _new_file_node(db_session)
        repo = BlobRepository(db_session)
        oid = repo.store(node.id, data, mime_type="text/plain", content_hash=_sha256(data)).lob_oid
        assert _lob_exists(db_session, oid)

        assert repo.delete(node.id) is True

        assert not _lob_exists(db_session, oid)
        assert repo.get(node.id) is None

    def test_delete_reports_false_when_there_was_nothing_to_delete(self, db_session):
        node = _new_file_node(db_session)
        assert BlobRepository(db_session).delete(node.id) is False


class TestDocumentDeleteUnlinks:
    """The leak spec Part 9 names: the cascade takes the row and leaves the object."""

    def test_deleting_the_node_unlinks_its_object(self, db_session):
        data = b"%PDF-1.4 not really a pdf"
        node = _new_file_node(db_session)
        oid = (
            BlobRepository(db_session)
            .store(node.id, data, mime_type="application/pdf", content_hash=_sha256(data))
            .lob_oid
        )

        assert DocumentRepository(db_session).delete(node.id) is True
        db_session.flush()

        assert not _lob_exists(db_session, oid)
        # And the row went with it, via ON DELETE CASCADE.
        assert (
            db_session.execute(
                text("SELECT count(*) FROM document_blobs WHERE lob_oid = :oid"), {"oid": oid}
            ).scalar()
            == 0
        )

    def test_deleting_a_parent_unlinks_a_childs_object(self, db_session):
        """The cascade reaches descendants, so the unlink must too."""
        repo = DocumentRepository(db_session)
        parent = repo.create(title="parent", content="parent body", auto_embed=False)
        child = repo.create(
            title="child.pdf",
            content=None,
            parent_id=parent.id,
            usetype=USETYPE_FILE,
            auto_embed=False,
            settled=SETTLED_IN_FLIGHT,
        )
        db_session.flush()
        oid = (
            BlobRepository(db_session)
            .store(
                child.id,
                b"child bytes",
                mime_type="application/pdf",
                content_hash=_sha256(b"child bytes"),
            )
            .lob_oid
        )

        assert repo.delete(parent.id) is True
        db_session.flush()

        assert not _lob_exists(db_session, oid)


class TestOrphanDetection:
    def test_finds_an_object_no_row_names(self, db_session):
        stray = db_session.execute(
            text("SELECT lo_from_bytea(0, :d)"), {"d": b"nobody owns these bytes"}
        ).scalar_one()

        assert stray in BlobRepository(db_session).find_orphaned_lobs()

    def test_a_referenced_object_is_not_an_orphan(self, db_session):
        node = _new_file_node(db_session)
        blob = BlobRepository(db_session).store(
            node.id, b"owned", mime_type="text/plain", content_hash=_sha256(b"owned")
        )

        assert blob.lob_oid not in BlobRepository(db_session).find_orphaned_lobs()

    def test_cleanup_unlinks_only_the_orphans(self, db_session):
        repo = BlobRepository(db_session)
        node = _new_file_node(db_session)
        kept = repo.store(
            node.id, b"owned", mime_type="text/plain", content_hash=_sha256(b"owned")
        ).lob_oid
        stray = db_session.execute(
            text("SELECT lo_from_bytea(0, :d)"), {"d": b"orphan"}
        ).scalar_one()

        removed = repo.cleanup_orphaned_lobs()

        assert removed >= 1
        assert not _lob_exists(db_session, stray)
        assert _lob_exists(db_session, kept)

    def test_finds_a_file_node_whose_bytes_never_landed(self, db_session):
        """The other direction: a record with no bytes, not bytes with no record."""
        blobless = _new_file_node(db_session, title="half-landed.pdf")
        stored = _new_file_node(db_session, title="complete.pdf")
        BlobRepository(db_session).store(
            stored.id, b"ok", mime_type="text/plain", content_hash=_sha256(b"ok")
        )
        db_session.flush()

        ids = BlobRepository(db_session).find_blobless_documents()

        assert blobless.id in ids
        assert stored.id not in ids


# ---------------------------------------------------------------------------
# 2. Format detection (spec 3.1) — pure, no database
# ---------------------------------------------------------------------------


class TestDetectFormat:
    def test_pdf_from_magic_bytes(self, pdf_bytes):
        detection = detect_format(pdf_bytes, filename="doc.pdf", declared_mime="application/pdf")

        assert detection.format == "pdf"
        assert detection.detected_mime == "application/pdf"
        assert detection.detected_by == "magic_bytes"
        assert detection.mime_agrees is True

    def test_the_extension_never_overrules_the_bytes(self):
        """A PDF named .txt is still a PDF, and the disagreement is visible."""
        detection = detect_format(
            b"%PDF-1.7\nrest", filename="notes.txt", declared_mime="text/plain"
        )

        assert detection.format == "pdf"
        assert detection.detected_mime == "application/pdf"
        assert detection.declared_mime == "text/plain"
        assert detection.mime_agrees is False

    def test_ooxml_is_identified_from_the_archive_manifest(self):
        detection = detect_format(_make_docx_like_zip(), filename="report.docx")

        assert detection.format == "docx"
        assert detection.detected_by == "zip_manifest"

    def test_an_unplaceable_zip_stays_a_zip(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("readme.txt", "hello")

        detection = detect_format(buffer.getvalue(), filename="bundle.zip")

        assert detection.format == "zip"
        assert detection.detected_mime == "application/zip"

    def test_utf8_text_is_sniffed_and_labelled_as_sniffed(self):
        detection = detect_format("# heading\n\nsome prose\n".encode(), filename="notes.md")

        assert detection.format == "text"
        assert detection.detected_mime == "text/plain"
        assert detection.detected_by == "content_sniff"

    def test_unrecognised_bytes_report_no_detection_at_all(self):
        """`format` falls back to the extension; detected_* stay null — not backfilled."""
        detection = detect_format(b"\x00\x01\x02\xfe\xff\x00", filename="thing.wat")

        assert detection.detected_mime is None
        assert detection.detected_by is None
        assert detection.format == "wat"
        assert detection.mime_agrees is None

    def test_content_type_parameters_do_not_create_a_false_conflict(self):
        detection = detect_format(
            b"plain text", filename="a.txt", declared_mime="text/plain; charset=utf-8"
        )

        assert detection.mime_agrees is True


# ---------------------------------------------------------------------------
# 3. Pattern probing (spec 3.3 / Part 4)
# ---------------------------------------------------------------------------


class TestProbePatterns:
    def test_pdf_patterns_match_the_fixture(self, pdf_bytes):
        detection = detect_format(pdf_bytes, filename="doc.pdf")
        patterns, detail = probe_patterns(pdf_bytes, detection)

        assert patterns["has_text_layer"] is True
        assert patterns["has_outline"] is True
        # The fixture's outline is [1, 2, 1] — two levels deep, three entries.
        assert patterns["outline_depth"] == 2
        assert detail["outline_entries"] == 3
        assert patterns["page_count"] == 2
        assert patterns["has_images"] is False
        assert patterns["image_count"] == 0
        assert patterns["is_scanned"] is False

    def test_the_scanned_verdict_can_be_re_derived_from_the_detail(self, pdf_bytes):
        """The threshold and both measurements are logged, so the heuristic is auditable."""
        detection = detect_format(pdf_bytes, filename="doc.pdf")
        patterns, detail = probe_patterns(pdf_bytes, detection)

        expected = patterns["has_images"] and (
            detail["chars_per_page"] < detail["scanned_threshold_chars_per_page"]
        )
        assert patterns["is_scanned"] is bool(expected)

    def test_a_pdf_without_an_outline_says_so(self):
        pymupdf = pytest.importorskip("pymupdf")
        doc = pymupdf.open()
        doc.new_page().insert_text((50, 60), "no outline here at all", fontsize=12)
        data = doc.tobytes()

        patterns, _ = probe_patterns(data, detect_format(data, filename="flat.pdf"))

        assert patterns["has_outline"] is False
        assert patterns["outline_depth"] == 0
        assert patterns["has_text_layer"] is True

    def test_a_format_with_no_prober_returns_empty_patterns_and_says_why(self):
        data = _make_docx_like_zip()
        detection = detect_format(data, filename="report.docx")

        patterns, detail = probe_patterns(data, detection)

        assert patterns == {}
        assert detail["no_prober_for_format"] == "docx"
        assert "pdf" in detail["probers_available"]

    def test_a_healthy_pdf_is_not_damaged(self, pdf_bytes):
        patterns, _ = probe_patterns(pdf_bytes, detect_format(pdf_bytes, filename="doc.pdf"))

        assert patterns["is_damaged"] is False

    def test_bytes_that_open_with_no_pages_are_damaged(self):
        """A truncated upload, or a PDF whose page tree is empty.

        pymupdf opens these without complaint and reports ``page_count`` 0, so nothing
        downstream raises: probe records "no text layer", every extraction task is judged
        not applicable, and the node settles holding nothing. Bytes arrived and none of
        them describe a page — that is a measurable fact and it belongs on the record.

        Written by hand rather than built with pymupdf, which refuses to *save* a zero-page
        document ("cannot save with zero pages"). Real generators emit them regardless.
        """
        data = (
            b"%PDF-1.4\n"
            b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Kids[]/Count 0>>endobj\n"
            b"trailer<</Root 1 0 R>>\n"
            b"%%EOF\n"
        )
        pytest.importorskip("pymupdf")

        patterns, detail = probe_patterns(data, detect_format(data, filename="broken.pdf"))

        assert patterns["page_count"] == 0
        assert patterns["is_damaged"] is True
        # Both inputs to the verdict are logged, so "0 pages from 0 bytes" is
        # distinguishable from "0 pages from 4 MB" without re-reading the blob.
        assert detail["byte_length"] == len(data)


# ---------------------------------------------------------------------------
# 4. The upload operation (spec 3.1, 3.3, 5.7)
# ---------------------------------------------------------------------------


class TestUploadFile:
    def test_the_file_node_is_in_flight_and_is_a_file(self, db_session, pdf_bytes):
        response = _upload(db_session, pdf_bytes, "annual.pdf", "application/pdf")

        node = DocumentRepository(db_session).get(response.document_id)
        assert node.usetype == USETYPE_FILE
        assert node.settled == SETTLED_IN_FLIGHT
        assert response.settled == SETTLED_IN_FLIGHT
        # It is a ROOT: spec 3.1 says the file node IS the tree's root, with no
        # separate container node above it.
        assert node.parent_id is None

    def test_the_stored_bytes_come_back_and_the_hash_matches(self, db_session, pdf_bytes):
        response = _upload(db_session, pdf_bytes, "annual.pdf", "application/pdf")

        assert BlobRepository(db_session).read_bytes(response.document_id) == pdf_bytes
        assert response.content_hash == f"sha256:{hashlib.sha256(pdf_bytes).hexdigest()}"
        assert response.byte_size == len(pdf_bytes)

    def test_the_file_block_records_everything_3_3_names(self, db_session, pdf_bytes):
        response = _upload(db_session, pdf_bytes, "annual.pdf", "application/pdf")

        block = DocumentRepository(db_session).get(response.document_id).structured_content["file"]
        assert set(block) == {
            "filename",
            "byte_size",
            "content_hash",
            "blob_ref",
            "declared_mime",
            "detected_mime",
            "detected_by",
            "uploaded_at",
        }
        assert block["filename"] == "annual.pdf"
        assert block["declared_mime"] == "application/pdf"
        assert block["detected_mime"] == "application/pdf"
        assert block["detected_by"] == "magic_bytes"
        # Part 9: blob_ref holds the large-object OID.
        blob = BlobRepository(db_session).get(response.document_id)
        assert block["blob_ref"] == f"lob:{blob.lob_oid}"

    def test_the_matched_block_carries_the_probe_result(self, db_session, pdf_bytes):
        _, node, _ = _upload_and_run_probe(db_session, pdf_bytes, "annual.pdf", "application/pdf")

        matched = node.structured_content["matched"]
        assert matched["format"] == "pdf"
        assert matched["patterns"]["has_text_layer"] is True
        assert matched["patterns"]["has_outline"] is True
        assert matched["patterns"]["outline_depth"] == 2
        assert "probed_at" in matched

    def test_probe_is_logged_as_a_completed_first_attempt(self, db_session, pdf_bytes):
        response, _, attempts = _upload_and_run_probe(
            db_session, pdf_bytes, "annual.pdf", "application/pdf"
        )

        probe = next(a for a in attempts if a.task == "probe")
        assert attempts[0] is probe  # probe runs first and nothing else can run before it
        assert probe.status == "completed"
        # ONE entry, not a pending one followed by a completed one: the worker replaces
        # the entry the enqueue wrote rather than appending beside it (spec 3.4's "one
        # entry per task attempt").
        assert probe.attempt == 1
        assert probe.scope_document_id == response.document_id
        assert probe.task_id is not None
        assert probe.write_mode == "self"
        assert probe.started_at is not None and probe.finished_at is not None
        # probe decides no node boundaries and creates no nodes.
        assert probe.rung is None
        assert probe.produced is None

    def test_a_format_with_no_prober_is_completed_not_skipped(self, db_session):
        """Spec 3.4: `skipped` means never attempted. probe ran — it named the format."""
        _, node, attempts = _upload_and_run_probe(
            db_session,
            _make_docx_like_zip(),
            "report.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

        probe = attempts[0]
        assert probe.status == "completed"
        assert probe.detail["no_prober_for_format"] == "docx"
        assert node.structured_content["matched"]["format"] == "docx"

    def test_a_mime_disagreement_is_recorded_on_both_sides(self, db_session, pdf_bytes):
        """Spec 3.1: record both, do not silently trust one."""
        response, node, attempts = _upload_and_run_probe(
            db_session, pdf_bytes, "annual.txt", "text/plain"
        )

        assert response.declared_mime == "text/plain"
        assert response.detected_mime == "application/pdf"
        block = node.structured_content["file"]
        assert block["declared_mime"] == "text/plain"
        assert block["detected_mime"] == "application/pdf"
        conflict = attempts[0].detail["mime_conflict"]
        assert conflict == {"declared": "text/plain", "detected": "application/pdf"}

    def test_agreement_writes_no_conflict_entry(self, db_session, pdf_bytes):
        _, _, attempts = _upload_and_run_probe(
            db_session, pdf_bytes, "annual.pdf", "application/pdf"
        )

        assert "mime_conflict" not in attempts[0].detail

    def test_a_client_that_declares_nothing_is_recorded_as_declaring_nothing(
        self, db_session, pdf_bytes
    ):
        response, _, attempts = _upload_and_run_probe(db_session, pdf_bytes, "annual.pdf", None)

        assert response.declared_mime is None
        assert "mime_conflict" not in attempts[0].detail

    def test_the_row_hash_matches_the_bytes(self, db_session, pdf_bytes):
        """`documents.content_hash` is the dedupe column; for a file node it is the bytes."""
        response = _upload(db_session, pdf_bytes, "annual.pdf", "application/pdf")

        node = DocumentRepository(db_session).get(response.document_id)
        assert node.content_hash == hashlib.sha256(pdf_bytes).hexdigest()

    def test_it_nests_under_a_parent_when_asked(self, db_session, pdf_bytes):
        parent = DocumentRepository(db_session).create(
            title="folder", content="a folder node", auto_embed=False
        )
        db_session.flush()

        response = _upload(db_session, pdf_bytes, "annual.pdf", "application/pdf", parent.id)

        assert DocumentRepository(db_session).get(response.document_id).parent_id == parent.id

    def test_empty_bytes_are_rejected(self, db_session):
        with pytest.raises(ValueError, match="empty"):
            _upload(db_session, b"", "nothing.pdf", "application/pdf")

    def test_a_nameless_upload_is_rejected_rather_than_named_for_us(self, db_session, pdf_bytes):
        with pytest.raises(ValueError, match="filename"):
            IngestService(db_session).upload_file(
                UploadedFile(data=pdf_bytes, filename="   ", content_type="application/pdf")
            )

    def test_an_unknown_parent_is_a_lookup_error(self, db_session, pdf_bytes):
        with pytest.raises(LookupError):
            _upload(db_session, pdf_bytes, "annual.pdf", "application/pdf", 10_000_000)

    def test_deleting_the_uploaded_node_unlinks_its_bytes(self, db_session, pdf_bytes):
        response = _upload(db_session, pdf_bytes, "annual.pdf", "application/pdf")
        oid = BlobRepository(db_session).get(response.document_id).lob_oid

        assert DocumentRepository(db_session).delete(response.document_id) is True
        db_session.flush()

        assert not _lob_exists(db_session, oid)

    def test_a_corrupt_pdf_fails_the_probe_without_losing_the_upload(self, db_session):
        """The bytes are the client's; the inspection is ours. Only the latter can fail.

        Step 3 ran probe inline and had to swallow this exception, because raising would
        have rolled back and discarded bytes the client had already sent. The queue removes
        that constraint: the upload committed long before the task ran, so probe simply
        RAISES and the worker records the classified failure. `matched` is absent rather
        than half-written, which is the same statement the old code made by omitting
        `patterns` — probe never got to look.
        """
        data = b"%PDF-1.7\nthis is not a parseable pdf at all"

        response, node, attempts = _upload_and_run_probe(
            db_session, data, "broken.pdf", "application/pdf"
        )

        probe = attempts[0]
        assert probe.status == "failed"
        assert probe.error
        assert probe.error_type is not None
        # The bytes still landed and are retrievable.
        assert BlobRepository(db_session).read_bytes(response.document_id) == data
        assert "matched" not in node.structured_content


# ---------------------------------------------------------------------------
# 4a. Uploading bytes that are already here
# ---------------------------------------------------------------------------


def _file_node_count(session) -> int:
    return session.execute(
        text("SELECT count(*) FROM documents WHERE usetype = 'file'")
    ).scalar_one()


def _blob_row_count(session) -> int:
    return session.execute(text("SELECT count(*) FROM document_blobs")).scalar_one()


def _lob_count(session) -> int:
    return session.execute(text("SELECT count(*) FROM pg_largeobject_metadata")).scalar_one()


def _contains_links(session, parent_id: int, target_id: int) -> list:
    return list(
        session.execute(
            select(DocumentLink).where(
                DocumentLink.source_id == parent_id,
                DocumentLink.target_id == target_id,
                DocumentLink.link_type == LINK_CONTAINS,
            )
        ).scalars()
    )


class TestUploadDeduplication:
    """The same bytes uploaded twice resolve to one node, one blob, one large object.

    The lookup is on `document_blobs.content_hash` and it is RBAC-scoped, which is not an
    optimisation: a principal who cannot READ the existing node must get their own upload
    and must not be able to tell that another one exists. Everything below is asserted
    against the real tables — including `pg_largeobject_metadata`, because a duplicated
    large object is invisible to `SELECT` and would be the whole cost this avoids.
    """

    def test_the_same_bytes_twice_return_one_node_and_say_so(self, db_session, pdf_bytes):
        first = _upload(db_session, pdf_bytes, "annual.pdf", "application/pdf")
        assert first.was_existing is False

        second = _upload(db_session, pdf_bytes, "annual-copy.pdf", "application/pdf")

        assert second.document_id == first.document_id
        # The flag is the point: without it a caller cannot tell a found id from a created
        # one, and 200-vs-201 has nothing to key on.
        assert second.was_existing is True
        assert second.linked_into_parent is False
        # The response describes the node that EXISTS, so it carries the filename that was
        # recorded with the bytes, not the one this request happened to use.
        assert second.filename == "annual.pdf"
        assert second.content_hash == first.content_hash

    def test_nothing_is_written_by_the_second_upload(self, db_session, pdf_bytes):
        _upload(db_session, pdf_bytes, "annual.pdf", "application/pdf")
        db_session.flush()
        nodes, blobs, lobs = (
            _file_node_count(db_session),
            _blob_row_count(db_session),
            _lob_count(db_session),
        )
        tasks = db_session.execute(text("SELECT count(*) FROM task_queue")).scalar_one()

        _upload(db_session, pdf_bytes, "annual.pdf", "application/pdf")
        db_session.flush()

        assert _file_node_count(db_session) == nodes
        assert _blob_row_count(db_session) == blobs
        # A second large object is the expensive half of the duplication and the half
        # nothing else in the database would report.
        assert _lob_count(db_session) == lobs
        assert BlobRepository(db_session).find_orphaned_lobs() == []
        # And no second `probe`: the bytes have already been looked at.
        assert db_session.execute(text("SELECT count(*) FROM task_queue")).scalar_one() == tasks

    def test_different_bytes_still_create_a_second_node(self, db_session, pdf_bytes):
        first = _upload(db_session, pdf_bytes, "annual.pdf", "application/pdf")
        second = _upload(
            db_session, pdf_bytes + b"\n% one byte more", "other.pdf", "application/pdf"
        )

        assert second.document_id != first.document_id
        assert second.was_existing is False
        assert _file_node_count(db_session) == 2

    def test_placing_known_bytes_in_a_tree_links_instead_of_copying(self, db_session, pdf_bytes):
        """Uploaded once to be analysed, then "uploaded" into a folder."""
        orphan = _upload(db_session, pdf_bytes, "annual.pdf", "application/pdf")
        repo = DocumentRepository(db_session)
        folder = repo.create(title="folder", content="a folder node", auto_embed=False)
        db_session.flush()

        placed = _upload(db_session, pdf_bytes, "annual.pdf", "application/pdf", folder.id)

        assert placed.document_id == orphan.document_id
        assert placed.was_existing is True
        assert placed.linked_into_parent is True
        assert _file_node_count(db_session) == 1
        assert len(_contains_links(db_session, folder.id, orphan.document_id)) == 1

        node = repo.get(orphan.document_id)
        # NOT adopted. The node keeps the (absent) parent it was uploaded with — moving
        # somebody else's record as a side effect of a third party's upload is a curator
        # action, not this one.
        assert node.parent_id is None
        # And the consequence of a link being a GRAPH edge: the folder's subtree does not
        # reach it. This assertion exists to make that fact fail loudly if anyone ever
        # assumes otherwise.
        assert repo.get_children(folder.id, depth=-1) == []

    def test_placing_it_twice_does_not_create_a_second_link(self, db_session, pdf_bytes):
        orphan = _upload(db_session, pdf_bytes, "annual.pdf", "application/pdf")
        folder = DocumentRepository(db_session).create(
            title="folder", content="a folder node", auto_embed=False
        )
        db_session.flush()

        _upload(db_session, pdf_bytes, "annual.pdf", "application/pdf", folder.id)
        again = _upload(db_session, pdf_bytes, "annual.pdf", "application/pdf", folder.id)

        assert again.document_id == orphan.document_id
        assert again.linked_into_parent is True
        assert len(_contains_links(db_session, folder.id, orphan.document_id)) == 1

    def test_a_file_already_a_child_of_that_parent_gains_no_duplicate_edge(
        self, db_session, pdf_bytes
    ):
        """The tree edge already says "contains"; a graph edge repeating it is noise."""
        folder = DocumentRepository(db_session).create(
            title="folder", content="a folder node", auto_embed=False
        )
        db_session.flush()
        first = _upload(db_session, pdf_bytes, "annual.pdf", "application/pdf", folder.id)

        second = _upload(db_session, pdf_bytes, "annual.pdf", "application/pdf", folder.id)

        assert second.document_id == first.document_id
        assert second.was_existing is True
        assert second.linked_into_parent is False
        assert _contains_links(db_session, folder.id, first.document_id) == []
        assert DocumentRepository(db_session).get(first.document_id).parent_id == folder.id

    def test_an_unreadable_match_is_no_match_and_leaks_nothing(self, db_session, pdf_bytes):
        """The RBAC predicate IS the correctness condition (the user's "isolated zones").

        `insider` holds the only grant on the folder the file lives under. `outsider`
        uploads the identical bytes and must get a node of their own — and a response
        indistinguishable from the one they would get if nothing like it existed.
        """
        repo = DocumentRepository(db_session)
        vault = repo.create(title="vault", content="the private folder", auto_embed=False)
        db_session.flush()
        insider = _principal(db_session, "insider")
        outsider = _principal(db_session, "outsider")
        db_session.add(AccessGrant(document_id=vault.id, principal_id=insider.id, level="write"))
        db_session.flush()

        with _as(insider):
            hidden = _upload(db_session, pdf_bytes, "annual.pdf", "application/pdf", vault.id)
            # The insider, who can read it, deduplicates against it. Asserted before the
            # outsider uploads, because the outsider's node lands UNGOVERNED and is then
            # readable by everybody — including the insider, whose lookup would legitimately
            # prefer it as the more recent match.
            again = _upload(db_session, pdf_bytes, "annual.pdf", "application/pdf")
        assert again.was_existing is True
        assert again.document_id == hidden.document_id

        with _as(outsider):
            theirs = _upload(db_session, pdf_bytes, "annual.pdf", "application/pdf")

        assert theirs.document_id != hidden.document_id
        # Byte-identical to the answer for bytes nobody has ever uploaded. There is no
        # field, no id and no flag from which the hidden node can be inferred.
        assert theirs.was_existing is False
        assert theirs.linked_into_parent is False
        assert _file_node_count(db_session) == 2

    def test_linking_into_a_folder_needs_write_on_the_folder(self, db_session, pdf_bytes):
        """The create path gets this gate from `repo.create`; the link path must ask for it.

        Otherwise read-only access to a folder would be enough to hang documents off it —
        by uploading bytes that are already stored, which writes nothing else at all.
        """
        repo = DocumentRepository(db_session)
        shared = repo.create(title="read-only folder", content="shared", auto_embed=False)
        db_session.flush()
        reader = _principal(db_session, "reader")
        db_session.add(AccessGrant(document_id=shared.id, principal_id=reader.id, level="read"))
        db_session.flush()
        orphan = _upload(db_session, pdf_bytes, "annual.pdf", "application/pdf")

        with _as(reader), pytest.raises(AccessDeniedError):
            _upload(db_session, pdf_bytes, "annual.pdf", "application/pdf", shared.id)

        assert _contains_links(db_session, shared.id, orphan.document_id) == []


def _grants_on(session, document_id: int) -> list:
    return list(
        session.execute(select(AccessGrant).where(AccessGrant.document_id == document_id)).scalars()
    )


class TestPrivateUpload:
    """`private=True` — opt-in, off by default, and the default is the design.

    JMFTS assumes access is controlled outside the application, so a token means access to
    the SHARED knowledgebase and an upload is readable by every principal. The first test
    below is the regression guard on exactly that: if anyone ever makes privacy the
    default, it fails, and it fails saying what changed rather than in some downstream
    dedup assertion.

    What the flag closes is the gap the default leaves for a ROOTLESS upload: no ancestor
    means no access-control root above it, and a grant issued later cannot cover it
    retroactively because ACR membership is strictly the tree path. One `access_grants` row
    on the new node is the entire mechanism — being an ACR is defined as having grants.
    """

    def test_the_default_is_shared_and_stays_shared(self, db_session, pdf_bytes):
        """THE STANDING DESIGN RULE. An upload with no flag is ungoverned and world-readable.

        "Private until shared" is the rejected design: it creates bugs of ignorance in
        every deployment holding the stated assumption that a JMFTS token IS access to the
        shared knowledgebase.
        """
        uploader = _principal(db_session, "uploader")
        stranger = _principal(db_session, "stranger")

        with _as(uploader):
            response = _upload(db_session, pdf_bytes, "annual.pdf", "application/pdf")

        node = DocumentRepository(db_session).get(response.document_id)
        assert _grants_on(db_session, node.id) == []  # not an ACR: nothing governs it
        with _as(stranger):
            assert can_read(db_session, node) is True
            # And the shared node is the shared dedup target, which is the point of the
            # default rather than a side effect of it.
            again = _upload(db_session, pdf_bytes, "annual.pdf", "application/pdf")
        assert again.document_id == node.id
        assert again.was_existing is True

    def test_private_makes_the_node_its_own_access_root(self, db_session, pdf_bytes):
        uploader = _principal(db_session, "uploader")
        stranger = _principal(db_session, "stranger")

        with _as(uploader):
            response = _upload(db_session, pdf_bytes, "annual.pdf", "application/pdf", private=True)

        node = DocumentRepository(db_session).get(response.document_id)
        grants = _grants_on(db_session, node.id)
        assert [(g.principal_id, g.level) for g in grants] == [(uploader.id, "write")]
        with _as(uploader):
            assert can_read(db_session, node) is True
        with _as(stranger):
            assert can_read(db_session, node) is False

    def test_the_uploader_can_write_what_they_uploaded(self, db_session, pdf_bytes):
        """`write`, not `read`: a read-only grant on your own file means every correction
        needs the appliance owner."""
        uploader = _principal(db_session, "uploader")

        with _as(uploader):
            response = _upload(db_session, pdf_bytes, "annual.pdf", "application/pdf", private=True)
            node = DocumentRepository(db_session).get(response.document_id)
            assert can_write(db_session, node) is True

    def test_private_with_no_bound_principal_is_refused(self, db_session, pdf_bytes):
        """Unbound in-process callers and the owner bearer have no principals row, so
        there is nobody to grant to. Ignoring the flag would return a 201 for a
        world-readable node to a caller who asked for the opposite."""
        with pytest.raises(ValueError, match="bound non-owner principal"):
            _upload(db_session, pdf_bytes, "annual.pdf", "application/pdf", private=True)

        with _as(OWNER), pytest.raises(ValueError, match="bound non-owner principal"):
            _upload(db_session, pdf_bytes, "annual.pdf", "application/pdf", private=True)

        # And nothing was written: a restriction that cannot be applied is not a detail to
        # be fixed after the node exists.
        assert _file_node_count(db_session) == 0

    def test_private_with_a_parent_is_refused_and_says_where_to_go(self, db_session, pdf_bytes):
        """Grants are ADDITIVE, so a new ACR inside a governed subtree WIDENS access. Doing
        it silently would make `private=True` a lie the caller could not detect."""
        folder = DocumentRepository(db_session).create(
            title="folder", content="a folder node", auto_embed=False
        )
        db_session.flush()
        uploader = _principal(db_session, "uploader")

        with _as(uploader), pytest.raises(ValueError, match="cannot be combined with parent_id"):
            _upload(db_session, pdf_bytes, "annual.pdf", "application/pdf", folder.id, private=True)

        assert _file_node_count(db_session) == 0

    def test_a_shared_node_is_never_returned_to_a_private_upload(self, db_session, pdf_bytes):
        """The dedup interaction, and the reason `private` narrows the lookup.

        The ordinary lookup matches anything READABLE, and an ungoverned node is readable
        by everyone — so without the narrowing a private upload of already-present bytes
        would resolve to the shared node and the caller would silently not be private.
        """
        shared = _upload(db_session, pdf_bytes, "annual.pdf", "application/pdf")
        uploader = _principal(db_session, "uploader")

        with _as(uploader):
            mine = _upload(db_session, pdf_bytes, "annual.pdf", "application/pdf", private=True)

        assert mine.document_id != shared.document_id
        assert mine.was_existing is False
        assert _file_node_count(db_session) == 2
        node = DocumentRepository(db_session).get(mine.document_id)
        assert [g.principal_id for g in _grants_on(db_session, node.id)] == [uploader.id]

    def test_the_same_principal_uploading_privately_twice_gets_one_node(
        self, db_session, pdf_bytes
    ):
        """Holding a grant on the node is what "already mine" means, so the second private
        upload deduplicates against the first rather than accumulating copies."""
        uploader = _principal(db_session, "uploader")

        with _as(uploader):
            first = _upload(db_session, pdf_bytes, "annual.pdf", "application/pdf", private=True)
            second = _upload(
                db_session, pdf_bytes, "annual-copy.pdf", "application/pdf", private=True
            )

        assert second.document_id == first.document_id
        assert second.was_existing is True
        assert _file_node_count(db_session) == 1
        assert len(_grants_on(db_session, first.document_id)) == 1

    def test_two_principals_uploading_privately_get_a_node_each(self, db_session, pdf_bytes):
        """The cost of isolation, not a defect in it — nothing can deduplicate across a
        boundary drawn to stop one side learning what the other has."""
        one = _principal(db_session, "one")
        two = _principal(db_session, "two")

        with _as(one):
            a = _upload(db_session, pdf_bytes, "annual.pdf", "application/pdf", private=True)
        with _as(two):
            b = _upload(db_session, pdf_bytes, "annual.pdf", "application/pdf", private=True)

        assert a.document_id != b.document_id
        assert b.was_existing is False
        assert _file_node_count(db_session) == 2


# ---------------------------------------------------------------------------
# 4b. The ingest record survives ordinary edits, and does not hijack text dedupe
# ---------------------------------------------------------------------------


class TestTheIngestRecordIsNotCallerWritable:
    """`structured_content` stopped being purely the caller's the moment ingestion put
    state in it. A whole-object PATCH — the ordinary way to add a tag — used to delete the
    `file` block, `matched`, and the entire append-only attempt log; `probe` then found no
    `file` block, raised, was classified PERMANENT, and the node was dead with its bytes
    still in a large object nothing pointed at."""

    def test_a_metadata_edit_keeps_the_file_block_and_the_log(self, db_session, pdf_bytes):
        response = _upload(db_session, pdf_bytes, "annual.pdf", "application/pdf")
        db_session.commit()

        updated = DocumentService(db_session).update_document(
            response.document_id, DocumentUpdate(structured_content={"tag": "q3"})
        )

        assert updated.structured_content["tag"] == "q3"
        assert updated.structured_content["file"]["filename"] == "annual.pdf"
        assert [a["task"] for a in updated.structured_content["attempts"]] == ["probe"]
        # And the node is still ingestible: probe finds its `file` block, and the whole
        # pipeline runs on top of an edit that used to destroy the record it reads.
        assert drain_ingest_queue(db_session) > 1
        node = DocumentRepository(db_session).get(response.document_id)
        assert node.structured_content["matched"]["format"] == "pdf"
        assert node.structured_content["tag"] == "q3"

    def test_a_caller_key_is_still_replaced_wholesale(self, db_session, pdf_bytes):
        """Only the reserved keys are carried over — the caller's half keeps replace
        semantics, so a client can still drop a key it no longer wants."""
        response = _upload(db_session, pdf_bytes, "annual.pdf", "application/pdf")
        db_session.commit()
        service = DocumentService(db_session)

        service.update_document(
            response.document_id, DocumentUpdate(structured_content={"tag": "q3", "old": 1})
        )
        updated = service.update_document(
            response.document_id, DocumentUpdate(structured_content={"tag": "q4"})
        )

        assert updated.structured_content["tag"] == "q4"
        assert "old" not in updated.structured_content
        assert "file" in updated.structured_content

    def test_naming_a_reserved_key_is_refused_rather_than_ignored(self, db_session, pdf_bytes):
        """An edit that silently does not do what it says is the failure being avoided."""
        response = _upload(db_session, pdf_bytes, "annual.pdf", "application/pdf")
        db_session.commit()

        with pytest.raises(ValueError, match="attempts"):
            DocumentService(db_session).update_document(
                response.document_id, DocumentUpdate(structured_content={"attempts": []})
            )

    def test_the_byte_hash_does_not_short_circuit_a_later_text_ingest(self, db_session):
        """`documents.content_hash` on a file node is the sha256 of the BYTES, and for a
        text file that is the same hex as the sha256 of its UTF-8 content. The idempotency
        lookup used to match on the hash alone, so ingesting the same file as text
        resolved to the contentless file node and was never ingested at all."""
        text_bytes = b"# Notes\n\nquokka husbandry for beginners\n"
        response = _upload(db_session, text_bytes, "notes.md", "text/markdown")
        digest = hashlib.sha256(text_bytes).hexdigest()

        repo = DocumentRepository(db_session)
        assert repo.get(response.document_id).content_hash == digest
        # Same hash, same (absent) parent — and no match, because the file node does not
        # hold the hashed text.
        assert repo.find_by_hash_and_parent(digest, None) is None

        # A real text node with that content is still found, which is what the lookup is for.
        node = repo.create(title="notes", content=text_bytes.decode(), auto_embed=False)
        db_session.flush()
        assert repo.find_by_hash_and_parent(digest, None).id == node.id

    def test_a_file_node_that_extraction_has_filled_in_is_still_excluded(
        self, db_session, pdf_bytes
    ):
        """The case above passes for a reason that expired.

        It exercises a `.md` upload, which has no extractor and so keeps `content` NULL —
        and the guard that excluded file nodes was `content IS NOT NULL`. Since `e7b1ffb`,
        `extract:text` writes the extracted markdown onto the FILE NODE, whose
        `content_hash` is still the sha256 of the bytes. The old guard then let it through
        and the short-circuit was back for every format that reaches extraction. Draining
        the queue is what makes this a real test rather than a restatement of the last one.
        """
        response = _upload(db_session, pdf_bytes, "annual.pdf", "application/pdf")
        drain_ingest_queue(db_session)

        repo = DocumentRepository(db_session)
        node = repo.get(response.document_id)
        # Preconditions: extraction ran, and the hash still describes the bytes.
        assert node.content
        assert node.content_hash == hashlib.sha256(pdf_bytes).hexdigest()

        assert repo.find_by_hash_and_parent(node.content_hash, None) is None


# ---------------------------------------------------------------------------
# 5. The generated multipart route
# ---------------------------------------------------------------------------


class TestUploadOverHttp:
    """The `UploadedFile` → `UploadFile` substitution in `api/wiring.py`.

    Without it there is no way to have a multipart endpoint that is both generated from
    the `@expose` registry and free of FastAPI imports in `jmfts_core` — see
    `contracts/upload.py`. These assert the translation works end to end rather than only
    that the route exists.
    """

    def test_a_multipart_post_creates_the_node(self, client_with_db, db_session, pdf_bytes):
        response = client_with_db.post(
            "/ingest/file",
            files={"file": ("annual.pdf", pdf_bytes, "application/pdf")},
        )

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["usetype"] == "file"
        assert body["settled"] == SETTLED_IN_FLIGHT
        assert body["content_hash"] == f"sha256:{hashlib.sha256(pdf_bytes).hexdigest()}"
        assert body["detected_by"] == "magic_bytes"
        assert [a["task"] for a in body["attempts"]] == ["probe"]

    def test_the_bytes_survive_the_multipart_encoding(self, client_with_db, db_session):
        """Binary-safe: a NUL and a high byte must arrive unchanged."""
        data = bytes(range(256)) * 8

        response = client_with_db.post(
            "/ingest/file",
            files={"file": ("raw.bin", data, "application/octet-stream")},
        )

        assert response.status_code == 201, response.text
        stored = BlobRepository(db_session).read_bytes(response.json()["document_id"])
        assert stored == data

    def test_parent_id_is_a_query_parameter(self, client_with_db, db_session, pdf_bytes):
        parent = DocumentRepository(db_session).create(
            title="folder", content="a folder node", auto_embed=False
        )
        db_session.flush()

        response = client_with_db.post(
            f"/ingest/file?parent_id={parent.id}",
            files={"file": ("annual.pdf", pdf_bytes, "application/pdf")},
        )

        assert response.status_code == 201, response.text
        assert (
            DocumentRepository(db_session).get(response.json()["document_id"]).parent_id
            == parent.id
        )

    def test_an_unknown_parent_is_a_404(self, client_with_db, pdf_bytes):
        response = client_with_db.post(
            "/ingest/file?parent_id=10000000",
            files={"file": ("annual.pdf", pdf_bytes, "application/pdf")},
        )

        assert response.status_code == 404

    def test_private_is_a_query_parameter_and_reaches_the_service(self, client_with_db, pdf_bytes):
        """A scalar is a query parameter even on a multipart operation — FastAPI's own
        inference, asserted rather than assumed, since `options` next door had to become a
        JSON form field for exactly the opposite reason.

        The test client authenticates as the OWNER, which has no principals row, so a
        request that arrives carrying `private=True` is a 400 naming that. Which is the
        proof: an ignored query parameter would be a 201.
        """
        response = client_with_db.post(
            "/ingest/file?private=true",
            files={"file": ("annual.pdf", pdf_bytes, "application/pdf")},
        )

        assert response.status_code == 400, response.text
        assert "bound non-owner principal" in response.json()["detail"]

    def test_an_upload_with_no_private_flag_is_still_a_201(self, client_with_db, pdf_bytes):
        """The default is unchanged over HTTP too: shared, and it does not 400."""
        response = client_with_db.post(
            "/ingest/file",
            files={"file": ("annual.pdf", pdf_bytes, "application/pdf")},
        )

        assert response.status_code == 201, response.text

    def test_an_empty_upload_is_a_400(self, client_with_db):
        response = client_with_db.post(
            "/ingest/file",
            files={"file": ("empty.pdf", b"", "application/pdf")},
        )

        assert response.status_code == 400


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _upload(session, data: bytes, filename: str, mime, parent_id=None, private=False):
    return IngestService(session).upload_file(
        UploadedFile(data=data, filename=filename, content_type=mime),
        parent_id=parent_id,
        private=private,
    )


def _upload_and_run_probe(session, data: bytes, filename: str, mime, parent_id=None):
    """Upload, then run the queued `probe` synchronously. Returns the node's attempt log.

    The upload is asynchronous from step 4 on (spec 5.7), so every assertion about what
    probe FOUND has to drain the queue first. `drain_ingest_queue` runs the real worker
    body on this session rather than starting a thread and waiting on it.
    """
    response = _upload(session, data, filename, mime, parent_id)
    drain_ingest_queue(session)
    node = DocumentRepository(session).get(response.document_id)
    attempts = [
        AttemptRecord.model_validate(entry)
        for entry in (node.structured_content or {}).get("attempts") or []
    ]
    return response, node, attempts
