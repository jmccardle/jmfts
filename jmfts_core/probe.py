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
import json
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Optional

# Imported at module scope, unlike `pymupdf` and the office readers, and the difference is
# the point of `OFFICE_SPEC.md` Part 1's tier 1: `olefile` is a single pure-Python module
# declared in the BASE dependencies, so it is present wherever probe is, and hiding it
# inside a function would imply a fallback for an absence that cannot happen. The tier-2
# readers (`python-docx`, `python-pptx`, `openpyxl`) are the ones that must never appear
# on this module's import path; `tests/test_office_packaging.py` asserts exactly that.
import olefile

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
#:
#: ``epub`` and ``zip`` are the ZIP containers still without one: EPUB declares its outline
#: in an ``.ncx`` or a nav document, which no prober here reads yet, and a plain ZIP
#: declares nothing at all. Both therefore keep taking the empty-pattern path below.
PROBERS_AVAILABLE: tuple[str, ...] = ("pdf", "text", "docx", "pptx", "xlsx", "ole2")

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

#: An HTML comment. Removed BEFORE the opening-character test, because a comment carries
#: no content and therefore cannot make a document be markup.
#:
#: Markdown has no comment syntax of its own, so `<!-- ... -->` is how markdown files
#: carry the things that have to sit above the first heading: `<!-- markdownlint-disable
#: MD013 -->`, `<!-- prettier-ignore -->`, an SPDX licence header. Without this strip such
#: a file opens with `<`, is called markup, and `extract:text` is held back — the document
#: then settles with ZERO chunks while every task reports "completed". Measured: 5 of 5
#: realistic leading-comment markdown files were misclassified before this, 0 after, with
#: no change to the 18 real `.html` files or the 98 `.md`/`.txt` files either way.
_COMMENT_RE = re.compile(r"<!--[\s\S]*?-->")


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

    THIS DOES NOT LOOK FOR TABLES, and the ``pages_with_tables`` pattern spec Part 4's
    ``extract:tables`` row reads is reported by ``extract:text`` instead
    (``jmfts_core.pdf_extraction.pdf_to_markdown``). Probe used to call
    ``page.find_tables()`` on every page, and so did ``_process_page`` — the same scan,
    twice per document, of the most expensive thing in PDF ingest. Only one of the two can
    be removed: ``_table_owner`` needs table geometry WHILE the prose is being written, to
    decide which text blocks are a table's content and must not be emitted twice, so
    extraction cannot defer the scan and probe is the copy that goes.

    The cost that removed, measured over the 124-file corpus behind
    ``docs/research/INTERMEDIATE_FORMATS.md`` (123 probed; ``core_fs.pdf`` is not a PDF):
    the scan was **96.5%** of this function's total wall clock, 318.6 s of 330.2 s. Per
    file the median probe went from 1.641 s to 0.052 s and the worst case
    (``kr89-proceedings.pdf``, 19 MB) from 64.1 s to 3.8 s. That is the difference between
    probe being what Part 4 calls it — cheap, model-free, always runs — and probe being
    the reason ingesting a corpus is slow.

    End to end, ``probe`` plus ``extract:text`` over that corpus went from 664.5 s to
    345.9 s — **48.0%, a 1.92x speedup** — and the shape of those numbers is the whole
    argument: extraction alone costs 334.3 s and probe-with-the-scan cost 330.2 s. Two
    nearly equal halves, because they were the same work done twice.
    ``INTERMEDIATE_FORMATS.md`` recorded the same gap as "true of the design and currently
    false of the PDF implementation"; it is now true of both.

    It also makes ``ANALYZE`` (11.2) affordable in the way that section claims, since
    ``analyze_ingest`` runs exactly this function and stops.

    What moved is not just cheaper, it is more correct. Probe counted raw ``find_tables()``
    candidates, and the ``lines`` strategy reports any ruled grid — on the attention paper
    that meant three pages of attention-visualisation figures were reported as pages with
    tables. ``pdf_to_markdown`` counts the tables it actually rendered, after the structural
    filters, so the same paper now reports the two pages that really carry one.

    ``elapsed_ms`` stays in the detail. It is what made this measurable, and it is what
    would show the next regression.
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
        for page in doc:
            text_chars += len(page.get_text().strip())
            # `full=True` puts the xref first. Counting UNIQUE xrefs, not placements:
            # a logo repeated on 88 pages is one image, and `image_count` feeds the
            # per-image describe/embed tasks of spec Part 4, which run once per image.
            for image in page.get_images(full=True):
                image_xrefs.add(image[0])
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
        # `pages_with_tables` IS NOT HERE, and its absence is the point of this function's
        # docstring. It is measured by `extract:text` and lands in that task's
        # `extraction` record, because the pass that renders tables has to find them
        # anyway. Nothing is substituted for it and no key is left holding a default: a
        # pattern probe did not measure must not appear in the pattern set at all, or
        # `plan_after_probe` would answer "this document has no tables" when the truth is
        # "probe did not look" — the same distinction 11.2 makes for `probe_failed`.
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
        "outline_entries": len(toc),
        "elapsed_ms": elapsed_ms,
    }
    return patterns, detail


#: How many leading lines :func:`_conversation_turns` will parse. A conversation is
#: decided by its FIRST message line; the rest are counted so the detail can say how much
#: of the prefix agreed, which is what separates a transcript from a one-line JSON file
#: that happens to carry the same keys.
CONVERSATION_SCAN_LINES = 64


