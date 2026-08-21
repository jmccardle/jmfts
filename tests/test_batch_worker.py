"""The reference batch worker: the ``batched`` handoff, driven end to end.

The premise under test, in John's words: a worker claims a set of tasks, gathers them,
submits them somewhere it cannot roll back, and trades its claim for a state that outlives
it — so the answer it has already paid for is not bought a second time.

Four things have to hold for that to be true, and they are the four classes below.

* :class:`TestTheMockProvider` — the model is not called until the batch is finalized.
  Everything else depends on that, because a mock that answered at submit time would make
  every ``batched`` window zero-width and the interesting failures untestable.
* :class:`TestGatherAndSubmit` — the claim becomes ``batched``, and a submission that fails
  does not leave rows pretending to be parked.
* :class:`TestPollAndApply` — results become ``effective_content``, errors become failures,
  and a task the provider did not mention is left alone rather than guessed at.
* :class:`TestTheHandoffSurvivesTheWorker` — the point of the whole design: a DIFFERENT
  worker finishes what this one started.

The two commercial adapters are tested by parsing recorded payload shapes. No network, and
no API key needed to run the suite.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from jmfts_core.ingest_tasks import TASK_SUMMARIZE, TASK_SUMMARIZE_LLM
from jmfts_core.models.document import SETTLED_SETTLED, Document
from jmfts_core.models.task_queue import (
    TASK_BATCHED,
    TASK_COMPLETED,
    TASK_FAILED,
)
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.task_queue import TaskQueueRepository
from jmfts_core.rollup_tasks import METHOD_CONCATENATED, METHOD_LLM_SUMMARY
from jmfts_core.settling import NO_ROLLUP
from jmfts_core.task_errors import ErrorType

from jmfts_batch.provider import BatchRequest, custom_id_for, task_id_from
from jmfts_batch.providers.mock import MockBatchProvider
from jmfts_batch.worker import BatchWorker
from tests.conftest import _borrowed_session

#: Long enough that concatenating two children blows the embedding window. Same trick as
#: ``tests/test_rollup_pelt.py`` — the real tokenizer decides, not a stubbed one.
LONG = "Retrieval quality is measured against a baseline. " * 900

#: What every stand-in model returns, so a test can assert on the stored text.
SUMMARY = "The span describes how retrieval quality is measured against a baseline."

BADGE = "llm"


@pytest.fixture(autouse=True)
def routing_policy(monkeypatch):
    """Turn the routing policy on for this module.

    ``BatchWorker`` refuses to start without one, and it is right to: ``claim_next``
    filters by badge and knows nothing about ``task_type``, so an un-badged fleet would
    hand a batch worker ``probe`` rows to summarize.
    """
    import jmfts_core.task_routing as routing

    class _Settings:
        task_badges = {TASK_SUMMARIZE_LLM: BADGE, TASK_SUMMARIZE: "embed"}

    monkeypatch.setattr(routing, "get_settings", lambda: _Settings())


@pytest.fixture
def store(tmp_path) -> Path:
    """The mock provider's scratch directory. A PVC mount in a deployment."""
    return tmp_path / "batches"


def _tree(session, *, text: str = LONG, children: int = 2) -> Document:
    """A parent whose children's combined text is over the embedding window."""
    repo = DocumentRepository(session)
    parent = repo.create(title="parent", content=None, auto_embed=False, settled=SETTLED_SETTLED)
    for index in range(children):
        repo.create(
            title=f"child {index}",
            content=f"{text} {index}",
            parent_id=parent.id,
            auto_embed=False,
            sequential=True,
            settled=SETTLED_SETTLED,
        )
    session.flush()
    return parent


def _worker(session, provider, *, worker_id="batcher-1", **kwargs) -> BatchWorker:
    kwargs.setdefault("planner", NO_ROLLUP)
    return BatchWorker(
        provider,
        model="test-model",
        worker_id=worker_id,
        session_factory=lambda: _borrowed_session(session),
        service_badges=[BADGE],
        **kwargs,
    )


