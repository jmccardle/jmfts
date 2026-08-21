-- Migration 003: Purge ghost predicates (predicates with no associated triples).
--
-- Background: Bulk fact extraction runs created predicate rows for every
-- extracted relation type, including many that never produced any triples
-- (extraction failures, rejected triples, non-English artifacts, etc.).
-- These ghost predicates pollute /triples/predicates and make schema
-- inspection useless.
--
-- This migration is safe to run multiple times (idempotent via the DELETE
-- itself — subsequent runs delete 0 rows).
--
-- Run: psql $DATABASE_URL -f migrations/003_purge_ghost_predicates.sql

BEGIN;

-- Report how many ghost predicates will be deleted
DO $$
DECLARE
    ghost_count INTEGER;
BEGIN
    SELECT COUNT(*)
    INTO ghost_count
    FROM predicates p
    WHERE NOT EXISTS (
        SELECT 1 FROM triples t WHERE t.predicate_id = p.id
    );
    RAISE NOTICE 'Purging % ghost predicates (predicates with 0 triples)', ghost_count;
END;
$$;

-- Delete ghost predicates (no triples reference them, so CASCADE is a no-op)
DELETE FROM predicates
WHERE NOT EXISTS (
    SELECT 1 FROM triples t WHERE t.predicate_id = predicates.id
);

-- Report the remaining predicate count
DO $$
DECLARE
    remaining INTEGER;
BEGIN
    SELECT COUNT(*) INTO remaining FROM predicates;
    RAISE NOTICE 'Remaining predicates after purge: %', remaining;
END;
$$;

COMMIT;
