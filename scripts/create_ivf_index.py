#!/usr/bin/env python3
"""
Create IVF-Flat index on token embeddings.

IVF-Flat (Inverted File with Flat quantization) is smaller than HNSW
and often faster for filtered queries. Good for our tier-based filtering.

Usage:
    python scripts/create_ivf_index.py
"""

import sys
import time

sys.path.insert(0, "/home/john/Development/jmfts")

from jmfts_core.database import get_engine
from sqlalchemy import text


def main():
    print("Creating IVF-Flat indexes on token embeddings", flush=True)
    print("=" * 60, flush=True)

    engine = get_engine()

    # IVF-Flat parameters:
    # - lists: number of clusters (sqrt(n) to 4*sqrt(n) is typical)
    #   With ~500k tokens, sqrt(500k) ≈ 707, so 512-2048 is reasonable
    # - probes: how many clusters to search (higher = more accurate, slower)
    #   Default is 1, we'll set 10 for good accuracy
    n_lists = 1024  # Number of Voronoi cells/clusters

    with engine.connect().execution_options(isolation_level='AUTOCOMMIT') as conn:
        # Check current token count
        result = conn.execute(text("SELECT COUNT(*) FROM token_embeddings")).fetchone()
        n_tokens = result[0]
        print(f"Token embeddings: {n_tokens:,}", flush=True)
        print(f"Using {n_lists} IVF lists (clusters)", flush=True)

        # Increase maintenance_work_mem for index creation
        print("\nSetting maintenance_work_mem = 512MB...", flush=True)
        conn.execute(text("SET maintenance_work_mem = '512MB'"))

        # Drop existing indexes if any
        print("\nDropping any existing vector indexes...", flush=True)
        conn.execute(text("DROP INDEX IF EXISTS idx_token_embed_256_ivf"))
        conn.execute(text("DROP INDEX IF EXISTS idx_token_embed_256"))

        # Create IVF-Flat index on embed_256 (only dimension we use now)
        print("\nCreating IVF-Flat index on embed_256...", flush=True)
        start = time.time()
        conn.execute(text(f"""
            CREATE INDEX idx_token_embed_256_ivf
            ON token_embeddings
            USING ivfflat (embed_256 halfvec_cosine_ops)
            WITH (lists = {n_lists})
        """))
        elapsed = time.time() - start
        print(f"  Created in {elapsed:.1f}s", flush=True)

        # Set default probes for queries
        print("\nSetting default ivfflat.probes = 10...", flush=True)
        conn.execute(text("SET ivfflat.probes = 10"))

        # Check index sizes
        result = conn.execute(text("""
            SELECT indexrelname, pg_size_pretty(pg_relation_size(indexrelid)) as size
            FROM pg_stat_user_indexes
            WHERE relname = 'token_embeddings' AND indexrelname LIKE '%ivf%'
            ORDER BY indexrelname
        """)).fetchall()

        print("\nIndex sizes:", flush=True)
        for name, size in result:
            print(f"  {name}: {size}", flush=True)

        # Total table size
        result = conn.execute(text("""
            SELECT pg_size_pretty(pg_total_relation_size('token_embeddings'))
        """)).fetchone()
        print(f"\nTotal token_embeddings size: {result[0]}", flush=True)

        print("\nDone!", flush=True)
        print("\nNote: Set 'ivfflat.probes' at query time for accuracy/speed tradeoff", flush=True)
        print("  Higher probes = more accurate, slower (default 1, we use 10)", flush=True)


if __name__ == "__main__":
    main()
