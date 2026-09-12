"""``profile:sheet`` measures a worksheet. ``INGEST_SPEC.md`` 8.2, 8.3, 8.5 and 8.7.

Four things are under test and they fail for different reasons.

The **measurer** (:mod:`jmfts_core.office.sheets`) turns bytes into 8.3's table. No
database. This is where the header rule, the canonical value form, the bounded distinct
set and the merged-cell count are asked directly, because each of them is a definition and
a definition is worth a test that names it.

The **prose** (:mod:`jmfts_core.sheet_profile`) turns a measurement into 8.5's sentences.
No database and no bytes — it is a pure function of the measurement, which is what lets
8.5's rule about which factoids are permitted be checked without a workbook at all.

The **seam** — ``datasketch`` behind ``require_datasketch`` and nothing else. The packaging
half of that claim lives in ``tests/test_office_packaging.py``, where the rest of the
extras are.

The **task**, on the real queue: upload, drain, and look at the tree. That is the half that
would notice a profile node written under the wrong parent, a sheet node left in flight
with nothing able to settle it, or a shape quietly decided.

**Not in ``tests/corpus``, on purpose.** The corpus runs with no database and no optional
dependency, because it is the fidelity corpus for the PROBER, which reads the ZIP directory
with the standard library. A workbook rich enough to exercise a measurement needs
``openpyxl`` to build, so it is built here and the whole module skips where the ``office``
extra is not installed.
"""

from __future__ import annotations

import datetime
import io

import pytest
from sqlalchemy import select

pytest.importorskip("openpyxl", reason="the office extra is not installed")

import openpyxl  # noqa: E402

import jmfts_core.ingest_tasks  # noqa: E402,F401  (import order; see the module cycle)
from jmfts_client.contracts.upload import UploadedFile  # noqa: E402
from jmfts_core.ingest_tasks import (  # noqa: E402
    TASK_EXTRACT_SHEET,
    TASK_PROFILE_SHEET,
    TASK_STRUCTURE_SHEETS,
)
from jmfts_core.ingest_options import resolve_options  # noqa: E402
from jmfts_core.models.document import (  # noqa: E402
    Document,
    SETTLED_SETTLED,
    USETYPE_SHEET,
    USETYPE_PROFILE,
)
from jmfts_core.models.task_queue import WRITE_CHILDREN  # noqa: E402
from jmfts_core.office.sheets import (  # noqa: E402
    TYPE_DATE,
    TYPE_EMPTY,
    TYPE_NUMBER,
    TYPE_TEXT,
    WorksheetPartMissing,
    canonical,
    column_letter,
    count_merged_cells,
    measure_sheet,
)
from jmfts_core.repositories.document import DocumentRepository  # noqa: E402
from jmfts_core.services.ingest_service import IngestService  # noqa: E402
from jmfts_core.sketch import (  # noqa: E402
    MINHASH_PERMUTATIONS,
    SKETCH_MINHASH,
    column_sketch,
    load_sketch,
)
from jmfts_core.sheet_profile import (  # noqa: E402
    build_profile_content,
    sheet_evidence_block,
)
from jmfts_core.sheet_tasks import run_profile_sheet  # noqa: E402
from tests.conftest import drain_ingest_queue  # noqa: E402

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

#: The document window, which is what `measure_sheet` is handed in production. Spelled here
#: so a reader-level test does not need settings loaded.
DOC_WINDOW = 8192


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _workbook_bytes() -> bytes:
    """Four sheets, each one a different thing 8.3 has to be able to measure.

    ``Pipeline`` is 8.4's ``records`` example in miniature: a header row, an identifier
    column, a closed set, numbers, dates, and one sparse column. ``Coverage`` is 8.4's
    ``matrix`` example, *including its empty top-left corner* — which is the sheet 8.3's
    own header rule cannot see, and the reason ``HeaderEvidence`` exists. ``Notes`` has a
    numeric first row, so neither header holds. ``Blank`` was never written to.
    """
    workbook = openpyxl.Workbook()

    pipeline = workbook.active
    pipeline.title = "Pipeline"
    pipeline.append(["Deal ID", "Account", "Stage", "Value", "Close Date", "Note"])
    pipeline.append(["D-1", "Northwind", "Proposal", 128000, datetime.date(2026, 9, 30), "call"])
    pipeline.append(["D-2", "Contoso", "Closed", 4200.5, datetime.date(2026, 8, 1), None])
    pipeline.append(["D-3", "Northwind", "Proposal", 900, datetime.date(2026, 7, 15), None])
    pipeline.append(["D-4", "Fabrikam", "Prospect", 1000.0, datetime.date(2026, 6, 2), None])

    coverage = workbook.create_sheet("Coverage")
    coverage.append([None, "Chassis Rev C", "Chassis Rev D"])
    coverage.append(["Payload Handling", "X", None])
    coverage.append(["Thermal", None, "X"])
    coverage.merge_cells("A5:B6")

    notes = workbook.create_sheet("Notes")
    notes.append([2026, "quarterly review"])
    notes.append(["attendees", "J. Okafor"])

    workbook.create_sheet("Blank")

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


