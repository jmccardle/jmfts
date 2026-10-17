-- Migration 025: a `summarize:tree` node carries no vector, and dies with its source.
-- ROADMAP.md known gap 11.
--
-- WHAT THE VECTOR WAS. `summarize` embeds a container's effective content onto the container
-- itself; `summarize:tree` then wrote a second row under the derived root and copied that
-- vector onto it. Measured 2026-10-16 on the stress corpus (59,242 documents): every one of
-- the 14,008 summary nodes whose source still exists holds a bit-identical copy of the
-- source's vector, 0 stale. The source is in every default result set and the summary node
-- is held out of all of them by `search_exclude_usetypes`, so the copy answered no query the
-- source did not — and it was 25.1% of `idx_documents_embed`, and 29–38% of the slots in an
-- unfiltered top-40. A vector belongs on a node when there is a retrieval reason to reach
-- that node, and there was none. The code stopped writing it in the same release.
--
-- WHAT THE ORPHANS WERE. A summary node lives under a derived root, not under its source,
-- so deleting a source never reached it: 821 nodes on the same corpus were still titled
-- "Summary of <a deleted document>", and 583 held a vector that existed nowhere else in the
-- store. That is content retained after the delete meant to remove it.
-- `DocumentRepository.delete` removes them from this release on; this removes the ones
-- already there.
--
-- NO BACKUP TABLE, and that is the point rather than an omission. The orphans are deleted
-- BECAUSE they retain deleted content; copying them into a backup table first would keep
-- exactly what this migration exists to remove. The cleared vectors need no backup either:
-- the guard below proves each one equals its source's before it is cleared, so the source
-- IS the backup (see REVERSIBILITY).
--
-- THE GUARD ABORTS RATHER THAN GUESSES. If any summary node's vector differs from its live
-- source's, something other than the copy wrote it, and clearing it would destroy a vector
-- that cannot be recomputed from here. Investigate that row first.
--
-- THE INDEX IS REBUILT IN THE SAME TRANSACTION, NOT LEFT TO VACUUM. Clearing 14k vectors
-- leaves 14k dead entries in the HNSW graph until vacuum reaches them, and
-- `docs/ANN_INDEX_HEALTH.md` measured dead entries as this index's worst recall hazard;
-- HNSW vacuum is also slow (189x IVFFlat, `ANN_INDEX_HEALTH.md` 6.3). NOT `CONCURRENTLY`,
-- for 022's reason: `REINDEX ... CONCURRENTLY` cannot run in a transaction block, and a
-- failure between it and the ledger row leaves a ledger that is wrong about the database.
-- The cost is a write lock on `documents` for the rebuild. An operator who would rather
-- take the risk than the lock can run everything but the REINDEX here, then
-- `REINDEX INDEX CONCURRENTLY idx_documents_embed;` by hand, and that is a deliberate act.
-- `max_parallel_maintenance_workers = 0` for 022's reason too: a parallel HNSW build in a
-- container with docker's default /dev/shm fails with an error that reads as a full disk.
--
-- AFTER IT: the TOAST bytes the vectors held (≈45 MB on the stress corpus) are returned only
-- by a rewrite (`VACUUM FULL documents` or pg_repack) — not needed for correctness, and
-- under 2% of that database.
--
-- REVERSIBILITY. The vectors: `UPDATE documents s SET embed = d.embed FROM documents d
-- WHERE s.produced_by = 'summarize:tree' AND d.id = (s.structured_content->>'source_node_id')::int`
-- restores every one exactly, then REINDEX. A 0.6.x process re-summarising a node writes its
-- copy again and loses nothing either way, since search excludes `summary` in both. The
-- orphans are NOT restorable, deliberately. The index is dropped with
-- `DROP INDEX idx_documents_summary_source`.

BEGIN;

-- The lookup `DocumentRepository.delete` makes on every delete: which summary nodes stand
-- for something in this subtree. Partial, because only `summarize:tree` writes the key.
CREATE INDEX IF NOT EXISTS idx_documents_summary_source ON documents
    (((structured_content ->> 'source_node_id')::int))
    WHERE produced_by = 'summarize:tree';

DO $$
DECLARE
    differs integer;
    orphans integer;
    cleared integer;
BEGIN
    SELECT count(*) INTO differs
    FROM documents s
    JOIN documents d ON d.id = (s.structured_content ->> 'source_node_id')::int
    WHERE s.produced_by = 'summarize:tree'
      AND s.embed IS NOT NULL
      AND s.embed IS DISTINCT FROM d.embed;
    IF differs > 0 THEN
        RAISE EXCEPTION '025: % summary node(s) hold a vector that differs from their '
            'source''s; it was not written by the copy this migration removes. Investigate '
            'before applying.', differs;
    END IF;

    DELETE FROM documents s
    WHERE s.produced_by = 'summarize:tree'
      AND NOT EXISTS (
          SELECT 1 FROM documents d
          WHERE d.id = (s.structured_content ->> 'source_node_id')::int
      );
    GET DIAGNOSTICS orphans = ROW_COUNT;

    UPDATE documents SET embed = NULL
    WHERE produced_by = 'summarize:tree' AND embed IS NOT NULL;
    GET DIAGNOSTICS cleared = ROW_COUNT;

    RAISE NOTICE '025: deleted % orphaned summary node(s), cleared % copied vector(s)',
        orphans, cleared;
END $$;

SET LOCAL max_parallel_maintenance_workers = 0;
REINDEX INDEX idx_documents_embed;

-- Every delta from 017 on ends with these three lines. `ON CONFLICT DO NOTHING` so a re-run
-- against a `schema.sql`-built database leaves that database's own 'schema' row standing.
INSERT INTO schema_migrations (name, applied_at, source) VALUES
    ('025_summary_tree_no_vector.sql', NOW(), 'delta')
ON CONFLICT (name) DO NOTHING;

COMMIT;
