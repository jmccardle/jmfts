"""Conversation ingestion orchestrator — #59

Accepts raw conversation data (adjutant JSONL or message arrays) and runs the
full pipeline: parse → create documents → embed → RAPTOR → fact extraction.
"""

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import numpy as np
from sqlalchemy.orm import Session

from jmfts_core.embedding import get_embedding_service
from jmfts_core.fact_extraction import extract_facts
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.segmentation import enforce_segment_bounds, pelt_segment
from jmfts_core.summarization import raptor_summarize

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ParsedMessage:
    role: str
    content: str
    timestamp: Optional[str] = None
    turn_index: int = 0


@dataclass
class StageResult:
    stage: str
    status: str  # "completed", "skipped", "failed"
    detail: dict = field(default_factory=dict)
    error: Optional[str] = None
    # Wall-clock bounds of the attempt, timezone-aware UTC. Stamped by StageClock at the
    # site that runs the stage, because that is the only place that knows when the work
    # actually began — everything downstream would have to infer it. They stay Optional
    # on the dataclass so the many construction sites read unchanged, but the attempt
    # record (contracts/attempt.py) rejects a finished attempt that carries no times, so
    # an unstamped stage is a loud failure rather than a hole in the log.
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class StageClock:
    """Measures one stage's wall-clock span and stamps the StageResult it produced.

    Constructed immediately before the stage's work, then ``stamp()``-ed on whichever
    StageResult that work produced — a stage with four outcome branches needs one clock,
    not four. ``time.monotonic`` is still used for the ``elapsed_ms`` details: monotonic
    is the right clock for a duration, wall time is the only one meaningful in a log.
    """

    __slots__ = ("started_at",)

    def __init__(self) -> None:
        self.started_at = datetime.now(timezone.utc)

    def stamp(self, result: StageResult) -> StageResult:
        result.started_at = self.started_at
        result.finished_at = datetime.now(timezone.utc)
        return result


