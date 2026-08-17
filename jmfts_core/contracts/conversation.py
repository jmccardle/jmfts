"""Conversation-ingest contracts — transport-neutral request/response models.

Moved verbatim from ``api/schemas.py`` (the ``#59`` conversation-ingestion schemas) so the
``ConversationService`` can depend on them without importing FastAPI. ``api/schemas.py``
re-exports every name here, so existing ``from api.schemas import X`` imports keep working.

Rule of the house: core may import contracts; contracts may not import ``api``/``fastapi``.
"""

from typing import Optional

from pydantic import BaseModel, Field


class ConversationMessage(BaseModel):
    """A single message in a conversation"""

    role: str = Field(description="Speaker role: user, assistant, system, etc.")
    content: str = Field(description="Message text content")
    timestamp: Optional[str] = Field(
        default=None, description="ISO 8601 timestamp for this message"
    )


class ConversationIngestRequest(BaseModel):
    """Request to ingest a conversation through the full pipeline"""

    messages: Optional[list[ConversationMessage]] = Field(
        default=None, description="Simple message array format"
    )
    jsonl: Optional[str] = Field(
        default=None, description="Raw JSONL string in adjutant session format"
    )

    # Metadata
    title: Optional[str] = Field(default=None, description="Title for the conversation document")
    parent_id: Optional[int] = Field(
        default=None, description="Parent document ID to nest this conversation under"
    )

    # Pipeline stage toggles
    summarize: bool = Field(default=True, description="Run RAPTOR hierarchical summarization")
    extract_facts: bool = Field(default=True, description="Run LLM-powered fact extraction")

    # RAPTOR options
    raptor_max_depth: int = Field(default=5, ge=1, le=20, description="Max RAPTOR recursion depth")
    raptor_min_cluster_size: int = Field(
        default=2, ge=2, description="Min documents per Leiden cluster"
    )
    llm_model: Optional[str] = Field(
        default=None, description="Override LLM model for summarization and extraction"
    )
    max_summary_tokens: Optional[int] = Field(
        default=None, ge=64, le=4096, description="Max tokens per RAPTOR summary"
    )

    # Fact extraction options
    max_facts: Optional[int] = Field(
        default=None, ge=1, le=20, description="Max triples per segment"
    )
    confidence_threshold: Optional[float] = Field(
        default=None, ge=0.0, le=1.0, description="Min confidence to keep a triple"
    )
    include_summaries: bool = Field(
        default=True, description="Also extract facts from RAPTOR summaries"
    )


class ConversationStageResult(BaseModel):
    """Result of a single pipeline stage"""

    stage: str
    status: str = Field(description="completed, skipped, or failed")
    detail: dict = Field(default_factory=dict)
    error: Optional[str] = None


class ConversationIngestResponse(BaseModel):
    """Result of conversation ingestion pipeline"""

    source_document_id: int
    title: str
    message_count: int
    segment_count: int
    summary_count: int
    triple_count: int
    tree_depth: int
    stages: list[ConversationStageResult]
