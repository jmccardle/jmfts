"""A spreadsheet row too long to embed becomes a container over its cells.

``INGEST_SPEC.md`` 8.4 writes one node per row and gives it the row's labelled prose as
``content``. That is the whole row in one node, and a sheet whose columns hold paragraphs
rather than values produces a node over the token/maxsim window — which ``embed`` refuses,
permanently, so the row ends with no vector at all and its ancestors never settle. On the
reference corpus that is 66 of 4,601 record nodes, across two sheets, and every one of them
is a row whose four short identifier columns sit beside one column holding a document.

The rule under test is the one the rest of the tree already follows: **a node whose text
does not fit carries no content and gets children instead.** A ``section`` holds no prose
and its chunks do; ``run_summarize`` gives such a node a document vector over the
concatenation of its children and no token vectors, because those belong to the nodes whose
content the text actually is (``rollup_tasks.store_effective_content``). A record is the
same shape one level down, and a cell is the same shape below that.

Three levels, one rule, applied twice:

===================  ==================================  =========================
node                 fits the token/maxsim window        does not fit
===================  ==================================  =========================
``record``           leaf, content, vector + maxsim      container over ``cell``
``cell``             leaf, content, vector + maxsim      container over ``chunk``
``chunk``            always — ``chunk_to_fit`` says so   —
===================  ==================================  =========================

The builder half is a pure function and is tested without a workbook, which is what
:mod:`jmfts_core.sheet_records`' own docstring promises. The task half runs the real queue,
because the container's vector is not written by ``extract:sheet`` at all — it arrives from
``summarize`` at the settling boundary, and only a drain with the rollup planner shows that.
"""

from __future__ import annotations

import io

import pytest
from sqlalchemy import select

pytest.importorskip("openpyxl", reason="the office extra is not installed")

import openpyxl  # noqa: E402

import jmfts_core.ingest_tasks  # noqa: E402,F401  (import order; see the module cycle)
from jmfts_client.contracts.upload import UploadedFile  # noqa: E402
from jmfts_core.models.document import (  # noqa: E402
    Document,
    SETTLED_SETTLED,
    USETYPE_CELL,
    USETYPE_CHUNK,
    USETYPE_RECORD,
    USETYPE_SHEET,
)
from jmfts_core.repositories.document import DocumentRepository  # noqa: E402
from jmfts_core.repositories.evidence import EvidenceRepository  # noqa: E402
from jmfts_core.rollup_tasks import IngestRollupPlanner, METHOD_CONCATENATED  # noqa: E402
from jmfts_core.services.ingest_service import IngestService  # noqa: E402
from jmfts_core.sheet_records import (  # noqa: E402
    CellDidNotSplit,
    Record,
    plan_record,
)

from tests.conftest import drain_ingest_queue  # noqa: E402

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

#: Deep inside the long cell, past any point a single chunk could reach from the start.
NEEDLE = "quadraphonic"


# ---------------------------------------------------------------------------
# The builder — a pure function of a record and two callables
# ---------------------------------------------------------------------------


def _fits(limit: int):
    """``fits`` as the embedding service supplies it, with characters standing in for
    tokens. The real predicate tokenises; nothing in :mod:`jmfts_core.sheet_records`
    depends on which measurement it is, which is what lets this be tested without a
    model."""
    return lambda text: len(text) <= limit


def _chunk(size: int):
    """``chunk`` as ``EmbeddingService.chunk_to_fit`` supplies it: pieces that each fit."""
    return lambda text: [text[i : i + size] for i in range(0, len(text), size)]


def _record(**values) -> Record:
    from jmfts_core.sheet_records import build_content

    return Record(row_index=7, record=values, cells={}, content=build_content(values))


class TestPlanRecord:
    def test_a_record_that_fits_keeps_its_content_and_gets_no_cells(self):
        record = _record(Designator="R59", Device="0603WAF4700T5E")
        plan = plan_record(record, fits=_fits(1000), chunk=_chunk(100))

        assert plan.content == record.content
        assert plan.cells == ()

    def test_a_record_that_does_not_fit_holds_no_content_and_gets_one_cell_per_column(self):
        record = _record(Identifier="AC-2", Discussion="d" * 500)
        plan = plan_record(record, fits=_fits(200), chunk=_chunk(100))

        assert plan.content is None
        assert [cell.key for cell in plan.cells] == ["Identifier", "Discussion"]

    def test_a_cell_that_fits_is_a_leaf_carrying_its_own_label(self):
        record = _record(Identifier="AC-2", Discussion="d" * 500)
        plan = plan_record(record, fits=_fits(200), chunk=_chunk(100))
        first = plan.cells[0]

        assert first.content == "Identifier: AC-2."
        assert first.pieces == ()

    def test_a_cell_that_does_not_fit_holds_no_content_and_gets_pieces_that_do(self):
        record = _record(Identifier="AC-2", Discussion="d" * 500)
        plan = plan_record(record, fits=_fits(200), chunk=_chunk(100))
        long = plan.cells[1]

        assert long.content is None
        assert len(long.pieces) == 6
        assert all(len(piece) <= 200 for piece in long.pieces)

    def test_the_leaves_carry_every_character_the_row_held(self):
        """The property the container BM25 pass rests on. ``f(t, container)`` is the sum
        over the frontier, so a term that was in the row has to still be in some leaf —
        otherwise splitting a row would silently drop postings the unsplit row had."""
        record = _record(Identifier="AC-2", Discussion="alpha " * 200 + NEEDLE)
        plan = plan_record(record, fits=_fits(200), chunk=_chunk(100))

        leaves = []
        for cell in plan.cells:
            leaves.extend(cell.pieces or [cell.content])
        assert NEEDLE in "".join(leaves)

    def test_a_chunker_that_returns_a_piece_that_still_does_not_fit_raises(self):
        """FAIL EARLY. The alternative is an ``embed`` task that fails permanently one
        rung later, which is the defect this split exists to close."""
        record = _record(Discussion="d" * 500)
        with pytest.raises(CellDidNotSplit):
            plan_record(record, fits=_fits(200), chunk=lambda text: [text])

    def test_a_chunker_that_returns_nothing_raises(self):
        record = _record(Discussion="d" * 500)
        with pytest.raises(CellDidNotSplit):
            plan_record(record, fits=_fits(200), chunk=lambda text: [])


