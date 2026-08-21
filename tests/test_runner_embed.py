"""The /runner embed endpoints: wire format, truncation, and the over-window refusal.

These tests do not load a model. ``EmbeddingService`` is replaced with a stub that returns
arrays of known values, because what is under test is the protocol — the encoding, the
matryoshka truncation, the row/metadata correspondence, and what happens to text that does
not fit. Whether the real model produces good vectors is a different question, tested where
the model is.

The one thing a stub cannot fake is the shape contract, so that is asserted precisely:
float32 for the document vector, float16 for the token matrix, little-endian, row-major,
one row per entry in ``tokens``.
"""

from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient

import jmfts_core.rest.routers.runner as runner_router
import jmfts_core.rest.main as main
from jmfts_core.config import get_settings
from jmfts_core.contracts.runner import (
    DOC_DTYPE,
    TOKEN_DTYPE,
    decode_matrix,
    decode_vector,
)
from jmfts_core.embedding import (
    EmbeddingResult,
    FitResult,
    TextTooLongError,
    TokenEmbeddingResult,
)

RUNNER_KEY = "test-runner-key-embed"
HEADERS = {"Authorization": f"Bearer {RUNNER_KEY}"}

NATIVE_DIMS = 8
KEPT_TOKENS = 5


class StubEmbeddingService:
    """A stand-in with the five methods the runner surface actually calls.

    ``truncate_embedding`` is the real implementation copied rather than mocked, because
    the truncate-and-renormalize behaviour is part of what these tests check.
    """

    model_name = "stub/model"
    device = "cpu"
    token_top_percent = 0.5
    model_loaded = False

    def __init__(self):
        self.too_long: TextTooLongError | None = None
        self.last_top_percent: float | None = None
        self.last_prefix: str | None = None

    def _raise_if_configured(self):
        if self.too_long is not None:
            raise self.too_long

    def check_fit(self, text, with_tokens=True, prefix="search_document: "):
        settings = get_settings()
        limit = settings.embedding_token_window if with_tokens else settings.embedding_doc_window
        return FitResult(
            token_count=len(text.split()),
            limit=limit,
            chars_total=len(text),
            truncated=False,
        )

    def embed_text(self, text, normalize=True, prefix=""):
        self._raise_if_configured()
        self.last_prefix = prefix
        return np.arange(NATIVE_DIMS, dtype=np.float32) / NATIVE_DIMS

    def truncate_embedding(self, embedding, target_dim, normalize=True):
        truncated = embedding[:target_dim]
        if normalize:
            norm = np.linalg.norm(truncated)
            if norm > 0:
                truncated = truncated / norm
        return truncated

    def embed_with_tokens(self, text, top_percent=None, token_selector=None, prefix=""):
        self._raise_if_configured()
        self.last_top_percent = top_percent
        self.last_prefix = prefix
        tokens = [
            TokenEmbeddingResult(
                token_idx=i,
                token_text=f"tok{i}",
                # Descending, as the real service returns them: highest importance first.
                importance_score=1.0 - i / 10.0,
                embedding=np.full(NATIVE_DIMS, i + 1, dtype=np.float32),
            )
            for i in range(KEPT_TOKENS)
        ]
        return EmbeddingResult(
            document_embedding=np.ones(NATIVE_DIMS, dtype=np.float32),
            token_embeddings=tokens,
        )


@pytest.fixture
def service(monkeypatch):
    stub = StubEmbeddingService()
    monkeypatch.setattr(runner_router, "get_embedding_service", lambda: stub)
    monkeypatch.setattr(get_settings(), "runner_key", RUNNER_KEY)
    return stub


@pytest.fixture
def client(service):
    return TestClient(main.app)


# --- the document-vector path ---------------------------------------------------------


def test_embed_returns_a_decodable_float32_vector(client):
    response = client.post("/runner/embed", json={"text": "hello world"}, headers=HEADERS)
    assert response.status_code == 200

    body = response.json()
    assert body["model"] == "stub/model"
    assert body["dtype"] == DOC_DTYPE
    assert body["dims"] == NATIVE_DIMS

    vec = decode_vector(body["document_embedding"], body["dtype"])
    assert vec.shape == (NATIVE_DIMS,)
    np.testing.assert_allclose(vec, np.arange(NATIVE_DIMS) / NATIVE_DIMS)


def test_embed_reports_the_document_window_not_the_token_window(client):
    """The two paths have different limits, and `fit` must name the one that applied."""
    settings = get_settings()
    response = client.post("/runner/embed", json={"text": "a b c"}, headers=HEADERS)
    assert response.json()["fit"]["limit"] == settings.embedding_doc_window


def test_embed_passes_the_caller_s_prefix_through(client, service):
    """Only the caller knows whether it is storing a document or asking a question."""
    client.post("/runner/embed", json={"text": "x", "prefix": "search_query: "}, headers=HEADERS)
    assert service.last_prefix == "search_query: "


def test_embed_defaults_the_prefix_to_document(client, service):
    client.post("/runner/embed", json={"text": "x"}, headers=HEADERS)
    assert service.last_prefix == "search_document: "


# --- the token path -------------------------------------------------------------------


