"""Tests for bulk ingestion error handling (#340).

Reproduces two bugs from adjutant startup:
1. IntegrityError on duplicate triples during fact extraction
2. ValueError on missing parent_id during conversation ingest

Tier 1: Unit tests with mocked DB session.
"""

import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

from jmfts_core.fact_extraction import extract_facts_from_document


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_settings(**overrides):
    from jmfts_core.config import Settings

    defaults = {
        "ensonet_url": "http://localhost:8853",
        "ensonet_model": "THUDM_GLM4_32b",
        "ensonet_timeout": 10.0,
        "effective_llm_url": "http://localhost:8853",
        "effective_llm_model": "THUDM_GLM4_32b",
        "effective_llm_timeout": 10.0,
        "extraction_max_facts": 5,
        "extraction_confidence_threshold": 0.5,
        "extraction_entity_similarity_threshold": 0.8,
        "extraction_temperature": 0.1,
        "extraction_max_tokens": 2048,
    }
    defaults.update(overrides)
    mock = MagicMock(spec=Settings)
    for k, v in defaults.items():
        setattr(mock, k, v)
    return mock


# ============================================================================
# Bug 1: Duplicate triple should not raise IntegrityError
# ============================================================================


class TestDuplicateTripleHandling:
    """Verify that duplicate triples are skipped cleanly via upsert,
    not via savepoint + IntegrityError catch."""

    def test_upsert_triple_returns_existing_on_duplicate(self):
        """upsert_triple returns (existing, False) when triple already exists."""
        from jmfts_core.repositories.triple import TripleRepository

        existing_triple = MagicMock()
        existing_triple.id = 42

        session = MagicMock()
        # Simulate ON CONFLICT DO NOTHING: rowcount=0 means conflict fired
        execute_result = MagicMock()
        execute_result.rowcount = 0

        # First execute call: the INSERT ... ON CONFLICT DO NOTHING
        # Second execute call: the SELECT to fetch existing
        select_result = MagicMock()
        select_result.scalar_one.return_value = existing_triple

        session.execute.side_effect = [execute_result, select_result]

        repo = TripleRepository(session)
        triple, created = repo.upsert_triple(
            subject_id=1,
            predicate_id=2,
            object_id=3,
        )

        assert triple.id == 42
        assert created is False

    def test_upsert_triple_creates_when_new(self):
        """upsert_triple returns (new_triple, True) when triple is new."""
        from jmfts_core.repositories.triple import TripleRepository

        new_triple = MagicMock()
        new_triple.id = 99

        session = MagicMock()
        # rowcount=1 means insert succeeded
        execute_result = MagicMock()
        execute_result.rowcount = 1

        select_result = MagicMock()
        select_result.scalar_one.return_value = new_triple

        session.execute.side_effect = [execute_result, select_result]

        repo = TripleRepository(session)
        triple, created = repo.upsert_triple(
            subject_id=10,
            predicate_id=20,
            object_id=30,
        )

        assert triple.id == 99
        assert created is True

    def test_extract_facts_counts_duplicates_as_skipped(self):
        """When upsert_triple returns created=False, the extraction
        increments skipped_count instead of raising IntegrityError."""
        llm_output = [
            {"subject": "A", "predicate": "rel", "object": "B", "confidence": 0.9},
            {"subject": "A", "predicate": "rel", "object": "B", "confidence": 0.9},
        ]

        mock_doc = MagicMock()
        mock_doc.content = "Text about A and B."

        session = MagicMock()
        session.get.return_value = mock_doc

        settings = _make_settings(extraction_confidence_threshold=0.5)

        mock_triple = MagicMock()
        mock_triple.id = 100

        with (
            patch("jmfts_core.fact_extraction._llm_extract", new_callable=AsyncMock) as mock_llm,
            patch("jmfts_core.fact_extraction.resolve_entity") as mock_entity,
            patch("jmfts_core.fact_extraction.resolve_predicate") as mock_pred,
            patch("jmfts_core.fact_extraction.TripleRepository") as mock_triple_repo_cls,
        ):
            mock_llm.return_value = llm_output
            mock_entity.return_value = (1, False)
            mock_pred.return_value = (1, False)

            mock_repo = mock_triple_repo_cls.return_value
            # First call: newly created; second call: duplicate
            mock_repo.upsert_triple.side_effect = [
                (mock_triple, True),
                (mock_triple, False),
            ]

            result = _run(
                extract_facts_from_document(document_id=1, session=session, settings=settings)
            )

            assert len(result.created_triple_ids) == 1
            assert result.skipped_count == 1
            assert mock_repo.upsert_triple.call_count == 2
            # Verify create_triple was NOT called (old code path)
            mock_repo.create_triple.assert_not_called()


