"""Phase 1 of the jobs overhaul: does the atom declaration derive today's ordering?

``SPRINT_JOBS.md`` Part 2.3 claims ``TaskRow.after`` and ``TaskRow.after_any`` are
redundant — that they spell out by hand an ordering the ``consumes``/``produces``
declarations already imply. Part 14 makes everything after Phase 3 rest on that claim, and
this module is what makes it falsifiable before anything acts on it.

**Nothing here changes behaviour and nothing in the ingest path reads
:data:`~jmfts_core.atoms.ATOMS`.** These tests read the declarations, derive the ordering,
and compare it against the fields the pipeline actually runs on today.

**The comparison is over TRANSITIVE CLOSURES, not raw edges.** ``citation`` reads the file
node's ``content``, so ``extract:text`` -> ``citation`` is a real dependency; the table
does not state it because ``after_any`` names the structure rungs, which come after
``extract:text`` anyway. Comparing raw edges would report that as a divergence when the two
orderings agree about every pair of tasks that can run.

**The divergences are listed and each one carries a reason.** A divergence that is not in
:data:`EXPECTED_DIVERGENCE` fails, and an entry in it that no longer diverges fails too, so
the list cannot rot into an excuse. There are two entries and they are the same
divergence twice: both tasks read `@subtree` evidence the structure rungs produce.

No database, no model, no optional dependency. It imports the handler modules for their
registration side effect and reads module-level data.
"""

from __future__ import annotations

import pytest

# The nine modules that carry `@register_task_handler`, imported for the side effect. The
# registry is populated at import, so a module nobody imports is an atom nobody declared —
# and `test_every_handler_declares_an_atom` would then not know it exists to complain about.
import jmfts_core.citation_tasks  # noqa: F401
import jmfts_core.conversation_tasks  # noqa: F401
import jmfts_core.embed_tasks  # noqa: F401
import jmfts_core.fetch_tasks  # noqa: F401
import jmfts_core.fact_tasks  # noqa: F401
import jmfts_core.index_tasks  # noqa: F401
import jmfts_core.rollup_tasks  # noqa: F401
import jmfts_core.sheet_tasks  # noqa: F401
import jmfts_core.structure_tasks  # noqa: F401
from jmfts_core.atoms import (
    ATOMS,
    COST_CLASSES,
    COST_CPU,
    COST_LLM,
    COST_MODEL,
    EVIDENCE,
    EXTERNAL_EVIDENCE,
    Atom,
    derive_edges,
    unproduced,
)
from jmfts_core.ingest_options import ROLLUP_PARAMS, STRUCTURE_CHUNK_PARAMS
from jmfts_core.ingest_tasks import (
    TASK_CITATION,
    TASK_EXTRACT_FACTS,
    TASK_STRUCTURE_CONVERSATION,
    TASK_INDEX_BM25,
    TASK_EXTRACT_SHEET,
    TASK_PROFILE_SHEET,
    TASK_ROWS,
    TASK_STRUCTURE_DECLARED,
    TASK_STRUCTURE_INFERRED,
    TASK_STRUCTURE_SEMANTIC,
    TASK_STRUCTURE_SHEETS,
    TASK_HANDLERS,
    ROOT_SCOPE,
    children_of,
)
from jmfts_core.ingest_options import DEFAULT_MAX_ROW_NODES
from jmfts_core.models.document import USETYPE_SHEET

# ---------------------------------------------------------------------------
# The batches — a derived edge orders two tasks on ONE node, so the comparison
# is per batch and never over every atom at once. See `derive_edges`.
# ---------------------------------------------------------------------------


def _scoped_batch(scope) -> dict[str, Atom]:
    """The rows at one scope that have a handler.

    A batch is one NODE's tasks, which is what makes a derived edge meaningful (2.3), and
    since Phase 3 a row states its own scope — so a batch is a filter over ``TASK_ROWS``
    rather than a list written here. ``ocr`` carries a ``skip_reason`` and never reaches
    the queue; ``extract:tables`` and ``extract:images`` are
    :data:`~jmfts_core.ingest_tasks.DEFERRED_REASON` rows whose handlers belong to later
    phasing steps. A row with no handler has no atom to declare one, and
    ``_split_by_handler`` already keeps it out of the batch at runtime.
    """
    return {
        row.task: ATOMS[row.task] for row in TASK_ROWS if row.scope == scope and row.task in ATOMS
    }