def _is_message_object(obj: object) -> bool:
    """Does this decoded JSON line carry the keys a conversation message carries?

    The two shapes :func:`~jmfts_core.conversation_ingest.parse_adjutant_jsonl` accepts,
    and nothing else: an adjutant prompt/response pair, or a pre-structured
    ``{role, content}`` message. Kept as a predicate over the DECODED object rather than a
    regex over the line, because "is this a message" is a question about the keys and a
    pattern that answered it from the bytes would be a second, weaker parser.
    """
    if not isinstance(obj, dict):
        return False
    return "prompt" in obj or ("role" in obj and "content" in obj)


def _conversation_turns(text: str) -> tuple[bool, int, int]:
    """``(first line is a message, message lines found, non-blank lines scanned)``.

    TIER 1, and it has to be: ``json`` is in the standard library and ``probe`` may depend
    on nothing else (``OFFICE_SPEC.md`` Part 1). A base install that could not recognise a
    conversation would accept one, probe it, and report a pattern set with no
    ``is_conversation`` in it — which is indistinguishable from a text file that is not one.
    """
    scanned = 0
    matched = 0
    first = False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            obj = None
        if _is_message_object(obj):
            matched += 1
            if scanned == 0:
                first = True
        scanned += 1
        if scanned >= CONVERSATION_SCAN_LINES:
            break
    return first, matched, scanned


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

    ``is_conversation`` — the file is a transcript: its first non-blank line decodes as a
    JSON object carrying the keys a message carries. ``SPRINT_JOBS.md`` 15.2 decision 3
    made this a probed FORMAT PATTERN rather than a usetype a caller declares, which is
    what gives conversations the attempt log, the retry classification and ``EXPLAIN``
    every other input already has. It selects a reader in ``extract:text`` and a rung in
    Part 4's table, and it holds the two prose rungs back — a transcript split on ATX
    headings or packed into sentences would lose the turn boundaries the file states.

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

    # Comments are stripped before BOTH tests. A file whose only leading `<` is a comment
    # is not markup, and a comment's contents are not tags — counting them would let a
    # three-line licence header out-vote the document it sits above.
    uncommented = _COMMENT_RE.sub("", text)
    prefix = uncommented[:MARKUP_SCAN_BYTES]
    tag_count = len(_TAG_RE.findall(prefix))
    has_markup = uncommented.strip().startswith("<") and tag_count > 0

    is_conversation, message_lines, scanned_lines = _conversation_turns(text)

    patterns = {
        "has_text_layer": bool(stripped),
        "has_headings": bool(headings),
        "heading_count": len(headings),
        "max_heading_level": max((level for level, _ in headings), default=0),
        "has_markup": has_markup,
        "is_conversation": is_conversation,
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
        "conversation_message_lines": message_lines,
        "conversation_lines_scanned": scanned_lines,
    }
    return patterns, detail


# ---------------------------------------------------------------------------
# Office formats — OFFICE_SPEC.md Part 2
#
# TIER 1 ONLY, and that is a hard constraint rather than a preference. OFFICE_SPEC.md
# Part 1: probe "depends on nothing, calls no model, and always runs", so if reading a
# `.docx` needed the `office` extra then a base install would accept the upload, run
# probe, and report an EMPTY pattern set — indistinguishable from a `.docx` that
# genuinely declares no structure. Everything below is `zipfile`, `xml.etree` and
# `olefile`. `python-docx`, `python-pptx` and `openpyxl` must not appear in this file.
# ---------------------------------------------------------------------------

#: The wordprocessingml namespace. Fixed by ECMA-376, so a document may vary the PREFIX
#: (``w:``, or a default namespace, or anything else) but never this URI — which is why
#: the parts small enough to parse properly are matched on the URI and only the streaming
#: scan below has to tolerate an arbitrary prefix.
_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

#: The spreadsheetml namespace, for the same reason.
_S_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"

#: How much of a large part is read at a time. `word/document.xml` is the one part of an
#: office package that is routinely megabytes, and probe is the task Part 4 calls cheap —
#: so it is scanned as a stream out of `ZipFile.open`, never decompressed whole into a
#: `bytes`. 64 KiB is the usual compromise: large enough that the per-read overhead
#: disappears, small enough that a 200 MB `document.xml` never costs more than this much
#: resident memory.
OOXML_SCAN_CHUNK_BYTES = 65536

#: How much of the previous chunk is prepended to the next one, so an element that straddles
#: a chunk boundary is still matched. It has to exceed the longest thing the scan looks for.
#: The longest is ``<w:pStyle w:val="..."/>``, whose value is an ``ST_String`` and therefore
#: bounded by ECMA-376 at 255 characters; 4 KiB clears that by an order of magnitude and
#: still costs 6% of a chunk. Matches inside the overlap are seen twice on purpose — every
#: scan result below is a SET or a boolean, never a count, so a duplicate changes nothing.
OOXML_SCAN_OVERLAP_BYTES = 4096

#: ``w:outlineLvl`` values that mean "paragraphs in this style are part of the outline".
#: ECMA-376 allows 0-9 and reserves 9 for body text, so a style carrying 9 is declaring
#: that it is NOT a heading. Treating the attribute's mere presence as the signal would
#: call Normal a heading in every document that sets the level explicitly.
OUTLINE_HEADING_LEVELS: frozenset[int] = frozenset(range(9))

#: A built-in heading style, matched against ``w:name`` and against ``w:styleId``.
#:
#: ``w:name`` is the style's INVARIANT name: Word stores ``heading 1`` there whatever the
#: UI language, and localizes only for display. ``w:styleId`` is what the document's own
#: markup references and is free-form — ``Heading1`` from Word, ``berschrift1`` from a
#: German original, ``Titre1`` from a French one. Neither is authoritative, which is why
#: this is the SECOND and THIRD test in :func:`_docx_heading_styles` and ``w:outlineLvl``
#: is the first: matching the literal string "Heading 1" would miss every renamed style
#: and every non-English document, and that is exactly the failure OFFICE_SPEC.md Part 2
#: names when it says to measure this through ``word/styles.xml``.
_HEADING_STYLE_NAME_RE = re.compile(r"^heading\s*[1-9]$", re.IGNORECASE)

