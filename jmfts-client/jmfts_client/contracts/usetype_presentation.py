"""Usetype-presentation contract — the rendering-rule request/response models.

These are the SINGLE definition of the usetype-presentation data shapes, shared by the
in-process Python API and the generated REST adapter. Moved verbatim from
``api/schemas.py`` (still re-exported from there for back-compat). This module is
FastAPI-free, so the service layer can depend on it without importing a web framework.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class UsetypePresentationCreate(BaseModel):
    usetype: str = Field(..., description="The usetype this rule applies to (or '*' for catch-all)")
    renderer: str = Field(..., description="markdown | code | json-table | transcript | plain")
    child_handling: str = Field(
        ..., description="collapsed | inline-headings | hidden | first-paragraph"
    )
    link_handling: str = Field(..., description="footnotes | inline-citations | sidebar | hidden")
    renderer_config: dict = Field(default_factory=dict)
    description: Optional[str] = None


class UsetypePresentationUpdate(BaseModel):
    renderer: Optional[str] = None
    child_handling: Optional[str] = None
    link_handling: Optional[str] = None
    renderer_config: Optional[dict] = None
    description: Optional[str] = None


class UsetypePresentationResponse(BaseModel):
    usetype: str
    renderer: str
    child_handling: str
    link_handling: str
    renderer_config: dict
    description: Optional[str]
    created_at: Optional[datetime]
    updated_at: Optional[datetime]

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_presentation(cls, row) -> "UsetypePresentationResponse":
        """Convert a ``UsetypePresentation`` ORM row to a response.

        The single source of truth for this mapping. The old router returned the ORM
        row directly and let FastAPI's ``response_model`` serialise it via
        ``from_attributes``; building the response through this classmethod is
        byte-identical (same fields, same order) and lets in-process callers receive a
        typed contract instead of a detached ORM object.
        """
        return cls(
            usetype=row.usetype,
            renderer=row.renderer,
            child_handling=row.child_handling,
            link_handling=row.link_handling,
            renderer_config=row.renderer_config,
            description=row.description,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
