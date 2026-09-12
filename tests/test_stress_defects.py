"""Three defects a real corpus found that no fixture had. ``docs/STRESS_CORPUS.md``.

Each class is the entry condition for one fix, in the sense ``SPRINT_0_4_0.md`` Part 0
requires: a test that failed before the change and passes after it. They are together in
one file because they share a provenance — 877 of the author's own documents through the
queued pipeline on 2026-09-07 — and nothing else.

None of them needs a model, and only the last needs a database.
"""

from __future__ import annotations

import psycopg2.errors
import pytest
import sqlalchemy.exc

from jmfts_core.structure_tasks import Extracted, _without_nul
from jmfts_core.task_errors import ErrorType, classify_exception

# ---------------------------------------------------------------------------
# 4.1 — a NUL in a PDF text layer must not abort the extraction
# ---------------------------------------------------------------------------


class TestNulIsStrippedAndReported:
    """``docs/STRESS_CORPUS.md`` 4.1.

    `Chalmers_The_Conscious_Mind.pdf` — a 900-page book with a NUL in its outline — lost
    its text, its chunks and all of its vectors, because the ``extraction`` evidence row is
    ``jsonb`` and PostgreSQL will not store `` `` in one:

        DataError: (psycopg2.errors.UntranslatableCharacter) unsupported Unicode escape
        sequence ... DETAIL:   cannot be converted to text.
    """

    def test_nul_is_removed_from_the_text(self):
        cleaned, removed = _without_nul(Extracted(text="a\x00b", source="pdf_text_layer"))
        assert cleaned.text == "ab"
        assert removed == 1

    def test_nul_is_removed_from_a_nested_outline(self):
        """The character arrives in the OUTLINE at least as often as in the body: a PDF's
        table of contents is a nested list of ``[level, title, page]`` and it is the titles
        that carry the padding."""
        extracted = Extracted(
            text="body",
            source="pdf_text_layer",
            record={"toc": [[1, "Taking Consciousness Seriously\x00", 3], [2, "clean", 9]]},
        )
        cleaned, removed = _without_nul(extracted)
        assert cleaned.record["toc"] == [[1, "Taking Consciousness Seriously", 3], [2, "clean", 9]]
        assert removed == 1

    def test_a_clean_extraction_is_returned_unchanged_and_uncounted(self):
        """No NUL means no copy and no key in the detail — an operator reading
        ``nul_characters_removed`` should only ever see it when something was removed."""
        original = Extracted(text="clean", source="pdf_text_layer", record={"toc": []})
        cleaned, removed = _without_nul(original)
        assert cleaned is original
        assert removed == 0

    def test_every_nul_is_counted_not_just_the_first(self):
        """The count goes into the evidence, so it has to be the real total: it is the only
        record of how much the stored text differs from the bytes on disk."""
        extracted = Extracted(
            text="a\x00b\x00c", source="pdf_text_layer", record={"pages": ["x\x00", "y"]}
        )
        cleaned, removed = _without_nul(extracted)
        assert removed == 3
        assert "\x00" not in cleaned.text
        assert "\x00" not in cleaned.record["pages"][0]


# ---------------------------------------------------------------------------
# 4.2 — a database error about the DATA is permanent, like every other data error
# ---------------------------------------------------------------------------


def _sqlalchemy_error(cls, orig):
    """A SQLAlchemy DBAPI error of `cls`, shaped the way the driver raises it."""
    return cls("INSERT INTO document_evidence ...", {}, orig)