@pytest.fixture(scope="module")
def workbook_bytes() -> bytes:
    return _workbook_bytes()


@pytest.fixture(scope="module")
def pipeline(workbook_bytes):
    return measure_sheet(workbook_bytes, "Pipeline", render_cell_budget=DOC_WINDOW)


@pytest.fixture(scope="module")
def coverage(workbook_bytes):
    return measure_sheet(workbook_bytes, "Coverage", render_cell_budget=DOC_WINDOW)


def _column(measurement, name: str):
    return next(column for column in measurement.columns if column.name == name)


def _upload(session, data: bytes, filename: str = "measured.xlsx"):
    return IngestService(session).upload_file(
        UploadedFile(data=data, filename=filename, content_type=XLSX_MIME)
    )


def _ingest(session, data: bytes) -> Document:
    response = _upload(session, data)
    drain_ingest_queue(session)
    return DocumentRepository(session).get(response.document_id)


def _children(session, node_id: int) -> list:
    return list(
        session.execute(
            select(Document)
            .where(Document.parent_id == node_id)
            .order_by(Document.position, Document.id)
        )
        .scalars()
        .all()
    )


class _ScopedTask:
    """The two fields a handler reads off its queue row. See ``test_sheet_tasks.py``.

    ``params`` is the RESOLVED ``sheet_profile`` group with the test's overrides on top.
    Since Phase 3 the group is complete on every queue row `plan_frontier` writes, and the
    handler reads ``params["sketch_columns"]`` rather than defaulting — so a stand-in row
    carrying an empty dict would exercise a shape the queue never produces.
    """

    def __init__(self, document_id: int, params: dict | None = None):
        self.scope_document_id = document_id
        self.params: dict = {**resolve_options("xlsx")["sheet_profile"], **(params or {})}


# ---------------------------------------------------------------------------
# Canonical values — the whole correctness of every count below
# ---------------------------------------------------------------------------


class TestCanonical:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (None, None),
            ("", None),
            ("   ", None),
            (" Proposal ", "Proposal"),
            (True, "TRUE"),
            (False, "FALSE"),
            (128000, "128000"),
            (128000.0, "128000"),
            (4200.5, "4200.5"),
            (datetime.date(2026, 9, 30), "2026-09-30"),
            (datetime.datetime(2026, 9, 30, 0, 0), "2026-09-30"),
            (datetime.datetime(2026, 9, 30, 14, 5), "2026-09-30T14:05:00"),
        ],
    )
    def test_one_value_one_string(self, value, expected):
        assert canonical(value) == expected

    def test_an_integral_float_and_an_integer_are_one_value(self):
        """8.6 compares value SETS across sheets. Two columns holding the same identifiers
        agree only if `128000` and `128000.0` reached the comparison as one string."""
        assert canonical(128000) == canonical(128000.0)

    def test_a_bool_is_not_an_integer(self):
        """`bool` subclasses `int`, so the order of the checks is the whole test."""
        assert canonical(True) != canonical(1)

    def test_column_letters(self):
        assert [column_letter(i) for i in (1, 2, 26, 27, 28, 52, 53)] == [
            "A",
            "B",
            "Z",
            "AA",
            "AB",
            "AZ",
            "BA",
        ]


# ---------------------------------------------------------------------------
# 8.3's table
# ---------------------------------------------------------------------------


