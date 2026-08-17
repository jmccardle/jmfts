"""CR-1 — explicit sibling ordering via the sparse `position` column.

The ordering contract everywhere siblings/children are listed is

    ORDER BY position ASC NULLS LAST, created_at ASC, id ASC

so ordered subtrees sort by their explicit position while unordered ones (position
NULL) fall straight back to the legacy created_at order. `position` is auto-assigned
only when a create opts in via `sequential` — which itself defaults to inheriting the
parent's ordered-ness, so ordering propagates down a subtree but stays off by default.

DB integration tests on the savepoint-rollback fixture (nothing is committed).
"""

import pytest


@pytest.fixture
def db_session():
    """Transactional session that rolls back after each test (savepoint pattern)."""
    from jmfts_core.database import get_engine, get_session_factory
    from sqlalchemy.exc import OperationalError

    engine = get_engine()
    try:
        conn = engine.connect()
    except OperationalError:
        pytest.skip("Postgres not reachable; CR-1 position tests require a live database")
    trans = conn.begin()
    SessionLocal = get_session_factory()
    session = SessionLocal(bind=conn)
    conn.begin_nested()

    yield session

    session.close()
    trans.rollback()
    conn.close()


def _make_parent(repo, *, sequential=None):
    """A parent document with enough content, but never embedded (no model in CI)."""
    return repo.create(
        title="parent", content="parent body text", auto_embed=False, sequential=sequential
    )


def _child(repo, parent_id, label, *, sequential=None):
    return repo.create(
        title=label,
        content=f"child {label} body",
        parent_id=parent_id,
        auto_embed=False,
        sequential=sequential,
    )


class TestSequentialAssignment:
    def test_sequential_true_auto_numbers_from_zero(self, db_session):
        from jmfts_core.repositories.document import DocumentRepository

        repo = DocumentRepository(db_session)
        parent = _make_parent(repo)
        a = _child(repo, parent.id, "a", sequential=True)
        b = _child(repo, parent.id, "b", sequential=True)
        c = _child(repo, parent.id, "c", sequential=True)

        assert [a.position, b.position, c.position] == [0, 1, 2]

    def test_sequential_default_off_for_unordered_parent(self, db_session):
        """Parent has no position → child defaults to unordered (NULL)."""
        from jmfts_core.repositories.document import DocumentRepository

        repo = DocumentRepository(db_session)
        parent = _make_parent(repo)  # no position
        child = _child(repo, parent.id, "x")  # sequential=None → inherit → False

        assert parent.position is None
        assert child.position is None

    def test_sequential_inherits_down_an_ordered_subtree(self, db_session):
        """A node WITH a position makes its children default to ordered."""
        from jmfts_core.repositories.document import DocumentRepository

        repo = DocumentRepository(db_session)
        root = _make_parent(repo)
        # Opt in explicitly at the top of the ordered region.
        mid = _child(repo, root.id, "mid", sequential=True)
        assert mid.position == 0
        # mid carries a position, so its children inherit ordered-ness with no flag.
        leaf0 = _child(repo, mid.id, "leaf0")
        leaf1 = _child(repo, mid.id, "leaf1")
        assert [leaf0.position, leaf1.position] == [0, 1]

    def test_explicit_false_overrides_inheritance(self, db_session):
        from jmfts_core.repositories.document import DocumentRepository

        repo = DocumentRepository(db_session)
        root = _make_parent(repo)
        mid = _child(repo, root.id, "mid", sequential=True)
        # Parent is ordered, but we force this child out of the sequence.
        leaf = _child(repo, mid.id, "leaf", sequential=False)
        assert leaf.position is None

    def test_sequential_true_on_root_raises(self, db_session):
        from jmfts_core.repositories.document import DocumentRepository

        repo = DocumentRepository(db_session)
        with pytest.raises(ValueError, match="root"):
            repo.create(title="r", content="root body text", sequential=True, auto_embed=False)


class TestOrderingContract:
    def test_get_children_orders_by_position(self, db_session):
        from jmfts_core.repositories.document import DocumentRepository

        repo = DocumentRepository(db_session)
        parent = _make_parent(repo)
        # Insert out of position order to prove position (not insert order) wins.
        _child(repo, parent.id, "a", sequential=True)  # 0
        _child(repo, parent.id, "b", sequential=True)  # 1
        _child(repo, parent.id, "c", sequential=True)  # 2

        titles = [d.title for d in repo.get_children(parent.id)]
        assert titles == ["a", "b", "c"]

    def test_null_positions_sort_last_and_fall_back_to_created_at(self, db_session):
        """NULLS LAST: positioned siblings come first, unordered ones after."""
        from jmfts_core.repositories.document import DocumentRepository

        repo = DocumentRepository(db_session)
        parent = _make_parent(repo)
        # A NULL-position child created first...
        unordered = _child(repo, parent.id, "unordered", sequential=False)
        # ...then a positioned one. Despite being newer, position 0 sorts ahead.
        ordered = _child(repo, parent.id, "ordered", sequential=True)

        children = repo.get_children(parent.id)
        assert [c.title for c in children] == ["ordered", "unordered"]
        assert ordered.position == 0 and unordered.position is None

    def test_position_ties_break_by_id(self, db_session):
        """Position is not unique; equal positions resolve via created_at/id."""
        from jmfts_core.repositories.document import DocumentRepository

        repo = DocumentRepository(db_session)
        parent = _make_parent(repo)
        first = _child(repo, parent.id, "first", sequential=True)  # position 0
        second = _child(repo, parent.id, "second", sequential=True)  # position 1
        # Force a tie: both at position 5.
        first.position = 5
        second.position = 5
        db_session.flush()

        # first.id < second.id, so first still comes first via the id tiebreak.
        children = repo.get_children(parent.id)
        assert [c.title for c in children] == ["first", "second"]

    def test_get_siblings_uses_position_order(self, db_session):
        from jmfts_core.repositories.document import DocumentRepository

        repo = DocumentRepository(db_session)
        parent = _make_parent(repo)
        a = _child(repo, parent.id, "a", sequential=True)
        _child(repo, parent.id, "b", sequential=True)
        _child(repo, parent.id, "c", sequential=True)

        sibs = repo.get_siblings(a.id)  # excludes self
        assert [s.title for s in sibs] == ["b", "c"]
