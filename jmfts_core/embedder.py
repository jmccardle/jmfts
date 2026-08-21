"""What turns text into vectors for THIS process — the model, or somebody else's.

``jmfts_core.embedding.EmbeddingService`` does five things that callers outside it use, and
only two of them need the model weights:

    check_fit / fits_token_window / chunk_to_fit    the tokenizer, no GPU
    truncate_embedding                              numpy, a slice and a division
    embed_text / embed_with_tokens                  the model, the GPU

:class:`RemoteEmbedder` replaces the last two with a call to another JMFTS's ``/runner``
surface and delegates the other three to a local :class:`EmbeddingService`, which answers
them from the tokenizer and never touches ``self.model``. So a process holding this object
can still decide what fits, still chunk to the window, and still truncate a matryoshka
vector — it just cannot produce one.

That is also the line ``pyproject.toml`` draws: the first three are base JMFTS, the last two
are the ``embed`` extra. A process holding one of these does not need the extra installed at
all, which is why ``EmbeddingService`` reports :class:`~jmfts_core.embedding
.ModelStackNotInstalled` rather than a bare ImportError — an install that measures but
cannot embed is this deployment, not a broken one. Measured: 583 MB installed against
5.2 GB with the stack; 689 MB as a worker image against 3.98 GB.

WHY THAT SPLIT IS WORTH HAVING. Ingest work is not uniform and the expensive part is now
one task type (``embed``). A worker that runs everything except that task type needs no
accelerator and no weights, so the GPUs a corpus needs are not pinned to whichever pool
happens to ingest: the storage-side workers scale on CPU, the embedding pool scales on
cards, and both drain the same queue. Set ``JMFTS_RUNNER_URL`` on a worker and it stops
loading the model for ingest.

WHAT THIS DOES NOT COVER. Search embeds its queries through
``jmfts_core.embedding.get_embedding_service`` directly and is untouched by this module. A
process that answers ``/search`` therefore still loads the model, whatever is set here.
That is not an oversight to be tidied later: a query embed is one small forward pass in the
request path, and routing it over HTTP would put a network hop inside every search. The
process this empties out is a worker, and a worker serves no queries.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

import httpx
import numpy as np

from jmfts_core.config import get_settings
from jmfts_core.contracts.runner import (
    RunnerEmbedResponse,
    RunnerEmbedTokensResponse,
    RunnerInfo,
    decode_matrix,
    decode_vector,
)
from jmfts_core.embedding import (
    EmbeddingResult,
    EmbeddingService,
    TextTooLongError,
    TokenEmbeddingResult,
    get_embedding_service,
)

logger = logging.getLogger(__name__)


class EmbedderMismatchError(RuntimeError):
    """The runner refused text this process measured as fitting inside the window.

    Both sides tokenize with ``Settings.embedding_model``, so agreeing on whether a text
    fits is the cheapest observable check that they are running the same one. When they
    disagree, the local tokenizer's chunking is producing pieces the remote model cannot
    embed, and every chunk of every document is affected — which is worth naming rather
    than retrying, because a retry sends the same bytes to the same model.

    Not a ``ValueError``: ``classify_exception`` would make it PERMANENT on the NODE, and
    the node is not what is wrong.
    """


class RemoteEmbedder:
    """``embed_text`` and ``embed_with_tokens`` over another JMFTS's ``/runner`` surface.

    Everything else is delegated to ``local``, explicitly and one method at a time. There
    is deliberately no ``__getattr__`` forwarding: it would also forward ``.model``, and
    the one thing this class exists to guarantee is that nothing in the ingest path pulls
    the weights into a process that was deployed without room for them.
    """

    def __init__(
        self,
        base_url: str,
        *,
        key: str,
        local: Optional[EmbeddingService] = None,
        timeout: Optional[float] = None,
        token_dims: Optional[int] = None,
    ):
        settings = get_settings()
        self.base_url = base_url.rstrip("/")
        self.key = key
        #: The tokenizer-and-numpy half. A real EmbeddingService, because those methods are
        #: already model-free; holding one is not holding a model.
        self.local = local if local is not None else get_embedding_service()
        #: Truncate token vectors on the RUNNER, at this width, instead of receiving full
        #: width and truncating here. ``None`` — the default — means full width, which is
        #: exactly what the local path returns, so this class substitutes for it with no
        #: change in what any caller gets. Set it only if you know what you store: JMFTS's
        #: own column is ``halfvec(256)``, and asking for 256 makes the response a third
        #: the size. Guessing wrong here is a silently short vector.
        self.token_dims = token_dims

        # One client for the object's life. A new one per call would open a TCP connection
        # and negotiate TLS once per CHUNK, which on a document is most of the wall clock.
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout if timeout is not None else settings.runner_timeout,
            headers={"Authorization": f"Bearer {key}"},
        )
        self._lock = threading.Lock()
        self._info: Optional[RunnerInfo] = None

    # -- identity ----------------------------------------------------------------------

    @property
    def model_name(self) -> str:
        """The model name this process is CONFIGURED with, not the runner's.

        Read from settings rather than from ``/runner/info`` because it is read in the
        request path — ``embed_document`` records it — and a network call there would make
        an attribute access fail. :meth:`info` is the one that asks the runner, and the
        two disagreeing is what ``fit`` in every response is for.
        """
        return self.local.model_name

    @property
    def device(self) -> str:
        """Where the vectors are produced, from this process's point of view."""
        return f"remote:{self.base_url}"

    @property
    def token_top_percent(self) -> float:
        return self.local.token_top_percent

    def info(self) -> RunnerInfo:
        """``GET /runner/info``, fetched once and remembered.

        Cached because it describes a process, not a request: a runner does not change
        model between two chunks of one document. A runner that is REPLACED by one holding
        a different model is a restart of this worker's business, and the cache is the
        length of this object.
        """
        with self._lock:
            if self._info is None:
                response = self._client.get("/runner/info")
                response.raise_for_status()
                self._info = RunnerInfo.model_validate(response.json())
            return self._info

    # -- the tokenizer and numpy half, delegated --------------------------------------

    def check_fit(self, text, with_tokens=True, prefix="search_document: "):
        return self.local.check_fit(text, with_tokens=with_tokens, prefix=prefix)

    def fits_token_window(self, text, prefix="search_document: ") -> bool:
        return self.local.fits_token_window(text, prefix=prefix)

    def chunk_to_fit(self, text, strategy=None, max_chars=None, prefix="search_document: "):
        return self.local.chunk_to_fit(text, strategy=strategy, max_chars=max_chars, prefix=prefix)

    def truncate_embedding(self, embedding, target_dim, normalize=True):
        return self.local.truncate_embedding(embedding, target_dim, normalize=normalize)

    # -- the model half, over HTTP ------------------------------------------------------

    def embed_text(self, text: str, normalize: bool = True, prefix: str = "") -> np.ndarray:
        """``POST /runner/embed``. Returns the same array shape the local path returns."""
        payload = self._post(
            "/runner/embed",
            {"text": text, "prefix": prefix, "normalize": normalize},
            text=text,
            with_tokens=False,
            prefix=prefix,
        )
        body = RunnerEmbedResponse.model_validate(payload)
        return decode_vector(body.document_embedding, body.dtype).astype(np.float32)

    def embed_with_tokens(
        self,
        text: str,
        top_percent: Optional[float] = None,
        token_selector=None,
        prefix: str = "",
    ) -> EmbeddingResult:
        """``POST /runner/embed/tokens``, rebuilt into the local path's return type.

        ``token_selector`` is accepted and REFUSED when set. Selection happens where the
        attentions are, which is the runner, and silently ignoring a custom selector would
        return vectors chosen by a policy the caller asked to replace.
        """
        if token_selector is not None:
            raise ValueError(
                "token_selector is a policy applied to attention matrices, which exist "
                f"only on the runner at {self.base_url}. A remote embedder cannot apply "
                "one; run the model in this process to use a custom selector."
            )

        request: dict = {"text": text, "prefix": prefix}
        if top_percent is not None:
            request["top_percent"] = top_percent
        if self.token_dims is not None:
            request["token_dims"] = self.token_dims

        payload = self._post(
            "/runner/embed/tokens", request, text=text, with_tokens=True, prefix=prefix
        )
        body = RunnerEmbedTokensResponse.model_validate(payload)

        matrix = decode_matrix(body.token_embeddings, body.token_dtype, body.token_dims)
        if matrix.shape[0] != len(body.tokens):
            raise ValueError(
                f"the runner returned {matrix.shape[0]} token vectors for "
                f"{len(body.tokens)} tokens; the matrix and the metadata describe "
                "different token sets and neither can be trusted"
            )

        # float32 in memory even though the wire is float16, so a token vector from a
        # runner is the same dtype as one from the local model. Renormalizing a float16
        # row in place would round twice.
        rows = matrix.astype(np.float32)
        return EmbeddingResult(
            document_embedding=decode_vector(body.document_embedding, body.dtype).astype(
                np.float32
            ),
            token_embeddings=[
                TokenEmbeddingResult(
                    token_idx=item.token_idx,
                    token_text=item.token_text,
                    importance_score=item.importance_score,
                    embedding=rows[index],
                )
                for index, item in enumerate(body.tokens)
            ],
        )

    # -- the one status code that is not an HTTP error -------------------------------

    def _post(self, path: str, json: dict, *, text: str, with_tokens: bool, prefix: str) -> dict:
        """POST and return the parsed body, translating 400 back into the local exception.

        The runner answers over-window text with 400 (see ``routers/runner.py::_too_long``),
        and the caller of an embedder has to see that as :class:`TextTooLongError` — same
        as the local path — or the chunking cue is lost and the retry policy is wrong: an
        ``HTTPStatusError`` is PERMANENT on the node without ever saying it was the length.

        The numbers in the raised error are measured HERE rather than parsed out of the
        runner's message, because this process has the tokenizer and can just ask it. When
        that measurement says the text fits and the runner says it does not, the two sides
        are not running the same model and :class:`EmbedderMismatchError` says so.
        """
        response = self._client.post(path, json=json)
        if response.status_code == 400:
            fit = self.local.check_fit(text, with_tokens=with_tokens, prefix=prefix)
            if not fit.truncated:
                raise EmbedderMismatchError(
                    f"the runner at {self.base_url} refused {fit.chars_total} characters "
                    f"that this process measures as {fit.token_count} tokens, inside its "
                    f"{fit.limit}-token window. Both sides tokenize with "
                    f"{self.local.model_name!r}, so they are not running the same model. "
                    f"The runner said: {_detail(response)}"
                )
            raise TextTooLongError(
                fit.token_count,
                fit.limit,
                fit.chars_total,
                "token/maxsim" if with_tokens else "document-vector",
            )
        response.raise_for_status()
        return response.json()

    def close(self) -> None:
        self._client.close()


