"""Subtree RBAC — the WRITE gate on triples (``SPRINT_0_6_0.md`` step 35).

``tests/test_triple_paging_access.py`` and ``tests/test_derived_triple_visibility.py`` cover
the READ half: a triple naming a document the principal cannot read does not surface, and the
filter is a condition on the statement rather than a pass over its page. This file covers the
half that was missing — five ``TripleService`` write verbs took a subject document and an
object document and gated neither, which is ``tests/test_access_edge_writes.py``'s defect on
the other edge table.

**Write on the subject, read on the object** (Part 4 question 4.1, ANSWERED 2026-09-13, and
step 35: *a triple is a directed edge between two documents and the argument does not change
because the table does*). The entry condition in the step table is the first test below: a
principal with read-only access to *B* asserts a triple naming *B*, and before this it
succeeded.

Fixture, identical in shape to ``test_access_edge_writes.py`` so the two tables can be read
side by side:

    OWNED   — `author` has write, `reader` has read
    SHARED  — `author` has read, `reader` has read   (a legitimate object across a boundary)
    SECRET  — only `reader` has read                 (invisible to `author`)
    OPEN    — under no ACR at all                    (ungoverned stays open)
"""

from contextlib import contextmanager

import pytest

from jmfts_client.contracts.triple import (
    TripleCreate,
    TripleInvalidateRequest,
    TripleSupersedRequest,
)
from jmfts_core.access import AccessDeniedError
from jmfts_core.models.principal import AccessGrant, Principal as PrincipalModel
from jmfts_core.principal_context import CurrentPrincipal, reset_principal, set_principal
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.triple import TripleRepository
from jmfts_core.services.triple_service import TripleService


@contextmanager
def _as(principal):
    token = set_principal(principal)
    try:
        yield
    finally:
        reset_principal(token)


def _principal(session, name):
    row = PrincipalModel(name=name)
    session.add(row)
    session.flush()
    return CurrentPrincipal(id=row.id, name=name, is_owner=False)


def _fixture(session):
    """The four documents and two principals of the module docstring, plus a predicate."""
    docs = DocumentRepository(session)
    owned = docs.create(title="OWNED", content="owned " * 3, auto_embed=False)
    shared = docs.create(title="SHARED", content="shared " * 3, auto_embed=False)
    secret = docs.create(title="SECRET", content="secret " * 3, auto_embed=False)
    open_doc = docs.create(title="OPEN", content="open " * 3, auto_embed=False)
    session.flush()

    predicate = TripleRepository(session).create_predicate(name="mentions")
    author = _principal(session, "author")
    reader = _principal(session, "reader")
    session.add_all(
        [
            AccessGrant(document_id=owned.id, principal_id=author.id, level="write"),
            AccessGrant(document_id=owned.id, principal_id=reader.id, level="read"),
            AccessGrant(document_id=shared.id, principal_id=author.id, level="read"),
            AccessGrant(document_id=shared.id, principal_id=reader.id, level="read"),
            AccessGrant(document_id=secret.id, principal_id=reader.id, level="read"),
        ]
    )
    session.flush()
    ids = dict(OWNED=owned.id, SHARED=shared.id, SECRET=secret.id, OPEN=open_doc.id, P=predicate.id)
    return ids, author, reader


def _assert_fact(service, ids, subject_id, object_id):
    return service.create_triple(
        TripleCreate(subject_id=subject_id, predicate_id=ids["P"], object_id=object_id)
    )


# ── assert: write on the subject ────────────────────────────────────────────


def test_write_on_the_subject_is_required(db_session):
    """THE ENTRY CONDITION. `reader` holds read on OWNED and asserts a fact about it.

    Before step 35 this returned 201. Read on the subject is not a licence to record facts
    about it — a fact is a row in somebody's graph, and writing one is a write.
    """
    ids, _author, reader = _fixture(db_session)
    service = TripleService(db_session)
    with _as(reader), pytest.raises(AccessDeniedError):
        _assert_fact(service, ids, ids["OWNED"], ids["SHARED"])


def test_write_on_the_subject_and_read_on_the_object_succeeds(db_session):
    """The case 4.1 chose this rule to permit: a fact of mine about a document of yours."""
    ids, author, _reader = _fixture(db_session)
    service = TripleService(db_session)
    with _as(author):
        triple = _assert_fact(service, ids, ids["OWNED"], ids["SHARED"])
    assert (triple.subject_id, triple.object_id) == (ids["OWNED"], ids["SHARED"])


