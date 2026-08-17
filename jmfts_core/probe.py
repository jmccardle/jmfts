"""probe — the cheap, model-free first task of file ingestion.

``INGEST_SPEC.md`` Part 4: ``probe`` depends on nothing, calls no model, and always runs.
Everything downstream is decided by what it writes into ``matched.patterns`` (spec 3.3).

The module has two halves, and spec 3.1 is the reason they are separate:

    "A file type is not a structure guarantee. A ``.pdf`` extension tells us how to open
    the bytes. Whether the document declares an outline, whether that outline covers all
    of the text, and whether there is a text layer at all are separate facts."

:func:`detect_format` answers *how to open the bytes* — from magic bytes, never from the
extension alone, and it records which. :func:`probe_patterns` answers *what is inside*,
and only for formats a prober exists for.

The declared type (the client's ``Content-Type``, ultimately the file extension) is never
used to overrule the bytes, and never discarded either: both go on the node's ``file``
block and a disagreement is reported in probe's attempt detail. Trusting one silently is
how a ``.pdf`` that is really a ZIP becomes an unexplained extraction failure later.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

#: (magic prefix, mime, short format name). Ordered — the first match wins, so a longer
#: signature must precede any prefix of it. Kept as a literal table rather than reaching
#: for libmagic: this is a dozen signatures, and `python-magic` needs a system library
#: that the appliance image does not carry.
_MAGIC: tuple[tuple[bytes, str, str], ...] = (
    (b"%PDF-", "application/pdf", "pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"\xff\xd8\xff", "image/jpeg", "jpeg"),
    (b"GIF87a", "image/gif", "gif"),
    (b"GIF89a", "image/gif", "gif"),
    (b"%!PS", "application/postscript", "postscript"),
    (b"{\\rtf", "application/rtf", "rtf"),
    # Legacy OLE2 compound file: .doc/.xls/.ppt before the OOXML era. Named honestly as
    # the container it is — telling the three apart needs the directory stream, and no
    # prober here can open any of them.
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "application/x-ole-storage", "ole2"),
)

#: ZIP local-file-header signatures. Every OOXML and OpenDocument file is a ZIP, so a
#: match here is refined by reading the archive's member list (`_refine_zip`).
_ZIP_MAGIC: tuple[bytes, ...] = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")

#: A member path that identifies a ZIP-container format, and what it identifies it as.
#: Order matters only in that the first hit wins; the three OOXML parts are mutually
#: exclusive in practice.
_ZIP_MEMBERS: tuple[tuple[str, str, str], ...] = (
    (
        "word/document.xml",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "docx",
    ),
    (
        "ppt/presentation.xml",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "pptx",
    ),
    (
        "xl/workbook.xml",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "xlsx",
    ),
)

#: Formats :func:`probe_patterns` can actually look inside. Everything else gets an empty
#: ``patterns`` and a detail saying so — see the note in :func:`probe_patterns`.
PROBERS_AVAILABLE: tuple[str, ...] = ("pdf", "text")

#: A PDF averaging fewer characters per page than this, while carrying images, is called
#: scanned. Named as a constant because probe writes it into its own attempt detail: a
#: threshold that decides whether OCR gets enqueued must be auditable from the log.
SCANNED_MAX_CHARS_PER_PAGE = 100

#: How far into a text file the markup check looks. A document that opens with markup is
#: markup; one that mentions a tag on page nine is prose that quotes a tag. The number is
#: a prefix length rather than a ratio for that reason, and it is generous enough to see
#: past a licence header or an XML declaration.
MARKUP_SCAN_BYTES = 4096

#: A tag, a doctype, a comment or a processing instruction. Used only on the prefix above,
#: and only when the document's first non-whitespace character is already ``<`` — the
#: opening character is what carries the claim, and the tag match is what stops a file
#: beginning with a mathematical ``<`` from being called markup on its own.
_TAG_RE = re.compile(r"<[a-zA-Z!/?][^>]*>")


@dataclass(frozen=True)
class FormatDetection:
    """What the bytes are, what the client said they were, and how we know.

    ``detected_mime``/``detected_by`` are BOTH None when nothing recognised the bytes.
    That is a real answer — "we do not know" — and it is deliberately not backfilled from
    ``declared_mime``, which would make the file block claim evidence it does not have.
    ``format`` still falls back to the filename extension in that case, because opening
    the bytes has to start somewhere and the extension is the only hint left; the file
    block's null ``detected_by`` is what says the hint was unverified.
    """

    format: str
    detected_mime: Optional[str]
    detected_by: Optional[str]
    declared_mime: Optional[str]

    @property
    def mime_agrees(self) -> Optional[bool]:
        """True/False when both types are known, None when the comparison cannot be made."""
        if self.detected_mime is None or self.declared_mime is None:
            return None
        return self.detected_mime == _normalize_mime(self.declared_mime)


def _normalize_mime(mime: str) -> str:
    """Strip parameters and case from a Content-Type: ``text/plain; charset=utf-8``."""
    return mime.split(";", 1)[0].strip().lower()


def _extension_format(filename: Optional[str]) -> str:
    """The extension, lowercased, with no dot; ``"unknown"`` when there is none."""
    if not filename or "." not in filename:
        return "unknown"
    suffix = filename.rsplit(".", 1)[1].strip().lower()
    return suffix or "unknown"


def _refine_zip(data: bytes) -> tuple[str, str, str]:
    """``(format, mime, detected_by)`` for bytes already known to be a ZIP.

    OOXML and EPUB are ZIPs with a conventional member layout, so the archive's own
    directory identifies them — still evidence from the bytes, not from the name, which
    is why ``detected_by`` says ``zip_manifest`` rather than ``magic_bytes``. A ZIP we
    cannot place stays ``application/zip``: that is what it demonstrably is.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
            # EPUB stores its type in an uncompressed `mimetype` member, by spec.
            if "mimetype" in names:
                declared = archive.read("mimetype").strip().decode("ascii", "replace")
                if declared == "application/epub+zip":
                    return ("epub", "application/epub+zip", "zip_manifest")
            for member, mime, fmt in _ZIP_MEMBERS:
                if member in names:
                    return (fmt, mime, "zip_manifest")
    except (zipfile.BadZipFile, OSError, UnicodeDecodeError):
        # The ZIP magic matched but the archive will not open — truncated upload, or
        # something that merely starts with "PK". Reporting it as a plain zip is the
        # honest answer; the pattern probe has no prober for it either way.
        logger.info("bytes carry a ZIP signature but the archive would not open")
    return ("zip", "application/zip", "magic_bytes")


