"""One vocabulary, three states of knowing it.

``docs/OFFICE_SPEC.md`` Part 10 makes a claim this module exists to hold:

    "The corpus needs a per-file feature vector to be a dataset rather than a pile. JMFTS
    already computes one: ``matched.patterns``. The pattern vocabulary of Part 2 **is** the
    corpus's tag vocabulary, and extending one extends the other."

The value of that claim is entirely in the word *is*. A corpus that keeps its own list of
feature names next to probe's list has two lists, and two lists diverge — quietly, in the
direction that makes coverage look better than it is. So this module never writes down a
name that probe already knows. It **runs probe** and reads the keys back out.

### Why the vocabulary is obtained by execution

``jmfts_core.probe`` has no exported list of pattern names. The names are the keys of the
dicts ``_probe_pdf`` and ``_probe_text`` return, built inline. There are two ways to
recover them and only one of them is safe: reading the source and matching string literals
would produce a list that is right until the day it is not, whereas calling
:func:`~jmfts_core.probe.probe_patterns` on real bytes produces the names probe *actually
emits*, for the version of probe that is installed. :data:`SPECIMENS` is what makes that
possible — one input per format in ``PROBERS_AVAILABLE``, and a missing one is an error
rather than a smaller vocabulary.

Both current probers emit a fixed key set regardless of content, so one specimen per format
is sufficient today. That is a property of those two functions and not a rule, so the
specimen list is a list: a future prober that emits a key only for some documents needs an
input here that exercises the branch, and :func:`probe_vocabulary` takes the union.

### Why a tag is not every pattern

Probe's patterns are a mix of flags and measurements — ``has_outline`` is a feature, and
``page_count`` is a number. A corpus tag is a claim about what a file *contains*, so only
the flags can be tags: ``tags = ["page_count"]`` would mean nothing. The split is not
declared here either. It is read off the runtime type of the probed value, which is why
:class:`Kind` has exactly two members and :func:`probe_vocabulary` returns them.

### The three statuses, and the ratchet on each

``PROBED``
    probe emits it now. Held against the live vocabulary in both directions: every probed
    term must be emitted, and **every emitted key must be a term here**. The second half is
    the one that matters — it means a new pattern cannot be added to probe without this
    table being updated, which is the drift the corpus exists to prevent.

``PLANNED``
    ``OFFICE_SPEC.md`` Part 2 names it and Part 11 step 4 will implement it. Held against
    the spec's own tables by :func:`spec_patterns`, so a pattern added to Part 2 fails this
    suite until the corpus carries it. Also held against probe: a planned term that probe
    has *started* emitting must be promoted to ``PROBED``, or the table would be describing
    the past.

``PROPOSED``
    **This corpus needs it and no spec section names it yet.** Every one of them is a
    container-level hazard from Part 10's fixture generator: zip-slip, duplicate members,
    a missing ``[Content_Types].xml``, entity expansion, a truncated archive. Part 2's
    pattern list covers what an office document *declares* and says nothing about whether
    its ZIP is honest, so the corpus can label those files and probe cannot yet measure
    them. That gap is real, it is reported by :func:`coverage`, and ``docs/CORPUS.md``
    proposes the Part 2 rows that would close it. Naming them here is what keeps the gap
    countable instead of invisible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from jmfts_core.probe import PROBERS_AVAILABLE, detect_format, probe_patterns
from tests.corpus.fixtures import minimal_docx, minimal_pdf, minimal_pptx, minimal_xlsx

REPO = Path(__file__).resolve().parents[2]
OFFICE_SPEC = REPO / "docs" / "OFFICE_SPEC.md"


class Kind(str, Enum):
    """What a pattern's value is. Only a flag can be a corpus tag."""

    FLAG = "flag"
    MEASUREMENT = "measurement"


class Status(str, Enum):
    """Who knows this name today."""

    PROBED = "probed"
    PLANNED = "planned"
    PROPOSED = "proposed"


@dataclass(frozen=True)
class Term:
    """One pattern name, and the reason it is in the vocabulary."""

    name: str
    kind: Kind
    status: Status
    #: Formats the term is meaningful for. ``("*",)`` means every format.
    formats: tuple[str, ...]
    #: Where the name comes from — a spec section, or the function that emits it.
    source: str


class VocabularyError(Exception):
    """The vocabulary and the code that owns it disagree."""


# ---------------------------------------------------------------------------
# Reading the live vocabulary out of probe
# ---------------------------------------------------------------------------


