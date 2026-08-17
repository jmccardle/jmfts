"""Tests for structural splitting — markdown headings and declared outlines.

Synthetic documents throughout. No database, no model, no PDF.
"""

from jmfts_core.structural_splitting import (
    nest,
    split_on_headings,
    split_on_outline,
)

# --------------------------------------------------------------------------- #
# Basic behaviour
# --------------------------------------------------------------------------- #


class TestSplitOnHeadingsBasic:
    """Core splitting sanity checks."""

    def test_single_h1_produces_one_section(self):
        text = "# Title\n\nSome content here."
        sections = split_on_headings(text)
        assert len(sections) == 1
        assert sections[0].title == "Title"
        assert sections[0].level == 1
        assert "Some content here." in sections[0].content

    def test_multiple_headings_produce_multiple_sections(self):
        text = "# Intro\n\nHello\n\n## Details\n\nWorld\n\n## Conclusion\n\nBye"
        sections = split_on_headings(text)
        assert len(sections) == 3
        assert sections[0].title == "Intro"
        assert sections[0].level == 1
        assert sections[1].title == "Details"
        assert sections[1].level == 2
        assert sections[2].title == "Conclusion"
        assert sections[2].level == 2

    def test_content_captured_correctly(self):
        text = "# First\n\nParagraph one.\n\n# Second\n\nParagraph two."
        sections = split_on_headings(text)
        assert sections[0].content == "Paragraph one."
        assert sections[1].content == "Paragraph two."

    def test_heading_levels_preserved(self):
        text = "# H1\n\n## H2\n\n### H3\n\n#### H4\n\n##### H5\n\n###### H6\n\n"
        sections = split_on_headings(text)
        assert len(sections) == 6
        for i, section in enumerate(sections):
            assert section.level == i + 1

    def test_source_lines_tracked(self):
        text = "# First\n\nContent\n\n# Second\n\nMore content"
        sections = split_on_headings(text)
        assert sections[0].source_line == 0
        assert sections[1].source_line == 4


# --------------------------------------------------------------------------- #
# Preamble handling
# --------------------------------------------------------------------------- #


class TestPreamble:
    """Content before the first heading."""

    def test_preamble_captured_as_level_zero(self):
        text = "Some intro text.\n\n# Heading\n\nBody"
        sections = split_on_headings(text)
        assert len(sections) == 2
        assert sections[0].level == 0
        assert sections[0].title == ""
        assert "Some intro text." in sections[0].content
        assert sections[1].title == "Heading"

    def test_no_preamble_when_heading_is_first(self):
        text = "# Heading\n\nBody"
        sections = split_on_headings(text)
        assert len(sections) == 1
        assert sections[0].level == 1


# --------------------------------------------------------------------------- #
# No headings — passthrough
# --------------------------------------------------------------------------- #


class TestNoHeadings:
    """Documents without headings pass through as a single section."""

    def test_plain_text_passthrough(self):
        text = "Just a plain document with no headings.\n\nMultiple paragraphs."
        sections = split_on_headings(text)
        assert len(sections) == 1
        assert sections[0].level == 0
        assert sections[0].title == ""
        assert "plain document" in sections[0].content

    def test_empty_string_returns_empty(self):
        assert split_on_headings("") == []

    def test_whitespace_only_returns_empty(self):
        assert split_on_headings("   \n\n  ") == []

    def test_none_returns_empty(self):
        assert split_on_headings(None) == []


# --------------------------------------------------------------------------- #
# Edge cases
# --------------------------------------------------------------------------- #


