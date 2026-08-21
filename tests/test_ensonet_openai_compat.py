"""OpenAI-compatibility test suite for ensonet.

Validates that the ensonet server (or any OpenAI-compatible endpoint configured
via JMFTS settings) correctly implements the OpenAI Chat Completions API.

Architecture note
-----------------
JMFTS consumes LLMs exclusively through the OpenAI REST API contract
(POST /v1/chat/completions, GET /v1/models).  There are no ensonet-specific
code paths in production code — ensonet is just the default value for
``effective_llm_url``.  Any OpenAI-compatible server can substitute.

What ensonet provides beyond the vanilla OpenAI API
-----------------------------------------------------
ensonet is a GPU-aware model orchestrator that sits in front of one or more
inference backends (turboquant, llama.cpp, vLLM, etc.).  Its extensions
beyond the standard OpenAI contract include:

- Cold-start model loading: loads a GGUF/GPTQ model from disk on first request,
  then keeps it resident in GPU VRAM.  The 180 s ``ensonet_timeout`` in JMFTS
  config exists specifically to survive this startup window.
- Multi-model orchestration: multiple named models can be served simultaneously
  from a single ensonet instance, each with its own GPU memory allocation.
- Service dispatch: routes completions requests to the appropriate backend
  process based on the ``model`` field.  Backend processes are managed
  transparently; callers never need to know which port an inference worker
  is listening on.
- Auto-scaling: can spawn additional backend instances under load and
  queue requests while capacity is being provisioned.
- Health and queue endpoints (ensonet-specific, not part of OpenAI spec):
  ``GET /health``, ``GET /v1/queues``, ``GET /v1/servers``.  These are
  documented here but not required by JMFTS.

Configuration
-------------
Tests read from JMFTS settings:
  JMFTS_LLM_BASE_URL  — override ensonet URL (default http://localhost:8853)
  JMFTS_LLM_MODEL     — override model name (default THUDM_GLM4_32b)
  JMFTS_LLM_TIMEOUT   — override timeout (default 180 s)

All tests are skipped when the server is unreachable.
"""

import asyncio
import time
from typing import Generator

import httpx
import pytest

from jmfts_core.config import get_settings


# ---------------------------------------------------------------------------
# Server availability
# ---------------------------------------------------------------------------


def _server_url() -> str:
    return get_settings().effective_llm_url.rstrip("/")


def _model_name() -> str:
    return get_settings().effective_llm_model


def _timeout() -> float:
    return get_settings().effective_llm_timeout


def _is_server_reachable() -> bool:
    """Return True if the LLM server responds to a basic health probe."""
    url = _server_url()
    try:
        # Try /health first (ensonet extension), fall back to /v1/models
        for path in ("/health", "/v1/models"):
            try:
                r = httpx.get(f"{url}{path}", timeout=5.0)
                if r.status_code < 500:
                    return True
            except httpx.RequestError:
                continue
    except Exception:
        pass
    return False


_SERVER_AVAILABLE = _is_server_reachable()
skip_if_no_server = pytest.mark.skipif(
    not _SERVER_AVAILABLE,
    reason=f"LLM server not reachable at {_server_url()}",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def base_url() -> str:
    return _server_url()


@pytest.fixture(scope="session")
def model() -> str:
    return _model_name()


@pytest.fixture(scope="session")
def timeout() -> float:
    return _timeout()


@pytest.fixture(scope="session")
def client(base_url: str, timeout: float) -> Generator[httpx.Client, None, None]:
    with httpx.Client(base_url=base_url, timeout=timeout) as c:
        yield c


# ---------------------------------------------------------------------------
# Helper: minimal valid chat completions payload
# ---------------------------------------------------------------------------


def _chat_payload(model: str, *, stream: bool = False, **kwargs) -> dict:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with the single word: pong"}],
        "max_tokens": 16,
        "temperature": 0.0,
        "stream": stream,
    }
    payload.update(kwargs)
    return payload


# ---------------------------------------------------------------------------
# 1. /v1/models endpoint
# ---------------------------------------------------------------------------


class TestModelsEndpoint:
    @skip_if_no_server
    def test_returns_200(self, client: httpx.Client):
        r = client.get("/v1/models")
        assert r.status_code == 200

    @skip_if_no_server
    def test_response_shape(self, client: httpx.Client):
        r = client.get("/v1/models")
        data = r.json()
        assert data.get("object") == "list"
        assert isinstance(data.get("data"), list)
        assert len(data["data"]) > 0

    @skip_if_no_server
    def test_each_model_has_required_fields(self, client: httpx.Client):
        r = client.get("/v1/models")
        for entry in r.json()["data"]:
            assert "id" in entry, f"model entry missing 'id': {entry}"
            assert "object" in entry, f"model entry missing 'object': {entry}"

    @skip_if_no_server
    def test_configured_model_present(self, client: httpx.Client, model: str):
        r = client.get("/v1/models")
        ids = [m["id"] for m in r.json()["data"]]
        assert model in ids, (
            f"Configured model '{model}' not found in /v1/models response: {ids}"
        )


