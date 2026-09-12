"""`jmfts-init-db --pending` — what one database has not applied, asked rather than recalled.

`tests/test_migration_ledger.py` seals `migrations/` against `schema.sql`'s fenced seed
block: a question about the PACKAGE, answered by pure functions over package data. This
file is the other half and it is a question about a DATABASE — the operator's half. Before
`--pending` the only reading an installed wheel offered was `--list-migrations`, which
prints every shipped name and compares it against nothing, so an operator upgrading an
appliance had to know the target's state from memory.

WHY THAT STOPPED BEING ACCEPTABLE AT MIGRATION 022. Every delta before it is a schema
change, and a database missing one says so — a query against a column that is not there
raises. `022_token_embed_256_hnsw.sql` changes an INDEX, and a query against a badly fitted
index returns rows. An appliance that upgrades its wheel and skips that delta keeps serving
MaxSim results from an index whose centroids were fitted to an empty table, returning 0.4542
of the documents it ranked, with no error anywhere (`docs/ANN_INDEX_HEALTH.md` 5.8). The
class of failure moved from loud to silent, and a silent one needs something to ask.

THE EXIT CODES ARE THE CONTRACT, and they follow `diff`: 0 nothing outstanding, 1 something
outstanding, 2 the question could not be answered. Two values would let a deploy step read
"I could not reach the ledger" as "you are current", which is the failure this whole surface
is against. The tests below drive `report_pending()` rather than a string, so a change to
the wording cannot break them and a change to the CODE cannot pass them.

NOTHING HERE APPLIES A MIGRATION. `--pending` does not either; choosing to apply a delta
stays a deliberate operator act (`jmfts_core/sql/__init__.py`).
"""

from __future__ import annotations

import os
from contextlib import contextmanager

import psycopg2
import pytest

from jmfts_core.config import get_settings
from jmfts_core.db_setup import (
    PENDING_NONE,
    PENDING_SOME,
    PENDING_UNANSWERABLE,
    LedgerTableAbsent,
    applied_migrations,
    main,
    report_pending,
)
from jmfts_core.sql import LEDGER_TABLE, migration_names

#: A database that exists on any Postgres server and holds no JMFTS table. Pointing the
#: settings at it is how the "no ledger table" branch is reached without dropping anything:
#: the tests here are read-only against it.
_DATABASE_WITHOUT_A_LEDGER = "postgres"

#: A name nothing creates. The `3D000` branch needs a database that is absent rather than
#: unreachable, and those two arrive as the same exception class.
_DATABASE_THAT_DOES_NOT_EXIST = "jmfts_no_such_database_ffdb1a"


@contextmanager
def _settings_pointed_at(db_name: str):
    """Run the body with `Settings.db_name` reading `db_name`.

    `applied_migrations` resolves its connection through `get_settings()` rather than taking
    a session, because it is called by a console script before the ORM is wired. So the way
    to test its branches is the way the operator reaches them: a different `JMFTS_DB_NAME`.
    The cache is cleared on both edges — leaving a settings object built against the wrong
    name would point every later test in the run at it.
    """
    previous = os.environ.get("JMFTS_DB_NAME")
    os.environ["JMFTS_DB_NAME"] = db_name
    get_settings.cache_clear()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("JMFTS_DB_NAME", None)
        else:
            os.environ["JMFTS_DB_NAME"] = previous
        get_settings.cache_clear()


@contextmanager
def _ledger_row_withheld(name: str):
    """Delete one ledger row, COMMITTED, and put it back afterwards.

    `db_session` rolls its transaction back and `report_pending` opens its own connection,
    so a withheld row has to be committed on a third connection or the function under test
    cannot see it. The restore is in a `finally` and is asserted by the caller: the test
    database is thrown away at the end of the run, but a row missing for the rest of THIS
    run would fail `test_a_freshly_built_database_has_nothing_pending` somewhere else and
    send the reader to the wrong file.
    """
    settings = get_settings()
    conn = psycopg2.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        database=settings.db_name,
    )
    conn.autocommit = True
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT name, applied_at, source FROM {LEDGER_TABLE} WHERE name = %s", (name,)
        )
        row = cursor.fetchone()
        assert row is not None, (
            f"{LEDGER_TABLE} has no row for {name} before this test removed one. "
            f"See test_migration_ledger.py::test_schema_fence_names_exactly_the_shipped_migrations."
        )
        cursor.execute(f"DELETE FROM {LEDGER_TABLE} WHERE name = %s", (name,))
        try:
            yield
        finally:
            cursor.execute(
                f"INSERT INTO {LEDGER_TABLE} (name, applied_at, source) VALUES (%s, %s, %s) "
                f"ON CONFLICT (name) DO NOTHING",
                row,
            )
    finally:
        conn.close()


