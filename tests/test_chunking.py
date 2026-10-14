"""Tests for text chunking strategies.

Tests the core chunking logic with synthetic text.
No database or model loading required.
"""

import pytest

from jmfts_core.chunking import chunk_text, ChunkStrategy, Chunk

# --------------------------------------------------------------------------- #
# Test data
# --------------------------------------------------------------------------- #

MULTI_SENTENCE = (
    "The quick brown fox jumped over the lazy dog. "
    "It was a sunny day in the meadow. "
    "Birds were singing in the trees. "
    "A gentle breeze carried the scent of wildflowers."
)

MULTI_PARAGRAPH = (
    "First paragraph about topic A. It has multiple sentences.\n\n"
    "Second paragraph about topic B. Also has detail.\n\n"
    "Third paragraph about topic C. Final thoughts here."
)

SHORT_TEXT = "Hello world."

LONG_TEXT = " ".join(f"word{i}" for i in range(500))


# --------------------------------------------------------------------------- #
# Sentence strategy
# --------------------------------------------------------------------------- #


class TestSentenceChunking:
    def test_splits_on_sentence_boundaries(self):
        chunks = chunk_text(MULTI_SENTENCE, strategy=ChunkStrategy.sentence)
        assert len(chunks) == 4
        assert chunks[0].text.startswith("The quick brown fox")
        assert chunks[-1].text.endswith("wildflowers.")

    def test_preserves_full_text(self):
        chunks = chunk_text(MULTI_SENTENCE, strategy=ChunkStrategy.sentence)
        reconstructed = " ".join(c.text for c in chunks)
        # Whitespace may differ but all words should be present
        assert set(MULTI_SENTENCE.split()) == set(reconstructed.split())

    def test_single_sentence_returns_one_chunk(self):
        chunks = chunk_text(SHORT_TEXT, strategy=ChunkStrategy.sentence)
        assert len(chunks) == 1
        assert chunks[0].text == SHORT_TEXT

    def test_indices_are_sequential(self):
        chunks = chunk_text(MULTI_SENTENCE, strategy=ChunkStrategy.sentence)
        for i, chunk in enumerate(chunks):
            assert chunk.index == i

    def test_abbreviations_not_split(self):
        text = "Dr. Smith went to Washington. He met the president."
        chunks = chunk_text(text, strategy=ChunkStrategy.sentence)
        # "Dr." should NOT cause a split
        assert len(chunks) == 2
        assert "Dr. Smith" in chunks[0].text

    def test_a_citation_year_does_not_open_a_sentence(self):
        """The lookahead does the work no abbreviation list can enumerate: a digit
        cannot start a sentence, so `et al. 2017.` stays whole without `al` being
        listed anywhere."""
        text = "We follow Vaswani et al. 2017. The result holds."
        chunks = chunk_text(text, strategy=ChunkStrategy.sentence)

        assert len(chunks) == 2
        assert chunks[0].text == "We follow Vaswani et al. 2017."

    def test_a_nul_in_the_input_is_not_turned_into_a_period(self):
        """The splitter rejects candidate boundaries; it does not rewrite the text.

        Protecting abbreviations by substituting their dots for a NUL placeholder and
        substituting back afterwards silently corrupted any NUL already present — the
        failure every in-band placeholder risks. A NUL now survives the splitter and is
        refused by PostgreSQL at flush, which is a visible error rather than a document
        with periods where its data used to be. `pdf_to_markdown` removes them at the
        source for the path they actually arrive on.
        """
        text = "Value\x00separated from Dr. Smith. Next sentence here."
        chunks = chunk_text(text, strategy=ChunkStrategy.sentence)

        assert "\x00" in chunks[0].text
        assert "Value.separated" not in chunks[0].text


# --------------------------------------------------------------------------- #
# Packed-sentence strategy
# --------------------------------------------------------------------------- #


