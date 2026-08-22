"""``.docx`` and ``.pptx`` to markdown — ``docs/INGEST_SPEC.md`` Part 10 step 6.

``INGEST_SPEC.md`` 11.3 makes markdown the intermediate format that every entry point
converges on, so these are CONVERSIONS in the same sense as
:func:`jmfts_core.structure_tasks._extract_html`: the output is markdown, the structure
rungs split it on its headings, and nothing downstream needs to know which office
application wrote the file.

TIER 2, and it is the whole reason this module is in :mod:`jmfts_core.office` rather than
beside the PDF reader. ``python-docx`` and ``python-pptx`` are the ``office`` extra, and
every import of them below happens inside a function, behind
:func:`~jmfts_core.office.require_docx` / :func:`~jmfts_core.office.require_pptx`. An
install without them can still detect and probe an office file — see the package
docstring — and ``tests/test_office_packaging.py`` fails if importing the app reaches a
reader.

WHAT "BASIC" COVERS, stated so the gaps are known rather than discovered:

* **Headings.** A paragraph whose style resolves to ``Heading 1``–``Heading 9`` becomes the
  matching ATX heading, and ``Title`` becomes ``#``. This is what makes
  ``structure:declared`` real for a ``.docx``: the pattern the scheduler gates that rung on
  is ``has_heading_styles``, and the headings it measured are the headings written here.
* **Prose, lists and tables.** Body order is preserved for a ``.docx`` by walking the body
  element rather than ``Document.paragraphs``, which skips tables entirely.
* **Slides and speaker notes** for a ``.pptx``, one ``#`` section per slide.

DELIBERATELY NOT COVERED, because a wrong rendering is worse than a missing one:

* **Ordered vs unordered lists.** A numbered list becomes ``-`` like any other. Telling
  them apart means resolving ``w:numId`` through ``word/numbering.xml`` to an abstract
  numbering definition, and a guess would renumber the author's list.
* **Images, charts, SmartArt.** ``probe`` reports ``has_images`` and ``has_smartart``; a
  reader for them is ``extract:images``, a different task.
* **Comments, footnotes, tracked changes.** ``probe`` reports each. Merging an unaccepted
  insertion into the prose would put text into the index that the document does not say.
* **Legacy ``.doc``/``.ppt``.** Not a ZIP at all. Tier 3, the ``convert`` extra.
"""

from __future__ import annotations

import io
import re

# The guards, not the readers. `jmfts_core.office` imports neither python-docx nor
# python-pptx at module scope, so importing it here costs a base install nothing.
from jmfts_core.office import require_docx, require_pptx

#: A style name that means "this paragraph is an outline heading at level N".
#:
#: Matched on the style NAME rather than on the id, and matched loosely, for the reason
#: ``jmfts_core.probe._HEADING_STYLE_NAME_RE`` gives: the literal string "Heading 1" misses
#: every renamed style and every non-English document. python-docx maps a built-in style
#: back to its canonical English name, so this sees "Heading 1" even where the file says
#: something else — which is exactly the resolution the prober does by hand through
#: ``word/styles.xml``, and the two agreeing is what makes the pattern and the output
#: describe one document.
_HEADING_NAME_RE = re.compile(r"^heading\s*([1-9])$", re.IGNORECASE)

#: Markdown's own heading marker, at the start of a paragraph that is NOT a heading.
#:
#: A ``.docx`` body paragraph may legitimately begin "## Notes" as literal prose. Written
#: through unescaped it would become an ATX heading, and ``split_on_headings`` would cut a
#: chunk boundary the author never wrote. Narrow on purpose: ATX needs the space, so
#: "#hashtag" is left alone.
_LEADING_ATX_RE = re.compile(r"^(#{1,6})(\s)")