@dataclass
class IngestResult:
    source_document_id: int
    title: str
    message_count: int
    segment_count: int
    summary_count: int
    triple_count: int
    tree_depth: int
    stages: list[StageResult] = field(default_factory=list)
    # Phase 3 idempotency: when a re-ingest matched a prior content_hash, we
    # short-circuit and return the existing document. Both fields default
    # to was_existing=False / None so all existing call sites stay correct.
    was_existing: bool = False
    existing_document_id: Optional[int] = None


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def parse_adjutant_jsonl(raw: str) -> list[ParsedMessage]:
    """Parse adjutant JSONL session data into messages.

    Adjutant format: one JSON object per line with prompt/response pairs.
    ``{"prompt": "...", "response": "...", "timestamp": "..."}``

    Also accepts pre-structured message lines:
    ``{"role": "user", "content": "...", "timestamp": "..."}``
    """
    messages: list[ParsedMessage] = []
    turn_index = 0
    for line_num, line in enumerate(raw.strip().splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("Skipping malformed JSONL line %d", line_num)
            continue

        ts = obj.get("timestamp")

        # Adjutant prompt/response pair format
        if "prompt" in obj:
            messages.append(
                ParsedMessage(
                    role=obj.get("role", "user"),
                    content=obj["prompt"],
                    timestamp=ts,
                    turn_index=turn_index,
                )
            )
            turn_index += 1
            if "response" in obj:
                messages.append(
                    ParsedMessage(
                        role="assistant",
                        content=obj["response"],
                        timestamp=ts,
                        turn_index=turn_index,
                    )
                )
                turn_index += 1

        # Pre-structured message format
        elif "role" in obj and "content" in obj:
            messages.append(
                ParsedMessage(
                    role=obj["role"],
                    content=obj["content"],
                    timestamp=ts,
                    turn_index=turn_index,
                )
            )
            turn_index += 1
        else:
            logger.warning("Skipping unrecognised JSONL line %d", line_num)

    return messages


def parse_message_array(messages: list[dict]) -> list[ParsedMessage]:
    """Parse simple message array format [{role, content, timestamp?}]."""
    return [
        ParsedMessage(
            role=msg.get("role", "unknown"),
            content=msg.get("content", ""),
            timestamp=msg.get("timestamp"),
            turn_index=i,
        )
        for i, msg in enumerate(messages)
    ]


# ---------------------------------------------------------------------------
# Constructive segmentation (PELT → interim topic containers)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SegmentationOutcome:
    """What the segment stage did, with "found nothing" separated from "never ran".

    INGEST_SPEC.md 3.4: *"a heuristic that ran and matched nothing is ``completed`` with a
    detail explaining what it looked for. A heuristic that ran and found nothing must
    never look like one that never ran."* 5.1 says it again for this exact shape of task.

    The two zero-production cases here are genuinely different facts. Below ``2 *
    min_segment`` embedded turns PELT is never called — there is nothing to re-examine
    later except by supplying more turns. Above it, PELT ran over a real embedding matrix
    with a real penalty and concluded the conversation is one topic run — a measurement,
    and one a different penalty could legitimately overturn, which is exactly what spec
    6.1's ``(task, param_fingerprint)`` diff re-runs on. Collapsing both into ``skipped``
    lost that distinction in the durable attempt log, where it is least recoverable.

    ``ran`` is the discriminator; ``detail`` carries the measurement either way.
    """

    container_ids: list[int]
    ran: bool
    detail: dict


def segment_conversation(
    session: Session,
    root_id: int,
    child_ids: list[int],
    *,
    min_segment: int = 3,
    max_segment: int = 10,
    penalty: Optional[float] = None,
) -> SegmentationOutcome:
    """Insert PELT topic-segment containers between a root and its ordered children.

    The order-preserving, episodic counterpart to RAPTOR's order-*blind* Leiden
    clustering: PELT change-point detection over the children's embedding *sequence*
    finds where the topic shifts, and each run of same-topic messages is grouped under a
    new ``usetype="segment"`` container, with the messages reparented beneath it. Because
    the containers become the root's new direct children (and are embedded), a subsequent
    RAPTOR pass composes cleanly — it summarises over topic segments instead of raw turns.

    Penalty
    -------
    ``min_segment``/``max_segment`` are Triskelion's 3/10 bounds. The penalty default,
    however, deliberately **drops** Triskelion's ``· embedding_dim · 0.1`` factor and uses
    the BIC-style ``log(n)``. Triskelion's ``log(n)·dim·0.1`` over-penalises catastrophically
    here: ruptures' ``l2`` cost on our **L2-normalised** embeddings is O(#points), not
    O(dim) (``||x-mean||² ≤ 4`` regardless of dim), so a dim-scaled penalty (≈177 at
    dim=768) never splits even two maximally-separated topic clusters — the feature would be
    inert. ``log(n)`` recovers the correct boundaries empirically. Pass ``penalty`` to
    override (e.g. from the ``segment`` stage params) once a corpus-calibrated value exists —
    that calibration is the real follow-up, not the 768-vs-1024 dim retune.

    Args:
        session: active session (caller owns the transaction).
        root_id: the conversation root whose children are being segmented.
        child_ids: the root's message children **in conversation order**.
        min_segment / max_segment: merge/split bounds on segment length.
        penalty: PELT breakpoint penalty; ``None`` ⇒ ``log(n)`` (see above).

    Returns:
        A :class:`SegmentationOutcome`. It creates nothing in two different cases and
        ``ran`` is what tells them apart — see that class; the flat tree is already the
        right shape in both.
    """
    repo = DocumentRepository(session)

    ordered = [d for d in (repo.get(cid) for cid in child_ids) if d is not None]
    embedded = [d for d in ordered if d.embed is not None]
    # Need at least two min-sized segments' worth of embedded turns to bother. PELT is
    # NOT run in this branch, which is why the outcome says so.
    if len(embedded) < 2 * min_segment:
        return SegmentationOutcome(
            container_ids=[],
            ran=False,
            detail={
                "reason": (
                    f"{len(embedded)} embedded turns is below the {2 * min_segment} "
                    f"needed for two segments of at least {min_segment}"
                ),
                "embedded_turns": len(embedded),
                "min_segment": min_segment,
            },
        )

    emb = np.array([np.asarray(d.embed, dtype=float) for d in embedded])
    ids = [d.id for d in embedded]
    if penalty is None:
        penalty = math.log(len(ids))

    segments = enforce_segment_bounds(
        pelt_segment(emb, ids, penalty=penalty, min_size=min_segment),
        min_segment=min_segment,
        max_segment=max_segment,
    )
    searched = {
        "embedded_turns": len(ids),
        "penalty": penalty,
        "min_segment": min_segment,
        "max_segment": max_segment,
        "segments_found": len(segments),
    }
    if len(segments) <= 1:
        # One topic run — no interim layer needed. PELT RAN and claimed nothing, which
        # spec 3.4 is explicit is a different fact from never having been attempted: "a
        # heuristic that ran and found nothing must never look like one that never ran".
        return SegmentationOutcome(container_ids=[], ran=True, detail=searched)

    service = get_embedding_service()
    container_ids: list[int] = []
    for k, seg in enumerate(segments):
        members = [repo.get(cid) for cid in seg.child_ids]
        seg_text = "\n\n".join(m.content for m in members if m is not None and m.content)
        # Embed the container's document vector only when it fits the doc window; an
        # over-window segment stays unembedded (RAPTOR simply skips it), never truncated.
        fits = bool(seg_text) and not service.check_fit(seg_text, with_tokens=False).truncated
        container = repo.create(
            title=f"Segment {k + 1}",
            content=seg_text,
            parent_id=root_id,
            usetype="segment",
            structured_content={
                "segment_index": k,
                "message_ids": list(seg.child_ids),
                "conversation_id": root_id,
            },
            auto_embed=fits,
            embed_tokens=False,
        )
        for cid in seg.child_ids:
            repo.reparent(cid, container.id)
        container_ids.append(container.id)

    session.flush()
    return SegmentationOutcome(container_ids=container_ids, ran=True, detail=searched)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


async def ingest_conversation(
    session: Session,
    messages: list[ParsedMessage],
    *,
    title: Optional[str] = None,
    parent_id: Optional[int] = None,
    # Pipeline toggles
    segment: bool = False,
    extract_triples: bool = True,
    summarize: bool = True,
    # Segmentation (PELT) options — Triskelion 3/10 bounds; penalty None ⇒ log(n)
    segment_min: int = 3,
    segment_max: int = 10,
    segment_penalty: Optional[float] = None,
    # RAPTOR options
    raptor_max_depth: int = 5,
    raptor_min_cluster_size: int = 2,
    llm_model: Optional[str] = None,
    max_summary_tokens: Optional[int] = None,
    # Fact extraction options
    max_facts: Optional[int] = None,
    confidence_threshold: Optional[float] = None,
    include_summaries: bool = True,
) -> IngestResult:
    """Orchestrate the full conversation ingestion pipeline.

    Stages executed in order:
      1. parse   – validate & count messages (already done by caller)
      2. chunk   – create root document + one child per message (auto-embed)
      2.5 segment (optional, opt-in) – PELT topic containers between root & turns
      3. summarize (optional) – RAPTOR hierarchical summarization
      4. extract_facts (optional) – LLM-powered triple extraction

    Each stage is resilient: a failure in a later stage does not discard the
    documents created by earlier stages. ``segment`` is off by default so the
    default tree shape (flat root→turns) is byte-unchanged; when on, RAPTOR then
    composes over the segment containers rather than the raw turns.
    """
    if not messages:
        raise ValueError("No messages to ingest")

    repo = DocumentRepository(session)
    stages: list[StageResult] = []

    # The parse clock starts here, not at the JSONL read: the caller
    # (``pipeline._execute_conversation``) or the API client did the actual parsing and
    # handed us ``messages``. The span this measures is the roll-up below, and claiming
    # anything wider would be a time this function never observed.
    parse_clock = StageClock()
    participants = sorted(set(m.role for m in messages))

    # Validate parent_id before creating any documents
    effective_parent_id = parent_id
    if parent_id is not None and repo.get(parent_id) is None:
        logger.warning(
            "Parent document %d does not exist; creating conversation as root document",
            parent_id,
        )
        effective_parent_id = None

    # ------------------------------------------------------------------
    # Stage 1 — Parse (already done, just record the result)
    # ------------------------------------------------------------------
    stages.append(
        parse_clock.stamp(
            StageResult(
                stage="parse",
                status="completed",
                detail={
                    "message_count": len(messages),
                    "participants": participants,
                },
            )
        )
    )

    # ------------------------------------------------------------------
    # Stage 2 — Create root + message chunk documents
    # ------------------------------------------------------------------
    chunk_clock = StageClock()
    conv_title = title or f"Conversation ({len(messages)} messages)"

    full_text = "\n\n".join(f"[{m.role}]: {m.content}" for m in messages)

    # The root is a container holding the whole conversation concatenated. It never
    # gets token/maxsim vectors (those live on the message children below), and it
    # gets a document vector ONLY when the concatenation fits the document-vector
    # window (embedding_doc_window, 8192 tokens).
    #
    # A real conversation is routinely OVER even the 8192 window, and the embedder
    # now refuses over-window text rather than truncating it (KNOWN-DEFECTS D1). So
    # embedding the container whole would raise TextTooLongError and fail the whole
    # ingest — which it did before this guard. There is no honest whole-text vector
    # for an over-window container, and a better conversation-level vector already
    # exists: the RAPTOR root summary produced in the summarize stage below. So embed
    # the container only when it fits; otherwise leave it unembedded (its message
    # children and its summary remain fully retrievable).
    service = get_embedding_service()
    root_doc_vector_fits = not service.check_fit(full_text, with_tokens=False).truncated

    root = repo.create(
        title=conv_title,
        content=full_text,
        parent_id=effective_parent_id,
        usetype="conversation",
        structured_content={
            "participants": participants,
            "turn_count": len(messages),
            "first_timestamp": messages[0].timestamp,
            "last_timestamp": messages[-1].timestamp,
            **({"original_parent_id": parent_id} if effective_parent_id != parent_id else {}),
        },
        auto_embed=root_doc_vector_fits,
        embed_tokens=False,
    )
    root_id = root.id

    # Most assistant messages exceed the token/maxsim window — that is the routine
    # case, not the edge case. Such a message becomes a container (document vector
    # only) with chunk children that carry the token vectors, mirroring what the
    # markdown/text pipelines do. Previously it was embedded as a ~2000-char prefix
    # and reported as fully embedded. See KNOWN-DEFECTS D1. (`service` is the
    # embedding service resolved above for the root's fit check.)
    message_doc_ids: list[int] = []
    for msg in messages:
        over_window = service.check_fit(msg.content, with_tokens=True).truncated

        child = repo.create(
            title=f"{msg.role} — turn {msg.turn_index}",
            content=msg.content,
            parent_id=root_id,
            usetype="chunk",
            structured_content={
                "speaker": msg.role,
                "turn_index": msg.turn_index,
                "timestamp": msg.timestamp,
                "conversation_id": root_id,
            },
            auto_embed=True,
            embed_tokens=not over_window,
        )
        message_doc_ids.append(child.id)

        if over_window:
            for i, piece in enumerate(service.chunk_to_fit(msg.content)):
                repo.create(
                    title=f"{msg.role} — turn {msg.turn_index} (part {i + 1})",
                    content=piece,
                    parent_id=child.id,
                    usetype="chunk",
                    structured_content={
                        "speaker": msg.role,
                        "turn_index": msg.turn_index,
                        "timestamp": msg.timestamp,
                        "conversation_id": root_id,
                        "part_index": i,
                    },
                    auto_embed=True,
                )

    session.flush()

    stages.append(
        chunk_clock.stamp(
            StageResult(
                stage="chunk",
                status="completed",
                detail={
                    "chunks_created": len(message_doc_ids),
                    "document_ids": message_doc_ids,
                    # Inspectable, not silent: whether the container itself carries a
                    # document vector, or was left unembedded because the concatenated
                    # conversation is over the 8192-token document-vector window (its
                    # summary and children remain retrievable — see the root-create note).
                    "root_document_vector": (
                        "embedded" if root_doc_vector_fits else "skipped_over_doc_window"
                    ),
                },
            )
        )
    )

    # ------------------------------------------------------------------
    # Stage 2.5 — Constructive PELT segmentation (optional, opt-in)
    # ------------------------------------------------------------------
    # Off by default: the flat root→turns tree is the canary-safe default. When on, this
    # inserts order-preserving topic containers between the root and its turns, so the
    # RAPTOR stage below composes over segments instead of raw messages.
    segment_count = 0
    segment_clock = StageClock()
    if segment:
        try:
            outcome = segment_conversation(
                session,
                root_id,
                message_doc_ids,
                min_segment=segment_min,
                max_segment=segment_max,
                penalty=segment_penalty,
            )
            segment_count = len(outcome.container_ids)
            session.flush()
            stages.append(
                segment_clock.stamp(
                    StageResult(
                        stage="segment",
                        # `completed` whenever PELT actually ran, WHATEVER it found —
                        # spec 3.4 and 5.1, and `_record_attempts` turns this into a
                        # durable attempt record where the distinction is the whole
                        # point. `skipped` is reserved for the stage that was never
                        # attempted, and then it carries `detail.reason`.
                        status="completed" if outcome.ran else "skipped",
                        detail={
                            "segments_created": segment_count,
                            "segment_document_ids": outcome.container_ids,
                            **outcome.detail,
                        },
                    )
                )
            )
        except Exception as e:
            logger.error("PELT segmentation failed: %s", e, exc_info=True)
            stages.append(
                segment_clock.stamp(StageResult(stage="segment", status="failed", error=str(e)))
            )
    else:
        stages.append(
            segment_clock.stamp(
                StageResult(stage="segment", status="skipped", detail={"reason": "disabled"})
            )
        )

    # ------------------------------------------------------------------
    # Stage 3 — RAPTOR hierarchical summarization (optional)
    # ------------------------------------------------------------------
    summary_count = 0
    tree_depth = 1  # root + chunks
    summarize_clock = StageClock()
    if summarize:
        try:
            children = repo.get_children(root_id, depth=1, limit=10000)
            embedded = [c for c in children if c.embed is not None]

            if len(embedded) >= 2:
                raptor_result = await raptor_summarize(
                    document_id=root_id,
                    session=session,
                    max_depth=raptor_max_depth,
                    min_cluster_size=raptor_min_cluster_size,
                    llm_model=llm_model,
                    max_summary_tokens=max_summary_tokens,
                )
                summary_count = raptor_result.total_summaries
                if raptor_result.layers:
                    tree_depth = max(lr.layer for lr in raptor_result.layers) + 1
                session.flush()

                stages.append(
                    summarize_clock.stamp(
                        StageResult(
                            stage="summarize",
                            status="completed",
                            detail={
                                "total_summaries": summary_count,
                                "total_bridge_links": raptor_result.total_bridge_links,
                                "layers": len(raptor_result.layers),
                                "tree_depth": tree_depth,
                            },
                        )
                    )
                )
            else:
                stages.append(
                    summarize_clock.stamp(
                        StageResult(
                            stage="summarize",
                            status="skipped",
                            detail={
                                "reason": (f"Only {len(embedded)} embedded children (need >= 2)")
                            },
                        )
                    )
                )
        except Exception as e:
            logger.error("RAPTOR summarization failed: %s", e, exc_info=True)
            stages.append(
                summarize_clock.stamp(StageResult(stage="summarize", status="failed", error=str(e)))
            )
    else:
        stages.append(
            summarize_clock.stamp(
                StageResult(stage="summarize", status="skipped", detail={"reason": "disabled"})
            )
        )

    # ------------------------------------------------------------------
    # Stage 4 — Fact extraction (optional)
    # ------------------------------------------------------------------
    triple_count = 0
    facts_clock = StageClock()
    if extract_triples:
        try:
            fact_result = await extract_facts(
                document_id=root_id,
                session=session,
                llm_model=llm_model,
                max_facts=max_facts,
                confidence_threshold=confidence_threshold,
                include_summaries=include_summaries,
            )
            triple_count = fact_result.total_triples_created
            session.flush()

            stages.append(
                facts_clock.stamp(
                    StageResult(
                        stage="extract_facts",
                        status="completed",
                        detail={
                            "documents_processed": fact_result.documents_processed,
                            "triples_created": triple_count,
                            "triples_skipped": fact_result.total_skipped,
                            "entities_created": fact_result.entities_created,
                            "entities_resolved": fact_result.entities_resolved,
                            "predicates_created": fact_result.predicates_created,
                        },
                    )
                )
            )
        except Exception as e:
            logger.error("Fact extraction failed: %s", e, exc_info=True)
            stages.append(
                facts_clock.stamp(StageResult(stage="extract_facts", status="failed", error=str(e)))
            )
    else:
        stages.append(
            facts_clock.stamp(
                StageResult(
                    stage="extract_facts",
                    status="skipped",
                    detail={"reason": "disabled"},
                )
            )
        )

    return IngestResult(
        source_document_id=root_id,
        title=conv_title,
        message_count=len(messages),
        segment_count=segment_count,
        summary_count=summary_count,
        triple_count=triple_count,
        tree_depth=tree_depth,
        stages=stages,
    )
