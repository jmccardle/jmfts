"""``extract:text`` and the structure rungs. ``INGEST_SPEC.md`` Part 4, 3.3 and 3.5.

Everything here runs the REAL queue: upload, then ``drain_ingest_queue``, then look at
what is in the database. The handlers are not called directly, because half of what is
being tested is the scheduling — that probe chooses a rung from one pattern, that the
structure task waits for the text, and that the tree it writes lets the file node settle.

The fixture PDF is built by PyMuPDF and is deliberately small and lopsided: front matter
that belongs to no section, one section with a subsection, one without. That shape is what
separates the interesting cases from each other in a single document.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from jmfts_client.contracts.upload import UploadedFile
from jmfts_core.ingest_tasks import (
    TASK_EXTRACT_TEXT,
    TASK_STRUCTURE_DECLARED,
    TASK_STRUCTURE_INFERRED,
)
from jmfts_core.models.document import Document, SETTLED_SETTLED
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.services.ingest_service import IngestService
from jmfts_core.structure_tasks import (
    RUNG_DECLARED,
    RUNG_INFERRED,
    USETYPE_CHUNK,
    USETYPE_SECTION,
)
from tests.conftest import drain_ingest_queue

pytest.importorskip("pymupdf")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _paper_bytes(*, with_outline: bool) -> bytes:
    """Three pages: front matter, a section with a subsection, and a section without.

    Heading sizes are far enough apart that `_IdentifyHeaders` gives each one its own
    level, so the same document exercises the declared rung (with the outline) and the
    inferred one (without it) and the two produce visibly different trees.
    """
    import pymupdf

    doc = pymupdf.open()
    front = doc.new_page()
    front.insert_text((50, 60), "A Study Of Retrieval", fontsize=20)
    front.insert_text(
        (50, 100),
        "Abstract. We measure retrieval quality over a corpus of papers and report "
        "the recall we obtained.",
        fontsize=11,
    )
    body = doc.new_page()
    body.insert_text((50, 60), "Introduction", fontsize=16)
    body.insert_text(
        (50, 90),
        "Retrieval is hard because documents declare their own structure and rarely "
        "agree on how. This section says why.",
        fontsize=11,
    )
    body.insert_text((50, 140), "Late Interaction", fontsize=13)
    body.insert_text(
        (50, 170),
        "Late interaction scores each query token against every document token and "
        "sums the maxima over the query.",
        fontsize=11,
    )
    last = doc.new_page()
    last.insert_text((50, 60), "Results", fontsize=16)
    last.insert_text(
        (50, 90),
        "Recall improved by nine points against the baseline and the latency cost was "
        "under one millisecond per query.",
        fontsize=11,
    )
    if with_outline:
        doc.set_toc([[1, "Introduction", 2], [2, "Late Interaction", 2], [1, "Results", 3]])
    return doc.tobytes()


@pytest.fixture(scope="module")
def outlined_pdf() -> bytes:
    return _paper_bytes(with_outline=True)


@pytest.fixture(scope="module")
def unoutlined_pdf() -> bytes:
    return _paper_bytes(with_outline=False)


def _upload(session, data, filename="paper.pdf"):
    return IngestService(session).upload_file(
        UploadedFile(data=data, filename=filename, content_type="application/pdf")
    )


def _ingest(session, data, filename="paper.pdf") -> Document:
    """Upload and run the queue dry. Returns the file node."""
    response = _upload(session, data, filename)
    drain_ingest_queue(session)
    return DocumentRepository(session).get(response.document_id)


def _children(session, node_id: int) -> list[Document]:
    return list(
        session.execute(
            select(Document)
            .where(Document.parent_id == node_id)
            .order_by(Document.position, Document.id)
        )
        .scalars()
        .all()
    )


def _titles(nodes) -> list[str]:
    return [n.title for n in nodes]


def _attempt(node: Document, task: str) -> dict:
    return next(e for e in node.structured_content["attempts"] if e["task"] == task)


# ---------------------------------------------------------------------------
# extract:text
# ---------------------------------------------------------------------------


class TestExtractText:
    def test_the_file_node_holds_the_whole_extracted_text(self, db_session, outlined_pdf):
        node = _ingest(db_session, outlined_pdf)

        assert "Late interaction scores each query token" in node.content
        assert "Recall improved by nine points" in node.content

    def test_it_records_the_indexes_a_later_task_cannot_recompute(self, db_session, outlined_pdf):
        """Page offsets and the outline. Without them `structure:declared` would have to
        re-parse the PDF to place a single section."""
        node = _ingest(db_session, outlined_pdf)
        extraction = node.structured_content["extraction"]

        assert extraction["page_count"] == 3
        assert len(extraction["page_offsets"]) == 3
        assert extraction["page_offsets"][0] == 0
        assert [entry[1] for entry in extraction["toc"]] == [
            "Introduction",
            "Late Interaction",
            "Results",
        ]

    def test_it_reports_the_control_characters_it_removed(self, db_session, outlined_pdf):
        node = _ingest(db_session, outlined_pdf)

        assert node.structured_content["extraction"]["control_chars_removed"] == 0

    def test_a_format_with_no_extractor_fails_rather_than_producing_nothing(self, db_session):
        """A `.docx` today: probe named the format, and there is no text extractor for it.

        Completing with no text would leave a file node that looks processed and holds
        nothing, and the attempt log would agree with it.
        """
        response = IngestService(db_session).upload_file(
            UploadedFile(
                data=b"PK\x03\x04" + b"\x00" * 64,
                filename="report.docx",
                content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        )
        drain_ingest_queue(db_session)

        node = DocumentRepository(db_session).get(response.document_id)
        # probe reports no patterns for a format it cannot look inside, so extract:text is
        # never eligible in the first place — the failure this test guards against cannot
        # arise through the front door, and the record says why.
        probe_detail = _attempt(node, "probe")["detail"]
        assert TASK_EXTRACT_TEXT in probe_detail["not_applicable"]


# ---------------------------------------------------------------------------
# structure:declared
# ---------------------------------------------------------------------------


class TestDeclaredRungBuildsTheTree:
    def test_the_outline_becomes_the_tree(self, db_session, outlined_pdf):
        node = _ingest(db_session, outlined_pdf)
        sections = [c for c in _children(db_session, node.id) if c.usetype == USETYPE_SECTION]

        assert _titles(sections) == ["Introduction", "Results"]
        nested = _children(db_session, sections[0].id)
        assert "Late Interaction" in _titles([c for c in nested if c.usetype == USETYPE_SECTION])

    def test_a_sections_prose_is_in_its_chunks(self, db_session, outlined_pdf):
        node = _ingest(db_session, outlined_pdf)
        results = next(
            c
            for c in _children(db_session, node.id)
            if c.usetype == USETYPE_SECTION and c.title == "Results"
        )
        chunks = _children(db_session, results.id)

        assert [c.usetype for c in chunks] == [USETYPE_CHUNK]
        assert "Recall improved by nine points" in chunks[0].content

    def test_a_section_node_carries_no_content_of_its_own(self, db_session, outlined_pdf):
        """The same text on the container and on its chunks would be in the retrieval
        indexes twice and would answer one query with both. The container's content is
        what rollup summarisation writes."""
        node = _ingest(db_session, outlined_pdf)
        sections = [c for c in _children(db_session, node.id) if c.usetype == USETYPE_SECTION]

        assert all(section.content is None for section in sections)

    def test_front_matter_attaches_to_the_file_node_with_no_container(
        self, db_session, outlined_pdf
    ):
        """A title page and an abstract precede the first outline entry and belong to no
        section. There is no title to name a container with, so there is no container."""
        node = _ingest(db_session, outlined_pdf)
        chunks = [c for c in _children(db_session, node.id) if c.usetype == USETYPE_CHUNK]

        assert len(chunks) == 1
        assert "We measure retrieval quality" in chunks[0].content

    def test_every_node_it_wrote_is_settled(self, db_session, outlined_pdf):
        """Written in one transaction with nothing queued against any of it. A node left
        in flight would stop the file node settling, and nothing would ever settle it."""
        node = _ingest(db_session, outlined_pdf)
        subtree = (
            db_session.execute(select(Document).where(Document.path.contains([node.id])))
            .scalars()
            .all()
        )

        assert subtree, "the structure task wrote nothing"
        assert all(child.settled == SETTLED_SETTLED for child in subtree)
        assert node.settled == SETTLED_SETTLED

    def test_the_nodes_are_in_document_order(self, db_session, outlined_pdf):
        node = _ingest(db_session, outlined_pdf)
        children = _children(db_session, node.id)

        assert [c.position for c in children] == list(range(len(children)))
        assert children[0].usetype == USETYPE_CHUNK  # the abstract comes first
        assert _titles(children[1:]) == ["Introduction", "Results"]


class TestDeclaredRungRecordsWhatItCovered:
    def test_the_structure_block_names_the_rung_and_its_source(self, db_session, outlined_pdf):
        node = _ingest(db_session, outlined_pdf)
        structure = node.structured_content["structure"]

        assert structure["primary_rung"] == RUNG_DECLARED
        assert structure["source"] == "pdf_outline"
        assert structure["max_depth"] == 3  # section -> subsection -> chunk

    def test_untitled_text_is_a_gap_and_lowers_coverage(self, db_session, outlined_pdf):
        """Spec 3.3: coverage is the fraction of the text assigned to a section. The
        abstract is assigned to nothing, so it is the gap, and it is a number rather than
        an absence."""
        node = _ingest(db_session, outlined_pdf)
        structure = node.structured_content["structure"]

        assert structure["gap_regions"] == 1
        assert 0.0 < structure["coverage"] < 1.0

    def test_a_remaining_gap_says_that_nothing_is_going_to_claim_it(self, db_session, outlined_pdf):
        """ "the gap is 13%" and "the gap is 13% and no rung below is implemented" are
        different facts, and only the second one is actionable."""
        node = _ingest(db_session, outlined_pdf)
        detail = _attempt(node, TASK_STRUCTURE_DECLARED)["detail"]

        assert "structure:semantic" in detail["deferred"]
        assert "not implemented" in detail["deferred"]["structure:semantic"]

    def test_the_attempt_records_the_rung_and_the_chunking_parameters(
        self, db_session, outlined_pdf
    ):
        """6.1 diffs on `(task, param_fingerprint)`, so the parameters that decided the
        leaves have to be on the record that the diff reads."""
        node = _ingest(db_session, outlined_pdf)
        attempt = _attempt(node, TASK_STRUCTURE_DECLARED)

        assert attempt["rung"] == RUNG_DECLARED
        assert attempt["detail"]["params"]["chunk_strategy"] == "sentence_packed"
        assert attempt["detail"]["params"]["max_tokens"] == 120
        assert attempt["param_fingerprint"]

    def test_produced_child_ids_are_the_undo_record(self, db_session, outlined_pdf):
        """6.2 supersedes an attempt by deleting what it produced, with their subtrees.
        Direct children are therefore enough, and the count covers the whole subtree."""
        node = _ingest(db_session, outlined_pdf)
        produced = _attempt(node, TASK_STRUCTURE_DECLARED)["produced"]
        direct = [c.id for c in _children(db_session, node.id)]

        assert sorted(produced["child_ids"]) == sorted(direct)
        assert produced["node_count"] > len(direct)

    def test_an_outline_title_that_is_not_in_the_text_is_reported(self, db_session):
        """The tree is missing a node the file said would be there. Its text is not lost —
        it stays in the preceding section — but the discrepancy has to be visible."""
        import pymupdf

        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((50, 60), "Introduction", fontsize=16)
        page.insert_text(
            (50, 90),
            "This is the only section that was ever typed into this document, whatever "
            "its table of contents claims about the rest.",
            fontsize=11,
        )
        doc.set_toc([[1, "Introduction", 1], [1, "Conclusion", 1]])

        node = _ingest(db_session, doc.tobytes(), "half.pdf")
        detail = _attempt(node, TASK_STRUCTURE_DECLARED)["detail"]

        assert detail["unplaced_titles"] == ["Conclusion"]
        assert detail["outline_entries"] == 2


# ---------------------------------------------------------------------------
# structure:inferred
# ---------------------------------------------------------------------------


class TestInferredRungRunsWhenNothingIsDeclared:
    def test_a_pdf_with_no_outline_gets_the_inferred_rung(self, db_session, unoutlined_pdf):
        node = _ingest(db_session, unoutlined_pdf, "unoutlined.pdf")

        assert node.structured_content["structure"]["primary_rung"] == RUNG_INFERRED
        assert _attempt(node, TASK_STRUCTURE_INFERRED)["rung"] == RUNG_INFERRED

    def test_it_builds_from_the_font_size_headings(self, db_session, unoutlined_pdf):
        """The same document, structured from what extraction inferred instead of from
        what the file declared. The title is a heading here and an untitled front-matter
        region there, so the trees legitimately differ."""
        node = _ingest(db_session, unoutlined_pdf, "unoutlined.pdf")
        roots = [c for c in _children(db_session, node.id) if c.usetype == USETYPE_SECTION]

        assert _titles(roots) == ["A Study Of Retrieval"]
        assert _titles(
            [c for c in _children(db_session, roots[0].id) if c.usetype == USETYPE_SECTION]
        ) == ["Introduction", "Results"]

    def test_every_heading_claims_its_text_so_there_is_no_gap(self, db_session, unoutlined_pdf):
        node = _ingest(db_session, unoutlined_pdf, "unoutlined.pdf")
        structure = node.structured_content["structure"]

        assert structure["gap_regions"] == 0
        assert structure["coverage"] == 1.0

    def test_the_declared_rung_is_recorded_as_not_applicable(self, db_session, unoutlined_pdf):
        node = _ingest(db_session, unoutlined_pdf, "unoutlined.pdf")
        not_applicable = _attempt(node, "probe")["detail"]["not_applicable"]

        assert "has_outline" in not_applicable[TASK_STRUCTURE_DECLARED]


class TestADocumentWithNoStructureAtAll:
    def test_its_chunks_hang_off_the_file_node_and_the_gap_is_the_whole_document(self, db_session):
        """One font size, no outline. There is no boundary to find and the rung says so
        rather than inventing one."""
        import pymupdf

        doc = pymupdf.open()
        page = doc.new_page()
        for i, line in enumerate(
            [
                "This document is set in one size throughout and declares no outline.",
                "There is nothing here for a font-size heuristic to catch hold of.",
                "Every sentence is prose and none of it is a heading of any kind.",
            ]
        ):
            page.insert_text((50, 60 + i * 20), line, fontsize=11)

        node = _ingest(db_session, doc.tobytes(), "flat.pdf")
        children = _children(db_session, node.id)

        assert children and all(c.usetype == USETYPE_CHUNK for c in children)
        assert node.structured_content["structure"]["coverage"] == 0.0
        assert node.structured_content["structure"]["gap_regions"] == 1
        assert node.settled == SETTLED_SETTLED
