"""Tests for the synthesis service and /search/synthesize endpoint.

Tests the context formatting logic, LLM call behavior, and endpoint
graceful degradation. Avoids real DB/LLM calls via mocking.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from jmfts_core.synthesis import _format_context, synthesize, SynthesisResult
from tests.llm_stub import llm_stub


def _run(coro):
    """Run an async coroutine synchronously for tests.

    A loop of its own, per call — the same shape ``test_fact_extraction`` and
    ``test_conversation_ingest`` use. It used to be
    ``asyncio.get_event_loop().run_until_complete(...)``, which depends on a loop the
    process happens to have left current: any earlier test calling ``asyncio.run`` leaves
    it unset (3.10+ ends ``asyncio.run`` with ``set_event_loop(None)``), and every test in
    this file then failed with "There is no current event loop" depending on collection
    order. ``jmfts_core.llm_client.complete_sync`` is such a caller.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ============================================================================
# _format_context tests
# ============================================================================


class TestFormatContext:
    def test_basic_formatting(self):
        docs = [
            {"id": 1, "title": "Alpha", "content": "Content A", "score": 0.95},
            {"id": 2, "title": "Beta", "content": "Content B", "score": 0.80},
        ]
        result = _format_context(docs, max_context_tokens=4096)
        assert "[Source 1: Alpha (id=1, score=0.950)]" in result
        assert "Content A" in result
        assert "[Source 2: Beta (id=2, score=0.800)]" in result
        assert "Content B" in result

    def test_missing_title_uses_fallback(self):
        docs = [{"id": 7, "title": None, "content": "Some text", "score": 0.5}]
        result = _format_context(docs, max_context_tokens=4096)
        assert "Document 7" in result

    def test_budget_truncation(self):
        docs = [
            {"id": 1, "title": "A", "content": "x" * 5000, "score": 0.9},
            {"id": 2, "title": "B", "content": "y" * 5000, "score": 0.8},
            {"id": 3, "title": "C", "content": "z" * 5000, "score": 0.7},
        ]
        # ~250 tokens * 4 chars = 1000 chars budget — not enough for all 3
        result = _format_context(docs, max_context_tokens=250)
        assert "Source 1" in result
        # Third doc should be cut or absent
        assert result.count("Source 3") == 0 or "..." in result

    def test_empty_documents(self):
        result = _format_context([], max_context_tokens=4096)
        assert result == ""


# ============================================================================
# synthesize() tests
# ============================================================================


