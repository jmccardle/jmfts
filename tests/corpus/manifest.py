"""The manifest: one record per corpus file, and a loader that refuses a bad one.

``docs/OFFICE_SPEC.md`` Part 10 asks for a dataset rather than a pile. The difference is
this file. A pile is a directory of ``.docx`` files somebody once found useful; a dataset
is a set of records that each say what the bytes are, where they came from, what is in
them, and what the appliance is supposed to do with them — so that "coverage" is a number
and "this file is untested" is a query.

### The record

``name``          the file, and its identity in the corpus.
``origin``        ``generated`` (built by ``fixtures.py``) or ``imported`` (came from
                  somewhere else, and therefore needs a URL and a licence that permit it).
``source``        the generator function, or the upstream path within the source project.
``licence``       an SPDX identifier. Never blank: an unlicensed corpus file is a file
                  that has to be deleted later, and later is after it is in the history.
``sha256``/``size``  the bytes. For an imported file this is identity; for a generated one
                  it is a determinism pin, and it is what makes ``fixtures.py`` a
                  reproducible build rather than a script that emits something.
``format``        what :func:`~jmfts_core.probe.detect_format` must answer. Held against
                  the real bytes by the suite, so a record cannot claim a format the
                  appliance does not see. Several of these are deliberately surprising —
                  a truncated ``.docx`` detects as ``zip``, because the archive will not
                  open — and pinning the surprise is the point.
``tags``          the feature vector, drawn from :mod:`tests.corpus.vocabulary`.
``expect``        ``parses`` / ``parses-lossy`` / ``must-reject``.
``minimal``       whether this is a minimal reproduction of one behaviour, or a realistic
                  document that happens to contain it. Both are worth having and they are
                  not interchangeable: a minimal file localises a bug, a realistic one
                  tells you the bug matters.
``note``          why the file is in the corpus. Required, and the shortest thing here
                  that a reviewer actually reads.

### Tags are probe's vocabulary, and the loader is where that is enforced

A tag that :mod:`tests.corpus.vocabulary` does not carry is a :class:`ManifestError`. So
is a tag that is not flag-valued (``page_count`` is a measurement, not a feature), and so
is a tag applied to a format it is not defined for (a ``.docx`` cannot ``is_scanned``).
Those three rules are the whole mechanism by which the corpus's vocabulary and probe's
stay the same vocabulary — without them the manifest is free to invent a word, and a
corpus that invents words is measuring itself.

### Every error, not the first one

:func:`load` collects every problem it finds and raises once, with all of them. A loader
that stopped at the first bad record would turn one broken manifest into a dozen edit-run
cycles, and the failure mode that matters here is a manifest half-updated after a
vocabulary change — which is many errors at once, all of the same shape.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from tests.corpus import fixtures
from tests.corpus.vocabulary import Kind, Term, tags as tag_vocabulary

MANIFEST = Path(__file__).resolve().parent / "manifest.toml"

#: The three answers ``expect`` may give, and what each one asserts.
#:
#: ``parses`` — ingestion produces the whole document.
#: ``parses-lossy`` — ingestion produces a document AND reports named content it did not
#: carry. This is a distinct answer from ``parses`` because ``OFFICE_SPEC.md`` Part 3 is
#: explicit that "a count in the attempt record is the difference between a lossy
#: extraction and an unknown one".
#: ``must-reject`` — ingestion fails with a named reason. Not "extracts to empty", which
#: is the silent failure this project's Fail Early rule exists to forbid.
EXPECTATIONS = ("parses", "parses-lossy", "must-reject")

ORIGINS = ("generated", "imported")

_REQUIRED = ("name", "origin", "source", "licence", "sha256", "size", "format", "expect", "note")
_OPTIONAL = ("tags", "minimal", "url", "declared_mime")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ManifestError(Exception):
    """One or more records are wrong. The message lists all of them."""


@dataclass(frozen=True)
class Record:
    """One corpus file."""

    name: str
    origin: str
    source: str
    licence: str
    sha256: str
    size: int
    format: str
    expect: str
    note: str
    tags: tuple[str, ...] = ()
    minimal: bool = False
    url: str | None = None
    declared_mime: str | None = None

    @property
    def generated(self) -> bool:
        return self.origin == "generated"


@dataclass(frozen=True)
class Corpus:
    """Every record, plus the ways the suite asks for a subset of them."""

    records: tuple[Record, ...]

    def __iter__(self) -> Iterable[Record]:
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)

    def by_name(self, name: str) -> Record:
        for record in self.records:
            if record.name == name:
                return record
        raise KeyError(f"no corpus record named {name!r}")

    def expecting(self, *expectations: str) -> tuple[Record, ...]:
        unknown = set(expectations) - set(EXPECTATIONS)
        if unknown:
            raise ValueError(f"not an expectation: {sorted(unknown)}")
        return tuple(r for r in self.records if r.expect in expectations)

    def tagged(self, tag: str) -> tuple[Record, ...]:
        return tuple(r for r in self.records if tag in r.tags)

    def untagged(self) -> tuple[Record, ...]:
        """Records carrying no feature at all — a pile's worth of file, in a dataset."""
        return tuple(r for r in self.records if not r.tags)

    def bytes_for(self, record: Record, *, root: Path | None = None) -> bytes:
        """The file's bytes. Built for a generated record, read for an imported one.

        ``root`` is where imported files live, and there is no default — ``datasets/`` is
        gitignored and its target is a per-machine choice (see ``docs/CORPUS.md``), so a
        default here would be a hard-coded path on somebody's laptop.
        """
        if record.generated:
            return fixtures.build(record.name)
        if root is None:
            raise ManifestError(
                f"{record.name} is an imported record and no corpus root was given; pass "
                "root=... (see docs/CORPUS.md for where imported files live)"
            )
        path = root / record.name
        if not path.is_file():
            raise ManifestError(
                f"{record.name} is recorded as imported from {record.source} and is not at "
                f"{path}. Fetch it (docs/CORPUS.md) or remove the record — a manifest "
                "entry with no file behind it is coverage this corpus does not have."
            )
        return path.read_bytes()


