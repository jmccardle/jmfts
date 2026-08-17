"""The settling walk, single-threaded. ``INGEST_SPEC.md`` Part 5.4.

Shape of the walk, its stopping conditions, and — the property the whole late-bound
design turns on — that it TERMINATES. The concurrency properties (exactly-once rollup,
late child, re-parent, deadlock) need real committed transactions and live in
``tests/test_ingest_scheduling_races.py``.

These tests drive the walk through the savepoint ``db_session`` by handing it a factory
that yields that one session. That deliberately breaks the "new transaction per level"
rule, which is fine here because nothing else is running: the levels are still walked one
at a time, in order, and the decisions are identical. It is not fine for the race tests,
which is why they build their own sessions.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from jmfts_core.models.document import (
    SETTLED_FAILED,
    SETTLED_IN_FLIGHT,
    SETTLED_SETTLED,
    USETYPE_FILE,
)
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.task_queue import TaskQueueRepository
from jmfts_core.services.document_service import DocumentService
from jmfts_core.settling import (
    NO_ROLLUP,
    AttemptDiffPlanner,
    SettleWalkError,
    TaskSpec,
    settle_node,
    settle_walk,
)
from jmfts_core.task_errors import ErrorType


@pytest.fixture
def factory(db_session):
    """A session factory that hands back the test's savepoint session (see module doc)."""

    @contextmanager
    def _factory():
        yield db_session
        db_session.commit()  # releases and restarts the savepoint

    return _factory


@pytest.fixture
def tree(db_session):
    """root -> parent -> child, all in flight, no tasks anywhere."""
    docs = DocumentRepository(db_session)
    root = docs.create(title="root", content=None, auto_embed=False, settled=SETTLED_IN_FLIGHT)
    db_session.flush()
    parent = docs.create(
        title="parent",
        content=None,
        parent_id=root.id,
        auto_embed=False,
        settled=SETTLED_IN_FLIGHT,
    )
    db_session.flush()
    child = docs.create(
        title="child",
        content=None,
        parent_id=parent.id,
        auto_embed=False,
        settled=SETTLED_IN_FLIGHT,
    )
    db_session.flush()
    return root, parent, child


class OnlyFor:
    """Apply a planner to one node and nothing else.

    A rollup policy in the real system is per-node; the tests need to say "summarise the
    parent" without also summarising every leaf, which would be a correct but much
    noisier tree to reason about.
    """

    def __init__(self, node_id, planner):
        self.node_id = node_id
        self.planner = planner

    def __call__(self, session, node):
        return self.planner(session, node) if node.id == self.node_id else ()


# ---------------------------------------------------------------------------
# The walk itself
# ---------------------------------------------------------------------------


class TestWalk:
    def test_a_finished_leaf_settles_itself_and_every_ancestor(self, tree, factory, db_session):
        """The spec's numbered steps start at the parent; if the walk literally did that,
        a leaf whose only task just finished would be evaluated by nobody — no child of
        it will ever walk up through it."""
        root, parent, child = tree
        tasks = TaskQueueRepository(db_session)
        tasks.enqueue("chunk", child.id, "children")
        claimed = tasks.claim_next("w")
        tasks.mark_running(claimed)
        tasks.complete(claimed, detail={})

        steps = settle_walk(child.id, NO_ROLLUP, session_factory=factory)

        assert [s.node_id for s in steps] == [child.id, parent.id, root.id]
        assert all(s.settled for s in steps)
        assert steps[-1].parent_id is None
        for node in (child, parent, root):
            assert db_session.get(type(node), node.id).settled == SETTLED_SETTLED

    def test_the_walk_stops_at_a_node_with_an_unfinished_task(self, tree, factory, db_session):
        root, parent, child = tree
        tasks = TaskQueueRepository(db_session)
        tasks.enqueue("late", parent.id, "children")

        steps = settle_walk(child.id, NO_ROLLUP, session_factory=factory)

        assert [(s.node_id, s.settled, s.blocked_by) for s in steps] == [
            (child.id, True, None),
            (parent.id, False, "tasks"),
        ]
        assert db_session.get(type(root), root.id).settled == SETTLED_IN_FLIGHT

    def test_an_unsettled_sibling_blocks_the_parent(self, tree, factory, db_session):
        root, parent, child = tree
        docs = DocumentRepository(db_session)
        docs.create(
            title="sibling",
            content=None,
            parent_id=parent.id,
            auto_embed=False,
            settled=SETTLED_IN_FLIGHT,
        )
        db_session.flush()

        steps = settle_walk(child.id, NO_ROLLUP, session_factory=factory)

        assert steps[-1].node_id == parent.id
        assert steps[-1].blocked_by == "children"

    def test_a_permanently_failed_node_is_terminal(self, tree, factory, db_session):
        """Spec 2.1: settling it would publish a node whose ingestion is known to have
        died, and let every ancestor settle on top of a hole."""
        root, parent, child = tree
        tasks = TaskQueueRepository(db_session)
        tasks.enqueue("probe", child.id, "self")
        claimed = tasks.claim_next("w")
        tasks.mark_running(claimed)
        tasks.fail(claimed, error="corrupt", error_type=ErrorType.PERMANENT)
        assert db_session.get(type(child), child.id).settled == SETTLED_FAILED

        steps = settle_walk(child.id, NO_ROLLUP, session_factory=factory)

        assert [(s.node_id, s.settled, s.blocked_by) for s in steps] == [
            (child.id, False, "failed")
        ]

    def test_the_walk_refuses_a_node_that_vanished(self, db_session):
        with pytest.raises(SettleWalkError, match="does not exist"):
            settle_node(db_session, 10**9, NO_ROLLUP)


