"""Per-collection ingest configuration — ``SPRINT_JOBS.md`` 5.1, phase 4's leftover.

**A collection is not a scope a table has to express. It is a subtree, and a setting on a
node reaches the documents beneath it.** That is the whole of 5.1, and it is why this
needs no bindings table: the tree is already there, and RBAC — which is already
subtree-scoped — answers "who may configure this collection" with "who may write to this
node".

Three things 5.1 decides, and each has its tests here:

1. **Layered merge, not nearest-wins.** ``rollup_tasks._options_owner`` returns ONE owner
   and uses its set wholesale, which is correct while a file node always has a complete
   row. Under a tree a caller puts ``structure.max_tokens`` on ``/papers`` and
   ``facts.enabled`` on ``/papers/2026``, and nearest-wins gives them only the second.
2. **The walk runs at upload, not at plan time.** ``plan_after_probe(fmt, patterns,
   options)`` never learns that ancestors exist, so ``EXPLAIN`` and the run stay provably
   one function (11.2's purity rule). The resolved set lands in the node's ``options``
   evidence row exactly as before.
3. **A collection is a default for new uploads, not a live policy.** A document ingested
   before the setting changed keeps what it was ingested with.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jmfts_core.access import AccessDeniedError
from jmfts_core.database import get_db
from jmfts_core.ingest_options import TASK_PARAM_DEFAULTS, merge_option_layers, resolve_options
from jmfts_core.ingest_tasks import OPTIONS_KEY, collection_option_layers
from jmfts_core.models.document import Document
from jmfts_core.models.principal import AccessGrant, Principal as PrincipalModel
from jmfts_core.principal_context import CurrentPrincipal, reset_principal, set_principal
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.evidence import EvidenceRepository
from jmfts_core.rest.main import app
from jmfts_core.services.ingest_service import IngestService

PAPERS = {"structure": {"max_tokens": 120}}
YEAR = {"facts": {"enabled": False}}


def _folder(session, title: str, parent: Document | None = None) -> Document:
    """An ASSERTED node — `produced_by` is NULL, which is what a collection is."""
    return DocumentRepository(session).create(
        title=title,
        content=None,
        usetype="collection",
        parent_id=parent.id if parent else None,
        auto_embed=False,
        embed_tokens=False,
    )


@pytest.fixture
def client_with_db(db_session):
    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    from tests.conftest import AUTH_HEADERS

    client = TestClient(app, headers=AUTH_HEADERS)
    yield client
    app.dependency_overrides.pop(get_db, None)


class TestResolveOptionsTakesLayers:
    """``TASK_PARAM_DEFAULTS <- INGEST_PROFILES[fmt] <- each ancestor, root-first <- the
    request``. 5.1 point 1 makes the layer count N; ``resolve_options`` already merged."""

    def test_an_inherited_layer_lands_under_the_request(self):
        resolved = resolve_options("", {"structure": {"max_tokens": 50}}, inherited=(PAPERS,))
        assert resolved["structure"]["max_tokens"] == 50

    def test_a_nearer_layer_refines_a_further_one_without_restating_it(self):
        # The case nearest-wins gets wrong: two settings on two levels, and both survive.
        resolved = resolve_options("", None, inherited=(PAPERS, YEAR))
        assert resolved["structure"]["max_tokens"] == 120
        assert resolved["facts"]["enabled"] is False

    def test_no_layers_is_the_answer_it_always_was(self):
        assert resolve_options("", None, inherited=()) == resolve_options("", None)

    def test_an_inherited_layer_is_validated_like_every_other(self):
        with pytest.raises(ValueError, match="structure.max_tokns"):
            resolve_options("", None, inherited=({"structure": {"max_tokns": 9}},))

    def test_an_inherited_layer_is_blamed_on_the_collection_and_not_on_the_request(self):
        # Attribution, not decoration: a caller told their option is wrong when the wrong
        # one is three folders above them looks for a bug in their own request.
        with pytest.raises(ValueError, match="a collection's options"):
            resolve_options("", None, inherited=({"structure": {"max_tokns": 9}},))

    def test_merging_first_and_resolving_with_layers_give_the_same_answer(self):
        """The ratchet ``merge_option_layers``' docstring promises.

        Two callers layer these declarations: the upload hands them to `resolve_options` as
        `inherited=`, and `analyze_ingest` merges them into one overrides dict so that
        `explain_plan` stays a pure function of `(format, patterns, options)`. If the two
        ever stopped agreeing, the forecast and the run would differ in exactly the way
        11.2 exists to prevent — and neither would look wrong on its own.
        """
        request = {"structure": {"chunk_strategy": "paragraph"}}
        merged = merge_option_layers(PAPERS, YEAR, request)
        assert resolve_options("pdf", merged) == resolve_options(
            "pdf", request, inherited=(PAPERS, YEAR)
        )


class TestTheAncestorWalk:
    """The same walk ``rollup_tasks._options_owner`` runs, started one level higher."""

    def test_a_file_with_no_parent_inherits_nothing(self, db_session):
        assert collection_option_layers(db_session, None) == ()

    def test_a_folder_with_no_options_contributes_no_layer(self, db_session):
        folder = _folder(db_session, "papers")
        db_session.flush()
        assert collection_option_layers(db_session, folder.id) == ()

    def test_the_layers_come_back_root_first(self, db_session):
        papers = _folder(db_session, "papers")
        db_session.flush()
        year = _folder(db_session, "2026", parent=papers)
        db_session.flush()
        EvidenceRepository(db_session).write(papers.id, OPTIONS_KEY, PAPERS)
        EvidenceRepository(db_session).write(year.id, OPTIONS_KEY, YEAR)
        db_session.flush()
        # Root-first, because the nearer layer must be applied LAST to win a key contest.
        assert collection_option_layers(db_session, year.id) == (PAPERS, YEAR)


class TestTheUploadResolvesUnderItsCollection:
    """5.1 point 2: at upload, where `resolve_options` is already called."""

    def test_a_file_uploaded_under_a_configured_folder_records_the_merged_set(self, db_session):
        papers = _folder(db_session, "papers")
        db_session.flush()
        EvidenceRepository(db_session).write(papers.id, OPTIONS_KEY, PAPERS)
        db_session.flush()

        node_id = _upload(db_session, b"# A paper\n\nprose.\n", "paper.md", parent_id=papers.id)
        recorded = EvidenceRepository(db_session).read(node_id, OPTIONS_KEY)
        assert recorded["structure"]["max_tokens"] == 120

    def test_the_request_still_wins_over_the_collection(self, db_session):
        papers = _folder(db_session, "papers")
        db_session.flush()
        EvidenceRepository(db_session).write(papers.id, OPTIONS_KEY, PAPERS)
        db_session.flush()

        node_id = _upload(
            db_session,
            b"# A paper\n\nprose.\n",
            "paper.md",
            parent_id=papers.id,
            options={"structure": {"max_tokens": 60}},
        )
        recorded = EvidenceRepository(db_session).read(node_id, OPTIONS_KEY)
        assert recorded["structure"]["max_tokens"] == 60

    def test_a_file_uploaded_with_no_parent_is_unchanged(self, db_session):
        node_id = _upload(db_session, b"# A paper\n\nprose.\n", "paper.md")
        recorded = EvidenceRepository(db_session).read(node_id, OPTIONS_KEY)
        assert recorded == resolve_options("markdown", None)

    def test_a_document_ingested_before_the_setting_changed_keeps_what_it_had(self, db_session):
        # 5.1 point 3, stated as a test because it is the honest consequence of point 2 and
        # the thing a caller is most likely to expect otherwise.
        papers = _folder(db_session, "papers")
        db_session.flush()
        node_id = _upload(db_session, b"# One\n\nprose.\n", "one.md", parent_id=papers.id)
        before = EvidenceRepository(db_session).read(node_id, OPTIONS_KEY)

        EvidenceRepository(db_session).write(papers.id, OPTIONS_KEY, PAPERS)
        db_session.flush()
        assert EvidenceRepository(db_session).read(node_id, OPTIONS_KEY) == before


def _upload(session, data: bytes, filename: str, *, parent_id=None, options=None) -> int:
    from jmfts_client.contracts.upload import UploadedFile

    service = IngestService(session)
    answer = service.upload_file(
        UploadedFile(data=data, filename=filename, content_type="text/markdown"),
        parent_id=parent_id,
        options=options,
    )
    return answer.document_id


class TestTheRoute:
    """5.1: "a route that writes an `options` row to a node that has none"."""

    def test_setting_and_reading_back_a_collections_options(self, db_session):
        folder = _folder(db_session, "papers")
        db_session.flush()
        service = IngestService(db_session)

        written = service.set_collection_options(folder.id, _request(PAPERS))
        assert written.options == PAPERS
        assert written.effective == PAPERS
        assert service.get_collection_options(folder.id).options == PAPERS

    def test_a_nested_collection_reports_what_it_inherits_and_what_it_adds(self, db_session):
        papers = _folder(db_session, "papers")
        db_session.flush()
        year = _folder(db_session, "2026", parent=papers)
        db_session.flush()
        service = IngestService(db_session)
        service.set_collection_options(papers.id, _request(PAPERS))
        service.set_collection_options(year.id, _request(YEAR))

        answer = service.get_collection_options(year.id)
        assert answer.options == YEAR
        assert answer.inherited == PAPERS
        assert answer.effective == {**PAPERS, **YEAR}

    def test_an_empty_declaration_clears_the_row_rather_than_needing_a_second_verb(
        self, db_session
    ):
        folder = _folder(db_session, "papers")
        db_session.flush()
        service = IngestService(db_session)
        service.set_collection_options(folder.id, _request(PAPERS))
        assert service.set_collection_options(folder.id, _request({})).effective == {}

    def test_an_unknown_option_is_refused_and_named(self, db_session):
        folder = _folder(db_session, "papers")
        db_session.flush()
        with pytest.raises(ValueError, match="max_tokns"):
            IngestService(db_session).set_collection_options(
                folder.id, _request({"structure": {"max_tokns": 9}})
            )

    def test_a_missing_document_is_a_lookup_error(self, db_session):
        with pytest.raises(LookupError):
            IngestService(db_session).set_collection_options(10_000_001, _request(PAPERS))

    def test_an_ingested_file_node_is_refused(self, db_session):
        # Its `options` row is the RECORD of the run that made it (3.3), not a policy for
        # uploads beneath it. Overwriting it would rewrite what the node says it was
        # ingested with, which is the one thing that row is for.
        node_id = _upload(db_session, b"# A paper\n\nprose.\n", "paper.md")
        with pytest.raises(ValueError, match="was ingested"):
            IngestService(db_session).set_collection_options(node_id, _request(PAPERS))

    def test_a_produced_node_is_refused(self, db_session):
        folder = _folder(db_session, "papers")
        db_session.flush()
        chunk = _folder(db_session, "a chunk", parent=folder)
        chunk.produced_by = "structure:declared"
        db_session.flush()
        with pytest.raises(ValueError, match="produced by"):
            IngestService(db_session).set_collection_options(chunk.id, _request(PAPERS))


def _request(options: dict):
    from jmfts_client.contracts.upload import CollectionOptionsRequest

    return CollectionOptionsRequest(options=options)


class TestTheWire:
    def test_the_routes_are_registered_once_each(self):
        from jmfts_core.registry import REGISTRY

        paths = [
            (spec.method, spec.path)
            for spec in REGISTRY
            if spec.path == "/documents/{document_id}/ingest-options"
        ]
        assert sorted(paths) == [
            ("GET", "/documents/{document_id}/ingest-options"),
            ("PUT", "/documents/{document_id}/ingest-options"),
        ]

    def test_a_collection_is_configured_over_http(self, client_with_db, db_session):
        folder = _folder(db_session, "papers")
        db_session.flush()
        put = client_with_db.put(f"/documents/{folder.id}/ingest-options", json={"options": PAPERS})
        assert put.status_code == 200, put.text
        assert put.json()["effective"] == PAPERS

        got = client_with_db.get(f"/documents/{folder.id}/ingest-options")
        assert got.status_code == 200, got.text
        assert got.json()["options"] == PAPERS

    def test_an_unknown_option_is_a_400_naming_it(self, client_with_db, db_session):
        folder = _folder(db_session, "papers")
        db_session.flush()
        response = client_with_db.put(
            f"/documents/{folder.id}/ingest-options",
            json={"options": {"structure": {"max_tokns": 9}}},
        )
        assert response.status_code == 400
        assert "max_tokns" in response.text

    def test_a_missing_document_is_a_404(self, client_with_db):
        assert client_with_db.get("/documents/10000001/ingest-options").status_code == 404


class TestAnalyzeForecastsTheCollectionItWouldLandIn:
    """``ANALYZE`` is a forecast of the upload, so it walks the same chain.

    Without this it resolves against the request alone and reports a plan under options the
    upload is not going to use — the confident wrong answer 11.2 refuses for ``EXPLAIN``.
    """

    def test_analyze_under_a_configured_folder_reports_the_merged_options(self, db_session):
        papers = _folder(db_session, "papers")
        db_session.flush()
        EvidenceRepository(db_session).write(papers.id, OPTIONS_KEY, PAPERS)
        db_session.flush()

        from jmfts_client.contracts.upload import UploadedFile

        answer = IngestService(db_session).analyze_ingest(
            UploadedFile(data=b"# One\n\nprose.\n", filename="one.md", content_type="text/plain"),
            parent_id=papers.id,
        )
        assert answer.plan.options["structure"]["max_tokens"] == 120

    def test_analyze_with_no_parent_is_unchanged(self, db_session):
        from jmfts_client.contracts.upload import UploadedFile

        answer = IngestService(db_session).analyze_ingest(
            UploadedFile(data=b"# One\n\nprose.\n", filename="one.md", content_type="text/plain")
        )
        assert answer.plan.options["structure"] == TASK_PARAM_DEFAULTS["structure"]


class TestAccessIsTheSubtreeRbacThatAlreadyExists:
    """5.1's "what this buys that a bindings table would not: authorization, free".

    "Who may configure this collection" is "who may write to this node", with no second
    answer to a question RBAC has already answered.
    """

    def test_writing_a_collections_options_needs_write_access_to_the_node(self, db_session):
        folder = _folder(db_session, "papers")
        db_session.flush()
        row = PrincipalModel(name="reader")
        db_session.add(row)
        db_session.flush()
        db_session.add(AccessGrant(document_id=folder.id, principal_id=row.id, level="read"))
        db_session.flush()

        reader = CurrentPrincipal(id=row.id, name="reader", is_owner=False)
        token = set_principal(reader)
        try:
            with pytest.raises(AccessDeniedError):
                IngestService(db_session).set_collection_options(folder.id, _request(PAPERS))
            # Reading is not writing: a grantee who may see the folder may see its policy.
            assert IngestService(db_session).get_collection_options(folder.id).options == {}
        finally:
            reset_principal(token)
