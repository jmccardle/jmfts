"""Say so when a request carries fields its model does not declare (issue #33, 0.6.2).

The request contracts are plain Pydantic models, and Pydantic's default is to drop what it
does not recognise. A caller on an older wire format — tectum still sends
``max_context_tokens``, which 0.6.0 removed — therefore gets a 200 for a request part of
which was never read, with nothing to say so.

This is a SIGNAL, not a rejection. ``extra="forbid"`` is the 0.7.0 half of the issue
(corrected 2026-10-17: moved to 0.8.0, on every request contract, #47), and it
changes what validates; this changes nothing about what validates. The response carries

    X-JMFTS-Ignored-Fields: max_context_tokens, filters.colour

a comma-separated list of the JSON paths the server read and discarded, and the server logs
one WARNING per distinct (route, field set) so a client that sends the same stale field on
every request produces one line, not one per request.

**It is computed from the raw JSON against the model, not by changing model config.** An
in-process caller constructing ``SynthesizeRequest(query="x", max_context_tokens=5)`` is
unaffected: the model still ignores it, silently, and the contracts package gains no
behaviour. The comparison respects aliases and is done here, in one place, as a custom
``APIRoute`` class that both the generated routes and the hand-written runner router use —
so no endpoint can forget it, and a new router gets it by naming ``route_class``.

Path syntax: ``a.b`` for a key inside an object, ``a[].b`` for a key inside each item of a
list. A model declared ``extra="allow"`` reads its extras on purpose, so none are reported
under it. A union of several models is walked only for keys none of its members declare.

Only JSON bodies are inspected. A multipart upload or a ``text/turtle`` body has no field
set to compare. A response that is a 422 does not carry the header — the request was
refused, nothing was ignored — and any other response, including an ``HTTPException``,
does.
"""

from __future__ import annotations

import json
import logging
import types
import typing
from typing import Any, Callable

from fastapi import HTTPException, Request, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel

logger = logging.getLogger(__name__)

IGNORED_FIELDS_HEADER = "X-JMFTS-Ignored-Fields"

#: The header as OpenAPI describes it (a Header Object).
IGNORED_FIELDS_HEADER_DOC = {
    "description": (
        "Present when the request body carried fields this operation's contract does not "
        "declare, which were read and discarded. Comma-separated JSON paths "
        "(`a.b`, `a[].b`); a long list ends `(+N more)`. The request still succeeded."
    ),
    "schema": {"type": "string"},
}

#: How many paths one header lists. A body with a thousand unknown keys is a different
#: client, and a header that long is refused by proxies; the tail is counted, not listed.
MAX_LISTED = 20

#: How many distinct (route, field set) pairs the warn-once memory holds. A caller that
#: invents a new field name per request would otherwise grow it without bound; past the cap
#: the log degrades to quiet, and the header — which costs no memory — is unaffected.
_MAX_REMEMBERED = 1024
_warned: set[tuple[str, str, frozenset[str]]] = set()


def _accepted_keys(model: type[BaseModel]) -> set[str]:
    """The top-level keys ``model`` reads: aliases, and names where names are allowed."""
    config = model.model_config
    by_name = bool(config.get("populate_by_name") or config.get("validate_by_name"))
    keys: set[str] = set()
    for name, field in model.model_fields.items():
        aliases: list[str] = []
        if field.alias:
            aliases.append(field.alias)
        va = field.validation_alias
        if isinstance(va, str):
            aliases.append(va)
        elif va is not None and hasattr(va, "choices"):
            aliases.extend(c for c in va.choices if isinstance(c, str))
        if not aliases or by_name:
            keys.add(name)
        keys.update(aliases)
    return keys


