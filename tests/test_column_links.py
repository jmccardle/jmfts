"""Columns whose value sets nest, proposed by containment. ``INGEST_SPEC.md`` 8.6.

Three things are under test and they are different in kind.

The **search** (:mod:`jmfts_core.column_links`) is a pure function of stored profiles, so it
is tested without a database and without a workbook: that containment and not Jaccard is
what finds a twelve-row lookup inside a much larger log, that a shared column NAME never
admits a pair, and that two sketches built under different permutations raise instead of
reporting a number.

The **task**, end to end, on the real queue: upload a workbook whose sheets genuinely
reference each other, drain, and read the proposals off the file node — including the exact
verification, which is the half the search does not do.

The **packaging seam**: ``propose:links`` on an install with no ``datasketch`` must fail by
name, before it can decide there is nothing to propose.

FIXTURES ARE BUILT BY ``openpyxl`` for the reason ``tests/test_sheet_tasks.py`` gives, and
the whole module needs the ``sketch`` extra as well — the search it exercises has no meaning
without one.

EVERY READ AND WRITE OF A PROFILE HERE GOES THROUGH :class:`EvidenceRepository`, where the
2026-08-23 draft of this file reached into ``Document.structured_content``. Phase 2b moved
every named fact into a row per name, and a test that poked the old column would be writing
somewhere nothing reads — which is the one kind of green this suite must not produce.
"""

from __future__ import annotations

import io
from dataclasses import replace

import pytest
from sqlalchemy import select

pytest.importorskip("openpyxl", reason="the office extra is not installed")
pytest.importorskip("datasketch", reason="the sketch extra is not installed")

import openpyxl  # noqa: E402

import jmfts_core.ingest_tasks  # noqa: E402,F401  (import order; see the module cycle)
from jmfts_client.contracts.upload import UploadedFile  # noqa: E402
from jmfts_core.atoms import EV_SHEET  # noqa: E402
from jmfts_core.column_links import (  # noqa: E402
    IMPLICATION_FOREIGN_KEY,
    IMPLICATION_INVERSE_FUNCTIONAL_PROPERTY,
    IMPLICATION_RESOURCE_VALUED_PREDICATE,
    VERIFIED_NONE,
    VERIFIED_SQL_EXACT,
    ColumnRef,
    IncomparableSketches,
    Verification,
    candidate_record,
    collapse_mutual,
    find_containments,
    implications_of,
    name_similarity,
    passes_threshold,
    review_payload,
    unverifiable_reason,
)
from jmfts_core.ingest_tasks import (  # noqa: E402
    TASK_PROFILE_SHEET,
    TASK_PROPOSE_LINKS,
    TASK_REVIEW_LINK,
)
from jmfts_core.models.document import Document, SETTLED_SETTLED  # noqa: E402
from jmfts_core.models.task_queue import TaskQueue  # noqa: E402
from jmfts_core.repositories.document import DocumentRepository  # noqa: E402
from jmfts_core.repositories.evidence import EvidenceRepository, evidence_value  # noqa: E402
from jmfts_core.services.ingest_service import IngestService  # noqa: E402
from jmfts_core.sheet_tasks import LINKS_KEY, USETYPE_SHEET, run_propose_links  # noqa: E402
from jmfts_core.sketch import SketchStackNotInstalled, column_sketch  # noqa: E402
from tests.conftest import drain_ingest_queue  # noqa: E402

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

#: The twelve region codes the fixture workbook's lookup sheet defines.
CODES = ["NW", "NE", "SW", "SE", "MW", "MT", "PC", "GL", "NG", "SG", "CE", "AT"]

#: The eight of them the deals sheet actually uses. EIGHT, not twelve, and that is the
#: point: an equal pair and a strictly nested pair are 8.6's two cases and they must not be
#: reported the same way. Eight of twelve puts the reverse containment at 0.67, below the
#: default threshold, so the pair is one-directional.
USED_CODES = CODES[:8]


# ---------------------------------------------------------------------------
# The search — no database, no workbook
# ---------------------------------------------------------------------------


