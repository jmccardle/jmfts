"""JMFTS API - John McCardle's Fusion Tree Search"""

from contextlib import asynccontextmanager

import httpx
from sqlalchemy import text
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from jmfts_core import __version__
from jmfts_core.access import AccessDeniedError
from jmfts_core.config import get_settings
from jmfts_core.database import get_engine
from jmfts_core.ingest_worker import build_worker_from_settings

# Importing the services package runs @register_service, populating the @expose
# REGISTRY that build_exposed_router() reads. Must happen before the router is built.
import jmfts_core.services  # noqa: F401
from jmfts_core.rest.wiring import build_exposed_router, build_openapi_tags
from jmfts_core.rest.routers import runner
from jmfts_core.rest.auth import (
    JMFTS_RUNNER_SCHEME,
    PUBLIC_PATHS,
    RUNNER_PREFIX,
    get_effective_token,
    require_token,
)
from jmfts_core.rest.schemas import HealthResponse, LlmHealthStatus

# Shown above the operation list in /docs and /redoc. Markdown; keep it to what a caller
# needs before their first request, which is the credential and the two carve-outs.
_API_DESCRIPTION = """
John McCardle's Fusion Tree Search — a retrieval appliance with matryoshka embeddings,
ColBERT-style late interaction (MaxSim) and BM25 over a tree-structured document store.

**Authentication.** Every operation below needs `Authorization: Bearer <token>`. Press
**Authorize** and paste `JMFTS_API_TOKEN`; if that setting is empty the server generated a
token at startup and printed it to the boot log. Two carve-outs: `GET /health` is open so a
liveness probe needs no secret, and `/runner/*` takes the separate `JMFTS_RUNNER_KEY`
instead — it embeds text and owns no documents, so the two credentials are kept disjoint.

**This page is public.** `/docs`, `/redoc` and `/openapi.json` answer without a token,
because a browser navigating to a page cannot send an `Authorization` header. The document
describes the interface; reaching anything it describes still costs a token.
"""

# Swagger UI display options. With 78 operations the default fully-expanded list is not
# readable, so the groups start collapsed and the filter box is on. `persistAuthorization`
# keeps the pasted token in the browser's localStorage across reloads — a convenience with
# a real cost, since that token is the appliance's master credential; drop this key to make
# the operator re-Authorize after every refresh.
_SWAGGER_UI_PARAMETERS = {
    "docExpansion": "none",
    "filter": True,
    "persistAuthorization": True,
}


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Resolve the effective auth token at startup (CR-4). With no
    # JMFTS_API_TOKEN configured this GENERATES and PRINTS the ephemeral token
    # in the boot log — rather than lazily on the first Bearer request, which
    # left an operator reading the startup output with no token to use.
    settings = get_settings()
    if settings.api_token:
        print("JMFTS: auth enabled — using the configured JMFTS_API_TOKEN.")
    else:
        get_effective_token()  # generates + prints the ephemeral token

    # The in-process ingest worker (INGEST_SPEC.md 5.8). It lives for exactly the
    # lifespan of the app: started before the first request can arrive, and JOINED on
    # shutdown. Joining is the point — a daemon thread left running while the process
    # tears down keeps committing to a database the rest of the code has stopped
    # watching, and under pytest that looks like rows appearing from nowhere.
    worker = None
    if settings.ingest_worker_enabled:
        worker = build_worker_from_settings()
        worker.start()
        _app.state.ingest_worker = worker
    try:
        yield
    finally:
        if worker is not None:
            worker.stop()
            _app.state.ingest_worker = None


# Create FastAPI app. The app-level `require_token` dependency (CR-4) gates
# EVERY route — including `/config` and `/` — with a single shared bearer token.
# `/health` and CORS-preflight `OPTIONS` are allow-listed inside the dependency.
#
# `require_token` also DECLARES the credential (jmfts_core/rest/auth.py::API_BEARER), which
# is what gives /docs its Authorize button. Before that, the generated document named no
# credential at all, so the page listed every operation and could not call one: "Try it out"
# sent no header and came back 401 with nothing on the page to explain it.
#
# The doc routes themselves — /docs, /redoc, /openapi.json — are plain Starlette routes, so
# this dependency never applied to them and they answer without a token. That is deliberate
# and tests/test_openapi_docs.py pins it; see the auth.py module docstring for the reason.
app = FastAPI(
    title="JMFTS",
    description=_API_DESCRIPTION,
    version=__version__,
    dependencies=[Depends(require_token)],
    lifespan=_lifespan,
    # Descriptions for the domain tag groups come from the service class that implements
    # each group. The two groups below have no service behind them, so they are named here:
    # `runner` is the hand-written surface, `meta` is the three infra routes on this file.
    openapi_tags=build_openapi_tags()
    + [
        {
            "name": "runner",
            "description": (
                "Embed text for a caller that keeps the documents somewhere else. Takes the "
                "runner key, not the API token, and touches no database."
            ),
        },
        {
            "name": "meta",
            "description": "Liveness, version and the non-sensitive half of the configuration.",
        },
    ],
    swagger_ui_parameters=_SWAGGER_UI_PARAMETERS,
)


