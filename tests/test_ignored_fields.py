"""Issue #33 (0.6.2): a request field the contract does not declare is reported, not silent.

The signal is a response header and a once-per-shape warning. It is computed from the raw
JSON against the model, so none of these tests may need the model config to change — and
the last one pins that the contracts still accept (and drop) the field in-process.
"""

from __future__ import annotations

import logging
from typing import Optional

import pytest
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field

import jmfts_core.rest.main as main
from jmfts_client.contracts.search import SynthesizeRequest
from jmfts_core.rest import ignored_fields
from jmfts_core.rest.ignored_fields import (
    IGNORED_FIELDS_HEADER,
    IgnoredFieldsRoute,
    find_ignored_fields,
)
from jmfts_core.rest.wiring import iter_mounted_api_routes


class Inner(BaseModel):
    colour: str = "red"


class Open(BaseModel):
    model_config = ConfigDict(extra="allow")


class Aliased(BaseModel):
    model_config = ConfigDict(populate_by_name=False)
    real_name: int = Field(0, alias="wireName")


class Req(BaseModel):
    query: str
    inner: Optional[Inner] = None
    items: list[Inner] = []
    free: Optional[Open] = None
    aliased: Optional[Aliased] = None


@pytest.fixture
def client():
    app = FastAPI()
    router = APIRouter(route_class=IgnoredFieldsRoute)

    @router.post("/echo")
    def echo(req: Req):
        return {"query": req.query}

    @router.post("/refuse")
    def refuse(req: Req):
        raise HTTPException(409, "no")

    app.include_router(router)
    ignored_fields._warned.clear()
    return TestClient(app)


def test_extra_top_level_field_is_reported_and_request_succeeds(client):
    r = client.post("/echo", json={"query": "x", "max_context_tokens": 5})
    assert r.status_code == 200
    assert r.headers[IGNORED_FIELDS_HEADER] == "max_context_tokens"


def test_clean_request_carries_no_header(client):
    r = client.post("/echo", json={"query": "x", "inner": {"colour": "b"}})
    assert IGNORED_FIELDS_HEADER not in r.headers


def test_nested_fields_use_dotted_and_list_paths(client):
    r = client.post(
        "/echo",
        json={"query": "x", "inner": {"shade": 1}, "items": [{"colour": "a"}, {"hue": 2}], "zz": 0},
    )
    assert r.headers[IGNORED_FIELDS_HEADER] == "inner.shade, items[].hue, zz"


def test_extra_allow_model_reports_nothing_under_it(client):
    r = client.post("/echo", json={"query": "x", "free": {"anything": 1}})
    assert IGNORED_FIELDS_HEADER not in r.headers


def test_alias_is_the_accepted_name_and_the_python_name_is_not(client):
    ok = client.post("/echo", json={"query": "x", "aliased": {"wireName": 1}})
    assert IGNORED_FIELDS_HEADER not in ok.headers
    bad = client.post("/echo", json={"query": "x", "aliased": {"real_name": 1}})
    assert bad.headers[IGNORED_FIELDS_HEADER] == "aliased.real_name"


def test_http_error_response_still_carries_the_header(client):
    r = client.post("/refuse", json={"query": "x", "stale": 1})
    assert r.status_code == 409
    assert r.headers[IGNORED_FIELDS_HEADER] == "stale"


def test_validation_failure_is_422_without_the_header(client):
    r = client.post("/echo", json={"stale": 1})
    assert r.status_code == 422
    assert IGNORED_FIELDS_HEADER not in r.headers


def test_malformed_json_is_left_to_fastapi(client):
    r = client.post("/echo", content=b"{nope", headers={"content-type": "application/json"})
    assert r.status_code == 422


def test_long_list_is_truncated_and_non_ascii_keys_are_safe(client):
    body = {"query": "x", **{f"k{i:02d}": i for i in range(25)}, "é": 1}
    r = client.post("/echo", json=body)
    value = r.headers[IGNORED_FIELDS_HEADER]
    assert value.endswith("(+6 more)")
    assert value.count(",") == ignored_fields.MAX_LISTED - 1


def test_warning_is_logged_once_per_route_and_field_set(client, caplog):
    with caplog.at_level(logging.WARNING, logger=ignored_fields.logger.name):
        for _ in range(3):
            client.post("/echo", json={"query": "x", "stale": 1})
        client.post("/echo", json={"query": "x", "other": 1})
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 2
    assert "stale" in messages[0] and "/echo" in messages[0]


def test_find_ignored_fields_ignores_non_dict_bodies():
    assert find_ignored_fields(Req, ["x"]) == []
    assert find_ignored_fields(str, "x") == []


# --- the real app --------------------------------------------------------------------


def test_every_body_route_in_the_app_uses_the_signalling_route_class():
    routes = [r for r in iter_mounted_api_routes(main.app) if r.body_field is not None]
    assert routes
    plain = [
        f"{sorted(r.methods)} {r.path}" for r in routes if not isinstance(r, IgnoredFieldsRoute)
    ]
    assert not plain, f"routes with a body that cannot report ignored fields: {plain}"


def test_runner_route_reports_through_the_real_app():
    """/runner/embed with no runner key answers an error before the body is read; it still signals."""
    r = TestClient(main.app).post("/runner/embed", json={"text": "x", "max_context_tokens": 5})
    assert r.headers[IGNORED_FIELDS_HEADER] == "max_context_tokens"


def test_openapi_documents_the_header_on_json_body_operations():
    doc = main.app.openapi()
    ok = doc["paths"]["/runner/embed"]["post"]["responses"]["200"]
    assert IGNORED_FIELDS_HEADER in ok["headers"]
    assert "headers" not in doc["paths"]["/health"]["get"]["responses"]["200"]


def test_contract_itself_still_drops_the_field_in_process():
    """0.6.2 adds a signal; `extra="forbid"` is 0.7.0. In-process callers see no change."""
    req = SynthesizeRequest.model_validate({"query": "x", "max_context_tokens": 5})
    assert not hasattr(req, "max_context_tokens")
