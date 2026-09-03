"""Ingest options for the queued pipeline. ``INGEST_SPEC.md`` 11.2.

Three layers — the task's defaults, a format's deviations from them, this request's
deviations from those — and each is tested where it actually decides something.

The merge is a pure function, so it is tested directly and exhaustively — including every
way an override can be wrong, because "wrong overrides raise" is the whole point of the
module and a silent one would be indistinguishable from a correct run.

Everything below the merge is tested THROUGH the real queue: upload, drain, then look at
the database. What matters there is not that a dict was copied around but that an option a
caller sent survives a request boundary and a worker and comes out the other end as a
differently shaped tree. That is the assertion in ``TestOptionsReachTheChunker``, and it is
the one that would fail if any of the four hand-offs between them dropped its argument.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from jmfts_client.contracts.upload import UploadedFile
from jmfts_core.ingest_options import (
    FACTS_PARAMS,
    ROLLUP_PARAMS,
    INGEST_PROFILES,
    INGEST_USETYPES,
    OPTION_CHECKS,
    STRUCTURE_CHUNK_PARAMS,
    TASK_PARAM_DEFAULTS,
    Usetype,
    _check_option_tables,
    _check_usetype_table,
    resolve_options,
    resolve_usetype_options,
)
from jmfts_core.ingest_tasks import (
    OPTIONS_KEY,
    TASK_STRUCTURE_DECLARED,
    TASK_STRUCTURE_INFERRED,
    plan_after_probe,
)
from jmfts_core.models.document import Document
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.services.ingest_service import IngestService
from jmfts_core.models.document import USETYPE_CHUNK
from tests.conftest import drain_ingest_queue

# ---------------------------------------------------------------------------
# The merge
# ---------------------------------------------------------------------------


class TestResolveOptions:
    def test_no_overrides_is_the_task_defaults(self):
        """Compared against `TASK_PARAM_DEFAULTS` and not a list of groups written here.
        Three of the six groups arrived with Phase 3 — a hand-written list would have read
        the new ones as a wrong answer rather than as new options."""
        assert resolve_options("pdf") == {
            group: dict(params) for group, params in TASK_PARAM_DEFAULTS.items()
        }
        assert resolve_options("pdf")["structure"] == dict(STRUCTURE_CHUNK_PARAMS)
        assert resolve_options("pdf")["rollup"] == dict(ROLLUP_PARAMS)
        assert resolve_options("pdf")["facts"] == dict(FACTS_PARAMS)

    def test_an_override_replaces_one_key_and_leaves_the_group_complete(self):
        """A group is merged, not substituted: the two options nobody mentioned keep the
        measured defaults, so a handler always receives a full set."""
        resolved = resolve_options("pdf", {"structure": {"max_tokens": 30}})

        assert resolved["structure"] == {
            "chunk_strategy": STRUCTURE_CHUNK_PARAMS["chunk_strategy"],
            "max_tokens": 30,
            "min_chunk_length": STRUCTURE_CHUNK_PARAMS["min_chunk_length"],
        }

    def test_the_defaults_are_not_mutated_by_a_resolve(self):
        """The tables are module state and the result goes onto a queue row. One caller
        raising max_tokens must not raise it for the next upload of the day."""
        resolve_options("pdf", {"structure": {"max_tokens": 30}})

        assert STRUCTURE_CHUNK_PARAMS["max_tokens"] == 120
        assert TASK_PARAM_DEFAULTS["structure"]["max_tokens"] == 120

    def test_resolving_a_resolved_set_is_the_identity(self):
        """What makes it safe to call twice — the upload resolves, records, and `probe`
        resolves the record again to rebuild the same plan."""
        once = resolve_options("pdf", {"structure": {"max_tokens": 30}})

        assert resolve_options("pdf", once) == once

    def test_a_format_with_no_profile_gets_the_task_defaults(self):
        """The layering, stated directly. A parameter belongs to the task that reads it, so
        a format nobody has listed is not a format with no options — it is a format that
        has asked for nothing different. `docx` here is not hypothetical: its declared-
        structure pattern is already in the Part 4 table, and 11.3's `text` entry point is
        the next thing to land."""
        assert INGEST_PROFILES == {}
        assert resolve_options("docx") == {
            group: dict(params) for group, params in TASK_PARAM_DEFAULTS.items()
        }
        assert resolve_options("wat") == resolve_options("pdf")

    def test_an_override_applies_to_a_format_with_no_profile(self):
        """A caller can tune a format nobody has written a profile for, because what is
        being tuned is the task."""
        resolved = resolve_options("docx", {"structure": {"max_tokens": 30}})

        assert resolved["structure"]["max_tokens"] == 30

    def test_a_format_profile_is_a_deviation_and_the_rest_is_inherited(self, monkeypatch):
        """The middle layer, exercised through a profile the registry does not yet have.
        Naming one option leaves the other two at the task's defaults."""
        monkeypatch.setitem(INGEST_PROFILES, "pptx", {"structure": {"max_tokens": 40}})

        assert resolve_options("pptx")["structure"] == {
            "chunk_strategy": STRUCTURE_CHUNK_PARAMS["chunk_strategy"],
            "max_tokens": 40,
            "min_chunk_length": STRUCTURE_CHUNK_PARAMS["min_chunk_length"],
        }
        # And only for that format.
        assert resolve_options("pdf")["structure"]["max_tokens"] == 120

    def test_a_caller_override_beats_a_format_profile(self, monkeypatch):
        """The order of the three layers: the request is the last word."""
        monkeypatch.setitem(INGEST_PROFILES, "pptx", {"structure": {"max_tokens": 40}})

        resolved = resolve_options("pptx", {"structure": {"max_tokens": 30}})

        assert resolved["structure"]["max_tokens"] == 30

    def test_a_profile_naming_an_option_that_does_not_exist_raises(self, monkeypatch):
        """A registry entry is held to the same standard a request is — the same merge
        checks both, so a typo in the table is found rather than absorbed."""
        monkeypatch.setitem(INGEST_PROFILES, "pptx", {"structure": {"max_token": 40}})

        with pytest.raises(ValueError, match="unknown option structure.max_token"):
            resolve_options("pptx")

    def test_an_unknown_group_raises_and_names_the_format(self):
        with pytest.raises(ValueError, match="unknown option group 'chunk'"):
            resolve_options("pdf", {"chunk": {"max_tokens": 30}})

    def test_an_unknown_group_raises_for_a_format_with_no_profile_too(self):
        with pytest.raises(ValueError, match="unknown option group 'chunk'"):
            resolve_options("png", {"chunk": {"max_tokens": 30}})

    def test_an_unknown_key_raises_rather_than_being_ignored(self):
        """The near-miss the deprecated `_resolve_stages` would swallow: a caller who
        typed `max_token` and got 120 has been told nothing at all."""
        with pytest.raises(ValueError, match="unknown option structure.max_token"):
            resolve_options("pdf", {"structure": {"max_token": 30}})

    def test_a_wrong_type_raises_and_says_what_it_wanted(self):
        with pytest.raises(ValueError, match="option structure.max_tokens: expected an integer"):
            resolve_options("pdf", {"structure": {"max_tokens": "thirty"}})

    def test_a_bool_is_not_an_integer_here(self):
        """`isinstance(True, int)` is true in Python; a chunker packing to one token is
        not what `max_tokens: true` was asking for."""
        with pytest.raises(ValueError, match="got bool"):
            resolve_options("pdf", {"structure": {"max_tokens": True}})

    def test_a_nonsense_count_raises(self):
        with pytest.raises(ValueError, match="expected a positive integer"):
            resolve_options("pdf", {"structure": {"max_tokens": 0}})

    def test_an_unknown_chunk_strategy_raises_and_lists_the_real_ones(self):
        with pytest.raises(ValueError, match="option structure.chunk_strategy: expected one of"):
            resolve_options("pdf", {"structure": {"chunk_strategy": "by_vibes"}})

    def test_a_legal_chunk_strategy_passes_through_by_value(self):
        resolved = resolve_options("pdf", {"structure": {"chunk_strategy": "paragraph"}})

        assert resolved["structure"]["chunk_strategy"] == "paragraph"

    def test_a_group_whose_value_is_not_a_mapping_raises(self):
        with pytest.raises(ValueError, match="takes a mapping"):
            resolve_options("pdf", {"structure": 120})