def _file_node_batch() -> dict[str, Atom]:
    """Part 4's table at the file node — every row that was in it before Phase 3."""
    return _scoped_batch(ROOT_SCOPE)


def _sheet_node_batch() -> dict[str, Atom]:
    """8.2's two per-sheet tasks, which are a batch on a node ``structure:sheets`` made."""
    return _scoped_batch(children_of(TASK_STRUCTURE_SHEETS, usetypes=(USETYPE_SHEET,)))


def _closure(edges: dict[str, set[str]]) -> dict[str, set[str]]:
    """Every task that must run before each key, directly or through another task."""
    out = {task: set(direct) for task, direct in edges.items()}
    changed = True
    while changed:
        changed = False
        for task, before in out.items():
            grown = set(before)
            for earlier in before:
                grown |= out.get(earlier, set())
            grown.discard(task)
            if grown != before:
                out[task] = grown
                changed = True
    return out


def _declared_order(batch: dict[str, Atom]) -> dict[str, set[str]]:
    """The ordering ``TASK_ROWS`` states by hand, as ``{task: {tasks before it}}``."""
    return {
        row.task: {name for name in row.after + row.after_any if name in batch}
        for row in TASK_ROWS
        if row.task in batch
    }


def _derived_order(batch: dict[str, Atom]) -> dict[str, set[str]]:
    """The same shape, derived from the declarations."""
    out: dict[str, set[str]] = {task: set() for task in batch}
    for producer, consumer, _via in derive_edges(batch):
        out[consumer].add(producer)
    return out


#: Where the derived ordering and the hand-written one disagree, and why.
#:
#: ``{task: (tasks the table names that the derivation does not, reason)}``. Part 14.1
#: predicted this entry before the declarations were written, which is the only reason it
#: is a finding rather than a hole: a list assembled AFTER running the comparison would
#: excuse whatever came out.
EXPECTED_DIVERGENCE: dict[str, tuple[frozenset[str], str]] = {
    TASK_CITATION: (
        frozenset({TASK_STRUCTURE_DECLARED, TASK_STRUCTURE_INFERRED}),
        "citation consumes `source_span@subtree` and the two rungs produce it at "
        "`@subtree`, so 2.2 makes it the settling walk's dependency rather than a "
        "within-node edge. Under the walk this task stops being a probe-time batch member "
        "and becomes a rule that fires when the file node is evaluated (14.1). That is a "
        "behaviour change and it belongs to Phase 3.",
    ),
    TASK_INDEX_BM25: (
        frozenset({TASK_STRUCTURE_DECLARED, TASK_STRUCTURE_INFERRED, TASK_STRUCTURE_CONVERSATION}),
        "the same divergence as `citation` above, for the same reason: it consumes "
        "`text@subtree`, the two rungs produce it at `@subtree`, and 2.2 makes `@subtree` "
        "evidence the settling walk's dependency rather than a within-node edge. "
        "INGEST_SPEC.md 11.5 in fact describes this task AS a rollup task, so the day "
        "Phase 3 moves it under the walk, the spec and the implementation converge rather "
        "than diverge.",
    ),
    TASK_EXTRACT_FACTS: (
        frozenset({TASK_STRUCTURE_DECLARED, TASK_STRUCTURE_INFERRED, TASK_STRUCTURE_CONVERSATION}),
        "the third instance of the same divergence: it consumes `text@subtree`, the two "
        "rungs produce it at `@subtree`, and 2.2 makes `@subtree` evidence the settling "
        "walk's dependency rather than a within-node edge. Unlike the other two, this one "
        "must NOT move under the walk when Phase 3 lands — `jmfts_core.fact_tasks` gives "
        "the reason: the planner is called per node, and one run over the whole tree with "
        "shared entity caches is the point of the task.",
    ),
}


