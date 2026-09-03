"""A missing optional stack answers 501, on every operation. ``SPRINT_0_3_0.md`` 13.10.

The appliance has two optional stacks and two matching errors:
:class:`~jmfts_core.embedding.ModelStackNotInstalled` for the ``embed`` extra and
:class:`~jmfts_core.office.OfficeStackNotInstalled` for the ``office`` extra. Both
subclass ``ImportError``, so :mod:`jmfts_core.task_errors` already classifies them
PERMANENT on the queue side. This file is the HTTP side of the same fact.

**What was wrong.** ``GET /documents/{id}/cells`` was the only route in the tree that
mapped either one, so a base install answered it with a 501 naming the missing extra and
answered ``POST /search/hybrid`` with a bare 500. Two reports of one deployment fact, and
the 500 is the wrong one on its own terms: it tells a caller the server broke, when what
happened is that this server does not do that and will not after a retry.

**Three things under test, and they are different in kind.**

The **rule**: every spec in ``REGISTRY`` resolves both exception types through
``registry.DEFAULT_ERRORS``. This is an audit over the live registry rather than a list of
routes — a service that grows a call to the embedder tomorrow is covered the same day, and
there is no second list to drift. Part 14's rule in ``SPRINT_JOBS.md``.

The **precedence**: a spec that declares one of the two explicitly still wins. Nothing in
the tree does today; the mechanism is what keeps the default from being a ceiling.

The **wire**: the mounted route actually answers 501, with the exception's message, which
is the half that no assertion over the registry can reach.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jmfts_client.contracts.search import HybridSearchRequest
from jmfts_core import registry as registry_mod
from jmfts_core.database import get_db
from jmfts_core.embedding import EmbeddingService, ModelStackNotInstalled
from jmfts_core.office import OfficeStackNotInstalled
from jmfts_core.registry import REGISTRY, ExposeSpec
from jmfts_core.rest.main import app
from jmfts_core.rest.wiring import _status_for

OPTIONAL_STACK_ERRORS = (ModelStackNotInstalled, OfficeStackNotInstalled)


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("exc_type", OPTIONAL_STACK_ERRORS, ids=lambda t: t.__name__)
def test_every_operation_maps_both_optional_stack_errors(exc_type):
    """No operation may let an optional-stack failure reach FastAPI's default handler."""
    assert REGISTRY, "the registry is empty; the app did not import"

    unmapped = [
        spec.name
        for spec in REGISTRY
        if _status_for(exc_type("stack absent"), spec.effective_errors) is None
    ]
    assert unmapped == [], (
        f"{exc_type.__name__} reaches no mapping on {len(unmapped)} operation(s), so a "
        f"base install answers them 500: {unmapped[:5]}"
    )


@pytest.mark.parametrize("exc_type", OPTIONAL_STACK_ERRORS, ids=lambda t: t.__name__)
def test_the_status_is_501_and_not_503(exc_type):
    """501 says "this server does not implement this"; 503 says "try later" and is false.

    An install without the extra is a supported deployment — a storage-side worker with
    ``JMFTS_RUNNER_URL`` is exactly one — so no retry changes the answer.
    """
    for spec in REGISTRY:
        assert _status_for(exc_type("stack absent"), spec.effective_errors) == 501, spec.name


def test_a_spec_may_still_declare_its_own_status():
    """The default is a floor, not a ceiling: a spec's own ``errors`` wins."""

    def op(self):  # pragma: no cover — never called; this is a spec, not an operation
        """A spec built for this test."""

    spec = ExposeSpec(
        method="GET",
        path="/test/override",
        func=op,
        errors={OfficeStackNotInstalled: 418},
    )
    assert spec.effective_errors[OfficeStackNotInstalled] == 418
    # The other half of the pair is untouched by the override.
    assert spec.effective_errors[ModelStackNotInstalled] == 501


def test_the_default_is_the_only_declaration_of_it():
    """No spec repeats what ``DEFAULT_ERRORS`` already says.

    A duplicate that agrees is invisible until it stops agreeing, and this pair is
    exactly the kind of thing that gets pasted into the next route that needs it.
    ``get_document_cells`` is the one that used to and now carries a pointer instead.
    """
    repeated = [
        f"{spec.name} → {exc.__name__}"
        for spec in REGISTRY
        for exc, status in spec.errors.items()
        if exc in registry_mod.DEFAULT_ERRORS and status == registry_mod.DEFAULT_ERRORS[exc]
    ]
    assert repeated == [], (
        "these specs declare a mapping `registry.DEFAULT_ERRORS` already makes; delete "
        f"the entry and let the default carry it: {repeated}"
    )


# ---------------------------------------------------------------------------
# The wire
# ---------------------------------------------------------------------------


@pytest.fixture
def client_with_db(db_session):
    """TestClient bound to the savepoint-wrapped session (the house pattern)."""

    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    from tests.conftest import AUTH_HEADERS

    client = TestClient(app, headers=AUTH_HEADERS)
    yield client
    app.dependency_overrides.pop(get_db, None)


def test_hybrid_search_without_the_model_stack_answers_501(client_with_db, monkeypatch):
    """The route 13.10 measured at 500. It is the one this change is for.

    The stack is patched rather than uninstalled: this suite runs under ``dev``, which
    implies ``embed``, so the only way to stand where a base install stands is to make
    the embedder raise what a base install's embedder raises.

    **``EmbeddingService.embed_text`` and not the service method.** ``_make_endpoint``
    binds ``spec.func`` when the route is generated, at import, so patching
    ``SearchService.hybrid_search`` afterwards changes the class and not the route. This
    patch also sits where the real failure sits: ``SearchRepository.vector_search_text``
    calls ``embed_text``, and nothing between there and the adapter catches.
    """
    message = "No module named 'torch'\n\nInstall the extra, or set JMFTS_RUNNER_URL."

    def _no_stack(self, *args, **kwargs):
        raise ModelStackNotInstalled(message)

    monkeypatch.setattr(EmbeddingService, "embed_text", _no_stack)

    response = client_with_db.post(
        "/search/hybrid",
        json=HybridSearchRequest(query="anything", limit=1).model_dump(mode="json"),
    )

    assert response.status_code == 501, response.text
    # The message is the whole value of the exception — it names both ways out — and a
    # status that threw it away was half the defect.
    assert "JMFTS_RUNNER_URL" in response.json()["detail"]
