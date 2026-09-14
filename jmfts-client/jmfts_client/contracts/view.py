"""View contracts — request/response models for the /view/* direct-readability surface.

Moved verbatim out of ``api/schemas.py`` so the service layer (``jmfts_core.services``)
can depend on them without importing FastAPI. ``api/schemas.py`` re-exports these names,
so existing ``from jmfts_core.rest.schemas import ViewResponse`` imports keep working.

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
    #: Null when the object is a literal value rather than a document node — see
    #: ``object_literal``. ``object_url`` is null with it, because there is nothing to
    #: navigate to.
    object_id: Optional[int]
    object_title: Optional[str]
    object_literal: Optional[str] = None
    object_datatype: Optional[str] = None
    fact_type: Optional[str]
    valid_from: Optional[datetime]
    valid_until: Optional[datetime]
    subject_url: str
    object_url: Optional[str]


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

    #: IC-3. Whether the CALLING principal may modify this document, add children under it,
    #: or attach an edge whose source it is — ``jmfts_core.access.can_write``, which is the
    #: same function Block A step 1's gate calls, so a page and a gate cannot disagree.
    #:
    #: **Added because a viewer could not ask.** No response reported the caller's own access
    #: level (checked 2026-09-13 across ``view.py`` and ``document.py``), so a page rendering
    #: a "create link" or "edit" control had to fire the call and read a 403 back to find
    #: out. That is a control that lies, and offering an action that will be refused is the
    #: user-facing half of the same defect Block A closes on the server side.
    #:
    #: ``True`` for an ungoverned document, which is not an oversight: access control in
    #: JMFTS is opt-in and ``can_write`` returns True when no access-control root governs the
    #: node (``jmfts_core/access.py:172``). It is also ``True`` for the owner and for an
    #: in-process caller, which ``_bypass`` (``:38``) exempts from all of it.
    can_write: bool = Field(
        ...,
        description=(
            "Whether the calling principal may modify this document. False means a write "
            "control should not be offered, not that one would fail silently."
        ),
    )


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
