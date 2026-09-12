"""Does it matter that `idx_token_embed_256_ivf` is built before its rows exist?

`docs/ANN_INDEX_HEALTH.md` 5.4 measured that on the real appliance path and found it the
largest term. 5.6 then left a four-way decision open, and its table has no column for the
one question that separates the fourth row from the other three: whether HNSW shares the
defect at all. This script measures that, and is what 5.7 records.

`jmfts_core/sql/schema.sql:514`-`:515` creates the IVFFlat index in the schema file; the
first INSERT in that file is at `:669`, and `git grep REINDEX -- jmfts_core` returns
nothing. So the shipped index fixes 1024 centroids against zero rows and never recomputes
them.

THE CORPUS IS SYNTHETIC AND MORE CLUSTERABLE THAN REAL TOKEN EMBEDDINGS. Every absolute
recall below is optimistic; 5.4's 0.7533 at 1.37M rows is the better figure for how a
fitted IVFFlat reads. What this measures reliably is the CONTRAST between two build orders
at a fixed `lists` and `probes`, because nothing else differs between the pair.

`lists` is swept at three values and only one of them mirrors the appliance.
`STRESS_CORPUS.md` 6.2 reads `token_embeddings` at 1,370,494 rows, which at `lists = 1024`
is 1,338 rows per list. At this script's default N that ratio is `lists = 37`; `lists =
1024` here is 49 rows per list and says nothing about the shipped configuration.

Needs a pgvector server and psycopg2. Nothing here touches the appliance database:

    docker run -d --name ivf-probe -e POSTGRES_USER=jmfts -e POSTGRES_PASSWORD=jmfts \
        -e POSTGRES_DB=jmfts -p 127.0.0.1:5470:5432 pgvector/pgvector:pg16
    PGHOST=localhost PGPORT=5470 PGUSER=jmfts PGPASSWORD=jmfts PGDB=jmfts \
        python -m scripts.measure_ivf_build_order

Parallel index build is disabled because the pgvector image ships docker's default 64 MB
/dev/shm and a parallel HNSW build asks for about 1 GB of it — the failure is
`could not resize shared memory segment`, which reads like a full disk and is not one.
"""

from __future__ import annotations

import argparse
import io
import os
import random
import sys
import time

import psycopg2

DIM = 256
K = 10

#: (label, lists). The first mirrors the appliance's rows-per-list at the default N.
DEFAULT_LISTS = [("appliance ratio", 37), ("sqrt(N)", 223), ("shipped literal", 1024)]

#: What `STRESS_CORPUS.md` 6.2 read, and what `sql/schema.sql:515` sets. Used only to
#: derive the ratio the first `lists` value reproduces.
APPLIANCE_ROWS = 1_370_494
APPLIANCE_LISTS = 1024


def dsn_from_env() -> str:
    missing = [
        k for k in ("PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDB") if k not in os.environ
    ]
    if missing:
        raise SystemExit(f"set {', '.join(missing)} — see this module's docstring")
    return (
        f"host={os.environ['PGHOST']} port={os.environ['PGPORT']} "
        f"user={os.environ['PGUSER']} password={os.environ['PGPASSWORD']} "
        f"dbname={os.environ['PGDB']}"
    )


def fmt(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.4f}" for x in v) + "]"


def make_corpus(rng: random.Random, n: int, clusters: int, spread: float, queries: int):
    """Points drawn around `clusters` centres, and queries drawn the same way.

    Gaussian noise with no cluster structure is the wrong fixture here: IVFFlat partitions
    by k-means, so on unclusterable data every arm reads badly and the build-order contrast
    the script is for disappears into the floor.
    """
    centres = [[rng.gauss(0, 1) for _ in range(DIM)] for _ in range(clusters)]

    def draw() -> list[float]:
        c = centres[rng.randrange(clusters)]
        return [c[i] + rng.gauss(0, spread) for i in range(DIM)]

    return [draw() for _ in range(n)], [fmt(draw()) for _ in range(queries)]


def copy_rows(cur, table: str, rows) -> float:
    buf = io.StringIO()
    for r in rows:
        buf.write(fmt(r) + "\n")
    buf.seek(0)
    t0 = time.perf_counter()
    cur.copy_expert(f"COPY {table} (embed) FROM STDIN", buf)
    return time.perf_counter() - t0


def exact_top_k(cur, table: str, queries: list[str]) -> list[set[int]]:
    """Brute force on the same table, with both index paths shut off in the planner."""
    cur.execute("SET enable_indexscan = off")
    cur.execute("SET enable_bitmapscan = off")
    out = []
    for q in queries:
        cur.execute(f"SELECT id FROM {table} ORDER BY embed <=> %s::halfvec LIMIT %s", (q, K))
        out.append({r[0] for r in cur.fetchall()})
    cur.execute("SET enable_indexscan = on")
    cur.execute("SET enable_bitmapscan = on")
    return out


def recall(cur, table: str, queries: list[str], exact: list[set[int]], setting: str):
    cur.execute(setting)
    hits = 0
    t0 = time.perf_counter()
    for q, ex in zip(queries, exact):
        cur.execute(f"SELECT id FROM {table} ORDER BY embed <=> %s::halfvec LIMIT %s", (q, K))
        hits += len({r[0] for r in cur.fetchall()} & ex)
    ms = (time.perf_counter() - t0) * 1000 / len(queries)
    return hits / (len(queries) * K), ms


