"""Regression tests for the four silent-failure defects in docs/KNOWN-DEFECTS.md.

Each test below reproduces a defect that shipped: the system lost text (D1, D2, D4)
or corrupted a statistic (D3) and reported success in every case. They are written
to fail against the pre-fix code, so that "chunking succeeded" can never again mean
"chunking silently did nothing".

D1, D2 and D4 are pure text/tokenizer operations — no database, no model weights.
D3 is a database integration test using the savepoint-rollback fixture.
"""

import pytest

from jmfts_core.chunking import ChunkStrategy, chunk_text
from jmfts_core.config import get_settings

# --------------------------------------------------------------------------- #
# Test data — the exact inputs that reproduced each defect
# --------------------------------------------------------------------------- #

# ~90 sentences averaging ~75 chars, none of which reaches min_chunk_length=100.
# Pre-fix, the merge loop glued all 90 into a single 6,840-char chunk.
MANY_SHORT_SENTENCES = " ".join(
    f"Sentence number {i} is short and carries a little payload of text." for i in range(90)
)

# A single unbroken "word" — a base64 blob, a minified JS line, a data URI.
# Pre-fix, a whitespace-based splitter could not split this at all.
UNBROKEN_TOKEN = "A" * 15_000

# One long paragraph with no double-newline and no sentence variety — the shape
# of most assistant messages. Pre-fix, `sentence` strategy returned it whole.
ONE_LONG_PARAGRAPH = "This is a clause that just keeps going and going " * 200


# --------------------------------------------------------------------------- #
# D1 — the embedder truncated at 512 tokens and said nothing
# --------------------------------------------------------------------------- #


class TestD1EmbedderTruncation:
    """The token-selection window is a memory budget, not a secret.

    `nomic-ai/modernbert-embed-base` handles 8192 tokens; the token-level path is
    capped at 512 because eager `output_attentions=True` materialises
    layers x heads x seq^2 floats (22 * 12 * 512^2 * 4B ~= 277 MB; 8192 would be ~71 GB).
    The cap is legitimate. Silently embedding a prefix and reporting success is not.
    """

    def test_token_window_is_configurable(self):
        """The 512 was hard-coded in two places. It must be one setting."""
        settings = get_settings()
        assert hasattr(settings, "embedding_token_window"), (
            "embedding_token_window must be a setting, not a literal buried in "
            "embedding.py:180 and :290"
        )
        assert settings.embedding_token_window == 512

    def test_over_window_text_is_reported_not_silently_truncated(self):
        """A caller must be able to learn that its text did not fit."""
        from jmfts_core.embedding import get_embedding_service

        service = get_embedding_service()
        # ~5 chars/token for English, so 2000 words ~= 2000 tokens — 4x the window.
        long_text = "word " * 2000
        fit = service.check_fit(long_text)

        assert fit.truncated is True
        assert fit.token_count > settings_window()
        assert fit.tokens_dropped > 0
        assert fit.chars_total == len(long_text)

    def test_within_window_text_is_not_flagged(self):
        from jmfts_core.embedding import get_embedding_service

        fit = get_embedding_service().check_fit("A short document about foxes.")
        assert fit.truncated is False
        assert fit.tokens_dropped == 0


def settings_window() -> int:
    return get_settings().embedding_token_window


# --------------------------------------------------------------------------- #
# D2 — the chunker did not bound chunk size on either default path
# --------------------------------------------------------------------------- #


