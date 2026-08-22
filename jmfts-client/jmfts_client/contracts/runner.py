"""The runner protocol: embeddings as a service, for a caller that stores them elsewhere.

The rest of the contracts package describes JMFTS's *documents*. This module describes a
narrower exchange with no documents in it at all. A caller sends text; a runner sends back
vectors, the model that produced them, and the fit measurement it used. The runner never
learns what the text was part of, and it needs no database to answer.

That is what makes one GPU able to serve several isolated JMFTS databases: the embedding
step is the only part of ingestion that wants the accelerator, and it is also the only part
with no state of its own.

Wire format
-----------
Vectors travel base64-encoded, little-endian, in the dtype they will be STORED in:

* ``float32`` for the document vector, matching ``documents.embed`` (``vector(768)``).
* ``float16`` for the token vectors, matching ``token_embeddings.embed_256``
  (``halfvec(256)``).

So the encoding loses nothing that survives the write anyway. Measured on a 256x256 matrix
of unit-norm rows, which is what one chunk of kept tokens actually looks like: 171 KB
base64 against 1398 KB for the same numbers as JSON decimals, and a worst-case component
error of 1.1e-4 — the half-precision rounding the ``halfvec`` column would apply regardless.
The document vector round-trips exactly, being float32 on both sides.

That factor of eight is the whole reason for the encoding. The token matrix is the payload;
everything else in these responses is a rounding error next to it.

See ``decode_vector`` and ``decode_matrix`` for the read side — callers should use those
rather than reimplement the layout.

numpy is an EXTRA, not a dependency
-----------------------------------
The four codec functions below are the only place in this distribution that wants numpy,
and they are needed by the narrow set of callers that actually move vectors — a runner, or
something storing embeddings itself. A consumer that searches and reads documents never
calls them, and making every such consumer install numpy to import a Pydantic model would
undo the reason this package is separate.

So numpy is imported inside the functions, behind :func:`_require_numpy`, and declared as
``jmfts-client[vectors]``. The models in this module carry the base64 strings and import
with no numpy at all; only decoding one needs it, and asking for that without the extra
raises a message naming the extra. This is the same shape as ``jmfts[office]`` on the
server side, for the same reason.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:  # annotations only — no runtime import, so no runtime dependency
    import numpy as np


class VectorCodecNotInstalled(ImportError):
    """Raised when a codec function is called in an install without numpy."""


_INSTALL_HINT = (
    "Encoding and decoding runner vectors needs numpy, which this client does not "
    "install by default — most callers never move a raw vector. Install it:\n"
    "    pip install 'jmfts-client[vectors]'"
)


def _require_numpy():
    """The ``numpy`` module, or say what is missing and what to do."""
    try:
        import numpy
    except ImportError as exc:
        raise VectorCodecNotInstalled(f"{exc}\n\n{_INSTALL_HINT}") from exc
    return numpy


# The dtypes are fixed by the columns the numbers land in, not chosen per request, so they
# are named here once and reported in every response rather than negotiated.
DOC_DTYPE = "float32"
TOKEN_DTYPE = "float16"


def encode_vector(vec: "np.ndarray", dtype: str) -> str:
    """Base64-encode an array as little-endian ``dtype``, row-major.

    Byte order is pinned rather than inherited, so a big-endian host would produce the
    same bytes a little-endian one does instead of vectors that decode to noise.
    """
    np = _require_numpy()
    arr = np.ascontiguousarray(np.asarray(vec, dtype=np.dtype(dtype).newbyteorder("<")))
    return base64.b64encode(arr.tobytes()).decode("ascii")


def decode_vector(blob: str, dtype: str) -> "np.ndarray":
    """Decode a base64 vector written by ``encode_vector``."""
    np = _require_numpy()
    return np.frombuffer(base64.b64decode(blob), dtype=np.dtype(dtype).newbyteorder("<"))


def decode_matrix(blob: str, dtype: str, dims: int) -> "np.ndarray":
    """Decode a base64 row-major matrix into shape ``(-1, dims)``.

    Raises ``ValueError`` if the byte count is not a whole number of rows, because a
    partial row means the payload and the token list disagree and no useful vector can be
    recovered from what arrived.
    """
    flat = decode_vector(blob, dtype)
    if dims <= 0 or flat.size % dims:
        raise ValueError(
            f"Token matrix has {flat.size} values, which is not a whole number of "
            f"{dims}-dimensional rows."
        )
    return flat.reshape(-1, dims)


class RunnerFit(BaseModel):
    """The measurement the runner actually used to accept the text.

    Reported on success as well as failure. A caller that chunks locally with its own
    tokenizer can compare this against its own count and find out that the two sides
    disagree — which is the observable symptom of running different models — without
    anything having to compare model names.
    """

    token_count: int = Field(..., description="Tokens in the prefixed text")
    limit: int = Field(..., description="Window this path is bounded by")
    chars_total: int = Field(..., description="Characters in the text, excluding the prefix")


class RunnerInfo(BaseModel):
    """What this runner is, so a caller can record it alongside the vectors it stores."""

    model: str = Field(..., description="Embedding model identity")
    device: str = Field(..., description="Device the model runs on, e.g. 'cuda' or 'cpu'")
    dims: int | None = Field(
        None,
        description=(
            "Native dimensionality of the document vector, or null when the model is not "
            "loaded yet. Null rather than a configured guess: this endpoint must not load "
            "the weights to answer, and reporting a number nobody measured is how a wrong "
            "one gets believed. Every embed response carries the real width."
        ),
    )
    doc_window: int = Field(..., description="Token limit for the document-vector path")
    token_window: int = Field(..., description="Token limit for the token/maxsim path")
    token_top_percent: float = Field(..., description="Default fraction of tokens kept")
    model_loaded: bool = Field(
        ...,
        description=(
            "Whether the model weights are resident. False means the first embed request "
            "pays the load; the tokenizer alone answers this endpoint."
        ),
    )

    # `model` and `model_loaded` collide with Pydantic v2's protected `model_` namespace.
    # The field names are the ones a reader expects on the wire, so the guard is what gives.
    model_config = {"protected_namespaces": ()}


class RunnerEmbedRequest(BaseModel):
    """Ask for a document vector alone — the 8192-token path, no attention pass."""

    text: str = Field(..., description="Text to embed")
    prefix: str = Field(
        "search_document: ",
        description=(
            "Task prefix for the model. The caller owns this, because only the caller "
            "knows whether it is storing a document or asking a question."
        ),
    )
    normalize: bool = Field(True, description="L2-normalize the returned vector")


class RunnerEmbedResponse(BaseModel):
    model: str
    dims: int
    dtype: str = DOC_DTYPE
    document_embedding: str = Field(..., description="Base64 little-endian float32 vector")
    fit: RunnerFit

    model_config = {"protected_namespaces": ()}


class RunnerTokenItem(BaseModel):
    """One kept token's metadata. Its vector is row ``i`` of the response matrix."""

    token_idx: int = Field(..., description="Index in the unprefixed text")
    token_text: str
    importance_score: float


