"""Advisory task types, and ``citation`` for PDF. ``OFFICE_SPEC.md`` Part 5, steps 1 and 2.

Three things are guarded here, and they fail in different ways:

* **Advisory** — the one assignment ``fail`` skips. The test that matters is not that the
  flag is read but that the NODE survives: a file whose ``citation`` died permanently must
  still settle, still hold its text, and still carry the failure in its log. The negative
  half is asserted alongside it, because an "advisory" that applied to every task would
  pass every positive test while removing the mechanism that ends a broken ingestion.

* **The anchor is right, not merely present.** ``anchor_for_span`` is unit-tested over a
  synthetic block map, and the end-to-end test then re-opens the PDF and asks PyMuPDF
  whether the chunk's own words are actually inside the rectangle that was written. An
  anchor that is plausible and wrong is the specific failure Part 5 says is worse than no
  anchor at all, and only the second check can see it.

* **Partial recovery completes.** A chunk that cannot be placed leaves a reason on the node
  and a count in the attempt detail, and the task is ``completed``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from jmfts_core.citation_tasks import (
    ANCHOR_KEY,
    ANCHOR_KIND_PDF,
    ANCHOR_UNRESOLVED_KEY,
    UNRESOLVED_NO_BLOCK,
    UNRESOLVED_NO_SPAN,
    anchor_for_span,
    run_citation,
)
from jmfts_client.contracts.upload import UploadedFile
from jmfts_core.ingest_tasks import (
    TASK_CITATION,
    TASK_EXTRACT_TEXT,
    TASK_PROBE,
    TASK_STRUCTURE_DECLARED,
)
from jmfts_core.models.document import Document, SETTLED_FAILED, SETTLED_SETTLED
from jmfts_core.models.task_queue import (
    ADVISORY_TASK_TYPES,
    TASK_FAILED,
    WRITE_SELF,
    WRITE_SUBTREE,
)
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.task_queue import TaskQueueRepository
from jmfts_core.services.ingest_service import IngestService
from jmfts_core.structure_tasks import USETYPE_CHUNK
from jmfts_core.task_errors import ErrorType
from tests.conftest import drain_ingest_queue

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_citation_pdf() -> bytes:
    """Three pages, one outline entry per page, several paragraphs on each.

    Several paragraphs because a page with one block cannot tell a rectangle that is right
    from one that is merely the whole page. Distinct wording per paragraph because the
    end-to-end check searches the page for the chunk's own words, and short enough to fit
    the page width because ``insert_text`` does not wrap — an overflowing line would run off
    the media box and the rectangle under test would be an artefact of that rather than of
    the code.
    """
    pymupdf = pytest.importorskip("pymupdf")

    doc = pymupdf.open()
    for index in range(3):
        page = doc.new_page()
        page.insert_text((50, 60), f"Chapter {index}", fontsize=24)
        for paragraph in range(3):
            page.insert_text(
                (50, 120 + paragraph * 60),
                f"Sentence {paragraph} of chapter {index} reads distinctly.",
                fontsize=12,
            )
    doc.set_toc([[1, f"Chapter {index}", index + 1] for index in range(3)])
    return doc.tobytes()


@pytest.fixture(scope="module")
def citation_pdf() -> bytes:
    return _make_citation_pdf()


def _upload(session, data, filename="citations.pdf"):
    return IngestService(session).upload_file(
        UploadedFile(data=data, filename=filename, content_type="application/pdf")
    )


def _chunks(session, root_id: int) -> list[Document]:
    return list(
        session.execute(
            select(Document)
            .where(Document.path.contains([root_id]))
            .where(Document.usetype == USETYPE_CHUNK)
            .order_by(Document.id)
        )
        .scalars()
        .all()
    )


# ---------------------------------------------------------------------------
# 1. The declaration — one string, spelled in two modules that cannot import each other
# ---------------------------------------------------------------------------


class TestAdvisoryDeclaration:
    def test_citation_is_declared_advisory(self):
        """``models.task_queue`` cannot import ``ingest_tasks``, so the name is spelled
        twice. This is what stops the two spellings from drifting apart — a rename that
        touched only one of them would silently turn citation back into a blocking task."""
        assert TASK_CITATION in ADVISORY_TASK_TYPES

    def test_the_tasks_that_must_never_be_advisory_are_not(self):
        """The rule the constant states: a task may be advisory only if a node without its
        output is still correct. A file node with no text is not a document."""
        for task_type in (TASK_PROBE, TASK_EXTRACT_TEXT, TASK_STRUCTURE_DECLARED):
            assert task_type not in ADVISORY_TASK_TYPES


# ---------------------------------------------------------------------------
# 2. What `fail` does differently, and what it does not
# ---------------------------------------------------------------------------


class TestAdvisoryFailure:
    def _permanently_fail(self, session, node_id: int, task_type: str, write_mode: str):
        tasks = TaskQueueRepository(session)
        task = tasks.enqueue(task_type, node_id, write_mode, service_badge=None)
        claimed = tasks.claim_next("test-worker")
        assert claimed is not None and claimed.id == task.id
        tasks.mark_running(claimed)
        return tasks.fail(claimed, error="no rectangles", error_type=ErrorType.PERMANENT)

    def test_a_permanent_advisory_failure_leaves_the_node_settled_state_alone(self, db_session):
        node = DocumentRepository(db_session).create(
            title="a file", content="text", settled=SETTLED_SETTLED, auto_embed=False
        )
        db_session.flush()

        self._permanently_fail(db_session, node.id, TASK_CITATION, WRITE_SUBTREE)

        db_session.refresh(node)
        assert node.settled != SETTLED_FAILED

    def test_a_permanent_ordinary_failure_still_ends_the_node(self, db_session):
        """The negative half. Without it an over-broad advisory set passes every test
        above while removing the mechanism spec 2.1 relies on."""
        node = DocumentRepository(db_session).create(
            title="a file", content="text", settled=SETTLED_SETTLED, auto_embed=False
        )
        db_session.flush()

        self._permanently_fail(db_session, node.id, TASK_EXTRACT_TEXT, WRITE_SELF)

        db_session.refresh(node)
        assert node.settled == SETTLED_FAILED

    def test_the_failure_is_recorded_exactly_as_any_other(self, db_session):
        """Advisory changes ONE assignment. The queue row, the retry budget and the
        durable attempt record are all the same, because the failure is just as real."""
        node = DocumentRepository(db_session).create(
            title="a file", content="text", auto_embed=False
        )
        db_session.flush()

        record = self._permanently_fail(db_session, node.id, TASK_CITATION, WRITE_SUBTREE)

        assert record.status == "failed"
        assert record.error_type == ErrorType.PERMANENT.value
        assert record.error == "no rectangles"
        queued = TaskQueueRepository(db_session).unfinished_tasks_for(node.id)
        assert queued == [], "a permanently failed task must not still count as unfinished"

    def test_the_node_can_still_settle_over_a_failed_advisory_task(self, db_session):
        """The point of the whole mechanism: a document whose rectangles could not be
        recovered is a perfectly good searchable document."""
        from jmfts_core.settling import NO_ROLLUP, settle_node

        node = DocumentRepository(db_session).create(
            title="a file", content="text", auto_embed=False
        )
        db_session.flush()
        self._permanently_fail(db_session, node.id, TASK_CITATION, WRITE_SUBTREE)

        step = settle_node(db_session, node.id, NO_ROLLUP)

        assert step.settled is True
        assert node.settled == SETTLED_SETTLED


# ---------------------------------------------------------------------------
# 3. anchor_for_span — the geometry, with no database and no PDF
# ---------------------------------------------------------------------------


def _blocks(*spec) -> tuple[list[dict], list[int]]:
    blocks = [
        {"page": page, "bbox": list(bbox), "start": start, "end": end}
        for page, bbox, start, end in spec
    ]
    return blocks, [block["start"] for block in blocks]


class TestAnchorForSpan:
    def test_a_span_inside_one_block_takes_that_blocks_rectangle(self):
        blocks, starts = _blocks((0, (10, 20, 30, 40), 0, 100))
        anchor, code = anchor_for_span(blocks, starts, 10, 50)
        assert code is None
        assert anchor == {"kind": ANCHOR_KIND_PDF, "page": 0, "bbox": [10, 20, 30, 40]}

    def test_a_span_over_several_blocks_on_one_page_takes_their_union(self):
        blocks, starts = _blocks(
            (0, (10, 20, 30, 40), 0, 50),
            (0, (5, 60, 80, 90), 52, 100),
        )
        anchor, code = anchor_for_span(blocks, starts, 40, 60)
        assert code is None
        assert anchor["page"] == 0
        assert anchor["bbox"] == [5, 20, 80, 90]
        assert "continues" not in anchor

    def test_a_span_that_runs_past_a_page_break_says_so_and_still_anchors(self):
        """Part 5 lists a chunk spanning a page break as a recovery failure, and for the
        SEARCH-based office path it is. For a source PDF the span is exact and the page it
        begins on is known, so declining to anchor would discard a correct answer —
        `continues` is what keeps a partial rectangle from reading as a complete one."""
        blocks, starts = _blocks(
            (0, (10, 20, 30, 40), 0, 50),
            (1, (10, 20, 30, 40), 52, 100),
            (2, (10, 20, 30, 40), 102, 150),
        )
        anchor, code = anchor_for_span(blocks, starts, 40, 120)
        assert code is None
        assert anchor["page"] == 0
        assert anchor["continues"] == [1, 2]

    def test_a_span_touching_no_block_is_reported_rather_than_guessed(self):
        blocks, starts = _blocks((0, (10, 20, 30, 40), 0, 50))
        anchor, code = anchor_for_span(blocks, starts, 50, 52)
        assert anchor is None
        assert code == UNRESOLVED_NO_BLOCK

    def test_a_zero_width_touch_at_a_boundary_is_not_an_overlap(self):
        """Half-open intervals on both sides. A chunk that starts exactly where a block
        ends does not belong to that block, and counting it would drag the previous
        paragraph — possibly on the previous page — into the rectangle."""
        blocks, starts = _blocks(
            (0, (0, 0, 1, 1), 0, 50),
            (1, (9, 9, 9, 9), 52, 100),
        )
        anchor, code = anchor_for_span(blocks, starts, 52, 60)
        assert code is None
        assert anchor["page"] == 1
        assert "continues" not in anchor


# ---------------------------------------------------------------------------
# 4. End to end: the rectangle really does contain the words
# ---------------------------------------------------------------------------


class TestCitationOverARealPdf:
    def test_every_chunk_is_anchored_and_the_rectangle_holds_its_text(
        self, db_session, citation_pdf
    ):
        """The check that a plausible-but-wrong anchor cannot pass: the words are looked
        for on the page the anchor names, and the rectangle they are found in has to be
        inside the rectangle that was recorded."""
        pymupdf = pytest.importorskip("pymupdf")

        response = _upload(db_session, citation_pdf)
        drain_ingest_queue(db_session, max_tasks=200)

        node = DocumentRepository(db_session).get(response.document_id)
        assert node.settled == SETTLED_SETTLED
        chunks = _chunks(db_session, node.id)
        assert chunks, "the structure rung wrote no chunks"

        source = pymupdf.open(stream=citation_pdf, filetype="pdf")
        try:
            for chunk in chunks:
                anchor = chunk.structured_content.get(ANCHOR_KEY)
                assert anchor is not None, chunk.structured_content.get(ANCHOR_UNRESOLVED_KEY)
                assert anchor["kind"] == ANCHOR_KIND_PDF

                page = source[anchor["page"]]
                recorded = pymupdf.Rect(anchor["bbox"])
                # The first sentence of the chunk, with the markdown emphasis and heading
                # markers extraction added stripped back off — `search_for` matches the
                # glyphs on the page, which never carried them.
                needle = chunk.content.lstrip("# ").split(". ")[0].strip()
                hits = page.search_for(needle)
                assert hits, f"{needle!r} is not on page {anchor['page']}"
                # Half a point of slack for the two-decimal rounding the anchor stores;
                # the distinction this assertion is for — one paragraph's rectangle versus
                # the whole page's — is tens of points wide.
                recorded += (-0.5, -0.5, 0.5, 0.5)
                assert recorded.contains(hits[0]), (
                    f"chunk {chunk.id}'s anchor {anchor['bbox']} does not contain the "
                    f"rectangle {list(hits[0])} its own words occupy"
                )
        finally:
            source.close()

    def test_the_attempt_detail_counts_what_was_and_was_not_placed(self, db_session, citation_pdf):
        response = _upload(db_session, citation_pdf)
        drain_ingest_queue(db_session, max_tasks=200)

        node = DocumentRepository(db_session).get(response.document_id)
        attempts = {entry["task"]: entry for entry in node.structured_content["attempts"]}
        assert TASK_CITATION in attempts, "citation was never enqueued for a PDF"
        attempt = attempts[TASK_CITATION]
        assert attempt["status"] == "completed"

        detail = attempt["detail"]
        assert detail["chunks"] == len(_chunks(db_session, node.id))
        assert detail["anchored"] + sum(detail["unresolved"].values()) == detail["chunks"]
        assert detail["page_count"] == 3
        assert detail["text_blocks"] > 0

    def test_a_chunk_with_no_source_span_is_reported_rather_than_failed(
        self, db_session, citation_pdf
    ):
        """Partial recovery is a COMPLETED task. The chunk keeps no anchor, gains a reason,
        and the rest of the document is anchored around it."""
        response = _upload(db_session, citation_pdf)
        drain_ingest_queue(db_session, max_tasks=200)

        node = DocumentRepository(db_session).get(response.document_id)
        chunks = _chunks(db_session, node.id)
        victim = chunks[0]
        structured = dict(victim.structured_content)
        structured.pop("source_span")
        victim.structured_content = structured
        db_session.flush()

        tasks = TaskQueueRepository(db_session)
        task = tasks.enqueue(TASK_CITATION, node.id, WRITE_SUBTREE, service_badge=None)
        claimed = tasks.claim_next("test-worker")
        assert claimed is not None and claimed.id == task.id
        tasks.mark_running(claimed)
        outcome = run_citation(db_session, claimed)

        assert outcome.status == "completed"
        assert outcome.detail["unresolved"] == {UNRESOLVED_NO_SPAN: 1}
        db_session.refresh(victim)
        assert ANCHOR_KEY not in victim.structured_content
        assert victim.structured_content[ANCHOR_UNRESOLVED_KEY]["code"] == UNRESOLVED_NO_SPAN

    def test_text_that_disagrees_with_a_re_extraction_raises_rather_than_anchoring(
        self, db_session, citation_pdf
    ):
        """An anchor is an offset inverted through a block map. A map derived from
        different text than the chunks were cut from produces rectangles nothing
        downstream could tell were wrong, so this is a raise and not a recovery."""
        response = _upload(db_session, citation_pdf)
        drain_ingest_queue(db_session, max_tasks=200)

        node = DocumentRepository(db_session).get(response.document_id)
        node.content = (node.content or "") + "\n\nan edit nobody extracted"
        db_session.flush()

        tasks = TaskQueueRepository(db_session)
        tasks.enqueue(TASK_CITATION, node.id, WRITE_SUBTREE, service_badge=None)
        claimed = tasks.claim_next("test-worker")
        tasks.mark_running(claimed)

        with pytest.raises(ValueError, match="different text"):
            run_citation(db_session, claimed)

    def test_a_failed_citation_does_not_take_the_document_with_it(self, db_session, citation_pdf):
        """The two halves of this lane meeting: citation raises, the worker classifies it,
        and the file node keeps its text, its chunks and its settled state."""
        response = _upload(db_session, citation_pdf)
        drain_ingest_queue(db_session, max_tasks=200)

        node = DocumentRepository(db_session).get(response.document_id)
        content_before = node.content
        chunks_before = len(_chunks(db_session, node.id))

        tasks = TaskQueueRepository(db_session)
        task = tasks.enqueue(TASK_CITATION, node.id, WRITE_SUBTREE, service_badge=None)
        claimed = tasks.claim_next("test-worker")
        tasks.mark_running(claimed)
        tasks.fail(claimed, error="boom", error_type=ErrorType.PERMANENT)

        db_session.refresh(node)
        assert node.settled != SETTLED_FAILED
        assert node.content == content_before
        assert len(_chunks(db_session, node.id)) == chunks_before
        assert db_session.get(type(task), task.id).status == TASK_FAILED
