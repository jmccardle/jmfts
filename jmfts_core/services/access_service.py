"""AccessService — owner-only management of principals, API tokens, and RBAC grants.

Every verb here is gated to the owner (``require_owner``): identity and grant
administration is the human's, not a delegated agent's. A bound non-owner principal that
reaches one of these gets 403; unbound in-process callers (seeding scripts) pass through.

The enforcement engine that consumes this state lives in ``jmfts_core/access.py``; a grant
created here makes its document an access-control root purely by existing (there is no
flag on ``documents``).
"""

from __future__ import annotations

import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from jmfts_core.access import hash_token, require_owner
from jmfts_core.contracts.access import (
    GrantCreate,
    GrantResponse,
    PrincipalCreate,
    PrincipalResponse,
    TokenCreate,
    TokenMintResponse,
    TokenResponse,
)
from jmfts_core.models.document import Document
from jmfts_core.models.principal import AccessGrant, ApiToken, Principal
from jmfts_core.registry import expose, register_service


class PrincipalConflictError(Exception):
    """A principal with the requested name already exists (→ 409)."""


@register_service
class AccessService:
    """Access-control administration over a single database session (owner-only)."""

    def __init__(self, session: Session):
        self.session = session

    # -- Principals ---------------------------------------------------------------

    @expose(
        "POST",
        "/access/principals",
        response_model=PrincipalResponse,
        errors={PrincipalConflictError: 409},
        tags=["access"],
        summary="Create a non-owner principal",
        status_code=201,
    )
    def create_principal(self, request: PrincipalCreate) -> PrincipalResponse:
        """Create a non-owner principal (identity that grants are issued to)."""
        require_owner()
        if self.session.execute(
            select(Principal).where(Principal.name == request.name)
        ).scalar_one_or_none():
            raise PrincipalConflictError(f"Principal '{request.name}' already exists")
        # is_owner stays False: the owner is the synthetic shared bearer, never a row.
        p = Principal(name=request.name, is_owner=False)
        self.session.add(p)
        self.session.flush()
        response = PrincipalResponse.from_principal(p)
        self.session.commit()
        return response

    @expose(
        "GET",
        "/access/principals",
        response_model=list[PrincipalResponse],
        tags=["access"],
        summary="List principals",
    )
    def list_principals(self) -> list[PrincipalResponse]:
        """List all principals."""
        require_owner()
        rows = self.session.execute(select(Principal).order_by(Principal.id)).scalars().all()
        return [PrincipalResponse.from_principal(p) for p in rows]

    @expose(
        "DELETE",
        "/access/principals/{principal_id}",
        errors={LookupError: 404},
        tags=["access"],
        summary="Delete a principal (cascades its tokens and grants)",
        status_code=204,
    )
    def delete_principal(self, principal_id: int) -> None:
        """Delete a principal; its API tokens and grants cascade away with it."""
        require_owner()
        p = self.session.get(Principal, principal_id)
        if p is None:
            raise LookupError(f"Principal {principal_id} not found")
        self.session.delete(p)
        self.session.commit()

    # -- API tokens ---------------------------------------------------------------

    @expose(
        "POST",
        "/access/principals/{principal_id}/tokens",
        response_model=TokenMintResponse,
        errors={LookupError: 404},
        tags=["access"],
        summary="Mint a bearer token for a principal (raw token returned once)",
        status_code=201,
    )
    def mint_token(self, principal_id: int, request: TokenCreate) -> TokenMintResponse:
        """Mint a bearer token. The raw token is returned ONCE and never stored — only its
        SHA-256 is persisted, so it can be revoked but never recovered."""
        require_owner()
        p = self.session.get(Principal, principal_id)
        if p is None:
            raise LookupError(f"Principal {principal_id} not found")
        raw = secrets.token_urlsafe(32)
        row = ApiToken(
            principal_id=principal_id,
            token_hash=hash_token(raw),
            label=request.label,
            expires_at=request.expires_at,
        )
        self.session.add(row)
        self.session.flush()
        response = TokenMintResponse(
            id=row.id,
            principal_id=principal_id,
            token=raw,
            label=row.label,
            created_at=row.created_at,
            expires_at=row.expires_at,
        )
        self.session.commit()
        return response

    @expose(
        "GET",
        "/access/principals/{principal_id}/tokens",
        response_model=list[TokenResponse],
        errors={LookupError: 404},
        tags=["access"],
        summary="List a principal's tokens (metadata only)",
    )
    def list_tokens(self, principal_id: int) -> list[TokenResponse]:
        """List a principal's tokens — metadata only, never the raw token."""
        require_owner()
        if self.session.get(Principal, principal_id) is None:
            raise LookupError(f"Principal {principal_id} not found")
        rows = (
            self.session.execute(
                select(ApiToken).where(ApiToken.principal_id == principal_id).order_by(ApiToken.id)
            )
            .scalars()
            .all()
        )
        return [TokenResponse.from_token(t) for t in rows]

    @expose(
        "DELETE",
        "/access/tokens/{token_id}",
        errors={LookupError: 404},
        tags=["access"],
        summary="Revoke a token",
        status_code=204,
    )
    def revoke_token(self, token_id: int) -> None:
        """Revoke (delete) a bearer token."""
        require_owner()
        row = self.session.get(ApiToken, token_id)
        if row is None:
            raise LookupError(f"Token {token_id} not found")
        self.session.delete(row)
        self.session.commit()

    # -- Grants -------------------------------------------------------------------

    @expose(
        "POST",
        "/access/documents/{document_id}/grants",
        response_model=GrantResponse,
        errors={LookupError: 404, ValueError: 400},
        tags=["access"],
        summary="Grant a principal read/write on a document (makes it an ACR)",
        status_code=201,
    )
    def grant(self, document_id: int, request: GrantCreate) -> GrantResponse:
        """Grant (or update) a principal's level on a document, making it an access-control
        root. Idempotent per (document, principal): re-granting updates the level."""
        require_owner()
        if request.level not in ("read", "write"):
            raise ValueError("level must be 'read' or 'write'")
        if self.session.get(Document, document_id) is None:
            raise LookupError(f"Document {document_id} not found")
        if self.session.get(Principal, request.principal_id) is None:
            raise LookupError(f"Principal {request.principal_id} not found")

        existing = self.session.execute(
            select(AccessGrant).where(
                AccessGrant.document_id == document_id,
                AccessGrant.principal_id == request.principal_id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.level = request.level
            grant = existing
        else:
            grant = AccessGrant(
                document_id=document_id,
                principal_id=request.principal_id,
                level=request.level,
            )
            self.session.add(grant)
        self.session.flush()
        response = GrantResponse.from_grant(grant)
        self.session.commit()
        return response

    @expose(
        "GET",
        "/access/documents/{document_id}/grants",
        response_model=list[GrantResponse],
        tags=["access"],
        summary="List the grants on a document (its ACR grants)",
    )
    def list_grants(self, document_id: int) -> list[GrantResponse]:
        """List every grant on a document. Empty means the document is not an ACR."""
        require_owner()
        rows = (
            self.session.execute(
                select(AccessGrant)
                .where(AccessGrant.document_id == document_id)
                .order_by(AccessGrant.id)
            )
            .scalars()
            .all()
        )
        return [GrantResponse.from_grant(g) for g in rows]

    @expose(
        "DELETE",
        "/access/documents/{document_id}/grants/{principal_id}",
        errors={LookupError: 404},
        tags=["access"],
        summary="Revoke a principal's grant (un-marks the ACR when the last one goes)",
        status_code=204,
    )
    def revoke_grant(self, document_id: int, principal_id: int) -> None:
        """Revoke a principal's grant on a document. When the last grant is removed the
        document is no longer an access-control root (ACR-ness is defined by having grants)."""
        require_owner()
        row = self.session.execute(
            select(AccessGrant).where(
                AccessGrant.document_id == document_id,
                AccessGrant.principal_id == principal_id,
            )
        ).scalar_one_or_none()
        if row is None:
            raise LookupError(f"No grant for principal {principal_id} on document {document_id}")
        self.session.delete(row)
        self.session.commit()
