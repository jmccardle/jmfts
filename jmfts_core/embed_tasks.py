"""``embed`` — the queued task that gives one node its vectors. ``INGEST_SPEC.md`` 5.4.

One handler, twelve lines of work, and it is the most expensive task type there is. Every
chunk the structure rungs write carries one, and it is the transformer forward pass that
used to run inline inside those rungs (see :data:`~jmfts_core.ingest_tasks.TASK_EMBED` for
why it moved out).

**It is scoped to the node it embeds, and the write mode is ``self``.** So the embed tasks
of one document are all claimable at once — ``claim_next``'s reservation table only makes a
``self`` task conflict with another ``self`` on the SAME node — and a document's chunks
embed across as many workers as the fleet has. That is the throughput change; the routing
change is that ``embed`` is one row in :data:`~jmfts_core.task_routing.EMBEDDING_POLICY`
rather than a badge on a handler that also splits text.

**It does not choose where the model runs.** ``DocumentRepository.embed_document`` asks
:func:`jmfts_core.embedder.get_embedder`, which is local unless ``JMFTS_RUNNER_URL`` is
set. A worker pointed at a runner runs this handler without ever loading weights.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from jmfts_core.embedder import get_embedder
from jmfts_core.ingest_tasks import TASK_EMBED, TaskOutcome, register_task_handler
from jmfts_core.models.document import Document
from jmfts_core.models.task_queue import TaskQueue
from jmfts_core.repositories.document import DocumentRepository


@register_task_handler(TASK_EMBED)
def run_embed(session: Session, task: TaskQueue) -> TaskOutcome:
    """Write this node's document vector, and its token vectors when asked for them.

    **Empty content RAISES.** The task was enqueued for text, and a node that has none is
    not a cheap no-op to skip past — it is a chunk whose prose went missing between being
    written and being embedded, and settling over it would publish a leaf that no query can
    reach with nothing recording why. ``_write_chunks`` never creates a chunk from an empty
    body, so reaching this is a fact about the tree having changed underneath the queue.

    **Over-window text raises too**, from the embedding service, and the classifier calls
    ``TextTooLongError`` PERMANENT (it is a ``ValueError``). That is the same outcome the
    inline path had and the same cue: chunk it. ``chunk_text`` bounds every piece by the
    tokenizer (KNOWN-DEFECTS D7), so a chunk that does not fit means the two sides measured
    it differently — which is what ``jmfts_core.embedder`` raises separately about.
    """
    doc = session.get(Document, task.scope_document_id)
    if doc is None:
        raise ValueError(
            f"embed is scoped to document {task.scope_document_id}, which does not exist"
        )

    if not (doc.content or "").strip():
        raise ValueError(
            f"embed is scoped to document {doc.id}, which carries no content; the task was "
            "enqueued for text that is not on the node"
        )

    # Default True: the token/maxsim vectors are the reason JMFTS embeds at all, and a
    # caller that wants the cheap document-vector-only path says so on the row. Reading it
    # as a param rather than hardcoding it is what lets `false` be a queued decision — a
    # container node whose text is not its own content wants exactly that (11.4).
    with_tokens = bool((task.params or {}).get("with_tokens", True))

    result = DocumentRepository(session).embed_document(doc.id, with_tokens=with_tokens)
    if result is None:
        # embed_document returns None for a missing document or empty content, both of
        # which are ruled out above. Reaching here means it grew a third reason and this
        # handler would otherwise report a completed embedding that did not happen.
        raise ValueError(
            f"embed_document returned nothing for document {doc.id}, which has content; "
            "the task completed without writing a vector"
        )
    session.flush()

    embedder = get_embedder()
    return TaskOutcome(
        detail={
            "model": embedder.model_name,
            "device": embedder.device,
            "dims": int(result.document_embedding.shape[0]),
            "with_tokens": with_tokens,
            "tokens_stored": len(result.token_embeddings),
            "characters": len(doc.content),
        }
    )
