"""SearchService — the search operations, transport-neutral.

Logic extracted verbatim from ``api/routers/search.py`` so the behaviour is identical;
the only intentional change is that document serialisation now goes through the single
``DocumentResponse.from_document`` converter, which fixes the position/event_time
field-drop for this endpoint.

Ports-and-adapters contract:
- The service takes a ``Session`` and returns typed contracts. No FastAPI here.
- Read operations do not commit; the caller's unit-of-work owns the transaction (the
  REST adapter's ``get_db`` teardown commits — a no-op for reads).
- Domain errors are raised as ``ValueError``; the ``@expose(errors={ValueError: 400})``
  metadata tells the REST adapter to map them to HTTP 400.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from sqlalchemy.orm import Session

from jmfts_core.config import get_settings
from jmfts_client.contracts.search import (
    DEFAULT_HYBRID_METHODS,
    SEARCH_METHODS,
    AppliedFilters,
    AutoSearchRequest,
    AutoSearchResponse,
    HybridSearchRequest,
    RoutingMetadata,
    SearchRequest,
    SearchResponse,
    SearchResultItem,
    SourceReference,
    SynthesizeRequest,
    SynthesizeResponse,
    validate_search_methods,
)
from jmfts_client.contracts.document import DocumentResponse
from jmfts_core.effective_content import project_effective_content
from jmfts_core.query_router import route_query
from jmfts_core.registry import expose, register_service
from jmfts_core.reranker import get_reranker_service
from jmfts_core.repositories.search import (
    SearchRepository,
    effective_exclude_types,
    effective_weights,
)
from jmfts_core.repositories.search_context import SearchContextRepository
from jmfts_core.synthesis import SynthesisResult, synthesize

logger = logging.getLogger(__name__)


@register_service
class SearchService:
    """Search operations over a single database session."""

    def __init__(self, session: Session):
        self.session = session

    # -- internal helpers (were module-level functions in the router) -------------

    def _resolve_context(self, context: Optional[str], overrides: dict) -> dict:
        """Resolve a named context preset into search params, applying overrides.

        A missing/unknown context is raised as ``LookupError`` (→ 404 at the REST
        edge), kept DISTINCT from the malformed-``importance`` ``ValueError`` the repo
        raises (→ 400). The old router disambiguated these two by *where* it caught
        them; the service disambiguates by *type* so ``@expose(errors=...)`` can map
        each to the same status the router used.
        """
        if not context:
            return {k: v for k, v in overrides.items() if v is not None}
        repo = SearchContextRepository(self.session)
        try:
            return repo.resolve_params(context, overrides)
        except ValueError:
            raise LookupError(f"Search context '{context}' not found")

    def _maybe_rerank(
        self, results, query: str, rerank: bool, limit: int, *, method: str = "cross_encoder"
    ):
        """Rerank an over-fetched candidate set, if requested.

        ``method`` selects the second stage:
        - ``"cross_encoder"`` (default) — a standard cross-encoder scores each
          (query, document) pair jointly (``JMFTS_RERANKER_MODEL``). It needs no stored
          token embeddings, so it works on any document the first stage can return.
        - ``"maxsim"`` — exact ColBERT MaxSim late-interaction rerank
          (``SearchRepository.maxsim_rerank``). Cheaper per query since it reuses vectors
          already in the table, but it only ranks documents that have been through
          ``/documents/{id}/tokens``; a candidate with no stored tokens scores 0 and sinks.
          It is the precise second stage behind the agent ``recall_exact_turn`` verb.

        Neither path is best-effort. A reranker that cannot load or cannot score is a
        fault, and returning the first-stage order under a ``+rerank`` label would report
        a second stage that never ran.
        """
        if not rerank or not results:
            return results
        if method == "maxsim":
            return SearchRepository(self.session).maxsim_rerank(results, query, limit=limit)
        if method == "cross_encoder":
            return get_reranker_service().rerank(query, results, limit=limit)
        raise ValueError(f"unknown rerank_method {method!r}; expected 'cross_encoder' or 'maxsim'")

    @staticmethod
    def _applied(
        *,
        limit: int,
        usetype: Optional[str] = None,
        exclude_types: Optional[list[str]] = None,
        parent_id: Optional[int] = None,
        methods: Optional[list[str]] = None,
        weights: Optional[dict[str, float]] = None,
        truncated: Optional[bool] = None,
    ) -> AppliedFilters:
        """What this search actually ran with, for the response's ``applied`` block.

        ``exclude_types`` here is the REQUEST's value — ``None`` meaning "not specified" —
        and :func:`~jmfts_core.repositories.search.effective_exclude_types` turns it into
        the list the query used. Resolving it a second time rather than having the repo
        report it is safe because that function is pure over ``(usetype, exclude_types)``
        and settings, and it is the same function the query calls.

        ``methods`` and ``weights`` are already resolved by the caller; a single-method
        endpoint passes neither, so they stay ``None`` and the field reads as
        not-applicable rather than as an empty fusion.

        ``truncated`` is the one field that CANNOT be recomputed here, and the callers pass
        ``repo.scan_truncated`` rather than deriving it from ``len(results)``. Three
        reasons, and each of them is a case where the two would disagree: an over-fetching
        ``rerank`` asks the repository for ``limit * 3`` and hands back ``limit``, so the
        response is full while the scan was short; ``hybrid_search`` fuses several legs and
        only the repository saw which of them came back short of ``candidate_limit``; and
        BM25 and full-text run no approximate scan at all, so their ``None`` is "nothing to
        report" and not "complete". ``docs/SPRINT_0_4_0.md`` Block A step 3.
        """
        return AppliedFilters(
            methods=methods,
            weights=weights,
            exclude_types=effective_exclude_types(usetype, exclude_types),
            usetype=usetype,
            parent_id=parent_id,
            limit=limit,
            truncated=truncated,
        )

    def _projected_text(self, results) -> dict:
        """``{document_id: text}`` for the results whose ``content`` is NULL and is not.

        A node whose ``content`` is NULL is not empty — it is a container whose text is
        computed from its subtree, and every search method except BM25 and full-text scores
        it on an embedding of exactly that text. Reading the column reports "no content" for
        the node the ranking just called the best answer.

        ONE query for the whole page, not one per result: see
        :mod:`jmfts_core.effective_content` for why the normative per-node walk is the wrong
        shape here. A node that genuinely stands for no text is ABSENT from the mapping
        rather than present with ``""``, because "computed, and empty" is a different fact
        from "not computed" and only the projection can tell them apart.

        Returns the texts rather than response models, because the two callers want
        different things from them and only one of them is going on the wire.
        """
        blank = [r.document.id for r in results if r.document.content is None]
        return project_effective_content(self.session, blank) if blank else {}

    def _project(self, results) -> list:
        """``DocumentResponse`` per result, with a container's text filled in."""
        projected = self._projected_text(results)
        documents = []
        for result in results:
            document = DocumentResponse.from_document(result.document)
            text = projected.get(document.id)
            if text is not None:
                document.content = text
                document.content_source = "effective"
            documents.append(document)
        return documents

    def _build_response(
        self, results, start: float, applied: Optional[AppliedFilters] = None
    ) -> SearchResponse:
        """Assemble a SearchResponse via the single shared document converter."""
        latency_ms = (time.time() - start) * 1000
        return SearchResponse(
            results=[
                SearchResultItem(document=document, score=r.score, method=r.method)
                for document, r in zip(self._project(results), results)
            ],
            total=len(results),
            latency_ms=latency_ms,
            applied=applied,
        )

    # -- exposed operations -------------------------------------------------------

    @expose(
        "POST",
        "/search/hybrid",
        response_model=SearchResponse,
        errors={LookupError: 404, ValueError: 400},
        tags=["search"],
        summary="Hybrid search with Reciprocal Rank Fusion",
    )
    def hybrid_search(
        self,
        request: HybridSearchRequest,
        *,
        context: Optional[str] = None,
        rerank: bool = False,
        rerank_method: str = "cross_encoder",
    ) -> SearchResponse:
        """Hybrid search with Reciprocal Rank Fusion.

        ``context`` selects a named parameter preset; ``rerank`` applies a second-stage
        rerank to an over-fetched candidate set, with ``rerank_method`` choosing
        ``"cross_encoder"`` (default) or ``"maxsim"`` (exact late-interaction). All are
        REST query parameters and plain keyword arguments in-process.

        ``request.methods`` of ``None`` means NOT SPECIFIED, which is what lets a named
        ``context`` supply the list. It used to be a list compared against the field's own
        default with ``!=``, and that made the request ORDER-DEPENDENT in a way nothing
        documented: the three declared names in the declared order resolved to ``None`` and
        ran ``vector``+``bm25``, while the same three names in any other order ran all
        three, with ``fulltext`` fused at 1.0 against vector's 0.86. Same meaning, different
        ranking.
        """
        start = time.time()

        params = self._resolve_context(
            context,
            {
                "usetype": request.usetype,
                "parent_id": request.parent_id,
                "methods": request.methods,
                "weights": request.weights,
                "index_name": request.index_name if request.index_name != "default" else None,
                "limit": request.limit,
            },
        )

        limit = params.get("limit", request.limit)
        # Resolved here as well as in the repo, so the response can report what ran. It is
        # also where a bad PRESET is caught: the contract validator only ever saw the
        # request, and a search context is free to store any list it likes.
        methods = list(params.get("methods") or DEFAULT_HYBRID_METHODS)
        validate_search_methods(methods)
        weights = effective_weights(params.get("weights"))
        repo = SearchRepository(self.session)
        # A candidate carrying a malformed structured_content['importance'] raises
        # ValueError from the repo — a broken writer, not a server fault. We let it
        # propagate; @expose maps it to HTTP 400.
        results = repo.hybrid_search(
            query_text=request.query,
            limit=limit * 3 if rerank else limit,
            methods=methods,
            weights=weights,
            usetype=params.get("usetype"),
            exclude_types=request.exclude_types,
            parent_id=params.get("parent_id"),
            index_name=params.get("index_name") or "default",
            recency_weight=request.recency_weight,
            importance_weight=request.importance_weight,
            recency_halflife_days=request.recency_halflife_days,
            now=request.now,
            as_of=request.as_of,
        )

        results = self._maybe_rerank(results, request.query, rerank, limit, method=rerank_method)
        return self._build_response(
            results,
            start,
            self._applied(
                limit=limit,
                usetype=params.get("usetype"),
                exclude_types=request.exclude_types,
                parent_id=params.get("parent_id"),
                methods=methods,
                weights=weights,
                truncated=repo.scan_truncated,
            ),
        )

    @expose(
        "POST",
        "/search/vector",
        response_model=SearchResponse,
        errors={LookupError: 404, ValueError: 400},
        tags=["search"],
        summary="Semantic vector search",
    )
    def vector_search(
        self,
        request: SearchRequest,
        *,
        context: Optional[str] = None,
        rerank: bool = False,
        rerank_method: str = "cross_encoder",
    ) -> SearchResponse:
        """Semantic vector search"""
        start = time.time()

        params = self._resolve_context(
            context,
            {
                "usetype": request.usetype,
                "parent_id": request.parent_id,
                "threshold": request.threshold if request.threshold > 0 else None,
                "limit": request.limit,
            },
        )

        limit = params.get("limit", request.limit)
        repo = SearchRepository(self.session)
        results = repo.vector_search_text(
            query_text=request.query,
            limit=limit * 3 if rerank else limit,
            usetype=params.get("usetype"),
            exclude_types=request.exclude_types,
            parent_id=params.get("parent_id"),
            threshold=params.get("threshold", 0.0) or 0.0,
            as_of=request.as_of,
        )

        results = self._maybe_rerank(results, request.query, rerank, limit, method=rerank_method)
        return self._build_response(
            results,
            start,
            self._applied(
                limit=limit,
                usetype=params.get("usetype"),
                exclude_types=request.exclude_types,
                parent_id=params.get("parent_id"),
                truncated=repo.scan_truncated,
            ),
        )

    @expose(
        "POST",
        "/search/fulltext",
        response_model=SearchResponse,
        errors={LookupError: 404, ValueError: 400},
        tags=["search"],
        summary="PostgreSQL full-text search",
    )
    def fulltext_search(
        self,
        request: SearchRequest,
        *,
        context: Optional[str] = None,
        rerank: bool = False,
        rerank_method: str = "cross_encoder",
    ) -> SearchResponse:
        """PostgreSQL full-text search"""
        start = time.time()

        params = self._resolve_context(
            context,
            {
                "usetype": request.usetype,
                "parent_id": request.parent_id,
                "limit": request.limit,
            },
        )

        limit = params.get("limit", request.limit)
        repo = SearchRepository(self.session)
        results = repo.fulltext_search(
            query_text=request.query,
            limit=limit * 3 if rerank else limit,
            usetype=params.get("usetype"),
            exclude_types=request.exclude_types,
            parent_id=params.get("parent_id"),
            as_of=request.as_of,
        )

        results = self._maybe_rerank(results, request.query, rerank, limit, method=rerank_method)
        return self._build_response(
            results,
            start,
            self._applied(
                limit=limit,
                usetype=params.get("usetype"),
                exclude_types=request.exclude_types,
                parent_id=params.get("parent_id"),
                truncated=repo.scan_truncated,
            ),
        )

    @expose(
        "POST",
        "/search/bm25",
        response_model=SearchResponse,
        errors={LookupError: 404, ValueError: 400},
        tags=["search"],
        summary="BM25 lexical search",
    )
    def bm25_search(
        self,
        request: SearchRequest,
        *,
        index_name: str = "default",
        context: Optional[str] = None,
        rerank: bool = False,
        rerank_method: str = "cross_encoder",
    ) -> SearchResponse:
        """BM25 lexical search"""
        start = time.time()

        params = self._resolve_context(
            context,
            {
                "usetype": request.usetype,
                "parent_id": request.parent_id,
                "index_name": index_name if index_name != "default" else None,
                "limit": request.limit,
            },
        )

        limit = params.get("limit", request.limit)
        repo = SearchRepository(self.session)
        results = repo.bm25_search(
            query_text=request.query,
            index_name=params.get("index_name") or "default",
            limit=limit * 3 if rerank else limit,
            usetype=params.get("usetype"),
            exclude_types=request.exclude_types,
            parent_id=params.get("parent_id"),
            as_of=request.as_of,
        )

        results = self._maybe_rerank(results, request.query, rerank, limit, method=rerank_method)
        return self._build_response(
            results,
            start,
            self._applied(
                limit=limit,
                usetype=params.get("usetype"),
                exclude_types=request.exclude_types,
                parent_id=params.get("parent_id"),
                truncated=repo.scan_truncated,
            ),
        )

    @expose(
        "POST",
        "/search/maxsim",
        response_model=SearchResponse,
        errors={LookupError: 404, ValueError: 400},
        tags=["search"],
        summary="ColBERT-style MaxSim late interaction search",
    )
    def maxsim_search(
        self,
        request: SearchRequest,
        *,
        embed_dim: int = 256,
        context: Optional[str] = None,
        rerank: bool = False,
        rerank_method: str = "cross_encoder",
    ) -> SearchResponse:
        """ColBERT-style MaxSim late interaction search"""
        start = time.time()

        params = self._resolve_context(
            context,
            {
                "usetype": request.usetype,
                "parent_id": request.parent_id,
                "limit": request.limit,
            },
        )

        limit = params.get("limit", request.limit)
        repo = SearchRepository(self.session)
        results = repo.maxsim_search(
            query_text=request.query,
            limit=limit * 3 if rerank else limit,
            embed_dim=embed_dim,
            usetype=params.get("usetype"),
            exclude_types=request.exclude_types,
            parent_id=params.get("parent_id"),
            as_of=request.as_of,
        )

        results = self._maybe_rerank(results, request.query, rerank, limit, method=rerank_method)
        return self._build_response(
            results,
            start,
            self._applied(
                limit=limit,
                usetype=params.get("usetype"),
                exclude_types=request.exclude_types,
                parent_id=params.get("parent_id"),
                truncated=repo.scan_truncated,
            ),
        )

    @expose(
        "POST",
        "/search/synthesize",
        response_model=SynthesizeResponse,
        errors={LookupError: 404},
        tags=["search"],
        summary="Search then synthesize an answer via LLM",
    )
    async def synthesize_search(
        self,
        request: SynthesizeRequest,
        *,
        context: Optional[str] = None,
    ) -> SynthesizeResponse:
        """Search then synthesize an answer via LLM.

        Performs the chosen search method, collects top-k results, formats them as
        context, and calls an LLM to produce a synthesized answer. If the LLM is
        unavailable, returns the search results without synthesis.
        """
        start = time.time()

        params = self._resolve_context(
            context,
            {
                "usetype": request.usetype,
                "parent_id": request.parent_id,
                "limit": request.top_k,
            },
        )

        resolved_usetype = params.get("usetype")
        resolved_parent_id = params.get("parent_id")
        resolved_limit = params.get("limit", request.top_k)

        repo = SearchRepository(self.session)
        method = request.search_method

        # Route "auto" through the query router
        if method == "auto":
            decision = route_query(request.query)
            method = decision.method

        if method == "vector":
            results = repo.vector_search_text(
                query_text=request.query,
                limit=resolved_limit,
                usetype=resolved_usetype,
                parent_id=resolved_parent_id,
            )
        elif method == "fulltext":
            results = repo.fulltext_search(
                query_text=request.query,
                limit=resolved_limit,
                usetype=resolved_usetype,
                parent_id=resolved_parent_id,
            )
        elif method == "bm25":
            results = repo.bm25_search(
                query_text=request.query,
                limit=resolved_limit,
                usetype=resolved_usetype,
                parent_id=resolved_parent_id,
            )
        elif method == "maxsim":
            results = repo.maxsim_search(
                query_text=request.query,
                limit=resolved_limit,
                usetype=resolved_usetype,
                parent_id=resolved_parent_id,
            )
        else:  # hybrid
            results = repo.hybrid_search(
                query_text=request.query,
                limit=resolved_limit,
                usetype=resolved_usetype,
                parent_id=resolved_parent_id,
            )

        search_latency_ms = (time.time() - start) * 1000

        sources = [
            SourceReference(
                document_id=r.document.id,
                title=r.document.title,
                score=r.score,
                method=r.method,
            )
            for r in results
        ]

        # Prepare docs for the LLM context, through `_project` and not from the ORM row.
        #
        # `r.document.content` is NULL for a container, so reading it directly hands the
        # model an empty string for the node the ranking just called the best answer. The
        # model then answers from the leaves that happened to rank below it, or says it
        # cannot find anything — either way the container occupies a slot in a bounded
        # context window and contributes nothing to it. This is the same defect as a blank
        # search result, and it is worse here because there is no reader to notice.
        projected = self._projected_text(results)
        doc_dicts = [
            {
                "id": r.document.id,
                "title": r.document.title,
                "content": projected.get(r.document.id) or r.document.content or "",
                "score": r.score,
            }
            for r in results
        ]

        # Attempt LLM synthesis; degrade gracefully if unavailable
        settings = get_settings()
        llm_model = request.llm_model or settings.effective_llm_model

        try:
            result: SynthesisResult = await synthesize(
                query=request.query,
                documents=doc_dicts,
                max_context_tokens=request.max_context_tokens,
                llm_model=llm_model,
            )
            synthesis_text = result.text
            llm_available = True
        except Exception as exc:
            logger.warning("LLM synthesis failed, returning results without synthesis: %s", exc)
            synthesis_text = (
                "LLM synthesis unavailable. Search results are provided below as source references."
            )
            llm_available = False

        total_latency_ms = (time.time() - start) * 1000

        return SynthesizeResponse(
            synthesis=synthesis_text,
            sources=sources,
            llm_model=llm_model,
            search_latency_ms=round(search_latency_ms, 1),
            total_latency_ms=round(total_latency_ms, 1),
            llm_available=llm_available,
        )

    @expose(
        "POST",
        "/search/auto",
        response_model=AutoSearchResponse,
        errors={LookupError: 404, ValueError: 400},
        tags=["search"],
        summary="Automatic search — analyzes the query and picks the best method",
    )
    def auto_search(
        self,
        request: AutoSearchRequest,
        *,
        context: Optional[str] = None,
        rerank: bool = False,
        rerank_method: str = "cross_encoder",
    ) -> AutoSearchResponse:
        """Automatic search — analyzes the query and picks the best method."""
        start = time.time()

        params = self._resolve_context(
            context,
            {
                "usetype": request.usetype,
                "parent_id": request.parent_id,
                "limit": request.limit,
            },
        )

        resolved_usetype = params.get("usetype")
        resolved_parent_id = params.get("parent_id")
        resolved_limit = params.get("limit", request.limit)
        fetch_limit = resolved_limit * 3 if rerank else resolved_limit

        repo = SearchRepository(self.session)
        decision = route_query(request.query)

        if decision.method == "vector":
            results = repo.vector_search_text(
                query_text=request.query,
                limit=fetch_limit,
                usetype=resolved_usetype,
                exclude_types=request.exclude_types,
                parent_id=resolved_parent_id,
            )
        elif decision.method == "fulltext":
            results = repo.fulltext_search(
                query_text=request.query,
                limit=fetch_limit,
                usetype=resolved_usetype,
                exclude_types=request.exclude_types,
                parent_id=resolved_parent_id,
            )
        elif decision.method == "bm25":
            results = repo.bm25_search(
                query_text=request.query,
                limit=fetch_limit,
                usetype=resolved_usetype,
                exclude_types=request.exclude_types,
                parent_id=resolved_parent_id,
            )
        elif decision.method == "maxsim":
            results = repo.maxsim_search(
                query_text=request.query,
                limit=fetch_limit,
                usetype=resolved_usetype,
                exclude_types=request.exclude_types,
            )
        else:  # hybrid
            results = repo.hybrid_search(
                query_text=request.query,
                limit=fetch_limit,
                usetype=resolved_usetype,
                exclude_types=request.exclude_types,
                parent_id=resolved_parent_id,
            )

        results = self._maybe_rerank(
            results, request.query, rerank, resolved_limit, method=rerank_method
        )
        latency_ms = (time.time() - start) * 1000

        return AutoSearchResponse(
            results=[
                SearchResultItem(document=document, score=r.score, method=r.method)
                for document, r in zip(self._project(results), results)
            ],
            total=len(results),
            latency_ms=latency_ms,
            routing=RoutingMetadata(
                method=decision.method,
                reason=decision.reason,
                signals=decision.signals,
            ),
            # The router picks one method and `routing.method` already names it, so
            # `applied.methods` is filled only on the hybrid branch, where it says which
            # methods were fused rather than which one was chosen.
            applied=self._applied(
                limit=resolved_limit,
                usetype=resolved_usetype,
                exclude_types=request.exclude_types,
                parent_id=resolved_parent_id,
                methods=(list(DEFAULT_HYBRID_METHODS) if decision.method == "hybrid" else None),
                weights=(effective_weights(None) if decision.method == "hybrid" else None),
                truncated=repo.scan_truncated,
            ),
        )

    @expose(
        "GET",
        "/search/",
        response_model=SearchResponse,
        errors={LookupError: 404, ValueError: 400},
        tags=["search"],
        summary="Quick search endpoint (GET)",
    )
    def quick_search(
        self,
        *,
        q: str,
        limit: int = 10,
        method: str = "hybrid",
        usetype: Optional[str] = None,
        parent_id: Optional[int] = None,
        context: Optional[str] = None,
        rerank: bool = False,
        rerank_method: str = "cross_encoder",
    ) -> SearchResponse:
        """Quick search endpoint (GET).

        ``method`` is one of the four retrieval methods or ``hybrid``. An unrecognised
        name RAISES; it used to fall through the ``elif`` ladder into the ``else`` branch
        and run a hybrid search, so ``?method=vetcor`` answered 200 with a fusion the
        caller had not asked for and no way to tell.
        """
        start = time.time()

        params = self._resolve_context(
            context,
            {
                "usetype": usetype,
                "parent_id": parent_id,
                "method": method if method != "hybrid" else None,
                "limit": limit if limit != 10 else None,
            },
        )

        resolved_method = params.get("method", "hybrid")
        # Checked AFTER resolution, which covers both sources in one place: a bad `method`
        # argument arrives here as an override, and a named context is free to store one
        # too.
        if resolved_method not in SEARCH_METHODS and resolved_method != "hybrid":
            raise ValueError(
                f"unknown method {resolved_method!r}; expected 'hybrid' or one of "
                f"{list(SEARCH_METHODS)}"
            )
        resolved_limit = params.get("limit", 10)
        resolved_usetype = params.get("usetype")
        resolved_parent_id = params.get("parent_id")
        fetch_limit = resolved_limit * 3 if rerank else resolved_limit

        repo = SearchRepository(self.session)
        #: The fused method list, set only on the hybrid branch — `None` elsewhere is what
        #: `applied.methods` reports as not-applicable for a single-method search.
        fused: Optional[list[str]] = None

        if resolved_method == "vector":
            results = repo.vector_search_text(
                q, limit=fetch_limit, usetype=resolved_usetype, parent_id=resolved_parent_id
            )
        elif resolved_method == "fulltext":
            results = repo.fulltext_search(
                q, limit=fetch_limit, usetype=resolved_usetype, parent_id=resolved_parent_id
            )
        elif resolved_method == "bm25":
            results = repo.bm25_search(
                q,
                limit=fetch_limit,
                usetype=resolved_usetype,
                parent_id=resolved_parent_id,
                index_name=params.get("index_name") or "default",
            )
        elif resolved_method == "maxsim":
            results = repo.maxsim_search(
                q, limit=fetch_limit, usetype=resolved_usetype, parent_id=resolved_parent_id
            )
        else:  # hybrid
            fused = list(params.get("methods") or DEFAULT_HYBRID_METHODS)
            validate_search_methods(fused)
            results = repo.hybrid_search(
                q,
                limit=fetch_limit,
                usetype=resolved_usetype,
                parent_id=resolved_parent_id,
                methods=fused,
                weights=params.get("weights"),
                index_name=params.get("index_name") or "default",
            )

        results = self._maybe_rerank(results, q, rerank, resolved_limit, method=rerank_method)
        return self._build_response(
            results,
            start,
            self._applied(
                limit=resolved_limit,
                usetype=resolved_usetype,
                parent_id=resolved_parent_id,
                methods=fused if resolved_method == "hybrid" else None,
                weights=(
                    effective_weights(params.get("weights"))
                    if resolved_method == "hybrid"
                    else None
                ),
                truncated=repo.scan_truncated,
            ),
        )