def _validate(
    raw: dict[str, Any],
    index: int,
    problems: list[str],
    known: dict[str, Term],
) -> Record | None:
    """One record, or None with the reasons appended to ``problems``."""
    before = len(problems)
    where = f"record {index}"
    name = raw.get("name")
    if isinstance(name, str) and name:
        where = f"{name!r}"

    missing = [key for key in _REQUIRED if key not in raw]
    if missing:
        problems.append(f"{where}: missing {missing}")
        return None
    unknown = sorted(set(raw) - set(_REQUIRED) - set(_OPTIONAL))
    if unknown:
        problems.append(f"{where}: unknown keys {unknown}")
        return None

    if raw["origin"] not in ORIGINS:
        problems.append(f"{where}: origin {raw['origin']!r} is not one of {list(ORIGINS)}")
    if raw["expect"] not in EXPECTATIONS:
        problems.append(f"{where}: expect {raw['expect']!r} is not one of {list(EXPECTATIONS)}")
    if not _SHA256.match(str(raw["sha256"])):
        problems.append(f"{where}: sha256 is not 64 lowercase hex digits")
    if not isinstance(raw["size"], int) or raw["size"] < 0:
        problems.append(f"{where}: size must be a non-negative integer")
    for key in ("source", "licence", "note", "format"):
        if not isinstance(raw[key], str) or not raw[key].strip():
            problems.append(f"{where}: {key} must be a non-empty string")
    if raw["origin"] == "imported" and not raw.get("url"):
        problems.append(
            f"{where}: an imported record needs a url — provenance that cannot be "
            "followed back is not provenance"
        )
    if raw["origin"] == "generated" and raw["name"] not in fixtures.GENERATORS:
        problems.append(f"{where}: generated, and fixtures.GENERATORS has no entry for it")

    tags = raw.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        problems.append(f"{where}: tags must be a list of strings")
        tags = []
    if len(set(tags)) != len(tags):
        problems.append(f"{where}: tags contains a duplicate")

    for tag in tags:
        term = known.get(tag)
        if term is None:
            problems.append(
                f"{where}: tag {tag!r} is not in the pattern vocabulary. Corpus tags are "
                "probe's patterns (OFFICE_SPEC Part 10); if this is a real feature, add "
                "the term to tests/corpus/vocabulary.py with the status that says who "
                "knows it, and propose the Part 2 row in docs/CORPUS.md."
            )
            continue
        if term.kind is not Kind.FLAG:
            problems.append(f"{where}: tag {tag!r} is a {term.kind.value}, not a feature flag")
        if "*" not in term.formats and raw["format"] not in term.formats:
            problems.append(
                f"{where}: tag {tag!r} is defined for {list(term.formats)} and this record "
                f"is {raw['format']!r}"
            )

    minimal = raw.get("minimal", False)
    if not isinstance(minimal, bool):
        problems.append(f"{where}: minimal must be a boolean")

    if len(problems) > before:
        return None
    return Record(
        name=raw["name"],
        origin=raw["origin"],
        source=raw["source"],
        licence=raw["licence"],
        sha256=raw["sha256"],
        size=raw["size"],
        format=raw["format"],
        expect=raw["expect"],
        note=raw["note"],
        tags=tuple(tags),
        minimal=bool(minimal),
        url=raw.get("url"),
        declared_mime=raw.get("declared_mime"),
    )


def load(path: Path = MANIFEST, *, require_all_generators: bool = True) -> Corpus:
    """Every record in ``path``, or a :class:`ManifestError` listing what is wrong.

    ``require_all_generators`` is the shipped manifest's rule: every fixture ``fixtures.py``
    can build must have a record. It is a parameter and not a constant because the loader's
    own tests load one-record manifests, and a check that fired on those could only be
    tested by not testing it.
    """
    document = tomllib.loads(path.read_text(encoding="utf-8"))
    raw_records = document.get("file")
    if not isinstance(raw_records, list) or not raw_records:
        raise ManifestError(f"{path} has no [[file]] records")

    known = tag_vocabulary()
    problems: list[str] = []
    records: list[Record] = []
    for index, raw in enumerate(raw_records):
        record = _validate(raw, index, problems, known)
        if record is not None:
            records.append(record)

    seen_names: set[str] = set()
    seen_hashes: dict[str, str] = {}
    for record in records:
        if record.name in seen_names:
            problems.append(f"{record.name!r}: recorded twice")
        seen_names.add(record.name)
        first = seen_hashes.get(record.sha256)
        if first is not None:
            problems.append(
                f"{record.name!r} has the same bytes as {first!r}; one of the two is a "
                "duplicate and the corpus should carry it once"
            )
        seen_hashes[record.sha256] = record.name

    unrecorded = sorted(set(fixtures.GENERATORS) - seen_names) if require_all_generators else []
    if unrecorded:
        problems.append(
            f"fixtures.py builds {unrecorded} and the manifest does not record them. A "
            "fixture with no record is a file with no expected behaviour, which is the "
            "pile Part 10 is trying not to be."
        )

    if problems:
        raise ManifestError(
            f"{path} has {len(problems)} problem(s):\n  - " + "\n  - ".join(problems)
        )
    return Corpus(records=tuple(records))


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
