"""UsetypePresentationService — rendering-rule CRUD, transport-neutral.

Logic lifted verbatim from ``api/routers/usetype_presentations.py`` so the behaviour is
identical; the only single-sourcing change is that ``UsetypePresentation ->
UsetypePresentationResponse`` now goes through the one
``UsetypePresentationResponse.from_presentation`` converter (the old router returned the
ORM row and let FastAPI's ``response_model`` serialise it — byte-identical).

Domain → HTTP mapping is declared per-op in ``@expose(errors=...)`` and keyed by EXCEPTION
TYPE, so the three hand-written statuses the router raised are reproduced without a
call-site check:

- ``LookupError``                        → 404 (presentation not found), detail
  ``"Presentation for <usetype> not found"``.
- ``UsetypePresentationConflictError``   → 409 (usetype already has a rule), detail
  ``"Presentation for usetype <usetype> already exists"``.
- ``ValueError`` (from the repo's ``_validate``) → 422 (bad renderer/handling value),
  detail ``str(e)`` verbatim.

All detail strings are preserved verbatim, including the ``!r`` repr of the usetype.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from jmfts_core.contracts.usetype_presentation import (
    UsetypePresentationCreate,
    UsetypePresentationResponse,
    UsetypePresentationUpdate,
)
from jmfts_core.registry import expose, register_service
from jmfts_core.repositories.usetype_presentation import UsetypePresentationRepository


class UsetypePresentationConflictError(Exception):
    """A presentation rule for the requested usetype already exists (→ HTTP 409)."""


@register_service
class UsetypePresentationService:
    """Usetype-presentation (rendering-rule) CRUD over a single database session."""

    def __init__(self, session: Session):
        self.session = session

    @expose(
        "GET",
        "/usetype-presentations/",
        response_model=list[UsetypePresentationResponse],
        tags=["usetype-presentations"],
        summary="List all usetype presentation rules.",
    )
    def list_presentations(self) -> list[UsetypePresentationResponse]:
        """List all usetype presentation rules."""
        repo = UsetypePresentationRepository(self.session)
        return [UsetypePresentationResponse.from_presentation(row) for row in repo.list_all()]

    @expose(
        "POST",
        "/usetype-presentations/",
        response_model=UsetypePresentationResponse,
        errors={UsetypePresentationConflictError: 409, ValueError: 422},
        tags=["usetype-presentations"],
        summary="Create a presentation rule for a usetype (use '*' for the catch-all).",
        status_code=201,
    )
    def create_presentation(
        self, request: UsetypePresentationCreate
    ) -> UsetypePresentationResponse:
        """Create a presentation rule for a usetype (use '*' for the catch-all)."""
        repo = UsetypePresentationRepository(self.session)
        if repo.get(request.usetype):
            raise UsetypePresentationConflictError(
                f"Presentation for usetype {request.usetype!r} already exists"
            )
        row = repo.create(
            usetype=request.usetype,
            renderer=request.renderer,
            child_handling=request.child_handling,
            link_handling=request.link_handling,
            renderer_config=request.renderer_config,
            description=request.description,
        )
        response = UsetypePresentationResponse.from_presentation(row)
        # Commit before responding: get_db's teardown commit runs after the
        # response is sent, so a client acting on the result would race it.
        self.session.commit()
        return response

    @expose(
        "GET",
        "/usetype-presentations/{usetype:path}",
        response_model=UsetypePresentationResponse,
        errors={LookupError: 404},
        tags=["usetype-presentations"],
        summary="Look up the presentation rule for a usetype.",
    )
    def get_presentation(self, usetype: str) -> UsetypePresentationResponse:
        """Look up the presentation rule for a usetype."""
        repo = UsetypePresentationRepository(self.session)
        row = repo.get(usetype)
        if not row:
            raise LookupError(f"Presentation for {usetype!r} not found")
        return UsetypePresentationResponse.from_presentation(row)

    @expose(
        "PUT",
        "/usetype-presentations/{usetype:path}",
        response_model=UsetypePresentationResponse,
        errors={LookupError: 404, ValueError: 422},
        tags=["usetype-presentations"],
        summary="Update fields on an existing presentation rule. Unspecified fields are kept.",
    )
    def update_presentation(
        self, usetype: str, request: UsetypePresentationUpdate
    ) -> UsetypePresentationResponse:
        """Update fields on an existing presentation rule. Unspecified fields are kept."""
        repo = UsetypePresentationRepository(self.session)
        row = repo.update(
            usetype,
            renderer=request.renderer,
            child_handling=request.child_handling,
            link_handling=request.link_handling,
            renderer_config=(
                request.renderer_config if request.renderer_config is not None else ...
            ),
            description=(request.description if request.description is not None else ...),
        )
        if not row:
            raise LookupError(f"Presentation for {usetype!r} not found")
        response = UsetypePresentationResponse.from_presentation(row)
        self.session.commit()
        return response

    @expose(
        "DELETE",
        "/usetype-presentations/{usetype:path}",
        errors={LookupError: 404},
        tags=["usetype-presentations"],
        summary="Delete a presentation rule.",
        status_code=204,
    )
    def delete_presentation(self, usetype: str) -> None:
        """Delete a presentation rule."""
        repo = UsetypePresentationRepository(self.session)
        if not repo.delete(usetype):
            raise LookupError(f"Presentation for {usetype!r} not found")
        self.session.commit()
