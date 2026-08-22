"""Integration test for jmfts_core.pdf_extraction.

Skipped when no test PDF is available. Uses a fixture PDF if PyMuPDF can
build one, otherwise checks whatever's on disk under TEST_PDF_PATH.
"""

import os
import re
from pathlib import Path

import pytest

pytest.importorskip("pymupdf")

from jmfts_core.pdf_extraction import _clean_cell, pdf_to_markdown

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


def _make_table_pdf(cells, *, row_height: int = 26, cell_lines: dict | None = None) -> bytes:
    """A page with prose, a ruled table, and more prose.

    Ruled, because the `lines`/`lines` strategy PyMuPDF defaults to finds tables from
    drawn rules and nothing else. `cell_lines` maps (row, col) to a list of strings
    stacked inside one cell, which is how a real header cell comes back carrying a
    newline.
    """
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    x0, y0, col_width = 60, 100, 150
    ncols, nrows = len(cells[0]), len(cells)

    page.insert_text((60, 70), "Prose before the table sits here.", fontsize=11)
    for r in range(nrows + 1):
        page.draw_line((x0, y0 + r * row_height), (x0 + ncols * col_width, y0 + r * row_height))
    for c in range(ncols + 1):
        page.draw_line((x0 + c * col_width, y0), (x0 + c * col_width, y0 + nrows * row_height))

    for r, row in enumerate(cells):
        for c, value in enumerate(row):
            stacked = (cell_lines or {}).get((r, c))
            if stacked:
                for i, piece in enumerate(stacked):
                    page.insert_text(
                        (x0 + c * col_width + 4, y0 + r * row_height + 12 + i * 12),
                        piece,
                        fontsize=9,
                    )
            elif value:
                page.insert_text(
                    (x0 + c * col_width + 4, y0 + r * row_height + 17), value, fontsize=10
                )

    page.insert_text(
        (60, y0 + nrows * row_height + 40), "Prose after the table sits here.", fontsize=11
    )
    return doc.tobytes()


def _table_blocks(md: str) -> list[list[str]]:
    """Every markdown table in the output, as its list of lines."""
    return [b.split("\n") for b in md.split("\n\n") if b.startswith("|")]


class TestATableRowIsOneLine:
    """A markdown table row is one line; the pipes are column separators, not text.

    PyMuPDF returns multi-line cells with their newlines intact, and stacked column
    headers are ordinary in academic tables. Measured on Table 3 of "Attention Is All You
    Need": the 3-cell header row came out as FOUR physical lines, which pushed the
    `| --- |` separator to row index 4 and left the block carrying 0, 1, 2 and 4 pipes on
    different lines. No parser reads that as a table.
    """

    def test_a_newline_inside_a_cell_does_not_end_the_row(self):
        md, _ = pdf_to_markdown(
            _make_table_pdf(
                [["", "Rate"], ["base", ""], ["big", ""]],
                row_height=40,
                cell_lines={(0, 0): ["train", "steps"], (1, 1): ["4.92", "25.8"]},
            )
        )
        blocks = _table_blocks(md)

        assert blocks, "no markdown table was emitted"
        for line in blocks[0]:
            assert "\n" not in line

    def test_the_separator_is_the_second_row(self):
        md, _ = pdf_to_markdown(
            _make_table_pdf(
                [["", "Rate"], ["base", ""], ["big", ""]],
                row_height=40,
                cell_lines={(0, 0): ["train", "steps"], (1, 1): ["4.92", "25.8"]},
            )
        )
        block = _table_blocks(md)[0]

        assert set(block[1].replace("|", "").replace(" ", "")) == {"-"}

    def test_every_row_has_the_same_column_count(self):
        md, _ = pdf_to_markdown(
            _make_table_pdf(
                [["", "Rate"], ["base", ""], ["big", ""]],
                row_height=40,
                cell_lines={(0, 0): ["train", "steps"], (1, 1): ["4.92", "25.8"]},
            )
        )
        block = _table_blocks(md)[0]

        assert len({line.count("|") for line in block}) == 1

    def test_a_pipe_in_a_cell_is_escaped_so_it_cannot_invent_a_column(self):
        assert _clean_cell("a|b") == r"a\|b"

    def test_whitespace_runs_collapse_to_one_space(self):
        assert _clean_cell("train\nN  d\tmodel") == "train N d model"

    def test_a_missing_cell_is_empty_not_the_word_none(self):
        assert _clean_cell(None) == ""


