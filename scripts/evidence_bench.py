#!/usr/bin/env python3
"""The one thing ``SPRINT_JOBS.md`` 13.1 says was never measured: the join cost.

13.1 asks where evidence should live — the ``structured_content`` JSONB column it lives in
today, or a table keyed ``(document_id, name)`` — and it says two facts already decide it
and both were measured in the tree:

1. Every evidence write today is a read-modify-write on one JSONB column, so two ``self``
   writers on one node lose a write with nothing raised. 10.1's finer claim overlap
   therefore REQUIRES per-name rows.
2. Bulk staleness over a subtree is one ``UPDATE`` against a table and a rewrite of every
   node's whole column without one.

And then: *"The join cost is the one thing above that was NOT measured."* This script
measures it, against a real PostgreSQL, on a tree the size of a real corpus.

    python -m scripts.evidence_bench                 # 20,000 nodes, the default
    python -m scripts.evidence_bench --nodes 200000  # a big appliance
    python -m scripts.evidence_bench --keep          # leave the schema for EXPLAIN by hand

Against a throwaway server rather than the appliance, which is how the numbers in 13.1 were
taken. ``JMFTS_DB_PASSWORD`` is not optional here: it defaults to blank, the image does not
accept blank, and the failure arrives as ``fe_sendauth: no password supplied``.

    docker run -d --rm --name jmfts-bench-pg -p 127.0.0.1:5434:5432 \\
        -e POSTGRES_USER=jmfts -e POSTGRES_PASSWORD=jmfts -e POSTGRES_DB=jmfts \\
        pgvector/pgvector:pg16
    JMFTS_DB_HOST=127.0.0.1 JMFTS_DB_PORT=5434 JMFTS_DB_PASSWORD=jmfts \\
        python -m scripts.evidence_bench --nodes 100000 --repeats 60
    docker rm -f -v jmfts-bench-pg

IT BUILDS ITS OWN SCHEMA AND DROPS IT. Nothing here touches ``documents``: both shapes are
built from scratch in a ``bench_evidence`` schema, populated from the same generated rows,
indexed the way the real column is indexed (``idx_documents_structured`` is GIN over the
whole JSONB, so the JSONB side gets that), and dropped on the way out. It needs
``JMFTS_DB_*`` to point at a database it may create a schema in.

FOUR MEASUREMENTS, AND THEY ARE NOT ALL THE SAME QUESTION:

``one node``
    Every evidence value on one node. What a handler does at the start of every task, and
    the case the join cost is worst for — one row versus N.
``guard``
    Part 4.4's read: which nodes in this subtree satisfy a predicate over one evidence
    name. What Phase 4 will run for every rule at every vertex.
``stale``
    13.1's second fact, timed rather than argued.
``race``
    Not a timing. Two concurrent writers, each adding one evidence name to one node, in
    both shapes — and a count of how many writes survived.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import time
from typing import Callable, Optional

from sqlalchemy import text

from jmfts_core.database import get_engine

SCHEMA = "bench_evidence"

#: What one node carries, in the proportions a real tree carries it. Taken from the
#: registry's own shape rather than invented: a chunk has the five chunker keys plus a
#: span, a sheet node has an identity block and twenty-eight measurements, a file node has
#: `file`, `options`, `matched`, `extraction`, `structure` and an attempt log.
PROFILES: dict[str, dict] = {
    "file": {
        "file": {"filename": "annual-report.pdf", "size": 4_100_000, "mime": "application/pdf"},
        "options": {"structure": {"min_chunk_length": 200, "max_tokens": 480}},
        "matched": {
            "format": "pdf",
            "probed_at": "2026-08-29T00:00:00Z",
            "patterns": {
                "has_text_layer": True,
                "has_outline": True,
                "has_images": True,
                "has_tables": False,
                "is_scanned": False,
                "is_encrypted": False,
                "is_damaged": False,
                "page_count": 184,
                "outline_depth": 3,
                "image_count": 22,
            },
        },
        "extraction": {"source": "pdf_text_layer", "characters": 412_009, "pages": 184},
        "structure": {
            "primary_rung": "declared",
            "source": "pdf_outline",
            "coverage": 0.9814,
            "node_count": 1_204,
            "max_depth": 3,
            "gap_regions": [[41_002, 41_990], [188_114, 190_006]],
        },
        "attempts": [
            {"task": "probe", "status": "completed", "at": "2026-08-29T00:00:01Z"},
            {"task": "extract:text", "status": "completed", "at": "2026-08-29T00:00:09Z"},
            {"task": "structure:declared", "status": "completed", "at": "2026-08-29T00:01:44Z"},
        ],
    },
    "section": {
        "rung": "declared",
        "section_title": "Results and discussion",
        "section_level": 2,
        "structure": {"primary_rung": "declared", "source": "pdf_outline"},
    },
    "chunk": {
        "rung": "declared",
        "section_title": "Results and discussion",
        "section_level": 2,
        "chunk_index": 3,
        "source_line": 4_182,
        "source_span": [418_224, 419_901],
        "anchor": {"page": 92, "kind": "pdf", "rects": [[72.0, 118.4, 523.2, 302.9]]},
    },
    "sheet": {
        "structure": {"primary_rung": "declared", "source": "workbook_sheets"},
        "sheet": {
            "index": 2,
            "name": "FY26 Bookings",
            "state": "visible",
            "shape": "records",
            "record_count": 1_284,
            "measurements": {
                "rows": 1_284,
                "cols": 17,
                "fill_ratio": 0.914,
                "header_row": True,
                "header_col": False,
                "interior_cardinality": 8_812,
                "merged_cells": 0,
                "rendered_tokens": None,
                "cells": 21_828,
                "non_empty_cells": 19_951,
                "interior_rows": 1_283,
                "interior_cols": 16,
                "interior_cells": 20_528,
                "interior_non_empty": 18_766,
                "interior_fill_ratio": 0.9142,
                "declared_rows": 1_284,
                "declared_cols": 17,
                "columns": [f"col_{i}" for i in range(17)],
            },
        },
    },
    "record": {
        "record": {f"Field {i}": f"value-{i}" for i in range(17)},
        "row_index": 918,
        "sheet_name": "FY26 Bookings",
    },
}

#: How many of each kind, per 100 nodes. A real tree is mostly chunks.
MIX: tuple[tuple[str, int], ...] = (
    ("file", 1),
    ("section", 6),
    ("chunk", 78),
    ("sheet", 1),
    ("record", 14),
)


def _flatten(blocks: dict, prefix: str = "") -> dict[str, object]:
    """One node's blocks as ``{name: value}`` rows, one row per TOP-LEVEL block.

    Per block and NOT per leaf, and that is the shape 13.1 proposes: the key is
    ``(document_id, name)`` where a name is what an atom produces. Splitting to leaves would
    turn a 28-key ``sheet.measurements`` into 28 rows and measure a shape nobody proposed.
    """
    return {f"{prefix}{key}": value for key, value in blocks.items()}


def _rows(nodes: int, seed: int) -> list[tuple[int, Optional[int], list[int], str, dict]]:
    """A tree of ``nodes`` documents: ids, parents, materialised paths, usetypes, blocks."""
    rng = random.Random(seed)
    kinds = [kind for kind, weight in MIX for _ in range(weight)]
    out: list[tuple[int, Optional[int], list[int], str, dict]] = []
    roots: list[int] = []
    by_kind: dict[str, list[int]] = {k: [] for k in PROFILES}
    for node_id in range(1, nodes + 1):
        kind = kinds[node_id % len(kinds)]
        if kind == "file":
            parent, path = None, []
        elif kind in ("section", "sheet"):
            parent = rng.choice(roots) if roots else None
            path = [parent] if parent else []
        else:
            pool = by_kind["section"] if kind == "chunk" else by_kind["sheet"]
            parent = rng.choice(pool) if pool else (rng.choice(roots) if roots else None)
            path = [] if parent is None else _path_of(out, parent) + [parent]
        blocks = dict(PROFILES[kind])
        out.append((node_id, parent, path, kind, blocks))
        by_kind[kind].append(node_id)
        if kind == "file":
            roots.append(node_id)
    return out


def _path_of(rows: list, node_id: int) -> list[int]:
    return rows[node_id - 1][2]


# ---------------------------------------------------------------------------
# The two shapes
# ---------------------------------------------------------------------------

DDL = f"""
DROP SCHEMA IF EXISTS {SCHEMA} CASCADE;
CREATE SCHEMA {SCHEMA};