def _sniff_text(data: bytes) -> Optional[tuple[str, str, str]]:
    """``(format, mime, detected_by)`` if the bytes decode as UTF-8 text, else None.

    Weaker evidence than a signature, and labelled as such: ``detected_by`` is
    ``content_sniff``. A NUL byte disqualifies it outright — no text format contains one,
    and its presence is the cheapest reliable binary marker there is.
    """
    if b"\x00" in data:
        return None
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return ("text", "text/plain", "content_sniff")


def detect_format(
    data: bytes,
    *,
    filename: Optional[str] = None,
    declared_mime: Optional[str] = None,
) -> FormatDetection:
    """Identify the bytes. Magic bytes first, archive manifest second, text sniff last."""
    for prefix, mime, fmt in _MAGIC:
        if data.startswith(prefix):
            return FormatDetection(
                format=fmt,
                detected_mime=mime,
                detected_by="magic_bytes",
                declared_mime=declared_mime,
            )

    if any(data.startswith(prefix) for prefix in _ZIP_MAGIC):
        fmt, mime, how = _refine_zip(data)
        return FormatDetection(
            format=fmt, detected_mime=mime, detected_by=how, declared_mime=declared_mime
        )

    sniffed = _sniff_text(data)
    if sniffed is not None:
        fmt, mime, how = sniffed
        return FormatDetection(
            format=fmt, detected_mime=mime, detected_by=how, declared_mime=declared_mime
        )

    # Nothing recognised it. `format` falls back to the extension so the record still says
    # how someone might open it; detected_mime/detected_by stay None so nobody mistakes
    # that for evidence.
    return FormatDetection(
        format=_extension_format(filename),
        detected_mime=None,
        detected_by=None,
        declared_mime=declared_mime,
    )


