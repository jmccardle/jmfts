"""TaskQueue — one row per queued unit of ingest work. ``INGEST_SPEC.md`` Part 5.

Ported from triskelion's ``vdo_core/models/task_queue.py`` with the column set spec 1.4
names: status, priority, ``dependencies``, ``retry_count``/``max_retries``,
``error_type``, ``retry_after``, ``claimed_by``, ``service_badge``.

**Why ``claimed_by`` and ``service_badge`` survive a port to an in-process worker.**
Spec 5.8: workers run as a thread inside the API process, so there is no fleet to
address. Both columns cost a nullable VARCHAR each and they are what a later split back
into processes would need — and Part 7's review tasks need ``service_badge`` immediately,
to route a task at a human rather than at the worker loop. Dropping them now would mean
a migration later to add them back with the same names.

**Two JMFTS columns triskelion has no equivalent of** — ``scope_document_id`` and
``write_mode`` (spec 5.3). Together they say what region of the tree the task reserves
while it runs, which is what the claim query checks for conflicts. Triskelion had only
``target_document_id``, "the node this task is about", and no notion of a reservation.
That column is deliberately NOT ported: the node a JMFTS task is about *is* the node it
is scoped to, and carrying both would let the two disagree with nothing to say which
one the conflict rules meant.

``params`` / ``param_fingerprint`` are also new. The worker needs the parameters to run
the task at all, the attempt record (spec 3.4) has to log them, and spec 6.1 keys the
re-run diff on ``(task_name, param_fingerprint)`` — so the fingerprint must be readable
from the queue row without re-deriving it from params that may have been written by an
older version of the code.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from jmfts_core.database import Base

#: Task lifecycle. Mirrors the ``ck_task_queue_status`` CHECK constraint.
#:
#: ``claimed`` and ``running`` are separate on purpose: a worker marks ``claimed`` in the
#: short claim transaction and ``running`` when it actually begins, so a row stuck in
#: ``claimed`` says the worker died between the two.
TASK_PENDING = "pending"
TASK_CLAIMED = "claimed"
TASK_RUNNING = "running"

#: Submitted to an external batch provider and waiting for it. Migration 012.
#:
#: Not a variant of ``failed`` despite looking like one from the claim query's side, where
#: both are simply "not claimable": ``batched`` must not consume a retry, must not touch
#: the node's ``settled``, and must never become claimable on its own. What it shares with
#: ``failed`` is only that no worker is holding it.
TASK_BATCHED = "batched"

TASK_COMPLETED = "completed"
TASK_FAILED = "failed"
TASK_STATUSES: tuple[str, ...] = (
    TASK_PENDING,
    TASK_CLAIMED,
    TASK_RUNNING,
    TASK_BATCHED,
    TASK_COMPLETED,
    TASK_FAILED,
)

#: Statuses that hold a reservation on the scope region. A ``pending`` task reserves
#: nothing — it has not started — which is what lets the queue hold thousands of
#: pending tasks over one subtree without any of them blocking each other.
#:
#: ``batched`` IS here: the task will write its node's ``effective_content`` when the batch
#: returns, and a second ``self`` task writing there in the meantime is exactly the race
#: the reservation exists to prevent.
TASK_RESERVING_STATUSES: tuple[str, ...] = (TASK_CLAIMED, TASK_RUNNING, TASK_BATCHED)

#: Statuses in which a LIVE WORKER is holding the row, so a stale heartbeat means the
#: worker died and the task should be requeued.
#:
#: ``batched`` is deliberately NOT here, and that exclusion is the whole reason these two
#: tuples exist separately. Until migration 012 one constant answered both questions
#: because the answers coincided. Nothing beats for a batched row and nothing should —
#: the work is at the provider, not in a worker — so reaping one would requeue a task
#: whose answer is already bought and paid for.
#:
#: The cost of that exclusion is that a ``batched`` row is invisible to every recovery
#: mechanism here. ``batched_at`` and
#: :meth:`~jmfts_core.repositories.task_queue.TaskQueueRepository.stalled_batches` are what
#: make its stall detectable at all.
TASK_REAPABLE_STATUSES: tuple[str, ...] = (TASK_CLAIMED, TASK_RUNNING)

#: Declared write modes, spec 5.3. See ``TaskQueueRepository.claim_next`` for the
#: conflict matrix these drive.
WRITE_SELF = "self"
WRITE_CHILDREN = "children"
WRITE_SUBTREE = "subtree"
WRITE_MODES: tuple[str, ...] = (WRITE_SELF, WRITE_CHILDREN, WRITE_SUBTREE)


class TaskQueue(Base):
    """One queued unit of ingest work, scoped to one document node."""

    __tablename__ = "task_queue"

    # Constraint names match migration 010 / schema.sql exactly. Triskelion declared
    # `task_status_check` in the model and `task_queue_status_check` in the DDL, so a
    # create_all() against an existing database added a second, duplicate constraint
    # under a different name.
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'claimed', 'running', 'batched', 'completed', 'failed')",
            name="ck_task_queue_status",
        ),
        CheckConstraint(
            "write_mode IN ('self', 'children', 'subtree')",
            name="ck_task_queue_write_mode",
        ),
        CheckConstraint(
            "error_type IS NULL OR error_type IN "
            "('retryable', 'permanent', 'timeout', 'dependency')",
            name="ck_task_queue_error_type",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # e.g. 'probe', 'structure:declared', 'summarize' — the Part 4 task list.
    task_type: Mapped[str] = mapped_column(String(50), nullable=False)

    # The node this task is scoped to: what it is about AND what it reserves.
    # ON DELETE CASCADE — a task for a document that no longer exists is not work, and
    # the alternative (triskelion's bare REFERENCES) makes deleting an in-flight
    # document raise a foreign-key violation from a table the caller never named.
    scope_document_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )

    # Spec 5.3. Checked at claim time, and enforced again inside the write path for
    # `children` (see DocumentRepository.reparent's childless guard).
    write_mode: Mapped[str] = mapped_column(String(10), nullable=False)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default=TASK_PENDING)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Ordering WITHIN one node (spec 5.5): ids of tasks that must be `completed` before
    # this one may be claimed. Cross-level ordering is deliberately NOT expressed here —
    # a planned list of rollup tasks goes stale the moment a child is added, so the walk
    # in jmfts_core/settling.py re-reads the tree instead (spec 5.4).
    dependencies: Mapped[Optional[list[int]]] = mapped_column(ARRAY(Integer), nullable=True)

    # Parameters that affect output, and spec 6.1's diff key over them.
    params: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    param_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)

    # Spec 5.8: who should run this, and who did. Both nullable; the in-process worker
    # claims un-badged work by default.
    service_badge: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    claimed_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # Worker liveness while the task is held. Touched on a fixed interval by whoever holds
    # the claim, so what it measures is "the worker is still alive", not "the task has run
    # this long". That distinction is the whole design: see migration 011: a lease keyed on
    # elapsed RUNTIME needs a bound on legitimate task duration, which this branch does not
    # have and cannot guess, while a bound on how long a live worker may go without
    # reporting in is a property of the loop itself.
    #
    # Set at claim time as well as by the beat, so a worker that dies between claiming and
    # its first beat is still reapable.
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Where a `batched` task is parked, and since when. Migration 012.
    #
    # COLUMNS, NOT KEYS IN `params`. `param_fingerprint` is derived from `params` at enqueue
    # and spec 6.1 keys the re-run diff on (task_name, param_fingerprint), so writing
    # execution state into `params` mid-flight would silently invalidate that key. `params`
    # is what the task was asked to do; this is what happened while doing it.
    #
    # `batch_id` survives completion on purpose: it is part of the record of how the answer
    # was obtained, and the index that serves the poll pass is partial on `batched` so the
    # kept-around ids cost nothing.
    batch_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    batched_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Server-side clocks throughout. `retry_after` is compared against the server's
    # NOW() in the claim query, so a client clock here would make backoff depend on
    # whichever machine wrote the row.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_type: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    # `retryable` is derived from `error_type` on failure and kept as its own column so
    # the claim query and the retry index can test it without re-encoding the policy.
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    retry_after: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<TaskQueue {self.id} {self.task_type!r} scope={self.scope_document_id} "
            f"mode={self.write_mode} status={self.status}>"
        )