# ---------------------------------------------------------------------------
# 2. /v1/chat/completions — basic (non-streaming)
# ---------------------------------------------------------------------------


class TestChatCompletionsBasic:
    @skip_if_no_server
    def test_returns_200(self, client: httpx.Client, model: str):
        r = client.post("/v1/chat/completions", json=_chat_payload(model))
        assert r.status_code == 200, r.text

    @skip_if_no_server
    def test_response_top_level_shape(self, client: httpx.Client, model: str):
        r = client.post("/v1/chat/completions", json=_chat_payload(model))
        data = r.json()
        assert data.get("object") == "chat.completion"
        assert isinstance(data.get("choices"), list)
        assert len(data["choices"]) >= 1
        assert "id" in data
        assert "model" in data
        assert "usage" in data

    @skip_if_no_server
    def test_choice_shape(self, client: httpx.Client, model: str):
        r = client.post("/v1/chat/completions", json=_chat_payload(model))
        choice = r.json()["choices"][0]
        assert choice.get("index") == 0
        assert choice.get("finish_reason") in ("stop", "length", "eos")
        msg = choice.get("message", {})
        assert msg.get("role") == "assistant"
        assert isinstance(msg.get("content"), str)
        assert len(msg["content"]) > 0

    @skip_if_no_server
    def test_usage_fields(self, client: httpx.Client, model: str):
        r = client.post("/v1/chat/completions", json=_chat_payload(model))
        usage = r.json()["usage"]
        assert isinstance(usage.get("prompt_tokens"), int)
        assert isinstance(usage.get("completion_tokens"), int)
        assert isinstance(usage.get("total_tokens"), int)
        assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]

    @skip_if_no_server
    def test_max_tokens_respected(self, client: httpx.Client, model: str):
        r = client.post("/v1/chat/completions", json=_chat_payload(model, max_tokens=4))
        usage = r.json()["usage"]
        assert usage["completion_tokens"] <= 4, (
            f"completion_tokens={usage['completion_tokens']} exceeded max_tokens=4"
        )

    @skip_if_no_server
    def test_temperature_zero_is_deterministic(self, client: httpx.Client, model: str):
        payload = _chat_payload(model, temperature=0.0)
        r1 = client.post("/v1/chat/completions", json=payload)
        r2 = client.post("/v1/chat/completions", json=payload)
        c1 = r1.json()["choices"][0]["message"]["content"].strip().lower()
        c2 = r2.json()["choices"][0]["message"]["content"].strip().lower()
        assert c1 == c2, (
            f"temperature=0 produced different outputs: {c1!r} vs {c2!r}"
        )


# ---------------------------------------------------------------------------
# 3. Streaming (SSE)
# ---------------------------------------------------------------------------


