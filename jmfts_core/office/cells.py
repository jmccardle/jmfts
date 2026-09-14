"""One worksheet's cells, typed, plus the two things read-only ``openpyxl`` cannot carry.

:mod:`jmfts_core.office.sheets` measures a sheet and keeps no cell. This module is the
other half: it hands back the cells themselves, in the types a JSON document can hold, so
``extract:sheet`` can write one record per row.

**openpyxl does the hard part and the standard library does the rest.** Shared strings,
number formats, date serial numbers and the 1900 leap-year bug are what a spreadsheet
reader is actually for, and ``openpyxl`` has had far more eyes on them than anything
written here would. Two facts it does not expose in read-only mode were MEASURED against
``openpyxl`` 3.1.5 rather than assumed:

1. ``ReadOnlyCell`` HAS NO ``quotePrefix`` ATTRIBUTE. A workbook loaded with
   ``read_only=False`` reports it; the same workbook loaded read-only does not have the
   attribute at all. This is the identical shape to the ``merged_cells`` finding in
   :mod:`~jmfts_core.office.sheets`, and it gets the identical answer: the flag is read
   from the package with the standard library (:func:`read_cell_notes`), because loading
   without ``read_only`` would materialise every cell as a Python object.

2. ``data_only`` IS AN EITHER/OR. With ``data_only=True`` a formula cell reads back as its
   cached value; with ``data_only=False`` it reads back as the formula text and the cached
   value is gone. There is no load that gives both. The worksheet part holds them side by
   side in one element — ``<c r="C2"><f>VLOOKUP(...)</f><v>Northeast</v></c>`` — so the
   value comes from ``openpyxl`` and the formula text comes from the same standard-library
   pass as the quote prefix. Two openpyxl loads would also work and cost a second full
   decode of every cell.

**Why the formula is worth the pass at all.** A formula is the one place in a workbook
where the author states a relationship outright: ``=VLOOKUP(B2,Lookup!A:B,2,FALSE)`` says
that this sheet's column B resolves against that sheet's column A. Everything else this
appliance can learn about cross-sheet structure is inferred from counting. This is
declared, and the rest of the tree already prefers declared evidence over inferred
evidence wherever a format offers both.

**What the quote prefix does and does not mean.** In Excel a leading apostrophe forces a
cell to be text. The apostrophe is not part of the value — the value is stored as a string
and comes back from ``openpyxl`` as one, leading zeros intact. What the flag adds is the
author's intent: ``0012345`` in this cell is an identifier and not a number that lost its
zeros. Nothing here acts on it. It is recorded so that a consumer comparing this column
with one holding the integer ``12345`` can see why the two do not match, rather than
concluding the values differ.

TIER 2, and in :mod:`jmfts_core.office` for that reason: ``openpyxl`` is imported inside
:func:`~jmfts_core.office.require_openpyxl` at the point of use.
"""

from __future__ import annotations

import datetime
import io
import re
import zipfile
from dataclasses import dataclass
from typing import Any, Iterable, Optional
from xml.etree import ElementTree as ET

from jmfts_core.office import require_openpyxl

# Private, and imported rather than copied. `_worksheet_part` resolves a sheet NAME to a
# part path through `xl/workbook.xml` and its relationships, which is the same walk this
# module needs and is already the one `count_merged_cells` is tested against. A second copy
# would be free to disagree with the first about which part a sheet is.
#
# `_MARKDOWN_UNSAFE` for the same reason: `render_region` below writes markdown table cells
# and `sheets._render` already writes them. One escaping rule, in one place — two copies
# would be free to disagree about whether a pipe closes a cell.
#
# `column_index` WAS DEFINED HERE and moved to `sheets` beside its inverse when the header
# scan's merged-range parser needed it. It is still exported from this module, because that
# is where every caller of it reaches for it and a name is not worth moving twice.
from jmfts_core.office.sheets import (
    _MARKDOWN_UNSAFE,
    _S_NS,
    _worksheet_part,
    canonical,
    column_index,
    column_letter,
)

