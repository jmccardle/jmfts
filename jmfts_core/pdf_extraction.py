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
from dataclasses import dataclass, field
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


#: Whitespace inside one table cell. A markdown table row is ONE LINE — the pipes are
#: column separators, not text — so a newline inside a cell does not wrap it, it ends the
#: row and starts a new one that no longer parses as part of the table.
#:
#: PyMuPDF returns multi-line cells with their newlines intact, and academic tables stack
#: their column headers routinely. Measured on `attn.pdf` p.8 (Table 3 of "Attention Is
#: All You Need"): the 3-cell header row came out as FOUR physical lines, which pushed the
#: `| --- |` separator to row index 4 and left the block with 0, 1, 2 and 4 pipes on
#: different lines. No markdown parser reads that as a table.
_CELL_WHITESPACE = re.compile(r"\s+")


def _clean_cell(value: Optional[str]) -> str:
    """One cell's text, safe to place between two pipes.

    Two alterations, both forced by the row-is-one-line rule above: whitespace runs
    (newlines included) collapse to a single space, and a literal ``|`` is escaped so a
    cell that happens to contain one cannot invent a column.
    """
    if value is None:
        return ""
    return _CELL_WHITESPACE.sub(" ", str(value)).strip().replace("|", r"\|")


def _table_to_markdown(data: list) -> str:
    """Extracted table rows as a markdown table, or ``""`` if they hold nothing.

    Takes the ROWS, not the table. ``extract()`` is what runs the cell-text intersection,
    :func:`_find_page_tables` already has to run it to measure how full the candidate is,
    and taking the table here would run it a second time for every table on the page.

    It is also no longer wrapped in ``except Exception: return ""``. That made a table
    which failed to extract indistinguishable from a page that had none;
    :func:`_find_page_tables` catches it now and records which candidate raised.
    """
    if not data:
        return ""
    lines = []
    header = [_clean_cell(c) for c in data[0]]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join([" --- " for _ in header]) + "|")
    for row in data[1:]:
        lines.append("| " + " | ".join(_clean_cell(c) for c in row) + " |")
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


#: How much of a text block has to lie inside a table's bounding box before the block is
#: read as that table's own content rather than as prose standing beside it.
#:
#: The threshold has wide margin on both sides and the measurement says so. On `attn.pdf`
#: p.8, all nine blocks that make up Table 3 overlap its bbox at 100%, and the caption
#: block "Table 3: Variations on the Transformer architecture..." directly above it
#: overlaps at 0%. Nothing on that page lands between the two, so the choice of 0.5 is a
#: statement that a block is either inside a table or beside it — not a tuned constant.
#: A fraction rather than strict containment because a block that pokes a point past the
#: bbox is still the table's text, and strict containment would emit it twice.
_TABLE_BLOCK_OVERLAP = 0.5


#: How many of a candidate's cells have to carry text before it is read as a table.
#:
#: NOT a fallback and not a tuned constant — the same kind of structural filter as the
#: ``row_count >= 2 and col_count >= 2`` rule beside it, and for the same reason: a grid
#: is not a table just because something drew lines. Measured over the six candidates
#: `find_tables` reports on `attn.pdf`:
#:
#:     page  rows  cols  cells  filled  fill
#:        8     8     3     24      23   96%   <- Table 3, a real table
#:        9     6     3     18      18  100%   <- Table 4, a real table
#:       12     9    39    351      93   26%   <- attention-visualisation figure
#:       12     8    33    264      94   36%   <- attention-visualisation figure
#:       13    14    56    784     138   18%   <- attention-visualisation figure
#:       14    11    48    528     109   21%   <- attention-visualisation figure
#:
#: Half the candidates on this paper are figures. The attention heat maps are drawn as a
#: grid of ruled cells, which is exactly what the ``lines`` strategy looks for, so they
#: are found and they are not tables. 0.5 sits 14 points above the densest figure and 46
#: below the sparsest real table.
#:
#: This filter is what makes excluding a table's blocks from the prose SAFE. Without it a
#: rejected figure still swallows its 110 text blocks, and the page emits a 56-column row
#: of empty pipes in place of the words that were there — replacing readable text with
#: worse text. With it, a candidate that fails is not a table at all, its blocks stay
#: prose, and the page reads exactly as it did before tables existed.
_TABLE_MIN_FILL = 0.5


