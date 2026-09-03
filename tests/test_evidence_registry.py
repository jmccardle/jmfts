"""The evidence registry, held against the code that writes the evidence.

``SPRINT_JOBS.md`` Part 3, and Phase 2's audit. :mod:`jmfts_core.evidence` is a
declaration, and a declaration nothing checks is a description of what somebody once
believed. This module is what makes it a claim.

Four groups, and they check four different things:

``Shape``
    The registry against :mod:`jmfts_core.atoms` — every declared evidence name has an
    entry, every fan-out bound's ``reads`` resolves, every type is one a value can be
    checked against.

``Patterns``
    :data:`~jmfts_core.evidence.PATTERN_TYPES` against the INSTALLED probe, in both
    directions. ``tests/corpus/vocabulary.py`` recovers probe's vocabulary by running
    probe rather than by listing it, for a reason its docstring states plainly: two lists
    diverge quietly. The registry states a RULE instead — everything probe emits is a flag
    unless named — and this is the ratchet on it. A new measurement in probe fails here.

``Storage``
    The rows a real ingest wrote, against the registry, in both directions. Phase 2's
    version of this group audited :func:`ingest_owned_keys` and found the defect that phase
    closed. Phase 2b deleted that function along with the metadata gate, so the audit is
    now the stronger statement 13.3 made possible: a finished ingest leaves NOTHING in
    ``structured_content``, because the column is wholly the caller's.

    That change is what caught the six keys ``structure:conversation`` writes. Phase 2's
    audit could not have: its fixture ingests documents, not transcripts, and an earlier
    version of this file recorded ``speaker`` and ``conversation_id`` as "CALLER metadata,
    correctly not ingest-owned" — which they never were.

``Types``
    Every value a real ingest wrote, against its declared type.
"""

from __future__ import annotations

import io

import pytest

# The five handler modules, for the registration side effect. `ATOMS` is empty without
# them, and an audit over an empty registry passes by saying nothing.
import jmfts_core.citation_tasks  # noqa: F401
import jmfts_core.embed_tasks  # noqa: F401
import jmfts_core.rollup_tasks  # noqa: F401
import jmfts_core.sheet_tasks  # noqa: F401
import jmfts_core.structure_tasks  # noqa: F401
from jmfts_client.contracts.upload import UploadedFile
from jmfts_core.atoms import ATOMS, EVIDENCE, EXTERNAL_EVIDENCE
from jmfts_core.evidence import (
    ABSENT,
    EVIDENCE_TYPES,
    PATTERN_TYPES,
    REGISTRY,
    STORE_EVIDENCE,
    TYPE_BOOL,
    TYPE_DICT,
    TYPE_SKETCH,
    EvidenceTypeError,
    check,
    get,
    owner,
    pattern_type,
    producers,
    resolve,
    rows,
    written_by,
)
from jmfts_core.models.document import Document
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.evidence import EvidenceRepository
from jmfts_core.services.ingest_service import IngestService
from tests.corpus.fixtures import minimal_pdf
from tests.corpus.vocabulary import specimens

# ---------------------------------------------------------------------------
# Shape — the registry against the declarations
# ---------------------------------------------------------------------------


