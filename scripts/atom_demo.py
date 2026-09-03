#!/usr/bin/env python3
"""What Phase 1's atom declarations say, run against real files.

``SPRINT_JOBS.md`` Part 2 gives every registered task handler a declaration: what it
reads, what it writes, where, how expensive it is, and how many children it may create.
``jmfts_core/atoms.py`` is that vocabulary and ``tests/test_atom_declarations.py`` is the
audit. Neither one SHOWS you anything — the test asserts and the module defines. This
script prints.

Five subcommands, and the first four need no database, no model and no optional
dependency:

    python -m scripts.atom_demo atoms                 # every declaration, one table
    python -m scripts.atom_demo fixtures DIR          # write real files to point `plan` at
    python -m scripts.atom_demo plan FILE [FILE ...]  # probe -> plan -> the derived order
    python -m scripts.atom_demo refuses               # what the vocabulary rejects, and why
    python -m scripts.atom_demo run FILE              # a real ingest, declaration vs reality

`run` is the only one that touches a database, and it touches the one ``JMFTS_DB_*``
names. Point it somewhere throwaway:

    docker run -d --name atom-demo-pg -e POSTGRES_USER=jmfts -e POSTGRES_PASSWORD=jmfts \\
        -e POSTGRES_DB=jmfts -p 127.0.0.1:5434:5432 pgvector/pgvector:pg16
    JMFTS_DB_PORT=5434 jmfts-init-db
    JMFTS_DB_PORT=5434 python -m scripts.atom_demo run /tmp/fx/minimal.pdf

**Nothing in the ingest path reads ``ATOMS``, and `run` is what makes that visible.** It
ingests through the ordinary client and the ordinary worker, and then holds each
declaration up against what the run actually did. Phase 1 changed no behaviour, so every
comparison here is an observation about code that would run identically with
``jmfts_core/atoms.py`` deleted. That is the point: the declarations have to be shown
true before Phase 3 starts deriving the schedule from them.

THIS SCRIPT USED TO KEEP TWO TABLES OF ITS OWN and Phase 2 took both. One said where a
fan-out bound's evidence lived, the other which usetype a ceiling counted, and each carried
a comment saying it was the half of a declaration that did not exist yet.
:mod:`jmfts_core.evidence` is the first and ``Fanout.counts`` is the second, so everything
printed below now comes off a declaration.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from jmfts_core.atoms import (
    ATOMS,
    COST_CLASSES,
    Atom,
    ChildKey,
    Fact,
    Fanout,
    KEY_NATURAL,
    KEY_POSITION,
    derive_edges,
    fact,
    unproduced,
)
from jmfts_core.evidence import ABSENT, resolve, written_by
from jmfts_core.evidence import check as ev_check
from jmfts_core.evidence import get as ev_get
from jmfts_core.ingest_options import resolve_options
from jmfts_core.ingest_tasks import SCOPE_CHILDREN, TASK_ROWS, plan_after_probe
from jmfts_core.probe import detect_format, probe_patterns

# Imported for the registration side effect. `register_task_handler` populates both
# `TASK_HANDLERS` and `ATOMS`, so a handler module that is never imported has no atom —
# the same rule the worker lives under, and the same five modules it imports.
import jmfts_core.citation_tasks  # noqa: F401
import jmfts_core.embed_tasks  # noqa: F401
import jmfts_core.rollup_tasks  # noqa: F401
import jmfts_core.sheet_tasks  # noqa: F401
import jmfts_core.structure_tasks  # noqa: F401


def _facts(facts: tuple[Fact, ...]) -> str:
    return ", ".join(str(f) for f in facts) if facts else "—"


def _rule(title: str) -> None:
    print(f"\n{title}\n{'=' * len(title)}")


def _sub(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


# ---------------------------------------------------------------------------
# atoms — the whole vocabulary, printed
# ---------------------------------------------------------------------------


def cmd_atoms(args: argparse.Namespace) -> int:
    """Every declaration, and the three things you can compute from all of them at once."""
    _rule(f"{len(ATOMS)} atoms, as declared beside their handlers")
    for name in sorted(ATOMS):
        atom = ATOMS[name]
        print(f"\n  {name}")
        print(f"    consumes    {_facts(atom.consumes)}")
        print(f"    produces    {_facts(atom.produces)}")
        print(f"    write_mode  {atom.write_mode}    cost  {atom.cost_class}")
        if atom.fanout is not None:
            assert atom.child_key is not None  # `Atom.__post_init__` refuses one without the other
            key = atom.child_key.kind + (f" ({atom.child_key.path})" if atom.child_key.path else "")
            print(f"    fan-out     {atom.fanout.basis}, from {list(atom.fanout.reads)}")
            print(f"    child key   {key}")
        else:
            print("    fan-out     none — this atom writes no children")

    _sub("Cost, which is what a badge and a budget are sized for")
    for cost in COST_CLASSES:
        members = sorted(n for n, a in ATOMS.items() if a.cost_class == cost)
        print(f"  {cost:6} {len(members):2}  {', '.join(members)}")
    print(
        "\n  A base install (no torch) can run every `cpu` atom and none of the `model`\n"
        "  ones, unless JMFTS_RUNNER_URL points at a JMFTS that has the weights."
    )

    _sub("Where each atom may write")
    for mode in ("self", "children", "subtree"):
        members = sorted(n for n, a in ATOMS.items() if a.write_mode == mode)
        print(f"  {mode:9} {len(members):2}  {', '.join(members)}")

    _sub("The fourth locus")
    upward = sorted(n for n, a in ATOMS.items() if any(f.locus == "ancestor" for f in a.consumes))
    print(
        "  SPRINT_JOBS.md 2.2 said a locus is one of the three write modes. These atoms\n"
        "  falsify it — they are scoped to a sheet node and read the workbook bytes off\n"
        f"  the file node above:\n\n    {', '.join(upward)}\n\n"
        "  `@ancestor` is legal in `consumes` and refused in `produces`. Reading upward\n"
        "  is not writing upward, and no write mode reserves a node above the scope."
    )
    return 0


# ---------------------------------------------------------------------------
# plan — probe a real file, plan from it, then derive the order
# ---------------------------------------------------------------------------


def _closure(predecessors: dict[str, set[str]], task: str) -> set[str]:
    """Every task that must finish before ``task``, following the edges transitively."""
    seen: set[str] = set()
    frontier = list(predecessors.get(task, ()))
    while frontier:
        name = frontier.pop()
        if name in seen:
            continue
        seen.add(name)
        frontier.extend(predecessors.get(name, ()))
    return seen


def spec_consumes_offnode(atom: Atom) -> tuple[Fact, ...]:
    """The consumed facts that make no within-node edge — the walk's dependencies."""
    return tuple(f for f in atom.consumes if f.locus in ("children", "subtree"))


