"""Create the JMFTS database and load the shipped schema.

Exposed as the ``jmfts-init-db`` console script, so an installed wheel can bootstrap its
own storage. This used to be ``scripts/setup_db.py``, which read ``schema.sql`` by walking
up from ``__file__`` and therefore only worked inside a git checkout; the DDL is package
data now (:mod:`jmfts_core.sql`) and resolves the same way from a wheel.

Loads ``schema.sql``, never the numbered migrations. ``schema.sql`` is the complete current
DDL, so a database built from it is already at the newest schema; the deltas in
``jmfts_core/sql/migrations/`` exist for databases that predate a change and are applied by
an operator who knows which ones the target has already seen.
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jmfts-init-db",
        description=(
            "Create the JMFTS database and load the shipped schema. "
            "Connection settings come from the JMFTS_DB_* environment variables."
        ),
    )
    parser.add_argument(
        "--list-migrations",
        action="store_true",
        help="print the shipped migration file names and exit, applying nothing",
    )
    args = parser.parse_args(argv)

    if args.list_migrations:
        for name in jmfts_sql.migration_names():
            print(name)
        return 0

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
