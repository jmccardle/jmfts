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

One further exception is not an exception to authentication but to *this*
authentication: paths under ``/runner`` carry a runner key instead, checked by
``require_runner``. See ``RUNNER_PREFIX`` for why the two are kept disjoint.

The interactive documentation (``/docs``, ``/redoc``, ``/openapi.json``) is open. Those
are plain Starlette routes, so the app-level dependency never ran on them, and gating them
would not work anyway: a browser navigating to ``/docs`` cannot be made to send an
``Authorization`` header, so a gate there would close the page rather than protect it. What
the document exposes is the *interface* — paths, parameters, schemas — and every operation
it describes still needs a token. See ``jmfts_core/rest/main.py`` for the declaration side.
"""

from __future__ import annotations

import secrets

from fastapi import HTTPException, Request, Security
from fastapi.openapi.models import HTTPBearer as HTTPBearerModel
from fastapi.security.base import SecurityBase

from jmfts_core.access import resolve_principal_token
from jmfts_core.config import get_settings
from jmfts_core.principal_context import OWNER, reset_principal, set_principal

_BEARER_PREFIX = "Bearer "


class BearerScheme(SecurityBase):
    """Declare an HTTP bearer credential in the OpenAPI document; return the raw header.

    This is a *declaration*, not a check. It exists because FastAPI only writes a
    ``securitySchemes`` entry for a dependency that is a ``SecurityBase``, and without one
    ``/docs`` has no Authorize button — every "Try it out" then goes out with no header and
    comes back 401, which makes the page a catalogue rather than a client.

    It is deliberately not ``fastapi.security.HTTPBearer``. That class compares the scheme
    case-insensitively, so it would accept ``bearer <token>``; JMFTS requires the exact
    ``Bearer `` prefix and ``tests/test_api_auth.py`` asserts the lowercase form is
    rejected. Swapping the parse for FastAPI's would have loosened authentication as a side
    effect of documenting it. So the parse stays below, unchanged, and this only names the
    credential.
    """

    def __init__(self, *, scheme_name: str, description: str) -> None:
        self.model = HTTPBearerModel(description=description)
        self.scheme_name = scheme_name

    async def __call__(self, request: Request) -> str | None:
        return request.headers.get("Authorization")


# The two credentials, as they appear in the OpenAPI document and in the Authorize dialog.
# The names are part of the published contract — a client generated from the document binds
# to them — so they are constants rather than string literals at the point of use, and
# tests/test_openapi_docs.py pins both.
API_SCHEME = "JMFTSToken"
JMFTS_RUNNER_SCHEME = "JMFTSRunnerKey"

API_BEARER = BearerScheme(
    scheme_name=API_SCHEME,
    description=(
        "The shared API token (JMFTS_API_TOKEN). When that setting is empty the server "
        "generates one per run and prints it to the boot log — it is never absent. Paste "
        "the token itself; the `Bearer ` prefix is added for you."
    ),
)

RUNNER_BEARER = BearerScheme(
    scheme_name=JMFTS_RUNNER_SCHEME,
    description=(
        "The runner key (JMFTS_RUNNER_KEY), accepted only under /runner. It is a different "
        "credential from the API token on purpose: an API token must not embed, and a "
        "runner key owns no documents. If this deployment has no runner key set, /runner "
        "answers 503 rather than 401."
    ),
)

# Paths that are reachable without a token. `/health` is the liveness probe;
# `/health/` covers trailing-slash routing if it is ever added.
PUBLIC_PATHS = frozenset({"/health", "/health/"})

# The runner surface. `require_token` steps aside for these paths — NOT because they are
# open, but because they are gated by `require_runner` instead, declared on the router
# itself. The two credentials are disjoint on purpose: an API token must not embed, and a
# runner key must not read a document. Making them alternatives inside one dependency
# would have produced exactly the coupling this split exists to avoid.
#
# tests/test_runner_auth.py asserts the correspondence in both directions: every route
# under this prefix declares `require_runner`, and no route outside it does.
RUNNER_PREFIX = "/runner"

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


async def require_token(request: Request, authorization: str | None = Security(API_BEARER)):
    """App-level dependency: authenticate the bearer and BIND the request principal.

    Early-allows CORS preflight (``OPTIONS``) and the public ``/health`` path. Raises
    ``HTTPException(401)`` on a missing, malformed, or unrecognised token. On success the
    resolved principal is bound in the contextvar for the duration of the request (repos
    read it to enforce subtree RBAC) and reset in the ``finally`` when the request ends.
    This is a generator dependency so the reset always runs; each request has its own
    context, so a binding never leaks across requests.

    ``authorization`` is the raw header, handed over by :data:`API_BEARER` rather than read
    off the request, so that declaring the credential and requiring it are the same
    statement. The parse below is unchanged.
    """
    if request.method == "OPTIONS" or request.url.path in PUBLIC_PATHS:
        yield
        return

    # Gated by `require_runner` on the router, with a different credential and no
    # principal. See RUNNER_PREFIX.
    if request.url.path.startswith(RUNNER_PREFIX):
        yield
        return

    if not authorization or not authorization.startswith(_BEARER_PREFIX):
        raise HTTPException(
            status_code=401,
            detail="Missing or malformed Authorization header; expected 'Bearer <token>'",
        )

    presented = authorization[len(_BEARER_PREFIX) :]
    principal = _resolve_principal(presented)
    if principal is None:
        raise HTTPException(status_code=401, detail="Invalid API token")

    token = set_principal(principal)
    try:
        yield
    finally:
        reset_principal(token)


async def require_runner(request: Request, authorization: str | None = Security(RUNNER_BEARER)):
    """Router-level dependency for the /runner surface: authenticate a RUNNER, not a user.

    Three outcomes, and the difference between the first two is the point:

    * No ``JMFTS_RUNNER_KEY`` configured -> 503. This deployment does not offer the runner
      surface. It is a distinct answer from "your key is wrong", so an operator wiring up a
      fleet can tell an unconfigured server from a mismatched Secret without reading logs on
      the other side.
    * Missing, malformed, or non-matching key -> 401.
    * Match -> proceed, binding NO principal.

    That last clause is the whole design. ``require_token`` binds a principal into a
    contextvar and repositories read it to enforce subtree access; a runner has no subtree,
    so there is nothing to bind and binding anything would be a lie about who is asking.
    """
    if request.method == "OPTIONS":
        return

    configured = get_settings().runner_key
    if not configured:
        raise HTTPException(
            status_code=503,
            detail=(
                "The runner surface is not configured on this server. Set JMFTS_RUNNER_KEY "
                "to enable it; it is off by default."
            ),
        )

    if not authorization or not authorization.startswith(_BEARER_PREFIX):
        raise HTTPException(
            status_code=401,
            detail="Missing or malformed Authorization header; expected 'Bearer <runner-key>'",
        )

    if not secrets.compare_digest(authorization[len(_BEARER_PREFIX) :], configured):
        raise HTTPException(status_code=401, detail="Invalid runner key")
