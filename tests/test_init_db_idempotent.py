"""`jmfts-init-db` run twice: the second run must neither fail nor change anything.

`schema.sql` is bare `CREATE TABLE`, so loading it into a database that already holds the
schema fails (issue #50), and `jmfts-init-db && jmfts-server` did not survive a restart. The
fix is not `IF NOT EXISTS` and not applying deltas: on a database that already has tables,
init-db applies NOTHING and answers with the `--pending` reading (0 when current, 1 naming
what is outstanding), because choosing to apply a delta stays the operator's act. A database
with tables but without the markers of an initialised one is refused, naming what is missing.

Each test builds its own scratch database through `main()`, so the first run is also the
"fresh database behaves as before" case, and nothing touches the suite's test database.
"""

from __future__ import annotations

import os
import uuid

import psycopg2
import pytest

from jmfts_core import db_setup
from jmfts_core.config import get_settings
from jmfts_core.sql import LEDGER_TABLE, migration_names, schema_ledger_names


def _connect(database: str):
    s = get_settings()
    conn = psycopg2.connect(
        host=s.db_host,
        port=s.db_port,
        user=s.db_user,
        password=s.db_password,
        database=database,
    )
    conn.autocommit = True
    return conn


@pytest.fixture
def scratch_db(db_session, monkeypatch):
    """A database name that does not exist yet, pointed at by the settings; dropped after.

    `db_session` is requested for its skip: it says whether a test server was provisioned.
    """
    name = f"jmfts_idem_{uuid.uuid4().hex[:10]}"
    previous = os.environ.get("JMFTS_DB_NAME")
    os.environ["JMFTS_DB_NAME"] = name
    get_settings.cache_clear()
    # check_connection goes through the ORM engine, which is bound to the suite's database.
    monkeypatch.setattr(db_setup, "check_connection", lambda: 0)
    try:
        yield name
    finally:
        if previous is None:
            os.environ.pop("JMFTS_DB_NAME", None)
        else:
            os.environ["JMFTS_DB_NAME"] = previous
        get_settings.cache_clear()
        admin = _connect("postgres")
        try:
            admin.cursor().execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            admin.close()


def _relations(name: str):
    conn = _connect(name)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT relname, relkind FROM pg_class c JOIN pg_namespace n "
            "ON n.oid = c.relnamespace WHERE nspname = 'public' ORDER BY 1"
        )
        return cur.fetchall()
    finally:
        conn.close()


def _ledger(name: str):
    conn = _connect(name)
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT name, applied_at FROM {LEDGER_TABLE} ORDER BY name")
        return cur.fetchall()
    finally:
        conn.close()


def _forbid_schema(monkeypatch):
    def refuse():
        raise AssertionError("apply_schema ran against a database that already has tables")

    monkeypatch.setattr(db_setup, "apply_schema", refuse)


def test_fresh_database_is_created_and_loaded(scratch_db):
    assert db_setup.main([]) == 0
    assert "documents" in {r[0] for r in _relations(scratch_db)}
    assert [n for n, _ in _ledger(scratch_db)] == sorted(schema_ledger_names())


def test_second_run_on_a_current_database_changes_nothing(scratch_db, monkeypatch, capsys):
    assert db_setup.main([]) == 0
    relations, ledger = _relations(scratch_db), _ledger(scratch_db)
    capsys.readouterr()

    _forbid_schema(monkeypatch)
    assert db_setup.main([]) == 0

    out, _ = capsys.readouterr()
    assert "already initialised, 0 pending" in out
    assert (_relations(scratch_db), _ledger(scratch_db)) == (relations, ledger)


def test_second_run_missing_the_newest_migration_exits_one_and_applies_nothing(
    scratch_db, monkeypatch, capsys
):
    newest = migration_names()[-1]
    assert db_setup.main([]) == 0
    conn = _connect(scratch_db)
    conn.cursor().execute(f"DELETE FROM {LEDGER_TABLE} WHERE name = %s", (newest,))
    conn.close()
    relations, ledger = _relations(scratch_db), _ledger(scratch_db)
    capsys.readouterr()

    _forbid_schema(monkeypatch)
    assert db_setup.main([]) == 1

    out, err = capsys.readouterr()
    assert newest in out.split()
    assert "ON_ERROR_STOP=1" in err and "Nothing was applied" in err
    assert (_relations(scratch_db), _ledger(scratch_db)) == (relations, ledger)


def test_partially_initialised_database_fails_naming_what_is_missing(
    scratch_db, monkeypatch, capsys
):
    assert db_setup.main([]) == 0
    conn = _connect(scratch_db)
    conn.cursor().execute(f"DROP TABLE {LEDGER_TABLE}")
    conn.close()
    relations = _relations(scratch_db)
    capsys.readouterr()

    _forbid_schema(monkeypatch)
    assert db_setup.main([]) == 1

    _, err = capsys.readouterr()
    assert "partially initialised" in err
    assert LEDGER_TABLE in err
    assert _relations(scratch_db) == relations
