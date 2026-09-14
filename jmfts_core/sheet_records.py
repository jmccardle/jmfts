"""Rows as records. ``INGEST_SPEC.md`` 8.4's ``records`` shape, built from stored cells.

8.4 defines the shape exactly and this module produces it and nothing else::

    content:  "Deal ID: D-4471. Account: Northwind Freight. Value: 128000."
    evidence `record`:    {"Deal ID": "D-4471",
                           "Account": "Northwind Freight",
                           "Value": 128000}
    evidence `row_index`: 47

``content`` is labelled prose rather than JSON because ``content`` is what gets embedded,
and braces, quotes and colons carry no meaning for the model while consuming tokens.
``record`` keeps the typed values, so a retrieval hit returns data rather than a string
somebody has to parse back.

**The shape rule here is ``header_row``, and it is not one of 8.8's thresholds.** 8.4 picks
among four shapes using several numbers that have never been calibrated, and
:mod:`jmfts_core.sheet_profile` refuses to invent them. This module needs none of them: a
sheet with a row in its first eight holding a distinct value in every column has record
keys, and a sheet without one has none. That is a measured boolean, already computed by
``profile:sheet``, and it decides only whether records are possible — not whether they are
the best representation. ``matrix`` and ``unstructured`` are still unbuilt and still waiting
on the corpus; ``small_table`` is built and is not a competitor, because a sheet that
matches both shapes gets both (:data:`BOTH_SHAPES_BASIS`).

**Where the keys come from.** The stored profile's column names AND the row number it found
them on, not a re-reading of the sheet. ``profile:sheet`` decided which row was the header —
it is not necessarily row 1 — and recorded the label per column; a second derivation here
could disagree with it, and then the profile and the records would describe different
sheets.

**A row too long to embed becomes a container over its columns**, and that is
:func:`plan_record` rather than :func:`build_records` — the record is the same either way,
and what changes is how many nodes carry it. The rule is the tree's own: a node whose text
does not fit the token/maxsim window holds no ``content`` and gets children instead, and
``summarize`` gives it a document vector over their concatenation. A ``section`` is that
shape already. What makes a row worth splitting by COLUMN rather than by character is that
the columns are named, so every piece can say which field it is part of.

This module imports no reader, touches no database and runs no model. It is a pure function
of what :mod:`jmfts_core.office.cells` read, which is what lets 8.4's wording be tested
without a workbook — the fit test and the chunker arrive as callables for the same reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from jmfts_core.office.cells import RowCells, SheetRows
from jmfts_core.office.sheets import NAME_SOURCE_HEADER

# The usetype 8.4's `records` shape gives each row node is `USETYPE_RECORD`, and it is
# DEFINED on the model with every other ingest usetype. See `jmfts_core.models.document`.

#: The name of the shape, as 8.4 names it, written onto the sheet node.
SHAPE_RECORDS = "records"

#: 8.4's first shape: the whole sheet as one markdown table, in one node, embedded as-is.
#: Written onto the sheet node beside :data:`SHAPE_RECORDS` when both match — see
#: :data:`BOTH_SHAPES_BASIS`.
SHAPE_SMALL_TABLE = "small_table"

# `HEADER_ROW_NUMBER = 1` WAS A MODULE CONSTANT HERE and it is gone. 8.3 defined
# `header_row` as a property of row 1 outright, so the row to skip was a constant and this
# named it once. The header is now looked for in rows 1 to 8
# (`jmfts_core.office.sheets.HEADER_SCAN_ROWS`), which makes its row a MEASUREMENT —
# `sheet.measurements.header_row_number` — and `build_records` takes it as an argument.
# A default of 1 here would have been a second source of a number the profile already
# measured, and the one that ran would depend on which caller supplied it.

#: Why this shape was chosen, written onto the sheet node beside it. It replaces
#: :data:`jmfts_core.sheet_profile.SHAPE_DEFERRED_REASON` on a sheet that got records, and
#: it is deliberately explicit that the four-way branch was NOT run — a reader who finds
#: ``shape: "records"`` must not conclude that 8.8's thresholds got set.
SHAPE_BASIS = (
    "INGEST_SPEC.md 8.4's four-way branch was not run: 8.8 leaves its thresholds unset. "
    "`records` was chosen on the one input that is a measured boolean rather than a "
    "threshold — 8.3's `header_row`, which is true here, so the row the header scan found "
    "supplies a key for every column. That decides only that records are POSSIBLE. Whether "
    "`matrix` would suit this sheet better is still what the calibration corpus is for; "
    "`small_table` is no longer a competitor, because a sheet that matches both gets both."
)

#: Why a sheet got no records. Not a failure: a sheet with no header row has no keys, and
#: the shapes that cover it (`matrix`, `unstructured`) are unbuilt.
NO_HEADER_REASON = (
    "INGEST_SPEC.md 8.3's `header_row` is false for this sheet: neither the all-text rule "
    "nor the any-type pass found a row in the first 8 that holds a distinct value in every "
    "column, so nothing supplies a key and there is no record to write. The shapes 8.4 "
    "gives such a sheet — `matrix` and `unstructured` — read thresholds 8.8 leaves unset "
    "and have no handler; `header_row_candidates` on this node is what a calibration sweep "
    "replays against."
)

#: Why this sheet also got a ``table`` node. 8.4's ``small_table``, and the one input it
#: needs is a token count against a window rather than one of 8.8's thresholds.
TABLE_SHAPE_BASIS = (
    "INGEST_SPEC.md 8.4's `small_table`: the sheet rendered as one markdown table fits the "
    "embedding document window, which is the model's own limit and therefore the largest "
    "value that test could be calibrated to. That is a token count against a window, not "
    "one of 8.8's unset thresholds. The window is 8192 and not 512 because the two are not "
    "close on the open web: 68.5% of FUSE sheets render inside 8192 and 16.4% inside 512, "
    "and 52.0% fit the first and not the second (measured 2026-09-03, scripts/"
    "render_tokens.py over 35,751 FUSE and 9,257 git-corpora sheets)."
)

#: Why a sheet got no ``table`` node.
NO_TABLE_REASON = (
    "the sheet does not render to a markdown table inside the embedding document window, "
    "so INGEST_SPEC.md 8.4's `small_table` does not match it; `rendered_tokens`, "
    "`rendered_unbounded_reason` and `rendered_withheld_reason` on this node say which of "
    "the two ways it missed."
)

#: Why both shapes are written where both match, rather than the first one 8.4 lists.
#:
#: **This AMENDS 8.4, which says "the first match wins".** Measured at the 8192 window over
#: the same two corpora on 2026-09-03: both shapes match 29.4% of git-corpora sheets and
#: 12.5% of FUSE sheets, `table` alone matches 56.8% / 56.0%, `records` alone 3.1% / 4.6%.
#: Across the 2,720 git and 4,462 FUSE contested sheets the median holds 7 and 13 data rows;
#: 39.9% of the git contested set has five data rows or fewer and 12.9% has exactly one, and
#: their median render is 168 and 419 tokens. So 8.4's own argument for `small_table` — "a
#: small table is often exactly the retrieval unit we want, and splitting it destroys it" —
#: is strongest precisely where the two shapes collide, and a first-match ordering either way
#: throws away the shape that argument is about. Emitting both costs a second embedding of a
#: 168-token table beside rows that are indexed anyway.
#:
#: NO RETRIEVAL-TIME DEDUPLICATION ACCOMPANIES IT. A table node and its own row nodes are
#: separate documents; a query matching both returns both, and the order they come back in
#: is signal. Suppressing one would be opinionated post-processing of a result set.
BOTH_SHAPES_BASIS = (
    "INGEST_SPEC.md 8.4 says the first matching shape wins; this sheet matched two and got "
    "both. A `table` node and its own `record` nodes are separate documents and a query "
    "matching both returns both — which is the case 8.4's own argument for `small_table` is "
    "strongest in, because the sheets where both shapes match are small ones."
)


class HeaderDoesNotCoverTheRow(ValueError):
    """A row holds a value in a column the stored header has no name for.

    ``profile:sheet`` measured the used width and named every column in it; this read found
    a value further right. The two ran over the same bytes with the same reader, so a
    disagreement means the blob changed underneath the queued task or the two passes do not
    agree about what a used column is. Dropping the value would put a record into the tree
    that is missing a field nobody can see is missing.
    """


class CellDidNotSplit(ValueError):
    """``chunk`` returned nothing, or a piece that still does not fit the window.

    ``EmbeddingService.chunk_to_fit`` enforces the predicate on every piece it returns, so
    reaching this means the chunker and the fit test disagree about the same text. Raising
    here is the whole point of checking: the alternative is a leaf whose ``embed`` fails
    permanently one rung later, which is the condition :func:`plan_record` exists to close.
    """


@dataclass(frozen=True)
class Record:
    """One row, in the three forms 8.4 asks for."""

    row_index: int
    record: dict
    #: ``{header: {"formula": ..., "text_forced": ...}}`` for the cells that carry either.
    #: Absent keys are the normal case; see :class:`~jmfts_core.office.cells.CellNote`.
    cells: dict
    content: str


@dataclass(frozen=True)
class Cell:
    """One column of one row, as the node it becomes.

    ``content`` and ``pieces`` are exclusive and the pair is the rule: a cell whose
    labelled text fits is a LEAF carrying that text, and one that does not is a CONTAINER
    carrying none — its text is its pieces, and it gets a document vector over their
    concatenation from ``summarize`` the same way a ``section`` does.
    """

    key: str
    #: The typed value, kept beside the prose for the reason 8.4 keeps ``record``: a
    #: retrieval hit on this node returns data rather than a string to parse back.
    value: object
    content: Optional[str]
    pieces: tuple[str, ...]


@dataclass(frozen=True)
class RecordPlan:
    """What one row becomes: a leaf, or a container over its columns.

    ``content`` is the row's labelled prose when the row fits, and ``None`` when it does
    not — a container holds no text of its own, which is what keeps the same words out of
    the full-text index twice and what stops the container BM25 pass double-counting its
    own frontier.
    """

    content: Optional[str]
    cells: tuple[Cell, ...]


def build_records(rows: SheetRows, *, header: Sequence[Optional[str]], header_row: int) -> tuple:
    """Every row below the header, as records. Rows that hold nothing produce nothing.

    ``header`` is positional and 1-based by column, exactly as the stored profile's
    ``columns`` array is: ``header[0]`` names column A. A shorter header than the rows are
    wide raises rather than dropping the overhang — see :class:`HeaderDoesNotCoverTheRow`.

    ``header_row`` is the worksheet row number the profile measured the header at, and every
    row AT OR ABOVE it is skipped. It used to be the constant 1 and the skip was ``!=``;
    with the header at row 4, rows 1 to 3 are the banner (``office.sheets.BannerRow``) and
    are not instances of anything. ``<=`` rather than a set membership because the banner is
    contiguous by construction: it is what the scan walked past on its way down.
    """
    if header_row < 1:
        raise ValueError(
            f"the header row is {header_row!r}; `build_records` is called only for a sheet "
            "whose header verdict is true, and such a sheet has a measured row number"
        )
    return tuple(
        record
        for row in rows.rows
        if row.index > header_row
        if (record := _record(row, header)) is not None
    )


def _record(row: RowCells, header: Sequence[Optional[str]]) -> Optional[Record]:
    values: dict = {}
    cells: dict = {}
    for position, value in enumerate(row.values):
        note = row.notes.get(position + 1)
        if value is None and note is None:
            continue
        if position >= len(header) or not header[position]:
            raise HeaderDoesNotCoverTheRow(
                f"row {row.index} holds a value in column {position + 1}, which the stored "
                f"profile's header does not name; the header covers {len(header)} column(s)"
            )
        key = header[position]
        if value is not None:
            values[key] = value
        if note is not None:
            stored = note.as_dict()
            if stored:
                cells[key] = stored

    if not values:
        # Every cell was empty, or held only a formula that evaluated to nothing. There is
        # no instance here, and a node saying so per row is 8.4's own argument against
        # emitting one per empty interior cell.
        return None
    return Record(row_index=row.index, record=values, cells=cells, content=build_content(values))


def build_content(values: dict) -> str:
    """8.4's labelled prose. What gets embedded, so it is sentences and not JSON."""
    return " ".join(cell_content(key, value) for key, value in values.items())