class TestMeasurements:
    def test_used_range_dimensions_come_from_the_cells(self, pipeline):
        """8.3's `rows`, `cols`. Measured from what holds a value, because `<dimension>`
        is optional and openpyxl reports None for both when it is absent."""
        assert (pipeline.rows, pipeline.cols) == (5, 6)

    def test_what_the_file_declared_is_kept_beside_what_was_measured(self, pipeline):
        """A writer whose dimension disagrees with its own cells is a fact about the file,
        and it is only visible if both numbers survive."""
        assert pipeline.declared_rows == 5
        assert pipeline.declared_cols == 6

    def test_fill_ratio_is_non_empty_over_the_used_range(self, pipeline):
        """One of the 30 cells is empty: `Note` holds a value in one row of four."""
        assert pipeline.non_empty_cells == 27
        assert pipeline.fill_ratio == pytest.approx(27 / 30)

    def test_header_row_is_all_text_all_distinct_no_numerics(self, pipeline):
        assert pipeline.header_row.verdict is True
        assert pipeline.header_row.text_cells == 6
        assert pipeline.header_row.numeric_cells == 0

    def test_header_col_is_column_a_below_row_one(self, pipeline):
        """`Deal ID` holds four distinct text values below row 1, so column A is a header
        by 8.3's rule — which says nothing about whether the sheet is a crossing table."""
        assert pipeline.header_col.verdict is True
        assert pipeline.header_col.cells == 4

    def test_a_numeric_first_row_is_not_a_header(self, workbook_bytes):
        notes = measure_sheet(workbook_bytes, "Notes", render_cell_budget=DOC_WINDOW)

        assert notes.header_row.verdict is False
        assert notes.header_row.numeric_cells == 1
        assert notes.header_row.all_text is False

    def test_interior_cardinality_counts_below_and_right_of_the_headers(self, pipeline):
        """Both headers hold, so the interior is B2:F5."""
        assert (pipeline.interior_rows, pipeline.interior_cols) == (4, 5)
        assert pipeline.interior_non_empty == 17
        # `Northwind` and `Proposal` each appear twice, so 17 filled cells hold 15 values.
        assert pipeline.interior_cardinality == 15
        assert pipeline.interior_cardinality_exact is True

    def test_merged_cells_are_counted(self, coverage):
        """`ReadOnlyWorksheet` has no `merged_cells` attribute at all in openpyxl 3.1.5,
        so this comes from the part's own `<mergeCells>` element. Reporting zero would be
        indistinguishable from a sheet that merges nothing."""
        assert coverage.merged_cells == 1

    def test_a_sheet_with_no_merges_counts_none(self, pipeline):
        assert pipeline.merged_cells == 0

    def test_rendered_markdown_is_one_table(self, pipeline):
        lines = pipeline.rendered_markdown.splitlines()

        assert lines[0] == "| Deal ID | Account | Stage | Value | Close Date | Note |"
        assert lines[1] == "| --- | --- | --- | --- | --- | --- |"
        assert lines[2] == "| D-1 | Northwind | Proposal | 128000 | 2026-09-30 | call |"
        assert len(lines) == 2 + 4

    def test_a_sheet_with_no_header_row_is_rendered_under_column_letters(self, workbook_bytes):
        notes = measure_sheet(workbook_bytes, "Notes", render_cell_budget=DOC_WINDOW)

        assert notes.rendered_markdown.splitlines()[0] == "| A | B |"

    def test_an_empty_sheet_measures_to_zero_rather_than_dividing_by_it(self, workbook_bytes):
        blank = measure_sheet(workbook_bytes, "Blank", render_cell_budget=DOC_WINDOW)

        assert (blank.rows, blank.cols, blank.non_empty_cells) == (0, 0, 0)
        assert blank.fill_ratio == 0.0
        assert blank.columns == ()
        assert blank.rendered_markdown is None
        assert "no value" in blank.rendered_unbounded_reason


class TestColumns:
    def test_an_identifier_column_is_unique(self, pipeline):
        deal_id = _column(pipeline, "Deal ID")

        assert deal_id.is_unique is True
        assert deal_id.distinct_count == 4
        assert deal_id.dominant_type == TYPE_TEXT

    def test_a_repeated_column_is_not_unique(self, pipeline):
        assert _column(pipeline, "Account").is_unique is False

    def test_a_closed_set_column_keeps_its_values(self, pipeline):
        stage = _column(pipeline, "Stage")

        assert stage.distinct_count == 3
        assert stage.values == ["Closed", "Proposal", "Prospect"]

    def test_dominant_type_is_the_commonest_cell_kind(self, pipeline):
        assert _column(pipeline, "Value").dominant_type == TYPE_NUMBER
        assert _column(pipeline, "Close Date").dominant_type == TYPE_DATE

    def test_a_sparse_column_reports_its_fill_ratio(self, pipeline):
        note = _column(pipeline, "Note")

        assert note.non_empty == 1
        assert note.body_rows == 4
        assert note.fill_ratio == pytest.approx(0.25)
        # A gap disqualifies a column from identifying a row even though what is there is
        # all distinct.
        assert note.is_unique is False

    def test_a_column_holding_nothing_says_so(self, workbook_bytes):
        """8.3's `dominant_type` for a column with no value in any row. `empty` was
        MEASURED; `unknown` would be a different claim."""
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "Gappy"
        sheet.append(["A", "B", "C"])
        sheet.append(["x", None, "z"])
        buffer = io.BytesIO()
        workbook.save(buffer)

        gappy = measure_sheet(buffer.getvalue(), "Gappy", render_cell_budget=DOC_WINDOW)

        assert _column(gappy, "B").dominant_type == TYPE_EMPTY

    def test_every_column_carries_a_sketch(self, pipeline):
        stage = _column(pipeline, "Stage")

        assert stage.sketch["kind"] == "minhash"
        assert stage.sketch["count"] == 3
        assert stage.sketch["partial"] is False
        assert len(stage.sketch["hashvalues"]) == stage.sketch["num_perm"]

    def test_sketches_can_be_turned_off(self, workbook_bytes):
        """An install with no `datasketch` still measures the sheet. What it loses is
        stated: a column with no sketch is invisible to 8.6's containment search."""
        measured = measure_sheet(
            workbook_bytes, "Pipeline", render_cell_budget=DOC_WINDOW, with_sketches=False
        )

        assert all(column.sketch is None for column in measured.columns)


