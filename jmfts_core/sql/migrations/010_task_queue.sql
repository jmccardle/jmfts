-- Migration 010: `task_queue` — the ingest scheduler's rows. INGEST_SPEC.md Part 5.
--
-- NOTE ON THE NUMBER: the phasing plan called this 008, then 009; both were taken by the
-- lifecycle column and the blob table as the earlier steps landed. This is 010 because
-- 010 is what was free.
--
-- Background: today `execute_pipeline` runs every stage inside one HTTP request and
-- there is no row anywhere that says a stage is pending (spec 1.2). This table is that
-- row. It is ported from triskelion's `vdo_core/schema.sql:164-181` plus its retry
-- migration, with the column set spec 1.4 names — status, priority, dependencies,
-- retry_count/max_retries, error_type, retry_after, claimed_by, service_badge — and two
-- columns triskelion has no equivalent of.
--
-- WHY `claimed_by` AND `service_badge` SURVIVE A PORT TO AN IN-PROCESS WORKER. Spec 5.8
-- puts the worker in a thread inside the API process, so there is no fleet to address
-- and neither column has a job today. They stay because they cost one nullable VARCHAR
-- each, because they are exactly what a later split back into processes needs, and
-- because Part 7's review tasks need `service_badge` immediately — a task routed at a
-- human rather than at the worker loop is a badge, not a new mechanism.
--
-- THE TWO JMFTS COLUMNS — `scope_document_id` and `write_mode` (spec 5.3). Together they
-- say what region of the tree a task RESERVES while it runs:
--
--     self      this node's own columns and structured_content
--     children  this node, plus creating and moving nodes that have no children
--     subtree   anything below, including descendants' `path`
--
-- and the claim query in jmfts_core/repositories/task_queue.py refuses to hand out a
-- task whose region overlaps one already claimed or running. Triskelion had only
-- `target_document_id`, "the node this task is about", with no notion of a reservation.
-- That column is deliberately NOT ported rather than kept alongside: the node a JMFTS
-- task is about IS the node it is scoped to, and two columns that could disagree would
-- leave nothing to say which one the conflict rules meant.
--
-- `params` / `param_fingerprint` are also new, and are not optional decoration. The
-- worker cannot run a task without its parameters; the attempt record the task writes
-- when it finishes (spec 3.4) has to log them; and spec 6.1 keys the re-run diff on
-- `(task_name, param_fingerprint)`, so the fingerprint has to be readable from the row
-- rather than re-derived later from params an older version of the code wrote.
--
-- THE RETRY POLICY IS NOT IN THIS FILE. Triskelion put it in a plpgsql `mark_task_failed`
-- function with a Python wrapper that did nothing but `SELECT mark_task_failed(...)`,
-- which split one decision across two languages; its backoff also SELECTed `max_retries`
-- into a variable it never used, and the cap was really enforced somewhere else
-- entirely. Here the policy lives in TaskQueueRepository.fail(), in Python, in one
-- place, and this file is pure DDL.
--
-- THERE IS ALSO NO `retry_failed_tasks()` SWEEP. Triskelion had a function that flipped
-- `failed` rows back to `pending` on a timer that nothing appeared to run. Retry is
-- folded into claimability instead — the claim query treats a `failed` row that is
-- retryable and under its cap as claimable once `retry_after` has passed, and increments
-- `retry_count` as it takes it. One less background timer, and no window in which a task
-- is due for retry and nothing has noticed.
--
-- Idempotent throughout (CREATE TABLE / CREATE INDEX IF NOT EXISTS). No index definition
-- from an earlier migration changes here, so IF NOT EXISTS cannot silently no-op over a
-- differing prior version.
--
-- Run: psql $DATABASE_URL -f migrations/010_task_queue.sql

BEGIN;

CREATE TABLE IF NOT EXISTS task_queue (
    id SERIAL PRIMARY KEY,

    -- Part 4's task list: 'probe', 'extract:text', 'structure:declared', 'summarize', …
    task_type VARCHAR(50) NOT NULL,

    -- The node this task is scoped to: what it is about AND what it reserves.
    -- ON DELETE CASCADE — a task for a document that no longer exists is not work.
    -- Triskelion's bare REFERENCES made deleting an in-flight document raise a foreign
    -- key violation naming a table the caller had never heard of.
    scope_document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,

    -- Spec 5.3. Checked at claim time; `children` is additionally enforced inside the
    -- write path by DocumentRepository.reparent's childless guard.
    write_mode VARCHAR(10) NOT NULL,

    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    priority INTEGER NOT NULL DEFAULT 0,

    -- Ordering WITHIN one node (spec 5.5), where the set is known at enqueue time.
    -- Cross-level ordering is deliberately absent: a planned rollup list goes stale the
    -- moment a child is added, so the walk in jmfts_core/settling.py re-reads the tree.
    dependencies INTEGER[],

    -- What the task should run with, and spec 6.1's diff key over it.
    params JSONB NOT NULL DEFAULT '{}'::jsonb,
    param_fingerprint TEXT NOT NULL,

    -- Spec 5.8: who should run this, and who did.
    service_badge VARCHAR(50),
    claimed_by VARCHAR(100),

    -- Server-side clocks throughout: `retry_after` is compared against the server's
    -- NOW() in the claim query, so a client clock here would make backoff depend on
    -- whichever machine happened to write the row.
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,

    error TEXT,
    error_type VARCHAR(20),

    -- Derived from error_type on failure, kept as its own column so the claim query and
    -- the retry index can test it without re-encoding the policy in SQL.
    retryable BOOLEAN NOT NULL DEFAULT TRUE,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    retry_after TIMESTAMPTZ,

    CONSTRAINT ck_task_queue_status
        CHECK (status IN ('pending', 'claimed', 'running', 'completed', 'failed')),
    CONSTRAINT ck_task_queue_write_mode
        CHECK (write_mode IN ('self', 'children', 'subtree')),
    CONSTRAINT ck_task_queue_error_type
        CHECK (error_type IS NULL OR error_type IN
               ('retryable', 'permanent', 'timeout', 'dependency'))
);

-- Spec 5.2's "structuring complete" query: are there tasks scoped to this node that are
-- pending or running? Asked once per node per settle walk, so it is the hot one.
CREATE INDEX IF NOT EXISTS idx_task_queue_scope ON task_queue(scope_document_id, status);

-- The claim query's candidate scan and its ORDER BY. Partial because 'completed' rows
-- accumulate without bound until they are purged and are never claim candidates.
CREATE INDEX IF NOT EXISTS idx_task_queue_claimable
    ON task_queue(priority DESC, created_at ASC, id ASC)
    WHERE status IN ('pending', 'failed');

-- The conflict predicate's inner NOT EXISTS: which tasks currently hold a reservation.
-- Expected to be tiny (one in-process worker), so a partial index keeps it that way.
CREATE INDEX IF NOT EXISTS idx_task_queue_active
    ON task_queue(scope_document_id)
    WHERE status IN ('claimed', 'running');

-- Spec 6.3 reads the DAG BACKWARD — "tasks whose dependencies array contains the re-run
-- task's id" is what is now stale — and that is an array containment query. GIN.
CREATE INDEX IF NOT EXISTS idx_task_queue_dependencies
    ON task_queue USING GIN (dependencies);

-- Part 7 routing: a badged worker (or a human review queue) asking for its own work.
CREATE INDEX IF NOT EXISTS idx_task_queue_service
    ON task_queue(service_badge, status, priority DESC);

COMMIT;
