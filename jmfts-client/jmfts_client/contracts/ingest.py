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

    # Ingest options — the queue's vocabulary.
    options: Optional[dict[str, dict]] = Field(
        default=None,
        description=(
            "Ingest option overrides: option group -> parameters, e.g. "
            "{'structure': {'max_tokens': 300}}. Same shape as the `options` field of "
            "POST /ingest/file. An unknown group, an unknown option or a value of the "
            "wrong type is a 400."
        ),
    )

    pipeline_config: Optional[dict[str, Any]] = Field(
        default=None,
        description=(
            "REMOVED. The synchronous pipeline's per-stage overrides. Sending this is a "
            "400 naming `options`, which is the ingest queue's vocabulary; the two do not "
            "translate, so accepting the field and doing something else would be worse "
            "than refusing it. The field is kept so the refusal can name it."
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


class PipelineInfo(BaseModel):
    """What one ``usetype`` a caller may name in :class:`IngestRequest` does.

    Two facts, and they are the two ``probe`` cannot supply: where the bytes come from, and
    what this entry point tunes differently from the task defaults. Everything else about
    an ingest is decided from what was measured.

    ``PipelineStageInfo`` and a ``stages`` field were here until ``SPRINT_JOBS.md`` 15.4
    S9. They described the synchronous pipeline's stage list, and there are no stages —
    there are tasks, and which of them run is ``GET /ingest/explain``'s answer, not a
    property of the usetype.
    """

    name: str
    description: str
    source: str = Field(
        default="content",
        description=(
            "What `content` holds for this usetype: 'content' for the document itself, or "
            "'url' / 'arxiv' / 'path' for an identifier the appliance fetches."
        ),
    )
    options: dict[str, dict] = Field(
        default_factory=dict,
        description=(
            "The resolved ingest options for this usetype, option group -> parameters. "
            "A request may override any of them; see the `options` field of an upload."
        ),
    )
