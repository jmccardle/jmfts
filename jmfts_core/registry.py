"""``@expose`` — the one-definition-many-transports registry.

A service method decorated with ``@expose`` becomes a first-class JMFTS operation:
the in-process Python caller invokes the method directly, and the REST adapter
(``api/wiring.py``) generates a route from the same metadata. There is no second,
hand-written definition of the endpoint to drift out of sync.

This module is deliberately FastAPI-free. It records *what* an operation is (verb,
path, response type, which exceptions map to which HTTP status); the adapter decides
*how* to serve it. ``tests/test_api_parity.py`` asserts REGISTRY and the mounted
routes stay in bijection, and that this module imports no web framework.

Usage::

    @register_service
    class SearchService:
        def __init__(self, session): ...

        @expose("POST", "/search/hybrid", response_model=SearchResponse,
                 errors={ValueError: 400})
        def hybrid_search(self, request: HybridSearchRequest, *,
                          context: str | None = None, rerank: bool = False): ...

The decorated method keeps its normal Python signature, so direct callers are
unaffected. Parameter kind → transport binding is left to FastAPI's own inference in
the adapter (a single ``BaseModel`` param is the body; scalars are query params),
which reproduces the pre-unification wire contract exactly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class ExposeSpec:
    """Everything the REST adapter needs to generate a route for one operation."""

    method: str  # HTTP verb, e.g. "POST"
    path: str  # route path, e.g. "/search/hybrid"
    func: Callable  # the unbound service method (takes ``self`` first)
    response_model: Optional[type] = None
    errors: dict[type, int] = field(default_factory=dict)  # exc type -> HTTP status
    tags: Optional[list[str]] = None
    summary: Optional[str] = None
    status_code: Optional[int] = None  # non-200 success status (e.g. 201); None = FastAPI default
    service_cls: Optional[type] = None  # filled in by ``register_service``

    @property
    def name(self) -> str:
        """Stable operation id: ``ServiceClass.method``."""
        cls = self.service_cls.__name__ if self.service_cls else "?"
        return f"{cls}.{self.func.__name__}"


# The global operation registry. Ordered; deduped by (method, path).
REGISTRY: list[ExposeSpec] = []


def expose(
    method: str,
    path: str,
    *,
    response_model: Optional[type] = None,
    errors: Optional[dict[type, int]] = None,
    tags: Optional[list[str]] = None,
    summary: Optional[str] = None,
    status_code: Optional[int] = None,
) -> Callable:
    """Mark a service method as an exposed operation.

    Attaches an :class:`ExposeSpec` to the function; ``register_service`` (applied to
    the owning class) links it to its class and adds it to :data:`REGISTRY`. The method
    itself is returned unchanged, so in-process callers see a plain method.
    """

    def decorator(func: Callable) -> Callable:
        func.__jmfts_expose__ = ExposeSpec(
            method=method.upper(),
            path=path,
            func=func,
            response_model=response_model,
            errors=dict(errors or {}),
            tags=tags,
            summary=summary or (func.__doc__ or "").strip().split("\n", 1)[0] or None,
            status_code=status_code,
        )
        return func

    return decorator


def register_service(cls: type) -> type:
    """Class decorator: collect every ``@expose``-marked method into the registry.

    Idempotent — re-importing the module (common under ``--reload`` and in tests)
    will not double-register: specs are deduped by (method, path).
    """
    existing = {(s.method, s.path) for s in REGISTRY}
    for attr in cls.__dict__.values():
        spec = getattr(attr, "__jmfts_expose__", None)
        if spec is None:
            continue
        spec.service_cls = cls
        if (spec.method, spec.path) in existing:
            continue
        REGISTRY.append(spec)
        existing.add((spec.method, spec.path))
    return cls