def _ref(
    node_id: int,
    sheet: str,
    name: str,
    values,
    *,
    position: int = 0,
    is_unique: bool = False,
    has_values: bool = True,
    sketch=None,
) -> ColumnRef:
    """A ``ColumnRef`` as ``columns_of`` would have built it from a stored profile."""
    distinct = sorted(set(values))
    return ColumnRef(
        node_id=node_id,
        sheet_name=sheet,
        sheet_index=None,
        position=position,
        index=position + 1,
        letter=chr(ord("A") + position),
        name=name,
        distinct_count=len(distinct),
        is_unique=is_unique,
        body_rows=len(list(values)),
        has_values=has_values,
        sketch=sketch if sketch is not None else column_sketch(distinct),
    )


def _proposed(columns, threshold: float = 0.9):
    """The three passes, with no database — so every pair is judged on its estimate.

    ``find_containments`` alone is a recall set and is not what a reviewer sees. The handler
    runs the same three steps with the SQL count standing in for the estimate wherever 8.5
    stored both value sets; here there is no session, so the sketches decide, which is
    exactly the path a high-cardinality pair takes in production.
    """
    unverified = Verification(method=VERIFIED_NONE, reason="no session in this test")
    found = find_containments(columns, threshold=threshold)
    kept = [pair for pair in found if passes_threshold(pair, unverified, threshold)]
    return collapse_mutual(kept)


class TestContainmentNotJaccard:
    def test_a_small_lookup_is_found_inside_a_much_larger_log(self):
        """The pair `SPRINT_0_3_0.md` 6.4 names, and the reason the ensemble is here.

        Twelve codes inside two hundred transactions is a Jaccard of 0.06 and a containment
        of 1.0. Anything ranking by Jaccard puts this pair below every coincidence in the
        workbook; a containment search finds it at the top.

        TWO HUNDRED AND NOT FIVE THOUSAND, and the difference is measured rather than
        cosmetic: see ``column_links.SKETCH_RESOLUTION``. At 128 permutations this pair is
        found every time at a size ratio of 0.06 and about 40% of the time at 0.0024, so a
        test written at the second ratio would be a coin flip and the appliance's own limit
        would be hidden inside a flaky test instead of stated in the record.
        """
        lookup = _ref(1, "Lookup", "Code", CODES, is_unique=True)
        log = _ref(2, "Ledger", "Region", CODES + [f"T-{n}" for n in range(188)])

        found = _proposed([lookup, log])

        assert [(pair.contained.sheet_name, pair.container.sheet_name) for pair in found] == [
            ("Lookup", "Ledger")
        ]
        assert found[0].estimate > 0.9
        assert found[0].mutual is False

    def test_an_equal_pair_is_one_candidate_and_says_so(self):
        """8.6's "equal" case. Two directions over one relationship is one review task."""
        left = _ref(1, "Lookup", "Code", CODES, is_unique=True)
        right = _ref(2, "Deals", "Region Code", CODES * 3)

        found = _proposed([left, right])

        assert len(found) == 1
        assert found[0].mutual is True
        # The unique column becomes the container, whichever direction the query ran, so
        # that the candidate reads as the key relationship it is.
        assert found[0].container.sheet_name == "Lookup"

    def test_a_nested_pair_keeps_its_direction(self):
        """8.6's "subset" case, and the reason the collapse runs after the measurement.

        The ensemble proposes both directions here — it is weighted for recall and its
        banding reads part of each sketch — and only the measurement separates 8 of 8 from
        8 of 12.
        """
        left = _ref(1, "Lookup", "Code", CODES, is_unique=True)
        right = _ref(2, "Deals", "Region Code", USED_CODES * 5)

        assert len(find_containments([left, right])) == 2
        found = _proposed([left, right])

        assert len(found) == 1
        assert found[0].mutual is False
        assert found[0].contained.sheet_name == "Deals"
        assert found[0].container.sheet_name == "Lookup"