def _ceiling(atom: Atom, evidence: dict, params: dict) -> str:
    """Evaluate one bound, or say which evidence is still missing."""
    assert atom.fanout is not None
    missing = [name for name in atom.fanout.reads if name not in evidence]
    if missing:
        # WHY it cannot be computed, from the registry rather than from a table kept here.
        # `extraction.characters` is a number `extract:text` produces, so at probe time it
        # does not exist yet and the honest answer is "not measured", not a guess.
        why = "; ".join(f"{n}: {written_by(n)}" for n in missing)
        return f"not computable yet — {why}"
    low, high = atom.fanout.bound(evidence, params)
    return f"{low}..{high} {atom.fanout.counts}  ({atom.fanout.basis})"


def cmd_plan(args: argparse.Namespace) -> int:
    """Probe real bytes, run Part 4's table over the result, and read the atoms back."""
    for path in args.files:
        data = Path(path).read_bytes()
        detection = detect_format(data, filename=Path(path).name)
        patterns, detail = probe_patterns(data, detection)
        plan = plan_after_probe(detection.format, patterns)

        _rule(f"{path}  —  {len(data)} bytes")
        print(f"  format      {detection.format}  (detected by {detection.detected_by})")
        true_patterns = sorted(k for k, v in patterns.items() if v is True)
        print(f"  patterns    {', '.join(true_patterns) if true_patterns else '(none true)'}")
        # `not isinstance(v, bool)` because bool IS an int in Python, and a counted
        # measurement and a true/false pattern are two different rows of spec 3.3.
        counted = {
            k: v for k, v in patterns.items() if isinstance(v, int) and not isinstance(v, bool)
        }
        if counted:
            print(f"  counted     {counted}")

        if not plan.eligible:
            print("\n  Nothing is eligible. Not-applicable reasons:")
            for task, reason in plan.not_applicable.items():
                print(f"    {task}: {reason}")
            continue

        batch = {spec.task_type: ATOMS[spec.task_type] for spec in plan.eligible}

        _sub("What Part 4's table scheduled AT THE FILE NODE, and what each declares")
        print(
            "  One scope, and since SPRINT_JOBS.md Phase 3 the table has more than one.\n"
            "  A batch is one NODE's tasks — which is what makes a derived edge below\n"
            "  mean anything — so this is the file node's, and the latent scopes are\n"
            "  listed after it.\n"
        )
        for spec in plan.eligible:
            atom = batch[spec.task_type]
            print(f"\n  {spec.task_type}   [{atom.cost_class}, writes {atom.write_mode}]")
            print(f"    consumes  {_facts(atom.consumes)}")
            print(f"    produces  {_facts(atom.produces)}")
            if atom.fanout is not None:
                # Registry-keyed, because that is what a bound's `reads` now names. Probe
                # has only written `matched`, so every ceiling whose evidence comes from a
                # later task says so instead of computing one.
                probed = {f"matched.patterns.{k}": v for k, v in patterns.items()}
                print(f"    ceiling   {_ceiling(atom, probed, spec.params)}")

        _sub("Ordering: what the table declares, and what the declarations derive")
        print(
            "  SPRINT_JOBS.md 2.3 claims `TaskRow.after` is redundant — that it is\n"
            "  derivable from consumes/produces. Only `@self` pairs make a within-node\n"
            "  edge; `@children` and `@subtree` are the settling walk's business.\n"
        )
        derived: dict[str, set[tuple[str, Fact]]] = {name: set() for name in batch}
        for producer, consumer, via in derive_edges(batch):
            derived[consumer].add((producer, via))

        table_pred = {spec.task_type: set(spec.after) for spec in plan.eligible}
        derived_pred = {
            name: {producer for producer, _ in pairs} for name, pairs in derived.items()
        }

        for spec in plan.eligible:
            task = spec.task_type
            mine = derived[task]
            print(f"  {task}")
            print(f"    table   after {sorted(table_pred[task]) if table_pred[task] else '—'}")
            if mine:
                for producer, via in sorted(mine, key=lambda pair: (pair[0], str(pair[1]))):
                    print(f"    atoms   after {producer}   via {via}")
            else:
                print("    atoms   after —")

            # THE COMPARISON IS OVER CLOSURES AND NOT OVER RAW EDGES. `citation` derives an
            # edge to `extract:text`; the table names only `structure:inferred`, which is
            # itself after `extract:text`. Compared edge by edge that reads as a second
            # divergence, and it is not one — it is the same ordering, stated one hop apart.
            table_closure = _closure(table_pred, task)
            derived_closure = _closure(derived_pred, task)
            unenforced = derived_closure - table_closure
            stricter = table_closure - derived_closure
            if unenforced:
                print(
                    f"    LIVE ORDERING BUG — the atoms require {sorted(unenforced)} first "
                    "and nothing in the table makes that happen"
                )
            elif stricter:
                print(f"    diverge — the table is stricter, by {sorted(stricter)}")
                for want in spec_consumes_offnode(batch[task]):
                    producers = sorted(
                        n for n, a in batch.items() if Fact(want.name, want.locus) in a.produces
                    )
                    if producers:
                        print(
                            f"              {task} consumes {want}, written by "
                            f"{', '.join(producers)}.\n"
                            f"              2.2 makes a @{want.locus} read the settling "
                            "walk's dependency, not a within-node edge."
                        )
            else:
                print("    agree")

        _sub("Consumptions with no producer in this batch")
        print(
            "  Not an error. `file` and `blob` come from the upload, and `matched` comes\n"
            "  from probe — which is not in Part 4's table, because it is what EVALUATES\n"
            "  that table.\n"
        )
        gaps = unproduced(batch)
        for name, consumers in sorted(gaps.items()):
            print(f"  {name}  consumed by {', '.join(sorted(consumers))}")
        if not gaps:
            print("  (none)")

        _sub("What is LATENT — scheduled on nodes that do not exist yet")
        print(
            "  SPRINT_JOBS.md 6.2. A vertex whose scope node exists now is instantiable;\n"
            "  one below an unresolved multiplicity is latent, and there is no instance of\n"
            "  it to name. Before Phase 3 these were literal TaskSpec tuples inside the\n"
            "  handlers that create the nodes, so a plan could not report them at all —\n"
            "  Part 0 records that as `EXPLAIN` stopping at the sheet list.\n"
        )
        eligible = {spec.task_type for spec in plan.eligible}
        latent = [
            row
            for row in TASK_ROWS
            if row.scope.kind == SCOPE_CHILDREN
            and any(name in eligible for name in row.scope.produced_by)
        ]
        for row in latent:
            fires = [name for name in row.scope.produced_by if name in eligible]
            print(f"  {row.task}   on {row.scope}")
            print(f"    reachable because {', '.join(fires)} is eligible here")
            print(f"    params    {resolve_options(detection.format)[row.params_key]}")
        if not latent:
            print("  (none — nothing this file schedules writes a node another rule wants)")
    return 0


