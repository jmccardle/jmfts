"""ConversationService — conversation-ingestion operations, transport-neutral.

Logic lifted verbatim from ``api/routers/conversations.py`` so the behaviour is identical;
the only structural change is that the domain→HTTP status mapping is now declared per-op in
``@expose(errors=...)`` and keyed by EXCEPTION TYPE rather than by inline ``HTTPException``:

- ``ValueError``  → 400 — bad/ambiguous input (both/neither of ``messages``/``jsonl``;
  no valid messages; all messages empty). Detail strings preserved verbatim.
- ``LookupError`` → 404 — a supplied ``parent_id`` does not resolve to a document. Detail
  string ``"Parent document <id> not found"`` preserved verbatim.

The service takes a ``Session`` and returns typed contracts — no FastAPI here. The single
exposed method is a coroutine (it awaits ``execute_pipeline``), so the adapter emits an
``async def`` endpoint that runs it on the event loop.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from jmfts_core.contracts.conversation import (
    ConversationIngestRequest,
    ConversationIngestResponse,
    ConversationStageResult,
)
from jmfts_core.conversation_ingest import (
    parse_adjutant_jsonl,
    parse_message_array,
)
from jmfts_core.pipeline import execute_pipeline
from jmfts_core.registry import expose, register_service

logger = logging.getLogger(__name__)


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
    async def ingest_conversation(
        self,
        request: ConversationIngestRequest,
    ) -> ConversationIngestResponse:
        """Ingest a conversation through the full pipeline.

        Accepts either:
        - ``messages``: a JSON array of ``{role, content, timestamp?}`` objects
        - ``jsonl``: a raw JSONL string in adjutant session format
          (``{prompt, response, timestamp}`` per line)

        Pipeline stages (each configurable via request body):
          1. **parse** — validate and normalise input
          2. **chunk** — create root document + one child per message (auto-embedded)
          3. **summarize** — RAPTOR hierarchical summarization (optional)
          4. **extract_facts** — LLM-powered triple extraction (optional)

        Later stages are resilient: a failure in RAPTOR or fact extraction does not
        discard documents created by earlier stages.
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

        # ---- Validate parent if provided ----
        if request.parent_id is not None:
            from jmfts_core.repositories.document import DocumentRepository

            repo = DocumentRepository(db)
            if not repo.get(request.parent_id):
                raise LookupError(f"Parent document {request.parent_id} not found")

        # ---- Build pipeline config from request fields ----
        pipeline_config = {
            "summarize": {
                "enabled": request.summarize,
                "params": {
                    "max_depth": request.raptor_max_depth,
                    "min_cluster_size": request.raptor_min_cluster_size,
                },
            },
            "extract_facts": {
                "enabled": request.extract_facts,
                "params": {},
            },
        }
        if request.max_summary_tokens is not None:
            pipeline_config["summarize"]["params"][
                "max_summary_tokens"
            ] = request.max_summary_tokens
        if request.max_facts is not None:
            pipeline_config["extract_facts"]["params"]["max_facts"] = request.max_facts
        if request.confidence_threshold is not None:
            pipeline_config["extract_facts"]["params"][
                "confidence_threshold"
            ] = request.confidence_threshold
        pipeline_config["extract_facts"]["params"]["include_summaries"] = request.include_summaries

        # ---- Run pipeline ----
        result = await execute_pipeline(
            session=db,
            content=request.jsonl or "",
            usetype="conversation",
            title=request.title,
            parent_id=request.parent_id,
            pipeline_config=pipeline_config,
            llm_model=request.llm_model,
            messages=parsed,
        )

        db.commit()

        return ConversationIngestResponse(
            source_document_id=result.source_document_id,
            title=result.title,
            message_count=result.message_count,
            segment_count=result.segment_count,
            summary_count=result.summary_count,
            triple_count=result.triple_count,
            tree_depth=result.tree_depth,
            stages=[
                ConversationStageResult(
                    stage=s.stage,
                    status=s.status,
                    detail=s.detail,
                    error=s.error,
                )
                for s in result.stages
            ],
        )
