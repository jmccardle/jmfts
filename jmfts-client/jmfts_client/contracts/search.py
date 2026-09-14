"""Search contracts — request/response models for the search surface.

Moved out of ``api/schemas.py`` so the service layer (``jmfts_core.services``) can
depend on them without importing FastAPI. ``api/schemas.py`` re-exports these names.
"""

from datetime import datetime
from typing import Any, Optional, Sequence, Union

from pydantic import BaseModel, Field, field_validator

from jmfts_client.contracts.document import DocumentResponse

#: Every retrieval method a request may name. A CLOSED list, and closed for the reason
#: ``ingest_options`` states about its own overrides: "An override naming something that
#: does not exist RAISES." ``hybrid_search``'s fusion loop is a series of
#: ``if "vector" in methods:`` tests, so an unrecognised name contributes nothing and
#: raises nothing — ``"maxsim "`` with a trailing space, or ``"hybrid"``, used to be
#: accepted and silently ignored, and the caller got a two-method fusion labelled as the
#: four-method one they asked for.
#:
#: Declared here rather than server-side because the check has to hold for a
#: ``RemoteJmftsClient`` caller before the request leaves the process, and contracts are
#: the one definition both sides validate against.
SEARCH_METHODS: tuple[str, ...] = ("vector", "fulltext", "bm25", "maxsim")

#: What ``methods=None`` runs. The tuned production ranking, and NOT the three names the
#: field's default used to advertise: the default weights are ``{"vector": 0.86,
#: "bm25": 0.14}`` and the fusion reads ``weights.get(name, 1.0)``, so adding ``fulltext``
#: to the method list without adding a weight for it gives full-text a fused weight of 1.0
#: — seven times vector's. The old default therefore could not have run the three methods
#: it named without changing the ranking, which is why it never did.
#:
#: ``SearchResponse.applied`` reports the resolved list and the resolved weights on every
#: response, so a caller who does name ``fulltext`` can see what weight it got.
DEFAULT_HYBRID_METHODS: tuple[str, ...] = ("vector", "bm25")


def validate_search_methods(value: Optional[list[str]]) -> Optional[list[str]]:
    """Reject unknown method names and duplicates; keep the caller's order.

    ``None`` passes through as "not specified", which is what lets a named search context
    supply the list and, failing that, :data:`DEFAULT_HYBRID_METHODS` apply. An empty list
    is refused rather than treated as "all" or as "none": a fusion over no methods returns
    nothing, and a request that asks for nothing is a mistake at the call site.
    """
    if value is None:
        return None
    unknown = [m for m in value if m not in SEARCH_METHODS]
    if unknown:
        raise ValueError(
            f"unknown search method(s) {unknown}; expected any of {list(SEARCH_METHODS)}"
        )
    if not value:
        raise ValueError(
            f"methods must name at least one of {list(SEARCH_METHODS)}; "
            f"omit the field to use the server default {list(DEFAULT_HYBRID_METHODS)}"
        )
    seen = set()
    duplicates = [m for m in value if m in seen or seen.add(m)]
    if duplicates:
        raise ValueError(f"duplicate search method(s) {duplicates}")
    return value


#: What a caller may pass as a POSITIVE usetype filter: one glob, or a set of them.
#:
#: A set, because two of the three named search-context presets could not be expressed by a
#: single glob — "everything I said" spans ``transcript:*`` and ``obsidian:*``, and no one
#: glob covers both without also covering everything between them. Both spellings reach
#: :func:`usetype_globs` and mean the same thing there; which one to use is decided by the
#: transport rather than by taste:
#:
#: * ``list[str]`` is the UNAMBIGUOUS form and the one to store in a
#:   ``search_contexts.config`` blob or send in a JSON body. Its elements are never split,
#:   so it is also the only way to name a usetype that contains a comma.
#: * ``str`` is a comma-separated list of globs, and it exists because ``GET /search/``
#:   takes ``usetype`` as a URL QUERY PARAMETER, where a list is not expressible without
#:   changing that operation's signature. ``"transcript:*,obsidian:*"`` is the shape a
#:   preset was already being written in when it was found to match nothing.
UsetypeFilter = Union[str, list[str]]

#: What separates two globs in the string form of :data:`UsetypeFilter`.
#:
#: A comma is safe HERE and would not be safe for every column: ``documents.usetype`` is
#: deliberately an open string (``models/document.py`` — "Usetype stays an open string"),
#: so an application built on JMFTS may define one containing a comma. Measured against
#: this tree on 2026-09-05: no usetype it can write contains one. ``INGEST_USETYPES`` names
#: seven (``conversation``, ``markdown``, ``raw``, ``transcript``, ``wiki:arxiv``,
#: ``wiki:pdf``, ``wiki:url``), the ``USETYPE_*`` constants on the model name seven more,
#: and every literal ``usetype="..."`` assignment in ``jmfts_core`` is one of nine names —
#: none has a comma, and the taxonomy that does exist separates with ``:`` and ``/``. A
#: deployment that does define one names it in the list form, which never splits.
USETYPE_GLOB_SEPARATOR = ","


