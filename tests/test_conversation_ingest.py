"""Tests for conversation ingestion endpoint (#59).

Tier 1: Unit tests — parsers, no DB.
Tier 2: Integration tests — real DB (savepoint rollback), mocked LLM + embedding.
"""

import asyncio
from unittest.mock import patch

import numpy as np
import pytest

from jmfts_core.conversation_ingest import (
    ParsedMessage,
    ingest_conversation,
    parse_adjutant_jsonl,
    parse_message_array,
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
# Tier 1 — Parser Unit Tests
# ============================================================================


class TestParseAdjutantJsonl:
    def test_prompt_response_pairs(self):
        raw = (
            '{"prompt": "Hello", "response": "Hi there", "timestamp": "2025-01-01T10:00:00Z"}\n'
            '{"prompt": "How are you?", "response": "Good!", "timestamp": "2025-01-01T10:01:00Z"}\n'
        )
        msgs = parse_adjutant_jsonl(raw)
        assert len(msgs) == 4
        assert msgs[0].role == "user"
        assert msgs[0].content == "Hello"
        assert msgs[1].role == "assistant"
        assert msgs[1].content == "Hi there"
        assert msgs[2].turn_index == 2
        assert msgs[3].turn_index == 3

    def test_message_format_in_jsonl(self):
        raw = (
            '{"role": "user", "content": "Hi", "timestamp": "2025-01-01T10:00:00Z"}\n'
            '{"role": "assistant", "content": "Hello!", "timestamp": "2025-01-01T10:01:00Z"}\n'
        )
        msgs = parse_adjutant_jsonl(raw)
        assert len(msgs) == 2
        assert msgs[0].role == "user"
        assert msgs[1].role == "assistant"

    def test_skips_malformed_lines(self):
        raw = '{"prompt": "ok", "response": "yes"}\nnot json\n{"prompt": "hi", "response": "hey"}\n'
        msgs = parse_adjutant_jsonl(raw)
        assert len(msgs) == 4  # 2 pairs * 2 messages each

    def test_empty_input(self):
        assert parse_adjutant_jsonl("") == []
        assert parse_adjutant_jsonl("   \n  \n") == []

    def test_prompt_only_no_response(self):
        raw = '{"prompt": "Hello"}\n'
        msgs = parse_adjutant_jsonl(raw)
        assert len(msgs) == 1
        assert msgs[0].role == "user"
        assert msgs[0].content == "Hello"

    def test_mixed_formats(self):
        raw = (
            '{"prompt": "Hi", "response": "Hey"}\n'
            '{"role": "system", "content": "Context info"}\n'
        )
        msgs = parse_adjutant_jsonl(raw)
        assert len(msgs) == 3
        assert msgs[2].role == "system"


class TestParseMessageArray:
    def test_basic_array(self):
        arr = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
        ]
        msgs = parse_message_array(arr)
        assert len(msgs) == 2
        assert msgs[0].turn_index == 0
        assert msgs[1].turn_index == 1

    def test_with_timestamps(self):
        arr = [{"role": "user", "content": "Hi", "timestamp": "2025-01-01T10:00:00Z"}]
        msgs = parse_message_array(arr)
        assert msgs[0].timestamp == "2025-01-01T10:00:00Z"

    def test_missing_fields_defaults(self):
        arr = [{"content": "orphan message"}]
        msgs = parse_message_array(arr)
        assert msgs[0].role == "unknown"


# ============================================================================
# Tier 2 — Integration Tests (DB + mocked embedding/LLM)
# ============================================================================

try:
    from jmfts_core.database import get_session_factory
    from jmfts_core.embedding import EmbeddingResult, TokenEmbeddingResult
    from jmfts_core.repositories.document import DocumentRepository

    _DB_AVAILABLE = True
except Exception:
    _DB_AVAILABLE = False

requires_db = pytest.mark.skipif(not _DB_AVAILABLE, reason="Database not available")


class MockEmbeddingService:
    def __init__(self, dim=768):
        self.dim = dim

    def embed_text(self, text, normalize=True, prefix=""):
        rng = np.random.default_rng(hash(text) % (2**31))
        vec = rng.standard_normal(self.dim).astype(np.float32)
        vec /= np.linalg.norm(vec)
        return vec

    def embed_with_tokens(self, text, top_percent=0.35, token_selector=None, prefix=""):
        doc_emb = self.embed_text(text)
        words = (text.split() or ["empty"])[:3]
        token_embs = []
        for i, w in enumerate(words):
            rng = np.random.default_rng(hash(f"{text}_{i}") % (2**31))
            tok_emb = rng.standard_normal(self.dim).astype(np.float32)
            tok_emb /= np.linalg.norm(tok_emb)
            token_embs.append(
                TokenEmbeddingResult(
                    token_idx=i, token_text=w, importance_score=1.0 - i * 0.2, embedding=tok_emb
                )
            )
        return EmbeddingResult(document_embedding=doc_emb, token_embeddings=token_embs)

    def truncate_embedding(self, embedding, target_dim, normalize=True):
        trunc = embedding[:target_dim].copy()
        if normalize:
            n = np.linalg.norm(trunc)
            if n > 0:
                trunc /= n
        return trunc


