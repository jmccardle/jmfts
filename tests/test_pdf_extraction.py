"""Integration test for jmfts_core.pdf_extraction.

Skipped when no test PDF is available. Uses a fixture PDF if PyMuPDF can
build one, otherwise checks whatever's on disk under TEST_PDF_PATH.
"""

import os
import re
from pathlib import Path

import pytest

pytest.importorskip("pymupdf")

from jmfts_core.pdf_extraction import pdf_to_markdown

#: The same pattern `structural_splitting.split_on_headings` uses. Duplicated on purpose:
#: these tests assert that extraction emits what THAT regex can see, so importing the
#: splitter's copy would let the two drift into agreement without either being right.
_LINE_START_HEADING = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)


_FIXTURE_PDF: bytes = b""


def _make_fixture_pdf() -> bytes:
    """Build a 1-page PDF with body text and a heading via PyMuPDF."""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    # Heading at large font, body text at default size — IdentifyHeaders
    # picks the most-frequent size as body and bigger sizes as H1+.
    page.insert_text((50, 60), "Big Heading", fontsize=24)
    body = (
        "This is body text in the test PDF. It should appear after the heading "
        "in the resulting markdown. The body has multiple sentences so the "
        "frequency analysis correctly identifies 12pt as body and 24pt as a heading."
    )
    page.insert_text((50, 120), body, fontsize=12)
    return doc.tobytes()


@pytest.fixture(scope="module")
def fixture_pdf_bytes() -> bytes:
    global _FIXTURE_PDF
    if not _FIXTURE_PDF:
        _FIXTURE_PDF = _make_fixture_pdf()
    return _FIXTURE_PDF


def _make_split_heading_pdf() -> bytes:
    """A heading broken across two spans by a font change, with body text under it.

    Both halves of the real failure in one page: PyMuPDF emits the title as two spans,
    and the following body lines land in the same text block, which used to be joined
    into a single line.
    """
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 60), "Big", fontsize=24)
    page.insert_text((92, 60), "Heading", fontsize=24, fontname="hebo")
    page.insert_text((50, 76), "Body text follows immediately under the heading.", fontsize=12)
    page.insert_text((50, 90), "It wraps onto a second line of the same block.", fontsize=12)
    return doc.tobytes()


class TestAHeadingGetsItsOwnLine:
    """`split_on_headings` matches `^#{1,6}\\s`, so a marker anywhere else is invisible.

    The level is decided per span, and the obvious way to use that — writing the '#' as
    each span is emitted — puts the marker wherever the span falls. Over 26 papers that
    lost 189 of 347 markers (54%) to the splitter. Reading the level from the line's
    first span and writing the marker once, at the front, recovers all of them.
    """

    def test_the_marker_is_at_line_start(self):
        md, _ = pdf_to_markdown(_make_split_heading_pdf())

        assert _LINE_START_HEADING.search(md) is not None

    def test_a_multi_span_heading_emits_one_marker_not_one_per_span(self):
        md, _ = pdf_to_markdown(_make_split_heading_pdf())
        level, title = _LINE_START_HEADING.findall(md)[0]

        assert level == "#"
        assert "Big" in title and "Heading" in title
        assert md.count("#") == 1

    def test_body_text_in_the_same_block_does_not_ride_on_the_heading_line(self):
        md, _ = pdf_to_markdown(_make_split_heading_pdf())
        heading_line = md.splitlines()[0]

        assert "Body text" not in heading_line

    def test_wrapped_body_lines_are_still_joined(self):
        """A PDF text block is a wrapped paragraph; its line breaks are typography, not
        structure. Only headings get their own line."""
        md, _ = pdf_to_markdown(_make_split_heading_pdf())

        assert "under the heading. It wraps onto" in md


def _make_small_caps_pdf() -> bytes:
    """A heading set in small caps, which PyMuPDF emits one span per case run.

    The real shape, from `66_Self_RAG_Self_reflective_Re.pdf`:
        ('R', 12pt) ('ELATED', 9.6pt) (' W', 12pt) ('ORK', 9.6pt)
    """
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    x = 50
    for text, size in (("R", 14), ("ELATED", 11), (" W", 14), ("ORK", 11)):
        page.insert_text((x, 60), text, fontsize=size)
        x += len(text) * size * 0.62
    page.insert_text((50, 90), "The body of the section goes here with several words.", fontsize=11)
    return doc.tobytes()


class TestSpanJoining:
    """A span boundary is a font change, not a word boundary.

    Neither rule works alone, and both fail loudly over 26 papers: joining spans with a
    space turns a small-capped "RELATED WORK" into "R ELATED  W ORK" and no outline title
    matches it; concatenating glues ordinary words wherever a font change met a word
    break, which lost 36% of the corpus's words. The page geometry answers it — outline
    titles located rose from 410/466 to 455/466.
    """

    def test_a_small_capped_heading_is_not_broken_into_pieces(self):
        md, _ = pdf_to_markdown(_make_small_caps_pdf())

        assert "RELATED WORK" in md
        assert "R ELATED" not in md

    def test_ordinary_words_are_not_glued_together(self):
        md, _ = pdf_to_markdown(_make_small_caps_pdf())

        assert "The body of the section goes here with several words." in md


