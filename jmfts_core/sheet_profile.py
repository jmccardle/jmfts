"""A measured sheet, written down. ``docs/INGEST_SPEC.md`` 8.5 and 8.7.

Two renderings of one :class:`~jmfts_core.office.sheets.SheetMeasurement`, and no third
measurement:

* :func:`sheet_evidence_block` — 8.7's block on the sheet node, which is what a
  threshold sweep reads;
* :func:`build_profile_content` — 8.5's prose, which is what a person and an embedding
  model read.

**No shape is chosen here.** 8.8 leaves every threshold in 8.4 unset pending calibration
against real workbooks, so the sheet node records the branch INPUTS and states that the
branch was not taken (:data:`SHAPE_DEFERRED_REASON`). ``shape`` and ``shape_margin``, the
two keys 8.7 names, are left absent rather than filled with a guess — an absent key is a
question; a key holding a number nobody calibrated is an answer that looks settled.

**Only factoids that come from counting.** 8.5 is explicit: no naming what a column means,
no relationship between sheets, no classification of the sheet's purpose. Every sentence
below is a count, a ratio or a list of values that were read. Where 8.5 asks for a
judgement this module refuses to make one and states the number instead — a "sparse
column" is `filled in 14% of rows`, because "well below the sheet's" is a threshold and
thresholds are 8.8's, not this pass's.

This module imports no reader, touches no database and runs no model. It is a pure
function of the measurement, which is what lets the whole of 8.5's wording be tested
without a workbook.
"""

from __future__ import annotations

from typing import Callable, Optional

from jmfts_core.office.sheets import SheetMeasurement, TYPE_EMPTY

# The usetype 8.5 gives the profile node is `USETYPE_SUMMARY`, and it is DEFINED on the
# model with every other ingest usetype — Part 4's rule table names it and cannot import
# this module. See `jmfts_core.models.document`.

#: Why the sheet node carries no ``shape``. Written into the node so a reader finds the
#: reason where the key would have been, rather than having to know which sprint step
#: they are looking at.
SHAPE_DEFERRED_REASON = (
    "INGEST_SPEC.md 8.8 leaves every threshold in 8.4 unset pending calibration against "
    "real workbooks, so no shape was chosen. `inputs` below holds the measured values a "
    "shape decision consumes, so the calibration is a query over stored profiles rather "
    "than a re-ingest (SPRINT_0_3_0.md 6.2)."
)


