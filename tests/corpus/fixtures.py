"""The fixture generator — every corpus file this repository carries is built here.

``docs/OFFICE_SPEC.md`` Part 10 excludes malware corpora and says why the exclusion costs
nothing:

    "the structural attacks they would cover — zip bombs, XXE, entity expansion, duplicate
    ZIP entries, central directory mismatch — can be synthesized in a fixture generator
    without carrying anyone's payloads."

This is that generator. It is ``zipfile`` and string literals, it needs no network and no
optional dependency, and **nothing it produces is committed**: the manifest pins each
fixture's sha256 and the bytes are rebuilt on demand. A generated corpus that lived in git
would be a binary blob nobody could review, and reviewing these is the point — a fixture
whose attack you cannot read is a fixture you cannot trust to be testing the attack.

### Determinism is a feature, not tidiness

Every member is written with a fixed timestamp, a fixed mode, an explicit compression
method, and in a fixed order, so ``build_all()`` produces the same bytes on every machine.
Two things depend on that. The manifest can pin a sha256, which is what makes "this file is
the file the record describes" checkable. And tier 1 of the fidelity table — *a no-op
repack reproduces the bytes* — is only a meaningful claim against an archive whose byte
layout was itself deliberate.

The one place determinism is not fully ours is deflate: the compressed bytes come from
whatever zlib is linked in. Only the fixtures that exist to exercise compression use it,
for that reason, and if a zlib upgrade changes them the manifest hash check fails loudly
and the recorded hash is what gets updated.

### What the fixtures cover

Container level — zip-slip by relative and absolute path, duplicate members, a missing
``[Content_Types].xml``, stored against deflated, a central directory naming a member with
no local header, a truncated archive, an empty archive, and an extreme compression ratio.

XML level — an internal entity declaration (billion laughs), an external entity, deep
nesting, a byte order mark, and bytes that are not valid UTF-8.

Format level — a minimal ``.docx``, ``.xlsx`` and ``.pptx`` that parse; a package carrying
``vbaProject.bin``; a package carrying a part no reader models; and a ``.docx`` whose bytes
are a PDF, which is the format-confusion case ``detect_format`` exists to catch.

Defect level — three files that reproduce ``docs/SPRINT_0_4_0.md`` Block B steps 4, 5 and
6: a workbook declaring 2.75e11 cells around five, a workbook whose only sheet is a chart,
and a package whose deflate stream will not inflate. **Every one of those is a
minimisation of a real file rather than a hazard we thought of**, which is the difference
between this group and the two above it — the three defects survived into 0.3.0 precisely
because everything in this generator was, until then, well formed by construction.

Each of those is a manifest record with an ``expect`` of ``parses``, ``parses-lossy`` or
``must-reject``, and the record is where the claim lives — this module only builds bytes.
"""

from __future__ import annotations

import hashlib
import io
import warnings
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

#: MS-DOS epoch. Any constant would do; what matters is that it is a constant.
FIXED_DATE = (1980, 1, 1, 0, 0, 0)

#: ``rw-------``, shifted into the high half of ``external_attr`` where the unix mode
#: lives. Fixed for the same reason the date is: the umask of whoever ran the generator
#: must not reach the bytes.
FIXED_MODE = 0o600 << 16

#: Unix. ``create_system`` otherwise follows the platform, and a corpus that hashes
#: differently on macOS is not a corpus.
UNIX = 3

#: The repository rule from this lane's brief, enforced where it can be enforced. A
#: generator that quietly emitted a megabyte would be a generator nobody noticed changed.
MAX_FIXTURE_BYTES = 100 * 1024

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

#: The word tier 2's one-element edit replaces. Distinctive so a byte-level search for it
#: cannot match anything else in the package.
EDIT_TARGET = "ZWEIUNDVIERZIG"


@dataclass(frozen=True)
class Member:
    """One archive member, with everything that decides its bytes stated."""

    name: str
    data: bytes
    compress_type: int = zipfile.ZIP_STORED


