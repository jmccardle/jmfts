"""The DDL that ships inside the package.

``schema.sql`` and ``migrations/`` used to live at the repository root, outside every
``packages.find`` include. An installed wheel therefore carried the SQLAlchemy models but
no way to build the tables they map to: ``scripts/setup_db.py`` resolved the schema as
``Path(__file__).parent.parent / "schema.sql"``, which only exists in a git checkout.
Anyone who installed JMFTS rather than cloning it got an import that worked and a database
that could not be created.

They are package data now, and are read through ``importlib.resources`` rather than
``__file__`` arithmetic, so they resolve the same way from a source tree, an editable
install, and a zip-imported wheel.

Two kinds of file, and they are not interchangeable:

``schema.sql``
    The complete, current DDL — extensions, every table, every index. Run once against an
    empty database. This is what ``docker-entrypoint-initdb.d`` and the test harness use.

``migrations/NNN_*.sql``
    Upgrade deltas for a database that already has an older schema. Numbering starts at
    002; there is no 001, because the original schema was never a delta. Applying these to
    a fresh database is neither necessary nor safe — ``schema.sql`` already includes
    everything they add.

A database says which of these it has seen. ``migrations/017_migration_ledger.sql`` adds
``schema_migrations``, one row per delta, and every migration from 018 onward ends by
recording itself; ``schema.sql`` seeds the same table with a row per shipped migration,
because a database it built already holds everything those deltas add and a ledger that
said otherwise would send an operator to apply them. See ``docs/SPRINT_0_4_0.md`` Block E
for why the ledger arrived in the one release that added no migration of its own — a ledger
written against a database with no deltas pending is a ledger whose first row is honest.

The functions here stay database-free. They read package data and nothing else, so
:func:`pending_migrations` takes the applied set as an argument rather than opening a
connection: this module is imported by ``jmfts-init-db`` before there is a database to ask.
Choosing to apply a delta is still the operator's act; what changed is that the target can
now be asked what it has, instead of the operator having to remember.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib.resources import files
from typing import Iterable

_PACKAGE = __name__

#: The ledger table. One constant because three places have to agree on the name: the
#: ``CREATE TABLE`` in ``schema.sql``, the ``INSERT`` every migration from 018 ends with,
#: and any caller that reads a database's applied set to compare it against
#: :func:`migration_names`.
LEDGER_TABLE = "schema_migrations"

#: The fenced seed block in ``schema.sql``. A marker rather than a line number, following
#: ``015_evidence_rows.sql``'s fenced key list, so reformatting the file cannot silently
#: make the reading point at the wrong statement.
_LEDGER_FENCE = re.compile(
    r"-- BEGIN MIGRATION LEDGER.*?VALUES\n(.*?);\n-- END MIGRATION LEDGER",
    re.DOTALL,
)
_LEDGER_NAME = re.compile(r"\('([^']+)',")


@dataclass(frozen=True)
class Migration:
    """One numbered upgrade delta."""

    name: str  #: file name, e.g. ``011_task_queue_heartbeat.sql``
    sql: str  #: the file's contents

    @property
    def number(self) -> int:
        """The leading sequence number, e.g. 11 for ``011_task_queue_heartbeat.sql``."""
        return int(self.name.split("_", 1)[0])


def schema_sql() -> str:
    """The complete current DDL, for loading into an EMPTY database."""
    return files(_PACKAGE).joinpath("schema.sql").read_text(encoding="utf-8")


def migration_names() -> list[str]:
    """Every shipped migration file name, in application order."""
    root = files(_PACKAGE).joinpath("migrations")
    return sorted(entry.name for entry in root.iterdir() if entry.name.endswith(".sql"))


def migration_sql(name: str) -> str:
    """One migration's contents by file name.

    Raises ``FileNotFoundError`` for a name that does not ship. It is not looked up on the
    filesystem or guessed at — a caller asking for a delta that does not exist has a bug,
    and finding out here is better than finding out from a half-applied database.
    """
    return files(_PACKAGE).joinpath("migrations", name).read_text(encoding="utf-8")


def migrations() -> list[Migration]:
    """Every shipped migration, in application order."""
    return [Migration(name=name, sql=migration_sql(name)) for name in migration_names()]


def schema_ledger_names() -> list[str]:
    """The migration names ``schema.sql`` seeds into the ledger, in the order it lists them.

    The two files are kept in step by hand — ``schema.sql`` is the complete DDL and
    ``migrations/`` are the deltas — and this is the reading that lets the pair be checked
    instead of trusted. A delta shipped without its row in the seed block leaves a
    freshly-built database claiming that delta is outstanding, which is the one failure the
    ledger exists to prevent; comparing this against :func:`migration_names` catches it.

    Raises ``ValueError`` if the fenced block is gone. An empty list would read as "the
    schema seeds nothing", which is a different and much worse claim than "the marker moved
    and this function can no longer answer".
    """
    body = _LEDGER_FENCE.search(schema_sql())
    if body is None:
        raise ValueError(
            f"the fenced {LEDGER_TABLE} seed block is missing from schema.sql; "
            "a database built from it would report every shipped migration as pending"
        )
    return _LEDGER_NAME.findall(body.group(1))


def pending_migrations(applied: Iterable[str]) -> list[Migration]:
    """The shipped deltas a database with this applied set has not seen, in order.

    ``applied`` is the ``name`` column of the target's ``schema_migrations`` table, read by
    the caller. It is passed in rather than fetched because this module is package data with
    no database of its own, and is imported by ``jmfts-init-db`` before the database exists.

    Raises ``ValueError`` if ``applied`` names a migration this package does not ship. That
    is a database ahead of the code — an older wheel pointed at a newer database, or a
    renamed file — and it is exactly the situation where reporting a tidy list of "pending"
    deltas would be a lie by omission. There is no lenient mode: the caller is about to
    write DDL.
    """
    applied_set = set(applied)
    shipped = set(migration_names())
    unknown = sorted(applied_set - shipped)
    if unknown:
        raise ValueError(
            f"{LEDGER_TABLE} names migrations this package does not ship: {unknown}. "
            "The database is ahead of the code, or a migration file was renamed; "
            "either way this install cannot say what is outstanding."
        )
    return [m for m in migrations() if m.name not in applied_set]
