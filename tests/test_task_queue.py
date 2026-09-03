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
    TASK_BATCHED,
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

        first = tasks.claim_next("w", service_badges=["human"])
        assert first.task_type == "review"
        tasks.mark_running(first)
        tasks.complete(first, detail={})

        second = tasks.claim_next("w", service_badges=["human"])
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
    def test_completion_writes_the_attempt_onto_the_node(self, repos, evidence):
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

        stored = evidence(docs.get(node.id))["attempts"]
        assert len(stored) == 1
        assert stored[0]["task"] == "structure:declared"
        assert stored[0]["status"] == "completed"
        assert stored[0]["rung"] == "declared"

    def test_failure_writes_the_attempt_with_its_classification(self, repos, evidence):
        docs, tasks = repos
        node = _node(docs)
        task = tasks.enqueue("probe", node.id, "self")
        record = tasks.fail(
            _claim_and_run(tasks, task), error="corrupt", error_type=ErrorType.PERMANENT
        )

        assert record.status == "failed"
        assert record.error_type == "permanent"
        stored = evidence(docs.get(node.id))["attempts"]
        assert stored[-1]["error"] == "corrupt"
        assert stored[-1]["error_type"] == "permanent"

    def test_the_attempt_counter_is_per_node_and_task(self, repos, db_session, evidence):
        """A re-run under spec 6.1 is a NEW queue row whose retry_count restarts at zero
        while the node's history does not, so the counter comes from the log."""
        docs, tasks = repos
        node = _node(docs)
        for _ in range(2):
            task = tasks.enqueue("probe", node.id, "self")
            tasks.complete(_claim_and_run(tasks, task), detail={})

        stored = evidence(docs.get(node.id))["attempts"]
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

    def test_rate_limiting_is_retryable_despite_being_a_4xx(self):
        """The one 4xx that is not a statement about the request. A worker pool whose job
        is forwarding to a metered LLM API meets 429 routinely; classified PERMANENT it
        would burn the retry budget at once and settle the node 'failed' during an ordinary
        traffic spike — a tree marked permanently broken because somebody else was busy."""
        request = httpx.Request("POST", "http://example.invalid")
        throttled = httpx.HTTPStatusError(
            "429", request=request, response=httpx.Response(429, request=request)
        )
        assert classify_exception(throttled) is ErrorType.RETRYABLE

    def test_a_408_reads_as_a_timeout(self):
        """Same retry behaviour as 429, but error_type exists to be read: 'the server said
        the request timed out' and 'the server throttled us' are different facts."""
        request = httpx.Request("POST", "http://example.invalid")
        timed_out = httpx.HTTPStatusError(
            "408", request=request, response=httpx.Response(408, request=request)
        )
        assert classify_exception(timed_out) is ErrorType.TIMEOUT

    def test_a_bad_model_name_is_still_permanent(self):
        """The behaviour that was right and stays right: a 400 from a model name that does
        not exist on the server will not fix itself, and retrying only delays the report."""
        request = httpx.Request("POST", "http://example.invalid")
        bad_model = httpx.HTTPStatusError(
            "400", request=request, response=httpx.Response(400, request=request)
        )
        assert classify_exception(bad_model) is ErrorType.PERMANENT

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


# ---------------------------------------------------------------------------
# Badge routing — jmfts_core/task_routing.py
# ---------------------------------------------------------------------------


