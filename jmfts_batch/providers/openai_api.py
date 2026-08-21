"""OpenAI's Batch API. Two calls to submit, a file to read back.

Verified against the current guide on 2026-08-20: 50,000 requests and 200 MB per batch,
``completion_window`` accepts only ``"24h"``, 50% of the synchronous price.

**Submitting takes two calls and only the second one costs money.** ``POST /v1/files``
uploads the JSONL and returns a file id; ``POST /v1/batches`` turns that file into work.
An orphaned upload — a worker that died between the two — bills nothing and expires on its
own, so the window the JMFTS handoff actually cares about is the second call's, exactly as
wide as Anthropic's single one.

``metadata`` is the one thing this provider has that Anthropic does not: up to 16
key-value pairs on the batch, readable from ``GET /v1/batches``. That is enough to make the
submit-crash recoverable by lookup instead of by resubmission — see ``README.md``. This
adapter writes the pairs; nothing reads them back yet, and that is on purpose. Recovery is
a decision about spending money twice, not a default.

No ``openai`` SDK. Four HTTP calls do not justify a dependency in a package whose whole
point is to be readable as a reference.
"""

from __future__ import annotations

import io
import json
import logging
from typing import Iterator, Optional, Sequence

import httpx

from jmfts_core.llm_utils import extract_llm_text
from jmfts_core.task_errors import ErrorType

from jmfts_batch.provider import BatchRequest, BatchResult, BatchStatus

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.openai.com"

#: Documented cap on one batch.
MAX_REQUESTS = 50_000

#: The only value the API accepts today.
COMPLETION_WINDOW = "24h"

#: ``status`` values that mean results are downloadable.
_READY = frozenset({"completed"})

#: ``status`` values that mean this id will never produce results. ``cancelling`` is NOT
#: here — it is still in flight and becomes ``cancelled`` shortly.
_DEAD = frozenset({"failed", "expired", "cancelled"})


class OpenAIBatchProvider:
    """Implements :class:`~jmfts_batch.provider.BatchProvider` against OpenAI."""

    name = "openai"
    max_requests = MAX_REQUESTS

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 120.0,
        metadata: Optional[dict] = None,
    ):
        if not api_key:
            raise ValueError("OpenAIBatchProvider needs an API key")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        #: Written onto every batch this worker creates. See the module docstring.
        self.metadata = dict(metadata or {})
        self._headers = {"Authorization": f"Bearer {api_key}"}

    def submit(self, requests: Sequence[BatchRequest]) -> str:
        if not requests:
            raise ValueError("refusing to submit an empty batch")
        if len(requests) > self.max_requests:
            raise ValueError(
                f"batch of {len(requests)} exceeds OpenAI's cap of {self.max_requests}"
            )

        payload = io.BytesIO(
            "".join(json.dumps(_as_line(request)) + "\n" for request in requests).encode()
        )

        with httpx.Client(timeout=self.timeout, headers=self._headers) as client:
            upload = client.post(
                f"{self.base_url}/v1/files",
                files={"file": ("jmfts_batch.jsonl", payload, "application/jsonl")},
                data={"purpose": "batch"},
            )
            upload.raise_for_status()
            input_file_id = upload.json()["id"]
            logger.info("uploaded %d requests as %s", len(requests), input_file_id)

            # The billable call. Everything before it is recoverable for free.
            created = client.post(
                f"{self.base_url}/v1/batches",
                json={
                    "input_file_id": input_file_id,
                    "endpoint": "/v1/chat/completions",
                    "completion_window": COMPLETION_WINDOW,
                    "metadata": self.metadata,
                },
            )
            created.raise_for_status()
            batch_id = created.json()["id"]

        logger.info("openai batch %s created from %s", batch_id, input_file_id)
        return batch_id

    def poll(self, batch_id: str) -> BatchStatus:
        batch = self._retrieve(batch_id)
        status = batch["status"]
        counts = batch.get("request_counts") or {}
        detail = (
            f"{status} "
            f"{counts.get('completed', 0)}/{counts.get('total', 0)} completed, "
            f"{counts.get('failed', 0)} failed"
        )
        return BatchStatus(
            batch_id,
            ready=status in _READY,
            dead=status in _DEAD,
            detail=detail,
        )

    def results(self, batch_id: str) -> Iterator[BatchResult]:
        batch = self._retrieve(batch_id)
        # Both files are read. `error_file_id` holds requests that never reached the model,
        # and skipping it would leave their tasks parked against a batch that has ended —
        # invisible to the lease, waiting on results that exist and say "no".
        for key in ("output_file_id", "error_file_id"):
            file_id = batch.get(key)
            if not file_id:
                continue
            yield from self._read_file(file_id)

    def cancel(self, batch_id: str) -> None:
        with httpx.Client(timeout=self.timeout, headers=self._headers) as client:
            response = client.post(f"{self.base_url}/v1/batches/{batch_id}/cancel")
            response.raise_for_status()

    def _retrieve(self, batch_id: str) -> dict:
        with httpx.Client(timeout=self.timeout, headers=self._headers) as client:
            response = client.get(f"{self.base_url}/v1/batches/{batch_id}")
            response.raise_for_status()
            return response.json()

    def _read_file(self, file_id: str) -> Iterator[BatchResult]:
        with httpx.Client(timeout=self.timeout, headers=self._headers) as client:
            with client.stream("GET", f"{self.base_url}/v1/files/{file_id}/content") as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if line.strip():
                        yield _as_result(json.loads(line))


def _as_line(request: BatchRequest) -> dict:
    """One JSONL line. The system prompt is a message here; Anthropic takes it separately."""
    return {
        "custom_id": request.custom_id,
        "method": "POST",
        "url": "/v1/chat/completions",
        "body": {
            "model": request.model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            "max_completion_tokens": request.max_tokens,
            "temperature": request.temperature,
        },
    }


def _as_result(raw: dict) -> BatchResult:
    custom_id = raw["custom_id"]

    if raw.get("error"):
        error = raw["error"]
        return BatchResult(
            custom_id=custom_id,
            error=f"{error.get('code')}: {error.get('message')}",
            # No HTTP status to classify by. Permanent is the safe reading: this request
            # never reached the model, and the usual cause is the request itself.
            error_type=ErrorType.PERMANENT,
        )

    response = raw.get("response") or {}
    status_code = response.get("status_code")
    if status_code != 200:
        return BatchResult(
            custom_id=custom_id,
            error=f"HTTP {status_code}: {json.dumps(response.get('body'))[:500]}",
            error_type=_classify(status_code),
        )

    return BatchResult(custom_id=custom_id, text=extract_llm_text(response["body"]["choices"][0]))


def _classify(status_code: Optional[int]) -> ErrorType:
    """Same policy as ``jmfts_core.task_errors`` applies to a live HTTP call.

    Spelled again rather than imported because that function classifies an
    ``httpx.HTTPStatusError`` from a request this process made, and these are status codes
    read out of a file describing a request made a day ago by someone else.
    """
    if status_code == 408:
        return ErrorType.TIMEOUT
    if status_code == 429 or (status_code is not None and status_code >= 500):
        return ErrorType.RETRYABLE
    return ErrorType.PERMANENT