def sheet_evidence_block(
    measurement: SheetMeasurement,
    *,
    rendered_tokens: Optional[int],
    token_window: int,
    doc_window: int,
    embedding_model: str,
    render_cell_budget: int,
) -> dict:
    """8.7's ``sheet`` block, minus the two keys that would be a decision.

    ``rendered_tokens`` is ``None`` when the sheet was too large to render (see
    :func:`~jmfts_core.office.sheets.measure_sheet`); the reason travels with it, and both
    windows are recorded so that a sweep can see WHICH window a fit was judged against.
    8.4 says "the embedding window" and this appliance has two — 512 on the token/MaxSim
    path and 8192 on the document-vector path — so which one ``small_table`` means is
    itself part of what step 7 decides.
    """
    fits_token = None if rendered_tokens is None else rendered_tokens <= token_window
    fits_doc = None if rendered_tokens is None else rendered_tokens <= doc_window
    interior_cells = measurement.interior_rows * measurement.interior_cols
    measurements = {
        # `INGEST_SPEC.md` 8.3's table, spelled as 8.3 spells it.
        "rows": measurement.rows,
        "cols": measurement.cols,
        "fill_ratio": measurement.fill_ratio,
        "header_row": measurement.header_row.verdict,
        "header_col": measurement.header_col.verdict,
        "interior_cardinality": measurement.interior_cardinality,
        "merged_cells": measurement.merged_cells,
        "rendered_tokens": rendered_tokens,
        # The denominators. 8.3 stores ratios and cardinalities without the counts they
        # were taken over, and a sweep cannot redefine a ratio it cannot see the parts of:
        # an interior cardinality of 3 means one thing in a 2x2 interior and another in a
        # 40,000x50 one, and 8.4's `matrix` rule ("interior_cardinality is small") is not
        # decidable from the cardinality alone.
        "cells": measurement.rows * measurement.cols,
        "non_empty_cells": measurement.non_empty_cells,
        "interior_rows": measurement.interior_rows,
        "interior_cols": measurement.interior_cols,
        "interior_cells": interior_cells,
        "interior_non_empty": measurement.interior_non_empty,
        "interior_fill_ratio": (
            measurement.interior_non_empty / interior_cells if interior_cells else 0.0
        ),
        # What the file DECLARED in `<dimension>`, beside what its cells actually occupy.
        # The element is optional and writers get it wrong; a disagreement is a fact about
        # the file rather than an error, and it is only visible if both are kept.
        "declared_rows": measurement.declared_rows,
        "declared_cols": measurement.declared_cols,
        # Whether a measurement is exact or a floor. A null with a floor beside it is a
        # measurement; a number that silently stopped counting is not.
        "interior_cardinality_exact": measurement.interior_cardinality_exact,
        "interior_cardinality_at_least": measurement.interior_cardinality_at_least,
        "rendered_tokens_exact": rendered_tokens is not None,
        "rendered_unbounded_reason": measurement.rendered_unbounded_reason,
        "rendered_fits_token_window": fits_token,
        "rendered_fits_doc_window": fits_doc,
        # The components of each header verdict. See `HeaderEvidence` for why: 8.3's rule
        # as written cannot see the crossing table 8.4's `matrix` shape is for.
        "header_row_evidence": _evidence(measurement.header_row),
        "header_col_evidence": _evidence(measurement.header_col),
        # What produced a token count, so two profiles taken under different models are not
        # compared as though they were one measurement.
        "embedding": {
            "model": embedding_model,
            "token_window": token_window,
            "doc_window": doc_window,
        },
        # The resource bounds this profile ran under. A sweep that wants a closed-set
        # threshold above `values_retained_max`, or an exact count above
        # `distinct_tracked_max`, has to re-profile — and this is where it finds that out.
        "limits": {
            "distinct_tracked_max": measurement.distinct_tracked_max,
            "values_retained_max": measurement.values_retained_max,
            "render_cell_budget": render_cell_budget,
        },
    }
    return {
        "measurements": measurements,
        "columns": [_column(column) for column in measurement.columns],
        "shape_decision": {
            "decided": False,
            "reason": SHAPE_DEFERRED_REASON,
            "spec": "INGEST_SPEC.md 8.4, 8.8",
            # Exactly the values 8.4's four branches read, gathered in one place so a
            # sweep is one path into the JSONB rather than a join across this block.
            "inputs": {
                "rendered_tokens": rendered_tokens,
                "rendered_fits_token_window": fits_token,
                "rendered_fits_doc_window": fits_doc,
                "header_row": measurement.header_row.verdict,
                "header_col": measurement.header_col.verdict,
                "fill_ratio": measurement.fill_ratio,
                "interior_cardinality": measurement.interior_cardinality,
                "interior_cells": interior_cells,
                "interior_fill_ratio": (
                    measurement.interior_non_empty / interior_cells if interior_cells else 0.0
                ),
            },
        },
    }


def _evidence(evidence) -> dict:
    return {
        "verdict": evidence.verdict,
        "cells": evidence.cells,
        "non_empty": evidence.non_empty,
        "text_cells": evidence.text_cells,
        "numeric_cells": evidence.numeric_cells,
        "other_cells": evidence.other_cells,
        "distinct": evidence.distinct,
        "all_non_empty": evidence.all_non_empty,
        "all_text": evidence.all_text,
        "all_distinct": evidence.all_distinct,
        "leading_empty": evidence.leading_empty,
    }


