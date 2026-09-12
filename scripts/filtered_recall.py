#!/usr/bin/env python3
"""Does an HNSW scan under a narrow filter return a short page? SPRINT_0_4_0.md step 1.

``vector_search`` ANDs five predicates into the same statement that carries the HNSW
``ORDER BY`` (``repositories/search.py:97``-``:162``), and nothing in ``jmfts_core/`` sets
``hnsw.iterative_scan`` or ``hnsw.ef_search``. If pgvector applies those predicates to the
rows the index hands back rather than pushing them into the walk, then a principal whose
readable subtree holds none of the ``ef_search`` nearest neighbours receives an empty page
while matching readable rows exist — and a short page is indistinguishable from "there is
nothing else".

**This script is a diagnosis, not a benchmark.** It does not measure recall against a
corpus and needs no model. The failure, if it is real, is structural, so it is constructed
rather than sampled: a large UNREADABLE cluster placed next to the query vector and a
small READABLE cluster placed far from it. Every readable row is a correct answer and
every one of them sorts below the whole unreadable cluster, so the exact answer is known
in advance and any shortfall is the index.

It runs against its own throwaway table, NOT against the appliance schema. That is
deliberate: the question is what pgvector does with a filter beside an HNSW ORDER BY, and
answering it inside `documents` would mix in five predicates, a partial index and the
settled lifecycle. If this reports truncation, the follow-up is a test in ``tests/``
driving the real ``vector_search``; if it reports none, there is nothing to write.

    ./scripts/filtered_recall.py --dsn postgresql://jmfts:jmfts@localhost:5434/postgres
    ./scripts/filtered_recall.py --sizes 1000,20000 --ef 40,100

Two things it reports before any result, because both change what the result means:
the pgvector version (``hnsw.iterative_scan`` is a 0.8.0 GUC; below that the fix is a
version bump, not a setting), and whether the planner actually chose the index scan. Below
some table size Postgres seq-scans and sorts exactly, which hides the defect rather than
disproving it.

WHAT CAME OF IT, 2026-09-05. It reported truncation, the follow-up test is
``tests/test_filtered_recall.py``, and writing it found a second defect underneath this
one: ``vector_search`` ordered by the similarity LABEL, so it never reached
``idx_documents_embed`` at all and the truncation this script measured could not reach the
appliance. All three of Block A's steps have now landed — the ORDER BY is the distance
operator, ``repositories/search.py`` sets ``hnsw.iterative_scan`` guarded on the server
registering it, and a short page reports itself on ``AppliedFilters.truncated``. The test
reproduces the 0-of-10 empty page against the appliance's own schema whenever the
iterative-scan mode is taken back off, which is what keeps this script's finding honest
rather than historical.

The guard in ``_supports_iterative_scan`` below is the same one
``repositories/search.py:_hnsw_iterative_scan_available`` needs, and for the same reason;
the appliance loads pgvector with a cast rather than ``LOAD`` because it must not require
a superuser.
"""

import argparse
import json
import random
import sys

DIM = 768
#: What the caller asks for.
LIMIT = 10
DEFAULT_SIZES = "20000,200000"
DEFAULT_EF = "40,100,400"
#: How much of the table the principal may read. **Swept, not chosen, and the sweep is the
#: whole experiment.** Postgres prices the HNSW path against a seq-scan-and-sort using its
#: estimate of how many rows survive the filter, so a filter it knows is narrow makes it
#: pick the exact plan and no truncation is possible. Truncation needs the planner to
#: choose the index, which it does when the filter looks wide. A principal holding a
#: sizeable subtree of a large appliance is exactly that case.
DEFAULT_READABLE_PCT = "1,5,20,50"
#: pgvector 0.8.0+ only. `off` is the default and is what the appliance runs under today.
SCAN_MODES = ("off", "relaxed_order", "strict_order")


def _unit(values):
    norm = sum(v * v for v in values) ** 0.5
    return [v / norm for v in values]


def _near(rng, spread):
    """A vector close to the probe query, which is the first basis vector."""
    v = [rng.gauss(0.0, spread) for _ in range(DIM)]
    v[0] += 1.0
    return _unit(v)


def _far(rng):
    """A vector in the opposite half-space from the probe query.

    Not merely orthogonal. Cosine distance to the query is then above 1, so every readable
    row sorts strictly below every near row and the ordering is a property of the
    construction rather than of HNSW's approximation. That is what makes the expected
    answer exact.
    """
    v = [rng.gauss(0.0, 1.0) for _ in range(DIM)]
    v[0] = -abs(v[0]) - 2.0
    return _unit(v)


def _literal(vector):
    return "[" + ",".join(f"{x:.6f}" for x in vector) + "]"


def _server_facts(cur):
    cur.execute("SELECT current_setting('server_version')")
    server = cur.fetchone()[0]
    cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    row = cur.fetchone()
    return server, (row[0] if row else None)


def _supports_iterative_scan(cur):
    """Is `hnsw.iterative_scan` settable in this session?

    `LOAD` first, and the reason is a trap this script fell into once. pgvector registers
    its GUCs in `_PG_init`, which does not run until the shared library is loaded into the
    backend, and `CREATE EXTENSION` alone does not load it. So `pg_settings` reports the
    setting as absent on a 0.8.5 server that supports it perfectly well, and a probe that
    trusts that answer silently skips the two modes it exists to test.
    """
    cur.execute("LOAD 'vector'")
    cur.execute("SELECT count(*) FROM pg_settings WHERE name = 'hnsw.iterative_scan'")
    return cur.fetchone()[0] == 1


