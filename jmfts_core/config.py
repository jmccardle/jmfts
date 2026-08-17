"""JMFTS Configuration"""

from urllib.parse import quote_plus
from pydantic_settings import BaseSettings
from functools import lru_cache


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
    bm25_exclude_usetypes: list[str] = ["entity", "summary"]
    search_exclude_usetypes: list[str] = ["entity", "summary"]

    # ensonet LLM service (GPU-aware model orchestrator)
    ensonet_url: str = "http://localhost:8853"
    ensonet_model: str = "THUDM_GLM4_32b"
    ensonet_timeout: float = 180.0  # generous for cold-start model loading

    # Generic OpenAI-compatible LLM settings (override ensonet defaults)
    llm_base_url: str = ""  # falls back to ensonet_url if empty
    llm_model: str = ""  # falls back to ensonet_model if empty
    llm_timeout: float = 0  # falls back to ensonet_timeout if 0

    # Summarization. Runs over the OpenAI-compatible endpoint above; these tune the
    # request, not a local process.
    summarization_context: int = 4096  # input budget, ~4 chars/token when packing
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

    @property
    def effective_reranker_device(self) -> str:
        """Device for the reranker.

        Defaults to whatever the embedding model runs on, so a CPU-only
        deployment does not have to set two variables to stay CPU-only.
        """
        return self.reranker_device or self.embedding_device

    @property
    def effective_llm_url(self) -> str:
        return self.llm_base_url or self.ensonet_url

    @property
    def effective_llm_model(self) -> str:
        return self.llm_model or self.ensonet_model

    @property
    def effective_llm_timeout(self) -> float:
        return self.llm_timeout or self.ensonet_timeout

    @property
    def database_url(self) -> str:
        # URL-encode password to handle special characters
        encoded_password = quote_plus(self.db_password)
        return f"postgresql://{self.db_user}:{encoded_password}@{self.db_host}:{self.db_port}/{self.db_name}"

    class Config:
        env_prefix = "JMFTS_"
        env_file = ".env"


@lru_cache
def get_settings() -> Settings:
    return Settings()