class TestRegistryAndDeclarations:
    def test_every_handler_declares_an_atom(self):
        # By construction — `register_task_handler` requires the declaration — so this
        # fails only if a second registration path grows. That is worth catching: the
        # second path would be the one that skips the declaration.
        assert set(TASK_HANDLERS) == set(ATOMS)

    def test_twenty_one_atoms_ship(self):
        # SPRINT_JOBS.md Part 2 counted twelve, and an earlier draft said "about fifteen".
        # The number is pinned so that adding a handler is a decision about the design
        # rather than a line that slips in — which is what it was: the thirteenth is
        # `index:bm25` (INGEST_SPEC.md 11.5, added by 15.4 S5) and `extract:facts` (11.4,
        # added by S6) are the twelfth and thirteenth... and the fourteenth. Both are
        # parity rows path A had and path B did not, and both were decisions rather than
        # lines that slipped in.
        # The fifteenth is `structure:conversation` (15.4 S7), the rung a transcript gets
        # instead of the two prose ones. Sixteen through eighteen are `fetch:url`,
        # `fetch:arxiv` and `fetch:path` (S8) — three names over one body, because the
        # work is identical and only the pool a deployment routes them to is not.
        #
        # NINETEEN THROUGH TWENTY-ONE ARRIVED 2026-09-06 with 0.5.0, and each is a
        # decision this comment exists to record. They are unlike the first eighteen in
        # one way worth stating up front: none of the three is scheduled by
        # `IngestPlanner` off a document's format, so none of them widens what an upload
        # runs. All three are enqueued by an explicit request or by the settling walk.
        #
        # * `validate:shape` (0.5.0 Block A step 4) — running a SHACL shape over a bound
        #   scope and writing the report as a node. A rung rather than a synchronous
        #   endpoint because the scope is a document set and `docs/MEASURE_SHACL_SCOPE.md`
        #   measures 138,000 documents as comfortable and ~288,000 as an OOM with no
        #   partial progress; that is a queue's problem, not a request's.
        # * `derive:rule` (Block B step 9) — `sh:rule` writing triples that carry
        #   `derived_by`. Separate from `validate:shape` and not a mode of it, because
        #   validation reads and derivation WRITES, and step 9's access hazard is a
        #   property of the writing half alone (see
        #   `tests/test_derived_triple_visibility.py`).
        # * `summarize:tree` (Block C step 11) — the RAPTOR roll-up, which was a rung
        #   before this sprint in everything but name: `IngestRollupPlanner` enqueued it at
        #   settle time and `commit 232cf4f` gave the summary its own node. Registering it
        #   is what makes `EXPLAIN` able to see it.
        assert len(ATOMS) == 21

    def test_every_evidence_name_is_written_by_something(self):
        # An atom waiting on evidence nothing produces would never be schedulable, and the
        # reason would be a typo in a string. `EXTERNAL_EVIDENCE` is the upload's, and no
        # atom will ever produce it.
        assert unproduced() == {}

    def test_no_evidence_name_is_declared_and_never_used(self):
        # The other direction: a constant in `EVIDENCE` that nothing names is either a
        # placeholder or a rename that half-landed.
        used = {item.name for atom in ATOMS.values() for item in atom.consumes + atom.produces}
        assert EVIDENCE - used - EXTERNAL_EVIDENCE == set()

    @pytest.mark.parametrize("task_type", sorted(ATOMS))
    def test_cost_class_is_known(self, task_type):
        assert ATOMS[task_type].cost_class in COST_CLASSES


