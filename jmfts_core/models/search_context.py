"""Search context presets - named bundles of search parameters"""

from datetime import datetime
from typing import Optional, Any
from sqlalchemy import String, Text, Integer, DateTime
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import JSONB

from jmfts_core.database import Base


class SearchContext(Base):
    """Named search context preset bundling search parameters"""

    __tablename__ = "search_contexts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Bundled search parameters
    # {
    #   "method": "hybrid",           -- default search method
    #   "methods": ["vector", "bm25"],-- methods for hybrid search
    #   "weights": {"vector": 0.86, "bm25": 0.14},
    #   "usetype": "conversation/*",  -- usetype filter (supports wildcards)
    #   "parent_id": 42,              -- scope to subtree
    #   "index_name": "default",      -- BM25 index
    #   "threshold": 0.0,             -- minimum similarity
    #   "limit": 10,                  -- default result limit
    # }
    config: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "config": self.config,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
