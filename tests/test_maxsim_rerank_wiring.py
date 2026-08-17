"""maxsim_rerank wiring — the `rerank_method` dispatch in SearchService._maybe_rerank.

The formerly-dead ``SearchRepository.maxsim_rerank`` is now reachable as a second-stage
rerank via ``rerank=True, rerank_method="maxsim"``. These tests exercise the dispatch
directly with a mock session — no database, no model — proving each branch routes where
it should and that neither branch swallows a failure.
"""

from unittest.mock import MagicMock

import pytest

from jmfts_core.repositories.search import SearchResult
from jmfts_core.services.search_service import SearchService


def _results(n=3):
    return [SearchResult(document=MagicMock(id=i), score=1.0, method="vector") for i in range(n)]


def test_rerank_off_returns_input_untouched():
    svc = SearchService(session=MagicMock())
    r = _results()
    assert svc._maybe_rerank(r, "q", rerank=False, limit=10) is r


def test_empty_candidates_short_circuit():
    svc = SearchService(session=MagicMock())
    assert svc._maybe_rerank([], "q", rerank=True, limit=10, method="maxsim") == []


def test_maxsim_method_routes_to_repo(monkeypatch):
    """rerank_method='maxsim' must call SearchRepository.maxsim_rerank with the query
    and the final limit, and return exactly what it returns."""
    captured = {}

    def fake_maxsim_rerank(self, candidates, query_text, limit=10, max_tier=None):
        captured["args"] = (candidates, query_text, limit)
        return candidates[:1]

    monkeypatch.setattr(
        "jmfts_core.services.search_service.SearchRepository.maxsim_rerank",
        fake_maxsim_rerank,
    )
    svc = SearchService(session=MagicMock())
    r = _results()
    out = svc._maybe_rerank(r, "hello world", rerank=True, limit=5, method="maxsim")

    assert captured["args"][1] == "hello world"
    assert captured["args"][2] == 5
    assert out == r[:1]


def test_maxsim_failure_is_not_swallowed(monkeypatch):
    """The maxsim path shares the search's own embedding service — a failure there is a
    real fault, so it must propagate."""

    def boom(self, candidates, query_text, limit=10, max_tier=None):
        raise RuntimeError("embedding down")

    monkeypatch.setattr("jmfts_core.services.search_service.SearchRepository.maxsim_rerank", boom)
    svc = SearchService(session=MagicMock())
    with pytest.raises(RuntimeError, match="embedding down"):
        svc._maybe_rerank(_results(), "q", rerank=True, limit=5, method="maxsim")


def test_cross_encoder_is_the_default(monkeypatch):
    fake = MagicMock()
    fake.rerank.return_value = _results(2)
    monkeypatch.setattr("jmfts_core.services.search_service.get_reranker_service", lambda: fake)
    svc = SearchService(session=MagicMock())
    out = svc._maybe_rerank(_results(), "q", rerank=True, limit=2)  # no method → default
    fake.rerank.assert_called_once()
    assert out == fake.rerank.return_value


def test_cross_encoder_failure_is_not_swallowed(monkeypatch):
    """A reranker that cannot load is a fault, not an optional extra. Returning the
    first-stage order would report a second stage that never ran, so it must propagate."""

    def boom():
        raise RuntimeError("no reranker model")

    monkeypatch.setattr("jmfts_core.services.search_service.get_reranker_service", boom)
    svc = SearchService(session=MagicMock())
    with pytest.raises(RuntimeError, match="no reranker model"):
        svc._maybe_rerank(_results(), "q", rerank=True, limit=2)


def test_unknown_method_raises():
    svc = SearchService(session=MagicMock())
    with pytest.raises(ValueError, match="unknown rerank_method"):
        svc._maybe_rerank(_results(), "q", rerank=True, limit=5, method="bogus")


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