def _build(cur, size, readable_pct, rng):
    """One table, one index. `readable_pct` of the rows are readable and ALL of them are
    far from the query; every unreadable row is near it.

    So the readable rows are simultaneously the only correct answers and the last rows the
    walk would reach. The appliance's shape is the same one: a principal granted a subtree
    that is a real fraction of the corpus, none of whose documents happens to be among the
    query's nearest neighbours.
    """
    cur.execute("DROP TABLE IF EXISTS filtered_recall_probe")
    cur.execute(
        "CREATE TABLE filtered_recall_probe ("
        "  id serial PRIMARY KEY,"
        "  readable boolean NOT NULL,"
        f"  embed vector({DIM}) NOT NULL)"
    )
    readable_count = max(LIMIT, round(size * readable_pct / 100.0))
    near_count = size - readable_count
    if near_count < 1:
        raise ValueError(f"size {size} at {readable_pct}% leaves no unreadable rows")

    def flush(batch):
        cur.executemany(
            "INSERT INTO filtered_recall_probe (readable, embed) VALUES (%s, %s)", batch
        )

    batch = []
    for index in range(size):
        readable = index < readable_count
        vector = _far(rng) if readable else _near(rng, 0.35)
        batch.append((readable, _literal(vector)))
        if len(batch) >= 2000:
            flush(batch)
            batch = []
    if batch:
        flush(batch)

    # `m` and `ef_construction` are the appliance's own, copied from
    # `sql/schema.sql:478`-`:480` so that graph connectivity is not a difference between
    # this probe and `idx_documents_embed`. They happen to be pgvector's defaults too.
    cur.execute(
        "CREATE INDEX ON filtered_recall_probe USING hnsw (embed vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )
    cur.execute("ANALYZE filtered_recall_probe")
    return readable_count


def _probe(cur, query, ef, scan_mode, supports_scan):
    cur.execute(f"SET hnsw.ef_search = {int(ef)}")
    if supports_scan:
        cur.execute(f"SET hnsw.iterative_scan = {scan_mode}")

    sql = (
        "SELECT id FROM filtered_recall_probe "
        "WHERE readable ORDER BY embed <=> %s::vector LIMIT %s"
    )
    cur.execute("EXPLAIN (FORMAT JSON) " + sql, (query, LIMIT))
    plan = json.dumps(cur.fetchone()[0])
    used_index = "Index Scan" in plan

    cur.execute(sql, (query, LIMIT))
    returned = len(cur.fetchall())
    return returned, used_index


def run(dsn, sizes, pcts, efs, seed):
    import psycopg2

    rng = random.Random(seed)
    query = _literal(_unit([1.0] + [0.0] * (DIM - 1)))

    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    server, vector_version = _server_facts(cur)
    supports_scan = _supports_iterative_scan(cur)

    print(f"# postgres {server}, pgvector {vector_version}")
    print(f"# hnsw.iterative_scan: {'available' if supports_scan else 'NOT AVAILABLE (< 0.8.0)'}")
    print(f"# the query asks for {LIMIT}; every readable row is a correct answer, so a")
    print("# returned count below that is the index truncating and not the data running out\n")

    modes = SCAN_MODES if supports_scan else ("off",)
    print(
        f"{'rows':>8s} {'read%':>6s} {'readable':>9s} {'ef':>5s} {'scan':>14s} "
        f"{'got':>4s} {'plan':>9s}  verdict"
    )
    findings = []
    for size in sizes:
        for pct in pcts:
            readable_count = _build(cur, size, pct, rng)
            for ef in efs:
                for mode in modes:
                    returned, used_index = _probe(cur, query, ef, mode, supports_scan)
                    if not used_index:
                        verdict = "exact plan; no truncation possible"
                    elif returned < LIMIT:
                        verdict = f"TRUNCATED, {LIMIT - returned} of {LIMIT} missing"
                        findings.append((size, pct, ef, mode, returned))
                    else:
                        verdict = "complete"
                    print(
                        f"{size:8d} {pct:6g} {readable_count:9d} {ef:5d} {mode:>14s} "
                        f"{returned:4d} {'hnsw' if used_index else 'seq+sort':>9s}  {verdict}"
                    )
    cur.execute("DROP TABLE IF EXISTS filtered_recall_probe")
    conn.close()

    print()
    if findings:
        default = [f for f in findings if f[2] == 40 and f[3] == "off"]
        print(f"# {len(findings)} truncating configuration(s).")
        if default:
            print(
                f"#   {len(default)} of them at the APPLIANCE'S OWN SETTINGS "
                "(ef_search=40, iterative_scan=off)."
            )
            worst = min(default, key=lambda f: f[4])
            print(
                f"#   Worst: {worst[0]} rows, {worst[1]}% readable, returned {worst[4]} of "
                f"{LIMIT}."
            )
        else:
            print(
                "#   NONE at ef_search=40 with iterative_scan=off. Widen --sizes or "
                "--readable-pct before concluding the appliance is unaffected."
            )
    else:
        print("# No configuration truncated. Either every plan here was the exact one, or")
        print("# the filter is pushed into the walk. Read the `plan` column before concluding.")
    return 0 if not findings else 1


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--sizes", default=DEFAULT_SIZES)
    ap.add_argument("--readable-pct", default=DEFAULT_READABLE_PCT)
    ap.add_argument("--ef", default=DEFAULT_EF)
    ap.add_argument("--seed", type=int, default=20260904)
    args = ap.parse_args()
    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]
    pcts = [float(p) for p in args.readable_pct.split(",") if p.strip()]
    efs = [int(e) for e in args.ef.split(",") if e.strip()]
    return run(args.dsn, sizes, pcts, efs, args.seed)


if __name__ == "__main__":
    sys.exit(main())
