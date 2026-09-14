"""``render:pdf`` and ``convert:ooxml`` — the two tasks that drive LibreOffice.

``docs/OFFICE_SPEC.md`` Part 6 and Part 11 step 7. Both handlers do the same three things in
the same order — read the node's stored bytes, hand them to tier 3, store what comes back as
a derived artefact — and they differ in what they ask for and in what the absence of the
result means.

**``render:pdf`` produces the page a rectangle can be drawn on.** An office document has no
pages until something paginates it, so until this runs there is nothing for
``/documents/{id}/image`` to serve and nothing for step 33's ``citation`` to recover a
rectangle against. Part 6 gives three reasons to store the result rather than convert on
demand, and the first is the one that shapes the fleet: after this task every renderable
document in the system is a PDF, so serving a citation image is ``pymupdf`` over stored bytes
and **no node on the query path needs LibreOffice at all**.

**``convert:ooxml`` produces the package a reader can open.** ``.doc``, ``.xls`` and ``.ppt``
are OLE2 binaries that no tier-2 reader opens; Part 1 routes them through LibreOffice rather
than through three more binary-format wheels, each worse at its format than LibreOffice is.

**Advisory, and only one of them.** ``render:pdf`` is in
:data:`~jmfts_core.models.task_queue.ADVISORY_TASK_TYPES` because Part 6 says "Rendering
degrades; retrieval does not" — a ``.docx`` whose rendition failed still has its text, its
chunks, its vectors and its place in every index, and failing the node over a missing picture
would throw all of that away. ``convert:ooxml`` is NOT advisory, and the asymmetry is the
rule the constant states rather than an oversight: a legacy binary with no conversion has no
text at all, so a node that could not be converted is a node with nothing in it, and
``settled='failed'`` is the true thing to say about it.

**What nothing reads yet, said here rather than discovered later.** The OOXML package this
writes has no consumer in the appliance today. ``extract:text`` dispatches on the format
``probe`` detected from the UPLOADED bytes, and for an OLE2 container that is ``ole2``, which
has no extractor — so wiring the conversion into the read path needs one concept this
appliance does not have: **which bytes are a node's current representation**, source blob
unless a conversion superseded it. That is a change to what every reader resolves before it
opens anything (``probe``, ``extract:text``, the sheet tasks, ``citation``), it is not
step 32, and building half of it here — a lookup in one handler, a blob read everywhere else
— would produce a tree where two tasks disagree about what the document is. The artefact is
stored and described; the resolution is named as missing.

**Where a reader looks to find out whether there is a rendition, and why it is not
``file.rendition_ref``.** Part 6's table says this task produces "a blob OID under
``file.rendition_ref``", and that is the one thing in Part 6 this step does not do. Two
reasons, and the first is a rule Part 6 does not reach: the ``file`` block is the record of
what was RECEIVED and "never changes after upload" (``INGEST_SPEC.md`` 3.3, quoted at
``repositories/blob.py``'s ``store``), so a task writing into it would be editing the upload's
record in order to say something about a derivation. The second is the value: an OID is a
physical address the repository maintains, in a namespace a caller may ``PATCH``, and what a
reader actually needs is the page count and the per-page geometry — which is what a box drawn
over a page has to be scaled against, and which a ref cannot carry. So the rendition gets its
own evidence row, ``rendition``, and :data:`~jmfts_core.repositories.rendition.EVIDENCE_NAME`
is where that name is spelled once.

**Renditions are unconditional, and eviction is the policy.** ``SPRINT_0_6_0.md`` Block G
answers ``OFFICE_SPEC.md`` Part 12 question 1 with a measurement — original bytes are 4.8% of
a reference database, so a rendition per office document is an upper bound of +4.8% and not
the doubling the question assumed — and concludes: on always, evictable by a sweeper,
regenerated on read. :meth:`RenditionRepository.delete` is the eviction half. **Nothing
schedules a sweeper**, and nothing regenerates on read either: both are a task somebody has
to write, and ``pdf_for`` raising :class:`NoRenditionError` is what a read gets until they do.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from jmfts_core.atoms import COST_CPU, EV_BLOB, EV_CONVERSION, EV_MATCHED, EV_RENDITION
from jmfts_core.convert import (
    OOXML_TARGET,
    TARGET_PDF,
    ConversionFailed,
    convert_bytes,
    renderer_version,
)
from jmfts_core.ingest_tasks import (
    TASK_CONVERT_OOXML,
    TASK_RENDER_PDF,
    TaskOutcome,
    register_task_handler,
)
from jmfts_core.models.document import Document
from jmfts_core.models.task_queue import WRITE_SELF, TaskQueue
from jmfts_core.probe import detect_format
from jmfts_core.rendering import UnreadablePdf, page_geometry
from jmfts_core.repositories.blob import BlobRepository
from jmfts_core.repositories.evidence import EvidenceRepository
from jmfts_core.repositories.rendition import (
    EVIDENCE_NAME,
    KIND_OOXML,
    KIND_PDF,
    PDF_MIME,
    RenditionRepository,
)

logger = logging.getLogger(__name__)

#: ``detect_format``'s name for a PDF. The INGEST side's spelling of "this is already a
#: page", where :func:`~jmfts_core.repositories.rendition.pdf_for` reads the mime type: a row
#: is only evaluated once ``probe`` has written ``matched``, so on this side the format name
#: is available and is what the sentinel that scheduled the task was resolved against.
FORMAT_PDF = "pdf"

#: The evidence rows these two handlers write: what was rendered or converted, by what, into
#: how many pages of what size. Taken from
#: :data:`~jmfts_core.repositories.rendition.EVIDENCE_NAME` rather than spelled again here,
#: because the repository's ``delete`` retracts the same rows this writes and two spellings
#: would let an eviction leave one of them standing. That constant is also where the argument
#: lives for why this is not ``file.rendition_ref``, which is what ``OFFICE_SPEC.md`` Part 6
#: proposed.
#:
#: Neither row carries the large-object OID. An OID is a physical address the repository
#: maintains (``document_blobs.lob_oid`` says the same about itself), and evidence is a
#: caller-readable, caller-PATCHable record of what a run measured.
RENDITION_NAME = EVIDENCE_NAME[KIND_PDF]
CONVERSION_NAME = EVIDENCE_NAME[KIND_OOXML]

#: Where ``legacy_application`` lives in the ``matched`` block. ``probe._probe_ole2`` writes
#: it "exactly when there is a legacy binary to name", so its absence is a measurement and
#: not a gap — which is why the handler refuses rather than guessing a target.
LEGACY_APPLICATION = "legacy_application"


def _format_of(session: Session, doc: Document) -> Optional[str]:
    matched = EvidenceRepository(session).read(doc.id, "matched")
    return matched.get("format") if isinstance(matched, dict) else None


def _patterns_of(session: Session, doc: Document) -> dict:
    matched = EvidenceRepository(session).read(doc.id, "matched")
    patterns = matched.get("patterns") if isinstance(matched, dict) else None
    return patterns if isinstance(patterns, dict) else {}


def _source_bytes(session: Session, doc: Document, task_type: str) -> bytes:
    data = BlobRepository(session).read_bytes(doc.id)
    if data is None:
        raise ValueError(
            f"document {doc.id} has no stored blob; {task_type} was enqueued for bytes that "
            "are no longer there"
        )
    return data


def _scope_node(session: Session, task: TaskQueue) -> Document:
    doc = session.get(Document, task.scope_document_id)
    if doc is None:
        raise ValueError(
            f"{task.task_type} is scoped to document {task.scope_document_id}, which does "
            "not exist"
        )
    return doc


# `self`, and it is the narrowest true statement: this task writes the node's own evidence
# row and a row in a side table keyed by the node. It creates no child and touches no
# descendant, so `children` or `subtree` would reserve a region it never writes and would
# block the chunk-level work that legitimately runs beside it.
@register_task_handler(
    TASK_RENDER_PDF,
    consumes=(f"{EV_MATCHED}@self", f"{EV_BLOB}@self"),
    produces=(f"{EV_RENDITION}@self",),
    write_mode=WRITE_SELF,
    cost_class=COST_CPU,
)
def run_render_pdf(session: Session, task: TaskQueue) -> TaskOutcome:
    """Paginate this document into a stored PDF, and record what the pagination is.

    The source bytes are the UPLOADED ones, always, including for a legacy binary that
    ``convert:ooxml`` is also converting. Two reasons, and the second is the one that decides
    it: LibreOffice reads a ``.doc`` natively so the conversion buys nothing here, and both
    rows are scoped to the same node with ``self`` write mode, so the queue serialises them
    but does not ORDER them — a handler that rendered the conversion "when one exists" would
    produce a different rendition depending on which task the worker happened to claim first.
    One input, one output, no dependence on a race.
    """
    doc = _scope_node(session, task)

    fmt = _format_of(session, doc)
    if fmt == FORMAT_PDF:
        raise ValueError(
            f"document {doc.id} is already a PDF, so a rendition of it would be a copy of "
            "its own bytes. `pdf_for` serves a PDF from the blob and the `render:pdf` row "
            "has no `pdf` entry in RENDITION_SOURCE_PATTERN; this task was enqueued by "
            "something other than the table"
        )

    data = _source_bytes(session, doc, TASK_RENDER_PDF)
    rendered = convert_bytes(data, to=TARGET_PDF)

    # The converter said it produced a PDF. This is what checks that it did, and the check is
    # `detect_format` — the same magic-byte reading probe does — rather than "did pymupdf
    # open it". THAT DISTINCTION IS MEASURED, not stylistic: `pymupdf.open(stream=...,
    # filetype="pdf")` on `b"<html>no</html>"` returns a document of ONE 400x600 page rather
    # than raising, so a service answering 200 with an error page would have been stored as a
    # one-page rendition and reported as a success. A rendition that is plausible and wrong is
    # the failure Part 5 says is worse than none.
    detection = detect_format(rendered)
    if detection.format != FORMAT_PDF:
        raise ConversionFailed(
            f"the converter was asked for a PDF and returned {len(rendered)} bytes that "
            f"detect as {detection.format!r}; document {doc.id} has no rendition"
        )
    try:
        pages = page_geometry(rendered)
    except UnreadablePdf as exc:
        raise ConversionFailed(
            f"the converter returned bytes for document {doc.id} that carry the PDF "
            f"signature and will not open, so there is no rendition to store: {exc}"
        ) from exc

    version = renderer_version()
    row = RenditionRepository(session).store(
        doc.id,
        KIND_PDF,
        rendered,
        mime_type=PDF_MIME,
        renderer_version=version,
    )

    record = {
        "kind": KIND_PDF,
        "source_format": fmt,
        "renderer": "libreoffice",
        "page_count": len(pages),
        # Per page and not one size for the document: a presentation is landscape, a
        # spreadsheet paginates into whatever fits, and a caller converting a PDF rectangle
        # into a pixel box needs the page it is on rather than the first one.
        "pages": pages,
        "byte_size": row.byte_size,
        "content_hash": row.content_hash,
    }
    if version:
        # ABSENT when undeclared, never null. `probe` sets the convention at
        # `legacy_application`: an absent key means nobody measured, and unoserver cannot
        # report which LibreOffice is behind it (see `convert.renderer_version`).
        record["renderer_version"] = version
    EvidenceRepository(session).write(doc.id, RENDITION_NAME, record)
    session.flush()

    return TaskOutcome(
        detail={
            "page_count": len(pages),
            "byte_size": row.byte_size,
            # The ratio Part 12 question 1 was actually about, per document, measured rather
            # than assumed. `SPRINT_0_6_0.md` Block G answered the question from a corpus
            # total; this is what makes the per-document number readable afterwards.
            "source_bytes": len(data),
            "renderer_version": version or None,
        }
    )


@register_task_handler(
    TASK_CONVERT_OOXML,
    consumes=(f"{EV_MATCHED}@self", f"{EV_BLOB}@self"),
    produces=(f"{EV_CONVERSION}@self",),
    write_mode=WRITE_SELF,
    cost_class=COST_CPU,
)
def run_convert_ooxml(session: Session, task: TaskQueue) -> TaskOutcome:
    """Re-express a legacy binary as the OOXML package of the same application.

    The target comes from ``matched.patterns.legacy_application``, which ``probe`` writes
    from the OLE2 directory — ``WordDocument``, ``Workbook``, ``PowerPoint Document``. It is
    NOT guessed from the filename or from the container, and the refusal when it is absent is
    the point: an OLE2 file that is not one of the three is an MSI, a ``Thumbs.db`` or an
    Outlook ``.msg``, and asking LibreOffice for a ``.docx`` of one produces a failure a long
    way from the cause.
    """
    doc = _scope_node(session, task)
    fmt = _format_of(session, doc)
    application = _patterns_of(session, doc).get(LEGACY_APPLICATION)

    if not application:
        raise ValueError(
            f"document {doc.id} carries no `{LEGACY_APPLICATION}` pattern, so nothing says "
            "which application's format these bytes are in. probe writes that pattern for "
            "every legacy binary it recognises and for nothing else, so its absence means "
            "this container is not one — and converting it would be a guess"
        )
    target = OOXML_TARGET.get(application)
    if target is None:
        raise ValueError(
            f"document {doc.id} names legacy application {application!r}, which this "
            f"appliance has no OOXML target for; the three it converts are "
            f"{sorted(OOXML_TARGET)}"
        )

    data = _source_bytes(session, doc, TASK_CONVERT_OOXML)
    converted = convert_bytes(data, to=target)

    # Detected, not assumed: this both supplies the mime type to serve the package back with
    # and checks that the converter produced the format it was asked for. A `.docx` that
    # comes back as a `.odt` would otherwise be stored under a name that lies about it.
    detection = detect_format(converted)
    if detection.format != target:
        raise ConversionFailed(
            f"the converter was asked for {target!r} and returned bytes that detect as "
            f"{detection.format!r}; document {doc.id} is not converted"
        )

    version = renderer_version()
    row = RenditionRepository(session).store(
        doc.id,
        KIND_OOXML,
        converted,
        mime_type=detection.detected_mime or "application/octet-stream",
        renderer_version=version,
    )

    record = {
        "kind": KIND_OOXML,
        "to": target,
        "from_format": fmt,
        "from_application": application,
        "renderer": "libreoffice",
        "byte_size": row.byte_size,
        "content_hash": row.content_hash,
    }
    if version:
        record["renderer_version"] = version
    EvidenceRepository(session).write(doc.id, CONVERSION_NAME, record)
    session.flush()

    return TaskOutcome(
        detail={
            "to": target,
            "byte_size": row.byte_size,
            "source_bytes": len(data),
            "renderer_version": version or None,
        }
    )
