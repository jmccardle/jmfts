"""ViewService — the /view/* direct-readability operations, transport-neutral.

Logic lifted verbatim from ``api/routers/view.py`` so the behaviour is identical; no
document is serialised through ``DocumentResponse`` on this surface (the view endpoints
return their own render-ready models — ``ViewResponse``/``BreadcrumbResponse``/
``BackReferenceResponse`` and the ``ViewChildStub`` list — never a raw document), so the
shared ``DocumentResponse.from_document`` converter is not involved here and there was no
local ``doc_to_response`` in ``view.py`` to fold in.

Domain → HTTP mapping is declared per-op in ``@expose(errors=...)`` and keyed by EXCEPTION
TYPE, reproducing the two statuses the router raised without a call-site check:

- ``LookupError``          → 404 (document not found), detail ``"Document <id> not found"``.
- ``InvalidIncludeError``  → 422 (unknown ``include`` keys), detail
  ``"Unknown include keys: <sorted list>"``.

``InvalidIncludeError`` is a plain ``Exception`` subclass (not a ``LookupError`` subclass),
so the adapter's MRO-nearest mapping keeps 404 and 422 distinct. All detail strings are
preserved verbatim.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from jmfts_client.contracts.view import (
    BackReferenceItem,
    BackReferenceResponse,
    BreadcrumbResponse,
    ViewAncestor,
    ViewChildStub,
    ViewLinkRef,
    ViewPresentation,
    ViewResponse,
    ViewTripleRef,
)
from jmfts_core.access import can_read
from jmfts_core.models.document import Document
from jmfts_core.registry import expose, register_service
from jmfts_core.repositories.usetype_presentation import UsetypePresentationRepository
from jmfts_core.repositories.view import ViewRepository
from jmfts_core.view_renderer import build_children_stubs, resolve_references

_VALID_INCLUDES = {"children", "links", "triples", "siblings"}


class InvalidIncludeError(Exception):
    """The ``include`` CSV carried unknown keys (→ HTTP 422)."""


def _parse_includes(include: Optional[str]) -> set[str]:
    if not include:
        return {"children", "links", "triples"}
    parsed = {s.strip() for s in include.split(",") if s.strip()}
    bad = parsed - _VALID_INCLUDES
    if bad:
        raise InvalidIncludeError(f"Unknown include keys: {sorted(bad)}")
    return parsed


@register_service
class ViewService:
    """Direct-readability /view/* operations over a single database session."""

    def __init__(self, session: Session):
        self.session = session

    @expose(
        "GET",
        "/view/breadcrumbs/{document_id}",
        response_model=BreadcrumbResponse,
        errors={LookupError: 404},
        tags=["view"],
        summary="Ancestor chain for a document, root-first",
    )
    def get_breadcrumbs(self, document_id: int) -> BreadcrumbResponse:
        """Ancestor chain for a document, root-first."""
        repo = ViewRepository(self.session)
        ancestors = repo.get_breadcrumbs(document_id)
        if ancestors is None:
            raise LookupError(f"Document {document_id} not found")
        return BreadcrumbResponse(
            document_id=document_id,
            breadcrumbs=[
                ViewAncestor(id=a.id, title=a.title, usetype=a.usetype) for a in ancestors
            ],
        )

    @expose(
        "GET",
        "/view/back-references/{document_id}",
        response_model=BackReferenceResponse,
        errors={LookupError: 404},
        tags=["view"],
        summary="Documents whose links or triples target this document",
    )
    def get_back_references(self, document_id: int, *, limit: int = 50) -> BackReferenceResponse:
        """Documents whose links or triples target this document."""
        repo = ViewRepository(self.session)
        # Subtree RBAC: unreadable target == missing (404); repo.get_back_references also
        # hides referrers the principal cannot read.
        target = repo.session.get(Document, document_id)
        if not target or not can_read(self.session, target):
            raise LookupError(f"Document {document_id} not found")
        refs = repo.get_back_references(document_id, limit=limit)
        return BackReferenceResponse(
            document_id=document_id,
            total=len(refs),
            references=[BackReferenceItem(**r) for r in refs],
        )

    @expose(
        "GET",
        "/view/{document_id}/expand-children",
        response_model=list[ViewChildStub],
        errors={LookupError: 404},
        tags=["view"],
        summary="Lazy-load additional children",
    )
    def expand_children(
        self, document_id: int, *, offset: int = 0, limit: int = 20
    ) -> list[ViewChildStub]:
        """Lazy-load additional children. Same shape as the children block in /view/{id}."""
        view_repo = ViewRepository(self.session)
        pres_repo = UsetypePresentationRepository(self.session)
        bundle = view_repo.get_bundle(
            document_id, include={"children"}, limit_children=limit, children_offset=offset
        )
        if bundle is None:
            raise LookupError(f"Document {document_id} not found")
        presentation = pres_repo.resolve(bundle.document.usetype)
        stubs = build_children_stubs(bundle.children, child_handling=presentation.child_handling)
        return [ViewChildStub(**s) for s in stubs]

    @expose(
        "GET",
        "/view/{document_id}",
        response_model=ViewResponse,
        errors={LookupError: 404, InvalidIncludeError: 422},
        tags=["view"],
        summary="Render-ready view of a document for direct human/agent reading",
    )
    def view_document(
        self,
        document_id: int,
        *,
        include: Optional[str] = None,
        limit_children: int = 20,
        link_direction: str = "both",
    ) -> ViewResponse:
        """Render-ready view of a document for direct human/agent reading."""
        includes = _parse_includes(include)

        view_repo = ViewRepository(self.session)
        pres_repo = UsetypePresentationRepository(self.session)

        bundle = view_repo.get_bundle(
            document_id,
            include=includes,
            limit_children=limit_children,
            link_direction=link_direction,
        )
        if bundle is None:
            raise LookupError(f"Document {document_id} not found")

        presentation = pres_repo.resolve(bundle.document.usetype)

        # Title lookup table for footnote rendering
        title_lookup: dict[int, Optional[str]] = {}
        for a in bundle.ancestors:
            title_lookup[a.id] = a.title
        for c in bundle.children:
            title_lookup[c["id"]] = c.get("title")
        for lk in bundle.outbound_links + bundle.inbound_links:
            if lk.get("title") and lk.get("_other_id") is not None:
                title_lookup[lk["_other_id"]] = lk["title"]
        for tp in bundle.triples:
            if tp.get("subject_title"):
                title_lookup[tp["subject_id"]] = tp["subject_title"]
            if tp.get("object_title") and tp.get("object_id") is not None:
                title_lookup[tp["object_id"]] = tp["object_title"]

        rendered = resolve_references(
            bundle.document.content,
            link_handling=presentation.link_handling,
            title_lookup=title_lookup,
            outbound_links=bundle.outbound_links,
            triples=bundle.triples,
        )

        children_stubs = build_children_stubs(
            bundle.children, child_handling=presentation.child_handling
        )

        def _link_to_ref(lk: dict) -> ViewLinkRef:
            target = lk["_other_id"]
            return ViewLinkRef(
                id=lk["id"],
                source_id=lk["source_id"],
                target_id=lk["target_id"],
                direction=lk["direction"],
                link_type=lk["link_type"],
                score=lk["score"],
                title=lk.get("title"),
                target_url=f"/view/{target}",
            )

        return ViewResponse(
            id=bundle.document.id,
            title=bundle.document.title,
            usetype=bundle.document.usetype,
            renderer=presentation.renderer,
            rendered_content=rendered,
            ancestors=[
                ViewAncestor(id=a.id, title=a.title, usetype=a.usetype) for a in bundle.ancestors
            ],
            children_stubs=[ViewChildStub(**s) for s in children_stubs],
            outbound_links=[_link_to_ref(lk) for lk in bundle.outbound_links],
            inbound_links=[_link_to_ref(lk) for lk in bundle.inbound_links],
            triples=[
                ViewTripleRef(
                    id=t["id"],
                    subject_id=t["subject_id"],
                    subject_title=t.get("subject_title"),
                    predicate_name=t.get("predicate_name"),
                    object_id=t["object_id"],
                    object_title=t.get("object_title"),
                    object_literal=t.get("object_literal"),
                    object_datatype=t.get("object_datatype"),
                    fact_type=t.get("fact_type"),
                    valid_from=t.get("valid_from"),
                    valid_until=t.get("valid_until"),
                    subject_url=f"/view/{t['subject_id']}",
                    # A literal object has no node to navigate to, so it gets no URL —
                    # rather than a `/view/None` that 404s for everyone who follows it.
                    object_url=(f"/view/{t['object_id']}" if t["object_id"] is not None else None),
                )
                for t in bundle.triples
            ],
            presentation=ViewPresentation(
                renderer=presentation.renderer,
                child_handling=presentation.child_handling,
                link_handling=presentation.link_handling,
                renderer_config=presentation.renderer_config or {},
            ),
        )
