"""The worker that embeds somewhere else. ``jmfts_core.embedder`` and ``--runner-url``.

Two claims are under test and they are different in kind.

The first is BEHAVIOURAL: a ``RemoteEmbedder`` substitutes for an ``EmbeddingService`` at
every call site the ingest write path has. That is checked by running both against the same
text and comparing the vectors, through the real ``/runner`` routes rather than a mock —
the wire format, the base64, the float16 rounding and the token-row ordering are exactly
what a mock would paper over.

The second is STRUCTURAL: a process that embeds remotely does not import torch. That one
needs a subprocess, because pytest has already imported torch by the time any of this runs,
so asserting on this process's ``sys.modules`` would prove nothing.
"""

from __future__ import annotations

import subprocess
import sys

import numpy as np
import pytest
from fastapi.testclient import TestClient

import jmfts_core.rest.main as main
import jmfts_core.rest.routers.runner as runner_router
from jmfts_core.config import get_settings
from jmfts_core.embedder import (
    EmbedderMismatchError,
    RemoteEmbedder,
    get_embedder,
    reset_embedder,
)
from jmfts_core.embedding import (
    EmbeddingService,
    TextTooLongError,
    get_embedding_service,
)

RUNNER_KEY = "test-thin-worker-key"

TEXT = (
    "Late interaction scores each query token against every document token and sums the "
    "maxima over the query, which is why the token vectors are stored at all."
)


@pytest.fixture
def runner_key(monkeypatch):
    monkeypatch.setattr(get_settings(), "runner_key", RUNNER_KEY)
    return RUNNER_KEY


@pytest.fixture
def remote(runner_key):
    """A RemoteEmbedder whose transport is the real app, served in-process.

    ``TestClient`` rather than an ``ASGITransport`` on a plain ``httpx.Client``: the runner
    routes are ``def``, so FastAPI runs them in a threadpool, and a synchronous client
    cannot drive an ASGI app without the portal TestClient brings.
    """
    embedder = RemoteEmbedder("http://testserver", key=RUNNER_KEY)
    embedder._client.close()
    embedder._client = TestClient(main.app, headers={"Authorization": f"Bearer {RUNNER_KEY}"})
    yield embedder
    embedder.close()


@pytest.fixture(autouse=True)
def _drop_cached_embedder():
    """`get_embedder` caches, and these tests change the setting it caches on."""
    yield
    reset_embedder()


def _stored_width(value) -> int:
    """How wide a ``halfvec`` column's loaded value is, whatever type the driver used.

    pgvector changed what SQLAlchemy hands back for a ``HALFVEC`` column: 0.4 returns a
    ``HalfVector`` instance, 0.5 returns a plain list. The two share no width accessor —
    ``HalfVector`` has ``dimensions()`` and defines neither ``__len__`` nor ``__iter__``
    in EITHER version, so ``len()`` raises on 0.4 and ``dimensions()`` is absent on 0.5.

    The assertion this serves is about the width that reached the column, not about which
    object the driver chose to represent it with, so it asks each type its own question.
    """
    dimensions = getattr(value, "dimensions", None)
    return dimensions() if callable(dimensions) else len(value)


# ---------------------------------------------------------------------------
# The substitution: the same vectors, over the wire
# ---------------------------------------------------------------------------


