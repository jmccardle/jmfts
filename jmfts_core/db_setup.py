"""Create the JMFTS database and load the shipped schema.

Exposed as the ``jmfts-init-db`` console script, so an installed wheel can bootstrap its
own storage. This used to be ``scripts/setup_db.py``, which read ``schema.sql`` by walking
up from ``__file__`` and therefore only worked inside a git checkout; the DDL is package
data now (:mod:`jmfts_core.sql`) and resolves the same way from a wheel.

Loads ``schema.sql``, never the numbered migrations. ``schema.sql`` is the complete current
DDL, so a database built from it is already at the newest schema; the deltas in
``jmfts_core/sql/migrations/`` exist for databases that predate a change and are applied by
an operator who knows which ones the target has already seen.

``--pending`` is how the operator finds that out without remembering it. It reads the
target's ledger and prints the shipped deltas the ledger does not name; it applies nothing,
because applying a delta stays a deliberate act. Before it existed the only reading
available was ``--list-migrations``, which prints every shipped name and compares it against
nothing — an operator upgrading an appliance had to know the target's state from memory.
Migration ``022`` is why that stopped being acceptable: a database that skips it keeps
answering MaxSim queries from an index fitted to an empty table, with no error anywhere
(``docs/ANN_INDEX_HEALTH.md`` 5.8).
"""

from __future__ import annotations

import argparse
import sys

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

from jmfts_core import sql as jmfts_sql
from jmfts_core.config import get_settings


def create_database() -> bool:
    """Create the configured database if it does not exist.

    Returns True if this call created it, False if it was already there. Connects to the
    server's ``postgres`` database to do so, because you cannot create a database from
    inside itself.
    """
    settings = get_settings()

    conn = psycopg2.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        database="postgres",
    )
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (settings.db_name,))
        if cursor.fetchone():
            print(f"Database '{settings.db_name}' already exists.")
            return False
        print(f"Creating database '{settings.db_name}'...")
        cursor.execute(f'CREATE DATABASE "{settings.db_name}"')
        print("Database created.")
        return True
    finally:
        conn.close()


def apply_schema() -> None:
    """Load the shipped ``schema.sql`` into the configured database.

    Raises ``psycopg2.Error`` on a failed statement, after rolling the whole load back. The
    schema is one transaction on purpose: a database holding half the tables is worse than
    one holding none, because the missing half is only discovered at the first query that
    needs it.
    """
    settings = get_settings()
    schema = jmfts_sql.schema_sql()

    conn = psycopg2.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        database=settings.db_name,
    )
    try:
        cursor = conn.cursor()
        print("Applying schema...")
        try:
            cursor.execute(schema)
            conn.commit()
        except psycopg2.Error:
            conn.rollback()
            raise
        print("Schema applied successfully.")
    finally:
        conn.close()


def check_connection() -> int:
    """Connect through the ORM and count documents. Returns the count."""
    from jmfts_core.database import get_session
    from jmfts_core.models.document import Document

    with get_session() as session:
        return session.query(Document).count()


class LedgerTableAbsent(RuntimeError):
    """The target has no ``schema_migrations`` table, so it predates migration ``017``.

    Not an empty applied set. An empty set would mean "this database has seen no delta",
    which for any database built by ``schema.sql`` or upgraded past 017 is false; the table's
    absence means the question cannot be answered from the database at all, because 002
    through 016 left no trace a reader can find. ``017_migration_ledger.sql`` is what ends
    that, and it carries its own guard — it raises rather than backfilling if
    ``documents.produced_by`` (migration ``016``) is missing, so applying it to a database
    that is NOT at 016 refuses instead of recording fifteen claims it cannot support.
    """


#: Exit codes for ``--pending``, following ``diff``: 0 nothing outstanding, 1 something
#: outstanding, 2 the question could not be answered. A deploy step can gate on 1 without
#: parsing the output, and cannot mistake "I could not read the ledger" for "you are current",
#: which is the mistake a two-valued code would invite.
PENDING_NONE = 0
PENDING_SOME = 1
PENDING_UNANSWERABLE = 2


def applied_migrations() -> list[str]:
    """The ``name`` column of the configured database's ledger, in application order.

    Raises :class:`LedgerTableAbsent` if the table is not there, and ``psycopg2.Error`` if
    the database is not either. Names sort lexically into application order because every
    migration file name is zero-padded to three digits.
    """
    settings = get_settings()

    conn = psycopg2.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        database=settings.db_name,
    )
    try:
        cursor = conn.cursor()
        # `to_regclass` answers with NULL rather than raising, so the absent-table case is a
        # value to branch on instead of an exception to catch — and catching it would mean
        # catching the same class the unreachable-database case raises.
        cursor.execute("SELECT to_regclass(%s)", (jmfts_sql.LEDGER_TABLE,))
        if cursor.fetchone()[0] is None:
            raise LedgerTableAbsent(
                f"{settings.db_name} has no {jmfts_sql.LEDGER_TABLE} table, so it predates "
                "migration 017 and cannot say which of 002 through 016 it holds"
            )
        cursor.execute(f"SELECT name FROM {jmfts_sql.LEDGER_TABLE} ORDER BY name")
        return [row[0] for row in cursor.fetchall()]
    finally:
        conn.close()


