"""The office probers. ``docs/OFFICE_SPEC.md`` Part 2, phasing step 4.

No database, and no office reader: everything here is built from `zipfile`, `struct` and
`olefile`, which is the same tier-1 constraint the code under test lives under. A fixture
that needed `python-docx` to construct would prove the prober works on packages
`python-docx` writes, which is the one case that does not matter — the base install that
has to probe a `.docx` is precisely the install with no reader in it.

Hand-built packages also buy the cases a corpus of real files cannot supply on demand: a
document whose heading style is named in German, a workbook whose `xl/workbook.xml` has
gone missing, an OLE2 container holding MS-OFFCRYPTO's two streams, and an element that
straddles the streaming scan's chunk boundary.

WHAT WAS CHECKED AGAINST REAL FILES, and could not be checked here. During development
these probers were run over `.docx`, `.pptx`, `.xlsx`, `.doc`, `.xls` and `.ppt` files
produced by LibreOffice 7.4, and every pattern below agreed with the real package. The one
claim that could NOT be verified against a real file is `is_encrypted`: LibreOffice 7.4
silently declines to encrypt an OOXML export, so the encrypted fixture here is built to
MS-OFFCRYPTO's documented structure — an OLE2 container whose root holds `EncryptionInfo`
and `EncryptedPackage` — rather than copied from Word's output.
"""

from __future__ import annotations

import io
import struct
import zipfile

import pytest

from jmfts_core.ingest_tasks import DECLARED_STRUCTURE_PATTERN, plan_after_probe
from jmfts_core.probe import (
    OOXML_SCAN_CHUNK_BYTES,
    PROBERS_AVAILABLE,
    UNKNOWN_PARTS_REPORTED_MAX,
    EncryptedPackageError,
    FormatDetection,
    detect_format,
    probe_patterns,
)
from jmfts_core.task_errors import ErrorType, classify_exception

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
S_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


# ---------------------------------------------------------------------------
# OOXML fixtures
# ---------------------------------------------------------------------------


