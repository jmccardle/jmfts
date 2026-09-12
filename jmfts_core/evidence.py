"""What each evidence name is, and where the appliance keeps it.

``SPRINT_JOBS.md`` Part 3, and Phases 2 and 2b. :mod:`jmfts_core.atoms` says which atom
reads and writes each name; this module says what the name's value IS — a type, and a
storage location — so that a guard (4.4) can be checked before it runs and a reader can find
the value without knowing which handler wrote it.

**Evidence lives in rows, and Phase 2b is when it stopped living in a column.** Twenty-three
of these names were keys in ``Document.structured_content``; migration 015 moved them to
``document_evidence``, one row per ``(document_id, name)``, and
:class:`~jmfts_core.repositories.evidence.EvidenceRepository` is the only door to them. 13.1
gives the two reasons and both are correctness: a write to the column was a read-modify-write
that lost a concurrent write to a DIFFERENT name with nothing raised, and 3.2's third state
has nowhere to live in a column — staling a JSONB block means deleting it, which is
indistinguishable from never having run.

**Nothing puts them back, and that was decided rather than defaulted.** 13.3 took its
option 2: ``structured_content`` returns exactly what a caller put there, no response
stitches the rows into it, and a client that wants evidence asks
``GET /documents/{id}/evidence``. Two consequences this module used to carry are gone with
it — ``ingest_owned_keys`` and the metadata gate it fed, because the column is wholly the
caller's now, and 3.1's reserved-word defect, because twenty-three ordinary English words
stopped being in the caller's dict at all.

TWO LEVELS, AND 3.1 IS WHAT FORCES IT. Part 3.1 maps a name to one of ``bool``, ``int``,
``float``, ``str``, ``list``, ``sketch`` — and not one of those is what ``matched`` or
``extraction`` holds. :mod:`jmfts_core.atoms` names BLOCKS, because a block is the unit an
atom writes; a guard and a fan-out bound read LEAVES inside them. Both are registered here,
a leaf naming the block it sits in through :attr:`Evidence.within`.

WHAT IS REGISTERED AS A LEAF, AND WHAT IS NOT. Every block gets an entry. A leaf gets one
when something SCHEDULES on it: a Part 4 condition, a fan-out bound, or a dispatch inside a
handler. ``sheet.measurements`` alone holds twenty-eight keys and none of the other
twenty-four decide anything, so registering them would be twenty-four names to keep in step
with ``sheet_profile.py`` in exchange for nothing. The block is typed ``dict`` and its
payload is the producing module's business.

``matched.patterns`` IS AN OPEN NAMESPACE AND THAT IS DELIBERATE.
``tests/corpus/vocabulary.py`` gets probe's pattern names by RUNNING probe, and its
docstring says why: "a corpus that keeps its own list of feature names next to probe's list
has two lists, and two lists diverge — quietly, in the direction that makes coverage look
better than it is." Writing those thirty names here would be that second list. So this
module declares the rule instead — :data:`PATTERN_TYPES` names the patterns that are not
``bool``, and every other emitted key is a flag — and ``tests/test_evidence_registry.py``
holds the rule against the live probe in both directions.

THE TYPE LIST IN 3.1 IS SHORT BY TWO, and both are recorded here rather than worked around.
``blob`` is bytes, which no member of that list describes, and ten of the fourteen blocks
are ``dict``. :data:`TYPE_SKETCH` is in the list and has no member: ``datasketch`` is behind
the ``sketch`` extra and reaches evidence only through ``sheet.measurements``' payload,
which is not registered leaf by leaf. It is kept because 3.1 names it and because
``propose:links`` is what will fill it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from jmfts_core.atoms import ATOMS, EXTERNAL_EVIDENCE

# ---------------------------------------------------------------------------
# Types — 3.1
# ---------------------------------------------------------------------------

TYPE_BOOL = "bool"
TYPE_INT = "int"
TYPE_FLOAT = "float"
TYPE_STR = "str"
TYPE_LIST = "list"

#: A JSONB object whose keys belong to the module that writes it. 3.1's list has no member
#: for this and ten blocks need one — see the module docstring.
TYPE_DICT = "dict"

#: Raw stored bytes. 3.1's list has no member for this either, and ``blob`` is read by four
#: handlers.
TYPE_BYTES = "bytes"

#: A MinHash or similar. Named by 3.1, with no registered member yet.
TYPE_SKETCH = "sketch"

EVIDENCE_TYPES: tuple[str, ...] = (
    TYPE_BOOL,
    TYPE_INT,
    TYPE_FLOAT,
    TYPE_STR,
    TYPE_LIST,
    TYPE_DICT,
    TYPE_BYTES,
    TYPE_SKETCH,
)

#: What a declared type accepts, for :func:`check`. ``bool`` is checked before ``int``
#: because ``isinstance(True, int)`` is true in Python, and 3.3 has separate rows for a
#: flag and a counted measurement — a prober that returned ``True`` where a count was
#: declared would otherwise pass.
_PYTHON_TYPES: dict[str, tuple[type, ...]] = {
    TYPE_BOOL: (bool,),
    TYPE_INT: (int,),
    TYPE_FLOAT: (float, int),
    TYPE_STR: (str,),
    TYPE_LIST: (list, tuple),
    TYPE_DICT: (dict,),
    TYPE_BYTES: (bytes, bytearray, memoryview),
    TYPE_SKETCH: (),
}


# ---------------------------------------------------------------------------
# Stores
# ---------------------------------------------------------------------------

#: A ``document_evidence`` row, named by ``row``, optionally at the dotted ``path`` inside
#: that row's value. Where evidence lives. Phase 2b put it here and 13.1 is why — the
#: ``structured_content`` column this replaced lost concurrent writes to different names and
#: had nowhere to put 3.2's third state.
STORE_EVIDENCE = "evidence"

#: A column on ``Document``, named by ``path``. ``text`` and the document vector.
STORE_COLUMN = "column"

#: The stored bytes, through :class:`~jmfts_core.repositories.blob.BlobRepository`.
STORE_BLOB = "blob"

#: Rows in the table named by ``path``. ``token_embeddings`` is the only one.
STORE_ROWS = "rows"

#: Not stored anywhere: read off the shape of the tree. ``child_count`` is the only one, and
#: it is real evidence — ``structure:semantic``'s bound is a function of it.
STORE_TREE = "tree"

STORE_KINDS: tuple[str, ...] = (
    STORE_EVIDENCE,
    STORE_COLUMN,
    STORE_BLOB,
    STORE_ROWS,
    STORE_TREE,
)


@dataclass(frozen=True)
class Store:
    """Where one evidence value physically is.

    ``row`` AND ``path`` ARE TWO FIELDS BECAUSE ONE STRING CANNOT SAY IT. For
    :data:`STORE_EVIDENCE`, ``row`` names the ``document_evidence`` row and ``path`` is a
    dotted descent into that row's JSONB value, empty for the whole row. A single dotted
    string would be ambiguous exactly once and it matters: ``source_anchor.unresolved`` is
    its OWN row, not the ``unresolved`` key of the ``source_anchor`` row, because 3.2 cites
    that pair as "two keys, never one with a null" — "could not be placed, because X" and
    "is at page 3" are different facts and neither is inside the other.

    This is not a second copy of :attr:`Evidence.within`. ``within`` says which FACT a leaf
    is part of, which is what makes a leaf inherit its block's producer; this says where the
    bytes are. They agree for every entry but that one, and that one is why both exist.
    """

    kind: str
    path: Optional[str] = None
    row: Optional[str] = None

    def __post_init__(self) -> None:
        if self.kind not in STORE_KINDS:
            raise ValueError(f"unknown store kind {self.kind!r}; expected one of {STORE_KINDS}")
        if self.kind in (STORE_COLUMN, STORE_ROWS) and not self.path:
            raise ValueError(f"a {self.kind} store must name where the value lives")
        if self.kind == STORE_EVIDENCE and not self.row:
            raise ValueError("an evidence store must name the document_evidence row it is in")
        if self.kind != STORE_EVIDENCE and self.row:
            raise ValueError(f"a {self.kind} store has no document_evidence row")

    def __str__(self) -> str:
        if self.kind == STORE_EVIDENCE:
            return f"{self.kind}:{self.row}.{self.path}" if self.path else f"{self.kind}:{self.row}"
        return f"{self.kind}:{self.path}" if self.path else self.kind


# ---------------------------------------------------------------------------
# One entry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Evidence:
    """One registered name. 3.1's mapping, with the storage location beside it.

    ``produced_by`` IS NOT A FIELD, and :func:`producers` is why: :data:`ATOMS` already
    records who writes what, and a copy of it here would be a second answer to the same
    question with nothing keeping the two equal.
    """

    name: str
    type: str
    store: Store
    #: The registered block this name sits inside, for a leaf. ``None`` for a block itself.
    within: Optional[str] = None
    #: Whether a successful write may leave this ``null``. 3.2 makes a produced null a
    #: RESULT rather than a gap, so this says which names have that result available.
    nullable: bool = False
    #: What the value asserts, in one line, for ``EXPLAIN``.
    doc: str = ""

    def __post_init__(self) -> None:
        if self.type not in EVIDENCE_TYPES:
            raise ValueError(
                f"evidence {self.name!r} declares type {self.type!r}; "
                f"expected one of {EVIDENCE_TYPES}"
            )


REGISTRY: dict[str, Evidence] = {}


def register(
    name: str,
    *,
    type: str,
    store: Store,
    within: Optional[str] = None,
    nullable: bool = False,
    doc: str = "",
) -> Evidence:
    """Add one entry. A repeated name is a collision and raises.

    Collision rather than replacement, which is the opposite of
    :func:`jmfts_core.atoms.declare`. An atom is re-declared every time its module is
    re-imported under ``--reload``; this module is imported once and every entry is a
    literal in it, so a repeat is a copy-paste rather than a reload.
    """
    if name in REGISTRY:
        raise ValueError(f"evidence {name!r} is already registered as {REGISTRY[name]}")
    if within is not None and within not in REGISTRY:
        raise ValueError(
            f"evidence {name!r} sits within {within!r}, which is not registered; "
            "a block is registered before the leaves inside it"
        )
    entry = Evidence(name=name, type=type, store=store, within=within, nullable=nullable, doc=doc)
    REGISTRY[name] = entry
    return entry


# ---------------------------------------------------------------------------
# The blocks — one per name in `atoms.EVIDENCE`, plus what the pipeline writes
# that no atom declares
# ---------------------------------------------------------------------------

register(
    "file",
    type=TYPE_DICT,
    store=Store(STORE_EVIDENCE, row="file"),
    doc="what was received: filename, declared mime, size. Written by the upload.",
)
register(
    "source",
    type=TYPE_DICT,
    store=Store(STORE_EVIDENCE, row="source"),
    doc="where to fetch this document from, when it was named rather than uploaded.",
)
register(
    "blob",
    type=TYPE_BYTES,
    store=Store(STORE_BLOB),
    doc="the stored bytes. Written by the upload, read by four handlers.",
)
register(
    "matched",
    type=TYPE_DICT,
    store=Store(STORE_EVIDENCE, row="matched"),
    doc="what probing found: the detected format and the probed patterns.",
)
register(
    "text",
    type=TYPE_STR,
    store=Store(STORE_COLUMN, "content"),
    nullable=True,
    doc="the node's own text. A sheet node legitimately has none.",
)
register(
    "extraction",
    type=TYPE_DICT,
    store=Store(STORE_EVIDENCE, row="extraction"),
    doc="which reader ran, and what it yielded that a later task cannot recompute.",
)
register(
    "structure",
    type=TYPE_DICT,
    store=Store(STORE_EVIDENCE, row="structure"),
    doc="the rung that built the tree below this node, its source, and its coverage.",
)
register(
    "sheet",
    type=TYPE_DICT,
    store=Store(STORE_EVIDENCE, row="sheet"),
    doc="which worksheet this node is, and everything since measured about it.",
)
register(
    "profile",
    type=TYPE_DICT,
    store=Store(STORE_EVIDENCE, row="profile"),
    doc="the rendered profile of a sheet, on the summary node `profile:sheet` writes.",
)
register(
    "record",
    type=TYPE_DICT,
    store=Store(STORE_EVIDENCE, row="record"),
    doc="one spreadsheet row as typed values, on the record node `extract:sheet` writes.",
)
register(
    "cell",
    type=TYPE_DICT,
    store=Store(STORE_EVIDENCE, row="cell"),
    doc="one column of one row — its name and typed value — on a `cell` node under a "
    "record too long to embed whole.",
)
register(
    "embedding",
    type=TYPE_LIST,
    store=Store(STORE_COLUMN, "embed"),
    nullable=True,
    doc="the document vector. Null until `embed` has run on this node.",
)
register(
    "effective_content",
    type=TYPE_DICT,
    store=Store(STORE_EVIDENCE, row="effective_content"),
    doc="how a container's embedded text was arrived at, and by which method.",
)
register(
    "source_span",
    type=TYPE_LIST,
    store=Store(STORE_EVIDENCE, row="source_span"),
    nullable=True,
    doc="`[start, end]` into the file node's markdown. Absent when it could not be placed.",
)
# THE NAME AND THE OLD COLUMN KEY DIFFERED HERE, and 2.5's finding 5 is the rule that says
# they may: `citation` wrote the column key `anchor`, and the fact it asserts is where the
# chunk's source is, which is what `source_anchor` names. Phase 2b closed the gap by moving
# the store rather than the name — the row IS `source_anchor`, and migration 015 is where
# `anchor` was renamed on its way out of the column.
register(
    "source_anchor",
    type=TYPE_DICT,
    store=Store(STORE_EVIDENCE, row="source_anchor"),
    nullable=True,
    doc="the page and rectangles a chunk came from. Absent when it could not be placed.",
)

# --- written by the pipeline, declared by no atom ---------------------------
#
# THREE BLOCKS NO ATOM PRODUCES, and each has a different reason. They are registered
# because the question this module answers for a reader — where does this value live, and
# may I overwrite it — has the same answer for them as for every block above, and because
# `EvidenceRepository` refuses to write a name this registry does not know.

register(
    "options",
    type=TYPE_DICT,
    store=Store(STORE_EVIDENCE, row="options"),
    doc="the resolved per-format configuration the upload was accepted with. Not a task's.",
)
register(
    "yield",
    type=TYPE_DICT,
    store=Store(STORE_EVIDENCE, row="yield"),
    doc="that a file node settled holding nothing. Written by the settling walk.",
)
register(
    "attempts",
    type=TYPE_LIST,
    store=Store(STORE_EVIDENCE, row="attempts"),
    doc="spec 5.6's durable append-only log. Written by the queue, never by a handler.",
)

# ---------------------------------------------------------------------------
# The leaves — what schedules on them, and nothing else
# ---------------------------------------------------------------------------

register(
    "matched.format",
    type=TYPE_STR,
    store=Store(STORE_EVIDENCE, "format", row="matched"),
    within="matched",
    doc="the detected format. Part 4's rows resolve their sentinel patterns per format.",
)
register(
    "matched.patterns",
    type=TYPE_DICT,
    store=Store(STORE_EVIDENCE, "patterns", row="matched"),
    within="matched",
    doc="probe's feature vector. An open namespace — see PATTERN_TYPES.",
)
register(
    "matched.patterns.sheet_count",
    type=TYPE_INT,
    store=Store(STORE_EVIDENCE, "patterns.sheet_count", row="matched"),
    within="matched.patterns",
    doc="how many sheets the workbook names. `structure:sheets`' ceiling, exactly.",
)
register(
    "extraction.source",
    type=TYPE_STR,
    store=Store(STORE_EVIDENCE, "source", row="extraction"),
    within="extraction",
    doc="which reader produced the text. `citation` dispatches on it and refuses the rest.",
)
register(
    "extraction.characters",
    type=TYPE_INT,
    store=Store(STORE_EVIDENCE, "characters", row="extraction"),
    within="extraction",
    doc="how much text came out. The numerator of both chunking rungs' ceiling.",
)
register(
    "structure.coverage",
    type=TYPE_FLOAT,
    store=Store(STORE_EVIDENCE, "coverage", row="structure"),
    within="structure",
    nullable=True,
    doc="what fraction of the text a rung placed. Part 4's 'coverage gap remains'.",
)
register(
    "sheet.measurements",
    type=TYPE_DICT,
    store=Store(STORE_EVIDENCE, "measurements", row="sheet"),
    within="sheet",
    doc="spec 8.3's counted signals, and the header verdict `extract:sheet` reads.",
)
register(
    "sheet.measurements.rows",
    type=TYPE_INT,
    store=Store(STORE_EVIDENCE, "measurements.rows", row="sheet"),
    within="sheet.measurements",
    doc="how many rows the sheet occupies. `extract:sheet`'s ceiling, less the header.",
)
register(
    "sheet.shape",
    type=TYPE_STR,
    store=Store(STORE_EVIDENCE, "shape", row="sheet"),
    within="sheet",
    nullable=True,
    doc="spec 8.4's verdict. Null with a `shape_decision` beside it when none was taken.",
)
# 3.2 CITES THIS PAIR AS THE MODEL FOR ITS THIRD STATE: "two keys, never one with a null,
# because 'could not be placed, because X' and 'is at page 3' are different facts". So it is
# two entries, not one nullable one — the same distinction, registered.
register(
    "source_anchor.unresolved",
    type=TYPE_DICT,
    store=Store(STORE_EVIDENCE, row="source_anchor.unresolved"),
    within="source_anchor",
    nullable=True,
    doc="why a chunk got no anchor, by code and reason. Present exactly when `anchor` is not.",
)
register(
    "embedding.tokens",
    type=TYPE_LIST,
    store=Store(STORE_ROWS, "token_embeddings"),
    within="embedding",
    nullable=True,
    doc="the per-token vectors, when they were asked for. Rows, not a column.",
)

# --- the record node's own keys ---------------------------------------------
#
# THREE KEYS BESIDE THE RECORD, and they are registered for the same reason it is: they sit
# at the TOP level of a record node's column, not inside `record`, so the metadata gate saw
# them as the caller's. `within` is `record` because `extract:sheet` is what writes all
# four and the row is what they are about.

register(
    "row_index",
    type=TYPE_INT,
    store=Store(STORE_EVIDENCE, row="row_index"),
    within="record",
    doc="the row number the workbook shows, not a position in any result.",
)
register(
    "sheet_name",
    type=TYPE_STR,
    store=Store(STORE_EVIDENCE, row="sheet_name"),
    within="record",
    doc="which worksheet the row came from.",
)
register(
    "cells",
    type=TYPE_DICT,
    store=Store(STORE_EVIDENCE, row="cells"),
    within="record",
    doc="per-cell notes — a formula, a forced text type. Sparse, and absent on a plain row.",
)

# --- the chunker's per-chunk keys -------------------------------------------
#
# WRITTEN AT CREATE TIME BY `_TreeWriter`, INSIDE `structure:declared` AND
# `structure:inferred`. They are each chunk's own record of how the rung placed it, which is
# why `within` is `structure` — the file node's `structure` block is the same fact about the
# whole subtree, and both rungs now declare `structure@subtree` for exactly these five.
# 2.5's unresolved question, whether `chunk` is its own atom, is what leaves them without a
# producer of their own.
#
# FIVE BARE KEYS, AND PHASE 2b IS WHAT STOPPED THEM COSTING ANYTHING. Every other producer
# namespaces what it writes; the chunker wrote `rung` and `section_title` straight into
# `structured_content`, so owning them reserved five ordinary words from every caller's
# metadata — a caller writing `{"structure": "loose"}` on a chunk was not making a mistake
# and could not be told so before the fact. 3.1 recorded that as a defect and wanted a
# namespace. 13.3 closed it without one: they are five rows now, and the caller's dict is
# the caller's. They are still five names rather than one block because a row per name is
# what makes two writers to one node safe, which is the whole of 13.1's first fact.

register(
    "rung",
    type=TYPE_STR,
    store=Store(STORE_EVIDENCE, row="rung"),
    within="structure",
    doc="which rung wrote this chunk. The per-node half of the file node's `structure`.",
)
register(
    "section_title",
    type=TYPE_STR,
    store=Store(STORE_EVIDENCE, row="section_title"),
    within="structure",
    nullable=True,
    doc="the heading the chunk sits under. Null under an untitled region.",
)
register(
    "section_level",
    type=TYPE_INT,
    store=Store(STORE_EVIDENCE, row="section_level"),
    within="structure",
    nullable=True,
    doc="the heading's depth.",
)
register(
    "chunk_index",
    type=TYPE_INT,
    store=Store(STORE_EVIDENCE, row="chunk_index"),
    within="structure",
    doc="the chunk's ordinal within its section.",
)
register(
    "source_line",
    type=TYPE_INT,
    store=Store(STORE_EVIDENCE, row="source_line"),
    within="structure",
    nullable=True,
    doc="the section's line offset into the extracted markdown. `source_span`'s sibling.",
)

# --- the conversation rung's per-turn keys -----------------------------------
#
# WRITTEN BY `structure:conversation` AND FOUND BY PHASE 2b'S AUDIT, not by Phase 2's. S7
# added them beside `rung` on every turn node and none of them was registered; the Phase 2
# audit that would have caught it (`no node carries a block the gate does not know`) never
# reached one, because its fixture ingests a document rather than a transcript. So they were
# six ordinary English words — `timestamp` and `speaker` among them — reserved from every
# caller's metadata with nothing saying so. `within` is `structure` for the same reason the
# chunker's five have it: they are one rung's record of how it placed this node.

register(
    "speaker",
    type=TYPE_STR,
    store=Store(STORE_EVIDENCE, row="speaker"),
    within="structure",
    doc="whose turn this is, as the transcript labelled it.",
)
register(
    "turn_index",
    type=TYPE_INT,
    store=Store(STORE_EVIDENCE, row="turn_index"),
    within="structure",
    doc="the turn's ordinal in the conversation. Shared by an over-window turn's parts.",
)
register(
    "timestamp",
    type=TYPE_STR,
    store=Store(STORE_EVIDENCE, row="timestamp"),
    within="structure",
    nullable=True,
    doc="when the turn was sent, as the transcript stated it. Null when it did not.",
)
register(
    "conversation_id",
    type=TYPE_INT,
    store=Store(STORE_EVIDENCE, row="conversation_id"),
    within="structure",
    doc="the file node the turn came from, so a part knows its conversation without a walk.",
)
register(
    "over_token_window",
    type=TYPE_BOOL,
    store=Store(STORE_EVIDENCE, row="over_token_window"),
    within="structure",
    doc="that the turn did not fit and was split. A turn with one part and a turn that fit "
    "are different facts, and only this says which.",
)
register(
    "part_index",
    type=TYPE_INT,
    store=Store(STORE_EVIDENCE, row="part_index"),
    within="structure",
    doc="which piece of an over-window turn this node is. Absent on the turn itself.",
)

# --- read off the tree ------------------------------------------------------

register(
    "child_count",
    type=TYPE_INT,
    store=Store(STORE_TREE),
    doc="how many children this node has. `structure:semantic`'s ceiling is a function of it.",
)


# ---------------------------------------------------------------------------
# probe's patterns — the rule, not the list
# ---------------------------------------------------------------------------

#: Every pattern probe emits that is NOT a ``bool``. Everything else it emits is a flag.
#:
#: A RULE AND ITS EXCEPTIONS, RATHER THAN THIRTY NAMES. ``tests/corpus/vocabulary.py``
#: recovers probe's vocabulary by running probe, precisely so that the corpus and probe
#: cannot come to hold different lists; writing the flag names out here would reintroduce
#: exactly that. The exceptions are few and they are the ones a guard does arithmetic on,
#: so they are worth naming — and ``tests/test_evidence_registry.py`` checks the rule in
#: both directions against the installed probe, which is the ratchet: a new measurement in
#: probe fails this suite until it is named here.
PATTERN_TYPES: dict[str, str] = {
    "char_count": TYPE_INT,
    "heading_count": TYPE_INT,
    "image_count": TYPE_INT,
    "legacy_application": TYPE_STR,
    "line_count": TYPE_INT,
    "max_heading_level": TYPE_INT,
    "outline_depth": TYPE_INT,
    "page_count": TYPE_INT,
    "part_count": TYPE_INT,
    "sheet_count": TYPE_INT,
    "slide_count": TYPE_INT,
    "unknown_part_count": TYPE_INT,
    "unknown_parts": TYPE_LIST,
}


def pattern_type(name: str) -> str:
    """What ``matched.patterns[name]`` holds. Unnamed patterns are flags."""
    return PATTERN_TYPES.get(name, TYPE_BOOL)


class EvidenceTypeError(TypeError):
    """A value does not match its registered type.

    A ``TypeError`` subclass so that the queue's classifier treats it the way it treats
    every other bad-value error, and not the way it treats an ``ImportError``: a handler
    that wrote the wrong shape wrote it deterministically, and retrying is pointless — but
    that is the classifier's call from the base class, not a policy stated twice.
    """


def check(name: str, value: object) -> object:
    """Return ``value`` if it matches ``name``'s registered type, else raise.

    3.1's reason for typing the registry at all: "Guards need this (Part 4.4), and so does
    the validation that ``ingest_options._positive_int`` currently provides for two groups
    only." This is the single place that answers it, so a guard in Phase 4 and a bound's
    evidence lookup today cannot come to disagree about what ``rows`` is.

    ``None`` PASSES ONLY FOR A NULLABLE NAME, and 3.2 is why: an atom must write every name
    it produces on success, null included, so a null is a RESULT. Accepting one for a name
    that has no null result would let "the reader returned nothing" and "the handler forgot"
    reach a guard as the same value.
    """
    entry = get(name)
    if value is None:
        if entry.nullable:
            return value
        raise EvidenceTypeError(
            f"evidence {name!r} is null and is not declared nullable; a null is a result "
            "(SPRINT_JOBS.md 3.2) and this name has no null result"
        )
    accepted = _PYTHON_TYPES[entry.type]
    if not accepted:
        raise EvidenceTypeError(
            f"evidence {name!r} declares type {entry.type!r}, which has no Python type "
            "registered against it yet"
        )
    # bool BEFORE int: `isinstance(True, int)` is true, and 3.3 has separate rows for a
    # flag and a counted measurement, so a `True` where a count is declared must fail.
    if entry.type != TYPE_BOOL and isinstance(value, bool):
        raise EvidenceTypeError(
            f"evidence {name!r} declares type {entry.type!r} and holds the flag {value!r}"
        )
    if not isinstance(value, accepted):
        raise EvidenceTypeError(
            f"evidence {name!r} declares type {entry.type!r} and holds "
            f"{type(value).__name__} {value!r}"
        )
    return value


# ---------------------------------------------------------------------------
# Reading the registry
# ---------------------------------------------------------------------------


def get(name: str) -> Evidence:
    """One entry. A pattern name resolves through :func:`pattern_type` if it is not here.

    Raises rather than returning ``None``, because every caller of this either has a name
    an atom declared or a name a bound asked for, and both are closed vocabularies. A miss
    is a typo, and a typo that returned ``None`` would read as evidence nothing writes.
    """
    entry = REGISTRY.get(name)
    if entry is not None:
        return entry
    block, sep, leaf = name.rpartition(".")
    if sep and block == "matched.patterns":
        return Evidence(
            name=name,
            type=pattern_type(leaf),
            store=Store(STORE_EVIDENCE, f"patterns.{leaf}", row="matched"),
            within="matched.patterns",
            nullable=True,
            doc=f"probe's {leaf!r}. Absent for a format whose prober does not measure it.",
        )
    raise KeyError(
        f"evidence {name!r} is not registered; known names are {sorted(REGISTRY)}. "
        "A new one is an entry in jmfts_core.evidence, not a string at the call site"
    )


def _declared_producers(name: str) -> set[str]:
    """Atoms whose ``produces`` names exactly ``name``, at any locus."""
    return {t for t, atom in ATOMS.items() if any(f.name == name for f in atom.produces)}


def owner(name: str) -> Optional[Evidence]:
    """The nearest registered name at or above ``name`` that an atom declares producing.

    A LEAF INHERITS ITS BLOCK'S PRODUCER, and that is what makes the answer useful: nothing
    declares that it produces ``extraction.characters``, because ``extract:text`` declares
    ``extraction@self`` and the characters are inside it. ``None`` when no name up the chain
    is produced by any atom — the upload's blocks, the walk's, the queue's.
    """
    try:
        entry: Optional[Evidence] = get(name)
    except KeyError:
        return None
    while entry is not None:
        if _declared_producers(entry.name):
            return entry
        entry = REGISTRY.get(entry.within) if entry.within else None
    return None


def producers(name: str) -> set[str]:
    """Every atom that writes ``name``, read out of :data:`ATOMS` and never re-declared.

    Empty for :data:`~jmfts_core.atoms.EXTERNAL_EVIDENCE` and for the three blocks no atom
    produces — which is a fact about them, not a gap. Callers that need to tell "nobody
    writes this" from "the upload writes this" ask :func:`written_by`.
    """
    found = owner(name)
    return _declared_producers(found.name) if found is not None else set()


#: Who writes a name that no atom produces, as the whole sentence :func:`written_by`
#: returns. Keyed by evidence name so that function has one answer per name rather than a
#: branch per case.
_NON_ATOM_WRITERS: dict[str, str] = {
    # "the upload OR a fetch": `SPRINT_JOBS.md` 15.4 S8 gave a document two ways to arrive.
    # An upload holds the bytes and writes both of these itself; a request that names a URL
    # writes `source` instead, and `fetch:*` produces these from it. Naming only the upload
    # would send a reader of a fetched node looking for an upload that never happened.
    "file": "the upload writes it, or one of the fetch tasks",
    "blob": "the upload writes it, or one of the fetch tasks",
    "source": "the request writes it, when a document is named rather than uploaded",
    "options": "the upload writes it",
    "yield": "the settling walk writes it",
    "attempts": "the task queue writes it",
    "child_count": "the tree has it once the children exist",
}


def written_by(name: str) -> str:
    """One line naming who writes ``name``. What ``EXPLAIN`` and the demo print.

    Names the block a leaf was inherited from, because "``extract:text`` writes it" alone
    leaves a reader looking for a ``characters`` the handler never mentions.
    """
    entry = get(name)
    found = owner(name)
    if found is not None:
        wrote = " or ".join(sorted(_declared_producers(found.name)))
        # EXTERNAL first, when a name is both. `SPRINT_JOBS.md` 15.4 S8 gave `file` and
        # `blob` atom producers — the three fetch tasks — without stopping the upload from
        # writing them, so the ordinary way a document arrives has to be named first or a
        # reader of an uploaded node is sent looking for a fetch that never happened.
        if found.name in EXTERNAL_EVIDENCE:
            wrote = f"{_NON_ATOM_WRITERS.get(found.name, 'the request writes it')} ({wrote})"
            if found.name == name:
                return wrote
            return f"{wrote} ({found.store.path or found.name})"
        if found.name == name:
            return f"{wrote} writes it"
        return f"{wrote} writes it ({found.store.path or found.name})"
    while entry is not None:
        known = _NON_ATOM_WRITERS.get(entry.name)
        if known is not None:
            return known
        entry = REGISTRY.get(entry.within) if entry.within else None
    if name in EXTERNAL_EVIDENCE:
        return "the request writes it before any task exists"
    return "nothing writes it"


#: What :func:`resolve` returns for a name that is not there at all. A distinct sentinel
#: rather than ``None``, because 3.2 makes those two different states — ``None`` is a
#: written null and this is "never attempted".
ABSENT = object()


def rows() -> frozenset[str]:
    """Every ``document_evidence`` row name the registry knows.

    Twenty-three of them, which is the count 3.1 and 13.3 both use, and it is derived rather
    than listed for Part 14's reason. Migration 015 carries the same set as SQL because SQL
    cannot import this module, and ``tests/test_evidence_rows.py`` audits that file against
    this function rather than letting the two drift.
    """
    return frozenset(
        entry.store.row
        for entry in REGISTRY.values()
        if entry.store.kind == STORE_EVIDENCE and entry.store.row
    )


def resolve(evidence: Optional[dict], name: str) -> object:
    """Follow ``name``'s store into a map of one node's evidence rows.

    ``evidence`` is ``{row name: value}``, which is what
    :meth:`~jmfts_core.repositories.evidence.EvidenceRepository.read_all` returns. A leaf
    descends into its row's value from there.

    :data:`ABSENT` for a name whose path stops short, and that is the 3.2 distinction the
    walk turns on: a written null is fresh and a missing row was never attempted. Raises for
    a name that is not stored as evidence at all, rather than answering ``ABSENT`` for a
    value that is simply somewhere else — a caller looking for ``text`` here is looking in
    the wrong place and should be told so.
    """
    entry = get(name)
    if entry.store.kind != STORE_EVIDENCE:
        raise ValueError(
            f"evidence {name!r} lives in {entry.store}, not in document_evidence; "
            "resolve cannot answer for it"
        )
    row = entry.store.row or ""
    if not isinstance(evidence, dict) or row not in evidence:
        return ABSENT
    cursor: object = evidence[row]
    for step in (entry.store.path or "").split("."):
        if not step:
            continue
        if not isinstance(cursor, dict) or step not in cursor:
            return ABSENT
        cursor = cursor[step]
    return cursor
