"""Search Repository - Vector, BM25, and Hybrid Search"""

import fnmatch
import math
import re
from datetime import datetime, timezone
from typing import Optional
from dataclasses import dataclass
from sqlalchemy import select, func, text, and_, or_
from sqlalchemy.orm import Session

from jmfts_core.models.document import Document, SETTLED_SETTLED
from jmfts_core.models.search_index import (
    SearchIndex,
    SearchIndexEntry,
    SearchTermPosting,
)
from jmfts_core.embedding import get_embedding_service
from jmfts_core.config import get_settings
from jmfts_core.access import readable_filter, readable_sql


@dataclass
class SearchResult:
    """A single search result"""

    document: Document
    score: float
    method: str  # "vector", "bm25", "fulltext", "maxsim", "hybrid"


def _usetype_has_wildcard(usetype: str) -> bool:
    """Check if a usetype filter contains wildcard characters."""
    return "*" in usetype or "?" in usetype


def _usetype_to_like(usetype: str) -> str:
    """Convert a glob-style usetype pattern to SQL LIKE pattern.

    Supports * (any chars) and ? (single char). Escapes SQL LIKE specials.
    """
    # Escape SQL LIKE special chars first
    pattern = usetype.replace("%", r"\%").replace("_", r"\_")
    # Then convert glob wildcards
    pattern = pattern.replace("*", "%").replace("?", "_")
    return pattern


def _as_of_utc(as_of: Optional[datetime]) -> Optional[datetime]:
    """Normalise an ``as_of`` cutoff to a tz-aware UTC datetime, or pass None through.

    Mirrors ``_recency_factor``'s naive-means-UTC invariant: the ORM writes naive
    ``datetime.utcnow()`` into TIMESTAMPTZ columns, so a naive cutoff is read as UTC
    too rather than as the Postgres session's local zone.
    """
    if as_of is not None and as_of.tzinfo is None:
        return as_of.replace(tzinfo=timezone.utc)
    return as_of


def _usetype_matches(usetype_filter: str, usetype_value: Optional[str]) -> bool:
    """In-memory wildcard match for usetype filtering (used by BM25 post-filter)."""
    if usetype_value is None:
        return False
    if _usetype_has_wildcard(usetype_filter):
        return fnmatch.fnmatch(usetype_value, usetype_filter)
    return usetype_value == usetype_filter


def _apply_usetype_filter(query, usetype: str):
    """Apply usetype filter to a SQLAlchemy query, supporting wildcards."""
    if _usetype_has_wildcard(usetype):
        query = query.where(Document.usetype.like(_usetype_to_like(usetype)))
    else:
        query = query.where(Document.usetype == usetype)
    return query


def _apply_usetype_exclusion(query, exclude_usetypes: list[str]):
    """Exclude documents with specific usetypes from results. NULL usetype is never excluded."""
    if not exclude_usetypes:
        return query
    query = query.where(or_(Document.usetype.is_(None), Document.usetype.notin_(exclude_usetypes)))
    return query