def test_a_freshly_built_database_has_nothing_outstanding(db_session):
    """schema.sql built this database, so the operator's reading must say so.

    `test_migration_ledger.py::test_a_freshly_built_database_has_nothing_pending` makes the
    same claim through the ORM session and `pending_migrations()`. This one makes it through
    the path the console script actually takes — `get_settings()`, a psycopg2 connection of
    its own, `to_regclass`, the `name` column — because that path is the one an operator
    runs and none of it is exercised by the other test.

    `db_session` is requested for its skip, not its session: it is what says whether a test
    database was provisioned at all.
    """
    assert report_pending() == PENDING_NONE


def test_a_withheld_delta_is_named_on_stdout_and_exits_one(db_session, capsys):
    """One row removed from the ledger, and the reading finds exactly that delta.

    The newest shipped migration rather than a name written here, so this test does not go
    stale the day 023 lands.

    The stdout/stderr split is a contract and not a formatting choice: `--pending
    2>/dev/null` is a bare list of file names, the way `--list-migrations` is, so an
    operator can pipe it. Everything a human reads goes to stderr.
    """
    newest = migration_names()[-1]

    with _ledger_row_withheld(newest):
        code = report_pending()
        out, err = capsys.readouterr()

    assert code == PENDING_SOME
    assert out.split() == [newest], (
        f"stdout must carry the pending file names and nothing else, so that "
        f"`jmfts-init-db --pending 2>/dev/null` pipes. Got: {out!r}"
    )
    assert newest in err or "pending" in err

    assert report_pending() == PENDING_NONE, (
        f"the withheld {LEDGER_TABLE} row was not restored, and every later test in this "
        f"run that reads the ledger will now fail for the wrong reason"
    )


def test_a_database_with_no_ledger_table_refuses_to_guess(db_session):
    """A target that predates 017 cannot say what it holds, and must not pretend otherwise.

    The tempting answer is "all of them": the ledger names nothing, so nothing is applied.
    It is wrong, and wrong in the expensive direction — a database at 016 with no ledger has
    fifteen deltas already in it, and applying them again is what
    `jmfts_core/sql/__init__.py` calls "neither necessary nor safe". `LedgerTableAbsent` is
    a separate signal from an empty applied set for exactly that reason, and it carries the
    exit code that means "could not answer" rather than the one that means "nothing to do".

    Read-only against the server's own `postgres` database, which exists everywhere and
    holds no JMFTS table.
    """
    with _settings_pointed_at(_DATABASE_WITHOUT_A_LEDGER):
        with pytest.raises(LedgerTableAbsent):
            applied_migrations()

        assert report_pending() == PENDING_UNANSWERABLE


def test_an_absent_database_is_not_a_clean_bill(db_session, capsys):
    """No database is `PENDING_UNANSWERABLE`, never `PENDING_NONE`.

    Both "the database is not there" and "the server is not there" reach `report_pending` as
    `psycopg2.OperationalError`, and they take different actions — one runs `jmfts-init-db`,
    the other fixes the connection. The one thing neither may do is return 0, which a deploy
    step reads as "this database is current".

    The message is asserted, not just the code, because the first version of that branch
    keyed on `exc.pgcode == "3D000"` and was unreachable: psycopg2 leaves `pgcode` None for a
    failure raised while connecting. Exit 2 was right for the wrong reason, and a test that
    read only the code passed anyway.
    """
    with _settings_pointed_at(_DATABASE_THAT_DOES_NOT_EXIST):
        code = report_pending()

    out, err = capsys.readouterr()
    assert code == PENDING_UNANSWERABLE
    assert _DATABASE_THAT_DOES_NOT_EXIST in err
    assert "does not exist" in err and "jmfts-init-db" in err, (
        f"an absent database must be told apart from an unreachable server, and named as "
        f"the one that `jmfts-init-db` fixes. stderr said: {err!r}"
    )
    assert out == "", "no database means no pending file names to pipe"


def test_the_two_readings_are_mutually_exclusive():
    """`--list-migrations` and `--pending` answer different questions, so asking both is a bug.

    One is about the package and touches no database; the other is about one database.
    Neither is a refinement of the other, so there is no sensible merge of the two outputs —
    argparse exits 2 rather than silently honouring whichever branch is written first.
    Touches no database.
    """
    with pytest.raises(SystemExit) as excinfo:
        main(["--list-migrations", "--pending"])

    assert excinfo.value.code == 2
