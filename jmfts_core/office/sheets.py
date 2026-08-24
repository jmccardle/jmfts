"""What a worksheet measures to. ``docs/INGEST_SPEC.md`` 8.3.

One pass over one sheet's used range, producing 8.3's table and nothing else. **No shape
is chosen here and none is chosen by the task that calls this** — 8.8 leaves every
threshold in 8.4 unset pending calibration against real workbooks, so this module's whole
job is to produce the numbers that calibration will be run against.

TIER 2, and the reason this file is in :mod:`jmfts_core.office`: ``openpyxl`` is the
``office`` extra, the import happens inside :func:`~jmfts_core.office.require_openpyxl` at
the point of use, and ``tests/test_office_packaging.py`` fails if it climbs to module
scope.

**Three things openpyxl 3.1.5 does that 8.3 was written without knowing.** All three were
measured rather than assumed, and each one changed a definition here:

1. ``ReadOnlyWorksheet`` HAS NO ``merged_cells`` ATTRIBUTE AT ALL. ``OFFICE_SPEC.md`` Part
   4 note 2 requires ``read_only=True`` *because* ``profile:sheet`` measures merged cells,
   and in this version those two requirements contradict each other. The count is
   therefore read from the worksheet part's own ``<mergeCells>`` element with the standard
   library (:func:`count_merged_cells`), which costs a second scan of that part and is
   still cheaper than materialising the grid. Reporting zero would have been the
   indistinguishable-from-none failure the project's Fail Early rule exists to stop.

2. ``max_row``/``max_column`` COME FROM THE ``<dimension>`` ELEMENT, WHICH IS OPTIONAL.
   A workbook written without one reports ``None`` for both, ``reset_dimensions()`` does
   not help in read-only mode, and the rows come back ragged. So 8.3's "used range
   dimensions" is measured from the cells that actually hold a value, and what the file
   DECLARED is recorded separately — a writer whose dimension disagrees with its own cells
   is a fact about the file, not an error.

3. ``iter_rows`` IS ANCHORED AT A1 whatever the dimension says (``min_col = min_col or 1``
   in ``worksheet.py``), so an enumeration index is a real row number and a position in
   the yielded tuple is a real column number even for a sheet whose data starts at C5.
   That is relied on below and is the reason no cell object is constructed.

**Memory is bounded and the bound is disclosed.** An exact distinct-value set is the only
way to an exact ``distinct_count`` and an exact ``is_unique``, and a column of a million
rows cannot have one held in memory. :data:`DISTINCT_TRACKED_MAX` is where tracking stops;
past it the column reports what it knows — a floor, and a sketch — rather than a number it
did not measure. See the constant for why the value is a resource bound and not one of
8.8's thresholds.
"""

from __future__ import annotations

import datetime
import io
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any, Optional
from xml.etree import ElementTree as ET

from jmfts_core.office import require_openpyxl
from jmfts_core.sketch import SketchBuilder

#: How many distinct values one tracked set holds before it stops growing. A RESOURCE
#: BOUND, not one of ``INGEST_SPEC.md`` 8.8's thresholds: nothing branches on it, no shape
#: is decided by it, and a threshold sweep may pick any closed-set threshold at all up to
#: this without re-reading a blob. What it costs to raise is memory — the tracked values
#: are the canonical strings themselves, so 50,000 of them is single-digit MB per column
#: and a sheet holds one set per column plus four for the interior.
#:
#: What it costs to have at all: a column with more distinct values than this reports
#: ``distinct_count: null`` with a floor beside it, and ``is_unique: null``. That is the
#: honest answer — "at least 50,000 distinct" is what was measured — and it is a real
#: limitation for the identifier column of a very large table, recorded rather than
#: papered over.
DISTINCT_TRACKED_MAX = 50_000

#: How many of a column's distinct values are handed back for storage. 8.5 stores
#: ``values`` for a closed-set column and a count for a high-cardinality one, and WHICH IS
#: WHICH IS 8.8'S UNSET THRESHOLD — so this is not that decision. It is the ceiling on how
#: large a set can be and still be replayable from the stored profile, which is what makes
#: any later choice of that threshold a query rather than a re-ingest.
VALUES_RETAINED_MAX = 1_000

