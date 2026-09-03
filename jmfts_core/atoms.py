"""What each task handler reads and writes, declared beside the handler itself.

``SPRINT_JOBS.md`` Part 2. An atom is a registered function that does one thing to a
node; this module is the vocabulary it declares itself in, and nothing here runs any of
them. :func:`~jmfts_core.ingest_tasks.register_task_handler` requires a declaration, so
:data:`ATOMS` and :data:`~jmfts_core.ingest_tasks.TASK_HANDLERS` cannot come to hold
different sets of task types.

**This module is Phase 1 and it changes no behaviour.** Nothing in the ingest path reads
:data:`ATOMS`, and that is still true after Phase 3: a scope, a stamp and a frontier are
read out of ``TASK_ROWS`` and off the node, never from here. What reads this is
``tests/test_atom_declarations.py``, which derives the dependency edges the declarations
imply and compares them against the ``after`` and ``after_any`` fields ``TASK_ROWS`` states
by hand. Part 2.3 claims those two fields are redundant — derivable rather than declared —
and that test is what makes the claim falsifiable before a phase acts on it. **No phase has
yet.** An earlier version of this paragraph expected Phase 3 to, and Phase 3 gave a row a
scope instead: what it needed from 2.3 was the confidence that a row's ordering fields are
consistent with its declarations, not their deletion.

One consequence of scope worth stating here, because it is what a reader of
:func:`derive_edges` will want: a batch is now a FILTER over ``TASK_ROWS`` rather than a
list somebody writes. ``among`` was always meant to be one node's atoms (2.3's finding 3),
and a row states which node it runs on, so the test no longer has to be told.

A LOCUS IS PART OF EVERY PAIR, and 2.2 is why. A rollup reads ``effective_content`` from
its children and writes ``effective_content`` to itself, so bare evidence names give the
dependency graph a self-edge for every rollup in the appliance, and an acyclicity check
whose exception fires on every rollup is not an exception. The locus separates the two
sides, and it also says WHICH mechanism enforces the pair:

============  =========================================================
Locus         Enforced by
============  =========================================================
``self``      the sort over one node's own tasks — a real within-node edge
``children``  the settling walk: children settle before the parent is evaluated
``subtree``   the settling walk, over the whole subtree
============  =========================================================

``X@children`` is never produced *within* the node that consumes it, so it contributes no
within-node edge at all. That is what makes the within-node graph genuinely acyclic
instead of acyclic-with-a-carve-out.

THERE IS A FOURTH LOCUS AND 2.2 DOES NOT HAVE IT. That section says a locus is drawn from
the same three values ``write_mode`` uses, and the audit falsified it: ``profile:sheet``
and ``extract:sheet`` are scoped to a sheet node and read the workbook bytes off the FILE
NODE ABOVE, which no downward locus can say. :data:`LOCUS_ANCESTOR` is that read, and it is
legal in ``consumes`` only — the three write modes are the regions a task may WRITE, and
reading upward is not writing upward. See ``run_profile_sheet``'s "The blob is on the
PARENT".

An ancestor read creates no within-node edge and needs no walk, because the evidence is
already there: an ancestor is settled enough to have children only if what it holds was
written before they existed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from jmfts_core.models.task_queue import WRITE_MODES, WRITE_SELF

# ---------------------------------------------------------------------------
# Loci
# ---------------------------------------------------------------------------

#: Evidence on a node ABOVE the one the task is scoped to. Readable, never writable, so it
#: appears in ``consumes`` and is rejected in ``produces``. See the module docstring.
LOCUS_ANCESTOR = "ancestor"

#: Every locus an atom may name. The three write modes plus the upward read.
LOCI: tuple[str, ...] = WRITE_MODES + (LOCUS_ANCESTOR,)

# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------

#: The record of what was uploaded: filename, declared mime, size. Written by the upload
#: itself, which is not a task, so nothing in :data:`ATOMS` produces it.
EV_FILE = "file"

#: the ``source`` evidence row — where a document is to be FETCHED FROM, when it was
#: named rather than uploaded: a URL, an arXiv id, a path on this host. ``SPRINT_JOBS.md``
#: 15.4 S8.
#:
#: The one piece of evidence that exists before the bytes do, and the reason the three
#: ``fetch:*`` atoms are the only ones that produce :data:`EV_BLOB` and :data:`EV_FILE`.
#: Written by the request, like `file` and `blob` are for an upload, so it is
#: :data:`EXTERNAL_EVIDENCE` too — the difference is that here a TASK produces the bytes it
#: names, which is what makes a flaky network a retry in machinery that already exists
#: rather than a request that hangs.
EV_SOURCE = "source"

#: The stored bytes, through :class:`~jmfts_core.repositories.blob.BlobRepository`. Also
#: written by the upload and by no atom. It is named here rather than left implicit
#: because four handlers read it, and a handler that reads bytes is a handler that fails
#: differently when the blob is gone — see ``run_probe``'s "bytes it describes are gone".
EV_BLOB = "blob"

#: the ``matched`` evidence row — the detected format and the probed patterns, in the
#: two separate fields spec 3.1 asks for. Every Part 4 condition is a predicate over this.
EV_MATCHED = "matched"

#: ``Document.content``. The node's own text, which for a file node is the whole extracted
#: markdown and for a chunk is the chunk.
EV_TEXT = "text"

#: the ``extraction`` evidence row — which reader ran, and how much it yielded. A
#: separate name from :data:`EV_TEXT` because ``citation`` consumes this and not the text:
#: it dispatches on ``extraction.source`` and refuses a source it cannot invert.
EV_EXTRACTION = "extraction"

#: the ``structure`` evidence row — the rung that built the tree below this node, its
#: source, and its coverage.
EV_STRUCTURE = "structure"

#: the ``sheet`` evidence row's identity fields — index, name, state. Written by
#: ``structure:sheets`` onto the node it creates, and read by both per-sheet tasks to know
#: which worksheet in the parent's workbook they are scoped to.
EV_SHEET = "sheet"

#: ``sheet.measurements`` — 8.3's counted signals, and the header
#: verdict and column labels ``extract:sheet`` reads rather than re-deriving.
#:
#: A SEPARATE NAME FROM :data:`EV_SHEET`, and the audit is what forced the split. Both
#: per-sheet tasks write into the ``sheet`` block, so one name for the whole block makes
#: ``extract:sheet`` a producer of what ``profile:sheet`` consumes — which derives the
#: ordering backwards. The two names are two different facts and the block they share is an
#: implementation detail of where they are stored.
EV_SHEET_MEASUREMENTS = "sheet.measurements"

#: ``sheet.shape`` and ``sheet.shape_decision`` — 8.4's verdict, and
#: the inputs it was or was not taken from. Written by ``extract:sheet``; nothing consumes
#: it yet, because 8.8's calibration is a query over stored profiles rather than a task.
EV_SHEET_SHAPE = "sheet.shape"

#: the ``profile`` evidence row on the profile node ``profile:sheet`` writes.
EV_PROFILE = "profile"

#: The typed values of one spreadsheet row, on the record node ``extract:sheet`` writes.
#: Spec 8.4's ``records`` shape, and a SEPARATE NAME FROM :data:`EV_TEXT` because the two
#: are different renderings of the row: ``content`` is labelled prose for the embedder and
#: this keeps the values, so a retrieval hit returns data rather than a string to re-parse.
#:
#: PHASE 2 ADDED IT, and the evidence registry's ownership audit is what found it missing.
#: ``extract:sheet`` declared that it produced ``text@children`` and nothing else, so the
#: three keys it writes beside that text belonged to no evidence name — which made them the
#: caller's, and a metadata PATCH deleted them.
EV_RECORD = "record"

#: The vectors: ``Document.embed``, and the token rows when they were asked for. Not a
#: an evidence row — it is columns and a table — but it is evidence in exactly
#: the sense that matters here, because ``structure:semantic`` cannot run without it.
EV_EMBEDDING = "embedding"

#: the ``effective_content`` evidence row — 11.4's record of how a container's
#: embedded text was arrived at, and by which method.
EV_EFFECTIVE_CONTENT = "effective_content"

#: the ``source_span`` evidence row on a chunk: where the chunk sits in the file
#: node's markdown. Written by the chunker, consumed by ``citation``, and the reason the
#: two are ordered at all.
EV_SOURCE_SPAN = "source_span"

#: the ``source_anchor`` evidence row on a chunk: the page and the rectangles.
EV_SOURCE_ANCHOR = "source_anchor"

#: Every evidence name an atom may name. A closed vocabulary on purpose: a typo in a
#: ``consumes`` entry would otherwise name evidence nothing produces, which derives no edge
#: and looks exactly like an atom that genuinely depends on nothing.
EVIDENCE: frozenset[str] = frozenset(
    {
        EV_FILE,
        EV_BLOB,
        EV_SOURCE,
        EV_MATCHED,
        EV_TEXT,
        EV_EXTRACTION,
        EV_STRUCTURE,
        EV_SHEET,
        EV_SHEET_MEASUREMENTS,
        EV_SHEET_SHAPE,
        EV_PROFILE,
        EV_RECORD,
        EV_EMBEDDING,
        EV_EFFECTIVE_CONTENT,
        EV_SOURCE_SPAN,
        EV_SOURCE_ANCHOR,
    }
)

#: Evidence the ingest pipeline never produces, because the REQUEST writes it before any
#: task exists. Named so that :func:`derive_edges` can tell "nothing produces this" apart
#: from "nothing produces this AND that is a bug".
#:
#: :data:`EV_FILE` and :data:`EV_BLOB` are here for the upload's sake and are nonetheless
#: produced by the three ``fetch:*`` atoms, which is not a contradiction: a document can
#: arrive as bytes or as a locator, and only the second has a task that turns one into the
#: other. :data:`EV_SOURCE` is external in the plain sense — nothing produces it, ever.
EXTERNAL_EVIDENCE: frozenset[str] = frozenset({EV_FILE, EV_BLOB, EV_SOURCE})


@dataclass(frozen=True)
class Fact:
    """One ``(evidence, locus)`` pair — half of a ``consumes`` or ``produces`` entry."""

    name: str
    locus: str

    def __str__(self) -> str:
        return f"{self.name}@{self.locus}"


def fact(spec: str, *, writing: bool = False) -> Fact:
    """Parse ``"text@self"``. Both halves are checked against a closed set.

    ``writing`` narrows the locus to the three write modes, because
    :data:`LOCUS_ANCESTOR` is a read and an atom that claimed to produce evidence upward
    would be claiming a region the queue has no way to reserve.
    """
    name, sep, locus = spec.partition("@")
    if not sep:
        raise ValueError(f"evidence {spec!r} names no locus; write it as `{spec}@self`")
    if name not in EVIDENCE:
        raise ValueError(
            f"unknown evidence {name!r} in {spec!r}; known names are {sorted(EVIDENCE)}. "
            "A new one is a constant in jmfts_core.atoms, not a string here"
        )
    allowed = WRITE_MODES if writing else LOCI
    if locus not in allowed:
        if writing and locus == LOCUS_ANCESTOR:
            raise ValueError(
                f"{spec!r} produces evidence at {LOCUS_ANCESTOR!r}; a task writes only "
                f"within {WRITE_MODES}, and no write mode reserves a node above the one "
                "the task is scoped to"
            )
        raise ValueError(f"unknown locus {locus!r} in {spec!r}; expected one of {allowed}")
    return Fact(name, locus)


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------

#: Counts, parses, reads bytes. The tokenizer is in this class and not the next one: it
#: loads no weights and touches no GPU (``run_profile_sheet``'s "No model runs" says the
#: same thing about the same call).
COST_CPU = "cpu"

#: Runs the embedding model — a forward pass, on whatever device
#: :func:`~jmfts_core.embedder.get_embedder` resolves to, possibly another host.
COST_MODEL = "model"

#: Calls the configured LLM endpoint. An atom that calls an LLM *and* embeds is ``llm``:
#: the class is the most expensive thing the atom does, because it is what a budget and a
#: badge have to be sized for.
COST_LLM = "llm"

COST_CLASSES: tuple[str, ...] = (COST_CPU, COST_MODEL, COST_LLM)


# ---------------------------------------------------------------------------
# Fan-out
# ---------------------------------------------------------------------------

#: Children are told apart across re-runs by their order under the parent. The default,
#: and the weakest of the three: it survives a re-run that produces the same children in
#: the same order and nothing else. ``SPRINT_JOBS.md`` 9.2, and open question 4 there is
#: about exactly this.
KEY_POSITION = "position"

#: Children are told apart by a value the source itself names — a sheet name, a record id.
#: The strongest form, because it survives reordering.
KEY_NATURAL = "natural"

#: Children are told apart by the hash of their content. Survives reordering and renaming,
#: and matches nothing when the content changed at all — which for a chunk is the point.
KEY_CONTENT_HASH = "content_hash"

KEY_KINDS: tuple[str, ...] = (KEY_POSITION, KEY_NATURAL, KEY_CONTENT_HASH)


@dataclass(frozen=True)
class ChildKey:
    """What identifies one produced child across re-runs. ``SPRINT_JOBS.md`` 9.1 and 9.2.

    ``path`` is required for :data:`KEY_NATURAL` and refused for the other two, because a
    natural key that does not say where it lives cannot be read by anything.
    """

    kind: str
    path: Optional[str] = None

    def __post_init__(self) -> None:
        if self.kind not in KEY_KINDS:
            raise ValueError(f"unknown child key kind {self.kind!r}; expected one of {KEY_KINDS}")
        if self.kind == KEY_NATURAL and not self.path:
            raise ValueError("a natural child key must name where the value lives")
        if self.kind != KEY_NATURAL and self.path:
            raise ValueError(f"a {self.kind} child key takes no path; {self.path!r} was given")


#: What a fan-out bound returns: ``(low, high)`` children, inclusive. ``high`` is the one
#: that matters — 2.4 — because what blows a budget is the ceiling, and ``⌈N/max_children⌉``
#: containers tells nobody whether to press the button.
Interval = tuple[int, int]


@dataclass(frozen=True)
class Fanout:
    """How many children an atom may write, as a function of evidence and parameters.

    ``reads`` names every evidence key ``bound`` is allowed to look at, and the audit test
    checks that by handing ``bound`` a mapping that records its lookups. A bound that
    reached for an undeclared measurement would be a scheduling decision made from
    evidence the plan does not know it needs.

    THE NAMES IN ``reads`` ARE REGISTRY NAMES — Phase 2. They were bare (``characters``,
    ``rows``) while nothing could resolve them, and a caller wanting to know where
    ``characters`` came from had to keep its own table saying "``extract:text`` writes it";
    ``scripts/atom_demo.py`` kept exactly that table and said in a comment that it was
    provisional. :mod:`jmfts_core.evidence` is what it was waiting for, so a bound now names
    ``extraction.characters`` and the store path, the type and the producer are lookups.
    """

    bound: Callable[[dict, dict], Interval]
    reads: tuple[str, ...]
    #: Prose for ``EXPLAIN``, in the terms 2.4's table uses.
    basis: str
    #: The ``usetype`` of the children this bound counts — the OTHER table
    #: ``scripts/atom_demo.py`` had to keep, and for the same reason. An interval is not
    #: checkable against a run without it: six atoms write children and four of them write
    #: children of a different kind from each other, so "wrote 5" has to say five WHAT
    #: before it can be held against a ceiling. Not derivable from ``produces``, which names
    #: evidence rather than node kinds.
    counts: str


def fixed(count: int, *, counts: str) -> Fanout:
    """A bound for an atom that writes exactly ``count`` children whatever it is given."""
    return Fanout(
        bound=lambda evidence, params: (count, count),
        reads=(),
        basis=f"always {count}",
        counts=counts,
    )


# ---------------------------------------------------------------------------
# The atom
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Atom:
    """One handler's declaration. 2.1's table, one field per row.

    ``write_mode`` is the mode a FIRST run of this atom takes, and it is still the only
    mode. 9.3 moves it onto the plan vertex, because a re-run that deletes an unmatched
    child writes ``subtree`` where the first run wrote ``children`` — an earlier version of
    this docstring said that was Phase 3's, and Phase 3 deliberately did not build it. 9.1's
    matching is what makes a re-run delete anything, nothing re-runs a fan-out rule yet (the
    attempt diff prevents it), so the escalation would reserve a region for a delete that
    cannot happen. Both land in the phase that makes re-derivation happen. ``produced_by``,
    which is what the planner would count to tell a first run from a re-run, is the column
    they were waiting on and it exists now.
    """

    task_type: str
    consumes: tuple[Fact, ...]
    produces: tuple[Fact, ...]
    write_mode: str
    cost_class: str
    fanout: Optional[Fanout] = None
    child_key: Optional[ChildKey] = None

    def __post_init__(self) -> None:
        if self.write_mode not in WRITE_MODES:
            raise ValueError(
                f"atom {self.task_type!r} declares write mode {self.write_mode!r}; "
                f"expected one of {WRITE_MODES}"
            )
        if self.cost_class not in COST_CLASSES:
            raise ValueError(
                f"atom {self.task_type!r} declares cost class {self.cost_class!r}; "
                f"expected one of {COST_CLASSES}"
            )
        # A fan-out atom without a child key cannot be re-run idempotently (9.1), and one
        # with a child key and no fan-out is claiming to identify children it never writes.
        # Both are declaration errors and neither is survivable: the first would silently
        # rebuild a subtree the second run did not need to touch.
        if (self.fanout is None) != (self.child_key is None):
            raise ValueError(
                f"atom {self.task_type!r} declares fanout={self.fanout is not None} and "
                f"child_key={self.child_key is not None}; an atom that writes children "
                "needs both (SPRINT_JOBS.md 9.1), and one that writes none needs neither"
            )


#: Every declared atom, by task type. Populated by
#: :func:`~jmfts_core.ingest_tasks.register_task_handler`, which requires the declaration,
#: so this is the same key set as ``TASK_HANDLERS`` by construction rather than by test.
ATOMS: dict[str, Atom] = {}


def declare(
    task_type: str,
    *,
    consumes: tuple[str, ...],
    produces: tuple[str, ...],
    write_mode: str,
    cost_class: str,
    fanout: Optional[Fanout] = None,
    child_key: Optional[ChildKey] = None,
) -> Atom:
    """Build and register one atom. Re-declaring the same task type replaces the entry.

    Replacement rather than collision, because the collision that matters — two different
    HANDLERS under one name — is caught by ``register_task_handler``, and this is called
    from there. A module re-imported under ``--reload`` re-declares an identical atom.
    """
    atom = Atom(
        task_type=task_type,
        consumes=tuple(fact(spec) for spec in consumes),
        produces=tuple(fact(spec, writing=True) for spec in produces),
        write_mode=write_mode,
        cost_class=cost_class,
        fanout=fanout,
        child_key=child_key,
    )
    ATOMS[task_type] = atom
    return atom


# ---------------------------------------------------------------------------
# Derivation — what Part 2.3 claims `after` and `after_any` are
# ---------------------------------------------------------------------------

#: One derived edge: ``consumer`` must run after ``producer``, because of ``via``.
Edge = tuple[str, str, Fact]


def producers_of(name: str, locus: str, *, among: Optional[dict[str, Atom]] = None) -> set[str]:
    """Every atom that writes ``name`` at ``locus``."""
    atoms = ATOMS if among is None else among
    return {t for t, atom in atoms.items() if Fact(name, locus) in atom.produces}


def derive_edges(among: Optional[dict[str, Atom]] = None) -> set[Edge]:
    """The within-node ordering the declarations imply. Part 2.3's claim, made concrete.

    ``among`` IS NOT AN OPTIMISATION AND THE DEFAULT IS THE WRONG THING TO PASS. An edge
    orders two tasks ON ONE NODE, so it is only meaningful over a set of atoms that can be
    scoped to the same node — one batch, which is what Part 8 means by a plan vertex's
    scope. Over every atom at once the derivation over-approximates, and the audit found
    the case: ``embed`` consumes ``text@self`` on a CHUNK and ``extract:text`` produces
    ``text@self`` on a FILE NODE, so the two share a fact name and a locus and are never
    on one node. Callers pass the batch.

    ONLY ``@self`` PAIRS PRODUCE AN EDGE, and 2.2's table is the reason. A ``@children`` or
    ``@subtree`` consumption is satisfied by the settling walk having reached this node,
    not by another task on the same node — so an edge for it would order two tasks that
    are not on the same node in the first place.

    :data:`LOCUS_ANCESTOR` produces no edge either, and for a third reason again: the
    evidence is on a different node and was written before this node existed.

    An unproduced ``@self`` consumption is not an error here. Two of them are legitimate:
    ``file`` and ``blob`` come from the upload (:data:`EXTERNAL_EVIDENCE`), and ``matched``
    comes from ``probe``, which is not in Part 4's table at all because it is what
    evaluates that table. Callers that care ask :func:`unproduced` for the list.
    """
    atoms = ATOMS if among is None else among
    edges: set[Edge] = set()
    for consumer, atom in atoms.items():
        for want in atom.consumes:
            if want.locus != WRITE_SELF:
                continue
            for producer in producers_of(want.name, want.locus, among=atoms):
                if producer != consumer:
                    edges.add((producer, consumer, want))
    return edges


def unproduced(among: Optional[dict[str, Atom]] = None) -> dict[str, set[str]]:
    """Evidence that is consumed and produced at NO locus, by evidence name.

    At no locus, rather than at the consuming locus, and the audit is what forced that.
    ``profile:sheet`` consumes ``sheet@self`` on a sheet node, and the atom that wrote it
    is ``structure:sheets``, which produced it at ``@children`` from the node above. A
    check that matched loci would call that unproduced, when what it actually is is the
    ordinary parent-writes-child relationship every fan-out atom has with its children.

    :data:`EXTERNAL_EVIDENCE` is excluded — the upload writes it and no atom ever will.
    What remains is evidence nothing in the appliance writes, which is a declaration
    error rather than a finding: an atom is waiting on a fact that does not exist.
    """
    atoms = ATOMS if among is None else among
    out: dict[str, set[str]] = {}
    written = {produced.name for atom in atoms.values() for produced in atom.produces}
    for consumer, atom in atoms.items():
        for want in atom.consumes:
            if want.name in EXTERNAL_EVIDENCE or want.name in written:
                continue
            out.setdefault(want.name, set()).add(consumer)
    return out