#: How many rows one call will read before it raises. A named limit that FAILS rather than
#: truncating, per ``SPRINT_0_3_0.md`` 6.6: a caller that silently got the first N rows of
#: a sheet cannot tell that from a sheet with N rows, and every count taken downstream
#: would then be a count of a prefix presented as a whole.
#:
#: It is a required keyword on :func:`read_rows` rather than a default here, because the
#: number that matters is the one the CALLER can afford — for ``extract:sheet`` that is a
#: node count, and it belongs in the task's parameters where the attempt record sees it.
#: This constant is only the ceiling the reader itself will not go past.
ROWS_READ_MAX = 100_000

#: How many cells one :func:`read_region` will serve before it raises. The same rule as
#: :data:`ROWS_READ_MAX` — ``SPRINT_0_3_0.md`` 6.6's named limit that FAILS rather than
#: truncating — applied to the unit a *region* is priced in, which is cells and not rows:
#: ``A1:XFD400`` is four hundred rows and six and a half million cells.
#:
#: The number is a CEILING, not a measurement, and it is round on purpose so that it does
#: not read as something that was counted. Two things put it here rather than higher: a
#: markdown table of N cells costs at least a byte per cell and in practice ten or so, so
#: fifty thousand cells is around a megabyte of rendered table in a response a caller is
#: waiting on; and ``INGEST_SPEC.md`` 8.4's own worked example is a 1,284-row sheet, which
#: at eight columns this clears by a factor of five.
CELLS_READ_MAX = 50_000

#: The format's own limits, from ECMA-376: 1,048,576 rows and 16,384 columns (``XFD``). A
#: reference outside them addresses no cell of any workbook, so it is rejected as malformed
#: rather than read as an empty region — ``B4:B99999999`` is a typo, and an empty answer
#: would present it as a fact about the sheet.
SHEET_MAX_ROW = 1_048_576
SHEET_MAX_COL = 16_384

#: SpreadsheetML's main namespace, used by both the worksheet part and the styles part.
_STYLES_PART = "xl/styles.xml"

#: One A1-style cell reference, with the ``$`` absolute markers Excel writes into a copied
#: reference. They are not part of the address — ``$B$4`` and ``B4`` are the same cell — so
#: they are accepted and dropped.
_CELL_REF = re.compile(r"^\$?([A-Za-z]{1,3})\$?([0-9]{1,7})$")


@dataclass(frozen=True)
class CellNote:
    """What a cell carries beyond its value, for the cells that carry anything.

    Sparse on purpose. Most cells in most sheets have no formula and no quote prefix, and
    a note per cell would be a second copy of the sheet made of mostly-empty records.

    ``formula_shared`` marks a cell that participates in a shared formula group. Excel
    writes the text once on the group's master cell and leaves the rest as a back
    reference, so ``formula`` here is the MASTER's text with the master's cell references,
    not this cell's. Translating those references is a formula-language problem this module
    does not attempt; the flag says the text is not literally this cell's so that nothing
    downstream reads it as though it were.
    """

    formula: Optional[str] = None
    formula_shared: bool = False
    text_forced: bool = False

    def as_dict(self) -> dict:
        """Only the keys that say something, for a JSONB write."""
        stored: dict = {}
        if self.formula is not None:
            stored["formula"] = self.formula
            if self.formula_shared:
                stored["formula_shared"] = True
        if self.text_forced:
            stored["text_forced"] = True
        return stored


@dataclass(frozen=True)
class RowCells:
    """One worksheet row: its real row number, its values, and its sparse notes.

    ``index`` is the 1-based worksheet row number, not a position in :attr:`SheetRows.rows`.
    A sheet whose data starts at row 5, or which has a blank row in the middle, must not
    have its rows renumbered — the number is how a person finds the row again in Excel.

    ``values`` is positional and 1-based by column, stored 0-based here, padded to the
    sheet's used width so that a column index is a position and never a search.
    """

    index: int
    values: tuple
    notes: dict


