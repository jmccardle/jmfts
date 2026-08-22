"""LocalJmftsClient — the embedded transport generated from the @expose registry.

Mirror of ``tests/test_api_parity.py`` for the in-process transport: assert the client
covers the registry (no verb is reachable over REST but not in-process), that async ops
stay awaitable, and that dispatch + the atomic ``unit_of_work()`` composition actually work
against the database. See ``jmfts_core/client.py``.
"""

import inspect

import pytest

from jmfts_core.client import LocalJmftsClient
from jmfts_client.contracts.document import DocumentCreate
from jmfts_core.registry import REGISTRY
from jmfts_core.repositories.document import DocumentRepository

# --- generation: client <-> registry coverage (no DB) --------------------------


def test_every_exposed_op_is_a_client_method():
    """Every @expose'd verb is a callable method on the client — same guarantee the
    parity test makes for REST routes, for the in-process transport."""
    missing = [
        s.name for s in REGISTRY if not callable(getattr(LocalJmftsClient, s.func.__name__, None))
    ]
    assert not missing, f"exposed ops with no generated client method: {missing}"


def test_async_service_ops_are_coroutine_methods():
    """A coroutine service op yields an awaitable client method; a sync one does not —
    so callers await ingest/embed and call search synchronously, matching wiring.py."""
    async_specs = [s for s in REGISTRY if inspect.iscoroutinefunction(s.func)]
    sync_specs = [s for s in REGISTRY if not inspect.iscoroutinefunction(s.func)]
    assert async_specs, "expected at least one async exposed op (e.g. ingest)"
    for s in async_specs:
        assert inspect.iscoroutinefunction(getattr(LocalJmftsClient, s.func.__name__))
    for s in sync_specs:
        assert not inspect.iscoroutinefunction(getattr(LocalJmftsClient, s.func.__name__))


def test_generated_verb_preserves_service_signature():
    """help()/IDE hints see the real params (minus self), not (*args, **kwargs)."""
    sig = inspect.signature(LocalJmftsClient.create_document)
    assert "self" not in sig.parameters
    assert "request" in sig.parameters
    assert "dedup" in sig.parameters  # the keyword-only flag added in item 1


# --- bound dispatch against the DB (isolated via the fixture session) -----------


def _bound(db_session):
    """A client whose verbs run on the fixture's rolled-back session (bound mode)."""
    c = LocalJmftsClient()
    c._session = db_session
    return c


class TestBoundDispatch:
    def test_read_verb_dispatches_to_service(self, db_session):
        DocumentRepository(db_session).create(
            title="clientdoc", content="hello", usetype="raw", auto_embed=False
        )
        db_session.flush()
        client = _bound(db_session)
        titles = [d.title for d in client.list_documents(title_prefix="clientdoc")]
        assert "clientdoc" in titles

    def test_write_verb_returns_response_and_persists_in_session(self, db_session):
        client = _bound(db_session)
        resp = client.create_document(
            DocumentCreate(title="viaclient", content="body", usetype="raw", auto_embed=False)
        )
        assert resp.title == "viaclient"
        # visible to a subsequent client read on the same session
        assert resp.id in {d.id for d in client.list_documents(title_prefix="viaclient")}

    def test_dedup_flag_flows_through_the_facade(self, db_session):
        client = _bound(db_session)
        a = client.create_document(
            DocumentCreate(title="d1", content="same body", usetype="raw", auto_embed=False),
            dedup=True,
        )
        b = client.create_document(
            DocumentCreate(title="d2", content="same body", usetype="raw", auto_embed=False),
            dedup=True,
        )
        assert a.id == b.id  # idempotent create reached the service via the client


# --- unit_of_work(): atomic multi-verb composition (real engine) ---------------
#
# These use the process engine directly (a real transaction, not the fixture savepoint).
# The rollback test self-cleans; the commit test deletes what it created in a finally.


class TestUnitOfWorkComposition:
    # Each test takes ``db_session`` purely for its runtime "skip if no DB" guard; the
    # client operations below run on the process engine (a real transaction), not on it.
    def test_shares_one_session_and_rolls_back_atomically(self, db_session):
        client = LocalJmftsClient()
        marker = "uowquokka_rollback"
        with pytest.raises(RuntimeError, match="abort"):
            with client.unit_of_work() as tx:
                created = tx.create_document(
                    DocumentCreate(title=marker, content="x", usetype="raw", auto_embed=False)
                )
                # reads-own-writes: the second verb sees the first's uncommitted write,
                # proving both ran on one shared session/transaction.
                seen = tx.list_documents(title_prefix=marker)
                assert created.id in {d.id for d in seen}
                raise RuntimeError("abort")  # force rollback of the whole unit

        # After rollback nothing persisted — a fresh standalone read sees none.
        assert LocalJmftsClient().list_documents(title_prefix=marker) == []

    def test_commits_all_on_clean_exit(self, db_session):
        client = LocalJmftsClient()
        marker = "uowquokka_commit"
        ids = []
        try:
            with client.unit_of_work() as tx:
                ids.append(
                    tx.create_document(
                        DocumentCreate(title=marker, content="a", usetype="raw", auto_embed=False)
                    ).id
                )
                ids.append(
                    tx.create_document(
                        DocumentCreate(title=marker, content="b", usetype="raw", auto_embed=False)
                    ).id
                )
            persisted = {d.id for d in client.list_documents(title_prefix=marker)}
            assert set(ids) <= persisted
        finally:
            for doc_id in ids:
                try:
                    client.delete_document(doc_id)
                except Exception:
                    pass