# ---------------------------------------------------------------------------
# Rollup: enqueue at the boundary, and terminate
# ---------------------------------------------------------------------------


class TestRollup:
    def test_the_rollup_is_enqueued_when_the_last_child_settles(self, tree, factory, db_session):
        root, parent, child = tree
        planner = OnlyFor(parent.id, AttemptDiffPlanner([TaskSpec("summarize", "self")]))

        steps = settle_walk(child.id, planner, session_factory=factory)

        assert steps[-1].node_id == parent.id
        assert steps[-1].settled is False
        assert steps[-1].blocked_by == "enqueued"
        assert len(steps[-1].enqueued_task_ids) == 1
        # It is not settled precisely BECAUSE it now has a pending task.
        assert TaskQueueRepository(db_session).structuring_complete(parent.id) is False
        assert db_session.get(type(root), root.id).settled == SETTLED_IN_FLIGHT

    def test_the_rollup_runs_once_and_then_the_walk_terminates(self, tree, factory, db_session):
        """Without the (task, fingerprint) diff this loop is infinite: every walk over a
        finished node would re-enqueue the same rollup."""
        root, parent, child = tree
        planner = OnlyFor(parent.id, AttemptDiffPlanner([TaskSpec("summarize", "self")]))
        tasks = TaskQueueRepository(db_session)

        settle_walk(child.id, planner, session_factory=factory)
        summarize = tasks.claim_next("w")
        assert summarize.task_type == "summarize"
        tasks.mark_running(summarize)
        tasks.complete(summarize, detail={"summary_chars": 120})

        steps = settle_walk(parent.id, planner, session_factory=factory)

        assert [(s.node_id, s.settled) for s in steps] == [(parent.id, True), (root.id, True)]
        # And a third walk enqueues nothing at all.
        again = settle_walk(parent.id, planner, session_factory=factory)
        assert all(s.enqueued_task_ids == () for s in again)

    def test_a_summary_node_created_as_a_child_unsettles_and_re_settles(
        self, tree, factory, db_session
    ):
        """Spec 5.4's closing case, made to happen: the rollup creates a summary node as
        a child of what it summarised. That node is a leaf, it settles on its own walk,
        and the parent re-settles on the next one. The loop terminates because the
        summary node produces no further work."""
        root, parent, child = tree
        planner = OnlyFor(parent.id, AttemptDiffPlanner([TaskSpec("summarize", "self")]))
        docs = DocumentRepository(db_session)
        tasks = TaskQueueRepository(db_session)

        settle_walk(child.id, planner, session_factory=factory)
        summarize = tasks.claim_next("w")
        tasks.mark_running(summarize)
        # The task's actual work: a summary node, in flight, with its own embed task.
        summary = docs.create(
            title="summary",
            content=None,
            parent_id=parent.id,
            auto_embed=False,
            settled=SETTLED_IN_FLIGHT,
        )
        db_session.flush()
        embed = tasks.enqueue("embed", summary.id, "self")
        tasks.complete(summarize, detail={}, produced={"node_count": 1, "child_ids": [summary.id]})

        # The parent is un-settled by its new in-flight child.
        blocked = settle_walk(parent.id, planner, session_factory=factory)
        assert blocked[-1].node_id == parent.id and blocked[-1].blocked_by == "children"

        claimed = tasks.claim_next("w")
        assert claimed.id == embed.id
        tasks.mark_running(claimed)
        tasks.complete(claimed, detail={})

        steps = settle_walk(summary.id, planner, session_factory=factory)

        assert [(s.node_id, s.settled) for s in steps] == [
            (summary.id, True),
            (parent.id, True),
            (root.id, True),
        ]

    def test_a_batch_resolves_within_node_ordering_to_real_dependencies(
        self, tree, factory, db_session
    ):
        """Spec 5.5: the `dependencies` array still earns its place for ordering tasks
        within one node, where the set IS known when they are enqueued."""
        root, parent, child = tree
        planner = OnlyFor(
            parent.id,
            AttemptDiffPlanner(
                [
                    TaskSpec("summarize", "self"),
                    TaskSpec("extract_facts", "self", after=("summarize",)),
                ]
            ),
        )
        tasks = TaskQueueRepository(db_session)

        steps = settle_walk(child.id, planner, session_factory=factory)

        first, second = (tasks.get(i) for i in steps[-1].enqueued_task_ids)
        assert first.task_type == "summarize" and first.dependencies is None
        assert second.dependencies == [first.id]
        # And the gate holds: extract_facts is not claimable yet.
        claimed = tasks.claim_next("w")
        assert claimed.id == first.id
        assert tasks.claim_next("w2") is None

    def test_an_unresolvable_ordering_reference_raises(self, tree, factory, db_session):
        root, parent, child = tree
        planner = OnlyFor(
            parent.id,
            AttemptDiffPlanner([TaskSpec("extract_facts", "self", after=("summarize",))]),
        )
        with pytest.raises(SettleWalkError, match="not earlier in the same batch"):
            settle_walk(child.id, planner, session_factory=factory)


