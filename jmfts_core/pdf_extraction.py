"""PDF → markdown extraction (PyMuPDF).

Ported from an earlier ingestion project and trimmed:
images are skipped (the LLM-Wiki use case has no consumer for them yet);
tables and font-size-driven heading detection are kept.

Public API: ``pdf_to_markdown(pdf_bytes_or_path) -> tuple[str, dict]``.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from pathlib import Path
from typing import Optional, Union

import pymupdf

logger = logging.getLogger(__name__)


_BULLET_CHARS = ("- ", "* ", "> ", "•", "·", "◦", "▪", "▫")

#: C0 control characters and DEL, minus the three that are legitimate text: tab, newline,
#: carriage return. PDF font encodings routinely map an unmapped glyph to a control
#: codepoint, so these arrive in extracted text from ordinary, undamaged papers — 9 of 26
#: in a working corpus carried one, 4 of those carried NUL.
#:
#: NUL is the one that matters: PostgreSQL rejects it in any text value
#: (``ValueError: A string literal cannot contain NUL (0x00) characters``), which rolls
#: back the whole extraction transaction and leaves the file node with no content at all.
#:
#: Removing them is NOT a fallback around a failure. It is normalisation of a known
#: encoding artifact, and it is reported rather than hidden: ``pdf_to_markdown`` returns
#: the count in ``metadata['control_chars_removed']`` so a caller can see that the text it
#: is holding was altered, and how much.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class _IdentifyHeaders:
    """Map font sizes to markdown header levels by frequency analysis.

    Body text is the most frequent font size; sizes larger than that become
    levels 1-N (largest = #).
    """

    def __init__(self, doc: pymupdf.Document, body_limit: float = 12, max_levels: int = 6):
        self.body_limit = body_limit
        self.header_id: dict[int, str] = {}

        sizes: dict[int, int] = defaultdict(int)
        for page in doc:
            blocks = page.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT)["blocks"]
            for span in [
                s
                for b in blocks
                for line in b.get("lines", [])
                for s in line.get("spans", [])
                if s.get("text", "").strip()
            ]:
                sizes[round(span["size"])] += len(span["text"].strip())

        if not sizes:
            return

        sorted_sizes = sorted(sizes.items(), key=lambda x: x[1], reverse=True)
        self.body_limit = max(body_limit, sorted_sizes[0][0])
        header_sizes = sorted(
            [f for f in sizes.keys() if f > self.body_limit],
            reverse=True,
        )[:max_levels]
        for i, size in enumerate(header_sizes, start=1):
            self.header_id[size] = "#" * i + " "

    def prefix(self, span: dict) -> str:
        size = round(span.get("size", 0))
        if size <= self.body_limit:
            return ""
        return self.header_id.get(size, "")


def _table_to_markdown(table) -> str:
    try:
        data = table.extract()
    except Exception:
        return ""
    if not data:
        return ""
    lines = []
    header = [str(c) if c else "" for c in data[0]]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join([" --- " for _ in header]) + "|")
    for row in data[1:]:
        lines.append("| " + " | ".join(str(c) if c else "" for c in row) + " |")
    return "\n".join(lines)


def _style_span(span: dict) -> str:
    """One span's text with its font flags rendered as markdown emphasis."""
    text = span.get("text", "")
    flags = span.get("flags", 0)
    if flags & (1 << 3):  # monospaced
        text = f"`{text}`"
    if flags & (1 << 4):  # bold
        text = f"**{text}**"
    if flags & (1 << 1):  # italic
        text = f"*{text}*"

    # Normalize bullet chars to "- "
    for bullet in _BULLET_CHARS:
        if text.startswith(bullet):
            text = "- " + text[len(bullet) :]
            break
    return text


#: How wide a horizontal gap between two spans has to be, as a fraction of the smaller
#: font size, before it is read as a word break. A space glyph is roughly 0.25em in most
#: text faces; this sits below that so a real space is never missed, and well above the
#: sub-point kerning jitter that separates two spans of one word.
_SPACE_GAP_EM = 0.15


def _join_spans(spans: list[dict]) -> str:
    """One line's spans as text, deciding each span boundary from the page geometry.

    A span boundary is a FONT CHANGE. Sometimes a word break falls on one and sometimes
    it does not, and the span text carries the space only sometimes — so neither joining
    with a space nor concatenating is right, and both fail loudly on this corpus:

      * joining with " " turns a small-capped heading, emitted one span per case run as
        ('R', 12pt) ('ELATED', 9.6pt) (' W', 12pt) ('ORK', 9.6pt), into "R ELATED  W ORK",
        which no outline title matches;
      * concatenating glues ordinary words together wherever a font change coincided with
        a word break — measured over 26 papers, that lost 36% of the corpus's words.

    The gap between one span's right edge and the next span's left edge answers it
    directly, so that is what this reads.
    """
    pieces: list[str] = []
    for i, span in enumerate(spans):
        if i:
            previous = spans[i - 1]
            raw_before = previous.get("text", "")
            raw_after = span.get("text", "")
            already_spaced = raw_before[-1:].isspace() or raw_after[:1].isspace()
            if not already_spaced:
                gap = span["bbox"][0] - previous["bbox"][2]
                em = min(span.get("size", 0.0), previous.get("size", 0.0))
                if gap > _SPACE_GAP_EM * em:
                    pieces.append(" ")
        pieces.append(_style_span(span))
    return "".join(pieces).strip()


def _process_page(
    page: pymupdf.Page, headers: Optional[_IdentifyHeaders], extract_tables: bool
) -> str:
    """One page as markdown.

    A HEADING GETS ITS OWN LINE, and that is the whole reason this function is shaped
    the way it is. `_IdentifyHeaders` decides heading level per SPAN, and the obvious
    way to use that — appending the '#' to the span as it is written out — puts the
    marker wherever the span happens to fall. PyMuPDF splits a heading across spans
    whenever the font changes mid-title, and a text block holds several wrapped lines
    which are then joined into one, so the marker lands mid-line more often than not.
    `structural_splitting.split_on_headings` matches `^#{1,6}\\s` and cannot see any of
    those: over 26 papers, 189 of 347 emitted markers (54%) were invisible to it.

    So the level is read from the line's FIRST span — a heading starts at the start of
    its line — and the marker is written once, at the front, with the heading emitted as
    its own entry in `markdown_lines`. Body lines within a block are still joined with a
    space, because a PDF text block is a wrapped paragraph and its line breaks are
    typography rather than structure.
    """
    markdown_lines: list[str] = []

    blocks = page.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT)["blocks"]

    tables = []
    if extract_tables:
        try:
            for t in page.find_tables().tables:
                if t.row_count >= 2 and t.col_count >= 2:
                    tables.append(t)
        except Exception:
            pass

    for block in blocks:
        if block.get("type") != 0:  # only text blocks
            continue
        body_lines: list[str] = []
        for line in block.get("lines", []):
            spans = [s for s in line.get("spans", []) if s.get("text", "").strip()]
            if not spans:
                continue
            line_text = _join_spans(spans)
            if not line_text:
                continue

            prefix = headers.prefix(spans[0]) if headers else ""
            if prefix:
                # Flush the paragraph this heading interrupts, then stand alone.
                if body_lines:
                    markdown_lines.append(" ".join(body_lines))
                    body_lines = []
                markdown_lines.append(prefix + line_text)
            else:
                body_lines.append(line_text)
        if body_lines:
            markdown_lines.append(" ".join(body_lines))

    for table in tables:
        md = _table_to_markdown(table)
        if md:
            markdown_lines.append(md)

    return "\n\n".join(markdown_lines)