class TestARemoteEmbedderSubstitutesForTheLocalOne:
    def test_the_document_vector_matches_the_local_one(self, remote):
        local = get_embedding_service().embed_text(TEXT, prefix="search_document: ")
        over_the_wire = remote.embed_text(TEXT, prefix="search_document: ")

        assert over_the_wire.dtype == np.float32
        assert over_the_wire.shape == local.shape
        # float32 both sides, so this is a byte-for-byte round trip, not an approximation.
        np.testing.assert_allclose(over_the_wire, local, rtol=0, atol=0)

    def test_the_token_vectors_match_within_half_precision(self, remote):
        local = get_embedding_service().embed_with_tokens(TEXT, prefix="search_document: ")
        over_the_wire = remote.embed_with_tokens(TEXT, prefix="search_document: ")

        assert len(over_the_wire.token_embeddings) == len(local.token_embeddings)
        for got, want in zip(over_the_wire.token_embeddings, local.token_embeddings):
            assert got.token_idx == want.token_idx
            assert got.token_text == want.token_text
            assert got.importance_score == pytest.approx(want.importance_score, abs=1e-6)
            # The wire is float16 — the dtype `token_embeddings.embed_256` stores anyway —
            # so the tolerance is half-precision rounding and nothing else.
            assert got.embedding.dtype == np.float32
            np.testing.assert_allclose(got.embedding, want.embedding, atol=2e-3)

    def test_token_dims_truncates_on_the_runner(self, runner_key):
        """The payload optimisation, off by default and correct when asked for.

        Truncating remotely is the same slice-and-renormalize the caller would do, and it
        makes the response a third the size. It is opt-in because guessing the width wrong
        is a silently short vector, and the default has to match the local path exactly.
        """
        embedder = RemoteEmbedder("http://testserver", key=runner_key, token_dims=256)
        embedder._client.close()
        embedder._client = TestClient(main.app, headers={"Authorization": f"Bearer {runner_key}"})
        try:
            result = embedder.embed_with_tokens(TEXT, prefix="search_document: ")
        finally:
            embedder.close()

        assert result.token_embeddings
        for token in result.token_embeddings:
            assert token.embedding.shape == (256,)
            assert np.linalg.norm(token.embedding) == pytest.approx(1.0, abs=1e-2)

    def test_the_default_is_full_width(self, remote):
        native = get_embedding_service().embed_text(TEXT, prefix="search_document: ").shape[0]
        result = remote.embed_with_tokens(TEXT, prefix="search_document: ")

        assert result.token_embeddings
        assert result.token_embeddings[0].embedding.shape == (native,)

    def test_info_reports_the_runner_s_own_identity(self, remote):
        info = remote.info()
        assert info.model == get_settings().embedding_model
        assert info.token_window > 0

    def test_it_reports_where_the_vectors_came_from(self, remote):
        """`embed` writes this into the attempt log, so a corpus embedded across a mixed
        fleet says which nodes went to which runner."""
        assert remote.device == "remote:http://testserver"
        assert remote.model_name == get_settings().embedding_model


# ---------------------------------------------------------------------------
# The tokenizer half never leaves the process
# ---------------------------------------------------------------------------


class TestTheLocalHalfIsStillLocal:
    def test_check_fit_is_answered_without_a_request(self, remote):
        """`chunk_text` calls this per candidate piece. One HTTP round trip each would be
        most of a document's wall clock, which is why these four are delegated and not
        forwarded."""
        remote._client.close()  # any request from here on would raise

        assert remote.fits_token_window(TEXT) is True
        assert (
            remote.check_fit(TEXT, with_tokens=True).limit == get_settings().embedding_token_window
        )
        assert len(remote.chunk_to_fit(TEXT)) >= 1

    def test_truncate_embedding_is_numpy_and_stays_here(self, remote):
        remote._client.close()

        vector = np.arange(768, dtype=np.float32)
        truncated = remote.truncate_embedding(vector, 256)

        assert truncated.shape == (256,)
        assert np.linalg.norm(truncated) == pytest.approx(1.0, abs=1e-5)


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


class _RefusesEverything:
    """A runner-side service that calls every text too long, whatever it is."""

    model_name = "stub/model"
    device = "cpu"
    token_top_percent = 0.5
    model_loaded = False

    def check_fit(self, text, with_tokens=True, prefix="search_document: "):
        from jmfts_core.embedding import FitResult

        return FitResult(token_count=9999, limit=512, chars_total=len(text), truncated=True)

    def embed_text(self, text, normalize=True, prefix=""):
        raise TextTooLongError(9999, 512, len(text), "document-vector")

    def embed_with_tokens(self, text, top_percent=None, token_selector=None, prefix=""):
        raise TextTooLongError(9999, 512, len(text), "token/maxsim")


