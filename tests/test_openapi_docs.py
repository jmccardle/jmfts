"""The OpenAPI document and the interactive pages built from it.

FastAPI has always served ``/docs``, ``/redoc`` and ``/openapi.json``. What it did not
serve was a document that named a credential, so the page listed every operation and could
not call one: "Try it out" went out with no ``Authorization`` header and came back 401 with
nothing on screen to say why. These tests cover the part that fixes that — the two declared
schemes, and the correction pass that makes each operation name the credential the running
gate actually accepts.

Two properties are asserted as properties rather than as a fixed list, so a route added
later is covered without this file being edited:

* every operation's declared credential is derived here from ``PUBLIC_PATHS`` and
  ``RUNNER_PREFIX`` — the same two constants ``require_token`` branches on;
* no operation offers a *choice* of credential, because none of them accepts two.

No database is needed: the document is built from the route table, and the handful of live
requests go to ``/config`` and ``/health``, which answer whatever the database is doing.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jmfts_core.rest.auth import (
    API_SCHEME,
    JMFTS_RUNNER_SCHEME,
    PUBLIC_PATHS,
    RUNNER_PREFIX,
)
from jmfts_core.rest.main import app
from tests.conftest import AUTH_HEADERS, TEST_API_TOKEN

client = TestClient(app)

_HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})


@pytest.fixture(scope="module")
def spec() -> dict:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    return response.json()


def _operations(spec: dict):
    """Yield ``(path, method, operation)`` for every operation in the document."""
    for path, path_item in spec["paths"].items():
        for method, operation in path_item.items():
            if method.lower() in _HTTP_METHODS:
                yield path, method.lower(), operation


def _expected_security(path: str) -> list[dict]:
    """The credential a path takes, read off the same constants the gate reads."""
    if path in PUBLIC_PATHS:
        return []
    if path.startswith(RUNNER_PREFIX):
        return [{JMFTS_RUNNER_SCHEME: []}]
    return [{API_SCHEME: []}]


# ── the pages are reachable, and deliberately so ────────────────────────────


@pytest.mark.parametrize("path", ["/openapi.json", "/docs", "/redoc"])
def test_the_document_and_its_pages_answer_without_a_token(path: str):
    """Pinned, because it is a decision and not an oversight.

    These are plain Starlette routes, so the app-level dependency never applied to them.
    Gating them would also not work: a browser navigating to ``/docs`` cannot be made to
    send an ``Authorization`` header, so the gate would close the page rather than protect
    it. The document describes the interface; every operation in it still costs a token,
    which is what the rest of this file checks.
    """
    assert client.get(path).status_code == 200


def test_the_swagger_page_is_wired_to_this_document():
    """The HTML must point at the document under test, not at some other path."""
    body = client.get("/docs").text
    assert app.openapi_url in body


# ── the credentials ─────────────────────────────────────────────────────────


def test_both_credentials_are_declared_as_bearer_schemes(spec: dict):
    schemes = spec["components"]["securitySchemes"]
    assert set(schemes) == {API_SCHEME, JMFTS_RUNNER_SCHEME}
    for name, scheme in schemes.items():
        assert scheme["type"] == "http", name
        assert scheme["scheme"] == "bearer", name
        # The description is what the operator reads in the Authorize dialog. Empty text
        # there means a box with two identical-looking fields and no way to tell them apart.
        assert scheme.get("description"), name


def test_every_operation_names_the_credential_the_gate_accepts(spec: dict):
    wrong = {
        f"{method.upper()} {path}": operation.get("security")
        for path, method, operation in _operations(spec)
        if operation.get("security") != _expected_security(path)
    }
    assert not wrong, f"operations documenting a credential the gate does not accept: {wrong}"


def test_no_operation_offers_a_choice_of_credential(spec: dict):
    """Several ``security`` entries read as alternatives, and nothing here accepts two.

    This is the failure the correction pass exists to prevent. Generation declares the API
    token on every route, including ``/runner``, which would publish "API token or runner
    key" for a surface that refuses the API token outright.
    """
    ambiguous = {
        f"{method.upper()} {path}": operation["security"]
        for path, method, operation in _operations(spec)
        if len(operation.get("security", [])) > 1
    }
    assert not ambiguous


def test_the_public_probe_asks_for_nothing(spec: dict):
    """A liveness probe carries no secret, and the document has to say so.

    ``security: []`` is how OpenAPI states that. An absent key would mean "inherit the
    document default", which is not the same claim.
    """
    for path in PUBLIC_PATHS & set(spec["paths"]):
        for _, method, operation in _operations({"paths": {path: spec["paths"][path]}}):
            assert operation["security"] == [], f"{method.upper()} {path}"


def test_the_runner_surface_asks_only_for_the_runner_key(spec: dict):
    runner_paths = [p for p in spec["paths"] if p.startswith(RUNNER_PREFIX)]
    assert runner_paths, "the /runner surface vanished from the document"
    for path in runner_paths:
        for _, method, operation in _operations({"paths": {path: spec["paths"][path]}}):
            assert operation["security"] == [{JMFTS_RUNNER_SCHEME: []}], f"{method.upper()} {path}"


# ── declaring the credential did not change what is enforced ────────────────


def test_declaring_the_scheme_did_not_loosen_the_parse():
    """The regression that the obvious implementation would have introduced.

    ``fastapi.security.HTTPBearer`` is the natural way to declare a bearer scheme, and it
    compares the scheme case-insensitively — adopting it would have started accepting
    ``bearer <token>`` as a side effect of writing documentation. ``BearerScheme`` declares
    without parsing for exactly this reason; ``tests/test_api_auth.py`` owns the full matrix
    and this is the one case that was at risk.
    """
    assert (
        client.get("/config", headers={"Authorization": f"bearer {TEST_API_TOKEN}"}).status_code
        == 401
    )
    assert client.get("/config", headers=AUTH_HEADERS).status_code == 200
    assert client.get("/config").status_code == 401


def test_the_public_probe_is_still_public():
    assert client.get("/health").status_code == 200


# ── navigability of a 78-operation document ─────────────────────────────────


def test_every_tag_in_use_is_described(spec: dict):
    """A tag with no description is a bare group heading in ``/docs``.

    Domain tag descriptions come from the service class that implements the group
    (``build_openapi_tags``), so this also catches a service losing its docstring.
    """
    described = {tag["name"] for tag in spec.get("tags", []) if tag.get("description")}
    in_use = {tag for _, _, op in _operations(spec) for tag in op.get("tags", [])}
    assert in_use - described == set()


def test_every_operation_is_tagged(spec: dict):
    untagged = [f"{m.upper()} {p}" for p, m, op in _operations(spec) if not op.get("tags")]
    assert not untagged


def test_every_operation_has_a_summary(spec: dict):
    """The summary is the one line ``/docs`` shows next to a collapsed operation.

    The groups start collapsed (``docExpansion: none``), so an operation with no summary is
    a path and a verb with nothing to say what it does.
    """
    unsummarised = [f"{m.upper()} {p}" for p, m, op in _operations(spec) if not op.get("summary")]
    assert not unsummarised
