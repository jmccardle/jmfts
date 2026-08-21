"""A batch provider backed by a directory and an ordinary chat endpoint.

It is a mock of the *protocol*, not of the *model*. The summaries it returns are real, from
whatever llama-server or vLLM you point it at. What it fakes is the part that costs money
and takes a day: a submission that returns a durable id, goes quiet, and answers later.

**Nothing is sent to the model until the batch is finalized.** That is the property worth
testing. A batch that has been submitted but not finalized is exactly the state a JMFTS
task sits in as ``batched``, and it is the state in which the interesting failures happen —
the worker is killed, the pod is rescheduled, a second worker adopts the batch. Held open,
this provider lets a test reach all of them without waiting on a provider's SLA.

Finalization happens two ways, and both are wanted:

* :meth:`finalize` — explicit, immediate, and what a test uses, because a test that sleeps
  is a test that is flaky on a loaded machine;
* ``release_after_seconds`` at submit — a wall clock, and what a soak run uses to imitate
  turnaround without anyone driving it.

**The store is a plain directory, so this provider is single-host.** Whoever holds the
volume is the only one who can poll. That is a real narrowing of the queue's own contract —
``TaskQueueRepository.outstanding_batches`` is deliberately not scoped to a worker, so that
any worker able to reach the provider may adopt an abandoned batch, and a local directory
means only one worker ever can. Mount it as a ``ReadWriteMany`` PVC to get that back, or
accept that the mock's batches are recoverable only by a pod that lands on the same volume.
The two commercial providers do not have this limitation because their store is the
provider.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Callable, Iterator, Sequence

from jmfts_core.task_errors import ErrorType

from jmfts_batch.provider import BatchRequest, BatchResult, BatchStatus

logger = logging.getLogger(__name__)

#: What the worker sees as ``task_queue.batch_id``. Prefixed like the real providers'
#: (``batch_``, ``msgbatch_``) so an id in the database says which provider issued it.
BATCH_ID_PREFIX = "mockbatch_"

_REQUESTS = "requests.jsonl"
_RESULTS = "results.jsonl"
_META = "meta.json"
_FINALIZED = "FINALIZED"
_CANCELED = "CANCELED"

#: One request's answer, and how it went. The signature the provider needs from a chat
#: endpoint — see :func:`local_llm_chat` for the one that talks to llama-server.
ChatFn = Callable[[BatchRequest], str]


class MockBatchProvider:
    """Implements :class:`~jmfts_batch.provider.BatchProvider` over a directory."""

    name = "mock"

    def __init__(
        self,
        root: Path,
        chat: ChatFn,
        *,
        max_requests: int = 10_000,
    ):
        #: The scratch directory or PVC mount. Created on first submit.
        self.root = Path(root)
        #: How one request is answered. INJECTED, not defaulted: this provider is the one
        #: place a wrong endpoint would silently produce summaries from the wrong model,
        #: and a caller that has not said which model it wants should not get one.
        self.chat = chat
        self.max_requests = max_requests

    # -- BatchProvider ----------------------------------------------------

    def submit(self, requests: Sequence[BatchRequest]) -> str:
        if not requests:
            raise ValueError("refusing to submit an empty batch")
        if len(requests) > self.max_requests:
            raise ValueError(
                f"batch of {len(requests)} exceeds this provider's cap of {self.max_requests}"
            )

        batch_id = f"{BATCH_ID_PREFIX}{uuid.uuid4().hex}"
        directory = self._dir(batch_id)
        directory.mkdir(parents=True, exist_ok=False)

        with (directory / _REQUESTS).open("w") as handle:
            for request in requests:
                handle.write(json.dumps(_encode(request)) + "\n")

        # Written LAST and atomically. `poll` treats a directory without meta.json as a
        # submission still in progress rather than as a batch of zero requests, so a crash
        # midway through writing the requests cannot present as an empty finished batch.
        _write_atomic(
            directory / _META, json.dumps({"created_at": time.time(), "count": len(requests)})
        )

        logger.info("mock batch %s submitted with %d requests", batch_id, len(requests))
        return batch_id

    def poll(self, batch_id: str) -> BatchStatus:
        directory = self._dir(batch_id)
        if not (directory / _META).exists():
            raise ValueError(f"no such batch: {batch_id}")

        if (directory / _CANCELED).exists():
            return BatchStatus(batch_id, ready=False, dead=True, detail="canceled")

        if (directory / _RESULTS).exists():
            return BatchStatus(batch_id, ready=True, dead=False, detail="ended")

        if not self._released(directory):
            return BatchStatus(batch_id, ready=False, dead=False, detail="in_progress")

        # Released, and nothing has run it yet. THIS is where the model is called, and it
        # is why a poll of a freshly finalized batch takes as long as the work does. A real
        # provider would have done it in the background; imitating that would need a
        # process this package does not run, and hiding the cost would make the mock quiet
        # about exactly the thing it exists to expose.
        self._run(directory)
        return BatchStatus(batch_id, ready=True, dead=False, detail="ended")

    def results(self, batch_id: str) -> Iterator[BatchResult]:
        path = self._dir(batch_id) / _RESULTS
        if not path.exists():
            raise ValueError(f"batch {batch_id} has no results; poll it until ready first")
        with path.open() as handle:
            for line in handle:
                if line.strip():
                    yield _decode(json.loads(line))

    def cancel(self, batch_id: str) -> None:
        directory = self._dir(batch_id)
        if not (directory / _META).exists():
            raise ValueError(f"no such batch: {batch_id}")
        (directory / _CANCELED).touch()

    # -- Mock-only --------------------------------------------------------

    def finalize(self, batch_id: str) -> None:
        """Release the batch now, whatever ``release_after_seconds`` said.

        The next :meth:`poll` runs the model. Separate from ``poll`` so a test can decide
        when the wall comes down without owning the worker's loop.
        """
        directory = self._dir(batch_id)
        if not (directory / _META).exists():
            raise ValueError(f"no such batch: {batch_id}")
        (directory / _FINALIZED).touch()

    def release_after(self, batch_id: str, seconds: float) -> None:
        """Finalize this batch automatically ``seconds`` after it was submitted."""
        directory = self._dir(batch_id)
        meta = json.loads((directory / _META).read_text())
        meta["release_after_seconds"] = seconds
        _write_atomic(directory / _META, json.dumps(meta))

    def _released(self, directory: Path) -> bool:
        if (directory / _FINALIZED).exists():
            return True
        meta = json.loads((directory / _META).read_text())
        delay = meta.get("release_after_seconds")
        if delay is None:
            return False
        return time.time() >= meta["created_at"] + delay

    def _run(self, directory: Path) -> None:
        """Answer every request, then publish the results in one atomic move."""
        lines = []
        with (directory / _REQUESTS).open() as handle:
            for line in handle:
                if not line.strip():
                    continue
                request = _decode_request(json.loads(line))
                try:
                    text = self.chat(request)
                except Exception as exc:  # noqa: BLE001 — becomes this request's result
                    # One request failing is a per-request outcome at both real providers,
                    # not a batch failure. Recorded the same way here so the worker's
                    # handling of it is exercised by the mock.
                    logger.warning("mock batch request %s failed: %s", request.custom_id, exc)
                    lines.append(
                        json.dumps(
                            {
                                "custom_id": request.custom_id,
                                "error": f"{type(exc).__name__}: {exc}",
                                "error_type": ErrorType.RETRYABLE.value,
                            }
                        )
                    )
                    continue
                lines.append(json.dumps({"custom_id": request.custom_id, "text": text}))

        # Atomic: a reader sees either no results file or the whole thing. A partially
        # written one would let `poll` report ready over a truncated batch, and the tasks
        # missing from it would sit `batched` with nothing left to deliver them.
        _write_atomic(directory / _RESULTS, "\n".join(lines) + "\n")

    def _dir(self, batch_id: str) -> Path:
        if not batch_id.startswith(BATCH_ID_PREFIX) or "/" in batch_id:
            raise ValueError(f"not a mock batch id: {batch_id!r}")
        return self.root / batch_id


def local_llm_chat(
    base_url: str,
    *,
    model_timeout: float,
    api_key: str = "",
    disable_thinking: bool = False,
) -> ChatFn:
    """A :data:`ChatFn` that posts to an OpenAI-compatible ``/v1/chat/completions``.

    Which is what llama-server, vLLM and Ollama all serve, so the same function covers
    every local runner this appliance is pointed at.

    ``disable_thinking`` sends ``chat_template_kwargs={"enable_thinking": False}``, exactly
    as :func:`jmfts_core.rollup_tasks.summarize_span` does. It is not cosmetic. A reasoning
    model spends ``max_tokens`` on its trace and returns a truncated fragment as the
    summary — measured against llama-server on 2026-08-20, a 200-token budget came back as
    the two words ``The appliance``. That fragment is not an error at any layer: it embeds,
    it stores, and the node ends up advertising an ``effective_content`` that says nothing.
    """
    import httpx

    from jmfts_core.llm_utils import extract_llm_text

    root = base_url.rstrip("/")
    # No key, no header — the same rule `rollup_tasks.summarize_span` follows, and for the
    # same reason: a llama-server on the LAN wants none and a metered endpoint refuses
    # without one.
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    def chat(request: BatchRequest) -> str:
        payload = {
            "model": request.model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        if disable_thinking:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        with httpx.Client(timeout=model_timeout) as client:
            response = client.post(f"{root}/v1/chat/completions", json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
        return extract_llm_text(data["choices"][0])

    return chat


def _encode(request: BatchRequest) -> dict:
    return {
        "custom_id": request.custom_id,
        "model": request.model,
        "system": request.system,
        "user": request.user,
        "max_tokens": request.max_tokens,
        "temperature": request.temperature,
    }


def _decode_request(raw: dict) -> BatchRequest:
    return BatchRequest(**raw)


def _decode(raw: dict) -> BatchResult:
    error_type = raw.get("error_type")
    return BatchResult(
        custom_id=raw["custom_id"],
        text=raw.get("text"),
        error=raw.get("error"),
        error_type=ErrorType(error_type) if error_type else None,
    )


def _write_atomic(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    os.replace(tmp, path)