class TestPlanCarriesTheOptions:
    """The seam: the resolved group named by a row's ``params_key`` becomes its params."""

    def test_an_override_reaches_the_task_spec(self):
        plan = plan_after_probe("pdf", {"has_text_layer": True}, {"structure": {"max_tokens": 30}})

        params = {spec.task_type: spec.params for spec in plan.eligible}
        assert params[TASK_STRUCTURE_INFERRED]["max_tokens"] == 30
        assert params[TASK_STRUCTURE_INFERRED]["chunk_strategy"] == "sentence_packed"

    def test_omitting_options_is_the_task_defaults(self):
        plan = plan_after_probe("pdf", {"has_text_layer": True})

        params = {spec.task_type: spec.params for spec in plan.eligible}
        assert params[TASK_STRUCTURE_INFERRED] == dict(STRUCTURE_CHUNK_PARAMS)

    def test_a_task_that_takes_no_parameters_still_gets_an_empty_dict(self):
        """`extract:text` has no `params_key`, so nothing about it can be tuned into
        needing a second run — which is what the constant fingerprint states."""
        plan = plan_after_probe("pdf", {"has_text_layer": True}, {"structure": {"max_tokens": 30}})

        assert {s.task_type: s.params for s in plan.eligible}["extract:text"] == {}

    def test_a_bad_override_raises_out_of_the_planner_too(self):
        with pytest.raises(ValueError, match="unknown option"):
            plan_after_probe("pdf", {"has_text_layer": True}, {"structure": {"nope": 1}})

    def test_a_format_with_no_profile_is_planned_with_the_task_defaults(self):
        """No format has to be listed in the registry to be planned. The alternative —
        recording the task as skipped because a table did not mention the format — would
        read in the attempt log as a decision about the document, and 11.3's `text` entry
        point would land as a plausible-looking skip that reports success."""
        plan = plan_after_probe("wat", {"has_text_layer": True})

        params = {spec.task_type: spec.params for spec in plan.eligible}
        assert params[TASK_STRUCTURE_INFERRED] == dict(STRUCTURE_CHUNK_PARAMS)
        assert plan.skipped == ()

    def test_an_override_reaches_a_format_with_no_profile(self):
        plan = plan_after_probe(
            "docx",
            {"has_text_layer": True, "has_heading_styles": True},
            {"structure": {"max_tokens": 30}},
        )

        params = {spec.task_type: spec.params for spec in plan.eligible}
        assert params[TASK_STRUCTURE_DECLARED]["max_tokens"] == 30


