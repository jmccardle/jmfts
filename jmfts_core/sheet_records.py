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
sheet whose first row holds a distinct text value in every column has record keys, and a
sheet whose first row does not have none. That is a measured boolean, already computed by
``profile:sheet``, and it decides only whether records are possible — not whether they are
the best representation. The other three shapes are still unbuilt and still waiting on the
corpus.

**Where the keys come from.** The stored profile's column names, not a re-reading of row 1.
``profile:sheet`` decided what the header row was and recorded the label per column; a
second derivation here could disagree with it, and then the profile and the records would
describe different sheets.

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

# The usetype 8.4's `records` shape gives each row node is `USETYPE_RECORD`, and it is
# DEFINED on the model with every other ingest usetype. See `jmfts_core.models.document`.

#: The name of the shape, as 8.4 names it, written onto the sheet node.
SHAPE_RECORDS = "records"

#: The header row's number. 8.3 defines ``header_row`` as a property of row 1 outright, not
#: of "whichever row turned out to look like a header", and
#: :func:`~jmfts_core.office.sheets._header_evidence` is called on row 1. Spelled once so
#: the skip below and the measurement cannot come to disagree.
HEADER_ROW_NUMBER = 1

#: Why this shape was chosen, written onto the sheet node beside it. It replaces
#: :data:`jmfts_core.sheet_profile.SHAPE_DEFERRED_REASON` on a sheet that got records, and
#: it is deliberately explicit that the four-way branch was NOT run — a reader who finds
#: ``shape: "records"`` must not conclude that 8.8's thresholds got set.
SHAPE_BASIS = (
    "INGEST_SPEC.md 8.4's four-way branch was not run: 8.8 leaves its thresholds unset. "
    "`records` was chosen on the one input that is a measured boolean rather than a "
    "threshold — 8.3's `header_row`, which is true here, so the sheet's first row supplies "
    "a key for every column. That decides only that records are POSSIBLE. Whether "
    "`small_table` or `matrix` would suit this sheet better is still what the calibration "
    "corpus is for."
)

#: Why a sheet got no records. Not a failure: a sheet with no header row has no keys, and
#: the shapes that cover it (`matrix`, `unstructured`) are unbuilt.
NO_HEADER_REASON = (
    "INGEST_SPEC.md 8.3's `header_row` is false for this sheet, so its first row does not "
    "supply a key for every column and there is no record to write. The shapes 8.4 gives "
    "such a sheet — `matrix` and `unstructured` — read thresholds 8.8 leaves unset and have "
    "no handler; `header_row_evidence` on this node is what a calibration sweep replays "
    "against."
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


def build_records(rows: SheetRows, *, header: Sequence[Optional[str]]) -> tuple:
    """Every row below the header, as records. Rows that hold nothing produce nothing.

    ``header`` is positional and 1-based by column, exactly as the stored profile's
    ``columns`` array is: ``header[0]`` names column A. A shorter header than the rows are
    wide raises rather than dropping the overhang — see :class:`HeaderDoesNotCoverTheRow`.
    """
    return tuple(
        record
        for row in rows.rows
        if row.index != HEADER_ROW_NUMBER
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
    """
    return [column.get("name") for column in columns]


__all__ = [
    "HEADER_ROW_NUMBER",
    "NO_HEADER_REASON",
    "SHAPE_BASIS",
    "SHAPE_RECORDS",
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