# ---------------------------------------------------------------------------
# refuses — the errors the vocabulary raises, and the ordering bug it caught
# ---------------------------------------------------------------------------


def _shows(label: str, thunk) -> None:
    """Run something expected to raise, and print the message it raised with."""
    print(f"\n  {label}")
    try:
        thunk()
    except Exception as exc:  # noqa: BLE001 — printing the message IS the demo
        print(f"    {type(exc).__name__}: {exc}")
    else:
        print("    IT DID NOT RAISE — this demo is out of date with jmfts_core.atoms")


def cmd_refuses(args: argparse.Namespace) -> int:
    """What a declaration cannot say, and the ordering the closed vocabulary caught."""
    _rule("A closed vocabulary, and what it refuses")
    print(
        "  A typo in `consumes` would otherwise name evidence nothing produces. That\n"
        "  derives no edge, and an atom with no edges looks exactly like an atom that\n"
        "  genuinely depends on nothing — so the schedule would be wrong and silent."
    )
    _shows("a misspelled evidence name", lambda: fact("txt@self"))
    _shows("evidence with no locus at all", lambda: fact("text"))
    _shows("an unknown locus", lambda: fact("text@parent"))
    _shows(
        "`blob@ancestor` in `produces` — reading upward is not writing upward",
        lambda: fact("blob@ancestor", writing=True),
    )
    print("\n  the same fact in `consumes`, which is where it belongs")
    print(f"    {fact('blob@ancestor')}")

    _shows(
        "an atom that writes children and cannot identify them across a re-run",
        lambda: Atom(
            task_type="demo:no-key",
            consumes=(),
            produces=(fact("text@children", writing=True),),
            write_mode="children",
            cost_class="cpu",
            fanout=Fanout(bound=lambda e, p: (0, 1), reads=(), basis="demo", counts="chunk"),
        ),
    )
    _sub("And what the evidence registry refuses — Phase 2")
    print(
        "  `jmfts_core.atoms` closes the vocabulary of NAMES. `jmfts_core.evidence`\n"
        "  closes what a name's value may be, which is what a Part 4.4 guard needs\n"
        "  before it can compare anything to a threshold."
    )
    _shows("a name nothing registered", lambda: ev_get("sheet.measurments.rows"))
    _shows(
        "a flag where a count is declared — `isinstance(True, int)` is why this is checked",
        lambda: ev_check("sheet.measurements.rows", True),
    )
    _shows(
        "a null for a name that has no null result (3.2)",
        lambda: ev_check("extraction.characters", None),
    )
    print("\n  and the same null on a name that does have one")
    print(f"    source_span = {ev_check('source_span', None)!r}")

    _shows(
        "a natural key that does not say where the value lives",
        lambda: ChildKey(KEY_NATURAL),
    )

    _rule("The split that the audit forced: one name was doing two jobs")
    print(
        "  `profile:sheet` measures a worksheet and `extract:sheet` reads those\n"
        "  measurements. Both write into the `sheet` evidence row, so ONE evidence\n"
        "  name for the whole block makes each of them a producer of what the other\n"
        "  consumes. Here is that mistake, made on purpose:"
    )

    before = {
        "profile:sheet": Atom(
            task_type="profile:sheet",
            consumes=(fact("sheet@self"),),
            produces=(fact("sheet@self", writing=True),),
            write_mode="children",
            cost_class="cpu",
            fanout=Fanout(bound=lambda e, p: (1, 1), reads=(), basis="always 1"),
            child_key=ChildKey(KEY_NATURAL, path="profile.sheet_name"),
        ),
        "extract:sheet": Atom(
            task_type="extract:sheet",
            consumes=(fact("sheet@self"),),
            produces=(fact("sheet@self", writing=True),),
            write_mode="children",
            cost_class="cpu",
            fanout=Fanout(bound=lambda e, p: (0, 1), reads=(), basis="one per row"),
            child_key=ChildKey(KEY_POSITION),
        ),
    }
    _sub("one `sheet` name")
    for producer, consumer, via in sorted(derive_edges(before), key=lambda e: e[:2]):
        print(f"  {consumer} after {producer}   via {via}")
    print(
        "\n  Two edges, in both directions. One of them is the real ordering and the other\n"
        "  is its exact reverse, so a topological sort over this has no answer at all —\n"
        "  and the derivation cannot tell you which half is the wrong one."
    )

    _sub("three names, as declared today")
    after = {name: ATOMS[name] for name in ("profile:sheet", "extract:sheet")}
    for producer, consumer, via in sorted(derive_edges(after), key=lambda e: e[:2]):
        print(f"  {consumer} after {producer}   via {via}")
    print(
        "\n  One edge, forward. The `extract:sheet` ROW says the same thing by hand,\n"
        "  which is the redundancy 2.3 claims. The rule the split teaches: evidence is\n"
        "  named by what it ASSERTS, not by where it is stored."
    )
    return 0


