"""The ingest worker. ``INGEST_SPEC.md`` 5.8, and the fleet that outgrew it.

5.8 said:

    Workers run in-process. Triskelion split services into separate processes under a
    ``ServiceManager`` because it had to control GPU VRAM allocation across models. JMFTS
    is a single appliance and does not have that problem. The task loop runs as a worker
    thread inside the API process.

That is still how the API runs one (:func:`build_worker_from_settings`, started from the
lifespan), and there is still no ``ServiceManager``, no service-definition document and no
VRAM accounting — just a thread running :meth:`IngestWorker.run_once` until it is told to
stop. What changed is that 5.8's premise, "JMFTS is a single appliance", stopped being
true: the same class now also runs as its own process, on more than one host, via
``python -m jmfts_core.worker``. Triskelion's reason for splitting has not come back —
VRAM is still not arbitrated here — but the throughput reason has.

TWO THINGS THE FLEET NEEDED THAT ONE IN-PROCESS WORKER DID NOT.

* **A heartbeat** (migration 011). :meth:`recover_own_claims` recovers a restarting
  worker's own rows and is provably safe with no clock, but it only ever fires for a
  worker that comes BACK. A pod rescheduled under a new id, or a host that stays down,
  leaves rows that nothing recovers — and a stranded row is not idle work, it is a live
  reservation that parks the whole subtree. :meth:`_heartbeat` reports liveness while a
  task is held and :meth:`reap_expired` requeues what has stopped reporting.
* **A badge on every worker.** ``claim_next`` lets an un-badged worker claim anything,
  which is the right default for one appliance and a silent misconfiguration in a fleet.
  See the module docstring of :mod:`jmfts_core.worker`.

**One session per iteration, and more than one per task.** The worker never touches a
request-scoped session; it opens its own, and it opens a *new* one for each phase, because
the phases have different transactional requirements:

1. **claim** — must be its own short transaction. The claim takes a transaction-scoped
   advisory lock (see ``TaskQueueRepository.claim_next``), so holding it for the length of
   the work would serialise the whole appliance behind one task.
2. **mark running** — its own transaction too, so the ``claimed`` → ``running`` transition
   is durable before the work starts. That is what makes a row stuck in ``claimed``
   readable as "the worker died before it began" rather than "died halfway through".
3. **run** — the handler's transaction. Everything the task writes, plus the completion
   and its attempt record, commit together (spec 5.6: the queue row and the durable log
   must not be able to diverge).
4. **fail** — a *fresh* transaction. By the time a failure is being recorded the previous
   one has been rolled back, and if the exception came from the database that session is
   unusable anyway. Recording the failure into the transaction that failed would lose it.
5. **settle** — :func:`jmfts_core.settling.settle_after_task`, which owns one session per
   level by design (5.4's "committing between levels").

**Off unless asked.** ``Settings.ingest_worker_enabled`` defaults to True for the
appliance and is pinned to False by ``tests/conftest.py``, because roughly half the
suite's ``TestClient`` fixtures run the lifespan and half do not: a worker started
unconditionally there would be alive in some unrelated tests and dead in others, running
a real poll loop against the test database while a fixture holds an uncommitted
transaction. Tests that want a worker drive :meth:`IngestWorker.drain` synchronously
instead (``tests/conftest.py::drain_ingest_queue``); polling with sleeps makes a flaky
suite.

**No principal is bound in a worker thread.** ``jmfts_core.principal_context`` is a
``ContextVar``, and a thread does not inherit one, so the repositories see ``None``, which
that module documents as owner-equivalent — the worker can write anything. For an ingest
worker that is what is wanted, but it is true by accident of the mechanism rather than by
declaration, and it means the principal who submitted an upload is not available when its
tasks run. Recorded here so the next person to need RBAC on ingested nodes knows it has
to come off the task row.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import AbstractContextManager, contextmanager
from typing import Callable, Iterator, Optional, Sequence

from sqlalchemy.orm import Session

from jmfts_core.config import get_settings
from jmfts_core.database import get_session
from jmfts_core.ingest_tasks import UnknownTaskError, get_task_handler
from jmfts_core.repositories.task_queue import TaskQueueRepository
from jmfts_core.settling import NO_ROLLUP, RollupPlanner, settle_after_task
from jmfts_core.task_errors import ErrorType, classify_exception

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], AbstractContextManager[Session]]

#: How long the loop waits after an error in the loop ITSELF (not in a task — a task
#: failure is recorded and the loop continues immediately). Triskelion's service template
#: used 5s and the reason still holds: the realistic cause is the database being
#: unreachable, and retrying that at the poll interval produces a log full of the same
#: traceback several times a second.
ERROR_BACKOFF_SECONDS = 5.0

#: Mirrors ``Settings.MIN_LEASE_TO_HEARTBEAT_RATIO``. Spelled here too because a worker
#: can be constructed without going through Settings at all.
MIN_LEASE_TO_HEARTBEAT_RATIO = 3.0


class IngestWorker:
    """Claims queued ingest tasks and runs them, one at a time.

    Usable three ways, on purpose:

    * :meth:`run_once` — one claim-and-run, no thread. This is the unit under test.
    * :meth:`drain` — run until the queue offers nothing more. Synchronous, bounded, and
      what the test suite uses instead of sleeping until something happens.
    * :meth:`start` / :meth:`stop` — the thread the API process runs.
    """

    def __init__(
        self,
        *,
        worker_id: Optional[str] = None,
        session_factory: SessionFactory = get_session,
        planner: RollupPlanner = NO_ROLLUP,
        service_badges: Optional[Sequence[str]] = None,
        poll_seconds: float = 1.0,
        error_backoff_seconds: float = ERROR_BACKOFF_SECONDS,
        heartbeat_seconds: float = 10.0,
        lease_seconds: float = 90.0,
        reap_seconds: float = 30.0,
    ):
        #: Identifies this worker in ``task_queue.claimed_by``. Includes the thread name
        #: so two workers in one process are distinguishable in the table.
        self.worker_id = worker_id or f"ingest-worker-{threading.get_ident()}"
        self.session_factory = session_factory
        #: What becomes eligible for a node once its subtree finishes (spec 5.4 step 4).
        #: ``NO_ROLLUP`` is a named policy, not an absent argument: rollup summarisation
        #: is not implemented as a queued task yet, and a walk that quietly enqueued
        #: nothing because nobody passed a planner would settle a whole tree without ever
        #: summarising it and look like it worked.
        self.planner = planner
        #: Every badge this worker answers to, or None for "claims anything". A list
        #: because capability and routing are different things: a host with a local LLM can
        #: serve both the urgent and the cheap badge, while a runner that forwards to a
        #: metered API serves only the cheap one. See ``TaskQueueRepository.claim_next``.
        self.service_badges = list(service_badges) if service_badges else None
        self.poll_seconds = poll_seconds
        self.error_backoff_seconds = error_backoff_seconds

        # Fleet liveness, migration 011. Checked here as well as in Settings because a
        # caller can construct a worker directly — the test suite does — and the failure
        # this ratio prevents (a live worker's task reaped and run a second time,
        # concurrently, on another host) is not one that shows up as a crash.
        if heartbeat_seconds <= 0:
            raise ValueError(f"heartbeat_seconds must be positive, got {heartbeat_seconds!r}")
        if lease_seconds < heartbeat_seconds * MIN_LEASE_TO_HEARTBEAT_RATIO:
            raise ValueError(
                f"lease_seconds ({lease_seconds:g}) must be at least "
                f"{MIN_LEASE_TO_HEARTBEAT_RATIO:g}x heartbeat_seconds "
                f"({heartbeat_seconds:g}); below that a missed beat costs a running task"
            )
        self.heartbeat_seconds = heartbeat_seconds
        self.lease_seconds = lease_seconds
        self.reap_seconds = reap_seconds

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        #: monotonic() of the last reap ATTEMPT, not the last successful one — a worker
        #: that loses the advisory lock every round must still back off, or the fleet
        #: spends its poll interval asking for a lock it will not get.
        self._last_reap_at = 0.0

    # =========================================================================
    # One task
    # =========================================================================

    def run_once(self) -> bool:
        """Claim one task, run it, record the outcome, settle from it.

        Returns True if a task was claimed (whatever happened to it), False if the queue
        had nothing claimable — which is the loop's signal to wait before asking again.
        """
        with self.session_factory() as session:
            task = TaskQueueRepository(session).claim_next(
                self.worker_id, service_badges=self.service_badges
            )
            if task is None:
                return False
            task_id = task.id
            task_type = task.task_type
            scope_document_id = task.scope_document_id

        # The beat covers everything from here to the terminal write, which is exactly the
        # span during which this row holds a reservation and another worker could reap it.
        with self._heartbeat(task_id):
            with self.session_factory() as session:
                tasks = TaskQueueRepository(session)
                claimed = tasks.get(task_id)
                if claimed is None:
                    # The scope document was deleted between the claim and here, cascading
                    # the row away. Nothing to run and nowhere to record it — the node the
                    # attempt would attach to is gone too.
                    logger.warning("task %s vanished after being claimed", task_id)
                    return True
                tasks.mark_running(claimed)

            # Whether the task reached a terminal state — the precondition for walking up.
            # Not a `finally`: the walk asks the database about the scope node, and the one
            # path that skips the work here is the one where that node has been deleted, so
            # a `finally` would turn a rare tidy-up into a SettleWalkError every time.
            finished = False
            try:
                with self.session_factory() as session:
                    tasks = TaskQueueRepository(session)
                    running = tasks.get(task_id)
                    if running is None:
                        logger.warning("task %s vanished after being marked running", task_id)
                    else:
                        handler = get_task_handler(task_type)
                        outcome = handler(session, running)
                        tasks.complete(
                            running,
                            detail=outcome.detail,
                            produced=outcome.produced,
                            rung=outcome.rung,
                            status=outcome.status,
                        )
                        finished = True
            except Exception as exc:  # noqa: BLE001 — classified and recorded, never swallowed
                # The session above is already rolled back (its context manager did that on
                # the way out) and may be unusable if the exception came from the database.
                # The failure is therefore recorded in a NEW transaction.
                logger.exception("ingest task %s (%s) failed", task_id, task_type)
                finished = self._record_failure(task_id, exc)

        if finished:
            # A completed task may have released its parent's rollup; a permanently failed
            # one has just put its node into 'failed', and the walk is what stops every
            # ancestor waiting on it forever. A retryable failure leaves an unfinished task
            # on the node, so the walk settles nothing and costs one query.
            settle_after_task(scope_document_id, self.planner, session_factory=self.session_factory)

        return True

    def _record_failure(self, task_id: int, exc: BaseException) -> bool:
        """Classify and persist a task failure. Spec 3.4's ``error_type``.

        Returns whether the failure was actually recorded, which is the caller's
        precondition for walking up: if the row is gone, so is the node the walk would
        have started from.

        An :class:`UnknownTaskError` is forced to PERMANENT rather than left to the
        classifier's conservative default: a task type nobody registered does not start
        working on the third attempt, and spending the retry budget on it only delays the
        moment somebody reads the reason.
        """
        error_type = (
            ErrorType.PERMANENT if isinstance(exc, UnknownTaskError) else classify_exception(exc)
        )
        with self.session_factory() as session:
            tasks = TaskQueueRepository(session)
            task = tasks.get(task_id)
            if task is None:
                logger.error(
                    "task %s failed with %s but its row is gone; the failure could not "
                    "be recorded",
                    task_id,
                    exc,
                )
                return False
            tasks.fail(task, error=f"{type(exc).__name__}: {exc}", error_type=error_type)
            return True

    @contextmanager
    def _heartbeat(self, task_id: int) -> Iterator[None]:
        """Report liveness for ``task_id`` on a timer, for as long as the block runs.

        A SEPARATE THREAD WITH ITS OWN SESSION, and it has to be. The handler runs inside
        one long transaction; an UPDATE issued on that session would not be visible to any
        other connection until the transaction commits, which is the moment the heartbeat
        stops being needed. The beat only means anything if it is committed while the work
        is still going, so it cannot share the work's transaction.

        The thread stops on its own if :meth:`TaskQueueRepository.touch_heartbeat` reports
        that the row is no longer active. That covers the case where another worker has
        already reaped this task: continuing to beat would be claiming liveness for a row
        somebody else now owns.

        A FAILED BEAT IS LOGGED AND RETRIED, not raised. The thread has no way to
        interrupt the handler, and the database being briefly unreachable is exactly the
        condition the lease exists to ride out — if it stays unreachable the task's own
        transaction fails anyway, and if it recovers within the lease nothing was lost.
        Killing the run from here would turn a blip into a failed task.
        """
        done = threading.Event()

        def beat() -> None:
            while not done.wait(self.heartbeat_seconds):
                try:
                    with self.session_factory() as session:
                        alive = TaskQueueRepository(session).touch_heartbeat(task_id)
                    if not alive:
                        logger.warning(
                            "task %s is no longer active; worker %s stopped beating for it "
                            "(it was most likely reaped as expired)",
                            task_id,
                            self.worker_id,
                        )
                        return
                except Exception:  # noqa: BLE001 — see the docstring: log and keep beating
                    logger.exception(
                        "worker %s could not record a heartbeat for task %s",
                        self.worker_id,
                        task_id,
                    )

        thread = threading.Thread(
            target=beat, name=f"jmfts-{self.worker_id}-beat-{task_id}", daemon=True
        )
        thread.start()
        try:
            yield
        finally:
            done.set()
            # Joined, not abandoned. An un-joined beat thread can commit its UPDATE after
            # the task row has already gone terminal, which writes a heartbeat onto a
            # completed row — harmless for the reaper, which only reads active rows, but it
            # also means the process can exit with a session mid-commit. The join is
            # bounded by the beat interval plus a margin for the statement in flight.
            thread.join(timeout=self.heartbeat_seconds + 5.0)
            if thread.is_alive():
                logger.error(
                    "heartbeat thread for task %s did not stop; it is still holding a " "session",
                    task_id,
                )

    def reap_expired(self) -> list[int]:
        """Requeue tasks across the WHOLE fleet whose worker has stopped beating.

        Complements :meth:`recover_own_claims`, which only ever fires for a worker that
        comes back. This is what covers a pod rescheduled under a new id and a host that
        stays down — see :meth:`TaskQueueRepository.requeue_expired_claims`.

        Every worker calls this on its own timer and the advisory lock picks one per round,
        so the fleet has no reaper component whose own death stops recovery. Losing the
        lock is the normal case, not an error: it returns an empty list.
        """
        with self.session_factory() as session:
            tasks = TaskQueueRepository(session)
            if not tasks.with_reaper_lock():
                return []
            reaped = tasks.requeue_expired_claims(self.lease_seconds)
        if reaped:
            logger.warning(
                "worker %s requeued %d task(s) whose worker stopped reporting in: %s",
                self.worker_id,
                len(reaped),
                reaped,
            )
        return reaped

    def _maybe_reap(self) -> None:
        """Run :meth:`reap_expired` if this worker's reap interval has elapsed."""
        now = time.monotonic()
        if now - self._last_reap_at < self.reap_seconds:
            return
        self._last_reap_at = now
        self.reap_expired()

    def drain(self, *, max_tasks: int = 1000) -> int:
        """Run tasks until the queue offers none, and return how many ran.

        Bounded rather than ``while True``: a handler that enqueues work which enqueues
        more work is exactly how structuring descends a tree, so a cycle in it would hang
        whatever called this. Reaching the bound RAISES rather than returning a count,
        because a drain that stopped early and returned normally would be read as "the
        queue is empty" when it is not — and every assertion made after it would be about
        a half-finished tree.
        """
        ran = 0
        while ran < max_tasks:
            if not self.run_once():
                return ran
            ran += 1
        raise RuntimeError(
            f"drain ran its bound of {max_tasks} tasks without the queue going quiet; "
            "raise max_tasks, or a handler is enqueueing work in a cycle"
        )

    # =========================================================================
    # The thread
    # =========================================================================

    def recover_own_claims(self) -> list[int]:
        """Requeue what a previous incarnation of this worker died holding. Spec 7.3.

        Driven once by :meth:`_loop` before it claims anything new — see
        :meth:`TaskQueueRepository.requeue_stale_claims` for why this is scoped to this
        worker's own id and why it goes through the failure path rather than flipping rows
        back to ``pending``. Public, so a deployment or a test can drive it without also
        starting a thread.
        """
        with self.session_factory() as session:
            recovered = TaskQueueRepository(session).requeue_stale_claims(self.worker_id)
        if recovered:
            logger.warning(
                "worker %s reclaimed %d task(s) left behind by a previous run: %s",
                self.worker_id,
                len(recovered),
                recovered,
            )
        return recovered

    def start(self) -> None:
        """Start the worker thread. Idempotent guard: starting twice is a bug, not a no-op."""
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError(f"worker {self.worker_id} is already running")
        self._stop.clear()
        # daemon=True so an interpreter that is exiting without a clean shutdown is not
        # held open by the loop. It is a backstop: `stop()` is the intended path, and it
        # joins, because a thread still committing while the process tears down is how a
        # test suite gets rows it never wrote.
        self._thread = threading.Thread(
            target=self._loop, name=f"jmfts-{self.worker_id}", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 30.0) -> None:
        """Signal the loop and join it. Raises if the thread does not finish in time."""
        self._stop.set()
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout)
        if thread.is_alive():
            raise RuntimeError(
                f"worker {self.worker_id} did not stop within {timeout}s; it is still "
                "holding a database session and will keep committing after shutdown"
            )
        self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _loop(self) -> None:
        logger.info("ingest worker %s started", self.worker_id)
        # Recovery runs INSIDE the loop, once, rather than in `start()`. Two reasons, both
        # about the database being unreachable at boot: `start()` is called from the API
        # lifespan, so a raise there stops the whole appliance from starting, and — worse —
        # the recovery would then simply not happen, leaving the rows it exists to rescue
        # stranded. In here it is covered by the loop's own backoff and is retried until it
        # succeeds, before the loop claims anything new.
        recovered = False
        while not self._stop.is_set():
            try:
                if not recovered:
                    self.recover_own_claims()
                    recovered = True
                # Before claiming, not after: a task freed by the reaper is claimable in
                # this same iteration, and on a fleet that has just lost a host the freed
                # work is the most important work there is.
                self._maybe_reap()
                did_work = self.run_once()
            except Exception:  # noqa: BLE001 — the loop's own resilience, not a task's
                # A task failure never reaches here; run_once records it. This is the
                # database being unreachable, or a bug in the loop itself. It is logged
                # with its traceback and the loop backs off — dying instead would leave
                # the appliance accepting uploads that nothing will ever process, with
                # the reason only in a stack trace nobody sees.
                logger.exception("ingest worker %s: loop error", self.worker_id)
                self._stop.wait(self.error_backoff_seconds)
                continue
            if not did_work:
                # Event.wait, not sleep: this is also the shutdown latency, and a sleeping
                # worker that cannot be interrupted makes `stop()` take a poll interval.
                self._stop.wait(self.poll_seconds)
        logger.info("ingest worker %s stopped", self.worker_id)


def build_worker_from_settings() -> IngestWorker:
    """The worker the API process runs, configured from ``Settings``.

    Carries :class:`~jmfts_core.rollup_tasks.IngestRollupPlanner`, so an uploaded file is
    segmented and given ``effective_content`` (spec 11.4). Before this it carried
    ``NO_ROLLUP``, which meant every tree settled without ever being summarized — the gap
    11.4 was written about. ``NO_ROLLUP`` remains available and named for a caller that
    wants structuring only.
    """
    from jmfts_core.rollup_tasks import IngestRollupPlanner

    settings = get_settings()
    return IngestWorker(
        worker_id="api-ingest-worker",
        planner=IngestRollupPlanner(),
        poll_seconds=settings.ingest_worker_poll_seconds,
        heartbeat_seconds=settings.worker_heartbeat_seconds,
        lease_seconds=settings.worker_lease_seconds,
        reap_seconds=settings.worker_reap_seconds,
    )
