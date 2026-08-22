"""A fixture that does not contain its attack is a test that passes for the wrong reason.

``docs/OFFICE_SPEC.md`` Part 10 declines to carry a malware corpus and says the structural
attacks can be synthesized instead. That trade is only sound if the synthesis is checked:
a ``zip-slip`` fixture whose hostile member name was normalised away by ``zipfile``, or an
entity-expansion fixture whose ``DOCTYPE`` never made it into the part, would sit in the
corpus proving that the appliance handles an attack it was never shown.

So each fixture is opened here and the thing it claims to be is found in it — by direct
inspection of the archive and the part bytes, not by asking a checker. That distinction
matters: a checker written here would be the parallel tool this lane exists to avoid, and
it would agree with the generator because the same person wrote both. Finding
``../`` in a member name is not a judgement, it is a fact about bytes.

The XML fixtures are additionally run through :mod:`tests.corpus.xmlsafe`, which is where
this repository's answer to Part 10's "``defusedxml``, or ``lxml`` with
``resolve_entities=False``" lives while neither library is installed. Those four cases —
entity expansion, external entity, deep nesting, invalid UTF-8 — are the reason that module
exists, and the two that must PARSE (a byte order mark, and an ordinary document) are what
keep it from being a parser that refuses everything.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from tests.corpus import fixtures, xmlsafe
from tests.corpus.manifest import load, sha256

CORPUS = load()


def _members(name: str) -> list[zipfile.ZipInfo]:
    with zipfile.ZipFile(io.BytesIO(fixtures.build(name))) as archive:
        return archive.infolist()


def _part(name: str, part: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(fixtures.build(name))) as archive:
        return archive.read(part)


# ---------------------------------------------------------------------------
# The build itself
# ---------------------------------------------------------------------------


def test_the_build_is_deterministic():
    """Twice in one process. The manifest hashes are the across-machines half."""
    first = fixtures.build_all()
    second = fixtures.build_all()
    differing = sorted(name for name in first if first[name] != second[name])
    assert not differing, f"{differing} differ between two builds in the same process"


def test_every_generator_has_a_record_and_every_record_a_generator():
    assert set(fixtures.GENERATORS) == {r.name for r in CORPUS.records if r.generated}


def test_the_size_limit_is_enforced_by_the_generator(monkeypatch):
    """The 100 KB rule, held where it can be held rather than in a review checklist."""
    monkeypatch.setitem(fixtures.GENERATORS, "oversized.zip", lambda: b"x" * (200 * 1024))
    with pytest.raises(fixtures.FixtureTooLarge):
        fixtures.build("oversized.zip")


def test_an_unknown_fixture_name_names_the_drift():
    with pytest.raises(KeyError, match="drifted"):
        fixtures.build("no-such-fixture.docx")


def test_pack_refuses_an_accidental_duplicate():
    """The one fixture that wants duplicates asks for them; nothing else can produce one."""
    members = [fixtures.Member("a.xml", b"1"), fixtures.Member("a.xml", b"2")]
    with pytest.raises(AssertionError, match="allow_duplicates"):
        fixtures.pack(members)
    assert fixtures.pack(members, allow_duplicates=True)


def test_writing_the_corpus_to_disk_reproduces_the_recorded_bytes(tmp_path):
    """``write_all`` is what a reader that needs real files uses; it must not transform."""
    written = fixtures.write_all(tmp_path / "corpus")
    assert set(written) == set(fixtures.GENERATORS)
    for record in CORPUS.records:
        assert sha256(written[record.name].read_bytes()) == record.sha256


# ---------------------------------------------------------------------------
# Container-level attacks: the bytes contain what the record claims
# ---------------------------------------------------------------------------


def test_zip_slip_relative_escapes_the_extraction_root():
    names = [info.filename for info in _members("zip-slip-relative.docx")]
    escaping = [name for name in names if ".." in name.split("/")]
    assert escaping == ["../../../../tmp/jmfts-zip-slip.txt"]
    assert "word/document.xml" in names, "the package must otherwise be a valid docx"


def test_zip_slip_absolute_has_no_dot_dot_to_filter():
    names = [info.filename for info in _members("zip-slip-absolute.zip")]
    absolute = [name for name in names if name.startswith("/")]
    assert absolute == ["/etc/cron.d/jmfts-zip-slip"]
    assert not any(".." in name for name in names), (
        "the absolute-path fixture must not also contain a traversal, or it stops "
        "distinguishing a filter that only checks for '..'"
    )


def test_duplicate_entries_really_are_two_members_with_different_content():
    infos = _members("duplicate-entries.docx")
    duplicated = [i for i in infos if i.filename == "word/document.xml"]
    assert len(duplicated) == 2
    with zipfile.ZipFile(io.BytesIO(fixtures.build("duplicate-entries.docx"))) as archive:
        first, second = (archive.read(info) for info in duplicated)
        assert first != second, "two identical copies would not be ambiguous"
        # zipfile resolves the NAME to the last entry. Other readers take the first, and
        # that disagreement is the whole hazard.
        assert archive.read("word/document.xml") == second


def test_missing_content_types_is_missing_exactly_that():
    names = [info.filename for info in _members("missing-content-types.docx")]
    assert "[Content_Types].xml" not in names
    assert "word/document.xml" in names


def test_stored_and_deflated_hold_the_same_parts_by_different_methods():
    stored = fixtures.build("minimal.docx")
    deflated = fixtures.build("deflated.docx")
    with zipfile.ZipFile(io.BytesIO(stored)) as a, zipfile.ZipFile(io.BytesIO(deflated)) as b:
        assert [i.filename for i in a.infolist()] == [i.filename for i in b.infolist()]
        assert {i.compress_type for i in a.infolist()} == {zipfile.ZIP_STORED}
        assert {i.compress_type for i in b.infolist()} == {zipfile.ZIP_DEFLATED}
        for info in a.infolist():
            assert a.read(info.filename) == b.read(info.filename)
    assert stored != deflated


def test_the_central_directory_mismatch_opens_and_then_fails_on_read():
    """The specific shape: the directory is complete and a part is not there.

    A fixture that failed to open would be testing truncation again.
    """
    data = fixtures.build("central-directory-mismatch.docx")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert "word/document.xml" in archive.namelist()
        assert archive.read("[Content_Types].xml"), "the undamaged parts must still read"
        with pytest.raises(zipfile.BadZipFile):
            archive.read("word/document.xml")


def test_the_truncated_archive_has_no_central_directory():
    data = fixtures.build("truncated.docx")
    assert data.startswith(b"PK\x03\x04"), "it must still look like a ZIP to a sniffer"
    assert not zipfile.is_zipfile(io.BytesIO(data))
    with pytest.raises(zipfile.BadZipFile):
        zipfile.ZipFile(io.BytesIO(data))


def test_the_empty_archive_opens_and_holds_nothing():
    with zipfile.ZipFile(io.BytesIO(fixtures.build("empty.zip"))) as archive:
        assert archive.namelist() == []


def test_the_compression_ratio_is_extreme_and_the_file_is_small():
    data = fixtures.build("extreme-compression.zip")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        info = archive.infolist()[0]
        ratio = info.file_size / info.compress_size
        assert ratio > 100, ratio
        # The declared size is readable from the directory, BEFORE anything decompresses.
        # That is what an extraction budget has to be decided from.
        assert info.file_size == 1024 * 1024
    assert len(data) < 8 * 1024


def test_macros_and_unknown_parts_are_where_the_record_says():
    assert "word/vbaProject.bin" in [i.filename for i in _members("macros.docx")]
    assert "word/afchunk.html" in [i.filename for i in _members("unknown-part.docx")]
    assert b"altChunk" in _part("unknown-part.docx", "word/document.xml")


# ---------------------------------------------------------------------------
# XML-level attacks, through the parser this repository actually has
# ---------------------------------------------------------------------------


def test_a_normal_part_parses():
    """The control. A parser that refuses everything passes every test below."""
    root = xmlsafe.parse(_part("minimal.docx", "word/document.xml"))
    assert root.tag.endswith("}document")


def test_a_byte_order_mark_parses():
    """Legal, and Word writes one. The fixture is here for readers that are too strict."""
    part = _part("byte-order-mark.docx", "word/document.xml")
    assert part.startswith(b"\xef\xbb\xbf")
    assert xmlsafe.parse(part).tag.endswith("}document")


def test_entity_expansion_is_refused_at_the_declaration():
    part = _part("entity-expansion.docx", "word/document.xml")
    assert b"<!ENTITY" in part, "the fixture must carry the declaration it is named for"
    with pytest.raises(xmlsafe.UnsafeXML, match="DOCTYPE"):
        xmlsafe.parse(part)


def test_the_stdlib_parser_expands_that_same_entity_quietly():
    """Why :mod:`tests.corpus.xmlsafe` exists, measured rather than asserted from memory.

    ``ElementTree.fromstring`` on the billion-laughs fixture returns a tree. No exception,
    no warning, and the expansion has already happened by the time anyone looks. This test
    is the evidence for the module docstring's claim, and it will start failing the day the
    standard library changes its mind — which is a thing worth being told about.
    """
    import xml.etree.ElementTree as ElementTree

    part = _part("entity-expansion.docx", "word/document.xml")
    root = ElementTree.fromstring(part)
    text = "".join(root.itertext())
    assert len(text) >= 300, f"the entity did not expand ({len(text)} characters)"


def test_an_external_entity_is_refused():
    part = _part("external-entity.docx", "word/document.xml")
    assert b"file:///etc/passwd" in part
    with pytest.raises(xmlsafe.UnsafeXML):
        xmlsafe.parse(part)


def test_deep_nesting_is_refused_at_the_depth_limit():
    part = _part("deep-nesting.docx", "word/document.xml")
    assert b"<!ENTITY" not in part, "the depth fixture must not also be an entity fixture"
    with pytest.raises(xmlsafe.UnsafeXML, match="nesting"):
        xmlsafe.parse(part)


def test_invalid_utf8_is_refused_by_the_parser_itself():
    """Not by a guard of ours — expat decodes as it goes, and this is what that looks like."""
    from xml.parsers.expat import ExpatError

    part = _part("invalid-utf8.docx", "word/document.xml")
    with pytest.raises(UnicodeDecodeError):
        part.decode("utf-8")
    with pytest.raises(ExpatError):
        xmlsafe.parse(part)