class TestShape:
    def test_every_declared_evidence_name_is_registered(self):
        """`atoms.EVIDENCE` closes the names; this closes what they hold."""
        missing = sorted(EVIDENCE - set(REGISTRY))
        assert not missing, (
            f"{missing} are declarable in an atom and have no registry entry, so nothing "
            "can say what type they are or where they live"
        )

    def test_every_registered_type_can_check_a_value(self):
        """A declared type with no Python types behind it fails `check` at the call site.

        :data:`TYPE_SKETCH` is the one exception and it is deliberate — 3.1 names it, and
        ``propose:links`` is what will fill it. Every other type must be usable now.
        """
        for name, entry in sorted(REGISTRY.items()):
            if entry.type == TYPE_SKETCH:
                continue
            assert entry.type in EVIDENCE_TYPES, name

    def test_the_sketch_type_still_has_no_member(self):
        """A ratchet in the other direction: when one appears, this test is what says so.

        Failing here means ``sketch`` became real, and the module docstring's paragraph
        about it being kept for ``propose:links`` needs replacing with what actually
        arrived.
        """
        assert not [n for n, e in REGISTRY.items() if e.type == TYPE_SKETCH]

    def test_the_counts_the_documents_quote_are_still_true(self):
        """The four numbers `SPRINT_JOBS.md` 3.1 and `INGEST_SPEC.md` 3.3 state in prose.

        Those two documents quote four numbers out of the registry in prose. Every one of
        those sentences was written from a reading of the registry, and every one was wrong
        within a day of being written, because nothing held them to it.

        This is deliberately a pinned number and not a derivation. Its whole job is to
        fail when the registry changes, so that whoever changes it is told the prose has
        moved and where. Deriving it would make it pass silently and leave the documents
        saying the old thing — which is the defect, not the fix.

        THE NUMBERS MOVED TWICE AND BOTH TIMES THIS TEST IS WHY ANYONE KNOWS. Phase 2b
        added the six the conversation rung writes, so the entries went 37 → 43 and the
        rows 23 → 29; the bare scalars went 9 → 15 for the same reason, and that count now
        costs a caller nothing, because a bare scalar is a row rather than a word reserved
        out of ``structured_content``.
        """
        stored = [
            e for e in REGISTRY.values() if e.store.kind == STORE_EVIDENCE and not e.store.path
        ]
        dicts = [e for e in stored if e.type == TYPE_DICT]
        stated = {
            "registry entries": (len(REGISTRY), 43),
            "evidence rows": (len(stored), 29),
            "rows that are dicts": (len(dicts), 14),
            "rows that are bare scalars": (len(stored) - len(dicts), 15),
        }
        wrong = {k: v for k, v in stated.items() if v[0] != v[1]}
        assert not wrong, (
            f"the registry moved: {wrong} (actual, quoted-in-prose). Update "
            "SPRINT_JOBS.md 3.1 and INGEST_SPEC.md 3.3 in the same commit, then this line."
        )

    def test_every_fanout_read_resolves(self):
        """A bound may only read evidence something can find.

        This is the check that made ``reads`` worth changing from bare names. A bound
        naming ``rows`` resolved against nothing; ``sheet.measurements.rows`` resolves to a
        type, a store path and a producer, and a typo in it now fails here rather than
        producing a ceiling nobody can evaluate.
        """
        for task_type, atom in sorted(ATOMS.items()):
            if atom.fanout is None:
                continue
            for name in atom.fanout.reads:
                entry = get(name)  # raises for an unregistered name
                assert entry.type == "int", (
                    f"{task_type}'s bound reads {name!r}, which is a {entry.type}; "
                    "a child count is arithmetic and every bound does arithmetic on it"
                )

    def test_every_fanout_declares_what_it_counts(self):
        """An interval is not checkable against a run without a node kind to count."""
        counts = {t: a.fanout.counts for t, a in ATOMS.items() if a.fanout is not None}
        assert counts == {
            "structure:declared": "chunk",
            "structure:inferred": "chunk",
            "structure:sheets": "sheet",
            "structure:semantic": "segment",
            "profile:sheet": "summary",
            "extract:sheet": "record",
        }

    def test_a_leaf_inherits_its_blocks_producer(self):
        """Nothing declares producing `extraction.characters`; `extract:text` writes it.

        The walk up ``within`` is what makes ``written_by`` answerable for a leaf, and it
        is what let ``scripts/atom_demo.py`` delete its own table of the same four facts.
        """
        assert producers("extraction.characters") == {"extract:text"}
        assert producers("matched.patterns.sheet_count") == {"probe"}
        assert producers("sheet.measurements.rows") == {"profile:sheet"}
        assert owner("extraction.characters").name == "extraction"

    def test_evidence_no_atom_produces_still_says_who_writes_it(self):
        """The request's, the walk's and the queue's blocks are not gaps."""
        # `source` is the only one nothing produces, ever: a document is either uploaded
        # with its bytes or named, and naming it is the request's act.
        assert producers("source") == set()
        assert "the request" in written_by("source")
        # `file` and `blob` are external for an UPLOAD and produced for a FETCH — S8 gave
        # a document two ways to arrive, and `written_by` names both.
        assert producers("file") == {"fetch:url", "fetch:arxiv", "fetch:path"}
        assert "the upload" in written_by("file")
        assert "the settling walk" in written_by("yield")
        assert "the task queue" in written_by("attempts")
        assert "the tree" in written_by("child_count")
        assert EXTERNAL_EVIDENCE <= set(REGISTRY)

    def test_a_name_and_its_storage_path_may_differ(self):
        """2.5's finding 5, kept: evidence is named by what it asserts.

        ``citation`` writes the column key ``anchor``. The fact is *where the chunk's
        source is*, which is what ``source_anchor`` names. A registry that took the path
        for the name would make the two per-sheet tasks' shared ``sheet`` block one fact
        again, and derive their ordering backwards.
        """
        assert get("source_anchor").store.row == "source_anchor"
        # ITS OWN ROW, not the `unresolved` key of the one above. `within` says these are
        # one fact's two halves; `store.row` says they are two rows, and 3.2's "two keys,
        # never one with a null" is why both statements are true at once.
        assert get("source_anchor.unresolved").store.row == "source_anchor.unresolved"
        assert get("source_anchor.unresolved").within == "source_anchor"
        assert producers("source_anchor.unresolved") == {"citation"}


