"""recall_as_of — point-in-time retrieval via an event_time cutoff.

Integration tests against the real database (savepoint-rolled-back by the shared
db_session fixture). They use fulltext and BM25 — neither needs the embedding model —
to prove the ``as_of`` cutoff filters on the DOMAIN clock COALESCE(event_time, created_at)
across search methods, including the BM25 path where the cutoff forced a new join.
See ROADMAP "recall_as_of".
"""

from datetime import datetime, timedelta, timezone

from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.search import SearchRepository

NOW = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
TERM = "quokkasearchtoken"  # distinctive so it can't collide with any seeded content


def _doc(session, *, event_time=None, created_at=None, parent_id=None):
    """Create a searchable doc, then stamp its clocks explicitly for determinism."""
    repo = DocumentRepository(session)
    doc = repo.create(
        title="clock doc",
        content=f"the {TERM} appears here for retrieval",
        parent_id=parent_id,
        usetype="raw",
        auto_embed=False,
    )
    if created_at is not None:
        doc.created_at = created_at
    if event_time is not None:
        doc.event_time = event_time
    session.flush()
    return doc


class TestAsOfFulltext:
    def test_cutoff_excludes_documents_after_it(self, db_session):
        old = _doc(db_session, event_time=NOW - timedelta(days=30))
        _new = _doc(db_session, event_time=NOW)

        repo = SearchRepository(db_session)
        results = repo.fulltext_search(TERM, limit=10, as_of=NOW - timedelta(days=10))

        ids = {r.document.id for r in results}
        assert old.id in ids
        assert _new.id not in ids

    def test_cutoff_after_all_includes_everything(self, db_session):
        old = _doc(db_session, event_time=NOW - timedelta(days=30))
        new = _doc(db_session, event_time=NOW)

        repo = SearchRepository(db_session)
        results = repo.fulltext_search(TERM, limit=10, as_of=NOW + timedelta(days=1))

        ids = {r.document.id for r in results}
        assert {old.id, new.id} <= ids

    def test_no_cutoff_returns_all(self, db_session):
        old = _doc(db_session, event_time=NOW - timedelta(days=30))
        new = _doc(db_session, event_time=NOW)

        repo = SearchRepository(db_session)
        ids = {r.document.id for r in repo.fulltext_search(TERM, limit=10)}
        assert {old.id, new.id} <= ids

    def test_cutoff_falls_back_to_created_at(self, db_session):
        """A document with no event_time is filtered on created_at — the COALESCE half."""
        no_event = _doc(db_session, event_time=None, created_at=NOW - timedelta(days=30))

        repo = SearchRepository(db_session)
        before = {r.document.id for r in repo.fulltext_search(TERM, as_of=NOW - timedelta(days=40))}
        after = {r.document.id for r in repo.fulltext_search(TERM, as_of=NOW - timedelta(days=10))}

        assert no_event.id not in before  # created 30d ago, cutoff at 40d ago → excluded
        assert no_event.id in after       # cutoff at 10d ago → visible

    def test_naive_cutoff_is_read_as_utc(self, db_session):
        """The ORM writes naive utcnow() into TIMESTAMPTZ, so a naive as_of means UTC —
        a naive and an explicit-UTC cutoff at the same wall time must agree."""
        old = _doc(db_session, event_time=NOW - timedelta(days=30))
        _new = _doc(db_session, event_time=NOW)

        repo = SearchRepository(db_session)
        naive_cut = (NOW - timedelta(days=10)).replace(tzinfo=None)
        ids = {r.document.id for r in repo.fulltext_search(TERM, as_of=naive_cut)}

        assert old.id in ids
        assert _new.id not in ids


class TestAsOfBM25:
    """The BM25 cutoff added a `JOIN documents` that the parent_id path used to own
    alone; exercise it directly so a regression in that SQL surfaces."""

    def _indexed(self, db_session, index_name):
        old = _doc(db_session, event_time=NOW - timedelta(days=30))
        new = _doc(db_session, event_time=NOW)
        repo = SearchRepository(db_session)
        repo.create_index(index_name, description="as_of test")
        repo.index_document(old.id, index_name=index_name)
        repo.index_document(new.id, index_name=index_name)
        db_session.flush()
        return repo, old, new

    def test_cutoff_excludes_future(self, db_session):
        repo, old, new = self._indexed(db_session, "asof_bm25_a")
        results = repo.bm25_search(
            TERM, index_name="asof_bm25_a", limit=10, as_of=NOW - timedelta(days=10)
        )
        ids = {r.document.id for r in results}
        assert old.id in ids
        assert new.id not in ids

    def test_no_cutoff_returns_all(self, db_session):
        repo, old, new = self._indexed(db_session, "asof_bm25_b")
        ids = {r.document.id for r in repo.bm25_search(TERM, index_name="asof_bm25_b", limit=10)}
        assert {old.id, new.id} <= ids