def _column(column) -> dict:
    return {
        "index": column.index,
        "letter": column.letter,
        "name": column.name,
        "dominant_type": column.dominant_type,
        "type_counts": column.type_counts,
        "non_empty": column.non_empty,
        "body_rows": column.body_rows,
        "fill_ratio": column.fill_ratio,
        "distinct_count": column.distinct_count,
        "distinct_at_least": column.distinct_at_least,
        "distinct_exact": column.distinct_exact,
        "is_unique": column.is_unique,
        # 8.5: values for a closed-set column, a count for a high-cardinality one. WHICH IS
        # WHICH is 8.8's unset threshold, so what governs here is only whether the set was
        # small enough to retain at all (`values_retained_max`) — which is a storage
        # ceiling, and leaves every threshold below it available to a later sweep.
        "values": column.values,
        "sketch": column.sketch,
    }


# ---------------------------------------------------------------------------
# 8.5's prose
# ---------------------------------------------------------------------------


def build_profile_content(
    measurement: SheetMeasurement,
    *,
    fits: Callable[[str], bool],
) -> tuple:
    """``(content, record)`` — the profile node's text and an account of what it left out.

    ``fits`` is the embedding window predicate
    (:meth:`~jmfts_core.embedding.EmbeddingService.fits_token_window`). The profile node is
    embedded like any other node, so its text has a hard ceiling, and a wide sheet's full
    prose does not fit it. What gets dropped is chosen in one order and recorded:

    1. the listed VALUES of the columns that have most of them — the count survives, which
       is 8.5's own treatment of a high-cardinality column;
    2. whole column sentences from the end, with a closing sentence saying how many were
       left out and where they still are.

    Both searches are binary, because both renderings shrink monotonically as more is
    dropped; a wide sheet therefore costs a logarithmic number of tokenizer calls rather
    than one per column.

    **Dropping a value list is not the closed-set threshold.** It is a fact about this
    node's length. The values are on the sheet node either way
    (:func:`sheet_evidence_block`), and 8.6 reads them from there.
    """
    sentences = _sheet_sentences(measurement)
    columns = list(measurement.columns)
    listing_order = sorted(
        range(len(columns)),
        key=lambda i: (len(columns[i].values or ()), i),
        reverse=True,
    )

    def render(drop_listings: int, keep_columns: int) -> str:
        suppressed = set(listing_order[:drop_listings])
        parts = list(sentences)
        for position, column in enumerate(columns[:keep_columns]):
            parts.append(_column_sentence(column, list_values=position not in suppressed))
        omitted = len(columns) - keep_columns
        if omitted > 0:
            verb = "is" if omitted == 1 else "are"
            parts.append(
                f'{_plural(omitted, "further column")} {verb} measured in this node\'s '
                "structured content and not described here."
            )
        return " ".join(parts)

    total = len(columns)
    dropped = _least_fitting(lambda d: fits(render(d, total)), total)
    if dropped is not None:
        return render(dropped, total), _record(
            measurement, listed=total - dropped, described=total, truncated=dropped > 0
        )

    kept = _most_fitting(lambda k: fits(render(total, k)), total)
    if kept is None:
        raise ValueError(
            f"the profile of sheet {measurement.name!r} does not fit the embedding window "
            "even with every column left out; its opening sentences alone are "
            f"{len(render(total, 0))} characters, which means the sheet's own name is the "
            "thing that does not fit"
        )
    return render(total, kept), _record(measurement, listed=0, described=kept, truncated=True)


def _record(measurement: SheetMeasurement, *, listed: int, described: int, truncated: bool) -> dict:
    """What the prose left out, so a reader is never guessing whether it is complete."""
    return {
        "columns": len(measurement.columns),
        "columns_described": described,
        "columns_with_values_listed": listed,
        "truncated": truncated,
        "reason": (
            "the profile node is embedded like any other node, so its text is bounded by "
            "the embedding window; every measurement is on the sheet node regardless"
            if truncated
            else None
        ),
    }


def _least_fitting(predicate: Callable[[int], bool], upper: int) -> Optional[int]:
    """Smallest ``d`` in ``0..upper`` with ``predicate(d)``, or ``None`` if there is none.

    ``predicate`` is monotone: dropping more can only shorten the text.
    """
    if predicate(0):
        return 0
    if not predicate(upper):
        return None
    low, high = 0, upper
    while high - low > 1:
        middle = (low + high) // 2
        if predicate(middle):
            high = middle
        else:
            low = middle
    return high


