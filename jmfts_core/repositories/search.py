"""Search Repository - Vector, BM25, and Hybrid Search"""

import fnmatch
import logging
import math
import re
from datetime import datetime, timezone
from typing import Optional
from dataclasses import dataclass
from sqlalchemy import select, func, text, and_, or_
from sqlalchemy.orm import Session

from jmfts_client.contracts.search import (
    DEFAULT_HYBRID_METHODS,
    UsetypeFilter,
    usetype_globs,
    validate_search_methods,
)
from jmfts_core.models.document import Document, SETTLED_SETTLED
from jmfts_core.models.search_index import (
    SearchIndex,
    SearchIndexEntry,
    SearchTermPosting,
)
from jmfts_core.embedding import get_embedding_service
from jmfts_core.config import get_settings
from jmfts_core.access import readable_filter, readable_sql
from jmfts_core.effective_content import frontier_members

logger = logging.getLogger(__name__)

#: The iterative-scan mode :meth:`SearchRepository.vector_search` asks pgvector for.
#: ``docs/SPRINT_0_4_0.md`` Block A step 2.
#:
#: With ``off`` — pgvector's default, and what every release up to 0.3.0 ran under — an
#: HNSW scan takes ``hnsw.ef_search`` candidates out of the graph, applies the query's
#: other predicates to those rows, and stops. A principal whose readable subtree holds
#: none of the nearest neighbours therefore gets an EMPTY page while thousands of
#: documents it may read match. Block A measured that: 0 of 10 rows at both ``ef_search =
#: 40`` and ``ef_search = 400``, which is the finding that says no amount of tuning a
#: number is a fix. ``iterative_scan`` is the different mechanism — it re-enters the graph
#: when the filter empties a batch.
#:
#: ``strict_order`` rather than ``relaxed_order``, and the reason is this method's own
#: contract. Both modes returned complete pages at every setting Block A tested, but
#: ``relaxed_order`` may return them out of distance order, and two callers depend on the
#: order rather than on the set: ``vector_search`` documents "sorted by score descending",
#: and :meth:`SearchRepository.hybrid_search` fuses by RANK POSITION (``rrf_score =
#: weight / (k + rank)``), so a page shuffled inside itself silently reweights the fusion.
#:
#: This setting turns out to be the strongest single mitigation measured for dead index
#: entries, and it was nearly discarded as ineffective. `docs/ANN_INDEX_HEALTH.md` 1.3
#: records the earlier reading — "reduces the rate, never to zero" — taken on a metric 1.6
#: retires for measuring its own fixture. On recall@10 against a brute-force scan (1.7),
#: this line is worth 0.100 -> 0.999 at 50 dead entries per query point and 0.105 -> 0.979
#: at 500, for a latency cost inside noise. It is not immunity: at the 500 dose, 3 of 100
#: queries were still degraded and ONE returned nothing at all.
#:
#: Do not substitute a larger ``hnsw.ef_search`` for it. 200 was clean at the 50 dose and
#: collapsed to 0.099 at 500 — a bigger candidate budget buys a bigger dose before failure,
#: where the scan mode changes how the walk terminates.
HNSW_ITERATIVE_SCAN = "strict_order"

#: Where the per-connection answer to "does this server register ``hnsw.iterative_scan``"
#: is cached. ``Connection.info`` is keyed to the DBAPI connection rather than to the
#: Session, which is the right lifetime: the probe below loads a shared library into one
#: backend, and that backend keeps it loaded.
_ITERATIVE_SCAN_INFO_KEY = "jmfts_hnsw_iterative_scan"


def _hnsw_iterative_scan_available(session: Session) -> bool:
    """Does this server register ``hnsw.iterative_scan``, a pgvector 0.8.0 GUC?

    **Asking is not optional and neither is the load.** pgvector registers its settings in
    ``_PG_init``, which does not run until the shared library is loaded into the backend,
    and ``CREATE EXTENSION`` alone does not load it. Two measured consequences, both of
    which would defeat a naive implementation of Block A step 2 (pgvector 0.8.5,
    ``pgvector/pgvector:pg16``, 2026-09-05):

    * On a fresh connection ``pg_settings`` reports the setting ABSENT on a server that
      supports it perfectly well. ``scripts/filtered_recall.py:109``
      (``_supports_iterative_scan``) fell into this once and skipped the two modes it
      existed to test.
    * ``SET hnsw.iterative_scan = strict_order`` on that same fresh connection SUCCEEDS,
      and ``SHOW`` reads the value back. Nothing has reserved the ``hnsw`` prefix yet, so
      Postgres accepts it as a placeholder custom GUC. On a server new enough it is later
      adopted by the real setting; on a server too old it stays a string nobody reads, and
      the scan goes on truncating while the code looks like it fixed something. That is
      exactly the silent fall-through this function exists to prevent.

    So: force the library in with a cast (any use of the type's input function does it,
    and unlike ``LOAD 'vector'`` it needs no superuser), THEN ask. A server that answers
    no gets a warning naming its pgvector version and no ``SET`` at all — the honest
    reading is reported to the caller through ``AppliedFilters.truncated``, which is why
    Block A step 3 is not made redundant by step 2.
    """
    connection = session.connection()
    cached = connection.info.get(_ITERATIVE_SCAN_INFO_KEY)
    if cached is not None:
        return cached

    session.execute(text("SELECT CAST('[1]' AS vector)"))
    available = bool(
        session.execute(
            text("SELECT count(*) FROM pg_settings WHERE name = 'hnsw.iterative_scan'")
        ).scalar()
    )
    connection.info[_ITERATIVE_SCAN_INFO_KEY] = available

    if not available:
        version = session.execute(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        ).scalar()
        logger.warning(
            "pgvector %s does not register hnsw.iterative_scan (0.8.0+); a filtered "
            "vector search may return a short or empty page while matching documents "
            "exist. AppliedFilters.truncated reports when a page came back short. "
            "See docs/SPRINT_0_4_0.md Block A.",
            version or "unknown",
        )

    return available


