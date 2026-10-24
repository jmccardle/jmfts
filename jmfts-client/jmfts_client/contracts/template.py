"""Prompt-template contracts — request/response models for the templates surface.

Moved out of ``api/schemas.py`` so the service layer (``jmfts_core.services``) can
depend on them without importing FastAPI. ``api/schemas.py`` re-exports these names,
so existing ``from jmfts_core.rest.schemas import TemplateCreate`` imports keep working.

``TemplateResponse.from_document`` is the SINGLE ORM→response mapping for a template
document (it was the router's local ``_doc_to_template``); keeping it here means the
mapping is defined once, mirroring ``DocumentResponse.from_document``.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class TemplateVariable(BaseModel):
    """A variable placeholder in a template"""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    required: bool = True


class StoredTemplateVariable(TemplateVariable):
    """A variable placeholder as it is READ BACK from storage: undeclared keys are dropped.

    ``TemplateResponse.variables`` is built from ``structured_content``, which may hold keys
    written before the request contract forbade extras (#47); a 422 there would be a 500 on
    read. Requests use ``TemplateVariable``, which refuses them.
    """

    model_config = ConfigDict(extra="ignore")


class TemplateCreate(BaseModel):
    """Request to create a prompt template"""

    model_config = ConfigDict(extra="forbid")

    title: str
    content: str = Field(description="Template body with {{variable}} placeholders")
    category: str = Field(
        description="One of: implementation, grooming, evaluation, ideation, refinement"
    )
    variables: list[TemplateVariable] = Field(default_factory=list)


class TemplateUpdate(BaseModel):
    """Request to update a prompt template"""

    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = None
    content: Optional[str] = None
    category: Optional[str] = None
    variables: Optional[list[TemplateVariable]] = None


class TemplateResponse(BaseModel):
    """Prompt template response"""

    id: int
    title: str
    content: str
    category: str
    variables: list[StoredTemplateVariable]
    usage_count: int = 0
    success_rate: float = 0.0
    last_used: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_document(cls, doc) -> "TemplateResponse":
        """Convert a template ``Document`` ORM row to a response.

        The single source of truth for this mapping (was the router's local
        ``_doc_to_template``). Template metadata lives in ``structured_content``.
        """
        sc = doc.structured_content or {}
        return cls(
            id=doc.id,
            title=doc.title or "",
            content=doc.content or "",
            category=sc.get("category", ""),
            variables=[v for v in sc.get("variables", [])],
            usage_count=sc.get("usage_count", 0),
            success_rate=sc.get("success_rate", 0.0),
            last_used=sc.get("last_used"),
            created_at=doc.created_at,
            updated_at=doc.updated_at,
        )


class TemplateRenderRequest(BaseModel):
    """Request to render a template with variables"""

    model_config = ConfigDict(extra="forbid")

    variables: dict[str, str] = Field(description="Variable name -> value mapping for substitution")


class TemplateRenderResponse(BaseModel):
    """Rendered template output"""

    rendered: str
    template_id: int
    missing_variables: list[str] = Field(default_factory=list)


class TemplateSearchRequest(BaseModel):
    """Request to search templates by semantic similarity"""

    model_config = ConfigDict(extra="forbid")

    query: str
    category: Optional[str] = None
    limit: int = Field(default=10, ge=1, le=50)