def cell_content(key, value) -> str:
    """One column's share of :func:`build_content`, verbatim.

    The join is over exactly these, so a cell node's text is a substring of the row's and
    the two renderings cannot come to disagree about how a value is spelled. That is the
    property :func:`plan_record` needs: a split row holds the same terms as the row it
    replaced, in the same words, which is what makes the container's BM25 sum over its
    frontier equal to the postings the unsplit row would have had.
    """
    return f"{key}: {_rendered(value)}."


def plan_record(record: Record, *, fits: Callable, chunk: Callable) -> RecordPlan:
    """A row as one node, or as a container over its columns. 8.4 plus the window.

    THE RULE IS THE TREE'S OWN, applied one level down. A ``section`` holds no text and its
    chunks do, because a node too long to embed is a node whose text belongs to smaller
    nodes; ``run_summarize`` then gives the container a document vector over their
    concatenation and no token vectors (``rollup_tasks.store_effective_content``). A row of
    a sheet whose columns hold paragraphs is that shape and was not treated as it: 8.4 wrote
    the whole row into one node, and a node over the token/maxsim window gets no vector at
    all, because ``embed`` refuses to embed a prefix and calls the refusal permanent.

    Splitting by COLUMN and not by character is what the record already knows how to do.
    The keys are measured — ``profile:sheet`` named every column — so each piece can carry
    the label of the column it came from, which a blind chunk of the row's prose could not:
    a chunk boundary inside ``Discussion:`` produces a node that says nothing about which
    field it is a part of.

    ``fits`` and ``chunk`` are the embedding service's, passed in rather than imported.
    This module touches no database, runs no model and reads no settings, which is what
    lets 8.4's wording be tested without a workbook — and a fit test is a property of a
    tokenizer, not of a spreadsheet.

    A cell whose own labelled text is over the window is the second application of the same
    rule: it holds no content and gets pieces. That is not a rare corner — on the reference
    corpus the 66 rows that do not fit became 344 cells, of which 20 do not fit either, in
    20 different rows. A split that stopped at the column boundary would leave 20 of the 66
    exactly where they started.
    """
    if fits(record.content):
        return RecordPlan(content=record.content, cells=())
    return RecordPlan(
        content=None,
        cells=tuple(
            _cell(key, value, fits=fits, chunk=chunk) for key, value in record.record.items()
        ),
    )