class TestTheTablesAgree:
    """``_check_option_tables`` at import. The invariant it holds is what removes the
    question of a missing parameter set from every caller downstream."""

    def test_a_group_declared_with_no_defaults_raises(self, monkeypatch):
        """The one that matters: a group whose options nobody gave defaults for would put
        an incomplete params dict on a queue row, and the handler would then supply its own
        numbers and the attempt log would record those as the ones that were asked for."""
        monkeypatch.setitem(OPTION_CHECKS, "summarize", {"max_depth": lambda v: None})

        with pytest.raises(ValueError, match="no entry in TASK_PARAM_DEFAULTS"):
            _check_option_tables()

    def test_a_group_with_incomplete_defaults_raises(self, monkeypatch):
        monkeypatch.setitem(OPTION_CHECKS, "summarize", {"max_depth": lambda v: None})
        monkeypatch.setitem(TASK_PARAM_DEFAULTS, "summarize", {})

        with pytest.raises(ValueError, match="declared options with no default"):
            _check_option_tables()

    def test_a_profile_naming_an_undeclared_group_raises(self, monkeypatch):
        monkeypatch.setitem(INGEST_PROFILES, "pptx", {"slides": {"per_node": 1}})

        with pytest.raises(ValueError, match="unknown option group 'slides'"):
            _check_option_tables()

    def test_the_shipped_tables_pass(self):
        _check_option_tables()


