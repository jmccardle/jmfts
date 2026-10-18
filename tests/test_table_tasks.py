"""``extract:tables``: a PDF's inline tables as ``table`` nodes. ``INGEST_SPEC.md`` Part 4.

Three layers, as in ``test_sheet_tasks.py``.

The **spans** — ``pdf_to_markdown`` records where each table it rendered sits in the
markdown, ``_extract_pdf`` persists them in the extraction RECORD. No database.

The **handler**, driven directly on a hand-built file node, so each fail-early check can be
reached with exactly the contradiction it exists for.

The **pipeline**, end to end on the real queue: upload, drain, and look at the tree. That is
the half that would notice the re-plan not firing, a table child left in flight, or a file
node that settled before its tables' ``embed`` ran.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

pymupdf = pytest.importorskip("pymupdf")

import jmfts_core.ingest_tasks  # noqa: E402,F401  (import order; see the module cycle)
from jmfts_client.contracts.upload import UploadedFile  # noqa: E402
from jmfts_core.ingest_tasks import (  # noqa: E402
    TASK_CITATION,
    TASK_EMBED,
    TASK_EXTRACT_TABLES,
    TASK_EXTRACT_TEXT,
    TASK_PROBE,
    TASK_STRUCTURE_INFERRED,
)
from jmfts_core.models.document import (  # noqa: E402
    SETTLED_IN_FLIGHT,
    SETTLED_SETTLED,
    USETYPE_TABLE,
    Document,
)
from jmfts_core.pdf_extraction import _table_to_markdown, pdf_to_markdown  # noqa: E402
from jmfts_core.repositories.document import DocumentRepository  # noqa: E402
from jmfts_core.repositories.evidence import EvidenceRepository  # noqa: E402
from jmfts_core.repositories.task_queue import TaskQueueRepository  # noqa: E402
from jmfts_core.services.document_service import DocumentService  # noqa: E402
from jmfts_core.services.ingest_service import IngestService  # noqa: E402
from jmfts_core.models.task_queue import TaskQueue  # noqa: E402
from jmfts_core.structure_tasks import _extract_pdf, _replan_after_extraction  # noqa: E402
from jmfts_core.table_tasks import run_extract_tables, table_key  # noqa: E402
from tests.conftest import drain_ingest_queue  # noqa: E402

CELLS = [["Model", "BLEU", "Cost"], ["Base", "27.3", "1.0"], ["Big", "28.4", "2.3"]]


def _ruled_table(page, cells, x0=50, y0=140, cw=120, rh=24) -> None:
    """A grid of drawn lines with text in each cell: what `find_tables` detects."""
    for r in range(len(cells) + 1):
        page.draw_line((x0, y0 + r * rh), (x0 + cw * len(cells[0]), y0 + r * rh))
    for c in range(len(cells[0]) + 1):
        page.draw_line((x0 + c * cw, y0), (x0 + c * cw, y0 + rh * len(cells)))
    for r, row in enumerate(cells):
        for c, value in enumerate(row):
            page.insert_text((x0 + c * cw + 6, y0 + r * rh + 16), value, fontsize=10)


def _table_pdf(table_pages=(1,), pages=2) -> bytes:
    """``pages`` pages of prose, with :data:`CELLS` drawn as a ruled table on each of
    ``table_pages``. The default puts it on the SECOND page, so its span has to be shifted
    by the first page's length — the cursor arithmetic is the thing worth catching."""
    doc = pymupdf.open()
    for number in range(pages):
        page = doc.new_page()
        page.insert_text((50, 60), f"Section {number + 1}", fontsize=18)
        page.insert_text(
            (50, 100),
            "Body text long enough that the average characters per page is comfortably "
            "above the scanned-document threshold.",
            fontsize=10,
        )
        if number in table_pages:
            _ruled_table(page, CELLS)
        page.insert_text((50, 260), "Text after the table.", fontsize=10)
    return doc.tobytes()


@pytest.fixture(scope="module")
def table_pdf() -> bytes:
    return _table_pdf()


# ---------------------------------------------------------------------------
# The spans
# ---------------------------------------------------------------------------


