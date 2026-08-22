"""Guards for the @expose API-unification pilot.

Three invariants that keep the internal Python API and the REST surface in lockstep:

1. Bijection — every registered @expose operation is mounted as exactly one route with
   the declared verb+path, and no generated route exists without a registry entry.
2. Core purity — the service/registry/contracts layer imports no web framework, so the
   in-process (no-network) embedding path stays clean.
3. Field-drop regression — a document flowing through the exposed hybrid_search keeps
   ``position`` and ``event_time`` (the bug the unification fixes), asserted without a DB
   by stubbing the repository.
"""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path

import pytest

from jmfts_core.models.document import Document
from jmfts_core.registry import REGISTRY
from jmfts_core.services.search_service import SearchService

REPO_ROOT = Path(__file__).resolve().parent.parent


# --- 1. bijection: registry <-> mounted routes ---------------------------------


def _mounted_exposed_routes():
    """(method, path) pairs for routes generated from the registry."""
    from jmfts_core.rest.wiring import build_exposed_router

    pairs = set()
    for route in build_exposed_router().routes:
        for method in route.methods:
            pairs.add((method, route.path))
    return pairs


def test_registry_and_generated_routes_are_in_bijection():
    registry_pairs = {(s.method, s.path) for s in REGISTRY}
    assert registry_pairs, "registry is empty — no @expose operations were collected"
    assert registry_pairs == _mounted_exposed_routes()


def test_hybrid_search_is_exposed_once():
    hits = [s for s in REGISTRY if s.path == "/search/hybrid"]
    assert len(hits) == 1, f"expected exactly one /search/hybrid spec, got {len(hits)}"
    assert hits[0].method == "POST"
    assert hits[0].service_cls is SearchService


def test_generated_route_not_also_hand_written():
    """The pilot must have DELETED the hand-written route, not shadowed it."""
    src = (REPO_ROOT / "jmfts_core" / "rest" / "routers" / "search.py").read_text()
    assert (
        '"/hybrid"' not in src and "'/hybrid'" not in src
    ), "a hand-written /hybrid route still exists in search.py; it should be generated"


# --- 1b. Phase E seal: no hand-written domain route escapes generation ----------

# The only hand-written routes allowed to survive on the app are these three
# infra endpoints, defined in jmfts_core/rest/main.py (health probes + non-sensitive config).
# They are NOT domain operations, so they stay out of the @expose registry.
# The /runner routes are here rather than in the registry because @expose generates the
# domain surface: routes that resolve a principal and enforce subtree access on documents.
# The runner surface has no document and no principal — it takes text and returns vectors,
# for a caller whose database this process may not be able to reach at all. Generating it
# from @expose would mean giving it the machinery it exists to do without. It is gated by
# `require_runner` and its own credential; tests/test_runner_auth.py holds that seal.
INFRA_ALLOWLIST = {
    "/",
    "/health",
    "/config",
    "/runner/info",
    "/runner/embed",
    "/runner/embed/tokens",
}


def test_all_domain_routes_are_generated():
    """Every mounted domain route is generated from the ``@expose`` REGISTRY.

    Phase E — the final seal. We enumerate every ``APIRoute`` on the live app and
    classify it: a route whose ``name`` is a ``Service.method`` from the REGISTRY is
    generated; a route whose path is in :data:`INFRA_ALLOWLIST` is permitted infra.
    Anything else is a hand-written domain route that escaped the unification, and
    fails this test. (FastAPI's own ``/openapi.json``, ``/docs``, ``/redoc`` are
    plain Starlette ``Route``s, not ``APIRoute``s, so they are excluded here.)

    This makes future drift impossible: a new hand-written domain route cannot land
    without either joining the registry or being explicitly allow-listed above.
    """
    import jmfts_core.rest.main as main
    from jmfts_core.rest.wiring import iter_mounted_api_routes

    registry_names = {s.name for s in REGISTRY}

    mounted = iter_mounted_api_routes(main.app)
    # Vacuity guard. This assertion is "no mounted route has a property", which an empty
    # list satisfies — and it DID, silently, under fastapi 0.141, where iterating
    # `app.routes` yields three infra routes rather than a hundred. The straggler check
    # inspected nothing and reported success. It cannot do that again without this line.
    assert len(mounted) >= len(REGISTRY), (
        f"only {len(mounted)} routes mounted against {len(REGISTRY)} registry entries; "
        "the straggler check below would inspect almost nothing and pass"
    )

    stragglers = set()
    for route in mounted:
        if route.name in registry_names:
            continue  # generated from a @expose spec
        if route.path in INFRA_ALLOWLIST:
            continue  # permitted infra survivor
        for method in route.methods:
            if method in {"HEAD", "OPTIONS"}:
                continue
            stragglers.add((method, route.path, route.name))

    assert not stragglers, (
        "hand-written domain routes escaped @expose generation: "
        f"{sorted(stragglers)}. Convert each to an @expose service method, or "
        "(only for genuine infra) add its path to INFRA_ALLOWLIST."
    )


# --- 2. core purity: no web framework under jmfts_core.{contracts,registry,services} --


