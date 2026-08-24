"""JMFTS Configuration"""

from typing import ClassVar
from urllib.parse import quote_plus
from pydantic import model_validator
from pydantic_settings import BaseSettings
from functools import lru_cache


class LlmNotConfiguredError(RuntimeError):
    """Raised when LLM-backed work is asked for and no endpoint is configured.

    JMFTS does not ship an LLM. Rather than defaulting to somebody's address and failing
    later as a connection error, the unconfigured case is named here, at the point of use,
    where the message can say which variable to set.
    """

    def __init__(self, what: str):
        super().__init__(
            f"{what} needs an LLM endpoint and none is configured. Set JMFTS_LLM_BASE_URL "
            "and JMFTS_LLM_MODEL to any OpenAI-compatible server (llama-server, vLLM, "
            "Ollama, or a hosted API). JMFTS does not ship an LLM."
        )


class Settings(BaseSettings):
    """Application settings loaded from environment variables"""

    # Database
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "jmfts"
    db_user: str = "jmfts"
    db_password: str = ""

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8100
    debug: bool = True

    # Shared-bearer auth (CR-4). Every request carries `Authorization: Bearer
    # <token>`, compared constant-time against this value. If left empty,
    # api/auth.py GENERATES an ephemeral token once at startup and prints it to
    # stdout (generate-print-require, like Jupyter) — a missing token NEVER
    # means "allow all". Set JMFTS_API_TOKEN to pin a fixed token.
    api_token: str = ""  # env JMFTS_API_TOKEN

    # Runner key — the credential for the /runner surface, which is a DIFFERENT kind of
    # credential from `api_token` and must not be confused with one. A bearer on the
    # regular API resolves to a *principal*, and that principal's grants filter which
    # subtree the request may see. The runner surface has no subtree: it answers "embed
    # this text" for a caller that holds the documents somewhere else entirely, possibly
    # in a database this process cannot reach. There is nothing there for a principal to
    # scope, so `require_runner` binds none.
    #
    # It is a shared secret rather than a row in `api_tokens` because the process that
    # checks it may have no database at all — an embedding-only deployment is a model, a
    # tokenizer, and an HTTP port. Two pods in one namespace read the same Secret and that
    # is the whole handshake.
    #
    # Blank is the OFF switch, unlike `api_token` where blank means generate-print-require.
    # The difference is deliberate: generating a key per process is right for one appliance
    # printing a token an operator reads, and wrong for a fleet, where two replicas would
    # generate two different keys and a worker would authenticate against whichever pod it
    # happened to reach. With no key set the /runner routes answer 503, which reads as
    # "this deployment does not offer embedding" — never as "come in".
    runner_key: str = ""  # env JMFTS_RUNNER_KEY

    # The CLIENT half of the same pair. `runner_key` above says what this process ACCEPTS
    # on /runner; this says where it SENDS text when it needs a vector and would rather not
    # produce one itself. Both read the same key, which is the whole handshake: two pods in
    # one namespace mount one Secret, one of them serves the surface and the other posts to
    # it (deploy/k8s/11-secret.example.yaml).
    #
    # Blank means "embed locally", which is what a single appliance does and is the reason
    # blank is the default rather than an error. Set it and this process stops loading the
    # embedding model for ingest work — see jmfts_core/embedder.py::get_embedder.
    #
    # SCOPE, stated because the name is broader than the behaviour: this covers the INGEST
    # WRITE path — `embed` and the vector `summarize` writes. Search still embeds its
    # queries locally. A process that serves /search therefore still needs the model, and
    # setting this on one does not make it modelless; the process this empties out is a
    # worker.
    runner_url: str = ""  # env JMFTS_RUNNER_URL

    # How long to wait on one /runner call. A cold runner loads several GB before it can
    # answer, so this is sized for that first request rather than for the steady state.
    runner_timeout: float = 300.0  # env JMFTS_RUNNER_TIMEOUT

    # CORS allow-list (CR-4). Empty (the default) = server-to-server only: no
    # browser Origin is permitted, which is the expected case (the τ client is
    # httpx and ignores CORS). Never "*" alongside allow_credentials. Set
    # JMFTS_CORS_ORIGINS to a JSON list of explicit origins.
    cors_origins: list[str] = []  # env JMFTS_CORS_ORIGINS

    # In-process ingest worker (INGEST_SPEC.md 5.8). The file pipeline is asynchronous:
    # POST /ingest/file returns once the bytes are stored and `probe` is enqueued, and a
    # worker THREAD inside this process drains the queue. On by default — an appliance
    # that accepts uploads and never processes them is not a useful default — and pinned
    # off by tests/conftest.py, because roughly half the suite's TestClient fixtures run
    # the lifespan and half do not, so an unconditional worker would be alive in some
    # unrelated tests and dead in others. Tests that want work done drive
    # IngestWorker.drain() synchronously instead.
    ingest_worker_enabled: bool = True  # env JMFTS_INGEST_WORKER_ENABLED
    ingest_worker_poll_seconds: float = 1.0  # env JMFTS_INGEST_WORKER_POLL_SECONDS

    # The worker fleet (migration 011). A worker that holds a task touches
    # `task_queue.heartbeat_at` every `worker_heartbeat_seconds`; any worker may reap a
    # task whose last beat is older than `worker_lease_seconds`.
    #
    # The lease bounds how long a LIVE worker may go without reporting in — not how long a
    # task may run, which is unbounded here by design. So it is sized against the things
    # that delay a beat rather than against the work: a GC pause, a database that is
    # briefly unreachable, a container that is CPU-throttled while another pod loads a
    # model. 90s over a 10s beat means eight consecutive missed beats before a live worker
    # loses a task it is still running.
    #
    # Reaping is a floor on recovery latency, not a target: `requeue_stale_claims` still
    # recovers a restarting worker's own rows immediately, and the lease is what covers the
    # worker that does not come back.
    worker_heartbeat_seconds: float = 10.0  # env JMFTS_WORKER_HEARTBEAT_SECONDS
    worker_lease_seconds: float = 90.0  # env JMFTS_WORKER_LEASE_SECONDS
    # How often a worker TRIES to reap. Only the one that wins the advisory lock does any
    # work, so this is per-worker and the fleet's actual reaping rate does not scale with
    # its size.
    worker_reap_seconds: float = 30.0  # env JMFTS_WORKER_REAP_SECONDS

    # Bearer token for the LLM endpoint, sent as `Authorization: Bearer ...` and ONLY when
    # non-empty. Blank by default because the appliance's own llama-server wants no auth;
    # a worker pool that forwards to a metered web API sets it from a Secret. This is what
    # lets one worker image serve a local model and a paid API without a code path for each.
    llm_api_key: str = ""  # env JMFTS_LLM_API_KEY

    # Task-type -> service_badge, the routing policy `enqueue` applies when the caller does
    # not name a badge itself. EMPTY BY DEFAULT, and that is load-bearing rather than
    # cautious: a `cpu`-badged worker will not claim `gpu`-badged work, so a cluster that
    # applies a GPU policy without running a GPU worker does not run slowly, it stalls
    # silently. See jmfts_core/task_routing.py, which holds the measured policy as a named
    # constant for a deployment to copy deliberately.
    #
    #   JMFTS_TASK_BADGES='{"embed":"gpu","summarize":"gpu"}'
    task_badges: dict[str, str] = {}  # env JMFTS_TASK_BADGES (JSON object)

    #: Minimum lease-to-beat ratio. Below this a worker that misses one or two beats to
    #: ordinary scheduling noise loses a task it is still running, and the task then runs
    #: TWICE — concurrently, against the same tree region, which is the exact failure the
    #: write-mode reservation exists to prevent.
    MIN_LEASE_TO_HEARTBEAT_RATIO: ClassVar[float] = 3.0

    # Embedding model
    embedding_model: str = "nomic-ai/modernbert-embed-base"
    embedding_device: str = "cpu"  # "cuda" for GPU-accelerated bulk ingestion
    embedding_batch_size: int = 32
    token_batch_size: int = 32  # Documents per batch for token-level embedding

    # Embedding windows (see docs/KNOWN-DEFECTS.md, D1)
    #
    # The model handles 8192 tokens. The token-level path is capped far lower
    # because embed_with_tokens runs the transformer with output_attentions=True
    # under eager attention, which materialises every layer's full attention
    # matrix: layers * heads * seq^2 * 4 bytes. For ModernBERT-base (22 layers,
    # 12 heads) that is ~277 MB at 512, ~4.4 GB at 2048 and ~71 GB at 8192.
    #
    # So embedding_token_window is a memory budget, not a model limit — which is
    # why it must never be exceeded silently. Text over it is refused, not
    # truncated to a prefix that looks faithful.
    embedding_token_window: int = 512  # token/maxsim path (attention-memory bound)
    embedding_doc_window: int = 8192  # document-vector path (the model's real limit)

    # Matryoshka dimensions to use for token embeddings
    # Full doc embeddings use 768 (base model), matryoshka validated down to 256
    token_embed_dims: list[int] = [256]  # Only 256, dropped 384

    # Late interaction settings
    token_top_percent: float = 0.50  # Store top 50% of tokens for tiered benchmarking

    # Chunking (see docs/KNOWN-DEFECTS.md, D2 and D4)
    #
    # A hard character cap enforced by chunk_text after splitting, for every
    # strategy. Chunks are produced to be embedded, so the cap is sized to land
    # inside embedding_token_window: English runs ~5 chars/subword-token, so
    # 1800 chars is ~360 tokens, leaving headroom for the "search_document: "
    # prefix and for text that tokenises less efficiently than prose.
    chunk_max_chars: int = 1800

    # BM25 defaults
    bm25_k1: float = 1.2
    bm25_b: float = 0.75
    # `entities` is the ROOT that holds entity nodes (SPRINT_0_3_0.md 7.5), `entity` is the
    # nodes under it. Both are held out: a root is a container with no content, so it
    # cannot match a vector or BM25 query anyway, but its title can match a full-text one
    # and "Entities" is a plausible thing to type. Neither is ever the answer to a search.
    bm25_exclude_usetypes: list[str] = ["entity", "entities", "summary"]
    search_exclude_usetypes: list[str] = ["entity", "entities", "summary"]

    # LLM endpoint (any OpenAI-compatible server: llama-server, vLLM, Ollama, ensonet, or
    # a metered web API).
    #
    # BLANK BY DEFAULT, and deliberately so. JMFTS does not ship an LLM, so there is no
    # address it could name that would be right for a fresh install: a default pointing at
    # some particular host or port answers nothing on anyone else's machine, and turns "you
    # have not configured an LLM" into a connection error that reads like a bug in JMFTS.
    # `llm_configured` is the question to ask, and `require_llm_url()` is what raises.
    #
    # LLM-backed work is optional. Ingestion, embedding, chunking, indexing and all four
    # search modes run with these empty; only summarize:llm, RAPTOR, fact extraction and
    # synthesis need them.
    llm_base_url: str = ""  # env JMFTS_LLM_BASE_URL
    llm_model: str = ""  # env JMFTS_LLM_MODEL
    llm_timeout: float = 0  # env JMFTS_LLM_TIMEOUT; 0 = use ensonet_timeout

    # ensonet (a GPU-aware model orchestrator) as a named second source for the same three
    # values. There are no ensonet-specific code paths — it is one possible OpenAI-compatible
    # backend — so these are a lower-precedence alias, also blank. The timeout keeps a real
    # default because it describes how long to wait, not where to connect, and a cold-start
    # model load is genuinely slow.
    ensonet_url: str = ""  # env JMFTS_ENSONET_URL
    ensonet_model: str = ""  # env JMFTS_ENSONET_MODEL
    ensonet_timeout: float = 180.0  # generous for cold-start model loading

    # Summarization request shape. These describe how to call the configured endpoint, not
    # which one — the endpoint is llm_base_url above.
    summarization_context: int = 4096
    summarization_temperature: float = 0.3

    # OpenAI-compatible endpoint settings (used by synthesis, RAPTOR, extraction)
    synthesis_max_tokens: int = 65536  # tokens for synthesis endpoint
    raptor_max_summary_tokens: int = 7168  # tokens for RAPTOR summarization
    # Suppress reasoning in summarization calls via chat_template_kwargs
    # (llama.cpp/vLLM). Reasoning models (Qwen3) otherwise spend the token
    # budget thinking; with small budgets content comes back empty and
    # extract_llm_text falls back to the reasoning trace as the "summary".
    summarization_disable_thinking: bool = True

    # RAPTOR hierarchical summarization
    raptor_max_depth: int = 5
    raptor_min_cluster_size: int = 2
    raptor_k_base: int = 5  # k-NN neighbors at layer 0
    raptor_k_step: int = 3  # k increases by this per layer
    raptor_gamma_base: float = 1.0  # Leiden resolution at layer 0
    raptor_gamma_decay: float = 0.5  # gamma multiplied by this per layer
    raptor_bridge_threshold: float = 0.7  # cosine similarity for bridge links

    # Fact extraction (#58)
    extraction_max_facts: int = 5  # max triples per segment
    extraction_confidence_threshold: float = 0.5  # min confidence to keep a triple
    extraction_entity_similarity_threshold: float = 0.8  # string similarity for entity resolution
    extraction_temperature: float = 0.1
    extraction_max_tokens: int = 4096

    # Cross-encoder reranker (second stage for ?rerank=true)
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    reranker_device: str = ""  # blank → follow embedding_device
    reranker_max_length: int = 512
    reranker_batch_size: int = 32

    @model_validator(mode="after")
    def validate_worker_lease(self) -> "Settings":
        """Refuse a lease that is too short for the beat interval.

        A deployment sets these two numbers in different places — a ConfigMap, a unit file,
        an env var somebody exported once — and nothing connects them. Getting the ratio
        wrong does not fail loudly: the fleet runs, and occasionally a task that is still
        running is reaped and started a second time on another host, concurrently, against
        the same tree region. That is a corrupted tree found weeks later, so it is checked
        here, at startup, where the reason is still attached to the cause.
        """
        floor = self.worker_heartbeat_seconds * self.MIN_LEASE_TO_HEARTBEAT_RATIO
        if self.worker_lease_seconds < floor:
            raise ValueError(
                f"worker_lease_seconds ({self.worker_lease_seconds:g}) must be at least "
                f"{self.MIN_LEASE_TO_HEARTBEAT_RATIO:g}x worker_heartbeat_seconds "
                f"({self.worker_heartbeat_seconds:g}), i.e. >= {floor:g}. Below that a "
                "worker that misses a beat to ordinary scheduling delay loses a task it is "
                "still running, and the task runs twice concurrently."
            )
        if self.worker_heartbeat_seconds <= 0:
            raise ValueError(
                f"worker_heartbeat_seconds must be positive, got "
                f"{self.worker_heartbeat_seconds:g}; a worker that never beats holds every "
                "task it claims until the lease reaps it"
            )
        return self

    @property
    def effective_reranker_device(self) -> str:
        """Device for the reranker.

        Defaults to whatever the embedding model runs on, so a CPU-only
        deployment does not have to set two variables to stay CPU-only.
        """
        return self.reranker_device or self.embedding_device

    @property
    def effective_llm_url(self) -> str:
        """The configured LLM endpoint, or "" when there is none."""
        return self.llm_base_url or self.ensonet_url

    @property
    def effective_llm_model(self) -> str:
        """The configured LLM model name, or "" when there is none."""
        return self.llm_model or self.ensonet_model

    @property
    def llm_configured(self) -> bool:
        """Whether LLM-backed work can run at all.

        Callers that can degrade honestly — a health probe, a rollup task that reports
        `skipped` with a reason — test this. Callers that cannot proceed without an answer
        use `require_llm_url` instead, so the failure names the missing variable.
        """
        return bool(self.effective_llm_url and self.effective_llm_model)

    def require_llm(self, what: str, model: str | None = None) -> tuple[str, str]:
        """Resolve ``(base_url, model)`` for an LLM call, or raise `LlmNotConfiguredError`.

        `model` is the caller's own override — a per-task `llm_model` param — and wins over
        the configured default, so a deployment can name one endpoint and route individual
        tasks to different models on it. `what` names the operation, so the message says
        which feature the caller wanted.

        Raises rather than returning empty strings because the alternative is a request to
        the URL "" with the model "", which surfaces as an httpx protocol error or a remote
        400 — neither of which mentions that no LLM was ever configured.
        """
        base_url = self.effective_llm_url
        resolved = model or self.effective_llm_model
        if not base_url or not resolved:
            raise LlmNotConfiguredError(what)
        return base_url.rstrip("/"), resolved

    @property
    def effective_llm_timeout(self) -> float:
        return self.llm_timeout or self.ensonet_timeout

    @property
    def database_url(self) -> str:
        # URL-encode password to handle special characters
        encoded_password = quote_plus(self.db_password)
        return f"postgresql://{self.db_user}:{encoded_password}@{self.db_host}:{self.db_port}/{self.db_name}"

    # `async_database_url` used to sit here, building a `postgresql+asyncpg://` URL. It had
    # zero references in code, tests, scripts, benchmarks or docs, and `asyncpg` is not a
    # dependency and is not installed — so the one thing it produced would have failed at
    # `create_engine`. The synchronous `database_url` above is the only connection string.

    class Config:
        env_prefix = "JMFTS_"
        env_file = ".env"


@lru_cache
def get_settings() -> Settings:
    return Settings()
