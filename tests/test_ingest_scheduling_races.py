"""Concurrency properties of the ingest scheduler. ``INGEST_SPEC.md`` 5.3 and 5.4.

Every test here uses REAL, separately-committed sessions and real threads. The savepoint
``db_session`` fixture cannot stage any of this: it is one connection, so two of its
"transactions" are the same transaction and no lock ever conflicts with anything.
``tests/test_write_races.py::test_cross_transaction_mint_does_not_abort`` is the existing
example of that shape; this file follows it, and adds a teardown that deletes each tree
it committed (``task_queue`` rows cascade with their documents).

The four properties, and the mechanism each one is asserting:

* **exactly one rollup** — the ``FOR UPDATE`` on the node serialises the "is anything
  pending and is every child settled" test against the enqueue that answers it;
* **a late child leaves the parent unsettled** — an ``INSERT`` naming the parent takes
  ``FOR KEY SHARE`` on that row for its foreign key, which conflicts with the walk's
  ``FOR UPDATE`` (an ordinary UPDATE would take ``FOR NO KEY UPDATE`` and would NOT
  conflict — the strength of the lock is the point);
* **a childless-move cannot be raced** — same lock, on the moving node;
* **no deadlock** — the walk holds exactly one document row at a time and commits
  between levels, so it can wait on a downward task but never be waited on by one.
"""

from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import text

from tests.conftest import DB_READY

pytestmark = pytest.mark.skipif(not DB_READY, reason="test database not provisioned")

from jmfts_core.database import get_session, get_session_factory  # noqa: E402
from jmfts_core.models.document import (  # noqa: E402
    Document,
    SETTLED_IN_FLIGHT,
    SETTLED_SETTLED,
)
from jmfts_core.repositories.document import DocumentRepository, PopulatedMoveError  # noqa: E402
from jmfts_core.repositories.task_queue import TaskQueueRepository  # noqa: E402
from jmfts_core.settling import (  # noqa: E402
    NO_ROLLUP,
    AttemptDiffPlanner,
    TaskSpec,
    settle_node,
    settle_walk,
)

#: How long a thread that is supposed to be BLOCKED is watched before concluding it is.
#: Long enough that a slow machine does not produce a false "it didn't block", short
#: enough that the suite does not crawl.
_BLOCK_OBSERVATION_SECONDS = 0.6

#: Join timeout for threads that must finish. Generous: the failure mode being guarded
#: against (a deadlock) shows up as a thread that never returns, and Postgres's own
#: deadlock detector fires within a second anyway.
_JOIN_TIMEOUT = 30


@pytest.fixture
def committed_roots():
    """Ids of committed root documents to delete at teardown, subtrees and all."""
    roots: list[int] = []
    yield roots
    with get_session() as session:
        for root_id in roots:
            session.execute(
                text("DELETE FROM documents WHERE id = :r OR path @> jsonb_build_array(:r)"),
                {"r": root_id},
            )


def _commit_tree(committed_roots, shape: dict[str, str | None]) -> dict[str, int]:
    """Create and COMMIT a tree of in-flight nodes. ``shape`` maps name -> parent name."""
    ids: dict[str, int] = {}
    with get_session() as session:
        docs = DocumentRepository(session)
        for name, parent in shape.items():
            node = docs.create(
                title=name,
                content=None,
                parent_id=ids[parent] if parent else None,
                auto_embed=False,
                settled=SETTLED_IN_FLIGHT,
            )
            session.flush()
            ids[name] = node.id
            if parent is None:
                committed_roots.append(node.id)
    return ids


def _fetch(document_id: int) -> Document:
    with get_session() as session:
        node = session.get(Document, document_id)
        session.expunge(node)
        return node


def _child_titled(parent_id: int, title: str) -> Document:
    with get_session() as session:
        node = (
            session.query(Document)
            .filter(Document.parent_id == parent_id, Document.title == title)
            .one()
        )
        session.expunge(node)
        return node


def _count_tasks(scope_id: int, task_type: str) -> int:
    with get_session() as session:
        return session.execute(
            text(
                "SELECT count(*) FROM task_queue " "WHERE scope_document_id = :s AND task_type = :t"
            ),
            {"s": scope_id, "t": task_type},
        ).scalar_one()


class _OnlyFor:
    def __init__(self, node_id, planner):
        self.node_id = node_id
        self.planner = planner

    def __call__(self, session, node):
        return self.planner(session, node) if node.id == self.node_id else ()


