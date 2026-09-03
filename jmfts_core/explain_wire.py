"""Server-side adapters from the ``EXPLAIN``/``ANALYZE`` dataclasses to their wire models.

These three functions used to be ``classmethod``s on the contracts in
``jmfts_client.contracts.explain``. They moved here when the contracts became their own
distribution, and the move is not cosmetic: they take :class:`~jmfts_core.ingest_tasks.
ExplainedPlan`, :class:`~jmfts_core.ingest_tasks.ExplainedTask` and
:class:`~jmfts_core.probe.FormatDetection`, which are SERVER types. A contract module that
imported them would drag the scheduler and the prober into a client whose whole purpose is
to carry neither — and, once the contracts sat outside ``jmfts_core``, would also close an
import cycle (``ingest_tasks`` → contracts → ``ingest_tasks``).

The direction of the dependency is the rule: the server knows the wire, the wire does not
know the server. ``tests/test_client_codegen.py::test_client_package_does_not_import_the_server``
holds it.
"""

from __future__ import annotations

from jmfts_client.contracts.explain import (
    AnalyzedFile,
    ExplainedTaskResponse,
    ExplainIngestResponse,
)

from jmfts_core.ingest_tasks import ExplainedPlan, ExplainedTask
from jmfts_core.probe import FormatDetection


def task_response(task: ExplainedTask) -> ExplainedTaskResponse:
    """The wire form of one explained task row."""
    return ExplainedTaskResponse(
        task=task.task,
        outcome=task.outcome,
        if_condition_holds=task.if_condition_holds,
        reason=task.reason,
        write_mode=task.write_mode,
        scope=task.scope,
        after=list(task.after),
        after_any=list(task.after_any),
        requires=list(task.requires),
        forbids=list(task.forbids),
        params=task.params,
    )


def explain_response_from_plan(plan: ExplainedPlan) -> ExplainIngestResponse:
    """The wire form of what :func:`~jmfts_core.ingest_tasks.explain_plan` decided."""
    return ExplainIngestResponse(
        format=plan.format,
        prober_available=plan.prober_available,
        patterns_known=plan.patterns_known,
        patterns_source=plan.patterns_source,
        patterns_ignored=list(plan.patterns_ignored),
        options=plan.options,
        tasks=[task_response(task) for task in plan.tasks],
    )


def analyzed_file_from_detection(
    detection: FormatDetection, *, filename: str, byte_size: int, content_hash: str
) -> AnalyzedFile:
    """The wire form of what :func:`~jmfts_core.probe.detect_format` found in the bytes."""
    return AnalyzedFile(
        filename=filename,
        byte_size=byte_size,
        content_hash=content_hash,
        declared_mime=detection.declared_mime,
        detected_mime=detection.detected_mime,
        detected_by=detection.detected_by,
        mime_agrees=detection.mime_agrees,
    )
