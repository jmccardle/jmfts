"""Search-context contract — the named-search-preset request/response models.

These are the SINGLE definition of the search-context data shapes, shared by the
in-process Python API and the generated REST adapter. Moved verbatim from
``api/schemas.py`` (still re-exported from there for back-compat). This module is
FastAPI-free, so the service layer can depend on it without importing a web framework.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class SearchContextCreate(BaseModel):
    """Request to create a search context preset"""

    name: str
    description: Optional[str] = None
    config: dict = Field(
        default_factory=dict,
        description=(
            "Bundled search parameters: method, methods, weights, usetype, "
            "parent_id, index_name, threshold, limit"
        ),
    )


class SearchContextUpdate(BaseModel):
    """Request to update a search context preset"""

    description: Optional[str] = None
    config: Optional[dict] = None


class SearchContextResponse(BaseModel):
    """Search context preset response"""

    id: int
    name: str
    description: Optional[str]
    config: dict
    created_at: Optional[datetime]
    updated_at: Optional[datetime]

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_context(cls, ctx) -> "SearchContextResponse":
        """Convert a ``SearchContext`` ORM row to a response.

        The single source of truth for this mapping. The old router returned the ORM
        row directly and let FastAPI's ``response_model`` serialise it via
        ``from_attributes``; building the response through this classmethod is
        byte-identical (same fields, same order) and lets in-process callers receive a
        typed contract instead of a detached ORM object.
        """
        return cls(
            id=ctx.id,
            name=ctx.name,
            description=ctx.description,
            config=ctx.config,
            created_at=ctx.created_at,
            updated_at=ctx.updated_at,
        )