class TestBounds:
    """What happens at the edges of the resource bounds, which are NOT 8.8's thresholds."""

    def test_a_column_past_the_tracking_limit_reports_a_floor_not_a_number(self, workbook_bytes):
        measured = measure_sheet(
            workbook_bytes,
            "Pipeline",
            render_cell_budget=DOC_WINDOW,
            distinct_tracked_max=2,
        )
        deal_id = _column(measured, "Deal ID")

        assert deal_id.distinct_count is None
        assert deal_id.distinct_at_least == 2
        assert deal_id.distinct_exact is False
        # It cannot answer, and does not guess: `is_unique` is
        # `distinct == non_empty == body_rows`, and one of the three is unknown.
        assert deal_id.is_unique is None
        assert deal_id.values is None

    def test_a_partial_sketch_says_it_is_partial(self, workbook_bytes):
        """The sketch is still produced — that is the point of it — but a containment
        estimate computed from it is about a prefix, and 8.6 has to be able to tell."""
        measured = measure_sheet(
            workbook_bytes,
            "Pipeline",
            render_cell_budget=DOC_WINDOW,
            distinct_tracked_max=2,
        )

        assert _column(measured, "Deal ID").sketch["partial"] is True

    def test_values_stop_being_retained_above_the_storage_ceiling(self, workbook_bytes):
        """The ceiling is a storage bound, not the closed-set threshold 8.8 leaves unset:
        the COUNT is exact either way, and only the listing goes."""
        measured = measure_sheet(
            workbook_bytes,
            "Pipeline",
            render_cell_budget=DOC_WINDOW,
            values_retained_max=2,
        )
        stage = _column(measured, "Stage")

        assert stage.distinct_count == 3
        assert stage.distinct_exact is True
        assert stage.values is None

    def test_a_sheet_too_large_to_render_says_why_instead_of_guessing(self, workbook_bytes):
        """The budget is an inequality: a markdown table spends at least one token on each
        row and one on each non-empty cell, so past the window no rendering can change any
        answer. Below it the count is exact."""
        measured = measure_sheet(workbook_bytes, "Pipeline", render_cell_budget=4)

        assert measured.rendered_markdown is None
        assert "above the embedding document window" in measured.rendered_unbounded_reason
        # Everything else still measured.
        assert measured.rows == 5


class TestTheCrossingTableGap:
    """8.3's header rule cannot see 8.4's own ``matrix`` example, and that is recorded.

    A crossing table has an empty top-left corner. 8.3 says ``header_row`` is "row 1 is all
    text, all distinct, no numerics" — which A1 being empty defeats — and 8.4's ``matrix``
    shape requires ``header_row`` and ``header_col`` both true. So the shape could never
    fire for the shape it was written for.

    This pass implements the rule AS WRITTEN and measures the corner case beside it. It is
    step 7's to redefine, from these numbers, with no blob read.
    """

    def test_the_rule_as_written_says_no(self, coverage):
        assert coverage.header_row.verdict is False

    def test_and_the_reason_is_exactly_the_corner_cell(self, coverage):
        evidence = coverage.header_row

        assert evidence.all_non_empty is False
        assert evidence.leading_empty is True
        assert evidence.text_cells == 2
        assert evidence.cells == 3

    def test_the_row_labels_are_a_header_by_the_same_rule(self, coverage):
        assert coverage.header_col.verdict is True


class TestMergedCellCount:
    def test_a_sheet_name_that_is_not_in_the_workbook_raises(self, workbook_bytes):
        with pytest.raises(WorksheetPartMissing) as raised:
            count_merged_cells(workbook_bytes, "Nope")

        assert "Nope" in str(raised.value)

    def test_measuring_a_sheet_that_is_not_there_raises(self, workbook_bytes):
        """A profile scoped to a node naming a sheet the bytes do not hold is a
        contradiction, not an empty measurement."""
        with pytest.raises(WorksheetPartMissing):
            measure_sheet(workbook_bytes, "Nope", render_cell_budget=DOC_WINDOW)


# ---------------------------------------------------------------------------
# 8.7's block on the sheet node
# ---------------------------------------------------------------------------


def _block(measurement, rendered_tokens=120):
    return sheet_evidence_block(
        measurement,
        rendered_tokens=rendered_tokens,
        token_window=512,
        doc_window=8192,
        embedding_model="test-model",
        render_cell_budget=8192,
    )