@dataclass(frozen=True)
class SheetRows:
    name: str
    cols: int
    rows: tuple
    #: True when :func:`read_cell_notes` was not run, so every :attr:`RowCells.notes` is
    #: empty because nothing looked rather than because there was nothing to find.
    notes_read: bool


class TooManyRows(ValueError):
    """The sheet has more rows than the caller said it could take.

    Raised rather than returning a prefix. ``SPRINT_0_3_0.md`` 6.6 states the rule for
    ``extract:sheet``'s fan-out and it is this one: a named limit that fails the task, not
    a silent truncation.
    """


class TooManyCells(ValueError):
    """The region names more cells than the caller said it could take.

    :class:`TooManyRows`'s rule in the unit a region is priced in. Raised BEFORE anything
    is read, from the rectangle's own arithmetic, so a reference naming a million cells
    costs one multiplication rather than a decode of the worksheet part.
    """


class BadCellRef(ValueError):
    """The reference does not name a rectangle of cells.

    Malformed, sheet-qualified, or outside the format's own row/column limits. Never
    clamped and never read as an empty region: an empty region is a fact about the sheet,
    and a caller who mistyped ``B4:H12O`` must not be told the sheet has nothing in it.
    """


@dataclass(frozen=True)
class CellRange:
    """A rectangle of cells, 1-based and inclusive at both ends.

    Normalised on construction by :func:`parse_ref`, so ``min_`` is always the smaller
    bound. That is not a repair of a bad input: ``H120:B4`` and ``B4:H120`` name the same
    rectangle to Excel and to anything reading one, and the pair is unordered.
    """

    min_row: int
    min_col: int
    max_row: int
    max_col: int

    @property
    def rows(self) -> int:
        return self.max_row - self.min_row + 1

    @property
    def cols(self) -> int:
        return self.max_col - self.min_col + 1

    @property
    def cells(self) -> int:
        """The area. What :data:`CELLS_READ_MAX` is measured against."""
        return self.rows * self.cols

    @property
    def column_letters(self) -> tuple:
        """``("B", "C", ..., "H")`` — one per column of the rectangle, left to right."""
        return tuple(column_letter(index) for index in range(self.min_col, self.max_col + 1))

    @property
    def ref(self) -> str:
        """The rectangle as one A1-style string, e.g. ``"B4:H120"``.

        Always the two-cell form, even for a single cell. It is what was SERVED, and a
        caller comparing it with what it asked for should not have to normalise ``B4``
        against ``B4:B4`` to see that the two agree.
        """
        return (
            f"{column_letter(self.min_col)}{self.min_row}:"
            f"{column_letter(self.max_col)}{self.max_row}"
        )


@dataclass(frozen=True)
class RegionRow:
    """One row of a region: its worksheet row number, its values, and its sparse notes.

    ``index`` is the 1-based worksheet row number, as :class:`RowCells`'s is, and for the
    same reason — it is how a person finds the row again in Excel.

    ``values`` is positional across the REGION, left to right: ``values[0]`` is the region's
    first column, which is column A only when the region starts there. This is the one place
    it differs from :class:`RowCells`, and it is why that class is not reused. ``notes`` is
    keyed by ABSOLUTE column number, so a note's address does not depend on where the
    rectangle happens to start.
    """

    index: int
    values: tuple
    notes: dict


@dataclass(frozen=True)
class CellRegion:
    """What :func:`read_region` read: one rectangle of one sheet.

    ``rows`` holds only the rows of the rectangle that carry something. A rectangle is
    mostly empty far more often than not — a caller naming ``B4:H120`` over a sheet whose
    data stops at row 40 wants the forty rows, not eighty records saying there is nothing
    there — and every row carries its own worksheet row number, so nothing is lost by their
    absence. :attr:`bounds` is what was asked for; ``rows`` is what was in it.
    """

    sheet: str
    bounds: CellRange
    rows: tuple
    #: True when :func:`read_cell_notes` was not run, so every :attr:`RegionRow.notes` is
    #: empty because nothing looked rather than because there was nothing to find.
    notes_read: bool


# ---------------------------------------------------------------------------
# Values, as JSON holds them
# ---------------------------------------------------------------------------