class TestSynthesize:
    """The transport moved to ``tau_llm`` in 0.3.0, so these drive a real loopback
    endpoint (``tests/llm_stub.py``) instead of a patched ``httpx.AsyncClient``. The
    assertions are the same ones — URL, payload, parsed answer — made against the bytes
    that actually left the process.
    """

    def test_synthesis_without_a_configured_llm_says_so(self):
        """The unconfigured case names the variables instead of failing at the socket."""
        from jmfts_core.config import LlmNotConfiguredError, Settings

        unconfigured = Settings(llm_base_url="", llm_model="", ensonet_url="", ensonet_model="")
        with patch("jmfts_core.synthesis.get_settings", return_value=unconfigured):
            with pytest.raises(LlmNotConfiguredError, match="JMFTS_LLM_BASE_URL"):
                _run(
                    synthesize(
                        query="test",
                        documents=[{"id": 1, "title": "A", "content": "B", "score": 0.5}],
                    )
                )

    def test_successful_synthesis(self):
        docs = [{"id": 1, "title": "Doc", "content": "Text", "score": 0.9}]

        with llm_stub(content="Synthesized answer here.") as stub:
            with patch("jmfts_core.synthesis.get_settings", return_value=stub.settings()):
                result = _run(
                    synthesize(
                        query="What is X?",
                        documents=docs,
                        max_context_tokens=4096,
                        llm_model="test-model",
                    )
                )

            assert isinstance(result, SynthesisResult)
            assert result.text == "Synthesized answer here."
            assert result.model == "test-model"
            # τ's Usage vocabulary, not OpenAI's `prompt_tokens` — the numbers are the
            # server's, the spelling is τ's. See llm_client.LlmCompletion.usage.
            assert result.usage["input_tokens"] == 11
            assert result.usage["output_tokens"] == 7

            assert len(stub.requests) == 1
            request = stub.requests[0]
            # The `/v1` the call site used to write into the path literal is now appended
            # once, in llm_client.build_model. This is what proves it lands in one place
            # and not two.
            assert request.path == "/v1/chat/completions"
            assert request.body["model"] == "test-model"
            assert len(request.messages) == 2
            assert request.messages[0]["role"] == "system"
            assert "What is X?" in request.role("user")

    def test_a_refusing_endpoint_propagates(self):
        """A non-200 reaches the caller as an exception naming the status.

        It used to be an ``httpx.HTTPStatusError`` from ``raise_for_status()``. τ 0.9.3
        reports a non-200 as an error carrying the status and the server's message, so the
        type is now plain ``Exception`` — see ``llm_client``'s module docstring for what
        that costs ``task_errors.classify_exception``. What has to keep holding, and is
        what this asserts, is that the failure is loud and says which status it was.
        """
        with llm_stub(status=503) as stub:
            with patch("jmfts_core.synthesis.get_settings", return_value=stub.settings()):
                with pytest.raises(Exception, match="503"):
                    _run(
                        synthesize(
                            query="test",
                            documents=[{"id": 1, "title": "A", "content": "B", "score": 0.5}],
                        )
                    )

    def test_an_unreachable_endpoint_propagates(self):
        """A dead socket is an error, never an empty synthesis.

        The endpoint is a port nothing listens on, so this exercises the real connect
        failure rather than a mock's ``side_effect``. ``search_service`` catches it and
        degrades to results-without-synthesis; that it is raised at all is the contract.

        The type is only ``Exception`` now (τ launders the ``httpx.ConnectError`` into an
        error event — see ``llm_client``'s module docstring), so ``match`` carries the
        assertion instead: a bare ``pytest.raises(Exception)`` would pass on a typo in the
        `Settings` above and prove nothing about the socket.
        """
        from jmfts_core.config import Settings

        dead = Settings(llm_base_url="http://127.0.0.1:1", llm_model="m", llm_timeout=5.0)
        with patch("jmfts_core.synthesis.get_settings", return_value=dead):
            with pytest.raises(Exception, match="127.0.0.1:1"):
                _run(
                    synthesize(
                        query="test",
                        documents=[{"id": 1, "title": "A", "content": "B", "score": 0.5}],
                    )
                )

    def test_uses_default_model_from_settings(self):
        with llm_stub(content="Answer") as stub:
            settings = stub.settings(llm_model="test-default-model")
            with patch("jmfts_core.synthesis.get_settings", return_value=settings):
                result = _run(
                    synthesize(
                        query="test",
                        documents=[{"id": 1, "title": "A", "content": "B", "score": 0.5}],
                        llm_model=None,
                    )
                )

        assert result.model == "test-default-model"
        assert stub.requests[0].body["model"] == "test-default-model"


# ============================================================================
# /search/synthesize endpoint tests (via mocked dependencies)
# ============================================================================


def _make_mock_search_result(doc_id, title, content, score, method="hybrid"):
    doc = MagicMock()
    doc.id = doc_id
    doc.title = title
    doc.content = content
    doc.parent_id = None
    doc.structured_content = {}
    doc.path = []
    doc.depth = 0
    doc.usetype = None
    doc.created_at = None
    doc.updated_at = None
    doc.content_hash = None
    doc.embed = None
    result = MagicMock()
    result.document = doc
    result.score = score
    result.method = method
    return result