#: What the scan of ``word/document.xml`` looks for, and what each match contributes.
#: Every pattern has exactly one capturing group, whose text goes into a set — so the
#: detail can report WHICH style ids and WHICH revision elements were seen rather than
#: only that something was.
#:
#: ``(?:\w+:)?`` in front of every local name is the prefix tolerance the streaming scan
#: needs: a byte scan cannot resolve namespaces, and a producer that is not Word may bind
#: wordprocessingml to a different prefix or to the default namespace.
_DOCX_SCAN: dict[str, re.Pattern[bytes]] = {
    # The style a paragraph references. Resolved against `word/styles.xml` afterwards —
    # this only collects the ids, it does not decide which of them are headings.
    "pstyle": re.compile(rb'<(?:\w+:)?pStyle[^>]*?\s(?:\w+:)?val="([^"]*)"'),
    # `w:ins` / `w:del`: an insertion or a deletion that has not been accepted. The
    # trailing character class is what keeps `w:delText`, `w:delInstrText` and `w:insideH`
    # out — all three continue past the three letters this is looking for.
    "revision": re.compile(rb"<(?:\w+:)?(ins|del)[\s/>]"),
    # `w:tbl` is the table itself; `w:tblPr` and `w:tblGrid` are its children and are
    # excluded by the same trailing character class.
    "table": re.compile(rb"<(?:\w+:)?(tbl)[\s>]"),
}

#: The same idea for a slide part. `a:tbl` is drawingml's table, which is how a PowerPoint
#: table is stored — inside a `p:graphicFrame`, but the frame also wraps charts and
#: SmartArt, so the table element is the specific marker.
_PPTX_SLIDE_SCAN: dict[str, re.Pattern[bytes]] = {
    "table": re.compile(rb"<(?:\w+:)?(tbl)[\s>]"),
}

#: How many unplaced part names are carried in the pattern set. OFFICE_SPEC.md Part 10
#: wants to know WHICH parts real packages carry that this appliance does not place, but
#: `matched.patterns` is JSONB on every file node and a pathological package could carry
#: thousands. The full count travels beside the list, so a truncated list is visibly
#: truncated rather than quietly short.
UNKNOWN_PARTS_REPORTED_MAX = 32

#: Parts every OOXML package carries regardless of which application wrote it: the content
#: types stream, the relationship parts, the core/app/custom properties, and the custom-XML
#: store. Matched as exact names or as directory prefixes.
_OOXML_COMMON_PARTS: tuple[str, ...] = (
    "[Content_Types].xml",
    "_rels/",
    "docProps/",
    "customXml/",
)


@dataclass(frozen=True)
class _OoxmlLayout:
    """Where one OOXML format keeps the things every OOXML format has.

    The three formats differ only in the name of their tree, so the parts that are common
    to all of them — the macro project, the media store — are derived from ``root`` rather
    than written out three times and left to drift.
    """

    #: The package directory: ``word``, ``ppt``, ``xl``.
    root: str
    #: The part `detect_format` used to identify the format, and the part the scan reads.
    main_part: str

    @property
    def media_prefix(self) -> str:
        """Where embedded images live. ``has_images`` is this prefix having members."""
        return f"{self.root}/media/"

    @property
    def macro_part(self) -> str:
        """The VBA project. Its presence IS ``has_macros`` — we never execute it, and
        OFFICE_SPEC.md Part 2 records it because the operator and the corpus both want to
        know which documents carry code, not because it is a hazard to this appliance."""
        return f"{self.root}/vbaProject.bin"


_OOXML_LAYOUTS: dict[str, _OoxmlLayout] = {
    "docx": _OoxmlLayout(root="word", main_part="word/document.xml"),
    "pptx": _OoxmlLayout(root="ppt", main_part="ppt/presentation.xml"),
    "xlsx": _OoxmlLayout(root="xl", main_part="xl/workbook.xml"),
}

#: The docx parts read by name rather than found by prefix.
_DOCX_STYLES_PART = "word/styles.xml"
_DOCX_COMMENTS_PART = "word/comments.xml"

#: Slide, notes and diagram trees of a presentation. `ppt/diagrams/` is SmartArt, and it is
#: measured because `python-pptx` cannot read it: the honest report is that content exists
#: which extraction will not carry, rather than a deck that silently loses a diagram.
_PPTX_SLIDE_PREFIX = "ppt/slides/"
_PPTX_NOTES_PREFIX = "ppt/notesSlides/"
_PPTX_DIAGRAM_PREFIX = "ppt/diagrams/"

#: A slide or notes-slide part, as opposed to the `_rels` and `.xml.rels` files that sit
#: beside them in the same directory.
_SLIDE_PART_RE = re.compile(r"^ppt/(?:slides|notesSlides)/[^/]+\.xml$")

#: Where a worksheet's comments live. Excel has two unrelated mechanisms and a workbook may
#: use either: `xl/comments1.xml` is the classic cell note, `xl/threadedComments/` is the
#: modern threaded comment. Reporting only one would call a commented workbook uncommented.
_XLSX_COMMENTS_RE = re.compile(r"^xl/(?:comments\d*\.xml|threadedComments/[^/]+\.xml)$")