def _models_in(annotation: Any) -> tuple[list[type[BaseModel]], bool]:
    """The model classes an annotation can hold, and whether they sit inside a list.

    Walks ``Optional``/``Union``/``Annotated``/``list``/``dict`` values. Returns
    ``([], False)`` for anything that is not a model, which is every scalar.
    """
    origin = typing.get_origin(annotation)
    if origin is typing.Annotated:
        return _models_in(typing.get_args(annotation)[0])
    if origin in (typing.Union, types.UnionType):
        found: list[type[BaseModel]] = []
        in_list = False
        for arm in typing.get_args(annotation):
            models, listed = _models_in(arm)
            found.extend(models)
            in_list = in_list or listed
        return found, in_list
    if origin in (list, set, frozenset, tuple):
        args = [a for a in typing.get_args(annotation) if a is not Ellipsis]
        found = []
        for arm in args:
            found.extend(_models_in(arm)[0])
        return found, True
    if origin is dict:
        args = typing.get_args(annotation)
        if len(args) == 2:
            # Values of a mapping have no key path of their own; walk them as a list would.
            return _models_in(args[1])[0], True
        return [], False
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation], False
    return [], False


def _walk(annotation: Any, data: Any, prefix: str, out: list[str]) -> None:
    models, _ = _models_in(annotation)
    if not models:
        return
    origin = typing.get_origin(annotation)
    # Unwrap containers so ``data`` is compared with the model it is made of.
    if isinstance(data, list):
        for item in data:
            _walk_models(models, item, f"{prefix}[]", out)
    elif isinstance(data, dict) and origin is dict:
        for value in data.values():
            _walk_models(models, value, f"{prefix}[]", out)
    else:
        _walk_models(models, data, prefix, out)


def _walk_models(models: list[type[BaseModel]], data: Any, prefix: str, out: list[str]) -> None:
    if not isinstance(data, dict):
        return
    if any(m.model_config.get("extra") == "allow" for m in models):
        return
    accepted: set[str] = set()
    for model in models:
        accepted |= _accepted_keys(model)
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if key not in accepted:
            out.append(path)
            continue
        # Descend only where exactly one model could own the key, so a union is never
        # blamed for a key one of its other members declares.
        owners = [m for m in models if key in _accepted_keys(m)]
        if len(owners) != 1:
            continue
        field = _field_for(owners[0], key)
        if field is not None:
            _walk(field.annotation, value, path, out)


def _field_for(model: type[BaseModel], key: str):
    for name, field in model.model_fields.items():
        if key == name or key == field.alias or key == field.validation_alias:
            return field
        va = field.validation_alias
        if va is not None and hasattr(va, "choices") and key in va.choices:
            return field
    return None


def find_ignored_fields(annotation: Any, data: Any) -> list[str]:
    """JSON paths in ``data`` that the model(s) in ``annotation`` do not declare, sorted."""
    out: list[str] = []
    _walk(annotation, data, "", out)
    return sorted(set(out))


def _header_value(paths: list[str]) -> str:
    # Header values are latin-1 on the wire; a key is caller-controlled text.
    safe = ["".join(c if 32 <= ord(c) < 127 else "?" for c in p) for p in paths]
    listed = ", ".join(safe[:MAX_LISTED])
    if len(safe) > MAX_LISTED:
        listed += f" (+{len(safe) - MAX_LISTED} more)"
    return listed


def _warn_once(method: str, path: str, paths: list[str]) -> None:
    key = (method, path, frozenset(paths))
    if key in _warned or len(_warned) >= _MAX_REMEMBERED:
        return
    _warned.add(key)
    logger.warning(
        "%s %s: request carried fields the contract does not declare, which were ignored: %s",
        key[0],
        key[1],
        _header_value(paths),
    )


class IgnoredFieldsRoute(APIRoute):
    """An ``APIRoute`` that reports the request-body fields its model discarded."""

    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            paths = await self._ignored(request)
            try:
                response = await original(request)
            except HTTPException as exc:
                if paths:
                    exc.headers = {
                        **(exc.headers or {}),
                        IGNORED_FIELDS_HEADER: _header_value(paths),
                    }
                raise
            if paths:
                response.headers[IGNORED_FIELDS_HEADER] = _header_value(paths)
            return response

        return handler

    async def _ignored(self, request: Request) -> list[str]:
        field = self.body_field
        if field is None or "json" not in request.headers.get("content-type", ""):
            return []
        raw = await request.body()
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except ValueError:
            return []  # FastAPI answers malformed JSON itself, with a 422.
        paths = find_ignored_fields(field.field_info.annotation, data)
        if paths:
            _warn_once(request.method, self.path, paths)
        return paths
