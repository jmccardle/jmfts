"""``extract:text`` and the structure rungs. ``INGEST_SPEC.md`` Part 4 and 3.5.

These are the handlers that turn an uploaded file into the tree the document describes.
``jmfts_core.ingest_tasks`` owns the registry and decides *which* of them runs; this module
is what they do.

**Two tasks, not one, and the split is the spec's.** ``extract:text`` reads the bytes and
writes text. ``structure:declared`` / ``structure:inferred`` read that text and write
nodes. Keeping them apart is what makes 6.1's progressive re-ingest possible: re-running a
structure task with different parameters re-reads ``content`` and never re-parses the PDF,
and a structure task that changes has no reason to invalidate the extraction. It is also
what makes the structure handlers format-agnostic — they consume text, an outline and page
offsets, and nothing in them knows what produced those.

**Which rung runs is decided before either task is enqueued.** ``plan_after_probe`` reads
one pattern (``has_outline`` for PDF) and queues the declared rung or the inferred one.
They are alternatives at the document level, not a ladder walked at run time, and that is a
deliberate limit of this pass: spec 3.5's rungs are per REGION, so an outline that names
eight chapters and nothing inside them should get ``declared`` boundaries with ``semantic``
structure inside each chapter. Nothing here descends into a claimed region. What it does
instead is MEASURE the region it did not claim — ``structure.coverage`` and
``structure.gap_regions`` — and record in the attempt detail that the lower rungs were not
attempted, so the gap is a number somebody can query rather than an absence nobody sees.

**The shape that comes out.**

    file node  (usetype="file")           content = the whole extracted markdown
    ├── chunk                             front matter: a title page or an abstract, which
    ├── chunk                             precedes the first heading and belongs to nobody
    └── section  (usetype="section")      content = None, title = the declared title
        ├── chunk                         the section's own prose, packed to a budget
        ├── chunk
        └── section                       a subsection, as deep as the source declares

A section node carries no ``content``. Its prose lives in its chunks, and duplicating it
onto the container would put the same text in the retrieval indexes twice and return both
for one query. The container's own content is what rollup summarisation writes; until that
runs the node is navigational, and its title is real information either way.

Text that precedes the first heading has no section to belong to, so its chunks attach
directly to the file node. That is the same rule as any other untitled region: a document
with no headings at all is one untitled region, and its chunks become the file node's
children with no container in between.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from sqlalchemy.orm import Session

from jmfts_core.chunking import ChunkStrategy, chunk_text
from jmfts_core.embedding import get_embedding_service
from jmfts_core.ingest_options import STRUCTURE_CHUNK_PARAMS
from jmfts_core.ingest_tasks import (
    TASK_EMBED,
    TASK_EXTRACT_TEXT,
    TASK_STRUCTURE_DECLARED,
    TASK_STRUCTURE_INFERRED,
    TaskOutcome,
    register_task_handler,
)
from jmfts_core.models.document import Document, SETTLED_IN_FLIGHT, SETTLED_SETTLED

# The office readers. Importing this module reaches NO office library: every import of
# python-docx / python-pptx inside it sits behind a require_* guard, at the point of use.
# tests/test_office_packaging.py::test_starting_the_app_imports_no_office_reader is what
# holds that, and it covers this import path.
from jmfts_core.office.extract import docx_to_markdown, pptx_to_markdown
from jmfts_core.models.task_queue import WRITE_SELF, TaskQueue
from jmfts_core.pdf_extraction import pdf_to_markdown
from jmfts_core.repositories.blob import BlobRepository
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.task_queue import TaskQueueRepository
from jmfts_core.settling import TaskSpec, enqueue_batch
from jmfts_core.structural_splitting import (
    Section,
    SectionNode,
    find_headings,
    nest,
    split_on_headings,
    split_on_outline,
)

#: Spec 3.5's rung names, as they are written into ``structure.primary_rung`` and into the
#: attempt record's ``rung`` field.
RUNG_DECLARED = "declared"
RUNG_INFERRED = "inferred"

#: What produced the boundaries, recorded beside the rung. The rung says how good the
#: evidence is; the source says where it came from, which is what a person needs to judge
#: a tree that came out wrong. It names what the rung READ, not what it found — a document
#: with no headings still gets ``atx_headings`` here, and ``coverage`` is what says nothing
#: was found.
SOURCE_OUTLINE = "pdf_outline"
SOURCE_FONT_SIZE = "font_size_headings"
SOURCE_ATX_HEADINGS = "atx_headings"

#: What ``extract:text`` writes into ``extraction.source``: which reader produced the text.
#: This is the contract between the two halves of ``INGEST_SPEC.md`` 11.3 — markdown is the
#: intermediate format, so a new entry point is a new value here plus the splitter to go
#: with it, and never a new structure task. The structure rungs dispatch on this and on
#: nothing else; neither of them knows what a PDF is.
EXTRACTION_PDF_TEXT_LAYER = "pdf_text_layer"
EXTRACTION_UTF8_TEXT = "utf8_text"
EXTRACTION_HTML_MARKUP = "html_markup"
EXTRACTION_DOCX_BODY = "docx_body"
EXTRACTION_PPTX_SLIDES = "pptx_slides"

#: Usetypes for the two kinds of node these tasks create. Open strings, like every usetype
#: (spec Part 9) — named here so the several places that mean the same node agree.
USETYPE_SECTION = "section"
USETYPE_CHUNK = "chunk"

#: The ``embed`` task every chunk is created with. One shared, frozen spec rather than one
#: built per chunk, because it is the same request every time and its ``params`` are part
#: of the ``param_fingerprint`` 6.1 diffs on — two chunks whose embed tasks differed by an
#: accident of construction would look to a re-ingest like two different requests.
#:
#: ``self`` (5.3): it writes this node's own ``embed`` column and its ``token_embeddings``
#: rows, and creates nothing. That is also what makes a document's chunks embed in
#: parallel — ``claim_next`` only conflicts a ``self`` task with another ``self`` on the
#: SAME node, so N chunks are N independently claimable tasks.
#:
#: ``with_tokens`` is stated rather than left to the handler's default, so the queue row
#: records which path was asked for. A chunk is exactly the node the token/maxsim path
#: exists for: it is a leaf, its text is its own, and it is bounded to the token window by
#: the chunker above.
EMBED_CHUNK_SPEC = TaskSpec(
    task_type=TASK_EMBED,
    write_mode=WRITE_SELF,
    params={"with_tokens": True},
)

#: The lower rungs, and why nothing enqueues them. Recorded in the attempt detail when a
#: coverage gap remains, because "the gap is 13% and nothing is going to claim it" and
#: "the gap is 13%" are different facts and only the first one is actionable.
LOWER_RUNG_REASON = (
    "spec 3.5's rungs are per region; running `semantic` or `flat` inside a region a "
    "higher rung already claimed is not implemented. `structure:semantic` exists, but as a "
    "rollup over this node's child sequence (11.4) rather than as a second pass over the "
    "text, so it claims no coverage here; `structure:flat` has no handler at all"
)


# ---------------------------------------------------------------------------
# extract:text
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Extracted:
    """One reader's output: the text, and what only that reader could have known.

    ``record`` is merged into ``extraction`` beside the fields every reader writes, and
    holds the indexes INTO the text that a later task cannot recompute without re-parsing
    the original bytes — page starts, a declared outline. ``detail`` is the same reader's
    own attempt-log entry.
    """

    text: str
    source: str
    record: dict = field(default_factory=dict)
    detail: dict = field(default_factory=dict)


def _extract_pdf(data: bytes) -> Extracted:
    """The PDF text layer, as markdown, with the page map and outline it carries.

    ``pages_with_tables`` is in the RECORD, not only the detail, because it is a pattern
    and not a statistic: spec Part 4's ``extract:tables`` row is predicated on it, and
    ``probe`` does not measure it (``jmfts_core.probe._probe_pdf``). A pattern that lived
    only in the attempt log would be a measurement nothing could plan from.

    ``table_failures`` and ``tables_rejected`` go to the DETAIL, because they are the
    attempt's own account of what it could not do — 3.4's business, not a later task's
    input. Without them the reporting `pdf_to_markdown` performs would stop at this
    function and never reach the node, which would make a page PyMuPDF could not analyse
    invisible again.
    """
    markdown, meta = pdf_to_markdown(data)
    return Extracted(
        text=markdown,
        source=EXTRACTION_PDF_TEXT_LAYER,
        record={
            "page_count": meta["page_count"],
            "page_offsets": meta["page_offsets"],
            "control_chars_removed": meta["control_chars_removed"],
            "pages_with_tables": meta["pages_with_tables"],
            "toc": meta["toc"],
        },
        detail={
            "page_count": meta["page_count"],
            "outline_entries": len(meta["toc"]),
            "control_chars_removed": meta["control_chars_removed"],
            "tables_found": meta["tables_found"],
            "pages_with_tables": meta["pages_with_tables"],
            "table_failures": meta["table_failures"],
            "tables_rejected": meta["tables_rejected"],
        },
    )


def _extract_utf8_text(data: bytes) -> Extracted:
    """A ``.md`` or ``.txt`` file. A DECODE, not a conversion — the bytes already are text.

    ``INGEST_SPEC.md`` 11.3: markdown is the intermediate format every other entry point
    converges on, so this reader has the least to do of any of them. It writes no
    ``page_offsets``, because a text file has no pages, and an empty ``toc``, because the
    headings are in the text where ``split_on_headings`` will find them — storing them
    twice would create two records of one fact that could disagree.

    The decode is strict, and ``probe`` already decoded the same bytes to measure them, so
    a failure here means the blob changed under us rather than that the file was ever
    ambiguous.
    """
    text = data.decode("utf-8")
    return Extracted(
        text=text,
        source=EXTRACTION_UTF8_TEXT,
        record={"toc": []},
        detail={"headings_found": len(find_headings(text))},
    )


def _extract_html(data: bytes) -> Extracted:
    """An HTML, XML or SVG document. A CONVERSION, unlike :func:`_extract_utf8_text`.

    ``INGEST_SPEC.md`` 11.3 makes markdown the intermediate format, and this is the reader
    that gets HTML there. Before it existed, ``has_markup`` was a ``forbids`` on the
    ``extract:text`` row: an HTML file was probed, held back with a stated reason, and
    settled with no content and no children. The reason was recorded, but from outside the
    upload returned 200 and every task reported "completed", so the only way to learn that
    a document had produced nothing was to read its attempt log.

    ``heading_style="ATX"`` is what makes the rest of the pipeline work unchanged:
    ``<h1>`` becomes ``# ``, which is what :func:`_split_atx` already looks for, so the
    structure rungs need no new dispatch entry beyond the source constant below.

    The decode is strict for the same reason the text reader's is — ``probe`` decoded these
    bytes already, so a failure here means they changed underneath us.
    """
    from jmfts_core.url_fetch import html_to_markdown

    html = data.decode("utf-8")
    text = html_to_markdown(html)
    return Extracted(
        text=text,
        source=EXTRACTION_HTML_MARKUP,
        record={"toc": []},
        detail={
            # Both numbers, not a ratio: conversion is expected to shrink a document (tags
            # leave), and how MUCH it shrank is the signal that something went wrong.
            "html_chars": len(html),
            "markdown_chars": len(text),
            "headings_found": len(find_headings(text)),
        },
    )


#: format -> the reader for it. A format absent from here has no extractor, and
#: ``extract:text`` says so by name rather than assuming one reader is the general case.
#:
#: HTML is NOT a key here, and cannot be: 11.3 measured that nothing in the bytes separates
def _extract_docx(data: bytes) -> Extracted:
    """A WordprocessingML document, as markdown. ``INGEST_SPEC.md`` Part 10 step 6.

    ``toc`` is empty for the reason :func:`_extract_utf8_text` gives: the headings are in
    the text, where ``split_on_headings`` will find them, and storing them twice would
    create two records of one fact that are free to disagree. The counts go to the DETAIL,
    because they are this attempt's account of what it read and no later task's input.
    """
    markdown, meta = docx_to_markdown(data)
    return Extracted(
        text=markdown,
        source=EXTRACTION_DOCX_BODY,
        record={"toc": []},
        detail={
            "paragraph_count": meta["paragraph_count"],
            "heading_count": meta["heading_count"],
            "list_item_count": meta["list_item_count"],
            "table_count": meta["table_count"],
            "markdown_chars": meta["characters"],
        },
    )


def _extract_pptx(data: bytes) -> Extracted:
    """A PresentationML deck, as markdown — one ``#`` section per slide.

    ``slide_offsets`` is in the RECORD, not only the detail, and it is the one thing here
    a later task could not recompute without re-opening the package: it is the analogue of
    :func:`_extract_pdf`'s ``page_offsets``, indexes INTO the extracted text. ``slide_count``
    travels with it so a consumer can tell a truncated list from a short deck.
    """
    markdown, meta = pptx_to_markdown(data)
    return Extracted(
        text=markdown,
        source=EXTRACTION_PPTX_SLIDES,
        record={
            "toc": [],
            "slide_count": meta["slide_count"],
            "slide_offsets": meta["slide_offsets"],
        },
        detail={
            "text_block_count": meta["text_block_count"],
            "table_count": meta["table_count"],
            "notes_slide_count": meta["notes_slide_count"],
            # How many slide headings this reader chose rather than read from a title
            # placeholder. In the attempt log because it is the difference between a
            # section title the author wrote and one derived from the slide's first line.
            "promoted_title_count": meta["promoted_title_count"],
            "markdown_chars": meta["characters"],
        },
    )


#: HTML from markdown at the FORMAT layer, so ``detect_format`` reports both as ``text``.
#: The pattern ``has_markup`` is what separates them, and :func:`run_extract_text` consults
#: it for the ``text`` format only. See :data:`MARKUP_EXTRACTOR`.
TEXT_EXTRACTORS: dict[str, Callable[[bytes], Extracted]] = {
    "pdf": _extract_pdf,
    "text": _extract_utf8_text,
    "docx": _extract_docx,
    "pptx": _extract_pptx,
}

#: The reader that a ``text``-format file gets INSTEAD when ``probe`` called it markup.
MARKUP_EXTRACTOR: Callable[[bytes], Extracted] = _extract_html


@register_task_handler(TASK_EXTRACT_TEXT)
def run_extract_text(session: Session, task: TaskQueue) -> TaskOutcome:
    """Read the stored bytes and write the file node's text and extraction record.

    Writes ``content`` — the whole markdown — and ``structured_content['extraction']``,
    whose ``source`` is what the structure rungs dispatch on. Which reader runs is decided
    by the format ``probe`` measured, from :data:`TEXT_EXTRACTORS`.

    **Short or empty output is not a failure.** A PDF whose text layer yields three
    characters has been processed safely and does not contain a document we can read; the
    node then settles with no content and no children, and ``settling`` records the zero
    yield with the probe patterns behind it. Raising on a character threshold would
    manufacture a failure out of a correct outcome. What DOES raise is being unable to do
    the job at all: no stored bytes, a format with no extractor, or a file PyMuPDF refuses
    to open.

    **The yield is recorded as a pair, not as a verdict.** 11.3 names the hazard this task
    creates by existing: before it, an unreadable file produced an empty node that was
    obviously wrong, and after it the same file can produce a plausible tree. So the detail
    carries ``bytes_in`` beside ``characters``, and — for a format whose prober measured
    the text ahead of time — ``characters_probed`` beside it. Two numbers that should agree
    and do not are the signal, which is a stronger check than a threshold nobody has
    measured, and it needs no constant to be wrong about.

    ONE SOURCE BREAKS THAT READING ON PURPOSE. When ``has_markup`` routed the file to
    :func:`_extract_html`, ``characters_probed`` counted HTML and ``characters`` counts the
    markdown it became, so they are SUPPOSED to disagree — tags left. Read ``html_chars``
    and ``markdown_chars`` in the same detail instead; those are the pair that describes
    this conversion, and a markdown side near zero against a large HTML side is what a
    collapsed conversion looks like.

    The file node is not embedded here. Its content is the entire document, routinely well
    past the model's window, and the nodes that exist to be embedded are the chunks the
    structure task creates from it.
    """
    doc = _scope_node(session, task, TASK_EXTRACT_TEXT)

    matched = (doc.structured_content or {}).get("matched") or {}
    fmt = matched.get("format")
    extractor = TEXT_EXTRACTORS.get(fmt)
    # `has_markup` selects the reader; it does not block the row. It is consulted only for
    # `text`, because that is the one format whose files can be either prose or markup —
    # every other format's identity already decided which reader it gets.
    if fmt == "text" and (matched.get("patterns") or {}).get("has_markup"):
        extractor = MARKUP_EXTRACTOR
    if extractor is None:
        raise ValueError(
            f"extract:text is scoped to document {doc.id}, whose probed format is {fmt!r}; "
            f"this appliance extracts text from {sorted(TEXT_EXTRACTORS)} (INGEST_SPEC.md "
            "Part 10 step 6 brings the office formats)"
        )

    data = BlobRepository(session).read_bytes(doc.id)
    if data is None:
        raise ValueError(
            f"document {doc.id} has no stored blob; extract:text was enqueued for bytes "
            "that are no longer there"
        )

    extracted = extractor(data)

    doc.content = extracted.text
    structured = dict(doc.structured_content or {})
    structured["extraction"] = {
        "source": extracted.source,
        "characters": len(extracted.text),
        **extracted.record,
    }
    doc.structured_content = structured
    session.flush()

    detail = {
        "source": extracted.source,
        "bytes_in": len(data),
        "characters": len(extracted.text),
        **extracted.detail,
    }
    # The one prober that counts characters is the text one, and it counts them so that
    # this comparison exists. A format whose prober measures something else contributes
    # nothing here, and an absent key is left absent rather than filled with a zero that
    # would read as a total loss.
    probed_chars = (matched.get("patterns") or {}).get("char_count")
    if probed_chars is not None:
        detail["characters_probed"] = probed_chars

    return TaskOutcome(detail=detail)


# ---------------------------------------------------------------------------
# The structure rungs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Boundaries:
    """Where one rung decided the sections are, and what it read to decide it."""

    sections: list[Section]
    source: str
    detail: dict = field(default_factory=dict)


def _declared_from_outline(doc: Document, extraction: dict) -> Boundaries:
    """The document's own outline. PDF and, when it lands, EPUB.

    The outline is taken as given, levels included. A file that files section 3 under
    section 2 gets a tree that files section 3 under section 2 — see
    :mod:`jmfts_core.structural_splitting` for why rebuilding the hierarchy from the
    numbering would be a heuristic applied on top of freeform input rather than a fix.

    An empty outline here is a CONTRADICTION and raises. ``probe`` reported one and
    ``extract:text`` found none, and both read it from the same bytes with the same call.
    A PDF that merely has no outline never reaches this task: ``has_outline`` is false,
    ``structure:declared`` is not applicable, and ``structure:inferred`` runs instead.
    """
    toc = extraction.get("toc")
    if not toc:
        raise ValueError(
            f"structure:declared is scoped to document {doc.id}, whose extraction record "
            "carries no outline; this task is enqueued only when probe reported one, so "
            "either extract:text did not run or it ran on different bytes"
        )
    split = split_on_outline(doc.content or "", toc, extraction.get("page_offsets"))
    return Boundaries(
        sections=split.sections,
        source=SOURCE_OUTLINE,
        detail={"outline_entries": len(toc), "unplaced_titles": split.unplaced},
    )


def _split_atx(doc: Document, extraction: dict) -> Boundaries:
    """ATX headings, which the author typed. The declared rung for markdown.

    No outline is consulted and none is expected: a text file's structure is in its text,
    and 11.3 is explicit that storing it in the extraction record as well would create two
    records of one fact that are free to disagree.
    """
    return Boundaries(
        sections=split_on_headings(doc.content or ""),
        source=SOURCE_ATX_HEADINGS,
    )


def _inferred_from_font_size(doc: Document, extraction: dict) -> Boundaries:
    """The headings ``pdf_extraction``'s font-size analysis wrote as ATX markers.

    The same splitter as :func:`_split_atx` and a different rung, because the markers mean
    different things: an author typed the ones in a markdown file, and this appliance
    inferred these from type size. The rung is a property of the evidence, not of the code.
    """
    return Boundaries(sections=split_on_headings(doc.content or ""), source=SOURCE_FONT_SIZE)


#: ``extraction.source`` -> the splitter for the DECLARED rung. This is 11.3's second open
#: question, settled: ``declared`` does not mean one mechanism. For PDF it is the outline;
#: for markdown it is the headings, which are the same function that is PDF's INFERRED
#: rung. So the splitter is per extraction source, and the task stays one task.
#: Converted HTML gets :func:`_split_atx` because ``html_to_markdown`` is called with
#: ``heading_style="ATX"``, so an ``<h2>`` arrives as ``## ``. The headings are DECLARED in
#: the same sense a markdown file's are — the author wrote them — even though a converter
#: rewrote the syntax on the way here.
#: The two office readers get :func:`_split_atx` for the same reason converted HTML does,
#: and the claim is stronger here rather than weaker. A ``.docx``'s ATX headings were
#: written FROM the heading styles ``probe`` measured to report ``has_heading_styles``, and
#: a ``.pptx``'s ``#`` per slide is the slides it counted for ``has_slides`` — so in both
#: cases the pattern that lets this rung run and the structure it splits on are two
#: readings of one fact about the file.
DECLARED_SPLITTERS: dict[str, Callable[[Document, dict], Boundaries]] = {
    EXTRACTION_PDF_TEXT_LAYER: _declared_from_outline,
    EXTRACTION_UTF8_TEXT: _split_atx,
    EXTRACTION_HTML_MARKUP: _split_atx,
    EXTRACTION_DOCX_BODY: _split_atx,
    EXTRACTION_PPTX_SLIDES: _split_atx,
}

#: ``extraction.source`` -> the splitter for the INFERRED rung. A text file reaches it only
#: when ``probe`` found no headings, so what it produces is one untitled region — which is
#: the correct answer for a ``.txt`` file and is recorded as a coverage gap rather than as
#: a failure.
#: HTML reaches this rung when the converted markdown carries no headings — a page built
#: entirely from styled ``<div>``s, which is common enough that the entry has to exist.
#: It produces one untitled region, the same honest answer a heading-less ``.txt`` gets.
#: A ``.docx`` reaches the inferred rung when it declared no heading styles, and a ``.pptx``
#: cannot reach it at all in practice — a deck with no slides has nothing to extract. The
#: ``docx`` entry produces one untitled region for a document that is flat prose, which is
#: the same honest answer a heading-less ``.txt`` gets.
INFERRED_SPLITTERS: dict[str, Callable[[Document, dict], Boundaries]] = {
    EXTRACTION_PDF_TEXT_LAYER: _inferred_from_font_size,
    EXTRACTION_UTF8_TEXT: _split_atx,
    EXTRACTION_HTML_MARKUP: _split_atx,
    EXTRACTION_DOCX_BODY: _split_atx,
    EXTRACTION_PPTX_SLIDES: _split_atx,
}


def _run_rung(
    session: Session,
    task: TaskQueue,
    task_type: str,
    splitters: dict[str, Callable[[Document, dict], Boundaries]],
    rung: str,
) -> TaskOutcome:
    """Dispatch on what produced the text, then write the tree the splitter found."""
    doc = _scope_node(session, task, task_type)
    extraction = (doc.structured_content or {}).get("extraction") or {}
    source = extraction.get("source")
    splitter = splitters.get(source)
    if splitter is None:
        raise ValueError(
            f"{task_type} is scoped to document {doc.id}, whose extraction record names "
            f"source {source!r}; this rung reads {sorted(splitters)}. A new entry point "
            "needs a splitter here, not a new structure task (INGEST_SPEC.md 11.3)"
        )

    boundaries = splitter(doc, extraction)
    detail = dict(boundaries.detail)
    # The paired measurement 11.3 asks for, on the half that can go wrong quietly. The text
    # prober counts headings with the same function the splitter uses, so these two numbers
    # agree unless the extraction changed the text between them — which is exactly the
    # silent mangle this task would otherwise settle over.
    probed_headings = (
        ((doc.structured_content or {}).get("matched") or {}).get("patterns") or {}
    ).get("heading_count")
    if probed_headings is not None:
        detail["headings_probed"] = probed_headings

    return _build_tree(
        session,
        task,
        doc,
        boundaries.sections,
        rung=rung,
        source=boundaries.source,
        extra_detail=detail,
    )


@register_task_handler(TASK_STRUCTURE_DECLARED)
def run_structure_declared(session: Session, task: TaskQueue) -> TaskOutcome:
    """Build the tree the document declares for itself. Spec 3.5's top rung."""
    return _run_rung(session, task, TASK_STRUCTURE_DECLARED, DECLARED_SPLITTERS, RUNG_DECLARED)


