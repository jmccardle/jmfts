"""dedup=true — idempotent create for documents and the assert-fact leg for triples.

``create_document(dedup=True)`` returns a pre-existing identical-content-under-same-parent
document instead of double-writing on replay; ``create_triple(dedup=True)`` routes through
``upsert_triple`` (INSERT ... ON CONFLICT on the (s,p,o) unique key) so re-asserting a fact
returns the existing row rather than raising. Both are best-effort replay idempotency, not
concurrency guards — see ROADMAP §A/§C. Service-level tests: they exercise the exposed flag.
"""

from jmfts_client.contracts.document import DocumentCreate
from jmfts_client.contracts.triple import TripleCreate
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.triple import TripleRepository
from jmfts_core.services.document_service import DocumentService
from jmfts_core.services.triple_service import TripleService

CONTENT = "the dedupquokka fact appears here"


class TestDocumentDedup:
    def test_dedup_returns_existing_and_does_not_double_write(self, db_session):
        svc = DocumentService(db_session)
        first = svc.create_document(
            DocumentCreate(title="a", content=CONTENT, usetype="raw", auto_embed=False),
            dedup=True,
        )
        second = svc.create_document(
            DocumentCreate(title="b", content=CONTENT, usetype="raw", auto_embed=False),
            dedup=True,
        )

        assert second.id == first.id  # same row returned, no new insert
        repo = DocumentRepository(db_session)
        same_hash = [
            d for d in repo.find(limit=1000) if d.content == CONTENT and d.parent_id is None
        ]
        assert len(same_hash) == 1

    def test_without_dedup_double_writes(self, db_session):
        svc = DocumentService(db_session)
        first = svc.create_document(
            DocumentCreate(title="a", content=CONTENT, usetype="raw", auto_embed=False)
        )
        second = svc.create_document(
            DocumentCreate(title="b", content=CONTENT, usetype="raw", auto_embed=False)
        )
        assert second.id != first.id  # no dedup → two distinct rows

    def test_dedup_is_scoped_by_parent(self, db_session):
        """Identical content under DIFFERENT parents are distinct memories — dedup must
        not collapse them (the reason a blanket content-hash UNIQUE was rejected)."""
        repo = DocumentRepository(db_session)
        p1 = repo.create(title="p1", content="parent one", usetype="raw", auto_embed=False)
        p2 = repo.create(title="p2", content="parent two", usetype="raw", auto_embed=False)
        db_session.flush()

        svc = DocumentService(db_session)
        c1 = svc.create_document(
            DocumentCreate(
                title="c", content=CONTENT, parent_id=p1.id, usetype="raw", auto_embed=False
            ),
            dedup=True,
        )
        c2 = svc.create_document(
            DocumentCreate(
                title="c", content=CONTENT, parent_id=p2.id, usetype="raw", auto_embed=False
            ),
            dedup=True,
        )
        assert c1.id != c2.id


class TestTripleDedup:
    def _spo(self, db_session):
        repo = DocumentRepository(db_session)
        s = repo.create(title="s", content="subject", usetype="raw", auto_embed=False)
        o = repo.create(title="o", content="object", usetype="raw", auto_embed=False)
        pred = TripleRepository(db_session).create_predicate(name="dedup_relates_to")
        db_session.flush()
        return s, pred, o

    def test_dedup_reassert_returns_same_triple(self, db_session):
        s, pred, o = self._spo(db_session)
        svc = TripleService(db_session)
        req = TripleCreate(subject_id=s.id, predicate_id=pred.id, object_id=o.id)

        first = svc.create_triple(req, dedup=True)
        second = svc.create_triple(req, dedup=True)

        assert second.id == first.id  # idempotent assert, no IntegrityError, same row