#: Cell value kinds, as ``dominant_type`` reports them. Open strings; named here so the
#: measurer and the prose renderer cannot come to spell them differently.
TYPE_TEXT = "text"
TYPE_NUMBER = "number"
TYPE_DATE = "date"
TYPE_BOOL = "bool"
#: What a column with no value in any row reports. Not "unknown" — it was measured, and
#: what was measured is that there is nothing there.
TYPE_EMPTY = "empty"

#: SpreadsheetML's main namespace, and the two relationships namespaces, for the tier-1
#: read of ``<mergeCells>``. Spelled here rather than imported from :mod:`jmfts_core.probe`:
#: probe must stay importable with nothing but the standard library and gains nothing from
#: exporting its constants to a tier-2 module.
_S_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PR_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

#: Escapes a cell value for a markdown table cell: a pipe would close the cell and a
#: newline would end the row.
_MARKDOWN_UNSAFE = re.compile(r"[|\r\n]")


class WorksheetPartMissing(ValueError):
    """The workbook does not resolve this sheet's name to a part in the package.

    Raised rather than returning a merged-cell count of zero. Zero merges and "the sheet's
    XML could not be found" are different facts, and a profile that recorded the second as
    the first would put a measurement into the record that nothing measured.
    """


# ---------------------------------------------------------------------------
# Canonical values
# ---------------------------------------------------------------------------


def canonical(value: Any) -> Optional[str]:
    """One cell as the string every count, comparison and sketch sees, or ``None``.

    Canonicalisation is the whole correctness of a cross-sheet comparison (8.6): two
    columns holding the same identifiers agree only if ``128000`` and ``128000.0`` reached
    the counter as one string. So it happens once, here, and every consumer downstream
    reads the result rather than the cell.

    ``None`` means the cell is empty, and a string of nothing but whitespace is empty too.
    That is a choice and it is recorded: a cell holding three spaces is not a value a
    person put there to mean something, and counting it as one would make ``fill_ratio``
    depend on a workbook's whitespace.

    A ``datetime`` at exactly midnight renders as a plain date. openpyxl returns
    ``datetime.datetime`` for a cell formatted as a date, with no way to tell a date from a
    date-time whose time is zero, and ``2026-09-30`` is what such a column holds.
    """
    if value is None:
        return None
    # Before `int`: `bool` is a subclass of it, and `str(True)` is "True" while a
    # spreadsheet's own rendering is TRUE.
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # `1.0` and `1` are the same value to a spreadsheet and must sketch to one string.
        # `is_integer()` is exact and `repr` round-trips, so neither branch loses a digit.
        return str(int(value)) if value.is_integer() else repr(value)
    if isinstance(value, datetime.datetime):
        if value.time() == datetime.time(0, 0):
            return value.date().isoformat()
        return value.isoformat()
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return str(value)
    text = str(value).strip()
    return text or None


def value_type(value: Any) -> str:
    """Which of :data:`TYPE_TEXT` and friends a non-empty cell is."""
    if isinstance(value, bool):
        return TYPE_BOOL
    if isinstance(value, (int, float)):
        return TYPE_NUMBER
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time, datetime.timedelta)):
        return TYPE_DATE
    return TYPE_TEXT


def column_letter(index: int) -> str:
    """1 -> ``A``, 27 -> ``AA``. The address a person reads off the top of the sheet."""
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


# ---------------------------------------------------------------------------
# The shapes the measurement comes back in
# ---------------------------------------------------------------------------


@dataclass
class _DistinctSet:
    """An exact distinct set that stops growing, and says that it stopped.

    The two fields a caller reads are :attr:`count` and :attr:`exact`. ``exact`` false
    means ``count`` is a floor and the true cardinality is unknown to this object — which
    is a measurement, not a failure, and is reported as one.
    """

    limit: int = DISTINCT_TRACKED_MAX
    values: set = field(default_factory=set)
    exact: bool = True

    def add(self, value: str) -> None:
        if not self.exact:
            return
        self.values.add(value)
        if len(self.values) >= self.limit:
            self.exact = False

    def plus(self, value: Optional[str]) -> "_DistinctSet":
        """A copy holding one more value, for the row-1 fold-in in :func:`_column`.

        A set cannot be un-added from, so a column's row-1 cell is held out of the scan and
        folded in here instead: a header label may also appear in its own column's body, and
        "subtract one" would then be wrong by exactly the case that matters.
        """
        if value is None:
            return self
        copied = _DistinctSet(limit=self.limit, values=set(self.values), exact=self.exact)
        copied.add(value)
        return copied

    @property
    def count(self) -> int:
        return len(self.values)


