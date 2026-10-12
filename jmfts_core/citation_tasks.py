"""Where a chunk came from. ``OFFICE_SPEC.md`` Part 5, and ALL THREE of the tasks that say.

An **anchor** is a stable address of a region of the source document, recorded on the node
that region produced. It is the ``source_anchor`` evidence row, and for a PDF it is::

    {"kind": "pdf", "page": 3, "bbox": [72.0, 118.4, 540.0, 262.9]}

and for an office package, since Part 11 step 9, it is::

    {"kind": "ooxml", "part": "word/document.xml", "path": "/w:document/w:body/w:p[42]"}

TWO TASKS AND ONE ROW. ``citation`` writes the first and ``anchor:source`` the second, and
a document reaches exactly one of them: a PDF has pages and no package, an office file has a
package and — until something paginates it — no pages. That is Part 5's "Two anchors, not
one" read from the ingest side.

THEN A THIRD TASK AND A SECOND ROW, which is Part 11 step 8 and the rest of this module::

    {"kind": "pdf", "of": "rendition", "page": 2, "bbox": [72.0, 118.4, 540.0, 262.9]}

``anchor:render`` writes it, onto the ``render_anchor`` row, and it is a rectangle RECOVERED
by matching a passage's own text against the PDF LibreOffice made of the document
(``render:pdf``, Part 6). **It is a second row and not a second writer of the first one**,
and both halves of that matter. A ``.docx`` carries both at once, so one key would have this
task silently overwrite ``anchor:source``'s address — the two are ``subtree``-scoped over the
same chunks. And the facts differ in kind: an address into the uploaded package cannot be
wrong, while a rectangle recovered out of somebody's pagination can be, and can fail to be
computed at all. Part 5: "Storing both under one key would make a recovered rectangle
indistinguishable from an exact one."

``page`` is 0-based, matching ``page_offsets`` and ``pages_with_tables`` in the same
extraction record; ``bbox`` is ``[x0, y0, x1, y1]`` in PDF points, which is what
``pymupdf`` takes back when Part 7's ``/image`` and ``/region`` verbs come to draw it.

**Why this is a task of its own rather than a field extraction fills in.** Part 5 says so,
and meeting the code agrees with it for a reason worth writing down: extraction's job is
text, and it is already the most format-specific code in the tree. Position recovery folded
into it would mean every extractor growing a second responsibility, and a change to how
rectangles are found touching all of them. As its own task it is one handler with its own
retry budget, its own routing badge, and its own line in the attempt log — and, once office
formats arrive (Part 5's "How a rectangle is recovered"), one place where the *recovery
method* differs by format while the anchor does not.

There was one concrete thing that could have forced the other design, and it did not.
Extraction is the only pass that holds the geometry, so writing the anchor here means
either persisting that geometry on the node or re-deriving it. Persisting it is what the
node cannot afford — tens of thousands of rectangles for a long book, in a JSONB column
every read of the node loads — so this task re-derives it, by calling the same
``pdf_to_markdown`` on the same stored bytes. That is one extra parse per document, once,
off the query path, and it buys a handler that owns its inputs end to end.

**It is best-effort, and non-blocking is the mechanism that makes that true.**
``citation`` is in :data:`~jmfts_core.models.task_queue.ADVISORY_TASK_TYPES`, so a
permanent failure records the attempt and does not put the file node into
``settled='failed'``. A document whose rectangles could not be recovered is a perfectly
good searchable document; failing it would throw away the extraction, the chunks and the
vectors over a missing convenience.

**Partial recovery is a COMPLETED task, not a failed one.** A chunk whose rectangle cannot
be resolved gets no anchor and a recorded reason, and the counts go in the attempt detail —
which is what makes the recovery rate a number Part 10 can decide on rather than an
impression. What DOES fail the task is being unable to do the job at all: no stored bytes,
an extraction record naming a source this handler cannot read, or a re-parse that disagrees
with the text the chunks were cut from.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from typing import Callable, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from jmfts_core.atoms import (
    COST_CPU,
    EV_BLOB,
    EV_EXTRACTION,
    EV_RENDER_ANCHOR,
    EV_RENDITION,
    EV_SOURCE_ANCHOR,
    EV_SOURCE_SPAN,
    EV_TEXT,
)
from jmfts_core.ingest_tasks import (
    TASK_ANCHOR_RENDER,
    TASK_ANCHOR_SOURCE,
    TASK_CITATION,
    TaskOutcome,
    register_task_handler,
)
from jmfts_core.models.document import Document, USETYPE_CHUNK
from jmfts_core.models.task_queue import WRITE_SUBTREE, TaskQueue

# The office readers, at module scope and safe there: `jmfts_core.office.extract` imports
# python-docx and python-pptx inside its functions, behind the `require_*` guards, so this
# costs a base install nothing. `structure_tasks` imports the same two names the same way.
from jmfts_core.office.extract import docx_to_markdown, pptx_to_markdown
from jmfts_core.pdf_extraction import pdf_to_markdown
from jmfts_core.repositories.blob import BlobRepository
from jmfts_core.repositories.evidence import EvidenceRepository
from jmfts_core.repositories.rendition import EVIDENCE_NAME, KIND_PDF, pdf_for
from jmfts_core.structure_tasks import (
    EXTRACTION_DOCX_BODY,
    EXTRACTION_PDF_TEXT_LAYER,
    EXTRACTION_PPTX_SLIDES,
)

#: ``anchor.kind`` for a rectangle on a PDF page. An open string for the same reason
#: ``usetype`` is (spec Part 9) — ``cells`` is the third Part 5 name and belongs to the
#: sheet tasks.
ANCHOR_KIND_PDF = "pdf"

#: ``anchor.kind`` for an element of an OOXML package: ``{"kind": "ooxml", "part":
#: "word/document.xml", "path": "/w:document/w:body/w:p[42]"}``, Part 5's second example.
#:
#: **THIS IS THE SOURCE ANCHOR AND NOT A SUBSTITUTE FOR A RECTANGLE.** Part 5's "Two
#: anchors, not one" says why they are different objects: this one is exact and permanent
#: and addresses the bytes that were uploaded; a rectangle on a page of a rendition is
#: approximate, depends on which LibreOffice paginated the file, and can fail to be computed
#: at all. Part 11 step 8 is where the second one arrives, and it must be recorded
#: SEPARATELY — "storing both under one key would make a recovered rectangle
#: indistinguishable from an exact one".
ANCHOR_KIND_OOXML = "ooxml"

#: Where the anchor lives on a chunk node, and where the reason lives when there is none.
#: TWO KEYS, NEVER ONE WITH A NULL: "this passage is at page 3, rectangle R" and "this
#: passage could not be placed, because X" are different facts, and a consumer that has to
#: tell them apart by testing for null gets no X to report.
#: The registry names, not the column keys these were before Phase 2b. `anchor` became
#: `source_anchor` and `anchor_unresolved` became `source_anchor.unresolved` on the way out
#: of `structured_content` — 2.5's finding 5, evidence is named by what it asserts. The dot
#: in the second is part of a name and not a path into the first: they are two rows, which
#: is `SPRINT_JOBS.md` 3.2's "two keys, never one with a null".
ANCHOR_NAME = "source_anchor"
ANCHOR_UNRESOLVED_NAME = "source_anchor.unresolved"

#: The chunk was written before ``source_span`` existed, or its offset into the extracted
#: markdown could not be verified (see ``_TreeWriter._verified_span``). Either way there is
#: no character range to invert, and inventing one from a text search would be the
#: plausible-rectangle-around-the-wrong-words failure Part 5 rejects.
UNRESOLVED_NO_SPAN = "no_source_span"

#: The chunk's span lands in no extracted text block. Reachable when the span covers only
#: the whitespace between blocks, which the chunker's strip should prevent — so this is the
#: code to look at if it ever appears in quantity.
UNRESOLVED_NO_BLOCK = "no_overlapping_block"

#: The chunk's span lands only in text the READER wrote. A ``# Slide 7`` heading for a slide
#: that named itself nothing, or the ``## Notes`` label above a notes section: real text in
#: the markdown, produced by no element of the package. Distinct from
#: :data:`UNRESOLVED_NO_BLOCK`, which says the span overlaps no measured block at all — this
#: one says the blocks are there and none of them has an address.
UNRESOLVED_NO_ELEMENT = "no_addressable_element"

UNRESOLVED_REASON: dict[str, str] = {
    UNRESOLVED_NO_SPAN: (
        "the chunk carries no verified `source_span`, so there is no character range in "
        "the extracted markdown to invert back into a source address"
    ),
    UNRESOLVED_NO_BLOCK: (
        "the chunk's `source_span` overlaps none of the text blocks the extraction "
        "measured, so nothing in the source accounts for it"
    ),
    UNRESOLVED_NO_ELEMENT: (
        "the chunk covers only text the reader wrote — a slide heading it chose, a `Notes` "
        "label — which no element of the package produced, so there is nothing in the "
        "source to address"
    ),
}


def overlapping_blocks(
    blocks: Sequence[dict], starts: Sequence[int], start: int, end: int
) -> list[dict]:
    """The blocks the markdown range ``[start, end)`` touches, in document order.

    Shared by both anchor handlers because the arithmetic is the same arithmetic — a block
    map is a block map whether its entries carry a rectangle or an XPath, and two copies of
    a half-open overlap test are two chances to disagree about a chunk that begins exactly
    where a block ends. What differs between the two callers is what they build from the
    result, which is where the formats genuinely differ.
    """
    # `bisect_right` finds the first block starting after `start`; the block before it is
    # the one that may CONTAIN `start`, which is the common case for a chunk cut out of the
    # middle of a paragraph.
    index = max(bisect_right(starts, start) - 1, 0)
    found: list[dict] = []
    while index < len(blocks) and blocks[index]["start"] < end:
        block = blocks[index]
        if min(end, block["end"]) > max(start, block["start"]):
            found.append(block)
        index += 1
    return found


def anchor_for_span(
    blocks: Sequence[dict], starts: Sequence[int], start: int, end: int
) -> tuple[Optional[dict], Optional[str]]:
    """``(anchor, unresolved_code)`` for the markdown range ``[start, end)``.

    ``blocks`` is ``pdf_to_markdown``'s ``text_blocks`` — one entry per emitted markdown
    segment, in document order, each with the page and rectangle it was set in — and
    ``starts`` is their ``start`` offsets, hoisted out so the scan is a bisect rather than a
    walk of the whole document per chunk.

    THE PAGE IS THE PAGE THE PASSAGE BEGINS ON, and the rectangle is the union of the
    segments it covers **on that page only**. A chunk that runs past a page break is
    ordinary — the chunker packs to a word budget and knows nothing about pagination — and
    it is not a failure to recover: the reader who clicks a citation wants to be taken to
    where the passage starts. What would be a failure is handing back a rectangle that
    looked complete when it was not, so an anchor whose passage continues overleaf carries
    ``continues``, the further page numbers in order.

    That key is an addition to the shape Part 5 writes, and it is here because meeting the
    code showed the shape to be short by one fact. Part 5's failure list — text found twice,
    text not found, chunk spans a page break — is written about recovering an office
    document's rectangle by SEARCHING a rendition, where a chunk that straddles a break
    genuinely cannot be resolved to one rectangle. For a source PDF nothing is searched: the
    span is exact, the blocks it covers are known, and which of them fall on the first page
    is known too. Declining to anchor those chunks would discard a correct answer, and
    anchoring them silently would make a partial rectangle indistinguishable from a complete
    one — which is the distinction the rest of Part 5 is most careful about.
    """
    overlapping = overlapping_blocks(blocks, starts, start, end)
    if not overlapping:
        return None, UNRESOLVED_NO_BLOCK

    page = overlapping[0]["page"]
    boxes = [block["bbox"] for block in overlapping if block["page"] == page]
    anchor = {
        "kind": ANCHOR_KIND_PDF,
        "page": page,
        "bbox": [
            min(box[0] for box in boxes),
            min(box[1] for box in boxes),
            max(box[2] for box in boxes),
            max(box[3] for box in boxes),
        ],
    }
    continues = sorted({block["page"] for block in overlapping} - {page})
    if continues:
        anchor["continues"] = continues
    return anchor, None


# THIS IS THE ROW THE PHASE 1 AUDIT WAS WATCHING. `SPRINT_JOBS.md` 14.1 predicted it and
# `tests/test_atom_declarations.py` records the result: `source_span@subtree` is consumed,
# the two structure rungs produce it at `@subtree`, and 2.2 says a `@subtree` consumption
# is enforced by the settling walk rather than by a within-node edge. So the declarations
# derive NO edge here, while `TASK_CITATION`'s row states `after_any` by hand — the one
# place in the table where the derivation and the hand-written field disagree.
#
# The disagreement is the finding, not a defect: under the walk this atom stops being a
# probe-time batch member and becomes a rule that fires when the file node is evaluated.
# Phase 3 makes that change; nothing here does it yet.
#
# No fan-out and no child key. This writes anchors ONTO chunks that already exist and
# creates nothing, which is also why `ADVISORY_TASK_TYPES` can hold it: a chunk with no
# anchor is still a chunk.
@register_task_handler(
    TASK_CITATION,
    consumes=(
        f"{EV_EXTRACTION}@self",
        f"{EV_TEXT}@self",
        f"{EV_BLOB}@self",
        f"{EV_SOURCE_SPAN}@subtree",
    ),
    produces=(f"{EV_SOURCE_ANCHOR}@subtree",),
    write_mode=WRITE_SUBTREE,
    cost_class=COST_CPU,
)
def run_citation(session: Session, task: TaskQueue) -> TaskOutcome:
    """Put a page and a rectangle on every chunk under this file node.

    The file node's own bytes are re-read and re-extracted, and the result is compared
    against the ``content`` the chunks were cut from. That comparison is not a formality: an
    anchor is a character offset inverted through a block map, so a block map derived from
    different text than the chunks were cut from would produce rectangles that are wrong in
    a way nothing downstream could detect. Text that disagrees means the blob changed under
    the node, or that extraction ran under different parameters than this task did, and both
    are conditions to raise on rather than to anchor through.
    """
    doc = session.get(Document, task.scope_document_id)
    if doc is None:
        raise ValueError(
            f"citation is scoped to document {task.scope_document_id}, which does not exist"
        )

    extraction = EvidenceRepository(session).read(doc.id, "extraction") or {}
    source = extraction.get("source")
    if source != EXTRACTION_PDF_TEXT_LAYER:
        raise ValueError(
            f"citation is scoped to document {doc.id}, whose extraction record names source "
            f"{source!r}; this handler recovers rectangles from a PDF text layer. Every "
            "other format reaches the same anchor through a rendition, which is "
            "OFFICE_SPEC.md Part 11 step 8 and has no code here yet"
        )

    data = BlobRepository(session).read_bytes(doc.id)
    if data is None:
        raise ValueError(
            f"document {doc.id} has no stored blob; citation was enqueued for bytes that "
            "are no longer there"
        )

    markdown, meta = pdf_to_markdown(data)
    content = doc.content or ""
    if markdown != content:
        raise ValueError(
            f"citation re-extracted document {doc.id} and got {len(markdown)} characters "
            f"where the node holds {len(content)}; the block map and the chunks would then "
            "be indexed against different text, and every rectangle derived from it would "
            "be confidently wrong"
        )

    blocks = meta["text_blocks"]
    starts = [block["start"] for block in blocks]

    chunks = (
        session.execute(
            select(Document)
            .where(Document.path.contains([doc.id]))
            .where(Document.usetype == USETYPE_CHUNK)
            .order_by(Document.id)
        )
        .scalars()
        .all()
    )

    anchored = 0
    continued = 0
    unresolved: dict[str, int] = {}
    pages: set[int] = set()

    evidence = EvidenceRepository(session)
    for chunk in chunks:
        # Both rows are deleted first so that a RE-RUN cannot leave last run's answer
        # standing beside this run's. Spec 6.1 makes re-running a first-class operation,
        # and a chunk holding a `source_anchor` from a superseded extraction next to a
        # `source_anchor.unresolved` from this one is a node that says two things.
        # DELETE, not a null: 3.2 makes those different facts, and "this run has not
        # decided yet" is neither of them.
        evidence.delete(chunk.id, ANCHOR_NAME)
        evidence.delete(chunk.id, ANCHOR_UNRESOLVED_NAME)

        span = evidence.read(chunk.id, "source_span")
        if isinstance(span, list) and len(span) == 2:
            anchor, code = anchor_for_span(blocks, starts, span[0], span[1])
        else:
            anchor, code = None, UNRESOLVED_NO_SPAN

        if anchor is not None:
            evidence.write(chunk.id, ANCHOR_NAME, anchor)
            anchored += 1
            pages.add(anchor["page"])
            if "continues" in anchor:
                continued += 1
        else:
            evidence.write(
                chunk.id,
                ANCHOR_UNRESOLVED_NAME,
                {"code": code, "reason": UNRESOLVED_REASON[code]},
            )
            unresolved[code] = unresolved.get(code, 0) + 1

    session.flush()

    return TaskOutcome(
        detail={
            # The pair that makes the recovery rate readable without arithmetic on the tree
            # — `chunks` is what was there to anchor, `anchored` is what got one, and
            # `unresolved` says why the difference, per reason, rather than as one number.
            "chunks": len(chunks),
            "anchored": anchored,
            "unresolved": unresolved,
            # An anchored chunk whose passage runs past a page break. Reported separately
            # because its rectangle is a PARTIAL one (see `anchor_for_span`), and a rate
            # worth watching: it is the number Part 5's tagged-PDF alternative would move.
            "continues_overleaf": continued,
            "pages_anchored": len(pages),
            "text_blocks": len(blocks),
            "page_count": meta["page_count"],
        }
    )


# ---------------------------------------------------------------------------
# The SOURCE anchor, for an office package — OFFICE_SPEC.md Part 5, Part 11 step 9
# ---------------------------------------------------------------------------

#: extraction source -> the reader whose ``source_blocks`` this handler inverts. The keys
#: are the same constants ``structure_tasks`` dispatches its structure rungs on, so a format
#: that grows a reader and forgets this table gets a named refusal rather than no anchors.
OOXML_READERS: dict[str, Callable[[bytes], tuple[str, dict]]] = {
    EXTRACTION_DOCX_BODY: docx_to_markdown,
    EXTRACTION_PPTX_SLIDES: pptx_to_markdown,
}


def anchor_for_ooxml_span(
    blocks: Sequence[dict], starts: Sequence[int], start: int, end: int
) -> tuple[Optional[dict], Optional[str]]:
    """``(anchor, unresolved_code)`` for the markdown range ``[start, end)`` in a package.

    ``blocks`` is an office reader's ``source_blocks`` — one entry per emitted markdown
    block, in document order, carrying ``start``/``end`` and, for a block some element
    produced, the ``part`` and ``path`` of that element.

    THE PATH IS THE FIRST ADDRESSED BLOCK THE PASSAGE TOUCHES, which is the rule
    :func:`anchor_for_span` applies to pages and for the same reason: the reader who clicks
    a citation wants to be taken to where the passage starts. A chunk packs to a word
    budget and knows nothing about paragraph boundaries, so covering several elements is
    ordinary rather than a failure — and saying so is what keeps a partial address
    distinguishable from a whole one:

    * ``through`` is the last addressed block in the SAME part, present only when the
      passage runs past its first element. ``path`` and ``through`` together bound the run
      of that part's elements the passage occupies.
    * ``continues`` is the further parts, in order, for a passage that leaves the one it
      began in — a chunk that runs from a slide into its speaker notes, which are a
      different part of the package. The analogue of a PDF anchor's further pages.

    A block the reader wrote — a ``Slide N`` heading, a ``Notes`` label — carries no path,
    and a passage made only of those resolves to :data:`UNRESOLVED_NO_ELEMENT` rather than
    to the nearest real element. That is the Part 5 rule this function exists to keep: an
    address that is probably right is worse than a recorded absence, because everything
    downstream would draw it either way.
    """
    overlapping = overlapping_blocks(blocks, starts, start, end)
    if not overlapping:
        return None, UNRESOLVED_NO_BLOCK

    addressed = [block for block in overlapping if block.get("path")]
    if not addressed:
        return None, UNRESOLVED_NO_ELEMENT

    first = addressed[0]
    anchor = {"kind": ANCHOR_KIND_OOXML, "part": first["part"], "path": first["path"]}
    if "slide" in first:
        anchor["slide"] = first["slide"]

    same_part = [block for block in addressed if block["part"] == first["part"]]
    if same_part[-1]["path"] != first["path"]:
        anchor["through"] = same_part[-1]["path"]

    continues: list[str] = []
    for block in addressed:
        if block["part"] != first["part"] and block["part"] not in continues:
            continues.append(block["part"])
    if continues:
        anchor["continues"] = continues
    return anchor, None


def shift_for_removed(text: str, blocks: Sequence[dict], removed: str) -> tuple[str, list[dict]]:
    """``(text, blocks)`` with every ``removed`` character gone and every offset moved with it.

    ``extract:text`` strips NUL from a reader's output before it writes the node's content
    (``structure_tasks._without_nul``), and stripping DELETES characters — so a block map
    measured against the unstripped markdown is wrong by however many were removed above
    each block. It is the hazard ``pdf_extraction`` documents at its own offset computation,
    met here from the other side: this handler re-runs the reader and therefore sees the
    unstripped text, while the chunks were cut from the stripped one.

    Re-deriving the offsets rather than refusing the document, because the arithmetic is
    exact: the removed positions are known and counting how many precede an offset is a
    bisect. Refusing would throw away every address in a document over a padding artefact
    that was never part of it.
    """
    if removed not in text:
        return text, list(blocks)
    positions = [index for index, char in enumerate(text) if char == removed]
    moved = [
        {
            **block,
            "start": block["start"] - bisect_right(positions, block["start"] - 1),
            "end": block["end"] - bisect_right(positions, block["end"] - 1),
        }
        for block in blocks
    ]
    return text.replace(removed, ""), moved


# `consumes` and `produces` are `citation`'s, exactly, and the identity is the point: the
# two tasks write the same evidence about the same nodes and differ only in how they recover
# it. So `EXPECTED_DIVERGENCE` in `tests/test_atom_declarations.py` carries the same entry
# for this task as for that one — `source_span@subtree` is a settling-walk dependency rather
# than a within-node edge (SPRINT_JOBS.md 2.2, 14.1) — and it is not a second finding.
@register_task_handler(
    TASK_ANCHOR_SOURCE,
    consumes=(
        f"{EV_EXTRACTION}@self",
        f"{EV_TEXT}@self",
        f"{EV_BLOB}@self",
        f"{EV_SOURCE_SPAN}@subtree",
    ),
    produces=(f"{EV_SOURCE_ANCHOR}@subtree",),
    write_mode=WRITE_SUBTREE,
    cost_class=COST_CPU,
)
def run_anchor_source(session: Session, task: TaskQueue) -> TaskOutcome:
    """Put the part and the element a passage came from on every chunk under this file node.

    The same shape as :func:`run_citation` and for the same reasons — the bytes are re-read
    and re-extracted rather than a block map being persisted on the node, and the result is
    compared against the ``content`` the chunks were cut from, because a block map derived
    from different text than the chunks were cut from would produce addresses that are wrong
    in a way nothing downstream could detect.

    It is a SEPARATE handler because the two anchors are separate objects. See
    :data:`~jmfts_core.ingest_tasks.TASK_ANCHOR_SOURCE` for the argument; the short version
    is that one is exact and the other is recovered, and a document reaches exactly one of
    them from its own bytes.
    """
    doc = session.get(Document, task.scope_document_id)
    if doc is None:
        raise ValueError(
            f"anchor:source is scoped to document {task.scope_document_id}, which does not exist"
        )

    extraction = EvidenceRepository(session).read(doc.id, "extraction") or {}
    source = extraction.get("source")
    reader = OOXML_READERS.get(source)
    if reader is None:
        raise ValueError(
            f"anchor:source is scoped to document {doc.id}, whose extraction record names "
            f"source {source!r}; this handler addresses elements of an OOXML package, which "
            f"is {sorted(OOXML_READERS)}. A PDF's source anchor is its rectangle and "
            "`citation` writes it; every other format reaches a rectangle through a "
            "rendition, which is OFFICE_SPEC.md Part 11 step 8"
        )

    data = BlobRepository(session).read_bytes(doc.id)
    if data is None:
        raise ValueError(
            f"document {doc.id} has no stored blob; anchor:source was enqueued for bytes "
            "that are no longer there"
        )

    markdown, meta = reader(data)
    markdown, blocks = shift_for_removed(markdown, meta["source_blocks"], "\x00")
    content = doc.content or ""
    if markdown != content:
        raise ValueError(
            f"anchor:source re-extracted document {doc.id} and got {len(markdown)} "
            f"characters where the node holds {len(content)}; the block map and the chunks "
            "would then be indexed against different text, and every address derived from "
            "it would be confidently wrong"
        )

    starts = [block["start"] for block in blocks]

    chunks = (
        session.execute(
            select(Document)
            .where(Document.path.contains([doc.id]))
            .where(Document.usetype == USETYPE_CHUNK)
            .order_by(Document.id)
        )
        .scalars()
        .all()
    )

    anchored = 0
    continued = 0
    unresolved: dict[str, int] = {}
    parts: set[str] = set()

    evidence = EvidenceRepository(session)
    for chunk in chunks:
        # Both rows first, for the reason `run_citation` gives: a chunk holding an anchor
        # from a superseded extraction beside an unresolved row from this run is a node that
        # says two things, and 3.2 makes those two different facts rather than one nullable
        # one.
        evidence.delete(chunk.id, ANCHOR_NAME)
        evidence.delete(chunk.id, ANCHOR_UNRESOLVED_NAME)

        span = evidence.read(chunk.id, "source_span")
        if isinstance(span, list) and len(span) == 2:
            anchor, code = anchor_for_ooxml_span(blocks, starts, span[0], span[1])
        else:
            anchor, code = None, UNRESOLVED_NO_SPAN

        if anchor is not None:
            evidence.write(chunk.id, ANCHOR_NAME, anchor)
            anchored += 1
            parts.add(anchor["part"])
            if "continues" in anchor:
                continued += 1
        else:
            evidence.write(
                chunk.id,
                ANCHOR_UNRESOLVED_NAME,
                {"code": code, "reason": UNRESOLVED_REASON[code]},
            )
            unresolved[code] = unresolved.get(code, 0) + 1

    session.flush()

    return TaskOutcome(
        detail={
            "chunks": len(chunks),
            "anchored": anchored,
            "unresolved": unresolved,
            # A passage that left the part it began in — a slide running into its speaker
            # notes. Reported separately because its address covers only the first part, so
            # it is the office half of `citation`'s `continues_overleaf`.
            "continues_in_another_part": continued,
            "parts_anchored": len(parts),
            # The pair `extract:text` already reports on its own attempt, repeated here
            # because this task is where it becomes a recovery rate: blocks with no address
            # are the reason a chunk can be unresolved without anything having gone wrong.
            "source_blocks": len(blocks),
            "addressed_blocks": sum(1 for block in blocks if block.get("path")),
        }
    )


# ---------------------------------------------------------------------------
# The RENDITION anchor, for an office package — OFFICE_SPEC.md Part 5, Part 11 step 8
# ---------------------------------------------------------------------------

#: Where the recovered rectangle lives, and where the reason lives when there is none. TWO
#: ROWS AND NOT ONE WITH A NULL, for :data:`ANCHOR_NAME`'s reason word for word — and TWO
#: ROWS SEPARATE FROM THAT PAIR for a reason of their own: a ``.docx`` gets an ``ooxml``
#: address from ``anchor:source`` AND a rectangle from this task, both tasks reserve the same
#: subtree, and one name for both would make whichever ran second the only survivor.
RENDER_ANCHOR_NAME = "render_anchor"
RENDER_ANCHOR_UNRESOLVED_NAME = "render_anchor.unresolved"

#: ``anchor.of`` — WHICH PDF the page and the rectangle are in. The field Part 5 writes as
#: ``{"kind": "pdf", ..., "of": "rendition"}``, and the one thing that keeps a recovered
#: rectangle readable as recovered once it has been parsed out of its row.
#:
#: **ABSENT MEANS** :data:`ANCHOR_OF_SOURCE`, and that is why :func:`run_citation` above was
#: NOT changed to write it. Every anchor written before this step is a source PDF's own
#: geometry; stamping the key on new rows and not on old ones would make it mean "was this
#: row written after 2026-09-14", which is a fact about the appliance and not about the
#: passage. ``jmfts_client.contracts.anchor.PdfAnchor.of`` defaults to ``"source"`` for the
#: same reason, so an old row and a new source anchor parse identically.
ANCHOR_OF_SOURCE = "source"
ANCHOR_OF_RENDITION = "rendition"

#: The chunk's blocks are in the source and none of them could be found in the rendition.
#: Part 5's failure list, met as ONE code rather than three: its "text appears more than
#: once", "text does not appear" and "chunk spans a page break" are three ways for a SEARCH
#: to come back unusable, and this handler does not search — it aligns two block maps in
#: document order (:func:`align_to_rendition`), where all three arrive as the same unmatched
#: block. Reporting "not found" and meaning it is the honest answer; naming which of the
#: three it was would be a claim the alignment never made.
UNRESOLVED_NOT_IN_RENDITION = "not_found_in_rendition"

UNRESOLVED_REASON[UNRESOLVED_NOT_IN_RENDITION] = (
    "none of the blocks the chunk covers could be matched against text in the rendition, so "
    "there is no page and no rectangle to point at. The renderer reflowed, hyphenated or "
    "dropped those characters, or the passage is text the reader wrote rather than text the "
    "document holds"
)

#: What a markdown marker is, for the purpose of comparing a source block against a rendered
#: one. Both sides are markdown — the office readers emit it, ``pdf_to_markdown`` emits it —
#: and NEITHER side's markers came from the other: a heading the ``.docx`` declares by style
#: becomes ``#`` here, and the same heading in the rendition becomes ``#`` only if PyMuPDF
#: judged its font size to be one. So the markers are noise on both sides, and removing them
#: from both is the comparison that means what it says. The dash range covers the soft and
#: typographic hyphens a renderer substitutes for the one the author typed.
_MATCH_NOISE = re.compile(r"[#*_`|>\[\]()~\-‐-―]+")
_MATCH_SPACE = re.compile(r"\s+")


def normalize_for_match(text: str) -> str:
    """``text`` reduced to what the two sides can be expected to agree about.

    Whitespace collapses because a line break is the renderer's decision and not the
    author's. It is CASE-FOLDED, and that is the one lossy step that needs an argument: a
    heading Word styles as small caps or all caps renders as capitals in the PDF while the
    package holds mixed case, so a case-sensitive test would refuse to locate exactly the
    headings a reader most wants located. Folding is not hiding a problem — nothing here
    decides whether two passages are the SAME, only which page one of them is on, and the
    passage's own text is on the node either way.
    """
    return _MATCH_SPACE.sub(" ", _MATCH_NOISE.sub(" ", text)).strip().casefold()


#: How far ahead of the cursor :func:`align_to_rendition` will look, in rendition blocks. The
#: rendition interleaves the document's own blocks with running heads, footers and page
#: numbers, so a block that is there at all is a few positions ahead — and a block found four
#: hundred positions ahead is a repeated string, not the paragraph. It also bounds the
#: alignment at ``O(source blocks x 400)`` rather than quadratic, which matters in the case
#: worth being fast in: a rendition that matches nothing is the one a bad conversion makes.
MATCH_WINDOW_BLOCKS = 400

#: The shortest normalized text that may be matched by PREFIX rather than in whole. An exact
#: match of any length is a match; a prefix one is how a paragraph split across a page break
#: is still located, and a short prefix would locate ``yes`` inside ``yes, and the following
#: quarter``. Sixteen characters is a few words, which is where a prefix stops being a
#: coincidence — a constant here and not an option, because nothing has measured the recovery
#: rate yet and a knob with no measurement behind it is a number two deployments disagree on.
MIN_PREFIX_CHARACTERS = 16


def _same_region(source: str, rendered: str) -> bool:
    """Whether two normalized blocks are the same text, allowing for a page break.

    Exact, or one a prefix of the other with the shorter at least
    :data:`MIN_PREFIX_CHARACTERS` long. The prefix arm is Part 5's third failure mode
    answered rather than reported: a paragraph the renderer split across a break becomes two
    rendition blocks, the first of which is a prefix of the source block, and that first
    fragment is exactly the rectangle an anchor wants — "the reader who clicks a citation
    wants to be taken to where the passage starts".
    """
    if source == rendered:
        return True
    if len(rendered) >= MIN_PREFIX_CHARACTERS and source.startswith(rendered):
        return True
    if len(source) >= MIN_PREFIX_CHARACTERS and rendered.startswith(source):
        return True
    return False


def align_to_rendition(
    source_texts: Sequence[str], rendered_texts: Sequence[str]
) -> list[Optional[int]]:
    """For each source block, the rendition block that holds it — or ``None``.

    **A MONOTONE WALK, NOT A SEARCH, and that is the design decision in this step.** Part 5
    proposes recovering each chunk's rectangle with ``page.search_for`` over the rendition,
    and names three ways it fails: the text appears more than once on the page, the text does
    not appear, the chunk spans a page break. Two of the three are properties of searching
    for one string in isolation, and they stop being failures when what is matched is a
    SEQUENCE: a repeated table header is ambiguous on its own and unambiguous as "the seventh
    block after the one before it". Both maps are in document order by construction — the
    office reader emits blocks in package order, ``pdf_to_markdown`` in page order — so the
    cursor only ever moves forward, and a running head that appears on every page matches the
    copy on the page the passage is actually on.

    The cost of the choice, stated because it is real: a block that does not match does not
    move the cursor, so a region the renderer restructured wholesale — a table laid out
    differently, a text box moved to another page — is skipped rather than resynchronised,
    and every block in it comes back ``None``. That is the honest outcome, because a
    rectangle guessed from a region the alignment lost is the plausible-and-wrong answer Part
    5 rejects, and the per-chunk counts are what make the rate visible.

    Neither list has to be complete or the same length as the other: the rendition holds page
    furniture no source block produced, and the source holds blocks the reader wrote — a
    ``## Notes`` label — that no page holds.
    """
    matched: list[Optional[int]] = []
    cursor = 0
    for text in source_texts:
        found: Optional[int] = None
        if text:
            limit = min(len(rendered_texts), cursor + MATCH_WINDOW_BLOCKS)
            for index in range(cursor, limit):
                if _same_region(text, rendered_texts[index]):
                    found = index
                    break
        matched.append(found)
        if found is not None:
            cursor = found + 1
    return matched


def anchor_for_rendition_span(
    blocks: Sequence[dict], starts: Sequence[int], start: int, end: int
) -> tuple[Optional[dict], Optional[str]]:
    """``(anchor, unresolved_code)`` for the markdown range ``[start, end)`` in a rendition.

    ``blocks`` is the SOURCE block map — the office reader's ``source_blocks``, whose offsets
    are the coordinates a chunk's ``source_span`` is measured in — with ``page`` and ``bbox``
    copied onto the entries :func:`align_to_rendition` located. A block with no ``bbox`` is
    one the rendition does not hold at a place this alignment could name.

    THE RULE IS :func:`anchor_for_span`'S RULE, deliberately: the page is the page the passage
    BEGINS on, the rectangle is the union of the located blocks on that page only, and
    ``continues`` carries the further pages in order. The two functions answer the same
    question about the same kind of object and differ only in where the geometry came from,
    so a consumer that can draw one can draw the other — which is Part 6's third reason for
    storing a rendition at all ("PDF and office unify").

    What they do not share is ``of``. This anchor says ``"of": "rendition"`` and a source
    PDF's says nothing, and the asymmetry is the point of the key: a reader holding a parsed
    anchor has to be able to tell a rectangle LibreOffice's pagination decided from one the
    document's own text layer did.
    """
    overlapping = overlapping_blocks(blocks, starts, start, end)
    if not overlapping:
        return None, UNRESOLVED_NO_BLOCK

    located = [block for block in overlapping if "bbox" in block]
    if not located:
        return None, UNRESOLVED_NOT_IN_RENDITION

    page = located[0]["page"]
    boxes = [block["bbox"] for block in located if block["page"] == page]
    anchor = {
        "kind": ANCHOR_KIND_PDF,
        "of": ANCHOR_OF_RENDITION,
        "page": page,
        "bbox": [
            min(box[0] for box in boxes),
            min(box[1] for box in boxes),
            max(box[2] for box in boxes),
            max(box[3] for box in boxes),
        ],
    }
    continues = sorted({block["page"] for block in located} - {page})
    if continues:
        anchor["continues"] = continues
    return anchor, None


def _located_blocks(source: tuple[str, Sequence[dict]], rendition: tuple[str, Sequence[dict]]):
    """The source blocks, each carrying the page and rectangle of the rendition block it met.

    Split out of the handler because it is the part with the decision in it and it needs no
    session, no blob and no queue row to be exercised: two markdowns and two block maps in,
    one augmented block map out.
    """
    source_markdown, source_blocks = source
    rendered_markdown, rendered_blocks = rendition

    matched = align_to_rendition(
        [normalize_for_match(source_markdown[b["start"] : b["end"]]) for b in source_blocks],
        [normalize_for_match(rendered_markdown[b["start"] : b["end"]]) for b in rendered_blocks],
    )
    # New dicts rather than a mutation of the reader's own output: the block map belongs to
    # the reader that returned it, and a handler that edited it in place would make a second
    # caller of the same reader see geometry it did not ask for.
    return [
        (
            {
                **block,
                "page": rendered_blocks[index]["page"],
                "bbox": rendered_blocks[index]["bbox"],
            }
            if index is not None
            else dict(block)
        )
        for block, index in zip(source_blocks, matched)
    ]


# NOT A ROW OF `TASK_ROWS`, and `ingest_tasks.TASK_ANCHOR_RENDER` carries the argument: the
# precondition is "the chunks exist AND a rendition exists", and the second half cannot be a
# row's `after` without hanging every office document on an appliance with no LibreOffice.
# `IngestRollupPlanner` offers it at the settling boundary instead. So there is no
# hand-written ordering here for the derivation to disagree with, and this task has no entry
# in `tests/test_atom_declarations.py::EXPECTED_DIVERGENCE` — not because it does not read
# `source_span@subtree` (it does, exactly as its two siblings do) but because it is not in
# the file-node batch that comparison is made over.
#
# `rendition@self` is the declaration that distinguishes this atom from `anchor:source`,
# whose `consumes` is otherwise identical, and it is the honest statement of why the two
# could not be one task: an atom over both would have to declare a dependency on a rendition
# that a `.pdf` never has.
@register_task_handler(
    TASK_ANCHOR_RENDER,
    consumes=(
        f"{EV_EXTRACTION}@self",
        f"{EV_TEXT}@self",
        f"{EV_BLOB}@self",
        f"{EV_RENDITION}@self",
        f"{EV_SOURCE_SPAN}@subtree",
    ),
    produces=(f"{EV_RENDER_ANCHOR}@subtree",),
    write_mode=WRITE_SUBTREE,
    cost_class=COST_CPU,
)
def run_anchor_render(session: Session, task: TaskQueue) -> TaskOutcome:
    """Put a page and a rectangle IN THE RENDITION on every chunk under this file node.

    Two parses and one alignment. The uploaded package is re-opened and re-extracted, for
    :func:`run_anchor_source`'s reason — the block map has to be in the same coordinates the
    chunks were cut in, and the comparison against ``content`` is what proves it is. The
    rendition is parsed with the same ``pdf_to_markdown`` ``citation`` uses on a source PDF,
    which is what gives every rendered block a page and a rectangle. Then the two maps are
    aligned in document order and each chunk's span is inverted through the result.

    **A SOURCE PDF IS REFUSED BY NAME.** ``pdf_for`` answers ``origin='source'`` for a
    document whose own bytes are the PDF, and for that document Part 5's two anchors are the
    same object: ``citation`` retains the geometry extraction already computed, and recovering
    it again by matching the text against itself would write a second row saying the same
    thing less exactly.

    **The stored ``rendition`` row is checked against the stored bytes.** Its ``page_count``
    is what a caller scales a rectangle against (``GET /documents/{id}/image``), so a row that
    disagrees with the object it describes means the two have come apart — a replaced
    rendition whose evidence write was rolled back, a restore that took one table and not the
    other — and anchoring through it would put rectangles on pages nobody sees. Raising is the
    only answer that does not publish that.
    """
    doc = session.get(Document, task.scope_document_id)
    if doc is None:
        raise ValueError(
            f"anchor:render is scoped to document {task.scope_document_id}, which does not exist"
        )

    evidence = EvidenceRepository(session)
    extraction = evidence.read(doc.id, "extraction") or {}
    source_name = extraction.get("source")
    reader = OOXML_READERS.get(source_name)
    if reader is None:
        raise ValueError(
            f"anchor:render is scoped to document {doc.id}, whose extraction record names "
            f"source {source_name!r}; this handler recovers a rectangle by aligning an OOXML "
            f"package's own blocks against a rendition of it, which is "
            f"{sorted(OOXML_READERS)}. A PDF's rectangle is its own text layer's, and "
            "`citation` writes it"
        )

    data = BlobRepository(session).read_bytes(doc.id)
    if data is None:
        raise ValueError(
            f"document {doc.id} has no stored blob; anchor:render was enqueued for bytes "
            "that are no longer there"
        )

    markdown, meta = reader(data)
    markdown, blocks = shift_for_removed(markdown, meta["source_blocks"], "\x00")
    content = doc.content or ""
    if markdown != content:
        raise ValueError(
            f"anchor:render re-extracted document {doc.id} and got {len(markdown)} characters "
            f"where the node holds {len(content)}; the block map and the chunks would then be "
            "indexed against different text, and every rectangle derived from it would be "
            "confidently wrong"
        )

    pdf = pdf_for(session, doc)
    if pdf.origin != ANCHOR_OF_RENDITION:
        raise ValueError(
            f"document {doc.id} resolves to a PDF of origin {pdf.origin!r}; anchor:render "
            "recovers a rectangle from a RENDITION, and a document whose own bytes are the "
            "PDF has its rectangle retained by `citation` from the text layer those "
            "coordinates belong to"
        )

    # The row and the bytes are written together by `render:pdf` and removed together by
    # `RenditionRepository.delete`, which is what lets a reader ask one question and get one
    # answer. Bytes with no row is that invariant already broken, and this atom declares
    # `rendition@self` as an input — so it is a refusal and not a shrug.
    record = evidence.read(doc.id, EVIDENCE_NAME[KIND_PDF])
    if not isinstance(record, dict):
        raise ValueError(
            f"document {doc.id} has stored rendition bytes and no `{EVIDENCE_NAME[KIND_PDF]}` "
            "evidence row describing them; those two are written in one transaction and "
            "removed in one, so this node is in a state neither `render:pdf` nor the eviction "
            "half can produce"
        )

    rendered_markdown, rendered_meta = pdf_to_markdown(pdf.data)
    page_count = rendered_meta["page_count"]
    declared = record.get("page_count")
    if declared is not None and declared != page_count:
        raise ValueError(
            f"document {doc.id}'s `{EVIDENCE_NAME[KIND_PDF]}` row says {declared} pages and "
            f"the stored rendition has {page_count}; the row and the object it describes have "
            "come apart, and a rectangle anchored through one and drawn through the other "
            "would point at a page nobody sees"
        )

    blocks = _located_blocks((markdown, blocks), (rendered_markdown, rendered_meta["text_blocks"]))
    starts = [block["start"] for block in blocks]

    chunks = (
        session.execute(
            select(Document)
            .where(Document.path.contains([doc.id]))
            .where(Document.usetype == USETYPE_CHUNK)
            .order_by(Document.id)
        )
        .scalars()
        .all()
    )

    anchored = 0
    continued = 0
    unresolved: dict[str, int] = {}
    pages: set[int] = set()

    for chunk in chunks:
        # Both rows first, for `run_citation`'s reason. The two rows DELETED here are this
        # task's own pair and NOT `anchor:source`'s — that is the whole of "two anchors, not
        # one" expressed as code, and deleting the wrong pair is exactly the overwrite the
        # separate name exists to prevent.
        evidence.delete(chunk.id, RENDER_ANCHOR_NAME)
        evidence.delete(chunk.id, RENDER_ANCHOR_UNRESOLVED_NAME)

        span = evidence.read(chunk.id, "source_span")
        if isinstance(span, list) and len(span) == 2:
            anchor, code = anchor_for_rendition_span(blocks, starts, span[0], span[1])
        else:
            anchor, code = None, UNRESOLVED_NO_SPAN

        if anchor is not None:
            evidence.write(chunk.id, RENDER_ANCHOR_NAME, anchor)
            anchored += 1
            pages.add(anchor["page"])
            if "continues" in anchor:
                continued += 1
        else:
            evidence.write(
                chunk.id,
                RENDER_ANCHOR_UNRESOLVED_NAME,
                {"code": code, "reason": UNRESOLVED_REASON[code]},
            )
            unresolved[code] = unresolved.get(code, 0) + 1

    session.flush()

    return TaskOutcome(
        detail={
            "chunks": len(chunks),
            "anchored": anchored,
            "unresolved": unresolved,
            "continues_overleaf": continued,
            "pages_anchored": len(pages),
            # THE RECOVERY RATE PART 10 IS FOR, and it is two numbers rather than one because
            # they fail in different places. `located_blocks / source_blocks` is how much of
            # the document the ALIGNMENT found; `anchored / chunks` is how much of it a reader
            # can click. A document with a good first ratio and a bad second one has chunks
            # falling in the gaps; the reverse means the alignment lost a region that happened
            # to hold few chunks.
            "source_blocks": len(blocks),
            "located_blocks": sum(1 for block in blocks if "bbox" in block),
            "rendered_blocks": len(rendered_meta["text_blocks"]),
            "rendition_pages": page_count,
            # WHAT PAGINATED IT, carried onto the attempt rather than onto each rectangle.
            # Part 6: two LibreOffice versions paginate differently, so which one produced
            # these coordinates is the fact that decides whether they are still valid. It
            # belongs once per run, beside the count, and not five hundred times on the
            # chunks — the `rendition` row is where it lives permanently.
            "renderer_version": record.get("renderer_version"),
        }
    )