class TestTheUsetypeTable:
    """``SPRINT_JOBS.md`` 15.4 S1. The seven entry points, as data — and the ONE table.

    Two named them while the migration ran: this one and ``jmfts_core/pipeline.py``'s
    ``PipelineDefinition`` registry, held in step by an import-time check. S9 deleted the
    second, so there is nothing left to drift against and the check went with it.
    """

    def test_the_seven_entry_points(self):
        assert sorted(INGEST_USETYPES) == [
            "conversation",
            "markdown",
            "raw",
            "transcript",
            "wiki:arxiv",
            "wiki:pdf",
            "wiki:url",
        ]

    def test_a_usetype_overriding_an_undeclared_option_raises(self, monkeypatch):
        monkeypatch.setitem(
            INGEST_USETYPES,
            "raw",
            Usetype(name="raw", description="x", source="content", options={"structure": {"n": 1}}),
        )
        with pytest.raises(ValueError, match="unknown option structure.n"):
            _check_usetype_table()

    def test_a_usetype_declaring_an_unknown_source_raises(self, monkeypatch):
        monkeypatch.setitem(
            INGEST_USETYPES, "raw", Usetype(name="raw", description="x", source="carrier pigeon")
        )
        with pytest.raises(ValueError, match="declares source 'carrier pigeon'"):
            _check_usetype_table()

    def test_a_usetype_registered_under_the_wrong_key_raises(self, monkeypatch):
        """The key is what a caller sends; a mismatch would make one of the two a lie."""
        monkeypatch.setitem(
            INGEST_USETYPES, "raw", Usetype(name="uncooked", description="x", source="content")
        )
        with pytest.raises(ValueError, match="calls itself 'uncooked'"):
            _check_usetype_table()

    def test_the_shipped_usetypes_pass(self):
        _check_usetype_table()

    def test_the_usetype_layer_sits_under_the_caller(self):
        """A request that names both wins. ``raw`` asks for ``sentence``; the caller does
        not have to accept it."""
        resolved = resolve_usetype_options(
            "raw", "", {"structure": {"chunk_strategy": "paragraph"}}
        )
        assert resolved["structure"]["chunk_strategy"] == "paragraph"
        # And the options the usetype set that the caller did not name still apply.
        assert resolved["structure"]["max_tokens"] == 200

    def test_the_usetype_layer_sits_over_the_task_defaults(self):
        assert resolve_usetype_options("raw", "")["structure"] == {
            "chunk_strategy": "sentence",
            "max_tokens": 200,
            "min_chunk_length": 20,
        }

    def test_a_usetype_that_overrides_nothing_gets_the_task_defaults(self):
        """``conversation`` states no chunking, so it must resolve to the MEASURED numbers
        rather than to path A's, which is the difference an empty ``options`` dict makes."""
        assert resolve_usetype_options("conversation", "")["structure"] == STRUCTURE_CHUNK_PARAMS

    def test_an_unknown_usetype_raises(self):
        with pytest.raises(ValueError, match="unknown ingest usetype 'frobnicate'"):
            resolve_usetype_options("frobnicate", "")


# ---------------------------------------------------------------------------
# Through the queue
# ---------------------------------------------------------------------------

pymupdf = pytest.importorskip("pymupdf")


def _prose_pdf() -> bytes:
    """One page of continuous prose at a single font size, and nothing else.

    No outline and no headings, so `probe` picks the inferred rung and the whole document
    is one untitled region whose chunks attach to the file node. That is the shape that
    makes the chunk count a direct readout of `max_tokens` — every other structural
    decision is held constant, so the only thing that can move it is the option.
    """
    sentence = (
        "Late interaction scores each query token against every document token and sums "
        "the maxima over the query, which is expensive to compute and cheap to explain. "
    )
    doc = pymupdf.open()
    for _ in range(2):
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(40, 40, 550, 780), sentence * 12, fontsize=11)
    return doc.tobytes()


@pytest.fixture(scope="module")
def prose_pdf() -> bytes:
    return _prose_pdf()