@dataclass(frozen=True)
class ColumnMeasurement:
    """8.3's per-column row, plus what a threshold sweep needs to replay it.

    ``distinct_count`` is ``None`` exactly when :data:`DISTINCT_TRACKED_MAX` was reached,
    and ``distinct_at_least`` is then the floor that was measured. ``is_unique`` is
    ``None`` in the same case: it is ``distinct_count == non_empty == body_rows``, and only
    two of those three are known.

    ``body_rows`` is the denominator ``fill_ratio`` was taken over — the rows below the
    header row, or every row where there is none. It is carried rather than left to be
    re-derived because a sweep that redefines ``header_row`` (see :class:`HeaderEvidence`)
    changes the denominator, and it must be able to see which one was used.
    """

    index: int
    letter: str
    name: Optional[str]
    dominant_type: str
    type_counts: dict
    non_empty: int
    body_rows: int
    fill_ratio: float
    distinct_count: Optional[int]
    distinct_at_least: int
    distinct_exact: bool
    values: Optional[list]
    is_unique: Optional[bool]
    sketch: Optional[dict]


@dataclass(frozen=True)
class HeaderEvidence:
    """Why :attr:`SheetMeasurement.header_row` (or ``header_col``) came out as it did.

    8.3 gives one boolean per header and 8.4 branches on it. The components are stored
    beside the verdict because **the verdict as 8.3 defines it cannot see the sheet 8.4's
    own example is drawn from**: a crossing table has an empty top-left corner, so its
    first row is not "all text" and ``header_row`` is false — and the ``matrix`` shape,
    which requires both headers, could never fire for the shape it was written for.

    That is a gap in the spec, not a licence to invent a looser rule here, so the rule is
    implemented exactly as written and ``leading_empty`` records the corner case as a
    measurement. Step 7 can redefine the verdict from these numbers with no blob read.
    """

    verdict: bool
    cells: int
    non_empty: int
    text_cells: int
    numeric_cells: int
    other_cells: int
    distinct: int
    all_non_empty: bool
    all_text: bool
    all_distinct: bool
    #: The first cell is empty and every other cell satisfies the rule. The crossing
    #: table's corner, measured.
    leading_empty: bool


@dataclass(frozen=True)
class SheetMeasurement:
    """Everything ``INGEST_SPEC.md`` 8.3 asks for about one sheet, and no decision.

    ``rendered_markdown`` is ``None`` when the sheet is provably too large to fit any
    embedding window; see :func:`measure_sheet` for the derivation, which is an inequality
    rather than a chosen size.
    """

    name: str
    rows: int
    cols: int
    non_empty_cells: int
    fill_ratio: float
    declared_rows: Optional[int]
    declared_cols: Optional[int]
    header_row: HeaderEvidence
    header_col: HeaderEvidence
    interior_rows: int
    interior_cols: int
    interior_non_empty: int
    interior_cardinality: Optional[int]
    interior_cardinality_at_least: int
    interior_cardinality_exact: bool
    merged_cells: int
    columns: tuple
    rendered_markdown: Optional[str]
    rendered_unbounded_reason: Optional[str]
    distinct_tracked_max: int
    values_retained_max: int


# ---------------------------------------------------------------------------
# The header rule
# ---------------------------------------------------------------------------


def _header_evidence(values: list, kinds: list) -> HeaderEvidence:
    """8.3's header rule over one row or column, with its components kept.

    ``values`` are canonical strings, ``None`` for empty, in address order and covering the
    whole used extent — so a trailing empty cell counts against the rule exactly as an
    interior one does, which is what "all text" means. ``kinds`` is the matching list of
    :func:`value_type` results, ``None`` where the cell is empty; the rule needs it because
    a canonical string cannot say what it came from and "no numerics" is half the rule.
    """
    present = [value for value in values if value is not None]
    text_cells = sum(1 for kind in kinds if kind == TYPE_TEXT)
    numeric_cells = sum(1 for kind in kinds if kind == TYPE_NUMBER)
    other_cells = sum(1 for kind in kinds if kind not in (None, TYPE_TEXT, TYPE_NUMBER))
    distinct = len(set(present))
    all_non_empty = bool(values) and len(present) == len(values)
    all_text = bool(values) and text_cells == len(values)
    all_distinct = bool(present) and distinct == len(present)
    tail_values = values[1:]
    tail_kinds = kinds[1:]
    tail_present = [value for value in tail_values if value is not None]
    leading_empty = (
        bool(tail_values)
        and values[0] is None
        and sum(1 for kind in tail_kinds if kind == TYPE_TEXT) == len(tail_values)
        and len(set(tail_present)) == len(tail_present)
    )
    return HeaderEvidence(
        verdict=all_non_empty and all_text and all_distinct,
        cells=len(values),
        non_empty=len(present),
        text_cells=text_cells,
        numeric_cells=numeric_cells,
        other_cells=other_cells,
        distinct=distinct,
        all_non_empty=all_non_empty,
        all_text=all_text,
        all_distinct=all_distinct,
        leading_empty=leading_empty,
    )


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------