# ---------------------------------------------------------------------------
# The task — on the real queue, with the rollup planner
# ---------------------------------------------------------------------------


@pytest.fixture
def long_row_workbook() -> bytes:
    """One sheet, one short row and one row whose ``Discussion`` column is a document.

    ``"word "`` repeated is deliberately poor prose: the point is length in tokens, and a
    fixture that has to be read is a fixture whose failure mode is a reader's patience.
    """
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Controls"
    sheet.append(["Identifier", "Name", "Discussion"])
    sheet.append(["AC-1", "Policy and Procedures", "Short enough to stay one node."])
    sheet.append(
        [
            "AC-2",
            "Account Management",
            "The organization manages accounts. " * 200 + f"The format is {NEEDLE}.",
        ]
    )
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _children(session, node_id: int, usetype: str) -> list:
    return list(
        session.execute(
            select(Document)
            .where(Document.parent_id == node_id, Document.usetype == usetype)
            .order_by(Document.id)
        )
        .scalars()
        .all()
    )


@pytest.fixture
def split_sheet(db_session, long_row_workbook):
    """Upload, drain WITH the rollup planner, and hand back the sheet node.

    ``IngestRollupPlanner`` and not ``NO_ROLLUP``: a container's vector is written by
    ``summarize`` at the settling boundary, so a drain without the planner would show the
    children and none of the roll-up that makes the container retrievable.
    """
    response = IngestService(db_session).upload_file(
        UploadedFile(data=long_row_workbook, filename="controls.xlsx", content_type=XLSX_MIME)
    )
    drain_ingest_queue(db_session, planner=IngestRollupPlanner(), max_tasks=400)
    file_node = DocumentRepository(db_session).get(response.document_id)
    return next(iter(_children(db_session, file_node.id, USETYPE_SHEET)))


class TestExtractSheetSplitsAnOversizedRow:
    def test_the_short_row_is_still_one_leaf(self, db_session, split_sheet):
        records = _children(db_session, split_sheet.id, USETYPE_RECORD)
        short = records[0]

        assert short.content is not None
        assert _children(db_session, short.id, USETYPE_CELL) == []

    def test_the_long_row_holds_no_content_and_has_one_cell_per_column(
        self, db_session, split_sheet
    ):
        records = _children(db_session, split_sheet.id, USETYPE_RECORD)
        long = records[1]
        cells = _children(db_session, long.id, USETYPE_CELL)

        assert long.content is None
        assert len(cells) == 3

    def test_the_long_cell_became_a_container_over_chunks(self, db_session, split_sheet):
        records = _children(db_session, split_sheet.id, USETYPE_RECORD)
        cells = _children(db_session, records[1].id, USETYPE_CELL)
        discussion = next(cell for cell in cells if cell.title.endswith("Discussion"))

        assert discussion.content is None
        assert len(_children(db_session, discussion.id, USETYPE_CHUNK)) > 1

    def test_every_node_under_the_long_row_has_a_vector(self, db_session, split_sheet):
        """The defect, stated as its absence. 66 nodes on the reference corpus reach here
        with ``embed`` NULL and an ``embed`` task failed permanent."""
        records = _children(db_session, split_sheet.id, USETYPE_RECORD)
        subtree = DocumentRepository(db_session).get_subtree(records[1].id)

        assert [node.id for node in subtree if node.embed is None] == []

    def test_the_row_container_was_concatenated_rather_than_summarized(
        self, db_session, split_sheet
    ):
        """No LLM. The cells fit the 8192-token document window when joined, so
        ``run_summarize`` takes the concatenation — which is the same text the unsplit row
        held, with fewer layers of interpretation between a query and the document."""
        records = _children(db_session, split_sheet.id, USETYPE_RECORD)
        block = EvidenceRepository(db_session).read(records[1].id, "effective_content")

        assert block["method"] == METHOD_CONCATENATED

    def test_the_whole_subtree_settled(self, db_session, split_sheet):
        records = _children(db_session, split_sheet.id, USETYPE_RECORD)
        subtree = DocumentRepository(db_session).get_subtree(records[1].id, include_in_flight=True)

        assert [node.id for node in subtree if node.settled != SETTLED_SETTLED] == []

    def test_a_cell_node_keeps_its_column_name_and_typed_value(self, db_session, split_sheet):
        """The JSON the record kept, one level down. A retrieval hit on a cell has to say
        which column it is, or the identifier columns beside it are the only way to tell
        and a split row has lost what an unsplit one carried in every sentence."""
        records = _children(db_session, split_sheet.id, USETYPE_RECORD)
        cells = _children(db_session, records[1].id, USETYPE_CELL)
        block = EvidenceRepository(db_session).read(cells[0].id, "cell")

        assert block["column"] == "Identifier"
        assert block["value"] == "AC-2"
        assert block["row_index"] == 3
