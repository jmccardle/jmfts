-- Migration: Add temporal metadata to triples table
-- Ref: Zep/Graphiti (arXiv 2501.13956), Hindsight (arXiv 2512.12818)

-- Step 1: Create fact_type enum
DO $$ BEGIN
    CREATE TYPE fact_type AS ENUM ('atemporal', 'static', 'dynamic');
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

-- Step 2: Add temporal validity columns
ALTER TABLE triples
    ADD COLUMN IF NOT EXISTS valid_from TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS valid_until TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS recorded_at TIMESTAMPTZ DEFAULT NOW();

-- Step 3: Add fact classification
ALTER TABLE triples
    ADD COLUMN IF NOT EXISTS fact_type fact_type NOT NULL DEFAULT 'atemporal';

-- Step 4: Add edge invalidation columns
ALTER TABLE triples
    ADD COLUMN IF NOT EXISTS invalidated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS invalidated_by INTEGER REFERENCES triples(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS invalidation_reason TEXT;

-- Step 5: Backfill recorded_at for existing rows
UPDATE triples SET recorded_at = created_at WHERE recorded_at IS NULL;

-- Step 6: Add indexes for temporal queries
CREATE INDEX IF NOT EXISTS ix_triples_valid_range ON triples(valid_from, valid_until);
CREATE INDEX IF NOT EXISTS ix_triples_fact_type ON triples(fact_type);
CREATE INDEX IF NOT EXISTS ix_triples_invalidated ON triples(invalidated_at) WHERE invalidated_at IS NOT NULL;