@dataclass
class _Scan:
    """The mutable accumulator :func:`_scan` fills and :func:`_assemble` freezes.

    **Four interior sets, not one.** The interior begins below row 1 only if ``header_row``
    holds and right of column A only if ``header_col`` does; the first is a property of
    every used COLUMN and the second of every used ROW, so neither is known until the last
    row has been read — by which time the interior cannot be re-walked without a second
    pass over the part. Keeping all four candidates costs four bounded sets and removes
    that pass. They are keyed ``(row_offset, col_offset)`` with offsets of 0 or 1.
    """

    rows: int = 0
    cols: int = 0
    non_empty_cells: int = 0
    first_row: dict = field(default_factory=dict)
    first_row_kind: dict = field(default_factory=dict)
    first_col: dict = field(default_factory=dict)
    first_col_kind: dict = field(default_factory=dict)
    per_column_non_empty: dict = field(default_factory=dict)
    per_column_types: dict = field(default_factory=dict)
    per_column_distinct: dict = field(default_factory=dict)
    interior: dict = field(default_factory=dict)
    interior_non_empty: dict = field(default_factory=dict)
    rendered_rows: list = field(default_factory=list)
    render_bounded: bool = False


_INTERIOR_KEYS = ((0, 0), (0, 1), (1, 0), (1, 1))


def _scan(worksheet, *, render_cell_budget: int, distinct_tracked_max: int) -> _Scan:
    """The single pass. Everything 8.3 measures is accumulated here or not at all."""
    scan = _Scan()
    for key in _INTERIOR_KEYS:
        scan.interior[key] = _DistinctSet(limit=distinct_tracked_max)
        scan.interior_non_empty[key] = 0

    for row_index, row in enumerate(worksheet.iter_rows(values_only=True), start=1):
        rendered: list = []
        row_has_value = False
        for col_index, raw in enumerate(row, start=1):
            text = canonical(raw)
            rendered.append("" if text is None else text)
            if text is None:
                continue
            kind = value_type(raw)
            row_has_value = True
            scan.non_empty_cells += 1
            if col_index > scan.cols:
                scan.cols = col_index

            if row_index == 1:
                scan.first_row[col_index] = text
                scan.first_row_kind[col_index] = kind
            if col_index == 1:
                scan.first_col[row_index] = text
                scan.first_col_kind[row_index] = kind

            for row_offset, col_offset in _INTERIOR_KEYS:
                if row_index <= row_offset or col_index <= col_offset:
                    continue
                scan.interior[(row_offset, col_offset)].add(text)
                scan.interior_non_empty[(row_offset, col_offset)] += 1

            # ROW 1 IS NOT PART OF A COLUMN'S STATISTICS while it might be the header, and
            # whether it is is not known until every used column has been read. So the
            # accumulators below cover rows 2..N and `first_row` above keeps row 1's cell;
            # a sheet whose first row turns out NOT to be a header folds it back in at
            # assembly (:func:`_column`). Counting it here would give a header cell a vote
            # in its own column's `dominant_type`, put the label in the column's distinct
            # set, and make `fill_ratio` exceed 1.
            if row_index == 1:
                continue

            scan.per_column_non_empty[col_index] = scan.per_column_non_empty.get(col_index, 0) + 1
            types = scan.per_column_types.setdefault(col_index, {})
            types[kind] = types.get(kind, 0) + 1
            distinct = scan.per_column_distinct.get(col_index)
            if distinct is None:
                distinct = _DistinctSet(limit=distinct_tracked_max)
                scan.per_column_distinct[col_index] = distinct
            distinct.add(text)

        if row_has_value:
            scan.rows = row_index
        _retain(scan, rendered, render_cell_budget)

    return scan