# ---------------------------------------------------------------------------
# Types — `check`, and the one distinction Python gets wrong
# ---------------------------------------------------------------------------


class TestCheck:
    def test_a_flag_is_not_a_count(self):
        """`isinstance(True, int)` is true, and 3.3 has two different rows for these."""
        with pytest.raises(EvidenceTypeError):
            check("sheet.measurements.rows", True)
        assert check("matched.patterns.has_outline", True) is True

    def test_a_null_passes_only_where_a_null_is_a_result(self):
        """3.2: an atom writes every name it produces on success, null included.

        So a null is a RESULT for the names that have one, and accepting it everywhere
        would let "the reader returned nothing" and "the handler forgot" reach a guard as
        the same value.
        """
        assert check("source_span", None) is None
        with pytest.raises(EvidenceTypeError):
            check("extraction.characters", None)

    def test_an_unregistered_name_raises_rather_than_answering_none(self):
        with pytest.raises(KeyError):
            get("sheet.measurments.rows")

    def test_absent_is_not_null(self):
        """The 3.2 distinction the walk turns on, at the read."""
        assert resolve({"extraction": {"characters": 4}}, "extraction.characters") == 4
        assert resolve({"extraction": {}}, "extraction.characters") is ABSENT
        assert resolve({}, "extraction.characters") is ABSENT
        assert resolve({"source_span": None}, "source_span") is None


# ---------------------------------------------------------------------------
# Patterns — the rule, against the installed probe
# ---------------------------------------------------------------------------


def _probed_types() -> dict[str, str]:
    """Every pattern the installed probe emits, mapped to the registry type of its value."""
    from jmfts_core.probe import detect_format, probe_patterns

    found: dict[str, str] = {}
    for fmt, blobs in specimens().items():
        for blob in blobs:
            detection = detect_format(blob, filename=f"specimen.{fmt}")
            patterns, _detail = probe_patterns(blob, detection)
            for name, value in patterns.items():
                if value is None:
                    continue
                observed = {
                    bool: "bool",
                    int: "int",
                    float: "float",
                    str: "str",
                    list: "list",
                    dict: "dict",
                }[type(value)]
                found[name] = observed
    return found


class TestPatterns:
    def test_every_named_pattern_is_one_probe_emits(self):
        """A name in PATTERN_TYPES that probe stopped emitting is a rule about nothing."""
        probed = _probed_types()
        stale = sorted(set(PATTERN_TYPES) - set(probed))
        assert not stale, (
            f"{stale} are typed in PATTERN_TYPES and the installed probe emits none of "
            "them; the exception list is describing a probe that no longer exists"
        )

    def test_every_pattern_probe_emits_matches_its_declared_type(self):
        """The half that matters: a new measurement fails here until it is named.

        Left unchecked, a new ``int`` pattern would default to ``bool`` through
        :func:`pattern_type`, and a Part 4.4 guard comparing it to a threshold would be
        comparing a number to a flag.
        """
        wrong = {
            name: (pattern_type(name), observed)
            for name, observed in sorted(_probed_types().items())
            if pattern_type(name) != observed
        }
        assert not wrong, (
            f"probe emits {wrong} as (declared, actual). Every pattern is a flag unless "
            "jmfts_core.evidence.PATTERN_TYPES names it"
        )

    def test_the_one_pattern_a_bound_reads_is_typed_once(self):
        """`matched.patterns.sheet_count` has an explicit entry AND falls under the rule."""
        assert get("matched.patterns.sheet_count").type == pattern_type("sheet_count")

    def test_an_unnamed_pattern_resolves_as_a_flag(self):
        entry = get("matched.patterns.has_outline")
        assert entry.type == TYPE_BOOL
        assert entry.store.row == "matched"
        assert entry.store.path == "patterns.has_outline"