class TestSynthesizeEndpoint:
    def test_endpoint_with_llm_available(self):
        from jmfts_core.services.search_service import SearchService
        from jmfts_core.rest.schemas import SynthesizeRequest

        request = SynthesizeRequest(
            query="What is JMFTS?",
            search_method="hybrid",
            top_k=3,
        )

        mock_results = [
            _make_mock_search_result(1, "Overview", "JMFTS is a search system", 0.95),
            _make_mock_search_result(2, "Details", "It uses embeddings", 0.85),
        ]

        mock_db = MagicMock()
        mock_repo = MagicMock()
        mock_repo.hybrid_search.return_value = mock_results

        llm_result = SynthesisResult(
            text="JMFTS is a search system that uses embeddings.",
            model="test-default-model",
            usage={"prompt_tokens": 200, "completion_tokens": 30},
        )

        mock_settings = MagicMock()
        mock_settings.effective_llm_model = "test-default-model"

        with (
            patch("jmfts_core.services.search_service.SearchRepository", return_value=mock_repo),
            patch(
                "jmfts_core.services.search_service.SearchService._resolve_context",
                return_value={},
            ),
            patch(
                "jmfts_core.services.search_service.synthesize",
                new_callable=AsyncMock,
                return_value=llm_result,
            ),
            patch("jmfts_core.services.search_service.get_settings", return_value=mock_settings),
        ):
            response = _run(
                SearchService(session=mock_db).synthesize_search(request=request, context=None)
            )

        assert response.llm_available is True
        assert "JMFTS" in response.synthesis
        assert len(response.sources) == 2
        assert response.sources[0].document_id == 1
        assert response.sources[0].score == 0.95
        assert response.llm_model == "test-default-model"
        assert response.search_latency_ms >= 0
        assert response.total_latency_ms >= response.search_latency_ms

    def test_endpoint_graceful_degradation(self):
        from jmfts_core.services.search_service import SearchService
        from jmfts_core.rest.schemas import SynthesizeRequest

        request = SynthesizeRequest(query="test query", search_method="vector", top_k=2)

        mock_results = [
            _make_mock_search_result(5, "Doc5", "Content five", 0.7, method="vector"),
        ]

        mock_db = MagicMock()
        mock_repo = MagicMock()
        mock_repo.vector_search_text.return_value = mock_results

        with (
            patch("jmfts_core.services.search_service.SearchRepository", return_value=mock_repo),
            patch(
                "jmfts_core.services.search_service.SearchService._resolve_context",
                return_value={},
            ),
            patch(
                "jmfts_core.services.search_service.synthesize",
                new_callable=AsyncMock,
                side_effect=httpx.ConnectError("LLM down"),
            ),
        ):
            response = _run(
                SearchService(session=mock_db).synthesize_search(request=request, context=None)
            )

        assert response.llm_available is False
        assert "unavailable" in response.synthesis.lower()
        assert len(response.sources) == 1
        assert response.sources[0].document_id == 5

    def test_endpoint_auto_routes_query(self):
        from jmfts_core.services.search_service import SearchService
        from jmfts_core.rest.schemas import SynthesizeRequest

        request = SynthesizeRequest(query="exact phrase lookup", search_method="auto", top_k=3)

        mock_results = [
            _make_mock_search_result(1, "Result", "Content", 0.9, method="bm25"),
        ]

        mock_db = MagicMock()
        mock_repo = MagicMock()
        mock_repo.bm25_search.return_value = mock_results
        mock_repo.vector_search_text.return_value = mock_results
        mock_repo.fulltext_search.return_value = mock_results
        mock_repo.hybrid_search.return_value = mock_results
        mock_repo.maxsim_search.return_value = mock_results

        llm_result = SynthesisResult(text="Answer", model="THUDM_GLM4_32b")

        with (
            patch("jmfts_core.services.search_service.SearchRepository", return_value=mock_repo),
            patch(
                "jmfts_core.services.search_service.SearchService._resolve_context",
                return_value={},
            ),
            patch(
                "jmfts_core.services.search_service.synthesize",
                new_callable=AsyncMock,
                return_value=llm_result,
            ),
        ):
            response = _run(
                SearchService(session=mock_db).synthesize_search(request=request, context=None)
            )

        assert response.llm_available is True
        assert response.synthesis == "Answer"
