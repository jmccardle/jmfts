"""Where a chunk came from. ``OFFICE_SPEC.md`` Part 5, and BOTH of the tasks that write it.

An **anchor** is a stable address of a region of the source document, recorded on the node
that region produced. It is the ``source_anchor`` evidence row, and for a PDF it is::

    {"kind": "pdf", "page": 3, "bbox": [72.0, 118.4, 540.0, 262.9]}

and for an office package, since Part 11 step 9, it is::

    {"kind": "ooxml", "part": "word/document.xml", "path": "/w:document/w:body/w:p[42]"}

TWO TASKS AND ONE ROW. ``citation`` writes the first and ``anchor:source`` the second, and
a document reaches exactly one of them: a PDF has pages and no package, an office file has a
package and — until something paginates it — no pages. That is Part 5's "Two anchors, not
one" read from the ingest side, and the anchor it does NOT cover is the third: a rectangle
recovered by searching a RENDITION of an office file, which is approximate, version
dependent, and must be recorded under a key of its own so that "recovered" and "exact" stay
distinguishable. Part 11 step 8 is where that one lands, and nothing here writes it.

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

from bisect import bisect_right
from typing import Callable, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from jmfts_core.atoms import (
    COST_CPU,
    EV_BLOB,
    EV_EXTRACTION,
    EV_SOURCE_ANCHOR,
    EV_SOURCE_SPAN,
    EV_TEXT,
)
from jmfts_core.ingest_tasks import (
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
    ordinary — the chunker packs to a token budget and knows nothing about pagination — and
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
    a citation wants to be taken to where the passage starts. A chunk packs to a token
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
