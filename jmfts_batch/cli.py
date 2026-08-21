"""``jmfts-batch-worker`` — run the reference batch worker, or inspect what it left behind.

Subcommands, because a batch worker is two jobs on different clocks. ``run`` is the loop a
container runs. ``status``, ``stalled``, ``finalize`` and ``cancel`` are what a person
needs when a batch has been parked for a day and they want to know why.

Signal handling copies :mod:`jmfts_core.worker` exactly, including the reason: a handler
that logs or joins can re-enter the logging lock when a second signal lands during a
flush, and the exception comes out of the shutdown path. The handler only sets an event.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import sys
import threading
from pathlib import Path
from types import FrameType
from typing import Optional

from jmfts_core.config import get_settings
from jmfts_core.database import get_session
from jmfts_core.repositories.task_queue import TaskQueueRepository

from jmfts_batch.provider import BatchProvider
from jmfts_batch.worker import DEFAULT_GATHER_SIZE, DEFAULT_STALL_SECONDS, BatchWorker, run_forever

logger = logging.getLogger(__name__)

PROVIDERS = ("mock", "openai", "anthropic")


def build_provider(args: argparse.Namespace) -> BatchProvider:
    """Construct the named provider, or explain exactly what is missing.

    No provider is defaulted and no key is read from a fallback location. Picking a
    provider for a caller who did not name one would mean choosing where their documents
    get sent.
    """
    if args.provider == "mock":
        from jmfts_batch.providers.mock import MockBatchProvider, local_llm_chat

        settings = get_settings()
        base_url = args.llm_url or settings.effective_llm_url
        if not base_url:
            raise SystemExit(
                "the mock provider needs a chat endpoint: pass --llm-url or set "
                "JMFTS_LLM_BASE_URL"
            )
        return MockBatchProvider(
            root=Path(args.store),
            chat=local_llm_chat(
                base_url,
                model_timeout=settings.effective_llm_timeout,
                api_key=settings.llm_api_key,
                disable_thinking=settings.summarization_disable_thinking,
            ),
        )

    if args.provider == "openai":
        from jmfts_batch.providers.openai_api import OpenAIBatchProvider

        key = os.environ.get("OPENAI_API_KEY", "")
        if not key:
            raise SystemExit("the openai provider needs OPENAI_API_KEY")
        return OpenAIBatchProvider(key, metadata={"source": "jmfts", "worker": args.worker_id})

    if args.provider == "anthropic":
        from jmfts_batch.providers.anthropic_api import AnthropicBatchProvider

        key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise SystemExit("the anthropic provider needs ANTHROPIC_API_KEY")
        return AnthropicBatchProvider(key)

    raise SystemExit(f"unknown provider {args.provider!r}; expected one of {PROVIDERS}")


def default_worker_id() -> str:
    """Same rule as :func:`jmfts_core.worker.default_worker_id`."""
    explicit = os.environ.get("JMFTS_BATCH_WORKER_ID")
    if explicit:
        return explicit
    return f"batch-{socket.gethostname()}-{os.getpid()}"


def build_worker(args: argparse.Namespace) -> BatchWorker:
    settings = get_settings()
    model = args.model or settings.effective_llm_model
    if not model:
        raise SystemExit("no model: pass --model or set JMFTS_LLM_MODEL")
    return BatchWorker(
        build_provider(args),
        model=model,
        worker_id=args.worker_id,
        service_badges=args.badges,
        gather_size=args.gather_size,
        max_tokens=settings.raptor_max_summary_tokens,
        temperature=settings.summarization_temperature,
        stall_seconds=args.stall_seconds,
    )


def cmd_run(args: argparse.Namespace) -> int:
    worker = build_worker(args)
    logger.info(
        "batch worker %s starting: provider=%s badges=%s gather=%d",
        args.worker_id,
        worker.provider.name,
        worker.service_badges,
        worker.gather_size,
    )

    if args.once:
        moved = worker.run_once()
        logger.info("single pass finished, moved=%s", moved)
        return 0

    stopping = threading.Event()
    received: list[int] = []

    def request_stop(signum: int, _frame: Optional[FrameType]) -> None:
        # ONLY these two statements. See the module docstring.
        received.append(signum)
        stopping.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    thread = threading.Thread(
        target=run_forever,
        args=(worker,),
        kwargs={"poll_seconds": args.poll_seconds, "stop": stopping},
        name="batch-worker",
        daemon=True,
    )
    thread.start()
    while not stopping.wait(1.0):
        if not thread.is_alive():
            logger.error("the batch loop exited on its own; shutting down")
            break

    if received:
        logger.info("batch worker %s stopping on signal %s", args.worker_id, received[0])
    # A batch in flight is NOT waited for. It is durable at the provider and the id is
    # committed, so any worker can adopt it — which is the whole point of `batched`.
    thread.join(timeout=args.poll_seconds + 30.0)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """What is parked right now, by batch."""
    with get_session() as session:
        tasks = TaskQueueRepository(session)
        batch_ids = tasks.outstanding_batches()
        if not batch_ids:
            print("no outstanding batches")
            return 0
        for batch_id in batch_ids:
            parked = tasks.batched_tasks(batch_id)
            oldest = min((task.batched_at for task in parked if task.batched_at), default=None)
            print(f"{batch_id}\t{len(parked)} tasks\tsince {oldest}")
    return 0


def cmd_stalled(args: argparse.Namespace) -> int:
    """Batches parked longer than the provider's turnaround. Reported, never acted on."""
    with get_session() as session:
        rows = TaskQueueRepository(session).stalled_batches(args.stall_seconds)
    if not rows:
        print(f"no batch has been parked longer than {args.stall_seconds:g}s")
        return 0
    for task in rows:
        print(f"{task.batch_id}\ttask {task.id}\tdoc {task.scope_document_id}\t{task.batched_at}")
    # Non-zero so a cron or a liveness probe can act on it without parsing the output.
    return 1


