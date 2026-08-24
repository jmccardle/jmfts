"""Tests for Fact Extraction Pipeline (#58).

Tier 1: Unit tests — no DB, no LLM. Tests parsing, entity resolution logic,
predicate normalization, temporal parsing, and prompt structure.

See test_methodology_raptor_pipeline.md § 2.2 for the full test plan.
"""

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


from jmfts_core.fact_extraction import (
    _best_match,
    _like_escape,
    _llm_extract,
    _parse_fact_type,
    _parse_raw_triples,
    _parse_temporal,
    _string_similarity,
    extract_facts_from_document,
    resolve_predicate,
    EXTRACTION_SYSTEM_PROMPT,
)
from jmfts_core.models.triple import FactType
from tests.llm_stub import llm_stub

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


#: The extraction knobs these tests pin, kept apart from the endpoint so the same set can
#: be layered onto a `Settings` pointed at a live stub.
EXTRACTION_DEFAULTS = {
    "extraction_max_facts": 5,
    "extraction_confidence_threshold": 0.5,
    "extraction_entity_similarity_threshold": 0.8,
    "extraction_temperature": 0.1,
    "extraction_max_tokens": 2048,
}


def _make_settings(**overrides):
    """A real Settings with extraction defaults, pointed at an endpoint nothing calls.

    Previously a `MagicMock(spec=Settings)` with the read-only `effective_llm_*` properties
    stubbed out. That bypassed the resolution chain being tested: `require_llm` is a method,
    so a mock returned another mock instead of the (url, model) pair, and the stubs hid the
    fact that the endpoint has to be configured at all.

    The tests that actually make a call pass `llm_base_url` through
    `llm_stub(...).settings(**EXTRACTION_DEFAULTS)` instead; this address belongs to the
    ones that never reach the transport.
    """
    from jmfts_core.config import Settings

    defaults = {
        "llm_base_url": "http://llm.invalid:8853",
        "llm_model": "a-model-that-is-never-called",
        "llm_timeout": 10.0,
        **EXTRACTION_DEFAULTS,
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
    """Test _llm_extract against a live stub endpoint.

    These used to patch `jmfts_core.fact_extraction.httpx.AsyncClient`. The transport
    moved to `tau_llm` in 0.3.0 and builds its own client, so there is no client in this
    module left to patch; `tests/llm_stub.py` serves the same canned bodies over loopback
    and each test asserts the same thing it did before, plus what reached the wire.
    """

    def _extract(self, stub, text="some text"):
        return _run(_llm_extract(text, stub.settings(**EXTRACTION_DEFAULTS)))

    def test_valid_json_response(self):
        triples = [{"subject": "A", "predicate": "rel", "object": "B", "confidence": 0.9}]
        with llm_stub(content=json.dumps(triples)) as stub:
            result = self._extract(stub)

        assert len(result) == 1
        assert result[0]["subject"] == "A"
        request = stub.requests[0]
        assert request.path == "/v1/chat/completions"
        assert request.body["temperature"] == EXTRACTION_DEFAULTS["extraction_temperature"]
        assert request.body["max_tokens"] == EXTRACTION_DEFAULTS["extraction_max_tokens"]
        assert "some text" in request.role("user")

    def test_markdown_fenced_json(self):
        triples = [{"subject": "A", "predicate": "rel", "object": "B"}]
        with llm_stub(content=f"```json\n{json.dumps(triples)}\n```") as stub:
            result = self._extract(stub)
        assert len(result) == 1

    def test_invalid_json_returns_empty(self):
        with llm_stub(content="not json at all") as stub:
            result = self._extract(stub)
        assert result == []

    def test_empty_array_response(self):
        with llm_stub(content="[]") as stub:
            result = self._extract(stub, text="The weather was nice.")
        assert result == []

    def test_reasoning_only_response_is_read(self):
        """A model that spent its budget thinking still answers.

        `llm_utils.extract_llm_text` fell back from an empty `content` to
        `reasoning_content`; `llm_client._text_of` restates that rule over τ's typed
        blocks. This is the test that keeps the two equivalent — without it the fallback
        is a comment nobody checks, and a reasoning model would silently extract nothing.
        """
        triples = [{"subject": "A", "predicate": "rel", "object": "B"}]
        with llm_stub(content="", reasoning=json.dumps(triples)) as stub:
            result = self._extract(stub)
        assert len(result) == 1
        assert result[0]["object"] == "B"


class TestEntityMatching:
    """The parts of entity resolution that are a decision rather than a query.

    `resolve_entity` itself no longer has a tier-1 shape: since `SPRINT_0_3_0.md` 7.5 it
    resolves against the entities root for the source document's ACCESS, so it reads
    grants, mints a root and writes links — a mocked session can only assert that it called
    the mock. Its behaviour is tested against a real database in
    `tests/test_entity_access_keys.py`. What stays here is the matching logic, which is
    pure.
    """

    def test_best_match_needs_the_threshold(self):
        paris = SimpleNamespace(id=99, title="Paris", parent_id=1)
        assert _best_match("Paris", [paris], 0.8) is paris
        assert _best_match("Berlin", [paris], 0.8) is None

    def test_best_match_prefers_the_closer_title(self):
        near = SimpleNamespace(id=1, title="Paris, France", parent_id=1)
        exact = SimpleNamespace(id=2, title="Paris", parent_id=1)
        assert _best_match("Paris", [near, exact], 0.8) is exact

    def test_best_match_ignores_untitled_candidates(self):
        assert _best_match("Paris", [SimpleNamespace(id=1, title=None, parent_id=1)], 0.0) is None

    def test_like_metacharacters_are_escaped(self):
        # An entity really named "50%" must not turn the candidate sieve into a wildcard.
        assert _like_escape("50%") == "50\\%"
        assert _like_escape("a_b") == "a\\_b"
        assert _like_escape("back\\slash") == "back\\\\slash"


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