class TestSentencePackedChunking:
    """Fill sentences to a word budget instead of emitting one node per sentence.

    Over 25 extracted papers the boundary-only strategies leave a large share of nodes
    too short to carry a retrievable idea — 49% of `paragraph` nodes and 21% of
    `sentence` nodes are under 8 words. Packing to 120 words takes that to 0.1%.
    """

    def test_it_packs_several_sentences_into_one_chunk(self):
        # The four sentences are 9, 8, 6 and 8 words. A 20-word budget takes the first
        # two and then the second two.
        chunks = chunk_text(MULTI_SENTENCE, strategy=ChunkStrategy.sentence_packed, max_words=20)

        assert len(chunks) == 2
        assert chunks[0].text.count(".") == 2

    def test_it_never_splits_a_sentence(self):
        chunks = chunk_text(MULTI_SENTENCE, strategy=ChunkStrategy.sentence_packed, max_words=5)

        for chunk in chunks:
            assert chunk.text.rstrip().endswith(".")

    def test_a_sentence_longer_than_the_budget_is_emitted_whole(self):
        """The budget is a target, not a cap. `max_chars` is the cap, and it is what
        cuts a runaway sentence — a reference list with no end punctuation."""
        long_sentence = " ".join(f"word{i}" for i in range(60)) + "."
        chunks = chunk_text(
            long_sentence, strategy=ChunkStrategy.sentence_packed, max_words=10, max_chars=10_000
        )

        assert len(chunks) == 1
        assert len(chunks[0].text.split()) == 60

    def test_the_char_cap_still_applies(self):
        text = " ".join(f"word{i}." for i in range(200))
        chunks = chunk_text(
            text, strategy=ChunkStrategy.sentence_packed, max_words=500, max_chars=100
        )

        assert all(len(c.text) <= 100 for c in chunks)

    def test_preserves_every_word(self):
        chunks = chunk_text(MULTI_SENTENCE, strategy=ChunkStrategy.sentence_packed, max_words=14)

        assert " ".join(c.text for c in chunks).split() == MULTI_SENTENCE.split()


# --------------------------------------------------------------------------- #
# Paragraph strategy
# --------------------------------------------------------------------------- #


class TestParagraphChunking:
    def test_splits_on_double_newlines(self):
        chunks = chunk_text(MULTI_PARAGRAPH, strategy=ChunkStrategy.paragraph)
        assert len(chunks) == 3

    def test_first_chunk_content(self):
        chunks = chunk_text(MULTI_PARAGRAPH, strategy=ChunkStrategy.paragraph)
        assert "topic A" in chunks[0].text

    def test_handles_extra_whitespace(self):
        text = "Para one.\n\n\n\n  \n\nPara two."
        chunks = chunk_text(text, strategy=ChunkStrategy.paragraph)
        assert len(chunks) == 2

    def test_single_paragraph_returns_one_chunk(self):
        text = "Just one paragraph with no double newlines."
        chunks = chunk_text(text, strategy=ChunkStrategy.paragraph)
        assert len(chunks) == 1


# --------------------------------------------------------------------------- #
# Token count strategy
# --------------------------------------------------------------------------- #


class TestTokenCountChunking:
    def test_splits_into_windows(self):
        chunks = chunk_text(LONG_TEXT, strategy=ChunkStrategy.token_count, max_words=100)
        assert len(chunks) == 5  # 500 words / 100 per chunk

    def test_max_words_respected(self):
        chunks = chunk_text(LONG_TEXT, strategy=ChunkStrategy.token_count, max_words=100)
        for chunk in chunks:
            word_count = len(chunk.text.split())
            assert word_count <= 100

    def test_overlap_creates_more_chunks(self):
        no_overlap = chunk_text(
            LONG_TEXT,
            strategy=ChunkStrategy.token_count,
            max_words=100,
            overlap=0,
        )
        with_overlap = chunk_text(
            LONG_TEXT,
            strategy=ChunkStrategy.token_count,
            max_words=100,
            overlap=50,
        )
        assert len(with_overlap) > len(no_overlap)

    def test_overlap_shares_words(self):
        chunks = chunk_text(
            LONG_TEXT,
            strategy=ChunkStrategy.token_count,
            max_words=100,
            overlap=50,
        )
        # Last 50 words of chunk 0 should appear in chunk 1
        words_0 = chunks[0].text.split()
        words_1 = chunks[1].text.split()
        tail_0 = set(words_0[-50:])
        head_1 = set(words_1[:50])
        assert tail_0 == head_1

    def test_small_text_returns_one_chunk(self):
        chunks = chunk_text(SHORT_TEXT, strategy=ChunkStrategy.token_count, max_words=100)
        assert len(chunks) == 1