class TestStructuredContent:
    def test_it_carries_the_eight_signals_8_3_names(self, pipeline):
        measurements = _block(pipeline)["measurements"]

        assert set(measurements) >= {
            "rows",
            "cols",
            "fill_ratio",
            "header_row",
            "header_col",
            "interior_cardinality",
            "merged_cells",
            "rendered_tokens",
        }

    def test_it_carries_the_denominators_8_3_leaves_out(self, pipeline):
        """An interior cardinality of 3 means one thing in a 2x2 interior and another in a
        40,000x50 one, and 8.4's `matrix` rule is not decidable from the cardinality."""
        measurements = _block(pipeline)["measurements"]

        assert measurements["interior_cells"] == 20
        assert measurements["interior_non_empty"] == 17
        assert measurements["cells"] == 30
        assert measurements["non_empty_cells"] == 27

    def test_no_shape_is_chosen_and_the_absence_is_stated(self, pipeline):
        """The single most important property of this pass. 8.7's `shape` and
        `shape_margin` keys are ABSENT — an absent key is a question, and a key holding a
        number nobody calibrated is an answer that looks settled."""
        block = _block(pipeline)

        assert "shape" not in block
        assert "shape_margin" not in block
        assert block["shape_decision"]["decided"] is False
        assert "8.8" in block["shape_decision"]["reason"]

    def test_the_branch_inputs_are_stored_so_a_sweep_never_reads_a_blob(self, pipeline):
        """SPRINT_0_3_0.md 6.2. Every value 8.4's four branches read, in one place."""
        inputs = _block(pipeline)["shape_decision"]["inputs"]

        assert set(inputs) == {
            "rendered_tokens",
            "rendered_fits_token_window",
            "rendered_fits_doc_window",
            "header_row",
            "header_col",
            "fill_ratio",
            "interior_cardinality",
            "interior_cells",
            "interior_fill_ratio",
        }
        assert inputs["rendered_fits_token_window"] is True
        assert inputs["rendered_fits_doc_window"] is True

    def test_which_embedding_window_a_fit_was_judged_against_is_recorded(self, pipeline):
        """8.4 says "the embedding window" and this appliance has two. Which one
        `small_table` means is itself part of what step 7 decides."""
        measurements = _block(pipeline, rendered_tokens=2000)["measurements"]

        assert measurements["rendered_fits_token_window"] is False
        assert measurements["rendered_fits_doc_window"] is True
        assert measurements["embedding"] == {
            "model": "test-model",
            "token_window": 512,
            "doc_window": 8192,
        }

    def test_an_unrendered_sheet_has_no_fit_rather_than_a_false_one(self, workbook_bytes):
        measured = measure_sheet(workbook_bytes, "Pipeline", render_cell_budget=4)
        measurements = _block(measured, rendered_tokens=None)["measurements"]

        assert measurements["rendered_tokens"] is None
        assert measurements["rendered_tokens_exact"] is False
        assert measurements["rendered_fits_token_window"] is None
        assert measurements["rendered_unbounded_reason"]

    def test_the_limits_the_profile_ran_under_are_recorded(self, pipeline):
        """A sweep that wants a threshold above one of these has to re-profile, and this
        is where it finds that out."""
        limits = _block(pipeline)["measurements"]["limits"]

        assert set(limits) == {
            "distinct_tracked_max",
            "values_retained_max",
            "render_cell_budget",
        }

    def test_a_column_block_carries_its_sketch(self, pipeline):
        columns = _block(pipeline)["columns"]
        stage = next(column for column in columns if column["name"] == "Stage")

        assert stage["sketch"]["kind"] == "minhash"
        assert stage["values"] == ["Closed", "Proposal", "Prospect"]


# ---------------------------------------------------------------------------
# 8.5's prose
# ---------------------------------------------------------------------------


def _fits_everything(_text: str) -> bool:
    return True


class TestProfileProse:
    def test_it_opens_with_the_dimensions(self, pipeline):
        content, _ = build_profile_content(pipeline, fits=_fits_everything)

        assert content.startswith('Sheet "Pipeline" has 5 rows and 6 columns.')

    def test_an_identifier_column_is_named_as_one(self, pipeline):
        content, _ = build_profile_content(pipeline, fits=_fits_everything)

        assert 'Column "Deal ID" holds text and has a distinct value in every row' in content

    def test_a_closed_set_column_lists_its_values(self, pipeline):
        content, _ = build_profile_content(pipeline, fits=_fits_everything)

        assert (
            'Column "Stage" holds text and has 3 distinct values: Closed, Proposal, Prospect.'
            in content
        )

    def test_a_sparse_column_states_its_ratio_instead_of_classifying_it(self, pipeline):
        """8.5's sparse-column factoid is "fill_ratio well below the sheet's". "Well
        below" is a threshold and 8.8 has not set it, so the ratio is stated. A reader
        gets the same fact and nothing was invented."""
        content, _ = build_profile_content(pipeline, fits=_fits_everything)

        assert 'Column "Note" holds text' in content
        assert "It is filled in 25% of rows." in content
        assert "sparse" not in content

    def test_it_names_no_meaning_and_no_purpose(self, pipeline):
        """8.5: we do not name what a column MEANS, do not assert relationships between
        sheets, and do not classify the sheet's purpose. Every sentence is a count, a
        ratio, or values that were read."""
        content, _ = build_profile_content(pipeline, fits=_fits_everything)

        for forbidden in ("records", "matrix", "table of", "appears to", "looks like"):
            assert forbidden not in content.lower()

    def test_merged_ranges_are_counted_in_the_prose(self, coverage):
        content, _ = build_profile_content(coverage, fits=_fits_everything)

        assert "It declares 1 merged cell range." in content

    def test_an_empty_sheet_gets_one_sentence(self, workbook_bytes):
        blank = measure_sheet(workbook_bytes, "Blank", render_cell_budget=DOC_WINDOW)
        content, record = build_profile_content(blank, fits=_fits_everything)

        assert content == 'Sheet "Blank" holds no value in any cell.'
        assert record["columns"] == 0

    def test_a_bounded_distinct_count_is_stated_as_a_floor(self, workbook_bytes):
        measured = measure_sheet(
            workbook_bytes, "Pipeline", render_cell_budget=DOC_WINDOW, distinct_tracked_max=2
        )
        content, _ = build_profile_content(measured, fits=_fits_everything)

        assert "has at least 2 distinct values" in content

    def test_value_listings_go_before_whole_columns_do(self, pipeline):
        """The budget is the embedding window, and what it costs is chosen in one order:
        the listed values of the columns that have most of them, then whole columns.

        This sheet's full prose is 833 characters; at 800 every column is still described
        and only the listings have gone."""
        content, record = build_profile_content(pipeline, fits=lambda text: len(text) <= 800)

        assert record["truncated"] is True
        assert record["columns_described"] == 6
        assert record["columns_with_values_listed"] < 6
        # The count survives even where the listing does not — 8.5's own treatment of a
        # high-cardinality column.
        assert "3 distinct values" in content
        assert "Closed, Proposal, Prospect" not in content

    def test_a_tighter_budget_drops_whole_columns_and_says_how_many(self, pipeline):
        content, record = build_profile_content(pipeline, fits=lambda text: len(text) <= 600)

        assert record["columns_described"] < 6
        assert "further columns are measured in this node's structured content" in content

    def test_a_budget_nothing_can_satisfy_raises_rather_than_truncating_bytes(self, pipeline):
        with pytest.raises(ValueError) as raised:
            build_profile_content(pipeline, fits=lambda text: False)

        assert "does not fit the embedding window" in str(raised.value)


