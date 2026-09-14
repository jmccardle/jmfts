"""A rule's scope, the ``produced_by`` stamp, and the one planner. ``SPRINT_JOBS.md`` Phase 3.

Three things land together here and each one is only useful because of the other two.

**Scope** (4.1) lets a row name nodes other than the uploaded file. Before it, a task
scoped to a sheet or a chunk had nowhere to be declared and was written as a literal
``TaskSpec`` inside a handler — which is Part 0's second planner, and the reason the sheet
knobs were unreachable, the ``EXPLAIN`` for a workbook stopped at the sheet list, and
``sheet_tasks``' own claim about ``param_fingerprint`` described a fingerprint no request
could move.

**``produced_by``** (4.2) is what makes a scope resolvable. A multiplicity gives a number;
a scope needs an identity, and no other column carries one — ``usetype`` says what a node
IS, and one structure rung writes both ``section`` and ``chunk``.

**The planner** (6.4) is :func:`~jmfts_core.ingest_tasks.plan_frontier`, called from the
five sites that used to carry a literal spec. The enqueue call survives, because the
settling walk travels upward and a freshly created child is unreachable from below; what
does not survive is the hand-written list of what to enqueue.

The tests below are grouped by which of the three they would falsify. What is deliberately
NOT here is 9.1's re-run matching and 9.3's per-vertex write mode: nothing re-runs a
fan-out rule yet — the attempt diff still prevents it — so a test of either would be a test
of code with no caller.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from jmfts_client.contracts.document import DocumentResponse
from jmfts_client.contracts.upload import UploadedFile
from jmfts_core.ingest_options import resolve_options
from jmfts_core.ingest_tasks import (
    ROOT_SCOPE,
    SCOPE_CHILDREN,
    SCOPE_ROOT,
    TASK_EMBED,
    TASK_EXTRACT_SHEET,
    TASK_PROFILE_SHEET,
    TASK_ROWS,
    TASK_STRUCTURE_CONVERSATION,
    TASK_STRUCTURE_DECLARED,
    TASK_STRUCTURE_INFERRED,
    TASK_STRUCTURE_SHEETS,
    Frontier,
    Scope,
    _check_task_rows,
    children_of,
    enqueue_frontier,
    plan_frontier,
)
from jmfts_core.models.document import (
    Document,
    USETYPE_CHUNK,
    USETYPE_FILE,
    USETYPE_RECORD,
    USETYPE_SECTION,
    USETYPE_SHEET,
    USETYPE_PROFILE,
    USETYPE_TABLE,
)
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.task_queue import TaskQueueRepository
from jmfts_core.services.document_service import DocumentService
from jmfts_core.services.ingest_service import IngestService
from jmfts_core.sql import migration_sql, schema_sql
from tests.conftest import drain_ingest_queue

MARKDOWN = b"""# Retrieval

Late interaction scores each query token against every document token and sums the maxima
over the query. That is more expensive than a single dot product, and it is why the token
vectors are stored at 256 dimensions rather than at the model's full width.

# Segmentation