def usetype_globs(usetype: Optional[UsetypeFilter]) -> Optional[tuple[str, ...]]:
    """Normalise a positive usetype filter to a non-empty tuple of globs, or ``None``.

    ``None`` in, ``None`` out, and that is the ONLY way to get ``None`` out. It means "no
    positive filter", which is what lets the exclusion list apply instead; every other
    input either yields at least one glob or raises.

    That asymmetry is the point of the function. Before it, the filter was a single string
    tested with ``if usetype:``, so an empty string quietly took the same branch ``None``
    takes — a request that named a filter got an UNFILTERED page, silently widened rather
    than refused. An empty set is a mistake at the call site and is refused here, the same
    way :func:`validate_search_methods` refuses ``methods=[]`` rather than reading it as
    "all" or as "none".

    A glob is stripped of surrounding whitespace in both forms, so a usetype whose name
    begins or ends with a space is not nameable through this filter; nothing in the ingest
    surface can produce one. Duplicates are refused rather than collapsed, again matching
    :func:`validate_search_methods` — ``"chunk,chunk"`` is a typo, not a request.

    Wildcards inside a glob (``*``, ``?``) are not interpreted here. This function decides
    where one glob ends and the next begins; the server turns each glob into a SQL ``LIKE``
    pattern or an equality test.
    """
    if usetype is None:
        return None
    if isinstance(usetype, str):
        parts: Sequence[Any] = usetype.split(USETYPE_GLOB_SEPARATOR)
    elif isinstance(usetype, (list, tuple)):
        # NOT split on the separator: the list form is the escape hatch for a usetype that
        # contains one.
        parts = list(usetype)
    else:
        raise ValueError(
            f"usetype must be a glob string or a list of glob strings, got {type(usetype).__name__}"
        )

    globs: list[str] = []
    for part in parts:
        if not isinstance(part, str):
            raise ValueError(f"usetype globs must be strings; got {part!r}")
        glob = part.strip()
        if not glob:
            raise ValueError(
                f"usetype {usetype!r} contains an empty glob; a filter that names nothing "
                f"is refused rather than read as 'match everything'. Omit usetype entirely "
                f"to run without a positive filter."
            )
        globs.append(glob)

    if not globs:
        raise ValueError(
            "usetype names no glob; omit it entirely to run without a positive filter."
        )
    duplicates = sorted({g for g in globs if globs.count(g) > 1})
    if duplicates:
        raise ValueError(f"duplicate usetype glob(s) {duplicates}")
    return tuple(globs)


def validate_usetype_filter(value: Optional[UsetypeFilter]) -> Optional[UsetypeFilter]:
    """Field validator: reject a usetype filter the server could not act on, at the edge.

    Returns the caller's value UNCHANGED rather than the normalised tuple, so a response's
    ``applied.usetype`` echoes what was asked for. The server normalises again at the
    query, because a filter can also arrive from a stored ``search_contexts.config``, which
    passes through no contract at all.
    """
    usetype_globs(value)
    return value


class AppliedFilters(BaseModel):
    """What the server actually ran, as opposed to what the request named.

    Every field here is something a caller could previously only find out by reading the
    server's source. ``exclude_types`` is the reason the model exists: omitting it applies
    ``JMFTS_SEARCH_EXCLUDE_USETYPES`` (``entity``, ``entities``, ``summary`` by default),
    so a short result list had no explanation on the wire.

    ``truncated`` is here, beside those, and NOT as a new top-level ``SearchResponse``
    field, because a truncation is a fact about what the server ran — the same kind of fact
    as "these usetypes were held out" and "this limit was resolved". A caller reading a
    short page asks one question, "is that everything?", and every field that answers it
    should be in one block. ``docs/SPRINT_0_4_0.md`` Block A step 3.
    """

    methods: Optional[list[str]] = Field(
        default=None,
        description="Resolved retrieval methods. Only set for fusion (hybrid) searches.",
    )
    weights: Optional[dict[str, float]] = Field(
        default=None,
        description=(
            "Resolved per-method RRF weights. Only set for fusion searches. A method "
            "absent from this map is fused at 1.0."
        ),
    )
    exclude_types: list[str] = Field(
        default_factory=list,
        description=(
            "Usetypes held out of this result set. Empty when `usetype` is set, because a "
            "positive usetype filter overrides exclusion."
        ),
    )
    usetype: Optional[UsetypeFilter] = Field(
        default=None,
        description=(
            "Resolved positive usetype filter, echoed in the shape it was given: one glob, "
            "a comma-separated set of globs, or a list of them. A document matches when it "
            "matches ANY glob in the set."
        ),
    )
    parent_id: Optional[int] = Field(default=None, description="Resolved subtree filter.")
    limit: int = Field(description="Resolved result ceiling.")
    truncated: Optional[bool] = Field(
        default=None,
        description=(
            "Whether an approximate (ANN) index scan behind this response came back with "
            "fewer rows than it asked for. True means the page is SHORT — vector and "
            "MaxSim retrieval walk approximate indexes whose scan is bounded, so a short "
            "page from them cannot be read as an exhausted corpus; it may be short "
            "because the walk stopped, because a subtree access grant removed rows the "
            "walk had already spent its budget on, or because nothing else matched, and "
            "the server cannot tell those apart. False means every such scan filled its "
            "request. Null means none was reported: BM25 and full-text run no ANN scan at "
            "all, so a short page from them IS the whole matching set."
        ),
    )