# --------------------------------------------------------------------------- #
# min_chunk_length merging
# --------------------------------------------------------------------------- #


class TestMinChunkLength:
    def test_short_chunks_merge_into_previous(self):
        # "Ok." is 3 chars — should merge with previous if min_chunk_length > 3
        text = "This is a long sentence that stands on its own. Ok. And another normal sentence follows."
        chunks = chunk_text(text, strategy=ChunkStrategy.sentence, min_chunk_length=10)
        # "Ok." should have merged with the first sentence
        assert all(len(c.text) >= 10 for c in chunks)

    def test_min_chunk_length_1_preserves_all(self):
        chunks = chunk_text(MULTI_SENTENCE, strategy=ChunkStrategy.sentence, min_chunk_length=1)
        assert len(chunks) == 4


# --------------------------------------------------------------------------- #
# Character offsets
# --------------------------------------------------------------------------- #


class TestCharOffsets:
    def test_sentence_offsets_valid(self):
        chunks = chunk_text(MULTI_SENTENCE, strategy=ChunkStrategy.sentence)
        for chunk in chunks:
            assert chunk.char_start >= 0
            assert chunk.char_end > chunk.char_start
            assert chunk.char_end <= len(MULTI_SENTENCE) + 1

    def test_paragraph_offsets_locate_text(self):
        chunks = chunk_text(MULTI_PARAGRAPH, strategy=ChunkStrategy.paragraph)
        for chunk in chunks:
            found = MULTI_PARAGRAPH.find(chunk.text, chunk.char_start)
            assert found == chunk.char_start


# --------------------------------------------------------------------------- #
# Edge cases
# --------------------------------------------------------------------------- #


class TestEdgeCases:
    def test_empty_text_raises(self):
        with pytest.raises(ValueError, match="empty"):
            chunk_text("", strategy=ChunkStrategy.sentence)

    def test_whitespace_only_raises(self):
        with pytest.raises(ValueError, match="empty"):
            chunk_text("   \n\n  ", strategy=ChunkStrategy.sentence)

    def test_unknown_strategy_raises(self):
        with pytest.raises(ValueError):
            chunk_text("hello", strategy="unknown")  # type: ignore

    def test_chunk_dataclass_fields(self):
        chunks = chunk_text(SHORT_TEXT, strategy=ChunkStrategy.sentence)
        c = chunks[0]
        assert isinstance(c, Chunk)
        assert isinstance(c.text, str)
        assert isinstance(c.index, int)
        assert isinstance(c.char_start, int)
        assert isinstance(c.char_end, int)


# --------------------------------------------------------------------------- #
# The measured bound (KNOWN-DEFECTS D7)
# --------------------------------------------------------------------------- #


class TestFitsPredicate:
    """`max_chars` is a proxy for a window measured in tokens; `fits` is the measurement.

    The predicate here is a stand-in for `EmbeddingService.fits_token_window` — a real
    tokenizer is not needed to test that the chunker honours what it is told, and using
    one would make a pure-text test load a model. Each stand-in window is well above
    `_MIN_FIT_CHARS`, as every real one is: 512 subword tokens is upwards of a thousand
    characters even for text that tokenises badly.
    """

    def test_a_piece_the_predicate_rejects_is_split_until_it_is_accepted(self):
        # The shape of D7: `max_chars` says 1800 is fine and the real window says 300 is
        # the most this text can carry, because it tokenises far denser than prose.
        def fits(text):
            return len(text) <= 300

        text = " ".join(f"word{i}" for i in range(200))
        chunks = chunk_text(text, strategy=ChunkStrategy.paragraph, max_chars=1800, fits=fits)

        assert len(chunks) > 1
        assert all(fits(chunk.text) for chunk in chunks)

    def test_without_the_predicate_the_character_cap_is_all_there_is(self):
        """The defect itself, stated as behaviour: 1800 chars can be one chunk."""
        text = " ".join(f"word{i}" for i in range(200))
        chunks = chunk_text(text, strategy=ChunkStrategy.paragraph, max_chars=1800)

        assert len(chunks) == 1
        assert len(chunks[0].text) > 300

    def test_the_merge_cannot_put_a_chunk_back_over_the_window(self):
        """`min_chunk_length` merges in characters, so it must run BEFORE the measure."""

        def fits(text):
            return len(text) <= 200

        text = ". ".join(f"sentence number {i}" for i in range(60)) + "."
        chunks = chunk_text(
            text,
            strategy=ChunkStrategy.sentence,
            min_chunk_length=400,
            max_chars=1800,
            fits=fits,
        )

        assert all(fits(chunk.text) for chunk in chunks)

    def test_a_piece_that_can_never_fit_raises_rather_than_recursing(self):
        chunks_are_never_acceptable = lambda text: False  # noqa: E731

        with pytest.raises(ValueError, match="cannot converge"):
            chunk_text(
                "a long enough piece of ordinary text to be split several times over",
                strategy=ChunkStrategy.sentence,
                fits=chunks_are_never_acceptable,
            )

    def test_text_survives_the_split(self):
        """Splitting for the window must not drop words on the floor."""

        def fits(text):
            return len(text) <= 250

        text = " ".join(f"word{i}" for i in range(300))
        chunks = chunk_text(text, strategy=ChunkStrategy.paragraph, max_chars=1800, fits=fits)

        assert " ".join(chunk.text for chunk in chunks).split() == text.split()