# Subtree RBAC (migration 006): a write the principal may not perform surfaces as 403.
# Raised from the repository write choke points and NOT declared in any @expose(errors),
# so the wiring re-raises it to this single app-level handler. The hidden case (a target
# the principal can't even read) is a LookupError → 404 instead, so a read-only holder
# gets an honest 403 while a non-reader can't distinguish it from a missing document.
@app.exception_handler(AccessDeniedError)
async def _access_denied_handler(_request: Request, exc: AccessDeniedError) -> JSONResponse:
    return JSONResponse(status_code=403, content={"detail": str(exc)})


# CORS middleware. `allow_origins` comes from settings (JMFTS_CORS_ORIGINS),
# defaulting to an empty list = server-to-server only (no browser Origin
# allowed). Never "*" alongside allow_credentials; with an empty list the
# credentials flag is inert, which is the intended posture.
_settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
# conversations.router is fully generated from the @expose registry now (see
# jmfts_core/services/conversation_service.py); it is mounted via build_exposed_router() below.
# documents.router is fully generated from the @expose registry now (see
# jmfts_core/services/document_service.py); it is mounted via build_exposed_router() below.
# graph.router is fully generated from the @expose registry now (see
# jmfts_core/services/graph_service.py); it is mounted via build_exposed_router() below.
# indexes.router is fully generated from the @expose registry now (see
# jmfts_core/services/index_service.py); it is mounted via build_exposed_router() below.
# ingest.router is fully generated from the @expose registry now (see
# jmfts_core/services/ingest_service.py); it is mounted via build_exposed_router() below.
# search.router is fully generated from the @expose registry now (see
# jmfts_core/services/search_service.py); it is mounted via build_exposed_router() below.
# search_contexts.router is fully generated from the @expose registry now (see
# jmfts_core/services/search_context_service.py); it is mounted via build_exposed_router() below.
# templates.router is fully generated from the @expose registry now (see
# jmfts_core/services/template_service.py); it is mounted via build_exposed_router() below.
# triples.router is fully generated from the @expose registry now (see
# jmfts_core/services/triple_service.py); it is mounted via build_exposed_router() below.
# usetype_presentations.router is fully generated from the @expose registry now (see
# jmfts_core/services/usetype_presentation_service.py); it is mounted via build_exposed_router() below.
# view.router is fully generated from the @expose registry now (see
# jmfts_core/services/view_service.py); it is mounted via build_exposed_router() below.

# Routes generated from the @expose registry (the API-unification pilot). Mounted
# last; the parity test asserts these stay in lockstep with the service layer.
app.include_router(build_exposed_router())

# The runner surface. Hand-written rather than generated from @expose, because @expose
# builds routes that carry a principal and enforce subtree access, and this surface has
# neither — it takes text and returns vectors. It brings its own `require_runner`
# dependency and its own credential; see jmfts_core/rest/routers/runner.py.
app.include_router(runner.router)


# The generated `security` block, corrected to match the gate that actually runs.
#
# FastAPI writes one entry per security-declaring dependency and OpenAPI reads several
# entries as alternatives. The app-level `require_token` declares the API token on EVERY
# route — but it steps aside for two sets of paths, and generation cannot see that:
#
#   * `PUBLIC_PATHS` (/health) needs no credential at all. Left as generated, the document
#     tells a liveness probe to carry the appliance's master token.
#   * `RUNNER_PREFIX` (/runner/*) takes the runner key INSTEAD. Left as generated it reads
#     "API token or runner key", so a caller following the document sends the credential
#     that is refused there and gets a 401 with nothing on the page to explain it.
#
# `openapi_extra` on the routes cannot express either correction: FastAPI merges a list by
# CONCATENATING it, so a per-route override can add entries and never remove one. Hence
# this pass. It branches on the same two constants `require_token` itself branches on, so a
# path added to either carve-out moves the document with it and nothing here is edited.
_generated_openapi = app.openapi

_HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})


def _openapi_matching_the_gate() -> dict:
    schema = _generated_openapi()
    for path, path_item in schema.get("paths", {}).items():
        if path in PUBLIC_PATHS:
            required: list[dict] = []
        elif path.startswith(RUNNER_PREFIX):
            required = [{JMFTS_RUNNER_SCHEME: []}]
        else:
            continue
        for method, operation in path_item.items():
            if method.lower() in _HTTP_METHODS:
                operation["security"] = required
    return schema


# `app.openapi()` memoises into `app.openapi_schema` and hands back that same dict, so this
# runs once per process. Assigning `security` is idempotent regardless, which is why the
# correction is a write and not an edit of what generation produced.
app.openapi = _openapi_matching_the_gate


