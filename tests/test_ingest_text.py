"""`.md` and `.txt` ingestion — `INGEST_SPEC.md` 11.3, all three steps.

Before this, 156 of 292 files in a real corpus got a file node and nothing under it. The
three changes that fix it are a prober for text, a decode branch in `extract:text`, and a
`structure:declared` that dispatches on what produced the text instead of assuming a PDF.
Each has its own class below, and the last two run the REAL queue — upload, drain, look at
the database — because half of what is being tested is that the same scheduling table
routes a `.md` file without being told about one.

The fourth class is the hazard 11.3 names. This step turns loud failures into silent
successes: an HTML file used to produce an obviously empty node, and a decoder pointed at
it would produce a plausible tree full of tags. `has_markup` is what keeps it out, and
`TestMarkupIsNotProse` is what keeps `has_markup` honest.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from jmfts_client.contracts.upload import UploadedFile
from jmfts_core.ingest_tasks import (
    TASK_EXTRACT_TEXT,
    TASK_PROBE,
    TASK_STRUCTURE_DECLARED,
    TASK_STRUCTURE_INFERRED,
    explain_plan,
)
from jmfts_core.models.document import SETTLED_SETTLED, Document
from jmfts_core.probe import detect_format, probe_patterns
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.evidence import EvidenceRepository
from jmfts_core.services.ingest_service import IngestService
from jmfts_core.structural_splitting import find_headings, split_on_headings
from jmfts_core.structure_tasks import (
    EXTRACTION_HTML_MARKUP,
    EXTRACTION_UTF8_TEXT,
    RUNG_DECLARED,
    RUNG_INFERRED,
    SOURCE_ATX_HEADINGS,
    USETYPE_CHUNK,
    USETYPE_SECTION,
    Boundaries,
    _run_rung,
)
from jmfts_core.task_errors import ErrorType, classify_exception
from tests.conftest import drain_ingest_queue

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

#: An authored markdown document: front matter with no heading, one section, one
#: subsection, one more section. The same lopsided shape the PDF fixtures use, for the same
#: reason — it separates the interesting cases from each other inside one document.
MARKDOWN = """An opening paragraph that belongs to no section at all, long enough to be
worth a chunk of its own once the chunker has packed it.

# Introduction

Retrieval is hard because documents declare their own structure and rarely agree on how.
This section says why, at enough length to survive the minimum chunk length.

## Late Interaction

Late interaction scores each query token against every document token and sums the maxima
over the query, which is the operation this appliance is built around.

# Results

Recall improved by nine points against the baseline and the latency cost was under one
millisecond per query, measured over the corpus described above.
"""

#: A `.txt` file: prose, no markers, no structure to declare. It is 11.3's degenerate
#: markdown, and the reason the inferred rung has to accept a text file at all.
PLAIN_TEXT = """Notes from the meeting on scheduling.

We agreed that the queue is the target and that the synchronous path is deprecated. The
remaining question was whether re-ingest should come before an end-to-end path for plain
documents, and it should not.

A second paragraph so that the chunker has more than one thing to do with this file.
"""

HTML = """<!DOCTYPE html>
<html lang="en">
  <head><title>A page</title></head>
  <body>
    <h1>A heading that is not an ATX heading</h1>
    <p>Prose wrapped in tags, which a decoder would hand to the chunker verbatim.</p>
  </body>
</html>
"""


def _probe(data: bytes, filename: str = "notes.md") -> dict:
    """The patterns `probe` would measure, without any of the queue around it."""
    detection = detect_format(data, filename=filename)
    patterns, _detail = probe_patterns(data, detection)
    return patterns


def _ingest(session, text: str, filename: str) -> Document:
    """Upload the text and run the queue dry. Returns the file node."""
    response = IngestService(session).upload_file(
        UploadedFile(data=text.encode("utf-8"), filename=filename, content_type="text/plain")
    )
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


def _attempt(session, node: Document, task: str) -> dict:
    """One entry from the node's durable attempt log, which is an evidence row since 2b."""
    log = DocumentRepository(session).attempt_log(node)
    return next(e for e in log if e["task"] == task)


# ---------------------------------------------------------------------------
# Step 1 — the prober
# ---------------------------------------------------------------------------


