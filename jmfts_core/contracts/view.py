"""View contracts — request/response models for the /view/* direct-readability surface.

Moved verbatim out of ``api/schemas.py`` so the service layer (``jmfts_core.services``)
can depend on them without importing FastAPI. ``api/schemas.py`` re-exports these names,
so existing ``from api.schemas import ViewResponse`` imports keep working.

These are all response models (the /view/* endpoints are reads); there is no request
body on this surface. Every field is unchanged from the pre-unification definitions.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class ViewPresentation(BaseModel):
    renderer: str
    child_handling: str
    link_handling: str
    renderer_config: dict = Field(default_factory=dict)


class ViewAncestor(BaseModel):
    id: int
    title: Optional[str]
    usetype: Optional[str]


class ViewChildStub(BaseModel):
    id: int
    title: Optional[str]
    usetype: Optional[str]
    preview: Optional[str]
    child_count: int
    expand_url: str


class ViewLinkRef(BaseModel):
    id: int
    target_id: int
    source_id: int
    direction: str  # "outbound" | "inbound"
    link_type: str
    score: float
    title: Optional[str]
    target_url: str


class ViewTripleRef(BaseModel):
    id: int
    subject_id: int
    subject_title: Optional[str]
    predicate_name: Optional[str]
    object_id: int
    object_title: Optional[str]
    fact_type: Optional[str]
    valid_from: Optional[datetime]
    valid_until: Optional[datetime]
    subject_url: str
    object_url: str


class ViewResponse(BaseModel):
    id: int
    title: Optional[str]
    usetype: Optional[str]
    renderer: str
    rendered_content: str
    ancestors: list[ViewAncestor] = Field(default_factory=list)
    children_stubs: list[ViewChildStub] = Field(default_factory=list)
    outbound_links: list[ViewLinkRef] = Field(default_factory=list)
    inbound_links: list[ViewLinkRef] = Field(default_factory=list)
    triples: list[ViewTripleRef] = Field(default_factory=list)
    presentation: ViewPresentation


class BreadcrumbResponse(BaseModel):
    document_id: int
    breadcrumbs: list[ViewAncestor]


class BackReferenceItem(BaseModel):
    document_id: int
    title: Optional[str]
    usetype: Optional[str]
    via: str  # "link" | "triple"
    relation: str  # link_type or predicate name
    snippet: Optional[str]
    url: str


class BackReferenceResponse(BaseModel):
    document_id: int
    total: int
    references: list[BackReferenceItem]