PELT finds changepoints in a sequence rather than clusters in a set, so a container it
creates holds a contiguous span of the document and the reading order survives the level
it added.
"""


def _ingest(session, data: bytes, filename: str, mime: str) -> Document:
    response = IngestService(session).upload_file(
        UploadedFile(data=data, filename=filename, content_type=mime)
    )
    drain_ingest_queue(session)
    return DocumentRepository(session).get(response.document_id)


def _subtree(session, node: Document) -> list[Document]:
    return list(
        session.execute(select(Document).where(Document.path.contains([node.id]))).scalars().all()
    )


def _row(task: str):
    return next(row for row in TASK_ROWS if row.task == task)


# ---------------------------------------------------------------------------
# 1. The vocabulary — what a scope may and may not say
# ---------------------------------------------------------------------------


class TestTheScopeVocabulary:
    def test_the_root_scope_names_no_producer_and_no_usetype(self):
        """It is one node — the binding root, which for file ingestion is the file node
        `probe` ran on. A producer or a usetype on it would be describing a set."""
        assert ROOT_SCOPE.kind == SCOPE_ROOT
        assert ROOT_SCOPE.produced_by == () and ROOT_SCOPE.usetypes == ()
        with pytest.raises(ValueError, match="names no producer"):
            Scope(SCOPE_ROOT, produced_by=("probe",))

    def test_a_children_scope_requires_both_halves(self):
        """Neither defaults, and both refusals are the same refusal: a scope that matches
        more nodes than the rule meant is work enqueued onto a node that cannot hold it. No
        producer matches every node in the tree; no usetype matches both kinds a rung
        writes."""
        with pytest.raises(ValueError, match="at least one producing rule"):
            Scope(SCOPE_CHILDREN, usetypes=(USETYPE_CHUNK,))
        with pytest.raises(ValueError, match="at least one child usetype"):
            Scope(SCOPE_CHILDREN, produced_by=(TASK_STRUCTURE_DECLARED,))

    def test_an_unknown_kind_is_refused(self):
        with pytest.raises(ValueError, match="unknown scope kind"):
            Scope("grandchildren", produced_by=("x",), usetypes=("y",))

    def test_an_unstamped_node_is_in_no_rule_s_scope(self):
        """4.2: NULL means asserted. A node a person wrote belongs to no rule's output, so
        no rule reschedules work over it — which is also what makes 9.4's clearing work."""
        scope = children_of(TASK_STRUCTURE_DECLARED, usetypes=(USETYPE_CHUNK,))
        assert scope.matches(TASK_STRUCTURE_DECLARED, USETYPE_CHUNK)
        assert not scope.matches(None, USETYPE_CHUNK)
        assert not scope.matches(TASK_STRUCTURE_DECLARED, None)

    def test_both_halves_are_disjunctions(self):
        """`embed` is one row over five rules and five leaf kinds, and `Scope`'s docstring
        argues why: five near-identical rows is the copy-drift `DECLARED_STRUCTURE` refuses
        for thirteen formats.

        FOUR AND NOT THREE since `cell` joined on 2026-09-08 — a spreadsheet row too long to
        embed becomes a container over its columns, and each column is a leaf carrying its
        own text (`sheet_records.plan_record`). FIVE AND NOT FOUR since `table` joined with
        Block C step 11: 8.4's `small_table` writes one node holding a whole worksheet, and
        it is the one leaf here whose text routinely does NOT fit the token/maxsim window —
        52.0% of open-web sheets render inside 8192 tokens and not inside 512, which is why
        `sheet_tasks._write_table` sets `with_tokens` false on its queue row rather than the
        row naming a second value. The cross-product widens each time and still costs
        nothing: `profile:sheet` writes no cell and no table, and a row only fires for a
        child that exists.
        """
        scope = _row(TASK_EMBED).scope
        assert len(scope.produced_by) == 5 and len(scope.usetypes) == 5
        assert scope.matches(TASK_STRUCTURE_CONVERSATION, USETYPE_CHUNK)
        assert scope.matches(TASK_EXTRACT_SHEET, USETYPE_RECORD)
        assert scope.matches(TASK_EXTRACT_SHEET, USETYPE_TABLE)
        assert scope.matches(TASK_PROFILE_SHEET, USETYPE_PROFILE)

    def test_a_scope_prints_as_something_a_person_can_read(self):
        """It goes on the wire, in `ExplainedTaskResponse.scope`, so it has to say which
        nodes without the reader holding the table."""
        assert str(ROOT_SCOPE) == "@root"
        assert (
            str(children_of(TASK_STRUCTURE_SHEETS, usetypes=(USETYPE_SHEET,)))
            == "@children_of(structure:sheets):sheet"
        )


# ---------------------------------------------------------------------------
# 2. The table's own audit — the four things `_check_task_rows` refuses
# ---------------------------------------------------------------------------


