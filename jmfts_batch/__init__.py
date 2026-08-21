"""A reference batch worker for JMFTS. Separate from ``jmfts_core`` on purpose.

It consumes ``summarize:llm`` tasks through an external batch provider at roughly half the
synchronous price, using only APIs ``jmfts_core`` already exposes: ``claim_next``,
``mark_running``, ``mark_batched``, ``outstanding_batches``, ``with_batch_lock``,
``batched_tasks``, ``stalled_batches``, ``touch_heartbeat``, ``complete``, ``fail``, and
``store_effective_content``.

**The dependency runs one way.** ``jmfts_batch`` imports ``jmfts_core``; nothing in
``jmfts_core`` imports ``jmfts_batch``. Deleting this directory leaves the appliance
working, with ``summarize:llm`` served the direct way. That is what makes it a reference
implementation rather than a feature: it demonstrates the ``batched`` state machine against
three providers, and a deployment with different provider requirements is expected to copy
it rather than configure it.

See ``README.md`` for what it does not do.
"""

from jmfts_batch.provider import (
    BatchProvider,
    BatchRequest,
    BatchResult,
    BatchStatus,
    custom_id_for,
    task_id_from,
)
from jmfts_batch.worker import BatchWorker

__all__ = [
    "BatchProvider",
    "BatchRequest",
    "BatchResult",
    "BatchStatus",
    "BatchWorker",
    "custom_id_for",
    "task_id_from",
]
