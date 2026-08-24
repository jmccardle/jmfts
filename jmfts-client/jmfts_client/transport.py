"""The HTTP transport every generated verb calls.

``_VerbTransport._call`` is the ONE hand-written request path in this package. Everything
in ``_verbs.py`` is generated and does nothing but name a method, a path, and where each
argument belongs. Keeping the request logic here is what makes regenerating the verb table
a safe operation: the generated file has no behaviour to lose.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Optional
from urllib.parse import quote

import httpx
from pydantic import BaseModel

from jmfts_client.contracts.upload import UploadedFile
from jmfts_client.errors import JmftsTransportError, exception_for

DEFAULT_BASE_URL = "http://localhost:8100"
DEFAULT_TIMEOUT = 60.0


class _VerbTransport:
    """Base class holding the connection and the single request path.

    Subclassed by :class:`jmfts_client.remote.RemoteJmftsClient`, which mixes in the
    generated verbs. It is separate from that class so the generated file can be replaced
    wholesale without touching anything that holds state.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        token: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        headers = {"User-Agent": "jmfts-client"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        #: An externally supplied client is NOT closed by ``close()`` — whoever built it
        #: owns its lifetime, and closing a shared pool from here would break the owner.
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout, headers=headers)
        if client is not None:
            self._client.headers.update(headers)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "_VerbTransport":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- the single request path -------------------------------------------------

    def _call(
        self,
        method: str,
        path_template: str,
        *,
        path: Optional[Mapping[str, Any]] = None,
        query: Optional[Mapping[str, Any]] = None,
        body: Any = None,
        content: Optional[str] = None,
        content_type: Optional[str] = None,
        files: Optional[Mapping[str, UploadedFile]] = None,
        form_json: Optional[Mapping[str, Any]] = None,
        response: Any = None,
    ) -> Any:
        """Issue one request and return the parsed, validated result.

        ``path`` fills ``{placeholder}`` segments; ``query`` becomes the query string with
        ``None`` values dropped, because a server default and an explicitly-sent null are
        different things. ``body`` is a single Pydantic model sent as JSON. ``files`` and
        ``form_json`` are the multipart case: the file parts, and the fields that travel
        beside them as JSON text.

        ``content`` with ``content_type`` is the fourth and narrowest case: a body the route
        declared under a media type of its own (``text/turtle``), sent verbatim. It is the
        one body form that is NOT serialised on the way out, because for these routes the
        bytes are the record — ``ontologies.source_turtle`` stores exactly what arrives.
        """
        url = self.base_url + _fill_path(path_template, path or {})

        params = None
        if query:
            params = {k: _query_value(v) for k, v in query.items() if v is not None}

        kwargs: dict[str, Any] = {}
        if content is not None:
            kwargs["content"] = content.encode("utf-8")
            # Explicit, and with a charset: httpx would otherwise send no Content-Type at
            # all for raw bytes, and FastAPI reads a missing Content-Type as JSON.
            kwargs["headers"] = {"Content-Type": f"{content_type or 'text/plain'}; charset=utf-8"}
        elif files is not None:
            kwargs["files"] = {
                name: (f.filename, f.data, f.content_type) for name, f in files.items()
            }
            data = {k: json.dumps(v) for k, v in (form_json or {}).items() if v is not None}
            if data:
                kwargs["data"] = data
        elif body is not None:
            kwargs["json"] = body.model_dump(mode="json") if isinstance(body, BaseModel) else body

        try:
            resp = self._client.request(method, url, params=params, **kwargs)
        except httpx.HTTPError as exc:
            raise JmftsTransportError(f"{method} {url} failed: {exc}") from exc

        if resp.status_code >= 400:
            raise exception_for(resp.status_code, _detail_of(resp), url)

        if resp.status_code == 204 or not resp.content:
            return None
        payload = resp.json()
        return _validate(payload, response)


def _fill_path(template: str, values: Mapping[str, Any]) -> str:
    """Substitute ``{name}`` placeholders, percent-encoding each value.

    One path uses Starlette's ``{usetype:path}`` converter, whose whole point is that the
    value may contain ``/``. The converter name is stripped and the slash is preserved for
    that parameter only; every other value has its slashes encoded.
    """
    out = template
    for name, value in values.items():
        text = str(value)
        if "{" + name + ":path}" in out:
            out = out.replace("{" + name + ":path}", quote(text, safe="/"))
        else:
            out = out.replace("{" + name + "}", quote(text, safe=""))
    return out


def _query_value(value: Any) -> Any:
    """Render one query value the way the server's parser expects to read it."""
    if isinstance(value, bool):
        # httpx would send Python's "True"/"False"; FastAPI's bool parser wants lowercase.
        return "true" if value else "false"
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _detail_of(resp: httpx.Response) -> Any:
    """The server's ``detail``, or the raw body when the error was not ours to shape."""
    try:
        payload = resp.json()
    except ValueError:
        return resp.text
    if isinstance(payload, dict) and "detail" in payload:
        return payload["detail"]
    return payload


def _validate(payload: Any, response: Any) -> Any:
    """Turn parsed JSON into the declared response type.

    ``response`` is ``None`` for the operations whose route declares no response model.
    Those return the parsed JSON unchanged — the server promised no shape, so inventing
    one here would be a claim this client cannot support.
    """
    if response is None:
        return payload
    origin = getattr(response, "__origin__", None)
    if origin is list:
        (item_type,) = response.__args__
        return [item_type.model_validate(item) for item in payload]
    return response.model_validate(payload)