#: A style name that means "this paragraph is a list item" on its own, with no ``w:numPr``
#: on the paragraph.
#:
#: Both signals are needed and neither subsumes the other. Word usually stamps ``w:numPr``
#: onto the paragraph and leaves the style as "List Paragraph"; a document built through
#: python-docx's ``add_paragraph(style="List Bullet")``, and plenty of authored ones, carry
#: the numbering in the STYLE and nothing on the paragraph. Checking only the paragraph
#: misses the second kind, which is how a bulleted list extracts as flat prose.
#:
#: "List Paragraph" is deliberately NOT here. It is applied to indented non-list text at
#: least as often as to lists, so it is a list item only when the paragraph also carries
#: numbering — which the ``w:numPr`` test already catches.
_LIST_STYLE_NAME_RE = re.compile(r"^list (bullet|number)\s*[0-9]*$", re.IGNORECASE)

#: Three or more consecutive blank lines collapse to one blank line. Empty paragraphs are
#: how a person spaces a Word document, and carrying every one of them through would make
#: the markdown mostly whitespace.
_BLANK_RUN_RE = re.compile(r"\n{3,}")


class OfficeReadError(ValueError):
    """These bytes will not open as the format the manifest said they were.

    A ``ValueError`` so :func:`jmfts_core.task_errors.classify` calls it PERMANENT. The
    default for an unrecognised exception is RETRYABLE, which is right for a database
    blip and wrong here: a package python-docx refuses to open at 10:00 is refused
    identically at 10:05, and three attempts only delay the moment someone reads why.
    """


def _escape_cell(text: str) -> str:
    """One table cell, safe to sit between pipes.

    A newline inside a cell would end the table row, and an unescaped pipe would add a
    column — both silently, producing a table that renders but says something else.
    """
    return text.replace("|", r"\|").replace("\n", "<br>").strip()


