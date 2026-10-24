"""#49 — `GET /documents/{id}/subtree?usetype=X`: every descendant of a type, at any depth.

`children?depth=-1&usetype=X` answers the same question but is capped by `limit`; the
subtree verb has no cap, so the filter is applied in the query and not after a page.

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
        pytest.skip("Postgres not reachable; subtree tests require a live database")
    trans = conn.begin()
    SessionLocal = get_session_factory()
    session = SessionLocal(bind=conn)
    conn.begin_nested()

    yield session

    session.close()
    trans.rollback()
    conn.close()


def _doc(repo, title, parent_id=None, usetype=None):
    return repo.create(
        title=title,
        content=f"{title} body",
        parent_id=parent_id,
        usetype=usetype,
        auto_embed=False,
    )


@pytest.fixture
def tree(db_session):
    """root -> section -> 105 `turn` leaves, one `note` deeper still, one `note` shallow."""
    from jmfts_core.repositories.document import DocumentRepository

    repo = DocumentRepository(db_session)
    root = _doc(repo, "root", usetype="conversation")
    section = _doc(repo, "section", root.id, usetype="section")
    turns = [_doc(repo, f"turn {i}", section.id, usetype="turn") for i in range(105)]
    deep_note = _doc(repo, "deep note", turns[0].id, usetype="note")
    shallow_note = _doc(repo, "shallow note", root.id, usetype="note")
    return root, section, turns, deep_note, shallow_note


def test_filter_returns_only_matching_descendants_past_the_children_cap(db_session, tree):
    from jmfts_core.services.document_service import DocumentService

    root, _, turns, _, _ = tree
    result = DocumentService(db_session).get_subtree(root.id, usetype="turn")

    assert result.root.id == root.id
    assert {d.id for d in result.descendants} == {t.id for t in turns}
    assert len(result.descendants) == 105  # children() would stop at limit=100
    # total is the root plus what survived the filter, not the unfiltered subtree size
    assert result.total == 106


def test_filter_matches_at_any_depth(db_session, tree):
    from jmfts_core.services.document_service import DocumentService

    root, _, _, deep_note, shallow_note = tree
    result = DocumentService(db_session).get_subtree(root.id, usetype="note")

    assert {d.id for d in result.descendants} == {deep_note.id, shallow_note.id}
    assert result.total == 3


def test_root_is_returned_even_when_it_does_not_match(db_session, tree):
    from jmfts_core.services.document_service import DocumentService

    root = tree[0]
    result = DocumentService(db_session).get_subtree(root.id, usetype="section")

    assert result.root.usetype == "conversation"
    assert [d.usetype for d in result.descendants] == ["section"]


def test_unknown_usetype_is_an_empty_list_not_an_error(db_session, tree):
    from jmfts_core.services.document_service import DocumentService

    root = tree[0]
    result = DocumentService(db_session).get_subtree(root.id, usetype="no-such-usetype")

    assert result.root.id == root.id
    assert result.descendants == []
    assert result.total == 1


def test_no_filter_is_unchanged(db_session, tree):
    from jmfts_core.services.document_service import DocumentService

    root = tree[0]
    result = DocumentService(db_session).get_subtree(root.id)

    assert result.total == 1 + 1 + 105 + 1 + 1
