-- Migration 012: the `batched` status — a task parked at an external batch provider.
--
-- WHAT IT IS FOR. Commercial LLM APIs price batch submission well below per-request calls
-- in exchange for a long turnaround. Taking that price means a task must sit, not running
-- and not finished, for hours: submitted, durable at the provider, waiting. None of the
-- four existing statuses can say that. `running` says a worker is on it and invites the
-- lease to reap it; `pending` invites another worker to claim it and submit it a SECOND
-- time, which is money spent twice for one answer; `failed` consumes retry budget and, at
-- the cap, settles the node 'failed'.
--
-- `batched` IS NOT A VARIANT OF `failed`, despite looking like one from the claim query's
-- side (both are simply "not claimable"). It must not consume a retry, must not touch
-- `settled`, and must never become claimable on its own. What it shares with `failed` is
-- only that no worker is holding it.
--
-- THE STATUS SET SPLIT. Until now (claimed, running) served two unrelated questions that
-- happened to have the same answer:
--
--   "does this row hold a reservation on its node?"   the conflict predicate, and
--                                                     idx_task_queue_active
--   "is a live worker holding this, so it can be      the lease sweep, the heartbeat
--    reaped when the heartbeat goes stale?"           guard, idx_task_queue_lease
--
-- `batched` answers YES to the first and NO to the second, which is what forces them
-- apart. Yes to the first because the task will write `effective_content` to that node
-- when the batch returns, and a second `self` task writing there meanwhile is the race
-- the reservation exists to prevent. No to the second because nothing is beating for a
-- batched row and nothing should be: the work is at the provider, not in a worker.
--
-- WHICH MEANS `batched` IS INVISIBLE TO EVERY RECOVERY MECHANISM. Not claimable, not
-- reaped, not counted as a live claim. That is deliberately the zombie-row shape migration
-- 011 was written to abolish, reintroduced on purpose — so `batched_at` exists to make the
-- stall detectable. `TaskQueueRepository.stalled_batches` is the query; a batch older than
-- the provider's turnaround window plus slack is stuck, and nothing else will ever say so.
--
-- THE HANDOFF IS NOT ATOMIC AND CANNOT BE MADE SO. Submitting to the provider and writing
-- `batched` are two systems; a worker that dies between them leaves rows in `claimed` with
-- the batch already submitted and billed. The lease then requeues them and a later worker
-- submits a second batch: one answer, paid for twice, and a retry consumed. Committing a
-- caller-supplied idempotency key BEFORE the submit would turn that into a lookup, but the
-- provider in use offers no such key, so the window is accepted rather than hidden. It is
-- milliseconds wide and the failure is expensive but not corrupting — the duplicate result
-- overwrites an identical one.

BEGIN;

ALTER TABLE task_queue DROP CONSTRAINT IF EXISTS ck_task_queue_status;
ALTER TABLE task_queue ADD CONSTRAINT ck_task_queue_status
    CHECK (status IN ('pending', 'claimed', 'running', 'batched', 'completed', 'failed'));

-- The provider's id for the batch this task was submitted in. TEXT because it is an opaque
-- identifier from somebody else's system and every provider shapes it differently.
--
-- A COLUMN, NOT A KEY IN `params`. `param_fingerprint` is derived from `params` at enqueue
-- and spec 6.1 keys the re-run diff on (task_name, param_fingerprint); writing execution
-- state into `params` mid-flight would silently invalidate that key. `params` is what the
-- task was asked to do, not what happened while doing it.
ALTER TABLE task_queue ADD COLUMN IF NOT EXISTS batch_id TEXT;

-- When the row entered `batched`. The stall detector's clock — see the note above on why
-- a batched row cannot be recovered by anything that already exists.
ALTER TABLE task_queue ADD COLUMN IF NOT EXISTS batched_at TIMESTAMPTZ;

-- The poll pass: "every task in this batch", and "which batches are outstanding". Partial,
-- because a batch_id stays on the row after completion as part of the record of how the
-- answer was obtained, and those rows are not what the poller is looking for.
CREATE INDEX IF NOT EXISTS idx_task_queue_batch
    ON task_queue(batch_id, batched_at)
    WHERE status = 'batched';

-- The reservation index gains 'batched'; the lease index deliberately does NOT. See the
-- status set split above — this is that distinction, in the two indexes that serve it.
DROP INDEX IF EXISTS idx_task_queue_active;
CREATE INDEX idx_task_queue_active
    ON task_queue(scope_document_id)
    WHERE status IN ('claimed', 'running', 'batched');

COMMIT;
