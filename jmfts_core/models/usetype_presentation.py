"""Per-usetype presentation rules — drives /view/{id} rendering decisions."""

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import DateTime, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from jmfts_core.database import Base

# Allowed values — kept as plain strings so the registry stays open
# (new renderers / handlings can be added by row insertion, no schema change).
RENDERERS = ("markdown", "code", "json-table", "transcript", "plain")
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