class RunnerEmbedTokensRequest(BaseModel):
    """Ask for the document vector AND the kept token vectors — the late-interaction path.

    This is the expensive verb. It runs the transformer with ``output_attentions=True``,
    which materialises layers x heads x seq^2 floats, and it is bounded by
    ``embedding_token_window`` for that reason rather than by anything the model cannot do.
    """

    text: str = Field(..., description="Text to embed; must fit the token window")
    prefix: str = Field("search_document: ", description="Task prefix for the model")
    top_percent: float | None = Field(
        None,
        gt=0.0,
        le=1.0,
        description="Fraction of tokens to keep; the runner's configured default if omitted",
    )
    token_dims: int | None = Field(
        None,
        gt=0,
        description=(
            "Truncate each token vector to this many dimensions and re-normalize, the "
            "matryoshka property. Set it to what you will store — 256 for JMFTS's "
            "halfvec(256) column — and the response is a third the size. Omitted means "
            "full width, and the caller truncates."
        ),
    )


class RunnerEmbedTokensResponse(BaseModel):
    model: str
    dims: int = Field(..., description="Dimensionality of the document vector")
    dtype: str = DOC_DTYPE
    document_embedding: str = Field(..., description="Base64 little-endian float32 vector")

    token_dims: int = Field(..., description="Dimensionality of each token vector")
    token_dtype: str = TOKEN_DTYPE
    tokens: list[RunnerTokenItem] = Field(
        ..., description="Kept tokens, highest importance first; row order of the matrix"
    )
    token_embeddings: str = Field(
        ...,
        description=(
            "Base64 little-endian float16, row-major, len(tokens) rows of token_dims. "
            "Decode with decode_matrix()."
        ),
    )
    fit: RunnerFit

    model_config = {"protected_namespaces": ()}