def _retain(scan: _Scan, rendered: list, budget: int) -> None:
    """Keep one rendered row while a table of them could still fit an embedding window.

    See :func:`measure_sheet` for the inequality. Once it fails, the rows already kept are
    dropped: they are a prefix of a table nobody can use, and holding them would trade the
    memory this bound exists to save for nothing.
    """
    if scan.render_bounded:
        return
    floor = max(len(scan.rendered_rows) + 1 + 2, scan.non_empty_cells)
    if floor > budget:
        scan.render_bounded = True
        scan.rendered_rows.clear()
        return
    scan.rendered_rows.append(tuple(rendered))


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------


def measure_sheet(
    data: bytes,
    sheet_name: str,
    *,
    render_cell_budget: int,
    with_sketches: bool = True,
    distinct_tracked_max: int = DISTINCT_TRACKED_MAX,
    values_retained_max: int = VALUES_RETAINED_MAX,
) -> SheetMeasurement:
    """One pass over one sheet's used range. ``INGEST_SPEC.md`` 8.3.

    **The render budget is an inequality, not a size somebody liked.** A markdown table of
    R data rows spends at least one token on each row's newline and at least one on each
    non-empty cell, so its token count is at least ``max(R + 2, non_empty)``. Once that
    floor passes ``render_cell_budget`` — the embedding document window, which is the
    model's own limit and therefore the largest value 8.4's ``small_table`` test could ever
    be calibrated to — rendering the rest cannot change the answer to any question anybody
    can ask of it, so the rows stop being kept and the reason is recorded. A sheet under
    the floor is rendered exactly and counted exactly.

    ``with_sketches`` false is for a caller that wants the measurements on an install with
    no ``datasketch``. It is not the default: a column with no sketch is invisible to 8.6's
    containment search whatever its cardinality, and going without has to be asked for.
    """
    openpyxl = require_openpyxl()
    # `data_only=True`: a profile measures VALUES. `=SUM(B2:B9)` is not a value, and a
    # column of them would count as text with 1,284 distinct strings. The cost is that a
    # workbook whose formulas were never evaluated by Excel — one openpyxl itself wrote —
    # reports those cells empty, which is what it can honestly say about a file that
    # carries no cached result.
    workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        if sheet_name not in workbook.sheetnames:
            raise WorksheetPartMissing(
                f"the workbook names no sheet {sheet_name!r}; it names "
                f"{workbook.sheetnames!r}, so the node this profile is scoped to and the "
                "stored bytes do not describe the same workbook"
            )
        worksheet = workbook[sheet_name]
        declared_rows = worksheet.max_row
        declared_cols = worksheet.max_column
        scanned = _scan(
            worksheet,
            render_cell_budget=render_cell_budget,
            distinct_tracked_max=distinct_tracked_max,
        )
    finally:
        # A read-only workbook holds the ZIP open and the caller is inside a database
        # transaction.
        workbook.close()

    return _assemble(
        scanned,
        name=sheet_name,
        declared_rows=declared_rows,
        declared_cols=declared_cols,
        merged_cells=count_merged_cells(data, sheet_name),
        with_sketches=with_sketches,
        distinct_tracked_max=distinct_tracked_max,
        values_retained_max=values_retained_max,
    )