def build(cur, table: str, rows, index_sql: str, empty_first: bool) -> float:
    """Create the table and its index in one of the two orders. Returns seconds of index work.

    `empty_first` is the schema.sql order: CREATE INDEX, then the rows arrive through it.
    """
    cur.execute(f"DROP TABLE IF EXISTS {table}")
    cur.execute(f"CREATE TABLE {table} (id serial PRIMARY KEY, embed halfvec({DIM}))")
    if empty_first:
        cur.execute(index_sql)
        seconds = copy_rows(cur, table, rows)
    else:
        copy_rows(cur, table, rows)
        cur.execute(f"ANALYZE {table}")
        t0 = time.perf_counter()
        cur.execute(index_sql)
        seconds = time.perf_counter() - t0
    cur.execute(f"ANALYZE {table}")
    return seconds


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rows", type=int, default=50_000)
    ap.add_argument("--clusters", type=int, default=400)
    ap.add_argument("--spread", type=float, default=0.35)
    ap.add_argument("--queries", type=int, default=100)
    ap.add_argument("--probes", type=int, nargs="+", default=[1, 10, 64])
    ap.add_argument("--ef-search", type=int, nargs="+", default=[40, 100])
    ap.add_argument("--seed", type=int, default=20260910)
    args = ap.parse_args(argv)

    ratio = APPLIANCE_ROWS / APPLIANCE_LISTS
    mirror = max(1, round(args.rows / ratio))
    lists_arms = [(label, n) for label, n in DEFAULT_LISTS if label != "appliance ratio"]
    lists_arms.insert(0, ("appliance ratio", mirror))

    rng = random.Random(args.seed)
    print(
        f">> corpus: {args.rows} rows, {args.clusters} clusters, spread {args.spread}",
        file=sys.stderr,
    )
    rows, queries = make_corpus(rng, args.rows, args.clusters, args.spread, args.queries)

    conn = psycopg2.connect(dsn_from_env())
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    cur.execute("SET maintenance_work_mem = '1GB'")
    cur.execute("SET max_parallel_maintenance_workers = 0")

    out: list[tuple] = []
    timings: list[tuple[str, float]] = []

    for label, lists in lists_arms:
        for empty_first in (True, False):
            order = "built empty" if empty_first else "built on data"
            table = f"ivf_{lists}_{'e' if empty_first else 'd'}"
            idx = (
                f"CREATE INDEX {table}_idx ON {table} "
                f"USING ivfflat (embed halfvec_cosine_ops) WITH (lists = {lists})"
            )
            secs = build(cur, table, rows, idx, empty_first)
            timings.append((f"IVFFlat lists={lists}, {order}", secs))
            exact = exact_top_k(cur, table, queries)
            for p in args.probes:
                r, ms = recall(cur, table, queries, exact, f"SET ivfflat.probes = {p}")
                out.append((f"IVFFlat lists={lists} ({label})", order, f"probes={p}", r, ms))
                print(
                    f"   lists={lists:>4} {order:<13} probes={p:>3}: recall {r:.3f}, {ms:.1f} ms",
                    file=sys.stderr,
                    flush=True,
                )

    for empty_first in (False, True):
        order = "built empty" if empty_first else "built on data"
        table = f"hnsw_{'e' if empty_first else 'd'}"
        idx = f"CREATE INDEX {table}_idx ON {table} USING hnsw (embed halfvec_cosine_ops)"
        secs = build(cur, table, rows, idx, empty_first)
        timings.append((f"HNSW, {order}", secs))
        exact = exact_top_k(cur, table, queries)
        for ef in args.ef_search:
            r, ms = recall(cur, table, queries, exact, f"SET hnsw.ef_search = {ef}")
            out.append(("HNSW", order, f"ef_search={ef}", r, ms))
            print(
                f"   hnsw       {order:<13} ef={ef:>4}: recall {r:.3f}, {ms:.1f} ms",
                file=sys.stderr,
                flush=True,
            )

    print()
    print(
        f"N={args.rows}, {args.clusters} clusters, spread {args.spread}, "
        f"recall@{K} over {args.queries} queries, seed {args.seed}."
    )
    print(
        f"The appliance runs {APPLIANCE_ROWS:,} rows at lists={APPLIANCE_LISTS} "
        f"= {ratio:.0f} rows/list; lists={mirror} reproduces that ratio at this N."
    )
    print("The corpus is more clusterable than real token embeddings: read the contrast")
    print("between build orders, not the absolute recall.")
    print()
    print("| index | build order | setting | recall@10 | ms/query |")
    print("|---|---|---|---:|---:|")
    for index, order, setting, r, ms in out:
        print(f"| {index} | {order} | {setting} | {r:.3f} | {ms:.1f} |")
    print()
    print("| index work | seconds |")
    print("|---|---:|")
    for label, secs in timings:
        print(f"| {label} | {secs:.1f} |")
    print()
    print("`built empty` seconds are the COPY through a live index; `built on data` seconds")
    print("are the CREATE INDEX. They are not the same quantity and do not subtract.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
