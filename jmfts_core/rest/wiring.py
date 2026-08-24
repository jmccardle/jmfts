"""REST adapter — generate FastAPI routes from the ``@expose`` registry.

For each :class:`~jmfts_core.registry.ExposeSpec` this builds an endpoint whose
signature MIRRORS the service method (minus ``self``, plus an injected ``db``). FastAPI
then infers body-vs-query exactly as it always has — a single ``BaseModel`` parameter is
the request body, scalars are query parameters — so the generated wire contract matches
the hand-written route it replaces, field for field.

A synchronous service method yields a plain ``def`` endpoint: FastAPI runs it in a
threadpool, so the synchronous repositories keep working without an async rewrite. A
service method that is itself a coroutine function yields an ``async def`` endpoint that
awaits it, so async services run on the event loop without a threadpool hop. Both set
``__signature__`` identically, so body/query/path inference is the same either way.

Domain → HTTP mapping lives here, not in the service: each spec's ``errors`` dict says
which exception types become which status codes (subclasses included). A spec's optional
``status_code`` sets the success status (e.g. 201 Created); unset means FastAPI's default.

Two annotations are TRANSLATED rather than mirrored, and both for the same reason: a
service method may not import a web framework, so it cannot say the thing FastAPI needs to
hear. The adapter learns the type instead.

* ``jmfts_client.contracts.upload.UploadedFile`` is republished as ``fastapi.UploadFile``
  and converted back before the service sees it. That is what lets a multipart upload be an
  ordinary ``@expose``'d service method — see ``contracts/upload.py`` for why the
  alternatives (importing ``UploadFile`` into core, or hand-writing the route) each break
  one of the parity seals.
* ``jmfts_client.contracts.rdf.TurtleDocument`` is republished as
  ``Annotated[str, Body(media_type="text/turtle")]``, so ``POST /ontologies`` takes a
  vocabulary as the file it is instead of as a JSON string. No conversion back is needed:
  a ``NewType`` of ``str`` IS a ``str`` at runtime, so the service receives exactly what
  FastAPI parsed.

A third entry in that list should prompt the question of whether ``@expose`` wants a way to
declare a media type, rather than the adapter growing another special case.

Annotations are resolved WITH their ``Annotated`` extras, because that metadata is how a
parameter declares its wire behaviour to FastAPI rather than being commentary on it.
"""

from __future__ import annotations

import inspect
import typing
from typing import Callable

from fastapi import APIRouter, Body, Depends, HTTPException, UploadFile
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session

from jmfts_client.contracts.rdf import TurtleDocument
from jmfts_client.contracts.upload import UploadedFile
from jmfts_core.database import get_db
from jmfts_core.registry import REGISTRY, ExposeSpec

#: What ``TurtleDocument`` becomes on the wire. A single non-model ``Body`` parameter is
#: NOT embedded by FastAPI, so the whole request body is the value — which is the point:
#: the bytes stored in ``ontologies.source_turtle`` are the bytes that were POSTed, with no
#: escaping step in between.
_TURTLE_BODY = typing.Annotated[str, Body(media_type="text/turtle")]


def _status_for(exc: Exception, error_map: dict[type, int]) -> int | None:
    """Return the HTTP status for ``exc`` per ``error_map`` (nearest base wins)."""
    match: int | None = None
    best_depth = -1
    for exc_type, status in error_map.items():
        if isinstance(exc, exc_type):
            # Prefer the most specific mapped type in the MRO.
            depth = len(exc_type.__mro__)
            if depth > best_depth:
                best_depth, match = depth, status
    return match


