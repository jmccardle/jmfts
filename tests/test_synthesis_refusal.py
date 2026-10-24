"""``POST /search/synthesize`` says WHY the LLM did not answer. Forgejo john/jmfts #32.

Until 0.6.2 every failure of the synthesis call — no endpoint configured, a refused
connection, an endpoint answering ``503 ... is loading`` — came back as the same
``llm_available: false`` with the same placeholder sentence. The caller could not tell
"set ``JMFTS_LLM_BASE_URL``" from "the server said 503", and on midlife the first request
after a cutover read as "no LLM" while the model was loading behind a configured endpoint.

Two properties, each tested here against ``tests/llm_stub.py`` on loopback:

* the response carries ``llm_error``, which keeps "not configured" (no request was made)
  apart from "refused" (the endpoint answered with a status) and "failed" (no status came
  back at all);
* a retryable status (502, 503, 504) is retried inside a small bounded budget, and nothing
  else is — an over-context 400 is a statement about the request and goes back at once.

No database: the retrieval half is replaced with an empty result set, because what is
under test is the LLM half and the shape of what it reports.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from jmfts_client.contracts.search import SynthesizeRequest, SynthesizeResponse
from jmfts_core import llm_client, synthesis
from jmfts_core.config import Settings
from jmfts_core.services import search_service as module
from tests.llm_stub import llm_stub

LOADING = "qwen36-moe-35b is loading (started 131615s ago)"


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch):
    """The schedule's SHAPE is under test, not its wall-clock length."""
    monkeypatch.setattr(synthesis, "RETRY_FIRST_DELAY_S", 0.001)


def _synthesize(monkeypatch, settings: Settings) -> SynthesizeResponse:
    repo = MagicMock()
    repo.hybrid_search.return_value = []
    monkeypatch.setattr(module, "SearchRepository", lambda session: repo)
    monkeypatch.setattr(module.SearchService, "_resolve_context", lambda self, c, o: {})
    monkeypatch.setattr(module.SearchService, "_projected_text", lambda self, results: {})
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(synthesis, "get_settings", lambda: settings)

    service = module.SearchService(MagicMock())
    return asyncio.run(
        service.synthesize_search(
            request=SynthesizeRequest(query="what", search_method="hybrid", top_k=3),
            context=None,
        )
    )


def _unconfigured() -> Settings:
    return Settings(llm_base_url="", llm_model="", ensonet_url="", ensonet_model="")


class TestRefusalIsNotAbsence:
    def test_a_503_is_distinguishable_from_not_configured(self, monkeypatch):
        """The entry condition of #32: a refusal and an absence read differently."""
        absent = _synthesize(monkeypatch, _unconfigured())

        with llm_stub(status=503, error_message=LOADING) as stub:
            refused = _synthesize(monkeypatch, stub.settings(synthesis_retry_budget=0))

        assert absent.llm_available is False and refused.llm_available is False
        assert absent.llm_error is not None and refused.llm_error is not None

        assert absent.llm_error.reason == "not_configured"
        assert absent.llm_error.status is None
        assert absent.llm_error.attempts == 0
        assert "JMFTS_LLM_BASE_URL" in absent.llm_error.message

        assert refused.llm_error.reason == "refused"
        assert refused.llm_error.status == 503
        assert "is loading" in refused.llm_error.message
        assert refused.llm_error.attempts == 1

    def test_a_success_carries_no_error(self, monkeypatch):
        with llm_stub(content="an answer") as stub:
            response = _synthesize(monkeypatch, stub.settings())
        assert response.llm_available is True
        assert response.llm_error is None
        assert response.synthesis == "an answer"

    def test_an_over_context_400_is_reported_and_not_retried(self, monkeypatch):
        """Not retryable — the same documents are just as large on the second attempt — but
        no longer an unexplained ``llm_available: false`` either. (#8 is the fix.)"""
        too_big = "the request exceeds the available context size"
        with llm_stub(status=400, error_message=too_big) as stub:
            response = _synthesize(monkeypatch, stub.settings(synthesis_retry_budget=10.0))
            requests = len(stub.requests)

        assert requests == 1
        assert response.llm_error.reason == "refused"
        assert response.llm_error.status == 400
        assert too_big in response.llm_error.message
        assert response.llm_error.attempts == 1

    def test_no_response_at_all_is_failed_not_refused(self, monkeypatch):
        """A refused CONNECTION has no status. It is configured, so not ``not_configured``;
        nothing answered, so not ``refused``."""
        with llm_stub() as stub:
            settings = stub.settings()
        # The context manager has closed the socket: the port now refuses connections.
        response = _synthesize(monkeypatch, settings)

        assert response.llm_error.reason == "failed"
        assert response.llm_error.status is None
        assert response.llm_error.attempts == 1