def _zip(members: dict[str, str | bytes]) -> bytes:
    """A ZIP of exactly these members, in this order. No compression games, no extras."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _styles(*definitions: str) -> str:
    """A ``word/styles.xml`` carrying these ``<w:style>`` elements and a `w:docDefaults`.

    The doc-defaults block is included in every fixture on purpose: it contains a
    ``w:pPr`` of its own, and a prober that searched the tree for `outlineLvl` instead of
    walking the `w:style` children would find it there.
    """
    return (
        f'<?xml version="1.0"?><w:styles xmlns:w="{W_NS}">'
        '<w:docDefaults><w:pPrDefault><w:pPr><w:spacing w:after="0"/></w:pPr>'
        "</w:pPrDefault></w:docDefaults>" + "".join(definitions) + "</w:styles>"
    )


def _style(
    style_id: str,
    *,
    name: str | None = None,
    outline: int | None = None,
    style_type: str | None = "paragraph",
) -> str:
    parts = [f'<w:style w:styleId="{style_id}"']
    if style_type is not None:
        parts.append(f' w:type="{style_type}"')
    parts.append(">")
    if name is not None:
        parts.append(f'<w:name w:val="{name}"/>')
    if outline is not None:
        parts.append(f'<w:pPr><w:outlineLvl w:val="{outline}"/></w:pPr>')
    parts.append("</w:style>")
    return "".join(parts)


def _document(body: str) -> str:
    return f'<?xml version="1.0"?><w:document xmlns:w="{W_NS}"><w:body>{body}</w:body></w:document>'


def _para(style_id: str | None = None, text: str = "prose") -> str:
    style = f'<w:pPr><w:pStyle w:val="{style_id}"/></w:pPr>' if style_id else ""
    return f"<w:p>{style}<w:r><w:t>{text}</w:t></w:r></w:p>"


def _docx(
    *,
    document: str | None = None,
    styles: str | None = None,
    extra: dict[str, str | bytes] | None = None,
) -> bytes:
    """A minimal but structurally honest `.docx`.

    ``styles=None`` omits `word/styles.xml` entirely, which is the case that makes
    ``has_heading_styles`` unmeasurable rather than false.
    """
    members: dict[str, str | bytes] = {
        "[Content_Types].xml": '<?xml version="1.0"?><Types/>',
        "_rels/.rels": '<?xml version="1.0"?><Relationships/>',
        "word/document.xml": document if document is not None else _document(_para()),
    }
    if styles is not None:
        members["word/styles.xml"] = styles
    members.update(extra or {})
    return _zip(members)


def _pptx(
    *,
    slides: dict[str, str] | None = None,
    notes: dict[str, str] | None = None,
    extra: dict[str, str | bytes] | None = None,
) -> bytes:
    members: dict[str, str | bytes] = {
        "[Content_Types].xml": '<?xml version="1.0"?><Types/>',
        "_rels/.rels": '<?xml version="1.0"?><Relationships/>',
        "ppt/presentation.xml": '<?xml version="1.0"?><p:presentation/>',
    }
    for name, body in (slides or {}).items():
        members[f"ppt/slides/{name}"] = body
        # The relationship part every real slide has. It shares the directory, and a
        # prober that counted directory members would count it as a slide.
        members[f"ppt/slides/_rels/{name}.rels"] = '<?xml version="1.0"?><Relationships/>'
    for name, body in (notes or {}).items():
        members[f"ppt/notesSlides/{name}"] = body
    members.update(extra or {})
    return _zip(members)


def _slide(shapes: str = "") -> str:
    return f'<?xml version="1.0"?><p:sld xmlns:a="{A_NS}"><p:cSld>{shapes}</p:cSld></p:sld>'


def _notes(runs: str) -> str:
    return f'<?xml version="1.0"?><p:notes xmlns:a="{A_NS}">{runs}</p:notes>'


def _xlsx(
    *,
    sheets: list[str] | None = None,
    workbook: str | None = None,
    extra: dict[str, str | bytes] | None = None,
) -> bytes:
    if workbook is None:
        entries = "".join(
            f'<sheet name="{name}" sheetId="{index + 1}"/>'
            for index, name in enumerate(sheets or [])
        )
        workbook = (
            f'<?xml version="1.0"?><workbook xmlns="{S_NS}"><sheets>{entries}</sheets></workbook>'
        )
    members: dict[str, str | bytes] = {
        "[Content_Types].xml": '<?xml version="1.0"?><Types/>',
        "_rels/.rels": '<?xml version="1.0"?><Relationships/>',
        "xl/workbook.xml": workbook,
    }
    members.update(extra or {})
    return _zip(members)


def _probe(data: bytes, filename: str) -> tuple[dict, dict]:
    detection = detect_format(data, filename=filename)
    return probe_patterns(data, detection)


# ---------------------------------------------------------------------------
# OLE2 fixtures — a compound file written by hand
# ---------------------------------------------------------------------------

_SECTOR = 512
_MINI_SECTOR = 64
_FREESECT = 0xFFFFFFFF
_ENDOFCHAIN = 0xFFFFFFFE
_FATSECT = 0xFFFFFFFD
_NOSTREAM = 0xFFFFFFFF

#: The magic `detect_format` matches to call something `ole2`.
OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _dir_entry(name: str, obj_type: int, child: int, right: int, start: int, size: int) -> bytes:
    """One 128-byte directory entry of a v3 compound file.

    Siblings are chained to the right rather than balanced. The format calls the sibling
    structure a red-black tree, but nothing that reads a directory rebalances it, and a
    degenerate chain is what a writer emitting entries in order produces.
    """
    raw = name.encode("utf-16-le") + b"\x00\x00"
    entry = raw.ljust(64, b"\x00")[:64]
    entry += struct.pack("<H", len(raw))
    entry += struct.pack("<BB", obj_type, 1)  # 1 = black
    entry += struct.pack("<III", _NOSTREAM, right, child)
    entry += b"\x00" * 16  # CLSID
    entry += struct.pack("<I", 0)  # state bits
    entry += b"\x00" * 16  # creation / modification times
    entry += struct.pack("<I", start)
    entry += struct.pack("<Q", size)
    assert len(entry) == 128
    return entry


def _ole2(streams: dict[str, bytes]) -> bytes:
    """An OLE2 compound file holding these root-level streams.

    Small enough that every stream goes in the mini stream, which keeps the layout to four
    sectors: FAT, directory, mini FAT, mini stream. That is the whole file — this exists to
    give `olefile.listdir` something real to list, not to be a document.
    """
    placements: list[tuple[str, int, int]] = []
    mini = bytearray()
    mini_fat: list[int] = []
    for name, payload in streams.items():
        start = len(mini) // _MINI_SECTOR
        count = max(1, -(-len(payload) // _MINI_SECTOR))
        mini += payload.ljust(count * _MINI_SECTOR, b"\x00")
        mini_fat += [start + i + 1 for i in range(count - 1)] + [_ENDOFCHAIN]
        placements.append((name, start, len(payload)))

    mini_sectors = max(1, -(-len(mini) // _SECTOR))
    mini += b"\x00" * (mini_sectors * _SECTOR - len(mini))

    # Sector 0 is the FAT itself, 1 the directory, 2 the mini FAT, 3.. the mini stream.
    fat = [_FATSECT, _ENDOFCHAIN, _ENDOFCHAIN]
    fat += [(4 + i) if i < mini_sectors - 1 else _ENDOFCHAIN for i in range(mini_sectors)]
    fat += [_FREESECT] * (_SECTOR // 4 - len(fat))

    entries = [
        _dir_entry(
            "Root Entry",
            5,
            1 if placements else _NOSTREAM,
            _NOSTREAM,
            3,
            len(mini),
        )
    ]
    for index, (name, start, size) in enumerate(placements):
        sibling = index + 2 if index + 1 < len(placements) else _NOSTREAM
        entries.append(_dir_entry(name, 2, _NOSTREAM, sibling, start, size))
    while len(entries) % 4:
        entries.append(b"\x00" * 128)
    directory = b"".join(entries)
    assert len(directory) == _SECTOR, "the fixture supports one directory sector"

    mini_fat += [_FREESECT] * (_SECTOR // 4 - len(mini_fat))

    header = bytearray(_SECTOR)
    header[0:8] = OLE2_MAGIC
    struct.pack_into("<H", header, 0x18, 0x003E)  # minor version
    struct.pack_into("<H", header, 0x1A, 0x0003)  # major version 3
    struct.pack_into("<H", header, 0x1C, 0xFFFE)  # little endian
    struct.pack_into("<H", header, 0x1E, 9)  # 512-byte sectors
    struct.pack_into("<H", header, 0x20, 6)  # 64-byte mini sectors
    struct.pack_into("<I", header, 0x2C, 1)  # one FAT sector
    struct.pack_into("<I", header, 0x30, 1)  # directory starts at sector 1
    struct.pack_into("<I", header, 0x38, 4096)  # mini stream cutoff
    struct.pack_into("<I", header, 0x3C, 2)  # mini FAT starts at sector 2
    struct.pack_into("<I", header, 0x40, 1)  # one mini FAT sector
    struct.pack_into("<I", header, 0x44, _ENDOFCHAIN)  # no DIFAT sectors
    struct.pack_into("<109I", header, 0x4C, 0, *([_FREESECT] * 108))

    return (
        bytes(header)
        + struct.pack("<128I", *fat)
        + directory
        + struct.pack("<128I", *mini_fat)
        + bytes(mini)
    )


# ---------------------------------------------------------------------------
# The dispatch table
# ---------------------------------------------------------------------------


class TestProbersAvailable:
    def test_the_four_office_formats_have_probers(self):
        for fmt in ("docx", "pptx", "xlsx", "ole2"):
            assert fmt in PROBERS_AVAILABLE

    def test_the_patterns_the_scheduler_names_are_the_patterns_probe_reports(self):
        """The whole point of phasing step 4, in one assertion.

        `DECLARED_STRUCTURE_PATTERN` already mapped these three formats to these three
        pattern names; the conditions simply evaluated false because nothing reported them.
        A rename on either side would put the scheduler back where it was — silently
        waiting for a pattern that never arrives — so the two are compared directly rather
        than each being spelled out twice.
        """
        probed = {
            "docx": _probe(
                _docx(
                    document=_document(_para("Heading1")),
                    styles=_styles(_style("Heading1", name="heading 1", outline=0)),
                ),
                "a.docx",
            )[0],
            "pptx": _probe(_pptx(slides={"slide1.xml": _slide()}), "a.pptx")[0],
            "xlsx": _probe(_xlsx(sheets=["Sheet1"]), "a.xlsx")[0],
        }
        for fmt, patterns in probed.items():
            pattern = DECLARED_STRUCTURE_PATTERN[fmt]
            assert patterns[pattern] is True, f"{fmt}: {pattern} not reported true"

    def test_a_declared_outline_reaches_the_declared_rung(self):
        """`plan_after_probe` is what consumes these patterns, so it gets asserted on.

        ``has_text_layer`` is supplied by hand: no office prober reports it, because
        `extract:text` for `docx` is phasing step 5 and enqueuing it today would hand the
        markdown decoder a ZIP. What this proves is the other half — that once extraction
        exists, `has_heading_styles` is the pattern that routes a Word document to the
        declared rung rather than the inferred one.
        """
        patterns, _ = _probe(
            _docx(
                document=_document(_para("Heading1")),
                styles=_styles(_style("Heading1", name="heading 1", outline=0)),
            ),
            "a.docx",
        )
        plan = plan_after_probe("docx", {**patterns, "has_text_layer": True})
        eligible = {spec.task_type for spec in plan.eligible}
        assert "structure:declared" in eligible
        assert "structure:inferred" not in eligible


# ---------------------------------------------------------------------------
# docx
# ---------------------------------------------------------------------------


class TestDocxHeadingStyles:
    def test_an_outline_level_is_what_makes_a_style_a_heading(self):
        data = _docx(
            document=_document(_para("ChapterOpener")),
            styles=_styles(_style("ChapterOpener", name="Chapter Opener", outline=0)),
        )
        patterns, detail = _probe(data, "a.docx")

        assert patterns["has_heading_styles"] is True
        # Nothing about this style's NAME says heading. `w:outlineLvl` is the document
        # stating its own structure, and it is the provenance that gets recorded.
        assert detail["heading_styles"] == {"ChapterOpener": "outline_level"}
        assert detail["heading_styles_used"] == ["ChapterOpener"]

    def test_a_localized_style_id_is_still_a_heading(self):
        """The failure OFFICE_SPEC.md Part 2 names: matching "Heading 1" as a string.

        A German original writes ``w:styleId="Uberschrift1"`` and keeps the invariant
        ``w:name="heading 1"``. String-matching the id would call this document
        structureless; every non-English document would go to the inferred rung.
        """
        data = _docx(
            document=_document(_para("Uberschrift1") + _para("Uberschrift2")),
            styles=_styles(
                _style("Uberschrift1", name="heading 1"),
                _style("Uberschrift2", name="heading 2"),
            ),
        )
        patterns, detail = _probe(data, "a.docx")

        assert patterns["has_heading_styles"] is True
        assert detail["heading_styles"] == {
            "Uberschrift1": "builtin_name",
            "Uberschrift2": "builtin_name",
        }

    def test_a_bare_heading_style_id_is_the_weakest_recognition_and_still_counts(self):
        data = _docx(
            document=_document(_para("Heading3")),
            styles=_styles(_style("Heading3")),
        )
        patterns, detail = _probe(data, "a.docx")

        assert patterns["has_heading_styles"] is True
        assert detail["heading_styles"] == {"Heading3": "style_id"}

    def test_a_style_declared_but_never_used_is_not_a_declared_outline(self):
        """Every Word document defines Heading 1-9 whether or not anything uses them.

        Reporting the STYLE TABLE would therefore report a declared outline for every
        `.docx` ever written, which is not a measurement of anything. The intersection
        with the ids `word/document.xml` actually references is what decides.
        """
        data = _docx(
            document=_document(_para("Normal")),
            styles=_styles(
                _style("Heading1", name="heading 1", outline=0),
                _style("Normal", name="Normal"),
            ),
        )
        patterns, detail = _probe(data, "a.docx")

        assert patterns["has_heading_styles"] is False
        assert "Heading1" in detail["heading_styles"]
        assert detail["heading_styles_used"] == []

    def test_outline_level_nine_is_body_text_and_not_a_heading(self):
        data = _docx(
            document=_document(_para("Quotation")),
            styles=_styles(_style("Quotation", name="Quotation", outline=9)),
        )
        patterns, detail = _probe(data, "a.docx")

        assert patterns["has_heading_styles"] is False
        assert detail["heading_styles"] == {}

    def test_a_character_style_named_like_a_heading_declares_no_outline(self):
        """`w:pStyle` can only reference a paragraph style, so a character style called
        ``Heading1`` is a different object that happens to share a name."""
        data = _docx(
            document=_document(_para("Heading1")),
            styles=_styles(_style("Heading1", name="heading 1", style_type="character")),
        )
        patterns, _ = _probe(data, "a.docx")

        assert patterns["has_heading_styles"] is False

    def test_a_missing_styles_part_is_unmeasured_not_false(self):
        """Fail Early, in the shape `_probe_pdf` already uses for `pages_with_tables`.

        Without `word/styles.xml` there is no way to resolve a style id, so the pattern is
        ABSENT — and `plan_after_probe` then says "was not measured" rather than answering
        "this document declares no outline", which would be a confident wrong answer.
        """
        data = _docx(document=_document(_para("Heading1")), styles=None)
        patterns, detail = _probe(data, "a.docx")

        assert "has_heading_styles" not in patterns
        assert "word/styles.xml" in detail["unmeasured"]["has_heading_styles"]

        plan = plan_after_probe("docx", {**patterns, "has_text_layer": True})
        assert plan.not_applicable["structure:declared"] == (
            "patterns.has_heading_styles was not measured"
        )

    def test_a_heading_element_split_across_the_scan_boundary_is_still_found(self):
        """The overlap in `_iter_chunks`, which is invisible until a document is big.

        `word/document.xml` is streamed in `OOXML_SCAN_CHUNK_BYTES` blocks, so an element
        that straddles a block boundary would be seen as two halves and matched by
        neither. The fixture places the `w:pStyle` deliberately across it.
        """
        head = f'<?xml version="1.0"?><w:document xmlns:w="{W_NS}"><w:body>'
        marker = _para("Heading1")
        offset = OOXML_SCAN_CHUNK_BYTES - 20
        padding = offset - len(head) - len("<!--") - len("-->")
        assert padding > 0
        document = head + "<!--" + "x" * padding + "-->" + marker + "</w:body></w:document>"
        assert document.index(marker) == offset
        assert offset < OOXML_SCAN_CHUNK_BYTES < offset + len(marker)

        patterns, detail = _probe(
            _docx(
                document=document,
                styles=_styles(_style("Heading1", name="heading 1", outline=0)),
            ),
            "big.docx",
        )

        assert patterns["has_heading_styles"] is True
        assert detail["document_bytes_scanned"] == len(document)

    def test_an_unusual_namespace_prefix_is_still_scanned(self):
        """The streaming scan cannot resolve namespaces, so it tolerates any prefix.

        ECMA-376 fixes the namespace URI but not the prefix, and a producer that is not
        Word may bind it to anything. `word/styles.xml` is parsed properly and so is
        unaffected; this is about the part that is too large to parse.
        """
        document = (
            f'<?xml version="1.0"?><ns0:document xmlns:ns0="{W_NS}"><ns0:body>'
            '<ns0:p><ns0:pPr><ns0:pStyle ns0:val="Heading1"/></ns0:pPr></ns0:p>'
            "</ns0:body></ns0:document>"
        )
        patterns, _ = _probe(
            _docx(
                document=document,
                styles=_styles(_style("Heading1", name="heading 1", outline=0)),
            ),
            "a.docx",
        )

        assert patterns["has_heading_styles"] is True


class TestDocxContent:
    def test_tracked_changes_are_reported_with_the_elements_that_showed_them(self):
        document = _document(
            '<w:p><w:ins w:id="1"><w:r><w:t>added</w:t></w:r></w:ins>'
            '<w:del w:id="2"><w:r><w:delText>gone</w:delText></w:r></w:del></w:p>'
        )
        patterns, detail = _probe(_docx(document=document, styles=_styles()), "a.docx")

        assert patterns["has_tracked_changes"] is True
        assert detail["revision_elements"] == ["del", "ins"]

    def test_elements_that_merely_start_with_ins_or_del_are_not_revisions(self):
        """`w:delText` is the text of a deletion and `w:insideH` is a table border.

        Both begin with the three letters the scan looks for. A scan that stopped at the
        prefix would report tracked changes on every document with a bordered table, and
        the extracted text would be recorded as ambiguous for no reason.
        """
        document = _document(
            '<w:tbl><w:tblPr><w:tblBorders><w:insideH w:val="single"/>'
            '<w:insideV w:val="single"/></w:tblBorders></w:tblPr>'
            "<w:tr><w:tc><w:p><w:r><w:delText>not a deletion</w:delText></w:r></w:p>"
            "</w:tc></w:tr></w:tbl>"
        )
        patterns, detail = _probe(_docx(document=document, styles=_styles()), "a.docx")

        assert patterns["has_tracked_changes"] is False
        assert detail["revision_elements"] == []
        # The same document DOES have a table, and `w:tblPr`/`w:tblBorders` are its
        # children — so this fixture pins both edges of the same character class.
        assert patterns["has_tables"] is True

    def test_a_document_without_a_table_says_so(self):
        patterns, _ = _probe(_docx(styles=_styles()), "a.docx")
        assert patterns["has_tables"] is False

    def test_comments_are_counted_rather_than_inferred_from_the_part(self):
        """Word can leave an empty `word/comments.xml` behind after the last comment is
        deleted, so the part's presence is not the measurement — its contents are."""
        empty = f'<?xml version="1.0"?><w:comments xmlns:w="{W_NS}"/>'
        patterns, detail = _probe(
            _docx(styles=_styles(), extra={"word/comments.xml": empty}), "a.docx"
        )
        assert patterns["has_comments"] is False
        assert detail["comment_count"] == 0

        filled = (
            f'<?xml version="1.0"?><w:comments xmlns:w="{W_NS}">'
            '<w:comment w:id="1" w:author="j"><w:p><w:r><w:t>why?</w:t></w:r></w:p>'
            "</w:comment></w:comments>"
        )
        patterns, detail = _probe(
            _docx(styles=_styles(), extra={"word/comments.xml": filled}), "a.docx"
        )
        assert patterns["has_comments"] is True
        assert detail["comment_count"] == 1

    def test_no_comments_part_is_a_measured_zero(self):
        """Absent for a good reason, unlike `word/styles.xml`: Word writes no comments part
        for a document with no comments, so its absence IS the answer."""
        patterns, detail = _probe(_docx(styles=_styles()), "a.docx")
        assert patterns["has_comments"] is False
        assert detail["comment_count"] == 0
        assert "unmeasured" not in detail