class TestTheTextProber:
    def test_it_measures_the_three_facts_the_table_reads(self):
        patterns = _probe(MARKDOWN.encode("utf-8"))
        assert patterns["has_text_layer"] is True
        assert patterns["has_headings"] is True
        assert patterns["has_markup"] is False

    def test_a_file_with_no_headings_declares_no_structure(self):
        patterns = _probe(PLAIN_TEXT.encode("utf-8"), "notes.txt")
        assert patterns["has_text_layer"] is True
        assert patterns["has_headings"] is False
        assert patterns["heading_count"] == 0
        assert patterns["max_heading_level"] == 0

    def test_the_heading_count_is_the_splitter_s_own_count(self):
        """The paired measurement of 11.3, at its source.

        The prober and the splitter must count the same things, or the disagreement the
        structure task reports would be a false alarm rather than the real signal it is
        for. They share `find_headings`, and this is what says so.
        """
        patterns = _probe(MARKDOWN.encode("utf-8"))
        assert patterns["heading_count"] == len(find_headings(MARKDOWN))
        assert patterns["heading_count"] == sum(1 for s in split_on_headings(MARKDOWN) if s.title)
        assert patterns["max_heading_level"] == 2

    def test_whitespace_is_not_a_text_layer(self):
        """A file that decodes to nothing has been read correctly and holds no document."""
        patterns = _probe(b"   \n\n\t\n  ", "empty.txt")
        assert patterns["has_text_layer"] is False
        assert patterns["char_count"] == 9

    def test_bytes_that_do_not_decode_fail_permanently(self):
        """`.text` on the name and Latin-1 in the bytes: `detect_format` falls back to the
        extension, and the prober refuses rather than guessing an encoding.

        Classified PERMANENT — `UnicodeDecodeError` is a `ValueError` — so the node records
        the failure once instead of spending three retries on bytes that will not decode on
        the third attempt either.
        """
        data = "café".encode("latin-1")
        detection = detect_format(data, filename="notes.text")
        assert detection.format == "text"

        with pytest.raises(UnicodeDecodeError) as caught:
            probe_patterns(data, detection)
        assert classify_exception(caught.value) is ErrorType.PERMANENT

    def test_the_detail_carries_what_the_booleans_were_derived_from(self):
        detection = detect_format(MARKDOWN.encode("utf-8"), filename="notes.md")
        _patterns, detail = probe_patterns(MARKDOWN.encode("utf-8"), detection)
        assert detail["byte_length"] == len(MARKDOWN.encode("utf-8"))
        assert detail["nonblank_chars"] == len(MARKDOWN.strip())
        assert detail["heading_levels"] == [1, 2]
        assert detail["markup_tags_in_prefix"] == 0
        assert "no_prober_for_format" not in detail


# ---------------------------------------------------------------------------
# The plan, before anything is stored
# ---------------------------------------------------------------------------


class TestThePlanForATextFile:
    def _outcomes(self, data: bytes, filename: str) -> dict:
        detection = detect_format(data, filename=filename)
        patterns, _ = probe_patterns(data, detection)
        plan = explain_plan(detection.format, patterns=patterns)
        return {task.task: task.outcome for task in plan.tasks}

    def test_markdown_reaches_the_declared_rung(self):
        outcomes = self._outcomes(MARKDOWN.encode("utf-8"), "notes.md")
        assert outcomes[TASK_PROBE] == "enqueued"
        assert outcomes[TASK_EXTRACT_TEXT] == "enqueued"
        assert outcomes[TASK_STRUCTURE_DECLARED] == "enqueued"
        assert outcomes[TASK_STRUCTURE_INFERRED] == "not_applicable"

    def test_plain_text_reaches_the_inferred_rung(self):
        outcomes = self._outcomes(PLAIN_TEXT.encode("utf-8"), "notes.txt")
        assert outcomes[TASK_EXTRACT_TEXT] == "enqueued"
        assert outcomes[TASK_STRUCTURE_DECLARED] == "not_applicable"
        assert outcomes[TASK_STRUCTURE_INFERRED] == "enqueued"


# ---------------------------------------------------------------------------
# Step 2 and 3 — the decode branch, and the rung that dispatches on it
# ---------------------------------------------------------------------------