def test_write_on_the_object_is_not_required(db_session):
    """`author` holds READ on SHARED and never write; the fact above is still legitimate.

    Stated separately from the test before it because this is the half write-on-both-ends
    would have refused, and it is the reason that candidate was rejected.
    """
    ids, author, _reader = _fixture(db_session)
    repo = TripleRepository(db_session)
    with _as(author):
        triple = repo.create_triple(
            subject_id=ids["OWNED"], predicate_id=ids["P"], object_id=ids["SHARED"]
        )
    assert triple.id is not None


def test_read_on_the_subject_is_not_enough_in_either_direction(db_session):
    """SHARED as SUBJECT, where `author` has read and no write."""
    ids, author, _reader = _fixture(db_session)
    service = TripleService(db_session)
    with _as(author), pytest.raises(AccessDeniedError):
        _assert_fact(service, ids, ids["SHARED"], ids["OWNED"])


# ── assert: read on the object ──────────────────────────────────────────────


def test_an_unreadable_object_is_spelled_as_missing(db_session):
    """404, not 403 — ``require_add_child``'s house style, shared with the link gate.

    A 403 here would confirm SECRET exists to a principal for whom it is 404 everywhere
    else, which is the leak that made write-on-subject-only unacceptable: the row would
    otherwise reach ``/triples/query`` and ``/triples/path``.
    """
    ids, author, _reader = _fixture(db_session)
    service = TripleService(db_session)
    with _as(author), pytest.raises(LookupError):
        _assert_fact(service, ids, ids["OWNED"], ids["SECRET"])


def test_a_caller_with_no_write_cannot_probe_object_existence(db_session):
    """Subject first, and completely.

    `reader` may not write OWNED. Whether the object is SECRET (readable to it), OPEN, or
    an id that exists nowhere, the answer is the same 403 — otherwise the verb is an
    existence oracle for a call that was never going to succeed.
    """
    ids, _author, reader = _fixture(db_session)
    service = TripleService(db_session)
    for object_id in (ids["SECRET"], ids["OPEN"], 9_999_999):
        with _as(reader), pytest.raises(AccessDeniedError):
            _assert_fact(service, ids, ids["OWNED"], object_id)


def test_an_unreadable_subject_is_spelled_as_missing(db_session):
    """A subject the caller cannot read at all is 404, before its writability is asked."""
    ids, author, _reader = _fixture(db_session)
    service = TripleService(db_session)
    with _as(author), pytest.raises(LookupError):
        _assert_fact(service, ids, ids["SECRET"], ids["OWNED"])


def test_the_idempotent_leg_is_gated_too(db_session):
    """``PUT /triples``. Re-asserting a fact is asserting it; the flag is not a way round.

    Both legs share ``repo.upsert_triple``, so ``create_triple(dedup=True)`` is covered by
    the same gate — asserted here as well, because the two entry points are what the audit
    listed.
    """
    ids, _author, reader = _fixture(db_session)
    service = TripleService(db_session)
    request = TripleCreate(subject_id=ids["OWNED"], predicate_id=ids["P"], object_id=ids["SHARED"])
    with _as(reader), pytest.raises(AccessDeniedError):
        service.upsert_triple(request)
    with _as(reader), pytest.raises(AccessDeniedError):
        service.create_triple(request, dedup=True)


def test_an_existing_fact_is_refused_on_the_same_terms_as_a_new_one(db_session):
    """The gate runs before the ON CONFLICT, so the refusal does not leak the conflict.

    If the gate ran after the INSERT's ``DO NOTHING``, a refused caller could tell an
    already-asserted fact from a fresh one by which error came back.
    """
    ids, author, reader = _fixture(db_session)
    service = TripleService(db_session)
    with _as(author):
        service.upsert_triple(
            TripleCreate(subject_id=ids["OWNED"], predicate_id=ids["P"], object_id=ids["SHARED"])
        )
    with _as(reader), pytest.raises(AccessDeniedError):
        service.upsert_triple(
            TripleCreate(subject_id=ids["OWNED"], predicate_id=ids["P"], object_id=ids["SHARED"])
        )