# ---------------------------------------------------------------------------
# Facts shared by every OOXML package
# ---------------------------------------------------------------------------


class TestOoxmlCommon:
    def test_a_macro_project_is_reported_without_being_opened(self):
        data = _docx(styles=_styles(), extra={"word/vbaProject.bin": b"\x00MSVBA"})
        patterns, detail = _probe(data, "a.docm")

        assert patterns["has_macros"] is True
        assert detail["macro_part"] == "word/vbaProject.bin"

    def test_images_are_counted_from_the_media_store(self):
        data = _pptx(
            slides={"slide1.xml": _slide()},
            extra={"ppt/media/image1.png": b"\x89PNG", "ppt/media/image2.jpeg": b"\xff\xd8\xff"},
        )
        patterns, _ = _probe(data, "a.pptx")

        assert patterns["has_images"] is True
        assert patterns["image_count"] == 2

    def test_a_package_with_no_media_has_no_images(self):
        patterns, _ = _probe(_xlsx(sheets=["S"]), "a.xlsx")
        assert patterns["has_images"] is False
        assert patterns["image_count"] == 0

    def test_parts_inside_the_format_tree_are_placed_and_foreign_ones_are_not(self):
        """`unknown_parts` is corpus instrumentation, so what it means has to be stable.

        Anything under the package's own tree is placed — including parts this appliance
        does not read — because the alternative is a number that grows every time Microsoft
        ships a part and says nothing about the document. What comes back is genuinely
        foreign material, here the OpenDocument leftovers of a bad conversion.
        """
        data = _docx(
            styles=_styles(),
            extra={
                "word/diagrams/data1.xml": "<dgm/>",
                "docProps/core.xml": "<cp/>",
                "customXml/item1.xml": "<x/>",
                "mimetype": "application/vnd.oasis.opendocument.text",
                "META-INF/manifest.xml": "<manifest/>",
            },
        )
        patterns, _ = _probe(data, "a.docx")

        assert patterns["unknown_parts"] == ["META-INF/manifest.xml", "mimetype"]
        assert patterns["unknown_part_count"] == 2
        assert patterns["part_count"] == 9

    def test_the_unknown_part_list_is_capped_and_says_it_was(self):
        strays = {f"stray/{index:03d}.bin": b"x" for index in range(UNKNOWN_PARTS_REPORTED_MAX + 5)}
        patterns, _ = _probe(_docx(styles=_styles(), extra=strays), "a.docx")

        assert len(patterns["unknown_parts"]) == UNKNOWN_PARTS_REPORTED_MAX
        assert patterns["unknown_part_count"] == UNKNOWN_PARTS_REPORTED_MAX + 5

    def test_an_archive_that_will_not_reopen_fails_permanently(self):
        """`detect_format` read this manifest, so a failure here is not a fact about the
        format — it is bytes that changed under us, and no retry recovers them."""
        whole = _docx(styles=_styles())
        # Truncated mid-upload: the end-of-central-directory record is gone, so `ZipFile`
        # refuses the archive outright even though `detect_format` read it a moment ago.
        data = whole[: whole.rindex(b"PK\x05\x06")]
        detection = FormatDetection(
            format="docx",
            detected_mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            detected_by="zip_manifest",
            declared_mime=None,
        )
        with pytest.raises(ValueError) as caught:
            probe_patterns(data, detection)

        assert "docx" in str(caught.value)
        assert classify_exception(caught.value) is ErrorType.PERMANENT