class TestTheTableAudit:
    """Each of these is a schedule that would come out wrong, not a tidiness rule.

    The check runs at import, so the shipped table is already known to pass; what these
    assert is that it would REFUSE the four mistakes, which is the part a passing import
    cannot demonstrate.
    """

    def _with(self, rows, monkeypatch):
        monkeypatch.setattr("jmfts_core.ingest_tasks.TASK_ROWS", tuple(rows))
        return _check_task_rows

    def test_the_shipped_table_passes(self):
        _check_task_rows()

    def test_two_rows_under_one_name_are_refused(self, monkeypatch):
        """Every outcome the table reports is keyed by task name, so one of the two would
        be invisible in the plan, in the attempt log and in EXPLAIN alike."""
        duplicate = _row(TASK_EMBED)
        check = self._with(list(TASK_ROWS) + [duplicate], monkeypatch)
        with pytest.raises(ValueError, match="two rows in TASK_ROWS"):
            check()

    def test_a_row_scoped_to_a_producer_below_it_is_refused(self, monkeypatch):
        """A scope decides eligibility from the producing row's, which has to be decided
        first — the whole reason one pass over the table can resolve every scope."""
        rows = [r for r in TASK_ROWS if r.task != TASK_STRUCTURE_SHEETS]
        rows.append(_row(TASK_STRUCTURE_SHEETS))
        check = self._with(rows, monkeypatch)
        with pytest.raises(ValueError, match="not above it in TASK_ROWS"):
            check()

    def test_an_ordering_across_scopes_is_refused(self, monkeypatch):
        """`after` orders two tasks on ONE node and `enqueue_batch` resolves it to an id in
        the same batch. Naming a task on another node has no id to resolve to."""
        import dataclasses

        broken = dataclasses.replace(_row(TASK_EXTRACT_SHEET), after=(TASK_STRUCTURE_SHEETS,))
        rows = [broken if r.task == TASK_EXTRACT_SHEET else r for r in TASK_ROWS]
        check = self._with(rows, monkeypatch)
        with pytest.raises(ValueError, match="on different ones"):
            check()

    def test_a_params_key_naming_no_group_is_refused(self, monkeypatch):
        """A row naming a group nobody declared would be planned with no parameters, and
        the handler would supply its own numbers while the log recorded the empty dict as
        what was asked for."""
        import dataclasses

        broken = dataclasses.replace(_row(TASK_EMBED), params_key="vectors")
        rows = [broken if r.task == TASK_EMBED else r for r in TASK_ROWS]
        check = self._with(rows, monkeypatch)
        with pytest.raises(ValueError, match="not in TASK_PARAM_DEFAULTS"):
            check()


# ---------------------------------------------------------------------------
# 3. `produced_by` — the column, the stamp, and what clears it
# ---------------------------------------------------------------------------