def json_value(value: Any) -> Any:
    """One cell as a JSON-native value, or ``None``.

    This is :func:`~jmfts_core.office.sheets.canonical`'s normalisation with one difference:
    a number stays a number and a boolean stays a boolean. The two must agree everywhere
    else, because a record written here and a value counted there are meant to be the same
    value — ``canonical`` produces the string a comparison keys on, and this produces the
    value a consumer reads.

    The rules that are shared, and why each one is a rule rather than a default:

    * A string is stripped, and a string of nothing but whitespace is ``None``. A cell
      holding three spaces is not a value a person put there to mean something.
    * A float that is integral becomes an ``int``. A spreadsheet has one numeric type and
      stores every number as a double, so ``1`` and ``1.0`` are the same cell to Excel and
      a record that reported ``1.0`` would be reporting the storage rather than the value.
    * A ``datetime`` at exactly midnight becomes a plain date. ``openpyxl`` returns
      ``datetime.datetime`` for any date-formatted cell with no way to tell a date from a
      date-time whose time is zero, and ``2026-09-30`` is what such a column holds.

    Temporal values become ISO 8601 strings because JSON has no date type. That is the one
    place a type is lost, and it is lost to the transport rather than to a choice here.
    """
    if value is None:
        return None
    # Before `int`: `bool` is a subclass of it, and a boolean column must not come back as
    # ones and zeroes.
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        # `is_integer` is exact and `float` round-trips through JSON, so neither branch
        # loses a digit.
        return int(value) if value.is_integer() else value
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


# ---------------------------------------------------------------------------
# The standard-library pass: formulas and the quote prefix
# ---------------------------------------------------------------------------


def _quote_prefix_flags(archive: zipfile.ZipFile) -> list:
    """One flag per ``cellXfs`` entry, which is what a cell's ``s`` attribute indexes.

    ``cellXfs`` specifically, and not ``cellStyleXfs``: both are lists of ``<xf>`` elements
    in the same part and only the first is what ``<c s="1">`` refers to. Reading the wrong
    one would resolve every cell to a flag belonging to a named style.

    An empty list where the part is absent or holds no ``cellXfs``. A workbook with no
    styles part has no cell that forced text, so "no flags" and "every flag false" are the
    same answer here.
    """
    if _STYLES_PART not in set(archive.namelist()):
        return []
    root = ET.fromstring(archive.read(_STYLES_PART))
    table = root.find(f"{{{_S_NS}}}cellXfs")
    if table is None:
        return []
    return [entry.get("quotePrefix") in ("1", "true") for entry in table]


def read_cell_notes(data: bytes, sheet_name: str, *, max_rows: int) -> dict:
    """``{(row, column): CellNote}`` for the cells that carry one. Standard library only.

    One streaming pass over the worksheet part. Rows past ``max_rows`` are not read at all,
    which is what bounds this: the returned dict is at most the number of annotated cells
    in the rows the caller is going to keep, and the parse never walks past them.

    Shared formulas are resolved forward. Excel writes ``<f t="shared" si="0">A2*2</f>`` on
    the group's first cell and ``<f t="shared" si="0"/>`` on the rest, so the text is
    remembered by ``si`` and handed to the members that follow. A member appearing before
    its master — which the schema does not forbid and no writer does — gets a note with the
    shared flag and no text, which is what was actually read.
    """
    notes: dict = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        quote_prefix = _quote_prefix_flags(archive)
        path = _worksheet_part(archive, sheet_name)
        shared_text: dict = {}
        row_tag = f"{{{_S_NS}}}row"
        cell_tag = f"{{{_S_NS}}}c"
        formula_tag = f"{{{_S_NS}}}f"
        with archive.open(path) as stream:
            for _event, element in ET.iterparse(stream, events=("end",)):
                if element.tag == row_tag:
                    # The bulk of the part is rows, and a row that has been read is dead
                    # weight. `count_merged_cells` clears the same way for the same reason.
                    element.clear()
                    continue
                if element.tag != cell_tag:
                    continue
                reference = element.get("r") or ""
                row_number = _row_number(reference)
                if row_number is None:
                    continue
                if row_number > max_rows:
                    # `<sheetData>` is in row order, so the first row past the caller's
                    # bound is the end of anything it can use.
                    break
                note = _cell_note(element, formula_tag, quote_prefix, shared_text)
                if note is not None:
                    notes[(row_number, column_index(reference))] = note
    return notes