def _assemble(
    scan: _Scan,
    *,
    name: str,
    declared_rows: Optional[int],
    declared_cols: Optional[int],
    merged_cells: int,
    with_sketches: bool,
    distinct_tracked_max: int,
    values_retained_max: int,
) -> SheetMeasurement:
    rows, cols = scan.rows, scan.cols
    cells = rows * cols
    fill_ratio = (scan.non_empty_cells / cells) if cells else 0.0

    header_row = _header_evidence(
        [scan.first_row.get(index) for index in range(1, cols + 1)],
        [scan.first_row_kind.get(index) for index in range(1, cols + 1)],
    )
    # 8.3: "column A is all text and all distinct BELOW ROW 1" — below row 1 outright, not
    # below whatever turned out to be the header row. Implemented as written.
    header_col = _header_evidence(
        [scan.first_col.get(index) for index in range(2, rows + 1)],
        [scan.first_col_kind.get(index) for index in range(2, rows + 1)],
    )

    key = (1 if header_row.verdict else 0, 1 if header_col.verdict else 0)
    interior = scan.interior[key]
    body_rows = max(rows - key[0], 0)
    columns = tuple(
        _column(
            index=index,
            name=(scan.first_row.get(index) if header_row.verdict else None),
            scan=scan,
            body_rows=body_rows,
            # Row 1 is a value in this column, not a label for it, so it is folded back in.
            first_row_value=(None if header_row.verdict else scan.first_row.get(index)),
            first_row_kind=(None if header_row.verdict else scan.first_row_kind.get(index)),
            with_sketches=with_sketches,
            values_retained_max=values_retained_max,
        )
        for index in range(1, cols + 1)
    )
    rendered, reason = _render(scan, header_row.verdict, rows, cols)
    return SheetMeasurement(
        name=name,
        rows=rows,
        cols=cols,
        non_empty_cells=scan.non_empty_cells,
        fill_ratio=fill_ratio,
        declared_rows=declared_rows,
        declared_cols=declared_cols,
        header_row=header_row,
        header_col=header_col,
        interior_rows=body_rows,
        interior_cols=max(cols - key[1], 0),
        interior_non_empty=scan.interior_non_empty[key],
        interior_cardinality=interior.count if interior.exact else None,
        interior_cardinality_at_least=interior.count,
        interior_cardinality_exact=interior.exact,
        merged_cells=merged_cells,
        columns=columns,
        rendered_markdown=rendered,
        rendered_unbounded_reason=reason,
        distinct_tracked_max=distinct_tracked_max,
        values_retained_max=values_retained_max,
    )


def _column(
    *,
    index: int,
    name: Optional[str],
    scan: _Scan,
    body_rows: int,
    first_row_value: Optional[str],
    first_row_kind: Optional[str],
    with_sketches: bool,
    values_retained_max: int,
) -> ColumnMeasurement:
    distinct = scan.per_column_distinct.get(index) or _DistinctSet()
    non_empty = scan.per_column_non_empty.get(index, 0)
    types = dict(scan.per_column_types.get(index, {}))
    if first_row_value is not None:
        distinct = distinct.plus(first_row_value)
        non_empty += 1
        types[first_row_kind] = types.get(first_row_kind, 0) + 1
    # Ties break on the type NAME so that two runs over the same bytes cannot disagree;
    # a dict's insertion order is a fact about which row came first, not about the column.
    dominant = max(types, key=lambda kind: (types[kind], kind)) if types else TYPE_EMPTY
    exact = distinct.exact
    count = distinct.count
    # `is_unique` is 8.5's identifier factoid: a distinct value in EVERY row, so a gap
    # disqualifies a column even when what is there is all distinct. A column whose set
    # stopped being tracked cannot answer, and says so rather than guessing.
    if not exact:
        is_unique = None
    else:
        is_unique = bool(body_rows) and non_empty == body_rows and count == body_rows
    values = None
    if exact and count <= values_retained_max:
        values = sorted(distinct.values)
    sketch = None
    if with_sketches and count:
        builder = SketchBuilder()
        # Built from the DISTINCT set. A MinHash is insensitive to repetition, so where the
        # set is exact this is the whole column's sketch; where tracking stopped it is a
        # sketch of the tracked prefix and `partial` says so.
        for value in distinct.values:
            builder.update(value)
        sketch = builder.finish(partial=not exact)
    return ColumnMeasurement(
        index=index,
        letter=column_letter(index),
        name=name,
        dominant_type=dominant,
        type_counts=types,
        non_empty=non_empty,
        body_rows=body_rows,
        fill_ratio=(non_empty / body_rows) if body_rows else 0.0,
        distinct_count=count if exact else None,
        distinct_at_least=count,
        distinct_exact=exact,
        values=values,
        is_unique=is_unique,
        sketch=sketch,
    )


def _render(scan: _Scan, header_row: bool, rows: int, cols: int) -> tuple:
    """The sheet as one markdown table, or ``None`` and the reason there is none."""
    if scan.render_bounded:
        return None, (
            "a markdown table of this sheet spends at least one token on each row and one "
            f"on each non-empty cell, so its {rows} rows and {scan.non_empty_cells} filled "
            "cells put its token count above the embedding document window whatever the "
            "tokeniser does; the rendered rows were not kept"
        )
    if not rows or not cols:
        return None, "the sheet's used range holds no value, so there is no table to render"

    lines = [_markdown_row(row, cols) for row in scan.rendered_rows[:rows]]
    if header_row:
        head, body = lines[0], lines[1:]
    else:
        head = _markdown_row(tuple(column_letter(i) for i in range(1, cols + 1)), cols)
        body = lines
    separator = "| " + " | ".join("---" for _ in range(cols)) + " |"
    return "\n".join([head, separator, *body]), None