class TestChatCompletionsStreaming:
    @skip_if_no_server
    def test_streaming_returns_200(self, base_url: str, model: str, timeout: float):
        with httpx.Client(base_url=base_url, timeout=timeout) as client:
            with client.stream(
                "POST", "/v1/chat/completions", json=_chat_payload(model, stream=True)
            ) as r:
                assert r.status_code == 200

    @skip_if_no_server
    def test_streaming_content_type(self, base_url: str, model: str, timeout: float):
        with httpx.Client(base_url=base_url, timeout=timeout) as client:
            with client.stream(
                "POST", "/v1/chat/completions", json=_chat_payload(model, stream=True)
            ) as r:
                ct = r.headers.get("content-type", "")
                assert "text/event-stream" in ct, f"Expected text/event-stream, got: {ct}"

    @skip_if_no_server
    def test_streaming_sse_chunks(self, base_url: str, model: str, timeout: float):
        """Collect SSE chunks, validate format, and ensure [DONE] sentinel arrives."""
        chunks = []
        done_seen = False

        with httpx.Client(base_url=base_url, timeout=timeout) as client:
            with client.stream(
                "POST", "/v1/chat/completions", json=_chat_payload(model, stream=True)
            ) as r:
                for line in r.iter_lines():
                    if not line:
                        continue
                    assert line.startswith("data:"), f"Unexpected SSE line: {line!r}"
                    payload_str = line[len("data:"):].strip()
                    if payload_str == "[DONE]":
                        done_seen = True
                        break
                    import json
                    chunk = json.loads(payload_str)
                    chunks.append(chunk)

        assert done_seen, "Stream ended without [DONE] sentinel"
        assert len(chunks) > 0, "No data chunks received before [DONE]"

    @skip_if_no_server
    def test_streaming_chunk_shape(self, base_url: str, model: str, timeout: float):
        """Each data chunk must follow the chat.completion.chunk schema."""
        import json

        with httpx.Client(base_url=base_url, timeout=timeout) as client:
            with client.stream(
                "POST", "/v1/chat/completions", json=_chat_payload(model, stream=True)
            ) as r:
                for line in r.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload_str = line[len("data:"):].strip()
                    if payload_str == "[DONE]":
                        break
                    chunk = json.loads(payload_str)
                    assert chunk.get("object") == "chat.completion.chunk", (
                        f"Unexpected object type: {chunk.get('object')}"
                    )
                    assert isinstance(chunk.get("choices"), list)
                    choice = chunk["choices"][0]
                    assert "delta" in choice

    @skip_if_no_server
    def test_streaming_content_reassembles(self, base_url: str, model: str, timeout: float):
        """Reassembled content from stream chunks must be non-empty."""
        import json

        content_parts = []
        with httpx.Client(base_url=base_url, timeout=timeout) as client:
            with client.stream(
                "POST", "/v1/chat/completions", json=_chat_payload(model, stream=True)
            ) as r:
                for line in r.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload_str = line[len("data:"):].strip()
                    if payload_str == "[DONE]":
                        break
                    chunk = json.loads(payload_str)
                    delta = chunk["choices"][0].get("delta", {})
                    if "content" in delta and delta["content"]:
                        content_parts.append(delta["content"])

        assembled = "".join(content_parts)
        assert len(assembled) > 0, "Reassembled streaming content is empty"