# ---------------------------------------------------------------------------
# The sketch seam
# ---------------------------------------------------------------------------


class TestSketch:
    """What ``propose:links`` will be handed. ``INGEST_SPEC.md`` 8.6, sprint 6.3.

    The packaging half of this — the guard, the message, the PERMANENT classification —
    is in ``tests/test_office_packaging.py`` beside the office extra's. What is here is
    the DATA contract: a sketch has to survive a round trip through JSONB and come back
    comparable, because 8.6's whole point is comparing a workbook profiled today against
    one profiled months ago.
    """

    def test_a_stored_sketch_says_what_produced_it(self):
        stored = column_sketch(["a", "b", "c"])

        assert stored["kind"] == SKETCH_MINHASH
        assert stored["num_perm"] == MINHASH_PERMUTATIONS
        assert stored["count"] == 3
        # The permutation SCHEME as well as the count and seed. datasketch 2.0.0 refuses
        # to rebuild a MinHash without it, and its reason is the one this seam already
        # gives for the other two: hash values carry no trace of the scheme that produced
        # them, so 1.x rebuilt them under the wrong family and compared them anyway.
        assert "scheme" in stored
        # Plain ints, not numpy scalars: psycopg2 has no adapter for those, and a JSONB
        # write of the raw array fails at the driver rather than where it was produced.
        assert all(isinstance(value, int) for value in stored["hashvalues"])

    def test_it_round_trips_through_json(self):
        """Through `json`, not through a dict: a stored sketch lives in JSONB, and this is
        the only test that would notice a numpy scalar or a tuple that does not serialise.
        """
        import json

        stored = column_sketch(["D-1", "D-2", "D-3"])
        rebuilt = load_sketch(json.loads(json.dumps(stored)))

        assert list(rebuilt.hashvalues) == stored["hashvalues"]

    def test_two_equal_sets_estimate_a_jaccard_of_one(self):
        left = load_sketch(column_sketch(["north", "south", "east", "west"]))
        right = load_sketch(column_sketch(["west", "east", "south", "north"]))

        assert left.jaccard(right) == pytest.approx(1.0)

    def test_a_sketch_this_appliance_cannot_read_comes_back_as_none(self):
        """A `kind` from a later version. The alternative is building a MinHash out of an
        array that means something else and comparing it with a straight face."""
        assert load_sketch({"kind": "something-later", "hashvalues": [1, 2, 3]}) is None

    def test_repetition_does_not_change_a_sketch(self):
        """The column sketch is built from the DISTINCT set, and a MinHash is insensitive
        to repetition — which is what makes it comparable with one built from a set that
        happened to be fed in a different order or with different multiplicity."""
        once = column_sketch(["a", "b"])
        twice = column_sketch(["a", "b", "a", "b"])

        assert once["hashvalues"] == twice["hashvalues"]


# ---------------------------------------------------------------------------
# The schedule
# ---------------------------------------------------------------------------


def _row(task: str):
    """The one `TASK_ROWS` entry for `task`. `_check_task_rows` guarantees there is one."""
    return next(row for row in jmfts_core.ingest_tasks.TASK_ROWS if row.task == task)