class TestTableTextIsEmittedOnce:
    """A table is drawn from the same text blocks the prose pass walks.

    Rendering both independently — which is what appending every table after the prose
    did — put the same characters in the output twice. Measured on `attn.pdf`: 101 of 174
    table cells (58%) appeared twice on their own page and the table pass added 9,770
    characters, 23.7% on top of the prose. That is text the chunker splits, the embedder
    pays for, and BM25 counts twice.
    """

    def test_a_cell_s_text_appears_once(self):
        md, _ = pdf_to_markdown(
            _make_table_pdf([["Name", "Role"], ["Ada", "Engineer"], ["Bo", "Designer"]])
        )

        assert md.count("Engineer") == 1

    def test_the_table_sits_where_its_content_was_not_at_the_end_of_the_page(self):
        md, _ = pdf_to_markdown(
            _make_table_pdf([["Name", "Role"], ["Ada", "Engineer"], ["Bo", "Designer"]])
        )

        assert md.index("Prose before") < md.index("| Name") < md.index("Prose after")

    def test_the_prose_around_the_table_survives(self):
        md, _ = pdf_to_markdown(
            _make_table_pdf([["Name", "Role"], ["Ada", "Engineer"], ["Bo", "Designer"]])
        )

        assert "Prose before the table sits here." in md
        assert "Prose after the table sits here." in md


class TestASparseGridIsNotATable:
    """A grid is not a table just because something drew lines.

    Half the candidates `find_tables` reports on `attn.pdf` are attention-visualisation
    FIGURES, drawn as a grid of ruled cells, which is exactly what the `lines` strategy
    looks for. They fill 18-36% of their cells; the two real tables fill 96% and 100%.

    This filter is what makes excluding a table's blocks from the prose safe. Without it
    a figure still swallows its text blocks and the page emits a row of empty pipes in
    place of the words that were there.
    """

    def test_a_mostly_empty_grid_is_rejected(self):
        sparse = _make_table_pdf(
            [["a", "", "", ""], ["", "", "b", ""], ["", "", "", ""], ["", "c", "", ""]]
        )
        _, meta = pdf_to_markdown(sparse)

        assert meta["tables_found"] == 0
        assert meta["tables_rejected"], "a sparse grid should be reported, not silently dropped"
        assert "cells carry text" in meta["tables_rejected"][0]["reason"]

    def test_a_rejected_grid_keeps_its_text_as_prose(self):
        """The whole point: the page must read as it did before tables existed."""
        sparse = _make_table_pdf(
            [["alpha", "", "", ""], ["", "", "beta", ""], ["", "", "", ""], ["", "gamma", "", ""]]
        )
        md, _ = pdf_to_markdown(sparse)

        assert "alpha" in md and "beta" in md and "gamma" in md
        assert not _table_blocks(md)

    def test_a_full_grid_is_kept(self):
        dense = _make_table_pdf([["Name", "Role"], ["Ada", "Engineer"], ["Bo", "Designer"]])
        _, meta = pdf_to_markdown(dense)

        assert meta["tables_found"] == 1
        assert meta["tables_rejected"] == []


