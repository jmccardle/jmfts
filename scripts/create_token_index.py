#!/usr/bin/env python3
"""Rebuild `idx_token_embed_256_hnsw` on an appliance database.

**This was `scripts/create_ivf_index.py` and it built the index migration 022 removes.**
Left as it was, running it after the migration would silently put the IVFFlat index back —
centroids fitted to whatever the table held at that moment, no ledger row saying so, and a
`jmfts_core` that sets `hnsw.iterative_scan` against an index with no such setting. That is
a worse state than either end of the migration, so the file builds what `schema.sql` builds
or it does nothing.

`docs/ANN_INDEX_HEALTH.md` 5.8 is the decision and `STRESS_CORPUS.md` 6.2 is what it cost
on the reference corpus: 2.7x build time and 46% more disk, for recall@10 0.7533 -> 0.9667
at unchanged query latency.

WHEN AN OPERATOR NEEDS THIS AT ALL. Not after ingest — an HNSW graph is built by insertion,
so unlike the index it replaces this one does not go stale and never needs a scheduled
rebuild; that property is the whole reason 5.8 chose it. The two real occasions are a
database whose index was dropped, and a corpus that has been bulk-deleted and re-ingested,
where `STRESS_CORPUS.md` 6.3 measured HNSW's vacuum arriving in full rather than
incrementally. Rebuilding is how that is paid off in one go.

Connection comes from `JMFTS_DB_*` through `jmfts_core.database`, as every other entry
point does. Nothing here names a machine.

    python -m scripts.create_token_index
    python -m scripts.create_token_index --concurrently   # no write lock, no transaction
"""

import argparse
import sys
import time

from sqlalchemy import text

from jmfts_core.database import get_engine

INDEX = "idx_token_embed_256_hnsw"
#: `sql/schema.sql:537`. Kept identical on purpose: a rebuild that quietly used different
#: parameters would make the shipped DDL a false description of a running appliance.
M = 16
EF_CONSTRUCTION = 64


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--concurrently",
        action="store_true",
        help="build without a write lock; slower, and leaves an INVALID index behind if it "
        "fails, which must then be dropped by hand",
    )
    ap.add_argument(
        "--maintenance-work-mem",
        default="1GB",
        help="a build that does not fit in this spills and takes far longer (default: 1GB)",
    )
    ap.add_argument(
        "--parallel-workers",
        type=int,
        default=0,
        help="0 (the default) because a parallel HNSW build asks for roughly 1 GB of shared "
        "memory, and a container gets docker's default 64 MB /dev/shm unless somebody "
        "raised it; the failure text names a shared memory segment and reads like a full "
        "disk. Raise it on bare metal, or where shm_size is set.",
    )
    args = ap.parse_args(argv)

    engine = get_engine()
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        rows = conn.execute(text("SELECT COUNT(*) FROM token_embeddings")).scalar()
        print(f"token_embeddings: {rows:,} rows", flush=True)

        conn.execute(text(f"SET maintenance_work_mem = '{args.maintenance_work_mem}'"))
        conn.execute(text(f"SET max_parallel_maintenance_workers = {args.parallel_workers}"))

        concurrently = " CONCURRENTLY" if args.concurrently else ""
        print(f"dropping {INDEX} if present", flush=True)
        conn.execute(text(f"DROP INDEX{concurrently} IF EXISTS {INDEX}"))

        print(f"building {INDEX} (m = {M}, ef_construction = {EF_CONSTRUCTION})", flush=True)
        started = time.monotonic()
        conn.execute(
            text(
                f"CREATE INDEX{concurrently} {INDEX} ON token_embeddings "
                f"USING hnsw (embed_256 halfvec_cosine_ops) "
                f"WITH (m = {M}, ef_construction = {EF_CONSTRUCTION})"
            )
        )
        print(f"  built in {time.monotonic() - started:.1f}s", flush=True)

        size = conn.execute(
            text(f"SELECT pg_size_pretty(pg_relation_size('{INDEX}'::regclass))")
        ).scalar()
        total = conn.execute(
            text("SELECT pg_size_pretty(pg_total_relation_size('token_embeddings'))")
        ).scalar()
        print(f"  {INDEX}: {size}", flush=True)
        print(f"  token_embeddings, everything included: {total}", flush=True)

    # No advice about a query-time setting is printed, and that is a statement rather than
    # an omission: `maxsim_search` sets `hnsw.iterative_scan` itself, and nothing else needs
    # setting for this index to answer. The file this replaced ended by telling its reader
    # to set `ivfflat.probes` per query, which nothing in the appliance ever did.
    return 0


if __name__ == "__main__":
    sys.exit(main())