class TestTheStamp:
    def test_schema_and_migration_agree_about_the_column(self):
        """`schema.sql` is the complete current schema and 016 is how an old database gets
        there. A column in one and not the other is a fresh install that behaves
        differently from a migrated one."""
        schema = schema_sql()
        migration = migration_sql("016_document_produced_by.sql")
        assert "produced_by VARCHAR(100)" in schema
        assert "idx_documents_produced_by" in schema
        assert "ADD COLUMN IF NOT EXISTS produced_by VARCHAR(100)" in migration
        assert "idx_documents_produced_by" in migration

    def test_the_migration_backfills_nothing(self, db_session):
        """NULL means asserted, and a node written before the stamp existed has no honest
        value. Inventing one would claim, for every chunk already in the store, that a
        re-run may delete and rebuild it (9.1)."""
        assert "UPDATE documents" not in migration_sql("016_document_produced_by.sql")

    def test_an_ordinary_create_is_unstamped(self, db_session):
        """The default is ASSERTED and that is correct for every caller outside the ingest
        handlers — a person, an importer, an upload."""
        doc = DocumentRepository(db_session).create(
            title="hand written", content="a note", auto_embed=False
        )
        db_session.flush()
        assert doc.produced_by is None

    def test_the_file_node_itself_is_unstamped(self, db_session):
        """It is created by the UPLOAD, which is not a rule. Nothing is scoped to a file
        node's producer, and a stamp would say a rule wrote bytes a person sent."""
        node = _ingest(db_session, MARKDOWN, "retrieval.md", "text/markdown")
        assert node.usetype == USETYPE_FILE
        assert node.produced_by is None

    def test_every_node_the_rungs_wrote_names_the_rule_that_wrote_it(self, db_session):
        """The rung, not the RUNG NAME. `structure.primary_rung` is 3.5's evidence grade —
        `declared` or `inferred` — and both rules write both grades; only this column says
        which of the two rules did it."""
        node = _ingest(db_session, MARKDOWN, "retrieval.md", "text/markdown")
        written = _subtree(db_session, node)

        assert written, "the rung wrote nothing"
        for child in written:
            assert child.produced_by in {TASK_STRUCTURE_DECLARED, TASK_STRUCTURE_INFERRED}
            assert child.usetype in {USETYPE_SECTION, USETYPE_CHUNK}

    def test_a_person_editing_the_content_clears_the_stamp(self, db_session):
        """9.4. Idempotence cannot tell "my output changed" from "somebody edited my
        output", so a re-run would silently overwrite the edit. Clearing the stamp takes
        the node out of the rule's match set: the re-run neither keeps it nor deletes it,
        and creates a sibling instead."""
        node = _ingest(db_session, MARKDOWN, "retrieval.md", "text/markdown")
        chunk = next(c for c in _subtree(db_session, node) if c.usetype == USETYPE_CHUNK)
        assert chunk.produced_by is not None

        DocumentRepository(db_session).update(
            chunk.id, content="a person rewrote this passage.", re_embed=False
        )
        db_session.flush()
        assert chunk.produced_by is None

    def test_retitling_a_produced_node_keeps_the_stamp(self, db_session):
        """Only `content`, and not `title`, `usetype` or `structured_content`. What a rule
        produced is the TEXT of the node; retitling or tagging it is metadata about a node
        the rule still owns, and clearing the stamp would drop it out of every future pass
        over its own tree."""
        node = _ingest(db_session, MARKDOWN, "retrieval.md", "text/markdown")
        chunk = next(c for c in _subtree(db_session, node) if c.usetype == USETYPE_CHUNK)
        stamp = chunk.produced_by

        DocumentRepository(db_session).update(
            chunk.id, title="renamed", structured_content={"tag": "q3"}, re_embed=False
        )
        db_session.flush()
        assert chunk.produced_by == stamp

    def test_the_response_carries_it(self, db_session):
        """A column on the row, so it costs no join — unlike evidence, which is why that
        went to a route and this is a field. "Did a machine write this, and which one" is a
        question about the node itself."""
        node = _ingest(db_session, MARKDOWN, "retrieval.md", "text/markdown")
        chunk = next(c for c in _subtree(db_session, node) if c.usetype == USETYPE_CHUNK)

        assert DocumentResponse.from_document(chunk).produced_by == chunk.produced_by
        assert DocumentResponse.from_document(node).produced_by is None
        assert chunk.to_dict()["produced_by"] == chunk.produced_by


# ---------------------------------------------------------------------------
# 4. The planner — one function, five callers, and the check that keeps them honest
# ---------------------------------------------------------------------------