def specimens() -> dict[str, list[bytes]]:
    """One or more inputs per format in ``PROBERS_AVAILABLE``.

    The ratchet described in the module docstring: the day a format joins
    ``PROBERS_AVAILABLE``, the vocabulary this harness reports goes quietly incomplete
    unless an input joins with it. :func:`uncovered_formats` is what notices, and
    ``test_vocabulary.py`` is what fails.

    **It reports rather than raises, and that is a deliberate change from the first
    version of this function.** Raising here fired during COLLECTION — ``vocabulary()``
    is called at module scope by three test modules — so a missing specimen did not fail
    one test, it interrupted the run and took ~1500 unrelated tests with it. That is the
    same blast radius ``testpaths`` was added to ``pyproject.toml`` to stop, arriving from
    a different direction. The ratchet is right; taking the suite down with it was not.
    """
    # `_ole2` lives in the probe's own test module, which owns the only compound-file
    # WRITER in the tree — ~110 lines of sector, FAT and directory packing. Importing it
    # is worse than a shared fixture module and better than a second copy that can
    # disagree with the first about the format. Imported here rather than at module scope
    # so that this module stays importable if that test file is ever moved.
    from tests.test_probe_office import _ole2

    return {
        "text": [
            b"# A heading\n\nA paragraph of prose.\n",
            b"<html><body><p>markup, not prose</p></body></html>\n",
        ],
        "pdf": [minimal_pdf()],
        # The minimal packages are enough BECAUSE the vocabulary is the set of pattern
        # NAMES, not their values. A prober that omits a name it could not measure —
        # which is how `probe` reports an unmeasurable pattern — is the one case a
        # minimal specimen would under-report, so richer inputs are added alongside as
        # each such pattern appears, not instead of these.
        "docx": [minimal_docx()],
        "pptx": [minimal_pptx()],
        "xlsx": [minimal_xlsx()],
        # A legacy binary rather than an encrypted package: both are `ole2`, and this one
        # exercises the branch that names an application. The encrypted branch is covered
        # by tests/test_probe_office.py, which owns that assertion.
        "ole2": [_ole2({"WordDocument": b"\x00" * 32})],
    }


def uncovered_formats() -> list[str]:
    """Formats ``probe`` can look inside that :func:`specimens` has no input for."""
    return [fmt for fmt in PROBERS_AVAILABLE if fmt not in specimens()]


def probe_vocabulary() -> dict[str, Kind]:
    """Every pattern name probe emits, mapped to what kind of value it carries.

    Derived by running :func:`~jmfts_core.probe.probe_patterns` over :func:`specimens`,
    so it is the installed probe's vocabulary and not a description of it.
    """
    found: dict[str, Kind] = {}
    for fmt, blobs in specimens().items():
        for blob in blobs:
            detection = detect_format(blob, filename=f"specimen.{fmt}")
            if detection.format != fmt:
                raise VocabularyError(
                    f"the {fmt!r} specimen is detected as {detection.format!r}; it cannot "
                    "exercise the prober it was written for"
                )
            patterns, _detail = probe_patterns(blob, detection)
            for name, value in patterns.items():
                kind = Kind.FLAG if isinstance(value, bool) else Kind.MEASUREMENT
                previous = found.get(name)
                if previous is not None and previous is not kind:
                    raise VocabularyError(
                        f"probe emits {name!r} as a {previous.value} for one input and a "
                        f"{kind.value} for another; a tag cannot be both"
                    )
                found[name] = kind
    return found


# ---------------------------------------------------------------------------
# Reading the planned vocabulary out of the spec
# ---------------------------------------------------------------------------

_PART_HEADING = re.compile(r"^## Part (\d+)", re.MULTILINE)
_BACKTICKED = re.compile(r"`([a-z][a-z0-9_]*)`")


def spec_part(number: int, text: str | None = None) -> str:
    """The body of one ``## Part N`` section of ``OFFICE_SPEC.md``."""
    text = text if text is not None else OFFICE_SPEC.read_text(encoding="utf-8")
    bounds = [(int(m.group(1)), m.start(), m.end()) for m in _PART_HEADING.finditer(text)]
    for index, (part, _start, end) in enumerate(bounds):
        if part == number:
            stop = bounds[index + 1][1] if index + 1 < len(bounds) else len(text)
            return text[end:stop]
    raise VocabularyError(f"OFFICE_SPEC.md has no '## Part {number}'")


def spec_patterns(text: str | None = None) -> set[str]:
    """Pattern names named by Part 2's tables.

    Part 2 has two tables with different shapes — one is ``| Format | Pattern | How |`` and
    the other ``| Pattern | Formats | Why |`` — so the column is located by its header
    rather than by position, and a cell holding two names (``part_count, unknown_parts``)
    contributes both.
    """
    found: set[str] = set()
    column: int | None = None
    for line in spec_part(2, text).splitlines():
        line = line.strip()
        if not line.startswith("|"):
            column = None
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if column is None:
            if "Pattern" in cells:
                column = cells.index("Pattern")
            continue
        if set(cells[0]) <= {"-", ":"} and cells[0]:
            continue  # the ``|---|---|`` separator row
        if column < len(cells):
            found.update(_BACKTICKED.findall(cells[column]))
    if not found:
        raise VocabularyError(
            "no pattern names were found in OFFICE_SPEC.md Part 2; either the section was "
            "restructured or this parser is reading the wrong column"
        )
    return found


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------