def _enqueue(session, parent) -> int:
    task = TaskQueueRepository(session).enqueue(TASK_SUMMARIZE_LLM, parent.id, "self")
    session.flush()
    return task.id


@dataclass
class _StandInTask:
    """A claimed queue row, without the queue. The handlers read only these three."""

    scope_document_id: int
    params: dict = field(default_factory=dict)
    task_type: str = TASK_SUMMARIZE_LLM


class _Chat:
    """A stand-in model that counts its calls."""

    def __init__(self, answer=SUMMARY):
        self.answer = answer
        self.calls: list[BatchRequest] = []

    def __call__(self, request: BatchRequest) -> str:
        self.calls.append(request)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


# ---------------------------------------------------------------------------
# The mock provider
# ---------------------------------------------------------------------------


class TestTheMockProvider:
    def test_submitting_does_not_call_the_model(self, store):
        """The property the whole mock exists for.

        A provider that answered at submit time would collapse the ``batched`` window to
        nothing, and every failure this design was built to survive would be unreachable
        from a test.
        """
        chat = _Chat()
        provider = MockBatchProvider(store, chat)

        batch_id = provider.submit([_request("task-1")])

        assert chat.calls == []
        assert provider.poll(batch_id).ready is False

    def test_finalizing_is_what_calls_the_model(self, store):
        chat = _Chat()
        provider = MockBatchProvider(store, chat)
        batch_id = provider.submit([_request("task-1"), _request("task-2")])

        provider.finalize(batch_id)
        status = provider.poll(batch_id)

        assert status.ready is True
        assert [call.custom_id for call in chat.calls] == ["task-1", "task-2"]
        assert [r.text for r in provider.results(batch_id)] == [SUMMARY, SUMMARY]

    def test_the_model_runs_once_however_often_it_is_polled(self, store):
        """Results are written once and read from disk after that. A poll loop that
        re-ran the batch would pay for the same answers on every pass."""
        chat = _Chat()
        provider = MockBatchProvider(store, chat)
        batch_id = provider.submit([_request("task-1")])
        provider.finalize(batch_id)

        for _ in range(3):
            assert provider.poll(batch_id).ready is True

        assert len(chat.calls) == 1

    def test_a_release_delay_finalizes_without_anyone_asking(self, store):
        chat = _Chat()
        provider = MockBatchProvider(store, chat)
        batch_id = provider.submit([_request("task-1")])

        provider.release_after(batch_id, 0.0)

        assert provider.poll(batch_id).ready is True
        assert len(chat.calls) == 1

    def test_one_failing_request_is_a_result_not_a_dead_batch(self, store):
        """Both real providers report per-request outcomes. The mock does too, so the
        worker's handling of a mixed batch is exercised without a live provider."""
        provider = MockBatchProvider(store, _Chat(answer=RuntimeError("model refused")))
        batch_id = provider.submit([_request("task-1")])
        provider.finalize(batch_id)

        # `poll` is what runs the batch; `results` is only valid after it reports ready.
        assert provider.poll(batch_id).dead is False
        results = list(provider.results(batch_id))

        assert results[0].text is None
        assert "model refused" in results[0].error
        assert results[0].error_type is ErrorType.RETRYABLE

    def test_a_cancelled_batch_is_dead(self, store):
        provider = MockBatchProvider(store, _Chat())
        batch_id = provider.submit([_request("task-1")])

        provider.cancel(batch_id)

        assert provider.poll(batch_id).dead is True

    def test_an_empty_batch_is_refused(self, store):
        with pytest.raises(ValueError, match="empty batch"):
            MockBatchProvider(store, _Chat()).submit([])

    def test_an_unknown_batch_id_raises(self, store):
        provider = MockBatchProvider(store, _Chat())
        with pytest.raises(ValueError, match="no such batch"):
            provider.poll("mockbatch_deadbeef")


