"""``python -m jmfts_core.worker`` — one ingest worker as its own process.

``INGEST_SPEC.md`` 5.8 put the worker in a thread inside the API process, and said why:
JMFTS is a single appliance, so it does not have triskelion's problem of arbitrating VRAM
between services and does not need triskelion's ``ServiceManager``. That reasoning holds
for an appliance and stops holding the moment the answer to "ingestion is too slow" is
"another machine". This module is the split back into processes that
``jmfts_core/models/task_queue.py`` predicted when it kept ``claimed_by`` and
``service_badge`` against the day there was a fleet to address.

NOTHING ABOUT THE QUEUE CHANGES HERE. ``claim_next`` was already safe for concurrent
claimers — it serialises the claim under an advisory lock and enforces the write-mode
reservation across every worker, whatever process it is in — and the badge filter was
already the routing mechanism. What was missing was a way to run the loop without also
running an HTTP server, and a recovery path for a worker that never comes back
(migration 011). This module is the first; the heartbeat is the second.

THE BADGE IS THE ROUTING, AND EVERY WORKER IN A FLEET NEEDS ONE. ``claim_next`` lets an
un-badged worker claim ANY task, badged or not. That is the right default for one
appliance and the wrong one for a fleet: a single un-badged worker on a CPU host will
happily claim the GPU-badged work and run it on a CPU, slowly, while the GPU idles — and
nothing reports an error, because nothing has gone wrong as far as the queue is concerned.
``--badge`` is therefore not defaulted. Running without one is legal and is what a
single-host appliance wants, so it is not refused, but it is logged as a warning at
startup so an unbadged worker in a fleet is visible in the first line of its logs.

A WORKER NEED NOT HOLD THE MODEL. ``--runner-url`` sends the ``embed`` task to another
JMFTS's ``/runner`` surface, and a process started that way never imports torch — the
tokenizer is all it loads, because ``check_fit`` and the chunker are the only model-shaped
things left in a worker that does not embed locally (:mod:`jmfts_core.embedder`).

That is what makes a fixed set of GPUs shareable rather than partitioned. Without it, every
worker that touches ingest owns weights, so the cards are allocated by which pool you
started; with it, the storage-side workers scale on CPU and the cards sit behind one runner
that both embedding and summarization draw from. The badge is unchanged and orthogonal: a
thin worker may still take ``--badge embed``, because it IS answering for embedding work —
it just is not the thing running the model.

WORKER IDS MUST BE UNIQUE ACROSS THE FLEET AND STABLE ACROSS A RESTART, and those two
requirements pull in opposite directions. Unique, because ``claimed_by`` is how a task's
owner is identified and ``recover_own_claims`` is scoped by it — two workers sharing an id
would recover each other's in-flight tasks out from under themselves. Stable, because that
same recovery only fires for a worker that comes back under the SAME id, and it is the
fast path: it recovers immediately instead of waiting out the lease. The default here,
``{hostname}-{pid}``, is unique but not stable. Under Kubernetes, set ``JMFTS_WORKER_ID``
from the pod name via the downward API — a restarted pod keeps its name, so the fast path
works, and the lease covers a pod that is rescheduled under a new one.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import sys
import threading
from types import FrameType
from typing import Optional

from jmfts_core.config import get_settings
from jmfts_core.ingest_worker import IngestWorker

logger = logging.getLogger(__name__)

#: How long ``stop()`` waits for the loop to finish its current task before it raises.
#: Generous because the thing being waited for is one ingest task, and a `summarize` that
#: calls an LLM over a large node is minutes. A worker that overshoots this raises rather
#: than exiting quietly, so a container that stops taking SIGTERM cleanly is visible.
_STOP_TIMEOUT_SECONDS = 600.0


def default_worker_id() -> str:
    """``JMFTS_WORKER_ID`` if set, else ``{hostname}-{pid}``.

    The env var is the deployment's hook — see the module docstring on why a stable id is
    worth supplying and why this default is not one.
    """
    explicit = os.environ.get("JMFTS_WORKER_ID")
    if explicit:
        return explicit
    return f"{socket.gethostname()}-{os.getpid()}"


def default_badges() -> Optional[list[str]]:
    """Badges from ``JMFTS_WORKER_BADGE``, comma-separated.

    A container sets one environment variable, not a repeated flag, so the env form has to
    carry a list. Blank entries are dropped rather than becoming a badge named ``""``,
    which nothing would ever match and which would silently make the worker claim only
    un-badged work.
    """
    raw = os.environ.get("JMFTS_WORKER_BADGE", "")
    badges = [part.strip() for part in raw.split(",") if part.strip()]
    return badges or None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m jmfts_core.worker",
        description="Run one JMFTS ingest worker as its own process.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Every flag defaults to the matching JMFTS_* setting, so a container needs no\n"
            "arguments at all. The flags exist for driving a worker by hand.\n"
        ),
    )
    settings = get_settings()

    parser.add_argument(
        "--badge",
        action="append",
        dest="badges",
        default=None,
        help=(
            "a service_badge this worker claims; repeat for more than one. It also claims "
            "un-badged work. Omit for a worker that claims everything — correct for a "
            "single appliance, and a misconfiguration in a fleet (see the module "
            "docstring). Defaults to $JMFTS_WORKER_BADGE, which may be comma-separated."
        ),
    )
    parser.add_argument(
        "--worker-id",
        default=default_worker_id(),
        help="identity written to task_queue.claimed_by (default: $JMFTS_WORKER_ID, else host-pid)",
    )
    parser.add_argument(
        "--runner-url",
        default=settings.runner_url,
        metavar="URL",
        help=(
            "embed through another JMFTS's /runner surface instead of loading the model "
            "here. This process then never imports torch, and needs only the tokenizer. "
            "It still CAN take --badge embed — a thin worker that posts to a runner is "
            "answering for embedding work, it just is not doing it locally. Requires "
            "$JMFTS_RUNNER_KEY, which both sides read. Defaults to $JMFTS_RUNNER_URL; "
            "blank means embed locally."
        ),
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=settings.ingest_worker_poll_seconds,
        help="how long to wait after finding the queue empty",
    )
    parser.add_argument(
        "--heartbeat-seconds",
        type=float,
        default=settings.worker_heartbeat_seconds,
        help="how often to report liveness while holding a task",
    )
    parser.add_argument(
        "--lease-seconds",
        type=float,
        default=settings.worker_lease_seconds,
        help="how long another worker may go without beating before this one requeues its task",
    )
    parser.add_argument(
        "--reap-seconds",
        type=float,
        default=settings.worker_reap_seconds,
        help="how often to try the fleet-wide expired-claim sweep",
    )
    parser.add_argument(
        "--drain",
        action="store_true",
        help=(
            "run until the queue offers nothing more, then exit, instead of polling "
            "forever. For a one-shot batch job or a test."
        ),
    )
    parser.add_argument(
        "--max-tasks",
        type=int,
        default=1000,
        help="bound on --drain; reaching it is an error, not a clean exit",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("JMFTS_LOG_LEVEL", "INFO"),
        help="root log level (default: $JMFTS_LOG_LEVEL, else INFO)",
    )
    return parser


def apply_runner_override(args: argparse.Namespace) -> Optional[str]:
    """Point this process's embedding at a runner, and prove the credential is there.

    Written onto ``Settings`` rather than passed down, because the thing that reads it —
    ``DocumentRepository.embed_document``, several layers below the worker — asks
    ``jmfts_core.embedder.get_embedder`` for the process's embedder rather than receiving
    one. That is what keeps every ingest call site free of a branch on where the model is.

    Constructing the embedder HERE, at startup, is the point of the function. A runner URL
    with no key is a misconfiguration, and ``get_embedder`` refuses it — but if the first
    call were the first embed task, the refusal would arrive as one failed task in a log
    rather than as a process that would not start. No network call is made: the runner
    being briefly unreachable is a transient the retry policy already handles, and failing
    to start over it would turn a blip into a pod that will not come up.
    """
    from jmfts_core.embedder import get_embedder, reset_embedder

    settings = get_settings()
    settings.runner_url = args.runner_url or ""
    # The module caches by URL, and a previous incarnation in the same process (a test,
    # mostly) may hold one built from different settings.
    reset_embedder()
    if not settings.runner_url:
        return None
    return get_embedder().device


def build_worker(args: argparse.Namespace) -> IngestWorker:
    """Construct the worker described by ``args``.

    Carries :class:`~jmfts_core.rollup_tasks.IngestRollupPlanner` for the same reason
    ``build_worker_from_settings`` does: with ``NO_ROLLUP`` every tree settles without ever
    being summarised, which looks like success and produces an unsearchable tree.
    """
    from jmfts_core.rollup_tasks import IngestRollupPlanner

    return IngestWorker(
        worker_id=args.worker_id,
        planner=IngestRollupPlanner(),
        service_badges=args.badges if args.badges else default_badges(),
        poll_seconds=args.poll_seconds,
        heartbeat_seconds=args.heartbeat_seconds,
        lease_seconds=args.lease_seconds,
        reap_seconds=args.reap_seconds,
    )


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    settings = get_settings()
    # Before build_worker, so a runner URL with no key stops the process here rather than
    # one embed task from now.
    remote = apply_runner_override(args)
    worker = build_worker(args)

    logger.info(
        "worker %s starting: badges=%s embedder=%s db=%s@%s:%s/%s beat=%gs lease=%gs",
        args.worker_id,
        ",".join(worker.service_badges) if worker.service_badges else "(none — claims everything)",
        remote or f"local:{settings.embedding_device}",
        settings.db_user,
        settings.db_host,
        settings.db_port,
        settings.db_name,
        args.heartbeat_seconds,
        args.lease_seconds,
    )
    if not worker.service_badges:
        logger.warning(
            "worker %s has no --badge, so it will claim every badge's work, including work "
            "routed at a GPU or an LLM pool. Correct for a single appliance; in a fleet "
            "this is how embedding ends up running on a CPU host while the GPU idles.",
            args.worker_id,
        )

    if args.drain:
        # Recovery is part of the loop, not of start(), so a drain has to ask for it.
        # Without this a --drain worker reusing an id would leave its predecessor's
        # in-flight rows stranded and then report the queue as empty.
        worker.recover_own_claims()
        worker.reap_expired()
        ran = worker.drain(max_tasks=args.max_tasks)
        logger.info("worker %s drained %d task(s)", args.worker_id, ran)
        return 0

    # SIGTERM is how Kubernetes and systemd both ask a process to stop, and the default
    # disposition kills the process outright — mid-task, mid-transaction. Handling it lets
    # the current task finish and commit.
    #
    # THE HANDLER ONLY SETS AN EVENT. A Python signal handler runs on the main thread
    # between bytecodes, which means it can interrupt code holding a lock and must not do
    # anything that would try to take that lock again. Logging is exactly that: the first
    # version of this called logger.info() and worker.stop() from the handler, and a
    # SIGTERM that landed while the main thread was inside logging's stream.flush() raised
    # a reentrancy error out of the shutdown path. stop() is worse — it joins a thread,
    # and blocking inside a signal handler blocks the interpreter that has to run the
    # thread you are waiting for.
    #
    # This is why terminationGracePeriodSeconds has to exceed the longest task. When it
    # does not, the container runtime follows with SIGKILL, the task dies mid-flight, and
    # the row is recovered by the lease instead of finishing — correct, but a wasted run.
    stopping = threading.Event()
    received: list[int] = []

    def request_stop(signum: int, _frame: Optional[FrameType]) -> None:
        received.append(signum)
        stopping.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    worker.start()

    # Polled rather than a bare wait(), so a loop thread that dies on its own — an
    # unhandled error in the loop's own resilience path, say — ends the process instead of
    # leaving a container that is Running, claiming nothing, and reporting no problem.
    while not stopping.wait(1.0):
        if not worker.running:
            logger.error(
                "worker %s: the loop thread exited on its own; shutting down",
                args.worker_id,
            )
            break

    if received:
        logger.info(
            "worker %s received %s; finishing the current task and stopping",
            args.worker_id,
            signal.Signals(received[0]).name,
        )
    worker.stop(timeout=_STOP_TIMEOUT_SECONDS)
    logger.info("worker %s exited", args.worker_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