@register_task_handler(TASK_STRUCTURE_INFERRED)
def run_structure_inferred(session: Session, task: TaskQueue) -> TaskOutcome:
    """Build the tree from boundaries this appliance inferred. Spec 3.5's second rung."""
    return _run_rung(session, task, TASK_STRUCTURE_INFERRED, INFERRED_SPLITTERS, RUNG_INFERRED)


def _build_tree(
    session: Session,
    task: TaskQueue,
    doc: Document,
    sections: Sequence[Section],
    *,
    rung: str,
    source: str,
    extra_detail: dict,
) -> TaskOutcome:
    """Write ``sections`` under ``doc`` as a tree, and record what that covered.

    Producing NOTHING is a completed task, not a failed one, and the detail says what was
    looked for. Spec 3.4 is explicit that a heuristic which ran and found nothing must
    never look like one that never ran.
    """
    params = dict(task.params or {})
    strategy = ChunkStrategy(params.get("chunk_strategy", STRUCTURE_CHUNK_PARAMS["chunk_strategy"]))
    max_tokens = int(params.get("max_tokens", STRUCTURE_CHUNK_PARAMS["max_tokens"]))
    min_chunk_length = int(
        params.get("min_chunk_length", STRUCTURE_CHUNK_PARAMS["min_chunk_length"])
    )

    text = doc.content or ""
    writer = _TreeWriter(
        repo=DocumentRepository(session),
        tasks=TaskQueueRepository(session),
        rung=rung,
        strategy=strategy,
        max_tokens=max_tokens,
        min_chunk_length=min_chunk_length,
        # Taken from the FLAT list, which is the one thing guaranteed to be in document
        # order. `nest` preserves that order under a preorder walk, but the writer's walk
        # is not the place to depend on it: a cursor that advanced in traversal order would
        # be silently wrong the day the traversal changed, and the wrongness would be a
        # rectangle on the wrong page rather than an exception.
        body_offsets=_locate_section_bodies(text, sections),
    )
    roots = nest(sections)
    for root in roots:
        writer.write(doc.id, root, depth=1)
    session.flush()

    covered, uncovered, gap_regions = _coverage(sections)
    assigned = covered + uncovered
    coverage = (covered / assigned) if assigned else 0.0

    structured = dict(doc.structured_content or {})
    structured["structure"] = {
        "primary_rung": rung,
        "source": source,
        "coverage": round(coverage, 4),
        "node_count": writer.node_count,
        "max_depth": writer.max_depth,
        "gap_regions": gap_regions,
    }
    doc.structured_content = structured
    session.flush()

    detail = {
        "sections": len(sections),
        "sections_titled": sum(1 for s in sections if s.title),
        "section_nodes": len(writer.section_ids),
        "chunk_nodes": writer.chunk_count,
        # The paired number for `chunk_nodes`, in the same spirit as `characters_probed`
        # beside `characters`: a chunk with no `source_span` is one `citation` cannot place,
        # and the difference between the two is the ceiling on the anchor recovery rate
        # before `citation` has run at all. See `_write_chunks`.
        "chunks_with_source_span": writer.spanned_chunk_count,
        "coverage": round(coverage, 4),
        "gap_regions": gap_regions,
        "params": {
            "chunk_strategy": strategy.value,
            "max_tokens": max_tokens,
            "min_chunk_length": min_chunk_length,
        },
        **extra_detail,
    }
    if gap_regions:
        detail["deferred"] = {"structure:semantic": LOWER_RUNG_REASON}

    return TaskOutcome(
        rung=rung,
        detail=detail,
        produced={"node_count": writer.node_count, "child_ids": writer.direct_child_ids},
    )


