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

One annotation is TRANSLATED rather than mirrored: a parameter typed
``jmfts_core.contracts.upload.UploadedFile`` is republished to FastAPI as
``fastapi.UploadFile`` and converted back before the service sees it. That is what lets a
multipart upload be an ordinary ``@expose``'d service method while ``jmfts_core`` stays
free of any web framework — see the module docstring of ``contracts/upload.py`` for why
the alternatives (importing ``UploadFile`` into core, or hand-writing the route) each
break one of the parity seals.

Annotations are resolved WITH their ``Annotated`` extras, because that metadata is how a
parameter declares its wire behaviour to FastAPI rather than being commentary on it.
"""

from __future__ import annotations

import inspect
import typing
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy.orm import Session

from jmfts_core.contracts.upload import UploadedFile
from jmfts_core.database import get_db
from jmfts_core.registry import REGISTRY, ExposeSpec


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
    before this is called; ``api.main`` imports them, then mounts this router.
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
