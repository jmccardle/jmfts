"""The seal on the migration ledger: `migrations/` and `schema.sql` must agree.

`jmfts_core/sql/__init__.py` grew `schema_ledger_names()` and `pending_migrations()` with
migration 017, and the property they exist to protect held from the day they landed —
`schema_ledger_names() == migration_names()`, both exactly 002 through 018 — with nothing
asserting it. The failure that property prevents is quiet by construction: add
`019_whatever.sql` to `jmfts_core/sql/migrations/` and forget the matching row inside the
`-- BEGIN MIGRATION LEDGER` fence in `jmfts_core/sql/schema.sql`, and every existing test
still passes while a freshly built database reports 019 as pending. An operator following
that ledger applies a delta to a database that already has its effect, which
`jmfts_core/sql/__init__.py` calls "neither necessary nor safe".

WHY THERE IS NO 001. Numbering starts at 002 because the original schema was never a
delta: `schema.sql` is the complete current DDL, not the sum of a chain of upgrades, so
there is no first migration for the chain to start from.
`migrations/017_migration_ledger.sql` makes the same point where it backfills — "inventing
a row would claim a file exists". :func:`test_migration_numbers_are_contiguous_from_002`
therefore starts its run at 2 rather than 1, and a `001_*.sql` appearing on disk fails it.

THE FIRST TWO TESTS TOUCH NO DATABASE, deliberately. They are pure functions over package
data, they run in the base install, and a seal that needs docker is a seal people skip.
The third does need one, because "a freshly built database reports nothing pending" is a
claim about a database and string comparison cannot make it.
"""

from __future__ import annotations

import re

from sqlalchemy import text

from jmfts_core.sql import (
    LEDGER_TABLE,
    migration_names,
    pending_migrations,
    schema_ledger_names,
)

#: `NNN_lower_snake_name.sql`. The number is what `Migration.number` parses with
#: `int(name.split("_", 1)[0])`, so a name that does not match this is a name that module
#: either mis-parses or refuses.
_MIGRATION_FILE = re.compile(r"^(?P<number>\d{3})_[a-z0-9]+(?:_[a-z0-9]+)*\.sql$")

#: The first delta. See "WHY THERE IS NO 001" above.
_FIRST_MIGRATION_NUMBER = 2

_FENCE = "the `-- BEGIN MIGRATION LEDGER` fence in jmfts_core/sql/schema.sql"


def test_schema_fence_names_exactly_the_shipped_migrations():
    """Every shipped delta has a seed row, and every seed row names a shipped delta.

    Sets, not sorted lists, because the two failures are different repairs and the person
    who trips this needs to be told which one they have. A delta with no row leaves a
    fresh database calling it pending; a row with no file leaves a fresh database claiming
    a delta this package cannot produce, which is what
    :func:`jmfts_core.sql.pending_migrations` refuses to reason about at all.
    """
    on_disk = set(migration_names())
    in_fence = set(schema_ledger_names())

    unseeded = sorted(on_disk - in_fence)
    unfiled = sorted(in_fence - on_disk)

    assert not unseeded and not unfiled, (
        f"jmfts_core/sql/migrations/ and {_FENCE} have drifted apart.\n"
        f"  in migrations/ but not in the schema.sql fence: {unseeded or 'none'}\n"
        f"    -> a database built from schema.sql reports these as PENDING, and an "
        f"operator applying them re-runs DDL that file already contains. Add a "
        f"('<name>', NOW(), 'schema') row inside the fence.\n"
        f"  in the fence but no such file: {unfiled or 'none'}\n"
        f"    -> a database built from schema.sql records a delta this package does not "
        f"ship, and pending_migrations() raises ValueError against it ('the database is "
        f"ahead of the code'). Restore the file or drop the row."
    )