# ---------------------------------------------------------------------------
# pptx
# ---------------------------------------------------------------------------


class TestPptx:
    def test_slides_are_counted_and_relationship_parts_are_not(self):
        data = _pptx(slides={"slide1.xml": _slide(), "slide2.xml": _slide()})
        patterns, detail = _probe(data, "a.pptx")

        assert patterns["has_slides"] is True
        assert patterns["slide_count"] == 2
        # `ppt/slides/_rels/slideN.xml.rels` sits in the same directory as the slides.
        assert detail["slide_parts"] == 2
        assert patterns["part_count"] == 7

    def test_a_deck_with_no_slides_says_so(self):
        patterns, _ = _probe(_pptx(), "empty.pptx")
        assert patterns["has_slides"] is False
        assert patterns["slide_count"] == 0

    def test_a_generated_slide_number_is_not_speaker_notes(self):
        """The case that makes this pattern worth measuring properly.

        PowerPoint attaches a notes part to slides nobody typed a note on, and that part
        always carries the slide-number field. Counting `a:t` naively reports speaker notes
        on every deck, which would make the pattern useless for deciding anything.
        """
        field_only = _notes(
            f'<a:p xmlns:a="{A_NS}"><a:fld id="1" type="slidenum"><a:t>2</a:t></a:fld></a:p>'
        )
        patterns, detail = _probe(
            _pptx(slides={"slide1.xml": _slide()}, notes={"notesSlide1.xml": field_only}),
            "a.pptx",
        )

        assert patterns["has_speaker_notes"] is False
        # The part EXISTS. Reporting both numbers is what makes the distinction auditable.
        assert detail["notes_parts"] == 1
        assert detail["notes_parts_with_text"] == 0

    def test_typed_notes_are_speaker_notes(self):
        typed = _notes(
            f'<a:p xmlns:a="{A_NS}"><a:fld id="1" type="slidenum"><a:t>2</a:t></a:fld>'
            "<a:r><a:t>Mention the budget.</a:t></a:r></a:p>"
        )
        patterns, detail = _probe(
            _pptx(slides={"slide1.xml": _slide()}, notes={"notesSlide1.xml": typed}),
            "a.pptx",
        )

        assert patterns["has_speaker_notes"] is True
        assert detail["notes_parts_with_text"] == 1

    def test_a_table_on_a_slide_is_found(self):
        with_table = _slide(
            f'<p:graphicFrame><a:graphic xmlns:a="{A_NS}"><a:graphicData>'
            "<a:tbl><a:tr><a:tc/></a:tr></a:tbl></a:graphicData></a:graphic></p:graphicFrame>"
        )
        patterns, _ = _probe(_pptx(slides={"slide1.xml": with_table}), "a.pptx")
        assert patterns["has_tables"] is True

        patterns, _ = _probe(_pptx(slides={"slide1.xml": _slide()}), "b.pptx")
        assert patterns["has_tables"] is False

    def test_smartart_is_reported_because_the_reader_cannot_carry_it(self):
        """`python-pptx` has no diagram support, so extraction will lose this content and
        report success. The pattern is how the node records that it happened."""
        data = _pptx(
            slides={"slide1.xml": _slide()},
            extra={
                "ppt/diagrams/data1.xml": "<dgm/>",
                "ppt/diagrams/layout1.xml": "<dgm/>",
            },
        )
        patterns, detail = _probe(data, "a.pptx")

        assert patterns["has_smartart"] is True
        assert detail["diagram_parts"] == 2

    def test_a_deck_without_diagrams_says_so(self):
        patterns, _ = _probe(_pptx(slides={"slide1.xml": _slide()}), "a.pptx")
        assert patterns["has_smartart"] is False


