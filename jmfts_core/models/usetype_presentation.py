"""Per-usetype presentation rules — drives /view/{id} rendering decisions."""

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import DateTime, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from jmfts_core.database import Base

# Allowed values — kept as plain strings so the registry stays open
# (new renderers / handlings can be added by row insertion, no schema change).
#
# FOUR NAMES JOINED THIS TUPLE ON 2026-09-14, and it is a contract change rather than a list
# getting longer. ``docs/SPRINT_0_6_0.md`` Block F step 25 made this tuple the dispatch key a
# web front end renders by — "adding a renderer is a tuple edit plus a migration that every
# client picks up, not a branch in JavaScript that only this front end has" — and step 29 is
# the step that adds four. ``jmfts-web/jmfts_web/static/render/renderers.js`` registers one
# implementation per value and ``tests/test_web_views.py`` holds the bijection in BOTH
# directions, so a name here with nothing to render it is a red suite and so is the reverse.
#
# The first five are text: they render ``rendered_content``, which is what the caller already
# has. The four new ones render something the node does NOT carry — a page image, a cropped
# rectangle, a grid of cells, the text read out of an office file — which is why each one
# reaches ``/documents/{id}/image``, ``/region``, ``/cells`` or ``/blob`` to draw itself:
#
#   pdf-page      one page of the source PDF, with the passage boxed on it
#   image         the node's own rectangle, cropped out of the page it came from
#   sheet-region  a worksheet region as CELLS, not as a picture of cells (OFFICE_SPEC Part 7)
#   office        an office document as the markdown this appliance read out of it, saying so
#
# ``sql/migrations/023_sheet_region_presentation.sql`` is what carries the one row that uses
# any of them to a database that already exists; the other three are named by a caller rather
# than by a usetype, because which of them a document wants is a property of its FORMAT and
# this table is keyed by usetype.
RENDERERS = (
    "markdown",
    "code",
    "json-table",
    "transcript",
    "plain",
    "pdf-page",
    "image",
    "sheet-region",
    "office",
)
CHILD_HANDLINGS = ("collapsed", "inline-headings", "hidden", "first-paragraph")
LINK_HANDLINGS = ("footnotes", "inline-citations", "sidebar", "hidden")


class UsetypePresentation(Base):
    """How `/view/{id}` should render documents of a given usetype."""

    __tablename__ = "usetype_presentations"

    usetype: Mapped[str] = mapped_column(String(100), primary_key=True)
    renderer: Mapped[str] = mapped_column(String(50), nullable=False)
    renderer_config: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    child_handling: Mapped[str] = mapped_column(String(50), nullable=False)
    link_handling: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "usetype": self.usetype,
            "renderer": self.renderer,
            "renderer_config": self.renderer_config,
            "child_handling": self.child_handling,
            "link_handling": self.link_handling,
            "description": self.description,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