-- Shape A: what the appliance does today. One JSONB column, GIN-indexed, exactly as
-- `idx_documents_structured` indexes the real one.
CREATE TABLE {SCHEMA}.docs (
    id                 INTEGER PRIMARY KEY,
    parent_id          INTEGER,
    path               JSONB NOT NULL,
    usetype            TEXT  NOT NULL,
    structured_content JSONB NOT NULL
);
CREATE INDEX docs_path ON {SCHEMA}.docs USING GIN (path);
CREATE INDEX docs_structured ON {SCHEMA}.docs USING GIN (structured_content);

-- Shape B: 13.1's proposal. One row per (document, evidence name), plus the two columns
-- 3.2 and 3.3 want and the JSONB column has nowhere to put — the write's fingerprint and
-- the third state.
CREATE TABLE {SCHEMA}.ev (
    document_id  INTEGER NOT NULL,
    name         TEXT    NOT NULL,
    value        JSONB,
    fingerprint  TEXT,
    state        TEXT    NOT NULL DEFAULT 'written',
    PRIMARY KEY (document_id, name)
);
CREATE INDEX ev_name ON {SCHEMA}.ev (name);
-- The GIN half of the JSONB side's `idx_documents_structured`, per name. A composite GIN
-- over (name, value) needs an operator class for `text` that this database does not have
-- without `btree_gin`, and requiring an extension to make the comparison fair would be
-- measuring an installation choice; a partial index per hot name is what an appliance
-- would actually create.
CREATE INDEX ev_value ON {SCHEMA}.ev USING GIN (value);
CREATE INDEX ev_stale ON {SCHEMA}.ev (name, document_id) WHERE state = 'stale';
"""


def build(conn, rows: list) -> None:
    for statement in DDL.split(";\n"):
        if statement.strip():
            conn.execute(text(statement))

    conn.execute(
        text(
            f"INSERT INTO {SCHEMA}.docs (id, parent_id, path, usetype, structured_content) "
            "VALUES (:id, :parent_id, :path, :usetype, :sc)"
        ),
        [
            {
                "id": node_id,
                "parent_id": parent,
                "path": json.dumps(path),
                "usetype": kind,
                "sc": json.dumps(blocks),
            }
            for node_id, parent, path, kind, blocks in rows
        ],
    )
    conn.execute(
        text(
            f"INSERT INTO {SCHEMA}.ev (document_id, name, value, fingerprint) "
            "VALUES (:d, :n, :v, :f)"
        ),
        [
            {"d": node_id, "n": name, "v": json.dumps(value), "f": f"{node_id}:{name}"}
            for node_id, _parent, _path, _kind, blocks in rows
            for name, value in _flatten(blocks).items()
        ],
    )
    conn.execute(text(f"ANALYZE {SCHEMA}.docs"))
    conn.execute(text(f"ANALYZE {SCHEMA}.ev"))


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------


def _time(fn: Callable[[], object], *, repeats: int) -> tuple[float, float]:
    """Median and best milliseconds over ``repeats`` runs. Median, because one cold run
    measures the buffer cache and one best run measures nothing that ever happens twice."""
    samples = []
    for _ in range(repeats):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000)
    return statistics.median(samples), min(samples)


def _report(label: str, jsonb: tuple[float, float], table: tuple[float, float]) -> None:
    ratio = table[0] / jsonb[0] if jsonb[0] else float("inf")
    verdict = "table faster" if ratio < 1 else f"{ratio:.2f}x the JSONB read"
    print(f"  {label:34} jsonb {jsonb[0]:8.3f} ms   table {table[0]:8.3f} ms   {verdict}")


def bench_one_node(conn, rows: list, repeats: int) -> None:
    """Every evidence value on one node — what a handler does at the start of a task."""
    chunk_id = next(r[0] for r in rows if r[3] == "chunk")

    def jsonb():
        conn.execute(
            text(f"SELECT structured_content FROM {SCHEMA}.docs WHERE id = :i"), {"i": chunk_id}
        ).fetchall()

    def table():
        conn.execute(
            text(f"SELECT name, value FROM {SCHEMA}.ev WHERE document_id = :i"), {"i": chunk_id}
        ).fetchall()

    _report("one node, every value", _time(jsonb, repeats=repeats), _time(table, repeats=repeats))


def bench_one_name(conn, rows: list, repeats: int) -> None:
    """One evidence name on one node — a guard's read, at one vertex."""
    sheet_id = next(r[0] for r in rows if r[3] == "sheet")

    def jsonb():
        conn.execute(
            text(
                f"SELECT structured_content #> '{{sheet,measurements,rows}}' "
                f"FROM {SCHEMA}.docs WHERE id = :i"
            ),
            {"i": sheet_id},
        ).fetchall()

    def table():
        conn.execute(
            text(
                f"SELECT value #> '{{measurements,rows}}' FROM {SCHEMA}.ev "
                "WHERE document_id = :i AND name = 'sheet'"
            ),
            {"i": sheet_id},
        ).fetchall()

    _report("one node, one name", _time(jsonb, repeats=repeats), _time(table, repeats=repeats))