@dataclass
class SearchResult:
    """A single search result"""

    document: Document
    score: float
    method: str  # "vector", "bm25", "fulltext", "maxsim", "hybrid"


def _usetype_has_wildcard(usetype: str) -> bool:
    """Check if a usetype glob contains wildcard characters."""
    return "*" in usetype or "?" in usetype


def _usetype_to_like(usetype: str) -> str:
    """Convert ONE glob-style usetype pattern to a SQL LIKE pattern.

    Supports * (any chars) and ? (single char). Escapes SQL LIKE specials.

    One glob in, one pattern out, and that is the whole contract: this function does not
    know that a filter may name several. Splitting a filter into globs is
    :func:`jmfts_client.contracts.search.usetype_globs`, and keeping the two apart is what
    the defect below was. Before ``usetype_globs`` existed a filter WAS one glob, so a
    preset written ``"transcript:*,obsidian:*"`` arrived here whole and left as
    ``transcript:%,obsidian:%`` — a pattern matching nothing, on a named preset, on every
    install, with an empty page and no error to say why.
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


def _usetype_matches(globs: tuple[str, ...], usetype_value: Optional[str]) -> bool:
    """In-memory match of one document's usetype against a set of globs (BM25 post-filter).

    ANY, not ALL: the globs are alternatives. A NULL usetype matches no positive filter,
    which is the same rule the SQL paths get for free from ``LIKE``/``=`` on NULL.
    """
    if usetype_value is None:
        return False
    return any(
        (
            fnmatch.fnmatch(usetype_value, glob)
            if _usetype_has_wildcard(glob)
            else usetype_value == glob
        )
        for glob in globs
    )


def _usetype_predicate(globs: tuple[str, ...]):
    """The ORM predicate for a set of usetype globs: OR over the alternatives.

    Exact globs are collected into ONE membership test and wildcard globs into one ``LIKE``
    each, rather than every glob becoming a ``LIKE``. Two reasons, and the first is not
    cosmetic: ``idx_documents_usetype`` is a plain btree, so ``=`` and ``IN`` are
    index-searchable while ``LIKE 'literal'`` is only index-searchable under a C collation.
    Splitting the two keeps a set of exact usetypes on the index. The second is that a
    single-glob filter — every filter that could be written before this sprint — produces
    the IDENTICAL statement it produced before: ``usetype = :x`` or ``usetype LIKE :x``,
    with no ``OR`` wrapper and no plan change.
    """
    exact = [glob for glob in globs if not _usetype_has_wildcard(glob)]
    wild = [glob for glob in globs if _usetype_has_wildcard(glob)]

    clauses = []
    if len(exact) == 1:
        clauses.append(Document.usetype == exact[0])
    elif exact:
        clauses.append(Document.usetype.in_(exact))
    clauses.extend(Document.usetype.like(_usetype_to_like(glob)) for glob in wild)

    # `usetype_globs` guarantees at least one glob, so `clauses` is never empty and this
    # never degenerates into `or_()` — which is SQL false, and would turn a filter the
    # caller wrote into an empty page.
    return clauses[0] if len(clauses) == 1 else or_(*clauses)


def _apply_usetype_filter(query, usetype: UsetypeFilter):
    """Apply a positive usetype filter to a SQLAlchemy query. Raises on an empty filter.

    Kept as the one entry point for a caller that holds an UNNORMALISED filter and wants a
    query back — ``rdf/shacl.py:279`` is the one outside this module, and it is deliberate:
    a SHACL binding's ``usetype`` scope and a search's ``usetype`` filter are the same word
    and must mean the same thing, so a scope now names a set of globs exactly the way a
    search does. The four search methods in this file normalise once at the top instead,
    because each of them also has to ask whether the filter was given at all.
    """
    globs = usetype_globs(usetype)
    if globs is None:
        raise ValueError(
            "_apply_usetype_filter needs a usetype filter and was given None; a caller that "
            "may have no filter must branch on usetype_globs() rather than narrow by nothing."
        )
    return query.where(_usetype_predicate(globs))


def _usetype_sql(globs: tuple[str, ...], alias: str) -> tuple[str, dict]:
    """The same predicate as :func:`_usetype_predicate`, for the hand-written MaxSim SQL.

    Returns ``(fragment, bind_params)``. The globs are BOUND, never interpolated: this
    filter comes from a request body, a URL query parameter or a ``search_contexts.config``
    blob, and the two lines this replaced formatted it straight into the statement
    (``f"d.usetype LIKE '{like_pattern}'"``), which a usetype containing an apostrophe was
    enough to break.
    """
    exact = [glob for glob in globs if not _usetype_has_wildcard(glob)]
    wild = [glob for glob in globs if _usetype_has_wildcard(glob)]

    fragments: list[str] = []
    params: dict = {}
    if exact:
        fragments.append(f"{alias}.usetype = ANY(CAST(:usetype_exact AS text[]))")
        params["usetype_exact"] = exact
    if wild:
        fragments.append(f"{alias}.usetype LIKE ANY(CAST(:usetype_globs AS text[]))")
        params["usetype_globs"] = [_usetype_to_like(glob) for glob in wild]

    return "(" + " OR ".join(fragments) + ")", params


def _apply_usetype_exclusion(query, exclude_usetypes: list[str]):
    """Exclude documents with specific usetypes from results. NULL usetype is never excluded."""
    if not exclude_usetypes:
        return query
    query = query.where(or_(Document.usetype.is_(None), Document.usetype.notin_(exclude_usetypes)))
    return query


def effective_exclude_types(
    usetype: Optional[UsetypeFilter], exclude_types: Optional[list[str]]
) -> list[str]:
    """The usetypes a search with these two arguments actually holds out.

    The three-way resolution — a positive ``usetype`` overrides exclusion entirely, an
    explicit list is used as given (``[]`` disables exclusion), and ``None`` falls back to
    ``JMFTS_SEARCH_EXCLUDE_USETYPES`` — was written out at each of the four search methods
    and nowhere else, so nothing outside this module could say what a response had been
    filtered by. ``SearchService`` calls this to fill ``SearchResponse.applied``.

    It is a pure function of its two arguments plus settings, so calling it beside the
    query rather than inside it cannot disagree with what the query did.

    ``usetype_globs`` rather than ``if usetype:``, and that is a behaviour change worth
    stating: an empty ``usetype`` used to be falsy here and fall through to the exclusion
    branch, so a request that named a filter got the DEFAULT hold-out list and an unfiltered
    page. It now raises, in this function and in every query that reads it, so the two
    cannot answer differently.
    """
    if usetype_globs(usetype) is not None:
        return []
    if exclude_types is not None:
        return list(exclude_types)
    return list(get_settings().search_exclude_usetypes)


#: The tuned production weights, from the successive-halving sweep. Two entries for the two
#: methods :data:`DEFAULT_HYBRID_METHODS` runs; a method outside this map is fused at 1.0,
#: which is what ``weights.get(name, 1.0)`` in the fusion loop does and what
#: ``SearchResponse.applied.weights`` now lets a caller see.
TUNED_HYBRID_WEIGHTS: dict[str, float] = {"vector": 0.86, "bm25": 0.14}


def effective_weights(weights: Optional[dict[str, float]]) -> dict[str, float]:
    """The per-method RRF multipliers a fusion with this argument actually uses.

    Weight resolution distinguishes "not specified" from "specified as no-opinion":

    * ``None`` -> :data:`TUNED_HYBRID_WEIGHTS`, the production ranking, byte-unchanged.
    * ``{}``   -> plain equal-weight RRF: the fusion loop reads ``weights.get(name, 1.0)``,
      so every method gets 1.0. This is the tuning-free baseline (a respected standard in
      the fusion literature), requestable without enumerating every method name.

    Existing callers pass either ``None`` or an explicit dict, so their behaviour is
    unchanged; the empty dict is the documented affordance.
    """
    return dict(TUNED_HYBRID_WEIGHTS) if weights is None else dict(weights)


class SearchRepository:
    """Repository for search operations"""

    def __init__(self, session: Session):
        self.session = session
        #: Did an approximate (ANN) scan run through THIS repository come back short of
        #: the rows it asked for? ``None`` until one runs, which is how a BM25-only or
        #: full-text-only search reports "no ANN scan" rather than "complete".
        #:
        #: ``SearchService`` reads this after the call and puts it on
        #: ``AppliedFilters.truncated``. It is an attribute rather than a second return
        #: value because ``hybrid_search`` runs several legs through one repository and
        #: the caller needs the fact about the FUSION, not about whichever leg went last —
        #: :meth:`_note_ann_page` ORs the legs together for exactly that reason.
        self.scan_truncated: Optional[bool] = None

    def _note_ann_page(self, returned: int, requested: int) -> None:
        """Record whether one approximate scan filled its request.

        ``docs/SPRINT_0_4_0.md`` Block A step 3. What is recorded is a FACT — the page came
        back short — and not an inference about why, because the "why" is not observable
        from here: pgvector reports no "I stopped early" signal, and short-because-the-walk-
        stopped is indistinguishable at this layer from short-because-nothing-else-matched.
        Saying which one it was would be a guess; saying the page is short is what lets the
        caller stop reading a short page as an exhausted corpus, which is the Fail Early
        requirement Block A states.

        HOW OFTEN that guess would be wrong is now measured, and it is the reason this stays
        a report and does not become a trigger. `docs/ANN_INDEX_HEALTH.md` 1.5 wrapped this
        method for one suite run: 44 of 51 ANN pages came back short — 86.3% — and only 4
        returned nothing, because the suite's corpora hold a handful of documents while its
        requests ask for 9 to 100 rows. Every requested size at or above 9 was short every
        time. A policy that vacuumed and retried on this signal would therefore fire on most
        searches, and 1.9 measures what each firing costs: the repair works (recall recovers
        to 1.0) but the VACUUM is 5.8 s on a 78 MB index and about 87 s on a 391 MB one.
        `ANN_INDEX_HEALTH.md` Part 2 option C is that trade written out.
        """
        short = returned < requested
        self.scan_truncated = (
            short if self.scan_truncated is None else (self.scan_truncated or short)
        )

    # =========================================================================
    # Vector Search
    # =========================================================================

    def vector_search(
        self,
        query_embedding: list[float],
        limit: int = 10,
        usetype: Optional[UsetypeFilter] = None,
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
            usetype: Restrict to documents matching ANY of these globs — one glob, a
                comma-separated set, or a list of them (overrides exclude_types).
                Omit it for no positive filter; an empty set raises.
            exclude_types: Exclude these document types; None uses config default
            parent_id: Filter to subtree
            threshold: Minimum similarity score
            as_of: Point-in-time cutoff — only documents whose domain clock
                COALESCE(event_time, created_at) <= as_of. Off when None.

        Returns:
            List of SearchResults sorted by score descending

        Side effect: sets :attr:`scan_truncated` to whether this page came back short of
        ``limit``. ``docs/SPRINT_0_4_0.md`` Block A step 3 — see :meth:`_note_ann_page`.
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

        globs = usetype_globs(usetype)
        if globs is not None:
            query = query.where(_usetype_predicate(globs))
        else:
            query = _apply_usetype_exclusion(query, effective_exclude_types(usetype, exclude_types))

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

        # ORDER BY THE DISTANCE OPERATOR, NOT BY THE SIMILARITY LABEL.
        # `docs/SPRINT_0_4_0.md` Block A step 1, and the reason that step exists at all.
        #
        # Up to 0.3.0 this read `text("score DESC")`, which orders by the label built
        # above — `(1 - (embed <=> q)) DESC`. pgvector's HNSW index answers `ORDER BY col
        # <=> q` ASCENDING and nothing else; the two expressions describe the same total
        # order and Postgres does not rewrite one into the other, so idx_documents_embed
        # (`sql/schema.sql:478`) was unreachable and every vector search was a full scan
        # of the settled embedded documents plus a top-N heapsort. Measured on
        # `tests/test_filtered_recall.py`'s fixture: the label form costs 2675.91, the
        # operator form 8.03, and `enable_seqscan = off` does not move the choice —
        # the planner was not preferring the sort, it had no index option to prefer.
        #
        # `score` is untouched: the SELECT list still returns `1 - distance`, callers still
        # get similarity where higher is better, and ascending distance is the identical
        # ordering of the identical rows. Only the expression the planner sees changes.
        query = query.order_by(Document.embed.cosine_distance(query_embedding)).limit(limit)

        # Step 2, and it is a PREREQUISITE of the line above rather than a peer of it: the
        # RBAC truncation Block A measured needs the index, so correcting the ORDER BY is
        # what makes it reachable. Guarded, because a placeholder SET on a server that does
        # not have the GUC is worse than not setting it — see
        # `_hnsw_iterative_scan_available`. SET LOCAL, so the mode is scoped to the
        # caller's transaction and never leaks back into the pool.
        if _hnsw_iterative_scan_available(self.session):
            self.session.execute(text(f"SET LOCAL hnsw.iterative_scan = {HNSW_ITERATIVE_SCAN}"))

        results = []
        for doc, score in self.session.execute(query).all():
            results.append(SearchResult(document=doc, score=float(score), method="vector"))

        # Step 3. Even with step 2 in force both iterative modes stop at
        # `hnsw.max_scan_tuples`, and a server too old for step 2 does not iterate at all,
        # so the caller still has to be able to tell a complete page from a bounded one.
        self._note_ann_page(len(results), limit)

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
        usetype: Optional[UsetypeFilter] = None,
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

        query = (
            select(
                Document,
                func.ts_rank(
                    func.to_tsvector(
                        "english",
                        func.coalesce(Document.title, "")
                        + " "
                        + func.coalesce(Document.content, ""),
                    ),
                    tsquery,
                ).label("score"),
            )
            .where(
                func.to_tsvector(
                    "english",
                    func.coalesce(Document.title, "") + " " + func.coalesce(Document.content, ""),
                ).op("@@")(tsquery)
                # Same clause idx_documents_content_fts is partial on. It must be present
                # for the GIN index to be usable at all, and it is the retrieval rule
                # anyway: a tree that is still being built is not an answer.
            )
            .where(Document.settled == SETTLED_SETTLED)
        )

        globs = usetype_globs(usetype)
        if globs is not None:
            query = query.where(_usetype_predicate(globs))
        else:
            query = _apply_usetype_exclusion(query, effective_exclude_types(usetype, exclude_types))

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
        usetype: Optional[UsetypeFilter] = None,
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
            usetype: Restrict to documents matching ANY of these globs — one glob, a
                comma-separated set, or a list of them (overrides exclude_types).
                Omit it for no positive filter; an empty set raises.
            exclude_types: Exclude these document types; None uses config default
            parent_id: Filter to subtree
            as_of: Point-in-time cutoff on COALESCE(event_time, created_at). Off when None.

        Returns:
            List of SearchResults sorted by BM25 score
        """
        # Normalised FIRST, above the two early `return []`s below: a malformed filter is a
        # malformed request whether or not this index exists and whether or not the query
        # tokenizes, and "no index" must not swallow it.
        globs = usetype_globs(usetype)

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
        as_of = _as_of_utc(as_of)

        # THE DOCUMENTS JOIN IS UNCONDITIONAL, AND THE SETTLED GATE IS WHY. Every other
        # document-scoped predicate here — subtree, `as_of`, access control — is optional,
        # and the join used to be optional with them. `settled = 'settled'` is not: it is
        # the retrieval rule the other three paths already state in SQL (`vector_search` at
        # both of its queries, `maxsim_search`, and this method's own CONTAINER pass, which
        # gates `a.settled` in `_container_candidates`), and the leaf scan stated it
        # nowhere. `search_term_postings` -> `search_index_entries` never reaches
        # `documents`, so there was no row to test and nowhere to put the predicate.
        #
        # The postings are real. `index_document` gates on content and on
        # `bm25_exclude_usetypes` and on nothing else — `refresh_index` is the settled-only
        # path, the incremental one is not. So BM25 returned in-flight nodes while the
        # other three methods did not, and `tests/test_bm25_settled_gate.py` is the test
        # that said so.
        #
        # WHAT WROTE THEM CHANGED ON 2026-09-13 AND THE GATE IS NOT LESS NEEDED FOR IT.
        # `index:bm25` used to be a `TASK_ROWS` row that fired straight after the structure
        # rung, while every `embed` below was still running, so a whole prose file was
        # answerable for the duration of its own embedding. `SPRINT_0_6_0.md` Block B step 7
        # made it a rule at the settling boundary over settled nodes (`jmfts_core.index_tasks`),
        # which narrows the window to one node: the boundary node itself is written while
        # its own `settled` column still reads `in_flight`, because `settle_node` sets that
        # column after the planner it just asked returns nothing. It is invisible here until
        # it does, which is the correct answer and is this gate producing it. Nothing else
        # is narrowed: `POST /documents` still writes into `default` inline when
        # `auto_index_bm25` is set (`document_service.create_document`), and
        # `POST /indexes/{name}/index-document/{id}` writes any node into any index.
        #
        # IN the scored CTE rather than after it, for the reason the ACL note below already
        # gives: a post-filter would trim the page after `LIMIT` and return a SHORT page
        # instead of a wrong one, which is quieter and just as wrong.
        #
        # IT IS NOT FREE, AND WHETHER IT IS FASTER DEPENDS ENTIRELY ON HOW MUCH IT PRUNES.
        # Measured 2026-09-10 on 30,000 documents / 1.72M postings, pgvector:pg16, median of
        # 9 EXPLAIN ANALYZE runs, three query shapes from 475 to 59,313 matching postings:
        #
        #   half the corpus in flight   unscoped  -26%..-16%    scoped  -20%..-8%
        #   nothing in flight           unscoped  +18%..+27%    scoped   +0%..+3%
        #
        # So on a corpus with real in-flight work the gate pays for itself by pruning before
        # the `GROUP BY`, and on a fully settled corpus — the steady state — it costs about a
        # fifth of the unscoped query for a `documents` join that was not there before. The
        # scoped case is at parity either way, because it already had the join and this adds
        # one column test to it. That cost is the price of the retrieval rule, not an
        # argument against it; it is written down here so nobody re-derives it as a surprise.
        #
        # Interpolated rather than bound, matching `maxsim_search`'s
        # f"d.settled = '{SETTLED_SETTLED}'" — the only other raw-SQL site in this file that
        # names the constant. `SETTLED_SETTLED` is a module constant, never a caller's value.
        doc_join = "JOIN documents d ON d.id = tp.document_id"
        doc_where = f" AND d.settled = '{SETTLED_SETTLED}'"

        # Subtree RBAC fragment (inlined int PKs; None for owner/unbound or no ACRs).
        # Keeping ACL in the scored CTE keeps top-k correct rather than trimmed by a
        # post-filter. It no longer has to force the join — the gate above already did.
        acl_sql = readable_sql(self.session, alias="d")
        if parent_id is not None:
            doc_where += " AND d.path @> jsonb_build_array(:parent_id)"
        if as_of is not None:
            doc_where += " AND COALESCE(d.event_time, d.created_at) <= :as_of"
        if acl_sql is not None:
            doc_where += f" AND {acl_sql}"

        # The same three predicates, against the CONTAINER row rather than the descendant
        # that matched, for `_container_scores`. Built here beside their originals so the
        # two cannot fall out of step: a predicate added above and not here would be a
        # filter every leaf obeys and every container ignores.
        #
        # The FOURTH predicate, `settled`, is the exception and is deliberately not here:
        # `_container_candidates` carries `a.settled = 'settled'` inline, because it is not
        # optional there either. The two gates are not redundant and neither subsumes the
        # other — `d` is the descendant that owns the matching posting, `a` is the ancestor
        # being scored, and a container has no posting of its own for the leaf gate to
        # reach. What makes the container gate sufficient for its frontier as well is that
        # settling is bottom-up (`schema.sql`: settled means *"its own work is done AND
        # every child is settled"*), so a settled container cannot have an in-flight
        # descendant in a well-formed tree.
        container_gate = ""
        if parent_id is not None:
            container_gate += " AND a.path @> jsonb_build_array(:parent_id)"
        if as_of is not None:
            container_gate += " AND COALESCE(a.event_time, a.created_at) <= :as_of"
        container_acl = readable_sql(self.session, alias="a")
        if container_acl is not None:
            container_gate += f" AND {container_acl}"

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
                {doc_join}
                WHERE tp.index_id = :index_id
                    AND tp.term = ANY(CAST(:terms AS text[]))
                    {doc_where}
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

        scored = list(self.session.execute(bm25_query, bind_params))

        # A container has no postings — its text is its frontier's — so the query above
        # cannot find it however well it matches. `_container_scores` scores those against
        # the SAME statistics without writing a posting; see its docstring for why the two
        # identities it sums are exact. Merged and re-sorted here rather than unioned into
        # the SQL, because the container pass needs the frontier walk between two queries.
        container_gate_params = {}
        if parent_id is not None:
            container_gate_params["parent_id"] = parent_id
        if as_of is not None:
            container_gate_params["as_of"] = as_of
        containers = self._container_scores(
            index,
            terms,
            k1,
            b,
            bind_params["limit"],
            gate=container_gate,
            gate_params=container_gate_params,
        )
        if containers:
            scored = sorted(
                list(scored) + list(containers.items()), key=lambda row: row[1], reverse=True
            )[: bind_params["limit"]]

        # Fetch documents and apply usetype/exclusion filters
        # BM25 entities/summaries are excluded at index time; exclude_types adds runtime post-filter
        effective_exclusions = (
            exclude_types if (globs is None and exclude_types is not None) else []
        )
        results = []
        for doc_id, score in scored:
            doc = self.session.get(Document, doc_id)
            if not doc:
                continue
            if globs is not None and not _usetype_matches(globs, doc.usetype):
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

    # -- BM25 for a node whose text is its children's -----------------------------

    def _container_candidates(
        self, index_id: int, terms: list, limit: int, gate: str, gate_params: dict
    ) -> list:
        """Container nodes that could score for ``terms``, most-matching first.

        An ancestor of a matching posting, with no ``content`` of its own — which is the
        definition of a node whose text is computed rather than stored, and therefore the
        definition of a node with no postings to be found by. ``documents.path`` holds the
        ancestor ids outright, so this is a join and not a walk.

        ``gate`` carries the SAME subtree, ``as_of`` and access-control predicates the leaf
        query applies, evaluated against the CONTAINER row rather than the descendant that
        matched. It is not optional and it is not a post-filter: a container is a document,
        an unreadable one must not be reachable by having a readable child, and a container
        outside the requested subtree is outside it however deep the match was.

        **THIS IS A BOUNDED APPROXIMATION AND THE BOUND IS ``limit``.** Ranking containers
        exactly would mean scoring every one of them, and on the reference corpus a
        stopword-bearing query reaches 18,435 documents whose paths name most of the tree.
        What is ordered here is how many DISTINCT query terms the subtree matched, which is
        not BM25 — a container matching four of five terms weakly is preferred to one
        matching a single term many times, and BM25 would not always agree. A container that
        would have scored highly on one rare term can therefore be missed. That is a real
        limitation, stated here rather than discovered.

        The inner ``hit`` CTE collapses postings to documents BEFORE the path expansion, and
        is the difference between 94.6 ms and 45.5 ms on that query: the expansion runs once
        per matching document instead of once per matching posting, 18,435 rows instead of
        25,420, and each one costs a ``jsonb_array_elements`` plus an ancestor join.
        """
        rows = self.session.execute(
            text(f"""
                WITH hit AS (
                    SELECT tp.document_id, count(DISTINCT tp.term) AS hits
                    FROM search_term_postings tp
                    WHERE tp.index_id = :index_id
                      AND tp.term = ANY(CAST(:terms AS text[]))
                    GROUP BY 1
                )
                SELECT (anc.value)::int AS container_id, max(h.hits) AS hits
                FROM hit h
                JOIN documents d ON d.id = h.document_id
                CROSS JOIN LATERAL jsonb_array_elements(d.path) AS anc(value)
                JOIN documents a ON a.id = (anc.value)::int
                WHERE a.content IS NULL
                  AND a.settled = 'settled'
                  {gate}
                GROUP BY 1
                ORDER BY hits DESC, container_id
                LIMIT :limit
            """),
            {"index_id": index_id, "terms": terms, "limit": limit, **gate_params},
        ).all()
        return [container_id for container_id, _ in rows]

    def _container_scores(
        self,
        index,
        terms: list,
        k1: float,
        b: float,
        limit: int,
        gate: str = "",
        gate_params: Optional[dict] = None,
    ) -> dict:
        """``{container_id: bm25_score}`` for containers, scored on their effective text.

        **No posting is written and no statistic moves.** A container's text is the
        concatenation of its frontier's, and ``_tokenize`` splits on ``[^a-z0-9]+`` while
        ``effective_text`` joins with ``"\\n\\n"`` — a separator that produces no token and
        merges none across the boundary. So the tokenization of the concatenation IS the
        concatenation of the tokenizations, which makes two identities exact:

            f(t, container) = SUM over the frontier of f(t, member)
            |container|     = SUM over the frontier of |member|

        Both are read from postings that already exist. ``doc_freq``, ``total_docs`` and
        ``avg_doc_length`` are untouched, so IDF and length normalisation stay exactly what
        the indexed leaves say they are and a container cannot skew the corpus it is part
        of. That is the difference between this and indexing containers: the same text would
        then be counted twice in every statistic derived from the index.

        **A container whose frontier holds a stored LLM summary gets no score at all.** Not
        a partial one over the rest. ``store_effective_content`` keeps summary text out of
        ``to_tsvector(title || content)`` so *"a summary cannot skew the BM25 statistics of
        the corpus it summarizes"*, and scoring the non-summary remainder would report a
        number for text the node does not stand for.
        """
        candidates = self._container_candidates(
            index.id, terms, limit, gate, dict(gate_params or {})
        )
        if not candidates:
            return {}

        frontiers = frontier_members(self.session, candidates)
        pairs = [
            (container_id, member_id)
            for container_id, (members, has_summary) in frontiers.items()
            if not has_summary
            for member_id in members
        ]
        if not pairs:
            return {}

        rows = self.session.execute(
            text("""
                WITH frontier AS (
                    SELECT unnest(CAST(:containers AS int[])) AS container_id,
                           unnest(CAST(:members AS int[]))    AS member_id
                ),
                query_terms AS (
                    SELECT unnest(CAST(:terms AS text[])) AS term
                ),
                term_idf AS (
                    SELECT qt.term,
                           LN((:total_docs - COALESCE(ts.doc_freq, 0) + 0.5) /
                              (COALESCE(ts.doc_freq, 0) + 0.5) + 1) AS idf
                    FROM query_terms qt
                    LEFT JOIN search_term_stats ts
                        ON ts.index_id = :index_id AND ts.term = qt.term
                ),
                lengths AS (
                    SELECT f.container_id, SUM(e.doc_length) AS doc_length
                    FROM frontier f
                    JOIN search_index_entries e
                      ON e.index_id = :index_id AND e.document_id = f.member_id
                    GROUP BY 1
                ),
                freqs AS (
                    SELECT f.container_id, tp.term, SUM(tp.term_freq) AS term_freq
                    FROM frontier f
                    JOIN search_term_postings tp
                      ON tp.index_id = :index_id AND tp.document_id = f.member_id
                    WHERE tp.term = ANY(CAST(:terms AS text[]))
                    GROUP BY 1, 2
                )
                SELECT q.container_id,
                       SUM(ti.idf * (q.term_freq * (:k1 + 1)) /
                           (q.term_freq + :k1 * (1 - :b + :b * l.doc_length
                                                 / NULLIF(:avg_doc_length, 1)))) AS score
                FROM freqs q
                JOIN term_idf ti ON ti.term = q.term
                JOIN lengths l ON l.container_id = q.container_id
                GROUP BY 1
            """),
            {
                "containers": [container_id for container_id, _ in pairs],
                "members": [member_id for _, member_id in pairs],
                "terms": terms,
                "index_id": index.id,
                "total_docs": index.total_docs or 1,
                "avg_doc_length": index.avg_doc_length or 1,
                "k1": k1,
                "b": b,
            },
        ).all()
        return {container_id: float(score) for container_id, score in rows if score is not None}

    # =========================================================================
    # MaxSim (Late Interaction) Search
    # =========================================================================

    def maxsim_search(
        self,
        query_text: str,
        limit: int = 10,
        embed_dim: int = 256,
        usetype: Optional[UsetypeFilter] = None,
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
            usetype: Restrict to documents matching ANY of these globs — one glob, a
                comma-separated set, or a list of them (overrides exclude_types).
                Omit it for no positive filter; an empty set raises.
            exclude_types: Exclude these document types; None uses config default
            parent_id: Filter to subtree under this document
            max_tier: Filter tokens by tier (5=top 5%, 10=top 10%, etc.)
            as_of: Point-in-time cutoff on COALESCE(event_time, created_at). Off when None.
        """
        # Normalised before the model is touched, for the same reason bm25_search does it
        # before its early returns: a malformed filter must not be reported as "the query
        # produced no tokens", and must not cost an embedding call to find out about.
        globs = usetype_globs(usetype)

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

        # Use the pgvector HNSW index on embed_256 (migration 022) for ANN search per query
        # token. For each query token, find top-K nearest document tokens, then aggregate by
        # document.
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
        usetype_params: dict = {}
        if globs is not None:
            fragment, usetype_params = _usetype_sql(globs, alias="d")
            filter_conditions.append(fragment)
        else:
            effective = effective_exclude_types(usetype, exclude_types)
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

        # THE SAME SCAN MODE `vector_search` SETS, AND MIGRATION 022 IS WHY IT IS HERE NOW.
        # Until that migration `embed_256` was IVFFlat and this call site set nothing; the
        # comment that stood here declined `ivfflat.iterative_scan` on the ground that it
        # would move MaxSim's ranking outside a step that measured it. Two things changed
        # and neither is a change of mind about that.
        #
        # First, the index. `ANN_INDEX_HEALTH.md` Parts 1-2 are about dead entries in an
        # HNSW graph, and until 022 they did not reach this column. They do now, harder than
        # they reach `documents.embed`: that index is PARTIAL on `settled` so a rewritten
        # chunk never enters the graph, and `token_embeddings` has no such column.
        # `repositories/document.py:1069` deletes every token row for a document and
        # rewrites them on each re-embed — 113,313 dead against 1,370,494 live on the
        # reference corpus (`STRESS_CORPUS.md` 6.3). 1.7 measures what that does at
        # pgvector's default scan mode: recall@10 of 0.100.
        #
        # Second, the measurement. `STRESS_CORPUS.md` 6.2 ran `strict_order` against this
        # exact column at 1.37M real token rows and read 0.9667 at 0.59 ms, against 0.9667
        # at 0.65 ms without it — same recall, slightly faster, on an index whose dead
        # tuples autovacuum had already been amortising. So it is not outside a step that
        # measured it, and `strict_order` cannot reorder a page by construction, which is
        # what the earlier objection was really about.
        #
        # Block A step 3 still applies and is not made redundant by this: a token whose
        # neighbour list comes back short of `k_per_token` is still reported through
        # `AppliedFilters.truncated` rather than absorbed into a lower MaxSim score.
        if _hnsw_iterative_scan_available(self.session):
            self.session.execute(text(f"SET LOCAL hnsw.iterative_scan = {HNSW_ITERATIVE_SCAN}"))

        # Collect (document_id -> max_similarity) for each query token
        # Then sum across query tokens for final MaxSim score
        doc_token_max_sims: dict[int, list[float]] = defaultdict(list)

        for q_embed in query_embeddings:
            # Convert embedding to pgvector format
            embed_str = "[" + ",".join(str(x) for x in q_embed) + "]"

            # ANN query using the HNSW index - finds K nearest tokens
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

            ann_params: dict = {"query_vec": embed_str, "k": k_per_token, **usetype_params}
            if excl_types_param is not None:
                ann_params["excl_types"] = excl_types_param
            if as_of is not None:
                ann_params["as_of"] = as_of
            results = self.session.execute(ann_query, ann_params).fetchall()

            # Block A step 3 applies here too, and this is the path that has ALWAYS reached
            # its index: `ORDER BY te.embed_256 <=> ...` is the bare operator, and the RBAC
            # fragment is ANDed into the same statement. `scripts/maxsim_recall.py`
            # measured what that costs on the IVFFlat this column carried until migration
            # 022 — at the appliance's own `ivfflat.probes = 1`, a readable share at or
            # below 5% loses most of its neighbours. The scan mode set above is what
            # addresses that, and it is a mitigation rather than immunity (1.7: at the 500
            # dead-entry dose, 3 of 100 queries were still degraded and one returned
            # nothing). A token whose neighbour list still comes back short of
            # `k_per_token` is that residue, so it is reported rather than absorbed into a
            # lower MaxSim score.
            self._note_ann_page(len(results), k_per_token)

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
        usetype: Optional[UsetypeFilter] = None,
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
            usetype: Restrict to documents matching ANY of these globs — one glob, a
                comma-separated set, or a list of them (overrides exclude_types).
                Omit it for no positive filter; an empty set raises.
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
            raise ValueError(f"recency_halflife_days must be positive, got {recency_halflife_days}")
        # Resolve and CHECK, in that order. The fusion below is four `if "name" in methods`
        # tests, so before this line an unrecognised name — `"maxsim "` with a trailing
        # space, or `"hybrid"` — contributed no results and raised nothing: the caller got a
        # narrower fusion than they asked for, labelled as the one they asked for. The
        # contract validator catches it at the REST edge; this catches an in-process caller,
        # who does not go through a Pydantic model at all.
        methods = list(methods) if methods else list(DEFAULT_HYBRID_METHODS)
        validate_search_methods(methods)
        # See `effective_weights` for what None and {} each mean.
        weights = effective_weights(weights)

        # RRF constant
        k = 60
        candidate_limit = limit * 3  # Get more candidates for fusion

        # Collect results from each method
        method_results: dict[str, list[SearchResult]] = {}

        if "vector" in methods:
            method_results["vector"] = self.vector_search_text(
                query_text,
                limit=candidate_limit,
                usetype=usetype,
                exclude_types=exclude_types,
                parent_id=parent_id,
                as_of=as_of,
            )

        if "fulltext" in methods:
            method_results["fulltext"] = self.fulltext_search(
                query_text,
                limit=candidate_limit,
                usetype=usetype,
                exclude_types=exclude_types,
                parent_id=parent_id,
                as_of=as_of,
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
                query_text,
                limit=candidate_limit,
                usetype=usetype,
                exclude_types=exclude_types,
                as_of=as_of,
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
        # Idempotency (docs/archive/KNOWN-DEFECTS.md, D3).
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
        dropped_terms = old_terms - new_terms
        for term in dropped_terms:
            self.session.execute(
                text("""
                    UPDATE search_term_stats SET doc_freq = doc_freq - 1
                    WHERE index_id = :index_id AND term = :term
                """),
                {"index_id": index.id, "term": term},
            )
        # ONLY WHEN SOMETHING WAS DECREMENTED, and only over the terms decremented.
        #
        # `search_term_stats` is keyed `(index_id, term)` and carries no index on
        # `doc_freq`, so `WHERE index_id = :id AND doc_freq <= 0` reads every term row for
        # the index. Unconditionally, once per document per covering index, that is a scan
        # whose cost grows with the corpus and whose result is almost always nothing: the
        # only way a row reaches zero is the loop directly above, so with `dropped_terms`
        # empty — which is EVERY document of a first-time ingest, where `old_terms` is
        # empty — it cannot delete anything by construction.
        #
        # Measured before this guard (`docs/STRESS_CORPUS.md` 4.6): 30,812 executions,
        # 249 s, `EXPLAIN` reporting `Rows Removed by Filter: 16295` against `rows=0`.
        #
        # Naming the terms as well as guarding the call keeps it an index lookup instead of
        # a scan in the case where it does have work to do.
        if dropped_terms:
            self.session.execute(
                text(
                    "DELETE FROM search_term_stats "
                    "WHERE index_id = :index_id AND doc_freq <= 0 AND term = ANY(:terms)"
                ),
                {"index_id": index.id, "terms": list(dropped_terms)},
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
        # per-term round-trips are round-trips whatever they cost each.
        # (This comment used to add "the SELECT/DELETE scan a table with no
        # (index_id, document_id) index". That stopped being true: migration
        # 007 added idx_term_postings_doc on search_term_postings
        # (index_id, document_id) and schema.sql:526 creates it on a fresh
        # install, so the scan is an index scan now and the rebuild is no
        # longer quadratic in the postings already written. Migration 007's
        # own header says as much — it speeds up the incremental write path
        # and makes a rebuild marginally SLOWER, one more btree to maintain.
        # The set-based path below is still the right one, for the round
        # trips rather than for the scan.) Instead tokenize with the SAME
        # _tokenize and batch-insert entries -> postings -> stats.
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