class _TreeWriter:
    """Creates the section and chunk nodes for one structure task.

    **A CHUNK is created in flight, holding an ``embed``.** Its prose is not retrievable
    until it has vectors, and since ``embed`` became its own task
    (:data:`~jmfts_core.ingest_tasks.TASK_EMBED`) that no longer happens before the node
    exists.

    THAT IS ALSO THE ORDERING. Rollup reads its children's embeddings and used to be safe
    only because they were written inline, inside this transaction. Now the chunk carries
    an unfinished task, so ``settle_node`` refuses to settle it, the node above it is
    blocked on ``children``, and the rollup planner is not called until every chunk under
    it has drained. No dependency array expresses that and none could — ``enqueue_batch``
    resolves ``after`` only within one node's batch (5.5), and this ordering is between a
    node and its children.

    **A SECTION is created in flight too, and settled here only if nothing beneath it
    queued anything.** ``settled`` is recursive: a node is settled when its own work is
    done AND every child is settled. A section created settled above in-flight chunks is
    therefore a false claim, and it is not a harmless one — the file node reads its DIRECT
    children, sees settled sections, and rolls up over grandchildren with no vectors. That
    is measured by ``tests/test_embed_task.py``, which caught exactly this.

    The case the old always-settled rule was protecting against is real and is now handled
    by name: a heading with no prose and no subsections is a container nothing will ever
    work on, and left in flight it would park the document forever. :meth:`write` returns
    whether its subtree queued anything, which is how that container is told apart from one
    whose chunks are on their way.

    A consequence worth stating: a section container now gets VISITED. Before this, the
    walk only ever started at the file node, so a section — settled at birth, with no work
    on it — was never evaluated and never summarized, which is the gap
    ``run_structure_semantic``'s docstring names about its own containers. Its chunks now
    walk up through it, and it gets ``effective_content`` like any other interior node.
    """

    def __init__(
        self,
        *,
        repo: DocumentRepository,
        tasks: TaskQueueRepository,
        rung: str,
        strategy: ChunkStrategy,
        max_tokens: int,
        min_chunk_length: int,
        body_offsets: dict[int, int],
    ):
        self.repo = repo
        self.tasks = tasks
        self.rung = rung
        self.strategy = strategy
        self.max_tokens = max_tokens
        self.min_chunk_length = min_chunk_length
        self.body_offsets = body_offsets

        #: Ids of the section containers, in creation order.
        self.section_ids: list[int] = []
        #: Ids of nodes attached directly to the file node — spec 6.2's undo record.
        #: Direct children are enough: superseding deletes them WITH their subtrees.
        self.direct_child_ids: list[int] = []
        self.chunk_count = 0
        #: How many of those chunks carry a verified ``source_span``.
        self.spanned_chunk_count = 0
        self.max_depth = 0
        self._root_id: Optional[int] = None

    @property
    def node_count(self) -> int:
        return len(self.section_ids) + self.chunk_count

    def write(self, parent_id: int, node: SectionNode, *, depth: int) -> bool:
        """Write one region and everything under it. Returns whether it queued any work.

        THE RETURN VALUE IS WHAT KEEPS ``settled`` HONEST. ``settled`` is recursive — a
        node is settled when its own work is done AND every child is settled — so a
        container created settled above in-flight chunks is a false claim, and the file
        node above IT would then read its direct children as finished and roll up over
        grandchildren that have no vectors yet.

        A container therefore starts ``in_flight`` and is settled here only if nothing
        beneath it queued anything. That case is real and has to be handled explicitly: a
        heading with no prose under it and no subsections is a container nothing will ever
        work on, and leaving it in flight would park the whole document forever.
        """
        if self._root_id is None:
            self._root_id = parent_id
        section = node.section

        container: Optional[Document] = None
        if section.title:
            container = self.repo.create(
                title=section.title,
                content=None,
                parent_id=parent_id,
                usetype=USETYPE_SECTION,
                structured_content={
                    "structure": {
                        "primary_rung": self.rung,
                        "level": section.level,
                        "source_line": section.source_line,
                    }
                },
                auto_embed=False,
                sequential=True,
                settled=SETTLED_IN_FLIGHT,
            )
            self._record(container.id, parent_id)
            self.section_ids.append(container.id)
            host, host_depth = container.id, depth
        else:
            # An untitled region — front matter, or a document with no headings at all.
            # There is no title to name a container with, and inventing one would put a
            # word in the tree that is not in the document. Its chunks attach where the
            # container would have.
            host, host_depth = parent_id, depth - 1

        self.max_depth = max(self.max_depth, host_depth)
        queued = self._write_chunks(host, section, depth=host_depth + 1)
        for child in node.children:
            # `|` and not `or`: `write` has to run for every child, and short-circuiting
            # would stop building the tree at the first subsection that queued something.
            queued = self.write(host, child, depth=host_depth + 1) | queued

        if container is not None and not queued:
            container.settled = SETTLED_SETTLED
        return queued

    def _write_chunks(self, parent_id: int, section: Section, *, depth: int) -> bool:
        """Chunk one region's prose. Returns whether it wrote (and queued) anything."""
        body = (section.content or "").strip()
        if not body:
            return False
        # `fits` because every chunk here gets an `embed` task, which takes the 512-token
        # token/maxsim path: a piece inside the character cap that tokenises denser than
        # the cap assumes raises TextTooLongError, and the classifier calls that permanent,
        # so one dense paragraph failed a whole document (D7). Measuring it here rather
        # than discovering it in the embed task is what keeps that a chunking decision.
        # `get_embedding_service`, not `get_embedder`: this is `check_fit` underneath, so
        # it is the tokenizer, and it must stay local even on a worker that embeds remotely
        # — one HTTP round trip per candidate piece would be most of the wall clock.
        service = get_embedding_service()
        chunks = chunk_text(
            body,
            strategy=self.strategy,
            max_tokens=self.max_tokens,
            min_chunk_length=self.min_chunk_length,
            fits=service.fits_token_window,
        )
        label = section.title or f"Section (line {section.source_line})"
        spans = _chunk_spans(body, self.body_offsets.get(id(section)), chunks)
        for chunk, span in zip(chunks, spans):
            structured = {
                "rung": self.rung,
                "section_title": section.title,
                "section_level": section.level,
                "chunk_index": chunk.index,
                "source_line": section.source_line,
            }
            if span is not None:
                structured["source_span"] = span
                self.spanned_chunk_count += 1
            node = self.repo.create(
                title=label if len(chunks) == 1 else f"{label} — chunk {chunk.index}",
                content=chunk.text,
                parent_id=parent_id,
                usetype=USETYPE_CHUNK,
                structured_content=structured,
                # The model does not run here any more. `embed` is its own task and this
                # node is not retrievable until that task has run, which is what
                # `in_flight` states — see the class docstring.
                auto_embed=False,
                sequential=True,
                settled=SETTLED_IN_FLIGHT,
            )
            enqueue_batch(self.tasks, node.id, (EMBED_CHUNK_SPEC,))
            self._record(node.id, parent_id)
            self.chunk_count += 1
        self.max_depth = max(self.max_depth, depth)
        # `chunk_text` can return nothing for a body that is all whitespace or shorter than
        # `min_chunk_length`, so this is the region's real yield rather than "it had text".
        return bool(chunks)

    def _record(self, node_id: int, parent_id: int) -> None:
        if parent_id == self._root_id:
            self.direct_child_ids.append(node_id)


