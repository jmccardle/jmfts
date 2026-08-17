"""The task queue: enqueue, claim, write-mode conflicts, retry, attempt records.

``INGEST_SPEC.md`` Parts 5.2, 5.3, 5.5, 5.6 and the error classification of 3.4.

Everything here runs inside the savepoint ``db_session``, i.e. one connection and one
transaction. That is sufficient for the *predicates* — a claim that must not happen still
must not happen with one connection — but not for the races, which need real committed
transactions and live in ``tests/test_settle_races.py``.
"""

from __future__ import annotations

import httpx
import pytest
import sqlalchemy.exc
from sqlalchemy import text

from jmfts_core.models.document import SETTLED_FAILED, SETTLED_IN_FLIGHT, SETTLED_SETTLED
from jmfts_core.models.task_queue import (
    TASK_CLAIMED,
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_PENDING,
    TaskQueue,
)
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.task_queue import TaskQueueRepository
from jmfts_core.task_errors import ErrorType, classify_exception


@pytest.fixture
def repos(db_session):
    return DocumentRepository(db_session), TaskQueueRepository(db_session)


def _node(docs, parent=None, title="n", settled=SETTLED_IN_FLIGHT):
    return docs.create(
        title=title,
        content=None,
        parent_id=parent.id if parent is not None else None,
        auto_embed=False,
        settled=settled,
    )


def _claim_and_run(tasks, task, worker="w"):
    """Claim a specific task and mark it running, asserting the claim landed on it."""
    claimed = tasks.claim_next(worker)
    assert claimed is not None and claimed.id == task.id
    return tasks.mark_running(claimed)


# ---------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------


class TestEnqueue:
    def test_enqueue_creates_a_pending_task(self, repos, db_session):
        docs, tasks = repos
        node = _node(docs)
        task = tasks.enqueue("probe", node.id, "children", params={"a": 1})

        assert task.id is not None
        assert task.status == TASK_PENDING
        assert task.write_mode == "children"
        assert task.params == {"a": 1}
        assert task.param_fingerprint.startswith("sha256:")
        assert task.retry_count == 0 and task.max_retries == 3
        assert task.retryable is True

    def test_enqueue_unsettles_the_scope_node(self, repos):
        """Spec 2.1: a settled node has no task pending for it. Enqueueing one is
        therefore not compatible with staying settled, and the repository — not the
        caller — is what guarantees it."""
        docs, tasks = repos
        node = _node(docs, settled=SETTLED_SETTLED)
        assert node.settled == SETTLED_SETTLED

        tasks.enqueue("probe", node.id, "self")

        assert node.settled == SETTLED_IN_FLIGHT

    def test_enqueue_rejects_unknown_write_mode(self, repos):
        docs, tasks = repos
        node = _node(docs)
        with pytest.raises(ValueError, match="write_mode"):
            tasks.enqueue("probe", node.id, "everything")

    def test_enqueue_rejects_missing_document(self, repos):
        _docs, tasks = repos
        with pytest.raises(ValueError, match="does not exist"):
            tasks.enqueue("probe", 10**9, "self")

    def test_dependencies_are_deduplicated(self, repos):
        """The claim gate counts completed rows against cardinality(dependencies), which
        is only an exact test when the array holds no duplicates."""
        docs, tasks = repos
        node = _node(docs)
        a = tasks.enqueue("a", node.id, "self")
        task = tasks.enqueue("b", node.id, "self", dependencies=[a.id, a.id])
        assert task.dependencies == [a.id]


# ---------------------------------------------------------------------------
# Claim: ordering, dependencies, retry gate
# ---------------------------------------------------------------------------