def _most_fitting(predicate: Callable[[int], bool], upper: int) -> Optional[int]:
    """Largest ``k`` in ``0..upper`` with ``predicate(k)``, or ``None`` if not even 0."""
    if predicate(upper):
        return upper
    if not predicate(0):
        return None
    low, high = 0, upper
    while high - low > 1:
        middle = (low + high) // 2
        if predicate(middle):
            low = middle
        else:
            high = middle
    return low


def _plural(count: int, singular: str, plural: Optional[str] = None) -> str:
    """``"1 row"`` / ``"5 rows"``. Grammar, not a measurement.

    The profile node is prose that gets embedded, and "has 1 distinct values" is a phrase
    no corpus the model was trained on contains.
    """
    word = singular if count == 1 else (plural or f"{singular}s")
    return f"{count:,} {word}"


def _sheet_sentences(measurement: SheetMeasurement) -> list:
    """What a person notices before reading a row. All of it counted."""
    name = measurement.name
    if not measurement.rows or not measurement.cols:
        return [f'Sheet "{name}" holds no value in any cell.']

    cells = measurement.rows * measurement.cols
    sentences = [
        f'Sheet "{name}" has {_plural(measurement.rows, "row")} and '
        f'{_plural(measurement.cols, "column")}.',
        f"{measurement.non_empty_cells:,} of its {cells:,} cells hold a value "
        f"({measurement.fill_ratio:.0%}).",
    ]
    if measurement.merged_cells:
        sentences.append(f'It declares {_plural(measurement.merged_cells, "merged cell range")}.')
    if measurement.header_row.verdict:
        sentences.append("Its first row holds a distinct text value in every column.")
    if measurement.header_col.verdict:
        sentences.append(
            "Its first column holds a distinct text value in every row below the first."
        )
    if measurement.interior_rows and measurement.interior_cols:
        where = (
            "Below and to the right of its headers"
            if measurement.header_row.verdict and measurement.header_col.verdict
            else "Below its header row" if measurement.header_row.verdict else "Across the sheet"
        )
        found = (
            measurement.interior_cardinality
            if measurement.interior_cardinality_exact
            else measurement.interior_cardinality_at_least
        )
        floor = "" if measurement.interior_cardinality_exact else "at least "
        verb = "appears" if found == 1 else "appear"
        count = f'{floor}{_plural(found, "distinct value")} {verb}'
        sentences.append(
            f'{where}, {count} across {_plural(measurement.interior_non_empty, "filled cell")}.'
        )
    return sentences


def _column_sentence(column, *, list_values: bool) -> str:
    """One column, as 8.5's table of permitted factoids allows and no further."""
    label = f'Column "{column.name}"' if column.name else f"Column {column.letter}"
    if column.dominant_type == TYPE_EMPTY:
        return f"{label} holds no value in any row."

    clauses = [f"{label} holds {column.dominant_type}"]
    if column.is_unique:
        clauses.append("and has a distinct value in every row, so it identifies a row")
    elif not column.distinct_exact:
        clauses.append(f'and has at least {_plural(column.distinct_at_least, "distinct value")}')
    elif list_values and column.values is not None:
        listed = ", ".join(column.values)
        clauses.append(f'and has {_plural(column.distinct_count, "distinct value")}: {listed}')
    else:
        clauses.append(f'and has {_plural(column.distinct_count, "distinct value")}')
    sentence = " ".join(clauses) + "."
    # 8.5 asks for a "sparse column" factoid whose rule is "fill_ratio well below the
    # sheet's". "Well below" is a threshold and 8.8 has not set it, so the ratio is stated
    # instead of classified. A reader gets the same fact and nothing was invented.
    if column.body_rows and column.fill_ratio < 1.0:
        sentence += f" It is filled in {column.fill_ratio:.0%} of rows."
    return sentence


__all__ = [
    "SHAPE_DEFERRED_REASON",
    "build_profile_content",
    "sheet_evidence_block",
]
