"""Knowledge triples from an ingested tree. ``INGEST_SPEC.md`` 11.4, ``SPRINT_JOBS.md`` S6.

The other half of 11.1's ``summarize / extract_facts`` parity row: path A ran fact
extraction as a pipeline stage and path B had nothing. This is the task, and it calls the
same :func:`jmfts_core.fact_extraction.extract_facts` the stage did, over the same tree.

**A ``TASK_ROWS`` row, not the rollup planner, and the reason is different from
``index:bm25``'s.** ``ingest_tasks.plan_after_probe``'s docstring has said since it was
written that facts "belong to rollup (5.4) and are the settling walk's business". Two
things about the work say otherwise:

* **It is one run over the whole tree, with shared entity and predicate caches.**
  ``extract_facts`` resolves entities across every leaf in one pass so that "the model" in
  chunk 3 and "the model" in chunk 40 land on one entity node. The planner is called per
  node, so under the walk a ``section`` and the ``file`` node above it would each start
  their own run over overlapping leaves — the same text extracted twice, into two caches
  that cannot see each other.
* **Nothing it needs comes from the rollup.** The rollup exists because segmentation and
  summarization read the child sequence and its embeddings, which do not exist until the
  rung below has settled. Fact extraction reads ``content``, which the rung wrote when it
  created the chunk.

So the condition belongs where every other enqueue condition is declared, and Part 4's
table is evaluated once per uploaded file, which is exactly the granularity this wants.

**It is gated on an option, not on a measurement.** Whether to spend an LLM call per chunk
is the caller's choice; the four text usetypes turn it on because path A did, an upload
leaves it off because path B never did it, and either can be overridden per request. That
was a ``TaskRow.enabled_by`` field until Phase 4 and is the ordinary guard term
``options.facts.enabled`` now — a boolean option that must be true was always the
degenerate case of a term, and there was never a second thing the field could express.

**The size floor is a ``forbids`` and not a ``requires``, and the measurement is the
argument.** Probe emits ``char_count`` for ``text`` alone, so required, it would stop fact
extraction on every PDF, ``.docx`` and ``.pptx``. Excluded, it says what a caller means by
a minimum: do not spend the call on a document measured as too small, and do spend it where
nobody measured a size.

**An unconfigured LLM is a SKIP, not a failure.** ``JMFTS_LLM_*`` is blank by default and
the project supports that state — "everything except summarization, RAPTOR, fact
extraction and synthesis works without an LLM". A task that failed here would put every
node it touched into ``settled = 'failed'`` on an appliance that is configured exactly as
documented, which is manufacturing a problem rather than reporting one. The skip carries
the reason and the variable to set, so nothing is hidden.
"""

from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy.orm import Session

from jmfts_core.atoms import COST_LLM, EV_TEXT
from jmfts_core.config import get_settings
from jmfts_core.fact_extraction import extract_facts
from jmfts_core.ingest_tasks import TASK_EXTRACT_FACTS, TaskOutcome, register_task_handler
from jmfts_core.llm_client import aclose_providers
from jmfts_core.models.task_queue import TaskQueue, WRITE_SELF

logger = logging.getLogger(__name__)


@register_task_handler(
    TASK_EXTRACT_FACTS,
    # BOTH loci, and the `@self` one is not redundant. `extract_facts` extracts from the
    # leaves, and falls back to the root's own `content` when the tree has none — a
    # document the rung could not chunk. So the file node's text is a real input on a real
    # path, and declaring only `@subtree` would leave the audit unable to derive that this
    # comes after `extract:text` (which is where the file node's `content` comes from).
    consumes=(f"{EV_TEXT}@self", f"{EV_TEXT}@subtree"),
    # Nothing, in the sense the atom vocabulary means. The product is rows in `triples`
    # and `predicates`, plus entity nodes under the entities root — none of which is
    # evidence on a node of THIS tree, and claiming otherwise would put this task in the
    # audit's derivation for a key nobody could read back.
    produces=(),
    write_mode=WRITE_SELF,
    cost_class=COST_LLM,
)
def run_extract_facts(session: Session, task: TaskQueue) -> TaskOutcome:
    """Extract triples from this file's leaves, or say why it did not."""
    settings = get_settings()
    if not settings.llm_configured:
        return TaskOutcome(
            status="skipped",
            detail={
                "reason": (
                    "no LLM endpoint is configured, and fact extraction needs one. Set "
                    "JMFTS_LLM_BASE_URL and JMFTS_LLM_MODEL, or leave "
                    "options.facts.enabled false for this ingest."
                )
            },
        )

    params = task.params or {}
    t0 = time.monotonic()
    result = _extract(
        document_id=task.scope_document_id,
        session=session,
        llm_model=params.get("llm_model") or None,
        max_facts=params.get("max_facts"),
        confidence_threshold=params.get("confidence_threshold"),
        include_summaries=params.get("include_summaries", True),
    )
    session.flush()

    # `errors` are per-document and non-fatal inside `extract_facts` — one leaf the model
    # answered badly about does not lose the other forty. They are counted and the first is
    # quoted, so a run that quietly produced nothing is distinguishable from one that had
    # nothing to find.
    detail = {
        "documents_processed": result.documents_processed,
        "triples_created": result.total_triples_created,
        "triples_skipped": result.total_skipped,
        "entities_created": result.entities_created,
        "entities_resolved": result.entities_resolved,
        "predicates_created": result.predicates_created,
        "errors": len(result.errors),
        "elapsed_ms": round((time.monotonic() - t0) * 1000, 1),
    }
    if result.errors:
        detail["first_error"] = result.errors[0]
    return TaskOutcome(detail=detail)


def _extract(
    *,
    document_id: int,
    session: Session,
    llm_model,
    max_facts=None,
    confidence_threshold=None,
    include_summaries=True,
):
    """Drive the async :func:`extract_facts` from a synchronous task handler.

    The same shape as :func:`jmfts_core.llm_client.complete_sync`, and for the same reason:
    handlers are synchronous by contract, the LLM client is async-only, and τ pools its
    providers PER EVENT LOOP and closes them explicitly rather than by GC — so a loop torn
    down without ``aclose_providers`` leaks an ``httpx.AsyncClient`` and its socket every
    time.

    A running loop in this thread is refused rather than worked around. ``POST /ingest``
    hands its inline drain to ``asyncio.to_thread`` precisely so this cannot happen; a
    caller that reached here on the loop would be one that skipped that.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError(
            "extract:facts was claimed on a thread that already runs an event loop; the "
            "handler owns one for the duration of the call and cannot nest. Drive the "
            "drain through asyncio.to_thread, as IngestService.ingest_content does."
        )

    async def _once():
        try:
            return await extract_facts(
                document_id=document_id,
                session=session,
                llm_model=llm_model,
                max_facts=max_facts,
                confidence_threshold=confidence_threshold,
                include_summaries=include_summaries,
            )
        finally:
            await aclose_providers()

    return asyncio.run(_once())