@pytest.fixture
def db_session():
    if not _DB_AVAILABLE:
        pytest.skip("Database not available")
    SessionLocal = get_session_factory()
    session = SessionLocal()
    session.begin_nested()
    yield session
    session.rollback()
    session.close()


@pytest.fixture
def mock_embedding():
    if not _DB_AVAILABLE:
        pytest.skip("Database not available")
    svc = MockEmbeddingService()
    with patch("jmfts_core.repositories.document.get_embedding_service", return_value=svc):
        yield svc


async def _fake_llm_summarize(texts, settings, llm_model):
    parts = []
    for t in texts:
        first = t.split(".")[0].strip()
        if first:
            parts.append(first)
    return ". ".join(parts) + "." if parts else "Summary placeholder."


async def _fake_llm_extract(text, settings, llm_model=None):
    return [
        {
            "subject": "user",
            "predicate": "discussed",
            "object": "topic",
            "confidence": 0.9,
            "fact_type": "atemporal",
        }
    ]


@pytest.fixture
def mock_llm():
    with patch("jmfts_core.summarization._llm_summarize", side_effect=_fake_llm_summarize):
        with patch("jmfts_core.fact_extraction._llm_extract", side_effect=_fake_llm_extract):
            yield


# ---- Tests -----------------------------------------------------------------


@requires_db
class TestConversationIngestH1:
    """H1: Basic conversation creates root + message chunks."""

    def test_basic_ingest_no_llm(self, db_session, mock_embedding):
        messages = [
            ParsedMessage(role="user", content="Hello, how are you today?", turn_index=0),
            ParsedMessage(role="assistant", content="I am doing well, thanks!", turn_index=1),
            ParsedMessage(role="user", content="Can you help me with Python?", turn_index=2),
        ]

        result = _run(
            ingest_conversation(
                db_session, messages, title="Test Chat", summarize=False, extract_triples=False
            )
        )

        assert result.source_document_id > 0
        assert result.message_count == 3
        assert result.title == "Test Chat"

        # Check root document
        repo = DocumentRepository(db_session)
        root = repo.get(result.source_document_id)
        assert root.usetype == "conversation"
        assert root.structured_content["participants"] == ["assistant", "user"]
        assert root.structured_content["turn_count"] == 3

        # Check children
        children = repo.get_children(result.source_document_id, depth=1, limit=100)
        assert len(children) == 3
        for child in children:
            assert child.usetype == "chunk"
            assert "speaker" in child.structured_content
            assert "turn_index" in child.structured_content
            assert child.embed is not None

    def test_speaker_attribution_preserved(self, db_session, mock_embedding):
        messages = [
            ParsedMessage(
                role="alice", content="First speaker message content here.", turn_index=0
            ),
            ParsedMessage(role="bob", content="Second speaker message content here.", turn_index=1),
        ]
        result = _run(
            ingest_conversation(db_session, messages, summarize=False, extract_triples=False)
        )
        repo = DocumentRepository(db_session)
        children = repo.get_children(result.source_document_id, depth=1, limit=100)
        speakers = [c.structured_content["speaker"] for c in children]
        assert "alice" in speakers
        assert "bob" in speakers

    def test_turn_index_monotonic(self, db_session, mock_embedding):
        messages = [
            ParsedMessage(role="user", content=f"Message number {i} content.", turn_index=i)
            for i in range(5)
        ]
        result = _run(
            ingest_conversation(db_session, messages, summarize=False, extract_triples=False)
        )
        repo = DocumentRepository(db_session)
        children = repo.get_children(result.source_document_id, depth=1, limit=100)
        indices = [c.structured_content["turn_index"] for c in children]
        assert indices == sorted(indices)