class TestDataErrorsAreNotRetried:
    """``docs/STRESS_CORPUS.md`` 4.2.

    ``task_errors``'s own docstring at the ``ValueError`` arm states the policy — data and
    programming errors are PERMANENT, "because retrying a bug three times only delays
    noticing it" — but the arm listed only Python's exceptions, so the database's way of
    saying the same thing fell through to the RETRYABLE default. 4.1's PDF spent three
    22-second attempts on a write that could not have succeeded.
    """

    def test_a_data_error_is_permanent(self):
        exc = _sqlalchemy_error(
            sqlalchemy.exc.DataError, psycopg2.errors.UntranslatableCharacter("bad \\u0000")
        )
        assert classify_exception(exc) is ErrorType.PERMANENT

    def test_an_integrity_error_is_permanent(self):
        exc = _sqlalchemy_error(
            sqlalchemy.exc.IntegrityError, psycopg2.errors.UniqueViolation("duplicate key")
        )
        assert classify_exception(exc) is ErrorType.PERMANENT

    def test_an_operational_error_is_still_retryable(self):
        """The counterpart, and the reason this arm names two classes rather than
        ``DatabaseError``: a dropped connection or a lock timeout is a fact about the
        moment, not about the row, and it must keep its retries."""
        exc = _sqlalchemy_error(
            sqlalchemy.exc.OperationalError, psycopg2.errors.AdminShutdown("terminating")
        )
        assert classify_exception(exc) is ErrorType.RETRYABLE


# ---------------------------------------------------------------------------
# 4.6 — the per-document term-stats cleanup must not scan when it cannot delete
# ---------------------------------------------------------------------------


class TestTermStatsCleanupIsGuarded:
    """``docs/STRESS_CORPUS.md`` 4.6.

    ``DELETE FROM search_term_stats WHERE index_id = :id AND doc_freq <= 0`` ran once per
    document per covering index, over a table keyed ``(index_id, term)`` with no index on
    ``doc_freq``. On a first ingest no row can have reached zero, so it read every term row
    for the index and deleted nothing: 30,812 executions, 249 s, and ``EXPLAIN`` reporting
    ``Rows Removed by Filter: 16295`` against ``rows=0``.

    What must not regress is the BEHAVIOUR the scan was there for — a term the document no
    longer contains is decounted, and a row that reaches zero goes away — so this checks
    the outcome rather than the statement count.
    """

    def test_a_term_dropped_by_a_reindex_loses_its_row(self, db_session):
        from jmfts_core.models.document import Document
        from jmfts_core.repositories.search import SearchRepository

        repo = SearchRepository(db_session)
        repo.create_index("stats_guard")
        doc = Document(title="t", content="alpha beta", usetype="chunk", settled="settled")
        db_session.add(doc)
        db_session.flush()

        assert repo.index_document(doc.id, "stats_guard")
        db_session.flush()
        terms = _stats(db_session, "stats_guard")
        assert {"alpha", "beta"} <= set(terms)

        # Re-index with `beta` gone. Its doc_freq falls to zero and the row must not be
        # left behind at 0 — a zero-frequency row makes IDF divide by a term nothing has.
        doc.content = "alpha gamma"
        db_session.flush()
        assert repo.index_document(doc.id, "stats_guard")
        db_session.flush()

        terms = _stats(db_session, "stats_guard")
        assert "beta" not in terms, "a term at doc_freq 0 must be deleted, not left at 0"
        assert {"alpha", "gamma"} <= set(terms)

    def test_a_first_time_index_leaves_every_term_it_wrote(self, db_session):
        """The guarded branch does not run here, and nothing it would have removed exists.
        This is the case that used to pay for a full scan of the index's term table."""
        from jmfts_core.models.document import Document
        from jmfts_core.repositories.search import SearchRepository

        repo = SearchRepository(db_session)
        repo.create_index("stats_first")
        doc = Document(title="t", content="delta epsilon zeta", usetype="chunk", settled="settled")
        db_session.add(doc)
        db_session.flush()
        assert repo.index_document(doc.id, "stats_first")
        db_session.flush()

        assert {"delta", "epsilon", "zeta"} <= set(_stats(db_session, "stats_first"))


def _stats(session, index_name: str) -> set[str]:
    from sqlalchemy import text as sql

    rows = session.execute(
        sql(
            "SELECT s.term FROM search_term_stats s JOIN search_indexes i ON i.id = s.index_id "
            "WHERE i.name = :name AND s.doc_freq > 0"
        ),
        {"name": index_name},
    ).scalars()
    return set(rows)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