class TestBadgeRouting:
    """``enqueue`` stamps the configured badge; the claim honours it.

    The mechanism is migration 010's and is already tested above. What is new is the
    POLICY, and the two things worth pinning about it are that it is off unless configured
    (a badge nobody can claim is a permanent stall, not a slowdown) and that an explicit
    ``None`` still beats it.
    """

    @pytest.fixture
    def gpu_policy(self, monkeypatch):
        from jmfts_core.config import get_settings
        from jmfts_core.task_routing import EMBEDDING_AND_LLM_POLICY

        settings = get_settings()
        monkeypatch.setattr(settings, "task_badges", dict(EMBEDDING_AND_LLM_POLICY))
        return settings

    def test_no_policy_configured_means_no_badges(self, repos):
        """The default reproduces the pre-fleet behaviour exactly. A cluster that has not
        declared its worker pools must not have work routed at pools it does not run."""
        docs, tasks = repos
        node = _node(docs)
        docs.session.flush()

        task = tasks.enqueue("embed", node.id, "self")

        assert task.service_badge is None

    def test_the_configured_policy_is_applied_at_enqueue(self, repos, gpu_policy):
        docs, tasks = repos
        node = _node(docs)
        docs.session.flush()

        embedding = tasks.enqueue("embed", node.id, "self")
        cheap = tasks.enqueue("probe", node.id, "self")

        assert embedding.service_badge == "embed"
        assert cheap.service_badge is None

    def test_an_explicit_none_overrides_the_policy(self, repos, gpu_policy):
        """Part 7's review tasks are claimed by people. A policy meant for machines must
        not be able to route one at a GPU pool."""
        docs, tasks = repos
        node = _node(docs)
        docs.session.flush()

        task = tasks.enqueue("summarize", node.id, "self", service_badge=None)

        assert task.service_badge is None

    def test_a_cpu_worker_does_not_claim_gpu_badged_work(self, repos, gpu_policy):
        """The asymmetry the empty default exists to protect against: this is a stall, not
        a slow path, and nothing in the queue reports it."""
        docs, tasks = repos
        node = _node(docs)
        docs.session.flush()
        tasks.enqueue("embed", node.id, "self")
        docs.session.flush()

        assert tasks.claim_next("cpu-worker", service_badges=["cpu"]) is None

    def test_a_gpu_worker_claims_its_own_badge_and_unbadged_work(self, repos, gpu_policy):
        docs, tasks = repos
        node = _node(docs)
        other = _node(docs, title="other")
        docs.session.flush()
        badged = tasks.enqueue("embed", node.id, "self")
        unbadged = tasks.enqueue("probe", other.id, "self")
        docs.session.flush()

        claimed = {
            tasks.claim_next("embed-worker", service_badges=["embed"]).id,
            tasks.claim_next("embed-worker", service_badges=["embed"]).id,
        }

        assert claimed == {badged.id, unbadged.id}

    def test_the_settle_walk_routes_the_tasks_it_creates(self, repos, gpu_policy):
        """The regression that motivated the sentinel. `TaskSpec.service_badge` defaulted
        to None, which `enqueue` correctly read as "deliberately un-badged" — so every
        structure and summarize task the walk created came out unrouted, the GPU pool saw
        none of the work it exists for, and nothing anywhere reported a problem.

        Asserted through `enqueue_batch` rather than by inspecting the dataclass, because
        the defect was in how the two layers composed, not in either one alone."""
        from jmfts_core.settling import TaskSpec, enqueue_batch

        docs, tasks = repos
        node = _node(docs)
        docs.session.flush()

        enqueue_batch(
            tasks,
            node.id,
            [
                TaskSpec(task_type="summarize", write_mode="self"),
                TaskSpec(task_type="probe", write_mode="self"),
            ],
        )
        docs.session.flush()

        badges = {
            t.task_type: t.service_badge
            for t in docs.session.query(TaskQueue).filter_by(scope_document_id=node.id)
        }
        assert badges == {"summarize": "embed", "probe": None}

    def test_a_planner_can_still_force_a_task_unbadged(self, repos, gpu_policy):
        """Part 7's review tasks are claimed by people. An explicit None must survive the
        policy, or a human queue could be routed at a GPU pool."""
        from jmfts_core.settling import TaskSpec, enqueue_batch

        docs, tasks = repos
        node = _node(docs)
        docs.session.flush()

        enqueue_batch(
            tasks,
            node.id,
            [TaskSpec(task_type="summarize", write_mode="self", service_badge=None)],
        )
        docs.session.flush()

        task = docs.session.query(TaskQueue).filter_by(scope_document_id=node.id).one()
        assert task.service_badge is None

    def test_a_worker_can_answer_more_than_one_badge(self, repos, gpu_policy):
        """The point of a badge LIST. A host with a local LLM on a GPU can serve both the
        embedding pool and the LLM pool; with one badge per worker it would sit idle
        whenever the other kind of work was queued."""
        docs, tasks = repos
        a, b, c = _node(docs, title="a"), _node(docs, title="b"), _node(docs, title="c")
        docs.session.flush()
        embed = tasks.enqueue("embed", a.id, "self")
        llm = tasks.enqueue("summarize:llm", b.id, "self")
        plain = tasks.enqueue("probe", c.id, "self")
        docs.session.flush()

        claimed = set()
        for _ in range(3):
            task = tasks.claim_next("big-box", service_badges=["embed", "llm"])
            assert task is not None
            claimed.add(task.id)

        assert claimed == {embed.id, llm.id, plain.id}

    def test_a_narrow_worker_still_only_takes_its_own(self, repos, gpu_policy):
        """The light runner that forwards to a web API answers `llm` and nothing else."""
        docs, tasks = repos
        a, b = _node(docs, title="a"), _node(docs, title="b")
        docs.session.flush()
        tasks.enqueue("embed", a.id, "self")
        llm = tasks.enqueue("summarize:llm", b.id, "self")
        docs.session.flush()

        first = tasks.claim_next("api-runner", service_badges=["llm"])
        second = tasks.claim_next("api-runner", service_badges=["llm"])

        assert first.id == llm.id
        assert second is None, "an llm-only worker must not claim embedding work"

    def test_an_empty_badge_list_means_claims_anything(self, repos, gpu_policy):
        """Not "claims nothing". `= ANY('{}')` is false for every badged row, so an empty
        list passed straight through would make a worker that silently does no work."""
        docs, tasks = repos
        node = _node(docs)
        docs.session.flush()
        badged = tasks.enqueue("embed", node.id, "self")
        docs.session.flush()

        assert tasks.claim_next("appliance", service_badges=[]).id == badged.id