def _markdown_row(values: tuple, cols: int) -> str:
    padded = list(values[:cols]) + [""] * max(cols - len(values), 0)
    return "| " + " | ".join(_MARKDOWN_UNSAFE.sub(" ", value or "") for value in padded) + " |"


# ---------------------------------------------------------------------------
# Merged cells — tier 1, because tier 2 cannot answer it
# ---------------------------------------------------------------------------


def count_merged_cells(data: bytes, sheet_name: str) -> int:
    """How many merged ranges the sheet declares, from the worksheet part itself.

    ``ReadOnlyWorksheet`` does not carry ``merged_cells`` (module docstring, note 1), and
    opening the workbook without ``read_only`` to get it would materialise every cell as a
    Python object — which is the cost ``OFFICE_SPEC.md`` Part 4 note 2 exists to avoid. So
    the count comes from ``<mergeCells count="...">`` in the part, read with the standard
    library.

    ``count`` is trusted where the attribute is present and the children are counted where
    it is not; the schema makes the attribute optional, and a writer that omits it has not
    written a broken file.
    """
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        path = _worksheet_part(archive, sheet_name)
        with archive.open(path) as stream:
            total = 0
            for _event, element in ET.iterparse(stream, events=("end",)):
                if element.tag == f"{{{_S_NS}}}mergeCells":
                    declared = element.get("count")
                    total = (
                        int(declared)
                        if declared is not None and declared.isdigit()
                        else len(element)
                    )
                    element.clear()
                    break
                # Rows are the bulk of the part and `mergeCells` follows `sheetData`, so
                # clearing as we go is what keeps this a stream rather than a full parse
                # into memory.
                if element.tag == f"{{{_S_NS}}}row":
                    element.clear()
            return total


def _worksheet_part(archive: zipfile.ZipFile, sheet_name: str) -> str:
    """Resolve a sheet name to its part path through the workbook and its relationships.

    openpyxl 3.1.5 leaves ``ReadOnlyWorksheet.path`` as ``None`` and keeps the real path on
    a private attribute, so this walks the two parts that define the mapping rather than
    reading an underscore. They are small — a manifest each — and they are the same two
    parts ``probe`` already reads to count sheets.
    """
    names = set(archive.namelist())
    if "xl/workbook.xml" not in names:
        raise WorksheetPartMissing(
            "the package holds no xl/workbook.xml, so no sheet name resolves to a part"
        )
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    relationship_id = None
    for element in workbook.iter(f"{{{_S_NS}}}sheet"):
        if element.get("sheetId") is not None and element.get("name") == sheet_name:
            relationship_id = element.get(f"{{{_R_NS}}}id")
            break
    if relationship_id is None:
        raise WorksheetPartMissing(
            f"xl/workbook.xml names no sheet {sheet_name!r} with a relationship id"
        )
    if "xl/_rels/workbook.xml.rels" not in names:
        raise WorksheetPartMissing(
            f"sheet {sheet_name!r} is related through {relationship_id!r} and the package "
            "holds no xl/_rels/workbook.xml.rels to resolve it"
        )
    rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    for element in rels.iter(f"{{{_PR_NS}}}Relationship"):
        if element.get("Id") != relationship_id:
            continue
        target = element.get("Target") or ""
        path = target[1:] if target.startswith("/") else f"xl/{target}"
        path = path.replace("/./", "/")
        if path not in names:
            raise WorksheetPartMissing(
                f"sheet {sheet_name!r} resolves to {path!r}, which is not in the package"
            )
        return path
    raise WorksheetPartMissing(
        f"sheet {sheet_name!r} names relationship {relationship_id!r}, which "
        "xl/_rels/workbook.xml.rels does not define"
    )


__all__ = [
    "DISTINCT_TRACKED_MAX",
    "TYPE_BOOL",
    "TYPE_DATE",
    "TYPE_EMPTY",
    "TYPE_NUMBER",
    "TYPE_TEXT",
    "VALUES_RETAINED_MAX",
    "ColumnMeasurement",
    "HeaderEvidence",
    "SheetMeasurement",
    "WorksheetPartMissing",
    "canonical",
    "column_letter",
    "count_merged_cells",
    "measure_sheet",
    "value_type",
]