class TestBoundedRetry:
    def test_a_503_then_a_200_answers(self, monkeypatch):
        """The midlife case: the model finishes loading while JMFTS is still waiting."""
        with llm_stub(status=(503, 200), content="loaded now", error_message=LOADING) as stub:
            response = _synthesize(monkeypatch, stub.settings(synthesis_retry_budget=4.0))
            requests = len(stub.requests)

        assert requests == 2
        assert response.llm_available is True
        assert response.llm_error is None
        assert response.synthesis == "loaded now"

    def test_a_persistent_503_stops_inside_the_budget(self, monkeypatch):
        """Doubling from the first delay: 1, 2, 4 ms fit in 7.5 ms and 8 more do not — so
        four attempts, and the response says four."""
        with llm_stub(status=503, error_message=LOADING) as stub:
            response = _synthesize(monkeypatch, stub.settings(synthesis_retry_budget=0.0075))
            requests = len(stub.requests)

        assert requests == 4
        assert response.llm_error.attempts == 4
        assert response.llm_error.status == 503

    @pytest.mark.parametrize("status", [502, 504])
    def test_the_other_gateway_statuses_retry(self, monkeypatch, status):
        with llm_stub(status=(status, 200)) as stub:
            response = _synthesize(monkeypatch, stub.settings(synthesis_retry_budget=4.0))
        assert response.llm_available is True

    @pytest.mark.parametrize("status", [400, 401, 404, 429, 500])
    def test_nothing_else_retries(self, monkeypatch, status):
        with llm_stub(status=(status, 200)) as stub:
            response = _synthesize(monkeypatch, stub.settings(synthesis_retry_budget=4.0))
            requests = len(stub.requests)
        assert requests == 1
        assert response.llm_error.status == status

    def test_a_zero_budget_never_retries(self, monkeypatch):
        with llm_stub(status=(503, 200)) as stub:
            response = _synthesize(monkeypatch, stub.settings(synthesis_retry_budget=0))
            requests = len(stub.requests)
        assert requests == 1
        assert response.llm_error.status == 503


class TestTheTypedError:
    """``llm_client.complete`` turns τ's bare ``Exception`` into :class:`LlmCallError`.

    τ 0.14 raises ``ProviderHttpError`` carrying ``status_code``; a refused connection is
    ``ProviderConnectionError``. These are the tests that fail if τ's types move — rather
    than every status silently becoming ``None`` and every refusal reading as ``failed``.
    """

    def _call(self, settings: Settings):
        base_url, model = settings.require_llm("test")
        return asyncio.run(
            llm_client.complete(
                settings=settings,
                base_url=base_url,
                model=model,
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=8,
                temperature=0.0,
            )
        )

    def test_a_non_200_carries_its_status(self):
        with llm_stub(status=503, error_message=LOADING) as stub:
            with pytest.raises(llm_client.LlmCallError) as caught:
                self._call(stub.settings())
        assert caught.value.status == 503
        assert LOADING in str(caught.value)

    def test_a_refused_connection_carries_none(self):
        with llm_stub() as stub:
            settings = stub.settings()
        with pytest.raises(llm_client.LlmCallError) as caught:
            self._call(settings)
        assert caught.value.status is None

    def test_a_context_length_refusal_fails_a_queued_task_on_its_first_attempt(self):
        """Forgejo #8, end to end from the wire: the 400 a llama-server sends for an
        over-long prompt reaches ``classify_exception`` as PERMANENT, so the task queue
        settles the node instead of spending its retry budget on a request that is refused
        identically every time. One request, because ``llm_client`` turns τ's own retry off."""
        from jmfts_core.task_errors import ErrorType, classify_exception

        too_long = "the request (9000 tokens) exceeds the available context size (8192)"
        with llm_stub(status=400, error_message=too_long) as stub:
            with pytest.raises(llm_client.LlmCallError) as caught:
                self._call(stub.settings())
            requests = len(stub.requests)
        assert requests == 1
        assert caught.value.status == 400
        assert classify_exception(caught.value) is ErrorType.PERMANENT
