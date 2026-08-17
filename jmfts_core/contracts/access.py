"""Access-control contracts — principals, API tokens, and subtree RBAC grants.

The single definition of these shapes, shared by the in-process Python API and the
generated REST adapter. FastAPI-free, so the service layer depends on it without a web
framework. See ``jmfts_core/access.py`` for the enforcement engine and
``jmfts_core/services/access_service.py`` for the owner-only management verbs.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

# --- Principals --------------------------------------------------------------------


class PrincipalCreate(BaseModel):
    """Request to create a non-owner identity."""

    name: str


class PrincipalResponse(BaseModel):
    """A principal (identity that grants are issued to)."""

    id: int
    name: str
    is_owner: bool
    created_at: Optional[datetime]

    class Config:
        from_attributes = True

    @classmethod
    def from_principal(cls, p) -> "PrincipalResponse":
        return cls(id=p.id, name=p.name, is_owner=p.is_owner, created_at=p.created_at)


# --- API tokens --------------------------------------------------------------------


class TokenCreate(BaseModel):
    """Request to mint a bearer token for a principal."""

    label: Optional[str] = None
    expires_at: Optional[datetime] = None


class TokenMintResponse(BaseModel):
    """The response to a mint — the ONLY time the raw token is ever returned. Only its
    SHA-256 is stored, so a lost token cannot be recovered, only revoked and re-minted."""

    id: int
    principal_id: int
    token: str = Field(description="The raw bearer token. Shown once; store it now.")
    label: Optional[str]
    created_at: Optional[datetime]
    expires_at: Optional[datetime]


class TokenResponse(BaseModel):
    """A token's metadata (never the token itself)."""

    id: int
    principal_id: int
    label: Optional[str]
    created_at: Optional[datetime]
    expires_at: Optional[datetime]

    class Config:
        from_attributes = True

    @classmethod
    def from_token(cls, t) -> "TokenResponse":
        return cls(
            id=t.id,
            principal_id=t.principal_id,
            label=t.label,
            created_at=t.created_at,
            expires_at=t.expires_at,
        )


# --- Grants ------------------------------------------------------------------------


class GrantCreate(BaseModel):
    """Request to grant a principal read/write on a document (making it an ACR)."""

    principal_id: int
    level: str = Field(description="'read' or 'write' (write implies read)")


class GrantResponse(BaseModel):
    """A subtree RBAC grant on an access-control root."""

    id: int
    document_id: int
    principal_id: int
    level: str
    created_at: Optional[datetime]

    class Config:
        from_attributes = True

    @classmethod
    def from_grant(cls, g) -> "GrantResponse":
        return cls(
            id=g.id,
            document_id=g.document_id,
            principal_id=g.principal_id,
            level=g.level,
            created_at=g.created_at,
        )