class TestPageOffsets:
    """Where each page starts in the markdown.

    The outline's page numbers are the only thing separating a section title from the
    same title printed on a contents page, and 9 of 20 papers print one.
    """

    def test_one_offset_per_page(self, fixture_pdf_bytes):
        _, meta = pdf_to_markdown(fixture_pdf_bytes)

        assert len(meta["page_offsets"]) == meta["page_count"]

    def test_the_first_page_starts_at_zero(self, fixture_pdf_bytes):
        _, meta = pdf_to_markdown(fixture_pdf_bytes)

        assert meta["page_offsets"][0] == 0

    def test_each_offset_lands_on_that_page_s_own_text(self):
        import pymupdf

        doc = pymupdf.open()
        for marker in ("AlphaPageOne", "BetaPageTwo", "GammaPageThree"):
            doc.new_page().insert_text((50, 60), f"{marker} and some body text.", fontsize=12)
        md, meta = pdf_to_markdown(doc.tobytes())

        for offset, marker in zip(meta["page_offsets"], ("Alpha", "Beta", "Gamma")):
            assert md[offset:].startswith(marker)

    def test_offsets_survive_control_character_removal(self):
        """Sanitising the whole document after taking the offsets would shift every one
        of them by the number of characters removed above it."""
        import pymupdf

        doc = pymupdf.open()
        doc.new_page().insert_text((50, 60), "first\x00page\x07text here", fontsize=12)
        doc.new_page().insert_text((50, 60), "SecondPageStartsHere now", fontsize=12)
        md, meta = pdf_to_markdown(doc.tobytes())

        assert meta["control_chars_removed"] == 2
        assert md[meta["page_offsets"][1] :].startswith("SecondPageStartsHere")


def _make_control_char_pdf() -> bytes:
    """A 1-page PDF whose text carries NUL and a few other C0 control characters.

    Real papers arrive this way — a PDF font encoding maps an unmapped glyph to a control
    codepoint and PyMuPDF hands it back verbatim. 9 of 26 papers in a working corpus
    carried one; 4 carried NUL. The character is inserted through the text layer, which is
    what makes it come back out of `get_text()`.
    """
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 60), "before\x00after\x07and\x1fend", fontsize=12)
    return doc.tobytes()


class TestControlCharactersAreRemovedAndCounted:
    """PostgreSQL refuses NUL in any text value.

    ``ValueError: A string literal cannot contain NUL (0x00) characters`` is raised at
    flush, which rolls back the whole extraction transaction and leaves the file node with
    no content at all — a 15%-of-corpus failure with no relation to the document's own
    structure. Stripping them is normalisation of a known encoding artifact, and the count
    is returned so a caller can tell that the text it holds was altered.
    """

    def test_no_control_characters_survive(self):
        md, _ = pdf_to_markdown(_make_control_char_pdf())
        assert "\x00" not in md
        assert not [c for c in md if ord(c) < 0x20 and c not in "\t\n\r"]
        assert "\x7f" not in md

    def test_the_surrounding_text_is_kept(self):
        md, _ = pdf_to_markdown(_make_control_char_pdf())
        assert "before" in md and "after" in md and "end" in md

    def test_the_count_is_reported(self):
        _, meta = pdf_to_markdown(_make_control_char_pdf())
        assert meta["control_chars_removed"] == 3

    def test_clean_text_reports_zero(self, fixture_pdf_bytes):
        _, meta = pdf_to_markdown(fixture_pdf_bytes)
        assert meta["control_chars_removed"] == 0


class TestPdfToMarkdown:
    def test_extracts_body_text(self, fixture_pdf_bytes):
        md, meta = pdf_to_markdown(fixture_pdf_bytes)
        assert "body text" in md.lower()
        assert meta["page_count"] == 1

    def test_metadata_returned(self, fixture_pdf_bytes):
        _, meta = pdf_to_markdown(fixture_pdf_bytes)
        assert "page_count" in meta
        assert "toc" in meta

    def test_accepts_bytes_or_path(self, fixture_pdf_bytes, tmp_path):
        # Bytes path
        md_b, _ = pdf_to_markdown(fixture_pdf_bytes)
        # File path
        f = tmp_path / "test.pdf"
        f.write_bytes(fixture_pdf_bytes)
        md_p, _ = pdf_to_markdown(f)
        assert md_b == md_p

    @pytest.mark.skipif(
        not os.environ.get("TEST_PDF_PATH"),
        reason="set TEST_PDF_PATH to run against a real PDF",
    )
    def test_real_pdf_path(self):
        path = Path(os.environ["TEST_PDF_PATH"])
        assert path.exists()
        md, meta = pdf_to_markdown(path)
        assert isinstance(md, str)
        assert meta["page_count"] >= 1
