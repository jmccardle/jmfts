"""RenditionRepository — derived bytes, and which bytes a page image is drawn from.

``docs/OFFICE_SPEC.md`` Part 6. The table's reasoning is in
:mod:`jmfts_core.models.document_rendition` and in migration 024; this module is what writes
it, what reads it, and — in :func:`pdf_for` — the one question every page-image caller has to
ask before it can render anything.

**The difference from** :class:`~jmfts_core.repositories.blob.BlobRepository` **is one
method's behaviour, and it is the whole reason the two are not one class.** ``store`` there
REFUSES a second write, because the uploaded bytes are immutable and replacing them would
rewrite history the attempt log claims is immutable. ``store`` here REPLACES, because a
rendition is derived: re-running ``render:pdf`` after a LibreOffice upgrade is
``SPRINT_0_6_0.md`` 4.9's answer to what happens on upgrade, and a re-run that could not
replace would either collide or leak. The old large object is unlinked in the same
transaction as the new row, so the two cannot come apart.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from jmfts_core.models.document import Document
from jmfts_core.models.document_rendition import DocumentRendition
from jmfts_core.rendering import PDF_MEDIA_TYPE
from jmfts_core.repositories.blob import BlobLeakError, BlobRepository, lob_exists, unlink_lob
from jmfts_core.repositories.evidence import EvidenceRepository

logger = logging.getLogger(__name__)

#: A PDF of the whole document, for drawing pages and rectangles on. The rendition proper.
KIND_PDF = "pdf"

#: A legacy binary re-expressed as the OOXML package of the same application, so that the
#: tier-2 readers can open it (``OFFICE_SPEC.md`` Part 1's first reason for LibreOffice).
KIND_OOXML = "ooxml"

#: Which evidence row describes each kind. The BYTES are this table's and the DESCRIPTION is
#: a row on the node, and the two are written together by
#: :mod:`jmfts_core.render_tasks` and removed together by :meth:`RenditionRepository.delete` —
#: which is what lets a reader ask one question ("is there a rendition") and get one answer.
#:
#: **NOT** ``file.rendition_ref``, **which is what** ``OFFICE_SPEC.md`` **Part 6 proposed**,
#: and the reason is a rule that document does not reach: the ``file`` block is written by the
#: upload and "never changes after upload" (``INGEST_SPEC.md`` 3.3, quoted at
#: ``repositories/blob.py``'s ``store``). A task that wrote into it would be editing the
#: record of what was received in order to say something about what was derived. The second
#: reason is the value: ``rendition_ref`` names a large-object OID, and an OID is a physical
#: address the repository maintains, in a namespace a caller can PATCH — while what a reader
#: actually needs is the page count and the per-page geometry, which a ref cannot carry.
EVIDENCE_NAME: dict[str, str] = {KIND_PDF: "rendition", KIND_OOXML: "conversion"}

#: What a rendition is stored as, and what a source PDF has to detect as before
#: :func:`pdf_for` hands its bytes back. ``rendering.PDF_MEDIA_TYPE`` and not a second copy:
#: that constant is defined beside the code that opens the bytes, and it says at its
#: definition that it is what ``file.detected_mime`` has to hold — which is the check below.
PDF_MIME = PDF_MEDIA_TYPE


class NoRenditionError(LookupError):
    """This document has no PDF to draw on, and the message says what would give it one.

    ``LookupError`` rather than ``ValueError``: nothing here is malformed. The document is
    fine, the request is fine, and what is absent is a derived artefact — which is the same
    shape as a cache miss and is recoverable by running one task. Callers on the query path
    map it to 409, beside ``UnreadablePdf``; callers on the ingest path let it fail the task.

    It is an EXCEPTION and not a ``None`` return, because every caller of :func:`pdf_for`
    has a different thing to say about the absence and none of them can say it from a null.
    """


@dataclass(frozen=True)
class PdfSource:
    """The bytes a page image is rendered from, and where they came from.

    ``origin`` is carried rather than inferred because the two cases mean different things to
    a caller drawing a rectangle on the result: a ``source`` PDF's coordinates are the ones
    ``citation`` recovered from its own text layer, and a ``rendition``'s are LibreOffice's
    pagination of a document that had no pages until it ran. A viewer that cached one must
    not silently start drawing on the other.
    """

    data: bytes
    origin: str
    #: The rendition row, when ``origin`` is ``"rendition"``. ``None`` for a source PDF.
    rendition: Optional[DocumentRendition] = None


class RenditionRepository:
    """Store, read and unlink the large objects behind ``document_renditions`` rows."""

    def __init__(self, session: Session):
        self.session = session

    # =========================================================================
    # Write
    # =========================================================================

    def store(
        self,
        document_id: int,
        kind: str,
        data: bytes,
        *,
        mime_type: str,
        renderer_version: Optional[str] = None,
    ) -> DocumentRendition:
        """Write ``data`` as this document's ``kind`` artefact, replacing any it had.

        Replacement rather than refusal — the one place this class deliberately behaves
        differently from :meth:`BlobRepository.store`. The old object is unlinked inside this
        transaction, so a rollback leaves the previous rendition intact and a commit leaves
        exactly one.
        """
        existing = self.get(document_id, kind)
        if existing is not None:
            unlink_lob(self.session, existing.lob_oid)
            self.session.delete(existing)
            self.session.flush()

        lob_oid = self.session.execute(
            text("SELECT lo_from_bytea(0, :data)"), {"data": data}
        ).scalar_one()

        row = DocumentRendition(
            document_id=document_id,
            kind=kind,
            lob_oid=lob_oid,
            mime_type=mime_type,
            byte_size=len(data),
            content_hash=hashlib.sha256(data).hexdigest(),
            renderer_version=renderer_version or None,
        )
        self.session.add(row)
        self.session.flush()
        return row

    # =========================================================================
    # Read
    # =========================================================================

    def get(self, document_id: int, kind: str) -> Optional[DocumentRendition]:
        """The metadata row, or None when this document has no artefact of that kind."""
        return self.session.execute(
            select(DocumentRendition).where(
                DocumentRendition.document_id == document_id,
                DocumentRendition.kind == kind,
            )
        ).scalar_one_or_none()

    def read_bytes(self, document_id: int, kind: str) -> Optional[bytes]:
        """The stored bytes, or None when there is no row at all.

        Raises :class:`BlobLeakError` when the row exists and the object does not, for
        :meth:`BlobRepository.read_bytes`'s reason: the row is an assertion that the bytes are
        there, so its being wrong is a corruption report and not a quiet ``None``. A
        rendition is regenerable, which makes this recoverable — but recovering it is
        re-running the task, not pretending the row was never written.
        """
        row = self.get(document_id, kind)
        if row is None:
            return None
        # Asked BEFORE the read, not after: `lo_get` on a missing OID raises, and a raise
        # here would abort the worker's whole transaction rather than report one bad row.
        if not lob_exists(self.session, row.lob_oid):
            raise BlobLeakError(
                f"document_renditions row {row.id} (document {document_id}, kind {kind!r}) "
                f"names large object {row.lob_oid}, which does not exist"
            )
        raw = self.session.execute(text("SELECT lo_get(:oid)"), {"oid": row.lob_oid}).scalar_one()
        return bytes(raw)

    # =========================================================================
    # Delete — the half ON DELETE CASCADE cannot do
    # =========================================================================

    def delete(self, document_id: int, kind: str) -> bool:
        """Unlink one artefact, delete its row, and retract the evidence that described it.

        This is the eviction half of Block G's answer to ``OFFICE_SPEC.md`` Part 12 question
        1: renditions are on always and a sweeper may delete them, because ``render:pdf``
        makes them again. Nothing schedules that sweeper yet — see the module note in
        :mod:`jmfts_core.render_tasks`.

        **THE EVIDENCE ROW GOES WITH IT, and that is a correction of an earlier version of
        this method.** The row (``rendition`` / ``conversion``) is what a reader asks in order
        to find out whether this node has a page to show — the browser's renderer does
        exactly that — so leaving it standing after eviction would make it assert something
        false. The first version kept it, on the argument that "what was rendered, and when"
        stays true after the bytes go; that argument is right about the SENTENCE and wrong
        about the ROW, because a row nobody can act on is read as availability. Existence is
        therefore asserted in one place maintained by one method: these two writes go
        together and they come apart together.
        """
        row = self.get(document_id, kind)
        if row is None:
            return False
        unlink_lob(self.session, row.lob_oid)
        self.session.delete(row)
        EvidenceRepository(self.session).delete(document_id, EVIDENCE_NAME[kind])
        self.session.flush()
        return True

    def unlink_subtree(self, root_id: int) -> int:
        """Unlink every derived object under ``root_id`` (inclusive). Returns the count.

        Called by :meth:`DocumentRepository.delete` beside
        :meth:`BlobRepository.unlink_subtree` and for the identical reason: after the cascade
        there is nothing left that knows the OIDs. The rows are left to the cascade.
        """
        rows = self.session.execute(
            text("""
                SELECT r.lob_oid
                FROM document_renditions r
                JOIN documents d ON d.id = r.document_id
                WHERE d.id = :root
                   OR d.path @> jsonb_build_array(:root)
                """),
            {"root": root_id},
        ).scalars()

        return sum(1 for oid in list(rows) if unlink_lob(self.session, oid))


def pdf_for(session: Session, document: Document) -> PdfSource:
    """The PDF this node's pages are drawn from: its own bytes, or its rendition.

    **This is the function ``OFFICE_SPEC.md`` Part 1's simplification cashes out as**, and it
    is what step 33 and Part 7's ``/image`` fork on. After ``render:pdf`` has run, every
    renderable document in the system is a PDF, so there is one code path for drawing a page
    and it needs no tier-3 dependency — a search node with no LibreOffice anywhere near it
    serves citation images out of ``pymupdf``, which is a base dependency.

    Three answers and no fourth:

    * the node's ``file.detected_mime`` is ``application/pdf`` — its own stored bytes,
      ``origin='source'``;
    * it has a ``pdf`` rendition — those bytes, ``origin='rendition'``;
    * neither — :class:`NoRenditionError`, naming the task that would produce one.

    **``file.detected_mime`` and not ``matched.format``**, which are the same reading of the
    same magic bytes taken at two different moments. The ``file`` block is written by the
    upload, so it answers before ``probe`` has run and for a node whose probe failed;
    ``matched`` is the scheduler's vocabulary and is what the ingest side branches on
    (``render_tasks`` reads it, because a row is only evaluated once ``probe`` has written
    it). ``document_service._pdf_bytes`` already made this choice for the query path and said
    why — "the bytes are evidence and the header is a claim" — and one read path with two
    answers to "is this a PDF" is the disagreement worth not having.

    **Never a fallback to the source bytes for a non-PDF.** Handing a ``.docx`` to a PDF
    renderer produces an exception a long way from the cause, and handing back the raw
    package as "the image" produces a broken picture with a 200 beside it.
    """
    file_block = EvidenceRepository(session).read(document.id, "file")
    detected = file_block.get("detected_mime") if isinstance(file_block, dict) else None

    if detected == PDF_MIME:
        data = BlobRepository(session).read_bytes(document.id)
        if data is None:
            raise NoRenditionError(
                f"document {document.id} is a PDF with no stored bytes; there is nothing to "
                "render, and the upload it describes is gone"
            )
        return PdfSource(data=data, origin="source")

    repo = RenditionRepository(session)
    row = repo.get(document.id, KIND_PDF)
    if row is None:
        raise NoRenditionError(
            f"document {document.id} is {detected or 'bytes nothing recognised'} and has no "
            "PDF rendition, so this appliance has no page to draw. A rendition is produced "
            "by the `render:pdf` task, which needs a LibreOffice: see jmfts_core/convert.py "
            "for the two halves of tier 3, and re-run the task for this node once they are "
            "present"
        )
    data = repo.read_bytes(document.id, KIND_PDF)
    assert data is not None  # `get` found the row; `read_bytes` raises if the object is gone
    return PdfSource(data=data, origin="rendition", rendition=row)
