"""Access-control models: principals, API tokens, and subtree RBAC grants.

See ``migration 006`` for the DDL and ``jmfts_core/access.py`` for the enforcement
engine. The shared "owner" bearer (``JMFTS_API_TOKEN`` / the ephemeral boot token) is
SYNTHETIC — it bypasses all checks and is NOT a ``principals`` row.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from jmfts_core.database import Base


class Principal(Base):
    """A non-owner identity that access grants can be issued to."""

    __tablename__ = "principals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    is_owner: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class ApiToken(Base):
    """A bearer token → principal mapping. Only the SHA-256 hex of the token is
    stored, never the token itself; auth hashes the presented bearer and looks it up.
    The owner token is matched separately (constant-time, no DB), so it needs no row.
    """

    __tablename__ = "api_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    principal_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("principals.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    label: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class AccessGrant(Base):
    """A subtree RBAC grant. ``document_id`` is an access-control root (ACR): being an
    ACR is DEFINED as appearing here — there is no flag on ``documents``. A principal's
    effective right on a document is the HIGHEST level granted on any ACR at-or-above it
    on its tree ``path`` (max-over-path; grants are additive). ``write`` implies ``read``.
    """

    __tablename__ = "access_grants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    principal_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("principals.id", ondelete="CASCADE"), nullable=False
    )
    level: Mapped[str] = mapped_column(String(10), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("document_id", "principal_id", name="uq_access_grants_doc_principal"),
        CheckConstraint("level IN ('read', 'write')", name="ck_access_grants_level"),
    )