class TestTheSpans:
    def test_one_span_per_rendered_table_shifted_into_document_offsets(self, table_pdf):
        markdown, meta = pdf_to_markdown(table_pdf)

        assert meta["pages_with_tables"] == [1]
        assert meta["tables_found"] == 1
        (span,) = meta["tables"]
        assert span["page"] == 1 and span["index"] == 0
        assert (span["rows"], span["cols"]) == (3, 3)
        # Past the first page: the page cursor was applied.
        assert span["start"] > meta["page_offsets"][1]
        assert markdown[span["start"] : span["end"]] == _table_to_markdown(CELLS)

    def test_the_span_is_the_same_one_text_blocks_carries(self, table_pdf):
        """Not a second measurement: the table's segment, labelled."""
        _markdown, meta = pdf_to_markdown(table_pdf)
        (span,) = meta["tables"]
        assert {"page": 1, "bbox": span["bbox"], "start": span["start"], "end": span["end"]} in (
            meta["text_blocks"]
        )

    def test_a_document_with_no_tables_records_none(self):
        _markdown, meta = pdf_to_markdown(_table_pdf(table_pages=()))
        assert meta["tables"] == [] and meta["pages_with_tables"] == []

    def test_the_spans_are_in_the_record_not_the_detail(self, table_pdf):
        """The record is what a later task plans and reads from; the detail is the log."""
        extracted = _extract_pdf(table_pdf)
        assert extracted.record["tables_found"] == 1
        assert [t["page"] for t in extracted.record["tables"]] == [1]
        assert "tables" not in extracted.detail
        start, end = extracted.record["tables"][0]["start"], extracted.record["tables"][0]["end"]
        assert extracted.text[start:end] == _table_to_markdown(CELLS)


# ---------------------------------------------------------------------------
# The handler, on a hand-built node
# ---------------------------------------------------------------------------


class _ScopedTask:
    """The one field a handler reads off its queue row (see ``test_sheet_tasks``)."""

    def __init__(self, document_id: int):
        self.scope_document_id = document_id
        self.params: dict = {}


TABLE_MD = _table_to_markdown(CELLS)
PROSE = "# Results\n\nSome prose before the table."


def _file_node(session, *, tables=None, pages=None, content=None, tables_found=None):
    """A PDF file node as `extract:text` leaves it: content, `matched`, `extraction`."""
    content = content if content is not None else f"{PROSE}\n\n{TABLE_MD}\n\nAfter."
    start = len(PROSE) + 2
    if tables is None:
        tables = [
            {
                "page": 0,
                "index": 0,
                "bbox": [50.0, 140.0, 410.0, 212.0],
                "start": start,
                "end": start + len(TABLE_MD),
                "rows": 3,
                "cols": 3,
            }
        ]
    node = DocumentRepository(session).create(
        title="paper.pdf",
        content=content,
        usetype="file",
        auto_embed=False,
        settled=SETTLED_IN_FLIGHT,
    )
    evidence = EvidenceRepository(session)
    evidence.write(
        node.id,
        "matched",
        {"format": "pdf", "patterns": {"has_text_layer": True}, "probed_at": "2026-10-13"},
    )
    evidence.write(
        node.id,
        "extraction",
        {
            "source": "pdf_text_layer",
            "characters": len(content),
            "pages_with_tables": (
                pages if pages is not None else sorted({t["page"] for t in tables})
            ),
            "tables_found": tables_found if tables_found is not None else len(tables),
            "tables": tables,
            "toc": [],
        },
    )
    session.flush()
    return node


def _children(session, node_id: int) -> list[Document]:
    return list(
        session.execute(select(Document).where(Document.parent_id == node_id).order_by(Document.id))
        .scalars()
        .all()
    )


