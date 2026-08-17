"""Structural splitting — finding a document's own sections.

Step 1 of the 4-step tree-building pipeline:
    structural split → chunk → PELT group → summarize

Two sources of structure, which are the top two rungs of ``INGEST_SPEC.md`` 3.5:

``split_on_outline`` — the DECLARED rung. The document states its own structure and we
take it as given, including where it is wrong. A PDF outline that files section 3 under
section 2 produces a tree that files section 3 under section 2. Rebuilding the hierarchy
from the numbering in the titles would correct that particular case and would itself be a
heuristic, applied on top of freeform input, failing differently on unnumbered documents.
The text of every section is retrievable either way; only a rollup over the tree is
affected, and that is repairable by a person who can see it.

``split_on_headings`` — the INFERRED rung for PDF, where the ``#`` markers come from
`pdf_extraction`'s font-size analysis, and the declared rung for markdown, where the
author typed them. Used when there is no outline at all (6 of 26 papers in a working
corpus).

``nest`` turns either one's flat, levelled output into the tree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional, Sequence

# Matches ATX headings: 1–6 leading '#' characters, then at least one space, then text.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)

#: Markdown emphasis and heading punctuation, dropped when comparing an outline title
#: against the extracted text. `pdf_extraction` writes `**Bold Title**` and `# Heading`;
#: the outline entry for both is the bare words.
_DECORATION = re.compile(r"[*`#]")
_WHITESPACE = re.compile(r"\s+")


@dataclass
class Section:
    """A contiguous section delimited by a markdown heading."""

    title: str  # heading text (without '#' prefix)
    level: int  # heading level 1–6
    content: str  # body text *below* the heading (may be empty)
    source_line: int  # 0-based line index of the heading in the original document


def find_headings(text: str) -> list[tuple[int, str]]:
    """Every ATX heading in ``text``, as ``(level, title)`` in document order.

    Exists so that the text prober and :func:`split_on_headings` count the same things.
    ``probe`` reports ``heading_count``; the structure task reports how many titled
    sections it produced; ``INGEST_SPEC.md`` 11.3 makes the two numbers a paired
    measurement, where a disagreement is the signal that an extraction mangled the
    document. Two regexes that were meant to match would be free to drift apart, and the
    drift would show up as a false alarm rather than as the real one it is for.
    """
    return [(len(match.group(1)), match.group(2).strip()) for match in _HEADING_RE.finditer(text)]


def split_on_headings(text: str) -> list[Section]:
    """Split markdown text into sections on ATX heading boundaries.

    If the document contains no headings the full text is returned as a
    single section with ``title=""`` and ``level=0``.

    Leading content before the first heading (if any) is captured as a
    preamble section with ``level=0``.

    Args:
        text: Markdown document content.

    Returns:
        Ordered list of sections.
    """
    if not text or not text.strip():
        return []

    matches = list(_HEADING_RE.finditer(text))

    if not matches:
        # No headings — pass through as a single section
        return [Section(title="", level=0, content=text.strip(), source_line=0)]

    sections: list[Section] = []

    # Capture preamble (content before the first heading)
    preamble = text[: matches[0].start()].strip()
    if preamble:
        sections.append(
            Section(
                title="",
                level=0,
                content=preamble,
                source_line=0,
            )
        )

    for i, match in enumerate(matches):
        level = len(match.group(1))
        title = match.group(2).strip()
        source_line = text[: match.start()].count("\n")

        # Body runs from end of this heading line to start of next heading (or EOF)
        body_start = match.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        content = text[body_start:body_end].strip()

        sections.append(
            Section(
                title=title,
                level=level,
                content=content,
                source_line=source_line,
            )
        )

    return sections


# ---------------------------------------------------------------------------
# The declared rung: the document's own outline
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OutlineSplit:
    """What ``split_on_outline`` made of a document's declared structure."""

    sections: list[Section]
    #: Outline titles that could not be found in the text. Their text is not lost — it
    #: stays inside the preceding section — but the entry they declared has no node, and
    #: an audit of the tree against the outline needs to see which ones.
    unplaced: list[str] = field(default_factory=list)


def _normalized_with_map(text: str) -> tuple[str, list[int]]:
    """``(normalized, offsets)`` where ``offsets[i]`` is where ``normalized[i]`` came from.

    Comparing an outline title to the extracted text needs both halves normalised —
    emphasis markers dropped, runs of whitespace collapsed, case folded — but the section
    boundary has to be a real offset into the ORIGINAL text. Carrying the map is what
    makes a match in normalised space usable in the text the reader gets.
    """
    out: list[str] = []
    offsets: list[int] = []
    for i, char in enumerate(text):
        if _DECORATION.match(char):
            continue
        if char.isspace():
            if out and out[-1] == " ":
                continue
            out.append(" ")
        else:
            out.append(char.lower())
        offsets.append(i)
    return "".join(out), offsets


