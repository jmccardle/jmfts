#!/usr/bin/env python3
"""What it costs to write a BM25 index in one task or in many.

``docs/MEASURE_BM25_BOUNDARY.md``, which is the reading of this script's output.
``docs/STRESS_CORPUS.md`` open question 7.1 asks whether ``index:bm25`` moves to the
settling boundary; the owner asked for the iterative and hierarchical alternatives to be
priced beside it. The number that separates them is the same one 4.5 measured on the
reference corpus — ``index_document`` takes ``SELECT ... FOR UPDATE`` on the ``SearchIndex``
row and holds it to commit, so every transaction boundary inside an index build is a
serialisation point for the whole fleet.

So this measures ONE thing, three ways, over the same synthetic workbook:

* **whole** — one transaction, N ``index_document`` calls. Today's ``run_index_bm25``.
* **per-sheet** — one transaction per sheet subtree. The hierarchical candidate.
* **per-leaf** — one transaction per record node. The iterative candidate.

and then runs each shape again with K writers against the SAME index name, which is the
case 4.5 caught live in ``pg_locks``: six workers, one row, five of them blocked.

It writes and drops its own database. The name must end in ``_measure`` for the same
reason ``measure_shacl_scope`` requires it — a measurement that can DROP is a measurement
that must not be able to point at an appliance.

    JMFTS_DB_HOST=localhost JMFTS_DB_PORT=5445 JMFTS_DB_USER=jmfts \\
      JMFTS_DB_PASSWORD=jmfts JMFTS_DB_NAME=jmfts_measure \\
      ./.venv/bin/python -m scripts.measure_bm25_boundary --records 900 --writers 6

Nothing here imports torch: no node gets a vector, because BM25 does not read one.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

from sqlalchemy import text

from jmfts_core.database import get_session
from jmfts_core.models.document import (
    Document,
    SETTLED_SETTLED,
    USETYPE_FILE,
    USETYPE_RECORD,
    USETYPE_SHEET,
)
from jmfts_core.repositories.search import SearchRepository

#: One row of a NIST control export, rendered `Key: value.` the way `extract:sheet` does.
#: 457 characters is STRESS_CORPUS.md 4.4's measured mean for a `record` node, and the
#: vocabulary is drawn from a fixed pool so term statistics behave like a real corpus's —
#: a few very common terms, a long tail of rare ones.
_COMMON = ["control", "identifier", "name", "month", "time", "population", "value", "sort"]
_TAIL = [f"tok{i:05d}" for i in range(20000)]


def _record_text(rng: random.Random) -> str:
    words = [rng.choice(_COMMON) for _ in range(8)] + [rng.choice(_TAIL) for _ in range(50)]
    rng.shuffle(words)
    out = []
    for i in range(0, len(words), 4):
        out.append(f"{words[i].title()}: {' '.join(words[i + 1 : i + 4])}.")
    return " ".join(out)


def build_tree(workbooks: int, sheets: int, records_per_sheet: int, seed: int = 7) -> list[dict]:
    """`workbooks` workbooks: a file node, `sheets` sheet nodes, `records_per_sheet` leaves.

    Exactly the shape STRESS_CORPUS.md 4.4b measured — `file` and `sheet` carry no
    `content` of their own, every `record` does. That asymmetry is the whole question:
    the leaves are two rungs below the file node's structure rung.
    """
    rng = random.Random(seed)
    out: list[dict] = []
    with get_session() as session:
        session.execute(text("TRUNCATE documents RESTART IDENTITY CASCADE"))
        session.execute(text("TRUNCATE search_indexes RESTART IDENTITY CASCADE"))
        session.execute(text("TRUNCATE search_term_postings"))
        for w in range(workbooks):
            file_node = Document(
                title=f"instruments-{w}.xlsx",
                usetype=USETYPE_FILE,
                path=[],
                settled=SETTLED_SETTLED,
            )
            session.add(file_node)
            session.flush()
            leaf_ids: list[list[int]] = []
            for s in range(sheets):
                sheet = Document(
                    title=f"Sheet{s + 1}",
                    usetype=USETYPE_SHEET,
                    produced_by="structure:sheets",
                    parent_id=file_node.id,
                    path=[file_node.id],
                    settled=SETTLED_SETTLED,
                )
                session.add(sheet)
                session.flush()
                ids = []
                for _ in range(records_per_sheet):
                    rec = Document(
                        title=None,
                        content=_record_text(rng),
                        usetype=USETYPE_RECORD,
                        produced_by="extract:sheet",
                        parent_id=sheet.id,
                        path=[file_node.id, sheet.id],
                        settled=SETTLED_SETTLED,
                    )
                    session.add(rec)
                    ids.append(rec)
                session.flush()
                leaf_ids.append([r.id for r in ids])
            out.append({"file_id": file_node.id, "leaves": leaf_ids})
        session.commit()
    return out


def _fresh_index(name: str) -> None:
    """Empty EVERY search table, then create this one index.

    Not just this index's rows. ``search_term_postings`` is keyed
    ``(index_id, term, document_id)`` and ``index_document`` reads it back per document, so
    a shape measured with two earlier shapes' postings still in the table pays for their
    index maintenance on every insert. The first draft of this script did exactly that and
    reported the shapes in run order, slowest last; the ordering was the finding, not the
    shape.
    """
    with get_session() as session:
        session.execute(text("TRUNCATE search_indexes RESTART IDENTITY CASCADE"))
        session.execute(text("TRUNCATE search_term_postings"))
        SearchRepository(session).create_index(name)
        session.commit()


def _index_batch(doc_ids: list[int], index_name: str) -> float:
    """One transaction, every id in it. Returns wall seconds."""
    t0 = time.perf_counter()
    with get_session() as session:
        repo = SearchRepository(session)
        for doc_id in doc_ids:
            repo.index_document(doc_id, index_name)
        session.commit()
    return time.perf_counter() - t0


def _lock_wait(index_name: str) -> float:
    """How long just the FOR UPDATE round trip takes on an uncontended row."""
    t0 = time.perf_counter()
    with get_session() as session:
        session.execute(
            text("SELECT id FROM search_indexes WHERE name = :n FOR UPDATE"), {"n": index_name}
        ).one()
        session.commit()
    return time.perf_counter() - t0


SHAPES = ("whole", "per-sheet", "per-leaf")


def _batches(book: dict, shape: str) -> list[list[int]]:
    flat = [i for sheet in book["leaves"] for i in sheet]
    if shape == "whole":
        return [flat]
    if shape == "per-sheet":
        return [list(sheet) for sheet in book["leaves"]]
    if shape == "per-leaf":
        return [[i] for i in flat]
    raise ValueError(shape)


def _index_stats(name: str) -> dict:
    with get_session() as session:
        row = session.execute(
            text(
                "SELECT total_docs, avg_doc_length, "
                "(SELECT count(*) FROM search_term_postings p WHERE p.index_id = i.id) AS postings,"
                "(SELECT count(*) FROM search_term_stats s WHERE s.index_id = i.id) AS terms "
                "FROM search_indexes i WHERE name = :n"
            ),
            {"n": name},
        ).one()
    return {
        "total_docs": row.total_docs,
        "avg_doc_length": round(float(row.avg_doc_length), 2),
        "postings": row.postings,
        "terms": row.terms,
    }


def run_serial(books: list[dict], shape: str) -> dict:
    """One writer, every workbook, in this shape. The single-worker cost of the build."""
    name = "m"
    _fresh_index(name)
    batches = [b for book in books for b in _batches(book, shape)]
    t0 = time.perf_counter()
    for batch in batches:
        _index_batch(batch, name)
    wall = time.perf_counter() - t0
    return {
        "shape": shape,
        "transactions": len(batches),
        "wall_s": round(wall, 3),
        **_index_stats(name),
    }


def _lane(arg: tuple[list[list[int]], str, bool]) -> None:
    """One writer's whole share. Module level so a PROCESS pool can pickle it.

    ``fork`` copies the parent's live libpq sockets into the child, and two processes
    writing one socket is not a race the driver survives — the first run of the process arm
    died with ``server closed the connection unexpectedly``. ``dispose(close=False)`` drops
    the inherited pool WITHOUT sending anything down those sockets (the parent still owns
    them), so the child opens its own.
    """
    batches, name, forked = arg
    if forked:
        import jmfts_core.database as _db

        if _db._engine is not None:
            _db._engine.dispose(close=False)
    for batch in batches:
        _index_batch(batch, name)


def run_fleet(books: list[dict], shape: str, writers: int, *, processes: bool) -> dict:
    """One writer per workbook, ONE index name. 4.5's live case, reproduced.

    Each writer owns a whole workbook, so no two writers touch the same document and the
    only contention measured is the ``SearchIndex`` row every one of them must lock.

    **Threads or processes, and it matters.** A thread pool overlaps one writer's Python
    work with another's socket wait only as far as the GIL allows, and a real fleet is six
    OS processes (``jmfts-worker``), not six threads. The first run of this script used
    threads and reported per-leaf as the FASTEST shape under contention; that is the number
    most likely to be an artefact, so both are available and the document reports both.
    """
    name = "c"
    _fresh_index(name)
    lanes = [(list(_batches(book, shape)), name, processes) for book in books]
    pool_cls = ProcessPoolExecutor if processes else ThreadPoolExecutor
    t0 = time.perf_counter()
    with pool_cls(max_workers=writers) as pool:
        list(pool.map(_lane, lanes))
    wall = time.perf_counter() - t0
    return {
        "shape": shape,
        "writers": min(writers, len(lanes)),
        "processes": processes,
        "transactions": sum(len(x[0]) for x in lanes),
        "wall_s": round(wall, 3),
        **_index_stats(name),
    }


def _median(rows: list[dict], key: str = "wall_s") -> float:
    return round(statistics.median(r[key] for r in rows), 3)


# ---------------------------------------------------------------------------
# Phase 2 — what a HALF-BUILT index answers, and what it would cost to say so
# ---------------------------------------------------------------------------

#: The shipped `doc_scores` CTE (`repositories/search.py:698`), reduced to the part the
#: lifecycle question turns on, and its variant with the gate every OTHER retrieval method
#: already applies (`:404` vector, `:1028` maxsim, `:836` the BM25 container pass).
_SCORE_SQL = """
    SELECT tp.document_id, SUM(tp.term_freq::float / e.doc_length) AS s
    FROM search_term_postings tp
    JOIN search_index_entries e
      ON e.index_id = tp.index_id AND e.document_id = tp.document_id
    {join}
    WHERE tp.index_id = :index_id AND tp.term = ANY(CAST(:terms AS text[]))
    {gate}
    GROUP BY tp.document_id
    ORDER BY s DESC LIMIT 20
