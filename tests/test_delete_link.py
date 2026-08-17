"""delete_link — the retract leg for the append-only DocumentLink graph.

``create_link``/``get_links`` were exposed but there was no delete; the link graph was
append-only. These tests cover the repository delete (incident scoping, link_type echo,
missing/unrelated → None) and the service 404 mapping. Deletion is deliberately
unconditional across link types (RAPTOR ``bridge`` edges included) — the caller owns policy.
See ROADMAP §A "Link-edge lifecycle".
"""

import pytest

from jmfts_core.repositories.document import DocumentRepository


def _doc(session, title):
    return DocumentRepository(session).create(
        title=title, content=f"content for {title}", usetype="raw", auto_embed=False
    )


class TestDeleteLinkRepo:
    def test_delete_returns_link_type_and_removes_edge(self, db_session):
        repo = DocumentRepository(db_session)
        a, b = _doc(db_session, "a"), _doc(db_session, "b")
        link = repo.create_link(source_id=a.id, target_id=b.id, link_type="ref")

        deleted_type = repo.delete_link(link.id, incident_to=a.id)

        assert deleted_type == "ref"
        assert repo.get_links(a.id, direction="outgoing") == []

    def test_delete_missing_link_returns_none(self, db_session):
        repo = DocumentRepository(db_session)
        assert repo.delete_link(9_999_999, incident_to=None) is None

    def test_delete_not_incident_returns_none_and_keeps_edge(self, db_session):
        """A link_id that exists but doesn't touch ``incident_to`` is untouchable via
        that document — the DELETE .../{doc}/links/{id} route can't reach a stranger."""
        repo = DocumentRepository(db_session)
        a, b, c = _doc(db_session, "a"), _doc(db_session, "b"), _doc(db_session, "c")
        link = repo.create_link(source_id=a.id, target_id=b.id, link_type="ref")

        assert repo.delete_link(link.id, incident_to=c.id) is None
        # Edge survived the refused delete.
        assert [_l.id for _l in repo.get_links(a.id, direction="outgoing")] == [link.id]

    def test_delete_targets_incoming_side_too(self, db_session):
        """Incident means source OR target — the URL doc may be the link's target."""
        repo = DocumentRepository(db_session)
        a, b = _doc(db_session, "a"), _doc(db_session, "b")
        link = repo.create_link(source_id=a.id, target_id=b.id, link_type="ref")

        assert repo.delete_link(link.id, incident_to=b.id) == "ref"

    def test_delete_is_unconditional_across_link_types(self, db_session):
        """RAPTOR 'bridge' edges are deletable — no protected type."""
        repo = DocumentRepository(db_session)
        a, b = _doc(db_session, "a"), _doc(db_session, "b")
        link = repo.create_link(source_id=a.id, target_id=b.id, link_type="bridge")

        assert repo.delete_link(link.id, incident_to=a.id) == "bridge"


class TestDeleteLinkService:
    def test_service_404_when_not_incident(self, db_session):
        from jmfts_core.services.document_service import DocumentService

        repo = DocumentRepository(db_session)
        a, b, c = _doc(db_session, "a"), _doc(db_session, "b"), _doc(db_session, "c")
        link = repo.create_link(source_id=a.id, target_id=b.id, link_type="ref")

        svc = DocumentService(db_session)
        with pytest.raises(LookupError, match="not found"):
            svc.delete_link(document_id=c.id, link_id=link.id)

    def test_service_deletes_and_echoes_link_type(self, db_session):
        from jmfts_core.services.document_service import DocumentService

        repo = DocumentRepository(db_session)
        a, b = _doc(db_session, "a"), _doc(db_session, "b")
        link = repo.create_link(source_id=a.id, target_id=b.id, link_type="ref")

        svc = DocumentService(db_session)
        out = svc.delete_link(document_id=a.id, link_id=link.id)

        assert out == {"deleted": True, "id": link.id, "link_type": "ref"}
        assert repo.get_links(a.id, direction="outgoing") == []