class TestSchedule:
    def test_the_two_tasks_are_rows_scoped_to_the_sheets_the_rung_produced(self):
        """`SPRINT_JOBS.md` Phase 3. This class used to assert the OPPOSITE — that neither
        task could be a row, because `TASK_ROWS` was evaluated once per uploaded file and
        both are scoped to a sheet node. The second half is still true and the first half
        was a fact about the table rather than about the tasks: Part 0 names it as the
        shoehorning, and 4.1 gave a row a scope. 8.2's "depends on the declared rung" is
        that scope."""
        for task in (TASK_PROFILE_SHEET, TASK_EXTRACT_SHEET):
            scope = _row(task).scope
            assert scope.kind == jmfts_core.ingest_tasks.SCOPE_CHILDREN
            assert scope.produced_by == (TASK_STRUCTURE_SHEETS,)
            assert scope.usetypes == (USETYPE_SHEET,)

    def test_profile_declares_children_where_8_2_says_self(self):
        """A deliberate divergence, and the reason is that a write mode is a concurrency
        declaration the claim query acts on (5.3). 8.5 makes the profile a CHILD of the
        sheet node, so this task writes children; declaring `self` would tell the queue it
        is safe to run a children-writer beside it."""
        assert _row(TASK_PROFILE_SHEET).write_mode == WRITE_CHILDREN

    def test_extract_is_ordered_after_profile(self):
        """`after` and not a second scope: both rows are scoped to the same node, so this
        is ordinary within-node ordering and resolves to a real `dependencies` id."""
        assert _row(TASK_EXTRACT_SHEET).after == (TASK_PROFILE_SHEET,)

    def test_sketching_is_on_by_default_and_is_in_the_fingerprint(self):
        """In `params` rather than read from configuration so that it lands in the row's
        `param_fingerprint` (6.1): a re-ingest that turns sketching on is a different
        request from the one that ran without it. Phase 3 is what made that claim TRUE —
        the value came from a literal dict on a module constant, so no request could move
        the fingerprint it was supposedly part of."""
        assert _row(TASK_PROFILE_SHEET).params_key == "sheet_profile"
        assert resolve_options("xlsx")["sheet_profile"] == {"sketch_columns": True}

    def test_the_sheet_knobs_are_reachable_from_a_callers_options(self):
        """Part 0's first measured consequence, closed. `max_rows`, `with_cell_notes` and
        `sketch_columns` "are not reachable from any caller"; they are three options in two
        groups now, and what reaches a queue row is what the caller asked for."""
        resolved = resolve_options(
            "xlsx",
            {"sheet_records": {"max_rows": 500}, "sheet_profile": {"sketch_columns": False}},
        )
        assert resolved["sheet_records"]["max_rows"] == 500
        assert resolved["sheet_records"]["with_cell_notes"] is True
        assert resolved["sheet_profile"] == {"sketch_columns": False}

    def test_max_rows_refuses_a_bool(self):
        """Part 0's fourth measured consequence, closed. `int(True)` is 1, so `max_rows:
        true` from a JSON caller would have written one record node for a whole sheet. It
        was latent while nothing could feed the task anything; making the knob reachable is
        what makes `_positive_int` load-bearing."""
        with pytest.raises(ValueError, match="sheet_records.max_rows"):
            resolve_options("xlsx", {"sheet_records": {"max_rows": True}})

    def test_explain_no_longer_stops_at_the_sheet_list(self):
        """Part 0's third measured consequence, closed. A `.xlsx` forecast used to end at
        `structure:sheets`; both per-sheet rows are in it now, carrying the scope that says
        they are one batch per sheet rather than two more tasks on the file."""
        plan = jmfts_core.ingest_tasks.explain_plan(
            "xlsx", patterns={"has_sheets": True, "sheet_count": 3}
        )
        rows = {task.task: task for task in plan.tasks}
        assert rows[TASK_PROFILE_SHEET].outcome == jmfts_core.ingest_tasks.OUTCOME_ENQUEUED
        assert rows[TASK_EXTRACT_SHEET].outcome == jmfts_core.ingest_tasks.OUTCOME_ENQUEUED
        assert rows[TASK_PROFILE_SHEET].scope != rows[TASK_STRUCTURE_SHEETS].scope
        assert rows[TASK_EXTRACT_SHEET].params["max_rows"] == 10_000

    def test_a_format_with_no_sheet_list_reports_them_impossible(self):
        """Not "the condition was false this time". A PDF names no worksheet list, so no
        sheet node can exist and neither row can ever fire for one."""
        plan = jmfts_core.ingest_tasks.explain_plan("pdf", patterns={"has_text_layer": True})
        rows = {task.task: task for task in plan.tasks}
        for task in (TASK_PROFILE_SHEET, TASK_EXTRACT_SHEET):
            assert rows[task].outcome == jmfts_core.ingest_tasks.OUTCOME_IMPOSSIBLE


# ---------------------------------------------------------------------------
# The task
# ---------------------------------------------------------------------------