#: Any run of whitespace, for :func:`_collapsed_with_map`.
_WHITESPACE_RUN = re.compile(r"\s+")


def _collapsed_with_map(text: str) -> tuple[str, list[int]]:
    """``(collapsed, offsets)`` where ``offsets[i]`` is where ``collapsed[i]`` came from.

    Whitespace runs become a single space and nothing else changes — no case folding and no
    markup stripping, unlike ``structural_splitting._normalized_with_map``, because the two
    are answering different questions. That one matches an outline title against text a
    reader typed differently; this one matches text against ITSELF after the chunker moved
    through it, and the only thing the chunker changes is whitespace.
    """
    out: list[str] = []
    offsets: list[int] = []
    for index, char in enumerate(text):
        if char.isspace():
            if out and out[-1] == " ":
                continue
            out.append(" ")
        else:
            out.append(char)
        offsets.append(index)
    return "".join(out), offsets


def _chunk_spans(body: str, base: Optional[int], chunks: Sequence) -> list[Optional[list[int]]]:
    """Where each chunk sits in the file node's markdown, one entry per chunk.

    THE SIBLING OF ``source_line``, IN THE SAME COORDINATES. ``source_line`` is a line offset
    into the extracted markdown and it locates the SECTION; this locates the chunk itself,
    and it is what lets ``citation`` (``OFFICE_SPEC.md`` Part 5) invert a chunk back to the
    rectangles on the page it was set in. It is not the anchor, and it is not positional
    information about the SOURCE document — it is an index into text this appliance produced,
    which is exactly why the task that produced the text can write it without knowing
    anything about the format the text came from.

    ``Chunk.char_start`` is not used, and cannot be. ``chunk_text`` computes it with a
    forward ``text.find`` of the chunk in the body, and for every packing strategy that find
    MISSES: ``sentence_packed`` — the shipped default — joins the sentences it packed with a
    single space, so a chunk that spans a paragraph break is not a verbatim slice of the
    body, and ``chunk_text`` falls back to a running offset short by however much whitespace
    it collapsed. The error is small per chunk and CUMULATIVE down a section, so a late chunk
    in a long chapter can be pointing a paragraph or more too early — which is a plausible
    rectangle around the wrong words, the one outcome Part 5 says is worse than no rectangle
    at all, because the reader cannot tell.

    What every strategy shares is that it only ever changes WHITESPACE: sentences and
    paragraphs are stripped and re-joined with a space, ``_enforce_max_chars`` re-joins
    words with a space, and the ``min_chunk_length`` merge glues two pieces with a space. So
    collapsing both sides' whitespace makes each chunk an exact substring of the body again,
    and :func:`_collapsed_with_map` carries the offsets needed to come back. The cursor keeps
    the matches in order, so a sentence repeated later in the section cannot claim an earlier
    chunk's position.

    ``None`` for a chunk that still cannot be placed — there is no fallback to an approximate
    offset, and the structure task's detail counts how many spans it wrote.
    """
    if base is None:
        return [None] * len(chunks)

    collapsed, offsets = _collapsed_with_map(body)
    spans: list[Optional[list[int]]] = []
    cursor = 0
    for chunk in chunks:
        needle = _WHITESPACE_RUN.sub(" ", chunk.text).strip()
        found = collapsed.find(needle, cursor) if needle else -1
        if found < 0:
            spans.append(None)
            continue
        last = found + len(needle) - 1
        spans.append([base + offsets[found], base + offsets[last] + 1])
        cursor = last + 1
    return spans


