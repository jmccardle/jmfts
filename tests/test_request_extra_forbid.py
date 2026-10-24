"""Every request contract refuses an undeclared field (issue #47, completing #33).

The list is built from the live app, not from a file of names: every mounted route's body
annotation, which covers the REGISTRY operations and the hand-written ``/runner`` routes
alike, then recursively every ``BaseModel`` those models hold in a field. A new request
model therefore fails here until it states ``extra="forbid"`` (or ``"allow"``, on purpose).

The multipart ``Body_*`` models are FastAPI's own, generated from ``Form``/``File``
parameters; they are not contracts and carry no declaration to check.
"""

from __future__ import annotations

import typing

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError

from jmfts_client.contracts import (
    SynthesizeRequest,
    TemplateCreate,
    TemplateResponse,
)


def _collect(annotation, out: set) -> None:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if annotation in out:
            return
        out.add(annotation)
        for field in annotation.model_fields.values():
            _collect(field.annotation, out)
        return
    for arg in typing.get_args(annotation):
        _collect(arg, out)


def request_models() -> list[type[BaseModel]]:
    import jmfts_core.rest.main as main
    from jmfts_core.rest.wiring import iter_mounted_api_routes

    found: set[type[BaseModel]] = set()
    for route in iter_mounted_api_routes(main.app):
        body = getattr(route, "body_field", None)
        if body is not None:
            _collect(body.field_info.annotation, found)
    return sorted(
        (m for m in found if not m.__name__.startswith("Body_")),
        key=lambda m: (m.__module__, m.__name__),
    )


def test_the_sweep_finds_the_request_surface():
    names = {m.__name__ for m in request_models()}
    # one REGISTRY body, one runner body, one nested model: the three ways in
    assert {"SynthesizeRequest", "RunnerEmbedRequest", "Projection"} <= names
    assert len(names) >= 40


def test_every_request_model_declares_extra():
    undeclared = [
        f"{m.__module__}.{m.__name__}"
        for m in request_models()
        if m.model_config.get("extra") not in ("forbid", "allow")
    ]
    assert (
        not undeclared
    ), f"request models that leave `extra` at Pydantic's default (silently drop): {undeclared}"


def test_a_stale_field_is_a_422_naming_it():
    app = FastAPI()

    @app.post("/echo")
    def echo(req: SynthesizeRequest):
        return {"query": req.query}

    response = TestClient(app).post("/echo", json={"query": "x", "max_context_tokens": 5})
    assert response.status_code == 422
    (detail,) = response.json()["detail"]
    assert detail["type"] == "extra_forbidden"
    assert detail["loc"] == ["body", "max_context_tokens"]


def test_the_contract_raises_in_process_too():
    with pytest.raises(ValidationError, match="max_context_tokens"):
        SynthesizeRequest.model_validate({"query": "x", "max_context_tokens": 5})


def test_stored_template_variables_with_extra_keys_still_read_back():
    # TemplateResponse reads variables from storage through the lenient StoredTemplateVariable;
    # the request-side TemplateVariable forbids.
    stored = {"name": "who", "legacy_key": 1}
    response = TemplateResponse(id=1, title="t", content="c", category="x", variables=[stored])
    assert response.variables[0].name == "who"
    with pytest.raises(ValidationError, match="legacy_key"):
        TemplateCreate(title="t", content="c", category="x", variables=[stored])