#: The one description every request's ``usetype`` field carries. Written once because
#: four requests carry the field and a fifth (``GET /search/`` — ``quick_search``) takes it
#: as a query parameter, and a set-valued filter that four of them documented differently
#: would be a set-valued filter nobody could rely on.
USETYPE_FIELD_DESCRIPTION = (
    "Restrict results to documents whose usetype matches ANY of these globs (`*` and `?`; "
    "`transcript:*`). Accepts one glob, a comma-separated set (`transcript:*,obsidian:*`), "
    "or a JSON list of globs — the list form is the one that can name a usetype containing "
    "a comma. Setting it overrides `exclude_types` entirely. Omit it to run with no "
    "positive filter; an empty string or empty list is REFUSED rather than read as "
    "'match everything'."
)


class HybridSearchRequest(BaseModel):
    """Hybrid search request"""

    query: str
    limit: int = Field(default=10, ge=1, le=100)
    methods: Optional[list[str]] = Field(
        default=None,
        description=(
            "Retrieval methods to fuse, any of vector, fulltext, bm25, maxsim. Omit to run "
            "the server default (vector, bm25). An unrecognised name is rejected, not "
            "ignored. The resolved list is echoed in `applied.methods`."
        ),
    )
    weights: Optional[dict[str, float]] = None
    usetype: Optional[UsetypeFilter] = Field(default=None, description=USETYPE_FIELD_DESCRIPTION)
    exclude_types: Optional[list[str]] = Field(
        default=None,
        description="Exclude documents with these usetypes. When omitted, the server default applies (entity, summary). Pass [] to disable all exclusion.",
    )
    parent_id: Optional[int] = None
    index_name: str = "default"
    # Generative-Agents scoring terms, applied multiplicatively after RRF fusion.
    # Both default to 0.0 = off, leaving plain RRF untouched; callers opt in.
    recency_weight: float = Field(
        default=0.0,
        ge=0.0,
        description="Strength of the recency term (0.0 = off). Decays on COALESCE(event_time, created_at).",
    )
    importance_weight: float = Field(
        default=0.0,
        ge=0.0,
        description="Strength of the importance term (0.0 = off). Reads structured_content['importance'] on the 1-10 scale.",
    )
    recency_halflife_days: float = Field(
        default=7.0,
        gt=0.0,
        description="Age at which the recency factor halves. Only consulted when recency_weight is non-zero.",
    )
    now: Optional[datetime] = Field(
        default=None,
        description="Reference time for recency decay; defaults to the current UTC time. Pass it explicitly when a corpus has its own timeline, so results are reproducible.",
    )
    as_of: Optional[datetime] = Field(
        default=None,
        description="Point-in-time retrieval cutoff: only return documents whose domain clock COALESCE(event_time, created_at) <= as_of. Naive datetimes are read as UTC. Orthogonal to `now` (which reweights by recency); `as_of` filters visibility. Off when omitted.",
    )

    _check_methods = field_validator("methods")(validate_search_methods)
    _check_usetype = field_validator("usetype")(validate_usetype_filter)


