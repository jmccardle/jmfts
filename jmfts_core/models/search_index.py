"""Search index models for BM25"""

from datetime import datetime
from typing import Optional, Any
from sqlalchemy import String, Text, Integer, Float, ForeignKey, DateTime
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import JSONB

from jmfts_core.database import Base


class SearchIndex(Base):
    """BM25 search index definition"""

    __tablename__ = "search_indexes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # BM25 configuration
    config: Mapped[dict] = mapped_column(JSONB, default=lambda: {"k1": 1.2, "b": 0.75})

    # Corpus statistics
    total_docs: Mapped[int] = mapped_column(Integer, default=0)
    avg_doc_length: Mapped[float] = mapped_column(Float, default=0.0)

    # Capabilities
    capabilities: Mapped[dict] = mapped_column(
        JSONB, default=lambda: {"bm25": True, "maxsim": False}
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "config": self.config,
            "total_docs": self.total_docs,
            "avg_doc_length": self.avg_doc_length,
            "capabilities": self.capabilities,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class SearchIndexMember(Base):
    """Association between search indexes and document subtrees"""

    __tablename__ = "search_index_members"

    index_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("search_indexes.id", ondelete="CASCADE"), primary_key=True
    )
    root_document_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class SearchIndexEntry(Base):
    """Per-document entry in a search index (for doc length normalization)"""

    __tablename__ = "search_index_entries"

    index_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("search_indexes.id", ondelete="CASCADE"), primary_key=True
    )
    document_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    doc_length: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class SearchTermPosting(Base):
    """Inverted index: term -> document postings"""

    __tablename__ = "search_term_postings"

    index_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    term: Mapped[str] = mapped_column(Text, primary_key=True)
    document_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    term_freq: Mapped[int] = mapped_column(Integer, nullable=False)


class SearchTermStats(Base):
    """Per-term statistics for IDF calculation"""

    __tablename__ = "search_term_stats"

    index_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("search_indexes.id", ondelete="CASCADE"), primary_key=True
    )
    term: Mapped[str] = mapped_column(Text, primary_key=True)
    doc_freq: Mapped[int] = mapped_column(Integer, nullable=False)