# ---------------------------------------------------------------------------
# `batched` — the handoff to an external batch provider (migration 012)
# ---------------------------------------------------------------------------


class TestBatchedStatus:
    """A task parked at a provider: not running, not finished, not claimable.

    The premise being tested is that the queue can survive the handoff. A batch worker
    claims a set, submits them somewhere it cannot roll back, and hands the rows to a state
    that outlives it — so that the answer it has already paid for is not bought a second
    time by whoever claims next.
    """

    def _claimed(self, repos, count, worker="batcher"):
        """``count`` tasks of the same type, each on its own node, all held by ``worker``."""
        docs, tasks = repos
        made = []
        for i in range(count):
            node = _node(docs, title=f"n{i}")
            docs.session.flush()
            made.append(tasks.enqueue("summarize:llm", node.id, "self", service_badge=None))
        docs.session.flush()
        held = []
        for _ in range(count):
            task = tasks.claim_next(worker)
            assert task is not None
            held.append(tasks.mark_running(task))
        docs.session.flush()
        return docs, tasks, held

    def test_a_batched_task_is_not_claimable_by_anyone(self, repos):
        """The whole point. Once the content is shipped it is paid for; a second worker
        claiming it and calling the provider directly is the same answer bought twice."""
        docs, tasks, held = self._claimed(repos, 2)

        tasks.mark_batched(held, "batch_abc123")
        docs.session.flush()

        assert tasks.claim_next("someone-else") is None

    def test_it_records_where_the_work_went(self, repos):
        """The id is the only handle on a batch that outlives the worker that submitted
        it. Without it the rows are unreachable by every mechanism in the repository."""
        docs, tasks, held = self._claimed(repos, 2)

        moved = tasks.mark_batched(held, "batch_abc123")
        docs.session.flush()

        assert sorted(moved) == sorted(t.id for t in held)
        for task in held:
            assert task.status == TASK_BATCHED
            assert task.batch_id == "batch_abc123"
            assert task.batched_at is not None

    def test_batching_costs_no_retry_and_does_not_fail_the_node(self, repos):
        """`batched` is not a variant of `failed`. Going through the failure path would
        spend a retry on work that has not been attempted, and at the cap it would settle
        the node 'failed' — publishing a permanent failure for a task that is waiting."""
        docs, tasks, held = self._claimed(repos, 1)
        node_id = held[0].scope_document_id

        tasks.mark_batched(held, "batch_abc123")
        docs.session.flush()

        assert held[0].retry_count == 0
        assert held[0].error is None
        assert docs.get(node_id).settled == SETTLED_IN_FLIGHT

    def test_a_batched_task_still_reserves_its_node(self, repos):
        """It is going to write this node's effective_content when the batch returns. A
        second `self` task writing there meanwhile is the race the reservation prevents."""
        docs, tasks, held = self._claimed(repos, 1)
        node_id = held[0].scope_document_id

        tasks.mark_batched(held, "batch_abc123")
        tasks.enqueue("summarize", node_id, "self", service_badge=None)
        docs.session.flush()

        assert tasks.claim_next("other-worker") is None
        assert tasks.active_task_count(node_id) == 1

    def test_the_node_is_not_structurally_complete_while_batched(self, repos):
        """The trap. If `_unfinished_criterion` misses `batched`, the tree settles and the
        node enters the retrieval indexes with no effective_content — silently, and it
        looks like success."""
        docs, tasks, held = self._claimed(repos, 1)
        node_id = held[0].scope_document_id

        tasks.mark_batched(held, "batch_abc123")
        docs.session.flush()

        assert tasks.structuring_complete(node_id) is False

    def test_the_lease_does_not_reap_a_batched_task(self, repos):
        """Nothing beats for a batched row and nothing should — the work is at the
        provider. Reaping one would requeue an answer already bought and paid for."""
        docs, tasks, held = self._claimed(repos, 2)
        tasks.mark_batched(held, "batch_abc123")
        docs.session.flush()
        docs.session.execute(
            text(
                "UPDATE task_queue SET batched_at = clock_timestamp() "
                "- make_interval(secs => 86400) WHERE status = 'batched'"
            )
        )
        docs.session.expire_all()

        assert tasks.requeue_expired_claims(90) == []

    def test_a_restarting_worker_does_not_reclaim_what_it_batched(self, repos):
        """`recover_own_claims` rescues rows a dead worker was HOLDING. It was not holding
        these — it handed them off before it died, which is the point of the handoff."""
        docs, tasks, held = self._claimed(repos, 2, worker="batcher")
        tasks.mark_batched(held, "batch_abc123")
        docs.session.flush()

        assert tasks.requeue_stale_claims("batcher") == []
        assert all(tasks.get(t.id).status == TASK_BATCHED for t in held)

    def test_any_worker_can_find_and_finish_the_batch(self, repos):
        """The batch outlives the process that submitted it, so the poll must not be tied
        to that process. A rescheduled pod would otherwise strand the batch forever."""
        docs, tasks, held = self._claimed(repos, 2)
        tasks.mark_batched(held, "batch_abc123")
        docs.session.flush()

        assert tasks.outstanding_batches() == ["batch_abc123"]
        found = tasks.batched_tasks("batch_abc123")
        assert [t.id for t in found] == sorted(t.id for t in held)

        for task in found:
            tasks.complete(task, detail={"batch_id": "batch_abc123"})
        docs.session.flush()

        assert tasks.outstanding_batches() == []
        assert all(tasks.get(t.id).status == TASK_COMPLETED for t in held)

    def test_completing_from_batched_needs_no_reclaim(self, repos, evidence):
        """`complete` requires only that the task was once started, which the claim did.
        A batched task never went back to `pending`, so there is nothing to re-claim and no
        second attempt record."""
        docs, tasks, held = self._claimed(repos, 1)
        node_id = held[0].scope_document_id
        tasks.mark_batched(held, "batch_abc123")
        docs.session.flush()

        tasks.complete(held[0], detail={"batch_id": "batch_abc123"})
        docs.session.flush()

        entries = evidence(docs.get(node_id))["attempts"]
        assert [e["status"] for e in entries] == ["completed"]
        assert tasks.structuring_complete(node_id) is True

    def test_a_batch_the_provider_rejects_fails_normally(self, repos):
        """The other exit. A batch that errors is a real failure and takes the ordinary
        retry path — `batched` is a waiting state, not a terminal one."""
        docs, tasks, held = self._claimed(repos, 1)
        tasks.mark_batched(held, "batch_abc123")
        docs.session.flush()

        tasks.fail(held[0], error="the provider rejected the batch", error_type=ErrorType.RETRYABLE)
        docs.session.flush()

        failed = tasks.get(held[0].id)
        assert failed.status == TASK_FAILED
        assert failed.retryable is True
        # Backed off like any other retryable failure — not claimable this instant, which
        # is the ordinary retry path and not something `batched` should short-circuit.
        assert failed.retry_after is not None
        assert tasks.claim_next("retry-worker") is None

    def test_a_stalled_batch_is_findable(self, repos):
        """The compensating control. A batched row is invisible to the claim, to the lease
        and to stale-claim recovery, so without this query a lost batch is a permanent,
        silent stall — exactly what migration 011 abolished, reintroduced on purpose."""
        docs, tasks, held = self._claimed(repos, 2)
        tasks.mark_batched(held, "batch_abc123")
        docs.session.flush()

        assert tasks.stalled_batches(3600) == []

        docs.session.execute(
            text(
                "UPDATE task_queue SET batched_at = clock_timestamp() "
                "- make_interval(secs => 172800) WHERE status = 'batched'"
            )
        )
        docs.session.expire_all()

        assert [t.id for t in tasks.stalled_batches(3600)] == sorted(t.id for t in held)

    def test_only_one_worker_polls_a_batch_at_a_time(self, repos):
        """Any worker may adopt a batch, so two can poll the same one. The write is
        idempotent in content but not in effect: both would append to the attempt log."""
        docs, tasks, held = self._claimed(repos, 1)
        tasks.mark_batched(held, "batch_abc123")
        docs.session.flush()

        assert tasks.with_batch_lock("batch_abc123") is True

        from jmfts_core.database import get_session

        with get_session() as other:
            rival = TaskQueueRepository(other)
            assert rival.with_batch_lock("batch_abc123") is False
            # A DIFFERENT batch is not blocked — the lock is per batch, not global.
            assert rival.with_batch_lock("batch_zzz999") is True

    def test_a_batch_without_an_id_is_refused(self, repos):
        """An id-less batched row is unreachable: nothing can poll it, the lease will not
        touch it, and it reserves its node forever."""
        docs, tasks, held = self._claimed(repos, 1)

        with pytest.raises(ValueError, match="batch id"):
            tasks.mark_batched(held, "")

    def test_only_a_held_task_can_be_handed_to_a_batch(self, repos):
        """Batching a row this worker does not hold would park work somebody else is
        running, or work nobody has claimed at all."""
        docs, tasks = repos
        node = _node(docs)
        docs.session.flush()
        pending = tasks.enqueue("summarize:llm", node.id, "self", service_badge=None)
        docs.session.flush()

        with pytest.raises(ValueError, match="only a task this worker is holding"):
            tasks.mark_batched([pending], "batch_abc123")
