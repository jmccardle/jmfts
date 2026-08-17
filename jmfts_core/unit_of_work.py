"""``unit_of_work()`` — atomic, caller-owned multi-verb transactions.

Every ``@expose``'d service self-commits (``self.session.commit()`` after a write) so
that the REST adapter's ``get_db`` teardown commit — which runs *after* the response is
sent — is only a backstop. That is correct for one-verb HTTP requests, but it means an
**in-process** caller (Tau) cannot compose several verbs into one atomic write: verb 1
would commit before verb 2 even runs, and a failure in verb 2 leaves verb 1 persisted.

``unit_of_work()`` fixes this without touching a single service. It hands the caller a
``Session`` bound to an outer connection-level transaction with SQLAlchemy 2.0's
``join_transaction_mode="create_savepoint"``. Under that mode every ``session.commit()``
a service issues merely **releases a SAVEPOINT** inside the outer transaction rather than
committing it; the real commit happens once, at the end of the ``with`` block. If the
block raises, the outer transaction is rolled back and *nothing* the services "committed"
persists.

This is the exact recipe ``tests/conftest.py``'s ``db_session`` fixture uses for test
isolation, promoted to a production primitive — proof the mechanism is load-bearing.

It is also the answer to the in-process thread-safety hazard (ROADMAP "Concurrency &
thread safety", Axis A #1): a raw ``Session`` is not thread-safe and nothing stops a
caller sharing one across threads. ``unit_of_work()`` gives each logical unit its own
session and its own pooled connection, so correct usage (one unit per thread/task) is the
path of least resistance.

Usage::

    from jmfts_core.unit_of_work import unit_of_work
    from jmfts_core.services.search_context_service import SearchContextService
    from jmfts_core.repositories.document import DocumentRepository

    with unit_of_work() as session:
        doc = DocumentRepository(session).create(title=..., content=...)
        SearchContextService(session).create_context(...)   # its .commit() is a savepoint
        # both persist together on clean exit; both roll back together on any exception

Caveats:
- Extract what you need (IDs, response DTOs) *inside* the block. On the final commit the
  session expires its objects, and after the block the session is closed — touching an ORM
  attribute afterwards raises ``DetachedInstanceError``. The services already return
  Pydantic responses, so this is a non-issue for the verb surface.
- One unit == one connection from the pool for its whole lifetime. Do not share the
  yielded session across threads; open one ``unit_of_work()`` per thread/task instead.
- This is the Axis-A primitive; it does NOT address Axis-B (two *separate* sessions racing
  on the same rows). That stays MVCC-governed — see ROADMAP section C.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Generator, Optional

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from jmfts_core.database import get_engine, get_session_factory


@contextmanager
def unit_of_work(engine: Optional[Engine] = None) -> Generator[Session, None, None]:
    """Yield a session in which composed writes commit or roll back atomically.

    Args:
        engine: connection source. Defaults to the process engine (``get_engine()``);
            pass an explicit engine to run a unit against a different pool/database
            (the "borrow hatch" for a future ``LocalJmftsClient`` that owns its own pool).

    Yields:
        A ``Session`` whose inner commits are savepoints of one outer transaction. The
        outer transaction commits on clean exit and rolls back on any exception.
    """
    engine = engine or get_engine()
    SessionLocal = get_session_factory()

    conn = engine.connect()
    trans = conn.begin()
    # create_savepoint: the session joins the live connection transaction via a SAVEPOINT,
    # so a service's self-commit releases the savepoint instead of ending the transaction.
    session = SessionLocal(bind=conn, join_transaction_mode="create_savepoint")
    try:
        yield session
        # Flush any repo-only (flush-not-commit) work and release the final savepoint,
        # then durably commit the whole unit as one transaction.
        session.commit()
        session.close()
        trans.commit()
    except BaseException:
        session.close()
        # A service commit may already have released/replaced the savepoint; the outer
        # transaction is the real boundary and is what we roll back.
        if trans.is_active:
            trans.rollback()
        raise
    finally:
        conn.close()