@pytest.mark.parametrize(
    "module_dir",
    ["contracts", "registry.py", "services"],
)
def test_core_layer_imports_no_fastapi(module_dir):
    target = REPO_ROOT / "jmfts_core" / module_dir
    files = [target] if target.is_file() else sorted(target.rglob("*.py"))
    offenders = []
    for path in files:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            if any(n.split(".")[0] in {"fastapi", "starlette"} for n in names):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not offenders, f"web framework imported in core layer: {offenders}"


# --- 3. field-drop regression on the exposed operation -------------------------


class _StubResult:
    def __init__(self, document, score, method):
        self.document = document
        self.score = score
        self.method = method


class _StubRepo:
    """Stands in for SearchRepository so the test needs no database."""

    def __init__(self, doc):
        self._doc = doc

    def hybrid_search(self, **kwargs):
        return [_StubResult(self._doc, 0.9, "hybrid")]


def _doc_with_temporal_fields():
    doc = Document(
        id=42,
        parent_id=None,
        title="t",
        content="c",
        structured_content={},
        path=[],
        usetype="note",
        position=3,
        content_hash="abc",
    )
    doc.created_at = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
    doc.updated_at = doc.created_at
    doc.event_time = datetime(2023, 1, 15, 8, 30, tzinfo=timezone.utc)
    # Set for the same reason created_at is: the lifecycle column's defaults fire at
    # INSERT and this Document is never inserted. A search hit is by definition settled
    # (retrieval filters on it), so that is the honest value for a stubbed hit.
    doc.settled = "settled"
    return doc


def test_hybrid_search_preserves_position_and_event_time(monkeypatch):
    doc = _doc_with_temporal_fields()
    # Patch the repo the service constructs so no DB is touched.
    monkeypatch.setattr(
        "jmfts_core.services.search_service.SearchRepository",
        lambda session: _StubRepo(doc),
    )
    from jmfts_client.contracts.search import HybridSearchRequest

    service = SearchService(session=None)
    response = service.hybrid_search(HybridSearchRequest(query="q"))

    assert len(response.results) == 1
    returned = response.results[0].document
    assert returned.position == 3, "position dropped on a hybrid search hit"
    assert returned.event_time == datetime(
        2023, 1, 15, 8, 30, tzinfo=timezone.utc
    ), "event_time dropped on a hybrid search hit"
    # embed must NOT leak into search results (contract: search omits the vector).
    assert returned.embed is None


# --- 4. adapter capability: async, status_code, path params --------------------


def _route_from_spec(spec):
    """Build one FastAPI route from a spec the way build_exposed_router does."""
    import inspect

    from fastapi import APIRouter

    from jmfts_core.rest.wiring import _make_endpoint

    router = APIRouter()
    router.add_api_route(
        spec.path,
        _make_endpoint(spec),
        methods=[spec.method],
        response_model=spec.response_model,
        status_code=spec.status_code,
        tags=spec.tags,
        summary=spec.summary,
        name=spec.name,
    )
    route = router.routes[-1]
    return route, inspect.iscoroutinefunction(route.endpoint)


class _FakeService:
    """A throwaway service exercising the adapter's new capabilities."""

    def __init__(self, session):
        self.session = session

    def get_thing(self, thing_id: int, *, verbose: bool = False):
        return {"id": thing_id, "verbose": verbose}

    async def make_thing(self, thing_id: int):
        return {"id": thing_id}

    def read_blob(self, blob_path: str):
        return {"blob_path": blob_path}


def _spec(func, method, path, status_code=None):
    from jmfts_core.registry import ExposeSpec

    spec = ExposeSpec(method=method, path=path, func=func, status_code=status_code)
    spec.service_cls = _FakeService
    return spec


def test_adapter_binds_path_param_and_status_code():
    """A service param named like a ``{placeholder}`` binds as a path param, and
    ``status_code`` reaches the route."""
    spec = _spec(_FakeService.get_thing, "GET", "/things/{thing_id}", status_code=201)
    route, is_async = _route_from_spec(spec)

    path_params = {p.name for p in route.dependant.path_params}
    query_params = {p.name for p in route.dependant.query_params}
    assert "thing_id" in path_params, "path placeholder did not bind as a path param"
    assert "verbose" in query_params, "scalar param should remain a query param"
    assert "thing_id" not in query_params
    assert route.status_code == 201, "status_code did not reach the generated route"
    assert is_async is False, "a sync service method must yield a sync endpoint"


def test_adapter_generates_async_endpoint_for_coroutine_method():
    """A coroutine service method yields an ``async def`` endpoint FastAPI awaits."""
    spec = _spec(_FakeService.make_thing, "POST", "/things/{thing_id}", status_code=202)
    route, is_async = _route_from_spec(spec)

    assert is_async is True, "coroutine service method must yield a coroutine endpoint"
    assert {p.name for p in route.dependant.path_params} == {"thing_id"}
    assert route.status_code == 202


def test_adapter_passes_through_path_converter():
    """The ``{name:path}`` converter survives add_api_route and still binds as a path
    param (needed for file-path style routes)."""
    spec = _spec(_FakeService.read_blob, "GET", "/blobs/{blob_path:path}")
    route, _ = _route_from_spec(spec)

    assert route.path == "/blobs/{blob_path:path}", "path converter was rewritten"
    assert {p.name for p in route.dependant.path_params} == {"blob_path"}
    assert route.status_code is None, "unset status_code must stay FastAPI's default"