class TestTableFailuresAreReported:
    """The bare ``except Exception: pass`` made a failed page and an empty one identical.

    ``find_tables`` already swallows its own exceptions and returns ``None``, so reading
    ``.tables`` off it raises ``AttributeError`` — which the old handler absorbed. The
    text of the page still extracts, so this is reported like ``control_chars_removed``
    rather than raised: the caller is told what about the output is incomplete.
    """

    def test_an_exception_is_recorded_against_its_page(self, monkeypatch):
        import pymupdf

        def explode(self, *args, **kwargs):
            raise RuntimeError("no tables for you")

        monkeypatch.setattr(pymupdf.Page, "find_tables", explode)
        _, meta = pdf_to_markdown(_make_table_pdf([["Name", "Role"], ["Ada", "Engineer"]]))

        assert len(meta["table_failures"]) == 1
        assert meta["table_failures"][0]["page"] == 0
        assert "no tables for you" in meta["table_failures"][0]["error"]

    def test_find_tables_returning_none_is_recorded(self, monkeypatch):
        import pymupdf

        monkeypatch.setattr(pymupdf.Page, "find_tables", lambda self, *a, **k: None)
        _, meta = pdf_to_markdown(_make_table_pdf([["Name", "Role"], ["Ada", "Engineer"]]))

        assert len(meta["table_failures"]) == 1
        assert "returned None" in meta["table_failures"][0]["error"]

    def test_the_page_s_prose_survives_a_table_failure(self, monkeypatch):
        import pymupdf

        def explode(self, *args, **kwargs):
            raise RuntimeError("no tables for you")

        monkeypatch.setattr(pymupdf.Page, "find_tables", explode)
        md, _ = pdf_to_markdown(_make_table_pdf([["Name", "Role"], ["Ada", "Engineer"]]))

        assert "Prose before the table sits here." in md
        assert "Engineer" in md

    def test_a_clean_document_reports_no_failures(self, fixture_pdf_bytes):
        _, meta = pdf_to_markdown(fixture_pdf_bytes)

        assert meta["table_failures"] == []
        assert meta["tables_found"] == 0

    def test_detection_is_not_run_at_all_when_tables_are_off(self, monkeypatch):
        """`extract_tables=False` must not reach the detector, so it cannot fail there."""
        import pymupdf

        def explode(self, *args, **kwargs):
            raise AssertionError("find_tables was called with extract_tables=False")

        monkeypatch.setattr(pymupdf.Page, "find_tables", explode)
        md, meta = pdf_to_markdown(
            _make_table_pdf([["Name", "Role"], ["Ada", "Engineer"]]), extract_tables=False
        )

        assert meta["tables_found"] == 0
        assert meta["table_failures"] == []
        assert "Engineer" in md


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


def _make_two_column_prose_pdf() -> bytes:
    """A page of ordinary two-column prose, with no rules drawn anywhere.

    Two columns of body text, aligned at fixed x positions, is what an academic paper
    looks like on nearly every page. It is NOT a table, and nothing on the page is drawn.
    """
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    left = [
        "Retrieval systems combine several ranking",
        "signals before a final ordering is chosen.",
        "Lexical matching remains competitive on",
        "queries that name a rare entity directly.",
        "Dense retrieval generalises better when the",
        "query and the document share no terms.",
    ]
    right = [
        "Late interaction keeps one vector for each",
        "token instead of one vector per passage.",
        "The cost is storage; the benefit is that a",
        "match can be localised inside a passage.",
        "Hybrid scoring weights the two components",
        "and is usually tuned on a development set.",
    ]
    for i, line in enumerate(left):
        page.insert_text((60, 100 + i * 14), line, fontsize=9)
    for i, line in enumerate(right):
        page.insert_text((320, 100 + i * 14), line, fontsize=9)
    return doc.tobytes()


class TestProseIsNeverReadAsATable:
    """Two-column prose must not be detected as a table, and this is load-bearing.

    Since `_table_owner` landed, a block a table claims is REMOVED from the prose stream
    so its text is not emitted twice. That makes a false positive far more expensive than
    a spurious table: the page's words are deleted from the output entirely.

    `_TABLE_MIN_FILL` does not protect against this. It rejects grids of EMPTY cells, and
    prose cells are full of text. Measured over the 15 pages of "Attention Is All You
    Need" with `vertical_strategy="text"` and `horizontal_strategy="text"`: 14 of 15 pages
    produced a candidate, 10 of them on pages holding no table at all, and every one
    scored between 0.53 and 0.69 fill — comfortably past the 0.5 threshold.

    The `lines` strategy the module uses cannot fire here because nothing is drawn on the
    page. This test pins that, so a later change of strategy fails loudly rather than
    quietly dropping prose.
    """

    def test_no_table_is_emitted_for_two_column_prose(self):
        md, meta = pdf_to_markdown(_make_two_column_prose_pdf())

        assert _table_blocks(md) == [], "two-column prose was rendered as a markdown table"
        assert meta["tables_rejected"] == []
        assert meta["table_failures"] == []

    def test_no_prose_is_lost_from_a_two_column_page(self):
        md, _ = pdf_to_markdown(_make_two_column_prose_pdf())

        # Both columns survive, and the words of each stay in the output.
        assert "Retrieval systems combine several ranking" in md
        assert "Late interaction keeps one vector for each" in md
        for phrase in ("development set", "rare entity directly", "localised inside a passage"):
            assert phrase in md, f"{phrase!r} was dropped from the page"


