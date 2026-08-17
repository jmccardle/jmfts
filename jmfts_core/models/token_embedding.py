"""Token-level embeddings for late interaction retrieval"""

from datetime import datetime
from typing import Optional, List, Any, TYPE_CHECKING
from sqlalchemy import Text, Integer, Float, ForeignKey, DateTime, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from pgvector.sqlalchemy import HALFVEC

from jmfts_core.database import Base

if TYPE_CHECKING:
    from jmfts_core.models.document import Document


class TokenEmbedding(Base):
    """
    Token-level embeddings for late interaction (ColBERT-style) retrieval.

    Stores the top N% most significant tokens from each document at reduced
    matryoshka dimensions for efficient late interaction scoring.
    """

    __tablename__ = "token_embeddings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    token_idx: Mapped[int] = mapped_column(Integer, nullable=False)
    token_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Token importance score (higher = more important)
    # Typically computed as IDF * attention_weight
    importance_score: Mapped[float] = mapped_column(Float, nullable=False)

    # Tier for percentage-based filtering (5, 10, 15, 20, 25, 30, 35, 40, 45, 50)
    # tier=5 means top 5%, tier=10 means 5-10%, etc.
    # Query with tier <= X to get top X% of tokens
    tier: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Matryoshka embedding at 256-dim halfvec (FP16, validated zero quality loss vs float32)
    embed_256: Mapped[Optional[List[float]]] = mapped_column(HALFVEC(256), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    # Relationship
    document: Mapped["Document"] = relationship("Document", back_populates="token_embeddings")

    __table_args__ = (
        UniqueConstraint("document_id", "token_idx", name="uq_token_doc_idx"),
    )

    def to_dict(self, include_embeds: bool = False) -> dict[str, Any]:
        result = {
            "id": self.id,
            "document_id": self.document_id,
            "token_idx": self.token_idx,
            "token_text": self.token_text,
            "importance_score": self.importance_score,
            "tier": self.tier,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
        if include_embeds and self.embed_256 is not None:
            result["embed_256"] = list(self.embed_256)
        return result