#: Drawingml text runs, and the generated fields that must not count as authored text.
#: A notes slide almost always carries `<a:fld type="slidenum"><a:t>7</a:t></a:fld>`, so a
#: naive "does this part contain any text" test reports speaker notes on every deck that
#: has none. Fields are removed first; whatever `a:t` survives was typed by a human.
_DRAWINGML_FIELD_RE = re.compile(rb"<(?:\w+:)?fld[\s>][\s\S]*?</(?:\w+:)?fld>")
_DRAWINGML_TEXT_RE = re.compile(rb"<(?:\w+:)?t[\s>]([^<]*)</(?:\w+:)?t>")


class EncryptedPackageError(ValueError):
    """These bytes are an encrypted OOXML package, so nothing can be measured or read.

    A ``ValueError``, deliberately: :func:`~jmfts_core.task_errors.classify_exception`
    calls that PERMANENT, and a document that needs a password this appliance has no way
    to accept will not decrypt on the third attempt either.

    This is the one probe result of OFFICE_SPEC.md Part 2 that CHANGES behaviour instead
    of recording it. An encrypted package makes probe fail with a named reason; it does
    not extract to empty and settle. `msoffcrypto-tool` is deliberately not carried —
    detecting encryption is the requirement, decrypting is not, and there is no path by
    which a caller could supply a password.
    """


#: The two streams that make an OLE2 container an encrypted OOXML package rather than a
#: legacy binary. MS-OFFCRYPTO defines both: ``EncryptionInfo`` holds the algorithm and the
#: password verifier, ``EncryptedPackage`` holds the ciphertext of the ``.docx``/``.xlsx``/
#: ``.pptx`` ZIP. BOTH are required here — a container with only one of them is malformed,
#: and calling it encrypted on the strength of half the pair would permanently fail an
#: upload the converter might well have read.
ENCRYPTED_PACKAGE_STREAMS: frozenset[str] = frozenset({"EncryptionInfo", "EncryptedPackage"})

#: Root stream name -> the application whose pre-OOXML binary format it names. This is the
#: only thing in the container that says WHICH of the three a `.doc`-shaped file is, and it
#: is what `convert:ooxml` needs in order to hand LibreOffice the right filter.
#:
#: ``Book`` is Excel 5.0/95; ``Workbook`` is Excel 97 and later. Both are listed because a
#: 1995 spreadsheet is exactly the kind of file that arrives in an archive import, and it
#: is a legacy binary by every measure that matters here.
LEGACY_BINARY_STREAMS: dict[str, str] = {
    "WordDocument": "word",
    "Workbook": "excel",
    "Book": "excel",
    "PowerPoint Document": "powerpoint",
}


def _open_package(data: bytes, fmt: str) -> zipfile.ZipFile:
    """The package, or a PERMANENT failure naming what could not be opened.

    ``detect_format`` already read this archive's directory to decide the format, so a
    failure here means the bytes changed under us or the central directory describes
    members the local headers do not. Either way nothing inside can be measured, and
    saying so is the answer — ``zipfile.BadZipFile`` derives straight from ``Exception``
    and would otherwise be classified RETRYABLE, which would spend the retry budget
    re-opening an archive that is not going to open.
    """
    try:
        return zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError(
            f"the ZIP manifest identified these bytes as {fmt}, but the archive will not "
            f"reopen, so nothing inside it can be measured: {exc}"
        ) from exc


def _iter_chunks(handle) -> Iterator[tuple[bytes, int]]:
    """``(chunk, fresh_bytes)`` over a member, each chunk carrying the previous one's tail.

    ``fresh_bytes`` is the length of the newly read block, NOT of the yielded chunk, so a
    caller summing it gets the size of the part rather than the size plus the overlaps.
    """
    tail = b""
    while True:
        block = handle.read(OOXML_SCAN_CHUNK_BYTES)
        if not block:
            return
        yield tail + block, len(block)
        tail = block[-OOXML_SCAN_OVERLAP_BYTES:]


def _scan_member(
    archive: zipfile.ZipFile, name: str, needles: dict[str, re.Pattern[bytes]]
) -> tuple[dict[str, set[str]], int]:
    """``(matches, bytes_scanned)`` — every capture of every needle, as decoded sets.

    One pass, streamed. The point of this function is that `word/document.xml` is read
    once and never held whole: a 40 MB one costs `OOXML_SCAN_CHUNK_BYTES` of memory here,
    against 40 MB for `archive.read()`.

    Captures are decoded with ``replace`` rather than strictly. A style id that is not
    valid UTF-8 is a broken document, but it is not a reason to fail a probe that has
    already answered every other question about the file, and the mangled id will simply
    not match anything in `word/styles.xml`.
    """
    found: dict[str, set[str]] = {key: set() for key in needles}
    scanned = 0
    with archive.open(name) as handle:
        for chunk, fresh in _iter_chunks(handle):
            scanned += fresh
            for key, pattern in needles.items():
                for match in pattern.finditer(chunk):
                    found[key].add(match.group(1).decode("utf-8", "replace"))
    return found, scanned


def _unknown_parts(names: list[str], layout: _OoxmlLayout) -> list[str]:
    """Members that sit outside the package layout this format defines.

    The granularity is the package TREE, not the individual part: anything under
    ``word/``/``ppt/``/``xl/``, under ``_rels/``, ``docProps/`` or ``customXml/``, or named
    ``[Content_Types].xml`` is placed. What comes back is the genuinely foreign material —
    an OpenDocument ``mimetype`` left in a converted file, a ``META-INF/`` directory, a
    stray file somebody added to the archive with a zip tool.

    Naming a full part inventory instead would make ``unknown_parts`` a synonym for "parts
    this version of the code has not enumerated yet", which grows every time Microsoft ships
    a part and tells the corpus nothing about the document.
    """
    known = _OOXML_COMMON_PARTS + (f"{layout.root}/",)
    unknown = [
        name
        for name in names
        # A directory entry, which carries no content and is not a part.
        if not name.endswith("/")
        and not any(name == entry or name.startswith(entry) for entry in known)
    ]
    return sorted(unknown)


