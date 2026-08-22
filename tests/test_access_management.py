"""Subtree RBAC — identity resolution + owner-only management + HTTP enforcement (stage 3).

Three things land in this stage and are tested here:
  1. AccessService management verbs (principals, tokens, grants), owner-only.
  2. resolve_principal_token: a minted bearer resolves to its principal; unknown/expired → None.
  3. End-to-end HTTP: a bound non-owner principal is filtered on reads and 403'd on writes,
     proving the request principal propagates from the auth dependency through FastAPI's
     threadpool into the repositories, and that AccessDeniedError reaches the 403 handler.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from jmfts_core.access import hash_token, resolve_principal_token
from jmfts_client.contracts.access import GrantCreate, PrincipalCreate, TokenCreate
from jmfts_core.models.principal import AccessGrant, ApiToken, Principal
from jmfts_core.principal_context import OWNER, CurrentPrincipal, reset_principal, set_principal
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.services.access_service import AccessService, PrincipalConflictError


@contextmanager
def _as(principal):
    token = set_principal(principal)
    try:
        yield
    finally:
        reset_principal(token)


# ── management verbs (owner-only) ───────────────────────────────────────────


def test_create_principal_and_conflict(db_session):
    svc = AccessService(db_session)
    with _as(OWNER):
        p = svc.create_principal(PrincipalCreate(name="agent-a"))
        assert p.id is not None and p.is_owner is False
        with pytest.raises(PrincipalConflictError):
            svc.create_principal(PrincipalCreate(name="agent-a"))


def test_mint_token_returns_raw_once_and_stores_only_hash(db_session):
    svc = AccessService(db_session)
    with _as(OWNER):
        p = svc.create_principal(PrincipalCreate(name="agent-b"))
        minted = svc.mint_token(p.id, TokenCreate(label="laptop"))
        assert minted.token  # raw token present exactly once
        # Only the hash is stored.
        row = db_session.get(ApiToken, minted.id)
        assert row.token_hash == hash_token(minted.token)
        assert row.token_hash != minted.token
        # Listing never re-exposes the raw token.
        listed = svc.list_tokens(p.id)
        assert [t.id for t in listed] == [minted.id]
        assert not hasattr(listed[0], "token")


def test_grant_is_idempotent_and_toggles_acr(db_session):
    svc = AccessService(db_session)
    repo = DocumentRepository(db_session)
    with _as(OWNER):
        doc = repo.create(title="root", content="x" * 20, auto_embed=False)
        db_session.flush()
        p = svc.create_principal(PrincipalCreate(name="agent-c"))

        # First grant makes the document an ACR.
        g1 = svc.grant(doc.id, GrantCreate(principal_id=p.id, level="read"))
        assert svc.list_grants(doc.id)[0].level == "read"
        # Re-grant updates the level in place (same row, idempotent per (doc, principal)).
        g2 = svc.grant(doc.id, GrantCreate(principal_id=p.id, level="write"))
        assert g2.id == g1.id and svc.list_grants(doc.id)[0].level == "write"
        # Revoking the last grant un-marks the ACR.
        svc.revoke_grant(doc.id, p.id)
        assert svc.list_grants(doc.id) == []


def test_grant_validates_level_and_targets(db_session):
    svc = AccessService(db_session)
    repo = DocumentRepository(db_session)
    with _as(OWNER):
        doc = repo.create(title="r", content="x" * 20, auto_embed=False)
        db_session.flush()
        p = svc.create_principal(PrincipalCreate(name="agent-d"))
        with pytest.raises(ValueError):
            svc.grant(doc.id, GrantCreate(principal_id=p.id, level="admin"))
        with pytest.raises(LookupError):
            svc.grant(999999, GrantCreate(principal_id=p.id, level="read"))
        with pytest.raises(LookupError):
            svc.grant(doc.id, GrantCreate(principal_id=999999, level="read"))


def test_management_is_owner_only(db_session):
    svc = AccessService(db_session)
    # A bound non-owner cannot administer access control.
    stranger = CurrentPrincipal(id=12345, name="stranger", is_owner=False)
    from jmfts_core.access import AccessDeniedError

    with _as(stranger):
        with pytest.raises(AccessDeniedError):
            svc.create_principal(PrincipalCreate(name="nope"))
        with pytest.raises(AccessDeniedError):
            svc.list_principals()


# ── token resolution ────────────────────────────────────────────────────────


def test_resolve_principal_token(db_session):
    p = Principal(name="resolvable")
    db_session.add(p)
    db_session.flush()
    good = "raw-token-value-123"
    db_session.add(ApiToken(principal_id=p.id, token_hash=hash_token(good)))
    expired = "expired-token-value-456"
    db_session.add(
        ApiToken(
            principal_id=p.id,
            token_hash=hash_token(expired),
            expires_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
    )
    db_session.flush()

    resolved = resolve_principal_token(good, session=db_session)
    assert resolved is not None and resolved.id == p.id and resolved.is_owner is False
    assert resolve_principal_token("no-such-token", session=db_session) is None
    assert resolve_principal_token(expired, session=db_session) is None


# ── end-to-end HTTP: a non-owner principal is filtered and 403'd ────────────


@contextmanager
def _http(db_session, principal):
    """A TestClient whose requests run as `principal` against `db_session`.

    Overrides the auth dependency to bind the principal in-request (so it propagates into
    the threadpool exactly as the real generator dependency does) and get_db to share the
    rolled-back test session, so uncommitted setup is visible to the endpoints.
    """
    from jmfts_core.rest.auth import require_token
    from jmfts_core.rest.main import app
    from jmfts_core.database import get_db

    async def _bind():
        token = set_principal(principal)
        try:
            yield
        finally:
            reset_principal(token)

    def _db():
        yield db_session

    app.dependency_overrides[require_token] = _bind
    app.dependency_overrides[get_db] = _db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(require_token, None)
        app.dependency_overrides.pop(get_db, None)


def _seed_governed(db_session):
    repo = DocumentRepository(db_session)
    secret = repo.create(title="Secret", content="x" * 20, auto_embed=False)
    public = repo.create(title="Public", content="y" * 20, auto_embed=False)
    db_session.flush()
    reader = Principal(name="reader")
    outsider = Principal(name="outsider")
    db_session.add_all([reader, outsider])
    db_session.flush()
    db_session.add(AccessGrant(document_id=secret.id, principal_id=reader.id, level="read"))
    db_session.flush()
    return (
        secret.id,
        public.id,
        CurrentPrincipal(id=reader.id, name="reader", is_owner=False),
        CurrentPrincipal(id=outsider.id, name="outsider", is_owner=False),
    )


def test_http_owner_sees_and_edits(db_session):
    secret, _public, _reader, _outsider = _seed_governed(db_session)
    with _http(db_session, OWNER) as client:
        assert client.get(f"/documents/{secret}").status_code == 200
        assert client.patch(f"/documents/{secret}", json={"title": "edited"}).status_code == 200


def test_http_read_only_gets_404_hidden_and_403_on_write(db_session):
    secret, public, reader, outsider = _seed_governed(db_session)
    # Reader: can retrieve, cannot modify (403 via the app-level AccessDeniedError handler).
    with _http(db_session, reader) as client:
        assert client.get(f"/documents/{secret}").status_code == 200
        assert client.patch(f"/documents/{secret}", json={"title": "no"}).status_code == 403
    # Outsider: the governed doc is indistinguishable from missing (404), on read and write.
    with _http(db_session, outsider) as client:
        assert client.get(f"/documents/{secret}").status_code == 404
        assert client.patch(f"/documents/{secret}", json={"title": "no"}).status_code == 404
        # The list is filtered to what the outsider may read.
        ids = {d["id"] for d in client.get("/documents").json()}
        assert public in ids and secret not in ids
