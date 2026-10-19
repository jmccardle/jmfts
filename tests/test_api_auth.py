"""CR-4: shared-bearer auth + CORS lockdown for the JMFTS API.

These tests deliberately need NO database: they exercise `/config`, `/health`,
`/`, and `OPTIONS`, none of which depend on a live DB (the health handlers
catch DB errors and still return 200). The app-level `require_token` dependency
and the CORS configuration are what is under test.

The pinned token / CORS origin come from ``tests/conftest.py`` (``TEST_API_TOKEN``,
``TEST_CORS_ORIGIN``), set in the environment before ``jmfts_core.rest.main`` is imported.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jmfts_core.rest.main import app
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


# ── empty-token path: no JMFTS_API_TOKEN → the server refuses to start (#51) ──


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_blank_token_refuses_to_start(monkeypatch, value):
    """Empty and whitespace-only are the same refusal, and it names the variable."""
    import jmfts_core.rest.auth as auth_mod
    from jmfts_core.config import get_settings

    # An empty *environment* value takes precedence over .env on dev machines.
    monkeypatch.setenv("JMFTS_API_TOKEN", value)
    get_settings.cache_clear()
    try:
        with pytest.raises(auth_mod.ApiTokenNotConfigured, match="JMFTS_API_TOKEN"):
            auth_mod.get_effective_token()
        # The lifespan (what uvicorn runs) refuses too.
        with pytest.raises(auth_mod.ApiTokenNotConfigured, match="JMFTS_API_TOKEN"):
            with TestClient(app):
                pass
    finally:
        get_settings.cache_clear()


def test_unset_token_refuses_to_start(monkeypatch, tmp_path):
    """Unset (not merely empty) refuses too. chdir away so a dev .env cannot supply one."""
    import jmfts_core.rest.auth as auth_mod
    from jmfts_core.config import get_settings

    monkeypatch.delenv("JMFTS_API_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    try:
        assert get_settings().api_token == ""
        with pytest.raises(auth_mod.ApiTokenNotConfigured, match="JMFTS_API_TOKEN"):
            auth_mod.get_effective_token()
    finally:
        get_settings.cache_clear()


def test_set_token_starts_authenticates_and_health_is_open():
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
        assert c.get("/config").status_code == 401
        assert c.get("/config", headers=AUTH_HEADERS).status_code == 200


def _server_cli(args, env_token, cwd):
    import os
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if k != "JMFTS_API_TOKEN"}
    # The child runs from a scratch cwd (so no .env can supply a token), which means it
    # must be told which tree to import: the one under test, not whatever is installed.
    import jmfts_client
    import jmfts_core

    env["PYTHONPATH"] = os.pathsep.join(
        sorted({os.path.dirname(os.path.dirname(m.__file__)) for m in (jmfts_core, jmfts_client)})
    )
    if env_token is not None:
        env["JMFTS_API_TOKEN"] = env_token
    return subprocess.run(
        [sys.executable, "-c", "from jmfts_core.rest.main import run; run()", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        cwd=cwd,
    )


def test_jmfts_server_help_works_without_a_token(tmp_path):
    r = _server_cli(["--help"], None, tmp_path)
    assert r.returncode == 0, r.stderr
    assert "Serve the JMFTS API" in r.stdout


@pytest.mark.parametrize("token", [None, "", "  "])
def test_jmfts_server_exits_with_one_clear_line_without_a_token(token, tmp_path):
    r = _server_cli(["--no-reload"], token, tmp_path)
    assert r.returncode != 0
    assert "JMFTS_API_TOKEN is not set" in r.stderr
    assert "openssl rand" in r.stderr
    assert "Traceback" not in r.stderr
