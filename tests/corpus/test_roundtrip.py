"""Tiers 1 and 2 of ``docs/OFFICE_SPEC.md`` Part 10's fidelity table.

    | 1 | no-op repack, part set and bytes | stdlib | packaging bugs, member ordering, compression drift |
    | 2 | one-element edit: every other part byte-identical | stdlib | over-broad rewrites, normalizing readers |

Part 8 is what these are for:

    "The source package is the truth. The tree is a projection of it. A modification
    rewrites named parts of a copy of the package, and never re-authors the document from
    the tree." … "If a one-word edit changes any part other than the one the anchor named,
    the implementation has started re-authoring, and the test says so before a user finds
    out."

**The edit path does not exist yet.** It is Part 11 step 10, and it depends on the source
anchor from step 9. What exists now is the measurement, and building the measurement first
is deliberate: tier 2 is the acceptance criterion for that verb, and an acceptance criterion
written after the implementation tends to describe the implementation.

So the two repackers in this file are deliberately, visibly simple. :func:`repack` copies
every member with its metadata; :func:`edit_one_part` does a byte-level string replacement
in a single named part. Neither is an edit API and neither should grow into one — when the
real verb lands it replaces the ``edit_one_part`` call here and every assertion stays as it
is. That substitution is the whole design.

### The negative control is not optional

A tier-2 test that only ever sees a correct edit proves nothing about the harness: an
``assert_only_changed`` that returned silently no matter what would pass it. So
:func:`reauthor` is also here — a repacker that does what the obvious implementation does,
parse every XML part and write it back — and the test asserts the harness CATCHES it. That
is the failure Part 8 is about, reproduced on purpose, so that the thing which is supposed
to detect it can be seen detecting it.
"""

from __future__ import annotations

import io
import warnings
import xml.etree.ElementTree as ElementTree
import zipfile

import pytest

from tests.corpus import fixtures
from tests.corpus.manifest import load
from tests.corpus.roundtrip import (
    UnreadablePackage,
    assert_only_changed,
    diff_packages,
    read_members,
)

CORPUS = load()

#: Tier 1 and 2 apply to packages that are supposed to open. A ``must-reject`` fixture is
#: not a repacking subject — several of them cannot be opened at all, which is the point of
#: them — and the suite says which ones those are rather than discovering it.
REPACKABLE = [
    record
    for record in CORPUS.expecting("parses", "parses-lossy")
    if record.format in ("docx", "xlsx", "pptx", "zip")
]

#: The one package tier 2 edits: minimal, stored, and carrying a distinctive word.
SUBJECT = "minimal.docx"

#: Metadata a faithful repacker copies. Named here because the list IS the claim — a
#: repacker that drops any of these has normalised the package.
COPIED = (
    "compress_type",
    "external_attr",
    "internal_attr",
    "create_system",
    "create_version",
    "extract_version",
    "flag_bits",
    "comment",
    "extra",
)


def repack(data: bytes, replacements: dict[str, bytes] | None = None) -> bytes:
    """Rewrite a package member by member, changing only what ``replacements`` names.

    Part 8 step 5: *"same ZIP, same member order, same compression, changed parts
    swapped."* This is the smallest thing that satisfies that sentence. Members are visited
    by ``ZipInfo`` and in archive order, so duplicates survive and nothing is sorted.
    """
    replacements = replacements or {}
    output = io.BytesIO()
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Duplicate name", category=UserWarning)
        with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(output, "w") as target:
            for info in source.infolist():
                payload = replacements.get(info.filename, source.read(info))
                copy = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                for field in COPIED:
                    setattr(copy, field, getattr(info, field))
                target.writestr(copy, payload)
            target.comment = source.comment
    return output.getvalue()


def edit_one_part(data: bytes, part: str, old: str, new: str) -> bytes:
    """Replace one word in one part, at the byte level. Not an edit API.

    A real edit resolves an anchor to an element and rewrites its runs (Part 8 step 4).
    This does the crudest possible version of the same thing so that the harness has a
    correct edit to measure. It asserts the word was there, because a replacement that
    matched nothing would produce a package tier 2 passes trivially.
    """
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        original = archive.read(part)
    if old.encode("utf-8") not in original:
        raise AssertionError(f"{old!r} is not in {part}; the fixture and the test disagree")
    return repack(data, {part: original.replace(old.encode("utf-8"), new.encode("utf-8"))})


def reauthor(data: bytes) -> bytes:
    """The obvious implementation, reproduced so the harness can be seen catching it.

    Parses every XML part and writes it back. Nothing is *edited*: this rewrites the
    package with the same content, the way a reader that models a document and re-emits it
    would. ``ElementTree`` drops the XML declaration and renames namespace prefixes, which
    is exactly the class of damage Part 8 describes — every part still "reads the same",
    and every part's bytes have moved.
    """
    replacements: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for info in archive.infolist():
            if not info.filename.endswith(".xml") and not info.filename.endswith(".rels"):
                continue
            root = ElementTree.fromstring(archive.read(info))
            replacements[info.filename] = ElementTree.tostring(root, encoding="utf-8")
    return repack(data, replacements)


# ---------------------------------------------------------------------------
# Tier 1
# ---------------------------------------------------------------------------


def test_the_tier_one_subject_list_is_not_empty():
    """A parametrized suite over an empty list is a green suite that ran nothing."""
    assert len(REPACKABLE) >= 5, [r.name for r in REPACKABLE]


@pytest.mark.parametrize("record", REPACKABLE, ids=lambda r: r.name)
def test_tier_one_a_noop_repack_reproduces_the_part_set(record):
    """Part set, content, order, member metadata. The structural half of tier 1."""
    original = CORPUS.bytes_for(record)
    diff = diff_packages(original, repack(original))
    assert diff.structurally_identical, diff.describe()


