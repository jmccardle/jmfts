-- Migration 008: Document ingest lifecycle via a `settled` column, and partial
-- retrieval indexes keyed on it.
--
-- Background: a document being shuffled between pipeline stages and a document that is
-- finished, embedded, searchable and referenced by other systems are not the same kind
-- of record. Today JMFTS treats them identically — every row is equally indexed and
-- equally searchable from the moment it is written. That is tolerable only because
-- ingestion is one synchronous HTTP call: nothing can observe the half-built tree
-- because nothing else runs while it is being built. The moment ingestion becomes a set
-- of queued tasks, a half-written subtree is visible to search, to the graph, and to
-- whatever downstream system is polling — and a chunk that is written, embedded,
-- superseded and rebuilt churns the HNSW graph with an insert-then-delete every time.
--
-- Design:
--   * `settled` is TEXT NOT NULL DEFAULT 'settled' with a CHECK over exactly three
--     values: 'in_flight', 'settled', 'failed'.
--       - in_flight: the node exists; tasks may be pending for it or for its subtree,
--         so its content and its children may still change.
--       - settled:   its own work is done AND every child is settled. It is recursive;
--         a node with no children settles on its own work alone.
--       - failed:    a task for this node failed with a permanent error and no retry is
--         scheduled. Not optional — without it, a permanently dead node looks exactly
--         like one still in progress, and a sweeper that settles "nodes with no pending
--         tasks" would eventually publish it.
--   * TEXT + CHECK, NOT a Postgres ENUM. Two reasons. (a) Every migration in this repo
--     runs inside BEGIN/COMMIT and `ALTER TYPE ... ADD VALUE` cannot run in a
--     transaction block, so an enum makes a fourth state disproportionately expensive.
--     (b) Portability — the same argument that keeps `usetype` an open string. The
--     difference is that `usetype` is deliberately OPEN (applications define their own)
--     while the lifecycle is a CLOSED set the scheduler reasons over, so it is
--     constrained here rather than left to convention. This follows the existing
--     `access_grants.level VARCHAR(10) CHECK (level IN ('read','write'))` precedent.
--   * DEFAULT 'settled', and the backfill that ADD COLUMN ... DEFAULT performs is
--     therefore 'settled' for every existing row. This is the correct value, not merely
--     the convenient one: every row that predates this migration was written by the
--     synchronous pipeline, which had already finished with it before returning. The
--     alternative — defaulting to 'in_flight' — would declare the entire existing
--     corpus unfinished and, combined with the partial indexes below, would empty
--     vector and full-text search on the live appliance in one statement.
--
-- THE INDEXES, and the one place this deliberately departs from the spec:
--   * idx_documents_embed (HNSW) and idx_documents_content_fts (GIN) BECOME PARTIAL
--     on `WHERE settled = 'settled'`. Each has exactly one consumer —
--     SearchRepository.vector_search and SearchRepository.fulltext_search — and both
--     now carry the matching `settled = 'settled'` predicate, so the planner can prove
--     the index predicate. Retrieval is precisely the surface that must not see a node
--     whose structure is about to be rebuilt, so index membership and retrieval
--     eligibility are the same question here.
--   * idx_documents_path STAYS NON-PARTIAL. The spec (INGEST_SPEC.md Part 2.2) proposes
--     making it partial too; this migration does not, and the reason is that the
--     majority of `path @>` consumers are the machinery responsible for MANAGING
--     in-flight nodes and therefore cannot filter them out:
--       - DocumentRepository.get_children(depth=-1)   (the whole-subtree branch)
--       - DocumentRepository.get_subtree(include_in_flight=True)  (ingestion's own walk)
--       - graph_analysis._candidate_doc_query and the tree gather beneath it
--       - fact_extraction's descendant sweep
--       - subtree RBAC: jmfts_core/access.py `_within_any` / `_within_sql` build
--         `path @> jsonb_build_array(acr_id)` to answer "is D within ACR R". That
--         predicate expresses WHO MAY SEE WHAT and must never be narrowed by lifecycle
--         — a freshly uploaded, still-in-flight file is exactly when protection
--         matters most, and `readable_id_subset` returning a smaller set would hide
--         legitimate graph edges. Migration 006's header states outright that the ACL
--         model "rides the pre-existing idx_documents_path GIN index"; making that
--         index partial would quietly falsify that line.
--     A partial path index would be unprovable for every one of those and would turn
--     each into a sequential scan over `documents` — a silent full-table scan is a
--     regression, not a no-op. The two alternatives were considered and rejected:
--     implementing it as specified and accepting degraded ACL/tree resolution, and
--     keeping a second non-partial copy under a distinct name (which pays double GIN
--     write amplification on exactly the re-parenting workload the partial index was
--     meant to protect, in exchange for an index the full one already serves).
--     The benefit given up is GIN churn during ingest re-parenting; the cost avoided is
--     a seq scan on every ACL, tree and graph query. Revisit if profiling ever says
--     otherwise.
--
-- NOT IDEMPOTENT BY `IF NOT EXISTS` ALONE — read this before re-running. `CREATE INDEX
-- IF NOT EXISTS idx_documents_embed ... WHERE settled = 'settled'` is a SILENT NO-OP
-- when a non-partial index of that name already exists: Postgres matches on name, not
-- on definition. Converting an existing index to partial therefore requires an explicit
-- DROP ... CREATE pair, which is what this file does. That pair IS re-runnable, at the
-- cost of rebuilding both indexes on every run — for a large corpus the HNSW rebuild is
-- expensive and holds ACCESS EXCLUSIVE on `documents` for the whole transaction. Run it
-- once, during a window where that is acceptable. (CREATE INDEX CONCURRENTLY would
-- avoid the lock but cannot run inside a transaction block, and every migration here is
-- transactional by convention.)
--
-- Safe to run multiple times (idempotent: ADD COLUMN IF NOT EXISTS; DROP INDEX IF
-- EXISTS + CREATE INDEX for the two indexes whose DEFINITION changes).
--
-- Run: psql $DATABASE_URL -f migrations/008_document_settled_lifecycle.sql

BEGIN;

-- The column. ADD COLUMN ... NOT NULL DEFAULT backfills every existing row to
-- 'settled' in one statement (Postgres 11+ does this without a table rewrite).
ALTER TABLE documents ADD COLUMN IF NOT EXISTS settled TEXT NOT NULL DEFAULT 'settled';

-- The closed-set constraint, added separately so a re-run does not fail on a duplicate
-- constraint name. Validated against existing rows, which are all 'settled' by the
-- backfill above.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_documents_settled'
    ) THEN
        ALTER TABLE documents ADD CONSTRAINT ck_documents_settled
            CHECK (settled IN ('in_flight', 'settled', 'failed'));
    END IF;
END
$$;

-- Vector search (HNSW): only settled rows enter the graph.
DROP INDEX IF EXISTS idx_documents_embed;
CREATE INDEX idx_documents_embed ON documents
    USING hnsw (embed vector_cosine_ops)
    WITH (m = 16, ef_construction = 64)
    WHERE settled = 'settled';

-- Full-text search (GIN): only settled rows are matchable.
-- NOTE the indexed expression is COALESCE(title,'') || ' ' || COALESCE(content,''),
-- NOT the bare `content` the spec's illustrative DDL shows. It must stay byte-identical
-- to the expression SearchRepository.fulltext_search builds, or the index cannot be
-- matched to the query at all.
DROP INDEX IF EXISTS idx_documents_content_fts;
CREATE INDEX idx_documents_content_fts ON documents
    USING GIN (to_tsvector('english', COALESCE(title, '') || ' ' || COALESCE(content, '')))
    WHERE settled = 'settled';

-- idx_documents_path is intentionally left alone. See the header.

COMMIT;