def test_schema_fence_lists_each_migration_once_in_order():
    """The fence is a list, and set comparison cannot see a repeat or a shuffle.

    `name` is the ledger table's primary key, so a duplicated row makes `schema.sql`
    itself fail to load — but it fails during database provisioning, in psql output that
    `tests/conftest.py` prints and swallows into "DB-backed tests will skip". Catching it
    here names the duplicate instead.
    """
    listed = schema_ledger_names()
    duplicates = sorted({name for name in listed if listed.count(name) > 1})
    assert not duplicates, (
        f"{_FENCE} lists these more than once: {duplicates}. `name` is the "
        f"{LEDGER_TABLE} primary key, so schema.sql will not load."
    )
    assert listed == sorted(listed), (
        f"{_FENCE} is out of order. It mirrors migrations/, which "
        f"jmfts_core.sql.migration_names() returns sorted, and application order is the "
        f"only order a ledger of deltas has."
    )


def test_migration_filenames_are_well_formed():
    """`NNN_lower_snake.sql`, because the number is parsed back out of the name.

    `Migration.number` is `int(name.split("_", 1)[0])`. A name that does not carry a
    three-digit prefix either raises there or — worse, for something like `18_foo.sql` —
    parses to a number that sorts differently from the string, and
    :func:`jmfts_core.sql.migration_names` sorts by string.
    """
    malformed = [name for name in migration_names() if not _MIGRATION_FILE.match(name)]
    assert not malformed, (
        f"jmfts_core/sql/migrations/ holds names that are not NNN_lower_snake_name.sql: "
        f"{malformed}. jmfts_core.sql.Migration.number parses the number back out of the "
        f"file name, and migration_names() orders by the string, so the two only agree "
        f"for zero-padded three-digit prefixes."
    )


def test_migration_numbers_are_contiguous_from_002():
    """002, 003, ... with no gap and no repeat. See "WHY THERE IS NO 001" above.

    A gap is a migration someone deleted or never committed; a repeat is two files racing
    for one slot, which is what a merge between two sprint branches produces and exactly
    what `docs/SPRINT_0_5_0.md` assigns numbers up front to avoid.
    """
    numbers = []
    for name in migration_names():
        match = _MIGRATION_FILE.match(name)
        assert (
            match is not None
        ), f"{name} is malformed; see test_migration_filenames_are_well_formed"
        numbers.append(int(match.group("number")))

    expected = list(range(_FIRST_MIGRATION_NUMBER, _FIRST_MIGRATION_NUMBER + len(numbers)))
    assert numbers == expected, (
        f"jmfts_core/sql/migrations/ is not contiguous from "
        f"{_FIRST_MIGRATION_NUMBER:03d}.\n"
        f"  found:   {numbers}\n"
        f"  expected: {expected}\n"
        f"  missing:   {sorted(set(expected) - set(numbers)) or 'none'}\n"
        f"  duplicated: {sorted({n for n in numbers if numbers.count(n) > 1}) or 'none'}"
    )


def test_a_freshly_built_database_has_nothing_pending(db_session):
    """schema.sql built this database, so `pending_migrations()` must return [].

    `tests/conftest.py` drops, recreates and loads `jmfts_core/sql/schema.sql` into
    `jmfts_test` on every run, so the session handed to this test IS the freshly built
    database the ledger's seed block is a claim about. Driven through the real reading
    path — the `name` column out of the live table, into
    :func:`jmfts_core.sql.pending_migrations` — rather than by comparing two strings,
    because the two-string version is the test above and it cannot catch a fence that
    parses but does not load.

    This is also the one direction that surfaces as an ERROR rather than a failure: a row
    inside the fence with no matching file makes `pending_migrations` raise ValueError
    ("the database is ahead of the code"), which is a bad rebase's signature.
    """
    applied = [row[0] for row in db_session.execute(text(f"SELECT name FROM {LEDGER_TABLE}"))]

    assert applied, (
        f"{LEDGER_TABLE} is empty in a database schema.sql just built. Either the fenced "
        f"seed block did not run or it seeds nothing; a database in this state reports "
        f"every shipped migration as pending."
    )

    pending = pending_migrations(applied)
    assert not pending, (
        f"a database built from jmfts_core/sql/schema.sql reports "
        f"{[m.name for m in pending]} as outstanding. schema.sql is the complete current "
        f"DDL, so those deltas are already in this database and applying them is what "
        f"jmfts_core/sql/__init__.py calls 'neither necessary nor safe'. Add their rows "
        f"to {_FENCE}."
    )