# ---------------------------------------------------------------------------
# 4. Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    @skip_if_no_server
    def test_invalid_model_returns_4xx(self, client: httpx.Client):
        payload = _chat_payload("nonexistent-model-xyz-12345")
        r = client.post("/v1/chat/completions", json=payload)
        assert 400 <= r.status_code < 500, (
            f"Expected 4xx for invalid model, got {r.status_code}"
        )

    @skip_if_no_server
    def test_missing_messages_returns_4xx(self, client: httpx.Client, model: str):
        r = client.post("/v1/chat/completions", json={"model": model})
        assert 400 <= r.status_code < 500, (
            f"Expected 4xx for missing messages, got {r.status_code}"
        )

    @skip_if_no_server
    def test_empty_messages_returns_4xx(self, client: httpx.Client, model: str):
        r = client.post("/v1/chat/completions", json={"model": model, "messages": []})
        assert 400 <= r.status_code < 500, (
            f"Expected 4xx for empty messages, got {r.status_code}"
        )

    @skip_if_no_server
    def test_malformed_json_returns_4xx(self, client: httpx.Client):
        r = client.post(
            "/v1/chat/completions",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        assert 400 <= r.status_code < 500, (
            f"Expected 4xx for malformed JSON, got {r.status_code}"
        )

    @skip_if_no_server
    def test_error_response_has_error_field(self, client: httpx.Client):
        payload = _chat_payload("nonexistent-model-xyz-12345")
        r = client.post("/v1/chat/completions", json=payload)
        if 400 <= r.status_code < 500:
            data = r.json()
            # OpenAI error format: {"error": {"message": ..., "type": ..., "code": ...}}
            assert "error" in data, f"Error response missing 'error' key: {data}"


# ---------------------------------------------------------------------------
# 5. Cold-start and warmup behavior
# ---------------------------------------------------------------------------


class TestColdStartAndWarmup:
    @skip_if_no_server
    def test_first_request_completes_within_timeout(
        self, client: httpx.Client, model: str, timeout: float
    ):
        """First request must complete within effective_llm_timeout (may load model)."""
        start = time.monotonic()
        r = client.post("/v1/chat/completions", json=_chat_payload(model))
        elapsed = time.monotonic() - start
        assert r.status_code == 200, f"Cold-start request failed: {r.text}"
        assert elapsed < timeout, (
            f"Cold-start took {elapsed:.1f}s, exceeding timeout {timeout}s"
        )

    @skip_if_no_server
    def test_warmup_is_faster_than_cold_start(
        self, client: httpx.Client, model: str
    ):
        """Subsequent (warm) request should be faster than the first.

        Warm threshold: if cold-start took > 10 s, warm must be < cold/2.
        If cold-start was already fast (model pre-loaded), we just verify
        warm requests also succeed.
        """
        payload = _chat_payload(model)

        t0 = time.monotonic()
        r_cold = client.post("/v1/chat/completions", json=payload)
        cold_elapsed = time.monotonic() - t0
        assert r_cold.status_code == 200

        t1 = time.monotonic()
        r_warm = client.post("/v1/chat/completions", json=payload)
        warm_elapsed = time.monotonic() - t1
        assert r_warm.status_code == 200

        if cold_elapsed > 10.0:
            # Model had to load — warm request should be meaningfully faster
            assert warm_elapsed < cold_elapsed / 2, (
                f"Warm request ({warm_elapsed:.1f}s) not significantly faster "
                f"than cold ({cold_elapsed:.1f}s)"
            )


# ---------------------------------------------------------------------------
# 6. Concurrent requests
# ---------------------------------------------------------------------------


class TestConcurrentRequests:
    @skip_if_no_server
    def test_three_concurrent_completions(self, base_url: str, model: str, timeout: float):
        """Three simultaneous requests must all succeed."""

        async def _run() -> list[int]:
            async def _one_request() -> int:
                async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as c:
                    r = await c.post("/v1/chat/completions", json=_chat_payload(model))
                    return r.status_code

            return await asyncio.gather(_one_request(), _one_request(), _one_request())

        statuses = asyncio.run(_run())
        assert all(s == 200 for s in statuses), (
            f"Not all concurrent requests succeeded: {statuses}"
        )

    @skip_if_no_server
    def test_concurrent_responses_are_independent(
        self, base_url: str, model: str, timeout: float
    ):
        """Each concurrent response must have a unique completion ID."""

        async def _run() -> list[str]:
            async def _get_response_id() -> str:
                async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as c:
                    r = await c.post("/v1/chat/completions", json=_chat_payload(model))
                    return r.json().get("id", "")

            return await asyncio.gather(
                _get_response_id(), _get_response_id(), _get_response_id()
            )

        ids = asyncio.run(_run())
        assert len(set(ids)) == len(ids), f"Duplicate completion IDs in concurrent responses: {ids}"


# ---------------------------------------------------------------------------
# 7. Timeout handling
# ---------------------------------------------------------------------------


class TestTimeoutHandling:
    @skip_if_no_server
    def test_very_short_timeout_raises_request_error(self, base_url: str, model: str):
        """A 0.1 s client timeout must raise httpx.TimeoutException, not hang."""
        with pytest.raises(httpx.TimeoutException):
            with httpx.Client(base_url=base_url, timeout=0.1) as c:
                c.post(
                    "/v1/chat/completions",
                    json=_chat_payload(model, max_tokens=256),
                )

    @skip_if_no_server
    def test_normal_timeout_does_not_fire(self, client: httpx.Client, model: str):
        """A request within configured timeout must complete without error."""
        # If this raises TimeoutException, the server or timeout config is broken.
        r = client.post("/v1/chat/completions", json=_chat_payload(model))
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# 8. JMFTS config integration
# ---------------------------------------------------------------------------


class TestJmftsConfigIntegration:
    """Validate that JMFTS Settings correctly exposes an OpenAI-compatible endpoint."""

    def test_no_llm_configured_is_a_supported_state(self):
        """The shipped default names no endpoint, because JMFTS does not ship an LLM.

        This used to assert the opposite — that `effective_llm_url` was always non-empty —
        which held only because the default was the author's own host and port. That value
        answers nothing on anyone else's machine, so it turned "you have not configured an
        LLM" into a connection error that reads like a defect in JMFTS.
        """
        from jmfts_core.config import Settings

        s = Settings(llm_base_url="", llm_model="", ensonet_url="", ensonet_model="")
        assert s.effective_llm_url == ""
        assert s.effective_llm_model == ""
        assert s.llm_configured is False

    def test_require_llm_names_the_variables_to_set(self):
        from jmfts_core.config import LlmNotConfiguredError, Settings

        s = Settings(llm_base_url="", llm_model="", ensonet_url="", ensonet_model="")
        with pytest.raises(LlmNotConfiguredError) as exc:
            s.require_llm("Synthesis")
        message = str(exc.value)
        assert "Synthesis" in message, "the message must name the operation that needed it"
        assert "JMFTS_LLM_BASE_URL" in message
        assert "JMFTS_LLM_MODEL" in message

    def test_require_llm_takes_the_callers_model_over_the_default(self):
        """A per-task `llm_model` is enough; the deployment need only name the endpoint."""
        from jmfts_core.config import Settings

        s = Settings(
            llm_base_url="http://server:8000/", llm_model="", ensonet_url="", ensonet_model=""
        )
        assert s.require_llm("RAPTOR", "per-task-model") == (
            "http://server:8000",
            "per-task-model",
        )

    def test_require_llm_rejects_a_model_with_no_endpoint(self):
        from jmfts_core.config import LlmNotConfiguredError, Settings

        s = Settings(llm_base_url="", llm_model="", ensonet_url="", ensonet_model="")
        with pytest.raises(LlmNotConfiguredError):
            s.require_llm("RAPTOR", "a-model-but-nowhere-to-send-it")

    def test_effective_llm_timeout_is_positive(self):
        settings = get_settings()
        assert settings.effective_llm_timeout > 0, (
            f"effective_llm_timeout must be positive, got {settings.effective_llm_timeout}"
        )

    def test_ensonet_defaults_used_when_llm_override_empty(self):
        from jmfts_core.config import Settings

        s = Settings(
            llm_base_url="",
            llm_model="",
            llm_timeout=0,
            ensonet_url="http://custom-ensonet:9000",
            ensonet_model="my-model",
            ensonet_timeout=99.0,
        )
        assert s.effective_llm_url == "http://custom-ensonet:9000"
        assert s.effective_llm_model == "my-model"
        assert s.effective_llm_timeout == 99.0

    def test_llm_override_takes_precedence_over_ensonet(self):
        from jmfts_core.config import Settings

        s = Settings(
            llm_base_url="http://other-server:11434/v1",
            llm_model="llama3",
            llm_timeout=60.0,
            ensonet_url="http://ensonet:8853",
            ensonet_model="glm4",
            ensonet_timeout=180.0,
        )
        assert s.effective_llm_url == "http://other-server:11434/v1"
        assert s.effective_llm_model == "llama3"
        assert s.effective_llm_timeout == 60.0

    def test_completions_endpoint_pattern(self):
        """Verify the URL pattern used by production code against effective_llm_url.

        Production callers (fact_extraction.py, summarization.py, synthesis.py) do:
            base_url = settings.effective_llm_url.rstrip("/")
            POST {base_url}/v1/chat/completions

        CONVENTION: effective_llm_url must NOT include a /v1 suffix — callers
        always append /v1/chat/completions themselves.  Setting
        JMFTS_LLM_BASE_URL=http://host:port/v1 would produce a broken double-/v1
        URL.  Use JMFTS_LLM_BASE_URL=http://host:port (no trailing /v1).

        NOTE: .env.example currently shows the /v1 form — that is incorrect and
        would result in http://host:port/v1/v1/chat/completions (404).
        """
        from jmfts_core.config import Settings

        # Ensonet-style: base URL without /v1 suffix
        s_ensonet = Settings(
            llm_base_url="",
            ensonet_url="http://my-ensonet:8853",
            ensonet_model="test-model",
            ensonet_timeout=30.0,
        )
        base = s_ensonet.effective_llm_url.rstrip("/")
        assert f"{base}/v1/chat/completions" == "http://my-ensonet:8853/v1/chat/completions"

        # Generic override: also without /v1 suffix (consistent convention)
        s_generic = Settings(
            llm_base_url="http://other-server:11434",
            llm_model="llama3",
            llm_timeout=60.0,
        )
        base2 = s_generic.effective_llm_url.rstrip("/")
        assert f"{base2}/v1/chat/completions" == "http://other-server:11434/v1/chat/completions"


# ---------------------------------------------------------------------------
# 9. Ensonet-specific extensions (informational)
# ---------------------------------------------------------------------------


class TestEnsonetExtensions:
    """Document and probe ensonet-specific endpoints not in the OpenAI spec.

    These tests are informational — they verify that ensonet *provides* these
    endpoints, not that they're required by JMFTS.  JMFTS never calls them.
    """

    @skip_if_no_server
    def test_health_endpoint_exists(self, client: httpx.Client):
        r = client.get("/health")
        assert r.status_code < 500, f"/health returned server error: {r.status_code}"

    @skip_if_no_server
    def test_queues_endpoint_exists(self, client: httpx.Client):
        """ensonet /v1/queues shows per-model request queues."""
        r = client.get("/v1/queues")
        # 404 if not implemented; 200 if present. Both are acceptable here.
        assert r.status_code != 500, f"/v1/queues returned server error: {r.status_code}"

    @skip_if_no_server
    def test_servers_endpoint_exists(self, client: httpx.Client):
        """ensonet /v1/servers shows backend inference process list."""
        r = client.get("/v1/servers")
        assert r.status_code != 500, f"/v1/servers returned server error: {r.status_code}"