def bench_guard(conn, rows: list, repeats: int) -> None:
    """Part 4.4 over a whole subtree: which nodes satisfy a predicate over one name.

    TWO LINES, AND THE SECOND ONE IS THE ANSWER 13.1 ASKED FOR. The first is what a
    caller would naively write, and it is dominated by a difference that is not about
    storage at all: with the ``ev`` join present the planner stops using ``docs_path`` for
    the subtree restriction and sequentially scans ``docs`` instead, which costs more than
    the join does. That is a real operational fact and it is worth having on the record,
    but reporting it as "the join cost" would be reporting a plan choice.

    So the second line resolves the subtree to an id list FIRST — identical work in both
    shapes, and untimed — and then times only the evidence read over that list. What
    remains is the join, and nothing else.
    """
    root = next(r[0] for r in rows if r[3] == "file")
    ids = [
        r[0]
        for r in conn.execute(
            text(
                f"SELECT d.id FROM {SCHEMA}.docs d "
                "WHERE d.path @> jsonb_build_array(CAST(:r AS int)) OR d.id = :r"
            ),
            {"r": root},
        )
    ]

    def jsonb_naive():
        conn.execute(
            text(
                f"SELECT d.id FROM {SCHEMA}.docs d "
                "WHERE (d.path @> jsonb_build_array(CAST(:r AS int)) OR d.id = :r) "
                "AND (d.structured_content #>> '{structure,coverage}')::float < 0.99"
            ),
            {"r": root},
        ).fetchall()

    def table_naive():
        conn.execute(
            text(
                f"SELECT d.id FROM {SCHEMA}.docs d JOIN {SCHEMA}.ev e ON e.document_id = d.id "
                "WHERE (d.path @> jsonb_build_array(CAST(:r AS int)) OR d.id = :r) "
                "AND e.name = 'structure' AND (e.value #>> '{coverage}')::float < 0.99"
            ),
            {"r": root},
        ).fetchall()

    def jsonb_ids():
        conn.execute(
            text(
                f"SELECT d.id FROM {SCHEMA}.docs d WHERE d.id = ANY(:ids) "
                "AND (d.structured_content #>> '{structure,coverage}')::float < 0.99"
            ),
            {"ids": ids},
        ).fetchall()

    def table_ids():
        conn.execute(
            text(
                f"SELECT e.document_id FROM {SCHEMA}.ev e WHERE e.document_id = ANY(:ids) "
                "AND e.name = 'structure' AND (e.value #>> '{coverage}')::float < 0.99"
            ),
            {"ids": ids},
        ).fetchall()

    print(f"    (the subtree is {len(ids):,} nodes)")
    _report(
        "guard, subtree written as a join",
        _time(jsonb_naive, repeats=repeats),
        _time(table_naive, repeats=repeats),
    )
    _report(
        "guard, subtree resolved first",
        _time(jsonb_ids, repeats=repeats),
        _time(table_ids, repeats=repeats),
    )