# ---------------------------------------------------------------------------
# Storage — the rows against a real tree
# ---------------------------------------------------------------------------

MARKDOWN = b"""# Annual report

The first section, with enough prose in it to be worth chunking into more than
one piece so that the tree has depth. It runs on for several sentences.

## Methods

A second section under a heading of its own, which gives the declared rung
something to declare.
"""


def _workbook() -> bytes:
    """A workbook openpyxl can read back.

    ``tests/corpus``' ``minimal_xlsx`` satisfies tier-1 probe and openpyxl raises
    ``KeyError: 'rId1'`` on it — the fixture omits relationship parts it does not need.
    The sheet ladder needs a workbook written by the library that reads it.
    """
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Measurements"
    sheet.append(["run", "corpus", "recall"])
    for row in range(1, 5):
        sheet.append([row, f"corpus-{row}", round(0.5 + row / 20, 3)])
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


@pytest.fixture
def ingested(db_session, drain_queue):
    """Three files taken all the way through the queue, on one session.

    Three because between them they reach every block the pipeline writes: markdown gives
    the declared rung and its chunks, the PDF gives ``citation`` and therefore an anchor,
    and the workbook gives the two per-sheet tasks. Any one of them alone would leave
    blocks unvisited, and an ownership audit over blocks nothing wrote proves nothing.

    THE THREE SUBTREES AND NOT EVERY DOCUMENT, which the first version got wrong. Under
    random ordering another module's fixtures are in the table, and auditing over a
    document this fixture never ingested makes the suite fail on somebody else's node.

    An earlier version of this docstring gave a second reason and it was wrong: it said a
    conversation node's ``speaker`` and ``conversation_id`` are "CALLER metadata, correctly
    not ingest-owned". ``structure:conversation`` writes both. They are registered evidence
    now and Phase 2b's audit is what found them — see the module docstring.
    """
    service = IngestService(db_session)
    roots = [
        service.upload_file(
            UploadedFile(data=data, filename=filename, content_type=mime)
        ).document_id
        for data, filename, mime in (
            (MARKDOWN, "annual.md", "text/markdown"),
            (minimal_pdf(), "paper.pdf", "application/pdf"),
            (_workbook(), "runs.xlsx", None),
        )
    ]
    drain_queue()
    db_session.commit()
    nodes = db_session.query(Document).all()
    return [n for n in nodes if n.id in roots or any(r in (n.path or []) for r in roots)]


#: Registered rows this fixture does not reach, and the reason for each. Every one is a
#: branch chosen by the INPUT or by the planner, not a name the pipeline stopped writing —
#: so the honest thing is to name them rather than to pick inputs that reach them and turn
#: this into an assertion about uncommon cases.
UNREACHABLE_HERE = {
    "yield": "written exactly when a file node settles holding nothing; all three yielded",
    "cells": "written exactly for a cell with a formula or a forced text type",
    "source_anchor.unresolved": "the other branch of `citation`; every chunk here got one",
    "effective_content": "written by the rollup tasks, and `drain_queue` runs NO_ROLLUP",
    "source": "written only when a document is NAMED rather than uploaded; this uploads",
    "speaker": "the conversation rung; none of these three files is a transcript",
    "turn_index": "the conversation rung",
    "timestamp": "the conversation rung",
    "conversation_id": "the conversation rung",
    "over_token_window": "the conversation rung",
    "part_index": "the conversation rung, and only for a turn that did not fit",
}