def _database_is_absent() -> bool:
    """Is the configured database missing from a server that IS answering?

    Asked only after a connection to it has already failed, to separate the two reasons it
    can fail. Connects to ``postgres`` the way :func:`create_database` does. False when the
    server cannot be reached either — that is a different report and a different fix, and
    this function is not the place to decide which; it answers one question and says no when
    it cannot.
    """
    settings = get_settings()
    try:
        conn = psycopg2.connect(
            host=settings.db_host,
            port=settings.db_port,
            user=settings.db_user,
            password=settings.db_password,
            database="postgres",
        )
    except psycopg2.Error:
        return False
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (settings.db_name,))
        return cursor.fetchone() is None
    finally:
        conn.close()


def report_pending() -> int:
    """Print the shipped deltas the configured database has not applied. Applies nothing.

    Returns one of :data:`PENDING_NONE`, :data:`PENDING_SOME`, :data:`PENDING_UNANSWERABLE`.
    Migration names go to stdout and everything else to stderr, so ``--pending 2>/dev/null``
    is a bare list of file names the way ``--list-migrations`` is.
    """
    settings = get_settings()
    print("JMFTS migration status", file=sys.stderr)
    print(f"  host:     {settings.db_host}:{settings.db_port}", file=sys.stderr)
    print(f"  database: {settings.db_name}", file=sys.stderr)
    print(file=sys.stderr)

    try:
        applied = applied_migrations()
    except LedgerTableAbsent as exc:
        # Deliberately not "everything is pending". This database may hold most of 002
        # through 016 already, and applying a delta a database already has is what
        # `jmfts_core/sql/__init__.py` calls "neither necessary nor safe".
        print(f"error: {exc}", file=sys.stderr)
        print(
            "\nApply 017_migration_ledger.sql first. It creates the table and backfills 002\n"
            "through 016, and it REFUSES rather than backfilling if documents.produced_by is\n"
            "missing — so it will tell you if this database is not at 016. Then re-run this.",
            file=sys.stderr,
        )
        return PENDING_UNANSWERABLE
    except psycopg2.OperationalError as exc:
        # The database not existing and the server not answering both arrive as
        # OperationalError, and they take different actions, so they have to be told apart.
        # NOT by `exc.pgcode`: psycopg2 leaves it None for a failure raised while CONNECTING,
        # which is every failure here. Measured 2026-09-12 against `pgvector/pgvector:pg16` —
        # a missing database gives `pgcode is None` with the catalog name only in the message
        # text, so the first version of this branch was unreachable. Asking the server is
        # deterministic where parsing an error string is not.
        if _database_is_absent():
            print(f"error: database '{settings.db_name}' does not exist", file=sys.stderr)
            print(
                "Nothing is pending, because there is nothing to be pending against. Run\n"
                "jmfts-init-db with no flags: it builds from schema.sql, which is the complete\n"
                "current DDL and seeds the ledger, so a database it creates has no deltas "
                "outstanding.",
                file=sys.stderr,
            )
        else:
            print(f"error: cannot reach the database: {exc}", file=sys.stderr)
        return PENDING_UNANSWERABLE

    try:
        pending = jmfts_sql.pending_migrations(applied)
    except ValueError as exc:
        # The ledger names a file this package does not ship: the database is ahead of the
        # code. `pending_migrations` refuses to answer and so does this.
        print(f"error: {exc}", file=sys.stderr)
        return PENDING_UNANSWERABLE

    shipped = len(jmfts_sql.migration_names())
    print(
        f"  {shipped} shipped, {len(applied)} applied, {len(pending)} pending",
        file=sys.stderr,
    )
    if not pending:
        return PENDING_NONE

    print("\npending, in application order:", file=sys.stderr)
    for migration in pending:
        print(migration.name)
    # Two streams, and Python buffers them differently: stderr is unbuffered, stdout is
    # block-buffered whenever it is not a terminal. Without this, a captured run prints the
    # closing advice BEFORE the names it is advice about. Measured 2026-09-12.
    sys.stdout.flush()
    print(
        "\nThe files are package data under jmfts_core/sql/migrations/. Apply them in the\n"
        "order printed, one at a time; each is its own transaction and each records itself.\n"
        f"  psql -h {settings.db_host} -p {settings.db_port} -U {settings.db_user} "
        f"-d {settings.db_name} -v ON_ERROR_STOP=1 -f <file>",
        file=sys.stderr,
    )
    return PENDING_SOME


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jmfts-init-db",
        description=(
            "Create the JMFTS database and load the shipped schema. "
            "Connection settings come from the JMFTS_DB_* environment variables."
        ),
    )
    # Mutually exclusive because the two answer different questions and neither is a
    # refinement of the other: --list-migrations is about the PACKAGE and touches no
    # database, --pending is about one DATABASE. Asking for both in one invocation is a
    # mistake about which one was wanted, so argparse refuses it.
    reading = parser.add_mutually_exclusive_group()
    reading.add_argument(
        "--list-migrations",
        action="store_true",
        help="print the shipped migration file names and exit, applying nothing",
    )
    reading.add_argument(
        "--pending",
        action="store_true",
        help="ask the configured database which shipped migrations it has NOT applied, and "
        "print those; applies nothing. Exit 0 if none are outstanding, 1 if some are, 2 if "
        "the question could not be answered",
    )
    args = parser.parse_args(argv)

    if args.list_migrations:
        for name in jmfts_sql.migration_names():
            print(name)
        return 0

    if args.pending:
        return report_pending()

    settings = get_settings()
    print("JMFTS database setup")
    print(f"  host:     {settings.db_host}:{settings.db_port}")
    print(f"  database: {settings.db_name}")
    print(f"  user:     {settings.db_user}")
    print()

    try:
        create_database()
        apply_schema()
        count = check_connection()
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"\nConnected. Document count: {count}")
    print("Start the API with: jmfts-server")
    return 0


if __name__ == "__main__":
    sys.exit(main())
