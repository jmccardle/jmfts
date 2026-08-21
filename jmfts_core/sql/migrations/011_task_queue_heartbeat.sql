-- Migration 011: `task_queue.heartbeat_at` — the liveness signal a worker FLEET needs.
--
-- Migration 010 shipped `claimed_by` and `service_badge` against the day the worker
-- stopped being a thread inside the API process. This is that day. Workers now run as
-- separate processes, on more than one host, and the recovery mechanism 010 left behind
-- does not survive the move.
--
-- WHAT BREAKS WITHOUT THIS COLUMN. `TaskQueueRepository.requeue_stale_claims(worker_id)`
-- recovers rows by matching `claimed_by` to the id of the worker that is STARTING UP. It
-- is provably safe without any clock — a process that is starting cannot also be running
-- the task its previous incarnation claimed — and that proof is exactly what stops
-- working in a fleet:
--
--   * a pod that dies and is rescheduled gets a NEW id, so nothing ever matches the dead
--     worker's `claimed_by` and its rows are never recovered;
--   * a host that stays down never starts a worker at all, so its rows are stranded for
--     as long as the host is gone;
--   * and a stranded row is not merely idle work. `claim_next` admits only 'pending' and
--     retryable 'failed', so it is never taken again; `_unfinished_criterion` counts it as
--     unfinished, so `structuring_complete` is permanently false and the node and every
--     ancestor stay out of the retrieval indexes; and the conflict predicate treats it as
--     a live reservation, so nothing else scoped to that region can be claimed either.
--     One dead pod parks a whole subtree.
--
-- WHY A HEARTBEAT AND NOT A LEASE DURATION. The obvious repair is "requeue anything
-- claimed longer than N", and `requeue_stale_claims`' docstring already rejected it for
-- the right reason: N has to be a bound on how long a task may LEGITIMATELY run, this
-- branch has no such number, and guessing one lets a slow `probe` be re-claimed and run
-- twice concurrently. Task durations here are not bounded in any useful way — a
-- `summarize` that calls an LLM over a long node is minutes, a `probe` is milliseconds,
-- and the same task type varies by two orders of magnitude with its input.
--
-- A heartbeat removes the need for that number. The worker touches this column on a
-- fixed interval while it holds the task, so the timestamp measures WORKER LIVENESS and
-- not task duration. The expiry threshold is then a statement about how long a live
-- worker may go without reporting in — a property of the worker loop, which we control
-- and which does not vary with the input — rather than a guess about the work. A slow
-- `summarize` keeps its claim for as long as it needs, because it is still beating.
--
-- NULLABLE, and read through COALESCE with `started_at` on the reaping side. Rows claimed
-- by a worker built before this migration have no heartbeat and must still be reapable;
-- `started_at` is the conservative stand-in, since it is written in the same statement
-- that sets 'claimed'. New claims set `heartbeat_at` at claim time for the same reason —
-- a worker that dies between the claim and the first beat still leaves a timestamp.

BEGIN;

ALTER TABLE task_queue ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ;

-- The reaper's scan: rows holding a reservation, oldest beat first. Partial on the two
-- active statuses for the same reason `idx_task_queue_active` is — the reservation set is
-- expected to stay small (one row per live worker) while completed rows accumulate
-- without bound, and a full index would be almost entirely rows the reaper never reads.
CREATE INDEX IF NOT EXISTS idx_task_queue_lease
    ON task_queue(heartbeat_at)
    WHERE status IN ('claimed', 'running');

COMMIT;