class TestParagraphPackedRespectsTheAuthorsBoundary:
    """``paragraph_packed`` packs to a budget WITHOUT crossing a blank line.

    ``sentence_packed`` fills its budget from the section body as one stream, so the blank
    line — the only boundary the author actually wrote — is invisible to it. These tests
    pin the difference by asserting WHICH sentences land together, because the two
    strategies produce chunks of similar SIZE and differ only in where they cut. No length
    assertion can see that, which is why the defect survived a size histogram.
    """

    #: Sentence N stays identifiable in the output, so a test can name the grouping.
    SENTENCES = {
        letter: f"Sentence {letter} carries one idea and runs to about a dozen words here."
        for letter in "ABCDEFGHIJKLMN"
    }

    def _document(self):
        """Three paragraphs of five, six and three sentences."""
        return "\n\n".join(
            " ".join(self.SENTENCES[c] for c in letters) for letters in ("ABCDE", "FGHIJK", "LMN")
        )

    def _sentences_in(self, text):
        return "".join(c for c in self.SENTENCES if f"Sentence {c} " in text)

    def test_sentence_packed_fuses_two_paragraphs_and_splits_a_third(self):
        """The behaviour being replaced, pinned so a change to it is deliberate."""
        chunks = chunk_text(self._document(), strategy=ChunkStrategy.sentence_packed, max_words=120)

        assert [self._sentences_in(c.text) for c in chunks] == ["ABCDEFGHI", "JKLMN"]

    def test_paragraph_packed_returns_one_chunk_per_paragraph(self):
        chunks = chunk_text(
            self._document(), strategy=ChunkStrategy.paragraph_packed, max_words=120
        )

        assert [self._sentences_in(c.text) for c in chunks] == ["ABCDE", "FGHIJK", "LMN"]

    def test_the_budget_still_applies_inside_an_oversized_paragraph(self):
        """One paragraph over budget is the only case where a boundary is invented."""
        one_long_paragraph = " ".join(self.SENTENCES[c] for c in "ABCDEFGHIJKLMN")

        chunks = chunk_text(
            one_long_paragraph, strategy=ChunkStrategy.paragraph_packed, max_words=40
        )

        assert len(chunks) > 1
        assert " ".join(c.text for c in chunks).split() == one_long_paragraph.split()

    def test_short_paragraphs_are_not_merged_by_the_splitter(self):
        """Merging is ``min_chunk_length``'s decision in ``chunk_text``, not the splitter's.

        Doing it in the splitter would fuse two paragraphs again by another route, which is
        the exact thing this strategy exists to stop.
        """
        text = "Short one.\n\nShort two.\n\nShort three."

        chunks = chunk_text(
            text, strategy=ChunkStrategy.paragraph_packed, max_words=120, min_chunk_length=1
        )

        assert [c.text for c in chunks] == ["Short one.", "Short two.", "Short three."]


