"""The manifest has to describe the bytes, or it is documentation.

``docs/OFFICE_SPEC.md`` Part 10 wants a dataset: a set of files each of which has a
recorded provenance, a recorded feature vector, and a recorded expectation. Every one of
those is a claim about bytes, and a claim about bytes that nothing checks is a comment.

So this file checks them. ``sha256`` and ``size`` are held against what
``fixtures.py`` builds, which turns the generator into a reproducible build — a change in
zlib, in ``zipfile``'s header defaults, or in a fixture body shows up here as a named hash
mismatch instead of as a mystery two lanes later. ``format`` is held against
``jmfts_core.probe.detect_format``, which is the strongest single check in the file: it
means a record cannot claim a file is a ``.docx`` that the appliance does not see as one,
and several records deliberately pin the case where those differ.

The loader's own refusals are tested too, against synthetic manifests rather than the real
one. Fail Early is not a property of a validator that has only ever seen valid input.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from jmfts_core.probe import detect_format
from tests.corpus import fixtures
from tests.corpus.manifest import (
    EXPECTATIONS,
    MANIFEST,
    ManifestError,
    load,
    sha256,
)

CORPUS = load()
RECORDS = CORPUS.records


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "manifest.toml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _load_synthetic(tmp_path: Path, body: str):
    """A one-record manifest. ``require_all_generators`` is the shipped manifest's rule."""
    return load(_write(tmp_path, body), require_all_generators=False)


VALID = """
    [[file]]
    name = "minimal.docx"
    origin = "generated"
    source = "tests/corpus/fixtures.py:minimal_docx"
    licence = "MIT"
    sha256 = "{sha}"
    size = {size}
    format = "docx"
    tags = ["has_content_types"]
    expect = "parses"
    note = "the control"
"""


def _valid_body() -> str:
    data = fixtures.build("minimal.docx")
    return VALID.format(sha=sha256(data), size=len(data))


# ---------------------------------------------------------------------------
# The real manifest against the real bytes
# ---------------------------------------------------------------------------


def test_the_manifest_loads():
    assert len(CORPUS) >= 20, f"{len(CORPUS)} records; the corpus has shrunk"


@pytest.mark.parametrize("record", RECORDS, ids=lambda r: r.name)
def test_the_recorded_bytes_are_the_built_bytes(record):
    """The determinism pin. A moved hash means the generator changed — say why."""
    data = CORPUS.bytes_for(record)
    assert len(data) == record.size, f"{record.name} is {len(data)} bytes, recorded {record.size}"
    assert sha256(data) == record.sha256, (
        f"{record.name} does not hash to its recorded value. Either a fixture body changed "
        "(update the record, in the same commit) or the build stopped being deterministic "
        "(a timestamp, a mode, or a zlib upgrade), which is the more interesting answer."
    )


@pytest.mark.parametrize("record", RECORDS, ids=lambda r: r.name)
def test_probe_detects_the_recorded_format(record):
    """The manifest may not claim a format the appliance does not see.

    Several records pin a surprise here — ``truncated.docx`` detects as ``zip`` because the
    archive will not open, ``pdf-named.docx`` as ``pdf`` because the magic bytes say so —
    and pinning the surprise is the point. A change to ``detect_format`` that "fixed" one
    of them would be a change to what the appliance believes about a file, and it should
    have to touch this manifest to make it.
    """
    detection = detect_format(
        CORPUS.bytes_for(record),
        filename=record.name,
        declared_mime=record.declared_mime,
    )
    assert detection.format == record.format, (
        f"{record.name}: recorded as {record.format!r}, detected as {detection.format!r} "
        f"(by {detection.detected_by!r})"
    )


@pytest.mark.parametrize("record", RECORDS, ids=lambda r: r.name)
def test_no_fixture_is_larger_than_the_repository_will_carry(record):
    assert record.size <= fixtures.MAX_FIXTURE_BYTES, record.size


def test_the_declared_type_disagreement_is_real():
    """``declared_type_disagrees`` is a proposed tag; the file behind it is not proposed."""
    record = CORPUS.by_name("pdf-named.docx")
    assert "declared_type_disagrees" in record.tags
    detection = detect_format(
        CORPUS.bytes_for(record), filename=record.name, declared_mime=record.declared_mime
    )
    assert detection.mime_agrees is False
    assert detection.detected_mime == "application/pdf"


def test_every_expectation_is_represented():
    """A corpus with no ``must-reject`` file cannot test a rejecter, and vice versa."""
    for expectation in EXPECTATIONS:
        assert CORPUS.expecting(expectation), f"no corpus file expects {expectation!r}"