class TestEdgeCases:
    def test_heading_with_no_body(self):
        text = "# Empty Section\n\n# Next Section\n\nContent"
        sections = split_on_headings(text)
        assert len(sections) == 2
        assert sections[0].title == "Empty Section"
        assert sections[0].content == ""
        assert sections[1].title == "Next Section"

    def test_heading_with_trailing_hashes_not_matched(self):
        # ATX headings can have trailing hashes but our regex captures them as title text
        text = "# Title ##\n\nContent"
        sections = split_on_headings(text)
        assert len(sections) == 1
        # The trailing ## is part of the title text per our regex
        assert "Title" in sections[0].title

    def test_hash_in_code_block_not_treated_as_heading(self):
        # Lines starting with # inside content are matched by the regex,
        # but fenced code blocks aren't special-cased (acceptable for v1)
        text = "# Real Heading\n\nSome code example"
        sections = split_on_headings(text)
        assert sections[0].title == "Real Heading"

    def test_deeply_nested_headings(self):
        text = "###### Deep\n\nContent"
        sections = split_on_headings(text)
        assert len(sections) == 1
        assert sections[0].level == 6
        assert sections[0].title == "Deep"

    def test_seven_hashes_not_a_heading(self):
        text = "####### Not a heading\n\nContent"
        sections = split_on_headings(text)
        assert len(sections) == 1
        assert sections[0].level == 0  # passthrough

    def test_mixed_heading_levels(self):
        text = "# Top\n\nA\n\n### Skip to h3\n\nB\n\n## Back to h2\n\nC"
        sections = split_on_headings(text)
        assert len(sections) == 3
        assert [s.level for s in sections] == [1, 3, 2]

    def test_consecutive_headings_no_content(self):
        text = "# A\n# B\n# C\n"
        sections = split_on_headings(text)
        assert len(sections) == 3
        assert all(s.content == "" for s in sections)


# --------------------------------------------------------------------------- #
# The declared rung: a document's own outline
# --------------------------------------------------------------------------- #

# One page of front matter, then three sections whose titles are printed on a
# contents page BEFORE they appear as sections. That listing is what a naive
# first-match search finds instead of the section, on 9 of 20 papers measured.
_PAGES = [
    "A Paper About Retrieval\n\nJane Doe\n\nAbstract. We study retrieval.\n\n"
    "Contents\n\nIntroduction 2\n\nMethod 2\n\nResults 3",
    "Introduction\n\nRetrieval is hard and this is why.\n\n"
    "Method\n\nWe use late interaction scoring.",
    "Results\n\nRecall improved by nine points.",
]


def _page_offsets(pages: list[str]) -> list[int]:
    """Where each page starts, computed the way `pdf_to_markdown` computes it."""
    offsets, cursor = [], 0
    for page in pages:
        offsets.append(cursor)
        cursor += len(page) + 2  # the "\n\n" the pages are joined with
    return offsets


PAPER = "\n\n".join(_PAGES)
PAGE_OFFSETS = _page_offsets(_PAGES)
OUTLINE = [[1, "Introduction", 2], [1, "Method", 2], [1, "Results", 3]]


class TestSplitOnOutline:
    def test_it_finds_the_section_not_the_contents_listing(self):
        """The whole reason `page_offsets` is threaded through from extraction."""
        split = split_on_outline(PAPER, OUTLINE, PAGE_OFFSETS)

        intro = next(s for s in split.sections if s.title == "Introduction")
        assert intro.content == "Retrieval is hard and this is why."

    def test_without_page_offsets_it_matches_the_listing(self):
        """Stated as a test so the dependency is not silently droppable: the same
        document, the same outline, no page offsets, and every section lands in the
        contents page."""
        split = split_on_outline(PAPER, OUTLINE, None)

        intro = next(s for s in split.sections if s.title == "Introduction")
        assert intro.content != "Retrieval is hard and this is why."

    def test_front_matter_becomes_a_level_0_section(self):
        split = split_on_outline(PAPER, OUTLINE, PAGE_OFFSETS)

        assert split.sections[0].level == 0
        assert split.sections[0].title == ""
        assert "Jane Doe" in split.sections[0].content

    def test_no_front_matter_section_when_the_outline_starts_at_the_beginning(self):
        text = "Introduction\n\nStraight in with no title page.\n"
        split = split_on_outline(text, [[1, "Introduction", 1]], [0])

        assert len(split.sections) == 1
        assert split.sections[0].title == "Introduction"

    def test_the_last_section_runs_to_the_end_of_the_document(self):
        """There is no "after the last section". Appendices and references carry their
        own outline entry when the document declares one, and belong to the section they
        follow when it does not."""
        text = "Body\n\nOne.\n\nAppendix A\n\nExtra material nobody indexed.\n"
        split = split_on_outline(text, [[1, "Body", 1]], [0])

        assert "Extra material nobody indexed." in split.sections[-1].content

    def test_a_title_that_is_not_in_the_text_is_reported_not_dropped(self):
        text = "Introduction\n\nOnly this section exists here.\n"
        outline = [[1, "Introduction", 1], [1, "Conclusion", 1]]

        split = split_on_outline(text, outline, [0])

        assert split.unplaced == ["Conclusion"]
        # Its text is not lost — there simply was none, and the section it would have
        # started stays part of its predecessor.
        assert "Only this section exists here." in split.sections[0].content

    def test_emphasis_and_case_do_not_prevent_a_match(self):
        """Extraction writes `**Bold Title**` and `# Heading`; the outline entry is the
        bare words."""
        text = "# **INTRODUCTION**\n\nBody text of the section.\n"
        split = split_on_outline(text, [[1, "Introduction", 1]], [0])

        assert split.unplaced == []
        assert split.sections[0].content == "Body text of the section."

    def test_a_document_with_no_outline_is_one_section(self):
        split = split_on_outline("Just some prose with no structure.", [], None)

        assert len(split.sections) == 1
        assert split.sections[0].level == 0
        assert split.unplaced == []

    def test_empty_text_produces_nothing(self):
        assert split_on_outline("   ", [[1, "Introduction", 1]], [0]).sections == []