class TestClaim:
    def test_claim_takes_highest_priority_first(self, repos):
        docs, tasks = repos
        node = _node(docs)
        tasks.enqueue("low", node.id, "self", priority=0)
        high = tasks.enqueue("high", node.id, "self", priority=10)

        claimed = tasks.claim_next("worker-1")
        assert claimed is not None and claimed.id == high.id
        assert claimed.status == TASK_CLAIMED
        assert claimed.claimed_by == "worker-1"
        assert claimed.started_at is not None

    def test_claim_returns_none_when_nothing_is_queued(self, repos):
        _docs, tasks = repos
        assert tasks.claim_next("worker-1") is None

    def test_dependencies_gate_the_claim(self, repos):
        """Spec 5.5: ordering WITHIN one node, where the set is known at enqueue time."""
        docs, tasks = repos
        node = _node(docs)
        first = tasks.enqueue("summarize", node.id, "self")
        second = tasks.enqueue("extract_facts", node.id, "self", dependencies=[first.id])

        # `second` must not be claimable while `first` is outstanding, even though it
        # would otherwise be a candidate.
        claimed = tasks.claim_next("w")
        assert claimed.id == first.id
        assert tasks.claim_next("w2") is None

        tasks.mark_running(claimed)
        tasks.complete(claimed, detail={})

        now_claimable = tasks.claim_next("w2")
        assert now_claimable is not None and now_claimable.id == second.id

    def test_a_vanished_dependency_blocks_rather_than_unblocks(self, repos, db_session):
        """Counting completed rows, not testing for a non-completed one: a dependency
        whose row was cascade-deleted must stall visibly, never run out of order."""
        docs, tasks = repos
        node = _node(docs)
        dep = tasks.enqueue("gone", node.id, "self")
        dependent = tasks.enqueue("after", node.id, "self", dependencies=[dep.id])
        db_session.execute(text("DELETE FROM task_queue WHERE id = :i"), {"i": dep.id})
        db_session.flush()

        assert tasks.claim_next("w") is None
        assert tasks.get(dependent.id).status == TASK_PENDING

    def test_badged_worker_claims_its_own_and_unbadged_work(self, repos):
        docs, tasks = repos
        node = _node(docs)
        tasks.enqueue("review", node.id, "self", service_badge="human", priority=5)
        tasks.enqueue("plain", node.id, "self", priority=1)

        first = tasks.claim_next("w", service_badge="human")
        assert first.task_type == "review"
        tasks.mark_running(first)
        tasks.complete(first, detail={})

        second = tasks.claim_next("w", service_badge="human")
        assert second.task_type == "plain"

    def test_unbadged_worker_claims_badged_work_too(self, repos):
        """Triskelion required strict badge equality, which made a NULL-badge task
        unclaimable by anyone. The in-process worker claims everything by default."""
        docs, tasks = repos
        node = _node(docs)
        tasks.enqueue("review", node.id, "self", service_badge="human")
        assert tasks.claim_next("w") is not None


# ---------------------------------------------------------------------------
# Spec 5.3 — declared write mode
# ---------------------------------------------------------------------------


class TestWriteModeConflicts:
    def test_self_conflicts_with_self_on_the_same_node(self, repos):
        docs, tasks = repos
        node = _node(docs)
        a = tasks.enqueue("a", node.id, "self")
        tasks.enqueue("b", node.id, "self")

        _claim_and_run(tasks, a)
        assert tasks.claim_next("w2") is None

    def test_self_does_not_conflict_on_different_nodes(self, repos):
        docs, tasks = repos
        one, two = _node(docs, title="one"), _node(docs, title="two")
        a = tasks.enqueue("a", one.id, "self")
        tasks.enqueue("b", two.id, "self")

        _claim_and_run(tasks, a)
        assert tasks.claim_next("w2") is not None

    def test_children_conflicts_with_children_on_the_same_node(self, repos):
        docs, tasks = repos
        node = _node(docs)
        a = tasks.enqueue("split", node.id, "children")
        tasks.enqueue("dividers", node.id, "children")

        _claim_and_run(tasks, a)
        assert tasks.claim_next("w2") is None

    def test_self_and_children_on_one_node_do_not_conflict(self, repos):
        """The 5.3 table is exact: `self` conflicts with another `self`, `children` with
        another `children`. It does not say self-vs-children, and the write sets do not
        overlap dangerously — one rewrites the node's own columns, the other arranges
        childless children."""
        docs, tasks = repos
        node = _node(docs)
        a = tasks.enqueue("summarize", node.id, "self")
        tasks.enqueue("split", node.id, "children")

        _claim_and_run(tasks, a)
        assert tasks.claim_next("w2") is not None

    def test_active_subtree_blocks_anything_scoped_inside_it(self, repos, db_session):
        docs, tasks = repos
        root = _node(docs, title="root")
        mid = _node(docs, parent=root, title="mid")
        leaf = _node(docs, parent=mid, title="leaf")
        db_session.flush()

        correction = tasks.enqueue("correct", root.id, "subtree", priority=10)
        tasks.enqueue("summarize", leaf.id, "self")

        _claim_and_run(tasks, correction)
        assert tasks.claim_next("w2") is None, "a self task inside a reserved subtree"

    def test_active_subtree_does_not_block_an_unrelated_branch(self, repos, db_session):
        docs, tasks = repos
        root = _node(docs, title="root")
        left = _node(docs, parent=root, title="left")
        other_root = _node(docs, title="other")
        db_session.flush()

        correction = tasks.enqueue("correct", left.id, "subtree", priority=10)
        tasks.enqueue("summarize", other_root.id, "self")

        _claim_and_run(tasks, correction)
        assert tasks.claim_next("w2") is not None

    def test_a_subtree_candidate_waits_for_active_work_inside_it(self, repos, db_session):
        """The rule is symmetric: `subtree` reserves a region, so it cannot start while
        something is already writing inside that region."""
        docs, tasks = repos
        root = _node(docs, title="root")
        leaf = _node(docs, parent=root, title="leaf")
        db_session.flush()

        inner = tasks.enqueue("summarize", leaf.id, "self", priority=10)
        tasks.enqueue("correct", root.id, "subtree")

        _claim_and_run(tasks, inner)
        assert tasks.claim_next("w2") is None

    def test_the_reservation_is_released_when_the_task_finishes(self, repos):
        docs, tasks = repos
        node = _node(docs)
        a = tasks.enqueue("a", node.id, "self")
        b = tasks.enqueue("b", node.id, "self")

        running = _claim_and_run(tasks, a)
        assert tasks.claim_next("w2") is None
        tasks.complete(running, detail={})

        released = tasks.claim_next("w2")
        assert released is not None and released.id == b.id

    def test_pending_tasks_reserve_nothing(self, repos):
        """Only claimed/running tasks hold a region — otherwise a thousand queued tasks
        over one subtree would block each other before any of them started."""
        docs, tasks = repos
        node = _node(docs)
        tasks.enqueue("a", node.id, "self")
        tasks.enqueue("b", node.id, "self")
        assert tasks.claim_next("w") is not None


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------