class TestD2ChunkerDoesNotBoundSize:
    def test_merge_loop_does_not_grow_without_bound(self):
        """Reproduces the headline case: 90 short sentences -> ONE 6,840-char chunk.

        Every sentence was under min_chunk_length, so each merged into the first,
        forever. The document was chunked into itself.
        """
        max_chars = 1800
        chunks = chunk_text(
            MANY_SHORT_SENTENCES,
            strategy=ChunkStrategy.sentence,
            min_chunk_length=100,
            max_chars=max_chars,
        )

        assert len(chunks) > 1, (
            f"the whole document collapsed into {len(chunks)} chunk of "
            f"{len(chunks[0].text)} chars — the merge loop is unbounded"
        )
        for c in chunks:
            assert (
                len(c.text) <= max_chars
            ), f"chunk {c.index} is {len(c.text)} chars, over the {max_chars} cap"

    def test_sentence_strategy_bounds_a_long_paragraph(self):
        """`max_tokens` was honoured by token_count only; sentence/paragraph ignored it.

        A single long paragraph — most assistant messages — came back as one chunk
        that still blew the embedder's window.
        """
        max_chars = 1800
        chunks = chunk_text(
            ONE_LONG_PARAGRAPH,
            strategy=ChunkStrategy.sentence,
            max_chars=max_chars,
        )

        assert len(chunks) > 1
        for c in chunks:
            assert len(c.text) <= max_chars

    def test_paragraph_strategy_bounds_a_long_paragraph(self):
        max_chars = 1800
        chunks = chunk_text(
            ONE_LONG_PARAGRAPH,
            strategy=ChunkStrategy.paragraph,
            max_chars=max_chars,
        )

        assert len(chunks) > 1
        for c in chunks:
            assert len(c.text) <= max_chars

    def test_cap_applies_after_merge_not_before(self):
        """A merge is applied after any strategy, so it can defeat token_count too."""
        max_chars = 500
        chunks = chunk_text(
            MANY_SHORT_SENTENCES,
            strategy=ChunkStrategy.token_count,
            max_tokens=50,
            min_chunk_length=400,
            max_chars=max_chars,
        )
        for c in chunks:
            assert len(c.text) <= max_chars

    def test_no_text_is_lost_when_capping(self):
        """Bounding size must not drop content — that would be the same bug again."""
        chunks = chunk_text(
            MANY_SHORT_SENTENCES,
            strategy=ChunkStrategy.sentence,
            min_chunk_length=100,
            max_chars=1800,
        )
        reconstructed = " ".join(c.text for c in chunks)
        assert set(MANY_SHORT_SENTENCES.split()) == set(reconstructed.split())


# --------------------------------------------------------------------------- #
# D4 — a word-based chunker cannot split text with no word boundaries
# --------------------------------------------------------------------------- #


class TestD4NoWordBoundaries:
    def test_unbroken_token_is_hard_split(self):
        """A 15,000-char base64 blob is one 'word'. Whitespace splitting cannot touch it.

        Pre-fix it passed through whole and was then embedded as an 8% prefix.
        Splitting mid-word is ugly; embedding 8% of the document and calling it
        done is worse.
        """
        max_chars = 1800
        chunks = chunk_text(
            UNBROKEN_TOKEN,
            strategy=ChunkStrategy.token_count,
            max_chars=max_chars,
        )

        assert len(chunks) > 1, "a single unbroken token chunked to one chunk"
        for c in chunks:
            assert len(c.text) <= max_chars

    def test_unbroken_token_loses_no_characters(self):
        chunks = chunk_text(UNBROKEN_TOKEN, strategy=ChunkStrategy.token_count, max_chars=1800)
        assert "".join(c.text for c in chunks) == UNBROKEN_TOKEN

    def test_mixed_prose_and_blob(self):
        """The realistic case: a transcript with a blob embedded in normal text."""
        max_chars = 1800
        text = "Here is the payload you asked for:\n\n" + "B" * 9000 + "\n\nThat is all."
        chunks = chunk_text(text, strategy=ChunkStrategy.sentence, max_chars=max_chars)

        for c in chunks:
            assert len(c.text) <= max_chars
        assert "B" * 9000 in "".join(c.text for c in chunks)


# --------------------------------------------------------------------------- #
# D3 — index-document double-counted, corrupting BM25 permanently
# --------------------------------------------------------------------------- #


@pytest.fixture
def db_session():
    """Transactional session that rolls back after each test (savepoint pattern)."""
    from jmfts_core.database import get_engine, get_session_factory
    from sqlalchemy.exc import OperationalError

    engine = get_engine()
    try:
        conn = engine.connect()
    except OperationalError:
        pytest.skip("Postgres not reachable; D3 tests require a live database")
    trans = conn.begin()
    SessionLocal = get_session_factory()
    session = SessionLocal(bind=conn)
    conn.begin_nested()

    yield session

    session.close()
    trans.rollback()
    conn.close()


