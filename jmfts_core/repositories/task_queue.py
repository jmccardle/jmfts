"""TaskQueueRepository — enqueue, claim, finish. ``INGEST_SPEC.md`` Part 5.

Ported from triskelion's ``vdo_core/repositories/tasks.py``, with the session-per-method
fallback removed (JMFTS repositories take the session in ``__init__`` and hold it) and
three things it did not have:

* **the write-mode conflict predicate** (spec 5.3) in the claim query;
* **retry folded into claimability** rather than run as a separate sweep;
* **the attempt record** written at completion (spec 5.6), so the durable log on the node
  and the ephemeral queue row are updated by the same call and cannot diverge.

WHAT SERIALISES A CLAIM. ``FOR UPDATE SKIP LOCKED`` guarantees that two workers never
take the *same row*. It does not guarantee that two workers never take two *conflicting*
rows: each evaluates the conflict predicate before either has written its own ``claimed``
status, so both see a clear field and both proceed. The predicate is only meaningful if
the claim is serialised, so ``claim_next`` takes a transaction-scoped advisory lock for
the length of the claim. That makes claims sequential, which is exactly right for spec
5.8's single in-process worker and costs one indexed query's worth of contention if that
ever becomes several. The lock is transaction-scoped, so it is released by the commit at
the end of the claim — the claim MUST therefore be its own short transaction, not the
same one that runs the task.

WHY THE TIMESTAMPS ARE ``clock_timestamp()``. PostgreSQL's ``now()`` — which is what
``func.now()`` emits and what an unqualified ``NOW()`` means — returns the TRANSACTION's
start time, not the current instant. The worker claims in one transaction and completes
in another (5.8), and the completing transaction begins when the handler issues its first
statement, so ``completed_at - started_at`` computed from ``now()`` measures the gap
between two transaction starts and not the work between them. Every task in a real
ingestion recorded a duration of a few milliseconds, including LLM calls that took
half a minute. ``clock_timestamp()`` is the wall clock at the moment of the statement,
which is what :meth:`complete`'s "the attempt's timing must be measured" always meant.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from jmfts_client.contracts.attempt import TERMINAL_STATUSES, AttemptRecord, param_fingerprint
from jmfts_core.models.document import Document, SETTLED_FAILED, SETTLED_IN_FLIGHT
from jmfts_core.models.task_queue import (
    ADVISORY_TASK_TYPES,
    TASK_BATCHED,
    TASK_CLAIMED,
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_PENDING,
    TASK_REAPABLE_STATUSES,
    TASK_RESERVING_STATUSES,
    TASK_RUNNING,
    WRITE_MODES,
    TaskQueue,
)
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.task_errors import RETRYABLE_ERROR_TYPES, ErrorType
from jmfts_core.task_routing import BADGE_FROM_POLICY, BadgeRequest, resolve_badge

#: Key for the transaction-scoped advisory lock that serialises claims. Arbitrary but
#: fixed; two-int form so it cannot collide with a single-bigint advisory lock taken
#: elsewhere. See the module docstring for why the claim needs it at all.
_CLAIM_LOCK_KEY = (0x4A4D_4653, 1)  # 'JMFS', slot 1

#: Key for the lease reaper's lock. Same namespace, next slot: every worker runs the
#: reaper on its own timer, and this is what makes exactly one of them do it per round.
#: Distinct from the claim lock on purpose — sharing one would make a reaping pass block
#: every claim in the fleet for its duration.
_REAPER_LOCK_KEY = (0x4A4D_4653, 2)  # 'JMFS', slot 2

#: First key of the per-batch poll lock; the second is ``hashtext(batch_id)``. A separate
#: namespace slot rather than a third fixed key, because there is one lock PER BATCH and
#: they must not serialise against each other — two workers polling two different batches
#: is the case this is built for.
_BATCH_LOCK_NAMESPACE = 0x4A4D_4654  # 'JMFT', the batch-id namespace

#: Base of the exponential backoff, in seconds. ``2 ** retry_count`` minutes — 1, 2, 4 —
#: which with the default ``max_retries = 3`` is where it stops.
_BACKOFF_BASE_SECONDS = 60

#: Multiplicative jitter range. Triskelion had none, so every task that failed in one
#: burst (a model going down takes all of them at once) retried in the same second and
#: knocked the same service over again. The spread is small because the point is to
#: decorrelate, not to delay.
_BACKOFF_JITTER = (1.0, 1.25)


class TaskQueueRepository:
    """Data access for ``task_queue``. One session, held for the repository's life."""

    def __init__(self, session: Session):
        self.session = session

    # =========================================================================
    # Enqueue
    # =========================================================================

    def enqueue(
        self,
        task_type: str,
        scope_document_id: int,
        write_mode: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        dependencies: Optional[Sequence[int]] = None,
        priority: int = 0,
        service_badge: BadgeRequest = BADGE_FROM_POLICY,
        max_retries: int = 3,
    ) -> TaskQueue:
        """Create a pending task and un-settle the node it is scoped to.

        ``service_badge`` defaults to the routing policy for this task type
        (:func:`jmfts_core.task_routing.badge_for`), which is empty unless a deployment
        configured one. Passing ``None`` explicitly is different from omitting it: it
        means "leave this task un-badged whatever the policy says", which is what Part 7's
        review tasks and the tests need. The sentinel is what keeps those two apart — with
        a plain ``None`` default there would be no way to say "un-badged, deliberately".

        The un-settling is not a side effect, it is the definition: spec 2.1 says a
        settled node has no task pending for it, so a node that gains one is by
        construction no longer settled. Doing it here rather than at the call site means
        no caller can enqueue work against a node that search still considers finished.

        ``dependencies`` are deduplicated and sorted. The claim gate counts matching
        completed rows against ``cardinality(dependencies)``, which is only an exact
        test if the array holds no duplicates.

        A ``pending`` attempt record is written to the node at the same time. Spec 5.7
        requires it — ``POST /ingest/file`` returns "the current attempt log, which at
        that moment is one pending entry" — and it is done here rather than at that one
        call site so that every queued task is visible in the log the client polls, from
        the instant it is queued. :meth:`complete` / :meth:`fail` replace that entry with
        the outcome of the same attempt rather than appending beside it.
        """
        if write_mode not in WRITE_MODES:
            raise ValueError(f"unknown write_mode {write_mode!r}; expected one of {WRITE_MODES}")

        doc = self.session.get(Document, scope_document_id)
        if doc is None:
            raise ValueError(
                f"cannot enqueue {task_type!r}: document {scope_document_id} does not exist"
            )

        resolved_badge = resolve_badge(task_type, service_badge)

        resolved_params = dict(params or {})
        task = TaskQueue(
            task_type=task_type,
            scope_document_id=scope_document_id,
            write_mode=write_mode,
            status=TASK_PENDING,
            priority=priority,
            dependencies=sorted(set(dependencies)) if dependencies else None,
            params=resolved_params,
            param_fingerprint=param_fingerprint(resolved_params),
            service_badge=resolved_badge,
            max_retries=max_retries,
        )
        self.session.add(task)

        docs = DocumentRepository(self.session)
        if doc.settled != SETTLED_IN_FLIGHT:
            # The scope node goes in flight from ANY prior state — including `failed`,
            # which is how spec 6.3's correction revives a node whose ingestion died.
            #
            # THE SCOPE NODE AND NOTHING ABOVE IT. Queueing work here is not work for the
            # parent, so the parent's own `settled` is not this statement's business; see
            # the note at the top of `repositories/document.py` for the invariant that
            # gives up and the failure containment it buys.
            doc.settled = SETTLED_IN_FLIGHT

        self.session.flush()

        docs.upsert_attempt(
            doc,
            AttemptRecord(
                task=task_type,
                task_id=task.id,
                status="pending",
                attempt=docs.attempt_counts(doc).get(task_type, 0) + 1,
                scope_document_id=scope_document_id,
                write_mode=write_mode,
                params=resolved_params,
                param_fingerprint=task.param_fingerprint,
                # `pending` is the one status with nothing measured yet: the record
                # contract rejects timestamps here, precisely so a queued task cannot
                # claim a start time it does not have.
            ),
        )
        return task

    def get(self, task_id: int) -> Optional[TaskQueue]:
        return self.session.get(TaskQueue, task_id)

    # =========================================================================
    # Claim — spec 5.3's conflict rules
    # =========================================================================

    def claim_next(
        self,
        worker_id: str,
        *,
        service_badges: Optional[Sequence[str]] = None,
        root_document_id: Optional[int] = None,
    ) -> Optional[TaskQueue]:
        """Atomically take the highest-priority claimable task, or return None.

        ``root_document_id`` narrows the search to one document's own tree — that node and
        everything whose ``path`` contains it. ``SPRINT_JOBS.md`` 15.4 S3 added it for
        ``POST /ingest``, which drains the queue INSIDE the caller's request and must not
        spend that request running some other document's work. It is ``None`` for the
        background worker, whose whole job is to take whatever is next.

        **A scoped claim returning None does not mean the root is finished.** The
        background worker may hold the row this call skipped — ``FOR UPDATE SKIP LOCKED``
        makes the two safe to run at once, which is the point, and the cost is that "I
        could not claim anything" and "there is nothing left" stop being the same fact.
        :meth:`unfinished_task_count_under` is the second question, and
        :meth:`~jmfts_core.ingest_worker.IngestWorker.drain_document` asks it rather than
        reading an empty claim as completion.

        ``service_badges`` is a LIST, not one badge, because a pool's capability and the
        routing decision are different things. A host running a local LLM on a GPU can
        answer both the urgent badge and the cheap one; a light runner that forwards to a
        metered web API can answer only the cheap one. With one badge per worker the
        expensive pool sits idle whenever no urgent work exists, and "route by cost and
        urgency" has no way to express the fallback that makes it worth doing.

        NOTE WHAT THIS DOES NOT GIVE YOU. The ordering is still ``priority DESC,
        created_at ASC`` — badges are a filter, not a preference order. A worker listing
        ``[urgent, bulk]`` takes whatever is oldest and highest priority among BOTH, so an
        idle expensive worker will start a bulk task a second before an urgent one
        arrives. ``priority`` on the row is the lever for that; preference ordering inside
        the claim would mean one query per badge and is deliberately not done here.

        A task is claimable when all of:

        * it is ``pending``, or ``failed`` with retries left (see the module docstring —
          retry is folded in here instead of running as a background sweep, so there is
          no window where a task is due for retry and nothing has noticed);
        * its ``retry_after`` has passed;
        * every id in ``dependencies`` names a row that is ``completed`` (spec 5.5,
          ordering *within* one node);
        * its declared write mode does not conflict with any task already claimed or
          running, per spec 5.3's table:

          ===========  ==========================================================
          candidate    conflicts with an active task when
          ===========  ==========================================================
          ``self``     another ``self`` on the same node
          ``children`` another ``children`` on the same node
          ``subtree``  anything scoped at that node or inside its subtree
          ===========  ==========================================================

          and symmetrically — an active ``subtree`` task blocks any candidate scoped
          inside it, whatever the candidate's own mode.

        Re-claiming a ``failed`` row increments ``retry_count`` and clears the previous
        error, which is what makes the retry cap bite: the row is only claimable while
        ``retry_count < max_retries``.
        """
        # Serialise the whole claim. Without this the conflict predicate is advisory:
        # two claimers both read the table before either writes 'claimed'.
        self.session.execute(
            text("SELECT pg_advisory_xact_lock(:k1, :k2)"),
            {"k1": _CLAIM_LOCK_KEY[0], "k2": _CLAIM_LOCK_KEY[1]},
        )

        sql = text("""
            UPDATE task_queue SET
                status = 'claimed',
                claimed_by = :worker_id,
                started_at = clock_timestamp(),
                -- The first beat, written by the claim itself. Without it a worker that
                -- died between claiming and starting its heartbeat thread would hold a
                -- row with a NULL heartbeat, and the reaper would have to decide whether
                -- NULL means "old worker, no heartbeat support" or "just claimed".
                heartbeat_at = clock_timestamp(),
                completed_at = NULL,
                error = NULL,
                error_type = NULL,
                retry_after = NULL,
                retry_count = retry_count + CASE WHEN status = 'failed' THEN 1 ELSE 0 END
            WHERE id = (
                SELECT t.id
                FROM task_queue t
                JOIN documents ds ON ds.id = t.scope_document_id
                WHERE (
                        t.status = 'pending'
                        OR (t.status = 'failed' AND t.retryable
                            AND t.retry_count < t.max_retries)
                      )
                  AND (t.retry_after IS NULL OR t.retry_after <= NOW())
                  -- An un-badged worker claims anything; a badged one claims ANY of its
                  -- own badges, plus un-badged work. Triskelion required strict equality
                  -- here, which made a task with a NULL badge unclaimable by anyone.
                  AND (:service_badges IS NULL
                       OR t.service_badge IS NULL
                       OR t.service_badge = ANY(CAST(:service_badges AS text[])))
                  -- One document's own tree, when a caller asked for that. Matched by
                  -- `path` containment, the same region `unfinished_task_count_under`
                  -- counts and the same one the `subtree` write mode reserves, so a
                  -- scoped drain and the check that ends it describe one set of rows.
                  AND (CAST(:root_document_id AS integer) IS NULL
                       OR ds.id = CAST(:root_document_id AS integer)
                       OR ds.path @> jsonb_build_array(CAST(:root_document_id AS integer)))
                  -- Dependency gate. Counting completed rows rather than testing for a
                  -- non-completed one means a dependency whose row has VANISHED (its
                  -- document was deleted, cascading) blocks instead of silently
                  -- unblocking: running a dependent out of order is a wrong answer,
                  -- waiting is a visible stall.
                  AND (
                        t.dependencies IS NULL
                        OR cardinality(t.dependencies) = 0
                        OR (
                            SELECT count(*) FROM task_queue dep
                            WHERE dep.id = ANY(t.dependencies)
                              AND dep.status = 'completed'
                           ) = cardinality(t.dependencies)
                      )
                  AND NOT EXISTS (
                        SELECT 1
                        FROM task_queue a
                        JOIN documents da ON da.id = a.scope_document_id
                        WHERE a.status IN ('claimed', 'running', 'batched')
                          AND a.id <> t.id
                          AND (
                               (a.write_mode = t.write_mode
                                AND a.write_mode IN ('self', 'children')
                                AND a.scope_document_id = t.scope_document_id)
                            OR (a.write_mode = 'subtree'
                                AND (a.scope_document_id = t.scope_document_id
                                     OR ds.path @> jsonb_build_array(a.scope_document_id)))
                            OR (t.write_mode = 'subtree'
                                AND (a.scope_document_id = t.scope_document_id
                                     OR da.path @> jsonb_build_array(t.scope_document_id)))
                          )
                      )
                ORDER BY t.priority DESC, t.created_at ASC, t.id ASC
                LIMIT 1
                FOR UPDATE OF t SKIP LOCKED
            )
            RETURNING id
            """)
        # An EMPTY list is normalised to None — "claims anything" — rather than passed
        # through as an empty array, which `= ANY('{}')` would make false for every badged
        # row. A worker configured with no badges is the un-badged appliance worker, not a
        # worker that can claim nothing.
        badges = list(service_badges) if service_badges else None
        row = self.session.execute(
            sql,
            {
                "worker_id": worker_id,
                "service_badges": badges,
                "root_document_id": root_document_id,
            },
        ).fetchone()
        if row is None:
            return None
        # populate_existing: the UPDATE above was raw SQL, so any copy of this row
        # already in the identity map still says 'pending'.
        return self.session.get(TaskQueue, row[0], populate_existing=True)

    def requeue_stale_claims(self, worker_id: str) -> list[int]:
        """Fail the rows ``worker_id`` still holds, so a died worker's work runs again.

        Spec 7.3's machine-task row: *"Stale claim | worker died, requeue"*. A row left
        ``claimed`` or ``running`` by a process that was killed is invisible to every other
        mechanism here — ``claim_next`` admits only ``pending`` and retryable ``failed``,
        so it is never taken again; ``_unfinished_criterion`` counts it as unfinished, so
        ``structuring_complete`` is permanently false and the node and every ancestor stay
        out of the retrieval indexes; and the claim query's conflict predicate treats it as
        a live reservation, so a re-enqueued task on the same node in the same write mode
        cannot be claimed either. Nothing times out. It stalls forever.

        CALLED AT WORKER STARTUP, and scoped to this worker's own id, which is what makes
        it provably safe without a lease clock: a worker that is starting cannot also be
        running the task its previous incarnation claimed, because that process is gone.
        A lease with an expiry — the general case, and what spec 7.3 requires for REVIEW
        tasks, which are claimed by people and held for weeks — needs a duration that says
        how long a task may legitimately run, and this branch has no such number to give.
        Guessing one would let a slow ``probe`` be re-claimed and run twice concurrently.

        RECORDED AS A FAILURE, not silently flipped back to ``pending``. Going through
        :meth:`fail` buys three things that matter and would otherwise have to be
        reinvented: the attempt log gets an entry saying the worker died holding this task
        (spec 5.6 — the durable record is what a person reads later), the retry cap is
        consumed, so a task that kills the process does not crash-loop the appliance
        forever, and when the cap runs out the node goes to ``settled = 'failed'`` instead
        of hanging in flight, which is exactly what 2.1 says ``failed`` is for.

        The retry is scheduled immediately rather than with the usual backoff: the backoff
        exists to decorrelate tasks that all failed against the same overloaded dependency,
        and a process that was killed is not that.

        Returns the ids it acted on.
        """
        stmt = (
            select(TaskQueue)
            .where(TaskQueue.claimed_by == worker_id)
            .where(TaskQueue.status.in_(TASK_REAPABLE_STATUSES))
            .order_by(TaskQueue.id)
        )
        stale = list(self.session.execute(stmt).scalars().all())
        for task in stale:
            self.fail(
                task,
                error=(
                    f"worker {worker_id!r} was holding this task in status "
                    f"{task.status!r} when it restarted; the run did not finish"
                ),
                error_type=ErrorType.RETRYABLE,
                detail={"requeued_from": task.status, "worker_id": worker_id},
                retry_delay_seconds=0,
            )
        return [task.id for task in stale]

    def touch_heartbeat(self, task_id: int) -> bool:
        """Report that the worker holding ``task_id`` is still alive.

        Returns whether a row was actually touched. False means the task is no longer
        active — it finished, it was reaped, or its document was deleted — which is the
        caller's signal to stop beating for it.

        MUST BE CALLED FROM ITS OWN SESSION, and this method deliberately does not have
        the usual ``self.session`` shape you would reach for. The worker runs its handler
        inside one long transaction (see ``IngestWorker.run_once``), and an UPDATE issued
        inside that transaction is invisible to every other connection until it commits —
        which is precisely when the heartbeat is no longer needed. A beat written into the
        task's own transaction reports nothing to anyone. ``IngestWorker`` therefore beats
        from a separate thread with a separate session; this is written as a plain UPDATE
        with no ORM identity-map involvement so that is cheap.

        Scoped to the active statuses so a beat can never resurrect a row's timestamp
        after something else has already decided the worker was gone.
        """
        result = self.session.execute(
            text("""
                UPDATE task_queue
                SET heartbeat_at = clock_timestamp()
                WHERE id = :task_id
                  AND status IN ('claimed', 'running')
            """),
            {"task_id": task_id},
        )
        return result.rowcount > 0

    def requeue_expired_claims(self, lease_seconds: float) -> list[int]:
        """Fail active tasks whose worker has stopped reporting in. Spec 7.3, for a fleet.

        The fleet counterpart to :meth:`requeue_stale_claims`. That one recovers rows by
        matching a restarting worker's own id, which is provably safe with no clock at all
        but only ever fires for a worker that comes BACK. This one covers the cases it
        cannot: a pod rescheduled under a new id, and a host that stays down.

        ``lease_seconds`` bounds how long a LIVE worker may go without beating, not how
        long a task may run — see migration 011. It must be a comfortable multiple of the
        beat interval, or an ordinary scheduling delay reaps a task that is still running;
        :func:`jmfts_core.config.Settings.validate_worker_lease` enforces that ratio at
        startup rather than leaving it to whoever writes the deployment.

        ``COALESCE(heartbeat_at, started_at)``: a row claimed by a worker built before the
        heartbeat existed has no beat, and must still be reapable. ``started_at`` is
        written by the same statement that sets 'claimed', so it is never NULL on an
        active row and the COALESCE cannot fall through.

        Failures go through :meth:`fail`, exactly as ``requeue_stale_claims`` does, so the
        attempt log records why the run ended, the retry cap is consumed, and a task that
        reliably kills its worker eventually settles the node 'failed' instead of
        crash-looping the fleet forever.

        The retry is scheduled immediately. The backoff exists to decorrelate tasks that
        failed against the same overloaded dependency, and a worker that stopped beating
        is not that.

        THIS METHOD DOES NOT TAKE THE REAPER LOCK. :meth:`with_reaper_lock` is separate so
        the caller can hold it across the whole read-decide-write, and so a test can drive
        the reaping directly without contending for it.
        """
        cutoff_expr = text(
            "COALESCE(heartbeat_at, started_at) < clock_timestamp() "
            "- make_interval(secs => :lease_seconds)"
        )
        stmt = (
            select(TaskQueue)
            .where(TaskQueue.status.in_(TASK_REAPABLE_STATUSES))
            .where(cutoff_expr)
            .order_by(TaskQueue.id)
        )
        expired = list(self.session.execute(stmt, {"lease_seconds": lease_seconds}).scalars().all())
        for task in expired:
            self.fail(
                task,
                error=(
                    f"worker {task.claimed_by!r} stopped reporting in while holding this "
                    f"task in status {task.status!r}; no heartbeat for over "
                    f"{lease_seconds:g}s"
                ),
                error_type=ErrorType.RETRYABLE,
                detail={
                    "requeued_from": task.status,
                    "worker_id": task.claimed_by,
                    "lease_seconds": lease_seconds,
                    "reason": "lease expired",
                },
                retry_delay_seconds=0,
            )
        return [task.id for task in expired]

    def with_reaper_lock(self) -> bool:
        """Try to take the fleet's reaper lock for this transaction. True if we got it.

        Every worker runs the reaper on its own timer, so that the fleet has no component
        whose death stops recovery — a dedicated reaper process would itself be the thing
        nothing recovers when it dies. The lock is what keeps that from meaning N workers
        all reaping the same rows at once: `pg_try_advisory_xact_lock` is non-blocking, so
        the losers simply skip this round instead of queueing up behind the winner to
        redo work that is already done.

        Transaction-scoped, so it is released by the commit and no worker can leak it.
        """
        got = self.session.execute(
            text("SELECT pg_try_advisory_xact_lock(:k1, :k2)"),
            {"k1": _REAPER_LOCK_KEY[0], "k2": _REAPER_LOCK_KEY[1]},
        ).scalar()
        return bool(got)

    def mark_batched(self, tasks: Sequence[TaskQueue], batch_id: str) -> list[int]:
        """Trade a set of claims for ``batched``, recording where the work went. Spec 012.

        Called AFTER the provider has accepted the batch, which is what makes this a
        handoff between two schedulers rather than one transaction. The order is forced:
        submit first, then write. Writing first would leave rows marked as parked against a
        batch that was never created, and nothing would ever poll them.

        The window that ordering opens is a worker dying between the provider's acceptance
        and this call. Those rows stay ``claimed``, the lease requeues them, and a later
        worker submits a SECOND batch for answers already bought — money spent twice and a
        retry consumed. Committing a caller-supplied idempotency key before the submit
        would turn that into a lookup; the provider in use offers no such key, so the
        window is accepted and named rather than papered over. It is milliseconds wide and
        the duplicate result overwrites an identical one, so it is expensive rather than
        corrupting.

        NOT ``fail()`` WITH A DIFFERENT STATUS. Going through the failure path would
        consume a retry for work that has not been attempted, and at the cap it would put
        the node into ``settled = 'failed'`` — publishing a permanent failure for a task
        that is merely waiting. Neither is touched here.

        No attempt record is written. The attempt is still open: it began at the claim and
        ends when the batch returns and :meth:`complete` records what happened. Appending a
        record here would log one attempt as two.
        """
        if not batch_id:
            raise ValueError(
                "mark_batched needs the provider's batch id; without it nothing can ever "
                "poll these rows and they are unreachable by every other mechanism here"
            )
        moved: list[int] = []
        for task in tasks:
            if task.status not in TASK_REAPABLE_STATUSES:
                raise ValueError(
                    f"task {task.id} is {task.status!r}, not one of {TASK_REAPABLE_STATUSES}; "
                    "only a task this worker is holding can be handed to a batch"
                )
            task.status = TASK_BATCHED
            task.batch_id = batch_id
            task.batched_at = func.clock_timestamp()
            # The heartbeat stops meaning anything here: nothing beats for a batched row.
            # Cleared so a stale timestamp cannot be read as liveness by a later reader.
            task.heartbeat_at = None
            moved.append(task.id)
        self.session.flush()
        return moved

    def batched_tasks(self, batch_id: str) -> list[TaskQueue]:
        """Every task still parked in ``batch_id``, oldest first.

        The poll pass's read. Scoped to ``batched`` rather than to the id alone because
        ``batch_id`` stays on the row after completion, as part of the record of how the
        answer was obtained.
        """
        stmt = (
            select(TaskQueue)
            .where(TaskQueue.batch_id == batch_id)
            .where(TaskQueue.status == TASK_BATCHED)
            .order_by(TaskQueue.id)
        )
        return list(self.session.execute(stmt).scalars().all())

    def outstanding_batches(self) -> list[str]:
        """Distinct batch ids with at least one task still parked.

        What a poll pass iterates. Deliberately not scoped to a worker: the batch is
        durable at the provider and addressed by this id, so ANY worker that can reach the
        provider can adopt it. Tying the poll to the worker that submitted would mean a
        rescheduled pod strands its batch forever — the same class of stall the lease was
        built to abolish, and one the lease cannot reach here.
        """
        stmt = (
            select(TaskQueue.batch_id)
            .where(TaskQueue.status == TASK_BATCHED)
            .where(TaskQueue.batch_id.is_not(None))
            .distinct()
            .order_by(TaskQueue.batch_id)
        )
        return list(self.session.execute(stmt).scalars().all())

    def with_batch_lock(self, batch_id: str) -> bool:
        """Try to take the poll lock for one batch. True if we got it.

        Any worker may adopt a batch, so two can poll the same one at once and both write
        its results. The write is idempotent in content but not in effect — two workers
        completing the same task race on the attempt log. Non-blocking, like the reaper
        lock: the loser skips this batch and finds it again next pass.

        Keyed by hashing the id into the same two-int namespace, so it cannot collide with
        the claim or reaper locks.
        """
        got = self.session.execute(
            text("SELECT pg_try_advisory_xact_lock(:k1, hashtext(:batch_id))"),
            {"k1": _BATCH_LOCK_NAMESPACE, "batch_id": batch_id},
        ).scalar()
        return bool(got)

    def stalled_batches(self, older_than_seconds: float) -> list[TaskQueue]:
        """Tasks parked longer than ``older_than_seconds``. The only stall signal there is.

        A ``batched`` row is invisible to every other recovery mechanism in this file, by
        design: ``claim_next`` will not take it, the lease will not reap it, and the
        conflict predicate counts it as a live reservation so nothing else can work that
        node either. That is exactly the zombie-row shape migration 011 abolished,
        reintroduced deliberately — and this query is the compensating control.

        ``older_than_seconds`` should be the provider's turnaround window plus slack.
        Anything past it is not slow, it is stuck: the batch was lost, or the worker that
        submitted it recorded an id the provider never issued.

        Returns the rows rather than acting on them. What to do about a stalled batch —
        re-poll, fail it, resubmit — depends on what the provider says about the id, and
        that answer does not live in this repository.
        """
        cutoff = text("batched_at < clock_timestamp() - make_interval(secs => :older_than_seconds)")
        stmt = (
            select(TaskQueue)
            .where(TaskQueue.status == TASK_BATCHED)
            .where(cutoff)
            .order_by(TaskQueue.batched_at)
        )
        return list(
            self.session.execute(stmt, {"older_than_seconds": older_than_seconds}).scalars().all()
        )

    def mark_running(self, task: TaskQueue) -> TaskQueue:
        """Move a claimed task to ``running``.

        Kept distinct from ``claimed`` so a row abandoned between the claim transaction
        and the start of work is distinguishable from one abandoned mid-run.
        """
        if task.status != TASK_CLAIMED:
            raise ValueError(f"task {task.id} is {task.status!r}, not {TASK_CLAIMED!r}")
        task.status = TASK_RUNNING
        self.session.flush()
        return task

    # =========================================================================
    # Finish — spec 5.6, the queue row and the durable log written together
    # =========================================================================

    def complete(
        self,
        task: TaskQueue,
        *,
        detail: Optional[dict] = None,
        produced: Optional[dict] = None,
        rung: Optional[str] = None,
        status: str = "completed",
    ) -> AttemptRecord:
        """Mark ``task`` finished and append its attempt record to the scope node.

        Spec 5.6 puts live state and the durable record in two places on purpose — the
        queue row is purgeable, the attempt log is what a person reads a year later — so
        the single thing that must not happen is one being written without the other.
        Both writes happen here, in the caller's transaction.

        ``status`` may be ``'skipped'`` for a task that was never attempted; the record
        contract then requires ``detail['reason']``.
        """
        if task.started_at is None:
            raise ValueError(
                f"task {task.id} has no started_at: a task that was never claimed "
                "cannot complete, and the attempt's timing must be measured"
            )
        task.status = TASK_COMPLETED
        task.completed_at = func.clock_timestamp()
        task.error = None
        task.error_type = None
        self.session.flush()
        self.session.refresh(task)

        return self._append_attempt(
            task,
            status=status,
            detail=dict(detail or {}),
            produced=produced,
            rung=rung,
            error=None,
            error_type=None,
        )

    def fail(
        self,
        task: TaskQueue,
        *,
        error: str,
        error_type: ErrorType,
        detail: Optional[dict] = None,
        retry_delay_seconds: Optional[int] = None,
    ) -> AttemptRecord:
        """Record a failure, decide whether it will run again, and log the attempt.

        The policy, in one place and in Python rather than triskelion's plpgsql:

        * ``retryable``/``timeout`` schedule another attempt with exponential backoff,
          as long as the retry cap has room;
        * ``permanent``/``dependency`` do not, and neither does an exhausted cap;
        * a task that will not run again puts its node into ``settled = 'failed'``
          (spec 2.1) — without that a node whose ingestion died permanently is
          indistinguishable from one still in progress, and the settle walk would keep
          waiting on it forever;
        * **unless the task type is advisory**
          (:data:`~jmfts_core.models.task_queue.ADVISORY_TASK_TYPES`,
          ``OFFICE_SPEC.md`` Part 5), in which case the attempt is recorded exactly as
          above and the node's ``settled`` is not touched at all.

        The advisory branch is one assignment wide, and it has to be: everything else about
        the failure — the ``failed`` status, the spent retry budget, the attempt record
        carrying the error and its classification — is the same, because the failure is
        just as real. What differs is only whether the NODE is declared dead because of it.
        A permanently failed task is already not "unfinished" (``_unfinished_criterion``),
        so skipping the write does not leave the walk waiting on anything; the node settles
        with the failure in its log and without the output the task would have added. See
        the constant for the rule about which types may be in that set.
        """
        if task.started_at is None:
            raise ValueError(f"task {task.id} has no started_at and cannot be failed")

        retryable = error_type in RETRYABLE_ERROR_TYPES
        will_retry = retryable and task.retry_count < task.max_retries

        task.status = TASK_FAILED
        task.completed_at = func.clock_timestamp()
        task.error = error
        task.error_type = error_type.value
        task.retryable = retryable
        if will_retry:
            delay = (
                retry_delay_seconds
                if retry_delay_seconds is not None
                else int(
                    _BACKOFF_BASE_SECONDS * (2**task.retry_count) * random.uniform(*_BACKOFF_JITTER)
                )
            )
            task.retry_after = func.now() + func.make_interval(0, 0, 0, 0, 0, 0, delay)
        else:
            task.retry_after = None
            # The one place the advisory set is read. Note what is NOT conditional on it:
            # the status, the cleared `retry_after`, and the attempt record below all
            # happen either way, because an advisory task that failed permanently really
            # did fail permanently and the log has to say so.
            if task.task_type not in ADVISORY_TASK_TYPES:
                doc = self.session.get(Document, task.scope_document_id)
                if doc is not None:
                    doc.settled = SETTLED_FAILED

        self.session.flush()
        self.session.refresh(task)

        return self._append_attempt(
            task,
            status="failed",
            detail=dict(detail or {}),
            produced=None,
            rung=None,
            error=error,
            error_type=error_type.value,
        )

    def _append_attempt(
        self,
        task: TaskQueue,
        *,
        status: str,
        detail: dict,
        produced: Optional[dict],
        rung: Optional[str],
        error: Optional[str],
        error_type: Optional[str],
    ) -> AttemptRecord:
        """Build the spec-3.4 record for a finished task and append it to its node."""
        doc = self.session.get(Document, task.scope_document_id)
        if doc is None:
            raise ValueError(
                f"task {task.id} is scoped to document {task.scope_document_id}, which "
                "no longer exists; the attempt has nowhere durable to go"
            )

        # `attempt` is a 1-based counter per (node, task) — count what the log already
        # holds for this task name rather than reading task.retry_count, because a
        # re-run under spec 6.1 is a NEW queue row whose retry_count restarts at zero
        # while the node's history does not.
        #
        # The LIVE entry for this very task is excluded from that count: enqueue writes a
        # `pending` record (spec 5.7) which this call replaces rather than follows, so
        # counting it would number the first outcome of an attempt as its second.
        prior = [
            entry
            for entry in DocumentRepository(self.session).attempt_log(doc)
            if isinstance(entry, dict)
            and entry.get("task") == task.task_type
            and not (
                entry.get("task_id") == task.id and entry.get("status") not in TERMINAL_STATUSES
            )
        ]

        record = AttemptRecord(
            task=task.task_type,
            task_id=task.id,
            status=status,
            attempt=len(prior) + 1,
            rung=rung,
            scope_document_id=task.scope_document_id,
            write_mode=task.write_mode,
            params=dict(task.params or {}),
            param_fingerprint=task.param_fingerprint,
            started_at=_as_utc(task.started_at),
            finished_at=_as_utc(task.completed_at),
            detail=detail,
            produced=produced,
            error=error,
            error_type=error_type,
        )
        DocumentRepository(self.session).upsert_attempt(doc, record)
        return record

    # =========================================================================
    # Queries the scheduler asks — spec 5.2
    # =========================================================================

    def unfinished_tasks_for(self, document_id: int) -> list[TaskQueue]:
        """Every task scoped to ``document_id`` that still owes work."""
        stmt = (
            select(TaskQueue)
            .where(TaskQueue.scope_document_id == document_id)
            .where(_unfinished_criterion())
            .order_by(TaskQueue.id)
        )
        return list(self.session.execute(stmt).scalars().all())

    def structuring_complete(self, document_id: int) -> bool:
        """Spec 5.2: is no task scoped to this node pending or running?

        A QUERY, not a stored column. The spec is explicit that "structuring complete"
        and "settled" are different conditions and that only the second one is worth a
        column: structuring complete is true the moment a node's own tasks drain, and
        false again the moment a correction enqueues one, with no transition anybody
        has to remember to write down.
        """
        stmt = (
            select(func.count())
            .select_from(TaskQueue)
            .where(TaskQueue.scope_document_id == document_id)
            .where(_unfinished_criterion())
        )
        return self.session.execute(stmt).scalar_one() == 0

    def unfinished_task_count_under(self, root_id: int) -> int:
        """Tasks that still owe work anywhere in ``root_id``'s subtree, root included.

        The frontier report of spec 2.4 counts NODES; this counts the queue rows behind
        them, so a client can tell "in flight and something is queued for it" from "in
        flight and nothing is" — the second being a stall worth looking at.

        Matched by ``path``, the same containment the node counts use, so the two numbers
        describe the same region.
        """
        stmt = (
            select(func.count())
            .select_from(TaskQueue)
            .join(Document, Document.id == TaskQueue.scope_document_id)
            .where(
                (Document.id == root_id) | (Document.path.op("@>")(func.jsonb_build_array(root_id)))
            )
            .where(_unfinished_criterion())
        )
        return self.session.execute(stmt).scalar_one()

    def active_task_count(self, document_id: int) -> int:
        """Tasks currently holding a reservation on this node (claimed or running)."""
        stmt = (
            select(func.count())
            .select_from(TaskQueue)
            .where(TaskQueue.scope_document_id == document_id)
            .where(TaskQueue.status.in_(TASK_RESERVING_STATUSES))
        )
        return self.session.execute(stmt).scalar_one()

    def attempted_fingerprints(self, document_id: int) -> set[tuple[str, str]]:
        """``(task, param_fingerprint)`` pairs the node's attempt log already records.

        Spec 6.1's diff key. The log is the authority rather than the queue, because
        queue rows are purgeable and the whole point of the key is to answer "has this
        exact work been done, ever" across purges and across days.
        """
        doc = self.session.get(Document, document_id)
        if doc is None:
            return set()
        pairs: set[tuple[str, str]] = set()
        for entry in DocumentRepository(self.session).attempt_log(doc):
            if not isinstance(entry, dict):
                continue
            task = entry.get("task")
            fingerprint = entry.get("param_fingerprint")
            if isinstance(task, str) and isinstance(fingerprint, str):
                pairs.add((task, fingerprint))
        return pairs


def _unfinished_criterion():
    """A task row that still owes work — spec 5.2's "structuring complete" predicate.

    Deliberately wider than the spec's literal "pending or running". ``claimed`` is a
    worker holding the row and about to run it. And because retry is folded into
    claimability (there is no sweeper flipping ``failed`` back to ``pending``), a failed
    task that is still retryable and under its cap IS the queue's representation of
    "queued, waiting for its backoff" — settling a node while one of those is outstanding
    would publish a node whose work is about to run again.
    """
    return TaskQueue.status.in_((TASK_PENDING, TASK_CLAIMED, TASK_RUNNING, TASK_BATCHED)) | (
        (TaskQueue.status == TASK_FAILED)
        & TaskQueue.retryable.is_(True)
        & (TaskQueue.retry_count < TaskQueue.max_retries)
    )


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """TIMESTAMPTZ comes back aware; assert that rather than assuming it."""
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError(
            "task timestamp is naive; the queue's clocks are server-side TIMESTAMPTZ "
            "and an attempt record must say which clock it came from"
        )
    return value.astimezone(timezone.utc)