class TestProfileSheetTask:
    def test_every_sheet_gets_exactly_one_profile_node(self, db_session, workbook_bytes):
        """ONE profile node, not one child. `extract:sheet` is the other children-writer on
        a sheet node and it writes a record per row, so the count under test is of the
        summaries."""
        node = _ingest(db_session, workbook_bytes)

        for sheet in _children(db_session, node.id):
            summaries = [
                child
                for child in _children(db_session, sheet.id)
                if child.usetype == USETYPE_PROFILE
            ]
            assert len(summaries) == 1
            assert summaries[0].content

    def test_the_sheet_node_records_the_profile_it_wrote(
        self, db_session, evidence, workbook_bytes
    ):
        node = _ingest(db_session, workbook_bytes)
        sheet = _children(db_session, node.id)[0]

        assert evidence(sheet)["sheet"]["profile_node_id"] == _children(db_session, sheet.id)[0].id

    def test_the_measurements_land_where_8_7_puts_them(self, db_session, evidence, workbook_bytes):
        node = _ingest(db_session, workbook_bytes)
        sheet = _children(db_session, node.id)[0]
        measurements = evidence(sheet)["sheet"]["measurements"]

        assert evidence(sheet)["sheet"]["name"] == "Pipeline"
        assert (measurements["rows"], measurements["cols"]) == (5, 6)
        assert measurements["header_row"] is True
        assert measurements["rendered_tokens"] > 0

    def test_the_profile_pass_itself_chooses_no_shape(self, db_session, evidence, workbook_bytes):
        """8.8 leaves 8.4's four thresholds unset and this pass invents none of them.

        This used to assert that no `shape` key reached the database at all. It does now:
        `extract:sheet` runs after this task and writes one, on 8.3's `header_row` — a
        measured boolean rather than one of the four thresholds. So the assertion moves to
        what THIS pass produced, which the attempt log keeps separate, plus the branch
        inputs 6.2 asks for, which outlive the verdict written over them.
        """
        node = _ingest(db_session, workbook_bytes)

        for sheet in _children(db_session, node.id):
            attempt = next(
                entry
                for entry in evidence(sheet)["attempts"]
                if entry["task"] == TASK_PROFILE_SHEET
            )
            # 8.7 makes the rung a function of the shape, so a pass that chose no shape
            # claims no rung either.
            assert attempt["rung"] is None
            assert "8.8" in attempt["detail"]["no_rung"]

            decision = evidence(sheet)["sheet"]["shape_decision"]
            assert "8.8" in decision["reason"]
            # Present whatever the verdict came out as. What 6.2 asks for is that the
            # values the branch READS survive, so that moving a threshold is a query over
            # stored profiles rather than a re-ingest.
            assert (
                decision["inputs"]["header_row"]
                == evidence(sheet)["sheet"]["measurements"]["header_row"]
            )
            assert "fill_ratio" in decision["inputs"]

    def test_a_sheet_node_settles_once_its_profile_has(self, db_session, workbook_bytes):
        """Step 5 created a sheet node SETTLED because nothing was queued beneath it. Work
        is queued beneath it now, so it is created in flight and the settle walk releases
        it — which is the same contract every container has."""
        node = _ingest(db_session, workbook_bytes)

        for sheet in _children(db_session, node.id):
            assert sheet.settled == SETTLED_SETTLED
            assert all(
                child.settled == SETTLED_SETTLED for child in _children(db_session, sheet.id)
            )
        assert node.settled == SETTLED_SETTLED

    def test_the_attempt_records_what_it_measured_and_what_it_did_not(
        self, db_session, evidence, workbook_bytes
    ):
        node = _ingest(db_session, workbook_bytes)
        sheet = _children(db_session, node.id)[0]
        attempt = next(
            entry for entry in evidence(sheet)["attempts"] if entry["task"] == TASK_PROFILE_SHEET
        )

        assert attempt["detail"]["sheet"] == "Pipeline"
        assert attempt["detail"]["columns"] == 6
        assert attempt["detail"]["sketched_columns"] == 6
        # 3.4, and it is COMPUTED rather than stated: this used to assert that
        # `extract:sheet` was deferred, from a hard-coded reason string that would have
        # gone on claiming so after the handler landed. It has one now, so nothing below a
        # sheet is deferred.
        assert attempt["detail"]["deferred"] == {}
        # 8.7 makes the sheet block's rung a function of the shape, and no shape was
        # chosen, so no rung is claimed.
        assert attempt["rung"] is None

    def test_both_per_sheet_tasks_are_queued_now_that_both_have_handlers(
        self, db_session, evidence, workbook_bytes
    ):
        """`_split_by_handler` decides this from what is registered, not from a list.

        That is the property under test rather than the count: the day `extract:sheet` got
        a handler, nothing in `structure:sheets` had to change for it to start being
        enqueued, and nothing had to change for its deferral reason to stop being
        reported."""
        node = _ingest(db_session, workbook_bytes)
        attempt = next(
            entry for entry in evidence(node)["attempts"] if entry["task"] == TASK_STRUCTURE_SHEETS
        )

        assert attempt["detail"]["queued_per_sheet"] == [TASK_PROFILE_SHEET, TASK_EXTRACT_SHEET]
        assert attempt["detail"]["deferred"] == {}

    def test_it_refuses_a_node_that_is_not_a_sheet(self, db_session, workbook_bytes):
        node = _ingest(db_session, workbook_bytes)

        with pytest.raises(ValueError) as raised:
            run_profile_sheet(db_session, _ScopedTask(node.id))

        assert "usetype" in str(raised.value)

    def test_it_refuses_a_sheet_node_whose_parent_holds_no_bytes(self, db_session):
        repo = DocumentRepository(db_session)
        parent = repo.create(title="no bytes", content=None)
        orphan = repo.create(
            title="Pipeline",
            content=None,
            parent_id=parent.id,
            usetype=USETYPE_SHEET,
            evidence={"sheet": {"name": "Pipeline", "index": 0, "state": "visible"}},
            auto_embed=False,
        )
        db_session.flush()

        with pytest.raises(ValueError) as raised:
            run_profile_sheet(db_session, _ScopedTask(orphan.id))

        assert "has no stored blob" in str(raised.value)