class TestTheFrontierPlanner:
    def test_a_chunk_s_frontier_is_embed(self, db_session):
        node = _ingest(db_session, MARKDOWN, "retrieval.md", "text/markdown")
        frontier = plan_frontier(
            db_session, node, produced_by=TASK_STRUCTURE_DECLARED, usetype=USETYPE_CHUNK
        )

        assert [spec.task_type for spec in frontier.specs] == [TASK_EMBED]
        assert frontier.specs[0].params == resolve_options("text")["embed"]
        assert frontier.in_flight is True

    def test_a_section_s_frontier_is_empty_and_that_is_an_answer(self, db_session):
        """A container holds no text of its own; its `effective_content` comes from the
        rollup, over children that have already embedded. Asking anyway is what makes the
        answer non-empty the day a rule is scoped there, rather than a container that
        quietly gets nothing because the writer never asked."""
        node = _ingest(db_session, MARKDOWN, "retrieval.md", "text/markdown")
        frontier = plan_frontier(
            db_session, node, produced_by=TASK_STRUCTURE_DECLARED, usetype=USETYPE_SECTION
        )

        assert frontier.specs == ()
        assert frontier.in_flight is False

    def test_it_reads_the_options_the_upload_froze(self, db_session):
        """Not the profile defaults as they stand now. The options were resolved and
        validated before any of this tree existed, so a fan-out schedules its children
        under what the upload asked for."""
        response = IngestService(db_session).upload_file(
            UploadedFile(data=MARKDOWN, filename="retrieval.md", content_type="text/markdown"),
            options={"embed": {"with_tokens": False}},
        )
        drain_ingest_queue(db_session)
        node = DocumentRepository(db_session).get(response.document_id)

        frontier = plan_frontier(
            db_session, node, produced_by=TASK_STRUCTURE_DECLARED, usetype=USETYPE_CHUNK
        )
        assert frontier.specs[0].params == {"with_tokens": False}

    def test_it_finds_the_inputs_on_the_nearest_ancestor(self, db_session):
        """`matched` and `options` are on the file node, and a fan-out happens further
        down. Nearest-ancestor rather than `path[0]`, because an upload may name a
        `parent_id` and the root of the path is then the caller's own folder node."""
        node = _ingest(db_session, MARKDOWN, "retrieval.md", "text/markdown")
        section = next(c for c in _subtree(db_session, node) if c.usetype == USETYPE_SECTION)

        frontier = plan_frontier(
            db_session, section, produced_by=TASK_STRUCTURE_DECLARED, usetype=USETYPE_CHUNK
        )
        assert [spec.task_type for spec in frontier.specs] == [TASK_EMBED]

    def test_a_tree_the_file_pipeline_did_not_build_gets_nothing(self, db_session):
        """Not a fallback. No `matched` block means nobody probed these bytes, and Part 4's
        conditions are predicates over patterns that were never measured — scheduling from
        them is the confident wrong answer 11.2 refuses for EXPLAIN."""
        folder = DocumentRepository(db_session).create(title="a folder", auto_embed=False)
        db_session.flush()

        frontier = plan_frontier(
            db_session, folder, produced_by=TASK_STRUCTURE_DECLARED, usetype=USETYPE_CHUNK
        )
        assert frontier.specs == ()

    def test_enqueueing_onto_the_wrong_kind_of_child_raises(self, db_session):
        """Part 14's ratchet. A frontier is planned once and used N times, so the pair it
        was planned for and the pair the node was stamped with are two statements made a
        few lines apart. If they drift the child gets another kind of node's batch, and
        nothing downstream could tell — the tasks would run, fail on evidence they cannot
        find, and read as a broken document."""
        node = _ingest(db_session, MARKDOWN, "retrieval.md", "text/markdown")
        chunk = next(c for c in _subtree(db_session, node) if c.usetype == USETYPE_CHUNK)
        wrong = Frontier(produced_by=TASK_EXTRACT_SHEET, usetype=USETYPE_RECORD)

        with pytest.raises(ValueError, match="another kind of node's batch"):
            enqueue_frontier(TaskQueueRepository(db_session), chunk, wrong)

    def test_no_handler_writes_its_own_spec_any_more(self):
        """Part 0's six literal `enqueue_batch` sites, gone. What is left is `enqueue_batch`
        itself and the rollup planner's call — 6.4 keeps the enqueue where the child is
        created, because the settling walk travels upward and never descends."""
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent / "jmfts_core"
        offenders = []
        for path in ("sheet_tasks.py", "structure_tasks.py", "conversation_tasks.py"):
            body = (root / path).read_text()
            if "TaskSpec(" in body:
                offenders.append(path)
        assert offenders == [], (
            f"{offenders} construct a TaskSpec. A task scoped to a node a handler creates "
            "is a row of TASK_ROWS with a scope (SPRINT_JOBS.md 4.1), not a literal here"
        )


