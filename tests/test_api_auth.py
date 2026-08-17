"""CR-4: shared-bearer auth + CORS lockdown for the JMFTS API.

These tests deliberately need NO database: they exercise `/config`, `/health`,
`/`, and `OPTIONS`, none of which depend on a live DB (the health handlers
catch DB errors and still return 200). The app-level `require_token` dependency
and the CORS configuration are what is under test.

The pinned token / CORS origin come from ``tests/conftest.py`` (``TEST_API_TOKEN``,
``TEST_CORS_ORIGIN``), set in the environment before ``api.main`` is imported.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from api.main import app
from tests.conftest import AUTH_HEADERS, TEST_API_TOKEN, TEST_CORS_ORIGIN

client = TestClient(app)


# ── the gate: /config requires a valid Bearer token ─────────────────────────


def test_no_authorization_header_is_401():
    resp = client.get("/config")
    assert resp.status_code == 401


def test_malformed_header_is_401():
    # Present but not "Bearer <tok>".
    for bad in ("token123", "Basic abc", "Bearer", "bearer " + TEST_API_TOKEN):
        resp = client.get("/config", headers={"Authorization": bad})
        assert resp.status_code == 401, f"expected 401 for header {bad!r}"


def test_wrong_token_is_401():
    resp = client.get("/config", headers={"Authorization": "Bearer not-the-real-token"})
    assert resp.status_code == 401


def test_correct_token_is_200():
    resp = client.get("/config", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    # Sanity: it is the real /config payload, not an auth error body.
    assert "embedding_model" in resp.json()


def test_config_is_gated():
    # Explicit restatement of the requirement: /config is NOT public.
    assert client.get("/config").status_code == 401
    assert client.get("/config", headers=AUTH_HEADERS).status_code == 200


def test_root_is_gated():
    # `/` (the short health check) is gated too — only `/health` is public.
    assert client.get("/").status_code == 401


# ── the public carve-outs: /health and OPTIONS preflight ────────────────────


def test_health_is_public_without_token():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] in ("ok", "degraded")


def test_options_preflight_is_not_401():
    # A CORS preflight must never be blocked by auth. With an allowed Origin the
    # middleware answers it directly (200) — but the key assertion is: not 401.
    resp = client.options(
        "/config",
        headers={
            "Origin": TEST_CORS_ORIGIN,
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.status_code != 401
    assert resp.status_code == 200


# ── CORS lockdown: configured origin reflected, never "*" ───────────────────


def test_cors_reflects_configured_origin_and_is_not_wildcard():
    resp = client.get("/health", headers={"Origin": TEST_CORS_ORIGIN})
    allow_origin = resp.headers.get("access-control-allow-origin")
    assert allow_origin == TEST_CORS_ORIGIN
    assert allow_origin != "*"


# ── ephemeral-token path: no JMFTS_API_TOKEN → generate-print-REQUIRE ────────


def test_ephemeral_token_is_generated_and_still_required(monkeypatch):
    """With JMFTS_API_TOKEN unset, the server generates a token, REQUIRES it,
    and NEVER allows unauthenticated access."""
    import api.auth as auth_mod
    from jmfts_core.config import get_settings

    # Force the empty-token path. delenv() is not enough: pydantic BaseSettings
    # still reads .env, which pins a real JMFTS_API_TOKEN on dev machines. An
    # empty *environment* value takes precedence over .env, so it reliably
    # exercises the generate-print-require branch everywhere.
    monkeypatch.setenv("JMFTS_API_TOKEN", "")
    get_settings.cache_clear()
    monkeypatch.setattr(auth_mod, "_generated_token", None)

    try:
        # The effective (empty) setting proves we are on the ephemeral path,
        # not silently reusing a pinned token.
        assert get_settings().api_token == ""

        # A no-token request STILL 401s — a missing config token is not "allow all".
        assert client.get("/config").status_code == 401
        # A wrong token STILL 401s.
        assert client.get("/config", headers={"Authorization": "Bearer nope"}).status_code == 401

        # A token was generated (once) and it works.
        generated = auth_mod.get_effective_token()
        assert generated and isinstance(generated, str)
        # Idempotent: asking again yields the same token (generated exactly once).
        assert auth_mod.get_effective_token() == generated

        resp = client.get("/config", headers={"Authorization": f"Bearer {generated}"})
        assert resp.status_code == 200
    finally:
        # Restore the pinned-token world for every following test. monkeypatch
        # re-adds JMFTS_API_TOKEN at teardown; clearing the settings cache and the
        # generated singleton here means the next get_settings() re-reads it.
        get_settings.cache_clear()
        auth_mod._generated_token = None