"""
_GATED_JOIN = "JOIN documents d ON d.id = tp.document_id"
_GATED_WHERE = "AND d.settled = 'settled'"


def run_retrieval_probe(books: list[dict]) -> dict:
    """Index everything, then put half of it back in flight and ask what answers.

    The question is the Fail Early one: can a caller tell a half-built index from a
    complete one? The index is fully built here and the TREE is half in flight, which is
    the same wire state an iterative build produces at its midpoint — postings exist for
    nodes the appliance does not consider finished.
    """
    _fresh_index("probe")
    flat = [i for book in books for sheet in book["leaves"] for i in sheet]
    _index_batch(flat, "probe")

    half = set(flat[: len(flat) // 2])
    with get_session() as session:
        session.execute(
            text("UPDATE documents SET settled = 'in_flight' WHERE id = ANY(:ids)"),
            {"ids": list(half)},
        )
        session.commit()

    out: dict = {"in_flight_nodes": len(half), "indexed_nodes": len(flat)}
    with get_session() as session:
        repo = SearchRepository(session)
        hits = repo.bm25_search("control identifier population", index_name="probe", limit=20)
        out["bm25_hits"] = len(hits)
        out["bm25_hits_in_flight"] = sum(1 for h in hits if h.document.id in half)

        index_id = session.execute(
            text("SELECT id FROM search_indexes WHERE name = 'probe'")
        ).scalar_one()
        terms = repo._tokenize("control identifier population")
        params = {"index_id": index_id, "terms": terms}
        for label, join, gate in (
            ("shipped", "", ""),
            ("with settled gate", _GATED_JOIN, _GATED_WHERE),
        ):
            sql = _SCORE_SQL.format(join=join, gate=gate)
            # Warm, then take the planner's own timing three times.
            session.execute(text(sql), params).all()
            plans = []
            for _ in range(3):
                rows = session.execute(
                    text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql), params
                ).scalar_one()
                plans.append(rows[0]["Execution Time"])
            out[label] = {
                "exec_ms": round(statistics.median(plans), 3),
                "rows": len(session.execute(text(sql), params).all()),
            }
        session.execute(text("UPDATE documents SET settled = 'settled'"))
        session.commit()
    return out


#: Option I's whole task body: the documents a covering index should hold and does not.
#: If this is not cheap at every settling boundary, Option I is not cheap.
_CATCHUP_SQL = """
    SELECT d.id
    FROM documents d
    LEFT JOIN search_index_entries e ON e.index_id = :index_id AND e.document_id = d.id
    WHERE d.path @> jsonb_build_array(CAST(:node_id AS integer))
      AND d.settled = 'settled'
      AND d.content IS NOT NULL
      AND e.document_id IS NULL
