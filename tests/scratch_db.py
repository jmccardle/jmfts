"""A throwaway database built from ``schema.sql``, for tests of migrations.

A migration ends in its own ``COMMIT``, so it cannot run inside the ``db_session``
fixture's rolled-back transaction, and running it against ``jmfts_test`` would leave its
rows for the next test. This builds a separate database per use and drops it after.
"""

from __future__ import annotations

from contextlib import contextmanager
from uuid import uuid4

import psycopg2

from jmfts_core.config import get_settings
from jmfts_core.sql import schema_sql


def _connect(dbname: str):
    s = get_settings()
    conn = psycopg2.connect(
        host=s.db_host, port=s.db_port, user=s.db_user, password=s.db_password, dbname=dbname
    )
    conn.autocommit = True
    return conn


def run_script(conn, sql: str) -> None:
    """Run a migration file as ``psql -f`` would; on failure, leave no open transaction.

    The files carry their own ``BEGIN``/``COMMIT``. An error inside leaves the session in an
    aborted transaction, which an autocommit psycopg2 connection will not clear by itself.
    """
    cur = conn.cursor()
    try:
        cur.execute(sql)
    except psycopg2.Error:
        cur.execute("ROLLBACK")
        raise


@contextmanager
def scratch_database():
    """Yield an autocommit connection to a new database holding ``schema.sql``'s DDL."""
    settings = get_settings()
    # Named like the suite's own database so the `*_test` safety convention holds.
    name = f"jmfts_scratch_{uuid4().hex[:10]}_test"
    admin = _connect(settings.db_name)
    try:
        admin.cursor().execute(f'CREATE DATABASE "{name}"')
        try:
            conn = _connect(name)
            try:
                conn.cursor().execute(schema_sql())
                yield conn
            finally:
                conn.close()
        finally:
            admin.cursor().execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        admin.close()