# ---------------------------------------------------------------------------
# xlsx
# ---------------------------------------------------------------------------


class TestXlsx:
    def test_sheets_come_from_the_workbook_manifest_and_keep_their_names(self):
        """The workbook part is the ORDERED, NAMED list. Counting `xl/worksheets/*.xml`
        would count an orphaned part as a sheet and would not give a caller addressing a
        range later anything to address it by."""
        data = _xlsx(
            sheets=["Revenue", "Assumptions"],
            extra={
                "xl/worksheets/sheet1.xml": "<worksheet/>",
                "xl/worksheets/sheet2.xml": "<worksheet/>",
            },
        )
        patterns, detail = _probe(data, "a.xlsx")

        assert patterns["has_sheets"] is True
        assert patterns["sheet_count"] == 2
        assert detail["sheet_names"] == ["Revenue", "Assumptions"]

    def test_a_workbook_with_no_sheets_says_so(self):
        patterns, _ = _probe(_xlsx(sheets=[]), "a.xlsx")
        assert patterns["has_sheets"] is False
        assert patterns["sheet_count"] == 0

    def test_an_external_reference_is_not_one_of_this_workbooks_sheets(self):
        """`<sheet>` also names sheets of OTHER workbooks in the external-link tree. Only
        the entries carrying a `sheetId` belong to this workbook."""
        workbook = (
            f'<?xml version="1.0"?><workbook xmlns="{S_NS}">'
            '<sheets><sheet name="Local" sheetId="1"/></sheets>'
            '<externalReferences><sheet name="Remote"/></externalReferences></workbook>'
        )
        patterns, detail = _probe(_xlsx(workbook=workbook), "a.xlsx")

        assert patterns["sheet_count"] == 1
        assert detail["sheet_names"] == ["Local"]

    def test_a_missing_workbook_part_is_unmeasured_not_false(self):
        data = _zip(
            {
                "[Content_Types].xml": '<?xml version="1.0"?><Types/>',
                "xl/styles.xml": "<styleSheet/>",
            }
        )
        detection = FormatDetection(
            format="xlsx",
            detected_mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            detected_by="zip_manifest",
            declared_mime=None,
        )
        patterns, detail = probe_patterns(data, detection)

        assert "has_sheets" not in patterns
        assert "xl/workbook.xml" in detail["unmeasured"]["has_sheets"]

    def test_both_kinds_of_excel_comment_are_reported(self):
        """A workbook may use the classic cell note, the threaded comment, or both.
        Reporting only one mechanism would call a commented workbook uncommented."""
        classic = _xlsx(sheets=["S"], extra={"xl/comments1.xml": "<comments/>"})
        assert _probe(classic, "a.xlsx")[0]["has_comments"] is True

        threaded = _xlsx(sheets=["S"], extra={"xl/threadedComments/threadedComment1.xml": "<tc/>"})
        assert _probe(threaded, "b.xlsx")[0]["has_comments"] is True

        assert _probe(_xlsx(sheets=["S"]), "c.xlsx")[0]["has_comments"] is False