"""


def run_catchup_probe(books: list[dict]) -> dict:
    """What the catch-up anti-join costs when it has nothing to do, and when it has work.

    The no-work case is the one that decides Option I: the rule is offered at EVERY
    settling boundary, so a corpus pays for it once per node that settles, and almost every
    one of those firings finds nothing.
    """
    _fresh_index("catchup")
    flat = [i for book in books for sheet in book["leaves"] for i in sheet]
    with get_session() as session:
        for book in books:
            SearchRepository(session).add_root_to_index("catchup", book["file_id"])
        session.commit()

    out: dict = {}
    with get_session() as session:
        index_id = session.execute(
            text("SELECT id FROM search_indexes WHERE name = 'catchup'")
        ).scalar_one()
        node_id = books[0]["file_id"]
        params = {"index_id": index_id, "node_id": node_id}

        def _timed(label: str) -> None:
            session.execute(text(_CATCHUP_SQL), params).all()
            plans = [
                session.execute(
                    text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + _CATCHUP_SQL), params
                ).scalar_one()[0]
                for _ in range(5)
            ]
            out[label] = {
                "exec_ms": round(statistics.median(p["Execution Time"] for p in plans), 3),
                "rows": len(session.execute(text(_CATCHUP_SQL), params).all()),
                "plan": plans[0]["Plan"]["Node Type"],
            }

        _timed("everything outstanding")
    _index_batch(flat, "catchup")
    with get_session() as session:
        index_id = session.execute(
            text("SELECT id FROM search_indexes WHERE name = 'catchup'")
        ).scalar_one()
        params = {"index_id": index_id, "node_id": books[0]["file_id"]}
        session.execute(text(_CATCHUP_SQL), params).all()
        plans = [
            session.execute(
                text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + _CATCHUP_SQL), params
            ).scalar_one()[0]
            for _ in range(5)
        ]
        out["nothing to do"] = {
            "exec_ms": round(statistics.median(p["Execution Time"] for p in plans), 3),
            "rows": len(session.execute(text(_CATCHUP_SQL), params).all()),
            "plan": plans[0]["Plan"]["Node Type"],
        }
    return out


def run_bulk_rebuild(books: list[dict]) -> dict:
    """``refresh_index`` over the same corpus: the batched path, for scale.

    ``index_document`` is one statement per term for the ``doc_freq`` upsert
    (``STRESS_CORPUS.md`` 4.6's second paragraph); ``refresh_index`` builds the same three
    tables with ``execute_values``. The ratio is what an option that rebuilds an index at a
    boundary would be paying, per document, instead of the incremental cost.
    """
    _fresh_index("bulk")
    with get_session() as session:
        repo = SearchRepository(session)
        for book in books:
            repo.add_root_to_index("bulk", book["file_id"])
        session.commit()
    t0 = time.perf_counter()
    with get_session() as session:
        stats = SearchRepository(session).refresh_index("bulk")
        session.commit()
    return {"wall_s": round(time.perf_counter() - t0, 3), **stats, **_index_stats("bulk")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workbooks", type=int, default=6)
    parser.add_argument("--sheets", type=int, default=2)
    parser.add_argument("--records", type=int, default=75, help="records PER SHEET")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--processes", action="store_true", help="fleet arm in PROCESSES")
    parser.add_argument(
        "--phase",
        choices=("shapes", "extra", "all"),
        default="all",
        help="`shapes` is Part 2's ladder; `extra` is the rebuild and the lifecycle probe",
    )
    parser.add_argument("--json", default=None)
    args = parser.parse_args()

    name = os.environ.get("JMFTS_DB_NAME", "")
    if not name.endswith("_measure"):
        raise SystemExit(
            f"JMFTS_DB_NAME is {name!r}; this script TRUNCATES documents and refuses any "
            "database whose name does not end in '_measure'"
        )

    books = build_tree(args.workbooks, args.sheets, args.records)
    n = args.workbooks * args.sheets * args.records
    print(
        f"corpus: {args.workbooks} workbooks x {args.sheets} sheets x {args.records} "
        f"records = {n} nodes with content\n"
    )

    results: dict = {"serial": {}, "fleet": {}, "records": n, "repeats": args.repeats}
    # Interleaved by repeat, not grouped by shape: a run-order effect then shows up as
    # spread WITHIN a shape rather than as a difference BETWEEN them. The first draft
    # grouped them and reported the shapes slowest-last, which was the order and not the
    # shape — `search_term_postings` was never emptied between them.
    if args.phase in ("shapes", "all"):
        for rep in range(args.repeats):
            for shape in SHAPES:
                r = run_serial(books, shape)
                results["serial"].setdefault(shape, []).append(r)
                r2 = run_fleet(books, shape, args.workbooks, processes=args.processes)
                results["fleet"].setdefault(shape, []).append(r2)
                print(
                    f"  rep{rep} {shape:10s} serial {r['wall_s']:7.3f} s / "
                    f"{r['transactions']:5d} tx   fleet {r2['wall_s']:7.3f} s"
                )

        print(
            f"\n{'shape':10s} {'tx':>6s} {'serial med':>11s} {'fleet med':>10s} {'docs':>6s}"
            f" {'postings':>9s} {'terms':>7s}"
        )
        for shape in SHAPES:
            s = results["serial"][shape]
            f = results["fleet"][shape]
            print(
                f"{shape:10s} {s[0]['transactions']:6d} {_median(s):11.3f} {_median(f):10.3f}"
                f" {s[0]['total_docs']:6d} {s[0]['postings']:9d} {s[0]['terms']:7d}"
            )

        _fresh_index("lockprobe")
        waits = [_lock_wait("lockprobe") for _ in range(50)]
        results["lock_roundtrip_ms"] = round(statistics.median(waits) * 1000, 3)
        print(
            f"\nuncontended BEGIN + SELECT FOR UPDATE + COMMIT: "
            f"median {results['lock_roundtrip_ms']:.2f} ms over 50"
        )

    if args.phase == "shapes":
        if args.json:
            with open(args.json, "w") as fh:
                json.dump(results, fh, indent=2)
        return 0

    bulk = run_bulk_rebuild(books)
    results["bulk"] = bulk
    print(
        f"\nrefresh_index (batched rebuild): {bulk['wall_s']:.3f} s for {bulk['indexed']} "
        f"documents, {bulk['postings']} postings"
    )

    catchup = run_catchup_probe(books)
    results["catchup"] = catchup
    print("\nOption I's anti-join, one workbook's subtree:")
    for label, r in catchup.items():
        print(f"  {label:22s} {r['exec_ms']:7.3f} ms, {r['rows']:5d} rows, {r['plan']}")

    probe = run_retrieval_probe(books)
    results["retrieval"] = probe
    print(
        f"\nhalf the corpus put back in flight ({probe['in_flight_nodes']} of "
        f"{probe['indexed_nodes']}), index fully built:\n"
        f"  bm25_search returned {probe['bm25_hits']} results, "
        f"{probe['bm25_hits_in_flight']} of them in flight\n"
        f"  scoring CTE, shipped:            {probe['shipped']['exec_ms']:7.3f} ms, "
        f"{probe['shipped']['rows']} rows\n"
        f"  scoring CTE, + settled gate:     "
        f"{probe['with settled gate']['exec_ms']:7.3f} ms, "
        f"{probe['with settled gate']['rows']} rows"
    )

    if args.json:
        with open(args.json, "w") as fh:
            json.dump(results, fh, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