# ---------------------------------------------------------------------------
# Pattern probing
# ---------------------------------------------------------------------------


def _probe_pdf(data: bytes) -> tuple[dict, dict]:
    """``(patterns, detail)`` for a PDF. The eight fields spec 3.3 names, plus
    ``is_damaged``, and the measurements the derived ones are computed from.

    ``pymupdf`` is imported here rather than at module scope on purpose: this module is
    reached from ``IngestService``, which every test that touches the API imports, and a
    PDF-only dependency must not be able to break the import of the whole service layer.
    An ImportError raised here surfaces as a FAILED probe attempt on the node, naming the
    missing library — which is a report, where a module-level import would be a crash on
    an unrelated request.

    Cost: ``find_tables()`` dominates, and it runs on every page. Probe is specified as
    cheap and this is the one part of it that is not obviously so on a large document.
    Scanning a prefix instead was rejected — ``has_tables: false`` decides whether
    ``extract:tables`` is ever enqueued, and a false negative from an unscanned page is a
    silently wrong answer. ``elapsed_ms`` goes into the detail so the real cost is
    measurable before anyone optimises it.
    """
    import time

    import pymupdf

    started = time.monotonic()
    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        page_count = doc.page_count
        toc = doc.get_toc()
        text_chars = 0
        image_xrefs: set[int] = set()
        table_count = 0
        for page in doc:
            text_chars += len(page.get_text().strip())
            # `full=True` puts the xref first. Counting UNIQUE xrefs, not placements:
            # a logo repeated on 88 pages is one image, and `image_count` feeds the
            # per-image describe/embed tasks of spec Part 4, which run once per image.
            for image in page.get_images(full=True):
                image_xrefs.add(image[0])
            table_count += len(page.find_tables().tables)
    finally:
        doc.close()

    elapsed_ms = int((time.monotonic() - started) * 1000)
    outline_depth = max((entry[0] for entry in toc), default=0)
    chars_per_page = (text_chars / page_count) if page_count else 0.0
    # "Scanned" means: page images and no meaningful text layer. Both halves are needed —
    # a text PDF full of figures is not scanned, and an empty PDF with neither is not
    # scanned either, it is empty.
    is_scanned = bool(image_xrefs) and chars_per_page < SCANNED_MAX_CHARS_PER_PAGE
    # Bytes arrived, and none of them describe a page. pymupdf opens such a file without
    # complaint and reports page_count 0 — a truncated upload, a document whose page tree
    # is empty, and a file whose xref table did not survive transport all land here
    # looking exactly like an empty document. They are not the same thing, and the
    # difference is not recoverable from `page_count` alone, so this says only what is
    # certain: something was sent, and it does not open as pages.
    is_damaged = page_count == 0 and len(data) > 0

    patterns = {
        "has_text_layer": text_chars > 0,
        "has_outline": bool(toc),
        "outline_depth": outline_depth,
        "has_tables": table_count > 0,
        "has_images": bool(image_xrefs),
        "image_count": len(image_xrefs),
        "page_count": page_count,
        "is_scanned": is_scanned,
        "is_damaged": is_damaged,
    }
    detail = {
        # `is_damaged` is derived from these two, so both are logged: a reader of the
        # attempt record can tell "0 pages from 0 bytes" from "0 pages from 4 MB".
        "byte_length": len(data),
        # The raw measurements behind the two derived booleans, so `is_scanned` can be
        # re-derived from the log instead of trusted.
        "text_chars": text_chars,
        "chars_per_page": round(chars_per_page, 2),
        "scanned_threshold_chars_per_page": SCANNED_MAX_CHARS_PER_PAGE,
        "table_count": table_count,
        "outline_entries": len(toc),
        "elapsed_ms": elapsed_ms,
    }
    return patterns, detail


