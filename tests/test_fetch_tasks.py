"""Getting the bytes, when what arrived was a locator. ``SPRINT_JOBS.md`` 15.4 S8.

Nothing here touches the network. ``url_fetch.fetch_url`` and ``arxiv_fetch.fetch_arxiv_pdf``
are patched — the real seam, one level below the handler, so the handler's own re-encoding
and error wrapping still run — because what is being tested is the SHAPE of the hand-off — a node that exists
before its bytes do, a `file` block written by a task instead of by an upload, `probe`
enqueued by the fetch rather than by the request — and a real HTTP call would make every
one of those assertions depend on somebody else's uptime. ``fetch:path`` is exercised
against a real temporary file, since the local one is the fetcher with no network in it.

The property that matters most is the LAST one: a fetched document is indistinguishable
from an uploaded one from `probe` onward. So the tests compare against an upload of the
same bytes rather than against a written-down expectation.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy import select

from jmfts_client.contracts.ingest import IngestRequest
from jmfts_core.ingest_tasks import SOURCE_KEY, TASK_FETCH_PATH, TASK_FETCH_URL, TASK_PROBE
from jmfts_core.models.document import SETTLED_IN_FLIGHT, SETTLED_SETTLED, USETYPE_FILE, Document
from jmfts_core.repositories.blob import BlobRepository
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.task_queue import TaskQueueRepository
from jmfts_core.services.ingest_service import IngestService
from jmfts_core.models.document import USETYPE_CHUNK
from tests.conftest import drain_ingest_queue

PAGE = "# A Fetched Page\n\nAlpha beta gamma delta epsilon zeta eta theta.\n"


def _ingest(session, content, usetype, **kwargs):
    """`POST /ingest` for a source usetype, which fetches inside the request."""
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            IngestService(session).ingest_content(
                IngestRequest(content=content, usetype=usetype, **kwargs)
            )
        )
    finally:
        loop.close()


class TestTheSourceNode:
    def test_a_locator_makes_a_node_with_no_bytes_and_a_fetch_task(self, db_session, evidence):
        """A state no upload can produce, and an honest one: somebody asked for this
        document, the bytes are on their way, nothing may treat it as retrievable yet."""
        response = IngestService(db_session).store_source_as_file(
            "url", "https://example.test/page"
        )

        node = DocumentRepository(db_session).get(response.document_id)
        assert node.usetype == USETYPE_FILE
        assert node.settled == SETTLED_IN_FLIGHT
        found = evidence(node)
        assert found[SOURCE_KEY]["kind"] == "url"
        assert found[SOURCE_KEY]["locator"] == "https://example.test/page"
        assert "file" not in found
        assert BlobRepository(db_session).read_bytes(node.id) is None
        queued = TaskQueueRepository(db_session).unfinished_tasks_for(node.id)
        assert [t.task_type for t in queued] == [TASK_FETCH_URL]

    def test_probe_is_not_enqueued_until_the_bytes_land(self, db_session, evidence):
        """The one ordering the whole design rests on. `probe` reads bytes; enqueuing it
        beside the fetch would let it be claimed first and fail on an empty blob."""
        response = IngestService(db_session).store_source_as_file(
            "url", "https://example.test/page"
        )
        tasks = TaskQueueRepository(db_session)
        assert TASK_PROBE not in [
            t.task_type for t in tasks.unfinished_tasks_for(response.document_id)
        ]

        with patch("jmfts_core.url_fetch.fetch_url", return_value=(PAGE, "text/plain")):
            drain_ingest_queue(db_session)

        node = DocumentRepository(db_session).get(response.document_id)
        assert [e["task"] for e in evidence(node)["attempts"]][:2] == [
            TASK_FETCH_URL,
            TASK_PROBE,
        ]

    def test_an_unknown_kind_is_refused(self, db_session):
        with pytest.raises(ValueError, match="unknown source kind 'carrier-pigeon'"):
            IngestService(db_session).store_source_as_file("carrier-pigeon", "somewhere")

    def test_an_empty_locator_is_refused(self, db_session):
        with pytest.raises(ValueError, match="needs a locator"):
            IngestService(db_session).store_source_as_file("url", "   ")

    def test_the_same_locator_twice_is_one_node(self, db_session):
        """Deduplicated on the LOCATOR, because the bytes are what has not been fetched."""
        service = IngestService(db_session)
        first = service.store_source_as_file("url", "https://example.test/page")

        second = service.store_source_as_file("url", "https://example.test/page")

        assert second.was_existing is True
        assert second.document_id == first.document_id
        assert (
            len(TaskQueueRepository(db_session).unfinished_tasks_for(first.document_id)) == 1
        ), "a second request must not enqueue a second fetch"

    def test_a_different_locator_is_a_different_node(self, db_session):
        service = IngestService(db_session)
        first = service.store_source_as_file("url", "https://example.test/one")
        second = service.store_source_as_file("url", "https://example.test/two")
        assert second.document_id != first.document_id

    def test_an_unknown_parent_is_refused_before_the_node_exists(self, db_session):
        before = db_session.execute(select(Document.id)).scalars().all()
        with pytest.raises(LookupError, match="Parent document 999999999 not found"):
            IngestService(db_session).store_source_as_file(
                "url", "https://example.test/page", parent_id=999999999
            )
        assert db_session.execute(select(Document.id)).scalars().all() == before


class TestTheFetchTask:
    def test_it_stores_what_it_fetched_and_probe_measures_that(self, db_session, evidence):
        """Path A converted HTML to markdown before storing and kept neither original.
        The blob is the fetched bytes, and the reader is chosen from what probe measured."""
        html = b"<html><body><h1>A Page</h1><p>Some prose here, at length.</p></body></html>"

        response = IngestService(db_session).store_source_as_file(
            "url", "https://example.test/page"
        )
        with patch(
            "jmfts_core.url_fetch.fetch_url",
            return_value=(html.decode("utf-8"), "text/html"),
        ):
            drain_ingest_queue(db_session)

        node = DocumentRepository(db_session).get(response.document_id)
        assert BlobRepository(db_session).read_bytes(node.id) == html
        assert evidence(node)["matched"]["patterns"]["has_markup"] is True
        # ...and `extract:text`'s markup reader converted it, exactly as it would for an
        # uploaded `.html`.
        assert evidence(node)["extraction"]["source"] == "html_markup"
        assert "<h1>" not in (node.content or "")
        assert "A Page" in (node.content or "")

    def test_the_file_block_is_the_one_an_upload_writes(self, db_session, evidence):
        response = IngestService(db_session).store_source_as_file(
            "url", "https://example.test/page"
        )
        with patch("jmfts_core.url_fetch.fetch_url", return_value=(PAGE, "text/plain")):
            drain_ingest_queue(db_session)

        node = DocumentRepository(db_session).get(response.document_id)
        block = evidence(node)["file"]
        assert set(block) == {
            "filename",
            "byte_size",
            "content_hash",
            "blob_ref",
            "declared_mime",
            "detected_mime",
            "detected_by",
            "uploaded_at",
        }
        assert block["byte_size"] == len(PAGE.encode("utf-8"))
        assert block["detected_by"] == "content_sniff"

    def test_the_tree_is_the_one_an_upload_of_the_same_bytes_gets(self, db_session, evidence):
        """The property S8 is for: from probe onward, nothing can tell the two apart.

        The upload uses text that differs by one word, because IDENTICAL bytes would
        deduplicate onto the fetched node — which is itself the point, and is asserted
        below.
        """
        service = IngestService(db_session)

        fetched = service.store_source_as_file("url", "https://example.test/page")
        with patch("jmfts_core.url_fetch.fetch_url", return_value=(PAGE, "text/plain")):
            drain_ingest_queue(db_session)
        uploaded = service.store_text_as_file(PAGE.replace("Fetched", "Uploaded"), filename="p2")
        drain_ingest_queue(db_session)

        repo = DocumentRepository(db_session)
        one, two = repo.get(fetched.document_id), repo.get(uploaded.document_id)
        assert evidence(one)["matched"]["format"] == (evidence(two)["matched"]["format"])
        # The fetched node's log has one extra entry at the front and is otherwise the
        # upload's, task for task.
        assert [e["task"] for e in evidence(one)["attempts"]] == [TASK_FETCH_URL] + [
            e["task"] for e in evidence(two)["attempts"]
        ]

    def test_an_upload_of_the_fetched_bytes_lands_on_the_fetched_node(self, db_session):
        """Deduplication does not care how the bytes got here, which is the same claim from
        the other side."""
        service = IngestService(db_session)
        fetched = service.store_source_as_file("url", "https://example.test/page")
        with patch("jmfts_core.url_fetch.fetch_url", return_value=(PAGE, "text/plain")):
            drain_ingest_queue(db_session)

        uploaded = service.store_text_as_file(PAGE, filename="page-2")

        assert uploaded.was_existing is True
        assert uploaded.document_id == fetched.document_id

    def test_a_node_with_no_source_block_fails_loudly(self, db_session, evidence):
        """A fetch task on a node that names nothing to fetch is a corrupt record, not a
        no-op."""
        node = DocumentRepository(db_session).create(
            title="no source", content=None, usetype=USETYPE_FILE, auto_embed=False
        )
        db_session.flush()
        tasks = TaskQueueRepository(db_session)
        tasks.enqueue(TASK_FETCH_URL, node.id, "self", params={})

        drain_ingest_queue(db_session)

        refreshed = DocumentRepository(db_session).get(node.id)
        failed = [
            e
            for e in evidence(refreshed)["attempts"]
            if e["task"] == TASK_FETCH_URL and e["status"] == "failed"
        ]
        assert failed, evidence(refreshed)["attempts"]
        assert "carries no `source` block" in failed[0]["error"]


class TestTheLocalPath:
    def test_a_file_on_disk_is_read_and_ingested(self, db_session, tmp_path):
        source = tmp_path / "notes.md"
        source.write_text(PAGE, encoding="utf-8")

        response = IngestService(db_session).store_source_as_file("path", str(source))
        drain_ingest_queue(db_session)

        node = DocumentRepository(db_session).get(response.document_id)
        assert node.settled == SETTLED_SETTLED
        assert BlobRepository(db_session).read_bytes(node.id) == PAGE.encode("utf-8")
        chunks = (
            db_session.execute(
                select(Document)
                .where(Document.path.contains([node.id]))
                .where(Document.usetype == USETYPE_CHUNK)
            )
            .scalars()
            .all()
        )
        assert chunks, "the local file produced no chunks"

    def test_it_is_no_longer_pdf_only(self, db_session, evidence, tmp_path):
        """`wiki:pdf` converted a local PDF to markdown and could take nothing else. The
        format is probe's decision now, so a path is a path."""
        source = tmp_path / "plain.txt"
        source.write_text("Alpha beta gamma delta epsilon. " * 20, encoding="utf-8")

        response = IngestService(db_session).store_source_as_file("path", str(source))
        drain_ingest_queue(db_session)

        node = DocumentRepository(db_session).get(response.document_id)
        assert evidence(node)["matched"]["format"] == "text"

    def test_a_missing_file_is_recorded_as_a_failure_with_the_path(
        self, db_session, evidence, tmp_path
    ):
        response = IngestService(db_session).store_source_as_file("path", str(tmp_path / "nope.md"))

        drain_ingest_queue(db_session)

        node = DocumentRepository(db_session).get(response.document_id)
        attempts = [e for e in evidence(node)["attempts"] if e["task"] == TASK_FETCH_PATH]
        assert attempts[-1]["status"] == "failed"
        assert "nope.md" in attempts[-1]["error"]


