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

Nothing here tracks which migrations a given database has already seen. JMFTS has no
migration ledger, so choosing and applying deltas is still a deliberate act by an operator
who knows the target's current state. These functions make the files reachable; they do
not make the decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files

_PACKAGE = __name__


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
