"""IC-1: an ``@expose``'d operation can answer with something that is not JSON.

``docs/SPRINT_0_6_0.md`` Block F step 16. Until this landed, ``ExposeSpec`` carried no way to
say a response was not JSON and ``rest/wiring.py`` passed only ``response_model``; the tree
had met the limit once and worked around it (``GET /rdf/turtle`` returns Turtle inside JSON,
and ``services/rdf_service.py:19`` gives the reason, which was right for that route). Steps 20
to 22 serve stored bytes, rendered pages and cropped rectangles, and none of those survives
being wrapped in a JSON string.

**These tests build their own service and their own app.** They deliberately do not wait for
steps 20 to 22 to exist, because the mechanism and its first three users are separate pieces
of work in separate worktrees (Part 5, gates M0 and M1) and a mechanism whose only test is its
first caller is a mechanism that gets re-litigated when the caller changes.

The registry is a module-level global, so every test here restores it. ``register_service``
dedupes by ``(method, path)``, which means a leaked fixture route would silently win over a
real one in a later test in the same session.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from jmfts_client.contracts.binary import BinaryPayload
from jmfts_core import registry
from jmfts_core.registry import expose, register_service
from jmfts_core.rest.wiring import build_exposed_router

#: A name that needs both halves of RFC 6266: the ASCII fallback cannot carry it, and
#: dropping it would lose the uploader's own name for their own file.
JAPANESE_NAME = "四半期報告書.docx"


class Count(BaseModel):
    """The control case's response model.

    Module scope, not function scope, and that is not a style preference: ``wiring.py``
    resolves a service method's annotations with ``typing.get_type_hints``, which evaluates
    the string form this file's ``from __future__ import annotations`` produces against the
    MODULE's globals. A class defined inside a test function is not in them, and the route
    build raises ``NameError`` — which is the same thing that would happen to a real service.
    """

    n: int


@pytest.fixture
def isolated_registry():
    """Swap ``REGISTRY`` for an empty list and put the real one back afterwards."""
    saved = list(registry.REGISTRY)
    registry.REGISTRY.clear()
    yield registry.REGISTRY
    registry.REGISTRY.clear()
    registry.REGISTRY.extend(saved)


def _app_with(service_cls) -> TestClient:
    """Mount one service's exposed routes on a bare app.

    ``get_db`` is overridden with a generator yielding ``None``: nothing under test touches
    a session, and provisioning a database to prove that a header is spelled correctly would
    make this file the slowest in the suite for no reading.
    """
    from jmfts_core.database import get_db

    app = FastAPI()
    app.include_router(build_exposed_router())
    app.dependency_overrides[get_db] = lambda: (yield None)  # type: ignore[misc]
    return TestClient(app)


def test_a_binary_operation_sends_bytes_with_its_own_content_type(isolated_registry):
    """The payload's media type reaches the wire, not the spec's."""

    @register_service
    class PixelService:
        def __init__(self, db):
            self.db = db

        @expose("GET", "/pixels/one", media_type="application/octet-stream", tags=["t"])
        def one(self) -> BinaryPayload:
            """One pixel."""
            return BinaryPayload(content=b"\x89PNG\r\n\x1a\n", media_type="image/png")

    resp = _app_with(PixelService).get("/pixels/one")

    assert resp.status_code == 200
    # The bytes are the bytes. A JSON-encoded body would be base64 inside quotes.
    assert resp.content == b"\x89PNG\r\n\x1a\n"
    # The PAYLOAD's type, not the `application/octet-stream` the route declares. That
    # separation is the whole reason BinaryPayload carries a media_type of its own.
    assert resp.headers["content-type"] == "image/png"
    assert resp.headers["content-disposition"] == "inline"


def test_the_declared_media_type_is_what_openapi_documents(isolated_registry):
    """A route that sends a PNG must not document an empty 200."""

    @register_service
    class DocumentedService:
        def __init__(self, db):
            self.db = db

        @expose("GET", "/pixels/documented", media_type="image/png", tags=["t"])
        def documented(self) -> BinaryPayload:
            """A documented pixel."""
            return BinaryPayload(content=b"x", media_type="image/png")

    schema = _app_with(DocumentedService).get("/openapi.json").json()
    content = schema["paths"]["/pixels/documented"]["get"]["responses"]["200"]["content"]

    assert "image/png" in content
    assert content["image/png"]["schema"] == {"type": "string", "format": "binary"}
    assert "application/json" not in content


def test_a_download_names_the_file_both_ways(isolated_registry):
    """RFC 6266: an ASCII fallback and the real name, so neither reader is lost."""

    @register_service
    class OriginalService:
        def __init__(self, db):
            self.db = db

        @expose("GET", "/originals/one", media_type="application/octet-stream", tags=["t"])
        def one(self) -> BinaryPayload:
            """The original bytes."""
            return BinaryPayload(
                content=b"PK\x03\x04",
                media_type=(
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                ),
                filename=JAPANESE_NAME,
                download=True,
            )

    disposition = _app_with(OriginalService).get("/originals/one").headers["content-disposition"]

    assert disposition.startswith("attachment; ")
    # The fallback keeps the name's shape rather than collapsing it, so two documents whose
    # names differ only outside ASCII do not land on one fallback.
    assert 'filename="______.docx"' in disposition  # 六 characters, one underscore each
    assert "filename*=UTF-8''%E5%9B%9B%E5%8D%8A%E6%9C%9F%E5%A0%B1%E5%91%8A%E6%9B%B8.docx" in (
        disposition
    )


def test_declaring_both_a_media_type_and_a_response_model_raises_at_import(isolated_registry):
    """The contradiction is refused where it is written, not discovered on the wire."""
    with pytest.raises(ValueError, match="pick one"):

        @expose("GET", "/pixels/confused", media_type="image/png", response_model=BinaryPayload)
        def confused(self):  # pragma: no cover — the decorator raises before this binds
            """Never registered."""


def test_a_binary_operation_that_forgets_to_wrap_its_bytes_raises(isolated_registry):
    """Returning raw bytes would reach FastAPI's encoder and become base64 in quotes."""

    @register_service
    class ForgetfulService:
        def __init__(self, db):
            self.db = db

        @expose("GET", "/pixels/raw", media_type="image/png", tags=["t"])
        def raw(self) -> BinaryPayload:
            """Bytes, unwrapped."""
            return b"\x89PNG"  # type: ignore[return-value]

    client = _app_with(ForgetfulService)
    with pytest.raises(TypeError, match="must return a BinaryPayload"):
        client.get("/pixels/raw")


def test_a_json_operation_is_untouched_by_the_new_path(isolated_registry):
    """The common case pays nothing and behaves exactly as before."""

    @register_service
    class CountingService:
        def __init__(self, db):
            self.db = db

        @expose("GET", "/counts/one", response_model=Count, tags=["t"])
        def one(self) -> Count:
            """A count."""
            return Count(n=1)

    resp = _app_with(CountingService).get("/counts/one")

    assert resp.status_code == 200
    assert resp.json() == {"n": 1}
    assert resp.headers["content-type"].startswith("application/json")
    assert "content-disposition" not in resp.headers