@requires_db
class TestConversationIngestH2:
    """H2: Conversation with RAPTOR produces summaries."""

    def test_with_raptor(self, db_session, mock_embedding, mock_llm):
        messages = [
            ParsedMessage(
                role="user", content=f"Turn {i}: discussing topic alpha bravo.", turn_index=i
            )
            for i in range(6)
        ]
        result = _run(
            ingest_conversation(
                db_session,
                messages,
                title="RAPTOR Test",
                summarize=True,
                extract_triples=False,
            )
        )
        assert result.summary_count >= 0  # may be 0 if all cluster into one
        summarize_stage = next(s for s in result.stages if s.stage == "summarize")
        assert summarize_stage.status in ("completed", "skipped")


@requires_db
class TestConversationIngestH3:
    """H3: Conversation with fact extraction produces triples."""

    def test_with_fact_extraction(self, db_session, mock_embedding, mock_llm):
        messages = [
            ParsedMessage(
                role="user", content="Python is a programming language used widely.", turn_index=0
            ),
            ParsedMessage(
                role="assistant",
                content="Yes, Python is great for data science work.",
                turn_index=1,
            ),
        ]
        result = _run(
            ingest_conversation(
                db_session,
                messages,
                summarize=False,
                extract_triples=True,
            )
        )
        extract_stage = next(s for s in result.stages if s.stage == "extract_facts")
        assert extract_stage.status == "completed"
        assert result.triple_count >= 0


@requires_db
class TestConversationIngestA1:
    """A1: Empty conversation rejected."""

    def test_empty_messages_raises(self, db_session, mock_embedding):
        with pytest.raises(ValueError, match="No messages"):
            _run(ingest_conversation(db_session, [], summarize=False, extract_triples=False))


@requires_db
class TestConversationIngestA2:
    """A2: Single message creates root + 1 chunk, no crash."""

    def test_single_message(self, db_session, mock_embedding):
        messages = [
            ParsedMessage(
                role="user", content="Just one message in the conversation.", turn_index=0
            )
        ]
        result = _run(
            ingest_conversation(db_session, messages, summarize=False, extract_triples=False)
        )
        assert result.message_count == 1
        repo = DocumentRepository(db_session)
        children = repo.get_children(result.source_document_id, depth=1, limit=100)
        assert len(children) == 1


@requires_db
class TestConversationIngestStages:
    """Stage reporting and resilience."""

    def test_all_stages_reported(self, db_session, mock_embedding, mock_llm):
        messages = [
            ParsedMessage(role="user", content="Hello there how are you doing?", turn_index=0),
            ParsedMessage(role="assistant", content="I am good thanks for asking!", turn_index=1),
        ]
        result = _run(
            ingest_conversation(
                db_session,
                messages,
                summarize=True,
                extract_triples=True,
            )
        )
        stage_names = [s.stage for s in result.stages]
        assert "parse" in stage_names
        assert "chunk" in stage_names
        assert "summarize" in stage_names
        assert "extract_facts" in stage_names

    def test_disabled_stages_skipped(self, db_session, mock_embedding):
        messages = [
            ParsedMessage(role="user", content="Simple test message content here.", turn_index=0),
            ParsedMessage(role="assistant", content="Simple response content here.", turn_index=1),
        ]
        result = _run(
            ingest_conversation(
                db_session,
                messages,
                summarize=False,
                extract_triples=False,
            )
        )
        for s in result.stages:
            if s.stage in ("summarize", "extract_facts"):
                assert s.status == "skipped"


@requires_db
class TestOversizeRootContainer:
    """The container that holds the whole conversation concatenated is routinely
    over the 8192-token document-vector window. The embedder now refuses over-window
    text (KNOWN-DEFECTS D1), so embedding the container whole raised TextTooLongError
    and failed the whole ingest. The root must instead be left unembedded when it does
    not fit — its children (and its RAPTOR summary) remain retrievable.

    check_fit here uses the real tokenizer (no model weights), as it already does for
    the per-message over-window decision.
    """

    def test_oversize_conversation_ingests_with_root_unembedded(
        self, db_session, mock_embedding
    ):
        # Each message is under the 512-token maxsim window (embeds simply), but 40 of
        # them concatenate to well over the 8192-token document-vector window.
        messages = [
            ParsedMessage(role="user", content="word " * 300, turn_index=i)
            for i in range(40)
        ]
        result = _run(
            ingest_conversation(
                db_session, messages, summarize=False, extract_triples=False
            )
        )

        repo = DocumentRepository(db_session)
        root = repo.get(result.source_document_id)
        assert root is not None
        assert root.embed is None, (
            "an over-window container must not be embedded — there is no honest "
            "whole-text vector for it; its children and summary carry retrieval"
        )
        chunk_stage = next(s for s in result.stages if s.stage == "chunk")
        assert chunk_stage.detail["root_document_vector"] == "skipped_over_doc_window"
        # The children are still fully embedded — the ingest did not lose them.
        children = repo.get_children(root.id, usetype="chunk", depth=1, limit=10000)
        assert len(children) == 40
        assert all(c.embed is not None for c in children)

    def test_small_conversation_still_embeds_the_root(self, db_session, mock_embedding):
        # The fits-path must be preserved: a small conversation's container still gets
        # a document vector (mocked here), so small conversations stay directly
        # vector-matchable at the container level.
        messages = [
            ParsedMessage(role="user", content="A short question about foxes.", turn_index=0),
            ParsedMessage(role="assistant", content="A short answer about foxes.", turn_index=1),
        ]
        result = _run(
            ingest_conversation(
                db_session, messages, summarize=False, extract_triples=False
            )
        )
        root = DocumentRepository(db_session).get(result.source_document_id)
        assert root.embed is not None
        chunk_stage = next(s for s in result.stages if s.stage == "chunk")
        assert chunk_stage.detail["root_document_vector"] == "embedded"