class TestChunkCeiling:
    """The EXPLAIN ceiling for the chunk rung must hold against what `chunk_text` writes.

    Issue #5: `_chunk_ceiling` declared `characters // min_chunk_length`, but `fits` splits
    pieces below `min_chunk_length` and a merge never crosses `max_chars`, so a run could
    write more chunks than EXPLAIN said it might. No database: the ceiling is a pure
    function of (characters, min_chunk_length, max_chars).
    """

    @staticmethod
    def _dense_sentences(count: int, width: int = 400) -> str:
        sentence = ("w" * 9 + " ") * (width // 10) + "end."
        return " ".join(sentence for _ in range(count))

    @pytest.mark.parametrize("minimum", [1, 100, 900, 1799, 1800])
    @pytest.mark.parametrize("window", [None, 700, 150])
    def test_actual_count_never_exceeds_ceiling(self, minimum, window):
        from jmfts_core.structure_tasks import _chunk_ceiling

        text = self._dense_sentences(60)
        fits = None if window is None else (lambda piece, w=window: len(piece) <= w)
        chunks = chunk_text(
            text,
            strategy=ChunkStrategy.sentence,
            min_chunk_length=minimum,
            max_chars=1800,
            fits=fits,
        )
        # One `chunk_text` call over the whole text: one section.
        _, ceiling = _chunk_ceiling(
            {"extraction.characters": len(text), "extraction.section_ceiling": 1},
            {"min_chunk_length": minimum},
        )
        assert len(chunks) <= ceiling

    def test_the_old_division_was_exceeded(self):
        """Pin the defect: min near max_chars, a fits that splits, naive ceiling beaten."""
        text = self._dense_sentences(60)
        chunks = chunk_text(
            text,
            strategy=ChunkStrategy.sentence,
            min_chunk_length=1800,
            max_chars=1800,
            fits=lambda piece: len(piece) <= 300,
        )
        assert len(chunks) > len(text) // 1800

    def test_ceiling_holds_across_sections(self):
        """Issue #5's remainder: `_write_chunks` calls `chunk_text` once PER SECTION.

        Every non-empty section body is at least one chunk however short, so a document of
        many short sections writes more chunks than any function of its length alone
        allows. Measured here as the rung measures it: split on headings, chunk each body.
        """
        from jmfts_core.structural_splitting import split_on_headings
        from jmfts_core.structure_tasks import _chunk_ceiling

        # The shortest a section gets: a one-letter title and a one-letter body, 6
        # characters each. The length-only bound allows about a quarter of that many chunks.
        text = "# H\nx\n" * 200
        sections = split_on_headings(text)
        written = sum(
            len(chunk_text(s.content, min_chunk_length=1800, max_chars=1800))
            for s in sections
            if s.content.strip()
        )
        _, ceiling = _chunk_ceiling(
            {
                "extraction.characters": len(text),
                "extraction.section_ceiling": len(sections),
            },
            {"min_chunk_length": 1800},
        )
        assert written == 200
        assert written <= ceiling

    @pytest.mark.parametrize("window", [None, 120, 70])
    @pytest.mark.parametrize("minimum", [1, 50, 1800])
    def test_multi_call_ceiling_holds_on_mixed_sections(self, window, minimum):
        """Short and long sections in one document, with and without a splitting `fits`."""
        from jmfts_core.structural_splitting import section_ceiling, split_on_headings
        from jmfts_core.structure_tasks import _chunk_ceiling

        bodies = ["x", self._dense_sentences(5), "Two words.", self._dense_sentences(1, 90)]
        text = "preamble.\n" + "".join(f"## S{i}\n{bodies[i % 4]}\n" for i in range(40))
        fits = None if window is None else (lambda piece, w=window: len(piece) <= w)
        written = sum(
            len(chunk_text(s.content, min_chunk_length=minimum, max_chars=1800, fits=fits))
            for s in split_on_headings(text)
            if s.content.strip()
        )
        _, ceiling = _chunk_ceiling(
            {
                "extraction.characters": len(text),
                "extraction.section_ceiling": section_ceiling(text),
            },
            {"min_chunk_length": minimum},
        )
        assert written <= ceiling

    def test_one_call_is_the_single_call_bound(self):
        """`calls=1` must be exactly 0.6.2's bound, so a one-section document is unchanged."""
        from jmfts_core.chunking import chunk_count_ceiling

        for characters, minimum in [(1, 1), (5000, 1), (5000, 1800), (123_456, 900)]:
            effective = min(minimum, 1800)
            single = min(characters, (2 * characters // effective + 1) + (3 * characters // 64 + 1))
            assert chunk_count_ceiling(characters, minimum, 1800) == single
            assert chunk_count_ceiling(characters, minimum, 1800, calls=1) == single


class TestSectionCeiling:
    """`section_ceiling` is an upper bound on what BOTH splitters cut. Issue #5."""

    def test_bounds_heading_split(self):
        from jmfts_core.structural_splitting import section_ceiling, split_on_headings

        for text in ["no headings at all.", "# A\nbody", "front\n# A\nx\n## B\ny", "# A\n# B\n"]:
            assert len(split_on_headings(text)) <= section_ceiling(text)

    def test_bounds_outline_split(self):
        from jmfts_core.structural_splitting import section_ceiling, split_on_outline

        text = "Title page.\nIntroduction\nsome text\nMethods\nmore text\nResults\nend"
        toc = [[1, "Introduction", 1], [1, "Methods", 1], [1, "Results", 1], [1, "Missing", 1]]
        sections = split_on_outline(text, toc).sections
        assert len(sections) == 4  # preamble + three located; the fourth is unplaced
        assert len(sections) <= section_ceiling(text, toc)

    def test_empty_text_has_no_sections(self):
        from jmfts_core.structural_splitting import section_ceiling

        assert section_ceiling("   \n") == 0


class TestSentenceWindow:
    """Issue #2: pack whole sentences up to the MEASURED window, not one per chunk."""

    FORTY = " ".join(f"Sentence number {i} says one small thing." for i in range(40))

    @staticmethod
    def _fits(width):
        return lambda piece: len(piece) <= width

    def test_packs_forty_sentences_into_passages(self):
        sentence = ChunkStrategy.sentence
        window = ChunkStrategy.sentence_window
        one_each = chunk_text(self.FORTY, strategy=sentence, fits=self._fits(300))
        packed = chunk_text(self.FORTY, strategy=window, fits=self._fits(300))
        assert len(one_each) == 40
        assert len(packed) < len(one_each) // 4

    def test_every_piece_fits_and_no_sentence_is_split(self):
        fits = self._fits(300)
        chunks = chunk_text(self.FORTY, strategy=ChunkStrategy.sentence_window, fits=fits)
        assert all(fits(c.text) for c in chunks)
        assert all(c.text.endswith(".") for c in chunks)
        assert " ".join(c.text for c in chunks) == self.FORTY

    def test_each_piece_is_as_full_as_the_window_allows(self):
        """Greedy: adding the next piece's first sentence would not have fit."""
        fits = self._fits(300)
        chunks = chunk_text(self.FORTY, strategy=ChunkStrategy.sentence_window, fits=fits)
        for here, after in zip(chunks, chunks[1:]):
            first_next = after.text.split(". ")[0] + "."
            assert not fits(here.text + " " + first_next)

    def test_respects_max_chars_when_the_window_is_wider(self):
        chunks = chunk_text(
            self.FORTY,
            strategy=ChunkStrategy.sentence_window,
            fits=self._fits(10_000),
            max_chars=200,
        )
        assert all(len(c.text) <= 200 for c in chunks)
        assert len(chunks) > 1

    def test_an_oversized_sentence_is_still_split_to_fit(self):
        long = "word " * 200 + "end."
        text = f"Short one. {long} Another short one."
        fits = self._fits(250)
        chunks = chunk_text(text, strategy=ChunkStrategy.sentence_window, fits=fits)
        assert all(fits(c.text) for c in chunks)

    def test_requires_fits(self):
        with pytest.raises(ValueError, match="needs `fits`"):
            chunk_text(self.FORTY, strategy=ChunkStrategy.sentence_window)


class TestChunkRequestRename:
    """Issue #1 on the wire: `POST /documents/{id}/chunk` takes `max_words`."""

    def test_the_old_field_is_refused_not_ignored(self):
        """Pydantic drops an unknown field by default, which would chunk at the default
        and answer 200 for a request that was not carried out."""
        from pydantic import ValidationError

        from jmfts_client.contracts.document import ChunkRequest

        with pytest.raises(ValidationError, match="max_tokens"):
            ChunkRequest(max_tokens=50)
        assert ChunkRequest(max_words=50).max_words == 50
