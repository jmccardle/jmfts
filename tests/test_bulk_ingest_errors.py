"""Tests for bulk ingestion error handling (#340).

Reproduces two bugs from adjutant startup:
1. IntegrityError on duplicate triples during fact extraction
2. ValueError on missing parent_id during conversation ingest

Tier 1: Unit tests with mocked DB session.
"""

import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

from jmfts_core.conversation_ingest import (
    ParsedMessage,
    ingest_conversation,
)
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
            patch(
                "jmfts_core.fact_extraction._llm_extract", new_callable=AsyncMock
            ) as mock_llm,
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
                extract_facts_from_document(
                    document_id=1, session=session, settings=settings
                )
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
    """Verify that a missing parent_id degrades gracefully to root-level
    document creation instead of raising ValueError."""

    def test_conversation_ingest_missing_parent_creates_root(self):
        """ingest_conversation with nonexistent parent_id creates the
        conversation as a root document instead of raising ValueError."""
        messages = [
            ParsedMessage(role="user", content="Hello there", turn_index=0),
            ParsedMessage(role="assistant", content="Hi!", turn_index=1),
        ]

        mock_root = MagicMock()
        mock_root.id = 500
        mock_child_1 = MagicMock()
        mock_child_1.id = 501
        mock_child_2 = MagicMock()
        mock_child_2.id = 502

        mock_repo = MagicMock()
        # get(parent_id=999) returns None — parent doesn't exist
        mock_repo.get.return_value = None
        mock_repo.create.side_effect = [mock_root, mock_child_1, mock_child_2]

        session = MagicMock()

        with patch(
            "jmfts_core.conversation_ingest.DocumentRepository",
            return_value=mock_repo,
        ):
            result = _run(
                ingest_conversation(
                    session=session,
                    messages=messages,
                    parent_id=999,
                    summarize=False,
                    extract_triples=False,
                )
            )

        assert result.source_document_id == 500
        # Root document should be created with parent_id=None (not 999)
        root_call = mock_repo.create.call_args_list[0]
        assert root_call.kwargs.get("parent_id") is None
        # The original parent_id should be recorded in structured_content
        sc = root_call.kwargs.get("structured_content", {})
        assert sc.get("original_parent_id") == 999

    def test_conversation_ingest_valid_parent_preserved(self):
        """When parent_id exists, it is passed through to repo.create."""
        messages = [
            ParsedMessage(role="user", content="Hello", turn_index=0),
        ]

        mock_parent = MagicMock()
        mock_parent.id = 100
        mock_parent.path = [50]

        mock_root = MagicMock()
        mock_root.id = 200
        mock_child = MagicMock()
        mock_child.id = 201

        mock_repo = MagicMock()
        mock_repo.get.return_value = mock_parent
        mock_repo.create.side_effect = [mock_root, mock_child]

        session = MagicMock()

        with patch(
            "jmfts_core.conversation_ingest.DocumentRepository",
            return_value=mock_repo,
        ):
            _run(
                ingest_conversation(
                    session=session,
                    messages=messages,
                    parent_id=100,
                    summarize=False,
                    extract_triples=False,
                )
            )

        root_call = mock_repo.create.call_args_list[0]
        assert root_call.kwargs.get("parent_id") == 100

    def test_pipeline_validates_parent_id(self):
        """execute_pipeline sets parent_id=None when parent doesn't exist."""
        from jmfts_core.pipeline import execute_pipeline

        mock_repo = MagicMock()
        mock_repo.get.return_value = None  # parent doesn't exist

        session = MagicMock()

        with (
            patch(
                "jmfts_core.pipeline.DocumentRepository",
                return_value=mock_repo,
            ),
            patch(
                "jmfts_core.pipeline._execute_conversation",
                new_callable=AsyncMock,
            ) as mock_exec,
        ):
            mock_result = MagicMock()
            mock_result.source_document_id = 1
            mock_result.stages = []
            mock_exec.return_value = mock_result

            with patch("jmfts_core.pipeline._index_subtree_bm25") as mock_bm25:
                mock_bm25.return_value = MagicMock()
                _run(
                    execute_pipeline(
                        session=session,
                        content='{"prompt": "hi", "response": "hello"}',
                        usetype="conversation",
                        parent_id=999,
                    )
                )

            # parent_id should have been set to None before delegation
            call_kwargs = mock_exec.call_args
            assert call_kwargs.kwargs.get("parent_id") is None


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
            patch(
                "jmfts_core.fact_extraction._llm_extract", new_callable=AsyncMock
            ) as mock_llm,
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
                extract_facts_from_document(
                    document_id=1, session=session, settings=settings
                )
            )

            # First triple should be created
            assert len(result.created_triple_ids) == 1
            # Second triple should be recorded as an error, not crash
            assert len(result.errors) == 1
            assert "Parent document" in result.errors[0]