# ---------------------------------------------------------------------------
# 5. The four consequences Part 0 measured, over a real workbook
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def workbook_bytes() -> bytes:
    openpyxl = pytest.importorskip("openpyxl", reason="the office extra is not installed")
    import io

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Deals"
    sheet.append(["Deal ID", "Account", "Value"])
    sheet.append(["D-1", "Northwind", 128000])
    sheet.append(["D-2", "Contoso", 4200])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class TestTheSheetTierThroughTheQueue:
    def test_the_sheet_nodes_and_everything_under_them_are_stamped(
        self, db_session, workbook_bytes
    ):
        """One rule per kind of node, all the way down: the rung stamps the sheets, the
        profile stamps its summary, the extraction stamps its records."""
        node = _ingest(db_session, workbook_bytes, "deals.xlsx", XLSX_MIME)
        stamps = {
            child.usetype: child.produced_by
            for child in _subtree(db_session, node)
            if child.produced_by is not None
        }
        assert stamps[USETYPE_SHEET] == TASK_STRUCTURE_SHEETS
        assert stamps[USETYPE_PROFILE] == TASK_PROFILE_SHEET
        assert stamps[USETYPE_RECORD] == TASK_EXTRACT_SHEET

    def test_a_callers_max_rows_reaches_the_queue_row(self, db_session, workbook_bytes):
        """Part 0's first and second consequences together. The knob is settable, and the
        value on the queue row is what `param_fingerprint` is taken over — so
        `sheet_tasks`' claim that "a re-ingest that raises the ceiling is a different
        request from the one that failed on it" is now a claim about something a request
        can move."""
        response = IngestService(db_session).upload_file(
            UploadedFile(data=workbook_bytes, filename="deals.xlsx", content_type=XLSX_MIME),
            options={"sheet_records": {"max_rows": 5}},
        )
        drain_ingest_queue(db_session)
        node = DocumentRepository(db_session).get(response.document_id)
        sheet = next(c for c in _subtree(db_session, node) if c.usetype == USETYPE_SHEET)

        attempts = DocumentRepository(db_session).attempt_log(sheet)
        extract = next(a for a in attempts if a["task"] == TASK_EXTRACT_SHEET)
        assert extract["params"]["max_rows"] == 5

    def test_the_same_upload_without_the_knob_carries_the_default(self, db_session, workbook_bytes):
        """The paired half of the test above: the value on the row moved because the
        request moved it, not because the row carries whatever was last written."""
        node = _ingest(db_session, workbook_bytes, "deals.xlsx", XLSX_MIME)
        sheet = next(c for c in _subtree(db_session, node) if c.usetype == USETYPE_SHEET)

        attempts = DocumentRepository(db_session).attempt_log(sheet)
        extract = next(a for a in attempts if a["task"] == TASK_EXTRACT_SHEET)
        assert extract["params"]["max_rows"] == 10_000

    def test_the_rungs_detail_reports_the_frontier_it_planned(self, db_session, workbook_bytes):
        """Per sheet, not in total. The three lists stay apart for the reason 3.4 keeps
        them apart everywhere else: queued, deliberately not queued, and condition false
        are three different facts."""
        node = _ingest(db_session, workbook_bytes, "deals.xlsx", XLSX_MIME)
        attempts = DocumentRepository(db_session).attempt_log(node)
        sheets = next(a for a in attempts if a["task"] == TASK_STRUCTURE_SHEETS)

        assert sheets["detail"]["queued_per_sheet"] == [TASK_PROFILE_SHEET, TASK_EXTRACT_SHEET]
        assert sheets["detail"]["deferred"] == {}
        assert sheets["detail"]["not_applicable"] == {}

    def test_the_two_per_sheet_tasks_run_on_the_sheet_and_are_ordered(
        self, db_session, workbook_bytes
    ):
        """The scope puts them on the sheet node; the `after` orders them there. Both are
        needed and they are different mechanisms — one decides WHICH node, the other
        resolves to a real `dependencies` id in the same batch."""
        node = _ingest(db_session, workbook_bytes, "deals.xlsx", XLSX_MIME)
        sheet = next(c for c in _subtree(db_session, node) if c.usetype == USETYPE_SHEET)

        log = DocumentRepository(db_session).attempt_log(sheet)
        ran = [a["task"] for a in log if a["status"] == "completed"]
        assert ran.index(TASK_PROFILE_SHEET) < ran.index(TASK_EXTRACT_SHEET)
        assert all(a["scope_document_id"] == sheet.id for a in log)


