"""Access-control contracts — principals, API tokens, and subtree RBAC grants.

The single definition of these shapes, shared by the in-process Python API and the
generated REST adapter. FastAPI-free, so the service layer depends on it without a web
framework. See ``jmfts_core/access.py`` for the enforcement engine and
``jmfts_core/services/access_service.py`` for the owner-only management verbs.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

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

    model_config = ConfigDict(from_attributes=True)

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

    model_config = ConfigDict(from_attributes=True)

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

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_grant(cls, g) -> "GrantResponse":
        return cls(
            id=g.id,
            document_id=g.document_id,
            principal_id=g.principal_id,
            level=g.level,
            created_at=g.created_at,
        )


class UngovernedTree(BaseModel):
    """One top-level tree with no access-control root over it.

    ``documents`` counts the whole subtree including this node; ``ungoverned`` counts the
    part of it that no ACR reaches. The two differ when a DESCENDANT carries grants — a
    deeper ACR governs its own subtree even though the tree above it is open.
    """

    id: int
    title: Optional[str] = None
    usetype: Optional[str] = None
    documents: int = Field(description="Documents in this subtree, this node included.")
    ungoverned: int = Field(
        description="How many of them no access-control root reaches. Readable and "
        "writable by any authenticated principal."
    )


class AccessAuditResponse(BaseModel):
    """What is NOT protected, which is the question grants alone cannot answer.

    A document under no access-control root is readable and writable by anyone with a
    token. That is the single-user default and it is deliberate — restriction is opt-in —
    but until this endpoint existed there was no way to ask which documents were in that
    state, so "open by default" and "open by accident" looked identical.

    Every count is over the whole corpus and ignores the caller's own grants: this is an
    audit of the appliance's configuration, not a view of what the caller may read.
    """

    total_documents: int
    ungoverned_documents: int = Field(description="Documents no access-control root reaches.")
    access_control_roots: int = Field(
        description="Documents carrying at least one grant. Zero means every document is "
        "open, which is the untouched default."
    )
    trees: list[UngovernedTree] = Field(
        description="Top-level trees with no grant on the root itself, worst first. A "
        "tree is absent when its root is an ACR, because everything below an ACR is "
        "governed by it."
    )
    truncated: bool = Field(
        description="True when `trees` was cut at the requested limit; the counts above "
        "are still over the whole corpus."
    )
