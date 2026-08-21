"""The runner credential, and the seal that keeps it disjoint from the API token.

Two things are under test here. The first is ``require_runner`` itself: unconfigured is
503, wrong is 401, right is through. The second matters more over time — the arrangement
that puts the runner routes under a different dependency depends on two facts staying true
together, and neither is visible from the other's file:

* ``require_token`` steps aside for paths under ``RUNNER_PREFIX``.
* every route under that prefix declares ``require_runner``.

Break the second and the first turns those routes into open endpoints. So the
correspondence is asserted in both directions, from the live app's route table.
"""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import jmfts_core.rest.main as main
from jmfts_core.config import get_settings
from jmfts_core.rest.auth import RUNNER_PREFIX, require_runner
from tests.conftest import AUTH_HEADERS

RUNNER_KEY = "test-runner-key-shared-secret"


@pytest.fixture
def client():
    """A bare TestClient. No lifespan — none of these tests touch the database."""
    return TestClient(main.app)


@pytest.fixture
def with_runner_key(monkeypatch):
    """Configure a runner key for the process the app reads settings from."""
    settings = get_settings()
    monkeypatch.setattr(settings, "runner_key", RUNNER_KEY)
    return RUNNER_KEY


@pytest.fixture
def without_runner_key(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "runner_key", "")


# --- the structural seal -------------------------------------------------------------


def _runner_routes() -> list[APIRoute]:
    return [
        r for r in main.app.routes if isinstance(r, APIRoute) and r.path.startswith(RUNNER_PREFIX)
    ]


def test_runner_prefix_is_actually_populated():
    """Guard the two tests below from passing vacuously if the router stops being mounted."""
    assert _runner_routes(), f"no routes mounted under {RUNNER_PREFIX}"


def test_every_runner_route_declares_require_runner():
    """`require_token` waives this prefix, so `require_runner` is the ONLY thing gating it.

    A route added under /runner without the dependency would be reachable with no
    credential at all. This is the test that makes that impossible to do quietly.
    """
    ungated = [
        r.path
        for r in _runner_routes()
        if not any(d.call is require_runner for d in r.dependant.dependencies)
    ]
    assert not ungated, (
        f"routes under {RUNNER_PREFIX} with no require_runner dependency: {ungated}. "
        "The app-level require_token waives this prefix, so these are open."
    )


def test_no_route_outside_the_prefix_declares_require_runner():
    """The runner key must not be an alternative credential on the document API.

    If it were accepted anywhere a principal is expected, it would be a second owner token
    that skips the grant checks — which is the coupling this whole split exists to avoid.
    """
    strays = [
        (r.path, r.name)
        for r in main.app.routes
        if isinstance(r, APIRoute)
        and not r.path.startswith(RUNNER_PREFIX)
        and any(d.call is require_runner for d in r.dependant.dependencies)
    ]
    assert not strays, f"require_runner declared outside {RUNNER_PREFIX}: {strays}"


# --- the credential ------------------------------------------------------------------


def test_unconfigured_runner_surface_is_503(client, without_runner_key):
    """No key set means this deployment does not offer the surface — not 'come in'.

    503 rather than 401 is deliberate: an operator wiring up a fleet can tell a server that
    was never configured from one whose Secret does not match, without reading its logs.
    """
    response = client.get(f"{RUNNER_PREFIX}/info", headers={"Authorization": "Bearer anything"})
    assert response.status_code == 503
    assert "JMFTS_RUNNER_KEY" in response.json()["detail"]


def test_missing_header_is_401(client, with_runner_key):
    response = client.get(f"{RUNNER_PREFIX}/info")
    assert response.status_code == 401


def test_malformed_header_is_401(client, with_runner_key):
    response = client.get(f"{RUNNER_PREFIX}/info", headers={"Authorization": RUNNER_KEY})
    assert response.status_code == 401


def test_wrong_key_is_401(client, with_runner_key):
    response = client.get(f"{RUNNER_PREFIX}/info", headers={"Authorization": "Bearer not-the-key"})
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid runner key"


def test_correct_key_is_admitted(client, with_runner_key):
    """The one positive case. /info answers from configuration, so no model is loaded."""
    response = client.get(
        f"{RUNNER_PREFIX}/info", headers={"Authorization": f"Bearer {RUNNER_KEY}"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["model"]
    assert body["token_window"] > 0
    assert body["doc_window"] >= body["token_window"]


# --- the two credentials do not substitute for each other -----------------------------


def test_api_token_is_rejected_by_the_runner_surface(client, with_runner_key):
    """An API token authenticates a principal. It says nothing about embedding."""
    response = client.get(f"{RUNNER_PREFIX}/info", headers=AUTH_HEADERS)
    assert response.status_code == 401


def test_runner_key_is_rejected_by_the_document_api(client, with_runner_key):
    """And the reverse. `/config` is the cheapest gated route that needs no database."""
    response = client.get("/config", headers={"Authorization": f"Bearer {RUNNER_KEY}"})
    assert response.status_code == 401


def test_runner_surface_binds_no_principal(client, with_runner_key):
    """A runner request must leave the principal contextvar unset FOR THE HANDLER.

    Repositories read that contextvar to decide which subtree the caller may touch. Binding
    anything for a runner would be a claim about which documents it may see, and it holds
    none. Checking after the response would prove nothing — `require_token` resets in a
    `finally` — so the reading happens inside a probe route mounted under the real prefix,
    which is where a leaked binding would actually do damage.
    """
    from jmfts_core.principal_context import get_current_principal

    probe_path = f"{RUNNER_PREFIX}/principal-probe"
    runner_router = main.runner.router

    @runner_router.get(probe_path.removeprefix(RUNNER_PREFIX), include_in_schema=False)
    def _probe():
        principal = get_current_principal()
        return {"principal": None if principal is None else principal.name}

    # Re-mounting the router is what makes the new route reachable; the app copies routes
    # at include time rather than holding a live reference.
    main.app.include_router(runner_router)
    try:
        response = client.get(probe_path, headers={"Authorization": f"Bearer {RUNNER_KEY}"})
        assert response.status_code == 200
        assert response.json() == {"principal": None}
    finally:
        runner_router.routes = [r for r in runner_router.routes if r.path != probe_path]
        main.app.router.routes = [
            r for r in main.app.router.routes if getattr(r, "path", None) != probe_path
        ]
