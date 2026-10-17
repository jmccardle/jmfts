"""The embedding model loads at a pinned Hub revision, and only the model it belongs to.

An unpinned load follows the Hub's ``main``, so an upstream push would change every vector
under an unchanged JMFTS version. No network here: the loaders are replaced.
"""

import logging

import pytest

pytest.importorskip("sentence_transformers")
pytest.importorskip("transformers")

import sentence_transformers  # noqa: E402
import transformers  # noqa: E402

import jmfts_core.embedding as emb  # noqa: E402
from jmfts_core.config import (  # noqa: E402
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_EMBEDDING_REVISION,
    Settings,
)


@pytest.fixture
def seen(monkeypatch):
    """Record the (args, kwargs) every loader of the embedding model receives."""
    calls = {}

    class _FakeModel:
        def __init__(self, *args, **kwargs):
            calls["model"] = (args, kwargs)

    def _fake_tokenizer(*args, **kwargs):
        calls["tokenizer"] = (args, kwargs)
        return object()

    monkeypatch.setattr(sentence_transformers, "SentenceTransformer", _FakeModel)
    monkeypatch.setattr(transformers.AutoTokenizer, "from_pretrained", _fake_tokenizer)
    monkeypatch.setattr(emb, "_warned_unpinned", set())
    return calls


def test_default_settings_pin_the_default_model():
    s = Settings()
    assert s.embedding_model == DEFAULT_EMBEDDING_MODEL
    assert len(s.embedding_revision) == 40


def test_revision_reaches_both_loaders(seen):
    svc = emb.EmbeddingService()
    svc.model
    svc.tokenizer
    assert seen["model"][1]["revision"] == DEFAULT_EMBEDDING_REVISION
    assert seen["tokenizer"][1]["revision"] == DEFAULT_EMBEDDING_REVISION


def test_overridden_model_does_not_inherit_the_default_sha(seen, caplog):
    with caplog.at_level(logging.WARNING, logger="jmfts_core.embedding"):
        svc = emb.EmbeddingService(model_name="acme/other-model")
        svc.model
        emb.EmbeddingService(model_name="acme/other-model")  # second: no second warning
    assert seen["model"][1]["revision"] is None
    warnings = [r for r in caplog.records if "UNPINNED" in r.getMessage()]
    assert len(warnings) == 1
    assert "acme/other-model" in warnings[0].getMessage()


def test_explicit_revision_pins_an_overridden_model(seen, monkeypatch, caplog):
    monkeypatch.setenv("JMFTS_EMBEDDING_MODEL", "acme/other-model")
    monkeypatch.setenv("JMFTS_EMBEDDING_REVISION", "a" * 40)
    monkeypatch.setattr(emb, "get_settings", lambda: Settings())
    with caplog.at_level(logging.WARNING, logger="jmfts_core.embedding"):
        emb.EmbeddingService().model
    assert seen["model"][1]["revision"] == "a" * 40
    assert not [r for r in caplog.records if "UNPINNED" in r.getMessage()]