def _terms() -> tuple[Term, ...]:
    """The declared table. Probed kinds come from probe, so they cannot be misdeclared."""
    live = probe_vocabulary()
    unplaced = sorted(set(live) - set(_PROBED_FORMATS))
    if unplaced:
        raise VocabularyError(
            f"probe emits {unplaced} and tests/corpus/vocabulary.py does not say which "
            "formats they apply to. A new pattern is a new corpus tag: add it to "
            "_PROBED_FORMATS (and promote it out of _PLANNED/_PROPOSED if it is there)."
        )
    probed = tuple(
        Term(
            name=name,
            kind=kind,
            status=Status.PROBED,
            formats=_PROBED_FORMATS[name],
            source="jmfts_core.probe.probe_patterns",
        )
        for name, kind in sorted(live.items())
    )
    # Promotion is DERIVED, not declared. "planned" means the spec names a pattern and
    # probe does not emit it yet; "proposed" means neither the spec nor probe has it. Both
    # are statements about what probe currently does, so the moment probe starts emitting
    # one, it simply IS probed — subtracting here is what makes that true.
    #
    # It used to be a manual step, enforced by the duplicate-name check in `vocabulary()`.
    # That check fired the instant the office probers landed: every pattern OFFICE_SPEC
    # Part 2 had listed as planned became probed at once, and the error demanded a hand
    # edit for a promotion carrying no judgement at all. The duplicate check is kept — it
    # still catches a name declared twice within `_PLANNED` or `_PROPOSED`, which IS a
    # mistake — but a promotion is no longer one of the things it can catch.
    emitted = {term.name for term in probed}
    planned = tuple(term for term in _PLANNED if term.name not in emitted)
    proposed = tuple(term for term in _PROPOSED if term.name not in emitted)
    return probed + planned + proposed


#: The three OOXML formats. Spelled once because the package-level patterns below —
#: macros, images, part inventory — are read out of the ZIP container and are therefore
#: the same question for all three, while the heading, slide and sheet patterns are not.
_OOXML: tuple[str, ...] = ("docx", "pptx", "xlsx")

#: Which formats each probed pattern is emitted for. Declared, because probe reports the
#: name and not its applicability — and a corpus record tagging a ``.docx`` with a
#: PDF-only pattern is a labelling error worth catching.
_PROBED_FORMATS: dict[str, tuple[str, ...]] = {
    "has_text_layer": ("pdf", "text"),
    "has_outline": ("pdf",),
    "outline_depth": ("pdf",),
    "has_images": ("*",),
    "image_count": ("pdf",) + _OOXML,
    "page_count": ("pdf",),
    "is_scanned": ("pdf",),
    "is_damaged": ("*",),
    "has_headings": ("text",),
    "heading_count": ("text",),
    "max_heading_level": ("text",),
    "has_markup": ("text",),
    "char_count": ("text",),
    "line_count": ("text",),
    # --- the office probers (OFFICE_SPEC.md Part 11 step 4) -----------------------
    # Container-level: the same question for every OOXML package, because all three are
    # read out of the ZIP manifest rather than out of the format's own parts.
    "has_macros": _OOXML,
    "part_count": _OOXML,
    "unknown_parts": _OOXML,
    "unknown_part_count": _OOXML,
    # Format-level. `has_tables` and `has_comments` each cover two formats and not the
    # third, which is why they are listed rather than folded into _OOXML: a spreadsheet
    # has no tables in this sense, and a deck carries no comments probe can see.
    "has_tables": ("docx", "pptx"),
    "has_comments": ("docx", "xlsx"),
    "has_tracked_changes": ("docx",),
    "has_slides": ("pptx",),
    "slide_count": ("pptx",),
    "has_smartart": ("pptx",),
    "has_speaker_notes": ("pptx",),
    "has_sheets": ("xlsx",),
    "sheet_count": ("xlsx",),
    # OLE2 carries two unrelated things under one magic number, and these three are how
    # they are told apart. See OFFICE_SPEC.md Part 1.
    "is_encrypted": ("ole2",),
    "is_legacy_binary": ("ole2",),
    "legacy_application": ("ole2",),
}

