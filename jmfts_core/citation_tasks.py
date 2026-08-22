"""``citation`` — the page and the rectangle a chunk came from. ``OFFICE_SPEC.md`` Part 5.

An **anchor** is a stable address of a region of the source document, recorded on the node
that region produced. It lives at ``structured_content['anchor']``, and for a PDF it is::

    {"kind": "pdf", "page": 3, "bbox": [72.0, 118.4, 540.0, 262.9]}

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
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from jmfts_core.ingest_tasks import TASK_CITATION, TaskOutcome, register_task_handler
from jmfts_core.models.document import Document
from jmfts_core.models.task_queue import TaskQueue
from jmfts_core.pdf_extraction import pdf_to_markdown
from jmfts_core.repositories.blob import BlobRepository
from jmfts_core.structure_tasks import EXTRACTION_PDF_TEXT_LAYER, USETYPE_CHUNK

#: ``anchor.kind`` for a rectangle on a PDF page. An open string for the same reason
#: ``usetype`` is (spec Part 9) — ``ooxml`` and ``cells`` are the other two Part 5 names,
#: and neither is this appliance's business yet.
ANCHOR_KIND_PDF = "pdf"

#: Where the anchor lives on a chunk node, and where the reason lives when there is none.
#: TWO KEYS, NEVER ONE WITH A NULL: "this passage is at page 3, rectangle R" and "this
#: passage could not be placed, because X" are different facts, and a consumer that has to
#: tell them apart by testing for null gets no X to report.
ANCHOR_KEY = "anchor"
ANCHOR_UNRESOLVED_KEY = "anchor_unresolved"

#: The chunk was written before ``source_span`` existed, or its offset into the extracted
#: markdown could not be verified (see ``_TreeWriter._verified_span``). Either way there is
#: no character range to invert, and inventing one from a text search would be the
#: plausible-rectangle-around-the-wrong-words failure Part 5 rejects.
UNRESOLVED_NO_SPAN = "no_source_span"

#: The chunk's span lands in no extracted text block. Reachable when the span covers only
#: the whitespace between blocks, which the chunker's strip should prevent — so this is the
#: code to look at if it ever appears in quantity.
UNRESOLVED_NO_BLOCK = "no_overlapping_block"

UNRESOLVED_REASON: dict[str, str] = {
    UNRESOLVED_NO_SPAN: (
        "the chunk carries no verified `source_span`, so there is no character range in "
        "the extracted markdown to invert back into a rectangle"
    ),
    UNRESOLVED_NO_BLOCK: (
        "the chunk's `source_span` overlaps none of the text blocks the extraction "
        "measured, so no rectangle on any page accounts for it"
    ),
}


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
    # `bisect_right` finds the first block starting after `start`; the block before it is
    # the one that may CONTAIN `start`, which is the common case for a chunk cut out of the
    # middle of a paragraph.
    index = max(bisect_right(starts, start) - 1, 0)
    overlapping: list[dict] = []
    while index < len(blocks) and blocks[index]["start"] < end:
        block = blocks[index]
        if min(end, block["end"]) > max(start, block["start"]):
            overlapping.append(block)
        index += 1

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


@register_task_handler(TASK_CITATION)
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

    extraction = (doc.structured_content or {}).get("extraction") or {}
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

    for chunk in chunks:
        structured = dict(chunk.structured_content or {})
        # Both keys are cleared first so that a RE-RUN cannot leave last run's answer
        # standing beside this run's. Spec 6.1 makes re-running a first-class operation,
        # and a chunk holding an `anchor` from a superseded extraction next to an
        # `anchor_unresolved` from this one is a node that says two things.
        structured.pop(ANCHOR_KEY, None)
        structured.pop(ANCHOR_UNRESOLVED_KEY, None)

        span = structured.get("source_span")
        if isinstance(span, list) and len(span) == 2:
            anchor, code = anchor_for_span(blocks, starts, span[0], span[1])
        else:
            anchor, code = None, UNRESOLVED_NO_SPAN

        if anchor is not None:
            structured[ANCHOR_KEY] = anchor
            anchored += 1
            pages.add(anchor["page"])
            if "continues" in anchor:
                continued += 1
        else:
            structured[ANCHOR_UNRESOLVED_KEY] = {"code": code, "reason": UNRESOLVED_REASON[code]}
            unresolved[code] = unresolved.get(code, 0) + 1

        chunk.structured_content = structured

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
