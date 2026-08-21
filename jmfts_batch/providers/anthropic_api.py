"""Anthropic's Message Batches API. One call to submit, a URL to stream back.

Verified against the current guide on 2026-08-20: 100,000 requests or 256 MB per batch,
most batches finish inside an hour, anything unfinished at 24 hours expires, results stay
downloadable for 29 days, 50% of the synchronous price.

**Two differences from OpenAI that reach the design.**

*Submitting is one call.* The requests go inline in the create body — there is no file to
upload first. That makes the JMFTS handoff's window as narrow as it can be without an
idempotency key: one HTTP response, then ``mark_batched``.

*There is no batch-level failure.* ``processing_status`` goes ``in_progress`` →
``canceling`` → ``ended`` and nothing else. A batch that went badly still ``ended``; every
request inside it reports its own outcome. So :attr:`BatchStatus.dead` is never set by this
adapter, and everything a caller needs to know about a bad batch arrives per-request.

Also no ``metadata`` field on a batch, which is what makes the submit-crash window
irreducible here in a way it is not at OpenAI.

No ``anthropic`` SDK, for the reason given in the OpenAI adapter.
"""

from __future__ import annotations

import json
import logging
from typing import Iterator, Optional, Sequence

import httpx

from jmfts_core.task_errors import ErrorType

from jmfts_batch.provider import BatchRequest, BatchResult, BatchStatus

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.anthropic.com"

#: Pinned. The API is versioned by header, so leaving it to the server's default would let
#: a response shape change under a worker that is only redeployed every few months.
API_VERSION = "2023-06-01"

#: Documented cap on one batch. The 256 MB limit is not checked here — the request body
#: would have to be measured after serialisation, and a batch that large is orders of
#: magnitude past what the reservation cost makes sensible anyway (see README.md).
MAX_REQUESTS = 100_000

#: ``processing_status`` value meaning every request has finished and results are ready.
_ENDED = "ended"

#: How one request ended. ``canceled`` and ``expired`` are explicitly not billed, so
#: requeueing them costs nothing and is the right answer.
_RESULT_ERROR_TYPES = {
    "canceled": ErrorType.RETRYABLE,
    "expired": ErrorType.RETRYABLE,
}

#: Anthropic's own error taxonomy, for ``result.type == "errored"``.
_API_ERROR_TYPES = {
    "invalid_request_error": ErrorType.PERMANENT,
    "authentication_error": ErrorType.PERMANENT,
    "permission_error": ErrorType.PERMANENT,
    "not_found_error": ErrorType.PERMANENT,
    "request_too_large": ErrorType.PERMANENT,
    "rate_limit_error": ErrorType.RETRYABLE,
    "api_error": ErrorType.RETRYABLE,
    "overloaded_error": ErrorType.RETRYABLE,
    "timeout_error": ErrorType.TIMEOUT,
}


class AnthropicBatchProvider:
    """Implements :class:`~jmfts_batch.provider.BatchProvider` against Anthropic."""

    name = "anthropic"
    max_requests = MAX_REQUESTS

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 120.0,
    ):
        if not api_key:
            raise ValueError("AnthropicBatchProvider needs an API key")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._headers = {
            "x-api-key": api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }

    def submit(self, requests: Sequence[BatchRequest]) -> str:
        if not requests:
            raise ValueError("refusing to submit an empty batch")
        if len(requests) > self.max_requests:
            raise ValueError(
                f"batch of {len(requests)} exceeds Anthropic's cap of {self.max_requests}"
            )

        body = {"requests": [_as_request(request) for request in requests]}
        with httpx.Client(timeout=self.timeout, headers=self._headers) as client:
            response = client.post(f"{self.base_url}/v1/messages/batches", json=body)
            response.raise_for_status()
            batch_id = response.json()["id"]

        logger.info("anthropic batch %s created with %d requests", batch_id, len(requests))
        return batch_id

    def poll(self, batch_id: str) -> BatchStatus:
        batch = self._retrieve(batch_id)
        status = batch["processing_status"]
        counts = batch.get("request_counts") or {}
        detail = (
            f"{status} "
            f"{counts.get('succeeded', 0)} succeeded, "
            f"{counts.get('errored', 0)} errored, "
            f"{counts.get('processing', 0)} processing"
        )
        # `dead` is never True here — see the module docstring. A batch always ends.
        return BatchStatus(batch_id, ready=status == _ENDED, dead=False, detail=detail)

    def results(self, batch_id: str) -> Iterator[BatchResult]:
        with httpx.Client(timeout=self.timeout, headers=self._headers) as client:
            url = f"{self.base_url}/v1/messages/batches/{batch_id}/results"
            with client.stream("GET", url) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if line.strip():
                        yield _as_result(json.loads(line))

    def cancel(self, batch_id: str) -> None:
        with httpx.Client(timeout=self.timeout, headers=self._headers) as client:
            response = client.post(f"{self.base_url}/v1/messages/batches/{batch_id}/cancel")
            response.raise_for_status()

    def _retrieve(self, batch_id: str) -> dict:
        with httpx.Client(timeout=self.timeout, headers=self._headers) as client:
            response = client.get(f"{self.base_url}/v1/messages/batches/{batch_id}")
            response.raise_for_status()
            return response.json()


def _as_request(request: BatchRequest) -> dict:
    """One entry in the create body. ``system`` is top-level, not a message."""
    return {
        "custom_id": request.custom_id,
        "params": {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "system": request.system,
            "messages": [{"role": "user", "content": request.user}],
        },
    }


def _as_result(raw: dict) -> BatchResult:
    custom_id = raw["custom_id"]
    result = raw["result"]
    result_type = result["type"]

    if result_type == "succeeded":
        return BatchResult(custom_id=custom_id, text=_text_of(result["message"]))

    if result_type == "errored":
        # Two levels of `error`: the envelope, then the API error itself.
        inner = (result.get("error") or {}).get("error") or {}
        api_type = inner.get("type", "api_error")
        return BatchResult(
            custom_id=custom_id,
            error=f"{api_type}: {inner.get('message', '')}"[:1000],
            error_type=_API_ERROR_TYPES.get(api_type, ErrorType.RETRYABLE),
        )

    error_type = _RESULT_ERROR_TYPES.get(result_type)
    if error_type is None:
        raise ValueError(
            f"result {custom_id} has an unknown type {result_type!r}; refusing to guess "
            "whether this task should be retried"
        )
    return BatchResult(
        custom_id=custom_id,
        error=f"the provider reported this request as {result_type}",
        error_type=error_type,
    )


def _text_of(message: dict) -> Optional[str]:
    """The first text block. Anthropic returns a list of content blocks, not a string."""
    for block in message.get("content", []):
        if block.get("type") == "text":
            return block["text"]
    raise ValueError(
        f"message {message.get('id')} succeeded but carries no text block; there is "
        "nothing to store as a summary"
    )