# ---------------------------------------------------------------------------
# run — a real ingest, one task at a time, against the declarations
# ---------------------------------------------------------------------------


def _require_database() -> None:
    """Fail here, naming the two ways out, rather than deep inside the first query."""
    from sqlalchemy import text

    from jmfts_core.database import get_engine

    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1 FROM documents LIMIT 1"))
    except Exception as exc:  # noqa: BLE001
        print(
            f"`run` needs a reachable JMFTS database with the schema in place.\n\n"
            f"  {type(exc).__name__}: {exc}\n\n"
            "Point JMFTS_DB_* at one and run `jmfts-init-db` against it. A throwaway:\n"
            "  docker run -d --name atom-demo-pg -e POSTGRES_USER=jmfts \\\n"
            "      -e POSTGRES_PASSWORD=jmfts -e POSTGRES_DB=jmfts \\\n"
            "      -p 127.0.0.1:5434:5432 pgvector/pgvector:pg16\n"
            "  JMFTS_DB_PORT=5434 jmfts-init-db\n"
            "  JMFTS_DB_PORT=5434 python -m scripts.atom_demo run FILE",
            file=sys.stderr,
        )
        raise SystemExit(2) from None


class _Snapshot:
    """The whole tree, once, in a shape the checks below can ask two questions of.

    Taken BEFORE each task runs and again after, because both halves of the fan-out check
    are as-of a moment: the evidence a bound reads is what was on the node when the
    scheduler would have read it, and the children a task wrote are the difference between
    the two. One query, because a demo tree is small and two round trips per task would be
    the slowest part of the run.
    """

    def __init__(self, session):
        from sqlalchemy import text

        self.by_usetype: dict[int, dict[Optional[str], int]] = {}
        self.evidence: dict[int, dict] = {}
        self.parent: dict[int, Optional[int]] = {}
        children: dict[int, int] = {}

        rows = session.execute(
            text(
                "SELECT d.id, d.parent_id, d.path, d.usetype, "
                "       COALESCE(jsonb_object_agg(e.name, e.value) "
                "                FILTER (WHERE e.name IS NOT NULL), '{}'::jsonb) "
                "FROM documents d "
                "LEFT JOIN document_evidence e ON e.document_id = d.id "
                "GROUP BY d.id"
            )
        ).all()
        for node_id, parent_id, path, usetype, blocks in rows:
            self.parent[node_id] = parent_id
            if parent_id is not None:
                children[parent_id] = children.get(parent_id, 0) + 1
            for ancestor in list(path or []) + [node_id]:
                bucket = self.by_usetype.setdefault(ancestor, {})
                bucket[usetype] = bucket.get(usetype, 0) + 1
            self.evidence[node_id] = _evidence_from(blocks or {})
        for node_id, evidence in self.evidence.items():
            evidence["child_count"] = children.get(node_id, 0)

    def counts(self, node_id: int) -> dict[Optional[str], int]:
        return self.by_usetype.get(node_id, {})