class TestFailureModes:
    def test_over_window_text_arrives_as_the_local_exception(self, remote):
        """A 400 must reach the caller as `TextTooLongError`, not as an HTTPStatusError.

        Both classify PERMANENT, so the retry policy is the same either way — what would be
        lost is the CUE. The chunker's contract is "raise and tell the caller to chunk",
        and a status code says nothing about length.
        """
        too_long = "sentence about retrieval. " * 3000

        with pytest.raises(TextTooLongError) as raised:
            remote.embed_with_tokens(too_long, prefix="search_document: ")

        # Measured here, from this process's own tokenizer, rather than parsed out of the
        # runner's message.
        assert raised.value.limit == get_settings().embedding_token_window
        assert raised.value.chars_total == len(too_long)

    def test_a_runner_that_refuses_text_this_side_says_fits_is_named_as_a_mismatch(
        self, remote, monkeypatch
    ):
        """Agreeing on what fits is the cheapest check that both sides run the same model.

        Not a `TextTooLongError`: the text is fine, and calling it too long would send the
        caller off to chunk something that is already inside the window, one chunk at a
        time, forever. Not a `ValueError` either — `classify_exception` would settle the
        NODE permanently failed, and the node is not what is wrong.
        """
        monkeypatch.setattr(runner_router, "get_embedding_service", _RefusesEverything)

        with pytest.raises(EmbedderMismatchError, match="not running the same model"):
            remote.embed_text(TEXT, prefix="search_document: ")

    def test_a_custom_token_selector_is_refused_rather_than_ignored(self, remote):
        """Selection happens where the attentions are. Ignoring the argument would return
        vectors chosen by the policy the caller asked to replace."""
        with pytest.raises(ValueError, match="token_selector"):
            remote.embed_with_tokens(TEXT, token_selector=object())

    def test_an_unreachable_runner_raises_a_retryable_transport_error(self, runner_key):
        """`classify_exception` maps httpx.TransportError to RETRYABLE, which is right: a
        runner pod being rescheduled is not a fact about the document."""
        import httpx

        from jmfts_core.task_errors import ErrorType, classify_exception

        embedder = RemoteEmbedder("http://127.0.0.1:1", key=runner_key, timeout=1.0)
        try:
            with pytest.raises(httpx.TransportError) as raised:
                embedder.embed_text(TEXT)
        finally:
            embedder.close()

        assert classify_exception(raised.value) is ErrorType.RETRYABLE


# ---------------------------------------------------------------------------
# The whole point: a document ingested without this process running the model
# ---------------------------------------------------------------------------


class TestIngestingThroughARunner:
    def test_a_file_ingests_end_to_end_with_the_embedder_pointed_elsewhere(
        self, db_session, monkeypatch, runner_key
    ):
        """The deployment this exists for, in one test.

        CONFIGURED, not patched: the setting is set and ``get_embedder`` is left to do what
        it does, with only the HTTP transport swapped for one that reaches the app
        in-process. Patching the function at each call site would have proved that three
        specific lines were patched — and would have hidden a fourth that was not, which is
        exactly what happened while this test was being written.
        """
        from sqlalchemy import select

        from jmfts_client.contracts.upload import UploadedFile
        from jmfts_core.models.document import Document, SETTLED_SETTLED
        from jmfts_core.models.token_embedding import TokenEmbedding
        from jmfts_core.repositories.document import DocumentRepository
        from jmfts_core.rollup_tasks import IngestRollupPlanner
        from jmfts_core.services.ingest_service import IngestService
        from jmfts_core.models.document import USETYPE_CHUNK
        from tests.conftest import drain_ingest_queue

        monkeypatch.setattr(get_settings(), "runner_url", "http://testserver")
        reset_embedder()
        embedder = get_embedder()
        assert isinstance(embedder, RemoteEmbedder)
        embedder._client.close()
        embedder._client = TestClient(main.app, headers={"Authorization": f"Bearer {runner_key}"})

        markdown = ("# Retrieval\n\n" + TEXT + "\n\n# Segmentation\n\n" + TEXT).encode()
        response = IngestService(db_session).upload_file(
            UploadedFile(data=markdown, filename="remote.md", content_type="text/markdown")
        )
        drain_ingest_queue(db_session, planner=IngestRollupPlanner(), max_tasks=200)

        node = DocumentRepository(db_session).get(response.document_id)
        assert node.settled == SETTLED_SETTLED

        chunks = list(
            db_session.execute(
                select(Document)
                .where(Document.path.contains([node.id]))
                .where(Document.usetype == USETYPE_CHUNK)
            )
            .scalars()
            .all()
        )
        assert chunks
        for chunk in chunks:
            assert chunk.settled == SETTLED_SETTLED
            assert chunk.embed is not None
            rows = (
                db_session.execute(
                    select(TokenEmbedding).where(TokenEmbedding.document_id == chunk.id)
                )
                .scalars()
                .all()
            )
            assert rows, f"chunk {chunk.id} got a document vector and no token vectors"
            # The column is halfvec(256); a remote row must land in it the same way a
            # local one does, whatever width it travelled at.
            assert _stored_width(rows[0].embed_256) == 256

        # And the attempt log says where they came from, per node.
        log = DocumentRepository(db_session).attempt_log(chunks[0])
        attempt = next(e for e in log if e["task"] == "embed")
        assert attempt["detail"]["device"] == "remote:http://testserver"


