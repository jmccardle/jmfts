"""Subtree RBAC — ``TemplateService.get_template`` and ``render_template`` take the read gate.

A template IS a document (usetype ``template``, under a container node). Both verbs once
read it by ``session.get(Document, template_id)`` with no ``can_read`` (ROADMAP known gap
12; ``test_expose_principal_audit``).

    hidden (unreadable) template -> LookupError          (404, existence-hiding)
    reader                       -> allowed
"""

import pytest

from jmfts_client.contracts.template import TemplateRenderRequest
from jmfts_core.repositories.document import DocumentRepository
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
