"""``unit_of_work()`` — atomic, caller-owned multi-verb transactions.

These tests exercise the REAL commit path (not the savepoint-rollback ``db_session``
fixture), because the whole point of ``unit_of_work`` is what happens at a true commit
boundary: several verbs — including services that self-commit — must land together or not
at all. They therefore persist to ``jmfts_test`` and clean up after themselves.

The three properties proved:
1. multiple verbs composed in one unit all persist on clean exit;
2. an exception mid-unit rolls back even the verbs whose services already "committed"
   (their commit was only a savepoint of the outer transaction);
3. a service self-commit inside a unit is NOT visible to a separate connection until the
   unit's outer transaction commits — the proof that inner commits are savepoints.
"""

import pytest

from jmfts_core.contracts.search_context import SearchContextCreate
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.search_context import SearchContextRepository
from jmfts_core.services.search_context_service import SearchContextService
from jmfts_core.unit_of_work import unit_of_work
from tests.conftest import DB_READY

pytestmark = pytest.mark.skipif(not DB_READY, reason="test database not provisioned")


def _fresh_session():
    from jmfts_core.database import get_session_factory

    return get_session_factory()()


def _delete_context(name):
    s = _fresh_session()
    try:
        if SearchContextRepository(s).delete(name):
            s.commit()
    finally:
        s.close()


def _delete_doc(doc_id):
    if doc_id is None:
        return
    s = _fresh_session()
    try:
        doc = DocumentRepository(s).get(doc_id)
        if doc:
            s.delete(doc)
            s.commit()
    finally:
        s.close()


def test_multi_verb_commit_persists():
    """A repo write + a self-committing service call in one unit both persist."""
    ctx_name = "uow_commit_ctx"
    doc_id = None
    _delete_context(ctx_name)  # ensure clean slate
    try:
        with unit_of_work() as session:
            doc = DocumentRepository(session).create(
                title="uow-doc", content="uow doc body", auto_embed=False
            )
            session.flush()
            doc_id = doc.id
            # A real service whose create_context() calls self.session.commit();
            # under the unit that commit is a savepoint, not a real commit.
            SearchContextService(session).create_context(
                SearchContextCreate(name=ctx_name, config={"method": "hybrid"})
            )

        # A fresh, independent session must see BOTH after the block commits.
        check = _fresh_session()
        try:
            assert DocumentRepository(check).get(doc_id) is not None
            assert SearchContextRepository(check).get_by_name(ctx_name) is not None
        finally:
            check.close()
    finally:
        _delete_context(ctx_name)
        _delete_doc(doc_id)


def test_exception_rolls_back_self_committed_verb():
    """An error after a service self-commit must undo it — nothing persists."""
    ctx_name = "uow_rollback_ctx"
    _delete_context(ctx_name)
    captured_id = {}

    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom):
        with unit_of_work() as session:
            doc = DocumentRepository(session).create(
                title="uow-rb-doc", content="rb body", auto_embed=False
            )
            session.flush()
            captured_id["doc"] = doc.id
            SearchContextService(session).create_context(
                SearchContextCreate(name=ctx_name, config={"method": "hybrid"})
            )
            # The service already called session.commit() above. Blow up anyway.
            raise Boom("verb 3 failed")

    # The outer transaction rolled back: neither the context (whose service
    # "committed") nor the document survives.
    check = _fresh_session()
    try:
        assert SearchContextRepository(check).get_by_name(ctx_name) is None
        assert DocumentRepository(check).get(captured_id["doc"]) is None
    finally:
        check.close()
    # Nothing to clean up if the assertions hold; be defensive anyway.
    _delete_context(ctx_name)


def test_inner_commit_is_a_savepoint_not_visible_externally():
    """Mid-unit, a self-committed service write is invisible to another connection.

    This is the direct proof that ``create_savepoint`` turns the service's
    ``session.commit()`` into a SAVEPOINT release inside the still-open outer
    transaction, rather than a durable commit.
    """
    ctx_name = "uow_savepoint_ctx"
    _delete_context(ctx_name)
    try:
        with unit_of_work() as session:
            SearchContextService(session).create_context(
                SearchContextCreate(name=ctx_name, config={"method": "hybrid"})
            )
            # Service.create_context has already run session.commit(). If that were a
            # REAL commit, a separate connection would see it now. It must NOT.
            outsider = _fresh_session()
            try:
                assert SearchContextRepository(outsider).get_by_name(ctx_name) is None
            finally:
                outsider.close()

        # After the unit's outer transaction commits, it becomes visible.
        after = _fresh_session()
        try:
            assert SearchContextRepository(after).get_by_name(ctx_name) is not None
        finally:
            after.close()
    finally:
        _delete_context(ctx_name)


def test_explicit_engine_argument_is_honored():
    """Passing engine= (the borrow hatch) uses that engine's pool and still commits."""
    from jmfts_core.database import get_engine

    ctx_name = "uow_engine_ctx"
    _delete_context(ctx_name)
    try:
        with unit_of_work(engine=get_engine()) as session:
            SearchContextService(session).create_context(
                SearchContextCreate(name=ctx_name, config={"method": "vector"})
            )
        check = _fresh_session()
        try:
            assert SearchContextRepository(check).get_by_name(ctx_name) is not None
        finally:
            check.close()
    finally:
        _delete_context(ctx_name)