def _table_markdown(rows: list[list[str]]) -> list[str]:
    """A GitHub-flavoured table, or nothing at all for a table with no cells.

    The first row is the header because markdown has no way to say a table has none, and
    a table whose header row is a data row is a smaller error than a table that does not
    render. The column count comes from the widest row: a short row would otherwise drop
    its trailing cells into the previous row on the reader's side.
    """
    if not rows:
        return []
    width = max(len(row) for row in rows)
    if width == 0:
        return []
    padded = [row + [""] * (width - len(row)) for row in rows]
    header, *body = padded
    lines = [
        "| " + " | ".join(_escape_cell(cell) for cell in header) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    for row in body:
        lines.append("| " + " | ".join(_escape_cell(cell) for cell in row) + " |")
    return lines


#: A block that is a single list item, at any indent depth. Used only to decide the
#: separator between two blocks.
_LIST_BLOCK_RE = re.compile(r"^\s*- ")


def _assemble(blocks: list[str]) -> str:
    """Join block strings into markdown, and no runaway whitespace.

    Blocks are separated by a blank line, EXCEPT two consecutive list items, which are
    separated by a single newline. A blank line between them is still a list to a markdown
    parser — a "loose" one — but it renders with paragraph spacing and doubles the
    whitespace in a document that is mostly bullets, which is most decks.
    """
    kept = [block for block in blocks if block]
    if not kept:
        return ""
    parts = [kept[0]]
    for previous, block in zip(kept, kept[1:]):
        separator = (
            "\n" if _LIST_BLOCK_RE.match(previous) and _LIST_BLOCK_RE.match(block) else "\n\n"
        )
        parts.append(separator + block)
    text = "".join(parts)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = _BLANK_RUN_RE.sub("\n\n", text).strip()
    return text + "\n" if text else ""


# ---------------------------------------------------------------------------
# docx
# ---------------------------------------------------------------------------


def _docx_paragraph_markdown(paragraph, counts: dict) -> str:
    """One ``w:p`` as a markdown block, or an empty string for an empty one."""
    text = paragraph.text.strip()
    if not text:
        return ""

    style_name = (paragraph.style.name or "") if paragraph.style is not None else ""
    heading = _HEADING_NAME_RE.match(style_name.strip())
    if heading:
        counts["headings"] += 1
        return "#" * int(heading.group(1)) + " " + text
    if style_name.strip().lower() == "title":
        counts["headings"] += 1
        return "# " + text

    numbering = paragraph._p.pPr is not None and paragraph._p.pPr.numPr is not None
    if numbering or _LIST_STYLE_NAME_RE.match(style_name.strip()):
        counts["list_items"] += 1
        return "- " + _LEADING_ATX_RE.sub(r"\\\1\2", text)

    counts["paragraphs"] += 1
    return _LEADING_ATX_RE.sub(r"\\\1\2", text)


def docx_to_markdown(data: bytes) -> tuple[str, dict]:
    """``(markdown, meta)`` for a WordprocessingML package.

    The body is walked as an ELEMENT SEQUENCE, not as ``Document.paragraphs``. That
    property yields only ``w:p`` and silently omits every table, so a document whose
    content is a table would extract as an empty string — a plausible tree built from
    nothing, which is the hazard ``INGEST_SPEC.md`` 11.3 names.
    """
    docx = require_docx()
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - any failure here means the same thing
        raise OfficeReadError(
            f"the ZIP manifest identified these bytes as docx, but python-docx will not "
            f"open them, so no text can be read: {exc}"
        ) from exc

    counts = {"paragraphs": 0, "headings": 0, "list_items": 0, "tables": 0}
    blocks: list[str] = []
    # The PARENT passed to each proxy is the document, not the body element. A proxy
    # resolves `paragraph.style` through `parent.part`, and a raw lxml element has no
    # `.part` — passing the body raises AttributeError on the first styled paragraph.
    body = document.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            block = _docx_paragraph_markdown(Paragraph(child, document), counts)
            if block:
                blocks.append(block)
        elif child.tag == qn("w:tbl"):
            table = Table(child, document)
            rows = [[cell.text for cell in row.cells] for row in table.rows]
            lines = _table_markdown(rows)
            if lines:
                counts["tables"] += 1
                blocks.append("\n".join(lines))

    markdown = _assemble(blocks)
    return markdown, {
        "paragraph_count": counts["paragraphs"],
        "heading_count": counts["headings"],
        "list_item_count": counts["list_items"],
        "table_count": counts["tables"],
        "characters": len(markdown),
    }


# ---------------------------------------------------------------------------
# pptx
# ---------------------------------------------------------------------------


def _pptx_frame_markdown(text_frame, counts: dict) -> list[str]:
    """A text frame's paragraphs, as markdown blocks.

    ``paragraph.level`` is PowerPoint's outline depth within the placeholder, and it
    becomes list indentation rather than a heading level: the depth is a position inside
    one text box, not a position in the deck, and promoting it to a heading would put a
    second heading hierarchy underneath the per-slide one that
    ``structure:declared`` splits on.
    """
    blocks: list[str] = []
    for paragraph in text_frame.paragraphs:
        text = paragraph.text.strip()
        if not text:
            continue
        counts["text_blocks"] += 1
        indent = "  " * max(0, paragraph.level)
        blocks.append(f"{indent}- " + _LEADING_ATX_RE.sub(r"\\\1\2", text))
    return blocks


def pptx_to_markdown(data: bytes) -> tuple[str, dict]:
    """``(markdown, meta)`` for a PresentationML package.

    ONE ``#`` SECTION PER SLIDE. That is what makes ``structure:declared`` mean something
    for a deck: ``DECLARED_STRUCTURE_PATTERN`` maps ``pptx`` to ``has_slides``, and the
    structure this writes is exactly the slides the prober counted, so a chunk is a slide.

    THE HEADING IS THE TITLE PLACEHOLDER, THEN THE FIRST TEXT ON THE SLIDE, THEN "Slide N".
    The middle step is not decoration. Measured over 22 real decks from EU open data — 220
    slides — the title placeholder was absent on 80 of them, 36%, because the deck was
    built from a blank layout with the title typed into an ordinary text box. Stopping at
    "Slide N" for those loses the heading that ``split_on_headings`` turns into the chunk's
    section title, which is the label a search result is read by.

    Using the slide's first text is a DEFAULT, not a guess about correctness: that text is
    really on the slide, and it is promoted rather than duplicated. Shape order within a
    slide is z-order, so it can pick the wrong box on a slide with several; a heading that
    is the wrong line of real text is still a better answer than a number, and the body
    keeps everything else.

    ``slide_offsets`` is the character offset of each slide's heading in the returned
    markdown — the analogue of the PDF reader's ``page_offsets``, and in the record for the
    same reason: it is an index INTO the text that a later task cannot recompute without
    re-opening the package.

    Shape order within a slide is z-order, not reading order, because PowerPoint stores no
    reading order. The title is lifted out first; everything else follows in the order the
    file lists it.
    """
    pptx = require_pptx()

    try:
        presentation = pptx.Presentation(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - any failure here means the same thing
        raise OfficeReadError(
            f"the ZIP manifest identified these bytes as pptx, but python-pptx will not "
            f"open them, so no text can be read: {exc}"
        ) from exc

    counts = {"text_blocks": 0, "tables": 0, "notes": 0, "promoted_titles": 0}
    sections: list[str] = []
    for number, slide in enumerate(presentation.slides, start=1):
        title_shape = slide.shapes.title
        title = (title_shape.text or "").strip() if title_shape is not None else ""

        # Compared by ELEMENT, not by identity. `shapes.title` builds a fresh proxy on
        # every access, so `shape is title_shape` is never true and the title is written
        # twice — once as the heading and once as a bullet under it.
        title_element = title_shape.element if title_shape is not None else None

        if not title:
            # No title placeholder, or an empty one. Promote the slide's first line of
            # text; `title_element` follows it so the body does not repeat it.
            for shape in slide.shapes:
                if not shape.has_text_frame:
                    continue
                first = next(
                    (p.text.strip() for p in shape.text_frame.paragraphs if p.text.strip()),
                    "",
                )
                if first:
                    title = first.split("\n")[0]
                    title_element = shape.element
                    counts["promoted_titles"] += 1
                    break

        blocks = ["# " + (title if title else f"Slide {number}")]
        promoted_paragraph = (
            title if title_shape is None or not (title_shape.text or "").strip() else None
        )

        for shape in slide.shapes:
            if title_element is not None and shape.element is title_element:
                # The promoted shape may hold more than the one line that became the
                # heading, so its remaining paragraphs still belong in the body.
                if promoted_paragraph is not None and shape.has_text_frame:
                    rest = [
                        p.text.strip()
                        for p in shape.text_frame.paragraphs
                        if p.text.strip() and p.text.strip() != promoted_paragraph
                    ]
                    for line in rest:
                        counts["text_blocks"] += 1
                        blocks.append("- " + _LEADING_ATX_RE.sub(r"\\\1\2", line))
                continue
            if getattr(shape, "has_table", False):
                rows = [[cell.text for cell in row.cells] for row in shape.table.rows]
                lines = _table_markdown(rows)
                if lines:
                    counts["tables"] += 1
                    blocks.append("\n".join(lines))
                continue
            if shape.has_text_frame:
                blocks.extend(_pptx_frame_markdown(shape.text_frame, counts))

        # Speaker notes are authored text and are kept, under their own subheading so a
        # reader can tell what was on the slide from what was said beside it. Dropping
        # them would lose the half of many decks that carries the argument.
        if slide.has_notes_slide:
            notes = (slide.notes_slide.notes_text_frame.text or "").strip()
            if notes:
                counts["notes"] += 1
                blocks.append("## Notes")
                blocks.append(_LEADING_ATX_RE.sub(r"\\\1\2", notes))

        sections.append(_assemble(blocks))

    # Offsets are computed against the joined text rather than accumulated while building
    # it, so they cannot disagree with what `_assemble` actually produced.
    markdown = ""
    offsets: list[int] = []
    for section in sections:
        if markdown:
            markdown += "\n"
        offsets.append(len(markdown))
        markdown += section

    return markdown, {
        "slide_count": len(sections),
        "slide_offsets": offsets,
        "text_block_count": counts["text_blocks"],
        "table_count": counts["tables"],
        "notes_slide_count": counts["notes"],
        # How many slides had no usable title placeholder and took their first line of
        # text instead. Reported because it is the difference between a heading somebody
        # wrote and one this reader chose.
        "promoted_title_count": counts["promoted_titles"],
        "characters": len(markdown),
    }