def _index_stats(session, index_name: str) -> dict:
    """Read the collection statistics BM25's IDF and length norm are derived from."""
    from sqlalchemy import text as sql

    row = session.execute(
        sql("SELECT id, total_docs, avg_doc_length FROM search_indexes WHERE name = :n"),
        {"n": index_name},
    ).one()
    doc_freqs = dict(
        session.execute(
            sql("SELECT term, doc_freq FROM search_term_stats WHERE index_id = :i"),
            {"i": row.id},
        ).all()
    )
    return {
        "total_docs": row.total_docs,
        "avg_doc_length": row.avg_doc_length,
        "doc_freqs": doc_freqs,
    }


class TestD3IndexDocumentIsIdempotent:
    """Re-indexing is not an error — it is the recovery path.

    An incremental indexing pass must be resumable: a crash has to be fixable by
    re-running it. Pre-fix, re-running is precisely what corrupted the index, which
    put the two requirements in direct conflict.
    """

    def test_reindexing_the_same_document_does_not_change_stats(self, db_session):
        from jmfts_core.repositories.document import DocumentRepository
        from jmfts_core.repositories.search import SearchRepository

        doc_repo = DocumentRepository(db_session)
        search_repo = SearchRepository(db_session)
        idx = "test_d3_idempotent"

        doc = doc_repo.create(
            title="Foxes",
            content="the quick brown fox jumped over the lazy dog",
            auto_embed=False,
        )
        db_session.flush()

        assert search_repo.index_document(doc.id, idx) is True
        db_session.flush()
        after_first = _index_stats(db_session, idx)

        # The resumable-pass case: index the very same document a second time.
        assert search_repo.index_document(doc.id, idx) is True
        db_session.flush()
        after_second = _index_stats(db_session, idx)

        assert after_second["total_docs"] == after_first["total_docs"], (
            "total_docs was incremented unconditionally — BM25 length normalisation "
            "is now derived from a corpus size that does not exist"
        )
        assert after_second["avg_doc_length"] == pytest.approx(after_first["avg_doc_length"])
        assert (
            after_second["doc_freqs"] == after_first["doc_freqs"]
        ), "doc_freq was incremented unconditionally — every term's IDF is now wrong"

    def test_reindexing_updates_stats_when_content_changes(self, db_session):
        """Idempotent must not mean inert: new terms count, dropped terms decount."""
        from jmfts_core.repositories.document import DocumentRepository
        from jmfts_core.repositories.search import SearchRepository

        doc_repo = DocumentRepository(db_session)
        search_repo = SearchRepository(db_session)
        idx = "test_d3_content_change"

        doc = doc_repo.create(title="A", content="alpha beta", auto_embed=False)
        db_session.flush()
        search_repo.index_document(doc.id, idx)
        db_session.flush()

        doc.content = "alpha gamma"
        db_session.flush()
        search_repo.index_document(doc.id, idx)
        db_session.flush()

        stats = _index_stats(db_session, idx)
        assert stats["total_docs"] == 1
        assert stats["doc_freqs"].get("alpha") == 1, "a surviving term must not double-count"
        assert stats["doc_freqs"].get("gamma") == 1, "a new term must be counted"
        assert stats["doc_freqs"].get("beta", 0) == 0, (
            "a term the document no longer contains must be decounted, or its IDF "
            "stays permanently depressed"
        )

    def test_two_distinct_documents_still_accumulate(self, db_session):
        """The fix must not break the thing that worked."""
        from jmfts_core.repositories.document import DocumentRepository
        from jmfts_core.repositories.search import SearchRepository

        doc_repo = DocumentRepository(db_session)
        search_repo = SearchRepository(db_session)
        idx = "test_d3_accumulate"

        a = doc_repo.create(title="A", content="alpha beta", auto_embed=False)
        b = doc_repo.create(title="B", content="alpha delta", auto_embed=False)
        db_session.flush()

        search_repo.index_document(a.id, idx)
        search_repo.index_document(b.id, idx)
        db_session.flush()

        stats = _index_stats(db_session, idx)
        assert stats["total_docs"] == 2
        assert stats["doc_freqs"]["alpha"] == 2  # in both documents
        assert stats["doc_freqs"]["beta"] == 1
        assert stats["doc_freqs"]["delta"] == 1