# ---------------------------------------------------------------------------
# 6. 13.2 open question 4 — does `position` survive reparenting?
# ---------------------------------------------------------------------------


class TestPositionUnderReparenting:
    """9.2 makes `position` the default `child_key`, and 13.2's question 4 asks whether it
    survives a partition inserting a level. **It does, and the answer is the shipped
    behaviour rather than a change:** `reparent` appends into the new sibling group, and
    `rollup_tasks._reorder` renumbers the parent's remaining children into document order.

    So positions are rewritten WITHIN each container, which is the answer 13.2 warned would
    make a partition's own children unmatchable across a re-run — and 9.2's saving would
    never fire for the atom that needs it most.

    What resolves it is that the two are different sibling groups. `structure:semantic`
    moves a span of the PARENT's children into a new container, and its own `child_key`
    identifies the CONTAINERS it created — those are numbered by `_reorder` in document
    order, deterministically, from the same segment sequence PELT returns. A chunk's
    position changes because its parent changed, and its matching is `structure:declared`'s
    business over a sibling group that partition never touched.

    Recorded here rather than only in prose because it is a property two modules have to
    keep, and the test is what would notice if either stopped.
    """

    def test_reparenting_renumbers_within_the_new_group_and_keeps_document_order(self, db_session):
        docs = DocumentRepository(db_session)
        parent = docs.create(title="parent", auto_embed=False)
        db_session.flush()
        children = [
            docs.create(title=f"c{i}", parent_id=parent.id, sequential=True, auto_embed=False)
            for i in range(4)
        ]
        db_session.flush()
        assert [c.position for c in children] == [0, 1, 2, 3]

        container = docs.create(title=None, parent_id=parent.id, sequential=True, auto_embed=False)
        db_session.flush()
        for child in children[1:3]:
            docs.reparent(child.id, container.id)
        db_session.flush()

        moved = sorted(
            (c for c in children[1:3]),
            key=lambda c: (c.position if c.position is not None else 0, c.id),
        )
        assert [c.title for c in moved] == ["c1", "c2"]
        assert [c.position for c in moved] == sorted(c.position for c in moved)

    def test_a_container_s_own_position_is_document_order_and_not_creation_order(self, db_session):
        """`rollup_tasks._reorder` exists for this: a new container is appended to the tail
        of the sibling group while a child left in place by 11.4's rule 2 keeps the position
        it had, so without the renumber a document whose second segment was containerised
        and whose first was not would come back with the second span first."""
        from jmfts_core.rollup_tasks import _reorder

        docs = DocumentRepository(db_session)
        parent = docs.create(title="parent", auto_embed=False)
        db_session.flush()
        first = docs.create(title="kept", parent_id=parent.id, sequential=True, auto_embed=False)
        db_session.flush()
        container = docs.create(
            title="container", parent_id=parent.id, sequential=True, auto_embed=False
        )
        db_session.flush()

        _reorder(db_session, [container.id, first.id])
        db_session.flush()
        assert container.position == 0 and first.position == 1


# ---------------------------------------------------------------------------
# 7. The read surface — a produced node is visible as one
# ---------------------------------------------------------------------------


class TestTheReadSurface:
    def test_a_get_reports_which_rule_wrote_the_node(self, db_session):
        node = _ingest(db_session, MARKDOWN, "retrieval.md", "text/markdown")
        chunk = next(c for c in _subtree(db_session, node) if c.usetype == USETYPE_CHUNK)

        response = DocumentService(db_session).get_document(chunk.id)
        assert response.produced_by == chunk.produced_by
        assert response.produced_by in {TASK_STRUCTURE_DECLARED, TASK_STRUCTURE_INFERRED}