# ============================================================================
# Bug 2: Missing parent_id should not crash the pipeline
# ============================================================================


class TestMissingParentHandling:
    """A parent_id that does not resolve. ``SPRINT_JOBS.md`` 15.4 S7 changed the answer.

    Three tests were here and all three asserted a GRACEFUL DEGRADE: ``ingest_conversation``
    and ``execute_pipeline`` both logged a warning, set ``parent_id = None``, and built the
    tree at the root — recording the requested parent in ``original_parent_id`` so the
    intent was at least written down somewhere.

    **The queue refuses instead**, and that is the correction rather than a regression. A
    caller who names a parent is saying where the document belongs; producing an orphan and
    returning 200 tells them nothing, and the ``original_parent_id`` key was only ever read
    by the test that asserted it. ``POST /conversations/ingest`` and ``POST /ingest`` now
    both raise ``LookupError`` — a 404 naming the document — before anything is written.

    Asserted where each route lives:
    ``tests/test_conversation_ingest.py::TestTheRefusals::test_an_unknown_parent_is_a_lookup_error``
    and
    ``tests/test_ingest_queued_usetypes.py::TestTheTwoVocabulariesDoNotCross::test_an_unknown_parent_is_still_a_lookup_error``.
    """


# ============================================================================
# Combined: pipeline resilience
# ============================================================================


class TestPipelineResilience:
    """Verify that fact extraction errors don't crash the full pipeline."""

    def test_entity_creation_failure_skips_triple(self):
        """If resolve_entity raises (e.g., parent deleted mid-extraction),
        the triple is skipped and the pipeline continues."""
        llm_output = [
            {"subject": "A", "predicate": "rel", "object": "B", "confidence": 0.9},
            {"subject": "C", "predicate": "rel2", "object": "D", "confidence": 0.9},
        ]

        mock_doc = MagicMock()
        mock_doc.content = "Text about entities."

        session = MagicMock()
        session.get.return_value = mock_doc

        settings = _make_settings(extraction_confidence_threshold=0.5)

        mock_triple = MagicMock()
        mock_triple.id = 300

        with (
            patch("jmfts_core.fact_extraction._llm_extract", new_callable=AsyncMock) as mock_llm,
            patch("jmfts_core.fact_extraction.resolve_entity") as mock_entity,
            patch("jmfts_core.fact_extraction.resolve_predicate") as mock_pred,
            patch("jmfts_core.fact_extraction.TripleRepository") as mock_triple_repo_cls,
        ):
            mock_llm.return_value = llm_output

            # First entity resolution succeeds, second raises ValueError
            mock_entity.side_effect = [
                (1, False),  # subject for triple 1
                (2, False),  # object for triple 1
                ValueError("Parent document 42 does not exist"),  # subject for triple 2
            ]
            mock_pred.return_value = (1, False)

            mock_repo = mock_triple_repo_cls.return_value
            mock_repo.upsert_triple.return_value = (mock_triple, True)

            result = _run(
                extract_facts_from_document(document_id=1, session=session, settings=settings)
            )

            # First triple should be created
            assert len(result.created_triple_ids) == 1
            # Second triple should be recorded as an error, not crash
            assert len(result.errors) == 1
            assert "Parent document" in result.errors[0]
