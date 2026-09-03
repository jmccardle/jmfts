"""``POST /ingest`` on the ingest queue. ``SPRINT_JOBS.md`` 15.4 S5.

``markdown``, ``raw`` and ``transcript`` no longer run a stage list inside the request.
The content string becomes a file node with stored bytes, and the request drains THAT
DOCUMENT'S queue before returning the finished tree it always returned.

Three properties are guarded here, and they fail differently:

1. **The wire did not move.** The same request shape comes back with the same fields, and
   the counts describe the tree that now exists.
2. **The work really is the queue's.** The node is a ``file`` node with a blob, its attempt
   log holds ``probe`` and the tasks Part 4's table scheduled, and the tree is settled —
   none of which a stage list inside the request would produce.
3. **The two option vocabularies do not silently cross.** ``pipeline_config`` on a queued
   usetype is a 400 and ``options`` on a synchronous one is a 400, because there is no
   translation between them and accepting one to do something else is the swallowed
   failure this codebase does not ship.

The suite pins ``JMFTS_INGEST_WORKER_ENABLED=0``, so the inline drain inside
``ingest_content`` is the only thing running tasks here — which is exactly the arrangement
being tested.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from jmfts_client.contracts.ingest import IngestRequest
from jmfts_core.ingest_options import TASK_PARAM_DEFAULTS
from jmfts_core.models.document import SETTLED_SETTLED, USETYPE_FILE, Document
from jmfts_core.repositories.blob import BlobRepository
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.services.ingest_service import IngestService
from jmfts_core.models.document import USETYPE_CHUNK

QUEUED = ["markdown", "raw", "transcript"]

PROSE = (
    "# Overview\n\n"
    "Alpha beta gamma delta epsilon zeta. The quick brown fox jumped over the lazy dog "
    "and kept running until it reached the river bank.\n\n"
    "# Details\n\n"
    "Eta theta iota kappa lambda mu. A second paragraph with enough words in it to be "
    "worth chunking at all, twice over if the chunker feels like it.\n"
)


def _ingest(session, content=PROSE, **kwargs):
    """Drive the real service method. It is `async def` but this path awaits nothing."""
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            IngestService(session).ingest_content(IngestRequest(content=content, **kwargs))
        )
    finally:
        loop.close()


class TestTheResponseStillDescribesAFinishedTree:
    @pytest.mark.parametrize("usetype", QUEUED)
    def test_every_queued_usetype_returns_a_settled_tree(self, db_session, usetype):
        response = _ingest(db_session, usetype=usetype)

        assert response.usetype == usetype
        node = DocumentRepository(db_session).get(response.source_document_id)
        assert node.settled == SETTLED_SETTLED
        assert response.message_count > 0, "the queue built no chunks"

    def test_the_counts_are_the_nodes_that_exist(self, db_session):
        response = _ingest(db_session, usetype="raw")

        chunks = (
            db_session.execute(
                select(Document)
                .where(Document.path.contains([response.source_document_id]))
                .where(Document.usetype == USETYPE_CHUNK)
            )
            .scalars()
            .all()
        )
        assert response.message_count == len(chunks)
        assert response.tree_depth >= 2

    def test_the_title_default_is_the_one_path_a_used(self, db_session):
        """A caller who sends no title got `"Raw (12 words)"` and still does — it is in the
        response and it is the file node's name."""
        content = "one two three four five"
        response = _ingest(db_session, content=content, usetype="raw")

        assert response.title == "Raw (5 words)"

    def test_a_supplied_title_is_kept(self, db_session):
        response = _ingest(db_session, usetype="markdown", title="A Named Document")
        assert response.title == "A Named Document"

    def test_the_stages_are_the_queue_tasks_that_ran(self, db_session):
        response = _ingest(db_session, usetype="markdown")

        names = [s.stage for s in response.stages]
        assert names[0] == "probe"
        assert "extract:text" in names
        assert any(n.startswith("structure:") for n in names), names
        assert all(s.status in ("completed", "skipped") for s in response.stages), names