@pytest.fixture(scope="module")
def prose_pdf_twin(prose_pdf) -> bytes:
    """The same document, different bytes: a trailing PDF comment after `%%EOF`.

    Uploads deduplicate on the sha256 of the bytes, so two uploads of `prose_pdf` are one
    node and cannot be compared against each other. A reader ignores everything after
    `%%EOF`, so this parses to byte-identical extracted text — which is what keeps a chunk
    count comparison between the two a readout of the OPTION and not of the content.
    """
    return prose_pdf + b"\n% a byte-distinct copy of the same document\n"


@pytest.fixture
def client_with_db(db_session):
    """TestClient bound to the savepoint-wrapped session (the house pattern)."""
    from fastapi.testclient import TestClient

    from jmfts_core.rest.main import app
    from jmfts_core.database import get_db
    from tests.conftest import AUTH_HEADERS

    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    yield TestClient(app, headers=AUTH_HEADERS)
    app.dependency_overrides.pop(get_db, None)


def _upload(session, data, *, options=None):
    return IngestService(session).upload_file(
        UploadedFile(data=data, filename="paper.pdf", content_type="application/pdf"),
        options=options,
    )


def _chunk_count(session, node_id: int) -> int:
    return len(
        session.execute(
            select(Document.id).where(
                Document.parent_id == node_id, Document.usetype == USETYPE_CHUNK
            )
        )
        .scalars()
        .all()
    )


class TestOptionsSurviveTheUpload:
    def test_the_resolved_options_are_written_onto_the_node(self, db_session, evidence, prose_pdf):
        """Resolved, not the overrides: the node answers "what was this ingested with?"
        without anyone having to know which profile was in effect at the time."""
        response = _upload(db_session, prose_pdf, options={"structure": {"max_tokens": 30}})

        node = DocumentRepository(db_session).get(response.document_id)
        stored = evidence(node)[OPTIONS_KEY]
        assert stored == resolve_options("pdf", {"structure": {"max_tokens": 30}})
        assert stored["structure"] == {
            "chunk_strategy": "sentence_packed",
            "max_tokens": 30,
            "min_chunk_length": 20,
        }
        # Every group, including the three that only a node BELOW this one will read. The
        # node is where the fan-out planner comes back for them (`plan_frontier` reads the
        # nearest ancestor with a `matched` block), so a sheet's `max_rows` has to be frozen
        # here at upload time, not resolved afresh when the sheet node is written.
        assert set(stored) == set(TASK_PARAM_DEFAULTS)

    def test_an_upload_with_no_options_records_the_resolved_defaults(
        self, db_session, evidence, prose_pdf
    ):
        response = _upload(db_session, prose_pdf)

        node = DocumentRepository(db_session).get(response.document_id)
        assert evidence(node)[OPTIONS_KEY] == {
            group: dict(params) for group, params in TASK_PARAM_DEFAULTS.items()
        }

    def test_a_bad_option_is_rejected_before_anything_is_created(self, db_session, prose_pdf):
        """The reason validation happens before `repo.create`: a misspelled option must be
        a 400 on an upload that never happened, not a failed task on a node that exists."""
        before = db_session.execute(select(Document.id)).scalars().all()

        with pytest.raises(ValueError, match="unknown option structure.max_token"):
            _upload(db_session, prose_pdf, options={"structure": {"max_token": 30}})
        db_session.rollback()

        assert db_session.execute(select(Document.id)).scalars().all() == before

    def test_the_options_block_is_not_caller_writable(self, db_session, evidence, prose_pdf):
        """`probe` reads these options minutes after the request that set them returned, so
        a metadata PATCH between the two would rewrite what the run does with nothing in the
        log saying where the numbers came from.

        A REFUSAL UNTIL PHASE 2b AND A SEPARATION AFTER IT. While options were a key in
        `structured_content` the only way to protect them was for `update` to refuse a
        caller that named the key. They are an evidence row now (13.3), so a caller writing
        `{"options": ...}` writes their own column key: it is accepted, it is theirs, and it
        reaches nothing the run reads.
        """
        response = _upload(db_session, prose_pdf)
        repo = DocumentRepository(db_session)
        before = evidence(response.document_id)[OPTIONS_KEY]

        repo.update(response.document_id, structured_content={"options": {"structure": {}}})
        db_session.flush()

        assert evidence(response.document_id)[OPTIONS_KEY] == before