# --------------------------------------------------------------------------- #
# Levels -> tree
# --------------------------------------------------------------------------- #


class TestNest:
    def test_it_builds_the_declared_hierarchy(self):
        split = split_on_outline(
            "S1\n\na\n\nS1.1\n\nb\n\nS1.1.1\n\nc\n\nS2\n\nd\n",
            [[1, "S1", 1], [2, "S1.1", 1], [3, "S1.1.1", 1], [1, "S2", 1]],
            [0],
        )
        roots = nest(split.sections)

        assert [r.section.title for r in roots] == ["S1", "S2"]
        assert [c.section.title for c in roots[0].children] == ["S1.1"]
        assert [c.section.title for c in roots[0].children[0].children] == ["S1.1.1"]

    def test_it_reproduces_a_wrong_hierarchy_rather_than_correcting_it(self):
        """The decision this module is built on. A paper whose outline files section 3
        under section 2 gets a tree that files section 3 under section 2. Rebuilding
        from the numbering in the titles would fix this case and would itself be a
        heuristic over freeform input, failing differently elsewhere. The text stays
        retrievable either way; only a rollup is affected.
        """
        split = split_on_outline(
            "2 Space\n\na\n\n3 Coherence\n\nb\n",
            [[1, "2 Space", 1], [2, "3 Coherence", 1]],  # the file says 3 nests under 2
            [0],
        )
        roots = nest(split.sections)

        assert len(roots) == 1
        assert roots[0].section.title == "2 Space"
        assert [c.section.title for c in roots[0].children] == ["3 Coherence"]

    def test_a_skipped_level_attaches_to_the_nearest_lower_ancestor(self):
        split = split_on_outline("Top\n\na\n\nDeep\n\nb\n", [[1, "Top", 1], [3, "Deep", 1]], [0])
        roots = nest(split.sections)

        assert len(roots) == 1
        assert [c.section.title for c in roots[0].children] == ["Deep"]

    def test_front_matter_is_a_sibling_of_the_sections_never_their_parent(self):
        """An abstract does not contain the paper."""
        roots = nest(split_on_outline(PAPER, OUTLINE, PAGE_OFFSETS).sections)

        assert [r.section.title for r in roots] == ["", "Introduction", "Method", "Results"]
        assert roots[0].children == []

    def test_headings_and_outlines_nest_the_same_way(self):
        """`nest` takes levelled sections; it does not care which rung produced them."""
        roots = nest(split_on_headings("# A\n\nx\n\n## B\n\ny\n\n# C\n\nz\n"))

        assert [r.section.title for r in roots] == ["A", "C"]
        assert [c.section.title for c in roots[0].children] == ["B"]