def pdf_to_markdown(
    pdf: Union[str, bytes, Path],
    *,
    identify_headers: bool = True,
    extract_tables: bool = True,
) -> tuple[str, dict]:
    """Convert a PDF (path or raw bytes) into markdown + metadata.

    Returns ``(markdown_text, metadata_dict)`` where metadata includes
    ``page_count``, ``title``, ``author``, ``toc``, ``page_offsets``, etc.

    ``page_offsets[i]`` is the character offset in ``markdown_text`` where page ``i``
    begins. The outline's page numbers are the only thing that tells a section title
    apart from the same title printed on a table-of-contents page — 9 of 20 papers in a
    working corpus print theirs — so an offset per page is what makes the outline usable
    for locating anything.
    """
    if isinstance(pdf, bytes):
        doc = pymupdf.open(stream=pdf, filetype="pdf")
        source_name = "<bytes>"
    else:
        path = Path(pdf)
        doc = pymupdf.open(str(path))
        source_name = path.name

    try:
        headers = _IdentifyHeaders(doc) if identify_headers else None
        meta = doc.metadata or {}
        page_md: list[str] = []
        page_offsets: list[int] = []
        control_chars_removed = 0
        cursor = 0
        for page in doc:
            # Sanitised per page, BEFORE the offsets are taken. Stripping the whole
            # document afterwards would shift every offset by the number of characters
            # removed above it, which is exactly the kind of quietly-wrong index this
            # list exists to avoid.
            text, removed = _CONTROL_CHARS.subn("", _process_page(page, headers, extract_tables))
            control_chars_removed += removed
            page_offsets.append(cursor)
            page_md.append(text)
            cursor += len(text) + 2  # the "\n\n" the pages are joined with

        markdown_text = "\n\n".join(page_md)
        metadata = {
            "control_chars_removed": control_chars_removed,
            "page_offsets": page_offsets,
            "filename": source_name,
            "title": meta.get("title", ""),
            "author": meta.get("author", ""),
            "subject": meta.get("subject", ""),
            "keywords": meta.get("keywords", ""),
            "page_count": doc.page_count,
            "producer": meta.get("producer", ""),
            "creator": meta.get("creator", ""),
            "created": str(meta.get("creationDate", "")),
            "modified": str(meta.get("modDate", "")),
            "toc": doc.get_toc(),
        }
        return markdown_text, metadata
    finally:
        doc.close()