# ---------------------------------------------------------------------------
# An ingestion starts at the node that has work, and nowhere higher
# ---------------------------------------------------------------------------


class TestAnAncestorIsNotPartOfItsChildsIngestion:
    """A settled node stays settled when something is added or queued below it.

    A folder that gains an uploaded file has acquired no work of its own: its content,
    embedding and index entries are as correct after the upload as before it. Walking
    upward to write `in_flight` made one unreadable PDF take an entire collection out of
    the partial retrieval indexes of Part 2.2 and leave it there, because the file's
    failure meant nothing ever settled it again.

    Spec 2.1's recursive reading of `settled` is still enforced where it decides
    something: `settle_node` counts unsettled children under a row lock, so a node can
    never *become* settled over unfinished children. What no longer happens is a settled
    node *stopping* being settled because of a descendant.
    """

    def test_adding_an_in_flight_child_leaves_the_parent_settled(self, db_session):
        docs = DocumentRepository(db_session)
        parent = docs.create(title="p", content=None, auto_embed=False)
        db_session.flush()
        assert parent.settled == SETTLED_SETTLED

        docs.create(
            title="c",
            content=None,
            parent_id=parent.id,
            auto_embed=False,
            settled=SETTLED_IN_FLIGHT,
        )
        db_session.flush()

        assert db_session.get(type(parent), parent.id).settled == SETTLED_SETTLED

    def test_adding_a_settled_child_leaves_the_parent_settled(self, db_session):
        docs = DocumentRepository(db_session)
        parent = docs.create(title="p", content=None, auto_embed=False)
        db_session.flush()

        docs.create(title="c", content=None, parent_id=parent.id, auto_embed=False)
        db_session.flush()

        assert db_session.get(type(parent), parent.id).settled == SETTLED_SETTLED

    def test_moving_an_in_flight_node_leaves_its_new_parent_settled(self, db_session):
        docs = DocumentRepository(db_session)
        old_parent = docs.create(title="old", content=None, auto_embed=False)
        new_parent = docs.create(title="new", content=None, auto_embed=False)
        db_session.flush()
        mover = docs.create(
            title="m",
            content=None,
            parent_id=old_parent.id,
            auto_embed=False,
            settled=SETTLED_IN_FLIGHT,
        )
        db_session.flush()

        docs.reparent(mover.id, new_parent.id)
        db_session.flush()

        assert db_session.get(type(new_parent), new_parent.id).settled == SETTLED_SETTLED

    def test_enqueueing_a_task_unsettles_its_own_node_and_no_ancestor(self, db_session):
        """The one writer of `in_flight` outside `create`, and its reach is one row."""
        docs = DocumentRepository(db_session)
        grandparent = docs.create(title="g", content=None, auto_embed=False)
        db_session.flush()
        parent = docs.create(title="p", content=None, parent_id=grandparent.id, auto_embed=False)
        db_session.flush()
        leaf = docs.create(title="c", content=None, parent_id=parent.id, auto_embed=False)
        db_session.flush()

        TaskQueueRepository(db_session).enqueue("probe", leaf.id, "self")
        db_session.flush()

        assert db_session.get(type(leaf), leaf.id).settled == SETTLED_IN_FLIGHT
        assert db_session.get(type(leaf), parent.id).settled == SETTLED_SETTLED
        assert db_session.get(type(leaf), grandparent.id).settled == SETTLED_SETTLED

    def test_enqueueing_revives_a_failed_node_but_only_that_node(self, db_session):
        """`failed` is terminal (2.1) and only a correction (Part 6) un-sets it — which is
        what `enqueue` is doing here. An ancestor that died is not revived by work
        happening underneath it, because nothing reaches an ancestor at all."""
        docs = DocumentRepository(db_session)
        dead = docs.create(title="dead", content=None, auto_embed=False, settled=SETTLED_FAILED)
        db_session.flush()
        child = docs.create(
            title="c", content=None, parent_id=dead.id, auto_embed=False, settled=SETTLED_FAILED
        )
        db_session.flush()

        TaskQueueRepository(db_session).enqueue("probe", child.id, "self")
        db_session.flush()

        assert db_session.get(type(child), child.id).settled == SETTLED_IN_FLIGHT
        assert db_session.get(type(child), dead.id).settled == SETTLED_FAILED

    def test_a_settled_parent_still_serves_a_subtree_while_a_new_child_ingests(self, db_session):
        """The point of the whole change, stated as what the reader gets.

        The upload is not in the settled-only view — it is not finished, and publishing it
        would be the lie. Everything that WAS published stays published, and the reader
        gets it without an error, which is the outcome the walk-to-the-root behaviour made
        impossible for as long as any file underneath was unfinished.
        """
        docs = DocumentRepository(db_session)
        library = docs.create(title="library", content="library body", auto_embed=False)
        db_session.flush()
        folder = docs.create(
            title="folder", content="folder body", parent_id=library.id, auto_embed=False
        )
        db_session.flush()
        docs.create(
            title="upload.pdf",
            content=None,
            parent_id=folder.id,
            auto_embed=False,
            settled=SETTLED_IN_FLIGHT,
        )
        db_session.flush()

        titles = {doc.title for doc in docs.get_subtree(library.id)}
        assert titles == {"library", "folder"}