# ---------------------------------------------------------------------------
# ole2 — the fork OFFICE_SPEC.md Part 1 put olefile in the base install for
# ---------------------------------------------------------------------------


class TestOle2:
    def test_the_fixture_and_a_legacy_doc_carry_the_same_magic_bytes(self):
        """Which is the whole problem. Both halves of this fork arrive as `ole2`."""
        legacy = _ole2({"WordDocument": b"\xec\xa5" + b"body"})
        encrypted = _ole2({"EncryptionInfo": b"\x04\x00\x04\x00", "EncryptedPackage": b"x" * 64})

        assert detect_format(legacy, filename="old.doc").format == "ole2"
        assert detect_format(encrypted, filename="secret.docx").format == "ole2"
        assert legacy[:8] == encrypted[:8] == OLE2_MAGIC

    @pytest.mark.parametrize(
        "stream,application",
        [
            ("WordDocument", "word"),
            ("Workbook", "excel"),
            ("Book", "excel"),
            ("PowerPoint Document", "powerpoint"),
        ],
    )
    def test_a_legacy_binary_names_its_application(self, stream, application):
        """This is what enqueues `convert:ooxml`, and the application is what picks the
        LibreOffice filter. Verified against real LibreOffice output for the first, second
        and fourth of these; ``Book`` is Excel 5.0/95, which nothing here can still write."""
        patterns, detail = _probe(_ole2({stream: b"\x00" * 32}), "old.doc")

        assert patterns["is_legacy_binary"] is True
        assert patterns["is_encrypted"] is False
        assert patterns["legacy_application"] == application
        assert detail["legacy_binary_stream"] == stream
        assert detail["detected_by"] == "ole2_directory"

    def test_an_encrypted_package_fails_permanently_and_names_the_reason(self):
        """OFFICE_SPEC.md Part 2's one behavioural pattern.

        It must not extract to empty text and settle as a success, and it must not be sent
        to a converter that would fail on it a long way from the cause. The fixture is
        MS-OFFCRYPTO's documented shape; see this module's docstring for why it is not a
        file Word wrote.
        """
        data = _ole2({"EncryptionInfo": b"\x04\x00\x04\x00", "EncryptedPackage": b"x" * 64})

        with pytest.raises(EncryptedPackageError) as caught:
            _probe(data, "secret.docx")

        message = str(caught.value)
        assert "EncryptionInfo" in message and "EncryptedPackage" in message
        assert classify_exception(caught.value) is ErrorType.PERMANENT

    def test_half_the_encryption_pair_is_not_an_encrypted_package(self):
        """A container with one of the two streams is malformed, not encrypted. Failing it
        would permanently refuse an upload a converter might well have read."""
        patterns, _ = _probe(_ole2({"EncryptionInfo": b"\x04\x00\x04\x00"}), "odd.doc")

        assert patterns["is_encrypted"] is False
        assert patterns["is_legacy_binary"] is False

    def test_an_ole2_file_that_is_neither_is_reported_as_neither(self):
        """MSI installers, `Thumbs.db` and Outlook `.msg` files are all OLE2. Reporting one
        as a legacy Word document is how a converter gets handed bytes it cannot read."""
        patterns, detail = _probe(_ole2({"__properties_version1.0": b"\x00" * 32}), "x.msg")

        assert patterns["is_encrypted"] is False
        assert patterns["is_legacy_binary"] is False
        assert "legacy_application" not in patterns
        assert detail["legacy_binary_stream"] is None
        assert detail["streams"] == ["__properties_version1.0"]

    def test_a_container_that_will_not_open_fails_permanently(self):
        data = OLE2_MAGIC + b"\x00" * 500
        with pytest.raises(ValueError) as caught:
            _probe(data, "broken.doc")

        assert "is_encrypted" in str(caught.value)
        assert classify_exception(caught.value) is ErrorType.PERMANENT