def _ooxml_common(names: list[str], layout: _OoxmlLayout) -> tuple[dict, dict]:
    """``(patterns, detail)`` for the facts OFFICE_SPEC.md Part 2 marks "all" OOXML.

    Everything here is decided from the member list alone — no part is opened — which is
    why it is shared by all three probers and costs nothing.
    """
    name_set = set(names)
    media = [name for name in names if name.startswith(layout.media_prefix)]
    unknown = _unknown_parts(names, layout)

    patterns = {
        "has_macros": layout.macro_part in name_set,
        "has_images": bool(media),
        "image_count": len(media),
        "part_count": len(names),
        "unknown_parts": unknown[:UNKNOWN_PARTS_REPORTED_MAX],
        "unknown_part_count": len(unknown),
    }
    detail = {
        "package_root": layout.root,
        "macro_part": layout.macro_part,
        "media_prefix": layout.media_prefix,
        "unknown_parts_reported_max": UNKNOWN_PARTS_REPORTED_MAX,
    }
    return patterns, detail


def _docx_heading_styles(archive: zipfile.ZipFile) -> dict[str, str]:
    """``style id -> how it was recognised``, for every outline heading style declared.

    Parsed in full, unlike ``word/document.xml``, and the asymmetry is deliberate:
    ``word/styles.xml`` is a bounded table of style definitions — tens of kilobytes even in
    a large document — so it can be read namespace-correctly, which is what lets the three
    tests below be applied in order of how much they actually prove.

    The order IS the provenance, strongest first, and it is what the returned value records:

    ``outline_level``
        The style carries ``w:pPr/w:outlineLvl``. This is the document stating, in the one
        place ECMA-376 provides for it, that paragraphs in this style belong to the outline.
        It holds for a style called ``ChapterOpener`` as surely as for ``Heading1``.
    ``builtin_name``
        The style's invariant ``w:name`` is ``heading N``. Word writes that name for its
        built-in heading styles whatever the interface language, so this catches a
        localized document whose style ids are in German or French.
    ``style_id``
        Only the id looks like a heading. The weakest of the three, kept because a
        stripped-down producer may emit ``Heading1`` and nothing else.

    A style with none of the three is not a heading, and a document referencing only such
    styles reports ``has_heading_styles`` false — which is a measurement, not a guess.
    """
    root = ET.fromstring(archive.read(_DOCX_STYLES_PART))
    found: dict[str, str] = {}
    for style in root.findall(f"{{{_W_NS}}}style"):
        style_id = style.get(f"{{{_W_NS}}}styleId")
        if not style_id:
            continue
        # `w:type` defaults to paragraph when absent. A character or table style with a
        # heading-shaped name declares no outline, and `w:pStyle` cannot reference one.
        style_type = style.get(f"{{{_W_NS}}}type")
        if style_type is not None and style_type != "paragraph":
            continue

        outline = style.find(f"{{{_W_NS}}}pPr/{{{_W_NS}}}outlineLvl")
        if outline is not None and _outline_level(outline.get(f"{{{_W_NS}}}val")) is not None:
            found[style_id] = "outline_level"
            continue

        name = style.find(f"{{{_W_NS}}}name")
        declared = (name.get(f"{{{_W_NS}}}val") or "") if name is not None else ""
        if _HEADING_STYLE_NAME_RE.match(declared.strip()):
            found[style_id] = "builtin_name"
            continue

        if _HEADING_STYLE_NAME_RE.match(style_id):
            found[style_id] = "style_id"
    return found


def _outline_level(value: Optional[str]) -> Optional[int]:
    """The outline level a ``w:outlineLvl`` declares, or None when it declares none.

    None covers three different bad answers with one honest one: a missing attribute, a
    value that is not a number, and the reserved body-text level. All three mean the same
    thing to the caller — this style does not put its paragraphs in the outline.
    """
    if value is None:
        return None
    try:
        level = int(value)
    except ValueError:
        return None
    return level if level in OUTLINE_HEADING_LEVELS else None


def _count_elements(archive: zipfile.ZipFile, name: str, namespace: str, tag: str) -> int:
    """How many ``{namespace}tag`` elements a small part contains, at any depth.

    For the comment parts only. They are bounded by how much a human typed, so they are
    parsed properly rather than scanned — which is what makes the count trustworthy enough
    to distinguish a part that exists from a part that has something in it.
    """
    root = ET.fromstring(archive.read(name))
    return sum(1 for _ in root.iter(f"{{{namespace}}}{tag}"))