def cmd_finalize(args: argparse.Namespace) -> int:
    """Release a mock batch so the next poll runs the model. Mock only."""
    from jmfts_batch.providers.mock import MockBatchProvider

    provider = MockBatchProvider(root=Path(args.store), chat=_refuse_to_chat)
    provider.finalize(args.batch_id)
    print(f"{args.batch_id} finalized")
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    provider = build_provider(args)
    provider.cancel(args.batch_id)
    print(f"{args.batch_id} cancelled at {provider.name}")
    return 0


def _refuse_to_chat(request):
    raise AssertionError(
        "finalize does not call the model; it marks the batch releasable and the worker's "
        "next poll runs it"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jmfts-batch-worker", description=__doc__)
    parser.add_argument("--log-level", default="INFO")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_provider_args(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--provider", choices=PROVIDERS, required=True)
        sub.add_argument(
            "--store",
            default=os.environ.get("JMFTS_BATCH_STORE", "/var/lib/jmfts/batches"),
            help="mock provider only: the directory or PVC mount its batches live in",
        )
        sub.add_argument("--llm-url", default="", help="mock provider only")
        sub.add_argument("--worker-id", default=default_worker_id())

    run = subparsers.add_parser("run", help="claim, submit, poll and apply")
    add_provider_args(run)
    run.add_argument("--model", default="", help="defaults to JMFTS_LLM_MODEL")
    run.add_argument(
        "--badge",
        action="append",
        dest="badges",
        default=None,
        help="repeatable; must include the badge JMFTS_TASK_BADGES routes summarize:llm to",
    )
    run.add_argument("--gather-size", type=int, default=DEFAULT_GATHER_SIZE)
    run.add_argument("--poll-seconds", type=float, default=60.0)
    run.add_argument("--stall-seconds", type=float, default=DEFAULT_STALL_SECONDS)
    run.add_argument("--once", action="store_true", help="one pass, then exit")
    run.set_defaults(func=cmd_run)

    status = subparsers.add_parser("status", help="list outstanding batches")
    status.set_defaults(func=cmd_status)

    stalled = subparsers.add_parser("stalled", help="list batches past their turnaround")
    stalled.add_argument("--stall-seconds", type=float, default=DEFAULT_STALL_SECONDS)
    stalled.set_defaults(func=cmd_stalled)

    finalize = subparsers.add_parser("finalize", help="release a mock batch")
    finalize.add_argument("batch_id")
    finalize.add_argument(
        "--store", default=os.environ.get("JMFTS_BATCH_STORE", "/var/lib/jmfts/batches")
    )
    finalize.set_defaults(func=cmd_finalize)

    cancel = subparsers.add_parser("cancel", help="cancel a batch at its provider")
    cancel.add_argument("batch_id")
    add_provider_args(cancel)
    cancel.set_defaults(func=cmd_cancel)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