def pack(members: list[Member], *, comment: bytes = b"", allow_duplicates: bool = False) -> bytes:
    """A ZIP holding ``members``, in order, deterministically.

    A duplicate member name is an error unless the caller asked for one. Exactly one
    fixture wants duplicates and ``zipfile`` only warns about them, so the choice is
    between a generator that can silently emit a package it did not mean to and one that
    makes the single deliberate case say so. ``allow_duplicates=True`` also suppresses
    ``zipfile``'s warning, which is correct and which pytest would otherwise report on
    every run of a fixture that is doing it on purpose.
    """
    names = [member.name for member in members]
    duplicated = sorted({name for name in names if names.count(name) > 1})
    if duplicated and not allow_duplicates:
        raise AssertionError(f"pack() would write {duplicated} twice; pass allow_duplicates=True")

    buffer = io.BytesIO()
    with warnings.catch_warnings(), zipfile.ZipFile(buffer, "w") as archive:
        if allow_duplicates:
            warnings.filterwarnings("ignore", message="Duplicate name", category=UserWarning)
        for member in members:
            info = zipfile.ZipInfo(member.name, date_time=FIXED_DATE)
            info.compress_type = member.compress_type
            info.external_attr = FIXED_MODE
            info.create_system = UNIX
            archive.writestr(info, member.data)
        archive.comment = comment
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# OOXML parts
# ---------------------------------------------------------------------------

DECLARATION = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'


def _content_types(*overrides: str) -> bytes:
    defaults = (
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Default Extension="bin" ContentType="application/vnd.ms-office.vbaProject"/>'
    )
    body = "".join(overrides)
    return (
        DECLARATION
        + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        + defaults
        + body
        + "</Types>"
    ).encode("utf-8")


def _package_rels(target: str, rel_type: str) -> bytes:
    return (
        DECLARATION
        + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + f'<Relationship Id="rId1" Type="{rel_type}" Target="{target}"/>'
        + "</Relationships>"
    ).encode("utf-8")


def _empty_rels() -> bytes:
    return (
        DECLARATION
        + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'
    ).encode("utf-8")


def _document_xml(body: str) -> bytes:
    document = DECLARATION + f'<w:document xmlns:w="{W_NS}"><w:body>{body}</w:body></w:document>'
    return document.encode("utf-8")


#: A heading (so a future ``has_heading_styles`` prober has something to find) and one
#: paragraph whose text is the tier-2 edit target.
DOCX_BODY = (
    '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
    "<w:r><w:t>The corpus is the feature probe</w:t></w:r></w:p>"
    f"<w:p><w:r><w:t>{EDIT_TARGET}</w:t></w:r></w:p>"
)

DOCX_CONTENT_TYPE = (
    '<Override PartName="/word/document.xml" ContentType="application/vnd.'
    'openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
)
DOCX_REL_TYPE = f"{R_NS}/officeDocument"


def _docx_members(document: bytes | None = None) -> list[Member]:
    """The four parts of a minimal ``.docx``, in the order Word writes them."""
    return [
        Member("[Content_Types].xml", _content_types(DOCX_CONTENT_TYPE)),
        Member("_rels/.rels", _package_rels("word/document.xml", DOCX_REL_TYPE)),
        Member("word/document.xml", document if document is not None else _document_xml(DOCX_BODY)),
        Member("word/_rels/document.xml.rels", _empty_rels()),
    ]


# ---------------------------------------------------------------------------
# The fixtures
# ---------------------------------------------------------------------------


def minimal_docx() -> bytes:
    """The control. Without one, ``must-reject`` proves nothing about the rejecter."""
    return pack(_docx_members())