def _make_multi_page_table_pdf(table_pages: set[int], *, pages: int = 3) -> bytes:
    """A `pages`-page PDF carrying a dense ruled table on exactly `table_pages`.

    Every page gets prose, so a page without a table is still a page with content and the
    two cases cannot be told apart by emptiness.
    """
    import pymupdf

    cells = [["Name", "Role"], ["Ada", "Engineer"], ["Bo", "Designer"]]
    x0, y0, col_width, row_height = 60, 100, 150, 26
    doc = pymupdf.open()
    for number in range(pages):
        page = doc.new_page()
        page.insert_text((60, 70), f"Prose on page {number}.", fontsize=11)
        if number not in table_pages:
            continue
        ncols, nrows = len(cells[0]), len(cells)
        for r in range(nrows + 1):
            page.draw_line((x0, y0 + r * row_height), (x0 + ncols * col_width, y0 + r * row_height))
        for c in range(ncols + 1):
            page.draw_line((x0 + c * col_width, y0), (x0 + c * col_width, y0 + nrows * row_height))
        for r, row in enumerate(cells):
            for c, value in enumerate(row):
                page.insert_text(
                    (x0 + c * col_width + 4, y0 + r * row_height + 16), value, fontsize=9
                )
    data = doc.tobytes()
    doc.close()
    return data


class TestPagesWithTablesIsReportedHere:
    """`pages_with_tables` is a Part 4 PATTERN, and extraction is what measures it.

    `probe` used to, on every page of every PDF, and `_process_page` then did the same
    scan a second time. Only one copy could go: `_table_owner` needs the geometry while
    the prose is being written, so extraction cannot defer it. See
    `jmfts_core.probe._probe_pdf` for the removal and INGEST_SPEC.md 3.3 for the rule
    that lets a pattern arrive after probe.
    """

    def test_the_page_numbers_are_reported_and_are_zero_based(self):
        _, meta = pdf_to_markdown(_make_multi_page_table_pdf({1}))

        assert meta["pages_with_tables"] == [1]
        assert meta["page_count"] == 3

    def test_every_page_carrying_one_is_listed_in_page_order(self):
        _, meta = pdf_to_markdown(_make_multi_page_table_pdf({0, 2}, pages=4))

        assert meta["pages_with_tables"] == [0, 2]
        assert meta["tables_found"] == 2

    def test_a_document_with_no_tables_reports_an_empty_list(self):
        _, meta = pdf_to_markdown(_make_multi_page_table_pdf(set()))

        assert meta["pages_with_tables"] == []
        assert meta["tables_found"] == 0

    def test_a_rejected_grid_does_not_make_a_page_with_a_table(self):
        """It counts RENDERED tables, not candidates, and that is the correctness gain.

        `probe` counted raw `find_tables()` output, so the three attention-visualisation
        figure pages of `attn.pdf` were reported as pages with tables. There is no table
        on them to give a node to.
        """
        sparse = _make_table_pdf(
            [["a", "", "", ""], ["", "", "b", ""], ["", "", "", ""], ["", "c", "", ""]]
        )
        _, meta = pdf_to_markdown(sparse)

        assert meta["tables_rejected"], "the fixture must produce a candidate to reject"
        assert meta["pages_with_tables"] == []
