-- Migration 020: find the nodes whose text is computed, without reading the whole table.
--
-- A CONTAINER is a settled node with no `content` of its own. Its text is its children's,
-- computed by `rollup_tasks.effective_text` and never stored, and its embedding is written
-- from exactly that text — so it is a first-class retrieval target that the inverted index
-- cannot find, because `SearchRepository.index_document` returns False on NULL content
-- before it looks at anything else.
--
-- `SearchRepository._container_candidates` closes that gap by scoring containers against
-- the statistics the leaves already wrote. Its shape is: take the postings that match, walk
-- UP through `documents.path` — which holds the ancestor ids outright — and keep the
-- ancestors that are containers. The last step is a membership test against a set that is
-- a minority of the table, run once per ancestor of every matching document.
--
-- MEASURED on the author's corpus (57,492 settled nodes, a 4,017,260-posting index),
-- 2026-09-07. A five-term query matches 6,028 postings across 4,671 documents, whose paths
-- name 2,432 distinct containers:
--
--     without this index   34.8 ms
--     with it              23.5 ms warm
--
-- The 11 ms is the heap fetch this index removes. `documents` is 458 MB on that corpus and
-- `content` is the widest column in it, so testing `content IS NULL` by visiting the row is
-- the expensive way to ask a question the index can answer by the row's absence.
--
-- PARTIAL, for `ix_links_derived_by`'s reason rather than `016`'s. Containers are 26,870 of
-- 57,492 nodes — a large minority, not a majority — and the query never asks the opposite
-- question, so the leaves are served by not being in the index. Indexing every id would
-- double the size and answer nothing more.
--
-- THE PREDICATE MUST MATCH THE QUERY'S EXACTLY or PostgreSQL will not use the index: both
-- are `content IS NULL AND settled = 'settled'`. `settled` is in it because retrieval is
-- partial on that column everywhere else and a container is not exempt — an in-flight node
-- must not be reachable by being somebody's ancestor.
--
-- REVERSIBILITY: purely additive, and an index at that. A 0.4.x process neither creates nor
-- consults it; dropping it makes the query slower and not wrong.

BEGIN;

CREATE INDEX IF NOT EXISTS idx_documents_container
    ON documents (id) WHERE content IS NULL AND settled = 'settled';

-- Every delta from 017 on ends with these three lines. `ON CONFLICT DO NOTHING` so a re-run
-- against a `schema.sql`-built database leaves that database's own 'schema' row standing.
INSERT INTO schema_migrations (name, applied_at, source) VALUES
    ('020_container_lookup.sql', NOW(), 'delta')
ON CONFLICT (name) DO NOTHING;

COMMIT;
