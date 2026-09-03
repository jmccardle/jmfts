"""ConversationService — conversation-ingestion operations, transport-neutral.

Logic lifted verbatim from ``api/routers/conversations.py`` so the behaviour is identical;
the only structural change is that the domain→HTTP status mapping is now declared per-op in
``@expose(errors=...)`` and keyed by EXCEPTION TYPE rather than by inline ``HTTPException``:

- ``ValueError``  → 400 — bad/ambiguous input (both/neither of ``messages``/``jsonl``;
  no valid messages; all messages empty). Detail strings preserved verbatim.
- ``LookupError`` → 404 — a supplied ``parent_id`` does not resolve to a document. Detail
  string ``"Parent document <id> not found"`` preserved verbatim.

The service takes a ``Session`` and returns typed contracts — no FastAPI here. The single
exposed method is a PLAIN ``def``, so FastAPI runs it in a threadpool. It used to be a
coroutine because it awaited ``execute_pipeline``; ``SPRINT_JOBS.md`` 15.4 S7 put the work
on the ingest queue, whose handlers are synchronous, and the ones that call an LLM own an
``asyncio.run`` for the duration of one call and cannot nest inside a running loop. A
threadpool endpoint is therefore not merely acceptable here, it is required — and it also
keeps a drain that can last minutes off the loop.
"""

from __future__ import annotations

import logging
from uuid import uuid4

from sqlalchemy.orm import Session

from jmfts_client.contracts.conversation import (
    ConversationIngestRequest,
    ConversationIngestResponse,
    ConversationStageResult,
)
from jmfts_core.conversation_ingest import (
    messages_to_jsonl,
    parse_adjutant_jsonl,
    parse_message_array,
)
from jmfts_core.config import get_settings
from jmfts_core.ingest_options import resolve_usetype_options
from jmfts_core.ingest_summary import summarize_tree
from jmfts_core.ingest_worker import IngestWorker, borrowed_session
from jmfts_core.registry import expose, register_service
from jmfts_core.rollup_tasks import IngestRollupPlanner
from jmfts_core.services.ingest_service import IngestService

logger = logging.getLogger(__name__)

#: The entry point a transcript is ingested under. Named rather than spelled inline so the
#: two callers that mean it — this service and `POST /ingest` — cannot come to disagree
#: about which usetype's options a conversation resolves.
USETYPE_CONVERSATION = "conversation"

#: The request model's own defaults for the two RAPTOR fields, read from the contract
#: rather than repeated, so a default that moves there moves the line this refuses at.
_RAPTOR_DEFAULTS = {
    field: ConversationIngestRequest.model_fields[field].default
    for field in ("raptor_max_depth", "raptor_min_cluster_size")
}

_RAPTOR_REFUSAL = (
    "{field} describes RAPTOR's Leiden clustering, and conversations are ingested through "
    "the queue now, whose rollup segments in document order instead (INGEST_SPEC.md 11.4). "
    "There is nothing for this value to tune. Leave it unset; use `options` on "
    "POST /ingest to reach the rollup's own parameters (max_children, penalty, "
    "min_segment)."
)


