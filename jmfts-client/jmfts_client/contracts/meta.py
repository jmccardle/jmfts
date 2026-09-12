"""Capability contracts — what an appliance accepts, before you send it anything.

``GET /capabilities`` is the endpoint an integrator reaches for first, and it exists
because nothing else answered the questions it answers. ``GET /config`` returns six tuning
numbers. ``GET /ingest/pipelines`` returns the seven entry-point usetypes, which are
deliberately not formats. ``POST /ingest/explain`` is the precise answer to "what would
this document do", and it needs the bytes first. None of them says whether the office
readers are installed, whether this process can produce a vector at all, or whether
MaxSim has anything to rank.

Every field is a fact about THIS process and its database. Nothing here is a promise about
another node in a fleet: a storage-side worker with ``JMFTS_RUNNER_URL`` set reports
``embedding.local_model_available = false`` and ``embedding.can_embed = true``, and those
are two different facts that a single "can it embed" would flatten.

NOTHING HERE MAKES A NETWORK CALL. ``llm.configured`` says a URL is set, not that anything
answers at it — ``GET /health/llm`` is the probe, and it is slow on purpose.
"""

from typing import Optional

from pydantic import BaseModel, Field


class ExtraStatus(BaseModel):
    """One optional dependency group, and whether this install has it."""

    name: str = Field(description="The extra's name in pyproject.toml, e.g. `office`.")
    installed: bool = Field(description="Whether its imports resolve in this process.")
    provides: str = Field(description="What the appliance can do only with it installed.")
    install: str = Field(description="The command that installs it.")


class EmbeddingCapability(BaseModel):
    """Where a vector comes from, and whether one can be had at all."""

    model: str
    device: str
    document_dims: int
    token_dims: int
    local_model_available: bool = Field(
        description="Whether the `embed` extra's imports resolve here, so this process can "
        "run the model itself."
    )
    runner_url: Optional[str] = Field(
        default=None,
        description="The JMFTS this process asks for vectors on the ingest write path "
        "(`JMFTS_RUNNER_URL`). Null means embed locally.",
    )
    can_embed: bool = Field(
        description="True if either a local model or a runner is available. False means "
        "asking for a vector raises ModelStackNotInstalled."
    )
    search_embeds_locally: bool = Field(
        default=True,
        description="Always true: /search embeds its queries in-process even when a runner "
        "is configured, so a process that serves search needs the model.",
    )


class RetrievalCapability(BaseModel):
    """What can be asked for, and what is silently held out of the answer."""

    methods: list[str] = Field(description="Every method name a request may fuse.")
    default_methods: list[str] = Field(description="What a hybrid request with no `methods` runs.")
    default_weights: dict[str, float] = Field(
        description="The tuned RRF multipliers. A fused method absent from this map is "
        "weighted 1.0."
    )
    search_exclude_usetypes: list[str] = Field(
        description="Usetypes held out of vector/full-text/MaxSim results when a request "
        "names no `exclude_types` (`JMFTS_SEARCH_EXCLUDE_USETYPES`)."
    )
    bm25_exclude_usetypes: list[str] = Field(
        description="Usetypes never written to a BM25 index in the first place "
        "(`JMFTS_BM25_EXCLUDE_USETYPES`). Excluded at index time, so re-indexing is what "
        "changes this, not a search argument."
    )


class IngestCapability(BaseModel):
    """What may be sent, and what the appliance can look inside."""

    usetypes: list[str] = Field(description="The entry points `POST /ingest` accepts.")
    detectable_formats: list[str] = Field(
        description="Formats identified from the bytes. Anything else is still accepted "
        "and falls back to the filename extension, which is a hint rather than evidence."
    )
    probeable_formats: list[str] = Field(
        description="The subset something can look INSIDE to measure patterns. A format "
        "outside this list gets an empty pattern set, and the structure rungs plan from "
        "that."
    )
    task_types: list[str] = Field(description="Every registered ingest task handler, by task type.")


class LlmCapability(BaseModel):
    """Whether an LLM endpoint is configured. NOT whether it answers."""

    configured: bool
    base_url: str
    model: str
    requires_llm: list[str] = Field(
        description="The operations that need it. Everything else works with it blank."
    )


class CorpusFacts(BaseModel):
    """Counts that answer "will this method return anything here?".

    Only present when the request asks for them, because each field is a ``COUNT`` over
    ``documents`` and a capability listing should not become a table scan by default.
    """

    documents: int
    documents_with_vectors: int = Field(
        description="Settled documents carrying a document embedding — what /search/vector "
        "and the vector leg of hybrid can reach."
    )
    documents_with_token_vectors: int = Field(
        description="Documents carrying per-token embeddings — what MaxSim can rank. Zero "
        "means /search/maxsim returns nothing on this corpus, whatever else is installed."
    )
    bm25_indexes: list[str] = Field(
        description="Named BM25 indexes that exist. An empty list means the bm25 leg of "
        "hybrid contributes nothing."
    )


class CapabilitiesResponse(BaseModel):
    """What this appliance accepts and can do, asked without sending it anything."""

    version: str
    extras: list[ExtraStatus]
    embedding: EmbeddingCapability
    retrieval: RetrievalCapability
    ingest: IngestCapability
    llm: LlmCapability
    corpus: Optional[CorpusFacts] = Field(
        default=None,
        description="Present only when the request asked for it (`corpus=true`).",
    )
