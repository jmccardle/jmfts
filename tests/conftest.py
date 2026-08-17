"""Shared pytest configuration for the JMFTS test suite.

Two responsibilities, both handled at import time (before any test module is
collected, so the lazily-built engine in the test modules points at the right
database from the very first `get_engine()`):

1. **Auth (CR-4).** The API carries an app-level shared-bearer dependency, so
   every TestClient request needs a token. We pin a known ``JMFTS_API_TOKEN``
   (and an explicit ``JMFTS_CORS_ORIGINS``) in the process environment, then
   clear the ``get_settings`` LRU cache so the app is built against them.

2. **An isolated, EMPTY test database.** Historically the suite ran against the
   developer's *production* appliance DB (``jmfts``, hundreds of thousands of
   rows). That caused three whole classes of failure — seed helpers colliding
   with committed rows (``predicates_name_key``), global-count assertions
   drowning in real data, and — worst — write-endpoint commits *leaking test
   rows into production*. We now redirect the whole suite to a dedicated
   ``jmfts_test`` database that this file drops, recreates, and loads from
   ``schema.sql`` on every run, so the schema is always current and the
   baseline is always empty. Combined with the savepoint-rollback ``db_session``
   fixture below (which contains even endpoint ``commit()``s), nothing a test
   writes can ever escape.

Provisioning needs a role that can ``CREATE DATABASE``. The dockerised dev
Postgres (``docker-compose.yml`` / the pgvector container) runs ``jmfts`` as a
superuser, so it works out of the box — point ``JMFTS_DB_*`` at it. A native
host Postgres whose ``jmfts`` role lacks ``CREATEDB`` needs a one-time
``ALTER ROLE jmfts CREATEDB;`` (or pre-create ``jmfts_test`` and grant it). If
provisioning fails we do NOT fall back to production — ``JMFTS_DB_NAME`` is
already pointed at the test DB, so DB-backed tests simply skip.
"""

from __future__ import annotations

import os
import subprocess
from contextlib import contextmanager
from pathlib import Path

# ---------------------------------------------------------------------------
# 1. Auth env — pinned before api.main is imported anywhere.
# ---------------------------------------------------------------------------

TEST_API_TOKEN = "test-shared-bearer-token-cr4"
TEST_CORS_ORIGIN = "https://cors-test.example"

os.environ["JMFTS_API_TOKEN"] = TEST_API_TOKEN
os.environ["JMFTS_CORS_ORIGINS"] = f'["{TEST_CORS_ORIGIN}"]'

# The in-process ingest worker (INGEST_SPEC.md 5.8) is OFF for the whole suite, pinned
# here for the same reason the token is: before `api.main` is imported anywhere. It is on
# by default in the appliance, and the lifespan starts it — but only about half the
# suite's TestClient fixtures use `with TestClient(app)`, so an unconditional worker would
# be alive in some unrelated tests and dead in others, running a real poll loop on its own
# connection while a fixture holds an uncommitted transaction on another. Tests that want
# queued work done call `drain_ingest_queue` below, which runs it synchronously, in-process
# and in the caller's transaction.
os.environ["JMFTS_INGEST_WORKER_ENABLED"] = "0"

AUTH_HEADERS = {"Authorization": f"Bearer {TEST_API_TOKEN}"}


# ---------------------------------------------------------------------------
# 2. Isolated, empty test database.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCHEMA_SQL = _REPO_ROOT / "schema.sql"

# Whether the test database was successfully provisioned and is reachable.
# The `requires_db` marker and the `db_session` fixture consult this.
DB_READY = False


def _psql(
    dbname: str, *args: str, password: str, host: str, port: int, user: str
) -> subprocess.CompletedProcess:
    """Run psql against ``dbname`` with ON_ERROR_STOP, returning the completed process."""
    return subprocess.run(
        [
            "psql",
            "-h",
            host,
            "-p",
            str(port),
            "-U",
            user,
            "-d",
            dbname,
            "-v",
            "ON_ERROR_STOP=1",
            "-Xq",
            *args,
        ],
        env={**os.environ, "PGPASSWORD": password},
        capture_output=True,
        text=True,
    )