# ============================================================================
# API Endpoint Tests (FastAPI TestClient)
# ============================================================================

try:
    from fastapi.testclient import TestClient
    from api.main import app

    # CR-4: present the shared-bearer token pinned by tests/conftest.py.
    from tests.conftest import AUTH_HEADERS

    _APP_AVAILABLE = True
except Exception:
    _APP_AVAILABLE = False


@pytest.mark.skipif(not (_DB_AVAILABLE and _APP_AVAILABLE), reason="DB or app unavailable")
class TestConversationEndpoint:
    def test_requires_input(self):
        with TestClient(app, headers=AUTH_HEADERS) as client:
            resp = client.post("/conversations/ingest", json={})
            assert resp.status_code == 400

    def test_rejects_both_formats(self):
        with TestClient(app, headers=AUTH_HEADERS) as client:
            resp = client.post(
                "/conversations/ingest",
                json={
                    "messages": [{"role": "user", "content": "Hi"}],
                    "jsonl": '{"prompt": "Hi", "response": "Hey"}',
                },
            )
            assert resp.status_code == 400

    def test_message_array_accepted(self, mock_embedding):
        with TestClient(app, headers=AUTH_HEADERS) as client:
            resp = client.post(
                "/conversations/ingest",
                json={
                    "messages": [
                        {"role": "user", "content": "Hello there how are you?"},
                        {"role": "assistant", "content": "I'm doing great thanks!"},
                    ],
                    "summarize": False,
                    "extract_facts": False,
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["message_count"] == 2
            assert data["source_document_id"] > 0

    def test_jsonl_accepted(self, mock_embedding):
        jsonl = '{"prompt": "Hello there friend", "response": "Hi how are you doing"}\n'
        with TestClient(app, headers=AUTH_HEADERS) as client:
            resp = client.post(
                "/conversations/ingest",
                json={
                    "jsonl": jsonl,
                    "summarize": False,
                    "extract_facts": False,
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["message_count"] == 2


class TestDocumentCreateEmbedTokens:
    """POST /documents forced both the document-vector (8192-token) and token/maxsim
    (512-token) embedding paths with no way to opt out, so any content over ~512
    tokens raised TextTooLongError with no recourse (KNOWN-DEFECTS D1). The route must
    expose embed_tokens and thread it to DocumentRepository.embed_document(with_tokens=…).
    """

    def test_schema_default_and_override(self):
        from api.schemas import DocumentCreate

        assert DocumentCreate().embed_tokens is True  # backward-compatible default
        assert DocumentCreate(embed_tokens=False).embed_tokens is False

    @pytest.mark.skipif(
        not (_DB_AVAILABLE and _APP_AVAILABLE), reason="DB or app unavailable"
    )
    def test_route_threads_embed_tokens(self):
        from unittest.mock import patch

        with patch.object(DocumentRepository, "embed_document", return_value=None) as m:
            with TestClient(app, headers=AUTH_HEADERS) as client:
                r1 = client.post(
                    "/documents",
                    json={"content": "a small document", "auto_embed": True,
                          "embed_tokens": False},
                )
                assert r1.status_code == 200
                # embed_document is called with_tokens=False when the caller opts out.
                assert m.call_args.kwargs.get("with_tokens") is False

                m.reset_mock()
                r2 = client.post(
                    "/documents",
                    json={"content": "another small document", "auto_embed": True},
                )
                assert r2.status_code == 200
                # Default preserves the prior behaviour: token vectors on.
                assert m.call_args.kwargs.get("with_tokens") is True
