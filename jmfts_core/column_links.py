"""Columns whose value sets are nested, found by containment. ``INGEST_SPEC.md`` 8.6.

8.6 asks a workbook-level task to find "two columns whose distinct value sets are equal, or
where one is a subset of the other", and specifies it as a pairwise comparison of the
``values`` ``profile:sheet`` stored. ``SPRINT_0_3_0.md`` 6.4 replaces the mechanism and
leaves the contract alone, for two separate reasons that are easy to run together:

* **Quadratic.** A forty-sheet workbook of twenty columns each is eight hundred columns and
  three hundred thousand pairs, and a workbook is not the unit anybody stops at.
* **Blind where it matters.** 8.5 retains ``values`` only while the distinct set is small
  (``VALUES_RETAINED_MAX``), so the comparison it specifies cannot see a
  high-cardinality column at all — and a transaction log's key column is exactly the
  column a lookup table's key is nested inside.

So the search is ``datasketch.MinHashLSHEnsemble`` over the per-column sketches 6.3 added
(:mod:`jmfts_core.sketch`), and the LSH pass is **recall, not the answer**. Three passes over
one workbook, each narrower than the last:

1. the ensemble proposes directed pairs, weighted hard towards recall
   (:data:`LSH_RECALL_WEIGHTS`) because its banding reads only part of each sketch;
2. every survivor is measured — exactly, in SQL, where 8.5 stored both value sets, and by
   the full sketches where it did not — and the ones that do not clear the threshold are
   dropped (:func:`passes_threshold`);
3. what remains is collapsed, so a pair nested in both directions is one candidate and not
   two (:func:`collapse_mutual`).

**What it cannot find** is stated with the numbers in :data:`SKETCH_RESOLUTION` and written
into every workbook: a 128-permutation MinHash stops resolving a containment when the two
sets are orders of magnitude apart in size, and it misses quietly.

**Containment, not Jaccard, and the asymmetry is the whole point.** Twelve region codes
inside a forty-thousand-row transaction log have a Jaccard of about 0.0003 and a containment
of 1.0. A Jaccard index would rank that pair below every coincidence in the workbook.
``MinHashLSHEnsemble`` indexes each set with its size and answers "which indexed sets contain
most of this one", which is the question an inclusion dependency actually is.

**Column names are a tie-break and never a reason.** ``name_similarity`` is recorded on every
candidate and used only to order them for a reviewer. Two columns called ``Region`` that
share no value are not proposed here, and two columns called ``Code`` and ``Ledger Key``
whose values nest are proposed with equal standing. Nothing in this module reads a name
before it has read a set.

**The machine proposes, a reviewer decides.** That is 8.6's contract and 6.4 does not touch
it. What this module produces is a candidate with the counts it was found by, and a review
payload in Part 7.6's shape; it never records "this is a foreign key". The one thing it does
assert is arithmetic: 6.4's observation that a high-containment pair whose CONTAINING column
is unique is simultaneously a foreign-key candidate, a resource-valued-predicate candidate
(6.5) and an inverse-functional-property candidate (7.2, and ``owl:InverseFunctionalProperty``
per 5.4) — one measurement, three uses, so all three are recorded rather than one chosen.

**Every threshold here is uncalibrated.** ``INGEST_SPEC.md`` 8.8 leaves the worksheet
thresholds unset pending a corpus of real workbooks, and that corpus has not been collected.
The numbers below are named, configurable, defaulted to the library's own default where
there is one, and stated as guesses in :data:`THRESHOLD_UNCALIBRATED` — which is written into
every workbook this runs over, so a reader finds it beside the numbers rather than in a
sprint document.

This module holds no session and opens no workbook. The exact verification needs the
database and the enqueueing needs the queue, so both live with the handler in
:mod:`jmfts_core.sheet_tasks`; what is here is a pure function of the stored profiles, which
is what lets 8.6's rules be tested without either.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, replace
from typing import Iterable, Optional, Sequence

from jmfts_core.sketch import SKETCH_MINHASH, load_sketch, require_datasketch

#: How much of a column's value set has to be inside another column's before the pair is
#: worth verifying. ``datasketch.MinHashLSHEnsemble``'s own default, kept rather than
#: chosen — the same move :data:`jmfts_core.sketch.MINHASH_PERMUTATIONS` makes, and for the
#: same reason: a number this appliance invented would look calibrated, and this one is at
#: least the number the library's published behaviour is quoted against.
#:
#: **It is a RECALL knob and it is uncalibrated** (:data:`THRESHOLD_UNCALIBRATED`). Lower it
#: and more pairs reach verification, which costs SQL and reviewer attention; raise it and a
#: real inclusion dependency with a few unmatched values in the contained column stops being
#: proposed at all. Which way real workbooks want it moved is what step 7's corpus is for.
DEFAULT_CONTAINMENT_THRESHOLD = 0.9

#: How many size partitions the ensemble keeps. ``datasketch``'s default. A COST, not a
#: threshold: LSH Ensemble partitions the indexed sets by size and tunes each partition
#: separately, so this decides how much index is built and how sharply the containment
#: estimate behaves across a wide range of set sizes — and nothing about it decides whether
#: two columns are related.
DEFAULT_LSH_PARTITIONS = 16

#: ``(false_positive_weight, false_negative_weight)`` for the ensemble's banding, and it is
#: NOT datasketch's default — this is the one library setting this module deliberately
#: overrides, because the default makes 6.4's own example undetectable.
#:
#: MEASURED, 2026-08-23, datasketch 2.0.0, ``num_perm=128``, ``num_part=16``, threshold 0.9,
#: twenty trials per cell, a query set fully contained in the indexed set, against an index
#: of forty unrelated sets of sizes 10–8,000. Recall of the true containment, and the mean
#: number of unrelated keys returned with it:
#:
#: ==============  ==========  ===========  ============  ============
#: weights         12 in 24    12 in 200    12 in 5,000   200 in 40,000
#: ==============  ==========  ===========  ============  ============
#: (0.5, 0.5)      0.45        0.00         0.00          0.00
#: (0.2, 0.8)      1.00        0.15         0.00          0.00
#: **(0.1, 0.9)**  **1.00**    **1.00**     **0.40**      **0.35**
#: (0.05, 0.95)    1.00        1.00         0.40          0.35
#: (0.01, 0.99)    1.00        1.00         0.40          0.35
#: ==============  ==========  ===========  ============  ============
#:
#: Not one unrelated key was returned at any setting. datasketch's own default minimises
#: false positives and false negatives together, integrated over every containment in
#: ``[0, 1]``; at a large size ratio the false-positive term dominates and it picks a
#: banding with no recall at all. **That is the wrong objective for this pipeline** — the LSH
#: pass is recall by construction and precision is the SQL pass's job
#: (:func:`passes_threshold`) — so the weight moves.
#:
#: ``(0.1, 0.9)`` rather than ``(0.01, 0.99)`` because the three bottom rows are the same
#: banding: any false-negative weight at or above 0.9 puts the optimiser on the same
#: boundary. It is the edge of a plateau, not a tuned point, which is why it is a constant
#: here and not a task parameter.
LSH_RECALL_WEIGHTS: tuple[float, float] = (0.1, 0.9)

#: What the search still cannot find, stated in the workbook's own record. See
#: :data:`LSH_RECALL_WEIGHTS`' last two columns: a MinHash of ``num_perm`` permutations can
#: only see a shared value at all when the contained set is a large enough fraction of the
#: containing one — twelve values inside five thousand shares a minimum in about a quarter
#: of the permutation draws — so recall degrades at the extreme ratios rather than failing
#: loudly. Raising :data:`jmfts_core.sketch.MINHASH_PERMUTATIONS` is the lever, and it is not
#: free: it changes every sketch's size and makes new sketches incomparable with stored ones.
SKETCH_RESOLUTION = (
    "A MinHash containment search degrades where the two sets are very different sizes: at "
    "128 permutations a twelve-value column inside a five-thousand-value column is found "
    "about 40% of the time, and a match is missed silently rather than reported as "
    "uncertain. Measured 2026-08-23 against datasketch 2.0.0; see "
    "jmfts_core.column_links.LSH_RECALL_WEIGHTS. Raising "
    "jmfts_core.sketch.MINHASH_PERMUTATIONS is the lever, and it makes new sketches "
    "incomparable with every stored one."
)

#: How many distinct values a column needs before it takes part. ``1`` — which is to say, no
#: filter at all: a column with one value is contained in every column that holds that value,
#: and saying so is true. The knob exists because it is the obvious lever for a corpus that
#: finds the proposals dominated by two-value flag columns, and a default above 1 today would
#: be exactly the invented threshold 8.8 refuses.
DEFAULT_MIN_DISTINCT_VALUES = 1

#: The ceiling on how many candidates one workbook may produce, and it FAILS the task rather
#: than truncating it. ``SPRINT_0_3_0.md`` 6.6 states the rule for the fan-out of
#: ``extract:sheet`` and it is the same rule: "a named limit that fails the task, not a
#: silent truncation". The fan-out here is not nodes, it is review tasks and one JSONB
#: block — eight hundred columns whose values all nest is three hundred thousand pairs, and
#: a workbook that silently wrote them all would be a workbook nobody could review and a
#: profile nobody could read.
#:
#: The number is a ceiling, not a measurement: it is round on purpose, so it does not read as
#: something that was counted. Raising it is a task parameter.
DEFAULT_MAX_CANDIDATES = 1_000

#: Said in the workbook's own record, beside the numbers it was produced with.
THRESHOLD_UNCALIBRATED = (
    "Every threshold this search reads is uncalibrated. INGEST_SPEC.md 8.8 leaves the "
    "worksheet thresholds unset pending a set of real workbooks, and that corpus has not "
    "been collected, so `containment_threshold` and `min_distinct_values` are named, "
    "configurable defaults rather than measured values: the threshold is datasketch's own "
    "default and the minimum is no filter at all. The exact counts on each candidate are "
    "what a calibration sweep replays against, so moving either number is a query over "
    "stored profiles rather than a re-ingest (SPRINT_0_3_0.md 6.2)."
)

#: How the candidates were found, written into the record. A stored profile outlives the
#: release that wrote it, and "these pairs came from an LSH ensemble over 128-permutation
#: MinHashes" is not recoverable from the pairs.
METHOD_LSH_ENSEMBLE = "minhash_lsh_ensemble"

#: Exact verification ran, in SQL, over both columns' stored ``values``.
VERIFIED_SQL_EXACT = "sql_exact"

#: It did not, and the candidate carries an estimate only. See :func:`unverifiable_reason`.
VERIFIED_NONE = "unverified"

#: 6.4's three readings of one high-containment pair whose container is unique. Recorded
#: together because they are one measurement — which one a reviewer wants is a decision
#: about meaning, and 8.6 gives that to the reviewer.
IMPLICATION_FOREIGN_KEY = "foreign_key"
IMPLICATION_RESOURCE_VALUED_PREDICATE = "resource_valued_predicate"
IMPLICATION_INVERSE_FUNCTIONAL_PROPERTY = "inverse_functional_property"
UNIQUE_CONTAINER_IMPLICATIONS: tuple[str, ...] = (
    IMPLICATION_FOREIGN_KEY,
    IMPLICATION_RESOURCE_VALUED_PREDICATE,
    IMPLICATION_INVERSE_FUNCTIONAL_PROPERTY,
)


@dataclass(frozen=True)
class ColumnRef:
    """One column of one profiled sheet, as 8.6's search sees it.

    ``position`` is the column's index in the stored ``sheet.columns`` ARRAY and
    ``index`` is its 1-based position in the worksheet. They are usually ``position + 1 ==
    index`` and they are not the same fact: the array is what a JSONB path addresses, and
    the exact verification query addresses one by path. Carrying both means the SQL never
    has to assume the profile wrote a dense array.
    """

    node_id: int
    sheet_name: str
    sheet_index: Optional[int]
    position: int
    index: int
    letter: str
    name: Optional[str]
    distinct_count: Optional[int]
    is_unique: Optional[bool]
    body_rows: int
    has_values: bool
    sketch: dict

    @property
    def label(self) -> str:
        """What a person calls this column. The header if it has one, the letter if not."""
        return self.name or f"column {self.letter}"

    @property
    def sketch_size(self) -> int:
        """How many distinct values went into the sketch. LSH Ensemble needs it.

        Not ``distinct_count``: that is ``None`` for a column whose tracking stopped, and
        the sketch's own ``count`` is the size of the set the sketch was actually built
        from — which is the prefix, and is the set a containment estimate from it is about.
        """
        return int(self.sketch["count"])

    @property
    def partial(self) -> bool:
        """The sketch is of a tracked PREFIX, not of the whole column.

        ``jmfts_core.sketch.SketchBuilder.finish`` records it, and it travels onto every
        candidate: a containment estimate taken from a prefix is a statement about the
        prefix, and a reviewer told otherwise would be told something nobody measured.
        """
        return bool(self.sketch.get("partial"))

    @property
    def key(self) -> tuple:
        """A stable identity, and the sort key everything here falls back on."""
        return (self.node_id, self.position)


def columns_of(node_id: int, sheet_block: dict) -> list[ColumnRef]:
    """The sketched columns of one profiled sheet node, in array order.

    Columns with no sketch are dropped HERE and counted by the caller
    (:func:`census`), rather than carried as a ``None``: a column that was never sketched
    cannot take part in a containment search under any threshold, and the useful thing to
    do with it is say how many there were and why.
    """
    name = sheet_block.get("name") or ""
    sheet_index = sheet_block.get("index")
    refs: list[ColumnRef] = []
    for position, column in enumerate(sheet_block.get("columns") or ()):
        sketch = column.get("sketch")
        if not sketch:
            continue
        refs.append(
            ColumnRef(
                node_id=node_id,
                sheet_name=name,
                sheet_index=sheet_index,
                position=position,
                index=int(column.get("index", position + 1)),
                letter=column.get("letter") or "",
                name=column.get("name"),
                distinct_count=column.get("distinct_count"),
                is_unique=column.get("is_unique"),
                body_rows=int(column.get("body_rows") or 0),
                has_values=column.get("values") is not None,
                sketch=sketch,
            )
        )
    return refs


def census(sheet_blocks: Iterable[dict], sketched: Sequence[ColumnRef], eligible: int) -> dict:
    """How many columns there were, how many carried a sketch, how many took part.

    Written into the workbook's record because "no candidates" has three completely
    different causes — nothing was sketched, everything was sketched and nothing nests, or
    the minimum excluded everything — and a bare zero cannot tell them apart.
    """
    total = sum(len(block.get("columns") or ()) for block in sheet_blocks)
    return {
        "columns": total,
        "columns_sketched": len(sketched),
        "columns_searched": eligible,
        "columns_without_a_sketch": total - len(sketched),
        "columns_with_a_partial_sketch": sum(1 for column in sketched if column.partial),
    }


class IncomparableSketches(ValueError):
    """Two sketches in one workbook were built by different permutations.

    A MinHash is comparable only with another built from the same ``num_perm``, ``seed`` and
    permutation scheme — :mod:`jmfts_core.sketch` carries all three with the stored sketch
    for exactly this moment. Comparing across them produces a number, which is the class of
    quiet wrong answer the project's Fail Early rule exists to stop, so it raises instead.

    In practice this means one sheet was re-profiled by a different release. The fix is to
    re-profile the rest, and the message says which recipes were found on which sheets.
    """


def recipe_of(columns: Sequence[ColumnRef]) -> dict:
    """The one permutation recipe every sketch in this workbook agrees on, or raise.

    Read off the SKETCHES rather than from configuration. Configuration says what the next
    sketch would be built with; the stored recipe says what these were, and they are the
    same number only until somebody changes a setting.
    """
    recipes: dict[tuple, list[str]] = {}
    for column in columns:
        kind = column.sketch.get("kind")
        if kind != SKETCH_MINHASH:
            raise IncomparableSketches(
                f"column {column.label!r} of sheet {column.sheet_name!r} carries a sketch "
                f"of kind {kind!r}, which this appliance cannot read; INGEST_SPEC.md 8.6's "
                "containment search has no way to compare it with a MinHash, and treating "
                "the stored array as one would compare two different algorithms' output"
            )
        recipe = (
            column.sketch.get("num_perm"),
            column.sketch.get("seed"),
            column.sketch.get("scheme"),
        )
        recipes.setdefault(recipe, []).append(f"{column.sheet_name}.{column.label}")
    if len(recipes) > 1:
        described = "; ".join(
            f"num_perm={recipe[0]} seed={recipe[1]} scheme={recipe[2]!r} on " f"{sorted(where)[:4]}"
            for recipe, where in sorted(recipes.items(), key=lambda item: str(item[0]))
        )
        raise IncomparableSketches(
            "this workbook's columns were sketched under more than one permutation recipe, "
            "and MinHashes built from different permutations cannot be compared: "
            f"{described}. Re-run profile:sheet over the sheets that disagree"
        )
    recipe = next(iter(recipes))
    return {"num_perm": recipe[0], "seed": recipe[1], "scheme": recipe[2]}


@dataclass(frozen=True)
class Containment:
    """One directed pair the LSH pass proposes: ``contained``'s values sit inside ``container``'s.

    ``estimate`` is from the sketches and nothing else. It is named an estimate everywhere it
    appears because a 128-permutation MinHash carries a standard error of about 0.09 on the
    Jaccard it is derived from, and a number that arrived by that route must not be presented
    beside one that was counted.
    """

    contained: ColumnRef
    container: ColumnRef
    estimate: float
    #: Both directions were proposed, so the two value sets are (approximately) equal —
    #: 8.6's "equal" case as distinct from its "subset" case. One candidate, not two: the
    #: relationship is one relationship, and two review tasks asking about it from opposite
    #: ends is the same question twice.
    mutual: bool = False


def find_containments(
    columns: Sequence[ColumnRef],
    *,
    threshold: float = DEFAULT_CONTAINMENT_THRESHOLD,
    partitions: int = DEFAULT_LSH_PARTITIONS,
) -> list[Containment]:
    """Every cross-sheet pair the ensemble proposes. **Directed, unfiltered, and not final.**

    This is pass 1 of three: what comes back is a recall set, it contains pairs that are not
    inclusion dependencies, and it routinely contains BOTH directions over a pair that nests
    only one way. :func:`passes_threshold` and :func:`collapse_mutual` are passes 2 and 3 and
    the caller runs them after the exact verification, which is the only place a count exists.

    **Cross-sheet only**, because 8.6 is titled "Cross-sheet closed sets" and specifies a
    workbook-level task over per-sheet profiles. A column nested inside another column of
    the SAME sheet is a real inclusion dependency and it is not what 8.6 asks for; proposing
    it here would widen the reviewer's queue with pairs nothing in the spec asked about.

    ``MinHashLSHEnsemble.query(minhash, size)`` answers "which indexed sets contain at least
    ``threshold`` of THIS one", so every column is indexed and every column is a query, and a
    hit means the querying column is the contained one. That direction is the whole reason
    this is an ensemble and not a plain :class:`MinHashLSH`.
    """
    datasketch = require_datasketch()
    recipe = recipe_of(columns)
    minhashes = {}
    for column in columns:
        loaded = load_sketch(column.sketch)
        if loaded is None:
            # `load_sketch` returns None for a kind it does not know, and `recipe_of` has
            # already raised on that. Reaching here means the two disagree.
            raise IncomparableSketches(
                f"column {column.label!r} of sheet {column.sheet_name!r} passed the sketch "
                "recipe check and then failed to load; jmfts_core.sketch.load_sketch and "
                "column_links.recipe_of disagree about what a readable sketch is"
            )
        minhashes[column.key] = loaded

    ensemble = datasketch.MinHashLSHEnsemble(
        threshold=threshold,
        num_perm=recipe["num_perm"],
        num_part=partitions,
        weights=LSH_RECALL_WEIGHTS,
    )
    ensemble.index((column.key, minhashes[column.key], column.sketch_size) for column in columns)

    by_key = {column.key: column for column in columns}
    found: list[Containment] = []
    for contained in columns:
        for key in ensemble.query(minhashes[contained.key], contained.sketch_size):
            container = by_key[key]
            if container.key == contained.key or container.node_id == contained.node_id:
                continue
            found.append(
                Containment(
                    contained=contained,
                    container=container,
                    estimate=_containment_estimate(
                        minhashes[contained.key],
                        minhashes[container.key],
                        contained.sketch_size,
                        container.sketch_size,
                    ),
                )
            )
    found.sort(key=lambda item: (item.contained.key, item.container.key))
    return found


def _containment_estimate(left, right, left_size: int, right_size: int) -> float:
    """``|left ∩ right| / |left|``, from the Jaccard the two sketches estimate.

    ``datasketch`` reports Jaccard, and containment is Jaccard rescaled by the two set
    sizes: with ``J = I/(a+b-I)`` the intersection is ``I = J(a+b)/(1+J)``, so the
    containment of the left set is ``I/a``. The sizes are exact — they were counted during
    the profile — so the only estimated quantity in the expression is ``J``.
    """
    jaccard = float(left.jaccard(right))
    if jaccard <= 0.0 or not left_size:
        return 0.0
    intersection = jaccard * (left_size + right_size) / (1.0 + jaccard)
    return min(1.0, intersection / left_size)


def passes_threshold(
    containment: Containment, verification: "Verification", threshold: float
) -> bool:
    """Does this pair still hold, on the best measurement there is of it?

    **The exact count wins wherever there is one.** The LSH pass is deliberately weighted
    towards recall (:data:`LSH_RECALL_WEIGHTS`), so it returns pairs that are not
    inclusion dependencies at all; rejecting them is what "then exact verification in SQL on
    the survivors" (6.4) is FOR, and a survivor that fails verification is not a candidate.

    Where 8.5 kept no values to count against, the full-sketch estimate decides instead. It
    is a weaker test than the count and a stronger one than the ensemble's banding — which
    reads only part of each sketch — so the pair is judged on the best measurement available
    to it, and the candidate records which one that was.
    """
    if verification.method == VERIFIED_SQL_EXACT:
        return (verification.containment or 0.0) >= threshold
    return containment.estimate >= threshold


def collapse_mutual(containments: Sequence[Containment]) -> list[Containment]:
    """Two surviving directions over one pair of columns become one candidate.

    Run AFTER verification, not before: with the LSH pass weighted for recall, both
    directions come back for many pairs that are nested only one way, and collapsing on the
    ensemble's word would label a strict subset as an equal set.

    **Which way round the survivor is left is not arbitrary.** A unique container is what
    makes the pair readable as a key and its three uses (6.4), so the unique column becomes
    the container; with both unique or neither, the sheet and column order decides, so that
    two runs over one workbook cannot produce mirror-image records.
    """
    found = {(item.contained.key, item.container.key): item for item in containments}
    collapsed: list[Containment] = []
    seen: set[tuple] = set()
    for (left_key, right_key), containment in found.items():
        unordered = tuple(sorted((left_key, right_key)))
        if unordered in seen:
            continue
        reverse = found.get((right_key, left_key))
        if reverse is None:
            collapsed.append(containment)
            continue
        seen.add(unordered)
        forward = containment
        if _prefers_as_container(reverse.container, forward.container):
            forward = reverse
        collapsed.append(replace(forward, mutual=True))
    collapsed.sort(key=lambda item: (item.contained.key, item.container.key))
    return collapsed


def _prefers_as_container(candidate: ColumnRef, incumbent: ColumnRef) -> bool:
    """Should ``candidate`` be the container of a mutual pair instead of ``incumbent``?"""
    if bool(candidate.is_unique) != bool(incumbent.is_unique):
        return bool(candidate.is_unique)
    return candidate.key < incumbent.key


# ---------------------------------------------------------------------------
# Names — a tie-break, never a reason
# ---------------------------------------------------------------------------


def name_similarity(left: Optional[str], right: Optional[str]) -> Optional[float]:
    """How alike two column headers read, or ``None`` when one of them has no header.

    ``difflib.SequenceMatcher``, which is what ``SPRINT_0_3_0.md`` 7.1 keeps for the same
    job in entity resolution — "as a tie-break rather than as the mechanism". Nothing calls
    this before a containment has been measured, and no caller may use the result to admit a
    pair; it orders the candidates a reviewer is shown and does nothing else.
    """
    if not left or not right:
        return None
    return round(
        difflib.SequenceMatcher(None, _normalise_name(left), _normalise_name(right)).ratio(), 4
    )


def _normalise_name(name: str) -> str:
    """Case and punctuation removed, so ``Deal ID`` and ``deal_id`` read as the same header."""
    return "".join(character for character in name.casefold() if character.isalnum())


# ---------------------------------------------------------------------------
# Verification, and the candidate a reviewer sees
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Verification:
    """What counting the two value sets said, or why it could not be done.

    The LSH pass is recall. This is the answer where there is one — and where there is not,
    it says so by name rather than letting an estimate stand in a field a reader would take
    for a count.
    """

    method: str
    reason: Optional[str] = None
    contained_size: Optional[int] = None
    container_size: Optional[int] = None
    overlap: Optional[int] = None

    @property
    def containment(self) -> Optional[float]:
        if self.overlap is None or not self.contained_size:
            return None
        return round(self.overlap / self.contained_size, 6)

    def as_dict(self) -> dict:
        return {
            "method": self.method,
            "reason": self.reason,
            "contained_size": self.contained_size,
            "container_size": self.container_size,
            "overlap": self.overlap,
            "containment": self.containment,
        }


def unverifiable_reason(containment: Containment) -> Optional[str]:
    """Why this pair cannot be counted from what is stored, or ``None`` when it can.

    **This is the seam between 6.4's two halves, and it does not fully close.** 6.4 asks for
    "exact verification in SQL on the survivors", and the only value sets in the database are
    the ``values`` arrays 8.5 retains — which it retains only while a column's distinct set is
    small (``VALUES_RETAINED_MAX``). So the pair the sketch exists to find, a small lookup
    inside a large log, is precisely the pair whose container has no stored values to count
    against. The two ways to close it are both future work and both named here rather than
    papered over: ``extract:sheet`` (8.4, blocked on 8.8's thresholds) puts every row in the
    database, and re-reading the workbook blob would make the verification a second pass over
    the file rather than a query.

    Until one of them lands, such a candidate is proposed with its estimate and labelled
    :data:`VERIFIED_NONE`. It is NOT dropped: dropping it would discard exactly the columns
    6.3 added a sketch to make visible, and it is not presented as counted either.
    """
    missing = [
        f"{column.sheet_name!r}.{column.label!r}"
        for column in (containment.contained, containment.container)
        if not column.has_values
    ]
    if not missing:
        return None
    return (
        f"no exact verification: {', '.join(missing)} has no stored `values`, because "
        "INGEST_SPEC.md 8.5 retains a column's value list only while its distinct set is "
        "small enough (jmfts_core.office.sheets.VALUES_RETAINED_MAX). The containment below "
        "is the MinHash estimate and was not counted. Verifying it needs the rows in the "
        "database (extract:sheet, 8.4) or a second read of the workbook"
    )


def implications_of(containment: Containment) -> tuple[tuple[str, ...], str]:
    """6.4's three readings of one measurement, and why they do or do not apply.

    "A high-containment pair where the containing column is unique is simultaneously a
    foreign-key candidate, a resource-valued-predicate candidate, and an
    inverse-functional-property candidate. One measurement, three uses." All three are
    recorded together; choosing between them is a statement about meaning and 8.6 gives that
    to the reviewer.
    """
    container = containment.container
    if container.is_unique:
        return (
            UNIQUE_CONTAINER_IMPLICATIONS,
            f"column {container.label!r} of sheet {container.sheet_name!r} holds a distinct "
            "value in every row, so it identifies a row; a column whose values are inside it "
            "is a foreign-key candidate (SPRINT_0_3_0.md 6.4), a resource-valued-predicate "
            "candidate (6.5) and an inverse-functional-property candidate (7.2, "
            "owl:InverseFunctionalProperty per 5.4). Which of the three it IS is the "
            "reviewer's to say",
        )
    if container.is_unique is None:
        return (
            (),
            f"column {container.label!r} of sheet {container.sheet_name!r} stopped being "
            "tracked before its distinct set was known "
            "(jmfts_core.office.sheets.DISTINCT_TRACKED_MAX), so `is_unique` is unknown and "
            "nothing is claimed about what containing it would mean",
        )
    return (
        (),
        f"column {container.label!r} of sheet {container.sheet_name!r} repeats values, so it "
        "identifies no row; the pair is a shared value set (INGEST_SPEC.md 8.6) and not a "
        "key relationship",
    )


def candidate_record(containment: Containment, verification: Verification) -> dict:
    """One proposal, with the counts it was found by and the review task that carries it."""
    implications, reason = implications_of(containment)
    similarity = name_similarity(containment.contained.name, containment.container.name)
    return {
        "contained": _column_record(containment.contained),
        "container": _column_record(containment.container),
        "mutual": containment.mutual,
        "containment_estimate": round(containment.estimate, 6),
        "verification": verification.as_dict(),
        # Recorded on every candidate and read by nothing that decides one. See
        # `name_similarity`: 6.4 makes the name a tie-break and never a reason, so this is an
        # ordering input for the reviewer's queue and an input to no test above it.
        "name_similarity": similarity,
        "implications": list(implications),
        "implications_reason": reason,
        "review": review_payload(containment, verification),
    }


def _column_record(column: ColumnRef) -> dict:
    return {
        "node_id": column.node_id,
        "sheet": column.sheet_name,
        "sheet_index": column.sheet_index,
        "column": column.name,
        "letter": column.letter,
        "index": column.index,
        # The JSONB array position the exact verification addressed, so a reader can replay
        # the query that produced the counts beside it.
        "position": column.position,
        "distinct_count": column.distinct_count,
        "sketched_values": column.sketch_size,
        "partial_sketch": column.partial,
        "is_unique": column.is_unique,
        "body_rows": column.body_rows,
    }


def review_payload(containment: Containment, verification: Verification) -> dict:
    """``INGEST_SPEC.md`` 7.6's payload, which is a public contract.

    ``confirm``, because the proposal is one pair and the reviewer's job is to say whether it
    means what the counting suggests — 7.5's "a review task carries a proposed answer", so
    ``proposed`` is ``true`` and approval is one click.

    ``estimated_rebuild_nodes`` is the contained column's row count: confirming the link
    changes how ``extract:sheet`` writes THAT sheet's rows (6.5 — a resolved column's objects
    become resources rather than literals). It assumes 8.4's ``records`` shape, one node per
    row, and no shape has been chosen for any sheet yet; the assumption is stated once in the
    workbook's record rather than repeated on every payload.
    """
    return {
        "question": _question(containment, verification),
        "field_type": "confirm",
        "options": None,
        "proposed": True,
        "target_task": "extract:sheet",
        "target_param": "column_links",
        "estimated_rebuild_nodes": containment.contained.body_rows,
    }


def _question(containment: Containment, verification: Verification) -> str:
    """One sentence of counting, then one question about meaning. Never the other way round."""
    contained, container = containment.contained, containment.container
    where_in = f"column {contained.label!r} on sheet {contained.sheet_name!r}"
    where_out = f"column {container.label!r} on sheet {container.sheet_name!r}"
    counted = verification.method == VERIFIED_SQL_EXACT
    if containment.mutual:
        held = "hold the same values" if counted else "appear to hold the same values"
        return f"Both {where_in} and {where_out} {held}. Do they mean the same thing?"
    if counted:
        # "All 8 values", not "8 of the 8 values". Total containment is the case this search
        # exists for, and a reviewer reading the second one has to do the comparison again.
        if verification.overlap == verification.contained_size:
            share = f"All {verification.contained_size:,} values"
        else:
            share = f"{verification.overlap:,} of the {verification.contained_size:,} values"
        opening = f"{share} of {where_in} also appear in {where_out}"
    else:
        opening = (
            f"An estimated {containment.estimate:.0%} of the values of {where_in} also "
            f"appear in {where_out}"
        )
    if container.is_unique:
        opening += ", which holds a distinct value in every row"
    return f"{opening}. Does {where_in} refer to {where_out}?"


def sort_candidates(records: Sequence[dict]) -> list[dict]:
    """Counted before estimated, then by containment, then by how alike the names read.

    The name is the LAST key and it only ever separates candidates that the counting could
    not separate — which is what 6.4 means by a tie-break.
    """
    return sorted(
        records,
        key=lambda record: (
            0 if record["verification"]["method"] == VERIFIED_SQL_EXACT else 1,
            -(record["verification"]["containment"] or record["containment_estimate"]),
            -(record["name_similarity"] or 0.0),
            record["contained"]["node_id"],
            record["contained"]["position"],
            record["container"]["node_id"],
            record["container"]["position"],
        ),
    )


__all__ = [
    "DEFAULT_CONTAINMENT_THRESHOLD",
    "DEFAULT_LSH_PARTITIONS",
    "DEFAULT_MAX_CANDIDATES",
    "DEFAULT_MIN_DISTINCT_VALUES",
    "IMPLICATION_FOREIGN_KEY",
    "IMPLICATION_INVERSE_FUNCTIONAL_PROPERTY",
    "IMPLICATION_RESOURCE_VALUED_PREDICATE",
    "LSH_RECALL_WEIGHTS",
    "METHOD_LSH_ENSEMBLE",
    "SKETCH_RESOLUTION",
    "THRESHOLD_UNCALIBRATED",
    "UNIQUE_CONTAINER_IMPLICATIONS",
    "VERIFIED_NONE",
    "VERIFIED_SQL_EXACT",
    "ColumnRef",
    "Containment",
    "IncomparableSketches",
    "Verification",
    "candidate_record",
    "census",
    "collapse_mutual",
    "columns_of",
    "find_containments",
    "implications_of",
    "name_similarity",
    "passes_threshold",
    "recipe_of",
    "review_payload",
    "sort_candidates",
    "unverifiable_reason",
]