class TestDerivedOrdering:
    """Part 2.3, tested. This is the class the rest of the sprint rests on."""

    def test_the_file_node_batch_derives_its_own_ordering(self):
        batch = _file_node_batch()
        declared = _closure(_declared_order(batch))
        derived = _closure(_derived_order(batch))

        for task in sorted(batch):
            missing = declared[task] - derived[task]
            expected, _reason = EXPECTED_DIVERGENCE.get(task, (frozenset(), ""))
            assert missing == set(expected), (
                f"{task}: the table orders it after {sorted(missing)} and the declarations "
                "do not. Either the atom under-declares what it reads, or this is a real "
                "divergence and belongs in EXPECTED_DIVERGENCE with a reason"
            )

    def test_the_derivation_orders_nothing_the_table_leaves_free(self):
        # The dangerous direction. A derived edge the table does not have would mean the
        # pipeline runs today in an order the declarations forbid — a live ordering bug,
        # not a design question. `extract:text -> citation` is derived and not stated, and
        # it is not a finding: the table gets it transitively through the structure rungs,
        # which is why this compares closures.
        batch = _file_node_batch()
        declared = _closure(_declared_order(batch))
        derived = _closure(_derived_order(batch))

        for task in sorted(batch):
            assert derived[task] <= declared[task], (
                f"{task}: the declarations order it after {sorted(derived[task] - declared[task])} "
                "and Part 4's table does not, so the pipeline is running it too early today"
            )

    def test_the_sheet_batch_derives_its_own_ordering(self):
        # 8.2's two tasks, and the one place a derived edge comes from evidence rather
        # than from text: `extract:sheet` reads the header verdict `profile:sheet`
        # measured, which is what the `extract:sheet` ROW's `after` states by hand.
        batch = _sheet_node_batch()
        derived = _derived_order(batch)
        assert derived == {
            TASK_PROFILE_SHEET: set(),
            TASK_EXTRACT_SHEET: {TASK_PROFILE_SHEET},
        }

    def test_every_expected_divergence_still_diverges(self):
        # The list cannot rot into an excuse. An entry that stopped diverging is an entry
        # Phase 3 no longer has to handle, and it should be deleted rather than carried.
        batch = _file_node_batch()
        declared = _closure(_declared_order(batch))
        derived = _closure(_derived_order(batch))
        for task, (expected, reason) in EXPECTED_DIVERGENCE.items():
            assert declared[task] - derived[task] == set(expected), (
                f"{task} no longer diverges as recorded; delete the entry. Reason given "
                f"was: {reason}"
            )

    def test_write_modes_match_what_the_queue_is_told(self):
        # The atom's `write_mode` is the mode a FIRST run takes (9.3 moves it onto the plan
        # vertex in Phase 3), so today it must equal what the row or the spec declares.
        # A declaration that drifted from the row would reserve a different region than
        # the one the audit reasons about.
        # ONE LOOP SINCE PHASE 3, where there were two. The second walked
        # `SHEET_TASK_SPECS`, because the sheet tier's write modes were declared on literal
        # specs in `sheet_tasks` rather than on rows. They are rows now, so the table is
        # the whole of what the queue is told.
        for row in TASK_ROWS:
            if row.task in ATOMS:
                assert ATOMS[row.task].write_mode == row.write_mode, row.task


