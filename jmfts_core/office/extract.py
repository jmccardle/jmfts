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

WHERE EACH BLOCK CAME FROM, which is ``OFFICE_SPEC.md`` Part 5's SOURCE anchor and Part 11
step 9. Every reader here returns ``source_blocks`` beside its counts: one entry per emitted
markdown block, in document order, carrying the block's ``[start, end)`` in the returned
markdown and — for a block that some element of the package actually produced — the part
and the XPath of that element::

    {"start": 412, "end": 486, "part": "word/document.xml",
     "path": "/w:document/w:body/w:p[42]"}

This is the positional half of the same idea as ``pdf_to_markdown``'s ``text_blocks``, and
:func:`jmfts_core.citation_tasks.run_anchor_source` inverts a chunk's ``source_span``
through it exactly as ``citation`` inverts one through the PDF block map. It is NOT written
into the extraction record, for the reason ``citation`` re-parses rather than persists its
own: a long document has thousands of these, and the extraction record is loaded on every
read of the node.

**A block with no ``path`` is a block this reader WROTE, and the absence is the point.** A
``# Slide 7`` heading for a slide with no title, and the ``## Notes`` label above a notes
section, are text this module invented; no element of the package accounts for them, so
recording an address for them would be an address that is probably right — which Part 5
rejects in as many words. The entry is still emitted, so a chunk that covers only invented
text is reported as unaddressed rather than as text nothing measured.
"""

from __future__ import annotations

import io
import re
from typing import Optional

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

#: The ``source_blocks`` key marking a block that, BY CONSTRUCTION, no page of a rendition
#: draws: the ``# Slide N`` heading and ``## Notes`` label this reader writes, and a slide's
#: speaker notes, which LibreOffice's slide export leaves out. ``anchor:render`` does not look
#: for these and reports a chunk made only of them as ``not_rendered`` rather than as text
#: the renderer lost (audit item #21). A key on the block map, not in the markdown, so the
#: text a chunk was cut from — and every stored ``content`` it is compared against — is
#: unchanged.
NOT_RENDERED = "not_rendered"

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


def _normalize_block(text: str) -> str:
    """One block's own whitespace, settled before it is joined to anything.

    THE NORMALISATION MOVED FROM THE JOINED TEXT TO THE BLOCK, and it had to, because
    :func:`_assemble` now reports where each block landed. A pass over the joined string
    DELETES characters, which is the hazard ``pdf_extraction`` names at its own offset
    computation — an offset taken before a deletion is wrong by however much came off above
    it.

    Doing it per block is equivalent rather than merely similar, and the third step is what
    makes it so: a block that neither begins nor ends with a newline cannot combine with the
    one- or two-newline separator to form the run of three that would otherwise have been
    collapsed across the boundary. Within a block the collapse is the same collapse. What is
    left for the join to do is nothing, so the offsets it hands out stand.
    """
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return _BLANK_RUN_RE.sub("\n\n", text).strip("\n")


def _assemble(blocks: list[tuple[str, Optional[dict]]]) -> tuple[str, list[dict]]:
    """``(markdown, source_blocks)`` for a sequence of ``(text, address)`` blocks.

    Blocks are separated by a blank line, EXCEPT two consecutive list items, which are
    separated by a single newline. A blank line between them is still a list to a markdown
    parser — a "loose" one — but it renders with paragraph spacing and doubles the
    whitespace in a document that is mostly bullets, which is most decks.

    ``address`` is ``{"part": ..., "path": ...}`` for a block some element produced and
    ``None`` for one this module wrote; either way the block gets an entry in
    ``source_blocks`` with its ``start`` and ``end``, because "no element accounts for this
    text" is a fact a later task needs and an omitted entry would read as "no text here".
    """
    kept = [(text, address) for text, address in blocks if text]
    if not kept:
        return "", []

    pieces: list[str] = []
    spans: list[dict] = []
    cursor = 0
    previous = ""
    for index, (text, address) in enumerate(kept):
        text = _normalize_block(text)
        if index:
            separator = (
                "\n" if _LIST_BLOCK_RE.match(previous) and _LIST_BLOCK_RE.match(text) else "\n\n"
            )
            pieces.append(separator)
            cursor += len(separator)
        spans.append({**(address or {}), "start": cursor, "end": cursor + len(text)})
        pieces.append(text)
        cursor += len(text)
        previous = text

    joined = "".join(pieces)
    stripped = joined.strip()
    if not stripped:
        return "", []
    # The only characters the join itself removes, and they come off the front — which is
    # the one place a span could still be wrong by a constant. Subtracting it is exact;
    # leaving it would put every offset in the section past the end of its own block.
    lead = len(joined) - len(joined.lstrip())
    limit = len(stripped)
    for span in spans:
        span["start"] = min(max(span["start"] - lead, 0), limit)
        span["end"] = min(max(span["end"] - lead, span["start"]), limit)
    return stripped + "\n", spans


# ---------------------------------------------------------------------------
# Addressing an element of the package — OFFICE_SPEC.md Part 5
# ---------------------------------------------------------------------------

#: The namespace URIs an anchor path may name, and the prefix it names them by.
#:
#: THREE, AND A FOURTH IS A BLOCK WITH NO ADDRESS RATHER THAN A PATH NOBODY CAN EVALUATE.
#: An XPath with an unbound prefix is not a worse address, it is not an address — a consumer
#: would either resolve it against the wrong namespace or fail, and both are worse than the
#: recorded absence Part 5 asks for. These three are the conventional OOXML prefixes and
#: every path this module writes is resolvable with exactly this mapping, which is why it is
#: here rather than carried on each anchor: it is a property of the vocabulary, not of the
#: document.
OOXML_PREFIXES: dict[str, str] = {
    "http://schemas.openxmlformats.org/wordprocessingml/2006/main": "w",
    "http://schemas.openxmlformats.org/presentationml/2006/main": "p",
    "http://schemas.openxmlformats.org/drawingml/2006/main": "a",
}


def _qname(element) -> Optional[str]:
    """``w:p`` for an lxml element, or ``None`` if its namespace is not one we may name."""
    tag = element.tag
    if not isinstance(tag, str) or not tag.startswith("{"):
        return None
    uri, _, local = tag[1:].partition("}")
    prefix = OOXML_PREFIXES.get(uri)
    return f"{prefix}:{local}" if prefix else None


def _element_path(element, root, *, absolute: bool) -> Optional[str]:
    """The XPath from ``root`` down to ``element``, or ``None`` if it cannot be written.

    Positional predicates are 1-based and count only siblings with the SAME name, which is
    what XPath's ``w:p[42]`` means — the 42nd ``w:p`` child, not the 42nd child. A body that
    interleaves paragraphs and tables therefore numbers each series independently, and that
    is the numbering a consumer evaluating the path against the part will reproduce.

    ``absolute`` writes the root's own name into the path (``/w:document/w:body/w:p[42]``),
    for a part whose root is the document. Relative writes ``./`` and starts below the root
    (``./p:cSld/p:spTree/p:sp[3]``), for a part — a slide — that is addressed by its own
    filename. Both forms are ``OFFICE_SPEC.md`` Part 5's, kept exactly.
    """
    segments: list[str] = []
    node = element
    while node is not root:
        name = _qname(node)
        parent = node.getparent()
        if name is None or parent is None:
            # No prefix for the namespace, or we ran out of ancestors without meeting
            # `root`. Either way the element is not addressable from here, and a path
            # written anyway would resolve against the wrong namespace or the wrong part.
            return None
        siblings = [child for child in parent if child.tag == node.tag]
        # The predicate is written only where it distinguishes something. `p:cSld` is the
        # only `p:cSld` its parent has, and Part 5's own examples write it bare.
        if len(siblings) > 1:
            segments.append(f"{name}[{_index_of(siblings, node)}]")
        else:
            segments.append(name)
        node = parent
    segments.reverse()
    if absolute:
        name = _qname(root)
        if name is None:
            return None
        return "/" + "/".join([name, *segments])
    return "./" + "/".join(segments) if segments else "."


def _index_of(siblings: list, node) -> int:
    """``node``'s 1-based position in ``siblings``, by identity rather than by equality.

    ``list.index`` compares with ``==``, and lxml elements compare by VALUE — two empty
    paragraphs are equal — so it would return the position of the first paragraph that
    looked like this one rather than of this one.
    """
    for position, sibling in enumerate(siblings, start=1):
        if sibling is node:
            return position
    raise ValueError("the element is not among the siblings it was looked up in")


def _address(part: str, path: Optional[str], **extra) -> Optional[dict]:
    """One block's address, or ``None`` when the path could not be written."""
    if path is None:
        return None
    return {"part": part, "path": path, **extra}