class TestTheWorkIsReallyTheQueues:
    def test_the_root_is_a_file_node_holding_the_bytes(self, db_session):
        """The visible change. It is a `file` node now, not a `raw` one, because that is
        what it really is: a node with stored bytes that probe measured."""
        content = "Alpha beta gamma delta. " * 20
        response = _ingest(db_session, content=content, usetype="raw")

        node = DocumentRepository(db_session).get(response.source_document_id)
        assert node.usetype == USETYPE_FILE
        stored = BlobRepository(db_session).read_bytes(node.id)
        assert stored.decode("utf-8") == content

    def test_probe_measured_the_content_rather_than_trusting_the_usetype(
        self, db_session, evidence
    ):
        """`raw` and `markdown` differ only in their chunking options now. Whether the
        document gets the DECLARED rung is decided by probe finding headings — so the same
        text sent as `raw` still gets `has_headings`."""
        response = _ingest(db_session, usetype="raw")

        node = DocumentRepository(db_session).get(response.source_document_id)
        matched = evidence(node)["matched"]
        assert matched["format"] == "text"
        assert matched["patterns"]["has_headings"] is True
        assert [s.stage for s in response.stages if s.stage.startswith("structure:")] == [
            "structure:declared"
        ]

    def test_a_document_with_no_headings_gets_the_inferred_rung(self, db_session):
        response = _ingest(db_session, content="Alpha beta gamma delta. " * 20, usetype="markdown")

        assert [s.stage for s in response.stages if s.stage.startswith("structure:")] == [
            "structure:inferred"
        ]

    def test_the_usetypes_chunking_option_reaches_the_chunker(self, db_session, evidence):
        """`raw` asks for `sentence`; the recorded options on the node say so, which is
        what `plan_after_probe` copies onto the queue row."""
        response = _ingest(db_session, usetype="raw")

        node = DocumentRepository(db_session).get(response.source_document_id)
        assert evidence(node)["options"]["structure"]["chunk_strategy"] == "sentence"

    def test_a_caller_override_beats_the_usetype(self, db_session, evidence):
        response = _ingest(
            db_session, usetype="raw", options={"structure": {"chunk_strategy": "paragraph"}}
        )

        node = DocumentRepository(db_session).get(response.source_document_id)
        assert evidence(node)["options"]["structure"]["chunk_strategy"] == "paragraph"

    def test_llm_model_becomes_a_rollup_option(self, db_session, evidence):
        """The one field of the old vocabulary that maps cleanly: it names the same thing
        in both, which model answers."""
        response = _ingest(db_session, usetype="raw", llm_model="some-other-model")

        node = DocumentRepository(db_session).get(response.source_document_id)
        assert evidence(node)["options"]["rollup"]["llm_model"] == "some-other-model"

    def test_a_misspelled_option_is_a_400_before_anything_is_written(self, db_session):
        before = db_session.execute(select(Document.id)).scalars().all()

        with pytest.raises(ValueError, match="unknown option structure.max_token"):
            _ingest(db_session, usetype="raw", options={"structure": {"max_token": 30}})

        assert db_session.execute(select(Document.id)).scalars().all() == before