def _run_threads(targets, *, timeout=_JOIN_TIMEOUT):
    """Start every target at the same instant on a barrier; return anything they raised."""
    barrier = threading.Barrier(len(targets))
    errors: list[BaseException] = []

    def wrap(fn):
        def runner():
            try:
                barrier.wait(timeout=timeout)
                fn()
            except BaseException as exc:  # noqa: BLE001 — reported, not swallowed
                errors.append(exc)

        return runner

    threads = [threading.Thread(target=wrap(fn)) for fn in targets]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=timeout)
    assert not any(t.is_alive() for t in threads), "a thread never finished — deadlock?"
    return errors


# ---------------------------------------------------------------------------
# 5.4 — exactly one rollup
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("trial", range(25))
def test_two_siblings_finishing_together_enqueue_exactly_one_rollup(trial, committed_roots):
    """Never zero and never two.

    This is the failure the row lock exists for: a count-and-check without it produces a
    duplicate rollup or none, *intermittently*, which is why this runs many times rather
    than once. Twenty-five trials is not a proof — nothing at this layer is — but a
    missing lock loses this within a couple of runs on a machine with more than one core.
    """
    ids = _commit_tree(
        committed_roots, {"root": None, "parent": "root", "a": "parent", "b": "parent"}
    )
    planner = _OnlyFor(ids["parent"], AttemptDiffPlanner([TaskSpec("summarize", "self")]))

    errors = _run_threads(
        [
            lambda: settle_walk(ids["a"], planner),
            lambda: settle_walk(ids["b"], planner),
        ]
    )
    assert not errors, errors

    assert _count_tasks(ids["parent"], "summarize") == 1
    # And the parent is unsettled BECAUSE of that task, not despite it.
    assert _fetch(ids["parent"]).settled == SETTLED_IN_FLIGHT
    assert _fetch(ids["a"]).settled == SETTLED_SETTLED
    assert _fetch(ids["b"]).settled == SETTLED_SETTLED


def test_concurrent_walkers_never_both_see_themselves_as_last(committed_roots):
    """Same guarantee with more siblings and more walkers at once."""
    shape = {"root": None, "parent": "root"}
    shape.update({f"c{i}": "parent" for i in range(6)})
    ids = _commit_tree(committed_roots, shape)
    planner = _OnlyFor(ids["parent"], AttemptDiffPlanner([TaskSpec("summarize", "self")]))

    errors = _run_threads(
        [(lambda node=ids[f"c{i}"]: settle_walk(node, planner)) for i in range(6)]
    )
    assert not errors, errors
    assert _count_tasks(ids["parent"], "summarize") == 1


# ---------------------------------------------------------------------------
# 5.4 — a child added during the check
# ---------------------------------------------------------------------------


def test_a_child_added_while_the_parent_is_checked_cannot_interleave_with_the_check(
    committed_roots,
):
    """The lock forces one ordering or the other rather than letting them interleave.

    Here the walk gets the lock first, so the insert BLOCKS — that is the assertion in
    the middle. The parent therefore settles against the child set it actually read, and
    stays settled: adding a child is not work for the parent, so nothing un-settles it
    afterwards. The opposite ordering is the one that matters for correctness, and
    `settle_node` covers it — a walk that arrives after the insert counts the in-flight
    child and refuses to settle.
    """
    ids = _commit_tree(committed_roots, {"root": None, "parent": "root", "settled": "parent"})
    with get_session() as session:
        session.get(Document, ids["settled"]).settled = SETTLED_SETTLED

    session_factory = get_session_factory()
    walker = session_factory()
    inserted = threading.Event()
    started = threading.Event()

    def add_late_child():
        started.set()
        with get_session() as session:
            DocumentRepository(session).create(
                title="late",
                content=None,
                parent_id=ids["parent"],
                auto_embed=False,
                settled=SETTLED_IN_FLIGHT,
            )
        inserted.set()

    try:
        step = settle_node(walker, ids["parent"], NO_ROLLUP)
        assert step.settled is True, "with one settled child the parent settles"

        thread = threading.Thread(target=add_late_child)
        thread.start()
        started.wait(timeout=5)
        time.sleep(_BLOCK_OBSERVATION_SECONDS)
        assert not inserted.is_set(), (
            "the INSERT must block on the walk's FOR UPDATE — its foreign key takes "
            "FOR KEY SHARE on the parent row, which conflicts"
        )

        walker.commit()
    finally:
        walker.close()

    thread.join(timeout=_JOIN_TIMEOUT)
    assert inserted.is_set()
    assert _fetch(ids["parent"]).settled == SETTLED_SETTLED
    assert _child_titled(ids["parent"], "late").settled == SETTLED_IN_FLIGHT


# ---------------------------------------------------------------------------
# 5.4 — re-parenting between finishing and checking
# ---------------------------------------------------------------------------