@dataclass
class _PageResult:
    """One page's markdown, and what happened to its tables.

    ``table_errors`` exists because the alternative was the bare ``except Exception:
    pass`` this replaces. ``find_tables`` already swallows its own exceptions and returns
    ``None`` (pymupdf/table.py), so ``page.find_tables().tables`` raises ``AttributeError``
    on a page it could not analyse — and catching that silently made a FAILED page and a
    page with no tables into the same answer. The same precedent
    ``control_chars_removed`` sets applies: the text is still returned, and the fact that
    part of it could not be tabulated is reported rather than hidden.

    ``tables_rejected`` is the same principle applied to :data:`_TABLE_MIN_FILL`. A
    candidate dropped for being too sparse is reported with its shape and fill, because
    "no table here" and "a grid I declined to call a table" are different facts and only
    one of them is worth looking at when a table goes missing from the output.

    ``blocks`` is the geometry this module used to compute and throw away —
    ``OFFICE_SPEC.md`` Part 0's "no positional anchor survives extraction". Every rectangle
    here was already read out of the page to order the text and to decide which blocks a
    table owns; what is new is only that each one is now paired with the SPAN of markdown it
    produced, so a later task can go from a character offset back to a rectangle. Spans are
    relative to :attr:`markdown`; :func:`pdf_to_markdown` shifts them into whole-document
    coordinates and adds the page number.

    ``control_chars_removed`` moved here from the caller for the same reason the spans are
    computed here: sanitisation DELETES characters, so it has to happen before the offsets
    are taken or every span past the first removal would be wrong by the number of
    characters removed above it. That is the same hazard ``page_offsets`` already documents
    one level up, at page granularity.
    """

    markdown: str
    blocks: list[dict] = field(default_factory=list)
    control_chars_removed: int = 0
    tables_found: int = 0
    table_errors: list[str] = field(default_factory=list)
    tables_rejected: list[dict] = field(default_factory=list)


def _union(boxes: list[tuple[float, float, float, float]]) -> tuple[float, float, float, float]:
    """The smallest rectangle containing all of ``boxes``.

    A markdown paragraph is several wrapped LINES of the source page, each with its own
    rectangle, and it is emitted as one segment. The union is that segment's rectangle. It
    is used rather than the enclosing text block's own bbox because a block can hold a
    heading and the paragraph under it, and the union of the lines that actually became
    this segment is the tighter — and therefore the more useful — of the two.
    """
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _table_owner(block_rect: pymupdf.Rect, table_rects: list[pymupdf.Rect]) -> Optional[int]:
    """Index of the table whose content this block is, or ``None`` if it is prose."""
    area = block_rect.get_area()
    if area <= 0:
        return None
    for index, table_rect in enumerate(table_rects):
        if (block_rect & table_rect).get_area() / area >= _TABLE_BLOCK_OVERLAP:
            return index
    return None


@dataclass
class _PageTables:
    """What table detection made of one page."""

    tables: list = field(default_factory=list)
    #: Extracted rows per accepted table, positionally aligned with :attr:`tables`. Kept
    #: because deciding whether a candidate is dense enough already costs an ``extract()``,
    #: and re-extracting to render it would run the cell-text intersection a second time.
    rows: list = field(default_factory=list)
    #: A LIST, not one string. Each candidate is extracted separately, so a page can fail
    #: more than once, and a single field would keep only whichever failed last.
    errors: list[str] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)


def _find_page_tables(page: pymupdf.Page) -> _PageTables:
    """The page's tables worth rendering, plus what was rejected and why.

    Two structural filters, and neither is a judgement about content. A table needs at
    least two rows and two columns — one of either is a rule drawn around a paragraph, and
    rendering it as a table adds pipes and no structure. And it needs
    :data:`_TABLE_MIN_FILL` of its cells to carry text, because a grid of empty cells is a
    figure somebody ruled, not a table.

    ``extract()`` failures are attributed to the candidate that raised and the rest of the
    page continues. One unreadable table is not a reason to lose the other one.
    """
    result = _PageTables()
    try:
        found = page.find_tables()
    except Exception as exc:  # noqa: BLE001 — recorded on the result, never dropped
        result.errors.append(f"{type(exc).__name__}: {exc}")
        return result
    # `find_tables` returns None when it caught something internally. Reading `.tables`
    # off that is the AttributeError the old bare `except` was quietly absorbing.
    if found is None:
        result.errors.append("find_tables() returned None; PyMuPDF could not analyse this page")
        return result

    for index, table in enumerate(found.tables):
        if table.row_count < 2 or table.col_count < 2:
            continue
        try:
            rows = table.extract()
        except Exception as exc:  # noqa: BLE001 — recorded on the result, never dropped
            result.errors.append(f"table {index}: {type(exc).__name__}: {exc}")
            continue
        cells = sum(len(row) for row in rows)
        filled = sum(1 for row in rows for cell in row if str(cell or "").strip())
        fill = (filled / cells) if cells else 0.0
        if fill < _TABLE_MIN_FILL:
            result.rejected.append(
                {
                    "rows": table.row_count,
                    "cols": table.col_count,
                    "fill": round(fill, 3),
                    "reason": f"only {fill:.0%} of cells carry text",
                }
            )
            continue
        result.tables.append(table)
        result.rows.append(rows)
    return result