def _probe_docx(data: bytes) -> tuple[dict, dict]:
    """``(patterns, detail)`` for a WordprocessingML package. OFFICE_SPEC.md Part 2.

    The pattern the scheduler is already waiting for is ``has_heading_styles``
    (``ingest_tasks.DECLARED_STRUCTURE_PATTERN``), and it takes two parts to answer:
    ``word/styles.xml`` says which style ids are outline headings, ``word/document.xml``
    says which of them any paragraph actually uses. A style declared and never used is not
    a declared outline, which is why the intersection is what decides.

    A PATTERN THIS CANNOT MEASURE IS OMITTED, NOT DEFAULTED. If ``word/styles.xml`` is
    missing there is no way to resolve a style id, so ``has_heading_styles`` does not
    appear in the pattern set at all and ``detail.unmeasured`` says why. ``plan_after_probe``
    then reports "patterns.has_heading_styles was not measured" instead of "this document
    declares no outline" — the same distinction ``_probe_pdf`` draws for
    ``pages_with_tables``, and the reason both are absences rather than falses.
    """
    layout = _OOXML_LAYOUTS["docx"]
    unmeasured: dict[str, str] = {}
    with _open_package(data, "docx") as archive:
        names = archive.namelist()
        patterns, detail = _ooxml_common(names, layout)
        name_set = set(names)

        heading_styles: dict[str, str] = {}
        if _DOCX_STYLES_PART in name_set:
            heading_styles = _docx_heading_styles(archive)
        else:
            unmeasured["has_heading_styles"] = (
                f"{_DOCX_STYLES_PART} is not in the package, so a style id referenced by "
                "the document cannot be resolved to an outline level"
            )

        if layout.main_part in name_set:
            scanned, scanned_bytes = _scan_member(archive, layout.main_part, _DOCX_SCAN)
            used = sorted(set(scanned["pstyle"]) & set(heading_styles))
            if _DOCX_STYLES_PART in name_set:
                patterns["has_heading_styles"] = bool(used)
            patterns["has_tracked_changes"] = bool(scanned["revision"])
            patterns["has_tables"] = bool(scanned["table"])
            # What `extract:text` is gated on (ingest_tasks.py). Measured rather than
            # assumed true for the format: a body with no text run extracts to an empty
            # string, and a row that ran anyway would produce a file node holding nothing
            # with no record of why.
            patterns["has_text_layer"] = _has_streamed_text(archive, layout.main_part)
            detail["heading_styles"] = heading_styles
            detail["heading_styles_used"] = used
            # The full set of referenced ids, so a document whose headings were NOT
            # recognised can be diagnosed from the log without re-opening the file.
            detail["pstyle_ids"] = sorted(scanned["pstyle"])[:UNKNOWN_PARTS_REPORTED_MAX]
            detail["revision_elements"] = sorted(scanned["revision"])
            detail["document_bytes_scanned"] = scanned_bytes
            detail["scan_chunk_bytes"] = OOXML_SCAN_CHUNK_BYTES
        else:
            # `detect_format` reports `docx` BECAUSE this member is in the manifest, so
            # reaching this means the archive's directory disagrees with itself.
            reason = (
                f"{layout.main_part} is named in the ZIP manifest but is not readable from "
                "the archive, so the document body was never scanned"
            )
            for pattern in (
                "has_heading_styles",
                "has_tracked_changes",
                "has_tables",
                "has_text_layer",
            ):
                unmeasured[pattern] = reason

        if _DOCX_COMMENTS_PART in name_set:
            comment_count = _count_elements(archive, _DOCX_COMMENTS_PART, _W_NS, "comment")
        else:
            # No comments part is a definite answer: Word does not write one for a document
            # with no comments. This is measured absence, not unmeasured.
            comment_count = 0
        patterns["has_comments"] = comment_count > 0
        detail["comment_count"] = comment_count

    detail["byte_length"] = len(data)
    if unmeasured:
        detail["unmeasured"] = unmeasured
    return patterns, detail


def _probe_pptx(data: bytes) -> tuple[dict, dict]:
    """``(patterns, detail)`` for a PresentationML package. OFFICE_SPEC.md Part 2.

    ``has_slides`` is what the scheduler reads, and it is the member list — a deck's
    declared structure IS its slide sequence, so no part has to be opened to answer it.

    ``has_speaker_notes`` is the one that needs care. A notes part exists on most slides
    whether or not anybody typed in it, and it always carries the generated slide-number
    field, so "the part contains text" reports notes on every deck. The fields are removed
    before the text runs are collected; what is left was typed by a person.

    ``has_smartart`` is measured because the reader that arrives in phasing step 5 cannot
    read it. `python-pptx` has no diagram support, so a deck with ``ppt/diagrams/`` will
    extract with the diagram's text missing and no error — the pattern is how the record
    says that content exists which extraction will not carry.
    """
    layout = _OOXML_LAYOUTS["pptx"]
    with _open_package(data, "pptx") as archive:
        names = archive.namelist()
        patterns, detail = _ooxml_common(names, layout)

        slides = sorted(
            name
            for name in names
            if name.startswith(_PPTX_SLIDE_PREFIX) and _SLIDE_PART_RE.match(name)
        )
        notes = sorted(
            name
            for name in names
            if name.startswith(_PPTX_NOTES_PREFIX) and _SLIDE_PART_RE.match(name)
        )
        diagrams = [name for name in names if name.startswith(_PPTX_DIAGRAM_PREFIX)]

        tables = False
        scanned_bytes = 0
        for slide in slides:
            found, part_bytes = _scan_member(archive, slide, _PPTX_SLIDE_SCAN)
            scanned_bytes += part_bytes
            tables = tables or bool(found["table"])

        notes_with_text = [name for name in notes if _has_authored_text(archive, name)]

        patterns["has_slides"] = bool(slides)
        patterns["slide_count"] = len(slides)
        # What `extract:text` is gated on. A deck of images and no text runs extracts to
        # nothing, and this is what lets that be a decision rather than an empty result.
        # `_has_authored_text` rather than the raw scan, so a deck whose only "text" is the
        # slide-number field does not claim a text layer.
        patterns["has_text_layer"] = any(_has_authored_text(archive, slide) for slide in slides)
        patterns["has_tables"] = tables
        patterns["has_smartart"] = bool(diagrams)
        patterns["has_speaker_notes"] = bool(notes_with_text)

        detail["slide_parts"] = len(slides)
        detail["notes_parts"] = len(notes)
        # Both numbers, because their DIFFERENCE is the whole justification for opening the
        # notes parts: a deck with 30 notes parts and 0 carrying text is the normal case.
        detail["notes_parts_with_text"] = len(notes_with_text)
        detail["diagram_parts"] = len(diagrams)
        detail["slide_bytes_scanned"] = scanned_bytes
        detail["scan_chunk_bytes"] = OOXML_SCAN_CHUNK_BYTES

    detail["byte_length"] = len(data)
    return patterns, detail


