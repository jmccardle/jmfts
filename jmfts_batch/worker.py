"""The batch worker: gather, submit, park, poll, apply.

It is a second consumer of the same queue :class:`~jmfts_core.ingest_worker.IngestWorker`
drains, claiming the same ``summarize:llm`` tasks under the same badge. The difference is
what it does between the claim and the answer. The direct worker holds the task open for
one HTTP call; this one hands a hundred of them to a provider at half price and comes back
hours later.

**Four states, in order.**

1. *Gathered.* Tasks are ``running``, held by this worker, with a heartbeat. If the worker
   dies here the lease requeues them and nothing has been spent.
2. *Submitted.* The provider has accepted the batch. Money is committed. The rows still say
   ``running``, and the lease will requeue them — which is the accepted window, described
   in ``jmfts_core.repositories.task_queue.mark_batched`` and again in ``README.md``.
3. *Batched.* ``mark_batched`` has committed. The fact now outlives the worker, and any
   worker that can reach the provider may adopt the batch.
4. *Applied.* Results are in, ``effective_content`` is written, the tasks are terminal and
   the settle walk runs.

**The gather size is set by the reservation, not by the provider.** Both providers allow
tens of thousands of requests per batch. Every task in a JMFTS batch holds a ``self``
reservation on its node for the batch's whole life — up to twenty-four hours — during which
nothing else may write that node. Gathering broadly freezes a large part of the tree for a
day to save a few HTTP calls. :data:`DEFAULT_GATHER_SIZE` is deliberately small.

**Polling is not scoped to the submitter.** ``outstanding_batches()`` returns every parked
batch id in the appliance, and this worker will adopt any of them. That is what stops a
rescheduled pod from stranding its batch forever — the same class of stall the heartbeat
lease abolished for claims, which the lease cannot reach here because nothing beats for a
``batched`` row.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from typing import Iterator, Optional, Sequence

from sqlalchemy.orm import Session

from jmfts_core.database import get_session
from jmfts_core.ingest_tasks import TASK_SUMMARIZE_LLM
from jmfts_core.models.task_queue import TASK_BATCHED, TaskQueue
from jmfts_core.repositories.task_queue import TaskQueueRepository
from jmfts_core.rollup_tasks import IngestRollupPlanner
from jmfts_core.settling import RollupPlanner, SessionFactory, settle_after_task
from jmfts_core.task_routing import badge_for
from jmfts_core.task_errors import ErrorType, classify_exception

from jmfts_batch.provider import BatchProvider, BatchResult, task_id_from
from jmfts_batch.summarize import apply_summary, prepare

logger = logging.getLogger(__name__)

#: How many tasks go into one batch. Small on purpose — see the module docstring.
DEFAULT_GATHER_SIZE = 32

#: How long a batch may sit ``batched`` before it is reported as stalled. Both providers
#: expire a batch at twenty-four hours, so anything past this is not slow, it is lost.
DEFAULT_STALL_SECONDS = 26 * 3600


class BatchWorker:
    """Drives ``summarize:llm`` tasks through an external batch provider."""

    def __init__(
        self,
        provider: BatchProvider,
        *,
        model: str,
        worker_id: str,
        session_factory: SessionFactory = get_session,
        planner: Optional[RollupPlanner] = None,
        service_badges: Optional[Sequence[str]] = None,
        gather_size: int = DEFAULT_GATHER_SIZE,
        max_tokens: int = 512,
        temperature: float = 0.2,
        heartbeat_seconds: float = 10.0,
        stall_seconds: float = DEFAULT_STALL_SECONDS,
    ):
        if gather_size < 1:
            raise ValueError(f"gather_size must be at least 1, got {gather_size}")
        if gather_size > provider.max_requests:
            raise ValueError(
                f"gather_size {gather_size} exceeds {provider.name}'s cap of "
                f"{provider.max_requests}"
            )
        if not model:
            raise ValueError(
                "BatchWorker needs a model name; a batch submitted without one would be "
                "answered by whatever the provider defaults to today"
            )

        # THE ROUTING POLICY IS OFF BY DEFAULT AND THIS WORKER CANNOT RUN WITHOUT IT.
        # `claim_next` filters by badge and nothing else — it does not know or care what
        # `task_type` a row carries. With no policy configured every task in the appliance
        # is un-badged, an un-badged task is claimable by anyone, and this worker would
        # gather `probe` and `extract:text` rows into an LLM batch and pay to summarize
        # them. Refusing to start is the only safe reading of that configuration.
        badge = badge_for(TASK_SUMMARIZE_LLM)
        if not service_badges:
            raise ValueError(
                "BatchWorker needs at least one service badge; an un-badged worker claims "
                "every task type in the queue, and this one can only run "
                f"{TASK_SUMMARIZE_LLM}"
            )
        if badge is None:
            raise ValueError(
                f"no badge is configured for {TASK_SUMMARIZE_LLM}, so the queue cannot "
                "route it to this worker. Set JMFTS_TASK_BADGES (see "
                "jmfts_core.task_routing.EMBEDDING_AND_LLM_POLICY) before running a batch "
                "worker"
            )
        if badge not in service_badges:
            raise ValueError(
                f"{TASK_SUMMARIZE_LLM} is routed to badge {badge!r}, which is not among "
                f"this worker's badges {list(service_badges)}; it would claim nothing it "
                "can run"
            )

        self.provider = provider
        self.model = model
        self.worker_id = worker_id
        self.session_factory = session_factory
        #: The real planner by default. A batch worker that settled with ``NO_ROLLUP``
        #: would complete a node's summary and never offer its parent one, stopping the
        #: upward recursion at whatever level the first batch happened to cover.
        self.planner = planner if planner is not None else IngestRollupPlanner()
        self.service_badges = list(service_badges) if service_badges else None
        self.gather_size = gather_size
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.heartbeat_seconds = heartbeat_seconds
        self.stall_seconds = stall_seconds

    # =========================================================================
    # One pass
    # =========================================================================

    def run_once(self) -> bool:
        """Deliver what has come back, then send what is waiting. True if anything moved.

        Polling first is not arbitrary. Applying a result releases that node's reservation
        and may make its parent's rollup eligible, so a pass that gathered first would
        submit a batch for work the pass was about to unblock anyway.
        """
        delivered = self.poll_once()
        submitted = self.gather_and_submit()
        return bool(delivered) or submitted is not None

    # =========================================================================
    # Gather and submit — states 1 and 2
    # =========================================================================

    def gather_and_submit(self) -> Optional[str]:
        """Claim up to ``gather_size`` tasks and hand them to the provider.

        Returns the batch id, or None when nothing was claimable or every claimed node
        turned out not to need an LLM after all.
        """
        task_ids = self._claim_batch()
        if not task_ids:
            return None

        # The beat runs from here until `mark_batched` commits. That span includes the
        # submit HTTP call, which for a large batch is minutes of upload — long enough for
        # the lease to reap a task that is very much still being worked on, and reaping a
        # task mid-submit is how the same node gets paid for twice.
        with self._heartbeat(task_ids):
            requests = []
            batched_ids: list[int] = []
            settle_after: list[int] = []

            for task_id in task_ids:
                with self.session_factory() as session:
                    tasks = TaskQueueRepository(session)
                    task = tasks.get(task_id)
                    if task is None:
                        logger.warning("task %s vanished after being claimed", task_id)
                        continue
                    if task.task_type != TASK_SUMMARIZE_LLM:
                        # Reachable despite the constructor's check: a task enqueued before
                        # the routing policy was turned on carries a NULL badge, and
                        # `claim_next` lets a badged worker take un-badged work. Retryable,
                        # because the task is fine and this worker is the wrong one.
                        logger.error(
                            "claimed task %s is %r, not %r — it was probably enqueued "
                            "before JMFTS_TASK_BADGES was set",
                            task_id,
                            task.task_type,
                            TASK_SUMMARIZE_LLM,
                        )
                        tasks.fail(
                            task,
                            error=(
                                f"a batch worker claimed {task.task_type!r}, which it "
                                "cannot run; the task carries no service badge"
                            ),
                            error_type=ErrorType.RETRYABLE,
                        )
                        continue
                    tasks.mark_running(task)
                    try:
                        prepared = prepare(
                            session,
                            task,
                            model=self.model,
                            max_tokens=self.max_tokens,
                            temperature=self.temperature,
                        )
                    except Exception as exc:  # noqa: BLE001 — recorded, never swallowed
                        logger.exception("preparing task %s for a batch failed", task_id)
                        session.rollback()
                        self._fail(task_id, exc)
                        continue

                    if prepared.outcome is not None:
                        # The node no longer needs an LLM. Finish it here rather than
                        # spending a batch slot on it.
                        tasks.complete(
                            task,
                            detail=prepared.outcome.detail,
                            produced=prepared.outcome.produced,
                            rung=prepared.outcome.rung,
                            status=prepared.outcome.status,
                        )
                        settle_after.append(prepared.document_id)
                        continue

                    requests.append(prepared.request)
                    batched_ids.append(task_id)

            batch_id = self._submit(requests, batched_ids) if requests else None

        for document_id in settle_after:
            settle_after_task(document_id, self.planner, session_factory=self.session_factory)

        return batch_id

    def _claim_batch(self) -> list[int]:
        """Take up to ``gather_size`` claimable tasks, one short transaction each.

        Not one transaction for all of them: ``claim_next`` holds a transaction-scoped
        advisory lock that serialises every claim in the appliance, so gathering thirty-two
        tasks inside one transaction would stop every other worker for the duration.
        """
        task_ids: list[int] = []
        for _ in range(self.gather_size):
            with self.session_factory() as session:
                task = TaskQueueRepository(session).claim_next(
                    self.worker_id, service_badges=self.service_badges
                )
                if task is None:
                    break
                task_ids.append(task.id)
        return task_ids

    def _submit(self, requests: list, task_ids: list[int]) -> Optional[str]:
        """Submit, then trade the claims for ``batched``. The order is forced.

        A failure to submit fails the tasks rather than leaving them for the lease. The
        lease would get there eventually and cost nothing extra, but it would record the
        stall as a timeout — losing the provider's actual complaint, which is the only
        thing that says whether the batch will ever work.
        """
        try:
            batch_id = self.provider.submit(requests)
        except Exception as exc:  # noqa: BLE001 — recorded against every gathered task
            logger.exception("submitting a batch of %d requests failed", len(requests))
            for task_id in task_ids:
                self._fail(task_id, exc)
            return None

        # Between the line above and the commit below is the window this design accepts:
        # the provider has the work and will bill for it, and nothing durable says so yet.
        with self.session_factory() as session:
            tasks = TaskQueueRepository(session)
            rows = [row for row in (tasks.get(task_id) for task_id in task_ids) if row is not None]
            tasks.mark_batched(rows, batch_id)

        logger.info(
            "batch %s submitted to %s with %d tasks", batch_id, self.provider.name, len(task_ids)
        )
        return batch_id

    # =========================================================================
    # Poll and apply — states 3 and 4
    # =========================================================================

    def poll_once(self) -> int:
        """Check every parked batch and apply whatever is ready. Returns tasks delivered."""
        with self.session_factory() as session:
            batch_ids = TaskQueueRepository(session).outstanding_batches()

        delivered = 0
        for batch_id in batch_ids:
            delivered += self.poll_batch(batch_id)
        return delivered

    def poll_batch(self, batch_id: str) -> int:
        """Advance one batch. Returns how many tasks reached a terminal state."""
        settle_after: list[int] = []
        delivered = 0

        # ONE TRANSACTION FOR THE WHOLE BATCH, because `with_batch_lock` is
        # transaction-scoped and the lock is what stops two workers applying the same
        # results and racing on the attempt log. It is a long transaction — it spans the
        # provider HTTP calls and the embedding of every summary — and that is the cost of
        # the lock being the only exclusion available here.
        with self.session_factory() as session:
            tasks = TaskQueueRepository(session)
            if not tasks.with_batch_lock(batch_id):
                logger.debug("batch %s is held by another worker; skipping this pass", batch_id)
                return 0

            parked = tasks.batched_tasks(batch_id)
            if not parked:
                return 0

            status = self.provider.poll(batch_id)
            logger.info("batch %s: %s", batch_id, status.detail)

            if status.dead:
                # The batch will never produce results. Every task parked against it has to
                # be resolved from here; waiting is not a state it can leave.
                for task in parked:
                    self._fail_in(
                        session,
                        task,
                        error=f"the provider reported batch {batch_id} as {status.detail}",
                        error_type=ErrorType.RETRYABLE,
                    )
                    settle_after.append(task.scope_document_id)
                delivered = len(parked)
            elif not status.ready:
                return 0
            else:
                by_task = {task.id: task for task in parked}
                for result in self.provider.results(batch_id):
                    task = self._match(by_task, result, batch_id)
                    if task is None:
                        continue
                    self._apply(session, task, result, batch_id)
                    settle_after.append(task.scope_document_id)
                    delivered += 1

                # Anything still parked was not in the results. Left alone deliberately:
                # `stalled_batches` will surface it, and inventing an outcome for a request
                # the provider did not mention would be a guess about work we paid for.
                unanswered = [
                    task_id for task_id, task in by_task.items() if task.status == TASK_BATCHED
                ]
                if unanswered:
                    logger.warning(
                        "batch %s ended without results for tasks %s; they stay batched "
                        "and will appear in stalled_batches",
                        batch_id,
                        sorted(unanswered),
                    )

        for document_id in settle_after:
            settle_after_task(document_id, self.planner, session_factory=self.session_factory)

        return delivered

    def _match(
        self, by_task: dict[int, TaskQueue], result: BatchResult, batch_id: str
    ) -> Optional[TaskQueue]:
        """Find the task this result belongs to, or skip it with a reason.

        A result for a task that is not parked here is not an error to raise on — the batch
        may legitimately contain a task that was already resolved — but it is never applied
        on a guess.
        """
        try:
            task_id = task_id_from(result.custom_id)
        except ValueError:
            logger.error(
                "batch %s returned an unrecognised custom_id %r", batch_id, result.custom_id
            )
            return None
        task = by_task.get(task_id)
        if task is None:
            logger.warning(
                "batch %s returned a result for task %s, which is no longer parked here",
                batch_id,
                task_id,
            )
        return task

    def _apply(self, session: Session, task: TaskQueue, result: BatchResult, batch_id: str) -> None:
        """Turn one result into a terminal task."""
        tasks = TaskQueueRepository(session)

        if result.error is not None:
            self._fail_in(session, task, error=result.error, error_type=result.error_type)
            return

        try:
            outcome = apply_summary(
                session,
                task,
                result.text,
                provider=self.provider.name,
                model=self.model,
                batch_id=batch_id,
            )
        except Exception as exc:  # noqa: BLE001 — recorded against this task
            logger.exception("applying the batch result for task %s failed", task.id)
            self._fail_in(session, task, error=str(exc), error_type=classify_exception(exc))
            return

        tasks.complete(
            task,
            detail=outcome.detail,
            produced=outcome.produced,
            rung=outcome.rung,
            status=outcome.status,
        )

    def _fail_in(
        self, session: Session, task: TaskQueue, *, error: str, error_type: ErrorType
    ) -> None:
        """Fail a task inside the caller's transaction.

        ``mark_batched`` wrote no attempt record — the attempt has been open since the
        claim — so this is where a batched task's one attempt is finally logged, whichever
        way it went.
        """
        TaskQueueRepository(session).fail(
            task,
            error=error,
            error_type=error_type,
            detail={"provider": self.provider.name, "batch_id": task.batch_id},
        )

    def _fail(self, task_id: int, exc: BaseException) -> None:
        """Fail a task in a fresh transaction, for errors raised outside one."""
        with self.session_factory() as session:
            tasks = TaskQueueRepository(session)
            task = tasks.get(task_id)
            if task is None:
                logger.warning("task %s vanished before its failure could be recorded", task_id)
                return
            tasks.fail(
                task,
                error=f"{type(exc).__name__}: {exc}",
                error_type=classify_exception(exc),
                detail={"provider": self.provider.name},
            )

    # =========================================================================
    # Liveness and stalls
    # =========================================================================

    @contextmanager
    def _heartbeat(self, task_ids: Sequence[int]) -> Iterator[None]:
        """Beat for every gathered task until the block exits.

        One thread and one session for the whole gather, not one per task: the tasks are
        held and released together, so their liveness is one fact.
        """
        done = threading.Event()

        def beat() -> None:
            while not done.wait(self.heartbeat_seconds):
                try:
                    with self.session_factory() as session:
                        tasks = TaskQueueRepository(session)
                        for task_id in task_ids:
                            tasks.touch_heartbeat(task_id)
                except (
                    Exception
                ):  # noqa: BLE001 — a missed beat is survivable, a dead thread is not
                    logger.exception("heartbeat for a gathered batch failed")

        thread = threading.Thread(target=beat, name=f"batch-beat-{self.worker_id}", daemon=True)
        thread.start()
        try:
            yield
        finally:
            done.set()
            thread.join(timeout=self.heartbeat_seconds + 5.0)

    def stalled(self) -> list[TaskQueue]:
        """Tasks parked longer than ``stall_seconds``. Reported, never acted on.

        What to do about a stalled batch depends on what the provider says about the id,
        and this worker does not get to decide that a paid-for answer is not coming.
        """
        with self.session_factory() as session:
            return TaskQueueRepository(session).stalled_batches(self.stall_seconds)


def run_forever(worker: BatchWorker, *, poll_seconds: float, stop: threading.Event) -> None:
    """Poll loop. Sleeps ``poll_seconds`` whenever a pass moved nothing."""
    while not stop.is_set():
        started = time.monotonic()
        try:
            moved = worker.run_once()
        except Exception:  # noqa: BLE001 — the loop outlives one bad pass
            logger.exception("batch worker pass failed")
            moved = False
        if not moved:
            stop.wait(poll_seconds)
        else:
            logger.debug("pass finished in %.1fs", time.monotonic() - started)