class TestDeduplication:
    def test_the_same_content_twice_is_one_node(self, db_session):
        first = _ingest(db_session, usetype="raw")
        second = _ingest(db_session, usetype="raw")

        assert second.was_existing is True
        assert second.existing_document_id == first.source_document_id
        assert second.source_document_id == first.source_document_id

    def test_a_dedup_hit_still_reports_the_finished_tree(self, db_session):
        """Path A returned zeroes on a hit. Here the counts describe the tree that is
        really there, because reading it back costs one query and reporting zero for a
        document with forty chunks would be a wrong answer."""
        first = _ingest(db_session, usetype="raw")
        second = _ingest(db_session, usetype="raw")

        assert second.message_count == first.message_count
        assert second.tree_depth == first.tree_depth

    def test_the_same_bytes_with_different_options_is_refused(self, db_session):
        """Spec 6.1, through `_place_existing_file`. Returning the node as though the
        options had been applied is the quiet wrong answer."""
        _ingest(db_session, usetype="raw")

        with pytest.raises(ValueError, match="already holds these exact bytes"):
            _ingest(db_session, usetype="raw", options={"structure": {"max_tokens": 33}})

    def test_the_same_content_under_two_usetypes_collides_on_their_options(self, db_session):
        """`raw` and `markdown` resolve to different chunk strategies, so the same text
        under both is the case above. Not a defect — it is the appliance saying it will not
        re-chunk stored bytes silently, which is the only honest answer until 6.1's re-run
        diff exists."""
        _ingest(db_session, usetype="raw")

        with pytest.raises(ValueError, match="already holds these exact bytes"):
            _ingest(db_session, usetype="markdown")


class TestTheTwoVocabulariesDoNotCross:
    def test_pipeline_config_on_a_queued_usetype_raises(self, db_session):
        with pytest.raises(ValueError, match="rather than `pipeline_config`"):
            _ingest(db_session, usetype="raw", pipeline_config={"summarize": False})

    # test_options_on_a_synchronous_usetype_raises WAS HERE. It sent `options` to
    # `wiki:url` and asserted the 400 that said to use `pipeline_config` instead. S8 moved
    # the three `wiki:` entry points onto the queue, so there is no synchronous usetype
    # left to refuse `options` — every one of the seven takes it. The refusal it tested is
    # still in the code and still correct; nothing can reach it, and S9 deletes it with the
    # branch it guards.

    def test_empty_content_is_still_refused_first(self, db_session):
        with pytest.raises(ValueError, match="Content must not be empty"):
            _ingest(db_session, content="   ", usetype="raw")

    def test_an_unknown_parent_is_still_a_lookup_error(self, db_session):
        with pytest.raises(LookupError, match="Parent document 999999999 not found"):
            _ingest(db_session, usetype="raw", parent_id=999999999)


class TestFactExtraction:
    """``extract:facts``. ``SPRINT_JOBS.md`` 15.4 S6, ``INGEST_SPEC.md`` 11.4.

    The suite runs with no LLM configured, which is the appliance's documented default, so
    what is asserted here is that the task is SCHEDULED correctly and reports honestly when
    it cannot run. Whether the model returns good triples is `tests/test_fact_extraction.py`.
    """

    def test_the_text_usetypes_schedule_it_because_path_a_did(self, db_session):
        response = _ingest(db_session, usetype="raw")

        assert "extract:facts" in [s.stage for s in response.stages]

    def test_an_unconfigured_llm_is_a_skip_with_a_reason_not_a_failure(self, db_session, evidence):
        """A blank `JMFTS_LLM_*` is a supported configuration. Failing an ingest over it
        would put the node into `settled = 'failed'` on an appliance configured exactly as
        documented — manufacturing a problem rather than reporting one."""
        from jmfts_core.config import get_settings

        assert not get_settings().llm_configured, "this test asserts the no-LLM path"

        response = _ingest(db_session, usetype="raw")

        stage = [s for s in response.stages if s.stage == "extract:facts"][0]
        assert stage.status == "skipped"
        assert stage.error is None
        node = DocumentRepository(db_session).get(response.source_document_id)
        attempt = [e for e in evidence(node)["attempts"] if e["task"] == "extract:facts"][0]
        assert "JMFTS_LLM_BASE_URL" in attempt["detail"]["reason"]
        # And the tree still settles: a skip is a terminal outcome, not an open task.
        assert node.settled == SETTLED_SETTLED

    def test_a_caller_can_turn_it_off(self, db_session, evidence):
        response = _ingest(db_session, usetype="raw", options={"facts": {"enabled": False}})

        assert "extract:facts" not in [s.stage for s in response.stages]
        node = DocumentRepository(db_session).get(response.source_document_id)
        not_applicable = evidence(node)["attempts"][0]["detail"]["not_applicable"]
        assert "options.facts.enabled is false" in not_applicable["extract:facts"]

    def test_an_upload_does_not_schedule_it(self, db_session, evidence):
        """The group default is off. An upload names no usetype and has never had fact
        extraction; turning it on here would put an LLM call over every chunk of every
        file anybody uploads."""
        stored = IngestService(db_session).store_text_as_file(PROSE, filename="note")
        from tests.conftest import drain_ingest_queue

        drain_ingest_queue(db_session)

        node = DocumentRepository(db_session).get(stored.document_id)
        assert "extract:facts" not in [e["task"] for e in evidence(node)["attempts"]]

    def test_llm_model_reaches_both_groups(self, db_session, evidence):
        """`IngestRequest.llm_model` says "summarization and extraction", and the two are
        separate option groups because they are separate tasks."""
        response = _ingest(db_session, usetype="raw", llm_model="some-other-model")

        node = DocumentRepository(db_session).get(response.source_document_id)
        options = evidence(node)["options"]
        assert options["rollup"]["llm_model"] == "some-other-model"
        assert options["facts"]["llm_model"] == "some-other-model"

    def test_the_task_reads_its_own_group(self, db_session, evidence):
        response = _ingest(
            db_session, usetype="raw", options={"facts": {"llm_model": "facts-only"}}
        )

        node = DocumentRepository(db_session).get(response.source_document_id)
        assert evidence(node)["options"]["facts"]["llm_model"] == "facts-only"
        assert evidence(node)["options"]["rollup"]["llm_model"] == ""