class TestRetry:
    def _failed(self, tasks, node, error_type):
        task = tasks.enqueue("flaky", node.id, "self")
        running = _claim_and_run(tasks, task)
        tasks.fail(running, error="boom", error_type=error_type)
        return tasks.get(task.id)

    def test_retryable_failure_schedules_a_backoff(self, repos):
        docs, tasks = repos
        node = _node(docs)
        task = self._failed(tasks, node, ErrorType.RETRYABLE)

        assert task.status == TASK_FAILED
        assert task.retryable is True
        assert task.error_type == "retryable"
        assert task.retry_after is not None and task.retry_after > task.completed_at
        # Backed off: not claimable yet.
        assert tasks.claim_next("w") is None

    def test_the_backoff_expires_and_the_retry_increments_the_count(self, repos, db_session):
        docs, tasks = repos
        node = _node(docs)
        task = self._failed(tasks, node, ErrorType.RETRYABLE)

        db_session.execute(
            text("UPDATE task_queue SET retry_after = NOW() - INTERVAL '1 hour' WHERE id = :i"),
            {"i": task.id},
        )
        db_session.flush()

        retried = tasks.claim_next("w")
        assert retried is not None and retried.id == task.id
        assert retried.retry_count == 1
        assert retried.error is None and retried.error_type is None

    def test_permanent_failure_is_not_retried_and_fails_the_node(self, repos):
        """Spec 2.1: without `failed`, a node whose ingestion died permanently looks
        identical to one still in progress."""
        docs, tasks = repos
        node = _node(docs)
        task = self._failed(tasks, node, ErrorType.PERMANENT)

        assert task.retryable is False
        assert task.retry_after is None
        assert tasks.claim_next("w") is None
        assert docs.get(node.id).settled == SETTLED_FAILED

    def test_dependency_failure_is_not_retried(self, repos):
        docs, tasks = repos
        node = _node(docs)
        task = self._failed(tasks, node, ErrorType.DEPENDENCY)
        assert task.retryable is False
        assert tasks.claim_next("w") is None

    def test_the_cap_stops_the_retries_and_fails_the_node(self, repos, db_session):
        docs, tasks = repos
        node = _node(docs)
        task = tasks.enqueue("flaky", node.id, "self", max_retries=1)

        # Attempt 1 fails and is retried.
        tasks.fail(_claim_and_run(tasks, task), error="1", error_type=ErrorType.TIMEOUT)
        db_session.execute(
            text("UPDATE task_queue SET retry_after = NOW() - INTERVAL '1 hour' WHERE id = :i"),
            {"i": task.id},
        )
        db_session.flush()

        # Attempt 2 exhausts the cap: retry_count is now 1 == max_retries.
        second = tasks.claim_next("w")
        assert second.retry_count == 1
        tasks.mark_running(second)
        tasks.fail(second, error="2", error_type=ErrorType.TIMEOUT)

        assert tasks.claim_next("w") is None
        assert docs.get(node.id).settled == SETTLED_FAILED

    def test_an_explicit_delay_overrides_the_backoff(self, repos):
        docs, tasks = repos
        node = _node(docs)
        task = tasks.enqueue("flaky", node.id, "self")
        tasks.fail(
            _claim_and_run(tasks, task),
            error="rate limited",
            error_type=ErrorType.RETRYABLE,
            retry_delay_seconds=3600,
        )
        row = tasks.get(task.id)
        assert (row.retry_after - row.completed_at).total_seconds() == pytest.approx(3600, abs=2)