class TestTheHandler:
    def test_each_span_becomes_a_table_child_byte_for_byte(self, db_session):
        node = _file_node(db_session)

        outcome = run_extract_tables(db_session, _ScopedTask(node.id))

        (child,) = _children(db_session, node.id)
        assert child.usetype == USETYPE_TABLE
        assert child.produced_by == TASK_EXTRACT_TABLES
        assert child.content == TABLE_MD
        assert child.settled == SETTLED_IN_FLIGHT
        evidence = EvidenceRepository(db_session)
        table = evidence.read(child.id, "table")
        assert table["key"] == table_key(0, 0) == "p0.t0"
        assert table["page"] == 0 and table["bbox"] == [50.0, 140.0, 410.0, 212.0]
        start = len(PROSE) + 2
        assert evidence.read(child.id, "source_span") == [start, start + len(TABLE_MD)]
        assert outcome.detail["tables_written"] == 1
        assert outcome.detail["pages"] == [0]

    def test_each_child_is_anchored_at_its_page_and_box(self, db_session):
        """0.7.1, preview audit #7: the page and box were on the `table` row only, which
        `/region?anchor=true` does not read, so a PDF table could not be previewed. The row
        is the chunk anchor's shape, with no `of` because it is the source PDF's geometry."""
        node = _file_node(db_session)
        run_extract_tables(db_session, _ScopedTask(node.id))

        (child,) = _children(db_session, node.id)
        anchor = EvidenceRepository(db_session).read(child.id, "source_anchor")
        assert anchor == {"kind": "pdf", "page": 0, "bbox": [50.0, 140.0, 410.0, 212.0]}
        found = DocumentService(db_session)._pdf_anchor(child.id)
        assert (found.page, found.of) == (0, "source")

    def test_a_recorded_box_that_is_not_a_rectangle_writes_nothing(self, db_session):
        start = len(PROSE) + 2
        node = _file_node(
            db_session,
            tables=[
                {
                    "page": 0,
                    "index": 0,
                    "bbox": [50.0, 140.0],
                    "start": start,
                    "end": start + len(TABLE_MD),
                    "rows": 3,
                    "cols": 3,
                }
            ],
        )
        with pytest.raises(ValueError, match="is not a page and a rectangle"):
            run_extract_tables(db_session, _ScopedTask(node.id))
        assert _children(db_session, node.id) == []

    def test_its_embed_is_planned_through_the_frontier(self, db_session):
        node = _file_node(db_session)
        run_extract_tables(db_session, _ScopedTask(node.id))

        (child,) = _children(db_session, node.id)
        queued = TaskQueueRepository(db_session).unfinished_tasks_for(child.id)
        assert [t.task_type for t in queued] == [TASK_EMBED]
        # A three-row table fits the token window, so it asks for token vectors too.
        assert queued[0].params.get("with_tokens", True) is True

    def test_a_table_past_the_token_window_asks_for_the_document_vector_only(self, db_session):
        """`_write_table`'s rule, through the same helper."""
        cells = [["Key", "Value"]] + [[f"row-{i}", f"value number {i} " * 3] for i in range(80)]
        big = _table_to_markdown(cells)
        start = len(PROSE) + 2
        node = _file_node(
            db_session,
            content=f"{PROSE}\n\n{big}",
            tables=[
                {
                    "page": 0,
                    "index": 0,
                    "bbox": [0, 0, 1, 1],
                    "start": start,
                    "end": start + len(big),
                    "rows": len(cells),
                    "cols": 2,
                }
            ],
        )

        outcome = run_extract_tables(db_session, _ScopedTask(node.id))

        (child,) = _children(db_session, node.id)
        (embed,) = TaskQueueRepository(db_session).unfinished_tasks_for(child.id)
        assert embed.params["with_tokens"] is False
        assert outcome.detail["document_vector_only"] == ["p0.t0"]
        assert EvidenceRepository(db_session).read(child.id, "table")["fits_token_window"] is False

    def test_a_second_run_matches_the_first_and_writes_nothing(self, db_session):
        """A worker that finished after its lease was reaped, and the run it was reaped into,
        both commit. The natural key matches the first run's children; the second writes
        no table and queues no second embed."""
        node = _file_node(db_session)
        run_extract_tables(db_session, _ScopedTask(node.id))
        db_session.flush()

        again = run_extract_tables(db_session, _ScopedTask(node.id))

        (child,) = _children(db_session, node.id)
        assert again.detail["tables_written"] == 0
        assert again.detail["tables_matched"] == ["p0.t0"]
        queued = TaskQueueRepository(db_session).unfinished_tasks_for(child.id)
        assert [t.task_type for t in queued] == [TASK_EMBED]

    def test_a_run_that_matches_only_some_keys_raises(self, db_session):
        """All or none: a partial set is not a re-run to absorb, it is a changed record."""
        node = _file_node(db_session)
        run_extract_tables(db_session, _ScopedTask(node.id))
        (child,) = _children(db_session, node.id)
        evidence = EvidenceRepository(db_session)
        table = evidence.read(child.id, "table")
        evidence.write(child.id, "table", {**table, "key": "p0.t7"})
        db_session.flush()

        with pytest.raises(ValueError, match=r"already has extract:tables children keyed"):
            run_extract_tables(db_session, _ScopedTask(node.id))
        assert len(_children(db_session, node.id)) == 1

    def test_the_re_plan_run_twice_enqueues_one_row(self, db_session):
        """6.1's attempt diff: the second re-plan finds the first one's pending record."""
        node = _file_node(db_session)
        extraction = EvidenceRepository(db_session).read(node.id, "extraction")

        first = _replan_after_extraction(db_session, node, extraction)
        second = _replan_after_extraction(db_session, node, extraction)

        assert list(first["enqueued"]) == [TASK_EXTRACT_TABLES]
        assert first["already_attempted"] == []
        assert second["enqueued"] == {}
        assert second["already_attempted"] == [TASK_EXTRACT_TABLES]
        rows = (
            db_session.execute(
                select(TaskQueue)
                .where(TaskQueue.scope_document_id == node.id)
                .where(TaskQueue.task_type == TASK_EXTRACT_TABLES)
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1

    def test_missing_spans_raise(self, db_session):
        node = _file_node(db_session)
        extraction = EvidenceRepository(db_session).read(node.id, "extraction")
        del extraction["tables"]
        EvidenceRepository(db_session).write(node.id, "extraction", extraction)
        db_session.flush()

        with pytest.raises(ValueError, match="carries no `tables` spans"):
            run_extract_tables(db_session, _ScopedTask(node.id))
        assert _children(db_session, node.id) == []

    def test_a_page_set_that_disagrees_raises(self, db_session):
        node = _file_node(db_session, pages=[0, 3])
        with pytest.raises(ValueError, match="names tables on pages"):
            run_extract_tables(db_session, _ScopedTask(node.id))

    def test_a_count_that_disagrees_raises(self, db_session):
        node = _file_node(db_session, tables_found=2)
        with pytest.raises(ValueError, match="counts 2 tables and carries 1 spans"):
            run_extract_tables(db_session, _ScopedTask(node.id))

    def test_a_span_that_is_not_a_table_raises(self, db_session):
        """Stale offsets: the span lands on prose."""
        node = _file_node(
            db_session,
            tables=[
                {
                    "page": 0,
                    "index": 0,
                    "bbox": [0, 0, 1, 1],
                    "start": 0,
                    "end": len(PROSE),
                    "rows": 3,
                    "cols": 3,
                }
            ],
        )
        with pytest.raises(ValueError, match="offsets are stale"):
            run_extract_tables(db_session, _ScopedTask(node.id))
        assert _children(db_session, node.id) == []

    def test_a_span_past_the_content_raises(self, db_session):
        node = _file_node(
            db_session,
            tables=[
                {
                    "page": 0,
                    "index": 0,
                    "bbox": [0, 0, 1, 1],
                    "start": 10,
                    "end": 10_000,
                    "rows": 3,
                    "cols": 3,
                }
            ],
        )
        with pytest.raises(ValueError, match="not this text's"):
            run_extract_tables(db_session, _ScopedTask(node.id))

    def test_a_row_count_that_disagrees_raises(self, db_session):
        start = len(PROSE) + 2
        node = _file_node(
            db_session,
            tables=[
                {
                    "page": 0,
                    "index": 0,
                    "bbox": [0, 0, 1, 1],
                    "start": start,
                    "end": start + len(TABLE_MD),
                    "rows": 5,
                    "cols": 3,
                }
            ],
        )
        with pytest.raises(ValueError, match="is not a 5-row markdown table"):
            run_extract_tables(db_session, _ScopedTask(node.id))


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------


def _ingest(session, data: bytes) -> Document:
    response = IngestService(session).upload_file(
        UploadedFile(data=data, filename="paper.pdf", content_type="application/pdf")
    )
    drain_ingest_queue(session)
    return DocumentRepository(session).get(response.document_id)


def _attempt(session, node: Document, task: str) -> dict:
    log = DocumentRepository(session).attempt_log(node)
    return next(e for e in log if e["task"] == task)


class TestThePipeline:
    def test_extract_text_re_plans_and_the_tables_land_as_settled_children(
        self, db_session, table_pdf
    ):
        node = _ingest(db_session, table_pdf)

        # Probe could not decide the row, and said who would.
        probe = _attempt(db_session, node, TASK_PROBE)["detail"]
        assert "extract:text measures it" in probe["not_applicable"][TASK_EXTRACT_TABLES]
        # extract:text did, and enqueued it on the same node.
        text = _attempt(db_session, node, TASK_EXTRACT_TEXT)["detail"]
        assert list(text["enqueued"]) == [TASK_EXTRACT_TABLES]
        assert text["deferred"] == {} and text["not_applicable"] == {}
        tables = _attempt(db_session, node, TASK_EXTRACT_TABLES)
        assert tables["status"] == "completed"
        assert tables["detail"]["tables_written"] == 1

        children = [
            c for c in _children(db_session, node.id) if c.produced_by == TASK_EXTRACT_TABLES
        ]
        (child,) = children
        assert child.usetype == USETYPE_TABLE
        assert child.content == _table_to_markdown(CELLS)
        assert child.settled == SETTLED_SETTLED
        assert child.embed is not None, "the table's embed did not run"
        assert [e["task"] for e in DocumentRepository(db_session).attempt_log(child)] == [
            TASK_EMBED
        ]
        assert node.settled == SETTLED_SETTLED
        assert TaskQueueRepository(db_session).unfinished_tasks_for(node.id) == []

    def test_a_table_node_crops_to_its_own_anchor(self, db_session, table_pdf):
        """Preview audit #7, end to end: `/region?anchor=true` on a table node is a picture."""
        node = _ingest(db_session, table_pdf)
        (child,) = [
            c for c in _children(db_session, node.id) if c.produced_by == TASK_EXTRACT_TABLES
        ]
        payload = DocumentService(db_session).get_document_region(child.id, anchor=True)
        assert payload.content.startswith(b"\x89PNG")

    def test_the_rung_is_not_enqueued_twice(self, db_session, table_pdf):
        """The re-plan decides only the rows gated on what extract:text measured."""
        node = _ingest(db_session, table_pdf)
        log = DocumentRepository(db_session).attempt_log(node)
        tasks = [e["task"] for e in log]
        assert tasks.count(TASK_STRUCTURE_INFERRED) == 1
        assert tasks.count(TASK_EXTRACT_TABLES) == 1
        assert tasks.count(TASK_CITATION) == 1

    def test_a_pdf_with_no_tables_enqueues_nothing_and_says_false(self, db_session):
        node = _ingest(db_session, _table_pdf(table_pages=()))

        text = _attempt(db_session, node, TASK_EXTRACT_TEXT)["detail"]
        assert text["enqueued"] == {}
        assert text["not_applicable"][TASK_EXTRACT_TABLES] == "patterns.pages_with_tables is false"
        assert TASK_EXTRACT_TABLES not in [
            e["task"] for e in DocumentRepository(db_session).attempt_log(node)
        ]

    def test_a_text_file_leaves_the_pattern_unmeasured(self, db_session):
        response = IngestService(db_session).upload_file(
            UploadedFile(
                data=b"# Notes\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n",
                filename="notes.md",
                content_type="text/markdown",
            )
        )
        drain_ingest_queue(db_session)
        node = DocumentRepository(db_session).get(response.document_id)

        text = _attempt(db_session, node, TASK_EXTRACT_TEXT)["detail"]
        assert text["enqueued"] == {}
        # extract:text has run, so the promise probe made ("extract:text measures it and
        # re-plans") is not repeated by the task it names: its reader for this format
        # does not measure the pattern, and the reason says so.
        assert text["not_applicable"][TASK_EXTRACT_TABLES] == (
            "patterns.pages_with_tables was not measured: extract:text ran, and it does not "
            "measure it for format 'text'"
        )
        probe = _attempt(db_session, node, TASK_PROBE)["detail"]
        assert "extract:text measures it" in probe["not_applicable"][TASK_EXTRACT_TABLES]