class TestNamesAreNeverAReason:
    def test_two_columns_with_one_name_and_no_shared_value_are_not_proposed(self):
        """6.4: "Column *name* similarity is a tie-break only." """
        left = _ref(1, "Lookup", "Region", ["Northwest", "Northeast", "Southwest"])
        right = _ref(2, "Deals", "Region", ["alpha", "bravo", "charlie"])

        assert name_similarity("Region", "Region") == 1.0
        assert _proposed([left, right]) == []

    def test_two_columns_with_unlike_names_and_nested_values_are_proposed(self):
        left = _ref(1, "Lookup", "Ledger Key", CODES, is_unique=True)
        right = _ref(2, "Deals", "rgn", USED_CODES * 4)

        found = _proposed([left, right])

        assert len(found) == 1
        assert (name_similarity("rgn", "Ledger Key") or 0.0) < 0.5

    def test_a_column_with_no_header_has_no_name_similarity(self):
        assert name_similarity(None, "Code") is None

    def test_case_and_punctuation_do_not_separate_two_headers(self):
        assert name_similarity("Deal ID", "deal_id") == 1.0


class TestSameSheetPairsAreOutOfScope:
    def test_two_columns_of_one_sheet_are_not_proposed(self):
        """8.6 is titled "Cross-sheet closed sets" and this task is workbook-level.

        A column nested inside another column of the same sheet is a real inclusion
        dependency and it is not the one the spec asks for.
        """
        left = _ref(3, "Deals", "Owner", ["a", "b"], position=0)
        right = _ref(3, "Deals", "Approver", ["a", "b", "c", "d"], position=1)

        assert _proposed([left, right]) == []


class TestIncomparableSketches:
    def test_two_permutation_recipes_in_one_workbook_raise(self):
        """`jmfts_core.sketch` carries the recipe for exactly this moment.

        A MinHash compared with one built from different permutations produces a number,
        and a number nobody can trust is worse than a failure that names the sheets.
        """
        left = _ref(1, "Lookup", "Code", CODES)
        right = _ref(
            2,
            "Deals",
            "Region Code",
            CODES,
            sketch=column_sketch(sorted(set(CODES)), num_perm=64),
        )

        with pytest.raises(IncomparableSketches) as error:
            find_containments([left, right])

        assert "permutation recipe" in str(error.value)

    def test_a_sketch_of_an_unknown_kind_raises(self):
        stored = dict(column_sketch(CODES), kind="hyperloglog")
        column = _ref(1, "Lookup", "Code", CODES, sketch=stored)

        with pytest.raises(IncomparableSketches) as error:
            find_containments([column, _ref(2, "Deals", "Region Code", CODES)])

        assert "hyperloglog" in str(error.value)


class TestOneMeasurementThreeUses:
    def test_a_unique_container_carries_all_three(self):
        """6.4: "One measurement, three uses." Recording one of them would be a choice."""
        found = _proposed(
            [
                _ref(1, "Lookup", "Code", CODES, is_unique=True),
                _ref(2, "Deals", "Region Code", USED_CODES * 5),
            ]
        )
        implications, reason = implications_of(found[0])

        assert set(implications) == {
            IMPLICATION_FOREIGN_KEY,
            IMPLICATION_RESOURCE_VALUED_PREDICATE,
            IMPLICATION_INVERSE_FUNCTIONAL_PROPERTY,
        }
        assert "identifies a row" in reason

    def test_a_repeating_container_carries_none_of_them(self):
        found = _proposed(
            [
                _ref(1, "Lookup", "Code", CODES * 2, is_unique=False),
                _ref(2, "Deals", "Region Code", USED_CODES * 5),
            ]
        )
        implications, reason = implications_of(found[0])

        assert implications == ()
        assert "identifies no row" in reason

    def test_an_untracked_container_claims_nothing_and_says_why(self):
        """`is_unique` is None where the distinct set stopped being tracked."""
        container = replace(_ref(1, "Lookup", "Code", CODES), is_unique=None)
        found = _proposed([container, _ref(2, "Deals", "Region Code", USED_CODES * 5)])
        implications, reason = implications_of(found[0])

        assert implications == ()
        assert "is_unique` is unknown" in reason