# ---------------------------------------------------------------------------
# Selecting one
# ---------------------------------------------------------------------------


class TestGetEmbedder:
    def test_blank_url_means_the_local_service(self, monkeypatch):
        monkeypatch.setattr(get_settings(), "runner_url", "")
        reset_embedder()

        assert get_embedder() is get_embedding_service()

    def test_a_url_means_a_remote_embedder(self, monkeypatch, runner_key):
        monkeypatch.setattr(get_settings(), "runner_url", "http://runner.jmfts.svc:8100/")
        reset_embedder()

        embedder = get_embedder()
        assert isinstance(embedder, RemoteEmbedder)
        # The trailing slash is dropped once, here, so paths are joined the same way every
        # time regardless of how the operator wrote the URL.
        assert embedder.base_url == "http://runner.jmfts.svc:8100"

    def test_a_url_with_no_key_is_refused_rather_than_called_anonymously(self, monkeypatch):
        """The surface answers 401 without a key, so defaulting to no credential does not
        mean "it works anonymously" — it means every ingest task fails one request later
        with a message about credentials instead of about configuration."""
        monkeypatch.setattr(get_settings(), "runner_url", "http://runner:8100")
        monkeypatch.setattr(get_settings(), "runner_key", "")
        reset_embedder()

        with pytest.raises(ValueError, match="JMFTS_RUNNER_KEY"):
            get_embedder()

    def test_the_local_half_is_a_real_embedding_service(self, monkeypatch, runner_key):
        """Not a second tokenizer implementation. Holding an EmbeddingService is not
        holding a model — the weights load in `model`, which a RemoteEmbedder never
        touches."""
        monkeypatch.setattr(get_settings(), "runner_url", "http://runner:8100")
        reset_embedder()

        assert isinstance(get_embedder().local, EmbeddingService)

    def test_holding_one_does_not_load_the_weights(self, runner_key):
        """Given a FRESH service rather than the process singleton, because that singleton
        is shared and some earlier test in any run may already have loaded it. What is
        under test is whether a RemoteEmbedder is what CAUSES a load, and the tokenizer
        half is the part most likely to drag one in by accident."""
        fresh = EmbeddingService()
        embedder = RemoteEmbedder("http://runner:8100", key=runner_key, local=fresh)
        try:
            assert embedder.fits_token_window(TEXT) is True
            assert embedder.truncate_embedding(np.ones(768, dtype=np.float32), 256).shape == (256,)
        finally:
            embedder.close()

        assert fresh.model_loaded is False


class TestTheWorkerFlag:
    def test_the_flag_points_the_process_s_embedder_at_the_runner(self, monkeypatch, runner_key):
        from jmfts_core.worker import apply_runner_override, build_parser

        monkeypatch.setattr(get_settings(), "runner_url", "")
        args = build_parser().parse_args(["--runner-url", "http://gpu-box:8100"])
        device = apply_runner_override(args)

        assert device == "remote:http://gpu-box:8100"
        assert isinstance(get_embedder(), RemoteEmbedder)

    def test_omitting_the_flag_leaves_embedding_local(self, monkeypatch):
        from jmfts_core.worker import apply_runner_override, build_parser

        monkeypatch.setattr(get_settings(), "runner_url", "")
        args = build_parser().parse_args([])

        assert apply_runner_override(args) is None
        assert get_embedder() is get_embedding_service()

    def test_the_flag_fails_at_startup_when_the_key_is_missing(self, monkeypatch):
        """A misconfiguration must stop the process, not arrive as one failed task."""
        from jmfts_core.worker import apply_runner_override, build_parser

        monkeypatch.setattr(get_settings(), "runner_url", "")
        monkeypatch.setattr(get_settings(), "runner_key", "")
        args = build_parser().parse_args(["--runner-url", "http://gpu-box:8100"])

        with pytest.raises(ValueError, match="JMFTS_RUNNER_KEY"):
            apply_runner_override(args)


# ---------------------------------------------------------------------------
# The structural claim
# ---------------------------------------------------------------------------


