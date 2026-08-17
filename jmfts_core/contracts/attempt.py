"""Ingest attempt record — the durable log of what was tried on a node.

``INGEST_SPEC.md`` Part 3.4. ``execute_pipeline`` has always computed this record and
then thrown it away: a ``list[StageResult]`` returned in the HTTP body and never written
down, so the node kept ``{"section_count": n}`` and no memory of how it got there. This
contract is that record with the fields the scheduling and correction model need. It is
persisted into ``structured_content["attempts"]`` on the node the task was scoped to.

The record is recursive (spec 3.2) — a file node, a section node and a sheet node all
carry the same shape, scoped to themselves. Nothing here is file-specific, and nothing
here knows about pipelines: ``task`` is a free string so a stage name today and a queued
task name tomorrow land in the same log.

Three fields are nullable *because the machinery that fills them does not exist yet*:
``task_id`` (Part 5's queue), ``write_mode`` (5.3) and ``superseded_by`` (6.2). A null
there says "that step is not built"; inventing a plausible value would make the log lie
about a subsystem that has not shipped.
"""

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal, Mapping, Optional

from pydantic import BaseModel, Field, model_validator

# Spec 3.4. `pending` and `running` describe a task the queue (Part 5) has taken
# ownership of but not finished; nothing in the synchronous pipeline emits them yet,
# and they are declared here so the queue does not have to widen the record later.
AttemptStatus = Literal["pending", "running", "completed", "skipped", "failed"]

#: Statuses that mean the attempt is over — the only ones that can carry ``finished_at``.
TERMINAL_STATUSES = frozenset({"completed", "skipped", "failed"})

# Triskelion's ``ErrorType`` values (vdo_core/task_error_handling.py), quoted rather than
# imported: retry policy is decided by classification, not by a string match on the
# message. Nothing populates this until the queue lands with the classifier.
AttemptErrorType = Literal["retryable", "permanent", "timeout", "dependency"]


def param_fingerprint(params: Mapping[str, Any]) -> str:
    """Stable hash of the parameters that affect a task's output.

    Spec 6.1 keys the re-run diff on ``(task_name, param_fingerprint)``, so this value is
    compared against fingerprints written by *other processes, on other days*. That rules
    out anything derived from :func:`hash` (randomised per interpreter by
    ``PYTHONHASHSEED``) and anything sensitive to dict insertion order, which a JSON
    request body controls. Canonical JSON with ``sort_keys=True`` gives both properties:
    key order is normalised recursively, and the byte encoding is fixed.

    Deliberately strict: a value JSON cannot represent raises ``TypeError`` here rather
    than being coerced to ``repr``, because two different objects with the same ``repr``
    would fingerprint identically and silently suppress a re-run that should happen.
    """
    canonical = json.dumps(
        dict(params),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class AttemptRecord(BaseModel):
    """One attempt at one task, scoped to one node. Spec 3.4."""

    task: str = Field(description="Task name, e.g. 'chunk' or 'structure:declared'")
    task_id: Optional[int] = Field(
        default=None, description="Queue row id; null until the Part 5 queue exists."
    )
    status: AttemptStatus = Field(description="pending, running, completed, skipped, failed")
    attempt: int = Field(default=1, ge=1, description="1-based counter per (node, task)")
    rung: Optional[str] = Field(
        default=None,
        description=(
            "Structure rung that produced the result (spec 3.5: declared, inferred, "
            "semantic, flat). Null for tasks that produce no structure."
        ),
    )
    scope_document_id: int = Field(description="The node this task was scoped to")
    write_mode: Optional[str] = Field(
        default=None, description="Declared write mode; null until spec 5.3 exists."
    )
    params: dict = Field(default_factory=dict, description="Parameters that affect output")
    param_fingerprint: str = Field(description="param_fingerprint(params) — the 6.1 diff key")
    started_at: Optional[datetime] = Field(
        default=None, description="Measured UTC start; null only while status='pending'"
    )
    finished_at: Optional[datetime] = Field(
        default=None, description="Measured UTC end; set exactly for terminal statuses"
    )
    detail: dict = Field(default_factory=dict, description="What the task did or looked for")
    produced: Optional[dict] = Field(
        default=None,
        description="Undo record: {'node_count': n, 'child_ids': [...]}. Null when the "
        "task produces no nodes — an empty dict would claim it produced none.",
    )
    superseded_by: Optional[int] = Field(
        default=None, description="Attempt that replaced this one; null until spec 6.2."
    )
    error: Optional[str] = None
    error_type: Optional[AttemptErrorType] = Field(
        default=None, description="Triskelion ErrorType; null until the queue classifies."
    )

    @model_validator(mode="after")
    def _check_record(self) -> "AttemptRecord":
        # Spec 3.4's central distinction: `skipped` means the task was NEVER ATTEMPTED,
        # and a heuristic that ran and matched nothing is `completed` with a detail saying
        # what it looked for. The only thing that keeps those two apart in a log a human
        # reads months later is the reason, so a reasonless `skipped` is rejected at
        # construction rather than written down as an unexplained gap.
        if self.status == "skipped":
            reason = self.detail.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError(
                    f"attempt {self.task!r} is 'skipped' but carries no detail.reason; "
                    "a task that ran and found nothing is 'completed' with a detail "
                    "explaining what it looked for"
                )

        # Timing. `pending` is the one status with nothing to measure yet; every other
        # status means the work started, and every terminal status means it stopped.
        if self.status == "pending":
            if self.started_at is not None or self.finished_at is not None:
                raise ValueError("attempt is 'pending' but carries timestamps")
        else:
            if self.started_at is None:
                raise ValueError(
                    f"attempt {self.task!r} has status {self.status!r} but no "
                    "started_at; the time must be measured, not inferred"
                )
            if self.status in TERMINAL_STATUSES and self.finished_at is None:
                raise ValueError(
                    f"attempt {self.task!r} finished as {self.status!r} but has no finished_at"
                )
            if self.status not in TERMINAL_STATUSES and self.finished_at is not None:
                raise ValueError("attempt is still running but carries finished_at")

        # Timezone-aware UTC, always. A naive timestamp in a JSONB log is unreadable a
        # year later — it does not say which clock it came from — so awareness is
        # required and everything is normalised to UTC for comparability.
        for name in ("started_at", "finished_at"):
            value = getattr(self, name)
            if value is None:
                continue
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
            setattr(self, name, value.astimezone(timezone.utc))

        if self.started_at and self.finished_at and self.finished_at < self.started_at:
            raise ValueError("finished_at precedes started_at")
        return self

    def to_jsonb(self) -> dict:
        """Render for storage in ``structured_content['attempts']`` (ISO-8601 datetimes)."""
        return self.model_dump(mode="json")
