-- Migration 005: Domain-time clock for documents via a sparse `event_time` column.
--
-- Background: `documents` only had SYSTEM time — `created_at` (row inserted) and
-- `updated_at` (row last written). Neither answers "when did the thing this document
-- records actually happen?" For anything imported rather than authored in place — a
-- conversation transcript, a benchmark haystack, a backfill — those two clocks
-- collapse: every row lands within seconds of every other at ingest, so recency
-- ranking over `created_at` measures ingest order, which is noise.
--
-- This is an ASYMMETRY the rest of the schema already resolved. `triples` is
-- bi-temporal: `valid_from`/`valid_until` (domain time — when the fact held) are kept
-- distinct from `created_at`/`recorded_at` (system time — when we learned it).
-- Documents get the same separation here.
--
-- Design:
--   * `event_time` is SPARSE and NULLABLE. Documents authored in place have no domain
--     time distinct from their system time and leave it NULL.
--   * The reading contract is `COALESCE(event_time, created_at)`, so NULL falls back
--     to the system clock — no backfill needed, and existing rows keep their exact
--     current behaviour.
--   * It is deliberately NOT the same thing as `updated_at`. `updated_at` carries an
--     ORM `onupdate` and re-stamps on every write (including the embed pass, which is
--     an UPDATE), so it cannot hold a backdated value. It also means "content changed",
--     so a real edit would clobber any other signal stored there.
--   * It is deliberately NOT an access clock either. A "last retrieved" timestamp is a
--     separate axis (decay on read, per Generative-Agents) with no consumer yet; when
--     it is wanted it needs its own column, precisely because `updated_at`'s onupdate
--     semantics cannot be shared. Do not overload this column for it.
--   * `created_at` stays immutable-by-convention: it is an audit fact (when the row
--     entered the store) and backdating it would also silently reorder siblings, since
--     the CR-1 contract is `position ASC NULLS LAST, created_at ASC, id ASC`.
--
-- Safe to run multiple times (idempotent: ADD COLUMN / CREATE INDEX IF NOT EXISTS).
--
-- Run: psql $DATABASE_URL -f migrations/005_document_event_time.sql

BEGIN;

ALTER TABLE documents ADD COLUMN IF NOT EXISTS event_time TIMESTAMPTZ;

-- Recency ranking reads COALESCE(event_time, created_at); index the expression so a
-- domain-time ordering or range scan can be served from the index rather than a sort.
CREATE INDEX IF NOT EXISTS idx_documents_event_time
    ON documents (COALESCE(event_time, created_at) DESC);

COMMIT;