class SearchResultItem(BaseModel):
    """Single search result"""

    document: DocumentResponse
    score: float
    method: str

    #: IC-4. Where in the source document this hit came from, verbatim from the node's
    #: ``source_anchor`` evidence row — a page and a rectangle for a PDF, a worksheet range
    #: for a spreadsheet. ``jmfts_client.contracts.anchor.parse_anchor`` reads it.
    #:
    #: **This is on the hit to remove a round trip from the beeline.** Without it a result
    #: list that offers "show me where" calls ``GET /documents/{id}/evidence`` once per hit
    #: before it can draw anything — ten hits, ten calls, to decide whether to render ten
    #: buttons. ``docs/SPRINT_0_6_0.md`` Block F step 31 is where the three pieces meet.
    #:
    #: ``None`` means this node has no anchor, which is the ordinary case for most of the
    #: corpus: ``citation`` runs only on PDFs today and is advisory
    #: (``models/task_queue.py:136``), so a document without one is not a document that
    #: failed.
    source_anchor: Optional[dict] = Field(
        default=None,
        description="The source_anchor evidence row for this node, if it has one.",
    )

    #: Why this hit has no anchor, when something tried and could not recover one. The
    #: ``source_anchor.unresolved`` row, "present exactly when `anchor` is not"
    #: (``jmfts_core/evidence.py:453``).
    #:
    #: Both fields are ``None`` for a node nothing ever attempted, and that is the third
    #: state: no attempt, a failed attempt with a reason, and a recovered anchor. A viewer
    #: that collapsed the first two would report every un-cited document as a recovery
    #: failure.
    source_anchor_unresolved: Optional[dict] = Field(
        default=None,
        description="The source_anchor.unresolved row: why no anchor was recovered.",
    )


class SearchResponse(BaseModel):
    """Search response.

    ``total`` is ``len(results)`` — the size of THIS page, not a corpus count. It has
    always been that and the name has always been misleading; ``applied.limit`` is now
    beside it, so a caller can see that a ``total`` equal to the limit is a ceiling rather
    than an exhaustive answer.
    """

    results: list[SearchResultItem]
    total: int
    latency_ms: float
    applied: Optional[AppliedFilters] = Field(
        default=None,
        description=(
            "What the server actually ran: resolved methods, weights and the usetype "
            "exclusions applied when the request named none."
        ),
    )


class SearchRequest(BaseModel):
    """Search request"""

    query: str
    limit: int = Field(default=10, ge=1, le=100)
    usetype: Optional[UsetypeFilter] = Field(default=None, description=USETYPE_FIELD_DESCRIPTION)
    exclude_types: Optional[list[str]] = Field(
        default=None,
        description="Exclude documents with these usetypes. When omitted, the server default applies (entity, summary). Pass [] to disable all exclusion.",
    )
    parent_id: Optional[int] = None
    threshold: float = Field(default=0.0, ge=0.0, le=1.0)
    as_of: Optional[datetime] = Field(
        default=None,
        description="Point-in-time retrieval cutoff: only return documents whose domain clock COALESCE(event_time, created_at) <= as_of. Naive datetimes are read as UTC. Off when omitted.",
    )

    _check_usetype = field_validator("usetype")(validate_usetype_filter)


class AutoSearchRequest(BaseModel):
    """Auto search request — heuristic router picks the best method"""

    query: str
    limit: int = Field(default=10, ge=1, le=100)
    usetype: Optional[UsetypeFilter] = Field(default=None, description=USETYPE_FIELD_DESCRIPTION)
    exclude_types: Optional[list[str]] = Field(
        default=None,
        description="Exclude documents with these usetypes. When omitted, the server default applies (entity, summary). Pass [] to disable all exclusion.",
    )
    parent_id: Optional[int] = None

    _check_usetype = field_validator("usetype")(validate_usetype_filter)


class RoutingMetadata(BaseModel):
    """Metadata about which search method was selected and why"""

    method: str
    reason: str
    signals: dict[str, Any]


class AutoSearchResponse(BaseModel):
    """Auto search response with routing metadata"""

    results: list[SearchResultItem]
    total: int
    latency_ms: float
    routing: RoutingMetadata
    applied: Optional[AppliedFilters] = Field(
        default=None,
        description="What the server actually ran. See `SearchResponse.applied`.",
    )


class SynthesizeRequest(BaseModel):
    """Request to synthesize an answer from search results"""

    query: str
    search_method: str = Field(
        default="auto",
        description="Search method: auto, hybrid, maxsim, bm25, vector, fulltext",
    )
    top_k: int = Field(default=5, ge=1, le=20)
    max_context_tokens: int = Field(default=4096, ge=256, le=32768)
    llm_model: Optional[str] = Field(default=None, description="Override the default LLM model")
    usetype: Optional[UsetypeFilter] = Field(default=None, description=USETYPE_FIELD_DESCRIPTION)
    parent_id: Optional[int] = None

    _check_usetype = field_validator("usetype")(validate_usetype_filter)


class SourceReference(BaseModel):
    """A source document referenced in the synthesis"""

    document_id: int
    title: Optional[str]
    score: float
    method: str


class SynthesizeResponse(BaseModel):
    """Synthesis response with generated text and source references"""

    synthesis: str
    sources: list[SourceReference]
    llm_model: str
    search_latency_ms: float
    total_latency_ms: float
    llm_available: bool = True
