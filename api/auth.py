"""Shared-bearer authentication for the JMFTS API (CR-4).

One app-level FastAPI dependency (`require_token`) gates every router. Clients
send ``Authorization: Bearer <token>``; the token is compared constant-time
(``secrets.compare_digest``) against the *effective token*.

The effective token is ``settings.api_token`` when that is set. When it is
empty, a token is GENERATED ONCE per process via ``secrets.token_urlsafe(32)``,
printed to stdout, and then REQUIRED on every request — exactly like Jupyter's
startup token. A missing ``JMFTS_API_TOKEN`` therefore never grants
unauthenticated access; it only changes where the required token comes from.

Public exceptions to the gate: the ``/health`` liveness probe stays open, and
``OPTIONS`` (CORS preflight) is never blocked. Everything else — including
``/config`` and ``/`` — requires the token.
"""

from __future__ import annotations

import secrets

from fastapi import HTTPException, Request

from jmfts_core.access import resolve_principal_token
from jmfts_core.config import get_settings
from jmfts_core.principal_context import OWNER, reset_principal, set_principal

_BEARER_PREFIX = "Bearer "

# Paths that are reachable without a token. `/health` is the liveness probe;
# `/health/` covers trailing-slash routing if it is ever added.
_PUBLIC_PATHS = frozenset({"/health", "/health/"})

# Process-singleton for the generated ephemeral token. Populated at most once by
# `get_effective_token()` when no JMFTS_API_TOKEN is configured.
_generated_token: str | None = None


def get_effective_token() -> str:
    """Return the token every request must present.

    ``settings.api_token`` when configured; otherwise a process-singleton token
    generated once and printed to stdout. Generation happens exactly once
    (guarded by the module-level singleton), not per request.
    """
    settings = get_settings()
    if settings.api_token:
        return settings.api_token

    global _generated_token
    if _generated_token is None:
        _generated_token = secrets.token_urlsafe(32)
        print(
            "JMFTS: no JMFTS_API_TOKEN set — generated an ephemeral token for "
            f"this run: {_generated_token}\n"
            "  Set JMFTS_API_TOKEN to pin a fixed token and silence this message."
        )
    return _generated_token


def _resolve_principal(presented: str):
    """Map a presented bearer to a principal, or None if it authenticates as nobody.

    The owner is a constant-time compare against the effective token — no DB round-trip,
    so owner auth (the single-user default) keeps working with the database down and never
    pays for a lookup. Any other token is resolved against the ``api_tokens`` table (which
    fails closed to None), so a DB-backed identity gets its principal and everything else
    is rejected.
    """
    if secrets.compare_digest(presented, get_effective_token()):
        return OWNER
    return resolve_principal_token(presented)


async def require_token(request: Request):
    """App-level dependency: authenticate the bearer and BIND the request principal.

    Early-allows CORS preflight (``OPTIONS``) and the public ``/health`` path. Raises
    ``HTTPException(401)`` on a missing, malformed, or unrecognised token. On success the
    resolved principal is bound in the contextvar for the duration of the request (repos
    read it to enforce subtree RBAC) and reset in the ``finally`` when the request ends.
    This is a generator dependency so the reset always runs; each request has its own
    context, so a binding never leaks across requests.
    """
    if request.method == "OPTIONS" or request.url.path in _PUBLIC_PATHS:
        yield
        return

    header = request.headers.get("Authorization")
    if not header or not header.startswith(_BEARER_PREFIX):
        raise HTTPException(
            status_code=401,
            detail="Missing or malformed Authorization header; expected 'Bearer <token>'",
        )

    presented = header[len(_BEARER_PREFIX) :]
    principal = _resolve_principal(presented)
    if principal is None:
        raise HTTPException(status_code=401, detail="Invalid API token")

    token = set_principal(principal)
    try:
        yield
    finally:
        reset_principal(token)