# ---------------------------------------------------------------------------
# Deleting an in-flight node is also a reason to settle
# ---------------------------------------------------------------------------


class TestDeleteDrivesTheWalk:
    """Spec 5.4 step 7 applies to a deletion as much as to a completed task.

    ``settle_after_task`` runs only when a task reaches a terminal state, and a deleted
    node's queue rows cascade away with it — so without a walk from the delete, removing
    an upload before its ``probe`` runs strands its parent at ``in_flight`` forever, out
    of the partial indexes of Part 2.2, with no task and no error.

    The parent here is ``in_flight`` on its own account — a file node whose subtree is
    being built. It is not in flight *because* of the child: a settled node stays settled
    when a child is added (see :class:`TestAnAncestorIsNotPartOfItsChildsIngestion`), so
    the case where a delete has something to re-settle is exactly this one.
    """

    def _tree(self, db_session):
        docs = DocumentRepository(db_session)
        grandparent = docs.create(title="g", content="g body", auto_embed=False)
        db_session.flush()
        parent = docs.create(
            title="p",
            content="p body",
            parent_id=grandparent.id,
            auto_embed=False,
            settled=SETTLED_IN_FLIGHT,
        )
        db_session.flush()
        upload = docs.create(
            title="upload",
            content=None,
            parent_id=parent.id,
            auto_embed=False,
            settled=SETTLED_IN_FLIGHT,
        )
        db_session.flush()
        return docs, grandparent, parent, upload

    def test_deleting_the_last_in_flight_child_re_settles_the_parent(self, db_session):
        docs, grandparent, parent, upload = self._tree(db_session)
        assert docs.get(parent.id).settled == SETTLED_IN_FLIGHT

        DocumentService(db_session).delete_document(upload.id)

        assert docs.get(parent.id).settled == SETTLED_SETTLED
        assert docs.get(grandparent.id).settled == SETTLED_SETTLED

    def test_the_pending_task_row_goes_with_it_and_no_longer_blocks(self, db_session):
        """The realistic version: the client cancels an upload before `probe` runs."""
        docs, grandparent, parent, upload = self._tree(db_session)
        TaskQueueRepository(db_session).enqueue("probe", upload.id, "self")
        db_session.flush()

        DocumentService(db_session).delete_document(upload.id)

        assert TaskQueueRepository(db_session).unfinished_task_count_under(grandparent.id) == 0
        assert docs.get(parent.id).settled == SETTLED_SETTLED
        assert docs.get(grandparent.id).settled == SETTLED_SETTLED

    def test_a_surviving_in_flight_sibling_still_blocks(self, db_session):
        """Not a blanket "settle everything after a delete": the walk asks the tree."""
        docs, grandparent, parent, upload = self._tree(db_session)
        sibling = docs.create(
            title="other upload",
            content=None,
            parent_id=parent.id,
            auto_embed=False,
            settled=SETTLED_IN_FLIGHT,
        )
        db_session.flush()

        DocumentService(db_session).delete_document(upload.id)

        assert docs.get(sibling.id).settled == SETTLED_IN_FLIGHT
        assert docs.get(parent.id).settled == SETTLED_IN_FLIGHT
        assert docs.get(grandparent.id).settled == SETTLED_SETTLED

    def test_deleting_a_root_has_nothing_to_walk(self, db_session):
        docs = DocumentRepository(db_session)
        root = docs.create(title="r", content=None, auto_embed=False, settled=SETTLED_IN_FLIGHT)
        db_session.flush()

        assert DocumentService(db_session).delete_document(root.id) == {"deleted": root.id}
        assert docs.get(root.id) is None


