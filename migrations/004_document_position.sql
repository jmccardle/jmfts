-- Migration 004: Explicit sibling ordering via a sparse `position` column (CR-1).
--
-- Background: sibling/child listings ordered by `created_at` alone, which has no
-- tiebreak — two sub-millisecond inserts under the same parent could come back in
-- an arbitrary (and unstable) order. Document sections, conversation branches, and
-- any other reading-order sequence need a deterministic, explicit order.
--
-- Design:
--   * `position` is SPARSE and NULLABLE. Most subtrees are unordered (JMFTS has
--     use cases well beyond ordered ingestion) and leave it NULL.
--   * The ordering contract everywhere siblings/children are listed is
--         ORDER BY position ASC NULLS LAST, created_at ASC, id ASC
--     so NULL positions fall straight back to the legacy created_at order — no
--     backfill needed, and existing data keeps its current order exactly.
--   * `position` is intentionally NOT unique per (parent_id, position): concurrent
--     auto-numbered inserts can tie, and the created_at/id tail resolves them. A
--     unique constraint would instead REJECT the second insert — wrong.
--   * Roots (parent_id IS NULL) never carry a position: there is no reading-order
--     relationship between root documents; they sort by id/created_at.
--
-- Safe to run multiple times (idempotent: ADD COLUMN / CREATE INDEX IF NOT EXISTS).
--
-- Run: psql $DATABASE_URL -f migrations/004_document_position.sql

BEGIN;

ALTER TABLE documents ADD COLUMN IF NOT EXISTS position INTEGER;

-- Composite index for the sibling-ordering contract. NULLS LAST matches the
-- ORDER BY so the planner can serve ordered child listings from the index.
CREATE INDEX IF NOT EXISTS idx_documents_parent_position
    ON documents (parent_id, position ASC NULLS LAST, created_at ASC, id ASC);

COMMIT;