def _partname(part) -> str:
    """``word/document.xml`` from the package's own ``/word/document.xml``.

    Taken from the part rather than composed from an index, because the two can disagree:
    a deck's seventh slide is not obliged to be ``slide7.xml``, and a path that assumed it
    was would address a different slide of the same file.
    """
    return str(part.partname).lstrip("/")


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
    blocks: list[tuple[str, Optional[dict]]] = []
    # The PARENT passed to each proxy is the document, not the body element. A proxy
    # resolves `paragraph.style` through `parent.part`, and a raw lxml element has no
    # `.part` — passing the body raises AttributeError on the first styled paragraph.
    body = document.element.body
    part = _partname(document.part)
    root = document.element
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            block = _docx_paragraph_markdown(Paragraph(child, document), counts)
            if block:
                blocks.append((block, _address(part, _element_path(child, root, absolute=True))))
        elif child.tag == qn("w:tbl"):
            table = Table(child, document)
            rows = [[cell.text for cell in row.cells] for row in table.rows]
            lines = _table_markdown(rows)
            if lines:
                counts["tables"] += 1
                blocks.append(
                    ("\n".join(lines), _address(part, _element_path(child, root, absolute=True)))
                )

    markdown, source_blocks = _assemble(blocks)
    return markdown, {
        "paragraph_count": counts["paragraphs"],
        "heading_count": counts["headings"],
        "list_item_count": counts["list_items"],
        "table_count": counts["tables"],
        "source_blocks": source_blocks,
        "characters": len(markdown),
    }


