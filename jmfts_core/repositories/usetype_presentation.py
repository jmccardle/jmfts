"""Usetype presentation repository — CRUD over the rendering-rules table."""

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from jmfts_core.models.usetype_presentation import (
    CHILD_HANDLINGS,
    LINK_HANDLINGS,
    RENDERERS,
    UsetypePresentation,
)

_DEFAULT_FALLBACK_USETYPE = "*"


class UsetypePresentationRepository:
    """CRUD for the ``usetype_presentations`` table."""

    def __init__(self, session: Session):
        self.session = session

    def create(
        self,
        *,
        usetype: str,
        renderer: str,
        child_handling: str,
        link_handling: str,
        renderer_config: Optional[dict] = None,
        description: Optional[str] = None,
    ) -> UsetypePresentation:
        _validate(renderer, child_handling, link_handling)
        row = UsetypePresentation(
            usetype=usetype,
            renderer=renderer,
            renderer_config=renderer_config or {},
            child_handling=child_handling,
            link_handling=link_handling,
            description=description,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def get(self, usetype: str) -> Optional[UsetypePresentation]:
        return self.session.get(UsetypePresentation, usetype)

    def list_all(self) -> list[UsetypePresentation]:
        return list(self.session.execute(select(UsetypePresentation)).scalars().all())

    def update(
        self,
        usetype: str,
        *,
        renderer: Optional[str] = None,
        child_handling: Optional[str] = None,
        link_handling: Optional[str] = None,
        renderer_config: Optional[dict] = ...,  # type: ignore[assignment]
        description: Optional[str] = ...,  # type: ignore[assignment]
    ) -> Optional[UsetypePresentation]:
        row = self.get(usetype)
        if not row:
            return None
        if renderer is not None:
            row.renderer = renderer
        if child_handling is not None:
            row.child_handling = child_handling
        if link_handling is not None:
            row.link_handling = link_handling
        if renderer_config is not ...:
            row.renderer_config = renderer_config or {}
        if description is not ...:
            row.description = description
        _validate(row.renderer, row.child_handling, row.link_handling)
        self.session.flush()
        return row

    def delete(self, usetype: str) -> bool:
        row = self.get(usetype)
        if not row:
            return False
        self.session.delete(row)
        self.session.flush()
        return True

    # -- resolution -------------------------------------------------------

    def resolve(self, usetype: Optional[str]) -> UsetypePresentation:
        """Look up the presentation for ``usetype``, falling back to '*' or a default.

        Resolution order:
          1. exact ``usetype`` match
          2. ``*`` row (catch-all) if present
          3. synthetic default (markdown / collapsed / footnotes)

        The synthetic default is *not* persisted — callers always get a valid
        presentation back without touching the DB twice.
        """
        if usetype:
            row = self.get(usetype)
            if row:
                return row
        fallback = self.get(_DEFAULT_FALLBACK_USETYPE)
        if fallback:
            return fallback
        return UsetypePresentation(
            usetype=usetype or _DEFAULT_FALLBACK_USETYPE,
            renderer="markdown",
            renderer_config={},
            child_handling="collapsed",
            link_handling="footnotes",
            description="(synthetic default)",
        )


def _validate(renderer: str, child_handling: str, link_handling: str) -> None:
    if renderer not in RENDERERS:
        raise ValueError(f"renderer must be one of {RENDERERS}, got {renderer!r}")
    if child_handling not in CHILD_HANDLINGS:
        raise ValueError(f"child_handling must be one of {CHILD_HANDLINGS}, got {child_handling!r}")
    if link_handling not in LINK_HANDLINGS:
        raise ValueError(f"link_handling must be one of {LINK_HANDLINGS}, got {link_handling!r}")