class TestThroughTheEndpoint:
    def test_wiki_path_ingests_a_local_file_and_returns_the_tree(self, db_session, tmp_path):
        source = tmp_path / "paper.md"
        source.write_text(PAGE, encoding="utf-8")

        response = _ingest(db_session, str(source), "wiki:pdf")

        assert response.usetype == "wiki:pdf"
        assert response.message_count > 0
        names = [s.stage for s in response.stages]
        assert names[0] == TASK_FETCH_PATH
        assert TASK_PROBE in names

    def test_the_usetype_decides_which_fetcher_runs(self, db_session):
        with patch("jmfts_core.url_fetch.fetch_url", return_value=(PAGE, "text/plain")) as f:
            _ingest(db_session, "https://example.test/page", "wiki:url")
        assert f.call_args.args == ("https://example.test/page",)

    def test_an_arxiv_id_reaches_the_arxiv_fetcher_and_the_pdf_stays_a_pdf(
        self, db_session, evidence
    ):
        """Path A converted the paper to markdown before storing it, so the outline, the
        page geometry and `citation` were all gone before probe saw anything. The blob is
        the PDF now and the real PDF pipeline runs."""
        pymupdf = pytest.importorskip("pymupdf")
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 100), "Alpha beta gamma delta epsilon zeta eta theta iota.")
        pdf = doc.tobytes()
        doc.close()

        with patch("jmfts_core.arxiv_fetch.fetch_arxiv_pdf", return_value=pdf) as f:
            response = _ingest(db_session, "2401.00001", "wiki:arxiv")

        assert f.call_args.args == ("2401.00001",)
        node = DocumentRepository(db_session).get(response.source_document_id)
        assert evidence(node)["matched"]["format"] == "pdf"
        assert BlobRepository(db_session).read_bytes(node.id) == pdf
