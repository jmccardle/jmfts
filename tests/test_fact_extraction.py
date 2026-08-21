"""Tests for Fact Extraction Pipeline (#58).

Tier 1: Unit tests — no DB, no LLM. Tests parsing, entity resolution logic,
predicate normalization, temporal parsing, and prompt structure.

See test_methodology_raptor_pipeline.md § 2.2 for the full test plan.
"""

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch


from jmfts_core.fact_extraction import (
    _llm_extract,
    _parse_fact_type,
    _parse_raw_triples,
    _parse_temporal,
    _string_similarity,
    extract_facts_from_document,
    resolve_entity,
    resolve_predicate,
    EXTRACTION_SYSTEM_PROMPT,
)
from jmfts_core.models.triple import FactType

# ============================================================================
# Helpers
# ============================================================================


def _run(coro):
    """Run async coroutine synchronously."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_settings(**overrides):
    """A real Settings with extraction defaults, pointed at an endpoint nothing calls.

    Previously a `MagicMock(spec=Settings)` with the read-only `effective_llm_*` properties
    stubbed out. That bypassed the resolution chain being tested: `require_llm` is a method,
    so a mock returned another mock instead of the (url, model) pair, and the stubs hid the
    fact that the endpoint has to be configured at all. The HTTP client is patched in each
    test, so this address is never contacted.
    """
    from jmfts_core.config import Settings

    defaults = {
        "llm_base_url": "http://llm.invalid:8853",
        "llm_model": "a-model-that-is-never-called",
        "llm_timeout": 10.0,
        "extraction_max_facts": 5,
        "extraction_confidence_threshold": 0.5,
        "extraction_entity_similarity_threshold": 0.8,
        "extraction_temperature": 0.1,
        "extraction_max_tokens": 2048,
    }
    defaults.update(overrides)
    return Settings(**defaults)


# ============================================================================
# Tier 1: Unit Tests — No DB, No LLM
# ============================================================================


class TestParseRawTriples:
    """Test _parse_raw_triples validation and filtering."""

    def test_valid_triple(self):
        raw = [
            {
                "subject": "Paris",
                "predicate": "is_capital_of",
                "object": "France",
                "confidence": 0.95,
                "fact_type": "atemporal",
            }
        ]
        result = _parse_raw_triples(raw, max_facts=5)
        assert len(result) == 1
        assert result[0].subject == "Paris"
        assert result[0].predicate == "is_capital_of"
        assert result[0].object == "France"
        assert result[0].confidence == 0.95

    def test_multiple_triples(self):
        raw = [
            {"subject": "Paris", "predicate": "capital_of", "object": "France"},
            {"subject": "France", "predicate": "located_in", "object": "Europe"},
        ]
        result = _parse_raw_triples(raw, max_facts=5)
        assert len(result) == 2

    def test_max_facts_limit(self):
        raw = [{"subject": f"E{i}", "predicate": "rel", "object": f"O{i}"} for i in range(10)]
        result = _parse_raw_triples(raw, max_facts=3)
        assert len(result) == 3

    def test_empty_subject_skipped(self):
        raw = [{"subject": "", "predicate": "rel", "object": "Obj"}]
        result = _parse_raw_triples(raw, max_facts=5)
        assert len(result) == 0

    def test_missing_predicate_skipped(self):
        raw = [{"subject": "Sub", "object": "Obj"}]
        result = _parse_raw_triples(raw, max_facts=5)
        assert len(result) == 0

    def test_non_dict_elements_skipped(self):
        raw = ["not a dict", 42, None]
        result = _parse_raw_triples(raw, max_facts=5)
        assert len(result) == 0

    def test_default_confidence(self):
        raw = [{"subject": "A", "predicate": "rel", "object": "B"}]
        result = _parse_raw_triples(raw, max_facts=5)
        assert result[0].confidence == 1.0

    def test_temporal_fields_preserved(self):
        raw = [
            {
                "subject": "A",
                "predicate": "rel",
                "object": "B",
                "valid_from": "2026-03-01T00:00:00Z",
                "valid_until": "2026-12-31T00:00:00Z",
            }
        ]
        result = _parse_raw_triples(raw, max_facts=5)
        assert result[0].valid_from == "2026-03-01T00:00:00Z"
        assert result[0].valid_until == "2026-12-31T00:00:00Z"

    def test_empty_list(self):
        result = _parse_raw_triples([], max_facts=5)
        assert result == []


class TestTemporalParsing:
    """Test _parse_temporal for ISO 8601 dates."""

    def test_iso_with_z(self):
        dt = _parse_temporal("2026-03-01T00:00:00Z")
        assert dt == datetime(2026, 3, 1, tzinfo=timezone.utc)

    def test_iso_with_offset(self):
        dt = _parse_temporal("2026-03-01T00:00:00+00:00")
        assert dt is not None
        assert dt.tzinfo is not None

    def test_iso_date_only(self):
        dt = _parse_temporal("2026-03-01")
        assert dt is not None
        assert dt.year == 2026

    def test_none_input(self):
        assert _parse_temporal(None) is None

    def test_empty_string(self):
        assert _parse_temporal("") is None

    def test_garbage_input(self):
        assert _parse_temporal("not-a-date") is None


class TestFactTypeParsing:
    """Test _parse_fact_type for enum conversion."""

    def test_atemporal(self):
        assert _parse_fact_type("atemporal") == FactType.atemporal

    def test_static(self):
        assert _parse_fact_type("static") == FactType.static

    def test_dynamic(self):
        assert _parse_fact_type("dynamic") == FactType.dynamic

    def test_case_insensitive(self):
        assert _parse_fact_type("STATIC") == FactType.static

    def test_unknown_defaults_to_atemporal(self):
        assert _parse_fact_type("unknown") == FactType.atemporal


class TestStringSimilarity:
    """Test _string_similarity helper."""

    def test_exact_match(self):
        assert _string_similarity("Paris", "Paris") == 1.0

    def test_case_insensitive(self):
        assert _string_similarity("paris", "Paris") == 1.0

    def test_similar_strings(self):
        score = _string_similarity("John Smith", "John Smithe")
        assert score > 0.8

    def test_dissimilar_strings(self):
        score = _string_similarity("Paris", "Tokyo")
        assert score < 0.5

    def test_empty_strings(self):
        assert _string_similarity("", "") == 1.0


class TestExtractionPrompt:
    """Test that the extraction prompt has the right structure."""

    def test_prompt_asks_for_json(self):
        assert "JSON" in EXTRACTION_SYSTEM_PROMPT

    def test_prompt_mentions_fact_types(self):
        assert "atemporal" in EXTRACTION_SYSTEM_PROMPT
        assert "static" in EXTRACTION_SYSTEM_PROMPT
        assert "dynamic" in EXTRACTION_SYSTEM_PROMPT

    def test_prompt_mentions_confidence(self):
        assert "confidence" in EXTRACTION_SYSTEM_PROMPT

    def test_prompt_mentions_temporal(self):
        assert "valid_from" in EXTRACTION_SYSTEM_PROMPT
        assert "valid_until" in EXTRACTION_SYSTEM_PROMPT


class TestLLMExtractParsing:
    """Test _llm_extract response parsing (mocked HTTP)."""

    def test_valid_json_response(self):
        triples = [{"subject": "A", "predicate": "rel", "object": "B", "confidence": 0.9}]
        mock_response = {"choices": [{"message": {"content": json.dumps(triples)}}]}

        async def mock_post(*args, **kwargs):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = mock_response
            return resp

        settings = _make_settings()
        with patch("jmfts_core.fact_extraction.httpx.AsyncClient") as mock_client:
            instance = MagicMock()
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            instance.post = AsyncMock(side_effect=mock_post)
            mock_client.return_value = instance

            result = _run(_llm_extract("some text", settings))
            assert len(result) == 1
            assert result[0]["subject"] == "A"

    def test_markdown_fenced_json(self):
        triples = [{"subject": "A", "predicate": "rel", "object": "B"}]
        content = f"```json\n{json.dumps(triples)}\n```"
        mock_response = {"choices": [{"message": {"content": content}}]}

        async def mock_post(*args, **kwargs):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = mock_response
            return resp

        settings = _make_settings()
        with patch("jmfts_core.fact_extraction.httpx.AsyncClient") as mock_client:
            instance = MagicMock()
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            instance.post = AsyncMock(side_effect=mock_post)
            mock_client.return_value = instance

            result = _run(_llm_extract("some text", settings))
            assert len(result) == 1

    def test_invalid_json_returns_empty(self):
        mock_response = {"choices": [{"message": {"content": "not json at all"}}]}

        async def mock_post(*args, **kwargs):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = mock_response
            return resp

        settings = _make_settings()
        with patch("jmfts_core.fact_extraction.httpx.AsyncClient") as mock_client:
            instance = MagicMock()
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            instance.post = AsyncMock(side_effect=mock_post)
            mock_client.return_value = instance

            result = _run(_llm_extract("some text", settings))
            assert result == []

    def test_empty_array_response(self):
        mock_response = {"choices": [{"message": {"content": "[]"}}]}

        async def mock_post(*args, **kwargs):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = mock_response
            return resp

        settings = _make_settings()
        with patch("jmfts_core.fact_extraction.httpx.AsyncClient") as mock_client:
            instance = MagicMock()
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            instance.post = AsyncMock(side_effect=mock_post)
            mock_client.return_value = instance

            result = _run(_llm_extract("The weather was nice.", settings))
            assert result == []


class TestResolveEntity:
    """Test entity resolution with mocked DB session."""

    def test_cache_hit(self):
        cache = {"paris": 42}
        session = MagicMock()
        doc_id, created = resolve_entity("Paris", session, threshold=0.8, _cache=cache)
        assert doc_id == 42
        assert created is False

    def test_exact_match_in_db(self):
        mock_doc = MagicMock()
        mock_doc.id = 99
        mock_doc.title = "Paris"

        mock_repo = MagicMock()
        mock_repo.find.return_value = [mock_doc]

        session = MagicMock()
        cache = {}

        with patch("jmfts_core.fact_extraction.DocumentRepository", return_value=mock_repo):
            doc_id, created = resolve_entity("Paris", session, threshold=0.8, _cache=cache)
            assert doc_id == 99
            assert created is False
            assert cache["paris"] == 99

    def test_creates_new_entity_when_no_match(self):
        new_doc = MagicMock()
        new_doc.id = 200

        mock_repo = MagicMock()
        mock_repo.find.return_value = []
        mock_repo.create.return_value = new_doc

        session = MagicMock()
        # Mock the select().where().limit() chain to return empty
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        session.execute.return_value = mock_result

        cache = {}

        with patch("jmfts_core.fact_extraction.DocumentRepository", return_value=mock_repo):
            doc_id, created = resolve_entity("NewEntity", session, threshold=0.8, _cache=cache)
            assert doc_id == 200
            assert created is True
            mock_repo.create.assert_called_once()


class TestResolvePredicate:
    """Test predicate resolution with mocked DB session."""

    def test_cache_hit(self):
        cache = {"authored": 10}
        session = MagicMock()
        pred_id, created = resolve_predicate("authored", session, _cache=cache)
        assert pred_id == 10
        assert created is False

    def test_normalizes_to_lowercase_snake_case(self):
        cache = {}
        mock_pred = MagicMock()
        mock_pred.id = 15

        # _resolve_predicate now mints via the atomic get_or_create_predicate.
        mock_repo = MagicMock()
        mock_repo.get_or_create_predicate.return_value = (mock_pred, True)

        session = MagicMock()

        with patch("jmfts_core.fact_extraction.TripleRepository", return_value=mock_repo):
            pred_id, created = resolve_predicate("Works At", session, _cache=cache)
            assert pred_id == 15
            assert created is True
            mock_repo.get_or_create_predicate.assert_called_with(name="works_at")
            assert "works_at" in cache

    def test_reuses_existing_predicate(self):
        cache = {}
        mock_pred = MagicMock()
        mock_pred.id = 20

        mock_repo = MagicMock()
        # Already exists → atomic resolve returns created=False.
        mock_repo.get_or_create_predicate.return_value = (mock_pred, False)

        session = MagicMock()

        with patch("jmfts_core.fact_extraction.TripleRepository", return_value=mock_repo):
            pred_id, created = resolve_predicate("authored", session, _cache=cache)
            assert pred_id == 20
            assert created is False


class TestExtractFactsFromDocument:
    """Test single-document extraction with mocked LLM."""

    def test_filters_low_confidence(self):
        """Triples below confidence threshold are skipped."""
        llm_output = [
            {"subject": "A", "predicate": "rel", "object": "B", "confidence": 0.9},
            {"subject": "C", "predicate": "rel", "object": "D", "confidence": 0.1},
        ]

        mock_doc = MagicMock()
        mock_doc.content = "Some text about A and B."

        session = MagicMock()
        session.get.return_value = mock_doc

        settings = _make_settings(extraction_confidence_threshold=0.5)

        with (
            patch("jmfts_core.fact_extraction._llm_extract", new_callable=AsyncMock) as mock_llm,
            patch("jmfts_core.fact_extraction.resolve_entity") as mock_entity,
            patch("jmfts_core.fact_extraction.resolve_predicate") as mock_pred,
            patch("jmfts_core.fact_extraction.TripleRepository") as mock_triple_repo_cls,
        ):
            mock_llm.return_value = llm_output
            mock_entity.return_value = (1, False)
            mock_pred.return_value = (1, False)

            mock_triple = MagicMock()
            mock_triple.id = 100
            mock_triple_repo_cls.return_value.upsert_triple.return_value = (mock_triple, True)

            result = _run(
                extract_facts_from_document(document_id=1, session=session, settings=settings)
            )

            assert len(result.created_triple_ids) == 1
            assert result.skipped_count == 1

    def test_no_content_returns_error(self):
        """Document with no content returns error."""
        mock_doc = MagicMock()
        mock_doc.content = None

        session = MagicMock()
        session.get.return_value = mock_doc

        settings = _make_settings()
        result = _run(
            extract_facts_from_document(document_id=1, session=session, settings=settings)
        )

        assert len(result.errors) == 1
        assert "no content" in result.errors[0].lower()

    def test_empty_llm_output(self):
        """LLM returning no triples produces zero results."""
        mock_doc = MagicMock()
        mock_doc.content = "The weather was nice today."

        session = MagicMock()
        session.get.return_value = mock_doc

        settings = _make_settings()

        with patch("jmfts_core.fact_extraction._llm_extract", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = []
            result = _run(
                extract_facts_from_document(document_id=1, session=session, settings=settings)
            )

            assert len(result.created_triple_ids) == 0
            assert result.skipped_count == 0