class TestUnverifiablePairsAreLabelledNotDropped:
    def test_a_container_with_no_stored_values_cannot_be_counted(self):
        """8.5 keeps no `values` for a high-cardinality column — the LSH pass's whole point.

        The candidate survives, and it says the containment beside it was estimated.
        """
        found = _proposed(
            [
                _ref(1, "Lookup", "Code", CODES),
                _ref(
                    2,
                    "Ledger",
                    "Region",
                    CODES + [f"T-{n}" for n in range(188)],
                    has_values=False,
                ),
            ]
        )
        reason = unverifiable_reason(found[0])

        assert reason is not None
        assert "VALUES_RETAINED_MAX" in reason
        record = candidate_record(found[0], Verification(method=VERIFIED_NONE, reason=reason))
        assert record["verification"]["containment"] is None
        assert record["containment_estimate"] > 0.9

    def test_a_pair_whose_two_sides_both_stored_values_is_countable(self):
        found = _proposed(
            [
                _ref(1, "Lookup", "Code", CODES),
                _ref(2, "Deals", "Region Code", USED_CODES * 4),
            ]
        )

        assert unverifiable_reason(found[0]) is None


class TestReviewPayload:
    def test_it_is_part_7_6_s_shape_and_proposes_an_answer(self):
        found = _proposed(
            [
                _ref(1, "Lookup", "Code", CODES, is_unique=True),
                _ref(2, "Deals", "Region Code", USED_CODES * 5),
            ]
        )
        payload = review_payload(
            found[0],
            Verification(method=VERIFIED_SQL_EXACT, contained_size=8, container_size=12, overlap=8),
        )

        assert set(payload) == {
            "question",
            "field_type",
            "options",
            "proposed",
            "target_task",
            "target_param",
            "estimated_rebuild_nodes",
        }
        # 7.5: "a review task carries a proposed answer. Approval takes seconds."
        assert payload["field_type"] == "confirm"
        assert payload["proposed"] is True
        assert payload["target_task"] == "extract:sheet"
        # Counting first, the question about meaning second — never the other way round.
        assert payload["question"].startswith("All 8 values")
        assert payload["question"].endswith("?")

    def test_an_unverified_pair_says_its_number_was_estimated(self):
        found = _proposed(
            [
                _ref(1, "Lookup", "Code", CODES),
                _ref(2, "Ledger", "Region", CODES + [f"T-{n}" for n in range(188)]),
            ]
        )
        payload = review_payload(found[0], Verification(method=VERIFIED_NONE, reason="…"))

        assert payload["question"].startswith("An estimated ")


# ---------------------------------------------------------------------------
# The task, end to end
# ---------------------------------------------------------------------------


def _workbook_bytes() -> bytes:
    """A lookup sheet, a deals sheet that references it, and a decoy that shares a name.

    ``Lookup!Code`` holds twelve distinct codes, one per row, so it identifies a row.
    ``Deals!Region Code`` uses eight of them across forty rows — nested, not equal. And both
    sheets have a column called ``Region`` whose values are disjoint, which is the pair a
    name-matching implementation would propose and this one must not.

    ``Status`` is on both sheets with the same two values, which is 8.6's other case — an
    equal pair, whose container identifies nothing — and it is what makes the workbook
    produce more than one candidate.
    """
    workbook = openpyxl.Workbook()
    lookup = workbook.active
    lookup.title = "Lookup"
    lookup.append(["Code", "Region", "Status"])
    for position, code in enumerate(CODES):
        lookup.append([code, f"Territory {position}", "Active" if position % 2 else "Retired"])

    deals = workbook.create_sheet("Deals")
    deals.append(["Deal ID", "Region Code", "Region", "Value", "Status"])
    for row in range(40):
        deals.append(
            [
                f"D-{4400 + row}",
                USED_CODES[row % len(USED_CODES)],
                f"zone-{row % 3}",
                1000 + row,
                "Active" if row % 2 else "Retired",
            ]
        )

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


