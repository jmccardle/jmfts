"""SearchContextService — named search-preset CRUD, transport-neutral.

Logic lifted verbatim from ``api/routers/search_contexts.py`` so the behaviour is
identical; the only single-sourcing change is that ``SearchContext -> SearchContextResponse``
now goes through the one ``SearchContextResponse.from_context`` converter (the old router
returned the ORM row and let FastAPI's ``response_model`` serialise it — byte-identical).

Domain → HTTP mapping is declared per-op in ``@expose(errors=...)`` and keyed by EXCEPTION
TYPE, so the two hand-written statuses the router raised are reproduced without a
call-site check:

- ``LookupError``                 → 404 (context not found), detail ``"Context '<name>' not found"``.
- ``SearchContextConflictError``  → 409 (name already exists), detail
  ``"Context '<name>' already exists"``.

All detail strings are preserved verbatim.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from jmfts_core.contracts.search_context import (
    SearchContextCreate,
    SearchContextResponse,
    SearchContextUpdate,
)
from jmfts_core.registry import expose, register_service
from jmfts_core.repositories.search_context import SearchContextRepository


class SearchContextConflictError(Exception):
    """A search context with the requested name already exists (→ HTTP 409)."""


@register_service
class SearchContextService:
    """Search-context (named-preset) CRUD over a single database session."""

    def __init__(self, session: Session):
        self.session = session

    @expose(
        "GET",
        "/search-contexts/",
        response_model=list[SearchContextResponse],
        tags=["search-contexts"],
        summary="List all search context presets",
    )
    def list_contexts(self) -> list[SearchContextResponse]:
        """List all search context presets"""
        repo = SearchContextRepository(self.session)
        return [SearchContextResponse.from_context(ctx) for ctx in repo.list_all()]

    @expose(
        "POST",
        "/search-contexts/",
        response_model=SearchContextResponse,
        errors={SearchContextConflictError: 409},
        tags=["search-contexts"],
        summary="Create a new search context preset",
        status_code=201,
    )
    def create_context(self, request: SearchContextCreate) -> SearchContextResponse:
        """Create a new search context preset"""
        repo = SearchContextRepository(self.session)
        if repo.get_by_name(request.name):
            raise SearchContextConflictError(f"Context '{request.name}' already exists")
        ctx = repo.create(
            name=request.name,
            config=request.config,
            description=request.description,
        )
        response = SearchContextResponse.from_context(ctx)
        # Commit before responding: get_db's teardown commit runs after the
        # response is sent, so a client acting on the result would race it.
        self.session.commit()
        return response

    @expose(
        "GET",
        "/search-contexts/{name}",
        response_model=SearchContextResponse,
        errors={LookupError: 404},
        tags=["search-contexts"],
        summary="Get a search context preset by name",
    )
    def get_context(self, name: str) -> SearchContextResponse:
        """Get a search context preset by name"""
        repo = SearchContextRepository(self.session)
        ctx = repo.get_by_name(name)
        if not ctx:
            raise LookupError(f"Context '{name}' not found")
        return SearchContextResponse.from_context(ctx)

    @expose(
        "PUT",
        "/search-contexts/{name}",
        response_model=SearchContextResponse,
        errors={LookupError: 404},
        tags=["search-contexts"],
        summary="Update a search context preset",
    )
    def update_context(self, name: str, request: SearchContextUpdate) -> SearchContextResponse:
        """Update a search context preset"""
        repo = SearchContextRepository(self.session)
        ctx = repo.update(
            name=name,
            config=request.config,
            description=request.description,
        )
        if not ctx:
            raise LookupError(f"Context '{name}' not found")
        response = SearchContextResponse.from_context(ctx)
        self.session.commit()
        return response

    @expose(
        "DELETE",
        "/search-contexts/{name}",
        errors={LookupError: 404},
        tags=["search-contexts"],
        summary="Delete a search context preset",
        status_code=204,
    )
    def delete_context(self, name: str) -> None:
        """Delete a search context preset"""
        repo = SearchContextRepository(self.session)
        if not repo.delete(name):
            raise LookupError(f"Context '{name}' not found")
        self.session.commit()