class TestOptionsReachTheChunker:
    """The end-to-end proof: an option a caller sent changes the tree that comes out.

    Chunk counts rather than params echoed in a log, because every hand-off between the
    request and the chunker is a place the argument could be dropped, and only the node
    count can tell that none of them did.
    """

    def test_a_smaller_budget_produces_more_chunks(self, db_session, prose_pdf, prose_pdf_twin):
        """Two nodes, so two byte-distinct files: the same bytes now resolve to one node."""
        default = _upload(db_session, prose_pdf)
        drain_ingest_queue(db_session)
        default_chunks = _chunk_count(db_session, default.document_id)

        smaller = _upload(db_session, prose_pdf_twin, options={"structure": {"max_tokens": 30}})
        drain_ingest_queue(db_session)
        smaller_chunks = _chunk_count(db_session, smaller.document_id)

        assert smaller.document_id != default.document_id
        assert default_chunks > 0
        assert smaller_chunks > default_chunks

    def test_new_options_on_known_bytes_are_refused_rather_than_ignored(
        self, db_session, prose_pdf
    ):
        """Spec 6.1: the same bytes resolve to the same node, and a parameter change is a
        re-ingest — an attempt diff that enqueues only the tasks whose fingerprint moved.
        That is the next step and is not built, so this upload can either do the work or
        say it was not done. Returning the node with the caller's `max_tokens` silently
        dropped is the option this refuses to take.
        """
        first = _upload(db_session, prose_pdf)
        drain_ingest_queue(db_session)

        with pytest.raises(ValueError, match="6.1 re-ingest"):
            _upload(db_session, prose_pdf, options={"structure": {"max_tokens": 30}})

        # And the same options twice is not a change: it deduplicates like any other repeat.
        again = _upload(db_session, prose_pdf)
        assert again.document_id == first.document_id
        assert again.was_existing is True

    def test_the_attempt_log_records_the_parameters_that_ran(self, db_session, evidence, prose_pdf):
        """3.4. The plan and the run have to agree, and the log is where a later reader
        checks that they did."""
        response = _upload(db_session, prose_pdf, options={"structure": {"max_tokens": 30}})
        drain_ingest_queue(db_session)

        node = DocumentRepository(db_session).get(response.document_id)
        attempt = next(
            entry
            for entry in evidence(node)["attempts"]
            if entry["task"] == TASK_STRUCTURE_INFERRED
        )
        assert attempt["params"]["max_tokens"] == 30
        assert attempt["detail"]["params"]["max_tokens"] == 30


class TestTheHttpSurface:
    """Multipart carries the options as a JSON form field — pydantic's own ``Json``.

    A structured parameter cannot ride on a multipart request as anything but a string, so
    the wire form is asserted here rather than assumed: the in-process caller above passes
    a real dict and would not notice if the HTTP route had stopped accepting anything.
    """

    def test_a_json_form_field_is_parsed_into_the_options(
        self, client_with_db, db_session, evidence, prose_pdf
    ):
        response = client_with_db.post(
            "/ingest/file",
            files={"file": ("paper.pdf", prose_pdf, "application/pdf")},
            data={"options": json.dumps({"structure": {"max_tokens": 30}})},
        )

        assert response.status_code == 201, response.text
        node = DocumentRepository(db_session).get(response.json()["document_id"])
        assert evidence(node)[OPTIONS_KEY]["structure"]["max_tokens"] == 30

    def test_an_upload_with_no_options_field_is_still_accepted(self, client_with_db, prose_pdf):
        """The field is optional on a multipart request, not merely nullable in the schema."""
        response = client_with_db.post(
            "/ingest/file",
            files={"file": ("paper.pdf", prose_pdf, "application/pdf")},
        )

        assert response.status_code == 201, response.text

    def test_a_misspelled_option_is_a_400(self, client_with_db, prose_pdf):
        response = client_with_db.post(
            "/ingest/file",
            files={"file": ("paper.pdf", prose_pdf, "application/pdf")},
            data={"options": json.dumps({"structure": {"max_token": 30}})},
        )

        assert response.status_code == 400
        assert "unknown option structure.max_token" in response.json()["detail"]
