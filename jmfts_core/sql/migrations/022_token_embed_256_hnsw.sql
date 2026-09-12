-- Migration 022: the MaxSim index stops being fitted to an empty table.
--
-- `docs/ANN_INDEX_HEALTH.md` Part 5 and `docs/STRESS_CORPUS.md` 6.2. The shipped
-- `idx_token_embed_256_ivf` returned 0.7533 of the true top-10 on the reference corpus,
-- with 15 of 30 queries degraded, and `tests/test_maxsim_recall.py` is the failing test
-- that Part 0's rule requires before any of this is a defect rather than an argument.
--
-- WHY THE INDEX TYPE AND NOT `probes`, `lists`, OR A REBUILD. 5.6 left a four-way choice,
-- and 5.7 measured the one column its table did not have. An IVFFlat index fixes its
-- k-means centroids at CREATE INDEX. `schema.sql` created this index before any row existed and the
-- file's first INSERT comes hundreds of lines later, so the centroids were fitted to zero rows; `git grep
-- REINDEX -- jmfts_core` returns nothing, so they were never recomputed. Measured at the
-- appliance's own rows-per-list ratio, built-empty reads recall 0.603 at `probes = 1`
-- against 0.998 built-on-data (5.7). An HNSW graph is built BY INSERTION, so the two build
-- orders produce the same index: 0.987 at `ef_search = 40` and 0.998 at 100, against 0.998
-- built on data.
--
-- That is what separates this from the other three options rather than any recall number.
-- Raising `probes` and rebuilding `lists` both clear the threshold too, and both leave a
-- rebuild that somebody has to remember to run — and 5.6's sub-question 2 is that
-- "after ingest" has no definition while ingest is a queue that never ends. HNSW needs
-- nobody to remember anything.
--
-- WHAT IT COSTS, on the real corpus at 1,370,494 rows (`STRESS_CORPUS.md` 6.2): 2.7x build
-- time (55.9 s against 20.6 s) and 46% more disk (1050 MB against 720 MB, on a table
-- already 1672 MB). Query latency is UNCHANGED — 0.65 ms in both arms, which is the
-- measurement that made this affordable at all. Budget the extra 330 MB before applying.
--
-- AND THE COST THAT IS NOT A NUMBER. `token_embeddings` has no `settled` column, so this
-- index cannot be partial the way `idx_documents_embed` is, and it cannot keep rewritten
-- rows out of the graph. `repositories/document.py:1069` deletes every token row for a
-- document and rewrites them on each re-embed: 113,313 dead against 1,370,494 live on the
-- reference corpus (6.3). Dead entries are what Parts 1-2 are about, and 1.7 measures
-- pgvector's default scan mode collapsing to recall 0.100 under them. The mitigation is
-- `hnsw.iterative_scan = strict_order`, which `repositories/search.py` now sets on the
-- MaxSim path as well as the document-vector path; 6.2 measured that exact combination on
-- this exact column at 0.9667 and 0.59 ms. **Applying this migration without the matching
-- `jmfts_core` is the one order that is worse than either end state**: an HNSW index read
-- at pgvector's default scan mode, on a column that accumulates dead entries by design.
-- HNSW also vacuums 189x slower than IVFFlat on real vectors (6.3), and that cost arrives
-- in full after a bulk delete rather than incrementally.
--
-- THE INDEX IS RENAMED, from `idx_token_embed_256_ivf` to `idx_token_embed_256_hnsw`. The
-- old name would be a false statement about the object it names, and `tests/maxsim_corpus.py`
-- asserts the planner reaches the index BY NAME — a rename that silently kept the old
-- string would leave that assertion passing against whichever index happened to answer.
--
-- NOT `CONCURRENTLY`, and the trade is deliberate. `CREATE INDEX CONCURRENTLY` cannot run
-- inside a transaction block, so a concurrent build would commit the DDL and the ledger row
-- separately and a failure between them leaves a database whose ledger is wrong about it.
-- The cost of the choice is a write lock on `token_embeddings` for the length of the build
-- — minutes at 1.37M rows, single-threaded (see below). An operator who would rather take
-- the risk than the lock can run the two CREATE/DROP statements by hand with
-- `CONCURRENTLY` and then INSERT the ledger row, and that is a deliberate act, which is the
-- point.
--
-- `max_parallel_maintenance_workers = 0` BECAUSE THE ERROR IT PREVENTS IS MISLEADING. A
-- parallel HNSW build asks for roughly 1 GB of shared memory, and a Postgres in a container
-- gets docker's default 64 MB `/dev/shm` unless somebody raised it. The failure is
-- `could not resize shared memory segment ... to 1070632384 bytes`, which reads as a full
-- disk and is not one; measured on `pgvector/pgvector:pg16` with 576 GB free on the host.
-- Single-threaded is slower and always finishes. An operator on bare metal, or on a
-- container with `shm_size` raised, can drop this line and get 6.2's 55.9 s.
--
-- REVERSIBILITY: exact, and cheap in the sense that matters. `DROP INDEX
-- idx_token_embed_256_hnsw` then the CREATE from `schema.sql` as it stood before this
-- migration rebuilds the IVFFlat index — and it rebuilds it ON THE DATA, which is a better
-- index than the one this replaces. A 0.4.x process reading a migrated database works
-- unchanged: nothing in `jmfts_core` names either index, the ANN statement is
-- `ORDER BY embed_256 <=> ...` and the planner picks whatever is there. What such a process
-- does NOT do is set the scan mode, which is the hazard named above.

BEGIN;

-- See "NOT `CONCURRENTLY`" above: 1.37M rows is minutes of write lock on this table.
SET LOCAL maintenance_work_mem = '1GB';
SET LOCAL max_parallel_maintenance_workers = 0;

-- IF EXISTS so that a database already carrying the HNSW index — one built from a current
-- `schema.sql` — is not stopped here on its way to the ledger row below. This is not a
-- fallback around a failure: the index's absence is the correct state for exactly that
-- database, and the CREATE below is `IF NOT EXISTS` for the same reason.
DROP INDEX IF EXISTS idx_token_embed_256_ivf;

CREATE INDEX IF NOT EXISTS idx_token_embed_256_hnsw ON token_embeddings
    USING hnsw (embed_256 halfvec_cosine_ops) WITH (m = 16, ef_construction = 64);

-- Every delta from 017 on ends with these three lines. `ON CONFLICT DO NOTHING` so a re-run
-- against a `schema.sql`-built database leaves that database's own 'schema' row standing.
INSERT INTO schema_migrations (name, applied_at, source) VALUES
    ('022_token_embed_256_hnsw.sql', NOW(), 'delta')
ON CONFLICT (name) DO NOTHING;

COMMIT;