def bench_guard_corpus(conn, rows: list, repeats: int) -> None:
    """The same predicate over the WHOLE corpus — Part 12.1's cross-tree query."""

    def jsonb():
        conn.execute(
            text(
                f"SELECT count(*) FROM {SCHEMA}.docs "
                "WHERE (structured_content #>> '{sheet,measurements,rows}')::int > 1000"
            )
        ).fetchall()

    def table():
        conn.execute(
            text(
                f"SELECT count(*) FROM {SCHEMA}.ev "
                "WHERE name = 'sheet' AND (value #>> '{measurements,rows}')::int > 1000"
            )
        ).fetchall()

    _report("guard over the corpus", _time(jsonb, repeats=repeats), _time(table, repeats=repeats))


def bench_stale(conn, rows: list, repeats: int) -> None:
    """13.1's second fact, timed. Binding a rule set stales the evidence its rules produce.

    The JSONB side has no per-name state to set, so the closest honest equivalent is what
    the appliance would actually have to do: rewrite the column on every node in the
    subtree, removing the two blocks the rules produce.
    """
    root = next(r[0] for r in rows if r[3] == "file")
    subtree = "(d.path @> jsonb_build_array(CAST(:r AS int)) OR d.id = :r)"

    def jsonb():
        conn.execute(
            text(
                f"UPDATE {SCHEMA}.docs d SET structured_content = "
                "d.structured_content - 'structure' - 'source_span' "
                f"WHERE {subtree}"
            ),
            {"r": root},
        )
        conn.rollback()

    def table():
        conn.execute(
            text(
                f"UPDATE {SCHEMA}.ev SET state = 'stale' WHERE name IN "
                f"('structure', 'source_span') AND document_id IN "
                f"(SELECT d.id FROM {SCHEMA}.docs d WHERE {subtree})"
            ),
            {"r": root},
        )
        conn.rollback()

    _report(
        "stale a subtree's evidence", _time(jsonb, repeats=repeats), _time(table, repeats=repeats)
    )