#: Every name a bound may read, taken off the declarations rather than listed here.
_BOUND_READS: frozenset[str] = frozenset(
    name for atom in ATOMS.values() if atom.fanout for name in atom.fanout.reads
)


def _evidence_from(blocks: dict) -> dict:
    """The values a fan-out bound reads, pulled out of one node's evidence rows.

    THIS FUNCTION USED TO KNOW THE PATHS and Phase 2 is what took them off it. It held
    ``extraction.characters``, ``matched.patterns.sheet_count`` and
    ``sheet.measurements.rows`` as three hand-written lookups, with a comment saying it was
    provisional until 13.1's registry existed. :mod:`jmfts_core.evidence` is that registry,
    so the paths are now a lookup and the missing-value case is
    :data:`~jmfts_core.evidence.ABSENT` rather than a key that happens not to be there.

    ``blocks`` is ``{row name: value}``. Phase 2b moved it from ``structured_content`` to
    ``document_evidence``, and the only thing that changed here is the query that fills it —
    the registry answers where a leaf sits either way, which is what that seam is for.
    """
    evidence: dict = {}
    for name in _BOUND_READS:
        if ev_get(name).store.kind != "evidence":
            continue
        value = resolve(blocks, name)
        if value is not ABSENT:
            evidence[name] = value
    return evidence