def test_embed_tokens_matrix_has_one_row_per_token(client):
    response = client.post("/runner/embed/tokens", json={"text": "hello"}, headers=HEADERS)
    assert response.status_code == 200

    body = response.json()
    assert body["token_dtype"] == TOKEN_DTYPE
    assert body["token_dims"] == NATIVE_DIMS
    assert len(body["tokens"]) == KEPT_TOKENS

    matrix = decode_matrix(body["token_embeddings"], body["token_dtype"], body["token_dims"])
    assert matrix.shape == (KEPT_TOKENS, NATIVE_DIMS)


def test_token_rows_correspond_to_token_metadata_in_order(client):
    """Row i belongs to tokens[i]. The stub gives each token a distinct constant vector,
    so a reordering between the two lists would show up as the wrong row content."""
    body = client.post("/runner/embed/tokens", json={"text": "hello"}, headers=HEADERS).json()
    matrix = decode_matrix(body["token_embeddings"], body["token_dtype"], body["token_dims"])

    for i, item in enumerate(body["tokens"]):
        assert item["token_text"] == f"tok{i}"
        # Each stub vector is a constant, normalized to 1/sqrt(dims) on every component.
        expected = 1.0 / np.sqrt(NATIVE_DIMS)
        np.testing.assert_allclose(matrix[i], expected, rtol=1e-2)


def test_token_dims_truncates_and_shrinks_the_payload(client):
    """Matryoshka truncation on the runner side is what makes the response small.

    JMFTS stores halfvec(256), so a caller asks for 256 and the matrix arrives at 256.
    """
    full = client.post("/runner/embed/tokens", json={"text": "hello"}, headers=HEADERS).json()
    half = client.post(
        "/runner/embed/tokens",
        json={"text": "hello", "token_dims": NATIVE_DIMS // 2},
        headers=HEADERS,
    ).json()

    assert half["token_dims"] == NATIVE_DIMS // 2
    # `dims` stays the model's native width; only the token vectors were truncated.
    assert half["dims"] == NATIVE_DIMS

    matrix = decode_matrix(half["token_embeddings"], half["token_dtype"], half["token_dims"])
    assert matrix.shape == (KEPT_TOKENS, NATIVE_DIMS // 2)
    assert len(half["token_embeddings"]) < len(full["token_embeddings"])


def test_truncated_token_rows_are_renormalized(client):
    """Truncation without renormalization would leave short vectors, and cosine distance
    against them is wrong in a way that still returns plausible rankings."""
    body = client.post(
        "/runner/embed/tokens",
        json={"text": "hello", "token_dims": NATIVE_DIMS // 2},
        headers=HEADERS,
    ).json()
    matrix = decode_matrix(body["token_embeddings"], body["token_dtype"], body["token_dims"])
    norms = np.linalg.norm(matrix.astype(np.float32), axis=1)
    np.testing.assert_allclose(norms, 1.0, rtol=1e-2)


def test_token_dims_above_the_model_width_is_a_400(client):
    """Matryoshka truncation can only make a vector shorter. Asking for more is a caller
    error, not something to pad."""
    response = client.post(
        "/runner/embed/tokens",
        json={"text": "hello", "token_dims": NATIVE_DIMS * 2},
        headers=HEADERS,
    )
    assert response.status_code == 400
    assert "exceeds" in response.json()["detail"]


def test_top_percent_reaches_the_service(client, service):
    client.post(
        "/runner/embed/tokens", json={"text": "hello", "top_percent": 0.25}, headers=HEADERS
    )
    assert service.last_top_percent == 0.25


def test_omitted_top_percent_leaves_the_runner_s_default(client, service):
    """None, not a number invented here — the runner's configured default is the default."""
    client.post("/runner/embed/tokens", json={"text": "hello"}, headers=HEADERS)
    assert service.last_top_percent is None


def test_embed_tokens_reports_the_token_window(client):
    settings = get_settings()
    body = client.post("/runner/embed/tokens", json={"text": "hello"}, headers=HEADERS).json()
    assert body["fit"]["limit"] == settings.embedding_token_window


# --- refusing over-window text --------------------------------------------------------


@pytest.mark.parametrize("path", ["/runner/embed", "/runner/embed/tokens"])
def test_over_window_text_is_a_400_not_a_truncated_success(client, service, path):
    """The runner will not embed a prefix and report success.

    A truncated embedding is indistinguishable from a good one downstream: the tail is
    simply absent from every future search, with nothing recording that it was dropped
    (KNOWN-DEFECTS D1). The caller's fix is to chunk, so it has to be told.
    """
    service.too_long = TextTooLongError(
        token_count=900, limit=512, chars_total=4000, path="token/maxsim"
    )
    response = client.post(path, json={"text": "long " * 1000}, headers=HEADERS)
    assert response.status_code == 400
    assert "900" in response.json()["detail"]


# --- info ------------------------------------------------------------------------------


def test_info_reports_null_dims_when_the_model_is_not_loaded(client):
    """Polling for a healthy runner must not be what pulls the weights onto the GPU, and
    a dimensionality nobody measured must not be reported as if it were."""
    body = client.get("/runner/info", headers=HEADERS).json()
    assert body["model_loaded"] is False
    assert body["dims"] is None
    assert body["model"] == "stub/model"