def _row_number(reference: str) -> Optional[int]:
    digits = "".join(character for character in reference if character.isdigit())
    return int(digits) if digits else None


def _cell_note(element, formula_tag: str, quote_prefix: list, shared_text: dict):
    """One ``<c>`` element, or ``None`` when it carries nothing worth a note."""
    style = element.get("s")
    text_forced = False
    if style is not None and style.isdigit():
        position = int(style)
        text_forced = position < len(quote_prefix) and quote_prefix[position]

    formula = None
    shared = False
    node = element.find(formula_tag)
    if node is not None:
        shared = node.get("t") == "shared"
        index = node.get("si")
        if node.text:
            formula = node.text
            if shared and index is not None:
                shared_text[index] = formula
        elif shared and index is not None:
            formula = shared_text.get(index)

    if formula is None and not text_forced:
        return None
    return CellNote(formula=formula, formula_shared=shared, text_forced=text_forced)


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------


def read_rows(
    data: bytes,
    sheet_name: str,
    *,
    max_rows: int,
    with_notes: bool = True,
) -> SheetRows:
    """Every row of one sheet that holds a value, typed, bounded, and annotated.

    ``max_rows`` counts the rows that HOLD A VALUE, which is the number a caller is really
    bounding — an empty row costs nothing downstream. It is a required keyword because the
    reader has no opinion about what the caller can afford, and it is capped at
    :data:`ROWS_READ_MAX` so that a caller passing something enormous by accident still
    fails at a stated number rather than by running out of memory.

    A row with no value in any cell yields nothing and does not consume the bound. Its row
    number is simply absent from the result, which is the same information without a record
    saying so 40,000 times.
    """
    if max_rows < 1:
        raise ValueError(f"max_rows must be at least 1; got {max_rows!r}")
    if max_rows > ROWS_READ_MAX:
        raise ValueError(
            f"max_rows of {max_rows:,} is above this reader's ceiling of "
            f"{ROWS_READ_MAX:,} rows (jmfts_core.office.cells.ROWS_READ_MAX); a sheet that "
            "large is not a set of records anybody reads one at a time"
        )

    notes = read_cell_notes(data, sheet_name, max_rows=max_rows) if with_notes else {}

    openpyxl = require_openpyxl()
    # `data_only=True`: the record holds VALUES. The formula TEXT is what the
    # standard-library pass above collected, because no single load gives both — see the
    # module docstring, note 2.
    workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        if sheet_name not in workbook.sheetnames:
            raise ValueError(
                f"the workbook names no sheet {sheet_name!r}; it names " f"{workbook.sheetnames!r}"
            )
        rows, cols = _collect(workbook[sheet_name], notes, max_rows=max_rows)
    finally:
        # A read-only workbook holds the ZIP open and the caller is inside a database
        # transaction.
        workbook.close()

    return SheetRows(name=sheet_name, cols=cols, rows=rows, notes_read=with_notes)


def _collect(worksheet, notes: dict, *, max_rows: int) -> tuple:
    """The scan. ``iter_rows`` is anchored at A1, so a position is a real column number."""
    by_row = _grouped_by_row(notes)
    collected: list = []
    cols = 0
    for row_number, raw_row in enumerate(worksheet.iter_rows(values_only=True), start=1):
        values = [json_value(raw) for raw in raw_row]
        row_notes = by_row.get(row_number, {})
        # A CELL, not a value. A formula whose result was never cached reads back as `None`
        # from the value pass, so a row's width taken from its values alone would end
        # before its own formula column and drop the note that says the column is there.
        # That is the case a workbook openpyxl itself wrote is entirely made of.
        last = max(_last_filled(values), max(row_notes, default=0))
        if last == 0:
            continue
        if len(collected) >= max_rows:
            raise TooManyRows(
                f"sheet {worksheet.title!r} holds more than {max_rows:,} rows with a value "
                f"in them; row {row_number} is past the bound this read was given"
            )
        cols = max(cols, last)
        values += [None] * max(last - len(values), 0)
        collected.append(
            RowCells(
                index=row_number,
                values=tuple(values[:last]),
                notes=row_notes,
            )
        )
    # Padded only now, because the used width is not known until the last row has been read
    # and a ragged tuple would make a column index mean different things on different rows.
    return tuple(_padded(row, cols) for row in collected), cols


