"""Subtree RBAC — the ``TemplateService`` read verbs take the read gate.

A template IS a document (usetype ``template``, under a container node). Both verbs once
read it by ``session.get(Document, template_id)`` with no ``can_read`` (ROADMAP known gap
12; ``test_expose_principal_audit``).

    hidden (unreadable) template -> LookupError          (404, existence-hiding)
    reader                       -> allowed

``list_templates`` and ``search_templates`` drop a hidden template from their results
rather than failing the call.
"""

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from jmfts_client.contracts.template import TemplateRenderRequest
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.principal_context import OWNER, reset_principal, set_principal
from jmfts_core.services import template_service
from jmfts_core.services.template_service import TEMPLATE_USETYPE, TemplateService
from tests.test_access_write_gate import _as, _grant, _principal


def _governed_template(session):
    """A template that is its own access-control root: RD may read it, NA holds nothing.

    Written through the repository rather than ``create_template``, which places it under
    a module-cached container below the adjutant root — fixture state this test has no
    reason to depend on. ``get_template`` asks only that the usetype be ``template``.
    """
    tpl = DocumentRepository(session).create(
        title="greeting",
        content="Hello {{name}}",
        usetype=TEMPLATE_USETYPE,
        structured_content={
            "category": "ideation",
            "variables": [{"name": "name", "description": "", "required": True}],
            "usage_count": 0,
        },
        auto_embed=False,
    )
    session.flush()
    rd = _principal(session, "RD")
    na = _principal(session, "NA")
    _grant(session, tpl.id, rd, "read")
    return tpl.id, rd, na


def _usage_count(session, template_id):
    session.expire_all()
    from jmfts_core.models.document import Document

    return (session.get(Document, template_id).structured_content or {}).get("usage_count", 0)


def test_get_template_non_reader_sees_missing_template(db_session):
    tid, _, na = _governed_template(db_session)
    with _as(na), pytest.raises(LookupError, match="Template not found"):
        TemplateService(db_session).get_template(tid)


def test_get_template_reader_allowed(db_session):
    tid, rd, _ = _governed_template(db_session)
    with _as(rd):
        assert TemplateService(db_session).get_template(tid).id == tid


def test_render_template_non_reader_sees_missing_template_and_bumps_nothing(db_session):
    tid, _, na = _governed_template(db_session)
    request = TemplateRenderRequest(variables={"name": "x"})
    with _as(na), pytest.raises(LookupError, match="Template not found"):
        TemplateService(db_session).render_template(tid, request)
    assert _usage_count(db_session, tid) == 0


def test_render_template_reader_allowed(db_session):
    tid, rd, _ = _governed_template(db_session)
    request = TemplateRenderRequest(variables={"name": "Ada"})
    with _as(rd):
        out = TemplateService(db_session).render_template(tid, request)
    assert out.rendered == "Hello Ada"
    assert _usage_count(db_session, tid) == 1


def test_get_template_unbound_caller_allowed(db_session):
    tid, _, _ = _governed_template(db_session)
    assert TemplateService(db_session).get_template(tid).id == tid


# --- GET /templates, over HTTP -------------------------------------------------------------
#
# The list verb once ran a raw ``db.query(Document)`` with no access filter, so it returned
# the full body of a template that ``GET /templates/{id}`` answered 404.


@contextmanager
def _http(db_session, principal):
    from jmfts_core.database import get_db
    from jmfts_core.rest.auth import require_token
    from jmfts_core.rest.main import app

    async def _bind():
        token = set_principal(principal)
        try:
            yield
        finally:
            reset_principal(token)

    def _db():
        yield db_session

    app.dependency_overrides[require_token] = _bind
    app.dependency_overrides[get_db] = _db
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.pop(require_token, None)
        app.dependency_overrides.pop(get_db, None)


def _listed_template(
    session, monkeypatch, *, governed, content="SECRET Hello {{name}}", auto_embed=False
):
    """A template inside the (patched) container; its own ACR for RD when ``governed``."""
    repo = DocumentRepository(session)
    container = repo.create(
        title="prompt_templates", content=None, usetype="adjutant:container", auto_embed=False
    )
    session.flush()
    monkeypatch.setattr(template_service, "_container_id", container.id)
    tpl = repo.create(
        title="secret greeting",
        content=content,
        parent_id=container.id,
        usetype=TEMPLATE_USETYPE,
        structured_content={"category": "ideation", "variables": [], "usage_count": 0},
        auto_embed=auto_embed,
    )
    session.flush()
    rd = _principal(session, "RD")
    na = _principal(session, "NA")
    if governed:
        _grant(session, tpl.id, rd, "read")
    return tpl.id, rd, na


def _list_ids(db_session, principal):
    with _http(db_session, principal) as client:
        resp = client.get("/templates")
    assert resp.status_code == 200, resp.text
    return [t["id"] for t in resp.json()]


def test_list_templates_hides_template_from_non_reader(db_session, monkeypatch):
    tid, _, na = _listed_template(db_session, monkeypatch, governed=True)
    assert tid not in _list_ids(db_session, na)
    with _http(db_session, na) as client:
        assert client.get(f"/templates/{tid}").status_code == 404


def test_list_templates_shows_template_to_reader_and_owner(db_session, monkeypatch):
    tid, rd, _ = _listed_template(db_session, monkeypatch, governed=True)
    assert tid in _list_ids(db_session, rd)
    assert tid in _list_ids(db_session, OWNER)


def test_list_templates_with_no_acr_shows_everyone(db_session, monkeypatch):
    tid, _, na = _listed_template(db_session, monkeypatch, governed=False)
    assert tid in _list_ids(db_session, na)


# --- POST /templates/search ----------------------------------------------------------------
#
# From 332ddda. search_templates has no gate of its own and needs none: the hit comes from
# SearchRepository.vector_search, which ANDs readable_filter into the scored statement. This
# pins the VERB's contract so that a change of leaf (one that forgot the filter) fails here,
# rather than a second, post-LIMIT can_read pass in the service that would short the page.

_SECRET_BODY = "the pangolin protocol authorizes zebra maneuvers for {{name}}"


def _search_ids(db_session, principal):
    with _http(db_session, principal) as client:
        resp = client.post(
            "/templates/search", json={"query": "pangolin protocol zebra maneuvers", "limit": 10}
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == len(body["results"])
    return [r["document"]["id"] for r in body["results"]]


def test_search_templates_hides_template_from_non_reader(db_session, monkeypatch):
    tid, rd, na = _listed_template(
        db_session, monkeypatch, governed=True, content=_SECRET_BODY, auto_embed=True
    )
    assert tid not in _search_ids(db_session, na)
    assert tid in _search_ids(db_session, rd)
