"""upsert_triple as a first-class verb — ``PUT /triples``.

Idempotent fact-assertion was reachable only as the ``create_triple(dedup=true)`` flag; this
promotes it to a named, discoverable, REST-idiomatic verb (``PUT`` = idempotent create-or-
return). Both share ``repo.upsert_triple`` (``INSERT ... ON CONFLICT DO NOTHING``), so a
re-assert returns the existing row (200) instead of the ``IntegrityError`` a plain
``POST /triples`` raises on a duplicate ``(s, p, o)``.

On-conflict is DO NOTHING, not DO UPDATE (deliberate — a re-assert must not silently overwrite
a fact's validity/source; that is ``supersede_triple``). These tests pin the verb wiring:
idempotency, distinct facts stay distinct, no-overwrite on re-assert, and fact_type validation.
See ROADMAP §"Agent verb surface" / §A "Idempotent triple upsert".
"""

import pytest

from jmfts_client.contracts.triple import TripleCreate
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.triple import TripleRepository
from jmfts_core.services.triple_service import InvalidFactTypeError, TripleService


def _spo(session, subj="s", obj="o", pred="upsert_relates_to"):
    repo = DocumentRepository(session)
    s = repo.create(title=subj, content=f"subject {subj}", usetype="raw", auto_embed=False)
    o = repo.create(title=obj, content=f"object {obj}", usetype="raw", auto_embed=False)
    p = TripleRepository(session).create_predicate(name=pred)
    session.flush()
    return s, p, o


class TestUpsertTripleVerb:
    def test_reassert_returns_same_triple(self, db_session):
        s, p, o = _spo(db_session)
        svc = TripleService(db_session)
        req = TripleCreate(subject_id=s.id, predicate_id=p.id, object_id=o.id)

        first = svc.upsert_triple(req)
        second = svc.upsert_triple(req)

        assert second.id == first.id  # idempotent — same row, no IntegrityError

    def test_reassert_does_not_write_a_duplicate_row(self, db_session):
        s, p, o = _spo(db_session)
        svc = TripleService(db_session)
        req = TripleCreate(subject_id=s.id, predicate_id=p.id, object_id=o.id)

        svc.upsert_triple(req)
        svc.upsert_triple(req)

        rows = TripleRepository(db_session).query_triples(
            entity_id=s.id, predicate_id=p.id, limit=100
        )
        assert len(rows) == 1

    def test_distinct_facts_stay_distinct(self, db_session):
        s, p, o = _spo(db_session)
        _, _, o2 = _spo(db_session, obj="o2", pred="upsert_relates_to_2")
        svc = TripleService(db_session)

        a = svc.upsert_triple(TripleCreate(subject_id=s.id, predicate_id=p.id, object_id=o.id))
        b = svc.upsert_triple(TripleCreate(subject_id=s.id, predicate_id=p.id, object_id=o2.id))

        assert a.id != b.id  # different object → different fact

    def test_reassert_does_not_overwrite_metadata(self, db_session):
        """DO NOTHING, not DO UPDATE: the first assertion's source/fact_type is preserved
        even when the re-assert carries different metadata (that is supersede's job)."""
        s, p, o = _spo(db_session)
        src = DocumentRepository(db_session).create(
            title="src", content="provenance", usetype="raw", auto_embed=False
        )
        db_session.flush()
        svc = TripleService(db_session)

        first = svc.upsert_triple(
            TripleCreate(
                subject_id=s.id,
                predicate_id=p.id,
                object_id=o.id,
                source_document_id=src.id,
                fact_type="static",
            )
        )
        # Re-assert the same (s, p, o) with different metadata — must not clobber.
        second = svc.upsert_triple(
            TripleCreate(
                subject_id=s.id,
                predicate_id=p.id,
                object_id=o.id,
                source_document_id=None,
                fact_type="dynamic",
            )
        )

        assert second.id == first.id
        stored = TripleRepository(db_session).get_triple(first.id)
        assert stored.source_document_id == src.id  # original provenance kept
        assert stored.fact_type.value == "static"  # original classification kept

    def test_invalid_fact_type_raises(self, db_session):
        s, p, o = _spo(db_session)
        svc = TripleService(db_session)
        with pytest.raises(InvalidFactTypeError):
            svc.upsert_triple(
                TripleCreate(
                    subject_id=s.id, predicate_id=p.id, object_id=o.id, fact_type="whenever"
                )
            )