def _padded(row: RowCells, cols: int) -> RowCells:
    if len(row.values) == cols:
        return row
    return RowCells(
        index=row.index,
        values=row.values + (None,) * (cols - len(row.values)),
        notes=row.notes,
    )


def _grouped_by_row(notes: dict) -> dict:
    """``{(row, col): note}`` -> ``{row: {col: note}}``, once, before the scan.

    The scan needs one row's notes per row and a lookup per column would walk the whole
    dict every time.
    """
    grouped: dict = {}
    for (row, column), note in notes.items():
        grouped.setdefault(row, {})[column] = note
    return grouped


def _last_filled(values: Iterable) -> int:
    last = 0
    for position, value in enumerate(values, start=1):
        if value is not None:
            last = position
    return last


# ---------------------------------------------------------------------------
# A region: one rectangle, addressed the way a person addresses one
# ---------------------------------------------------------------------------


def parse_ref(text: str) -> CellRange:
    """``"B4:H120"`` -> a :class:`CellRange`. ``OFFICE_SPEC.md`` Part 5's ``ref``.

    Two forms are accepted, and they are the two an anchor is written in: a rectangle
    (``B4:H120``) and a single cell (``B4``, which is the 1x1 rectangle ``B4:B4``). The
    ``$`` of an absolute reference is dropped and the ends are put in order, because
    ``$H$120:$B$4`` names the same rectangle.

    **What is refused, and why each is refused rather than resolved.**

    * ``B:H`` and ``4:120`` — whole columns and whole rows. They are valid Excel references
      and they mean "as far as the sheet goes", which is a bound this function cannot know
      and the caller did not state. Resolving them against the used range would make the
      same string mean different rectangles on the same sheet at different times.
    * ``Deals!B4:H120`` — a sheet-qualified reference. The sheet is the node being read;
      accepting a second opinion about it would let a caller address any sheet of the
      workbook through a node that names one.
    * Anything outside :data:`SHEET_MAX_ROW` / :data:`SHEET_MAX_COL`. It addresses no cell
      of any workbook, so it is a typo, and an empty region would report it as a fact.
    """
    raw = (text or "").strip()
    if not raw:
        raise BadCellRef(
            "an empty cell reference names no region; use a rectangle (`B4:H120`) or a "
            "single cell (`B4`), or omit `ref` for the sheet's used range"
        )
    if "!" in raw:
        raise BadCellRef(
            f"{raw!r} names a sheet, and the sheet is the document being read; give the "
            "reference alone, as `B4:H120`"
        )
    parts = raw.split(":")
    if len(parts) == 1:
        first = second = parts[0]
    elif len(parts) == 2:
        first, second = parts
    else:
        raise BadCellRef(f"{raw!r} is not a cell reference; a rectangle has two ends, as `B4:H120`")

    rows, cols = [], []
    for part in (first, second):
        match = _CELL_REF.match(part)
        if match is None:
            raise BadCellRef(
                f"{part!r} is not a cell reference. Accepted: a rectangle `B4:H120` or a "
                "single cell `B4`. Whole-column (`B:H`) and whole-row (`4:120`) references "
                "are not, because they mean `as far as the sheet goes` and that bound is "
                "not stated in the request"
            )
        letters, digits = match.groups()
        row = int(digits)
        col = column_index(letters)
        if not 1 <= row <= SHEET_MAX_ROW:
            raise BadCellRef(
                f"{part!r} names row {row}, and a worksheet has rows 1 to " f"{SHEET_MAX_ROW:,}"
            )
        if not 1 <= col <= SHEET_MAX_COL:
            raise BadCellRef(
                f"{part!r} names column {letters.upper()!r}, and a worksheet has columns A "
                f"to {column_letter(SHEET_MAX_COL)}"
            )
        rows.append(row)
        cols.append(col)

    return CellRange(min_row=min(rows), min_col=min(cols), max_row=max(rows), max_col=max(cols))


