"""Conversation ingestion (#59), on the ingest queue since ``SPRINT_JOBS.md`` 15.4 S7.

Tier 1: the parsers, no DB. Unchanged — they are still the one place that decides what a
message is, and they are what ``probe``'s ``is_conversation`` and
``structure:conversation`` both read through.

Tier 2: the whole ingest, through ``ConversationService`` against a real database. It used
to drive ``conversation_ingest.ingest_conversation`` directly with a mocked embedding
service; that function is deleted, and the assertions moved onto what the queue produces:
a ``file`` node holding the JSONL, one ``chunk`` per turn, and a settled tree.

The embedding service is REAL here (CPU, tokenizer plus the cached model) rather than
mocked. The mock existed to keep a stage list fast, and the queue's `embed` task is what
writes vectors now — mocking `get_embedder` in one module while the task resolves its own
would assert something no deployment does.
"""

import asyncio
from unittest.mock import patch

import pytest

from jmfts_core.conversation_ingest import (
    ParsedMessage,
    conversation_markdown,
    messages_to_jsonl,
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
# Tier 1 — the two round trips
# ============================================================================


class TestMessagesToJsonl:
    """``SPRINT_JOBS.md`` 15.4 S7 — the bytes a JSON array becomes."""

    def test_it_round_trips_through_the_parser(self):
        messages = [
            ParsedMessage(role="user", content="Hello there", turn_index=0),
            ParsedMessage(role="assistant", content="Hi yourself", turn_index=1),
        ]

        back = parse_adjutant_jsonl(messages_to_jsonl(messages))

        assert [(m.role, m.content, m.turn_index) for m in back] == [
            ("user", "Hello there", 0),
            ("assistant", "Hi yourself", 1),
        ]

    def test_a_timestamp_survives_and_an_absent_one_is_not_invented(self):
        messages = [
            ParsedMessage(role="user", content="a", timestamp="2025-01-01T10:00:00Z"),
            ParsedMessage(role="user", content="b"),
        ]

        lines = messages_to_jsonl(messages).splitlines()

        assert "2025-01-01T10:00:00Z" in lines[0]
        assert "timestamp" not in lines[1]

    def test_the_result_is_what_probe_calls_a_conversation(self):
        """The property the whole cut-over rests on: what this writes, probe recognises."""
        from jmfts_core.probe import detect_format, probe_patterns

        data = messages_to_jsonl(
            [ParsedMessage(role="user", content="Hello", turn_index=0)]
        ).encode("utf-8")

        patterns, _ = probe_patterns(data, detect_format(data))
        assert patterns["is_conversation"] is True

    def test_non_ascii_content_is_not_escaped_away(self):
        messages = [ParsedMessage(role="user", content="café ☕")]
        assert "café ☕" in messages_to_jsonl(messages)


class TestConversationMarkdown:
    def test_it_is_the_readable_concatenation(self):
        messages = [
            ParsedMessage(role="user", content="Question?"),
            ParsedMessage(role="assistant", content="Answer."),
        ]
        assert conversation_markdown(messages) == "[user]: Question?\n\n[assistant]: Answer."


# ============================================================================
# Tier 2 — Integration, through the queue
# ============================================================================

try:
    from jmfts_core.database import get_session_factory  # noqa: F401
    from jmfts_core.repositories.document import DocumentRepository

    _DB_AVAILABLE = True
except Exception:
    _DB_AVAILABLE = False

requires_db = pytest.mark.skipif(not _DB_AVAILABLE, reason="Database not available")


def _ingest(session, messages=None, jsonl=None, **kwargs):
    """One conversation through the real service, which drains its own queue."""
    from jmfts_client.contracts.conversation import ConversationIngestRequest
    from jmfts_core.services.conversation_service import ConversationService

    return ConversationService(session).ingest_conversation(
        ConversationIngestRequest(messages=messages, jsonl=jsonl, **kwargs)
    )


def _msgs(*pairs):
    return [{"role": role, "content": content} for role, content in pairs]


@requires_db
class TestTheTreeTheQueueBuilds:
    def test_a_conversation_becomes_a_file_node_with_one_chunk_per_turn(self, db_session, evidence):
        response = _ingest(
            db_session,
            messages=_msgs(
                ("user", "Hello, how are you today?"),
                ("assistant", "I am doing well, thanks!"),
                ("user", "Can you help me with Python?"),
            ),
            title="Test Chat",
            extract_facts=False,
        )

        assert response.message_count == 3
        assert response.title == "Test Chat"

        repo = DocumentRepository(db_session)
        root = repo.get(response.source_document_id)
        # WAS `usetype == "conversation"`. The root holds the stored JSONL now, which is
        # what a `file` node is; what it holds is a transcript, and `matched.patterns`
        # says so.
        assert root.usetype == "file"
        assert evidence(root)["matched"]["patterns"]["is_conversation"] is True
        assert evidence(root)["structure"]["source"] == "conversation_turns"
        assert root.settled == "settled"

        children = repo.get_children(root.id, depth=1, limit=100)
        assert len(children) == 3
        for child in children:
            assert child.usetype == "chunk"
            assert "speaker" in evidence(child)
            assert "turn_index" in evidence(child)
            assert child.embed is not None, "the embed task did not run"

    def test_the_file_node_reads_as_the_conversation_not_as_json(self, db_session, evidence):
        """`extract:text`'s conversation reader. A search hit on the root shows the turns,
        not the braces — which is also what the BM25 index would hold."""
        response = _ingest(
            db_session,
            messages=_msgs(("user", "A question about foxes."), ("assistant", "An answer.")),
            extract_facts=False,
        )

        root = DocumentRepository(db_session).get(response.source_document_id)
        assert root.content == "[user]: A question about foxes.\n\n[assistant]: An answer."
        assert evidence(root)["extraction"]["source"] == "conversation_transcript"

    def test_speaker_attribution_preserved(self, db_session, evidence):
        response = _ingest(
            db_session,
            messages=_msgs(
                ("alice", "First speaker message content here."),
                ("bob", "Second speaker message content here."),
            ),
            extract_facts=False,
        )
        children = DocumentRepository(db_session).get_children(
            response.source_document_id, depth=1, limit=100
        )
        speakers = [evidence(c)["speaker"] for c in children]
        assert "alice" in speakers
        assert "bob" in speakers

    def test_turn_index_monotonic(self, db_session, evidence):
        response = _ingest(
            db_session,
            messages=_msgs(*[("user", f"Message number {i} content.") for i in range(5)]),
            extract_facts=False,
        )
        children = DocumentRepository(db_session).get_children(
            response.source_document_id, depth=1, limit=100
        )
        indices = [evidence(c)["turn_index"] for c in children]
        assert indices == sorted(indices)
        assert indices == list(range(5))

    def test_a_single_message_is_a_conversation_of_one(self, db_session):
        response = _ingest(
            db_session,
            messages=_msgs(("user", "Just one message in the conversation.")),
            extract_facts=False,
        )
        assert response.message_count == 1
        children = DocumentRepository(db_session).get_children(
            response.source_document_id, depth=1, limit=100
        )
        assert len(children) == 1

    def test_jsonl_and_the_equivalent_array_reach_the_same_node(self, db_session):
        """The re-encoding is faithful: the two spellings of one transcript deduplicate."""
        first = _ingest(
            db_session,
            messages=_msgs(("user", "Hello there friend"), ("assistant", "Hi how are you")),
            extract_facts=False,
        )
        jsonl = messages_to_jsonl(
            [
                ParsedMessage(role="user", content="Hello there friend"),
                ParsedMessage(role="assistant", content="Hi how are you"),
            ]
        )

        second = _ingest(db_session, jsonl=jsonl, extract_facts=False)

        assert second.source_document_id == first.source_document_id


@requires_db
class TestTheStagesAreTasks:
    def test_the_reported_stages_are_the_queue_tasks(self, db_session):
        response = _ingest(
            db_session,
            messages=_msgs(("user", "Hello there how are you doing?"), ("assistant", "Good!")),
            extract_facts=False,
        )

        names = [s.stage for s in response.stages]
        assert names[0] == "probe"
        assert "extract:text" in names
        assert "structure:conversation" in names, names
        # The two prose rungs must NOT have run: a transcript's leaves are its turns.
        assert "structure:declared" not in names
        assert "structure:inferred" not in names
        assert all(s.status in ("completed", "skipped") for s in response.stages), names

    def test_fact_extraction_is_scheduled_when_asked_for(self, db_session):
        response = _ingest(
            db_session,
            messages=_msgs(("user", "Python is a programming language used widely.")),
            extract_facts=True,
        )

        stage = [s for s in response.stages if s.stage == "extract:facts"]
        assert len(stage) == 1
        # The suite runs with no LLM configured, so the honest outcome is a skip with a
        # reason rather than a failure. See tests/test_ingest_queued_usetypes.py.
        assert stage[0].status == "skipped"

    def test_fact_extraction_is_not_scheduled_when_declined(self, db_session):
        response = _ingest(
            db_session,
            messages=_msgs(("user", "Python is a programming language used widely.")),
            extract_facts=False,
        )
        assert "extract:facts" not in [s.stage for s in response.stages]


@requires_db
class TestTheRefusals:
    def test_empty_messages_raises(self, db_session):
        with pytest.raises(ValueError, match="Provide either"):
            _ingest(db_session, messages=[])

    def test_both_formats_raises(self, db_session):
        with pytest.raises(ValueError, match="not both"):
            _ingest(db_session, messages=_msgs(("user", "Hi")), jsonl='{"prompt": "Hi"}')

    def test_a_raptor_depth_is_refused_rather_than_ignored(self, db_session):
        """The rollup segments in document order and does not cluster, so there is nothing
        for the value to tune. A run that built a different tree and reported success would
        have told the caller nothing."""
        with pytest.raises(ValueError, match="raptor_max_depth describes RAPTOR"):
            _ingest(db_session, messages=_msgs(("user", "Hi there")), raptor_max_depth=3)

    def test_a_min_cluster_size_is_refused_too(self, db_session):
        with pytest.raises(ValueError, match="raptor_min_cluster_size describes RAPTOR"):
            _ingest(db_session, messages=_msgs(("user", "Hi there")), raptor_min_cluster_size=4)

    def test_summarize_false_is_refused(self, db_session):
        """Path A's `summarize` was optional RAPTOR clustering. The queue's `summarize`
        task is what gives a container its content and vectors, so honouring the flag by
        doing nothing would leave the rollup's containers unretrievable."""
        with pytest.raises(ValueError, match="summarize=False cannot be honoured"):
            _ingest(db_session, messages=_msgs(("user", "Hi there")), summarize=False)

    def test_an_unknown_parent_is_a_lookup_error(self, db_session):
        """WAS a warning and a silent degrade to a root document. The queue refuses: a
        caller who named a parent and got an orphan was told nothing."""
        with pytest.raises(LookupError, match="Parent document 999999999 not found"):
            _ingest(db_session, messages=_msgs(("user", "Hi there")), parent_id=999999999)


@requires_db
class TestOversizeTurns:
    """KNOWN-DEFECTS D1, carried across from the deprecated pipeline.

    Most assistant messages exceed the 512-token token/maxsim window. Such a turn becomes a
    container with chunk children that fit; before that shape existed it was embedded as a
    ~2000-character prefix and reported as fully embedded.
    """

    def test_an_over_window_turn_gets_children_that_fit(self, db_session, evidence):
        response = _ingest(
            db_session,
            messages=_msgs(("assistant", "word " * 600), ("user", "A short reply.")),
            extract_facts=False,
        )

        repo = DocumentRepository(db_session)
        turns = repo.get_children(response.source_document_id, depth=1, limit=100)
        big = next(t for t in turns if evidence(t)["over_token_window"])
        parts = repo.get_children(big.id, depth=1, limit=100)
        assert parts, "an over-window turn must be split into pieces that fit"
        assert all(evidence(p)["part_index"] == i for i, p in enumerate(parts))
        assert all(p.embed is not None for p in parts)

    def test_a_turn_that_fits_gets_no_children(self, db_session, evidence):
        response = _ingest(
            db_session,
            messages=_msgs(("user", "A short question about foxes.")),
            extract_facts=False,
        )
        repo = DocumentRepository(db_session)
        turn = repo.get_children(response.source_document_id, depth=1, limit=100)[0]
        assert evidence(turn)["over_token_window"] is False
        assert repo.get_children(turn.id, depth=1, limit=100) == []


# ============================================================================
# API Endpoint Tests (FastAPI TestClient)
# ============================================================================

try:
    from fastapi.testclient import TestClient
    from jmfts_core.rest.main import app

    # CR-4: present the shared-bearer token pinned by tests/conftest.py.
    from tests.conftest import AUTH_HEADERS

    _APP_AVAILABLE = True
except Exception:
    _APP_AVAILABLE = False


@pytest.fixture
def client_with_db(db_session):
    """A ``TestClient`` bound to the rolled-back session.

    Not a bare ``TestClient(app)``, and the difference is load-bearing since
    ``SPRINT_JOBS.md`` 15.4 S7. These routes commit — they always did — and without the
    override the commit goes to the real connection and the rows outlive the test. That was
    invisible while a conversation's root was a ``conversation`` node, because the counts
    other modules assert are over ``file`` nodes; a transcript's root is a ``file`` node
    now, so the leak started failing `tests/test_file_upload.py` a hundred tests later.
    """
    from jmfts_core.database import get_db

    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    client = TestClient(app, headers=AUTH_HEADERS)
    yield client
    app.dependency_overrides.pop(get_db, None)


@pytest.mark.skipif(not (_DB_AVAILABLE and _APP_AVAILABLE), reason="DB or app unavailable")
class TestConversationEndpoint:
    def test_requires_input(self, client_with_db):
        client = client_with_db
        resp = client.post("/conversations/ingest", json={})
        assert resp.status_code == 400

    def test_rejects_both_formats(self, client_with_db):
        client = client_with_db
        resp = client.post(
            "/conversations/ingest",
            json={
                "messages": [{"role": "user", "content": "Hi"}],
                "jsonl": '{"prompt": "Hi", "response": "Hey"}',
            },
        )
        assert resp.status_code == 400

    def test_message_array_accepted(self, client_with_db):
        client = client_with_db
        resp = client.post(
            "/conversations/ingest",
            json={
                "messages": [
                    {"role": "user", "content": "Hello there how are you?"},
                    {"role": "assistant", "content": "I'm doing great thanks!"},
                ],
                "extract_facts": False,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["message_count"] == 2
        assert data["source_document_id"] > 0

    def test_jsonl_accepted(self, client_with_db):
        jsonl = '{"prompt": "Hello there friend", "response": "Hi how are you doing"}\n'
        resp = client_with_db.post(
            "/conversations/ingest",
            json={"jsonl": jsonl, "extract_facts": False},
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
        from jmfts_core.rest.schemas import DocumentCreate

        assert DocumentCreate().embed_tokens is True  # backward-compatible default
        assert DocumentCreate(embed_tokens=False).embed_tokens is False

    @pytest.mark.skipif(not (_DB_AVAILABLE and _APP_AVAILABLE), reason="DB or app unavailable")
    def test_route_threads_embed_tokens(self, client_with_db):
        client = client_with_db
        with patch.object(DocumentRepository, "embed_document", return_value=None) as m:
            if True:
                r1 = client.post(
                    "/documents",
                    json={"content": "a small document", "auto_embed": True, "embed_tokens": False},
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
