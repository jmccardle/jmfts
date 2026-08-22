"""Axis-B: supersede locks the old triple and refuses to fork an already-dead chain.

Two concurrent supersessions of the same triple, under READ COMMITTED, both read it as
live, both mint a successor, and both set ``invalidated_by`` — last writer wins, so the
version chain forks (two live successors, one lost back-pointer). The fix locks the old
row ``FOR UPDATE`` and re-checks ``invalidated_at`` before invalidating.

A real two-writer interleaving needs two live connections the single-connection savepoint
fixture can't stage; the observable contract the lock enforces — "you cannot supersede a
triple that is already superseded" — is deterministic, so that is what we pin here (plus a
check that the lock is actually requested).
"""

import pytest
from sqlalchemy import event

from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.triple import (
    TripleAlreadyInvalidatedError,
    TripleRepository,
)
from jmfts_core.services.triple_service import TripleService
from jmfts_client.contracts.triple import TripleSupersedRequest


def _facts(db_session):
    """Create (subject, object_v1, object_v2, predicate) and return their ids."""
    docs = DocumentRepository(db_session)
    triples = TripleRepository(db_session)
    subj = docs.create(title="John", content="John", usetype="entity", auto_embed=False)
    obj1 = docs.create(title="NYC", content="NYC", usetype="entity", auto_embed=False)
    obj2 = docs.create(title="SF", content="SF", usetype="entity", auto_embed=False)
    pred = triples.create_predicate(name="lives_in")
    db_session.flush()
    return triples, subj.id, obj1.id, obj2.id, pred.id


def test_normal_supersede_invalidates_and_links(db_session):
    repo, subj, obj1, obj2, pred = _facts(db_session)
    original = repo.create_triple(subject_id=subj, predicate_id=pred, object_id=obj1)
    db_session.flush()

    new_triple, old_triple = repo.supersede_triple(
        old_triple_id=original.id, subject_id=subj, predicate_id=pred, object_id=obj2
    )
    assert old_triple.id == original.id
    assert old_triple.invalidated_at is not None
    assert old_triple.invalidated_by == new_triple.id  # the single, correct back-pointer
    assert new_triple.invalidated_at is None  # the new fact is live


def test_superseding_an_already_superseded_triple_raises(db_session):
    """The guard the FOR UPDATE lock enforces: no second successor for a dead triple."""
    repo, subj, obj1, obj2, pred = _facts(db_session)
    original = repo.create_triple(subject_id=subj, predicate_id=pred, object_id=obj1)
    db_session.flush()

    first, _ = repo.supersede_triple(
        old_triple_id=original.id, subject_id=subj, predicate_id=pred, object_id=obj2
    )
    db_session.flush()

    with pytest.raises(TripleAlreadyInvalidatedError) as exc:
        repo.supersede_triple(
            old_triple_id=original.id, subject_id=subj, predicate_id=pred, object_id=obj1
        )
    # The error names the successor so the caller can chase to the live head.
    assert exc.value.invalidated_by == first.id


def test_supersede_locks_the_old_row_for_update(db_session):
    repo, subj, obj1, obj2, pred = _facts(db_session)
    original = repo.create_triple(subject_id=subj, predicate_id=pred, object_id=obj1)
    db_session.flush()

    statements: list[str] = []
    conn = db_session.connection()

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(conn, "before_cursor_execute", _capture)
    try:
        repo.supersede_triple(
            old_triple_id=original.id, subject_id=subj, predicate_id=pred, object_id=obj2
        )
    finally:
        event.remove(conn, "before_cursor_execute", _capture)

    assert any(
        "triples" in s.lower() and "for update" in s.lower() for s in statements
    ), f"supersede must lock the old triple row FOR UPDATE; saw: {statements!r}"


def test_service_supersede_conflict_bubbles_as_the_typed_error(db_session):
    """The service raises TripleAlreadyInvalidatedError, which @expose maps to HTTP 409."""
    repo, subj, obj1, obj2, pred = _facts(db_session)
    original = repo.create_triple(subject_id=subj, predicate_id=pred, object_id=obj1)
    db_session.flush()
    repo.supersede_triple(
        old_triple_id=original.id, subject_id=subj, predicate_id=pred, object_id=obj2
    )
    db_session.flush()

    service = TripleService(db_session)
    req = TripleSupersedRequest(
        subject_id=subj, predicate_id=pred, object_id=obj1, fact_type="atemporal"
    )
    with pytest.raises(TripleAlreadyInvalidatedError):
        service.supersede_triple(original.id, req)