def used_range(rows: int, cols: int) -> Optional[CellRange]:
    """The rectangle ``A1:<cols><rows>``, or ``None`` when the sheet holds nothing.

    ``rows`` and ``cols`` are ``profile:sheet``'s measurements — the last row and the last
    column that hold a value — so this is the sheet's used range as it was MEASURED, not as
    the file's optional ``<dimension>`` element declares it. The two disagree often enough
    that the profile records both (``sheet_profile.py``), and the measured pair is the one
    that describes the cells.

    ``None`` rather than ``A1:A1`` for an empty sheet. A sheet with no used range has no
    region to default to, and a 1x1 rectangle over a cell that holds nothing would answer a
    question about the sheet with a shape invented here.
    """
    if rows < 1 or cols < 1:
        return None
    return CellRange(min_row=1, min_col=1, max_row=rows, max_col=cols)


def read_region(
    data: bytes,
    sheet_name: str,
    *,
    bounds: CellRange,
    max_cells: int,
    with_notes: bool = True,
) -> CellRegion:
    """One rectangle of one sheet: values, and the notes that carry formulas.

    ``max_cells`` is a required keyword for the reason :func:`read_rows`'s ``max_rows`` is —
    the reader has no opinion about what the caller can afford — and it is capped at
    :data:`CELLS_READ_MAX`. The check happens against the rectangle's own arithmetic, before
    the package is opened, so a reference naming six million cells is refused without a
    decode.

    The value pass and the note pass are the two this module's docstring describes, over the
    same rectangle: ``openpyxl`` with ``data_only=True`` for what each cell EVALUATED to, and
    the standard-library pass for the formula text and the quote prefix that a read-only load
    cannot carry.
    """
    if max_cells < 1:
        raise ValueError(f"max_cells must be at least 1; got {max_cells!r}")
    if max_cells > CELLS_READ_MAX:
        raise ValueError(
            f"max_cells of {max_cells:,} is above this reader's ceiling of "
            f"{CELLS_READ_MAX:,} cells (jmfts_core.office.cells.CELLS_READ_MAX)"
        )
    if bounds.cells > max_cells:
        raise TooManyCells(
            f"{bounds.ref} on sheet {sheet_name!r} is {bounds.rows:,} row(s) by "
            f"{bounds.cols:,} column(s), which is {bounds.cells:,} cells; this read serves "
            f"at most {max_cells:,} (jmfts_core.office.cells.CELLS_READ_MAX). Name a "
            "smaller region"
        )

    notes = _region_notes(data, sheet_name, bounds) if with_notes else {}

    openpyxl = require_openpyxl()
    # `data_only=True` and `read_only=True`, for the two reasons the module docstring gives:
    # the value is what the cell evaluated to, and a full load materialises the whole grid.
    workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        if sheet_name not in workbook.sheetnames:
            raise ValueError(
                f"the workbook names no sheet {sheet_name!r}; it names {workbook.sheetnames!r}"
            )
        rows = _collect_region(workbook[sheet_name], bounds, notes)
    finally:
        # A read-only workbook holds the ZIP open and the caller is inside a database
        # transaction.
        workbook.close()

    return CellRegion(sheet=sheet_name, bounds=bounds, rows=rows, notes_read=with_notes)