# ── a literal object ────────────────────────────────────────────────────────
#
# `ck_triples_object_exactly_one`: a triple's object is a document node OR a literal. A
# literal is a value, not a node, so there is nothing to read-gate and the subject's write
# check is the whole gate. That is the answer the READ side already gives
# (`repositories/triple.py:325`, `TripleService.get_triple`), and a write side that said
# anything else would make a literal fact either assertable-and-invisible or refusable for
# want of a row nobody can point at. No REST verb mints one today — `TripleCreate.object_id`
# is required — so these go through the repository, which is where the gate lives.


def test_a_literal_fact_needs_write_on_its_subject(db_session):
    ids, _author, reader = _fixture(db_session)
    repo = TripleRepository(db_session)
    with _as(reader), pytest.raises(AccessDeniedError):
        repo.create_triple(subject_id=ids["OWNED"], predicate_id=ids["P"], object_literal="Paris")


def test_a_literal_fact_needs_nothing_else(db_session):
    """Write on the subject is sufficient as well as necessary: no object to check."""
    ids, author, _reader = _fixture(db_session)
    repo = TripleRepository(db_session)
    with _as(author):
        triple = repo.create_triple(
            subject_id=ids["OWNED"], predicate_id=ids["P"], object_literal="Paris"
        )
    assert triple.object_id is None and triple.object_literal == "Paris"


def test_a_literal_fact_about_an_unreadable_subject_is_missing(db_session):
    ids, author, _reader = _fixture(db_session)
    repo = TripleRepository(db_session)
    with _as(author), pytest.raises(LookupError):
        repo.create_triple(subject_id=ids["SECRET"], predicate_id=ids["P"], object_literal="Paris")


# ── ungoverned stays open ───────────────────────────────────────────────────


def test_ungoverned_documents_stay_assertable_by_anyone(db_session):
    """``access.py:172`` — ungoverned → open. Access control here is opt-in and stays so."""
    ids, _author, reader = _fixture(db_session)
    repo = TripleRepository(db_session)
    with _as(reader):
        triple = repo.create_triple(
            subject_id=ids["OPEN"], predicate_id=ids["P"], object_id=ids["SHARED"]
        )
    assert triple.id is not None


def test_an_unbound_caller_bypasses_the_gate(db_session):
    """Workers bind no principal: ``fact_extraction`` and ``shacl_rules`` write this way."""
    ids, _author, _reader = _fixture(db_session)
    repo = TripleRepository(db_session)
    triple = repo.create_triple(
        subject_id=ids["SECRET"], predicate_id=ids["P"], object_id=ids["OWNED"]
    )
    assert triple.id is not None


# ── retract: delete, invalidate, supersede ──────────────────────────────────


def _fact(db_session, ids, subject_id, object_id=None, derived_by=None, object_literal=None):
    """A stored fact, written with no principal bound (so the write gate is not the test)."""
    triple = TripleRepository(db_session).create_triple(
        subject_id=subject_id,
        predicate_id=ids["P"],
        object_id=object_id,
        object_literal=object_literal,
    )
    triple.derived_by = derived_by
    db_session.flush()
    return triple


def test_delete_needs_write_on_one_end(db_session):
    ids, author, reader = _fixture(db_session)
    triple = _fact(db_session, ids, ids["OWNED"], ids["SHARED"])
    service = TripleService(db_session)
    with _as(reader), pytest.raises(AccessDeniedError):
        service.delete_triple(triple.id)
    with _as(author):
        assert service.delete_triple(triple.id)["deleted"] == triple.id


def test_delete_of_an_asserted_fact_accepts_write_on_either_end(db_session):
    """``derived_by IS NULL``: nothing recorded who asserted it, so either end will do.

    ``require_edge_delete``'s rule, read off the other table's copy of the same column.
    The fact below runs SHARED → OWNED, so `author` holds read on the subject and write on
    the object.
    """
    ids, author, _reader = _fixture(db_session)
    triple = _fact(db_session, ids, ids["SHARED"], ids["OWNED"])
    repo = TripleRepository(db_session)
    with _as(author):
        assert repo.delete_triple(triple.id) is True


def test_delete_of_a_derived_fact_needs_write_on_the_subject(db_session):
    """``derived_by IS NOT NULL``: the rule derived it FROM the subject, so that is the end.

    Write on the object is refused because ``shacl_rules.apply_derivation`` puts the row
    back on its next run — a verb that reports success and changes nothing is the swallowed
    failure this codebase does not ship.
    """
    ids, author, _reader = _fixture(db_session)
    triple = _fact(db_session, ids, ids["SHARED"], ids["OWNED"], derived_by="a-rule")
    repo = TripleRepository(db_session)
    with _as(author), pytest.raises(AccessDeniedError):
        repo.delete_triple(triple.id)