def minimal_xlsx() -> bytes:
    override = (
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.'
        'openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    )
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
    workbook = (
        DECLARATION
        + '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        + '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1" '
        + 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/>'
        + "</sheets></workbook>"
    ).encode("utf-8")
    return pack(
        [
            Member("[Content_Types].xml", _content_types(override)),
            Member("_rels/.rels", _package_rels("xl/workbook.xml", rel)),
            Member("xl/workbook.xml", workbook),
            Member("xl/_rels/workbook.xml.rels", _empty_rels()),
        ]
    )


def minimal_pptx() -> bytes:
    override = (
        '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.'
        'openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
    )
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
    presentation = (
        DECLARATION
        + '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
        + ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        + '<p:sldIdLst><p:sldId id="256" r:id="rId2"/></p:sldIdLst></p:presentation>'
    ).encode("utf-8")
    slide = (
        DECLARATION
        + '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>'
    ).encode("utf-8")
    return pack(
        [
            Member("[Content_Types].xml", _content_types(override)),
            Member("_rels/.rels", _package_rels("ppt/presentation.xml", rel)),
            Member("ppt/presentation.xml", presentation),
            Member("ppt/slides/slide1.xml", slide),
            Member("ppt/_rels/presentation.xml.rels", _empty_rels()),
        ]
    )


def zip_slip_relative_docx() -> bytes:
    """A member that escapes the extraction root by ``..``.

    A valid ``.docx`` otherwise, which is the hazard: a reader that extracts to a temporary
    directory before parsing writes outside it, and every declared part is still where the
    reader expects, so nothing downstream notices.
    """
    members = _docx_members()
    members.insert(
        2, Member("../../../../tmp/jmfts-zip-slip.txt", b"escaped the extraction root\n")
    )
    return pack(members)


def zip_slip_absolute_zip() -> bytes:
    """The other half of the same attack: an absolute path, with no ``..`` to filter."""
    return pack(
        [
            Member("readme.txt", b"a plain archive with one hostile member\n"),
            Member("/etc/cron.d/jmfts-zip-slip", b"* * * * * root id\n"),
        ]
    )


def duplicate_entries_docx() -> bytes:
    """``word/document.xml`` twice, with different content.

    Which one is the document is not defined: ``zipfile`` reads the last by name and some
    readers take the first. A package where the answer depends on the reader has no single
    text, so it cannot be ingested honestly and must be rejected.
    """
    members = _docx_members()
    shadow = _document_xml(
        "<w:p><w:r><w:t>the second copy, which some readers win</w:t></w:r></w:p>"
    )
    members.append(Member("word/document.xml", shadow))
    return pack(members, allow_duplicates=True)


def missing_content_types_docx() -> bytes:
    """Every part but the one that says what the parts are.

    OPC requires ``[Content_Types].xml``. Without it the package is not an OOXML document,
    however well-formed its XML — and ``detect_format`` still calls it a ``docx``, because
    it detects from ``word/document.xml``. That divergence is the fixture's whole value.
    """
    return pack([m for m in _docx_members() if m.name != "[Content_Types].xml"])


def deflated_docx() -> bytes:
    """``minimal.docx``'s parts, deflated. Different bytes, identical part contents.

    ``minimal.docx`` stores every member, so the two together are the stored-vs-deflate
    pair, and there is no third fixture holding a second copy of the same bytes. That pair
    is what lets tier 1 say *compression drift*: a repack that reproduces the part set
    while changing the method produces exactly this difference and no other.
    """
    return pack([Member(m.name, m.data, zipfile.ZIP_DEFLATED) for m in _docx_members()])


def central_directory_mismatch_docx() -> bytes:
    """The central directory names a part whose local header was overwritten.

    Built by packing normally and then blanking the local file header signature of one
    member, so the directory still lists it and reading it fails. ``namelist()`` — which is
    what ``detect_format`` uses — therefore still reports a ``.docx``, and the file only
    falls apart when something reads a part.
    """
    data = bytearray(minimal_docx())
    marker = b"word/document.xml"
    offset = data.find(b"PK\x03\x04", 0)
    while offset != -1:
        # The name follows the 30-byte local header.
        if data[offset + 30 : offset + 30 + len(marker)] == marker:
            data[offset : offset + 4] = b"PK\x00\x00"
            return bytes(data)
        offset = data.find(b"PK\x03\x04", offset + 1)
    raise AssertionError("no local header for word/document.xml — the generator is broken")


def truncated_docx() -> bytes:
    """The first 60% of a valid package: local headers, no central directory.

    An upload that died mid-transfer. It still opens with ``PK\\x03\\x04``, so
    ``detect_format`` sees a ZIP, cannot open the archive, and reports ``zip`` rather than
    ``docx`` — the honest answer, and one the manifest records so a change to it is visible.
    """
    data = deflated_docx()
    cut = (len(data) * 3) // 5
    if cut <= 4:
        raise AssertionError("the source package is too small to truncate meaningfully")
    return data[:cut]


def empty_zip() -> bytes:
    """A valid archive with no members. Opens, lists nothing, extracts nothing."""
    return pack([])


def entity_expansion_docx() -> bytes:
    """Billion laughs, three levels, inside ``word/document.xml``.

    Deliberately small: ten to the third, not ten to the ninth. The finding is *an entity
    declaration reached the parser*, and a fixture that has to allocate a gigabyte to make
    that point is a fixture that cannot be run in a unit test.
    """
    document = (
        DECLARATION
        + "<!DOCTYPE w:document [\n"
        + '  <!ENTITY a "lol">\n'
        + '  <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">\n'
        + '  <!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">\n'
        + "]>\n"
        + f'<w:document xmlns:w="{W_NS}"><w:body><w:p><w:r><w:t>&c;</w:t></w:r></w:p>'
        + "</w:body></w:document>"
    ).encode("utf-8")
    return pack(_docx_members(document))


def external_entity_docx() -> bytes:
    """XXE: an entity whose replacement text is a local file."""
    document = (
        DECLARATION
        + '<!DOCTYPE w:document [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>\n'
        + f'<w:document xmlns:w="{W_NS}"><w:body><w:p><w:r><w:t>&xxe;</w:t></w:r></w:p>'
        + "</w:body></w:document>"
    ).encode("utf-8")
    return pack(_docx_members(document))


def deep_nesting_docx() -> bytes:
    """Nesting past any reader's recursion limit, in valid, entity-free XML.

    The depth is chosen to be well past :data:`tests.corpus.xmlsafe.MAX_DEPTH` and well
    short of anything that costs time to build.
    """
    depth = 500
    body = "<w:tbl><w:tr><w:tc>" * depth + "<w:p/>" + "</w:tc></w:tr></w:tbl>" * depth
    return pack(_docx_members(_document_xml(body)))


def byte_order_mark_docx() -> bytes:
    """A UTF-8 BOM before the XML declaration.

    Legal, common — Word writes one — and it breaks any code that compares the first bytes
    of a part against ``<?xml``. Expected to PARSE, which is what makes it a useful
    fixture: it is the case a reader gets wrong by being too strict.
    """
    document = b"\xef\xbb\xbf" + _document_xml(DOCX_BODY)
    return pack(_docx_members(document))


def invalid_utf8_docx() -> bytes:
    """A part that declares UTF-8 and is not.

    ``\\xff\\xfe`` is a UTF-16 BOM sitting in the middle of a document that says it is
    UTF-8 — the shape a mis-transcoded file actually has, rather than a random bad byte.
    """
    document = _document_xml(DOCX_BODY).replace(b"corpus", b"cor\xff\xfeus")
    return pack(_docx_members(document))


def extreme_compression_zip() -> bytes:
    """One megabyte of zeros in about a kilobyte.

    Not a zip bomb — a bomb is nested and unbounded, and this repository has no business
    carrying one. It is the *ratio*, which is the measurable thing: an extraction budget
    has to be decided from the declared uncompressed size before decompressing, and this
    is the file that says whether it was.
    """
    return pack([Member("zeros.bin", b"\x00" * (1024 * 1024), zipfile.ZIP_DEFLATED)])


def macros_docx() -> bytes:
    """A package carrying ``word/vbaProject.bin``.

    Not a hazard to us — nothing here executes anything — but a fact an operator wants, and
    ``has_macros`` is a Part 2 pattern. The payload is a plausible OLE2 header and nothing
    else; a real VBA project would be someone else's code.
    """
    members = _docx_members()
    members.append(
        Member("word/vbaProject.bin", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 56)
    )
    return pack(members)


def unknown_part_docx() -> bytes:
    """A part no reader models: an ``altChunk`` target holding another document.

    ``OFFICE_SPEC.md`` Part 3 names this case — ``python-docx`` ignores ``altChunk``, so
    extraction that meets one must fail rather than silently drop the embedded content.
    Expected ``parses-lossy``: the document reads, and something in it does not come out.
    """
    members = _docx_members(
        _document_xml(DOCX_BODY + f'<w:altChunk xmlns:r="{R_NS}" r:id="rId9"/>')
    )
    members.append(
        Member("word/afchunk.html", b"<html><body><p>embedded, and dropped</p></body></html>")
    )
    return pack(members)


def minimal_pdf(text: str = "one page, one line, no outline") -> bytes:
    """A one-page PDF, assembled by hand with a real cross-reference table.

    Written out rather than produced with ``pymupdf`` for one reason: a PDF writer stamps
    its own version and a document id into the file, so two calls a second apart differ and
    a library upgrade changes every hash. A corpus fixture has to be the same bytes next
    year. This is also why ``fixtures.py`` imports nothing outside the standard library —
    ``pymupdf`` is what *reads* the corpus, and a reader that also authors the fixtures is
    a reader agreeing with itself.
    """
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length "
        + str(len(stream)).encode("ascii")
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    trailer = f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n"
    out += trailer.encode("ascii") + b"%%EOF\n"
    return bytes(out)


# ---------------------------------------------------------------------------
# The three 0.4.0 defect fixtures — docs/SPRINT_0_4_0.md Block B steps 4, 5 and 6
#
# Each one was found by running the SHIPPED 0.3.0 `probe` and `measure_sheet` over 16,856
# real workbooks, and none of them is reachable from the fixtures above: those are
# well-formed by construction, which is exactly why three defects survived them into a
# release. Each is a minimisation of a file that exists — the workbook or the message is
# named in the docstring — because a fixture nobody can trace back to real bytes is a
# fixture asserting what we already believe.
#
# The two spreadsheets build their own workbook parts rather than sharing
# `minimal_xlsx`'s. Sharing would move `minimal.xlsx`'s bytes, and its recorded sha256 is
# a determinism pin: a hash that moves for a refactor teaches a reader to accept a moved
# hash.
# ---------------------------------------------------------------------------

S_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"

#: `<Override>` rows for the parts openpyxl decides a sheet's KIND from. It reads the
#: content type, not the relationship target, so a chartsheet mislabelled here would load
#: as a worksheet and the fixture would stop being the fixture.
XLSX_WORKBOOK_TYPE = (
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.'
    'openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
)
XLSX_WORKSHEET_TYPE = (
    '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.'
    'openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
)
XLSX_CHARTSHEET_TYPE = (
    '<Override PartName="/xl/chartsheets/sheet1.xml" ContentType="application/vnd.'
    'openxmlformats-officedocument.spreadsheetml.chartsheet+xml"/>'
)
XLSX_REL_TYPE = f"{R_NS}/officeDocument"


def _workbook_xml(sheet_name: str) -> bytes:
    """``xl/workbook.xml`` naming one sheet. The name is what ``measure_sheet`` is given."""
    return (
        DECLARATION
        + f'<workbook xmlns="{S_NS}" xmlns:r="{R_NS}">'
        + f'<sheets><sheet name="{sheet_name}" sheetId="1" r:id="rId1"/></sheets>'
        + "</workbook>"
    ).encode("utf-8")


def _workbook_rels(target: str, kind: str) -> bytes:
    """``rId1`` -> the one sheet part, which is how openpyxl finds it at all."""
    return (
        DECLARATION
        + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + f'<Relationship Id="rId1" Type="{R_NS}/{kind}" Target="{target}"/>'
        + "</Relationships>"
    ).encode("utf-8")


def declared_extent_xlsx() -> bytes:
    """Five cells under ``<dimension ref="A1:XFE16777217"/>`` — 2.75e11 declared cells.

    A minimisation of ``datasets/lo-qa/sc/qa/unit/data/xlsx/too-many-cols-rows.xlsx``
    (LibreOffice's own QA corpus, 5,526 bytes), carrying the two properties that matter and
    nothing else. **Both are needed.** The wide ``<dimension>`` is where openpyxl's
    ``max_column`` comes from, and the ROW NUMBERS are what make it cost anything:
    ``ReadOnlyWorksheet._cells_by_row`` fills the gap between two ``<row r="...">`` indices
    by yielding one ``max_column``-wide empty row per missing index, so rows numbered 1,
    16777214 and 16777217 across 16,385 declared columns are 2.75e11 iterations of
    ``_scan``'s inner loop. Measured at ~1.2e7 cells/s that is six and a half hours, on a
    worker that cannot be interrupted and whose heartbeat keeps beating throughout.

    Sheet-level, not container-level: the ZIP is ordinary and every part is well formed.
    """
    rows = (
        '<row r="1"><c r="A1" t="n"><v>1</v></c><c r="XFB1" t="n"><v>2</v></c>'
        '<c r="XFE1" t="n"><v>3</v></c></row>'
        '<row r="16777214"><c r="A16777214" t="n"><v>11</v></c></row>'
        '<row r="16777217"><c r="A16777217" t="n"><v>21</v></c></row>'
    )
    worksheet = (
        DECLARATION
        + f'<worksheet xmlns="{S_NS}"><dimension ref="A1:XFE16777217"/>'
        + f"<sheetData>{rows}</sheetData></worksheet>"
    ).encode("utf-8")
    return pack(
        [
            Member("[Content_Types].xml", _content_types(XLSX_WORKBOOK_TYPE, XLSX_WORKSHEET_TYPE)),
            Member("_rels/.rels", _package_rels("xl/workbook.xml", XLSX_REL_TYPE)),
            Member("xl/workbook.xml", _workbook_xml("Sheet1")),
            Member(
                "xl/_rels/workbook.xml.rels", _workbook_rels("worksheets/sheet1.xml", "worksheet")
            ),
            Member("xl/worksheets/sheet1.xml", worksheet),
        ]
    )


def chartsheet_xlsx() -> bytes:
    """A workbook whose only sheet is a chart, not a grid. 1.3% of open-web workbooks.

    A chartsheet is a full-window chart on its own tab, drawn from cells that live on some
    other sheet. It is a ``<sheet>`` in ``xl/workbook.xml`` and it appears in
    ``workbook.sheetnames`` exactly like a worksheet, so anything that walks the sheet names
    reaches it — and ``openpyxl`` hands back a ``Chartsheet``, which has no ``max_row``,
    no ``max_column`` and no ``iter_rows``.

    ``xl/chartsheets/_rels/sheet1.xml.rels`` is present and empty because ``openpyxl``'s
    reader asks the chartsheet's relationships for a drawing before it constructs anything;
    without the part it raises ``AttributeError: 'list' object has no attribute 'find'``
    from inside ``load_workbook``, which would make this a fixture about a missing rels part
    instead of a fixture about a chartsheet.
    """
    chartsheet = (
        DECLARATION
        + f'<chartsheet xmlns="{S_NS}"><sheetPr/>'
        + '<sheetViews><sheetView workbookViewId="0" zoomScale="100"/></sheetViews>'
        + "</chartsheet>"
    ).encode("utf-8")
    return pack(
        [
            Member("[Content_Types].xml", _content_types(XLSX_WORKBOOK_TYPE, XLSX_CHARTSHEET_TYPE)),
            Member("_rels/.rels", _package_rels("xl/workbook.xml", XLSX_REL_TYPE)),
            Member("xl/workbook.xml", _workbook_xml("Chart1")),
            Member(
                "xl/_rels/workbook.xml.rels",
                _workbook_rels("chartsheets/sheet1.xml", "chartsheet"),
            ),
            Member("xl/chartsheets/_rels/sheet1.xml.rels", _empty_rels()),
            Member("xl/chartsheets/sheet1.xml", chartsheet),
        ]
    )


#: The paragraph ``corrupt_deflate_docx`` repeats. Repetition is the point: deflate answers
#: it with long back-references, and a back-reference is what a spliced stream cannot
#: resolve.
_REPEATED = "the same sentence, over and over, so that deflate emits long matches"


def _deflate(data: bytes) -> bytes:
    """Raw deflate with ``zipfile``'s own parameters, so lengths line up byte for byte.

    ``zipfile._get_compressor(ZIP_DEFLATED, None)`` is
    ``zlib.compressobj(Z_DEFAULT_COMPRESSION, DEFLATED, -15)``; matching it is what lets
    the splice below replace a member's payload IN PLACE, leaving every header, size and
    offset in the archive untouched.
    """
    compressor = zlib.compressobj(zlib.Z_DEFAULT_COMPRESSION, zlib.DEFLATED, -15)
    return compressor.compress(data) + compressor.flush()


def corrupt_deflate_docx() -> bytes:
    """A valid package whose ``word/document.xml`` will not inflate. Step 6.

    The archive is intact — central directory, local headers, sizes, CRCs, every other
    part — and one member's compressed payload is spliced from two different deflate
    streams. The second half's back-references point into a window the first half never
    produced, so ``zlib`` gives up with *"Error -3 while decompressing data: invalid
    distance too far back"*: the same message
    ``datasets/lo-qa/sw/qa/extras/uiwriter/data/ofz18563.docx`` produces, and the reason
    that file was retried three times in 0.3.0.

    **This is not ``central-directory-mismatch.docx`` again.** That one damages the local
    HEADER, so ``zipfile`` refuses before inflating and raises ``BadZipFile``, which
    ``probe_patterns`` already caught. This one has a perfect header and unreadable data,
    which is one layer further down and a different exception class. The pair is what makes
    the distinction testable rather than argued.
    """
    document = _document_xml(f"<w:p><w:r><w:t>{_REPEATED}</w:t></w:r></w:p>" * 40)
    package = bytearray(
        pack([Member(m.name, m.data, zipfile.ZIP_DEFLATED) for m in _docx_members(document)])
    )

    with zipfile.ZipFile(io.BytesIO(bytes(package))) as archive:
        info = archive.getinfo("word/document.xml")
    # The payload starts after the 30-byte local header, the name and the extra field —
    # both lengths read from the header itself rather than assumed, because a `zipfile`
    # that started writing an extra field would otherwise corrupt a different member.
    name_length = int.from_bytes(
        package[info.header_offset + 26 : info.header_offset + 28], "little"
    )
    extra_length = int.from_bytes(
        package[info.header_offset + 28 : info.header_offset + 30], "little"
    )
    start = info.header_offset + 30 + name_length + extra_length
    payload = bytes(package[start : start + info.compress_size])
    if _deflate(document) != payload:
        raise AssertionError(
            "the member's payload is not this module's own deflate of the part; the "
            "splice below would be operating on bytes it did not produce"
        )

    # Deliberately NOT repetitive: the second stream has to be at least as long as the
    # member it fills, and a stream of one repeated sentence deflates shorter than the
    # payload does.
    other = _deflate(
        _document_xml(
            "".join(
                f"<w:p><w:r><w:t>paragraph {n} of an unrelated document</w:t></w:r></w:p>"
                for n in range(80)
            )
        )
    )
    half = len(payload) // 2
    spliced = payload[:half] + other[half : len(payload)]
    if len(spliced) != len(payload):
        raise AssertionError(
            f"the splice is {len(spliced)} bytes against a {len(payload)}-byte member; "
            "the second stream is too short to fill it and every offset after this member "
            "would move"
        )
    package[start : start + len(payload)] = spliced
    return bytes(package)


def pdf_named_docx() -> bytes:
    """A PDF. The manifest records the extension it will be uploaded under.

    ``detect_format`` reads the magic bytes and answers ``pdf``; the declared type says
    ``docx``; ``FormatDetection.mime_agrees`` is what reports the disagreement. This is the
    fixture behind the ``declared_type_disagrees`` tag, and behind probe's module docstring:
    "how a ``.pdf`` that is really a ZIP becomes an unexplained extraction failure later."
    """
    return minimal_pdf("a PDF wearing a .docx extension")


#: Every fixture, keyed by the filename the manifest records. Order is the order the
#: report prints them in and has no other meaning.
GENERATORS: dict[str, Callable[[], bytes]] = {
    "minimal.docx": minimal_docx,
    "minimal.xlsx": minimal_xlsx,
    "minimal.pptx": minimal_pptx,
    "zip-slip-relative.docx": zip_slip_relative_docx,
    "zip-slip-absolute.zip": zip_slip_absolute_zip,
    "duplicate-entries.docx": duplicate_entries_docx,
    "missing-content-types.docx": missing_content_types_docx,
    "deflated.docx": deflated_docx,
    "central-directory-mismatch.docx": central_directory_mismatch_docx,
    "truncated.docx": truncated_docx,
    "empty.zip": empty_zip,
    "entity-expansion.docx": entity_expansion_docx,
    "external-entity.docx": external_entity_docx,
    "deep-nesting.docx": deep_nesting_docx,
    "byte-order-mark.docx": byte_order_mark_docx,
    "invalid-utf8.docx": invalid_utf8_docx,
    "extreme-compression.zip": extreme_compression_zip,
    "macros.docx": macros_docx,
    "unknown-part.docx": unknown_part_docx,
    "pdf-named.docx": pdf_named_docx,
    # docs/SPRINT_0_4_0.md Block B steps 4, 5 and 6. Three defects in released 0.3.0, one
    # fixture each, every one a minimisation of a real file.
    "declared-extent.xlsx": declared_extent_xlsx,
    "chartsheet.xlsx": chartsheet_xlsx,
    "corrupt-deflate.docx": corrupt_deflate_docx,
}


class FixtureTooLarge(Exception):
    """A generator produced bytes this repository will not carry."""


def build(name: str) -> bytes:
    """One fixture, by manifest filename."""
    try:
        generator = GENERATORS[name]
    except KeyError:
        raise KeyError(
            f"no generator named {name!r}; the manifest and fixtures.py have drifted"
        ) from None
    data = generator()
    if len(data) > MAX_FIXTURE_BYTES:
        raise FixtureTooLarge(
            f"{name} is {len(data)} bytes, over the {MAX_FIXTURE_BYTES} byte limit"
        )
    return data


def build_all() -> dict[str, bytes]:
    """Every fixture. A generator that raises takes the whole build down with it."""
    return {name: build(name) for name in GENERATORS}


def write_all(destination: Path) -> dict[str, Path]:
    """Materialise the corpus on disk, for a reader that needs real files."""
    destination.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for name, data in build_all().items():
        path = destination / name
        path.write_bytes(data)
        written[name] = path
    return written


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