def _process_page(
    page: pymupdf.Page, headers: Optional[_IdentifyHeaders], extract_tables: bool
) -> _PageResult:
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

    A TABLE'S TEXT IS EMITTED ONCE, WHERE IT SITS. A table is drawn from the same text
    blocks this function walks as prose, so rendering the tables and the blocks
    independently — which is what it used to do, appending every table after the prose —
    put the same characters in the output twice. Measured on `attn.pdf`: 101 of 174 table
    cells (58%) appeared twice on their own page, and the table pass added 9,770
    characters, 23.7% on top of the prose. That is text the chunker splits and the
    embedder pays for, and BM25 counts twice.

    So each block is asked which table it belongs to (:func:`_table_owner`), and a block
    that belongs to one is not emitted as prose. The table takes the place of the FIRST
    block it accounts for, which is what puts it back in the reading flow instead of at
    the end of the page.

    THE BLOCKS ARE NOT RE-SORTED to do that, and they must not be. PyMuPDF's block order
    is a reading order, and it is not sorted by vertical position — on `attn.pdf` p.8 the
    block at y=185 arrives before the block at y=168, because a row label and its row are
    separate blocks. Ordering the page by `y0` to interleave the tables would reflow every
    multi-column page in the corpus. Anchoring to a block position leaves prose order
    exactly as it was.

    EVERY EMITTED SEGMENT CARRIES ITS RECTANGLE. `markdown_lines` used to be a list of
    strings; it is a list of (text, rectangle) pairs now, and the rectangle is the one the
    surrounding code was already holding — a line's own bbox for a heading, the union of the
    lines in a paragraph run, the table's bbox for a table. Nothing new is measured and no
    extra pass is made over the page. See `_PageResult.blocks` for what that buys.
    """
    segments: list[tuple[str, tuple[float, float, float, float]]] = []
    result = _PageResult(markdown="")

    blocks = page.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT)["blocks"]

    found = _find_page_tables(page) if extract_tables else _PageTables()
    result.table_errors = found.errors
    result.tables_rejected = found.rejected
    result.tables_found = len(found.tables)
    table_rects = [pymupdf.Rect(t.bbox) for t in found.tables]
    # A table is rendered at most once even if several blocks belong to it.
    emitted = [False] * len(found.tables)

    def render(index: int) -> None:
        """Emit table ``index`` once, in the place of the first block it accounts for."""
        emitted[index] = True
        md = _table_to_markdown(found.rows[index])
        if md:
            segments.append((md, tuple(found.tables[index].bbox)))

    for block in blocks:
        if block.get("type") != 0:  # only text blocks
            continue

        owner = _table_owner(pymupdf.Rect(block["bbox"]), table_rects)
        if owner is not None:
            # This block IS the table's text. The table stands in for it, once.
            if not emitted[owner]:
                render(owner)
            continue

        body_lines: list[str] = []
        body_boxes: list[tuple[float, float, float, float]] = []
        for line in block.get("lines", []):
            spans = [s for s in line.get("spans", []) if s.get("text", "").strip()]
            if not spans:
                continue
            line_text = _join_spans(spans)
            if not line_text:
                continue

            line_box = tuple(line["bbox"])
            prefix = headers.prefix(spans[0]) if headers else ""
            if prefix:
                # Flush the paragraph this heading interrupts, then stand alone.
                if body_lines:
                    segments.append((" ".join(body_lines), _union(body_boxes)))
                    body_lines, body_boxes = [], []
                segments.append((prefix + line_text, line_box))
            else:
                body_lines.append(line_text)
                body_boxes.append(line_box)
        if body_lines:
            segments.append((" ".join(body_lines), _union(body_boxes)))

    # A table no block was inside has nothing to anchor it to. It goes at the end, which
    # is where every table used to go, so this is the old behaviour surviving for the one
    # case it was right for.
    for index in range(len(found.tables)):
        if not emitted[index]:
            render(index)

    # Sanitise and measure in one pass, in that order. `_CONTROL_CHARS` DELETES characters
    # (see the constant), so an offset taken before the removal is wrong by however many
    # were removed above it. The "\n\n" the segments are joined with carries no control
    # character, so accounting for it as two characters between segments is exact.
    pieces: list[str] = []
    cursor = 0
    for text, bbox in segments:
        clean, removed = _CONTROL_CHARS.subn("", text)
        result.control_chars_removed += removed
        if pieces:
            cursor += 2  # the "\n\n" join
        result.blocks.append(
            {
                "bbox": [round(value, 2) for value in bbox],
                "start": cursor,
                "end": cursor + len(clean),
            }
        )
        pieces.append(clean)
        cursor += len(clean)

    result.markdown = "\n\n".join(pieces)
    return result


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

    Three keys report what became of the tables, and they follow ``control_chars_removed``:
    the text is returned either way, and the caller is told what about it is incomplete
    instead of having to infer it.

    * ``tables_found`` — how many tables were rendered into the markdown.
    * ``pages_with_tables`` — the 0-based numbers of the pages those tables came from, in
      page order. It is the pattern spec Part 4's ``extract:tables`` row reads, and it is
      reported HERE rather than by ``probe`` because this is the only pass that has to
      detect tables anyway: ``_table_owner`` needs their geometry while the prose is being
      written, so the scan cannot be deferred to a later task and does not need to be done
      by an earlier one. See ``jmfts_core.probe._probe_pdf`` for what that removed.

      It counts RENDERED tables, not candidates. A page whose only candidate was rejected
      by :data:`_TABLE_MIN_FILL` is not in this list, because there is no table on it to
      give a node to. ``probe`` used to count raw ``find_tables()`` output and therefore
      reported the attention paper's three figure pages as pages with tables; they are
      figures drawn as ruled grids, and that answer was wrong.
    * ``table_failures`` — ``{"page": int, "error": str}`` per page whose tables could not
      be detected or extracted. Empty is the ordinary case, and it is a different fact
      from a document with no tables, which reports ``tables_found == 0`` and no failures.
    * ``tables_rejected`` — ``{"page": int, "rows": int, "cols": int, "fill": float,
      "reason": str}`` per candidate dropped by :data:`_TABLE_MIN_FILL`. On a paper whose
      figures are drawn as ruled grids this is where they go, and reporting them is what
      keeps "there was no table" apart from "there was a grid I declined to call one".

    ``text_blocks`` is the positional half of the same idea as ``page_offsets``, at the
    granularity of a paragraph rather than a page: ``{"page": int, "bbox": [x0, y0, x1, y1],
    "start": int, "end": int}`` per emitted markdown segment, in document order, with
    ``start``/``end`` being character offsets into ``markdown_text`` and ``bbox`` being the
    rectangle in PDF points on ``page``. It is what makes a character offset in the markdown
    invertible back into a place on a page, which is ``OFFICE_SPEC.md`` Part 5's anchor —
    see ``jmfts_core.citation_tasks``, which is the only consumer and which recomputes this
    from the blob rather than having it stored on the node.

    IT IS RETURNED, NOT PERSISTED, and that is a deliberate split. A 500-page book has tens
    of thousands of segments; carrying them in the file node's ``extraction`` record would
    put roughly a megabyte of geometry into a JSONB column that every read of the node
    loads, in exchange for saving one re-parse of a document that is already parsed once per
    ingest. The ``citation`` task re-derives them instead, which costs a second pass over
    the bytes and keeps the node the size it was.

    A failed page is NOT an exception. The prose of that page extracted normally; what is
    missing is the tabular rendering of part of it, and raising here would fail a whole
    document over one page PyMuPDF could not analyse. It is logged as a warning as well,
    because a caller that ignores the metadata should still leave a trace.
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
        tables_found = 0
        pages_with_tables: list[int] = []
        table_failures: list[dict] = []
        tables_rejected: list[dict] = []
        text_blocks: list[dict] = []
        cursor = 0
        for page_number, page in enumerate(doc):
            # Sanitised per page and per segment, BEFORE the offsets are taken — see
            # `_PageResult`. Stripping the whole document afterwards would shift every
            # offset by the number of characters removed above it, which is exactly the
            # kind of quietly-wrong index these lists exist to avoid.
            result = _process_page(page, headers, extract_tables)
            tables_found += result.tables_found
            if result.tables_found:
                pages_with_tables.append(page_number)
            for error in result.table_errors:
                table_failures.append({"page": page_number, "error": error})
                logger.warning("%s page %d: %s", source_name, page_number, error)
            for rejection in result.tables_rejected:
                tables_rejected.append({"page": page_number, **rejection})
            control_chars_removed += result.control_chars_removed
            for block in result.blocks:
                text_blocks.append(
                    {
                        "page": page_number,
                        "bbox": block["bbox"],
                        "start": cursor + block["start"],
                        "end": cursor + block["end"],
                    }
                )
            page_offsets.append(cursor)
            page_md.append(result.markdown)
            cursor += len(result.markdown) + 2  # the "\n\n" the pages are joined with

        markdown_text = "\n\n".join(page_md)
        metadata = {
            "control_chars_removed": control_chars_removed,
            "tables_found": tables_found,
            "pages_with_tables": pages_with_tables,
            "table_failures": table_failures,
            "tables_rejected": tables_rejected,
            "page_offsets": page_offsets,
            "text_blocks": text_blocks,
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