# ---------------------------------------------------------------------------
# Spec 5.6 — the durable record
# ---------------------------------------------------------------------------


class TestAttemptRecords:
    def test_completion_writes_the_attempt_onto_the_node(self, repos):
        docs, tasks = repos
        node = _node(docs)
        task = tasks.enqueue("structure:declared", node.id, "children", params={"min": 200})
        running = _claim_and_run(tasks, task)

        record = tasks.complete(
            running,
            detail={"sections": 3},
            produced={"node_count": 3, "child_ids": [1, 2, 3]},
            rung="declared",
        )

        assert record.task_id == task.id
        assert record.write_mode == "children"
        assert record.scope_document_id == node.id
        assert record.params == {"min": 200}
        assert record.param_fingerprint == task.param_fingerprint
        assert record.started_at is not None and record.finished_at is not None

        stored = docs.get(node.id).structured_content["attempts"]
        assert len(stored) == 1
        assert stored[0]["task"] == "structure:declared"
        assert stored[0]["status"] == "completed"
        assert stored[0]["rung"] == "declared"

    def test_failure_writes_the_attempt_with_its_classification(self, repos):
        docs, tasks = repos
        node = _node(docs)
        task = tasks.enqueue("probe", node.id, "self")
        record = tasks.fail(
            _claim_and_run(tasks, task), error="corrupt", error_type=ErrorType.PERMANENT
        )

        assert record.status == "failed"
        assert record.error_type == "permanent"
        stored = docs.get(node.id).structured_content["attempts"]
        assert stored[-1]["error"] == "corrupt"
        assert stored[-1]["error_type"] == "permanent"

    def test_the_attempt_counter_is_per_node_and_task(self, repos, db_session):
        """A re-run under spec 6.1 is a NEW queue row whose retry_count restarts at zero
        while the node's history does not, so the counter comes from the log."""
        docs, tasks = repos
        node = _node(docs)
        for _ in range(2):
            task = tasks.enqueue("probe", node.id, "self")
            tasks.complete(_claim_and_run(tasks, task), detail={})

        stored = docs.get(node.id).structured_content["attempts"]
        assert [entry["attempt"] for entry in stored] == [1, 2]

    def test_a_skipped_completion_still_needs_a_reason(self, repos):
        docs, tasks = repos
        node = _node(docs)
        task = tasks.enqueue("describe:images", node.id, "self")
        running = _claim_and_run(tasks, task)
        with pytest.raises(Exception, match="reason"):
            tasks.complete(running, detail={}, status="skipped")

    def test_completing_a_never_started_task_is_refused(self, repos, db_session):
        docs, tasks = repos
        node = _node(docs)
        task = tasks.enqueue("probe", node.id, "self")
        with pytest.raises(ValueError, match="started_at"):
            tasks.complete(task, detail={})


# ---------------------------------------------------------------------------
# Spec 5.2 — structuring complete is a QUERY
# ---------------------------------------------------------------------------


