"""Tests for the general ingest pipeline (#61).

Tier 1: Unit tests — registry, config resolution, no DB.
Tier 2: Integration tests — real DB (savepoint rollback), mocked LLM + embedding.
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from jmfts_core.pipeline import (
    PipelineDefinition,
    StageConfig,
    _resolve_stages,
    execute_pipeline,
    get_pipeline,
    list_pipelines,
    register_pipeline,
)

# ============================================================================
# Helpers
# ============================================================================


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ============================================================================
# Tier 1 — Registry Unit Tests
# ============================================================================


class TestPipelineRegistry:
    def test_builtin_pipelines_registered(self):
        names = [p.name for p in list_pipelines()]
        assert "conversation" in names
        assert "markdown" in names
        assert "raw" in names
        assert "transcript" in names

    def test_get_pipeline_returns_definition(self):
        p = get_pipeline("conversation")
        assert p is not None
        assert p.name == "conversation"
        assert "parse" in p.default_stages
        assert "chunk" in p.default_stages
        assert "summarize" in p.default_stages
        assert "extract_facts" in p.default_stages

    def test_get_pipeline_unknown_returns_none(self):
        assert get_pipeline("nonexistent") is None

    def test_register_custom_pipeline(self):
        register_pipeline(
            PipelineDefinition(
                name="_test_custom",
                description="test only",
                default_stages={
                    "parse": StageConfig(),
                    "chunk": StageConfig(params={"strategy": "paragraph"}),
                },
            )
        )
        p = get_pipeline("_test_custom")
        assert p is not None
        assert p.default_stages["chunk"].params["strategy"] == "paragraph"

    def test_list_pipelines_returns_all(self):
        pipelines = list_pipelines()
        assert len(pipelines) >= 4  # at least the 4 built-ins


class TestStageResolution:
    def test_defaults_preserved_without_overrides(self):
        p = get_pipeline("markdown")
        stages = _resolve_stages(p, None)
        assert stages["chunk"].params["strategy"] == "paragraph"
        assert stages["summarize"].enabled is True

    def test_override_disable_stage(self):
        p = get_pipeline("raw")
        stages = _resolve_stages(p, {"summarize": False})
        assert stages["summarize"].enabled is False
        # Other stages unaffected
        assert stages["extract_facts"].enabled is True

    def test_override_stage_params(self):
        p = get_pipeline("raw")
        stages = _resolve_stages(p, {"chunk": {"strategy": "paragraph", "max_tokens": 500}})
        assert stages["chunk"].params["strategy"] == "paragraph"
        assert stages["chunk"].params["max_tokens"] == 500

    def test_override_enabled_and_params(self):
        p = get_pipeline("raw")
        stages = _resolve_stages(
            p,
            {"summarize": {"enabled": False, "params": {"max_depth": 3}}},
        )
        assert stages["summarize"].enabled is False
        assert stages["summarize"].params["max_depth"] == 3

    def test_unknown_stage_override_ignored(self):
        p = get_pipeline("raw")
        stages = _resolve_stages(p, {"nonexistent_stage": {"enabled": True}})
        assert "nonexistent_stage" not in stages

    def test_override_does_not_mutate_defaults(self):
        p = get_pipeline("raw")
        original_strategy = p.default_stages["chunk"].params.get("strategy")
        _resolve_stages(p, {"chunk": {"strategy": "paragraph"}})
        assert p.default_stages["chunk"].params.get("strategy") == original_strategy


class TestExecutePipelineValidation:
    def test_unknown_usetype_raises(self):
        session = MagicMock()
        with pytest.raises(ValueError, match="Unknown pipeline usetype"):
            _run(execute_pipeline(session, "hello", "nonexistent_type"))


# ============================================================================
# Tier 2 — Integration Tests (mocked DB + embedding)
# ============================================================================


class _FakeDocument:
    """Minimal stand-in for the Document ORM model."""

    def __init__(self, id, embed=None, content=""):
        self.id = id
        self.embed = embed
        self.content = content


class _FakeRepo:
    """Minimal stand-in for DocumentRepository."""

    def __init__(self):
        self._next_id = 1
        self._docs: dict[int, _FakeDocument] = {}

    def create(self, **kwargs):
        doc = _FakeDocument(
            id=self._next_id,
            embed=[0.1] * 1024 if kwargs.get("auto_embed", True) else None,
            content=kwargs.get("content", ""),
        )
        self._docs[doc.id] = doc
        self._next_id += 1
        return doc

    def get(self, doc_id):
        return self._docs.get(doc_id)

    def get_children(self, parent_id, depth=1, limit=10000):
        return [d for d in self._docs.values() if d.id != 1]


class TestMarkdownPipeline:
    def test_markdown_with_headings(self):
        content = (
            "# Introduction\n\n"
            "This is the intro paragraph. It has enough text to be meaningful.\n\n"
            "# Methods\n\n"
            "We used several methods in our research. The first was observation.\n\n"
            "# Results\n\n"
            "The results were significant. We found many interesting patterns.\n"
        )
        fake_repo = _FakeRepo()
        session = MagicMock()
        session.flush = MagicMock()

        with patch("jmfts_core.pipeline.DocumentRepository") as MockRepo:
            MockRepo.return_value = MagicMock(
                find_by_hash_and_parent=MagicMock(return_value=None),
                create=MagicMock(side_effect=lambda **kw: fake_repo.create(**kw)),
                get_children=MagicMock(return_value=[]),
            )

            result = _run(
                execute_pipeline(
                    session,
                    content,
                    "markdown",
                    title="Test Markdown",
                    pipeline_config={
                        "summarize": False,
                        "extract_facts": False,
                    },
                )
            )

        assert result.source_document_id == 1  # root doc
        assert result.title == "Test Markdown"
        assert result.segment_count >= 3  # at least 3 sections
        assert len(result.stages) == 5  # parse, chunk, summarize(skip), facts(skip), bm25_index

        # Parse stage
        parse_stage = result.stages[0]
        assert parse_stage.stage == "parse"
        assert parse_stage.status == "completed"
        assert parse_stage.detail["sections"] == 3
        assert parse_stage.detail["had_headings"] is True

        # Chunk stage
        chunk_stage = result.stages[1]
        assert chunk_stage.stage == "chunk"
        assert chunk_stage.status == "completed"
        assert chunk_stage.detail["chunks_created"] >= 3

        # Summarize/facts skipped
        assert result.stages[2].status == "skipped"
        assert result.stages[3].status == "skipped"

    def test_markdown_without_headings(self):
        content = "Just a plain paragraph with no headings at all. Another sentence here."
        fake_repo = _FakeRepo()
        session = MagicMock()
        session.flush = MagicMock()

        with patch("jmfts_core.pipeline.DocumentRepository") as MockRepo:
            MockRepo.return_value = MagicMock(
                find_by_hash_and_parent=MagicMock(return_value=None),
                create=MagicMock(side_effect=lambda **kw: fake_repo.create(**kw)),
                get_children=MagicMock(return_value=[]),
            )

            result = _run(
                execute_pipeline(
                    session,
                    content,
                    "markdown",
                    pipeline_config={"summarize": False, "extract_facts": False},
                )
            )

        assert result.source_document_id == 1
        assert result.segment_count >= 1

    def test_markdown_empty_raises(self):
        session = MagicMock()
        with pytest.raises(ValueError, match="empty markdown"):
            _run(
                execute_pipeline(
                    session,
                    "",
                    "markdown",
                    pipeline_config={"summarize": False, "extract_facts": False},
                )
            )


class TestRawPipeline:
    def test_raw_text_ingestion(self):
        content = (
            "The quick brown fox jumped over the lazy dog. "
            "This is a test of the raw text ingestion pipeline. "
            "It should chunk the text into sentences and create documents."
        )
        fake_repo = _FakeRepo()
        session = MagicMock()
        session.flush = MagicMock()

        with patch("jmfts_core.pipeline.DocumentRepository") as MockRepo:
            MockRepo.return_value = MagicMock(
                find_by_hash_and_parent=MagicMock(return_value=None),
                create=MagicMock(side_effect=lambda **kw: fake_repo.create(**kw)),
                get_children=MagicMock(return_value=[]),
            )

            result = _run(
                execute_pipeline(
                    session,
                    content,
                    "raw",
                    title="Test Raw",
                    pipeline_config={"summarize": False, "extract_facts": False},
                )
            )

        assert result.source_document_id == 1
        assert result.title == "Test Raw"
        assert result.segment_count >= 1
        assert result.stages[0].stage == "parse"
        assert result.stages[1].stage == "chunk"

    def test_raw_empty_raises(self):
        session = MagicMock()
        with pytest.raises(ValueError, match="empty text"):
            _run(
                execute_pipeline(
                    session,
                    "  ",
                    "raw",
                    pipeline_config={"summarize": False, "extract_facts": False},
                )
            )

    def test_raw_config_override_strategy(self):
        content = (
            "First paragraph of text here.\n\n"
            "Second paragraph of text here.\n\n"
            "Third paragraph of text here."
        )
        fake_repo = _FakeRepo()
        session = MagicMock()
        session.flush = MagicMock()

        with patch("jmfts_core.pipeline.DocumentRepository") as MockRepo:
            MockRepo.return_value = MagicMock(
                find_by_hash_and_parent=MagicMock(return_value=None),
                create=MagicMock(side_effect=lambda **kw: fake_repo.create(**kw)),
                get_children=MagicMock(return_value=[]),
            )

            result = _run(
                execute_pipeline(
                    session,
                    content,
                    "raw",
                    pipeline_config={
                        "chunk": {"strategy": "paragraph"},
                        "summarize": False,
                        "extract_facts": False,
                    },
                )
            )

        assert result.segment_count == 3  # 3 paragraphs


class TestTranscriptPipeline:
    def test_transcript_uses_transcript_usetype(self):
        content = "Hello, welcome to the show. Today we discuss AI. It is very interesting."
        fake_repo = _FakeRepo()
        created_docs = []
        original_create = fake_repo.create

        def tracking_create(**kw):
            doc = original_create(**kw)
            created_docs.append(kw)
            return doc

        session = MagicMock()
        session.flush = MagicMock()

        with patch("jmfts_core.pipeline.DocumentRepository") as MockRepo:
            MockRepo.return_value = MagicMock(
                find_by_hash_and_parent=MagicMock(return_value=None),
                create=MagicMock(side_effect=tracking_create),
                get_children=MagicMock(return_value=[]),
            )

            result = _run(
                execute_pipeline(
                    session,
                    content,
                    "transcript",
                    pipeline_config={"summarize": False, "extract_facts": False},
                )
            )

        # Root doc should have usetype="transcript"
        root_kwargs = created_docs[0]
        assert root_kwargs["usetype"] == "transcript"
        assert result.segment_count >= 1


class TestConversationPipelineViaExecutor:
    def test_conversation_delegates_to_ingest_conversation(self):
        jsonl = (
            '{"prompt": "Hello", "response": "Hi!"}\n'
            '{"prompt": "How are you?", "response": "Good"}\n'
        )
        session = MagicMock()
        session.flush = MagicMock()

        with patch("jmfts_core.pipeline.ingest_conversation") as mock_ingest:
            from jmfts_core.conversation_ingest import IngestResult, StageClock, StageResult

            # Stamped and reasoned like the real orchestrator's stages: execute_pipeline
            # now persists these as attempt records, and the record rejects a finished
            # attempt with no measured time or a `skipped` with no reason.
            clock = StageClock()
            mock_ingest.return_value = IngestResult(
                source_document_id=1,
                title="Test conv",
                message_count=4,
                segment_count=0,
                summary_count=0,
                triple_count=0,
                tree_depth=1,
                stages=[
                    clock.stamp(StageResult(stage="parse", status="completed")),
                    clock.stamp(StageResult(stage="chunk", status="completed")),
                    clock.stamp(
                        StageResult(
                            stage="summarize", status="skipped", detail={"reason": "disabled"}
                        )
                    ),
                    clock.stamp(
                        StageResult(
                            stage="extract_facts", status="skipped", detail={"reason": "disabled"}
                        )
                    ),
                ],
            )

            result = _run(
                execute_pipeline(
                    session,
                    jsonl,
                    "conversation",
                    title="Test conv",
                    pipeline_config={"summarize": False, "extract_facts": False},
                )
            )

        mock_ingest.assert_called_once()
        assert result.message_count == 4


# ============================================================================
# API Endpoint Tests
# ============================================================================


class TestIngestEndpoint:
    """Test the /ingest API endpoint via FastAPI test client."""

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient

        from jmfts_core.rest.main import app

        # CR-4: present the shared-bearer token pinned by tests/conftest.py.
        from tests.conftest import AUTH_HEADERS

        return TestClient(app, headers=AUTH_HEADERS)

    def test_list_pipelines(self, client):
        resp = client.get("/ingest/pipelines")
        assert resp.status_code == 200
        data = resp.json()
        names = [p["name"] for p in data]
        assert "conversation" in names
        assert "markdown" in names
        assert "raw" in names
        assert "transcript" in names

        # Check structure
        for pipeline in data:
            assert "description" in pipeline
            assert "stages" in pipeline
            for stage in pipeline["stages"]:
                assert "name" in stage
                assert "enabled" in stage

    def test_empty_content_rejected(self, client):
        resp = client.post("/ingest", json={"content": "", "usetype": "raw"})
        assert resp.status_code == 400

    def test_unknown_usetype_rejected(self, client):
        resp = client.post("/ingest", json={"content": "hello world", "usetype": "frobnicate"})
        assert resp.status_code == 400
        assert "frobnicate" in resp.json()["detail"]

    def test_missing_required_fields(self, client):
        resp = client.post("/ingest", json={"content": "hello"})
        assert resp.status_code == 422  # missing usetype

    def test_raw_ingest_accepted(self, client):
        """Smoke test that valid raw request gets through validation.

        The /ingest route is now generated from the @expose registry, so its
        ``db`` dependency is overridden via ``app.dependency_overrides[get_db]``
        rather than by patching a (now-deleted) router module.
        """
        from jmfts_core.rest.main import app
        from jmfts_core.database import get_db

        fake_repo = _FakeRepo()
        session = MagicMock()
        session.flush = MagicMock()
        session.commit = MagicMock()

        def _override_get_db():
            yield session

        app.dependency_overrides[get_db] = _override_get_db
        try:
            with (
                patch("jmfts_core.pipeline.DocumentRepository") as MockRepo,
                patch("jmfts_core.pipeline.SearchRepository") as MockSearchRepo,
            ):
                MockRepo.return_value = MagicMock(
                    find_by_hash_and_parent=MagicMock(return_value=None),
                    create=MagicMock(side_effect=lambda **kw: fake_repo.create(**kw)),
                    get_children=MagicMock(return_value=[]),
                    get=MagicMock(return_value=None),
                    get_subtree=MagicMock(return_value=[]),
                )
                mock_search_repo = MagicMock()
                mock_search_repo.get_index.return_value = None
                mock_search_repo.index_document.return_value = True
                MockSearchRepo.return_value = mock_search_repo

                resp = client.post(
                    "/ingest",
                    json={
                        "content": "The quick brown fox. The lazy dog. Another sentence.",
                        "usetype": "raw",
                        "title": "Test Raw Ingest",
                        "pipeline_config": {
                            "summarize": False,
                            "extract_facts": False,
                        },
                    },
                )
        finally:
            app.dependency_overrides.pop(get_db, None)

        assert resp.status_code == 200
        data = resp.json()
        assert data["usetype"] == "raw"
        assert data["title"] == "Test Raw Ingest"
        assert data["source_document_id"] >= 1
        assert len(data["stages"]) == 5