def _region_notes(data: bytes, sheet_name: str, bounds: CellRange) -> dict:
    """:func:`read_cell_notes`, bounded by the rectangle's last row and clipped to it.

    The bound is what keeps the pass cheap: the parse stops at the rectangle's last row and
    never walks the rest of the part. The clip is what keeps the ANSWER the rectangle —
    formulas in the rows above ``min_row`` were read on the way past and belong to cells
    nobody asked for.
    """
    found = read_cell_notes(data, sheet_name, max_rows=bounds.max_row)
    return {
        (row, column): note
        for (row, column), note in found.items()
        if bounds.min_row <= row <= bounds.max_row and bounds.min_col <= column <= bounds.max_col
    }


def _collect_region(worksheet, bounds: CellRange, notes: dict) -> tuple:
    """The scan, over the rectangle only. Rows that carry nothing yield nothing.

    ``iter_rows`` with all four bounds yields consecutive rows starting at ``min_row``,
    padding rows the part omits, and each row is exactly the rectangle's width — so the row
    number is an offset from ``min_row`` and a position is a real column number. Both are
    properties of ``openpyxl``'s ``_cells_by_row`` and both are asserted by the tests, not
    assumed here.
    """
    by_row = _grouped_by_row(notes)
    collected: list = []
    for offset, raw_row in enumerate(
        worksheet.iter_rows(
            min_row=bounds.min_row,
            max_row=bounds.max_row,
            min_col=bounds.min_col,
            max_col=bounds.max_col,
            values_only=True,
        )
    ):
        row_number = bounds.min_row + offset
        if row_number > bounds.max_row:
            break
        values = [json_value(raw) for raw in raw_row][: bounds.cols]
        values += [None] * (bounds.cols - len(values))
        row_notes = by_row.get(row_number, {})
        # A CELL, not a value — the same rule `_collect` applies, and for the same reason: a
        # formula whose result was never cached reads back as `None` from the value pass, and
        # a row held to its values alone would vanish along with the note that says the
        # formula is there.
        if not row_notes and all(value is None for value in values):
            continue
        collected.append(RegionRow(index=row_number, values=tuple(values), notes=row_notes))
    return tuple(collected)


def render_region(region: CellRegion) -> str:
    """The region as one markdown table, addressed the way the sheet is.

    **The first column holds the worksheet row number and the header row holds the column
    letters.** That is a grid, which is what a spreadsheet region is and what the person
    reading it has open in another window; and it is the only rendering that stays honest
    when the rectangle does not start at A1 or when the rows it holds are not contiguous.
    :func:`~jmfts_core.office.sheets._render` renders a whole sheet from A1 as a plain table
    with the sheet's own header row on top, and the two are different products of different
    inputs — one is a table the model reads, this one is an address a person checks.

    A rectangle holding nothing renders as its header and separator alone: the columns that
    were asked for, and no row claiming to be a row.
    """
    letters = [
        column_letter(index) for index in range(region.bounds.min_col, region.bounds.max_col + 1)
    ]
    head = "| " + " | ".join(["", *letters]) + " |"
    separator = "| " + " | ".join("---" for _ in range(len(letters) + 1)) + " |"
    body = [
        "| " + " | ".join([str(row.index), *(_markdown_cell(value) for value in row.values)]) + " |"
        for row in region.rows
    ]
    return "\n".join([head, separator, *body])


def _markdown_cell(value: Any) -> str:
    """One value as the same string every other rendering in this package produces."""
    text = canonical(value)
    return _MARKDOWN_UNSAFE.sub(" ", text) if text is not None else ""


def cell_ref(row: int, column: int) -> str:
    """``(4, 2)`` -> ``"B4"``. The address a note is keyed by on the wire."""
    return f"{column_letter(column)}{row}"


__all__ = [
    "CELLS_READ_MAX",
    "ROWS_READ_MAX",
    "SHEET_MAX_COL",
    "SHEET_MAX_ROW",
    "BadCellRef",
    "CellNote",
    "CellRange",
    "CellRegion",
    "RegionRow",
    "RowCells",
    "SheetRows",
    "TooManyCells",
    "TooManyRows",
    "cell_ref",
    "column_index",
    "json_value",
    "parse_ref",
    "read_cell_notes",
    "read_region",
    "read_rows",
    "render_region",
    "used_range",
]