class TestStructuringComplete:
    def test_true_when_no_tasks_exist(self, repos):
        docs, tasks = repos
        node = _node(docs)
        assert tasks.structuring_complete(node.id) is True

    def test_false_while_pending_claimed_or_running(self, repos):
        docs, tasks = repos
        node = _node(docs)
        task = tasks.enqueue("probe", node.id, "self")
        assert tasks.structuring_complete(node.id) is False

        claimed = tasks.claim_next("w")
        assert tasks.structuring_complete(node.id) is False
        tasks.mark_running(claimed)
        assert tasks.structuring_complete(node.id) is False

        tasks.complete(claimed, detail={})
        assert tasks.structuring_complete(node.id) is True
        assert tasks.get(task.id).status == TASK_COMPLETED

    def test_a_retryable_failure_still_counts_as_unfinished(self, repos):
        """Retry is folded into claimability rather than run as a sweep, so a failed
        task with retries left IS the queue's representation of "waiting for backoff".
        Settling the node under one would publish work that is about to run again."""
        docs, tasks = repos
        node = _node(docs)
        task = tasks.enqueue("flaky", node.id, "self")
        tasks.fail(_claim_and_run(tasks, task), error="x", error_type=ErrorType.RETRYABLE)
        assert tasks.structuring_complete(node.id) is False

    def test_a_permanent_failure_is_finished(self, repos):
        docs, tasks = repos
        node = _node(docs)
        task = tasks.enqueue("flaky", node.id, "self")
        tasks.fail(_claim_and_run(tasks, task), error="x", error_type=ErrorType.PERMANENT)
        assert tasks.structuring_complete(node.id) is True

    def test_it_is_scoped_to_the_node_not_the_subtree(self, repos, db_session):
        docs, tasks = repos
        parent = _node(docs, title="p")
        child = _node(docs, parent=parent, title="c")
        db_session.flush()
        tasks.enqueue("probe", child.id, "self")

        assert tasks.structuring_complete(parent.id) is True
        assert tasks.structuring_complete(child.id) is False


# ---------------------------------------------------------------------------
# Spec 3.4 — classification, not string matching
# ---------------------------------------------------------------------------


class TestClassifyException:
    @pytest.mark.parametrize(
        "exc,expected",
        [
            (httpx.ReadTimeout("slow"), ErrorType.TIMEOUT),
            (TimeoutError("slow"), ErrorType.TIMEOUT),
            (httpx.ConnectError("refused"), ErrorType.RETRYABLE),
            (ConnectionError("refused"), ErrorType.RETRYABLE),
            (ValueError("bad page"), ErrorType.PERMANENT),
            (KeyError("missing"), ErrorType.PERMANENT),
            (OSError("No space left on device"), ErrorType.RETRYABLE),
            (OSError("Permission denied"), ErrorType.PERMANENT),
            (RuntimeError("who knows"), ErrorType.RETRYABLE),
        ],
    )
    def test_mapping(self, exc, expected):
        assert classify_exception(exc) is expected

    def test_a_timeout_is_not_reported_as_a_connection_failure(self):
        """httpx.TimeoutException subclasses TransportError, so arm order is what keeps
        these apart — and error_type exists to be read, not just to gate retries."""
        assert classify_exception(httpx.ConnectTimeout("x")) is ErrorType.TIMEOUT

    def test_http_status_splits_on_server_vs_client_error(self):
        request = httpx.Request("GET", "http://example.invalid")
        server = httpx.HTTPStatusError(
            "502", request=request, response=httpx.Response(502, request=request)
        )
        client = httpx.HTTPStatusError(
            "400", request=request, response=httpx.Response(400, request=request)
        )
        assert classify_exception(server) is ErrorType.RETRYABLE
        assert classify_exception(client) is ErrorType.PERMANENT

    def test_sqlalchemy_operational_errors_are_retryable(self):
        exc = sqlalchemy.exc.OperationalError("SELECT 1", {}, Exception("conn lost"))
        assert classify_exception(exc) is ErrorType.RETRYABLE

    def test_the_enum_values_match_the_database_check_constraint(self, db_session):
        """The column is VARCHAR + CHECK, so a renamed enum value fails at flush time in
        production and here instead."""
        rows = db_session.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'ck_task_queue_error_type'"
            )
        ).scalar_one()
        for member in ErrorType:
            assert f"'{member.value}'" in rows


def test_the_model_and_the_schema_agree_on_columns(db_session):
    """schema.sql (what tests and fresh installs get) and the SQLAlchemy model (what
    create_all would build) must not drift — triskelion's did, silently dropping two
    columns from the model."""
    live = {
        row[0]
        for row in db_session.execute(
            text("SELECT column_name FROM information_schema.columns WHERE table_name='task_queue'")
        )
    }
    declared = {column.name for column in TaskQueue.__table__.columns}
    assert declared == live