class TestCustomIds:
    def test_a_task_id_round_trips(self):
        assert task_id_from(custom_id_for(4321)) == 4321

    def test_a_foreign_custom_id_is_refused(self):
        """Applying one node's summary to another because an id looked parseable is the
        worst outcome available here, so the prefix is required rather than stripped."""
        with pytest.raises(ValueError, match="did not come from a JMFTS submission"):
            task_id_from("request-17")

    def test_an_id_anthropic_would_reject_is_caught_at_construction(self):
        """One bad id fails the whole batch at Anthropic, so it is caught before submit."""
        with pytest.raises(ValueError, match="does not match"):
            _request("task 1 with spaces")


# ---------------------------------------------------------------------------
# Gather and submit
# ---------------------------------------------------------------------------


class TestGatherAndSubmit:
    def test_a_gathered_task_becomes_batched_against_the_provider_id(self, db_session, store):
        chat = _Chat()
        provider = MockBatchProvider(store, chat)
        parent = _tree(db_session)
        task_id = _enqueue(db_session, parent)

        batch_id = _worker(db_session, provider).gather_and_submit()

        task = TaskQueueRepository(db_session).get(task_id)
        assert batch_id is not None
        assert task.status == TASK_BATCHED
        assert task.batch_id == batch_id
        assert task.batched_at is not None
        # Nothing has been answered yet. The task is parked, not running.
        assert chat.calls == []

    def test_a_batched_task_is_not_claimable_by_the_direct_worker(self, db_session, store):
        """The premise. Once it is shipped it is paid for."""
        provider = MockBatchProvider(store, _Chat())
        parent = _tree(db_session)
        _enqueue(db_session, parent)
        _worker(db_session, provider).gather_and_submit()

        assert TaskQueueRepository(db_session).claim_next("direct-llm-worker") is None

    def test_a_node_that_now_fits_is_completed_instead_of_batched(self, db_session, store):
        """A node deferred to ``summarize:llm`` can lose children before a batch worker
        reaches it. Concatenating is better on both cost and fidelity, so it wins."""
        provider = MockBatchProvider(store, _Chat())
        parent = _tree(db_session, text="a short passage about retrieval")
        task_id = _enqueue(db_session, parent)

        batch_id = _worker(db_session, provider).gather_and_submit()

        task = TaskQueueRepository(db_session).get(task_id)
        assert batch_id is None
        assert task.status == TASK_COMPLETED
        node = db_session.get(Document, parent.id)
        assert node.structured_content["effective_content"]["method"] == METHOD_CONCATENATED

    def test_the_gather_size_bounds_one_batch(self, db_session, store):
        """Not the provider's cap. Every gathered task holds its node's reservation for
        the life of the batch, so the limit that binds is the tree, not the API."""
        provider = MockBatchProvider(store, _Chat())
        for _ in range(4):
            _enqueue(db_session, _tree(db_session))

        _worker(db_session, provider, gather_size=2).gather_and_submit()

        tasks = TaskQueueRepository(db_session)
        assert len(tasks.batched_tasks(tasks.outstanding_batches()[0])) == 2

    def test_a_failed_submission_fails_the_tasks_with_the_provider_error(self, db_session, store):
        """Left for the lease they would be recorded as a timeout, losing the provider's
        actual complaint — the only thing that says whether the batch will ever work."""

        class _Refuses(MockBatchProvider):
            def submit(self, requests):
                raise RuntimeError("provider says no")

        parent = _tree(db_session)
        task_id = _enqueue(db_session, parent)

        batch_id = _worker(db_session, _Refuses(store, _Chat())).gather_and_submit()

        task = TaskQueueRepository(db_session).get(task_id)
        assert batch_id is None
        assert task.status == TASK_FAILED
        assert "provider says no" in task.error
        assert TaskQueueRepository(db_session).outstanding_batches() == []

    def test_it_refuses_to_run_without_a_badge(self, db_session, store):
        """An un-badged worker claims every task type in the queue, and this one can only
        run summarize:llm."""
        with pytest.raises(ValueError, match="needs at least one service badge"):
            BatchWorker(
                MockBatchProvider(store, _Chat()),
                model="test-model",
                worker_id="w",
                service_badges=None,
            )

    def test_it_refuses_to_run_when_the_routing_policy_is_off(self, db_session, store, monkeypatch):
        """The default configuration. Without a policy every task is un-badged, and an
        un-badged task is claimable by anyone — including this worker."""
        import jmfts_core.task_routing as routing

        class _NoPolicy:
            task_badges: dict = {}

        monkeypatch.setattr(routing, "get_settings", lambda: _NoPolicy())

        with pytest.raises(ValueError, match="Set JMFTS_TASK_BADGES"):
            BatchWorker(
                MockBatchProvider(store, _Chat()),
                model="test-model",
                worker_id="w",
                service_badges=[BADGE],
            )

    def test_it_refuses_a_gather_size_over_the_provider_cap(self, db_session, store):
        provider = MockBatchProvider(store, _Chat(), max_requests=10)
        with pytest.raises(ValueError, match="exceeds mock's cap"):
            _worker(db_session, provider, gather_size=11)


