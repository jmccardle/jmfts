"""Tests for the query router heuristics."""

from jmfts_core.query_router import route_query


class TestQuotedPhrases:
    def test_single_quoted_phrase(self):
        d = route_query('"machine learning"')
        assert d.method == "fulltext"
        assert d.signals["quoted_phrases"] == ["machine learning"]

    def test_quoted_phrase_with_context(self):
        d = route_query('how does "gradient descent" work')
        assert d.method == "fulltext"


class TestBooleanOperators:
    def test_and_operator(self):
        d = route_query("python AND asyncio")
        assert d.method == "fulltext"
        assert "AND" in d.signals["boolean_operators"]

    def test_or_operator(self):
        d = route_query("fastapi OR flask")
        assert d.method == "fulltext"

    def test_not_operator(self):
        d = route_query("search NOT deprecated")
        assert d.method == "fulltext"

    def test_lowercase_and_is_not_boolean(self):
        d = route_query("bread and butter")
        assert d.method != "fulltext" or d.signals["boolean_operators"] == []


class TestShortKeyword:
    def test_single_keyword(self):
        d = route_query("pgvector")
        assert d.method == "bm25"

    def test_two_keywords(self):
        d = route_query("cosine similarity")
        assert d.method == "bm25"

    def test_keyword_with_stopword(self):
        # "the" is a stopword → high stopword ratio pushes to hybrid, not bm25
        d = route_query("the pgvector")
        assert d.method == "hybrid"

    def test_pure_keywords(self):
        d = route_query("pgvector hnsw")
        assert d.method == "bm25"


class TestNaturalLanguageQuestion:
    def test_long_question(self):
        d = route_query("how does the hybrid search algorithm combine vector and bm25 scores?")
        assert d.method == "vector"
        assert d.signals["is_question"] is True

    def test_question_mark(self):
        d = route_query("what embedding model is used for document indexing?")
        assert d.method == "vector"

    def test_explain_prefix(self):
        d = route_query("explain how token selection works in detail")
        assert d.method == "vector"
        assert d.signals["is_question"] is True


class TestHybridDefault:
    def test_medium_query(self):
        d = route_query("matryoshka embeddings late interaction")
        assert d.method == "hybrid"

    def test_medium_with_some_stopwords(self):
        d = route_query("search for documents about retrieval augmented generation")
        assert d.method in ("hybrid", "vector")  # could go either way at boundary


class TestEdgeCases:
    def test_empty_query(self):
        d = route_query("")
        assert d.method == "hybrid"

    def test_whitespace_only(self):
        d = route_query("   ")
        assert d.method == "hybrid"

    def test_signals_always_present(self):
        d = route_query("test query")
        assert "token_count" in d.signals
        assert "content_token_count" in d.signals
        assert "stopword_ratio" in d.signals
        assert "is_question" in d.signals
        assert "quoted_phrases" in d.signals
        assert "boolean_operators" in d.signals
