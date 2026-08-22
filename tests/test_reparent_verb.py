"""reparent as a first-class verb — ``PATCH /documents/{id}`` with ``parent_id``.

``reparent()`` was the only true subtree-surgery op, buried inside segmentation/RAPTOR.
These tests cover the newly-exposed service surface: passing ``parent_id`` on the update
verb performs the move, composes with field edits, is a no-op when already the current
parent, and surfaces a bad-parent / cycle as a ``ValueError`` (which @expose maps to 400).

The repository mechanics themselves — position reset, descendant path rewrite, cycle guard,
and the two-ended RBAC write gate — are covered by ``test_write_races.py`` and
``test_access_write_gate.py`` and are not re-tested here. See ROADMAP §A "reparent as a
first-class verb".
"""

import pytest

from jmfts_client.contracts.document import DocumentUpdate
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.services.document_service import DocumentService


def _doc(session, title, parent_id=None):
    return DocumentRepository(session).create(
        title=title,
        content=f"content for {title}",
        parent_id=parent_id,
        usetype="raw",
        auto_embed=False,
    )


class TestReparentVerb:
    def test_parent_id_moves_document_under_new_parent(self, db_session):
        svc = DocumentService(db_session)
        a = _doc(db_session, "A")
        b = _doc(db_session, "B")
        child = _doc(db_session, "child", parent_id=a.id)

        moved = svc.update_document(child.id, DocumentUpdate(parent_id=b.id))

        assert moved.parent_id == b.id
        # It now lives under B and no longer under A.
        repo = DocumentRepository(db_session)
        assert child.id in [c.id for c in repo.get_children(b.id)]
        assert child.id not in [c.id for c in repo.get_children(a.id)]

    def test_field_edit_and_move_compose_in_one_call(self, db_session):
        svc = DocumentService(db_session)
        a = _doc(db_session, "A")
        b = _doc(db_session, "B")
        child = _doc(db_session, "child", parent_id=a.id)

        moved = svc.update_document(child.id, DocumentUpdate(title="renamed", parent_id=b.id))

        assert moved.title == "renamed"
        assert moved.parent_id == b.id

    def test_omitting_parent_id_leaves_parent_unchanged(self, db_session):
        svc = DocumentService(db_session)
        a = _doc(db_session, "A")
        child = _doc(db_session, "child", parent_id=a.id)

        moved = svc.update_document(child.id, DocumentUpdate(title="renamed"))

        assert moved.title == "renamed"
        assert moved.parent_id == a.id

    def test_same_parent_is_a_noop(self, db_session):
        """parent_id == current parent skips reparent() entirely (no path churn, no error)."""
        svc = DocumentService(db_session)
        a = _doc(db_session, "A")
        child = _doc(db_session, "child", parent_id=a.id)

        moved = svc.update_document(child.id, DocumentUpdate(parent_id=a.id))

        assert moved.parent_id == a.id

    def test_missing_document_is_404(self, db_session):
        svc = DocumentService(db_session)
        with pytest.raises(LookupError):
            svc.update_document(9_999_999, DocumentUpdate(parent_id=1))

    def test_missing_new_parent_raises_valueerror(self, db_session):
        """A parent_id that does not exist -> ValueError -> @expose 400 (not a 404 on the doc)."""
        svc = DocumentService(db_session)
        child = _doc(db_session, "child")

        with pytest.raises(ValueError):
            svc.update_document(child.id, DocumentUpdate(parent_id=9_999_999))

    def test_cycle_under_own_descendant_raises_valueerror(self, db_session):
        """Moving a node under one of its own descendants is a cycle -> ValueError -> 400."""
        svc = DocumentService(db_session)
        parent = _doc(db_session, "parent")
        child = _doc(db_session, "child", parent_id=parent.id)

        with pytest.raises(ValueError):
            svc.update_document(parent.id, DocumentUpdate(parent_id=child.id))
