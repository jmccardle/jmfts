"""Axis-B: index_document locks the SearchIndex row before the corpus-stat RMW.

The BM25 corpus stats (``total_docs`` / ``avg_doc_length``) are a read-modify-write in
Python. Under READ COMMITTED, two indexers of the same index both read ``total_docs=N``
and both write ``N+1`` — a lost update that drifts the collection size BM25's length
normalisation depends on. The fix serialises indexers of one index by selecting its row
``FOR UPDATE``.

A true lost-update needs two live connections, which the single-connection savepoint
fixture can't stage; instead we (1) assert the lock is actually requested — a ``SELECT
... FOR UPDATE`` on ``search_indexes`` is emitted — and (2) lean on the D3 stat-integrity
tests (``test_known_defects.py``) as the regression guard that the lock didn't break the
single-writer RMW.
"""

from sqlalchemy import event

from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.search import SearchRepository


def test_index_document_locks_the_index_row_for_update(db_session):
    doc_repo = DocumentRepository(db_session)
    search_repo = SearchRepository(db_session)

    doc = doc_repo.create(
        title="Foxes",
        content="the quick brown fox jumped over the lazy dog",
        auto_embed=False,
    )
    db_session.flush()

    statements: list[str] = []
    conn = db_session.connection()

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(conn, "before_cursor_execute", _capture)
    try:
        assert search_repo.index_document(doc.id, "test_index_lock") is True
    finally:
        event.remove(conn, "before_cursor_execute", _capture)

    locked = [s for s in statements if "search_indexes" in s.lower() and "for update" in s.lower()]
    assert locked, (
        "index_document must SELECT the SearchIndex row FOR UPDATE so concurrent "
        "indexers of one index serialise their total_docs/avg_doc_length RMW; "
        f"no such statement was emitted. Saw: {statements!r}"
    )