def _normalize(text: str) -> str:
    return _WHITESPACE.sub(" ", _DECORATION.sub("", text)).strip().lower()


def split_on_outline(
    text: str,
    toc: Sequence[Sequence],
    page_offsets: Optional[Sequence[int]] = None,
) -> OutlineSplit:
    """Split ``text`` at the titles the document's own outline declares.

    Args:
        text: The extracted document text.
        toc: PyMuPDF's outline — ``[[level, title, page], ...]``, ``level`` and ``page``
            both 1-based. Levels are used AS GIVEN; see the module docstring.
        page_offsets: Where each page starts in ``text``
            (``pdf_to_markdown``'s metadata). Without it a title is searched from the end
            of the previous section, which places sections inside a printed
            table-of-contents page on the 9-of-20 papers that have one.

    Returns:
        The sections in document order, plus the titles that could not be located.

    Sections are bounded by the NEXT located title, so the last one runs to the end of
    the document. There is no "after the last section": appendices and references carry
    their own outline entries when the document declares them, and belong to the section
    they follow when it does not.

    Text before the first located title — a title page, an abstract — becomes a section
    with an empty title at level 0. A document whose outline opens with an entry pointing
    at offset 0 has no such text, and gets no such section.
    """
    if not text or not text.strip():
        return OutlineSplit(sections=[], unplaced=[])

    normalized, offsets = _normalized_with_map(text)

    located: list[tuple[int, int, int, str]] = []  # (start, end, level, title)
    unplaced: list[str] = []
    search_from = 0  # in normalized coordinates

    for entry in toc:
        if len(entry) < 2:
            continue
        level, title = int(entry[0]), str(entry[1])
        page = int(entry[2]) if len(entry) > 2 else 0
        needle = _normalize(title)
        if not needle:
            continue

        lower_bound = search_from
        if page_offsets and 1 <= page <= len(page_offsets):
            # The outline says which page the section starts on. Anything earlier is
            # some other occurrence of the words — most often the contents listing.
            page_start = page_offsets[page - 1]
            while lower_bound < len(offsets) and offsets[lower_bound] < page_start:
                lower_bound += 1

        found = normalized.find(needle, lower_bound)
        if found < 0:
            unplaced.append(title)
            continue

        start = offsets[found]
        end_index = found + len(needle) - 1
        end = offsets[end_index] + 1 if end_index < len(offsets) else len(text)
        # The match is in normalized space, where the markup around the heading does not
        # exist. Widen it back over that markup in the real text, or `# **Title**` leaves
        # `# **` behind as a spurious preamble and `**` at the head of the section.
        # Only markup and same-line spacing — a newline ends the widening, so the
        # preceding paragraph is never eaten.
        while start > 0 and text[start - 1] in "*`# \t":
            start -= 1
        while end < len(text) and text[end] in "*`":
            end += 1
        located.append((start, end, level, title))
        search_from = found + len(needle)

    if not located:
        return OutlineSplit(
            sections=[Section(title="", level=0, content=text.strip(), source_line=0)],
            unplaced=unplaced,
        )

    sections: list[Section] = []
    preamble = text[: located[0][0]].strip()
    if preamble:
        sections.append(Section(title="", level=0, content=preamble, source_line=0))

    for i, (start, end, level, title) in enumerate(located):
        body_end = located[i + 1][0] if i + 1 < len(located) else len(text)
        sections.append(
            Section(
                title=title.strip(),
                level=level,
                content=text[end:body_end].strip(),
                source_line=text.count("\n", 0, start),
            )
        )

    return OutlineSplit(sections=sections, unplaced=unplaced)


# ---------------------------------------------------------------------------
# Flat, levelled sections -> a tree
# ---------------------------------------------------------------------------


@dataclass
class SectionNode:
    """A section and the sections declared underneath it."""

    section: Section
    children: list["SectionNode"] = field(default_factory=list)


def nest(sections: Sequence[Section]) -> list[SectionNode]:
    """Build the tree the levels describe, taking them as given.

    A section attaches to the nearest preceding section of a LOWER level. That rule
    reproduces whatever the source declared, including a level 3 that follows a level 2
    without a level 2 in between — the document says they nest, so they nest.

    Level 0 is the preamble (see :func:`split_on_outline`) and the whole document when
    there is no structure at all. It is always a root and never a parent: an abstract
    does not contain the paper.
    """
    roots: list[SectionNode] = []
    stack: list[SectionNode] = []

    for section in sections:
        node = SectionNode(section=section)
        if section.level <= 0:
            stack.clear()
            roots.append(node)
            continue
        while stack and stack[-1].section.level >= section.level:
            stack.pop()
        if stack:
            stack[-1].children.append(node)
        else:
            roots.append(node)
        stack.append(node)

    return roots
