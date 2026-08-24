"""The one door from JMFTS to an LLM endpoint (``jmfts_core/llm_client.py``).

Four call sites share this transport, and three properties of it are worth a test of
their own rather than four copies inside the callers' suites: the URL it builds, the
credential it sends, and the fact that one of the four callers is synchronous.

No database and no optional dependency — everything here runs against
``tests/llm_stub.py`` on loopback.
"""

from __future__ import annotations

import asyncio

import pytest

from jmfts_core import llm_client, rollup_tasks
from jmfts_core.config import LlmNotConfiguredError, Settings
from tests.llm_stub import llm_stub


class TestEndpoint:
    def test_the_v1_prefix_is_appended_once(self):
        """``require_llm`` returns a server root; ``/v1/chat/completions`` is built here.

        The four call sites each used to write the whole path into an f-string. One of
        them appending ``/v1`` to a base URL that already carried it would have produced
        ``/v1/v1/chat/completions`` and a 404 — the failure
        ``test_ensonet_openai_compat`` documents at length. There is one construction site
        now, and this is it.
        """
        with llm_stub() as stub:
            settings = stub.settings()
            base_url, model = settings.require_llm("test")
            assert not base_url.endswith("/v1")

            asyncio.run(
                llm_client.complete(
                    settings=settings,
                    base_url=base_url,
                    model=model,
                    messages=[{"role": "user", "content": "hi"}],
                    max_tokens=64,
                    temperature=0.0,
                )
            )

        assert [r.path for r in stub.requests] == ["/v1/chat/completions"]

    def test_a_configured_key_is_sent_as_the_bearer(self):
        with llm_stub() as stub:
            settings = stub.settings(llm_api_key="s3cret")
            base_url, model = settings.require_llm("test")
            asyncio.run(
                llm_client.complete(
                    settings=settings,
                    base_url=base_url,
                    model=model,
                    messages=[{"role": "user", "content": "hi"}],
                    max_tokens=64,
                    temperature=0.0,
                )
            )

        assert stub.requests[0].authorization == "Bearer s3cret"

    def test_no_configured_key_sends_the_sentinel(self):
        """τ refuses to send a request with no credential; the sentinel is its spelling
        of "this endpoint wants none".

        The hand-written call sites sent no ``Authorization`` header at all in this case.
        A keyless local server ignores the header either way, and pinning the value here
        is what makes that a decision on the record rather than a surprise on the wire.
        """
        with llm_stub() as stub:
            settings = stub.settings()
            assert settings.llm_api_key == ""
            base_url, model = settings.require_llm("test")
            asyncio.run(
                llm_client.complete(
                    settings=settings,
                    base_url=base_url,
                    model=model,
                    messages=[{"role": "user", "content": "hi"}],
                    max_tokens=64,
                    temperature=0.0,
                )
            )

        assert stub.requests[0].authorization == f"Bearer {llm_client.NO_KEY_SENTINEL}"

    def test_max_tokens_reaches_the_wire(self):
        """``Model.max_tokens`` rather than a body field, so ``tau_llm.compat`` picks the
        spelling the endpoint accepts. An unrecognised endpoint — every local server —
        keeps ``max_tokens``, which is what the hand-written call sites sent.
        """
        with llm_stub() as stub:
            settings = stub.settings()
            base_url, model = settings.require_llm("test")
            asyncio.run(
                llm_client.complete(
                    settings=settings,
                    base_url=base_url,
                    model=model,
                    messages=[{"role": "user", "content": "hi"}],
                    max_tokens=1234,
                    temperature=0.25,
                )
            )

        body = stub.requests[0].body
        assert body["max_tokens"] == 1234
        assert body["temperature"] == 0.25
        # A buffered completion, not SSE — the shape the four call sites already used.
        assert body["stream"] is False


class TestSyncCallSite:
    """``rollup_tasks.summarize_span`` is synchronous and τ's client is not."""

    def test_summarize_span_completes_without_an_event_loop(self):
        with llm_stub(content="a span summary") as stub:
            settings = stub.settings()
            assert rollup_tasks.summarize_span("some span text", settings, "stub-model") == (
                "a span summary"
            )

        request = stub.requests[0]
        assert request.path == "/v1/chat/completions"
        assert request.role("user") == "some span text"
        # The whole span is sent; nothing truncates it to a character budget. That is the
        # property `summarize_span`'s docstring exists to state, and it survives the move.
        assert "chat_template_kwargs" in request.body

    def test_a_second_call_works_after_the_first_closed_its_loop(self):
        """``complete_sync`` owns a loop per call and closes τ's provider pool on it.

        Skipping that teardown leaks an ``httpx.AsyncClient`` bound to a loop that is
        about to close — a socket per call, and a pool entry that can never be reused. A
        worker draining a queue makes this call thousands of times, so "it worked once"
        is not the question.
        """
        with llm_stub(content="ok") as stub:
            settings = stub.settings()
            for _ in range(3):
                assert rollup_tasks.summarize_span("text", settings, "stub-model") == "ok"

        assert len(stub.requests) == 3

    def test_calling_it_from_a_running_loop_is_refused_by_name(self):
        """Rather than deadlocking or reporting an asyncio internal.

        Nothing in the tree does this today — the ingest worker is a plain thread — but
        the async sibling is one line away and the error has to say so.
        """

        async def inside() -> None:
            llm_client.complete_sync(
                settings=Settings(llm_base_url="http://127.0.0.1:1", llm_model="m"),
                base_url="http://127.0.0.1:1",
                model="m",
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=8,
                temperature=0.0,
            )

        with pytest.raises(RuntimeError, match="complete\\(\\)"):
            asyncio.run(inside())


class TestBlankConfigurationStillMeansWhatItMeant:
    """``JMFTS_LLM_*`` blank: everything except the four LLM paths works.

    The endpoint decision stayed in ``Settings.require_llm`` across the migration, so the
    unconfigured case must still raise before any transport is built — naming the
    variable, not failing at a socket with an empty host.
    """

    BLANK = dict(llm_base_url="", llm_model="", ensonet_url="", ensonet_model="")

    def test_settings_report_themselves_unconfigured(self):
        assert Settings(**self.BLANK).llm_configured is False

    @pytest.mark.parametrize(
        "what", ["Synthesis", "RAPTOR summarization", "Fact extraction", "Span summarization"]
    )
    def test_every_llm_feature_names_the_missing_variable(self, what):
        with pytest.raises(LlmNotConfiguredError, match="JMFTS_LLM_BASE_URL"):
            Settings(**self.BLANK).require_llm(what)

    def test_span_summarization_refuses_before_it_builds_a_request(self):
        with pytest.raises(LlmNotConfiguredError):
            rollup_tasks.summarize_span("text", Settings(**self.BLANK), "")