# ---------------------------------------------------------------------------
# Over HTTP
#
# MOVED HERE from tests/test_pipeline.py by SPRINT_JOBS.md 15.4 S8. The class is about
# `POST /ingest` and `GET /ingest/pipelines`, which outlive the synchronous pipeline;
# it was in that module because that module used to be where the endpoint's behaviour
# came from.
# ---------------------------------------------------------------------------


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

        # A `stages` list was here until SPRINT_JOBS.md 15.4 S9. There are no stages —
        # there are tasks, and which of them a document runs is what POST /ingest/explain
        # answers, from a format and a set of options rather than from a name.
        assert all("stages" not in pipeline for pipeline in data)

        # The endpoint answers from INGEST_USETYPES, so every entry carries where its
        # content comes from and the options it resolves to.
        by_name = {p["name"]: p for p in data}
        assert by_name["raw"]["source"] == "content"
        assert by_name["wiki:arxiv"]["source"] == "arxiv"
        assert by_name["raw"]["options"]["structure"]["chunk_strategy"] == "sentence"
        assert by_name["markdown"]["options"]["structure"]["chunk_strategy"] == "paragraph"
        # Every group resolves for every entry point, so `options` is a complete set and
        # not a diff — the same property `resolve_options` has.
        assert set(by_name["raw"]["options"]) == set(TASK_PARAM_DEFAULTS)

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

    def test_a_queued_usetype_refuses_pipeline_config(self, client):
        """S5. `raw` runs on the queue now, and the two option vocabularies do not
        translate — path A's `summarize` stage was RAPTOR clustering, path B's `summarize`
        task is what gives a container its vectors. Accepting the field and doing something
        else is the swallowed failure; the 400 names the field to use instead."""
        resp = client.post(
            "/ingest",
            json={
                "content": "The quick brown fox. The lazy dog.",
                "usetype": "raw",
                "pipeline_config": {"summarize": False},
            },
        )
        assert resp.status_code == 400
        assert "options" in resp.json()["detail"]

    # test_an_unqueued_usetype_refuses_options WAS HERE, for the reason its twin in
    # tests/test_ingest_queued_usetypes.py gives: after 15.4 S8 there is no usetype the
    # synchronous pipeline still serves, so nothing can reach the refusal.