# ---------------------------------------------------------------------------
# pptx
# ---------------------------------------------------------------------------


def _slide_address(part: str, root, number: int, element) -> Optional[dict]:
    """One slide element's address: the part it is in, its path, and the slide it is on.

    ``slide`` is 1-based and matches the ``Slide N`` headings this reader writes and the
    ``slide_offsets`` beside them. It is redundant with ``part`` only if the reader is
    assumed — the seventh slide of a deck is not obliged to be ``slide7.xml`` — so it is
    carried rather than derived, which is Part 5's own example shape.
    """
    return _address(part, _element_path(element, root, absolute=False), slide=number)


def _pptx_frame_markdown(
    text_frame, counts: dict, part: str, root, number: int
) -> list[tuple[str, Optional[dict]]]:
    """A text frame's paragraphs, as markdown blocks.

    ``paragraph.level`` is PowerPoint's outline depth within the placeholder, and it
    becomes list indentation rather than a heading level: the depth is a position inside
    one text box, not a position in the deck, and promoting it to a heading would put a
    second heading hierarchy underneath the per-slide one that
    ``structure:declared`` splits on.
    """
    blocks: list[tuple[str, Optional[dict]]] = []
    for paragraph in text_frame.paragraphs:
        text = paragraph.text.strip()
        if not text:
            continue
        counts["text_blocks"] += 1
        indent = "  " * max(0, paragraph.level)
        blocks.append(
            (
                f"{indent}- " + _LEADING_ATX_RE.sub(r"\\\1\2", text),
                # THE PARAGRAPH, NOT THE SHAPE. A block here is one `a:p`, and addressing
                # it by its shape would hand every bullet on the slide the same address —
                # an anchor that is right about which box and wrong about which line, which
                # is the resolution Part 5 spends its failure list refusing.
                _slide_address(part, root, number, paragraph._p),
            )
        )
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
    section_blocks: list[list[dict]] = []
    for number, slide in enumerate(presentation.slides, start=1):
        part = _partname(slide.part)
        root = slide.element
        title_shape = slide.shapes.title
        title = (title_shape.text or "").strip() if title_shape is not None else ""

        # Compared by ELEMENT, not by identity. `shapes.title` builds a fresh proxy on
        # every access, so `shape is title_shape` is never true and the title is written
        # twice — once as the heading and once as a bullet under it.
        title_element = title_shape.element if title_shape is not None else None

        # Where the heading's text really is, which is not always `title_element`: a
        # promoted title is ONE PARAGRAPH of a shape that goes on to hold others, and
        # addressing the whole shape would put the heading's anchor over lines that are not
        # in it. `title_element` keeps its own job — the shape the body must not repeat.
        title_anchor_element = title_element
        if not title:
            # No title placeholder, or an empty one. Promote the slide's first line of
            # text; `title_element` follows it so the body does not repeat it.
            for shape in slide.shapes:
                if not shape.has_text_frame:
                    continue
                first = next(
                    (p for p in shape.text_frame.paragraphs if p.text.strip()),
                    None,
                )
                if first is not None:
                    title = first.text.strip().split("\n")[0]
                    title_element = shape.element
                    title_anchor_element = first._p
                    counts["promoted_titles"] += 1
                    break

        # THE HEADING'S ADDRESS IS THE SHAPE THE TEXT CAME FROM, AND NOTHING WHERE THERE IS
        # NO SUCH SHAPE. `Slide N` is a label this reader wrote for a slide that named
        # itself nothing; no element of the package produced those characters, and giving
        # them the slide's own element would be an address for text that is not there.
        blocks: list[tuple[str, Optional[dict]]] = [
            (
                "# " + (title if title else f"Slide {number}"),
                (
                    _slide_address(part, root, number, title_anchor_element)
                    if title
                    else {NOT_RENDERED: True}
                ),
            )
        ]
        promoted_paragraph = (
            title if title_shape is None or not (title_shape.text or "").strip() else None
        )

        for shape in slide.shapes:
            if title_element is not None and shape.element is title_element:
                # The promoted shape may hold more than the one line that became the
                # heading, so its remaining paragraphs still belong in the body.
                if promoted_paragraph is not None and shape.has_text_frame:
                    rest = [
                        p
                        for p in shape.text_frame.paragraphs
                        if p.text.strip() and p.text.strip() != promoted_paragraph
                    ]
                    for paragraph in rest:
                        counts["text_blocks"] += 1
                        blocks.append(
                            (
                                "- " + _LEADING_ATX_RE.sub(r"\\\1\2", paragraph.text.strip()),
                                _slide_address(part, root, number, paragraph._p),
                            )
                        )
                continue
            if getattr(shape, "has_table", False):
                rows = [[cell.text for cell in row.cells] for row in shape.table.rows]
                lines = _table_markdown(rows)
                if lines:
                    counts["tables"] += 1
                    blocks.append(
                        ("\n".join(lines), _slide_address(part, root, number, shape.element))
                    )
                continue
            if shape.has_text_frame:
                blocks.extend(_pptx_frame_markdown(shape.text_frame, counts, part, root, number))

        # Speaker notes are authored text and are kept, under their own subheading so a
        # reader can tell what was on the slide from what was said beside it. Dropping
        # them would lose the half of many decks that carries the argument.
        if slide.has_notes_slide:
            notes = (slide.notes_slide.notes_text_frame.text or "").strip()
            if notes:
                counts["notes"] += 1
                # The notes live in their OWN PART, which is why the address is taken from
                # the notes slide rather than from the slide: a path into `slide7.xml` would
                # name whatever element happened to sit at that position on the slide.
                placeholder = slide.notes_slide.notes_placeholder
                notes_part = _partname(slide.notes_slide.part)
                notes_root = slide.notes_slide.element
                # `## Notes` is this reader's label, like `Slide N` above, and gets no
                # address for the same reason.
                blocks.append(("## Notes", {NOT_RENDERED: True}))
                address = (
                    _slide_address(notes_part, notes_root, number, placeholder.element)
                    if placeholder is not None
                    else None
                )
                blocks.append(
                    (_LEADING_ATX_RE.sub(r"\\\1\2", notes), {**(address or {}), NOT_RENDERED: True})
                )

        section, section_span = _assemble(blocks)
        sections.append(section)
        section_blocks.append(section_span)

    # Offsets are computed against the joined text rather than accumulated while building
    # it, so they cannot disagree with what `_assemble` actually produced.
    markdown = ""
    offsets: list[int] = []
    source_blocks: list[dict] = []
    for section, spans in zip(sections, section_blocks):
        if markdown:
            markdown += "\n"
        offsets.append(len(markdown))
        # Each section's spans are its own, so the join shifts them by exactly where the
        # section landed. Taken from the same `len(markdown)` the offset above is, rather
        # than accumulated separately, so the two cannot come to disagree.
        source_blocks.extend(
            {**span, "start": span["start"] + len(markdown), "end": span["end"] + len(markdown)}
            for span in spans
        )
        markdown += section

    return markdown, {
        "slide_count": len(sections),
        "slide_offsets": offsets,
        "source_blocks": source_blocks,
        "text_block_count": counts["text_blocks"],
        "table_count": counts["tables"],
        "notes_slide_count": counts["notes"],
        # How many slides had no usable title placeholder and took their first line of
        # text instead. Reported because it is the difference between a heading somebody
        # wrote and one this reader chose.
        "promoted_title_count": counts["promoted_titles"],
        "characters": len(markdown),
    }
