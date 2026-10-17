"""Every shipped migration, applied in order to a database built from ``schema.sql``.

Each delta from 017 on ends with ``INSERT ... ON CONFLICT DO NOTHING`` so that a re-run
against a ``schema.sql``-built database is safe. That claim was never executed by CI:
migration 025's SQL first ran in a maintainer's hand-made scratch database. This applies
each such file to an empty-table schema and requires that it commits.

SCOPE IS 017 ONWARD, because that is where the claim starts. Measured when this was written:
002 through 016 also apply cleanly EXCEPT ``013_rdf_layer.sql``, whose
``RENAME COLUMN domain TO namespace`` fails on a schema that already says ``namespace``. That
is the pre-ledger design (a delta is for a database that lacks it), not a defect, so it is
left out rather than excused by name.

Empty tables only. This proves each file parses, names objects that exist, and ends in its
own ledger row; the DATA behaviour of a delta belongs in its own test
(``test_migration_024.py``).
"""

from __future__ import annotations

import pytest

from jmfts_core.sql import migration_names, migration_sql
from tests.conftest import requires_db
from tests.scratch_db import run_script, scratch_database

pytestmark = requires_db


@pytest.fixture(scope="module")
def db():
    with scratch_database() as conn:
        yield conn


_LEDGER_ERA = [n for n in migration_names() if int(n.split("_", 1)[0]) >= 17]


@pytest.mark.parametrize("name", _LEDGER_ERA)
def test_migration_applies_to_a_schema_sql_database(db, name):
    run_script(db, migration_sql(name))
    cur = db.cursor()
    cur.execute("SELECT 1 FROM schema_migrations WHERE name = %s", (name,))
    assert cur.fetchone() is not None, f"{name} is not in the ledger after applying it"