class SearchRepository:
    """Repository for search operations"""

    def __init__(self, session: Session):
        self.session = session

    # =========================================================================
    # Vector Search
    # =========================================================================

    def vector_search(
        self,
        query_embedding: list[float],
        limit: int = 10,
        usetype: Optional[str] = None,
        exclude_types: Optional[list[str]] = None,
        parent_id: Optional[int] = None,
        threshold: float = 0.0,
        as_of: Optional[datetime] = None,
    ) -> list[SearchResult]:
        """
        Semantic vector search using cosine similarity.

        Args:
            query_embedding: Query embedding (1024-dim)
            limit: Max results
            usetype: Filter to only this document type (overrides exclude_types)
            exclude_types: Exclude these document types; None uses config default
            parent_id: Filter to subtree
            threshold: Minimum similarity score
            as_of: Point-in-time cutoff — only documents whose domain clock
                COALESCE(event_time, created_at) <= as_of. Off when None.

        Returns:
            List of SearchResults sorted by score descending
        """
        # Build query with cosine distance
        # pgvector: <=> is cosine distance, lower is better
        # Convert to similarity: 1 - distance
        # `settled = 'settled'` is BOTH the retrieval rule and the predicate that makes
        # idx_documents_embed reachable: that index is partial on exactly this clause,
        # and a partial index the planner cannot prove is a silent sequential scan.
        query = (
            select(Document, (1 - Document.embed.cosine_distance(query_embedding)).label("score"))
            .where(Document.embed.isnot(None))
            .where(Document.settled == SETTLED_SETTLED)
        )

        if usetype:
            query = _apply_usetype_filter(query, usetype)
        else:
            effective = exclude_types if exclude_types is not None else get_settings().search_exclude_usetypes
            query = _apply_usetype_exclusion(query, effective)

        if parent_id:
            # Use path GIN index for subtree filtering
            query = query.where(Document.path.op("@>")(func.jsonb_build_array(parent_id)))

        if threshold > 0:
            query = query.where((1 - Document.embed.cosine_distance(query_embedding)) >= threshold)

        as_of = _as_of_utc(as_of)
        if as_of is not None:
            query = query.where(func.coalesce(Document.event_time, Document.created_at) <= as_of)

        # Subtree RBAC: scope to documents the current principal may read (no-op for
        # owner/unbound callers or when no access-control roots exist).
        acl = readable_filter(self.session)
        if acl is not None:
            query = query.where(acl)

        query = query.order_by(text("score DESC")).limit(limit)

        results = []
        for doc, score in self.session.execute(query).all():
            results.append(SearchResult(document=doc, score=float(score), method="vector"))

        return results

    def vector_search_text(self, query_text: str, limit: int = 10, **kwargs) -> list[SearchResult]:
        """Vector search with automatic query embedding"""
        service = get_embedding_service()
        query_embedding = service.embed_text(query_text, prefix="search_query: ")
        return self.vector_search(query_embedding.tolist(), limit=limit, **kwargs)

    # =========================================================================
    # Full-Text Search (PostgreSQL GIN)
    # =========================================================================

    def fulltext_search(
        self,
        query_text: str,
        limit: int = 10,
        usetype: Optional[str] = None,
        exclude_types: Optional[list[str]] = None,
        parent_id: Optional[int] = None,
        as_of: Optional[datetime] = None,
    ) -> list[SearchResult]:
        """
        PostgreSQL full-text search using GIN index.

        Uses websearch_to_tsquery for natural query parsing.

        ``as_of`` applies the same domain-clock cutoff as vector_search:
        only documents with COALESCE(event_time, created_at) <= as_of.
        """
        # Build tsvector query
        tsquery = func.websearch_to_tsquery("english", query_text)

        query = select(
            Document,
            func.ts_rank(
                func.to_tsvector(
                    "english",
                    func.coalesce(Document.title, "") + " " + func.coalesce(Document.content, ""),
                ),
                tsquery,
            ).label("score"),
        ).where(
            func.to_tsvector(
                "english",
                func.coalesce(Document.title, "") + " " + func.coalesce(Document.content, ""),
            ).op("@@")(tsquery)
            # Same clause idx_documents_content_fts is partial on. It must be present
            # for the GIN index to be usable at all, and it is the retrieval rule
            # anyway: a tree that is still being built is not an answer.
        ).where(Document.settled == SETTLED_SETTLED)

        if usetype:
            query = _apply_usetype_filter(query, usetype)
        else:
            effective = exclude_types if exclude_types is not None else get_settings().search_exclude_usetypes
            query = _apply_usetype_exclusion(query, effective)

        if parent_id:
            query = query.where(Document.path.op("@>")(func.jsonb_build_array(parent_id)))

        as_of = _as_of_utc(as_of)
        if as_of is not None:
            query = query.where(func.coalesce(Document.event_time, Document.created_at) <= as_of)

        # Subtree RBAC: scope to documents the current principal may read.
        acl = readable_filter(self.session)
        if acl is not None:
            query = query.where(acl)

        query = query.order_by(text("score DESC")).limit(limit)

        results = []
        for doc, score in self.session.execute(query).all():
            results.append(SearchResult(document=doc, score=float(score), method="fulltext"))

        return results

    # =========================================================================
    # BM25 Search
    # =========================================================================

    def _find_covering_index(self, parent_id: int, default_index: str) -> str:
        """Return the best named index whose registered root covers parent_id's tree.

        When a caller uses the 'default' index but the target documents live in a
        named index (e.g. 'adjutant'), the BM25 posting-list lookup finds nothing.
        This method queries search_index_members to find an index whose root is an
        ancestor of (or equal to) parent_id, and returns its name.

        Args:
            parent_id: The scope filter document ID.
            default_index: The caller-specified index name (returned as-is when
                it is not 'default', i.e. the caller made an explicit choice).

        Returns:
            A named index that covers parent_id's tree, or default_index if none found.
        """
        if default_index != "default":
            return default_index  # caller made an explicit choice, respect it

        doc = self.session.get(Document, parent_id)
        if doc is None:
            return default_index

        # candidate roots: parent_id itself plus all its ancestors (closest first)
        candidate_ids = [parent_id] + list(doc.path or [])


        # Find a named index whose root is one of the candidate ancestors.
        # Use array_position to prefer the most specific (deepest) match.
        result = self.session.execute(
            text("""
                SELECT si.name
                FROM search_indexes si
                JOIN search_index_members sim ON sim.index_id = si.id
                WHERE sim.root_document_id = ANY(CAST(:candidates AS integer[]))
                  AND si.name != 'default'
                ORDER BY array_position(CAST(:candidates AS integer[]), sim.root_document_id)
                LIMIT 1
            """),
            {"candidates": candidate_ids},
        ).scalar_one_or_none()

        return result or default_index

    def bm25_search(
        self,
        query_text: str,
        index_name: str = "default",
        limit: int = 10,
        usetype: Optional[str] = None,
        exclude_types: Optional[list[str]] = None,
        parent_id: Optional[int] = None,
        as_of: Optional[datetime] = None,
    ) -> list[SearchResult]:
        """
        BM25 lexical search using inverted index tables.

        Args:
            query_text: Search query
            index_name: Name of the search index
            limit: Max results
            usetype: Filter to only this document type (overrides exclude_types)
            exclude_types: Exclude these document types; None uses config default
            parent_id: Filter to subtree
            as_of: Point-in-time cutoff on COALESCE(event_time, created_at). Off when None.

        Returns:
            List of SearchResults sorted by BM25 score
        """
        # When parent_id scopes the search and the caller used the default index,
        # auto-select the named index whose root covers parent_id's document tree.
        # This prevents zero results when documents are indexed in a named index
        # (e.g. "adjutant") but the caller didn't specify index_name explicitly.
        if parent_id is not None:
            index_name = self._find_covering_index(parent_id, index_name)

        # Get index
        index = self.session.execute(
            select(SearchIndex).where(SearchIndex.name == index_name)
        ).scalar_one_or_none()

        if not index:
            return []

        # Tokenize query (simple whitespace + lowercase)
        terms = self._tokenize(query_text)
        if not terms:
            return []

        settings = get_settings()
        k1 = index.config.get("k1", settings.bm25_k1)
        b = index.config.get("b", settings.bm25_b)

        # BM25 scoring query using inverted index
        # Note: Using CAST instead of :: to avoid SQLAlchemy parameter confusion
        # Build optional document-scoped filter clauses (parent subtree and/or as_of
        # cutoff). Either one requires joining `documents`, so the join is shared.
        as_of = _as_of_utc(as_of)
        # Subtree RBAC fragment (inlined int PKs; None for owner/unbound or no ACRs).
        # Forcing the documents join keeps ACL in the scored CTE, so top-k stays
        # correct rather than being trimmed by a post-filter.
        acl_sql = readable_sql(self.session, alias="d")
        parent_join = ""
        parent_where = ""
        if parent_id is not None or as_of is not None or acl_sql is not None:
            parent_join = "JOIN documents d ON d.id = tp.document_id"
        if parent_id is not None:
            parent_where += " AND d.path @> jsonb_build_array(:parent_id)"
        if as_of is not None:
            parent_where += " AND COALESCE(d.event_time, d.created_at) <= :as_of"
        if acl_sql is not None:
            parent_where += f" AND {acl_sql}"

        bm25_query = text(f"""
            WITH query_terms AS (
                SELECT unnest(CAST(:terms AS text[])) as term
            ),
            term_idf AS (
                SELECT
                    qt.term,
                    LN((:total_docs - COALESCE(ts.doc_freq, 0) + 0.5) /
                       (COALESCE(ts.doc_freq, 0) + 0.5) + 1) as idf
                FROM query_terms qt
                LEFT JOIN search_term_stats ts
                    ON ts.index_id = :index_id AND ts.term = qt.term
            ),
            doc_scores AS (
                SELECT
                    tp.document_id,
                    SUM(
                        ti.idf *
                        (tp.term_freq * (:k1 + 1)) /
                        (tp.term_freq + :k1 * (1 - :b + :b * e.doc_length / NULLIF(:avg_doc_length, 1)))
                    ) as bm25_score
                FROM search_term_postings tp
                JOIN term_idf ti ON ti.term = tp.term
                JOIN search_index_entries e
                    ON e.index_id = tp.index_id AND e.document_id = tp.document_id
                {parent_join}
                WHERE tp.index_id = :index_id
                    AND tp.term = ANY(CAST(:terms AS text[]))
                    {parent_where}
                GROUP BY tp.document_id
            )
            SELECT document_id, bm25_score
            FROM doc_scores
            ORDER BY bm25_score DESC
            LIMIT :limit
        """)

        bind_params = {
            "terms": terms,
            "index_id": index.id,
            "total_docs": index.total_docs or 1,
            "avg_doc_length": index.avg_doc_length or 1,
            "k1": k1,
            "b": b,
            "limit": limit * 2,  # Get extra to filter by usetype
        }
        if parent_id is not None:
            bind_params["parent_id"] = parent_id
        if as_of is not None:
            bind_params["as_of"] = as_of

        result = self.session.execute(bm25_query, bind_params)

        # Fetch documents and apply usetype/exclusion filters
        # BM25 entities/summaries are excluded at index time; exclude_types adds runtime post-filter
        effective_exclusions = exclude_types if (usetype is None and exclude_types is not None) else []
        results = []
        for doc_id, score in result:
            doc = self.session.get(Document, doc_id)
            if not doc:
                continue
            if usetype and not _usetype_matches(usetype, doc.usetype):
                continue
            if effective_exclusions and doc.usetype in effective_exclusions:
                continue
            results.append(SearchResult(document=doc, score=float(score), method="bm25"))
            if len(results) >= limit:
                break

        return results

    def _tokenize(self, text: str) -> list[str]:
        """Simple tokenization for BM25"""
        # Lowercase, split on non-alphanumeric, filter short tokens
        text = text.lower()
        tokens = re.split(r"[^a-z0-9]+", text)
        return [t for t in tokens if len(t) >= 2]

    # =========================================================================
    # MaxSim (Late Interaction) Search
    # =========================================================================

    def maxsim_search(
        self,
        query_text: str,
        limit: int = 10,
        embed_dim: int = 256,
        usetype: Optional[str] = None,
        exclude_types: Optional[list[str]] = None,
        parent_id: Optional[int] = None,
        max_tier: Optional[int] = None,
        as_of: Optional[datetime] = None,
    ) -> list[SearchResult]:
        """
        ColBERT-style MaxSim search using token-level embeddings.

        For each query token, finds max similarity with any document token,
        then sums across query tokens.

        Args:
            query_text: Search query
            limit: Max results
            embed_dim: Which embedding dimension to use (256, 384)
            usetype: Filter to only this document type (overrides exclude_types)
            exclude_types: Exclude these document types; None uses config default
            parent_id: Filter to subtree under this document
            max_tier: Filter tokens by tier (5=top 5%, 10=top 10%, etc.)
            as_of: Point-in-time cutoff on COALESCE(event_time, created_at). Off when None.
        """
        service = get_embedding_service()

        # Get query token embeddings
        result = service.embed_with_tokens(query_text, top_percent=1.0, prefix="search_query: ")
        if not result.token_embeddings:
            return []

        # Filter to content tokens only (positive importance scores)
        # Stopwords have -99 importance and would dilute MaxSim signal
        content_tokens = [tok for tok in result.token_embeddings if tok.importance_score > 0]

        # Fall back to all tokens if no content tokens (edge case)
        tokens_to_use = content_tokens if content_tokens else result.token_embeddings

        # Truncate query embeddings to target dimension
        query_embeddings = [
            service.truncate_embedding(tok.embedding, embed_dim).tolist() for tok in tokens_to_use
        ]

        # Only 256-dim supported now
        if embed_dim != 256:
            raise ValueError(f"Invalid embed_dim: {embed_dim}. Only 256 is supported.")

        # Use pgvector IVF-Flat index for efficient ANN search per query token
        # For each query token, find top-K nearest document tokens, then aggregate by document
        from collections import defaultdict
        from sqlalchemy import text

        embed_col_name = "embed_256"

        # How many nearest neighbors per query token (higher = more accurate but slower)
        k_per_token = 100

        # Build filter conditions for the query.
        # `documents d` is joined unconditionally below, so the lifecycle filter is free
        # here: MaxSim is a retrieval path and must not surface a node whose structure is
        # about to be rebuilt. (No index consideration — the ANN scan is over
        # token_embeddings, and the token rows of an in-flight document are excluded via
        # the join, not via idx_documents_embed.)
        filter_conditions = [
            f"{embed_col_name} IS NOT NULL",
            f"d.settled = '{SETTLED_SETTLED}'",
        ]

        if max_tier is not None:
            filter_conditions.append(f"te.tier <= {max_tier}")

        if parent_id is not None:
            filter_conditions.append(f"d.path @> jsonb_build_array({parent_id})")

        # Point-in-time cutoff on the domain clock, parameterized (never inlined).
        as_of = _as_of_utc(as_of)
        if as_of is not None:
            filter_conditions.append("COALESCE(d.event_time, d.created_at) <= :as_of")

        # Track parameterized exclusion separately so we don't inline user input into SQL
        excl_types_param: Optional[list[str]] = None
        if usetype is not None:
            if _usetype_has_wildcard(usetype):
                like_pattern = _usetype_to_like(usetype)
                filter_conditions.append(f"d.usetype LIKE '{like_pattern}'")
            else:
                filter_conditions.append(f"d.usetype = '{usetype}'")
        else:
            effective = exclude_types if exclude_types is not None else get_settings().search_exclude_usetypes
            if effective:
                filter_conditions.append(
                    "(d.usetype IS NULL OR NOT (d.usetype = ANY(CAST(:excl_types AS text[]))))"
                )
                excl_types_param = effective

        # Subtree RBAC fragment (inlined int PKs; None for owner/unbound or no ACRs).
        # `documents d` is already joined below, so this rides the same scan.
        acl_sql = readable_sql(self.session, alias="d")
        if acl_sql is not None:
            filter_conditions.append(acl_sql)

        filter_sql = " AND ".join(filter_conditions)

        # Collect (document_id -> max_similarity) for each query token
        # Then sum across query tokens for final MaxSim score
        doc_token_max_sims: dict[int, list[float]] = defaultdict(list)

        for q_embed in query_embeddings:
            # Convert embedding to pgvector format
            embed_str = "[" + ",".join(str(x) for x in q_embed) + "]"

            # ANN query using HNSW index - finds K nearest tokens
            # 1 - cosine_distance gives cosine similarity
            # Cast to base vector type — works with both halfvec and tqvec columns
            # (PostgreSQL implicitly casts vector to the column's storage type)
            ann_query = text(f"""
                SELECT te.document_id, 1 - (te.{embed_col_name} <=> CAST(:query_vec AS vector)) as similarity
                FROM token_embeddings te
                JOIN documents d ON te.document_id = d.id
                WHERE {filter_sql}
                ORDER BY te.{embed_col_name} <=> CAST(:query_vec AS vector)
                LIMIT :k
            """)

            ann_params: dict = {"query_vec": embed_str, "k": k_per_token}
            if excl_types_param is not None:
                ann_params["excl_types"] = excl_types_param
            if as_of is not None:
                ann_params["as_of"] = as_of
            results = self.session.execute(ann_query, ann_params).fetchall()

            # Track max similarity per document for this query token
            doc_max_for_token: dict[int, float] = {}
            for doc_id, sim in results:
                if doc_id not in doc_max_for_token or sim > doc_max_for_token[doc_id]:
                    doc_max_for_token[doc_id] = sim

            # Add to running totals
            for doc_id, max_sim in doc_max_for_token.items():
                doc_token_max_sims[doc_id].append(max_sim)

        if not doc_token_max_sims:
            return []

        # Compute final MaxSim scores (sum of max similarities across query tokens)
        doc_scores = {doc_id: sum(sims) for doc_id, sims in doc_token_max_sims.items()}

        # Sort by score
        sorted_doc_ids = sorted(doc_scores.keys(), key=lambda x: doc_scores[x], reverse=True)

        results = []
        for doc_id in sorted_doc_ids[:limit]:
            doc = self.session.get(Document, doc_id)
            if doc:
                results.append(
                    SearchResult(document=doc, score=doc_scores[doc_id], method="maxsim")
                )

        return results

    def maxsim_rerank(
        self,
        candidates: list[SearchResult],
        query_text: str,
        limit: int = 10,
        max_tier: Optional[int] = None,
    ) -> list[SearchResult]:
        """
        Rerank candidate documents using exact MaxSim scoring.

        Unlike maxsim_search() which does ANN queries against the full token
        index, this computes exact pairwise similarities between query tokens
        and the stored tokens of each candidate document. Designed for
        two-stage retrieval: fast first stage (vector/hybrid) → precise
        MaxSim reranking.

        Args:
            candidates: Pre-retrieved search results to rerank
            query_text: Original query text
            limit: Number of results to return after reranking
            max_tier: Filter tokens by tier (5=top 5%, 10=top 10%, etc.)
        """
        if not candidates:
            return []

        import numpy as np

        service = get_embedding_service()

        # Get query token embeddings
        result = service.embed_with_tokens(query_text, top_percent=1.0, prefix="search_query: ")
        if not result.token_embeddings:
            return candidates[:limit]

        # Filter to content tokens only
        content_tokens = [tok for tok in result.token_embeddings if tok.importance_score > 0]
        tokens_to_use = content_tokens if content_tokens else result.token_embeddings

        # Build query token matrix (n_query_tokens × 256)
        query_matrix = np.array(
            [service.truncate_embedding(tok.embedding, 256) for tok in tokens_to_use]
        )  # shape: (Q, 256)

        # Fetch all stored token embeddings for candidate documents in one query
        candidate_doc_ids = [r.document.id for r in candidates]
        doc_map = {r.document.id: r.document for r in candidates}

        tier_filter = ""
        if max_tier is not None:
            tier_filter = f"AND tier <= {max_tier}"

        token_query = text(f"""
            SELECT document_id, embed_256
            FROM token_embeddings
            WHERE document_id = ANY(:doc_ids)
              AND embed_256 IS NOT NULL
              {tier_filter}
            ORDER BY document_id, importance_score DESC
        """)

        rows = self.session.execute(token_query, {"doc_ids": candidate_doc_ids}).fetchall()

        if not rows:
            return candidates[:limit]

        # Group token embeddings by document
        from collections import defaultdict

        doc_tokens: dict[int, list[np.ndarray]] = defaultdict(list)
        for doc_id, embed in rows:
            # embed may be a pgvector string "[0.1,0.2,...]" or a list
            if isinstance(embed, str):
                embed = [float(x) for x in embed.strip("[]").split(",")]
            doc_tokens[doc_id].append(np.array(embed, dtype=np.float32))

        # Score each candidate document
        doc_scores: dict[int, float] = {}
        for doc_id, token_vecs in doc_tokens.items():
            # Build document token matrix (n_doc_tokens × 256)
            doc_matrix = np.array(token_vecs)  # shape: (D, 256)

            # Compute cosine similarity: (Q, 256) @ (256, D) → (Q, D)
            # Both are already L2-normalized, so dot product = cosine similarity
            sim_matrix = query_matrix @ doc_matrix.T  # shape: (Q, D)

            # MaxSim: for each query token, take max similarity across doc tokens
            # Then sum across query tokens
            max_sims = sim_matrix.max(axis=1)  # shape: (Q,)
            doc_scores[doc_id] = float(max_sims.sum())

        # Include candidates that had no token embeddings with score 0
        for doc_id in candidate_doc_ids:
            if doc_id not in doc_scores:
                doc_scores[doc_id] = 0.0

        # Sort by MaxSim score descending
        sorted_ids = sorted(doc_scores.keys(), key=lambda x: doc_scores[x], reverse=True)

        results = []
        for doc_id in sorted_ids[:limit]:
            doc = doc_map.get(doc_id)
            if doc:
                results.append(
                    SearchResult(
                        document=doc,
                        score=doc_scores[doc_id],
                        method="maxsim_rerank",
                    )
                )

        return results

    # =========================================================================
    # Hybrid Search (Reciprocal Rank Fusion)
    # =========================================================================

    @staticmethod
    def _recency_factor(doc: Document, now: datetime, halflife_days: float) -> float:
        """Exponential decay on the document's DOMAIN clock, in [0, 1].

        Reads COALESCE(event_time, created_at): imported content carries the time the
        thing actually happened, authored content falls back to the system clock.

        Naive timestamps are read as UTC — the ORM writes `datetime.utcnow()` into a
        TIMESTAMPTZ column, so naive-means-UTC is this schema's actual invariant, not a
        guess. A future timestamp clamps to 1.0 rather than scoring above maximum.
        """
        stamp = doc.event_time or doc.created_at
        if stamp is None:
            # No clock at all: no evidence of age, so no recency opinion. Neutral,
            # not fabricated — a 0-recency doc would be actively demoted instead.
            return 0.0
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        age_days = (now - stamp).total_seconds() / 86400.0
        if age_days <= 0:
            return 1.0
        return math.exp(-math.log(2) * age_days / halflife_days)

    @staticmethod
    def _importance_factor(doc: Document) -> float:
        """Importance from `structured_content['importance']`, normalised to [0, 1].

        The 1–10 scale is Generative-Agents'; the scorer that writes it is caller
        policy, JMFTS only reads the number. A document with no importance key scores
        0.0 — neutral under the multiplicative form below, since it contributes a
        factor of exactly 1. That is "no evidence of importance", not a default value
        standing in for a real one.

        A present-but-malformed value raises: a non-numeric or out-of-range importance
        means the writer is broken, and silently coercing it would bury that.
        """
        raw = (doc.structured_content or {}).get("importance")
        if raw is None:
            return 0.0
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(
                f"document {doc.id}: structured_content['importance'] must be a number "
                f"on the 1-10 scale, got {type(raw).__name__} {raw!r}"
            )
        if not 1.0 <= float(raw) <= 10.0:
            raise ValueError(
                f"document {doc.id}: structured_content['importance'] must be within "
                f"the 1-10 scale, got {raw!r}"
            )
        return (float(raw) - 1.0) / 9.0

    def hybrid_search(
        self,
        query_text: str,
        limit: int = 10,
        methods: list[str] = None,
        weights: dict[str, float] = None,
        usetype: Optional[str] = None,
        exclude_types: Optional[list[str]] = None,
        parent_id: Optional[int] = None,
        index_name: str = "default",
        recency_weight: float = 0.0,
        importance_weight: float = 0.0,
        recency_halflife_days: float = 7.0,
        now: Optional[datetime] = None,
        as_of: Optional[datetime] = None,
    ) -> list[SearchResult]:
        """
        Hybrid search using Reciprocal Rank Fusion (RRF).

        Combines multiple search methods and fuses their rankings.

        Optionally reranks the fused candidates by recency and/or importance — the
        Generative-Agents retrieval terms. Both weights default to 0.0, which skips the
        rerank entirely and leaves results bit-for-bit identical to plain RRF; callers
        that want the terms opt in explicitly.

        The rerank is multiplicative, applied after fusion:

            final = rrf * (1 + importance_weight * importance)
                        * (1 + recency_weight * recency)

        rather than the paper's additive `a*recency + b*importance + c*relevance`. RRF
        scores are rank-derived and tiny (weight/(k+rank), so <= ~0.016 here) while
        recency and importance are both in [0, 1] — adding them raw would swamp
        relevance outright, and min-max normalising to fix that is unstable across the
        small candidate set (limit*3) this fuses over. The multiplicative form is
        scale-free and monotone in each term: a factor of 1 means "no opinion", so a
        document missing a signal is ranked exactly as plain RRF ranked it.

        Args:
            query_text: Search query
            limit: Max results
            methods: List of methods to use ("vector", "fulltext", "bm25", "maxsim")
            weights: Per-method multiplier on the RRF term. None uses the tuned default
                (0.86/0.14); an explicit empty dict {} means equal-weight RRF (every
                method 1.0) — the tuning-free baseline.
            usetype: Filter to only this document type (overrides exclude_types)
            exclude_types: Exclude these document types; None uses config default
            parent_id: Filter to subtree
            index_name: BM25 index name
            recency_weight: Strength of the recency term (0.0 = off). At 1.0 a
                just-now document scores double a maximally-decayed one.
            importance_weight: Strength of the importance term (0.0 = off), read from
                structured_content['importance'].
            recency_halflife_days: Age at which the recency factor halves. Only
                consulted when recency_weight is non-zero.
            now: Reference time for the decay; defaults to the current UTC time. Pass
                it explicitly to make a corpus with a fixed domain timeline reproducible.
            as_of: Point-in-time retrieval cutoff — every sub-method only considers
                documents whose domain clock COALESCE(event_time, created_at) <= as_of.
                Orthogonal to `now`/recency: `as_of` filters what is *visible*, recency
                reweights what survives. Off when None.

        Returns:
            Fused search results

        Raises:
            ValueError: If a weight is negative, the half-life is non-positive, or a
                candidate carries a malformed importance value.
        """
        if recency_weight < 0 or importance_weight < 0:
            raise ValueError(
                f"rerank weights must be non-negative (0.0 disables the term); got "
                f"recency_weight={recency_weight}, importance_weight={importance_weight}"
            )
        if recency_weight and recency_halflife_days <= 0:
            raise ValueError(
                f"recency_halflife_days must be positive, got {recency_halflife_days}"
            )
        methods = methods or ["vector", "bm25"]
        # Weight resolution distinguishes "not specified" from "specified as no-opinion":
        #   None -> the tuned default (0.86/0.14 from the successive-halving sweep) — the
        #          production ranking, byte-unchanged.
        #   {}   -> plain equal-weight RRF: the fusion loop below reads
        #          weights.get(name, 1.0), so every method gets 1.0. This is the
        #          tuning-free baseline (a respected standard in the fusion literature),
        #          requestable without having to enumerate every method name.
        # Existing callers pass either None or an explicit dict, so their behaviour is
        # unchanged; the empty dict is the new, documented affordance.
        weights = {"vector": 0.86, "bm25": 0.14} if weights is None else weights

        # RRF constant
        k = 60
        candidate_limit = limit * 3  # Get more candidates for fusion

        # Collect results from each method
        method_results: dict[str, list[SearchResult]] = {}

        if "vector" in methods:
            method_results["vector"] = self.vector_search_text(
                query_text, limit=candidate_limit, usetype=usetype, exclude_types=exclude_types, parent_id=parent_id, as_of=as_of
            )

        if "fulltext" in methods:
            method_results["fulltext"] = self.fulltext_search(
                query_text, limit=candidate_limit, usetype=usetype, exclude_types=exclude_types, parent_id=parent_id, as_of=as_of
            )

        if "bm25" in methods:
            method_results["bm25"] = self.bm25_search(
                query_text,
                index_name=index_name,
                limit=candidate_limit,
                usetype=usetype,
                exclude_types=exclude_types,
                parent_id=parent_id,
                as_of=as_of,
            )

        if "maxsim" in methods:
            method_results["maxsim"] = self.maxsim_search(
                query_text, limit=candidate_limit, usetype=usetype, exclude_types=exclude_types, as_of=as_of
            )

        # RRF fusion
        doc_scores: dict[int, float] = {}
        doc_map: dict[int, Document] = {}

        for method_name, results in method_results.items():
            weight = weights.get(method_name, 1.0)
            for rank, result in enumerate(results, 1):
                doc_id = result.document.id
                doc_map[doc_id] = result.document
                rrf_score = weight / (k + rank)
                doc_scores[doc_id] = doc_scores.get(doc_id, 0) + rrf_score

        # Optional Generative-Agents rerank over the fused candidates. Skipped entirely
        # when both weights are 0.0, so the default path is untouched plain RRF.
        if recency_weight or importance_weight:
            reference_now = now or datetime.now(timezone.utc)
            for doc_id, doc in doc_map.items():
                factor = 1.0
                if importance_weight:
                    factor *= 1.0 + importance_weight * self._importance_factor(doc)
                if recency_weight:
                    factor *= 1.0 + recency_weight * self._recency_factor(
                        doc, reference_now, recency_halflife_days
                    )
                doc_scores[doc_id] *= factor

        # Sort by fused score
        sorted_ids = sorted(doc_scores.keys(), key=lambda x: doc_scores[x], reverse=True)

        results = []
        for doc_id in sorted_ids[:limit]:
            results.append(
                SearchResult(document=doc_map[doc_id], score=doc_scores[doc_id], method="hybrid")
            )

        return results

    # =========================================================================
    # Index Management
    # =========================================================================

    def index_document(
        self,
        document_id: int,
        index_name: str = "default",
    ) -> bool:
        """
        Index a document for BM25 search.

        Tokenizes content and updates inverted index tables.
        """
        doc = self.session.get(Document, document_id)
        if not doc or not doc.content:
            return False

        # Skip excluded usetypes (e.g. entity, summary) — Kanban #279
        settings = get_settings()
        if doc.usetype and doc.usetype in settings.bm25_exclude_usetypes:
            return False

        # Get or create index.
        #
        # Axis-B (concurrent indexers of the SAME index): the corpus stats below
        # (total_docs / avg_doc_length) are a read-modify-write in Python — under READ
        # COMMITTED two indexers both read total_docs=N and both write N+1, a lost update
        # that drifts the collection size BM25's length normalisation is derived from
        # (systematically downward under load). Locking the index row with FOR UPDATE
        # serialises indexers of this index so the RMW is safe with no schema change. It
        # holds until commit, so all indexing of one index is serial — fine at
        # single-writer throughput; revisit if bulk parallel indexing of one index lands.
        # (The doc_freq postings are already atomic single-statement RMW.)
        index = self.session.execute(
            select(SearchIndex).where(SearchIndex.name == index_name).with_for_update()
        ).scalar_one_or_none()

        if not index:
            index = SearchIndex(name=index_name)
            self.session.add(index)
            self.session.flush()

        # Tokenize and count
        tokens = self._tokenize(doc.content)
        if not tokens:
            return False

        from collections import Counter

        term_freqs = Counter(tokens)
        doc_length = len(tokens)

        # Upsert index entry
        entry = self.session.execute(
            select(SearchIndexEntry).where(
                and_(
                    SearchIndexEntry.index_id == index.id,
                    SearchIndexEntry.document_id == document_id,
                )
            )
        ).scalar_one_or_none()

        is_new = entry is None
        previous_doc_length = 0 if entry is None else entry.doc_length

        if entry:
            entry.doc_length = doc_length
        else:
            entry = SearchIndexEntry(
                index_id=index.id,
                document_id=document_id,
                doc_length=doc_length,
            )
            self.session.add(entry)

        # ------------------------------------------------------------------
        # Idempotency (docs/KNOWN-DEFECTS.md, D3).
        #
        # Re-indexing is not an error — it is the recovery path.  An incremental
        # indexing pass has to be resumable: a crash must be fixable by re-running
        # it.  This code used to increment doc_freq and total_docs unconditionally,
        # so re-running was precisely what corrupted the collection statistics that
        # BM25's IDF and length normalisation are derived from — putting the two
        # requirements in direct conflict, silently, with no client-side repair.
        #
        # The fix is to diff against what this document already contributed. Its
        # previous terms are still in search_term_postings, so read them BEFORE
        # the delete below.
        # ------------------------------------------------------------------
        old_terms = set(
            self.session.execute(
                text(
                    "SELECT term FROM search_term_postings "
                    "WHERE index_id = :index_id AND document_id = :doc_id"
                ),
                {"index_id": index.id, "doc_id": document_id},
            )
            .scalars()
            .all()
        )
        new_terms = set(term_freqs.keys())

        # Clear old postings
        self.session.execute(
            text(
                "DELETE FROM search_term_postings WHERE index_id = :index_id AND document_id = :doc_id"
            ),
            {"index_id": index.id, "doc_id": document_id},
        )

        # Insert new postings
        for term, freq in term_freqs.items():
            posting = SearchTermPosting(
                index_id=index.id,
                term=term,
                document_id=document_id,
                term_freq=freq,
            )
            self.session.add(posting)

        # Flush postings first
        self.session.flush()

        # doc_freq counts DOCUMENTS containing a term, so only terms this document
        # gained or lost move it.  Terms present both before and after are untouched
        # — that is the double-count that used to corrupt every re-indexed term's IDF.
        for term in new_terms - old_terms:
            self.session.execute(
                text("""
                    INSERT INTO search_term_stats (index_id, term, doc_freq)
                    VALUES (:index_id, :term, 1)
                    ON CONFLICT (index_id, term)
                    DO UPDATE SET doc_freq = search_term_stats.doc_freq + 1
                """),
                {"index_id": index.id, "term": term},
            )

        # A term the document no longer contains must be decounted, or its IDF stays
        # permanently depressed.  Rows that reach zero are deleted, not left at 0.
        for term in old_terms - new_terms:
            self.session.execute(
                text("""
                    UPDATE search_term_stats SET doc_freq = doc_freq - 1
                    WHERE index_id = :index_id AND term = :term
                """),
                {"index_id": index.id, "term": term},
            )
        self.session.execute(
            text(
                "DELETE FROM search_term_stats WHERE index_id = :index_id AND doc_freq <= 0"
            ),
            {"index_id": index.id},
        )

        # Corpus stats: a re-index replaces this document's old length, it does not
        # add a new document.  `is_new` is decided by whether an entry row existed
        # BEFORE the upsert above (`entry` was None), which is the only reliable
        # signal of membership — there is no other per-document index-membership query.
        if is_new:
            index.total_docs = (index.total_docs or 0) + 1
            total_length = (index.avg_doc_length or 0) * ((index.total_docs or 1) - 1) + doc_length
        else:
            total_length = (
                (index.avg_doc_length or 0) * (index.total_docs or 1)
                - previous_doc_length
                + doc_length
            )
        index.avg_doc_length = total_length / max(1, index.total_docs or 1)

        return True

    def create_index(
        self,
        name: str,
        description: str = None,
        config: dict = None,
        capabilities: dict = None,
    ) -> SearchIndex:
        """Create a new search index."""
        index = SearchIndex(
            name=name,
            description=description,
            config=config or {"k1": 1.2, "b": 0.75},
            capabilities=capabilities or {"bm25": True, "maxsim": True},
        )
        self.session.add(index)
        self.session.flush()
        return index

    def get_index(self, name: str) -> SearchIndex:
        """Get a search index by name."""
        return self.session.execute(
            select(SearchIndex).where(SearchIndex.name == name)
        ).scalar_one_or_none()

    def list_indexes(self) -> list[SearchIndex]:
        """List all search indexes."""
        return list(self.session.execute(select(SearchIndex)).scalars().all())

    def add_root_to_index(self, index_name: str, root_document_id: int) -> bool:
        """Add a document subtree to an index."""
        index = self.get_index(index_name)
        if not index:
            return False

        # Check if already a member
        from jmfts_core.models.search_index import SearchIndexMember

        existing = self.session.execute(
            select(SearchIndexMember).where(
                and_(
                    SearchIndexMember.index_id == index.id,
                    SearchIndexMember.root_document_id == root_document_id,
                )
            )
        ).scalar_one_or_none()

        if existing:
            return True  # Already a member

        member = SearchIndexMember(
            index_id=index.id,
            root_document_id=root_document_id,
        )
        self.session.add(member)
        return True

    def remove_root_from_index(self, index_name: str, root_document_id: int) -> bool:
        """Remove a document subtree from an index."""
        index = self.get_index(index_name)
        if not index:
            return False


        self.session.execute(
            text(
                "DELETE FROM search_index_members WHERE index_id = :index_id AND root_document_id = :root_id"
            ),
            {"index_id": index.id, "root_id": root_document_id},
        )
        return True

    def get_index_roots(self, index_name: str) -> list[int]:
        """Get root document IDs for an index."""
        index = self.get_index(index_name)
        if not index:
            return []

        from jmfts_core.models.search_index import SearchIndexMember

        return list(
            self.session.execute(
                select(SearchIndexMember.root_document_id).where(
                    SearchIndexMember.index_id == index.id
                )
            )
            .scalars()
            .all()
        )

    def refresh_index(self, index_name: str) -> dict:
        """
        Rebuild an index from its member subtrees.

        Clears existing entries and re-indexes all documents in member subtrees.
        Returns stats about the refresh operation.
        """
        index = self.get_index(index_name)
        if not index:
            return {"error": "Index not found"}

        # Get all root document IDs
        root_ids = self.get_index_roots(index_name)
        if not root_ids:
            return {"error": "No roots in index", "indexed": 0}

        # Clear existing index data
        self.session.execute(
            text("DELETE FROM search_term_postings WHERE index_id = :index_id"),
            {"index_id": index.id},
        )
        self.session.execute(
            text("DELETE FROM search_term_stats WHERE index_id = :index_id"), {"index_id": index.id}
        )
        self.session.execute(
            text("DELETE FROM search_index_entries WHERE index_id = :index_id"),
            {"index_id": index.id},
        )

        # Reset index stats
        index.total_docs = 0
        index.avg_doc_length = 0

        # Get all documents in member subtrees
        from jmfts_core.repositories.document import DocumentRepository

        doc_repo = DocumentRepository(self.session)

        all_docs = []
        for root_id in root_ids:
            # Settled-only (the default), unlike the ingest pipeline's own indexing
            # stage. A full corpus rebuild is a reader, not the owner of the tree: it
            # must not fold half-written chunks into the term statistics, because IDF is
            # global and a rebuild that included them would shift the scores of every
            # unrelated document. If a registered root is itself in flight this raises
            # InFlightSubtreeError rather than rebuilding around it — a corpus rebuild
            # that silently skipped a member subtree would leave the index quietly wrong.
            subtree = doc_repo.get_subtree(root_id)
            all_docs.extend(subtree)

        # Deduplicate by ID
        seen_ids = set()
        unique_docs = []
        for doc in all_docs:
            if doc.id not in seen_ids:
                seen_ids.add(doc.id)
                unique_docs.append(doc)

        # ------------------------------------------------------------------
        # Bulk build. The clears above already emptied this index, so
        # index_document()'s per-document machinery — the (index_id,
        # document_id) SELECT+DELETE of prior postings and the per-term
        # doc_freq diff — is dead weight on a rebuild, and its per-doc,
        # per-term round-trips make the rebuild quadratic in the postings
        # already written (the SELECT/DELETE scan a table with no
        # (index_id, document_id) index that is growing in this same
        # transaction). Instead tokenize with the SAME _tokenize and
        # batch-insert entries -> postings -> stats.
        #
        # FK ordering: search_term_postings references search_index_entries
        # on (index_id, document_id), so a document's entry must be inserted
        # before its postings. We flush per chunk of documents to preserve
        # that while bounding memory — a full rebuild's postings can be tens
        # of millions of rows.
        # ------------------------------------------------------------------
        from collections import Counter
        from psycopg2.extras import execute_values

        settings = get_settings()
        excluded = set(settings.bm25_exclude_usetypes or ())
        cur = self.session.connection().connection.driver_connection.cursor()

        CHUNK_DOCS = 5000
        doc_freq: Counter = Counter()
        chunk_entries: list = []
        chunk_postings: list = []
        indexed_count = 0

        def _flush_chunk():
            if chunk_entries:
                execute_values(
                    cur,
                    "INSERT INTO search_index_entries (index_id, document_id, doc_length) "
                    "VALUES %s",
                    chunk_entries,
                    page_size=10000,
                )
            if chunk_postings:
                execute_values(
                    cur,
                    "INSERT INTO search_term_postings (index_id, term, document_id, term_freq) "
                    "VALUES %s",
                    chunk_postings,
                    page_size=10000,
                )
            chunk_entries.clear()
            chunk_postings.clear()

        for doc in unique_docs:
            if not doc.content:
                continue
            if doc.usetype and doc.usetype in excluded:
                continue
            term_freqs = Counter(self._tokenize(doc.content))
            if not term_freqs:
                continue
            chunk_entries.append((index.id, doc.id, sum(term_freqs.values())))
            for term, freq in term_freqs.items():
                chunk_postings.append((index.id, term, doc.id, freq))
            doc_freq.update(term_freqs.keys())
            indexed_count += 1
            if len(chunk_entries) >= CHUNK_DOCS:
                _flush_chunk()
        _flush_chunk()

        if doc_freq:
            execute_values(
                cur,
                "INSERT INTO search_term_stats (index_id, term, doc_freq) VALUES %s",
                [(index.id, term, df) for term, df in doc_freq.items()],
                page_size=10000,
            )
        cur.close()

        self.session.flush()

        # index_document maintains total_docs / avg_doc_length incrementally, which is
        # exact but accumulates float error over a long rebuild.  A refresh has every
        # entry in front of it, so recompute the aggregate from the entries themselves
        # and let that be authoritative.
        stats = self.session.execute(
            text(
                "SELECT COUNT(*) AS n, COALESCE(AVG(doc_length), 0) AS avg_len "
                "FROM search_index_entries WHERE index_id = :index_id"
            ),
            {"index_id": index.id},
        ).one()
        index.total_docs = stats.n
        index.avg_doc_length = float(stats.avg_len)

        return {
            "index": index_name,
            "roots": root_ids,
            "total_docs_found": len(unique_docs),
            "indexed": indexed_count,
        }