def _make_endpoint(spec: ExposeSpec) -> Callable:
    """Build a FastAPI endpoint that constructs the service and calls the method."""
    method_sig = inspect.signature(spec.func)
    # Resolve annotations in the SERVICE module's namespace. Services commonly use
    # ``from __future__ import annotations``, which makes signature annotations plain
    # strings; those would fail to resolve in this adapter module's globals, and
    # FastAPI would silently misclassify a Pydantic body param as a query param.
    # get_type_hints evaluates the forward refs where the method is defined.
    #
    # ``include_extras=True`` because ``Annotated`` metadata is not decoration to FastAPI
    # — it is where a parameter's wire behaviour is declared. The default strips it, so
    # ``Optional[Json[dict]]`` would arrive here as a plain ``Optional[dict]`` and the
    # multipart form field a caller can actually send (a JSON string) would be rejected as
    # not-a-dict, with the endpoint still advertising it in the schema.
    hints = typing.get_type_hints(spec.func, include_extras=True)
    # Every parameter of the service method except ``self``, with real-type annotations.
    # ``UploadedFile`` is the one annotation that does not survive verbatim: FastAPI has
    # no idea what it is, so the endpoint advertises ``UploadFile`` and the wrapper below
    # converts. The substitution MUST happen before the Signature is built at the bottom
    # of this function — that signature is what FastAPI reads to decide the request is
    # multipart at all.
    service_params = []
    upload_params: list[str] = []
    for name, param in method_sig.parameters.items():
        if name == "self":
            continue
        annotation = hints.get(name, param.annotation)
        if annotation is UploadedFile:
            upload_params.append(name)
            annotation = UploadFile
        elif annotation is TurtleDocument:
            annotation = _TURTLE_BODY
        service_params.append(param.replace(annotation=annotation))
    # Inject the request-scoped session as a keyword-only Depends param.
    db_param = inspect.Parameter(
        "db",
        inspect.Parameter.KEYWORD_ONLY,
        default=Depends(get_db),
        annotation=Session,
    )
    error_map = spec.errors
    service_cls = spec.service_cls
    func = spec.func

    def _to_http(exc: Exception) -> HTTPException:
        """Translate a domain exception into the mapped HTTPException, or re-raise.

        The HTTP ``detail`` is ``str(exc)`` by default — byte-identical to the
        hand-written routes' ``HTTPException(status, detail=str(e))``. An exception may
        instead carry a structured ``http_detail`` (dict/list) attribute, which is
        emitted verbatim; this preserves the one route whose detail was a JSON object
        rather than a string (``POST /documents/{id}/embed``'s ``text_too_long`` payload).
        """
        status = _status_for(exc, error_map)
        if status is None:
            raise exc
        detail = getattr(exc, "http_detail", None)
        return HTTPException(status_code=status, detail=detail if detail is not None else str(exc))

    # A coroutine service method needs an ``async def`` endpoint so the await happens on
    # the event loop; a sync method keeps the plain ``def`` FastAPI runs in a threadpool.
    if inspect.iscoroutinefunction(func):

        async def endpoint(**kwargs):
            db = kwargs.pop("db")
            for name in upload_params:
                part = kwargs[name]
                # ``await part.read()`` on the event loop — the sync ``part.file.read()``
                # below would block it while a large upload is copied off the spool.
                kwargs[name] = UploadedFile(
                    data=await part.read(),
                    filename=part.filename,
                    content_type=part.content_type,
                )
            service = service_cls(db)
            try:
                return await func(service, **kwargs)
            except Exception as exc:  # noqa: BLE001 — deliberate domain→HTTP boundary
                raise _to_http(exc)

    else:

        def endpoint(**kwargs):
            db = kwargs.pop("db")
            for name in upload_params:
                part = kwargs[name]
                # This endpoint already runs in FastAPI's threadpool, so reading the
                # SpooledTemporaryFile synchronously blocks a worker thread, not the loop.
                kwargs[name] = UploadedFile(
                    data=part.file.read(),
                    filename=part.filename,
                    content_type=part.content_type,
                )
            service = service_cls(db)
            try:
                return func(service, **kwargs)
            except Exception as exc:  # noqa: BLE001 — deliberate domain→HTTP boundary
                raise _to_http(exc)

    # FastAPI reads __signature__ for dependency/param inference. Presenting the
    # service method's own parameters here is what preserves the wire contract — a param
    # whose name matches a ``{placeholder}`` in the path binds as a path param, the rest
    # as body/query, exactly as inference does for a hand-written route.
    endpoint.__signature__ = inspect.Signature(service_params + [db_param])
    endpoint.__name__ = func.__name__
    endpoint.__doc__ = func.__doc__
    return endpoint