# ---------------------------------------------------------------------------
# Poll and apply
# ---------------------------------------------------------------------------


class TestPollAndApply:
    def test_a_finished_batch_writes_effective_content(self, db_session, store):
        provider = MockBatchProvider(store, _Chat())
        parent = _tree(db_session)
        task_id = _enqueue(db_session, parent)
        worker = _worker(db_session, provider)
        batch_id = worker.gather_and_submit()
        provider.finalize(batch_id)

        delivered = worker.poll_once()

        task = TaskQueueRepository(db_session).get(task_id)
        record = db_session.get(Document, parent.id).structured_content["effective_content"]
        assert delivered == 1
        assert task.status == TASK_COMPLETED
        assert record["method"] == METHOD_LLM_SUMMARY
        assert record["text"] == SUMMARY

    def test_the_record_says_which_provider_and_which_batch(self, db_session, store):
        """Provenance, in the attempt record rather than only on the queue row: the row is
        purgeable and the record is what a person reads a year later."""
        provider = MockBatchProvider(store, _Chat())
        parent = _tree(db_session)
        _enqueue(db_session, parent)
        worker = _worker(db_session, provider)
        batch_id = worker.gather_and_submit()
        provider.finalize(batch_id)
        worker.poll_once()

        record = db_session.get(Document, parent.id).structured_content["effective_content"]
        assert record["provider"] == "mock"
        assert record["batch_id"] == batch_id

    def test_an_unfinished_batch_delivers_nothing_and_leaves_the_task_parked(
        self, db_session, store
    ):
        provider = MockBatchProvider(store, _Chat())
        parent = _tree(db_session)
        task_id = _enqueue(db_session, parent)
        worker = _worker(db_session, provider)
        worker.gather_and_submit()

        assert worker.poll_once() == 0
        assert TaskQueueRepository(db_session).get(task_id).status == TASK_BATCHED

    def test_a_per_request_error_fails_that_task(self, db_session, store):
        provider = MockBatchProvider(store, _Chat(answer=RuntimeError("model refused")))
        parent = _tree(db_session)
        task_id = _enqueue(db_session, parent)
        worker = _worker(db_session, provider)
        batch_id = worker.gather_and_submit()
        provider.finalize(batch_id)

        worker.poll_once()

        task = TaskQueueRepository(db_session).get(task_id)
        assert task.status == TASK_FAILED
        assert task.error_type == ErrorType.RETRYABLE.value
        assert "model refused" in task.error

    def test_a_summary_over_the_window_is_a_permanent_failure(self, db_session, store):
        """The core handler's rule, unchanged by the wall. Storing it would leave the node
        advertising an ``effective_content`` no query can reach."""
        provider = MockBatchProvider(store, _Chat(answer=LONG))
        parent = _tree(db_session)
        task_id = _enqueue(db_session, parent)
        worker = _worker(db_session, provider)
        batch_id = worker.gather_and_submit()
        provider.finalize(batch_id)

        worker.poll_once()

        task = TaskQueueRepository(db_session).get(task_id)
        assert task.status == TASK_FAILED
        assert task.error_type == ErrorType.PERMANENT.value
        assert "did not summarize" in task.error

    def test_a_dead_batch_resolves_every_task_parked_against_it(self, db_session, store):
        """A cancelled or expired batch will never produce results. Waiting is not a state
        those tasks can leave, and nothing else in the queue can see them."""
        provider = MockBatchProvider(store, _Chat())
        parent = _tree(db_session)
        task_id = _enqueue(db_session, parent)
        worker = _worker(db_session, provider)
        batch_id = worker.gather_and_submit()
        provider.cancel(batch_id)

        worker.poll_once()

        task = TaskQueueRepository(db_session).get(task_id)
        assert task.status == TASK_FAILED
        assert task.error_type == ErrorType.RETRYABLE.value

    def test_a_result_for_an_unknown_task_is_skipped_not_applied(self, db_session, store):
        """Applying a summary to a node it was not written for is the worst failure
        available here, so an unrecognised id is dropped with a log line."""
        provider = MockBatchProvider(store, _Chat())
        parent = _tree(db_session)
        task_id = _enqueue(db_session, parent)
        worker = _worker(db_session, provider)
        batch_id = worker.gather_and_submit()
        provider.finalize(batch_id)

        # Rewrite the results so the custom_id names a task that is not parked here.
        results = store / batch_id / "results.jsonl"
        results.write_text(
            json.dumps({"custom_id": custom_id_for(999_999), "text": SUMMARY}) + "\n"
        )

        assert worker.poll_once() == 0
        assert TaskQueueRepository(db_session).get(task_id).status == TASK_BATCHED

    def test_stalled_batches_are_reported_never_acted_on(self, db_session, store):
        """Deciding a paid-for answer is not coming is not this worker's call."""
        provider = MockBatchProvider(store, _Chat())
        _enqueue(db_session, _tree(db_session))
        worker = _worker(db_session, provider, stall_seconds=0.0)
        worker.gather_and_submit()
        db_session.commit()

        stalled = worker.stalled()

        assert len(stalled) == 1
        assert stalled[0].status == TASK_BATCHED