def cmd_run(args: argparse.Namespace) -> int:
    """Upload one file, run the queue one task at a time, and check every declaration."""
    _require_database()

    from jmfts_client.contracts.upload import UploadedFile
    from jmfts_core.client import LocalJmftsClient
    from jmfts_core.database import get_session
    from jmfts_core.ingest_worker import IngestWorker
    from jmfts_core.models.task_queue import TaskQueue
    from jmfts_core.settling import NO_ROLLUP

    path = Path(args.file)
    data = path.read_bytes()

    client = LocalJmftsClient()
    uploaded = client.upload_file(UploadedFile(data=data, filename=path.name))
    root_id = uploaded.document_id

    _rule(f"{path}  ->  node {root_id}")
    print(
        "  The upload wrote `file` and `blob` and enqueued `probe`. Those two are the\n"
        "  EXTERNAL_EVIDENCE the derivation excuses: no atom produces them, and every\n"
        "  atom that reads bytes reads them from here."
    )

    # `NO_ROLLUP` is a named policy, not an omission: rollup is the settling walk's
    # business (INGEST_SPEC.md 5.4) and `summarize` is the atom it enqueues. Leaving it
    # out keeps this demo to the file-node ladder and off the model.
    worker = IngestWorker(worker_id="atom-demo", session_factory=get_session, planner=NO_ROLLUP)

    _sub("Every task the run claimed, against what its atom declared")
    print(
        "  `wrote` counts nodes that appeared in the scope node's subtree while the task\n"
        "  held it, by the usetype its ceiling counts.\n"
    )

    with get_session() as session:
        ran = 0
        while ran < args.max_tasks:
            # WHICH TASK RAN IS READ BACK, NOT PREDICTED. The worker claims by priority and
            # by satisfied dependencies, so "the lowest pending id" is a guess that would
            # eventually label one task's numbers with another task's name.
            before = _Snapshot(session)
            session.rollback()

            if not worker.run_once():
                break
            ran += 1
            session.expire_all()
            after = _Snapshot(session)

            row = (
                session.query(TaskQueue)
                .filter(TaskQueue.started_at.isnot(None))
                .order_by(TaskQueue.started_at.desc(), TaskQueue.id.desc())
                .first()
            )
            if row is None:
                print("  a task ran but no queue row records having started — stopping")
                break
            task_type, scope_id, params = row.task_type, row.scope_document_id, dict(row.params)

            atom = ATOMS.get(task_type)
            if atom is None:
                print(f"  {task_type}  — no atom; this task type is not a registered handler")
                continue

            print(f"  {task_type}   [{atom.cost_class}]  on node {scope_id}")
            agrees = "same" if atom.write_mode == row.write_mode else "DIFFERENT"
            print(
                f"    write mode  atom says {atom.write_mode}, "
                f"queue row says {row.write_mode} — {agrees}"
            )

            offnode = spec_consumes_offnode(atom)
            if offnode:
                print(
                    f"    the walk    {_facts(offnode)}\n"
                    "                satisfied by the settling walk having reached this "
                    "node, not by a task beside it"
                )

            upward = [f for f in atom.consumes if f.locus == "ancestor"]
            if upward:
                above = before.parent.get(scope_id)
                print(
                    f"    @ancestor   {_facts(tuple(upward))} — read off node {above}, "
                    f"not node {scope_id}"
                )

            # A FAILED TASK IS NOT A CEILING VIOLATION, and the difference has to be said
            # rather than left to the reader. A handler that raised wrote nothing, so
            # "0 children against a floor of 1" reads as a broken bound when what actually
            # happened is that the work never ran.
            if row.status != "completed":
                print(f"    outcome     {row.status} ({row.error_type}): {row.error}")
                print("                nothing was written, so the ceiling says nothing here\n")
                continue

            if atom.fanout is not None:
                usetype = atom.fanout.counts
                wrote = after.counts(scope_id).get(usetype, 0) - before.counts(scope_id).get(
                    usetype, 0
                )
                evidence = before.evidence.get(scope_id, {})
                missing = [n for n in atom.fanout.reads if n not in evidence]
                if missing:
                    print(f"    ceiling     not computable — {missing} was not on the node")
                else:
                    low, high = atom.fanout.bound(evidence, params)
                    verdict = "within it" if low <= wrote <= high else "OUTSIDE THE DECLARED BOUND"
                    reads = {n: evidence[n] for n in atom.fanout.reads}
                    print(f"    ceiling     {low}..{high} `{usetype}` nodes — {atom.fanout.basis}")
                    print(f"                from {reads}, read before the task ran")
                    print(f"    wrote       {wrote} — {verdict}")
            print()

        if ran >= args.max_tasks:
            print(f"  stopped at --max-tasks {args.max_tasks}; the queue was not empty")

        _sub("The tree that came out")
        final = _Snapshot(session)
        for usetype, count in sorted(final.counts(root_id).items(), key=lambda kv: (kv[0] or "")):
            print(f"  {usetype or '(no usetype)':13} {count}")
        # STILL TRUE, and Phase 2b changed what it means. `jmfts_core.evidence` IS read
        # by the appliance now — `EvidenceRepository` refuses a name it does not know, so
        # every write in the run above went through the registry. What no task consulted is
        # `ATOMS`: the declarations still describe the run rather than driving it, and
        # Phase 3 is what changes that.
        print(f"\n  {ran} tasks ran. Node {root_id} is the root; none of this read ATOMS.")
    return 0