class TestFanout:
    """Part 2.4: a fan-out atom declares a CEILING, and it is a function, not a floor."""

    #: What each bound is handed. Real values, in the units the bound divides: a 40 kB
    #: document, a six-sheet workbook, a 1,284-row sheet (8.4's own worked example), and a
    #: container with 40 children.
    #:
    #: KEYED BY REGISTRY NAME SINCE PHASE 2. The keys were bare — ``characters``, ``rows``
    #: — and resolved against nothing, so a bound could read a name no module writes and
    #: this sample would still hand it a number. ``jmfts_core.evidence`` is what they
    #: resolve against now, and ``tests/test_evidence_registry.py`` is what checks that
    #: every one of them is registered.
    EVIDENCE_SAMPLE = {
        "extraction.characters": 40_000,
        "matched.patterns.sheet_count": 6,
        "sheet.measurements.rows": 1_284,
        "child_count": 40,
    }

    PARAMS_SAMPLE = {
        **STRUCTURE_CHUNK_PARAMS,
        **ROLLUP_PARAMS,
        "max_rows": DEFAULT_MAX_ROW_NODES,
    }

    def _fanout_atoms(self):
        return {t: a for t, a in ATOMS.items() if a.fanout is not None}

    def test_six_atoms_write_children(self):
        # A guard on the tests below: if the declarations lost their fan-out fields, every
        # one of them would pass vacuously. Six of the twelve write nodes — the three
        # structure rungs, the two per-sheet tasks, and PELT.
        assert {t for t, a in ATOMS.items() if a.fanout is not None} == {
            TASK_STRUCTURE_DECLARED,
            TASK_STRUCTURE_INFERRED,
            TASK_STRUCTURE_SHEETS,
            TASK_PROFILE_SHEET,
            TASK_EXTRACT_SHEET,
            TASK_STRUCTURE_SEMANTIC,
        }

    def test_every_bound_returns_a_well_formed_interval(self):
        for task_type, atom in sorted(self._fanout_atoms().items()):
            low, high = atom.fanout.bound(self.EVIDENCE_SAMPLE, self.PARAMS_SAMPLE)
            assert 0 <= low <= high, f"{task_type} bounded [{low}, {high}]"

    def test_a_bound_reads_only_the_evidence_it_declares(self):
        # A bound that reached for an undeclared measurement would be a scheduling
        # decision made from evidence the plan does not know it needs — so `EXPLAIN` could
        # not tell a caller what the answer depends on.
        class _Recording(dict):
            def __init__(self, source):
                super().__init__(source)
                self.read: set[str] = set()

            def __getitem__(self, key):
                self.read.add(key)
                return super().__getitem__(key)

        for task_type, atom in sorted(self._fanout_atoms().items()):
            evidence = _Recording(self.EVIDENCE_SAMPLE)
            atom.fanout.bound(evidence, self.PARAMS_SAMPLE)
            assert evidence.read <= set(atom.fanout.reads), (
                f"{task_type} read {sorted(evidence.read - set(atom.fanout.reads))}, "
                f"which its `reads` does not declare"
            )

    def test_a_fanout_atom_has_a_child_key(self):
        # Enforced in `Atom.__post_init__` both ways; this states the invariant where a
        # reader of the sprint document will look for it. 9.1's idempotent re-run is not
        # expressible without one.
        for task_type, atom in sorted(self._fanout_atoms().items()):
            assert atom.child_key is not None, task_type

    def test_the_cheap_atoms_write_no_children(self):
        # The three atoms that touch one node and create nothing. Stated as its own fact
        # because a fan-out field appearing on one of them would be the first sign that a
        # handler had quietly grown a tree-writing step.
        for task_type in ("probe", "embed", "citation"):
            assert ATOMS[task_type].fanout is None, task_type


class TestCostClasses:
    def test_the_llm_class_is_exactly_the_tasks_that_always_call_one(self):
        # WAS "only the llm handoff". The split between `summarize` and `summarize:llm`
        # exists so that the expensive pool is asked for only after the work is known to
        # need it — `check_fit` runs inside `summarize` and most nodes never hand off. That
        # argument is about a task that MIGHT not need an LLM, and it still holds:
        # `summarize` is `model`, not `llm`.
        #
        # `extract:facts` (SPRINT_JOBS.md 15.4 S6) is the other kind. Every run of it calls
        # an LLM for every leaf — there is no cheap path to check for first — so routing it
        # anywhere but the LLM pool would be wrong about its cost, not careful about it.
        assert {t for t, a in ATOMS.items() if a.cost_class == COST_LLM} == {
            "summarize:llm",
            "extract:facts",
        }

    def test_the_model_class_is_embed_and_the_rollup(self):
        # `summarize` is `model` and not `cpu` because `store_effective_content` embeds the
        # concatenation. Missing that would size the rollup as free.
        assert {t for t, a in ATOMS.items() if a.cost_class == COST_MODEL} == {
            "embed",
            "summarize",
        }

    def test_the_tokenizer_is_not_a_model(self):
        # `profile:sheet` and the two structure rungs all call the embedding service, and
        # all three call it for `check_fit`/`fits_token_window`, which is the tokenizer.
        # It loads no weights and touches no GPU, so it is `cpu` — and a badge sized on
        # this being `model` would pin three parsing tasks to the GPU pool.
        for task_type in (TASK_PROFILE_SHEET, TASK_STRUCTURE_DECLARED, TASK_STRUCTURE_INFERRED):
            assert ATOMS[task_type].cost_class == COST_CPU, task_type