class TestMarkdownEndToEnd:
    @pytest.fixture
    def node(self, db_session) -> Document:
        return _ingest(db_session, MARKDOWN, "notes.md")

    def test_the_file_node_holds_the_bytes_as_text(self, node):
        assert node.content == MARKDOWN

    def test_the_extraction_record_names_the_decoder(self, node, evidence):
        extraction = evidence(node)["extraction"]
        assert extraction["source"] == EXTRACTION_UTF8_TEXT
        assert extraction["characters"] == len(MARKDOWN)
        # 11.3's table: no pages, and the headings stay in the text rather than being
        # copied into a second record that could disagree with it.
        assert extraction["toc"] == []
        assert "page_offsets" not in extraction

    def test_the_declared_rung_ran_over_the_author_s_own_headings(self, db_session, node, evidence):
        structure = evidence(node)["structure"]
        assert structure["primary_rung"] == RUNG_DECLARED
        assert structure["source"] == SOURCE_ATX_HEADINGS
        assert _attempt(db_session, node, TASK_STRUCTURE_DECLARED)["rung"] == RUNG_DECLARED

    def test_the_tree_is_the_one_the_document_declares(self, db_session, node):
        children = _children(db_session, node.id)
        sections = [c for c in children if c.usetype == USETYPE_SECTION]
        assert [s.title for s in sections] == ["Introduction", "Results"]

        introduction = sections[0]
        nested = [c for c in _children(db_session, introduction.id) if c.usetype == USETYPE_SECTION]
        assert [s.title for s in nested] == ["Late Interaction"]

        # The front matter has no heading to name a container with, so its chunks attach
        # to the file node directly — the same rule a PDF title page follows.
        assert any(c.usetype == USETYPE_CHUNK for c in children)

    def test_a_section_container_holds_no_content(self, db_session, node):
        sections = [c for c in _children(db_session, node.id) if c.usetype == USETYPE_SECTION]
        assert all(s.content is None for s in sections)

    def test_the_node_settles(self, node):
        assert node.settled == SETTLED_SETTLED

    def test_the_extraction_attempt_pairs_the_yield_with_the_probe(self, db_session, node):
        """11.3's hazard, answered with two numbers rather than a threshold."""
        detail = _attempt(db_session, node, TASK_EXTRACT_TEXT)["detail"]
        assert detail["source"] == EXTRACTION_UTF8_TEXT
        assert detail["bytes_in"] == len(MARKDOWN.encode("utf-8"))
        assert detail["characters"] == len(MARKDOWN)
        assert detail["characters_probed"] == len(MARKDOWN)

    def test_the_structure_attempt_pairs_its_sections_with_the_probe(self, db_session, node):
        detail = _attempt(db_session, node, TASK_STRUCTURE_DECLARED)["detail"]
        assert detail["headings_probed"] == 3
        assert detail["sections_titled"] == 3


class TestPlainTextEndToEnd:
    @pytest.fixture
    def node(self, db_session) -> Document:
        return _ingest(db_session, PLAIN_TEXT, "notes.txt")

    def test_it_becomes_one_untitled_region_of_chunks(self, db_session, node):
        children = _children(db_session, node.id)
        assert children
        assert all(c.usetype == USETYPE_CHUNK for c in children)

    def test_the_gap_is_recorded_rather_than_papered_over(self, node, evidence):
        """A `.txt` file has no structure, and that is a measurement, not a failure.

        `coverage: 0.0` with one gap region is the honest description of a document
        nothing could attribute to a section, and it is what the lower rungs — and 11.4's
        segmentation — exist to claim later.
        """
        structure = evidence(node)["structure"]
        assert structure["primary_rung"] == RUNG_INFERRED
        assert structure["coverage"] == 0.0
        assert structure["gap_regions"] == 1

    def test_the_node_settles(self, node):
        assert node.settled == SETTLED_SETTLED


# ---------------------------------------------------------------------------
# The hazard: markup must not be read as prose
# ---------------------------------------------------------------------------