# ---------------------------------------------------------------------------
# The handoff
# ---------------------------------------------------------------------------


class TestTheHandoffSurvivesTheWorker:
    def test_a_different_worker_finishes_the_batch(self, db_session, store):
        """The reason ``batched`` is a column and not a field on the claim.

        A claim is broken by the lease on purpose — that is how a dead worker's work gets
        run again. The 'this content has been shipped' fact must therefore NOT live in the
        claim, or the recovery mechanism would spend the money twice.
        """
        provider = MockBatchProvider(store, _Chat())
        parent = _tree(db_session)
        task_id = _enqueue(db_session, parent)

        submitter = _worker(db_session, provider, worker_id="worker-that-dies")
        batch_id = submitter.gather_and_submit()
        del submitter  # the pod is gone; nothing of it remains but the row

        provider.finalize(batch_id)
        adopter = _worker(db_session, provider, worker_id="worker-that-arrives-later")
        delivered = adopter.poll_once()

        task = TaskQueueRepository(db_session).get(task_id)
        assert delivered == 1
        assert task.status == TASK_COMPLETED
        assert task.batch_id == batch_id

    def test_the_lease_does_not_reap_a_batched_task(self, db_session, store):
        """``batched`` is excluded from the reapable statuses, and this is why. Nothing
        beats for a parked row — the work is at the provider, not in a worker — so a lease
        that could see it would requeue an answer already bought."""
        provider = MockBatchProvider(store, _Chat())
        _enqueue(db_session, _tree(db_session))
        _worker(db_session, provider).gather_and_submit()
        db_session.commit()

        requeued = TaskQueueRepository(db_session).requeue_expired_claims(lease_seconds=0.0)

        assert requeued == []

    def test_two_workers_do_not_both_apply_one_batch(self, db_session, store):
        """Any worker may adopt a batch, so two can reach the same one. The batch lock is
        what stops both writing its results and racing on the attempt log."""
        provider = MockBatchProvider(store, _Chat())
        parent = _tree(db_session)
        task_id = _enqueue(db_session, parent)
        first = _worker(db_session, provider, worker_id="first")
        batch_id = first.gather_and_submit()
        provider.finalize(batch_id)

        assert first.poll_once() == 1
        # The second pass finds nothing outstanding: the batch is no longer parked.
        second = _worker(db_session, provider, worker_id="second")
        assert second.poll_once() == 0
        assert TaskQueueRepository(db_session).get(task_id).status == TASK_COMPLETED


