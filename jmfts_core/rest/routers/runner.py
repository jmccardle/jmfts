"""The /runner surface: embed text for a caller that keeps the documents somewhere else.

Three endpoints, no database, no principal, no document ids. ``GET /runner/info`` says what
model this process holds; the two POSTs return vectors for text handed to them.

Why this exists as a separate surface rather than as more of the document API: embedding is
the only step in ingestion that wants a GPU, and the only one that holds no state. Splitting
it out lets one accelerator serve several isolated JMFTS databases, and lets a storage-side
worker run with a tokenizer and no model weights at all. See jmfts-client/jmfts_client/contracts/runner.py
for the wire format and jmfts_core/rest/auth.py::require_runner for the credential.

Every route here declares `require_runner` through the router's `dependencies`, and the
app-level `require_token` steps aside for this prefix. tests/test_runner_auth.py asserts
both halves of that arrangement so a route added later cannot land here ungated.
"""

from __future__ import annotations

import numpy as np
from fastapi import APIRouter, Depends, HTTPException

from jmfts_core.config import get_settings
from jmfts_client.contracts.runner import (
    DOC_DTYPE,
    TOKEN_DTYPE,
    RunnerEmbedRequest,
    RunnerEmbedResponse,
    RunnerEmbedTokensRequest,
    RunnerEmbedTokensResponse,
    RunnerFit,
    RunnerInfo,
    RunnerTokenItem,
    encode_vector,
)
from jmfts_core.embedding import TextTooLongError, get_embedding_service
from jmfts_core.rest.auth import RUNNER_PREFIX, require_runner

router = APIRouter(
    prefix=RUNNER_PREFIX,
    tags=["runner"],
    dependencies=[Depends(require_runner)],
)


def _too_long(exc: TextTooLongError) -> HTTPException:
    """Over-window text is a 400, matching POST /documents/{id}/embed.

    It is the caller's error and the caller's fix: chunk the text and ask again. The runner
    will not embed a prefix and report success, because the tail would then be missing from
    every future search with no record that it was ever dropped (KNOWN-DEFECTS D1).
    """
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/info", response_model=RunnerInfo)
def runner_info() -> RunnerInfo:
    """Report the model identity and the windows this runner enforces.

    Answers from configuration and does NOT load the model — a caller polling this to find
    a healthy runner must not be what pulls several GB onto the GPU. ``model_loaded`` says
    which state the process is in.
    """
    settings = get_settings()
    service = get_embedding_service()
    loaded = service.model_loaded
    return RunnerInfo(
        model=service.model_name,
        device=service.device,
        dims=service.model.get_sentence_embedding_dimension() if loaded else None,
        doc_window=settings.embedding_doc_window,
        token_window=settings.embedding_token_window,
        token_top_percent=service.token_top_percent,
        model_loaded=loaded,
    )


@router.post("/embed", response_model=RunnerEmbedResponse)
def runner_embed(request: RunnerEmbedRequest) -> RunnerEmbedResponse:
    """Return the single document vector for `text`, over the model's full window."""
    service = get_embedding_service()
    try:
        vec = service.embed_text(request.text, normalize=request.normalize, prefix=request.prefix)
    except TextTooLongError as exc:
        raise _too_long(exc) from exc

    fit = service.check_fit(request.text, with_tokens=False, prefix=request.prefix)
    return RunnerEmbedResponse(
        model=service.model_name,
        dims=int(vec.shape[0]),
        dtype=DOC_DTYPE,
        document_embedding=encode_vector(vec, DOC_DTYPE),
        fit=RunnerFit(token_count=fit.token_count, limit=fit.limit, chars_total=fit.chars_total),
    )


@router.post("/embed/tokens", response_model=RunnerEmbedTokensResponse)
def runner_embed_tokens(request: RunnerEmbedTokensRequest) -> RunnerEmbedTokensResponse:
    """Return the document vector plus the kept token vectors, for late interaction.

    Token order is by importance, highest first, and row `i` of the matrix belongs to
    `tokens[i]`. That is the order the storage side already assigns tiers in, so it can
    write the rows out as they arrive.
    """
    service = get_embedding_service()
    try:
        result = service.embed_with_tokens(
            request.text, top_percent=request.top_percent, prefix=request.prefix
        )
    except TextTooLongError as exc:
        raise _too_long(exc) from exc

    doc_vec = result.document_embedding
    native_dims = int(doc_vec.shape[0])
    token_dims = request.token_dims or native_dims
    if token_dims > native_dims:
        raise HTTPException(
            status_code=400,
            detail=(
                f"token_dims={token_dims} exceeds the model's {native_dims} dimensions. "
                "Matryoshka truncation can only make a vector shorter."
            ),
        )

    # Truncate on this side when asked. It is the same pure-numpy slice-and-renormalize the
    # caller would run, and doing it here is what makes the response a third the size.
    rows = [service.truncate_embedding(t.embedding, token_dims) for t in result.token_embeddings]
    matrix = np.stack(rows) if rows else np.empty((0, token_dims), dtype=np.float32)

    fit = service.check_fit(request.text, with_tokens=True, prefix=request.prefix)
    return RunnerEmbedTokensResponse(
        model=service.model_name,
        dims=native_dims,
        dtype=DOC_DTYPE,
        document_embedding=encode_vector(doc_vec, DOC_DTYPE),
        token_dims=token_dims,
        token_dtype=TOKEN_DTYPE,
        tokens=[
            RunnerTokenItem(
                token_idx=t.token_idx,
                token_text=t.token_text,
                importance_score=t.importance_score,
            )
            for t in result.token_embeddings
        ],
        token_embeddings=encode_vector(matrix, TOKEN_DTYPE),
        fit=RunnerFit(token_count=fit.token_count, limit=fit.limit, chars_total=fit.chars_total),
    )
