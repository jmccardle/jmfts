"""What a batch provider has to do, reduced to four calls.

The two commercial providers this package implements differ in almost every surface
detail — one uploads a file and then creates a batch, the other posts the requests inline;
one calls the field ``status``, the other ``processing_status``; one reports
``completed``/``failed`` counts, the other
``succeeded``/``errored``/``canceled``/``expired``. None of that reaches the worker. What
they agree on is the only thing the worker needs:

1. a submission returns an id that outlives the caller;
2. the id can be polled;
3. results come back carrying the caller's own ``custom_id``;
4. a submitted batch can be cancelled.

**``custom_id`` is why this package needs no bookkeeping of its own.** Both providers
carry it from request to result untouched, so the worker sets it to the JMFTS task id and
the download step is a primary-key lookup. The scripts this was modelled on
(``/storage/ModernBERT-NLI-advanced/scripts/batch/``) write a ``_metadata.jsonl`` beside
every batch to map results back, because their sample ids are not database keys. Ours are.

Anthropic constrains ``custom_id`` to ``^[a-zA-Z0-9_-]{1,64}$``, which is the tighter of
the two rules, so :func:`custom_id_for` targets it and OpenAI accepts the result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator, Optional, Protocol, Sequence

from jmfts_core.task_errors import ErrorType

#: Anthropic's rule. Applied to both providers so one id format works everywhere.
CUSTOM_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

#: The prefix on a JMFTS task's ``custom_id``. Present so a result whose id did not come
#: from this appliance is rejected rather than parsed into a plausible task id.
CUSTOM_ID_PREFIX = "task-"


def custom_id_for(task_id: int) -> str:
    """The ``custom_id`` a JMFTS task travels under."""
    return f"{CUSTOM_ID_PREFIX}{task_id}"


def task_id_from(custom_id: str) -> int:
    """Recover the task id, or raise.

    Raising is the point. A result whose ``custom_id`` this appliance did not write means
    the batch is not the batch we think it is, and guessing a task id from it would apply
    one node's summary to another node.
    """
    if not custom_id.startswith(CUSTOM_ID_PREFIX):
        raise ValueError(
            f"custom_id {custom_id!r} does not start with {CUSTOM_ID_PREFIX!r}; this "
            "result did not come from a JMFTS submission"
        )
    return int(custom_id[len(CUSTOM_ID_PREFIX) :])


@dataclass(frozen=True)
class BatchRequest:
    """One chat completion, in neither provider's vocabulary.

    ``system`` is a separate field rather than the first message because that is the one
    structural difference between the two APIs: OpenAI wants the system prompt as a message
    in the list, Anthropic wants it as a top-level parameter. Absorbing that is the whole
    job of the two adapters.
    """

    custom_id: str
    model: str
    system: str
    user: str
    max_tokens: int
    temperature: float

    def __post_init__(self) -> None:
        if not CUSTOM_ID_PATTERN.match(self.custom_id):
            raise ValueError(
                f"custom_id {self.custom_id!r} does not match {CUSTOM_ID_PATTERN.pattern}; "
                "Anthropic rejects the whole batch for one bad id"
            )


@dataclass(frozen=True)
class BatchStatus:
    """Where a submitted batch is.

    Three states, not the union of both providers' vocabularies:

    * neither flag — still working, poll again later;
    * ``ready`` — results may be read now;
    * ``dead`` — this id will never produce results, so every task parked against it has
      to be resolved from here rather than by waiting.

    ``dead`` exists because OpenAI has batch-level terminal failures (``failed``,
    ``expired``, ``cancelled``) and Anthropic does not — its batch always reaches ``ended``
    and reports per-request outcomes instead. A worker written against Anthropic alone
    would hang forever on a failed OpenAI batch.
    """

    batch_id: str
    ready: bool
    dead: bool
    detail: str = ""

    def __post_init__(self) -> None:
        if self.ready and self.dead:
            raise ValueError(f"batch {self.batch_id} cannot be both ready and dead")


@dataclass(frozen=True)
class BatchResult:
    """What came back for one request.

    Exactly one of ``text`` and ``error`` is set. ``error_type`` is a JMFTS
    :class:`~jmfts_core.task_errors.ErrorType` rather than the provider's own word for it,
    because each adapter is the only place that knows whether its provider's failure is
    worth retrying — and mapping it once, there, is cheaper than teaching the worker four
    vocabularies.
    """

    custom_id: str
    text: Optional[str] = None
    error: Optional[str] = None
    error_type: Optional[ErrorType] = None

    def __post_init__(self) -> None:
        if (self.text is None) == (self.error is None):
            raise ValueError(
                f"result {self.custom_id} must carry exactly one of text and error, "
                f"got text={self.text!r} error={self.error!r}"
            )
        if self.error is not None and self.error_type is None:
            raise ValueError(
                f"result {self.custom_id} failed without an error_type; the queue cannot "
                "decide whether to retry it"
            )


class BatchProvider(Protocol):
    """The four calls. Implemented three times in ``jmfts_batch/providers/``."""

    #: Written into the attempt record, so a person reading a node's history a year later
    #: can tell which provider produced its summary.
    name: str

    #: The provider's own cap on one batch. The worker's gather size is separately
    #: configurable and is normally far below this — see ``BatchWorker.gather_size``.
    max_requests: int

    def submit(self, requests: Sequence[BatchRequest]) -> str:
        """Hand the requests to the provider and return its batch id.

        MUST NOT return until the provider has accepted the batch. The worker writes
        ``batched`` on the strength of this return value, and a batch id for a submission
        that did not happen is a set of rows nothing will ever poll.
        """
        ...

    def poll(self, batch_id: str) -> BatchStatus: ...

    def results(self, batch_id: str) -> Iterator[BatchResult]:
        """Stream the results. Only valid once :meth:`poll` reported ``ready``."""
        ...

    def cancel(self, batch_id: str) -> None: ...