def _locate_section_bodies(text: str, sections: Sequence[Section]) -> dict[int, int]:
    """``{id(section): offset of its body in text}`` for the sections whose body is found.

    Both splitters cut a document into ordered, non-overlapping slices and then ``strip()``
    each one, so a forward-scanning ``find`` over the flat list recovers exactly where each
    body sat — the cursor is what stops a section whose text repeats verbatim later in the
    document from being located at the wrong copy.

    Keyed by ``id`` because :class:`~jmfts_core.structural_splitting.Section` is a
    dataclass with a generated ``__eq__`` and therefore unhashable, and because the tree
    :func:`~jmfts_core.structural_splitting.nest` builds holds these very objects — so
    identity is what connects the flat list this reads to the nodes the writer walks. The
    dict lives only as long as the write does.

    A body that is not found is simply absent from the result, and its chunks then carry no
    ``source_span``. That is a report, not a silent default: the structure task's detail
    counts the chunks that got one.
    """
    offsets: dict[int, int] = {}
    cursor = 0
    for section in sections:
        body = (section.content or "").strip()
        if not body:
            continue
        found = text.find(body, cursor)
        if found < 0:
            continue
        offsets[id(section)] = found
        cursor = found + len(body)
    return offsets


def _coverage(sections: Sequence[Section]) -> tuple[int, int, int]:
    """``(covered, uncovered, gap_regions)`` in non-whitespace characters.

    Spec 3.3 defines ``coverage`` as the fraction of extracted text assigned to a declared
    or inferred section, so what counts as covered is text under a TITLE. An untitled
    region is text the rung could not attribute to anything — a title page, or the whole
    document when no rung found a boundary in it — and it is the gap the lower rungs exist
    to claim.

    Whitespace is excluded from both sides so that a document's coverage does not move
    when extraction changes how it lays paragraphs out.
    """
    covered = uncovered = gap_regions = 0
    for section in sections:
        weight = sum(1 for char in (section.content or "") if not char.isspace())
        if section.title:
            covered += weight
        else:
            uncovered += weight
            if weight:
                gap_regions += 1
    return covered, uncovered, gap_regions


def _scope_node(session: Session, task: TaskQueue, task_type: str) -> Document:
    doc = session.get(Document, task.scope_document_id)
    if doc is None:
        raise ValueError(
            f"{task_type} is scoped to document {task.scope_document_id}, which does not exist"
        )
    return doc