#: Named by ``OFFICE_SPEC.md`` Part 2, implemented by Part 11 step 4. ``has_images`` is in
#: Part 2's table too and is NOT repeated here: probe already emits it, so it is probed.
_PLANNED: tuple[Term, ...] = (
    Term("has_heading_styles", Kind.FLAG, Status.PLANNED, ("docx",), "OFFICE_SPEC Part 2"),
    Term("has_slides", Kind.FLAG, Status.PLANNED, ("pptx",), "OFFICE_SPEC Part 2"),
    Term("has_sheets", Kind.FLAG, Status.PLANNED, ("xlsx",), "OFFICE_SPEC Part 2"),
    Term("is_encrypted", Kind.FLAG, Status.PLANNED, ("ole2",), "OFFICE_SPEC Part 2"),
    Term("is_legacy_binary", Kind.FLAG, Status.PLANNED, ("ole2",), "OFFICE_SPEC Part 2"),
    Term("has_macros", Kind.FLAG, Status.PLANNED, ("docx", "pptx", "xlsx"), "OFFICE_SPEC Part 2"),
    Term("has_tracked_changes", Kind.FLAG, Status.PLANNED, ("docx",), "OFFICE_SPEC Part 2"),
    Term("has_comments", Kind.FLAG, Status.PLANNED, ("docx", "xlsx"), "OFFICE_SPEC Part 2"),
    Term("has_tables", Kind.FLAG, Status.PLANNED, ("docx", "pptx"), "OFFICE_SPEC Part 2"),
    Term("has_smartart", Kind.FLAG, Status.PLANNED, ("pptx",), "OFFICE_SPEC Part 2"),
    Term("has_speaker_notes", Kind.FLAG, Status.PLANNED, ("pptx",), "OFFICE_SPEC Part 2"),
    Term("part_count", Kind.MEASUREMENT, Status.PLANNED, ("*",), "OFFICE_SPEC Part 2"),
    Term("unknown_parts", Kind.MEASUREMENT, Status.PLANNED, ("*",), "OFFICE_SPEC Part 2"),
)

#: Needed by Part 10's fixture generator and named by no spec section. See the module
#: docstring: these are container-level facts, and Part 2 is about document-level ones.
#: ``docs/CORPUS.md`` carries the proposed Part 2 rows.
_PROPOSED: tuple[Term, ...] = (
    Term("has_unsafe_member_paths", Kind.FLAG, Status.PROPOSED, ("*",), "OFFICE_SPEC Part 10"),
    Term("has_duplicate_members", Kind.FLAG, Status.PROPOSED, ("*",), "OFFICE_SPEC Part 10"),
    Term("has_content_types", Kind.FLAG, Status.PROPOSED, ("docx", "pptx", "xlsx"), "Part 10"),
    Term("has_entity_declaration", Kind.FLAG, Status.PROPOSED, ("*",), "OFFICE_SPEC Part 10"),
    Term("has_external_entity", Kind.FLAG, Status.PROPOSED, ("*",), "OFFICE_SPEC Part 10"),
    Term("has_deep_nesting", Kind.FLAG, Status.PROPOSED, ("*",), "OFFICE_SPEC Part 10"),
    Term("has_byte_order_mark", Kind.FLAG, Status.PROPOSED, ("*",), "OFFICE_SPEC Part 10"),
    Term("has_invalid_encoding", Kind.FLAG, Status.PROPOSED, ("*",), "OFFICE_SPEC Part 10"),
    Term("has_extreme_compression", Kind.FLAG, Status.PROPOSED, ("*",), "OFFICE_SPEC Part 10"),
    Term("declared_type_disagrees", Kind.FLAG, Status.PROPOSED, ("*",), "OFFICE_SPEC Part 10"),
    Term("compression_methods", Kind.MEASUREMENT, Status.PROPOSED, ("*",), "OFFICE_SPEC Part 10"),
    Term("member_count", Kind.MEASUREMENT, Status.PROPOSED, ("*",), "OFFICE_SPEC Part 10"),
)


def vocabulary() -> dict[str, Term]:
    """Every name the corpus may use, keyed by name."""
    terms = _terms()
    table: dict[str, Term] = {}
    for term in terms:
        if term.name in table:
            raise VocabularyError(
                f"{term.name!r} is declared twice — as {table[term.name].status.value} and "
                f"as {term.status.value}"
            )
        table[term.name] = term
    return table


def tags() -> dict[str, Term]:
    """The flag-valued half of :func:`vocabulary` — the names a manifest may tag with."""
    return {name: term for name, term in vocabulary().items() if term.kind is Kind.FLAG}


def coverage() -> dict[Status, list[str]]:
    """Tag names by status, for the report ``scripts/corpus_fixtures.py`` prints.

    The ``PROPOSED`` list is the honest headline: those are the features the corpus can
    label and shipped code cannot yet measure.
    """
    grouped: dict[Status, list[str]] = {status: [] for status in Status}
    for name, term in sorted(tags().items()):
        grouped[term.status].append(name)
    return grouped