# ---------------------------------------------------------------------------
# Drift between this package and the core handler
# ---------------------------------------------------------------------------


class TestMirrorsTheCoreHandler:
    def test_the_batch_path_stores_what_the_direct_path_stores(
        self, db_session, store, monkeypatch
    ):
        """``jmfts_batch.summarize`` is a copy of ``run_summarize_llm`` split in half, and
        a copy drifts. This is the test that says so.

        If ``run_summarize_llm`` changes what it derives, embeds or records, this fails and
        ``jmfts_batch/summarize.py`` has to change with it. The two paths differ only in
        the provenance the batch adds and the input measurement it cannot carry across the
        wall — everything about the summary itself has to match.
        """
        import jmfts_core.rollup_tasks as rollup
        from jmfts_core.config import get_settings

        # The direct path reports `skipped` when no LLM is configured, and the shipped
        # default configures none. `summarize_span` is replaced below, so nothing connects
        # to this address — it exists to get past that guard.
        configured = get_settings().model_copy()
        configured.llm_base_url = "http://llm.invalid:8000"
        configured.llm_model = "a-model-that-is-never-called"
        monkeypatch.setattr(rollup, "get_settings", lambda: configured)

        monkeyed = _tree(db_session)
        batched = _tree(db_session)

        # Direct path. Driven with a stand-in row rather than a queued one, the way
        # `tests/test_rollup_pelt.py` drives it: a real pending row here would be claimed
        # by the batch worker below, which would then find the node already summarized,
        # concatenate that summary and overwrite the record under test.
        original = rollup.summarize_span
        rollup.summarize_span = lambda text, settings, model: SUMMARY
        try:
            rollup.run_summarize_llm(db_session, _StandInTask(monkeyed.id))
        finally:
            rollup.summarize_span = original

        # Batch path.
        provider = MockBatchProvider(store, _Chat())
        _enqueue(db_session, batched)
        worker = _worker(db_session, provider)
        batch_id = worker.gather_and_submit()
        provider.finalize(batch_id)
        worker.poll_once()

        direct = db_session.get(Document, monkeyed.id).structured_content["effective_content"]
        through_batch = db_session.get(Document, batched.id).structured_content["effective_content"]

        shared = ("method", "source_children", "text", "tokens", "window")
        assert {k: direct[k] for k in shared} == {k: through_batch[k] for k in shared}
        # And the embedding itself, which is the thing retrieval actually uses.
        assert db_session.get(Document, monkeyed.id).embed is not None
        assert db_session.get(Document, batched.id).embed is not None


# ---------------------------------------------------------------------------
# The commercial adapters, parsed rather than called
# ---------------------------------------------------------------------------


