"""An uncached tokenizer on an offline host is a named, PERMANENT failure (issue #6).

``EmbeddingService.tokenizer`` used to hand the caller whatever ``AutoTokenizer.from_pretrained``
raised, which on a host with ``HF_HUB_OFFLINE=1`` is a bare ``OSError`` about a hub it was
told not to reach. No database, no network, no model.
"""

import pytest

pytest.importorskip("transformers")

import transformers  # noqa: E402

from jmfts_core.embedding import EmbeddingService, TokenizerNotCached  # noqa: E402
from jmfts_core.task_errors import ErrorType, classify_exception  # noqa: E402


def _offline(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("We couldn't connect to 'https://huggingface.co' to load this file")

    monkeypatch.setattr(transformers.AutoTokenizer, "from_pretrained", boom)


def test_message_names_model_cause_and_ways_out(monkeypatch):
    _offline(monkeypatch)
    svc = EmbeddingService(model_name="acme/some-model")
    with pytest.raises(TokenizerNotCached) as info:
        svc.tokenizer
    msg = str(info.value)
    assert "acme/some-model" in msg
    assert "not cached" in msg
    assert "huggingface-cli download acme/some-model" in msg
    assert "HF_HUB_OFFLINE" in msg
    assert "HF_HOME" in msg
    assert isinstance(info.value.__cause__, OSError)
    assert info.value.model_name == "acme/some-model"


def test_failure_is_not_cached_and_retried_on_next_access(monkeypatch):
    _offline(monkeypatch)
    svc = EmbeddingService(model_name="acme/some-model")
    with pytest.raises(TokenizerNotCached):
        svc.tokenizer
    assert svc._tokenizer is None


def test_classified_permanent(monkeypatch):
    _offline(monkeypatch)
    svc = EmbeddingService(model_name="acme/some-model")
    with pytest.raises(TokenizerNotCached) as info:
        svc.tokenizer
    assert classify_exception(info.value) == ErrorType.PERMANENT