def _has_authored_text(archive: zipfile.ZipFile, name: str) -> bool:
    """Whether a drawingml part carries text a person typed.

    Generated fields — the slide number, the date, the footer — are removed first. They are
    drawingml text runs like any other, and counting them would make every notes part in
    every deck look like speaker notes.

    Read whole rather than streamed: a notes slide is one text box, and the field strip has
    to see a complete ``<a:fld>...</a:fld>`` pair, which a chunked scan could split.
    """
    body = _DRAWINGML_FIELD_RE.sub(b"", archive.read(name))
    return any(match.group(1).strip() for match in _DRAWINGML_TEXT_RE.finditer(body))


def _has_streamed_text(archive: zipfile.ZipFile, name: str) -> bool:
    """Whether a part carries a text run with something in it. Streamed, early-exit.

    For ``word/document.xml``, which :func:`_has_authored_text` must not be used on: that
    one reads the part whole, which is right for a notes slide and wrong for a 40 MB body.

    ``_DRAWINGML_TEXT_RE`` serves both because its prefix tolerance is not a drawingml
    detail — ``<w:t>`` and ``<a:t>`` are the same shape, and a byte scan cannot resolve
    namespaces anyway. It also excludes ``w:delText`` and ``w:instrText`` for free: both
    continue past the single ``t`` the pattern requires, so deleted text does not count as
    text this appliance can extract, which is the same line ``has_tracked_changes`` draws.

    A document WITH text stops on the first chunk. Only one with none is read to the end,
    and that is the case where the answer has to be certain.
    """
    with archive.open(name) as handle:
        for chunk, _ in _iter_chunks(handle):
            if any(match.group(1).strip() for match in _DRAWINGML_TEXT_RE.finditer(chunk)):
                return True
    return False


def _probe_xlsx(data: bytes) -> tuple[dict, dict]:
    """``(patterns, detail)`` for a SpreadsheetML package. OFFICE_SPEC.md Part 2.

    ``has_sheets`` is the scheduler's pattern and it comes from ``xl/workbook.xml`` rather
    than from counting ``xl/worksheets/sheetN.xml``: the workbook part is the ordered,
    NAMED list, and the sheet names are what a caller addressing a range later will use.
    A worksheet part with no entry in the workbook is orphaned and is not a sheet.

    The workbook part is parsed in full. It is a manifest — one element per sheet — so it
    is small however large the workbook's data is, and the cell data lives in the
    ``xl/worksheets/`` parts this never opens.
    """
    layout = _OOXML_LAYOUTS["xlsx"]
    unmeasured: dict[str, str] = {}
    with _open_package(data, "xlsx") as archive:
        names = archive.namelist()
        patterns, detail = _ooxml_common(names, layout)

        if layout.main_part in set(names):
            workbook = ET.fromstring(archive.read(layout.main_part))
            sheet_names = [
                sheet.get("name") or ""
                for sheet in workbook.iter(f"{{{_S_NS}}}sheet")
                # `<sheet>` also names external-workbook references elsewhere in the tree;
                # only the ones under `<sheets>` are this workbook's own.
                if sheet.get("sheetId") is not None
            ]
            patterns["has_sheets"] = bool(sheet_names)
            patterns["sheet_count"] = len(sheet_names)
            detail["sheet_names"] = sheet_names[:UNKNOWN_PARTS_REPORTED_MAX]
        else:
            unmeasured["has_sheets"] = (
                f"{layout.main_part} is named in the ZIP manifest but is not readable from "
                "the archive, so the workbook's sheet list was never read"
            )

        comment_parts = [name for name in names if _XLSX_COMMENTS_RE.match(name)]
        patterns["has_comments"] = bool(comment_parts)
        detail["comment_parts"] = sorted(comment_parts)[:UNKNOWN_PARTS_REPORTED_MAX]

    detail["byte_length"] = len(data)
    if unmeasured:
        detail["unmeasured"] = unmeasured
    return patterns, detail