@app.get("/", response_model=HealthResponse, tags=["meta"])
def health_check():
    """Health check endpoint"""
    settings = get_settings()

    # Test database connection
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception as e:
        db_status = f"error: {str(e)}"

    return HealthResponse(
        status="ok" if db_status == "connected" else "degraded",
        version=__version__,
        database=db_status,
        embedding_model=settings.embedding_model,
    )


def _probe_llm() -> LlmHealthStatus:
    """Probe the configured LLM endpoint for reachability and OpenAI compatibility."""
    settings = get_settings()
    base = settings.effective_llm_url.rstrip("/")
    model = settings.effective_llm_model

    reachable = False
    openai_compatible = False
    detail = None

    # JMFTS ships no LLM and defaults to no endpoint, so "not configured" is an ordinary
    # state, not a failure. Say so instead of probing "" and reporting the protocol error
    # that produces — which reads like the endpoint is broken rather than absent.
    if not settings.llm_configured:
        return LlmHealthStatus(
            url=base,
            model=model,
            reachable=False,
            openai_compatible=False,
            detail="not configured: set JMFTS_LLM_BASE_URL and JMFTS_LLM_MODEL",
        )

    try:
        # Lightweight health probe (ensonet extension, may 404 on other servers)
        r = httpx.get(f"{base}/health", timeout=5.0)
        if r.status_code < 500:
            reachable = True
            detail = r.text[:200] if r.text else f"HTTP {r.status_code}"
    except httpx.RequestError as exc:
        detail = f"unreachable: {exc}"
        return LlmHealthStatus(
            url=base, model=model, reachable=False, openai_compatible=False, detail=detail
        )

    # Check OpenAI-compatible /v1/models endpoint
    try:
        r2 = httpx.get(f"{base}/v1/models", timeout=5.0)
        if r2.status_code == 200:
            data = r2.json()
            if isinstance(data.get("data"), list):
                openai_compatible = True
                detail = f"models: {[m.get('id') for m in data['data'][:3]]}"
        elif r2.status_code == 404:
            detail = (detail or "") + " | /v1/models → 404 (not OpenAI-compatible)"
    except Exception:
        pass

    return LlmHealthStatus(
        url=base,
        model=model,
        reachable=reachable,
        openai_compatible=openai_compatible,
        detail=detail,
    )


@app.get("/health", response_model=HealthResponse, tags=["meta"])
def health_check_full():
    """Comprehensive health check — includes LLM reachability probe."""
    settings = get_settings()

    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception as e:
        db_status = f"error: {str(e)}"

    llm_status = _probe_llm()
    overall = "ok" if db_status == "connected" and llm_status.reachable else "degraded"

    return HealthResponse(
        status=overall,
        version=__version__,
        database=db_status,
        embedding_model=settings.embedding_model,
        llm=llm_status,
    )


@app.get("/config", tags=["meta"])
def get_config():
    """Get current configuration (non-sensitive)"""
    settings = get_settings()
    return {
        "embedding_model": settings.embedding_model,
        "embedding_device": settings.embedding_device,
        "token_embed_dims": settings.token_embed_dims,
        "token_top_percent": settings.token_top_percent,
        "bm25_k1": settings.bm25_k1,
        "bm25_b": settings.bm25_b,
    }


def run(argv: list[str] | None = None):
    """Run the API server. The ``jmfts-server`` console script.

    IT PARSES ARGUMENTS, and that is not decoration. This used to take none at all, so
    ``jmfts-server --help`` ignored the flag and bound a port — which meant
    ``docs/RELEASING.md`` step 4 listed it as a smoke check that in fact started a server
    and blocked, and on a host where the port was busy it "failed" for a reason that had
    nothing to do with the wheel being sound.

    The environment still decides: every default below is read from
    :func:`~jmfts_core.config.get_settings`, so an install with no flags behaves exactly as
    it did before. A flag is an override for the one run, which is what makes the same
    wheel usable from a shell, a unit file and a container CMD without three ways to
    configure it.
    """
    import argparse

    import uvicorn

    settings = get_settings()
    parser = argparse.ArgumentParser(
        prog="jmfts-server",
        description=(
            "Serve the JMFTS API. Defaults come from the JMFTS_* environment "
            "(see .env.example); a flag overrides one for this run only."
        ),
    )
    parser.add_argument(
        "--host",
        default=settings.api_host,
        help=f"interface to bind (JMFTS_API_HOST, {settings.api_host})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=settings.api_port,
        help=f"port to bind (JMFTS_API_PORT, {settings.api_port})",
    )
    reload_group = parser.add_mutually_exclusive_group()
    reload_group.add_argument(
        "--reload",
        dest="reload",
        action="store_true",
        default=settings.debug,
        help=f"reload on source change (JMFTS_DEBUG, {settings.debug})",
    )
    reload_group.add_argument(
        "--no-reload", dest="reload", action="store_false", help="serve without the reloader"
    )
    args = parser.parse_args(argv)

    uvicorn.run(
        "jmfts_core.rest.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    run()