def _provision_test_db() -> None:
    """Drop + recreate the test database and load schema.sql into it.

    Reads the base connection from ``Settings`` (env + .env), derives the test
    DB name (``JMFTS_TEST_DB_NAME`` or ``<db_name>_test``), and — crucially —
    refuses to touch anything whose name isn't clearly a ``*_test`` database
    distinct from the configured production name. On success, repoints
    ``JMFTS_DB_NAME`` at the test DB and marks ``DB_READY``.
    """
    global DB_READY

    if os.environ.get("JMFTS_TEST_DB_PROVISION", "1") == "0":
        # Caller asserts the target DB is already an empty, current-schema test
        # DB (e.g. a throwaway container). Trust JMFTS_DB_NAME as-is.
        DB_READY = True
        return

    # Read the base connection BEFORE we override the DB name.
    from jmfts_core.config import Settings

    base = Settings()
    prod_name = base.db_name
    test_name = os.environ.get("JMFTS_TEST_DB_NAME") or f"{prod_name}_test"

    # Hard safety rail: never let the suite point at a non-test database.
    assert test_name.endswith("_test"), f"refusing non-_test test DB: {test_name!r}"
    assert test_name != prod_name, "test DB name must differ from production db_name"

    conn = dict(
        password=base.db_password,
        host=base.db_host,
        port=base.db_port,
        user=base.db_user,
    )

    try:
        # DROP + CREATE from the base (production) DB — a maintenance connection
        # that is never the DB being dropped. WITH (FORCE) terminates any
        # lingering connections (pg13+). Fresh each run ⇒ schema always current.
        drop = _psql(prod_name, "-c", f'DROP DATABASE IF EXISTS "{test_name}" WITH (FORCE)', **conn)
        if drop.returncode != 0:
            raise RuntimeError(f"DROP failed: {drop.stderr.strip()}")
        create = _psql(prod_name, "-c", f'CREATE DATABASE "{test_name}"', **conn)
        if create.returncode != 0:
            raise RuntimeError(f"CREATE failed: {create.stderr.strip()}")
        load = _psql(test_name, "-f", str(_SCHEMA_SQL), **conn)
        if load.returncode != 0:
            raise RuntimeError(f"schema load failed: {load.stderr.strip()}")
    except FileNotFoundError:
        print(
            "\n[conftest] psql not found on PATH — cannot provision jmfts_test; "
            "DB-backed tests will skip."
        )
        _point_at_test_db(test_name)
        return
    except Exception as exc:  # noqa: BLE001 — provisioning must never abort collection
        print(
            f"\n[conftest] could not provision '{test_name}': {exc}\n"
            f"[conftest] Grant CREATEDB (ALTER ROLE {base.db_user} CREATEDB;) or point "
            f"JMFTS_DB_* at the docker stack. DB-backed tests will skip."
        )
        _point_at_test_db(test_name)
        return

    _point_at_test_db(test_name)
    DB_READY = True


def _point_at_test_db(test_name: str) -> None:
    """Redirect the whole app at the test DB and reset the lazy engine/settings."""
    os.environ["JMFTS_DB_NAME"] = test_name

    from jmfts_core.config import get_settings

    get_settings.cache_clear()
    # The engine/session factory are lazy module globals; ensure a fresh one is
    # built against the test DB even if something imported the module already.
    import jmfts_core.database as _db

    _db._engine = None
    _db._SessionLocal = None


_provision_test_db()

# Clear the settings cache once more so the pinned auth token + test DB name are
# both reflected no matter what imported config first.
from jmfts_core.config import get_settings  # noqa: E402

get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

import pytest  # noqa: E402

#: Marker for tests that need a live, provisioned database.
requires_db = pytest.mark.skipif(not DB_READY, reason="test database not provisioned")


@pytest.fixture
def db_session():
    """A transactional session whose writes are always rolled back.

    Uses an outer connection-level transaction plus SQLAlchemy 2.0's
    ``join_transaction_mode="create_savepoint"``: every ``session.commit()`` —
    including the ones the write *endpoints* issue via ``get_db`` — merely
    releases and restarts a SAVEPOINT inside the outer transaction, which we
    roll back at teardown. So endpoint commits can no longer leak, which is the
    exact defect that polluted the production DB before this fixture existed.
    """
    if not DB_READY:
        pytest.skip("test database not provisioned")

    from jmfts_core.database import get_engine, get_session_factory

    engine = get_engine()
    conn = engine.connect()
    trans = conn.begin()
    SessionLocal = get_session_factory()
    session = SessionLocal(bind=conn, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        trans.rollback()
        conn.close()


# ---------------------------------------------------------------------------
# Draining the ingest queue, synchronously, in the caller's transaction
# ---------------------------------------------------------------------------


@contextmanager
def _borrowed_session(session):
    """Hand the worker a session it does not own, with ``get_session``'s semantics.

    ``IngestWorker`` opens one context per phase and relies on the exit to commit. Under
    the savepoint-bound ``db_session`` that commit is a SAVEPOINT release and the rollback
    a rollback *to* that savepoint, so the worker's phases behave like real transactions
    while still being contained by the fixture's outer rollback.
    """
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise


def drain_ingest_queue(session, *, planner=None, worker_id="test-ingest-worker", max_tasks=100):
    """Run every claimable ingest task to completion on ``session``. Returns how many ran.

    This is the whole test-side substitute for the worker THREAD (INGEST_SPEC.md 5.8).
    Polling a background thread with sleeps makes a flaky suite and hides ordering bugs
    behind timing; running the identical ``IngestWorker.run_once`` synchronously on the
    test's own connection makes every assertion after it deterministic, and keeps the
    work inside the fixture's rollback.

    The ``session.commit()`` before the drain is load-bearing. The worker rolls its
    session back when a handler raises, and under the savepoint fixture a rollback
    unwinds to the last savepoint — which, without this, would be the one taken before
    the test created its own fixtures. Committing first releases that savepoint, so a
    failing task discards only its own partial work, exactly as it would in production.
    """
    from jmfts_core.ingest_worker import IngestWorker
    from jmfts_core.settling import NO_ROLLUP

    session.commit()
    worker = IngestWorker(
        worker_id=worker_id,
        session_factory=lambda: _borrowed_session(session),
        planner=planner if planner is not None else NO_ROLLUP,
    )
    return worker.drain(max_tasks=max_tasks)


@pytest.fixture
def drain_queue(db_session):
    """``drain_ingest_queue`` bound to the test's session."""

    def _drain(**kwargs):
        return drain_ingest_queue(db_session, **kwargs)

    return _drain