def test_retracting_a_fact_with_no_readable_end_is_missing(db_session):
    """Existence-hiding, and the 404 body is the service's own string for a real miss.

    A LITERAL fact about SECRET, so there is exactly one endpoint and `author` can read
    none of it. If the gate's 404 read differently from ``TripleService``'s own
    "Triple not found", the body would hand back the existence the gate is hiding.
    """
    ids, author, _reader = _fixture(db_session)
    triple = _fact(db_session, ids, ids["SECRET"], object_literal="Paris")
    repo = TripleRepository(db_session)
    with _as(author), pytest.raises(LookupError) as caught:
        repo.delete_triple(triple.id)
    assert str(caught.value) == "Triple not found"


def test_retracting_a_literal_fact_takes_write_on_its_subject(db_session):
    """One endpoint, so "either end" is the subject — the same rule with nothing to or."""
    ids, author, reader = _fixture(db_session)
    triple = _fact(db_session, ids, ids["OWNED"], object_literal="Paris")
    repo = TripleRepository(db_session)
    with _as(reader), pytest.raises(AccessDeniedError):
        repo.delete_triple(triple.id)
    with _as(author):
        assert repo.delete_triple(triple.id) is True


def test_invalidate_is_gated(db_session):
    """Retracting a fact is writing to the graph it is a fact in; soft is still a write."""
    ids, author, reader = _fixture(db_session)
    triple = _fact(db_session, ids, ids["OWNED"], ids["SHARED"])
    service = TripleService(db_session)
    with _as(reader), pytest.raises(AccessDeniedError):
        service.invalidate_triple(triple.id, TripleInvalidateRequest(reason="wrong"))
    with _as(author):
        assert service.invalidate_triple(triple.id, TripleInvalidateRequest()).invalidated_at


def test_supersede_needs_the_retract_right_on_the_old_fact(db_session):
    ids, _author, reader = _fixture(db_session)
    triple = _fact(db_session, ids, ids["OWNED"], ids["SHARED"])
    service = TripleService(db_session)
    with _as(reader), pytest.raises(AccessDeniedError):
        service.supersede_triple(
            triple.id,
            TripleSupersedRequest(
                subject_id=ids["OWNED"], predicate_id=ids["P"], object_id=ids["SHARED"]
            ),
        )


def test_supersede_gates_the_successor_as_well(db_session):
    """A supersede is a retract AND an assert, and neither half is free.

    `author` may retract this fact, and the successor points at SECRET — which it may not
    read, so the assert half refuses with the same 404 a plain assert gives.
    """
    ids, author, _reader = _fixture(db_session)
    triple = _fact(db_session, ids, ids["OWNED"], ids["SHARED"])
    service = TripleService(db_session)
    with _as(author), pytest.raises(LookupError):
        service.supersede_triple(
            triple.id,
            TripleSupersedRequest(
                subject_id=ids["OWNED"], predicate_id=ids["P"], object_id=ids["SECRET"]
            ),
        )


def test_supersede_is_gated_before_the_already_superseded_check(db_session):
    """403 and not 409: the conflict is not a thing to learn from a call you may not make.

    The old fact is already invalidated, so a permitted caller would get
    ``TripleAlreadyInvalidatedError``. `reader` must not be able to read that off it.
    """
    ids, _author, reader = _fixture(db_session)
    triple = _fact(db_session, ids, ids["OWNED"], ids["SHARED"])
    TripleRepository(db_session).invalidate_triple(triple.id, reason="already gone")
    db_session.flush()
    service = TripleService(db_session)
    with _as(reader), pytest.raises(AccessDeniedError):
        service.supersede_triple(
            triple.id,
            TripleSupersedRequest(
                subject_id=ids["OWNED"], predicate_id=ids["P"], object_id=ids["SHARED"]
            ),
        )


def test_retract_of_an_ungoverned_fact_stays_open(db_session):
    ids, _author, reader = _fixture(db_session)
    triple = _fact(db_session, ids, ids["OPEN"], ids["SHARED"])
    repo = TripleRepository(db_session)
    with _as(reader):
        assert repo.delete_triple(triple.id) is True