@register_service
class ConversationService:
    """Conversation-ingestion operations over a single database session."""

    def __init__(self, session: Session):
        self.session = session

    @expose(
        "POST",
        "/conversations/ingest",
        response_model=ConversationIngestResponse,
        errors={ValueError: 400, LookupError: 404},
        tags=["conversations"],
        summary="Ingest a conversation through the full pipeline",
    )
    def ingest_conversation(
        self,
        request: ConversationIngestRequest,
    ) -> ConversationIngestResponse:
        """Ingest a conversation through the full pipeline.

        Accepts either:
        - ``messages``: a JSON array of ``{role, content, timestamp?}`` objects
        - ``jsonl``: a raw JSONL string in adjutant session format
          (``{prompt, response, timestamp}`` per line)

        **On the ingest queue since ``SPRINT_JOBS.md`` 15.4 S7**, and still synchronous:
        the transcript is stored as JSONL bytes, ``probe`` reports ``is_conversation``, and
        ``structure:conversation`` writes one chunk per turn. This request then drains that
        document's tasks and reports the finished tree, exactly as it did when a pipeline
        ran inline. The response model has not changed.

        A caller who sends ``messages`` gets those messages re-encoded as JSONL, because
        the queue starts from bytes and a JSON array has no line-oriented spelling. A
        caller who sends ``jsonl`` gets their bytes stored verbatim.

        WHAT THE REQUEST'S TUNING FIELDS DO NOW, and the two that stopped meaning anything:

        * ``summarize`` and ``extract_facts`` still switch their work on and off —
          ``extract_facts`` through the ``facts.enabled`` ingest option. ``summarize=False``
          is REFUSED rather than ignored: path B's ``summarize`` task is what gives a
          container node its content and its vectors, so turning it off would leave every
          container the rollup builds unretrievable. That is a different switch from path
          A's RAPTOR toggle, which is why it cannot be honoured by silently doing nothing.
        * ``raptor_max_depth`` and ``raptor_min_cluster_size`` describe RAPTOR's clustering,
          and the rollup does not cluster — 11.4 segments in document order instead. A
          request that sets either is REFUSED, for the reason ``ingest_options`` gives about
          every unknown option: a run that does something other than what was asked and
          reports success is the swallowed failure this codebase does not ship.
        * ``max_summary_tokens``, ``max_facts``, ``confidence_threshold`` and
          ``include_summaries`` reach the tasks that read them; the last three are fact
          extraction's and are governed by the appliance's ``JMFTS_EXTRACTION_*`` settings
          until the ``facts`` option group grows them.
        """
        db = self.session

        # ---- Parse input ----
        if request.messages and request.jsonl:
            raise ValueError("Provide either 'messages' or 'jsonl', not both")
        if not request.messages and not request.jsonl:
            raise ValueError("Provide either 'messages' (array) or 'jsonl' (string)")

        if request.jsonl:
            parsed = parse_adjutant_jsonl(request.jsonl)
        else:
            parsed = parse_message_array([m.model_dump() for m in request.messages])

        if not parsed:
            raise ValueError("No valid messages found in input")

        # Filter out empty messages
        parsed = [m for m in parsed if m.content and m.content.strip()]
        if not parsed:
            raise ValueError("All messages are empty")

        # Re-index after filtering
        for i, msg in enumerate(parsed):
            msg.turn_index = i

        # ---- Refuse the two fields that stopped meaning anything ----
        # Not ignored. `raptor_max_depth` and `raptor_min_cluster_size` describe RAPTOR's
        # Leiden clustering, and the rollup segments in document order instead (11.4). A
        # request that named a depth and got a tree built a different way has been told
        # nothing. The comparison is against the field's DEFAULT, because that is the only
        # detectable line between "asked for" and "not mentioned" on a model with defaults.
        if request.raptor_max_depth != _RAPTOR_DEFAULTS["raptor_max_depth"]:
            raise ValueError(_RAPTOR_REFUSAL.format(field="raptor_max_depth"))
        if request.raptor_min_cluster_size != _RAPTOR_DEFAULTS["raptor_min_cluster_size"]:
            raise ValueError(_RAPTOR_REFUSAL.format(field="raptor_min_cluster_size"))
        if request.max_summary_tokens is not None:
            raise ValueError(_RAPTOR_REFUSAL.format(field="max_summary_tokens"))
        if not request.summarize:
            raise ValueError(
                "summarize=False cannot be honoured on the ingest queue. Path A's "
                "`summarize` stage was optional RAPTOR clustering; the queue's `summarize` "
                "task is what gives every container node its content and its vectors, so "
                "turning it off would leave the containers the rollup builds unretrievable. "
                "The two are different switches and the request field describes the one "
                "that no longer exists."
            )

        # ---- Validate parent if provided ----
        if request.parent_id is not None:
            from jmfts_core.repositories.document import DocumentRepository

            repo = DocumentRepository(db)
            if not repo.get(request.parent_id):
                raise LookupError(f"Parent document {request.parent_id} not found")

        # ---- Store the transcript and drain its queue ----
        # `messages_to_jsonl` for BOTH inputs, deliberately: a caller who sent `jsonl` had
        # it parsed above (empty messages filtered, turn indices re-assigned), and storing
        # their original text would store bytes that disagree with the messages this
        # request validated. The blob is a record of what was ingested.
        overrides: dict[str, dict] = {
            "facts": {
                "enabled": request.extract_facts,
                "include_summaries": request.include_summaries,
            }
        }
        if request.max_facts is not None:
            overrides["facts"]["max_facts"] = request.max_facts
        if request.confidence_threshold is not None:
            overrides["facts"]["confidence_threshold"] = request.confidence_threshold
        if request.llm_model:
            overrides["facts"]["llm_model"] = request.llm_model
            overrides["rollup"] = {"llm_model": request.llm_model}

        ingest = IngestService(db)
        title = request.title or f"Conversation ({len(parsed)} messages)"
        stored = ingest.store_text_as_file(
            messages_to_jsonl(parsed),
            filename=title,
            parent_id=request.parent_id,
            options=resolve_usetype_options(USETYPE_CONVERSATION, "", overrides),
        )

        if not stored.was_existing:
            worker = IngestWorker(
                worker_id=f"inline-conversation-{uuid4().hex[:12]}",
                session_factory=lambda: borrowed_session(db),
                planner=IngestRollupPlanner(),
            )
            worker.drain_document(
                stored.document_id,
                timeout_seconds=get_settings().ingest_sync_timeout_seconds,
            )

        db.expire_all()
        summary = summarize_tree(db, stored.document_id)
        if summary is None:
            raise RuntimeError(
                f"document {stored.document_id} was ingested and then could not be read "
                "back; the tree it describes is gone"
            )

        return ConversationIngestResponse(
            source_document_id=stored.document_id,
            title=summary.title,
            message_count=summary.message_count,
            segment_count=summary.segment_count,
            summary_count=summary.summary_count,
            triple_count=summary.triple_count,
            tree_depth=summary.tree_depth,
            stages=[
                ConversationStageResult(
                    stage=s.stage,
                    status=s.status,
                    detail=s.detail,
                    error=s.error,
                )
                for s in summary.stages
            ],
        )