def _detail(response: httpx.Response) -> str:
    """A FastAPI error body's ``detail``, or the raw text if it is not one."""
    try:
        return str(response.json().get("detail", response.text))
    except ValueError:
        return response.text


_embedder = None
_embedder_lock = threading.Lock()


def get_embedder():
    """The embedder for the ingest write path in this process. Local unless told otherwise.

    Returns :func:`~jmfts_core.embedding.get_embedding_service`'s singleton when
    ``JMFTS_RUNNER_URL`` is blank, which is every single-appliance deployment, and a
    :class:`RemoteEmbedder` when it is set. The two are interchangeable at every call site
    that reads this, which is the point: no caller branches on which one it has.

    A runner URL with no runner key is refused rather than defaulted to an unauthenticated
    call. The surface answers 401 without one, so the alternative is not "it works
    anonymously", it is every ingest task failing one request later with a message about
    credentials instead of about configuration.
    """
    global _embedder
    settings = get_settings()
    if not settings.runner_url:
        return get_embedding_service()

    with _embedder_lock:
        if _embedder is None or _embedder.base_url != settings.runner_url.rstrip("/"):
            if not settings.runner_key:
                raise ValueError(
                    f"JMFTS_RUNNER_URL is set to {settings.runner_url!r} and "
                    "JMFTS_RUNNER_KEY is empty. The /runner surface answers 401 without "
                    "one; both sides read the same key (deploy/k8s/11-secret.example.yaml)."
                )
            logger.info(
                "embedding for ingest goes to the runner at %s; this process will not "
                "load the embedding model for it",
                settings.runner_url,
            )
            _embedder = RemoteEmbedder(settings.runner_url, key=settings.runner_key)
    return _embedder


def reset_embedder() -> None:
    """Drop the cached remote embedder, closing its connection pool.

    For a test that changes the setting, and for a caller that wants the next call to
    re-read configuration. The local service is not touched: it is
    ``embedding.get_embedding_service``'s singleton and this module does not own it.
    """
    global _embedder
    with _embedder_lock:
        if _embedder is not None:
            _embedder.close()
        _embedder = None