@pytest.fixture(scope="module")
def workbook_bytes() -> bytes:
    return _workbook_bytes()


def _ingest(session, data: bytes, filename: str = "links.xlsx") -> Document:
    response = IngestService(session).upload_file(
        UploadedFile(data=data, filename=filename, content_type=XLSX_MIME)
    )
    drain_ingest_queue(session)
    return DocumentRepository(session).get(response.document_id)


def _links(session, node: Document) -> dict:
    """The proposals, read the way anything else reads a named fact: as an evidence row."""
    return evidence_value(session, node.id, LINKS_KEY)


def _candidate(links: dict, contained: str, container: str) -> dict:
    return next(
        candidate
        for candidate in links["candidates"]
        if candidate["contained"]["column"] == contained
        and candidate["container"]["column"] == container
    )


class _ScopedTask:
    """The two fields the handler reads off its queue row. See ``tests/test_sheet_tasks.py``."""

    def __init__(self, document_id: int, params: dict | None = None):
        self.scope_document_id = document_id
        self.params = params or {}


class TestProposeLinksEndToEnd:
    def test_the_nested_pair_is_proposed_and_verified_by_counting(self, db_session, workbook_bytes):
        node = _ingest(db_session, workbook_bytes)
        links = _links(db_session, node)

        candidate = _candidate(links, "Region Code", "Code")
        assert candidate["contained"]["sheet"] == "Deals"
        assert candidate["container"]["sheet"] == "Lookup"
        # The LSH pass proposed it; SQL counted it. Eight of eight, exactly.
        assert candidate["verification"] == {
            "method": VERIFIED_SQL_EXACT,
            "reason": None,
            "contained_size": 8,
            "container_size": 12,
            "overlap": 8,
            "containment": 1.0,
        }

    def test_the_pair_that_only_shares_a_name_is_not_proposed(self, db_session, workbook_bytes):
        node = _ingest(db_session, workbook_bytes)

        pairs = {
            (candidate["contained"]["column"], candidate["container"]["column"])
            for candidate in _links(db_session, node)["candidates"]
        }

        assert ("Region", "Region") not in pairs

    def test_a_unique_container_records_all_three_uses(self, db_session, workbook_bytes):
        node = _ingest(db_session, workbook_bytes)

        candidate = _candidate(_links(db_session, node), "Region Code", "Code")

        assert candidate["container"]["is_unique"] is True
        assert set(candidate["implications"]) == {
            IMPLICATION_FOREIGN_KEY,
            IMPLICATION_RESOURCE_VALUED_PREDICATE,
            IMPLICATION_INVERSE_FUNCTIONAL_PROPERTY,
        }

    def test_the_record_states_that_its_thresholds_are_uncalibrated(
        self, db_session, workbook_bytes
    ):
        """8.8, in the workbook rather than only in a document nobody ships."""
        links = _links(db_session, _ingest(db_session, workbook_bytes))

        assert links["parameters"]["containment_threshold"] == 0.9
        assert "uncalibrated" in links["thresholds"]
        assert "8.8" in links["thresholds"]

    def test_the_review_task_is_carried_and_not_queued(self, db_session, workbook_bytes):
        """8.6's contract: the machine proposes, the payload exists, nobody's meaning is set.

        The row itself is deferred — a worker would claim it and a settle walk would wait
        on it. See `DEFERRED_REASON[TASK_REVIEW_LINK]`.
        """
        node = _ingest(db_session, workbook_bytes)
        links = _links(db_session, node)

        assert links["review"]["task"] == TASK_REVIEW_LINK
        assert "7.3" in links["review"]["deferred"]
        assert _candidate(links, "Region Code", "Code")["review"]["field_type"] == "confirm"
        queued = db_session.execute(
            select(TaskQueue).where(TaskQueue.task_type == TASK_REVIEW_LINK)
        ).all()
        assert queued == []

    def test_it_waits_for_every_profile_in_the_workbook(self, db_session, workbook_bytes):
        """8.6: "it needs all sheets profiled first"."""
        node = _ingest(db_session, workbook_bytes)

        task = db_session.execute(
            select(TaskQueue).where(
                TaskQueue.task_type == TASK_PROPOSE_LINKS,
                TaskQueue.scope_document_id == node.id,
            )
        ).scalar_one()
        profiles = (
            db_session.execute(
                select(TaskQueue.id).where(TaskQueue.task_type == TASK_PROFILE_SHEET)
            )
            .scalars()
            .all()
        )

        assert task.status == "completed"
        assert sorted(task.dependencies) == sorted(profiles)

    def test_the_workbook_settles_when_it_has_run(self, db_session, workbook_bytes):
        """A workbook-level task on the file node must not leave the file in flight."""
        node = _ingest(db_session, workbook_bytes)

        assert node.settled == SETTLED_SETTLED