PROBE = """
import sys
import jmfts_core.worker            # the CLI, the parser, the loop
import jmfts_core.ingest_tasks      # every registered handler, embed included
import jmfts_core.embedder          # the remote embedder itself

heavy = sorted(m for m in ("torch", "sentence_transformers") if m in sys.modules)
print(",".join(heavy))
"""


def test_a_worker_process_does_not_import_torch_to_start():
    """The claim that makes a thin worker thin, checked where it can actually be checked.

    In a SUBPROCESS, because pytest imported torch long before this test ran — asserting on
    this process's ``sys.modules`` would pass no matter what the code did.

    This runs in an environment that HAS torch, so what it proves is that nothing on the
    worker's import path reaches for it. That torch can be absent altogether is a different
    claim, about packaging rather than about imports, and it is asserted by
    ``TestTheModelStackIsAnExtra`` below.
    """
    result = subprocess.run(
        [sys.executable, "-c", PROBE], capture_output=True, text=True, timeout=180
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", (
        f"a worker imported {result.stdout.strip()} just by starting up; the ingest path "
        "has grown an eager import of the model stack"
    )


# ---------------------------------------------------------------------------
# The packaging claim: torch is an extra, and its absence is a named state
# ---------------------------------------------------------------------------


#: Simulate the absent stack by poisoning `sys.modules`, which makes `import torch` raise
#: `ImportError: import of torch halted; None in sys.modules` — the same exception class the
#: guards catch. Done AFTER the tokenizer is loaded, because `transformers` decides whether
#: torch exists from `find_spec`, which still finds the installed one; blocking earlier
#: would break the tokenizer for a reason that does not happen on a real base install.
#:
#: `scripts/check_base_install.sh` is the version with no simulation in it at all — it
#: builds a clean venv, installs base, and runs the same assertions.
MISSING_STACK_PROBE = """
from jmfts_core.embedding import EmbeddingService, ModelStackNotInstalled
from jmfts_core.task_errors import ErrorType, classify_exception

service = EmbeddingService()
assert service.check_fit("warm the tokenizer while torch is still reachable").token_count > 0

import sys
sys.modules["torch"] = None
sys.modules["sentence_transformers"] = None

# The tokenizer half keeps working. This is the whole reason the split is possible.
assert service.fits_token_window("still measurable") is True
assert len(service.chunk_to_fit("A sentence. " * 40)) >= 1

for call in (
    lambda: service.model,
    lambda: service.embed_text("hello"),
    lambda: service.embed_with_tokens("hello"),
    lambda: service.embed_batch_with_tokens(["hello"]),
):
    try:
        call()
    except ModelStackNotInstalled as exc:
        assert "jmfts[embed]" in str(exc), "the message does not name the extra"
        assert "JMFTS_RUNNER_URL" in str(exc), "the message does not name the other way out"
        assert classify_exception(exc) is ErrorType.PERMANENT
    else:
        raise AssertionError("embedding succeeded with no model stack")

print("OK")
"""


class TestTheModelStackIsAnExtra:
    def test_base_dependencies_do_not_include_the_model_stack(self):
        """The declaration itself, because that is what a `pip install jmfts` obeys.

        A guard against the easy regression: adding `torch` back to `dependencies` because
        something imported it, which would put several GB into every storage-side worker
        and give nobody an error to notice.
        """
        import tomllib
        from pathlib import Path

        pyproject = tomllib.loads(
            (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text()
        )
        base = " ".join(pyproject["project"]["dependencies"])
        extras = pyproject["project"]["optional-dependencies"]

        assert "torch" not in base, "torch is back in the base dependencies"
        assert "sentence-transformers" not in base
        # The tokenizer IS base — check_fit, the chunker and the whole /runner client half
        # need it, and none of them need weights.
        assert "transformers" in base

        embed = " ".join(extras["embed"])
        assert "torch" in embed and "sentence-transformers" in embed
        # The suite ingests documents and asserts on real vectors, so dev has to imply it.
        assert "jmfts[embed]" in extras["dev"]

    def test_embedding_without_the_stack_is_a_named_state_not_a_broken_import(self):
        """An install that can measure but not embed is a deployment, not a fault.

        So the failure has to name which of the two things is missing — the extra, or a
        runner to ask — rather than surfacing `No module named 'torch'`, which reads as a
        broken environment and sends the reader to fix the wrong thing.
        """
        result = subprocess.run(
            [sys.executable, "-c", MISSING_STACK_PROBE],
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip().endswith("OK")
