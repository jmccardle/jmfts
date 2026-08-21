"""``summarize:llm``, split across the hours a batch takes.

:func:`jmfts_core.rollup_tasks.run_summarize_llm` does five things in one function call:
re-derive the node's text, check whether it still needs an LLM at all, call one, check the
answer fits the embedding window, and store it. A batch provider puts a wall between the
third step and the fourth — the request goes out now and the answer arrives in up to
twenty-four hours, in a different process, possibly on a different host. So the two halves
are separated here.

**THIS MIRRORS THE CORE HANDLER AND IS NOT THE CORE HANDLER.** Deliberately: this package
is a reference implementation and does not get to restructure a tested path in
``jmfts_core`` to suit itself. The risk that buys is the obvious one — the two drift, and a
batched node ends up with a differently-derived summary than a directly-summarized one.
``tests/test_batch_worker.py::TestMirrorsTheCoreHandler`` is what holds them together: it
runs both paths over the same node and asserts the stored ``effective_content`` matches. If
you change ``run_summarize_llm``, that test tells you to change this too.

The one thing NOT copied is the storage itself.
:func:`~jmfts_core.rollup_tasks.store_effective_content` is called directly, so the
embedding, the prefix and the record shape have exactly one definition.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

from jmfts_core.embedding import get_embedding_service
from jmfts_core.ingest_tasks import TaskOutcome
from jmfts_core.models.document import Document
from jmfts_core.models.task_queue import TaskQueue
from jmfts_core.rollup_tasks import (
    EMBED_PREFIX,
    METHOD_CONCATENATED,
    METHOD_LLM_SUMMARY,
    SUMMARIZE_SYSTEM_PROMPT,
    child_ids,
    effective_text,
    store_effective_content,
)

from jmfts_batch.provider import BatchRequest, custom_id_for


@dataclass
class Prepared:
    """What :func:`prepare` decided about one claimed task.

    Exactly one of ``request`` and ``outcome`` is set. ``outcome`` means the node was
    resolved without an LLM and the task should be completed now rather than batched —
    which is not an edge case: a node deferred to ``summarize:llm`` while it was too wide
    can have lost children by the time a batch worker picks it up, and concatenating is
    always preferable to paraphrasing.
    """

    task_id: int
    document_id: int
    request: Optional[BatchRequest] = None
    outcome: Optional[TaskOutcome] = None
    input_tokens: int = 0

    def __post_init__(self) -> None:
        if (self.request is None) == (self.outcome is None):
            raise ValueError(
                f"task {self.task_id}: prepare must either produce a request or resolve "
                "the node, never both and never neither"
            )


def prepare(
    session: Session,
    task: TaskQueue,
    *,
    model: str,
    max_tokens: int,
    temperature: float,
) -> Prepared:
    """The front half of ``summarize:llm``: derive the text, or resolve the node without one.

    Mirrors :func:`jmfts_core.rollup_tasks.run_summarize_llm` up to its LLM call. The text
    is re-derived from the tree rather than read off the task row for the reason that
    handler gives, and which a batch makes sharper: hours pass between the deferral and
    this call, and a summary of a stale concatenation is a quiet wrong answer.
    """
    doc = session.get(Document, task.scope_document_id)
    if doc is None:
        raise ValueError(
            f"{task.task_type} is scoped to document {task.scope_document_id}, which does "
            "not exist"
        )

    children = child_ids(session, doc.id)
    text = effective_text(session, doc.id, own_content=False)
    service = get_embedding_service()
    fit = service.check_fit(text, with_tokens=False, prefix=EMBED_PREFIX)

    if not fit.truncated:
        # It fits now. Spending a batch slot to paraphrase text we could keep verbatim
        # would be worse on both axes — cost and fidelity.
        outcome = store_effective_content(
            session,
            doc,
            embed_text=text,
            method=METHOD_CONCATENATED,
            children=len(children),
            detail={
                "tokens": fit.token_count,
                "window": fit.limit,
                "characters": len(text),
                "note": "the node fit the window before the batch was submitted",
            },
        )
        return Prepared(task_id=task.id, document_id=doc.id, outcome=outcome)

    return Prepared(
        task_id=task.id,
        document_id=doc.id,
        input_tokens=fit.token_count,
        request=BatchRequest(
            custom_id=custom_id_for(task.id),
            model=model,
            system=SUMMARIZE_SYSTEM_PROMPT,
            user=text,
            max_tokens=max_tokens,
            temperature=temperature,
        ),
    )


def apply_summary(
    session: Session,
    task: TaskQueue,
    summary: str,
    *,
    provider: str,
    model: str,
    batch_id: str,
) -> TaskOutcome:
    """The back half: check the returned summary fits, then store it.

    Raises when it does not. That is the core handler's rule and it survives the wall
    intact — embedding an over-length summary is impossible, and storing it unembedded
    would leave a node advertising an ``effective_content`` that no query can reach. The
    worker classifies the raise as permanent, because a model that ignored the instruction
    once will ignore it again on the same input.
    """
    doc = session.get(Document, task.scope_document_id)
    if doc is None:
        raise ValueError(
            f"the batch result for task {task.id} is scoped to document "
            f"{task.scope_document_id}, which no longer exists"
        )

    children = child_ids(session, doc.id)
    service = get_embedding_service()
    fit = service.check_fit(summary, with_tokens=False, prefix=EMBED_PREFIX)
    if fit.truncated:
        raise ValueError(
            f"the summary of document {doc.id} is {fit.token_count} tokens, over the "
            f"{fit.limit}-token embedding window; the model did not summarize"
        )

    return store_effective_content(
        session,
        doc,
        embed_text=summary,
        method=METHOD_LLM_SUMMARY,
        children=len(children),
        text=summary,
        detail={
            "tokens": fit.token_count,
            "window": fit.limit,
            "model": model,
            # How this summary was obtained. `batch_id` is on the queue row too, but the
            # attempt record is what a person reads a year later and the queue row is
            # purgeable, so the provenance is written where it survives.
            "provider": provider,
            "batch_id": batch_id,
        },
    )