def test_a_node_reparented_before_the_check_signals_its_new_parent(committed_roots):
    """ "Reading ``parent_id`` fresh" is not free: the walker's session may already hold
    a copy of the row saying the OLD parent, and SQLAlchemy hands back identity-mapped
    objects without re-reading them. The walk's SELECT carries ``populate_existing`` for
    exactly this."""
    ids = _commit_tree(
        committed_roots,
        {"root": None, "p1": "root", "p2": "root", "mover": "p1", "stuck": "p1"},
    )

    session_factory = get_session_factory()
    walker = session_factory()
    try:
        stale = walker.get(Document, ids["mover"])
        assert stale.parent_id == ids["p1"]

        with get_session() as other:
            DocumentRepository(other).reparent(ids["mover"], ids["p2"])

        step = settle_node(walker, ids["mover"], NO_ROLLUP)
        assert step.settled is True
        assert step.parent_id == ids["p2"], "the walk must signal the NEW parent"
        walker.commit()
    finally:
        walker.close()

    settle_walk(ids["mover"], NO_ROLLUP)

    assert _fetch(ids["p2"]).settled == SETTLED_SETTLED
    # p1 still has an in-flight child of its own and must not have settled.
    assert _fetch(ids["p1"]).settled == SETTLED_IN_FLIGHT


# ---------------------------------------------------------------------------
# 5.3 — the childless-move guard
# ---------------------------------------------------------------------------


class TestChildlessMoveGuard:
    def test_a_children_mode_move_of_a_populated_node_is_refused(self, db_session):
        docs = DocumentRepository(db_session)
        root = docs.create(title="root", content=None, auto_embed=False)
        target = docs.create(title="target", content=None, auto_embed=False)
        db_session.flush()
        mover = docs.create(title="mover", content=None, parent_id=root.id, auto_embed=False)
        db_session.flush()
        docs.create(title="kid", content=None, parent_id=mover.id, auto_embed=False)
        db_session.flush()

        with pytest.raises(PopulatedMoveError) as excinfo:
            docs.reparent(mover.id, target.id, childless_only=True)

        assert excinfo.value.child_count == 1
        # Refused, not partially applied.
        assert db_session.get(Document, mover.id).parent_id == root.id

    def test_a_childless_move_is_allowed(self, db_session):
        docs = DocumentRepository(db_session)
        root = docs.create(title="root", content=None, auto_embed=False)
        target = docs.create(title="target", content=None, auto_embed=False)
        db_session.flush()
        mover = docs.create(title="mover", content=None, parent_id=root.id, auto_embed=False)
        db_session.flush()

        moved = docs.reparent(mover.id, target.id, childless_only=True)

        assert moved.parent_id == target.id
        assert moved.path == [target.id]

    def test_an_ordinary_move_is_unaffected(self, db_session):
        """Off by default: the administrative move and every existing caller are
        unchanged; only a task that declared write_mode='children' passes True."""
        docs = DocumentRepository(db_session)
        root = docs.create(title="root", content=None, auto_embed=False)
        target = docs.create(title="target", content=None, auto_embed=False)
        db_session.flush()
        mover = docs.create(title="mover", content=None, parent_id=root.id, auto_embed=False)
        db_session.flush()
        docs.create(title="kid", content=None, parent_id=mover.id, auto_embed=False)
        db_session.flush()

        assert docs.reparent(mover.id, target.id).parent_id == target.id

    def test_a_child_committed_before_the_check_refuses_the_move(self, committed_roots):
        ids = _commit_tree(committed_roots, {"root": None, "target": "root", "mover": "root"})
        with get_session() as session:
            DocumentRepository(session).create(
                title="kid", content=None, parent_id=ids["mover"], auto_embed=False
            )

        with pytest.raises(PopulatedMoveError):
            with get_session() as session:
                DocumentRepository(session).reparent(
                    ids["mover"], ids["target"], childless_only=True
                )

    def test_a_child_arriving_mid_move_cannot_slip_through(self, committed_roots):
        """The check and the move are in ONE transaction, and the row lock is what makes
        that true rather than merely intended. A concurrent INSERT either commits first
        (and the move is refused, above) or blocks until the move commits — so there is
        no interleaving in which a populated node gets moved.

        WHAT THIS DOES NOT ASSERT, deliberately. The blocked INSERT computes its `path`
        from a read of the parent taken BEFORE it blocked, so the late child lands with a
        path naming the mover's old location. That is a pre-existing race between
        ``create`` and ``reparent`` — it happens today with ``childless_only=False`` and
        with no queue involved — and closing it means taking the parent's row lock inside
        ``create`` before computing the path. Out of scope here, recorded rather than
        papered over; what spec 5.3 asks for is that the MOVE is refused or clean, and
        that is what is checked below."""
        ids = _commit_tree(committed_roots, {"root": None, "target": "root", "mover": "root"})

        session_factory = get_session_factory()
        mover_session = session_factory()
        inserted = threading.Event()
        started = threading.Event()

        def add_child():
            started.set()
            with get_session() as session:
                DocumentRepository(session).create(
                    title="kid", content=None, parent_id=ids["mover"], auto_embed=False
                )
            inserted.set()

        try:
            DocumentRepository(mover_session).reparent(
                ids["mover"], ids["target"], childless_only=True
            )
            mover_session.flush()

            thread = threading.Thread(target=add_child)
            thread.start()
            started.wait(timeout=5)
            time.sleep(_BLOCK_OBSERVATION_SECONDS)
            assert not inserted.is_set(), (
                "the INSERT must block on the childless check's FOR UPDATE; otherwise "
                "a node can gain children between the check and the write"
            )
            mover_session.commit()
        finally:
            mover_session.close()

        thread.join(timeout=_JOIN_TIMEOUT)
        assert inserted.is_set()

        with get_session() as session:
            moved = session.get(Document, ids["mover"])
            # The move applied exactly once, to a node that really had no children at the
            # moment it was checked, and the mover's own path is the new chain.
            assert moved.parent_id == ids["target"]
            assert moved.path == [ids["root"], ids["target"]]
            kid_count = session.execute(
                text("SELECT count(*) FROM documents WHERE parent_id = :p"),
                {"p": ids["mover"]},
            ).scalar_one()
            assert kid_count == 1