class TestMarkupIsNotProse:
    def test_html_selects_the_markup_reader_rather_than_blocking_the_row(self):
        """`has_markup` chooses an extractor; it no longer cancels extraction.

        It used to be a `forbids` on the `extract:text` row, which made an HTML file settle
        with no content and no children while the upload returned 200 and every task
        reported "completed". The prohibition was right about the hazard — decoding HTML
        yields HTML — and wrong about the remedy, because the appliance now has a reader
        that converts instead of one that refuses.
        """
        patterns = _probe(HTML.encode("utf-8"), "page.html")
        assert patterns["has_markup"] is True
        assert patterns["has_text_layer"] is True

        plan = explain_plan("text", patterns=patterns)
        extract = next(t for t in plan.tasks if t.task == TASK_EXTRACT_TEXT)
        assert extract.outcome == "enqueued"

    def test_an_html_file_produces_prose_and_children(self, db_session, evidence):
        node = _ingest(db_session, HTML, "page.html")
        assert node.settled == SETTLED_SETTLED
        assert node.content, "an HTML file must now yield markdown, not nothing"
        assert "<" not in node.content, "tags must not survive into the stored prose"
        assert _children(db_session, node.id) != []

        extraction = evidence(node)["extraction"]
        assert extraction["source"] == EXTRACTION_HTML_MARKUP

        # Both sides of the conversion are recorded — in the ATTEMPT detail, which is where
        # `Extracted.detail` is merged, not in the extraction record. A collapse is then
        # visible as two numbers that disagree rather than inferred from a ratio nobody
        # stored. For HTML they are EXPECTED to differ: tags leave.
        detail = _attempt(db_session, node, TASK_EXTRACT_TEXT)["detail"]
        assert detail["html_chars"] > detail["markdown_chars"] > 0

    def test_markdown_opening_with_a_comment_is_not_markup(self):
        """The regression that cost a whole ingest run.

        Markdown has no comment syntax, so `<!-- ... -->` is how a markdown file carries a
        lint directive or a licence header above its first heading. Such a file opened with
        `<`, was called markup, and — before the reader existed — produced zero chunks with
        every task reporting success.
        """
        for opener in (
            "<!-- markdownlint-disable MD013 -->",
            "<!-- prettier-ignore -->",
            "<!--\nCopyright 2026\nSPDX: MIT\n-->",
        ):
            document = f"{opener}\n\n# Title\n\nA paragraph of ordinary prose.\n"
            patterns = _probe(document.encode("utf-8"))
            assert patterns["has_markup"] is False, opener
            assert patterns["has_headings"] is True, opener

    def test_a_comment_cannot_supply_the_tags_that_prove_markup(self):
        """Stripping comments removes them from the COUNT as well as from the opener.

        A long comment full of tag-like text would otherwise out-vote the document it sits
        above, which is the same misclassification arriving by the other door.
        """
        document = "<!-- <html><body><p><div><span> -->\n\n# Title\n\nProse.\n"
        patterns = _probe(document.encode("utf-8"))
        assert patterns["has_markup"] is False

    def test_prose_that_quotes_a_tag_is_still_prose(self):
        """The opening character carries the claim, not the presence of a tag anywhere.

        A markdown document explaining HTML mentions tags constantly, and calling it markup
        would take the whole corpus this feature exists for away again.
        """
        document = "# On markup\n\nWrite `<html>` when you mean it, and <p> otherwise.\n"
        patterns = _probe(document.encode("utf-8"))
        assert patterns["has_markup"] is False
        assert patterns["has_headings"] is True

    def test_a_lone_angle_bracket_is_not_markup(self):
        patterns = _probe(b"<- this points at the previous line, and nothing else does.\n")
        assert patterns["has_markup"] is False


# ---------------------------------------------------------------------------
# The dispatch itself
# ---------------------------------------------------------------------------


class TestTheRungDispatch:
    def test_an_unknown_extraction_source_raises_and_names_itself(self, db_session):
        """Fail Early at the seam 11.3 creates.

        A new entry point that adds an extractor and forgets the splitter would otherwise
        get whichever splitter happened to be first. The message says what it read and what
        the rung knows how to read.
        """
        node = _ingest(db_session, MARKDOWN, "notes.md")
        repo = EvidenceRepository(db_session)
        repo.write(
            node.id, "extraction", {**repo.read(node.id, "extraction"), "source": "epub_spine"}
        )
        db_session.flush()

        class _Task:
            scope_document_id = node.id
            params: dict = {}

        with pytest.raises(ValueError) as caught:
            _run_rung(
                db_session,
                _Task(),
                TASK_STRUCTURE_DECLARED,
                {EXTRACTION_UTF8_TEXT: lambda doc, extraction: Boundaries([], "x")},
                RUNG_DECLARED,
            )
        assert "epub_spine" in str(caught.value)
        assert EXTRACTION_UTF8_TEXT in str(caught.value)