@pytest.mark.parametrize("record", REPACKABLE, ids=lambda r: r.name)
def test_tier_one_a_noop_repack_reproduces_the_bytes(record):
    """The byte half, and the stronger claim.

    It holds here because these fixtures were written by the same ``zipfile`` with the same
    parameters, which is a property of a GENERATED corpus and not of a corpus. An imported
    Word document will not reproduce byte for byte — Word writes extra fields and a
    different zlib level — and when imported files arrive, the structural test above is the
    one that applies to them. Keeping the two claims in separate tests is what will make
    that distinction reportable instead of a weakened assertion.
    """
    original = CORPUS.bytes_for(record)
    assert repack(original) == original


def test_tier_one_notices_compression_drift():
    """The negative control for tier 1, using the stored/deflated pair.

    Nothing in the part set, the order or the content differs between these two — only the
    method. A tier-1 check that compared only the part set would call them identical, and
    would therefore never see a repacker that decompressed and recompressed a package.
    """
    stored = fixtures.build("minimal.docx")
    deflated = fixtures.build("deflated.docx")
    diff = diff_packages(stored, deflated)
    assert not diff.content_changed and not diff.added and not diff.removed
    assert not diff.structurally_identical
    assert diff.metadata_changed, "the compression method change was not reported"
    assert "metadata changed" in diff.describe()


def test_tier_one_notices_a_reordered_archive():
    """OPC does not require an order. Word writes one, and a rebuild loses it."""
    members = [
        fixtures.Member("[Content_Types].xml", b"<Types/>"),
        fixtures.Member("_rels/.rels", b"<Relationships/>"),
        fixtures.Member("word/document.xml", b"<w:document/>"),
    ]
    forward = fixtures.pack(members)
    backward = fixtures.pack(list(reversed(members)))
    diff = diff_packages(forward, backward)
    assert diff.order_changed
    assert not diff.added and not diff.removed and not diff.content_changed


def test_a_package_that_will_not_open_raises_rather_than_comparing_as_equal():
    """Two unreadable archives must not diff as identical — see :class:`UnreadablePackage`."""
    broken = fixtures.build("truncated.docx")
    with pytest.raises(UnreadablePackage):
        read_members(broken)
    with pytest.raises(UnreadablePackage):
        diff_packages(broken, broken)


# ---------------------------------------------------------------------------
# Tier 2
# ---------------------------------------------------------------------------


def test_tier_two_a_one_word_edit_touches_exactly_one_part():
    """The test that will hold Part 8's rule when the edit verb exists."""
    original = fixtures.build(SUBJECT)
    edited = edit_one_part(original, "word/document.xml", fixtures.EDIT_TARGET, "VIERUNDZWANZIG")

    diff = assert_only_changed(original, edited, {"word/document.xml"})
    assert diff.changed_parts == (("word/document.xml", 0),)
    assert not diff.added and not diff.removed
    assert not diff.order_changed
    assert not diff.metadata_changed

    with zipfile.ZipFile(io.BytesIO(edited)) as archive:
        document = archive.read("word/document.xml").decode("utf-8")
    assert "VIERUNDZWANZIG" in document and fixtures.EDIT_TARGET not in document
    # The heading in the same part is untouched. Part 8 step 6's "no other node's text
    # changed", at the granularity this tier can see.
    assert "The corpus is the feature probe" in document


def test_tier_two_leaves_every_other_part_byte_identical():
    """Stated as bytes, not as 'reads the same'. The distinction is the whole tier."""
    original = fixtures.build(SUBJECT)
    edited = edit_one_part(original, "word/document.xml", fixtures.EDIT_TARGET, "VIERUNDZWANZIG")
    before = {m.key: m for m in read_members(original)}
    after = {m.key: m for m in read_members(edited)}
    for key, member in before.items():
        if key[0] == "word/document.xml":
            continue
        assert after[key].content_sha256 == member.content_sha256, key
        assert after[key].metadata == member.metadata, key
        assert after[key].position == member.position, key


def test_tier_two_catches_a_re_authoring_repack():
    """The negative control. Without it, the tier-2 pass above proves nothing.

    ``reauthor`` changes no text at all and rewrites every XML part, which is precisely the
    "format-converting round trip" Part 8 refuses: the output opens, it reads the same, and
    everything the reader did not model is gone.
    """
    original = fixtures.build(SUBJECT)
    rewritten = reauthor(original)

    with pytest.raises(AssertionError, match="changed beyond it"):
        assert_only_changed(original, rewritten, {"word/document.xml"})

    diff = diff_packages(original, rewritten)
    assert len(diff.content_changed) >= 3, diff.describe()
    assert "[Content_Types].xml" in {name for name, _ in diff.content_changed}


def test_tier_two_refuses_an_edit_that_did_nothing():
    """A no-op is not a pass. It is the failure that looks most like a success."""
    original = fixtures.build(SUBJECT)
    with pytest.raises(AssertionError, match="nothing changed"):
        assert_only_changed(original, repack(original), {"word/document.xml"})


def test_tier_two_names_the_part_that_should_not_have_moved():
    """The message is the deliverable: 'the package changed beyond it', with the part."""
    original = fixtures.build(SUBJECT)
    both = repack(
        original,
        {
            "word/document.xml": b"<w:document/>",
            "word/_rels/document.xml.rels": b"<Relationships/>",
        },
    )
    with pytest.raises(AssertionError) as raised:
        assert_only_changed(original, both, {"word/document.xml"})
    assert "word/_rels/document.xml.rels" in str(raised.value)
    assert "word/document.xml" in str(raised.value)
