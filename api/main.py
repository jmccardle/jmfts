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
from api.wiring import build_exposed_router
from api.auth import get_effective_token, require_token
from api.schemas import HealthResponse, LlmHealthStatus


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
app = FastAPI(
    title="JMFTS",
    description="John McCardle's Fusion Tree Search - A focused retrieval appliance with matryoshka embeddings and late interaction",
    version=__version__,
    dependencies=[Depends(require_token)],
    lifespan=_lifespan,
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


@app.get("/", response_model=HealthResponse)
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


@app.get("/health", response_model=HealthResponse)
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


@app.get("/config")
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


def run():
    """Run the API server"""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "api.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.debug,
    )


if __name__ == "__main__":
    run()