def build_exposed_router() -> APIRouter:
    """Assemble one APIRouter carrying every registered ``@expose`` operation.

    Importing the service modules (which triggers ``@register_service``) must happen
    before this is called; ``jmfts_core.rest.main`` imports them, then mounts this router.
    """
    router = APIRouter()
    for spec in REGISTRY:
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
    return router


class RouteWalkError(RuntimeError):
    """This walker met a mounting shape it has not been taught to flatten."""


def iter_mounted_api_routes(app) -> list[APIRoute]:
    """Every ``APIRoute`` the app actually serves, in mounting order.

    **Do not iterate ``app.routes`` directly.** It is not a flat list of routes and has
    not been one since FastAPI 0.141: ``include_router`` now leaves an ``_IncludedRouter``
    wrapper in ``app.routes`` and keeps the real routes on its ``original_router``. The
    same app reports 100 ``APIRoute`` objects under fastapi 0.135.3 and 3 under 0.141.1.

    Measured 2026-08-22. Three of those 0.141 failures were loud — ``test_runner_auth``'s
    vacuity guard, and ``scripts/generate_client`` refusing to render a client from 0
    joined routes. The fourth was not: ``test_api_parity``'s straggler check iterates
    every mounted route looking for hand-written ones that escaped ``@expose``, found
    three infra routes instead of a hundred, and PASSED. A seal that silently inspects
    nothing is worse than one that breaks, so the traversal lives here once rather than
    being open-coded at each call site where it can rot separately.

    Walks recursively and de-duplicates, so it is correct on both shapes: a FastAPI that
    flattens returns its routes directly, and one that wraps is followed through the
    wrapper.
    """
    found: list[APIRoute] = []
    seen: set[int] = set()

    def walk(routes) -> None:
        for route in routes:
            if isinstance(route, APIRoute):
                if id(route) not in seen:
                    seen.add(id(route))
                    found.append(route)
                continue

            # The 0.141 wrapper: no `.routes` of its own, the real ones hang off
            # `original_router`.
            inner = getattr(route, "original_router", None)
            if inner is not None:
                _refuse_a_prefix_we_cannot_compose(route)
                walk(inner.routes)
                continue

            # A Mount, a sub-application, or an older FastAPI's nested router.
            nested = getattr(route, "routes", None)
            if nested is not None:
                walk(nested)

    walk(app.routes)
    return found


def _refuse_a_prefix_we_cannot_compose(wrapper) -> None:
    """Raise if a wrapped router was included under a prefix.

    Both of this app's routers are included with no prefix and carry their full path on
    the route itself, so flattening is a plain collection. A non-empty prefix would mean
    the served path is the prefix plus the route's own path, and this walker would start
    reporting paths the app does not serve — which every caller here uses to decide
    whether a route exists at all.

    There is no such case in the tree to verify a composition against, so this refuses
    instead of guessing. Teach it the composition when the first prefixed router lands.
    """
    context = getattr(wrapper, "include_context", None)
    prefix = getattr(context, "prefix", "") or ""
    if prefix:
        raise RouteWalkError(
            f"a router was included under the prefix {prefix!r}, and "
            "iter_mounted_api_routes() has not been taught to compose a prefix with the "
            "paths beneath it. Teach it (jmfts_core/rest/wiring.py) rather than removing "
            "this check: the callers use the returned paths to decide what is mounted."
        )


def build_openapi_tags() -> list[dict[str, str]]:
    """Describe each tag group in the OpenAPI document, taken from the owning service.

    ``/docs`` groups 78 operations by tag and shows nothing but the tag's own name above
    each group. The description comes from the docstring of the service class that
    implements those operations, so the text in the browser is the text in the code and
    cannot drift from it — there is no second list of tag blurbs to update.

    A tag is expected to belong to exactly one service. If a later tag is shared by two,
    this returns the first one's docstring rather than guessing at a merge; the parity
    tests are the place to catch that, not a silent join here.
    """
    described: dict[str, str] = {}
    for spec in REGISTRY:
        for tag in spec.tags or ():
            if tag in described or spec.service_cls is None:
                continue
            summary = (spec.service_cls.__doc__ or "").strip().split("\n", 1)[0]
            if summary:
                described[tag] = summary
    return [{"name": tag, "description": text} for tag, text in described.items()]