def test_no_record_is_untagged_except_the_one_with_nothing_in_it():
    """An untagged file is a query, not an audit — Part 10's phrase.

    ``empty.zip`` is the exception and is allowed to be: a file with no members has no
    features, and saying so is the record's content.
    """
    assert [r.name for r in CORPUS.untagged()] == ["empty.zip"]


def test_every_generated_record_names_a_real_generator():
    for record in RECORDS:
        if record.generated:
            assert record.name in fixtures.GENERATORS


def test_every_record_carries_a_licence_and_a_reason():
    for record in RECORDS:
        assert record.licence.strip(), record.name
        assert len(record.note.strip()) > 40, (
            f"{record.name}: the note is the only part of a corpus record anyone reads; "
            "'why this file exists' does not fit in a few words"
        )


# ---------------------------------------------------------------------------
# The loader's refusals
# ---------------------------------------------------------------------------


def test_a_valid_synthetic_manifest_loads(tmp_path):
    """The control for the refusals below."""
    corpus = _load_synthetic(tmp_path, _valid_body())
    assert len(corpus) == 1


def test_a_tag_probe_does_not_know_is_an_error(tmp_path):
    """The rule that keeps the two vocabularies one vocabulary."""
    body = _valid_body().replace('["has_content_types"]', '["has_vibes"]')
    with pytest.raises(ManifestError) as raised:
        _load_synthetic(tmp_path, body)
    assert "has_vibes" in str(raised.value)
    assert "pattern vocabulary" in str(raised.value)


def test_a_measurement_is_not_a_tag(tmp_path):
    body = _valid_body().replace('["has_content_types"]', '["page_count"]')
    with pytest.raises(ManifestError) as raised:
        _load_synthetic(tmp_path, body)
    assert "measurement" in str(raised.value)


def test_a_tag_from_another_format_is_an_error(tmp_path):
    """``is_scanned`` is a PDF pattern. A ``.docx`` cannot have it."""
    body = _valid_body().replace('["has_content_types"]', '["is_scanned"]')
    with pytest.raises(ManifestError) as raised:
        _load_synthetic(tmp_path, body)
    assert "is_scanned" in str(raised.value)
    assert "defined for" in str(raised.value)


def test_an_unknown_expectation_is_an_error(tmp_path):
    body = _valid_body().replace('expect = "parses"', 'expect = "probably fine"')
    with pytest.raises(ManifestError):
        _load_synthetic(tmp_path, body)


def test_a_missing_field_is_an_error(tmp_path):
    body = "\n".join(
        line for line in _valid_body().splitlines() if not line.strip().startswith("licence")
    )
    with pytest.raises(ManifestError) as raised:
        _load_synthetic(tmp_path, body)
    assert "licence" in str(raised.value)


def test_an_imported_record_without_a_url_is_an_error(tmp_path):
    """Provenance that cannot be followed back is not provenance."""
    body = _valid_body().replace('origin = "generated"', 'origin = "imported"')
    with pytest.raises(ManifestError) as raised:
        _load_synthetic(tmp_path, body)
    assert "url" in str(raised.value)


def test_a_fixture_with_no_record_is_an_error(tmp_path):
    """The generator and the manifest are held together in both directions."""
    with pytest.raises(ManifestError) as raised:
        load(_write(tmp_path, _valid_body()))
    # The synthetic manifest records one of twenty generators, so every other one is
    # reported as unrecorded — which is the check, stated from the other side.
    assert "fixtures.py builds" in str(raised.value)


def test_every_problem_is_reported_at_once(tmp_path):
    """One bad edit to the vocabulary breaks many records; naming one at a time is cruel."""
    body = _valid_body().replace('["has_content_types"]', '["has_vibes", "page_count"]')
    with pytest.raises(ManifestError) as raised:
        _load_synthetic(tmp_path, body)
    message = str(raised.value)
    assert "has_vibes" in message and "page_count" in message


def test_an_imported_record_with_no_file_says_where_it_should_be(tmp_path):
    """The one path a real corpus fetch will take, exercised without fetching anything."""
    body = (
        _valid_body()
        .replace('origin = "generated"', 'origin = "imported"')
        .replace(
            'note = "the control"', 'url = "https://example.invalid/x.docx"\nnote = "the control"'
        )
    )
    corpus = _load_synthetic(tmp_path, body)
    with pytest.raises(ManifestError) as raised:
        corpus.bytes_for(corpus.by_name("minimal.docx"), root=tmp_path / "nowhere")
    assert "docs/CORPUS.md" in str(raised.value)


def test_the_shipped_manifest_is_the_one_the_suite_reads():
    assert MANIFEST.is_file()
    assert MANIFEST.name == "manifest.toml"