def _cell(key, value, *, fits: Callable, chunk: Callable) -> Cell:
    content = cell_content(key, value)
    if fits(content):
        return Cell(key=key, value=value, content=content, pieces=())

    pieces = tuple(chunk(content))
    if not pieces:
        raise CellDidNotSplit(
            f"column {key!r} does not fit the embedding window and the chunker returned no "
            "pieces for it; there is no node that could carry the value"
        )
    oversized = [index for index, piece in enumerate(pieces) if not fits(piece)]
    if oversized:
        raise CellDidNotSplit(
            f"column {key!r} was split into {len(pieces)} piece(s) and {len(oversized)} of "
            f"them still do not fit the embedding window (piece {oversized[0]}); the "
            "chunker and the fit test disagree about the same text"
        )
    return Cell(key=key, value=value, content=None, pieces=pieces)


def _rendered(value) -> str:
    """One value as a person reads it off the sheet.

    ``TRUE``/``FALSE`` rather than Python's ``True``/``False``, matching
    :func:`~jmfts_core.office.sheets.canonical`: the spreadsheet's own rendering is what a
    corpus the embedding model was trained on contains, and the two paths must not spell
    one value two ways.
    """
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return str(value)


def header_labels(columns: Sequence[dict]) -> list:
    """The stored profile's ``columns`` array, as the positional header ``build_records`` wants.

    ``profile:sheet`` writes ``name`` per column and leaves it ``None`` where the sheet has
    no header row. Reading it back rather than re-deriving it is what keeps the profile and
    the records describing the same sheet.

    **ONLY A HEADER-SOURCED NAME IS A KEY.** A column may also be named from a merged banner
    (``office.sheets.NAME_SOURCE_BANNER``), which is a label for the GROUP of columns the
    merge spans — three columns under one banner share one string, and three record keys
    that are the same string are one key holding the last value. So the source is checked
    rather than the name, and a banner-named column reads as unnamed here; a row with a
    value in it then raises `HeaderDoesNotCoverTheRow` rather than writing a record that has
    silently lost two of its fields.
    """
    return [
        column.get("name") if column.get("name_source") == NAME_SOURCE_HEADER else None
        for column in columns
    ]


__all__ = [
    "BOTH_SHAPES_BASIS",
    "NO_HEADER_REASON",
    "NO_TABLE_REASON",
    "SHAPE_BASIS",
    "SHAPE_RECORDS",
    "SHAPE_SMALL_TABLE",
    "TABLE_SHAPE_BASIS",
    "Cell",
    "CellDidNotSplit",
    "HeaderDoesNotCoverTheRow",
    "Record",
    "RecordPlan",
    "build_content",
    "build_records",
    "cell_content",
    "header_labels",
    "plan_record",
]