@pytest.fixture
def evidence_by_node(db_session, ingested):
    """``{document_id: {name: value}}`` for the fixture's three subtrees."""
    return EvidenceRepository(db_session).read_many([node.id for node in ingested])


class TestStorage:
    def test_a_finished_ingest_leaves_nothing_in_the_column(self, ingested):
        """THE AUDIT PHASE 2b MADE POSSIBLE, and it is stronger than the one it replaces.

        Phase 2 could only ask whether every key on a node was one the metadata gate knew
        the pipeline owned, because the column was two things stitched together. 13.3
        removed the overlap, so the question is now simply: did the pipeline write anything
        into the caller's column? The answer has to be no, on every node, and no list is
        involved in asking it.

        This fixture makes no caller writes, so any key here came from a handler.
        """
        left = {key: node.id for node in ingested for key in (node.structured_content or {})}
        assert not left, (
            f"a finished ingest left {sorted(left)} in `structured_content`, which after "
            "SPRINT_JOBS.md 13.3 is wholly the caller's. Register the name in "
            "jmfts_core.evidence and write it with EvidenceRepository"
        )

    def test_a_metadata_patch_cannot_touch_evidence(self, db_session, ingested):
        """The regression Phase 2 fixed, restated for the store that replaced the gate.

        Before Phase 2 a ``PATCH`` adding a tag deleted every block the hand-written list
        did not name. Phase 2 derived the list; 2b removed the need for one, because the
        two no longer share a column. A whole-object assignment to ``structured_content``
        now cannot reach evidence at all — including under a key of the same name, which
        the gate used to have to refuse.
        """
        repo = DocumentRepository(db_session)
        evidence = EvidenceRepository(db_session)
        touched = 0
        for node in ingested:
            before = evidence.read_all(node.id)
            if not before:
                continue
            # `source_span` names a real evidence row, and naming it here is now an
            # ordinary caller key rather than a refusal: it lands in the column and the row
            # does not move.
            repo.update(
                node.id,
                structured_content={"tag": "q3", "source_span": "not mine to set"},
                re_embed=False,
            )
            db_session.flush()
            assert node.structured_content["tag"] == "q3"
            assert (
                evidence.read_all(node.id) == before
            ), f"node {node.id} ({node.usetype}) lost evidence to a metadata update"
            touched += 1
        assert touched >= 3, "the fixture did not produce nodes carrying evidence to lose"

    def test_every_registered_row_was_written_by_the_run(self, evidence_by_node):
        """The other direction: a registered row nothing writes is a claim about nothing.

        Over the evidence-stored names only — ``embedding`` is a column and ``blob`` is a
        large object — less the ones in :data:`UNREACHABLE_HERE`.
        """
        seen = {name for found in evidence_by_node.values() for name in found}
        unwritten = sorted(rows() - seen - set(UNREACHABLE_HERE))
        assert not unwritten, (
            f"{unwritten} are registered as rows the pipeline writes and this run wrote "
            "none of them; either the entry is wrong or the fixture stopped reaching it"
        )

    def test_the_row_names_are_the_registry_and_nothing_else(self, evidence_by_node):
        """No handler invented a name. ``EvidenceRepository.write`` refuses one, and this
        is the same statement made over what actually landed in the table."""
        seen = {name for found in evidence_by_node.values() for name in found}
        assert not sorted(seen - rows())


class TestDeclaredTypesHoldAgainstRealValues:
    def test_every_registered_value_matches_its_type(self, evidence_by_node):
        """The declaration, against what the handlers actually wrote.

        ``ABSENT`` is skipped rather than failed: no single node carries every name — a
        chunk has no ``sheet`` and a sheet node has no ``source_span`` — and 3.2 makes
        never-attempted a legitimate state.
        """
        checked = 0
        for found in evidence_by_node.values():
            for name, entry in sorted(REGISTRY.items()):
                if entry.store.kind != STORE_EVIDENCE:
                    continue
                value = resolve(found, name)
                if value is ABSENT:
                    continue
                check(name, value)
                checked += 1
        assert checked > 20, f"only {checked} values were reachable; the fixture is too thin"

    def test_the_column_backed_names_match_too(self, ingested):
        for node in ingested:
            check("text", node.content)
            if node.embed is not None:
                check("embedding", list(node.embed))
