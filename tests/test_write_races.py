"""Write-path correctness: the reparent bug and the predicate-create race.

Two defects the investigation (2026-07-23) surfaced, fixed here TDD-style:

1. ``reparent()`` rewrote ``path`` but never reset ``position``, so a node moved
   into a new sibling group kept a meaningless position integer and mis-sorted
   among its new siblings. It also had no cycle guard (moving a node under its
   own descendant corrupts every ``path``).

2. Predicate creation was a plain ``add()/flush()`` with no ``ON CONFLICT``, so
   two concurrent transactions minting the same predicate name collided on
   ``predicates_name_key`` — the second raising ``IntegrityError`` and aborting
   its whole batch (fact-extraction). ``get_or_create_predicate`` closes it.
"""

import pytest

from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.triple import TripleRepository

# ---------------------------------------------------------------------------
# reparent(): position reset + cycle guard
# ---------------------------------------------------------------------------


class TestReparentPosition:
    def _ordered_parent_with_two_children(self, repo, root):
        """A parent that is itself ordered (position not NULL) with children at 0,1."""
        parent = repo.create(title="P", content="p", parent_id=root.id, sequential=True)
        repo.create(title="c0", content="c0", parent_id=parent.id, sequential=True)
        repo.create(title="c1", content="c1", parent_id=parent.id, sequential=True)
        return parent

    def test_move_into_ordered_group_appends_position(self, db_session):
        repo = DocumentRepository(db_session)
        root = repo.create(title="root", content="root")
        parent = self._ordered_parent_with_two_children(repo, root)  # children at 0, 1

        # A node that currently carries position 0 in a different ordered group.
        other = repo.create(title="Q", content="q", parent_id=root.id, sequential=True)
        mover = repo.create(title="X", content="x", parent_id=other.id, sequential=True)
        assert mover.position == 0
        db_session.flush()

        moved = repo.reparent(mover.id, parent.id)

        assert moved.parent_id == parent.id
        # MUST be renumbered to the tail of the NEW sibling group (0,1 → 2),
        # not left at its stale 0.
        assert moved.position == 2

    def test_move_into_unordered_group_clears_position(self, db_session):
        repo = DocumentRepository(db_session)
        root = repo.create(title="root", content="root")
        parent = self._ordered_parent_with_two_children(repo, root)
        mover = repo.create(title="X", content="x", parent_id=parent.id, sequential=True)
        assert mover.position == 2
        db_session.flush()

        # A root is unordered (position is NULL); moving under it must clear the
        # stale position rather than carry a meaningless integer.
        new_root = repo.create(title="root2", content="root2")
        moved = repo.reparent(mover.id, new_root.id)

        assert moved.parent_id == new_root.id
        assert moved.position is None

    def test_reparent_under_self_is_rejected(self, db_session):
        repo = DocumentRepository(db_session)
        root = repo.create(title="root", content="root")
        node = repo.create(title="n", content="n", parent_id=root.id)
        with pytest.raises(ValueError, match="cycle|itself|descendant"):
            repo.reparent(node.id, node.id)

    def test_reparent_under_own_descendant_is_rejected(self, db_session):
        repo = DocumentRepository(db_session)
        root = repo.create(title="root", content="root")
        parent = repo.create(title="p", content="p", parent_id=root.id)
        child = repo.create(title="c", content="c", parent_id=parent.id)
        db_session.flush()
        # Moving parent under its own child would create a cycle.
        with pytest.raises(ValueError, match="cycle|descendant"):
            repo.reparent(parent.id, child.id)

    def test_reparent_still_rewrites_descendant_paths(self, db_session):
        """The original correct behavior (path rewrite) must be preserved."""
        repo = DocumentRepository(db_session)
        root_a = repo.create(title="A", content="a")
        root_b = repo.create(title="B", content="b")
        mid = repo.create(title="mid", content="mid", parent_id=root_a.id)
        leaf = repo.create(title="leaf", content="leaf", parent_id=mid.id)
        db_session.flush()

        repo.reparent(mid.id, root_b.id)
        db_session.flush()

        assert repo.get(mid.id).path == [root_b.id]
        assert repo.get(leaf.id).path == [root_b.id, mid.id]


# ---------------------------------------------------------------------------
# get_or_create_predicate(): idempotent, race-safe
# ---------------------------------------------------------------------------


class TestPredicateGetOrCreate:
    def test_idempotent_within_session(self, db_session):
        repo = TripleRepository(db_session)
        pred1, created1 = repo.get_or_create_predicate(name="located_in")
        pred2, created2 = repo.get_or_create_predicate(name="located_in")
        assert created1 is True
        assert created2 is False
        assert pred1.id == pred2.id
        assert pred2.name == "located_in"

    def test_get_or_create_after_plain_create_does_not_raise(self, db_session):
        """The exact batch-abort scenario: a predicate already exists, and a
        second creator must resolve it instead of raising on the unique key."""
        repo = TripleRepository(db_session)
        first = repo.create_predicate(name="capital_of")
        db_session.flush()
        pred, created = repo.get_or_create_predicate(name="capital_of")
        assert created is False
        assert pred.id == first.id

    def test_cross_transaction_mint_does_not_abort(self):
        """Two independent transactions minting the same new predicate: exactly
        one creates it, the other resolves it — neither raises IntegrityError.

        Uses real, separately-committed sessions (not the savepoint fixture) to
        exercise the true cross-transaction ON CONFLICT path. Cleans up after.
        """
        pytest.importorskip("psycopg2")
        from tests.conftest import DB_READY

        if not DB_READY:
            pytest.skip("test database not provisioned")

        from jmfts_core.database import get_session_factory

        SessionLocal = get_session_factory()
        name = "raced_predicate_xyz"
        s1 = SessionLocal()
        s2 = SessionLocal()
        try:
            p1, c1 = TripleRepository(s1).get_or_create_predicate(name=name)
            s1.commit()
            # s2's transaction started independently; it must see the committed
            # row via ON CONFLICT and resolve, not raise.
            p2, c2 = TripleRepository(s2).get_or_create_predicate(name=name)
            s2.commit()

            assert c1 is True and c2 is False
            assert p1.id == p2.id
        finally:
            cleanup = SessionLocal()
            try:
                pred = TripleRepository(cleanup).get_predicate_by_name(name)
                if pred:
                    cleanup.delete(pred)
                    cleanup.commit()
            finally:
                cleanup.close()
            s1.close()
            s2.close()
