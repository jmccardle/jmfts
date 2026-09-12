"""What the appliance answers for the three files 0.3.0 got wrong.

``tests/corpus/test_fixtures.py`` asserts that each fixture CONTAINS its defect; this
module asserts what shipped code DOES with it. The split is the same one the rest of this
package draws — the manifest describes bytes, the suite holds the description against them
— and it matters here because all three fixtures exist to reproduce a failure that was
found outside this repository: ``docs/SPRINT_0_4_0.md`` Block B, 16,856 real workbooks
run through the shipped ``probe`` and ``measure_sheet`` on 2026-09-03.

**Two of the three need ``openpyxl``, and this module is the first thing in
``tests/corpus`` that needs anything.** They are skipped rather than dropped when the
``office`` extra is absent: the guards they cover are in a tier-2 module, so a base
install cannot reach them at all, and a test that quietly passed there would be reporting
coverage of code it never imported. The ``zlib`` half needs nothing — ``probe`` is tier 1
and stays in the base install (``docs/OFFICE_SPEC.md`` Part 1).

The controls are not optional. A ``measure_sheet`` that raised on every workbook would
pass both refusal tests, so each one is paired with a sheet that must still measure.
"""

from __future__ import annotations

import io
import zipfile
import zlib

import pytest

from jmfts_core.probe import detect_format, probe_patterns
from jmfts_core.task_errors import ErrorType, classify_exception
from tests.corpus import fixtures
from tests.corpus.manifest import load

CORPUS = load()

#: A workbook openpyxl is expected to measure, built here rather than added to the corpus.
#: It carries no property worth a manifest record — it is an ordinary three-row sheet — and
#: its whole job is to be the control that keeps the two refusals below from being
#: satisfied by a function that refuses everything.
CONTROL_SHEET_ROWS = (
    '<row r="1"><c r="A1" t="inlineStr"><is><t>id</t></is></c>'
    '<c r="B1" t="inlineStr"><is><t>label</t></is></c></row>'
    '<row r="2"><c r="A2" t="n"><v>1</v></c>'
    '<c r="B2" t="inlineStr"><is><t>one</t></is></c></row>'
    '<row r="3"><c r="A3" t="n"><v>2</v></c>'
    '<c r="B3" t="inlineStr"><is><t>two</t></is></c></row>'
)


def control_workbook() -> bytes:
    """The same package shape as ``declared-extent.xlsx`` with an honest ``<dimension>``."""
    worksheet = (
        fixtures.DECLARATION
        + f'<worksheet xmlns="{fixtures.S_NS}"><dimension ref="A1:B3"/>'
        + f"<sheetData>{CONTROL_SHEET_ROWS}</sheetData></worksheet>"
    ).encode("utf-8")
    return fixtures.pack(
        [
            fixtures.Member(
                "[Content_Types].xml",
                fixtures._content_types(fixtures.XLSX_WORKBOOK_TYPE, fixtures.XLSX_WORKSHEET_TYPE),
            ),
            fixtures.Member(
                "_rels/.rels",
                fixtures._package_rels("xl/workbook.xml", fixtures.XLSX_REL_TYPE),
            ),
            fixtures.Member("xl/workbook.xml", fixtures._workbook_xml("Sheet1")),
            fixtures.Member(
                "xl/_rels/workbook.xml.rels",
                fixtures._workbook_rels("worksheets/sheet1.xml", "worksheet"),
            ),
            fixtures.Member("xl/worksheets/sheet1.xml", worksheet),
        ]
    )


def sheets_module():
    """:mod:`jmfts_core.office.sheets`, or a skip naming the extra that is missing."""
    pytest.importorskip("openpyxl", reason="the office extra; measure_sheet is tier 2")
    from jmfts_core.office import sheets

    return sheets


# ---------------------------------------------------------------------------
# Step 6 — a corrupt deflate stream is PERMANENT, not RETRYABLE
# ---------------------------------------------------------------------------


def test_a_corrupt_member_reaches_probe_as_a_zlib_error():
    """The precondition. Without it the next test could pass for the wrong reason.

    ``detect_format`` answers ``docx`` from the ZIP manifest without inflating anything, so
    the file is dispatched to ``_probe_docx`` exactly like a healthy package and only then
    falls apart.
    """
    data = CORPUS.bytes_for(CORPUS.by_name("corrupt-deflate.docx"))
    assert detect_format(data, filename="corrupt-deflate.docx").format == "docx"
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        with pytest.raises(zlib.error):
            archive.read("word/document.xml")