# ---------------------------------------------------------------------------
# fixtures — real files to point `plan` at
# ---------------------------------------------------------------------------


def cmd_fixtures(args: argparse.Namespace) -> int:
    """Write the corpus fixtures somewhere `plan` and `run` can read them.

    Two of the files come from here rather than from ``tests/corpus``, and both are about
    reaching an atom the corpus does not reach.
    """
    from tests.corpus import fixtures

    destination = Path(args.directory)
    destination.mkdir(parents=True, exist_ok=True)
    written = fixtures.write_all(destination)

    # `minimal_pdf` builds `pdf-named.docx` and is not itself in the manifest, so
    # `write_all` never lands a `.pdf`. PDF is the one format that reaches `citation`,
    # which is the divergence `plan` exists to show.
    pdf = destination / "minimal.pdf"
    pdf.write_bytes(fixtures.minimal_pdf())
    written["minimal.pdf"] = pdf

    # THE CORPUS `.xlsx` IS BUILT FOR PROBE AND OPENPYXL CANNOT READ IT. It is the
    # smallest package that satisfies tier 1 — `probe` reads the ZIP manifest and
    # `xl/workbook.xml` and never opens a worksheet part — and that is deliberate:
    # `tests/corpus` commits no binaries and asserts nothing that needs the office extra.
    # `structure:sheets` is tier 2 and calls `openpyxl.load_workbook`, which wants the
    # relationship parts the fixture leaves out. So the sheet ladder needs a workbook
    # written by the library that will read it back.
    try:
        import openpyxl
    except ImportError:
        print('  (no `sheets.xlsx`: openpyxl is not installed — `pip install -e ".[office]"`)')
    else:
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = "Measurements"
        sheet.append(["run", "corpus", "recall@10", "seconds"])
        for row, corpus in enumerate(("multihop", "wiki", "arxiv", "email", "notes"), start=1):
            sheet.append([row, corpus, round(0.60 + row / 40, 3), 12 * row])
        workbook_path = destination / "sheets.xlsx"
        book.save(workbook_path)
        written["sheets.xlsx"] = workbook_path

    for name, target in sorted(written.items()):
        print(f"  {target}")
    print(f"\n  {len(written)} files. They are generated, deterministic, and carry no real data.")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("atoms", help="every declaration, one table").set_defaults(func=cmd_atoms)

    p_fixtures = sub.add_parser("fixtures", help="write the corpus fixtures to a directory")
    p_fixtures.add_argument("directory")
    p_fixtures.set_defaults(func=cmd_fixtures)

    p_plan = sub.add_parser("plan", help="probe a file, plan from it, derive the order")
    p_plan.add_argument("files", nargs="+")
    p_plan.set_defaults(func=cmd_plan)

    sub.add_parser("refuses", help="what the vocabulary rejects").set_defaults(func=cmd_refuses)

    p_run = sub.add_parser("run", help="a real ingest, checked against the declarations")
    p_run.add_argument("file")
    p_run.add_argument("--max-tasks", type=int, default=200)
    p_run.set_defaults(func=cmd_run)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
