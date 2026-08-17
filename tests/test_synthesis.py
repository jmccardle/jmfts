"""Tests for the synthesis service and /search/synthesize endpoint.

Tests the context formatting logic, LLM call behavior, and endpoint
graceful degradation. Avoids real DB/LLM calls via mocking.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from jmfts_core.synthesis import _format_context, synthesize, SynthesisResult


def _run(coro):
    """Run an async coroutine synchronously for tests."""
    return asyncio.get_event_loop().run_until_complete(coro)


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


def _mock_llm_client(response_json, side_effect=None):
    """Create a mock httpx.AsyncClient with a preset response."""
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = response_json

    mock_client = AsyncMock()
    if side_effect:
        mock_client.post = AsyncMock(side_effect=side_effect)
    else:
        mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    return mock_client


class TestSynthesize:
    def test_successful_synthesis(self):
        mock_client = _mock_llm_client(
            {
                "choices": [{"message": {"content": "Synthesized answer here."}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            }
        )
        docs = [{"id": 1, "title": "Doc", "content": "Text", "score": 0.9}]

        with patch("jmfts_core.synthesis.httpx.AsyncClient", return_value=mock_client):
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
        assert result.usage["prompt_tokens"] == 100

        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert "/chat/completions" in call_args[0][0]
        payload = call_args[1]["json"]
        assert payload["model"] == "test-model"
        assert len(payload["messages"]) == 2

    def test_connection_error_propagates(self):
        mock_client = _mock_llm_client(None, side_effect=httpx.ConnectError("Connection refused"))

        with patch("jmfts_core.synthesis.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(httpx.ConnectError):
                _run(
                    synthesize(
                        query="test",
                        documents=[{"id": 1, "title": "A", "content": "B", "score": 0.5}],
                    )
                )

    def test_uses_default_model_from_settings(self):
        mock_client = _mock_llm_client(
            {
                "choices": [{"message": {"content": "Answer"}}],
                "usage": None,
            }
        )

        mock_settings = MagicMock()
        mock_settings.effective_llm_model = "test-default-model"
        mock_settings.effective_llm_url = "http://localhost:8853"
        mock_settings.effective_llm_timeout = 10.0

        with (
            patch("jmfts_core.synthesis.httpx.AsyncClient", return_value=mock_client),
            patch("jmfts_core.synthesis.get_settings", return_value=mock_settings),
        ):
            result = _run(
                synthesize(
                    query="test",
                    documents=[{"id": 1, "title": "A", "content": "B", "score": 0.5}],
                    llm_model=None,
                )
            )

        assert result.model == "test-default-model"


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
        from api.schemas import SynthesizeRequest

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
        from api.schemas import SynthesizeRequest

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
        from api.schemas import SynthesizeRequest

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