# ---------------------------------------------------------------------------
# 5.4 — one row lock at a time
# ---------------------------------------------------------------------------


def test_the_walk_does_not_deadlock_against_a_downward_task_holding_the_grandparent(
    committed_roots,
):
    """The deadlock the spec's "commit between levels" prevents.

    A downward task takes the grandparent and then wants the parent. A walk that held
    the parent while reaching for the grandparent would close the cycle and one of the
    two would be shot by Postgres's deadlock detector. This walk holds one row at a
    time, so it can wait on the downward task but the downward task never waits on it.
    """
    ids = _commit_tree(committed_roots, {"g": None, "p": "g", "c": "p"})

    holder_has_grandparent = threading.Event()

    def downward_task():
        """Holds G (as a children-mode task arranging G's children would), then wants P."""
        with get_session() as session:
            session.execute(
                text("SELECT id FROM documents WHERE id = :i FOR UPDATE"), {"i": ids["g"]}
            )
            holder_has_grandparent.set()
            time.sleep(_BLOCK_OBSERVATION_SECONDS)
            session.execute(
                text("SELECT id FROM documents WHERE id = :i FOR UPDATE"), {"i": ids["p"]}
            )

    def walker():
        holder_has_grandparent.wait(timeout=5)
        settle_walk(ids["c"], NO_ROLLUP)

    errors = _run_threads([downward_task, walker])

    assert not errors, errors
    assert _fetch(ids["c"]).settled == SETTLED_SETTLED
    assert _fetch(ids["p"]).settled == SETTLED_SETTLED
    assert _fetch(ids["g"]).settled == SETTLED_SETTLED


# ---------------------------------------------------------------------------
# 5.3 — the claim's conflict predicate under real concurrency
# ---------------------------------------------------------------------------


def test_concurrent_claimers_never_both_take_conflicting_tasks(committed_roots):
    """``FOR UPDATE SKIP LOCKED`` alone does not give this.

    It guarantees two workers never take the same ROW. Two workers evaluating the
    conflict predicate at the same instant, before either has written its own 'claimed'
    status, both see a clear field — so the claim itself is serialised with a
    transaction-scoped advisory lock. Without it this test fails outright.
    """
    ids = _commit_tree(committed_roots, {"root": None, "node": "root"})
    with get_session() as session:
        tasks = TaskQueueRepository(session)
        tasks.enqueue("a", ids["node"], "self")
        tasks.enqueue("b", ids["node"], "self")

    claimed: list[int] = []
    lock = threading.Lock()

    def claim(worker):
        def run():
            with get_session() as session:
                task = TaskQueueRepository(session).claim_next(worker)
                if task is not None:
                    with lock:
                        claimed.append(task.id)

        return run

    errors = _run_threads([claim("w1"), claim("w2")])

    assert not errors, errors
    assert (
        len(claimed) == 1
    ), f"two `self` tasks on one node must not both be claimed; got {claimed}"
