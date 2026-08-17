"""TemplateService — prompt-template library operations, transport-neutral.

Logic extracted verbatim from ``api/routers/templates.py`` so the behaviour is
identical; the only intentional change is that document serialisation in the template
*search* endpoint now goes through the single ``DocumentResponse.from_document``
converter (deleting ``templates.py``'s local ``_doc_to_doc_response`` — converter 4 of
4, after which ``DocumentResponse.from_document`` is the ONLY ORM→document-response
mapping left in the codebase). The template-specific mapping keeps a single home too, as
``TemplateResponse.from_document`` in ``jmfts_core/contracts/template.py``.

Domain → HTTP mapping is declared per-op in ``@expose(errors=...)`` and keyed by
EXCEPTION TYPE, so the two hand-written statuses the router raised are reproduced without
a call-site check:

- ``LookupError``            → 404 (template not found), detail ``"Template not found"``.
- ``InvalidCategoryError``   → 422 (category outside the allowed set), detail
  ``"Invalid category '<c>'. Must be one of: <sorted list>"``.

Both detail strings are preserved verbatim.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from jmfts_core.contracts.document import DocumentResponse
from jmfts_core.contracts.search import SearchResponse, SearchResultItem
from jmfts_core.contracts.template import (
    TemplateCreate,
    TemplateRenderRequest,
    TemplateRenderResponse,
    TemplateResponse,
    TemplateSearchRequest,
    TemplateUpdate,
)
from jmfts_core.models.document import Document
from jmfts_core.registry import expose, register_service
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.search import SearchRepository

logger = logging.getLogger(__name__)

ADJUTANT_ROOT_ID = 7231
TEMPLATE_USETYPE = "adjutant:template"
VALID_CATEGORIES = {"implementation", "grooming", "evaluation", "ideation", "refinement"}

# Module-level cache for the container ID (was module-level in the router).
_container_id: Optional[int] = None


class InvalidCategoryError(Exception):
    """A category value outside the allowed set was supplied (→ HTTP 422)."""


def _validate_category(category: str) -> None:
    """Raise the 422-mapped error for an unknown category, detail preserved verbatim."""
    if category not in VALID_CATEGORIES:
        raise InvalidCategoryError(
            f"Invalid category '{category}'. Must be one of: {sorted(VALID_CATEGORIES)}"
        )


@register_service
class TemplateService:
    """Prompt-template library operations over a single database session."""

    def __init__(self, session: Session):
        self.session = session

    # -- internal helpers (were module-level functions in the router) -------------

    def _get_container_id(self) -> int:
        """Find or create the prompt_templates container under adjutant root."""
        global _container_id
        if _container_id is not None:
            return _container_id

        db = self.session
        container = (
            db.query(Document)
            .filter(
                Document.parent_id == ADJUTANT_ROOT_ID,
                Document.title == "prompt_templates",
            )
            .first()
        )
        if container:
            _container_id = container.id
            return _container_id

        # Create if missing
        repo = DocumentRepository(db)
        container = repo.create(
            title="prompt_templates",
            content=None,
            parent_id=ADJUTANT_ROOT_ID,
            usetype="adjutant:container",
            structured_content={"description": "Prompt template library for adjutant dispatches"},
            auto_embed=False,
        )
        _container_id = container.id
        logger.info("Created prompt_templates container: id=%d", container.id)
        return _container_id

    # -- CRUD endpoints -----------------------------------------------------------

    @expose(
        "GET",
        "/templates",
        response_model=list[TemplateResponse],
        errors={InvalidCategoryError: 422},
        tags=["templates"],
        summary="List all templates, optionally filtered by category",
    )
    def list_templates(self, *, category: Optional[str] = None) -> list[TemplateResponse]:
        """List all templates, optionally filtered by category."""
        db = self.session
        container_id = self._get_container_id()
        query = db.query(Document).filter(
            Document.parent_id == container_id,
            Document.usetype == TEMPLATE_USETYPE,
        )

        if category:
            _validate_category(category)
            # Filter by category in structured_content JSONB
            query = query.filter(Document.structured_content["category"].astext == category)

        docs = query.order_by(Document.title).all()
        return [TemplateResponse.from_document(d) for d in docs]

    @expose(
        "GET",
        "/templates/{template_id}",
        response_model=TemplateResponse,
        errors={LookupError: 404},
        tags=["templates"],
        summary="Get a single template by ID",
    )
    def get_template(self, template_id: int) -> TemplateResponse:
        """Get a single template by ID."""
        doc = self.session.get(Document, template_id)
        if not doc or doc.usetype != TEMPLATE_USETYPE:
            raise LookupError("Template not found")
        return TemplateResponse.from_document(doc)

    @expose(
        "POST",
        "/templates",
        response_model=TemplateResponse,
        status_code=201,
        errors={InvalidCategoryError: 422},
        tags=["templates"],
        summary="Create a new prompt template",
    )
    def create_template(self, request: TemplateCreate) -> TemplateResponse:
        """Create a new prompt template."""
        _validate_category(request.category)
        container_id = self._get_container_id()

        structured_content = {
            "category": request.category,
            "variables": [v.model_dump() for v in request.variables],
            "usage_count": 0,
            "success_rate": 0.0,
            "last_used": None,
        }

        repo = DocumentRepository(self.session)
        doc = repo.create(
            title=request.title,
            content=request.content,
            parent_id=container_id,
            usetype=TEMPLATE_USETYPE,
            structured_content=structured_content,
            auto_embed=True,
        )
        response = TemplateResponse.from_document(doc)
        # Commit before responding: get_db's teardown commit runs after the
        # response is sent, so a client acting on the returned id would race it.
        self.session.commit()
        return response

    @expose(
        "PUT",
        "/templates/{template_id}",
        response_model=TemplateResponse,
        errors={LookupError: 404, InvalidCategoryError: 422},
        tags=["templates"],
        summary="Update a template's content or metadata",
    )
    def update_template(self, template_id: int, request: TemplateUpdate) -> TemplateResponse:
        """Update a template's content or metadata."""
        doc = self.session.get(Document, template_id)
        if not doc or doc.usetype != TEMPLATE_USETYPE:
            raise LookupError("Template not found")

        if request.category is not None:
            _validate_category(request.category)

        repo = DocumentRepository(self.session)

        # Build updated structured_content
        sc = dict(doc.structured_content or {})
        if request.category is not None:
            sc["category"] = request.category
        if request.variables is not None:
            sc["variables"] = [v.model_dump() for v in request.variables]

        updated = repo.update(
            document_id=template_id,
            title=request.title,
            content=request.content,
            structured_content=sc,
            re_embed=request.content is not None,
        )
        response = TemplateResponse.from_document(updated)
        self.session.commit()
        return response

    # -- Render endpoint ----------------------------------------------------------

    @expose(
        "POST",
        "/templates/{template_id}/render",
        response_model=TemplateRenderResponse,
        errors={LookupError: 404},
        tags=["templates"],
        summary="Render a template by substituting {{variable}} placeholders",
    )
    def render_template(
        self, template_id: int, request: TemplateRenderRequest
    ) -> TemplateRenderResponse:
        """Render a template by substituting {{variable}} placeholders."""
        doc = self.session.get(Document, template_id)
        if not doc or doc.usetype != TEMPLATE_USETYPE:
            raise LookupError("Template not found")

        template_body = doc.content or ""
        sc = doc.structured_content or {}
        defined_vars = {v["name"] for v in sc.get("variables", [])}
        required_vars = {v["name"] for v in sc.get("variables", []) if v.get("required", True)}

        # Find all placeholders in template
        placeholders = set(re.findall(r"\{\{(\w+)\}\}", template_body))

        # Check for missing required variables
        missing = (required_vars | (placeholders - defined_vars)) - set(request.variables.keys())

        # Substitute all provided variables
        rendered = template_body
        for var_name, var_value in request.variables.items():
            rendered = rendered.replace("{{" + var_name + "}}", var_value)

        # Bump usage_count
        sc["usage_count"] = sc.get("usage_count", 0) + 1
        sc["last_used"] = datetime.now(timezone.utc).isoformat()
        doc.structured_content = sc
        # Mark structured_content as modified for JSONB
        flag_modified(doc, "structured_content")
        self.session.commit()

        return TemplateRenderResponse(
            rendered=rendered,
            template_id=template_id,
            missing_variables=sorted(missing),
        )

    # -- Search endpoint ----------------------------------------------------------

    @expose(
        "POST",
        "/templates/search",
        response_model=SearchResponse,
        errors={InvalidCategoryError: 422},
        tags=["templates"],
        summary="Semantic search for templates by description",
    )
    def search_templates(self, request: TemplateSearchRequest) -> SearchResponse:
        """Semantic search for templates by description."""
        container_id = self._get_container_id()
        start = time.time()

        search_repo = SearchRepository(self.session)
        results = search_repo.vector_search_text(
            query_text=request.query,
            limit=request.limit,
            usetype=TEMPLATE_USETYPE,
            parent_id=container_id,
        )

        # Post-filter by category if specified
        if request.category:
            _validate_category(request.category)
            results = [
                r
                for r in results
                if (r.document.structured_content or {}).get("category") == request.category
            ]

        latency_ms = (time.time() - start) * 1000
        return SearchResponse(
            results=[
                SearchResultItem(
                    document=DocumentResponse.from_document(r.document),
                    score=r.score,
                    method=r.method,
                )
                for r in results
            ],
            total=len(results),
            latency_ms=latency_ms,
        )