def _probe_ole2(data: bytes) -> tuple[dict, dict]:
    """``(patterns, detail)`` for an OLE2 compound file — or a refusal, if it is encrypted.

    THIS IS THE PROBER OFFICE_SPEC.md PART 1 PUT ``olefile`` IN THE BASE INSTALL FOR. An
    encrypted `.docx` is NOT a ZIP: it is an OLE2 container holding ``EncryptionInfo`` and
    ``EncryptedPackage``, so it carries the same eight magic bytes as a 1997 `.doc` and
    :func:`detect_format` reports both as ``ole2``. The two go opposite ways — a legacy
    binary gets converted through LibreOffice, an encrypted package cannot be read by
    anything this appliance has — and separating them is one directory listing.

    Sending an encrypted package down the converter's path would mean LibreOffice failing
    on bytes it was never going to read, a long way from the cause. So the encrypted case
    raises :class:`EncryptedPackageError` here, at the first task that touches the file,
    and the node records a PERMANENT failure naming the reason rather than extracting to
    empty text and settling as a success.

    An OLE2 file that is neither — an MSI, a `Thumbs.db`, an Outlook `.msg` — gets both
    patterns false and its stream list in the detail. That is a measurement too, and it is
    what stops "we do not recognise this" from being reported as "this is a legacy Word
    document".
    """
    try:
        container = olefile.OleFileIO(io.BytesIO(data))
    except OSError as exc:
        # olefile raises IOError/OSError for a container whose FAT or directory will not
        # parse. The magic bytes matched, so this is a damaged or truncated compound file;
        # neither pattern can be measured and there is nothing to retry.
        raise ValueError(
            "the bytes carry the OLE2 compound-file signature but the container will not "
            f"open, so neither is_encrypted nor is_legacy_binary can be measured: {exc}"
        ) from exc

    try:
        entries = container.listdir(streams=True, storages=True)
    finally:
        container.close()

    # Both signatures are ROOT-level names. A stream called `WordDocument` nested inside a
    # storage is an embedded object — a Word document pasted into a spreadsheet — and does
    # not make the containing file a legacy Word binary.
    root_names = {path[0] for path in entries if len(path) == 1}
    stream_paths = sorted("/".join(path) for path in entries)

    encrypted = ENCRYPTED_PACKAGE_STREAMS <= root_names
    legacy_stream = next((stream for stream in LEGACY_BINARY_STREAMS if stream in root_names), None)

    detail = {
        "byte_length": len(data),
        "detected_by": "ole2_directory",
        "stream_count": len(entries),
        "streams": stream_paths[:UNKNOWN_PARTS_REPORTED_MAX],
        "encrypted_package_streams": sorted(ENCRYPTED_PACKAGE_STREAMS),
        "legacy_binary_stream": legacy_stream,
    }

    if encrypted:
        raise EncryptedPackageError(
            "these bytes are an encrypted OOXML package: the OLE2 container holds "
            f"{sorted(ENCRYPTED_PACKAGE_STREAMS)}, which is MS-OFFCRYPTO's wrapper around "
            "an encrypted .docx/.xlsx/.pptx. JMFTS carries no decryptor and has no way to "
            "accept a password, so this file cannot be read — and it is refused here rather "
            "than converted, because a converter given these bytes would fail on them a "
            "long way from the cause"
        )

    patterns = {
        # Always False by construction: the True case raised above and never gets here.
        # It is reported anyway rather than left out, because absent means "not measured"
        # everywhere else in this module, and "we looked at the directory and this is not
        # an encrypted package" is a measurement a later task is entitled to read.
        "is_encrypted": False,
        "is_legacy_binary": legacy_stream is not None,
    }
    if legacy_stream is not None:
        # Present exactly when there is a legacy binary to name. `convert:ooxml` needs to
        # know which application's format it is in order to pick a filter, and an absent
        # key is this module's convention for "not measured" — which is the truth for a
        # container that is not a legacy binary at all.
        patterns["legacy_application"] = LEGACY_BINARY_STREAMS[legacy_stream]
    return patterns, detail


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

#: Format -> the function that looks inside it. Its keys are the SAME set as
#: :data:`PROBERS_AVAILABLE`, which is what the module-scope assertion below pins: the
#: tuple is what ``EXPLAIN`` publishes to callers and the dict is what actually runs, and a
#: format present in one but not the other would make the appliance's answer to "can you
#: look inside a .pptx" disagree with what happens when it does.
_PROBERS: dict[str, Callable[[bytes], tuple[dict, dict]]] = {
    "pdf": _probe_pdf,
    "text": _probe_text,
    "docx": _probe_docx,
    "pptx": _probe_pptx,
    "xlsx": _probe_xlsx,
    "ole2": _probe_ole2,
}

assert set(_PROBERS) == set(PROBERS_AVAILABLE), (
    "PROBERS_AVAILABLE and the dispatch table disagree; one of them is what callers are "
    f"told and the other is what runs: {sorted(set(_PROBERS) ^ set(PROBERS_AVAILABLE))}"
)


def probe_patterns(data: bytes, detection: FormatDetection) -> tuple[dict, dict]:
    """``(patterns, detail)`` — the content patterns for a detected format.

    A format with no prober returns EMPTY patterns and a detail naming it. That keeps the
    probe attempt ``completed`` rather than ``skipped``, and spec 3.4 is explicit about
    why: ``skipped`` means the task was never attempted. probe *was* attempted — it ran,
    it identified the format, it wrote ``matched.format``. What it could not do is look
    inside an ``.epub``, because that prober has not been written. Recording that as
    ``skipped`` would claim probe never ran on this file, and the scheduler would have no
    record that the format was already identified.

    A prober may also RAISE, and one deliberately does: :func:`_probe_ole2` refuses an
    encrypted OOXML package (OFFICE_SPEC.md Part 2). That is not the same state as an
    empty pattern set — an empty set says "nothing to measure here", a raise says "these
    bytes cannot be read at all" — and the callers already treat them apart:
    ``run_probe`` records a FAILED attempt, and ``analyze_ingest`` returns ``probe_failed``
    with a null plan rather than a plan built on patterns nobody measured.
    """
    prober = _PROBERS.get(detection.format)
    if prober is not None:
        try:
            return prober(data)
        except zipfile.BadZipFile as exc:
            # A MEMBER failed to inflate, which `_open_package` cannot catch: it guards the
            # OPEN, and this archive opened. `central-directory-mismatch.docx` in
            # tests/corpus is exactly this shape — the directory is complete, namelist()
            # answers, detect_format says docx, and the file only falls apart when a part
            # is read.
            #
            # Left to escape, `zipfile.BadZipFile` derives straight from Exception and
            # `task_errors.classify_exception` grades it RETRYABLE, so probe would attempt
            # these bytes three times. A local header does not repair itself between
            # attempts. Same reasoning as `_open_package`, one layer in.
            raise ValueError(
                f"the {detection.format} package opened, but a part inside it could not be "
                f"read, so its patterns cannot be measured: {exc}"
            ) from exc
    return (
        {},
        {
            "no_prober_for_format": detection.format,
            "probers_available": list(PROBERS_AVAILABLE),
        },
    )