# ---------------------------------------------------------------------------
# The race — not a timing
# ---------------------------------------------------------------------------


def race(engine, node_id: int) -> None:
    """Two writers, one node, two different evidence names. Count what survived.

    13.1's first fact, demonstrated rather than argued. Both writers do exactly what every
    handler in the tree does today — read the column, copy it, add a key, assign it back —
    and they interleave the way two workers on one node interleave.
    """
    print("\n  two concurrent writers, two different evidence names, one node")
    with engine.connect() as a, engine.connect() as b:
        a.execute(
            text(
                f"UPDATE {SCHEMA}.docs SET structured_content = '{{}}'::jsonb "
                f"WHERE id = {node_id}"
            )
        )
        a.execute(text(f"DELETE FROM {SCHEMA}.ev WHERE document_id = {node_id}"))
        a.commit()

        # --- JSONB: read-modify-write, exactly as settling.py:269 and sheet_tasks.py:243
        read_a = a.execute(
            text(f"SELECT structured_content FROM {SCHEMA}.docs WHERE id = {node_id}")
        ).scalar_one()
        read_b = b.execute(
            text(f"SELECT structured_content FROM {SCHEMA}.docs WHERE id = {node_id}")
        ).scalar_one()
        a.execute(
            text(f"UPDATE {SCHEMA}.docs SET structured_content = :v WHERE id = {node_id}"),
            {"v": json.dumps(dict(read_a, extraction={"characters": 1}))},
        )
        a.commit()
        b.execute(
            text(f"UPDATE {SCHEMA}.docs SET structured_content = :v WHERE id = {node_id}"),
            {"v": json.dumps(dict(read_b, matched={"format": "pdf"}))},
        )
        b.commit()
        survived = a.execute(
            text(f"SELECT structured_content FROM {SCHEMA}.docs WHERE id = {node_id}")
        ).scalar_one()
        print(f"    jsonb   wrote 2 names, {len(survived)} survived: {sorted(survived)}")

        # --- Per-name rows: two different keys, so nothing to lose
        a.execute(
            text(
                f"INSERT INTO {SCHEMA}.ev (document_id, name, value) "
                f"VALUES ({node_id}, 'extraction', :v)"
            ),
            {"v": json.dumps({"characters": 1})},
        )
        b.execute(
            text(
                f"INSERT INTO {SCHEMA}.ev (document_id, name, value) "
                f"VALUES ({node_id}, 'matched', :v)"
            ),
            {"v": json.dumps({"format": "pdf"})},
        )
        a.commit()
        b.commit()
        names = [
            r[0]
            for r in a.execute(
                text(f"SELECT name FROM {SCHEMA}.ev WHERE document_id = {node_id} ORDER BY name")
            )
        ]
        print(f"    table   wrote 2 names, {len(names)} survived: {names}")


# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--nodes", type=int, default=20_000)
    parser.add_argument("--repeats", type=int, default=25)
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--keep", action="store_true", help=f"leave the {SCHEMA} schema behind")
    args = parser.parse_args(argv)

    engine = get_engine()
    rows = _rows(args.nodes, args.seed)
    values = sum(len(_flatten(blocks)) for *_rest, blocks in rows)
    print(f"\n{args.nodes:,} nodes, {values:,} evidence values, {values / args.nodes:.1f} per node")
    print(f"{args.repeats} repeats per measurement; the number shown is the median\n")

    with engine.connect() as conn:
        build(conn, rows)
        conn.commit()
        bench_one_node(conn, rows, args.repeats)
        bench_one_name(conn, rows, args.repeats)
        bench_guard(conn, rows, args.repeats)
        bench_guard_corpus(conn, rows, args.repeats)
        bench_stale(conn, rows, args.repeats)

    race(engine, rows[0][0])

    if not args.keep:
        with engine.connect() as conn:
            conn.execute(text(f"DROP SCHEMA {SCHEMA} CASCADE"))
            conn.commit()
        print(f"\n  {SCHEMA} dropped. --keep leaves it for EXPLAIN by hand.")
    else:
        print(f"\n  {SCHEMA} kept.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
