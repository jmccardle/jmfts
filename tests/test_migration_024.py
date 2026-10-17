"""Migration 024 (`structure.max_tokens` -> `structure.max_words`) run against real rows.

Until this file the migration's SQL had been executed once, by hand, on a scratch database.
It is the only data-mutating change in its release and the release refuses the old key, so
a regression here is a collection whose every later ingest is a 400.

Each test builds the state BEFORE 024 (rows holding the old key), applies the file as an
operator would, and reads the rows back. The last group covers the pre-flight: a structure
task that could still run and names `max_tokens` stops the migration before it changes
anything, because `structure_chunk_params` would refuse that task anyway.
"""

from __future__ import annotations

import json

import psycopg2
import pytest

from jmfts_core.sql import migration_sql
from tests.conftest import requires_db
from tests.scratch_db import run_script, scratch_database

pytestmark = requires_db

_MIGRATION = "024_max_words.sql"


@pytest.fixture
def db():
    with scratch_database() as conn:
        yield conn


def _node(conn) -> int:
    cur = conn.cursor()
    cur.execute("INSERT INTO documents DEFAULT VALUES RETURNING id")
    return cur.fetchone()[0]


def _evidence(conn, name: str, value) -> int:
    doc = _node(conn)
    conn.cursor().execute(
        "INSERT INTO document_evidence (document_id, name, value) VALUES (%s, %s, %s::jsonb)",
        (doc, name, json.dumps(value)),
    )
    return doc


def _read(conn, doc: int, name: str = "options"):
    cur = conn.cursor()
    cur.execute("SELECT value FROM document_evidence WHERE document_id=%s AND name=%s", (doc, name))
    return cur.fetchone()[0]


def _queue(conn, params: dict, *, status="pending", task_type="structure:declared", **cols):
    doc = _node(conn)
    cols = {"retryable": True, "retry_count": 0, "max_retries": 3, **cols}
    names = ", ".join(cols)
    marks = ", ".join(["%s"] * len(cols))
    conn.cursor().execute(
        "INSERT INTO task_queue (task_type, scope_document_id, write_mode, status, params, "
        f"param_fingerprint, {names}) VALUES (%s, %s, 'self', %s, %s::jsonb, 'fp', {marks})",
        (task_type, doc, status, json.dumps(params), *cols.values()),
    )


def _apply(conn) -> None:
    run_script(conn, migration_sql(_MIGRATION))


def test_single_key_row_is_renamed_and_other_groups_untouched(db):
    doc = _evidence(
        db,
        "options",
        {"structure": {"max_tokens": 150, "min_chunk_length": 20}, "facts": {"enabled": True}},
    )
    _apply(db)
    assert _read(db, doc) == {
        "structure": {"max_words": 150, "min_chunk_length": 20},
        "facts": {"enabled": True},
    }


def test_rows_that_do_not_qualify_are_left_alone(db):
    untouched = {
        "no_structure": {"facts": {"enabled": False}},
        "null_structure": {"structure": None},
        "array_structure": {"structure": ["max_tokens"]},
        "string_structure": {"structure": "max_tokens"},
        "already_renamed": {"structure": {"max_words": 90}},
    }
    docs = {k: _evidence(db, "options", v) for k, v in untouched.items()}
    # Same payload under another evidence name: only the `options` row is rewritten.
    other = _evidence(db, "something_else", {"structure": {"max_tokens": 7}})
    _apply(db)
    for key, value in untouched.items():
        assert _read(db, docs[key]) == value, key
    assert _read(db, other, "something_else") == {"structure": {"max_tokens": 7}}


def test_a_row_naming_both_keys_stops_the_migration_and_changes_nothing(db):
    good = _evidence(db, "options", {"structure": {"max_tokens": 150}})
    _evidence(db, "options", {"structure": {"max_tokens": 1, "max_words": 2}})
    with pytest.raises(psycopg2.Error, match="names both"):
        _apply(db)
    assert _read(db, good) == {"structure": {"max_tokens": 150}}


def test_rerun_changes_nothing(db):
    doc = _evidence(db, "options", {"structure": {"max_tokens": 150}})
    _apply(db)
    first = _read(db, doc)
    _apply(db)
    assert _read(db, doc) == first == {"structure": {"max_words": 150}}


def test_applying_records_itself_in_the_ledger(db):
    _apply(db)
    cur = db.cursor()
    cur.execute("SELECT count(*) FROM schema_migrations WHERE name = %s", (_MIGRATION,))
    assert cur.fetchone()[0] == 1


@pytest.mark.parametrize("status", ["pending", "claimed", "running", "batched"])
def test_a_live_task_with_the_old_key_refuses_the_migration(db, status):
    doc = _evidence(db, "options", {"structure": {"max_tokens": 150}})
    _queue(db, {"max_tokens": 150}, status=status)
    with pytest.raises(psycopg2.Error, match="drain the queue"):
        _apply(db)
    assert _read(db, doc) == {"structure": {"max_tokens": 150}}


def test_a_retryable_failed_task_with_the_old_key_refuses_the_migration(db):
    _queue(db, {"max_tokens": 150}, status="failed", retry_count=1)
    with pytest.raises(psycopg2.Error, match="drain the queue"):
        _apply(db)


@pytest.mark.parametrize(
    "row",
    [
        dict(status="completed"),
        dict(status="failed", retryable=False),
        dict(status="failed", retry_count=3),
    ],
    ids=["completed", "permanent-failure", "retries-exhausted"],
)
def test_finished_tasks_are_history_and_do_not_stop_the_migration(db, row):
    _queue(db, {"max_tokens": 150}, **row)
    _apply(db)


def test_live_tasks_without_the_old_key_do_not_stop_the_migration(db):
    _queue(db, {"max_words": 150})
    _queue(db, {"max_tokens": 150}, task_type="summarize")
    _apply(db)