class TestProposeLinksFailsByName:
    def test_an_install_with_no_sketch_stack_says_which_install_it_is(
        self, db_session, workbook_bytes, monkeypatch
    ):
        """Before it reads a sheet, and before it can decide there is nothing to propose."""
        node = _ingest(db_session, workbook_bytes)

        def refuse():
            raise SketchStackNotInstalled("no datasketch here")

        monkeypatch.setattr("jmfts_core.sheet_tasks.require_datasketch", refuse)

        with pytest.raises(SketchStackNotInstalled):
            run_propose_links(db_session, _ScopedTask(node.id))

    def test_profiles_written_without_sketches_are_a_named_failure(
        self, db_session, workbook_bytes
    ):
        """Not "no candidates". A search that could not look has not looked."""
        node = _ingest(db_session, workbook_bytes)
        evidence = EvidenceRepository(db_session)
        for sheet in _sheet_nodes(db_session, node.id):
            block = dict(evidence.read(sheet.id, EV_SHEET))
            block["columns"] = [dict(column, sketch=None) for column in block["columns"]]
            evidence.write(sheet.id, EV_SHEET, block)
        db_session.flush()

        with pytest.raises(ValueError) as error:
            run_propose_links(db_session, _ScopedTask(node.id))

        assert "sketch_columns" in str(error.value)

    def test_a_sheet_with_no_profile_at_all_is_a_contradiction(self, db_session, workbook_bytes):
        node = _ingest(db_session, workbook_bytes)
        evidence = EvidenceRepository(db_session)
        sheet = _sheet_nodes(db_session, node.id)[0]
        block = dict(evidence.read(sheet.id, EV_SHEET))
        evidence.write(sheet.id, EV_SHEET, {"name": block["name"]})
        db_session.flush()

        with pytest.raises(ValueError) as error:
            run_propose_links(db_session, _ScopedTask(node.id))

        assert "no profile" in str(error.value)

    def test_more_candidates_than_the_limit_fails_rather_than_truncating(
        self, db_session, workbook_bytes
    ):
        """`SPRINT_0_3_0.md` 6.6's rule: a named limit that fails, not a silent cut."""
        node = _ingest(db_session, workbook_bytes)

        with pytest.raises(ValueError) as error:
            run_propose_links(db_session, _ScopedTask(node.id, {"max_candidates": 1}))

        assert "over the `max_candidates` limit" in str(error.value)
        # Nothing was written: the record still holds the full run's proposals.
        assert len(_links(db_session, node)["candidates"]) > 1

    def test_a_threshold_outside_the_unit_interval_is_refused(self, db_session, workbook_bytes):
        node = _ingest(db_session, workbook_bytes)

        with pytest.raises(ValueError) as error:
            run_propose_links(db_session, _ScopedTask(node.id, {"containment_threshold": 1.4}))

        assert "containment_threshold" in str(error.value)


def _sheet_nodes(session, file_node_id: int) -> list[Document]:
    return list(
        session.execute(
            select(Document)
            .where(Document.parent_id == file_node_id, Document.usetype == USETYPE_SHEET)
            .order_by(Document.id)
        )
        .scalars()
        .all()
    )