def _probe_text(data: bytes) -> tuple[dict, dict]:
    """``(patterns, detail)`` for a UTF-8 text file. ``INGEST_SPEC.md`` 11.3.

    Three facts, and each decides a Part 4 row:

    ``has_text_layer`` — the bytes decode and carry something other than whitespace. The
    name is PDF's, and it means the same thing here: there is text to extract. A file that
    decodes to nothing but blank lines is not a failure and is not a document either.

    ``has_headings`` — the file carries ATX structure, which is 3.5's DECLARED rung for
    markdown: the author typed those markers, so they are the document stating its own
    shape rather than a heuristic guessing at it. Counted with
    :func:`~jmfts_core.structural_splitting.find_headings`, the same function the splitter
    uses, so ``heading_count`` here and the titled sections the structure task produces are
    comparable numbers rather than two independent guesses.

    ``has_markup`` — the document IS markup: HTML, XML, an RDF dump. It holds ``extract:text``
    back, and that is the whole reason it is measured. The text extractor is a DECODER, and
    a decoder applied to HTML produces HTML — which would then be chunked with its tags
    intact and settle looking exactly like a success. 11.3 names that hazard specifically:
    this step turns loud failures into silent ones, and a pattern that keeps markup out of
    the prose path is the guard. When a real HTML converter lands it arrives as a different
    extraction source and this row's condition changes with it; until then, an HTML file
    gets ``probe`` and a stated reason, which is the answer this appliance actually has.

    Measured over 292 files: no ``.md`` or ``.txt`` file was called markup, 18 of 19
    ``.html`` files were (the nineteenth is empty), and so were 11 files with a ``.pdf``
    extension that are really saved web pages — which the magic-byte check had already
    declined to call PDFs.

    Decoding is strict and a failure is allowed to raise. ``UnicodeDecodeError`` is a
    ``ValueError``, so :func:`~jmfts_core.task_errors.classify_exception` calls it
    PERMANENT and the node records the failure once instead of retrying bytes that will
    not decode on the third attempt either.
    """
    from jmfts_core.structural_splitting import find_headings

    text = data.decode("utf-8")
    stripped = text.strip()
    headings = find_headings(text)

    prefix = text[:MARKUP_SCAN_BYTES]
    tag_count = len(_TAG_RE.findall(prefix))
    has_markup = stripped.startswith("<") and tag_count > 0

    patterns = {
        "has_text_layer": bool(stripped),
        "has_headings": bool(headings),
        "heading_count": len(headings),
        "max_heading_level": max((level for level, _ in headings), default=0),
        "has_markup": has_markup,
        "char_count": len(text),
        "line_count": text.count("\n") + 1,
    }
    detail = {
        "byte_length": len(data),
        # The raw measurements behind the two derived booleans, so `has_text_layer` and
        # `has_markup` can be re-derived from the log rather than trusted.
        "char_count": len(text),
        "nonblank_chars": len(stripped),
        "markup_tags_in_prefix": tag_count,
        "markup_scan_bytes": MARKUP_SCAN_BYTES,
        "heading_count": len(headings),
        "heading_levels": sorted({level for level, _ in headings}),
    }
    return patterns, detail


def probe_patterns(data: bytes, detection: FormatDetection) -> tuple[dict, dict]:
    """``(patterns, detail)`` — the content patterns for a detected format.

    A format with no prober returns EMPTY patterns and a detail naming it. That keeps the
    probe attempt ``completed`` rather than ``skipped``, and spec 3.4 is explicit about
    why: ``skipped`` means the task was never attempted. probe *was* attempted — it ran,
    it identified the format, it wrote ``matched.format``. What it could not do is look
    inside a ``.docx``, because that prober arrives with phasing step 6. Recording that
    as ``skipped`` would claim probe never ran on this file, and the scheduler would have
    no record that the format was already identified.
    """
    if detection.format == "pdf":
        return _probe_pdf(data)
    if detection.format == "text":
        return _probe_text(data)
    return (
        {},
        {
            "no_prober_for_format": detection.format,
            "probers_available": list(PROBERS_AVAILABLE),
        },
    )
