-- Migration 007: per-document BM25 postings index.
--
-- Background: search_term_postings had only PRIMARY KEY (index_id, term,
-- document_id) and idx_term_postings_lookup (index_id, term). Neither can serve a
-- lookup keyed by (index_id, document_id), so three hot operations degraded to a
-- scan of all of an index's postings:
--   * index_document()'s per-document "read old terms" SELECT and "clear old
--     postings" DELETE (incremental re-index of one document into a populated
--     index — the write path for agent memory).
--   * The reverse FK check when deleting a search_index_entries row: the FK
--     search_term_postings (index_id, document_id) -> search_index_entries had no
--     index on the *referencing* columns, so each parent-row delete seq-scanned the
--     whole postings table. Dropping/refreshing a large index was O(entries × postings).
--
-- This index turns all three into index scans. It does NOT speed up a full rebuild
-- via SearchRepository.refresh_index — that path was made set-based separately and
-- does no per-document SELECT/DELETE — and it adds one btree to maintain on inserts,
-- so a rebuild is marginally slower with it; the win is on incremental writes and
-- deletes.
--
-- Safe to run multiple times (CREATE INDEX IF NOT EXISTS). On a large live index,
-- consider running the CREATE INDEX CONCURRENTLY variant (outside a transaction)
-- instead to avoid holding a write lock for the build.
--
-- Run: psql $DATABASE_URL -f migrations/007_term_postings_doc_index.sql

BEGIN;

CREATE INDEX IF NOT EXISTS idx_term_postings_doc
    ON search_term_postings (index_id, document_id);

COMMIT;
