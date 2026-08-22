"""General-ingest contracts — transport-neutral request/response models.

Moved verbatim from ``api/schemas.py`` (the ``#61`` general-ingest pipeline schemas) so the
``IngestService`` can depend on them without importing FastAPI. ``api/schemas.py`` re-exports
every name here, so existing ``from jmfts_core.rest.schemas import X`` imports keep working.

Rule of the house: core may import contracts; contracts may not import ``api``/``fastapi``.
"""

from typing import Any, Optional

from pydantic import BaseModel, Field


class IngestRequest(BaseModel):
    """Request to ingest content through a named pipeline."""

    content: str = Field(description="Raw text content to ingest")
    usetype: str = Field(description="Pipeline name: conversation, markdown, raw, transcript")

    # Metadata
    title: Optional[str] = Field(default=None, description="Title for the root document")
    parent_id: Optional[int] = Field(default=None, description="Parent document ID to nest under")

    # Pipeline configuration overrides
    pipeline_config: Optional[dict[str, Any]] = Field(
        default=None,
        description=(
            "Per-stage overrides. Keys are stage names (parse, chunk, summarize, "
            "extract_facts). Values can be bool (enable/disable) or dict with "
            "'enabled' and/or param keys."
        ),
    )

    # LLM override
    llm_model: Optional[str] = Field(
        default=None, description="Override LLM model for summarization and extraction"
    )


class IngestStageResult(BaseModel):
    """Result of a single pipeline stage."""

    stage: str
    status: str = Field(description="completed, skipped, or failed")
    detail: dict = Field(default_factory=dict)
    error: Optional[str] = None


class IngestResponse(BaseModel):
    """Result of the general ingest pipeline."""

    source_document_id: int
    title: str
    usetype: str
    message_count: int
    segment_count: int
    summary_count: int
    triple_count: int
    tree_depth: int
    stages: list[IngestStageResult]
    was_existing: bool = Field(
        default=False,
        description="True when content_hash matched an existing doc and ingestion was skipped.",
    )
    existing_document_id: Optional[int] = Field(
        default=None,
        description="Set when was_existing=true; the document that already held this content.",
    )


class PipelineStageInfo(BaseModel):
    """Info about a single stage in a pipeline definition."""

    name: str
    enabled: bool
    params: dict = Field(default_factory=dict)


class PipelineInfo(BaseModel):
    """Info about a registered pipeline."""

    name: str
    description: str
    stages: list[PipelineStageInfo]
