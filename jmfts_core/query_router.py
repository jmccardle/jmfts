"""Query Router — heuristic-based search method selection.

Analyzes a query string and selects the best search method based on
lexical and structural signals.
"""

import re
from dataclasses import dataclass

# Common English stopwords (subset for ratio calculation)
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "shall",
        "can",
        "need",
        "dare",
        "ought",
        "used",
        "to",
        "of",
        "in",
        "for",
        "on",
        "with",
        "at",
        "by",
        "from",
        "as",
        "into",
        "through",
        "during",
        "before",
        "after",
        "above",
        "below",
        "between",
        "out",
        "off",
        "over",
        "under",
        "again",
        "further",
        "then",
        "once",
        "and",
        "but",
        "or",
        "nor",
        "not",
        "so",
        "yet",
        "both",
        "each",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "no",
        "only",
        "own",
        "same",
        "than",
        "too",
        "very",
        "just",
        "because",
        "if",
        "when",
        "where",
        "how",
        "what",
        "which",
        "who",
        "whom",
        "this",
        "that",
        "these",
        "those",
        "i",
        "me",
        "my",
        "we",
        "our",
        "you",
        "your",
        "he",
        "him",
        "his",
        "she",
        "her",
        "it",
        "its",
        "they",
        "them",
        "their",
        "all",
        "any",
        "every",
    }
)


@dataclass
class RoutingDecision:
    """Result of query analysis — which method to use and why."""

    method: str
    reason: str
    signals: dict


def route_query(query: str) -> RoutingDecision:
    """Analyze a query and pick the best search method.

    Heuristics (evaluated in priority order):

    1. **Quoted phrases / exact match** → fulltext
       Quotes signal the user wants literal matching, which PostgreSQL
       ts_vector phrase search handles well.

    2. **Boolean operators** (AND, OR, NOT in caps) → fulltext
       These map directly to PostgreSQL tsquery operators.

    3. **Very short keyword queries** (1-2 non-stop tokens) → bm25
       Short keyword lookups benefit from precise term-frequency scoring.

    4. **Long natural-language questions** (≥8 tokens, high stopword ratio) → vector
       Semantic search captures intent better than keyword matching for
       questions like "how does the authentication system work?"

    5. **Medium-length queries with mixed content** → hybrid
       Default when no strong signal pushes toward a single method.
    """
    stripped = query.strip()
    if not stripped:
        return RoutingDecision(
            method="hybrid", reason="empty query, falling back to hybrid", signals={}
        )

    signals: dict = {}

    # --- Signal extraction ---

    # Quoted phrases
    quoted_phrases = re.findall(r'"([^"]+)"', stripped)
    has_quotes = len(quoted_phrases) > 0
    signals["quoted_phrases"] = quoted_phrases

    # Boolean operators (must be uppercase to avoid matching normal words)
    bool_ops = re.findall(r"\b(AND|OR|NOT)\b", stripped)
    has_bool = len(bool_ops) > 0
    signals["boolean_operators"] = bool_ops

    # Tokenize (lowercase, split on non-alphanum)
    tokens = [t for t in re.split(r"[^a-z0-9]+", stripped.lower()) if len(t) >= 2]
    signals["token_count"] = len(tokens)

    # Content tokens (non-stopword)
    content_tokens = [t for t in tokens if t not in _STOPWORDS]
    signals["content_token_count"] = len(content_tokens)

    # Stopword ratio
    stopword_ratio = 1 - (len(content_tokens) / len(tokens)) if tokens else 0
    signals["stopword_ratio"] = round(stopword_ratio, 2)

    # Question pattern
    question_words = {"how", "what", "why", "when", "where", "who", "which", "explain", "describe"}
    is_question = stripped.rstrip().endswith("?") or (
        len(tokens) > 0 and tokens[0] in question_words
    )
    signals["is_question"] = is_question

    # --- Decision logic ---

    # 1. Quoted phrases → fulltext
    if has_quotes:
        return RoutingDecision(
            method="fulltext",
            reason="query contains quoted phrase(s) — using full-text phrase matching",
            signals=signals,
        )

    # 2. Boolean operators → fulltext
    if has_bool:
        return RoutingDecision(
            method="fulltext",
            reason="query contains boolean operators — using full-text search",
            signals=signals,
        )

    # 3. Very short keyword queries → bm25
    if len(content_tokens) <= 2 and stopword_ratio < 0.4:
        return RoutingDecision(
            method="bm25",
            reason="short keyword query — BM25 term-frequency scoring is most precise",
            signals=signals,
        )

    # 4. Long natural-language / question → vector
    if len(tokens) >= 8 or (is_question and len(tokens) >= 4):
        return RoutingDecision(
            method="vector",
            reason="natural-language question — semantic vector search captures intent best",
            signals=signals,
        )

    # 5. Default → hybrid
    return RoutingDecision(
        method="hybrid",
        reason="medium-length mixed query — hybrid fusion covers both lexical and semantic",
        signals=signals,
    )