class TestOpenAIAdapter:
    def test_a_request_puts_the_system_prompt_in_the_messages(self):
        from jmfts_batch.providers.openai_api import _as_line

        line = _as_line(_request("task-7"))

        assert line["custom_id"] == "task-7"
        assert line["url"] == "/v1/chat/completions"
        assert line["body"]["messages"][0]["role"] == "system"

    def test_a_successful_line_becomes_text(self):
        from jmfts_batch.providers.openai_api import _as_result

        result = _as_result(
            {
                "custom_id": "task-7",
                "error": None,
                "response": {
                    "status_code": 200,
                    "body": {"choices": [{"message": {"role": "assistant", "content": SUMMARY}}]},
                },
            }
        )

        assert result.text == SUMMARY

    @pytest.mark.parametrize(
        "status_code,expected",
        [
            (408, ErrorType.TIMEOUT),
            (429, ErrorType.RETRYABLE),
            (503, ErrorType.RETRYABLE),
            (400, ErrorType.PERMANENT),
        ],
    )
    def test_status_codes_map_to_the_retry_policy(self, status_code, expected):
        from jmfts_batch.providers.openai_api import _as_result

        result = _as_result(
            {"custom_id": "task-7", "response": {"status_code": status_code, "body": {}}}
        )

        assert result.error_type is expected

    def test_an_error_file_line_is_permanent(self):
        """It never reached the model, and the usual cause is the request itself."""
        from jmfts_batch.providers.openai_api import _as_result

        result = _as_result(
            {"custom_id": "task-7", "error": {"code": "invalid_request", "message": "bad"}}
        )

        assert result.error_type is ErrorType.PERMANENT


class TestAnthropicAdapter:
    def test_a_request_puts_the_system_prompt_beside_the_messages(self):
        from jmfts_batch.providers.anthropic_api import _as_request

        entry = _as_request(_request("task-7"))

        assert entry["custom_id"] == "task-7"
        assert entry["params"]["system"]
        assert [m["role"] for m in entry["params"]["messages"]] == ["user"]

    def test_a_succeeded_result_becomes_text(self):
        from jmfts_batch.providers.anthropic_api import _as_result

        result = _as_result(
            {
                "custom_id": "task-7",
                "result": {
                    "type": "succeeded",
                    "message": {"id": "msg_1", "content": [{"type": "text", "text": SUMMARY}]},
                },
            }
        )

        assert result.text == SUMMARY

    @pytest.mark.parametrize(
        "result_type,expected",
        [("canceled", ErrorType.RETRYABLE), ("expired", ErrorType.RETRYABLE)],
    )
    def test_unbilled_outcomes_are_retryable(self, result_type, expected):
        """Anthropic states these are not billed, so requeueing them is free and right."""
        from jmfts_batch.providers.anthropic_api import _as_result

        result = _as_result({"custom_id": "task-7", "result": {"type": result_type}})

        assert result.error_type is expected

    def test_an_invalid_request_is_permanent(self):
        from jmfts_batch.providers.anthropic_api import _as_result

        result = _as_result(
            {
                "custom_id": "task-7",
                "result": {
                    "type": "errored",
                    "error": {"error": {"type": "invalid_request_error", "message": "too long"}},
                },
            }
        )

        assert result.error_type is ErrorType.PERMANENT

    def test_an_unknown_result_type_raises(self):
        """Guessing whether an outcome we have never seen should be retried is a guess
        about money."""
        from jmfts_batch.providers.anthropic_api import _as_result

        with pytest.raises(ValueError, match="unknown type"):
            _as_result({"custom_id": "task-7", "result": {"type": "invented"}})


def _request(custom_id: str) -> BatchRequest:
    return BatchRequest(
        custom_id=custom_id,
        model="test-model",
        system="summarize this",
        user="some text",
        max_tokens=256,
        temperature=0.2,
    )