def test_probe_refuses_a_corrupt_deflate_stream_permanently():
    """Step 6. A stream that will never inflate must not be attempted three times."""
    data = CORPUS.bytes_for(CORPUS.by_name("corrupt-deflate.docx"))
    detection = detect_format(data, filename="corrupt-deflate.docx")

    with pytest.raises(ValueError) as raised:
        probe_patterns(data, detection)

    assert not isinstance(raised.value, zlib.error), (
        "a bare zlib.error escaping probe_patterns is the defect: classify_exception "
        "grades it RETRYABLE and a permanently unreadable file is retried on a backoff"
    )
    assert isinstance(raised.value.__cause__, zlib.error), "the cause must survive the re-raise"
    assert "zlib.error" in str(raised.value), "the message names which layer failed"
    assert classify_exception(raised.value) is ErrorType.PERMANENT


def test_the_healthy_package_still_probes():
    """The control for the two above."""
    data = CORPUS.bytes_for(CORPUS.by_name("deflated.docx"))
    detection = detect_format(data, filename="deflated.docx")
    patterns, _detail = probe_patterns(data, detection)
    assert patterns["has_text_layer"] is True


# ---------------------------------------------------------------------------
# Step 5 — a chartsheet has no grid
# ---------------------------------------------------------------------------


def test_a_chartsheet_is_named_like_a_worksheet_and_has_no_grid():
    """Why the type check is the fix and a name check would not be.

    Nothing about the sheet's NAME says it is a chart, and the sheet-name membership test
    ``measure_sheet`` already performs passes. What arrives is a class with no ``max_row``.
    """
    openpyxl = pytest.importorskip("openpyxl", reason="the office extra")
    data = CORPUS.bytes_for(CORPUS.by_name("chartsheet.xlsx"))
    workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        assert workbook.sheetnames == ["Chart1"]
        assert not hasattr(workbook["Chart1"], "max_row")
    finally:
        workbook.close()


def test_measure_sheet_names_the_chartsheet_instead_of_a_missing_attribute():
    """Step 5. 135 in 10,702 open-web workbooks."""
    sheets = sheets_module()
    data = CORPUS.bytes_for(CORPUS.by_name("chartsheet.xlsx"))
    with pytest.raises(sheets.SheetIsNotAWorksheet) as raised:
        sheets.measure_sheet(data, "Chart1", render_cell_budget=8192, with_sketches=False)
    assert "Chartsheet" in str(raised.value)
    assert classify_exception(raised.value) is ErrorType.PERMANENT


# ---------------------------------------------------------------------------
# Step 4 — a declared extent nothing will ever finish scanning
# ---------------------------------------------------------------------------


def test_measure_sheet_refuses_an_extent_it_cannot_afford_to_scan():
    """Step 4.

    **A regression here HANGS rather than failing**, and that is the defect stated exactly:
    there is no clock anywhere between this call and the last of 2.75e11 loop iterations —
    not the lease, which bounds heartbeat silence, and not the worker, which cannot
    interrupt a handler. The guard is read before the scan starts, so the assertion below
    is reached in milliseconds while the guard exists.
    """
    sheets = sheets_module()
    data = CORPUS.bytes_for(CORPUS.by_name("declared-extent.xlsx"))
    with pytest.raises(sheets.DeclaredExtentTooLarge) as raised:
        sheets.measure_sheet(data, "Sheet1", render_cell_budget=8192, with_sketches=False)

    message = str(raised.value)
    assert "274,894,700,545" in message, "the message must say how large the declaration is"
    assert f"{sheets.DECLARED_CELLS_MAX:,}" in message, "and what the bound is"
    assert classify_exception(raised.value) is ErrorType.PERMANENT


def test_an_ordinary_sheet_of_the_same_shape_still_measures():
    """The control. The refusal above must be about the extent and nothing else."""
    sheets = sheets_module()
    measurement = sheets.measure_sheet(
        control_workbook(), "Sheet1", render_cell_budget=8192, with_sketches=False
    )
    assert (measurement.rows, measurement.cols) == (3, 2)
    assert measurement.declared_rows == 3 and measurement.declared_cols == 2
    assert measurement.non_empty_cells == 6


def test_the_bound_is_above_every_declared_extent_the_corpora_hold():
    """The constant is a measurement, not a taste. ``sheets.DECLARED_CELLS_MAX``'s comment
    records the distribution it was read from: of 35,547 worksheet parts in 11,447 real
    workbooks, four declare more than this and the largest accepted is 6.3e7 cells. This
    holds the two ends of that sentence against the constant, so a change to it has to
    restate the measurement rather than quietly widen the refusal.
    """
    sheets = sheets_module()
    assert sheets.DECLARED_CELLS_MAX > 62_914_440, "the largest sheet measured that passes"
    assert sheets.DECLARED_CELLS_MAX < 332_398_592, "the smallest sheet measured that fails"