# ---------------------------------------------------------------------------
# What came out of the bytes, recorded at the moment the file node settles
# ---------------------------------------------------------------------------


class TestAFileNodeRecordsThatItYieldedNothing:
    """Zero children from an unreadable file is a correct outcome, and an invisible one.

    A scan with no OCR yet, a truncated upload, a ZIP wearing a `.pdf` name and a
    zero-byte file all reach the end of their work with nothing to show. The bytes were
    processed safely and they do not contain a document we can read — that is the right
    answer, and it does not make the node `failed`.

    It leaves no trace otherwise. `settled` means retrievable, and an empty `content`
    contributes nothing to the full-text and vector indexes, so the only evidence of the
    upload is a title. The record is what makes "which of my uploads produced no
    document?" one query.
    """

    def _file_node(self, db_session, **kwargs):
        docs = DocumentRepository(db_session)
        node = docs.create(
            title="upload.pdf",
            usetype=USETYPE_FILE,
            auto_embed=False,
            settled=SETTLED_IN_FLIGHT,
            **kwargs,
        )
        db_session.flush()
        return docs, node

    def test_an_empty_file_node_settles_and_says_it_yielded_nothing(self, db_session, factory):
        docs, node = self._file_node(
            db_session,
            content=None,
            structured_content={"matched": {"patterns": {"has_text_layer": False}}},
        )

        steps = settle_walk(node.id, NO_ROLLUP, session_factory=factory)

        assert steps[0].settled is True
        refreshed = docs.get(node.id)
        assert refreshed.settled == SETTLED_SETTLED
        assert refreshed.structured_content["yield"]["documents"] == 0
        # The probe's own patterns are carried forward as the evidence for why, rather
        # than a restated guess.
        assert refreshed.structured_content["yield"]["matched"] == {"has_text_layer": False}

    def test_a_file_node_with_content_records_nothing(self, db_session, factory):
        docs, node = self._file_node(db_session, content="the extracted text of the paper")

        settle_walk(node.id, NO_ROLLUP, session_factory=factory)

        assert "yield" not in (docs.get(node.id).structured_content or {})

    def test_a_file_node_with_children_records_nothing(self, db_session, factory):
        docs, node = self._file_node(db_session, content=None)
        docs.create(title="chunk", content="body", parent_id=node.id, auto_embed=False)
        db_session.flush()

        settle_walk(node.id, NO_ROLLUP, session_factory=factory)

        assert "yield" not in (docs.get(node.id).structured_content or {})

    def test_an_empty_structural_node_records_nothing(self, db_session, factory):
        """Scoped to `file`, whose whole purpose is to hold what came out of a byte
        stream. A structural node legitimately holds only children."""
        docs = DocumentRepository(db_session)
        node = docs.create(
            title="Inforetrieval docs",
            content=None,
            usetype="structural",
            auto_embed=False,
            settled=SETTLED_IN_FLIGHT,
        )
        db_session.flush()

        settle_walk(node.id, NO_ROLLUP, session_factory=factory)

        assert docs.get(node.id).settled == SETTLED_SETTLED
        assert "yield" not in (docs.get(node.id).structured_content or {})
