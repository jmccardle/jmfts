# Sprint: The job system

Companion to `INGEST_SPEC.md` and `SPRINT_0_3_0.md`. Written 2026-08-27.

**Reviewed for publication 2026-09-15** (`SPRINT_0_6_0.md` Block E step 4). This document
already corrects itself as it goes — a `landed`, a `DECIDED`, a `What Phase N found` — and
the review pass found only three claims about the tree that had gone false and were not
already marked: 3.1's four registry counts, 4.4's example sentence about
`has_heading_styles`, and Part 14's citation count. All three are corrected in place with a
dated note; nothing else was touched. **Phases 5, 6 and 7 have NOT shipped**, so Part 14's
table is current about what is left: multiplicity and the cost fold, rule sets and bindings,
and the generalised walk. `SPRINT_0_3_0.md` and `SPRINT_0_5_0.md` are cited throughout and
stay held back by decision — see `README.md`'s "A note on documentation".

> **Corrected 2026-10-12: phase 5 HAS shipped, and so has 5.1's collection walk.** Both
> landed on 2026-09-15 in commits this review pass had not seen. `6565385` is phase 5 —
> `PlanCost` in `jmfts_core/ingest_tasks.py` folds tasks, child nodes, embeddings and LLM
> calls as intervals and names what it could not count, which is Part 7. `0aa14ec` built
> 5.1: `GET`/`PUT /documents/{document_id}/ingest-options` and `collection_option_layers`,
> and `580c1df` then deleted `rollup_tasks._options_owner`, the function 5.1 quotes as
> "the walk already runs". Phases 6 and 7 are what is left. Part 14's row 5 and 5.1's quote
> are marked where they sit; `ROADMAP.md`'s job-system table carries the dates.

**Revised 2026-08-28**, after the design was read back against the code it
describes. Six things changed and each is marked where it sits: the planner-2
count (Part 0), the atom list against the registered handlers (Part 2), the
claim that the walk removes every handler-side enqueue (Parts 6 and 9), the
re-run rule (Part 9.1, now decided), bind-time check 8 (Part 10), and the
phasing (Part 14, new). Verified in the tree at `cc7eeae`.

**Phase 1 landed 2026-08-28** and its six results are in 2.5. Two of them
corrected this document rather than the code: there is a fourth locus (2.2), and
a derived edge belongs to a batch rather than to a pair of atom names (2.3).
Part 2.3's claim — the one everything after Phase 3 rests on — held for eleven
of the twelve atoms, with `citation` diverging exactly as 14.1 predicted.

**Phase 3 landed 2026-09-01.** A rule names its scope, a node records which rule
produced it, and the five handlers that carried a literal task list ask one
planner instead. All four of Part 0's measured consequences close, 13.2's open
question 4 is answered, and `INGEST_SPEC.md` Part 4 gains a `Scope` column in the
same commit. 14.3 is the record and it names two places where 4.1 as written here
was too narrow to build against.

**Phase 4 landed 2026-09-01.** The design was taken by working backwards from
what a person would ask an appliance to do differently, rather than forwards from
the grammar 4.4 proposed. Three of that section's four sentences survive and the
fourth would have emptied every `.docx`; 4.4's correction subsections are the
record, and the second of them is a threshold the design wrote as a requirement
that had to become an exclusion once the per-format pattern coverage was
measured. Two decisions came with the phase — what "ingest" means, and that a
collection is a subtree rather than a table (5.1) — and both reach further than
Phase 4. 14.4 is the summary, and its last subsection is what the build changed.

**This precedes `SPRINT_0_3_0.md` steps 7, 8 and 9.** Those three add a
calibration sweep, a workbook-level `propose:links`, and rows-to-entities. Each
wants a knob bound to a scope, a count estimated before the work runs, or a pass
cast at a subtree that already exists. The current job system supports none of
the three, and each step that lands before this one lands adds another
hand-written path around it.

**Steps 10 and 11 are a weaker claim and the earlier wording overstated it.**
Step 10 is entity resolution and does not touch the ingest spine at all. Step 11
needs a shape bound to a scope, which is `shape_bindings` from
`SPRINT_0_3_0.md` step 4 — the same table shape as a rule binding (Part 5), but
a different table and a different dependency. Neither is blocked by this sprint.

The scope of this sprint is the ingest spine. It does not change what any
existing task computes. It changes how a task is declared, scoped, ordered,
counted and re-run.

---

## Part 0 — What exists

Verified in the tree at `2a55363`, not recalled.

**Read in the past tense from Phase 3 onward.** Everything below was true when it
was measured and most of it is not true now; 14.3 says what each finding became.
It is kept as written because the four consequences it measures are what the
phases after it were sized against, and a rewritten diagnosis would make the
sprint look easier than it was.

### There are three planners and they do not know about each other

| # | Where | Scope | Shape |
|---|---|---|---|
| 1 | `plan_after_probe` (`ingest_tasks.py:698`) | the file node | declarative table |
| 2 | handler-internal `enqueue_batch` calls | whatever the handler chose | imperative |
| 3 | `IngestRollupPlanner` (`rollup_tasks.py:110`) | a node whose subtree finished | late-bound |

Planner 1 is the good one. `TASK_ROWS` (`ingest_tasks.py:423-550`) is a table of
rows whose conditions may only name patterns, and `plan_after_probe` evaluates
it as a pure function of `(format, patterns, options)`. That restriction is what
`EXPLAIN` rests on.

**Planner 2 is six sites, not one.** An earlier draft named
`run_structure_sheets` alone. Every handler that creates a node also enqueues
that node's work, and five of the six carry a literal `TaskSpec`:

| Site | What it enqueues | Literal? |
|---|---|---|
| `sheet_tasks.py:240` | `SHEET_TASK_SPECS` per sheet node | yes |
| `sheet_tasks.py:421` | `EMBED_CHUNK_SPEC` on a profile node | yes |
| `sheet_tasks.py:564` | `EMBED_CHUNK_SPEC` per row node | yes |
| `structure_tasks.py:845` | `EMBED_CHUNK_SPEC` per chunk node | yes |
| `rollup_tasks.py:544` | the `summarize:llm` handoff | yes |
| `rollup_tasks.py:432` | the container's first task | **no — it calls the planner** |

The last row is the shape the other five should have. `run_structure_semantic`
writes `specs = planner(session, container)` and enqueues what comes back, so a
container that is itself too wide segments before it summarizes without that
rule being written twice.

**What this sprint removes is the literal list, not the call.** Part 6 and
Part 9 say why: the settling walk travels upward only, so a freshly created child
is unreachable from below and its work must be enqueued where it is created.

`SHEET_TASK_SPECS`'s two per-sheet tasks are `TaskSpec` values with literal
`params` dicts, which is what makes their knobs unreachable (below).

### `TaskRow` and `TaskSpec` are two declarations of one idea

`TaskRow` (`ingest_tasks.py:347-420`) carries `task`, `write_mode`, `requires`,
`forbids`, `after`, `after_any`, `params_key`, `skip_reason`. `params_key` names
a group in `jmfts_core.ingest_options`, which resolves task defaults, then a
per-format profile, then caller overrides, and **raises on an override naming a
key that does not exist**.

`TaskSpec` (`settling.py:78-99`) carries `task_type`, `write_mode`, `params`,
`priority`, `service_badge`, `after`, `max_retries`. `params` is a literal dict.
There is no `params_key`.

### Four consequences, each measured — all four closed by Phase 3

* **The sheet knobs cannot be set.** `ingest_service.py:359` resolves a caller's
  `options` at upload and stores them on the node, but only the `structure` and
  `rollup` groups exist. `max_rows`, `with_cell_notes` and `sketch_columns` are
  not reachable from any caller.

* **`sheet_tasks.py:135` claims otherwise.** It records that `max_rows` and
  `with_cell_notes` "are parameters rather than configuration" so that "a
  re-ingest that raises the ceiling is a different request from the one that
  failed on it". No request can vary them, so that fingerprint never changes.

* **`EXPLAIN` stops at the sheet list.** `_explain_concrete` and
  `_explain_conditional` both iterate `TASK_ROWS`. `profile:sheet` and
  `extract:sheet` are not rows. `plan_after_probe`'s own docstring
  (`ingest_tasks.py:711`) claims the table covers "the thirteen tasks Part 4
  lists and Part 8's two per worksheet"; that sentence is not true of the code.

* **The validation `ingest_options` exists to provide is absent.**
  `ingest_options._positive_int` rejects `bool` on purpose, because
  `isinstance(True, int)` is true and `max_tokens: true` would pack to one
  token. `sheet_tasks.py:503` reads `int(params.get("max_rows", ...))`, and
  `int(True)` is `1`. Latent rather than live — nothing can feed it a bool
  today, precisely because nothing can feed it anything.

### The settling walk already knows the central lesson

`settling.py`'s module docstring opens with it: *"A task list that names the
tree as it currently stands goes stale the moment a child is added, removed, or
moved — so there are no sentinel tasks here and no precomputed cross-level
dependency arrays. Each task, when it finishes, walks one step up and re-reads
the tree."*

That is correct, and it is currently applied to one layer. Part 6 makes it the
general rule.

### Two formats cost nothing and one cost a tier

`.docx` and `.pptx` added **zero rows**. They added a reader behind
`extract:text` (`office/extract.py`, both to markdown) and an entry in the
`@declared_structure` sentinel dict. The structure handlers consume text and an
outline and know nothing about what produced them.

`.xlsx` added one row (`structure:sheets`) and a tier of tasks below the table.
`SPRINT_0_3_0.md` 13.8 argues that divergence correctly: `TASK_ROWS` is
evaluated once per uploaded file, and a per-sheet task is scoped to a node that
does not exist at probe time. `embed` is not a row for the same reason.

**So the shoehorning is not about spreadsheets.** It is that a `TaskRow` has no
way to name a scope other than the file node.

**It has one now (Phase 3), and `embed` and both per-sheet tasks are rows.** What
did NOT change is the paragraph above it: a per-sheet task is still scoped to a
node that does not exist at probe time, and it is still enqueued by the rung that
creates that node. 13.8's argument was correct about the tasks and wrong only
about the table.

---

## Part 1 — The diagnosis

Two things are missing, and one thing is declared twice.

1. **A rule cannot name its scope.** Every `TaskRow` is implicitly scoped to the
   uploaded file. A task scoped to a sheet, a chapter, or an arbitrary subtree
   has nowhere to be declared, so it is written in a handler.

2. **A task that creates children does not declare its count.** So no pass can
   estimate the work without doing it, and `EXPLAIN` reports which tasks fire
   rather than how much they will do.

3. **Ordering is declared twice.** `structure:declared` carries
   `requires=("has_text_layer", @declared_structure)` and
   `after=(TASK_EXTRACT_TEXT,)`. The second is derivable from the first if a
   task declares what evidence it produces.

---

## Part 2 — Atoms

An atom is a registered function that does one thing to a node. It is code, not
data.

**Twelve functions carry `@register_task_handler` today**, and that is the set an
atom declaration has to cover first: `probe`, `extract:text`,
`structure:declared`, `structure:inferred`, `structure:sheets`,
`structure:semantic`, `profile:sheet`, `extract:sheet`, `embed`, `summarize`,
`summarize:llm`, `citation`.

An earlier draft listed thirteen names and four of them are not handlers.
`chunk` and `reparent` are steps INSIDE `structure:inferred` and
`structure:semantic`; `keywords` and `extract:facts` are unbuilt in the queued
path. **Whether `chunk` becomes its own atom is a real decision and it is
Phase 1's**, because it decides whether `STRUCTURE_CHUNK_PARAMS` belongs to a
rule or stays inside the structure handler. Listing it as an atom before that is
decided would settle the question by assumption.

### 2.1 What an atom declares

| Field | Meaning |
|---|---|
| `consumes` | `(evidence name, locus)` pairs it reads |
| `produces` | `(evidence name, locus)` pairs it writes |
| `write_mode` | `self`, `children`, `subtree` — see 9.3, which makes it per-vertex |
| `cost_class` | `cpu`, `model`, `llm` |
| `fanout` | `false`, or a bound function (2.4) |
| `child_key` | what identifies one produced child across re-runs (9.1) |

### 2.2 A locus is required, and it is what removes the recursion carve-out

An earlier draft declared `consumes` and `produces` as bare evidence names and
sorted topologically over them. **That is wrong**, and the case that breaks it is
every rollup you have: `summarize` reads `summary` from its children and writes
`summary` to itself. Same name, so the graph has a self-edge, so the acyclicity
check needs an exception — and an exception that fires for every rollup is not
an exception.

So `summarize` declares `consumes: text@children`, `produces:
effective_content@self`.

**The locus decides which mechanism enforces the dependency, and that is the
point of it:**

| Locus consumed | Enforced by |
|---|---|
| `@self` | the topological sort, within one node |
| `@children` | the settling walk — children settled before the parent is evaluated |
| `@subtree` | the settling walk, over the whole subtree |
| `@ancestor` | nothing — the evidence was there before this node existed |

`X@children` is never produced *within* the node that consumes it, so it creates
no within-node edge at all. The within-node graph over `@self` pairs is
genuinely acyclic, the sort orders real dependencies instead of name collisions,
and the acyclicity check (Part 10) needs no carve-out.

This also explains the settling walk rather than working around it. The walk is
the enforcement mechanism for the two downward loci.

**There are four loci and this section said three — corrected by Phase 1.** The
claim was that a locus is drawn from the same three values `write_mode` already
uses. Two atoms falsified it. `profile:sheet` and `extract:sheet` are scoped to a
sheet node and read the workbook bytes off the **file node above**, which no
downward locus can express. `@ancestor` is that read, and it is legal in
`consumes` only: the three write modes are the regions a task may *write*, and
reading upward is not writing upward. `jmfts_core/atoms.py` rejects an
`@ancestor` in `produces` by name.

An ancestor read needs no enforcement at all, which is why it is a fourth row in
the table rather than a fourth case in the walk. A node has an ancestor only
because that ancestor's handler created it, so the evidence predates the
consumer.

### 2.3 `after` and `after_any` are deleted — CONFIRMED, with one exception

`structure:declared` consumes `text@self`; only `extract:text` produces it; the
edge is derived. `citation`'s `after_any=(declared, inferred)` means "this
evidence has more than one producer", which is a fact about the produces map,
not a field.

**Phase 1 tested this and it holds for every row but one.**
`tests/test_atom_declarations.py` derives the ordering from the twelve
declarations and compares it against `after` and `after_any` as the pipeline runs
them today. The one divergence is `citation`, it is the one 14.1 predicted, and
it is recorded in that module's `EXPECTED_DIVERGENCE` with its reason. See 2.5.

Two things the audit corrected in the method rather than in the result:

* **The comparison is over transitive closures, not raw edges.** `citation`
  reads the file node's `content`, so `extract:text → citation` is a real derived
  edge; Part 4's table does not state it because `after_any` names the structure
  rungs, which already come after `extract:text`. Compared as raw edges the two
  orderings look different while agreeing about every pair of tasks that can run.
* **A derived edge is only meaningful over one node's batch.** Over every atom at
  once the derivation over-approximates, and the case is `embed`: it consumes
  `text@self` on a chunk, `extract:text` produces `text@self` on a file node, so
  the two share a name and a locus and are never on one node. This is Part 8's
  plan vertex seen from below — an edge belongs to a scope, not to a pair of
  atom names.

### 2.4 A fan-out atom must declare a bound function

**A floor is the wrong bound.** `⌈N / max_children⌉` containers does not tell
anybody whether to press the button. What blows a budget is the **ceiling** on
`llm`-class vertices.

Most fan-out atoms already carry a bounding parameter:

| Atom | Ceiling | From |
|---|---|---|
| `partition` (PELT) | `⌊N / min_segment⌋` | `min_segment`, already in `ROLLUP_PARAMS` |
| `chunk` | `⌊bytes / min_chunk_length⌋` | `min_chunk_length`, already in `STRUCTURE_CHUNK_PARAMS` |
| `extract:sheet` | `min(rows, max_rows)` | exact in both directions |
| `structure:sheets` | `sheet_count` | exact in both directions |

So `fanout: true` requires a function from `(evidence, params)` to an interval.
Declaring one is a bind-time check (Part 10), not a convention.

---

### 2.5 What Phase 1 found

Six results, and the two that change the design are already folded into 2.2 and
2.3 above.

| # | Finding | Where it landed |
|---|---|---|
| 1 | 2.3 holds for every row but `citation` | `EXPECTED_DIVERGENCE`, one entry |
| 2 | There is a fourth locus, `@ancestor` | 2.2, corrected |
| 3 | The derivation is per batch, not per atom | 2.3, and `derive_edges`' contract |
| 4 | The comparison is over closures | 2.3 |
| 5 | `sheet` was one evidence name doing two jobs | split, below |
| 6 | Six atoms write children, not four | 2.4's table was short by two |

**Finding 5 is the one that would have derived an ordering backwards.** Both
per-sheet tasks write into `structured_content['sheet']`, and `extract:sheet`
reads what `profile:sheet` put there. One evidence name for the whole block makes
`extract:sheet` a producer of what `profile:sheet` consumes, which derives the
dependency in the wrong direction. The block is now three names —
`sheet` (identity), `sheet.measurements` (8.3's signals), `sheet.shape` (8.4's
verdict) — and the derived edge `profile:sheet → extract:sheet` comes out
matching `EXTRACT_SHEET_SPEC.after` exactly.

The general rule it teaches: **evidence is named by what it asserts, not by where
it is stored.** A JSONB block that two atoms write is one storage location and
two facts, and 13.1's Phase 2 storage decision has to keep that distinction
whichever way it goes.

**Finding 6.** 2.4's table listed four fan-out atoms. Six declare one:
`structure:declared`, `structure:inferred`, `structure:sheets`, `profile:sheet`,
`extract:sheet`, `structure:semantic`. The two the table missed are
`structure:sheets` — whose ceiling is exact, `sheet_count` — and `profile:sheet`,
which writes exactly one child, the profile node. A constant is a legitimate
bound and declaring it is what keeps `fanout: false` meaning "writes no nodes"
rather than "writes few enough not to worry about".

### 2.6 Seeing it, rather than asserting it

`tests/test_atom_declarations.py` is the audit and it only ever says pass. The
same facts are printed by `scripts/atom_demo.py`, which is research code and
outside the gate:

```bash
python -m scripts.atom_demo atoms              # every declaration, one table
python -m scripts.atom_demo fixtures /tmp/fx   # real files to point the rest at
python -m scripts.atom_demo plan /tmp/fx/minimal.pdf   # probe -> plan -> derived order
python -m scripts.atom_demo refuses            # what the vocabulary rejects
python -m scripts.atom_demo run /tmp/fx/sheets.xlsx    # a real ingest, checked
```

`plan` needs neither a database nor a model. It probes the bytes, runs Part 4's
table over the patterns, derives the within-node order from the batch's
declarations, and prints it beside the `after` the table states by hand — so
finding 1 is a line of output on a PDF rather than a constant in a test file.
`refuses` builds the pre-split `sheet` declarations on purpose and shows finding
5 as two edges pointing at each other.

`run` uploads through `LocalJmftsClient`, drives `IngestWorker.run_once` one task
at a time against the database `JMFTS_DB_*` names, and holds each declaration up
against what the task did: the write mode against the queue row's, and the
children written against the ceiling evaluated on the evidence that was on the
node before it ran.

**Both of the script's provisional tables are gone, and Phase 2 is where they
went.** It kept two — where a fan-out bound's evidence lives, and which usetype
its ceiling counts — with a comment on each saying they were the half of a
declaration that did not exist. The first is `jmfts_core/evidence.py`, so a
bound now names `extraction.characters` and the path, the type and the producer
are all lookups. The second is `Fanout.counts`, declared by the six atoms that
write children. `refuses` gained a section for what the registry rejects and the
atom vocabulary does not: a null where a null is not a result, and a flag where a
count is declared.

## Part 3 — Evidence

A named fact stored on a node. This is the existing `patterns` dict plus what
tasks currently write into `structured_content`, treated as one namespace with
one registry.

### 3.1 Evidence is typed

The registry maps a name to a type: `bool`, `int`, `float`, `str`, `list`,
`sketch`. Guards need this (Part 4.4), and so does the validation that
`ingest_options._positive_int` currently provides for two groups only.

**Phase 2 built it and it is `jmfts_core/evidence.py`.** It held thirty-six
entries then and holds **fifty-one** now, a type and a storage location on
each, with `evidence.check` as the one place that answers whether a value is
what its name says. `producers()` is derived from `ATOMS` rather than declared,
so who-writes-what has one answer and not two.

**Thirty-six of those entries are `document_evidence` rows, and the other
fifteen are leaves inside them or values stored elsewhere.** Phase 2b moved the
rows out of `structured_content`; 13.1 measured the store and 13.3 decided the
contract. The four counts this section quotes — 51 entries, 36 rows, 21 of the
rows `dict`, 15 bare scalars — are pinned by
`tests/test_evidence_registry.py::TestShape::test_the_counts_the_documents_quote_are_still_true`,
which fails when the registry moves so that this prose is told rather than left
wrong.
<!-- Updated 2026-09-15 from 44/30/15/15 to 51/36/21/15. The test's own docstring is the
     record of all six moves, and it is the mechanism working: it tells whoever changes the
     registry that this sentence has moved and where. These documents were outside the
     repository between 0.5.1 and 0.6.0, so nobody who saw the failure could act on it. -->


Four things the build corrected, and 3.1 as written above is wrong about two:

1. **The type list is short by two.** `blob` is bytes, which none of the six
   describes, and fourteen of the twenty-three blocks are `dict`. Both are added.
   `sketch` is kept with no member — `datasketch` reaches evidence only through
   `sheet.measurements`' payload — and a test fails on the day one appears.
2. **A registry over blocks alone cannot be typed at all, so it has two
   levels.** `atoms.py` names blocks, because a block is what an atom writes;
   guards and fan-out bounds read leaves inside them. A leaf names its block
   through `within`, and inherits that block's producer — which is what makes
   "who writes `extraction.characters`" answerable when nothing declares it.
3. **A leaf is registered only when something schedules on it.**
   `sheet.measurements` holds twenty-eight keys and twenty-four decide nothing;
   registering them would be twenty-four names to keep in step with
   `sheet_profile.py` in exchange for nothing.
4. **`matched.patterns` stays an open namespace.**
   `tests/corpus/vocabulary.py` recovers probe's pattern names by *running*
   probe, and its docstring says why: two lists diverge quietly, in the
   direction that makes coverage look better than it is. So the registry states
   a rule — everything probe emits is a flag unless `PATTERN_TYPES` names it —
   and `tests/test_evidence_registry.py::TestPatterns` holds the rule against
   the installed probe in both directions.

**The audit found a live data-loss defect and closing it is Phase 2's only
user-visible change.** `DocumentRepository.INGEST_OWNED_KEYS` was seven names
written by hand; the pipeline writes twenty-three blocks. Every `PATCH
/documents/{id}` that added a caller field to a node deleted the fifteen it did
not name — the `sheet` block off a sheet node, `profile` off a summary node,
`source_span`, `anchor`, `anchor_unresolved` and the chunker's five keys off a
chunk, `record`, `row_index` and `sheet_name` off a record node, `cells` off a
cell node, and `effective_content` off a rolled-up one. That is the same unrecoverable
edit `_merge_structured_content` was written to stop, arriving through the
blocks its list did not know about. The constant is now
`evidence.ingest_owned_keys()`.

Two atoms gained a `produces` entry as a consequence, because the audit asks
who writes each block and three of them had no answer: `extract:sheet` now
produces `record@children`, and both chunking rungs produce `structure@subtree`
— each chunk's own record of how the rung placed it. Neither adds a derived
edge, because nothing consumes either name.

**One defect was recorded here and Phase 2b closed it.** All twenty-three
blocks were then top-level keys in the same dict a caller writes its own
metadata into, and nine of them were bare scalars rather than namespaces:
`rung`, `section_title`, `section_level`, `chunk_index`, `source_line`,
`source_span`, `sheet_name`, `row_index` and `attempts`. Owning them is correct,
and it reserved twenty-three ordinary English words from every caller. A caller
who writes `{"structure": "loose"}` on a chunk is not making a mistake and could
not be told so before the fact. Moving them under one block is a data
migration.

**13.3 closed it, and not by adding the namespace.** Evidence leaves
`structured_content` for its own rows and nothing merges it back, so the
twenty-three words stop being reserved because they stop being in the caller's
dict. There is no rename and no second pass over the chunk nodes.

**Phase 2b did it, and found six more words on the way.** Migration 015 moved
twenty-nine keys, not twenty-three: `structure:conversation` writes `speaker`,
`turn_index`, `timestamp`, `conversation_id`, `over_token_window` and
`part_index` on every turn node, and S7 registered none of them. Phase 2's audit
could not have caught it — its fixture ingests documents, not transcripts — and
`tests/test_evidence_registry.py` recorded two of the six as "CALLER metadata,
correctly not ingest-owned", which they never were. The audit that found them is
the one 13.3 made possible: not "is every key one the gate knows", but "did the
pipeline write anything into the caller's column at all", which needs no list to
ask.

### 3.2 Three states, not two

An earlier draft said "never-computed is the initial case of stale". **That is
wrong**, and it produces a walk that never terminates. An atom legitimately
produces nothing in ordinary cases: `extract:text` on a scanned page,
`keywords` on a table of numbers. The evidence is absent, absence reads as
stale, and every walk re-enqueues the task forever.

The fix has three parts:

1. **An atom must write every name in its `produces` on success, null
   included.** Producing nothing is a result, not a gap.
2. **The fingerprint attaches to the write, not to the value.** A null carrying
   a current fingerprint is fresh.
3. **Failure is a distinct third state** carrying its own retry policy, which
   the current design has nowhere to put.

| State | Meaning | Walk behaviour |
|---|---|---|
| absent | never attempted | enqueue |
| written, fingerprint current | fresh, value may be null | skip |
| written, fingerprint stale | inputs moved | re-enqueue |
| failed | carries reason and attempt count | retry policy decides |

This is the same distinction `citation_tasks.py` already makes for anchors: two
keys, never one with a null, because "could not be placed, because X" and "is at
page 3" are different facts.

### 3.3 The input fingerprint

Recorded on each write. Covers the child node ids read, the source evidence
values read, and the parameters used.

This widens the existing `param_fingerprint` on queue rows from parameters alone
to parameters plus inputs. `Document.content_hash` already exists and is the
right input for content-derived evidence.

---

## Part 4 — Rules

A rule is one row of a rule set. Data, not code.

| Field | Meaning |
|---|---|
| `atom` | which verb |
| `scope` | which nodes it applies to (4.1) |
| `guard` | which evidence must hold (4.4) |
| `params` | the atom's settings |
| `multiplicity` | which evidence gives the child count, for costing only |

This replaces `TaskRow`. It adds `scope` and `multiplicity`; it drops `after`,
`after_any` and `write_mode` (which moves to the atom, where it is a property of
the verb rather than of the rule).

### 4.1 Scope has exactly two forms

* **An explicit node id.** Permitted only at the root of a binding. This is what
  "apply this rule set to that subtree" means.
* **The children produced by another named rule.** Everywhere else.

No queries in scopes. The parent rule's fan-out *is* the child rule's
multiplicity, so cardinality composes without anything having to read the tree.

#### Both halves of the second form are wider than this, and Phase 3 is what found out

The constraint that survives is the one that matters — a scope is resolved from
columns on the node in front of the planner and never from a search over the
tree. What did not survive is "another named rule", singular, and the absence of
any statement about which KIND of child.

**A rule may name several producers.** `embed` applies to the leaves of five
different rules. Written this section's way that is five rows all called `embed`,
differing in one field — which is precisely the copy-drift `DECLARED_STRUCTURE`
already refuses for thirteen formats, in those words: "thirteen near-identical
rows would let the copies drift apart". It is also the disjunction `after_any`
exists to express, one level up.

**A rule must name the kind of child.** This is the half that would have shipped
a defect. `produced_by` alone cannot separate the two kinds of node one rung
writes: `structure:declared` writes `section` containers AND `chunk` leaves, and
only the chunks carry text. A rule scoped to "the children of
`structure:declared`" would have put an `embed` on every section container, and
`run_embed` refuses empty content — so every titled document would have failed a
task per section. `usetype` is the second half, and 4.2's own sentence is what
licenses it: `usetype` describes what a node *is*, not what made it. A scope that
needs both is naming two facts about one node, not running a query about it.

So the shipped form is `(produced_by ∈ R, usetype ∈ U)`, both non-empty, both
read off the child with no I/O. `NULL` in either column matches nothing, which is
what keeps an asserted node (4.2, 9.4) out of every rule's scope.

### 4.2 Scope form 2 resolves by a stamp, not by a count

**A multiplicity gives a number; a scope needs an identity.** When a node has
children from `partition` and children from `chunk`, "the children produced by
rule R" is only answerable if each child records which rule instance created it.
Without that stamp, the manually rearranged tree — the case Part 9 exists for —
is exactly the one that misattributes.

`Document` gains a `produced_by` column, stamped at creation. It is a new column;
there is nothing on the model today that carries this (`usetype` describes what a
node *is*, not what made it).

The symmetry with `Triple.derived_by` is deliberate: a derived thing names the
rule that produced it, and `NULL` means asserted — which for a document node
means a person or an importer created it, not a rule.

### 4.3 The stamp is also what keeps the walk linear

Frontier scopes resolve by `produced_by`. So the walk at a node recomputes only
the subplan rooted at the vertex whose scope resolves to that node, rather than
the whole plan. Without this the walk is `O(n²)` in tree size.

### 4.4 Guards take operators, not presence alone

**Presence-only guards will not survive contact with routing.** The conditions
that are actually wanted are `sheet_count > 1`, `rows > 10000`,
`has_text_layer = false`. Forcing each into a boolean evidence name that probe
has to write — `is_multi_sheet`, `is_large` — puts the threshold back inside the
atom, which is the thing a rule set exists to lift out.

A guard term is `name op literal`, with `op` from a fixed set:
`=`, `!=`, `<`, `<=`, `>`, `>=`, `in`. Terms combine with `and` / `or` / `not`.

Still no function calls. Still statically checkable against the evidence
registry's types. The payoff is that a binding can move a threshold without a
code change, which is most of the reason to have rule sets.

`forbids` becomes `not (name = true)` and stops being its own field.

#### Three of those four sentences survive, and the fourth would empty every `.docx`

**What "ingest" means is what decides what a guard is, so it is written down
first.** Ingest is not "perform every step". It is *autonomously, as configured,
extract structured information*. Producing nothing is a valid outcome, and it is
valid terminally: when a step cannot run, the steps that consume it cannot
either, and a cascade of no-ops to the end of the tree is the correct answer
rather than a failure to report.

So a guard is not a precondition on a task's correctness. **A guard is where a
short-circuit is configured** — the statement that this work is not worth doing
on this document, either because it would fail or because the caller does not
want it spent.

**The cascade is already built, and a guard adds an entry point to it rather
than adding the cascade.** A `.docx` whose pattern set came back empty plans to
eleven not-applicable rows today, each with its own reason and none of them a
failure: `extract:text` because `has_text_layer` was not measured, the three
structure rungs because `extract:text` is not eligible, `citation` and
`index:bm25` because none of the rungs is. Nothing about Phase 4 has to build
that. `_blocking_reason`, `_dependency_reason`, `_alternative_dependency_reason`
and `_no_scope_reason` are it.

**There is therefore no refusing guard, and the one refusal that exists is
already at the right layer.** `probe` fails with a named reason on an encrypted
OOXML package (`probe.py:667-676`) because it cannot measure anything: there is
no pattern set to plan from. Probe refuses when it has no input; the table never
refuses, because nothing is a valid output. `is_encrypted`, `is_damaged` and
`has_macros` are ordinary guard names with no special outcome.

Against that, the four sentences above:

1. **Operators: kept.** The demand is real and larger than this section's
   examples. Probe measures 31 patterns; `TASK_ROWS` reads 8 of them. All ten
   integer measurements are unread, `char_count`, `page_count`, `sheet_count`
   and `slide_count` among them.
2. **`name op literal`: widened.** The right side may also name an option.
   Without that, 14.2's promise is false — a literal `5000` inside `TASK_ROWS`
   moves only by a code change. With it, `char_count > options.facts.min_characters`
   is settable by a caller through the stack `resolve_options` already has, and
   `enabled_by` folds in as the degenerate term `options.facts.enabled = true`.
3. **`and` / `or` / `not`: dropped.** Not deferred — they cost nothing to leave
   out. `requires` is already a conjunction and `forbids` already a disjunction
   of exclusions, and every guard worth writing fits one or the other. Their
   absence also keeps the not-applicable sentence per-term, which is the shape
   `_blocking_reason` writes today.
4. **"`forbids` stops being its own field": REVERSED.** It is load-bearing, and
   this is the sentence that would have shipped an empty `.docx`.

**`requires` and `forbids` are not a weak boolean algebra. They are two unknown
policies, and the field name is what carries the polarity.**

| Field | An unmeasured name means |
|---|---|
| `requires` | the precondition cannot be confirmed — do not run |
| `forbids` | there is no evidence of a blocker — do not block |

Both are "nothing rather than bad output", pointed in opposite directions. The
first protects against running on absent input. The second protects against a
rung standing down for a reason nobody measured.

A `.docx` with a text layer, planned in the tree at `1e8ae5b`:

```
ELIGIBLE: ['extract:text', 'structure:inferred', 'index:bm25']
 NA structure:declared :: patterns.has_heading_styles was not measured
```

`has_heading_styles` is `@declared_structure` for `docx` and probe does not emit
it — it reads `planned` in `tests/corpus/vocabulary.py` and `OFFICE_SPEC.md`
Part 2 is why.
<!-- Corrected 2026-09-15: probe HAS emitted it since the docx prober landed
     (`probe._probe_docx`, the intersection of `word/styles.xml` and `word/document.xml`).
     The plan output above is dated `1e8ae5b` and was true then. The ARGUMENT is unchanged,
     because the prober OMITS the pattern rather than defaulting it when the styles part is
     missing — so an unmeasured name is still what `requires` and `forbids` disagree about,
     and flattening them would still empty every `.docx` whose package has no styles part.
     `INGEST_SPEC.md` 4.2 carries the same correction. -->
One rung reads that unmeasured name as a precondition and stands
down. The other reads the same name as an exclusion and runs. Flatten the two
into one expression and `structure:inferred`'s guard becomes
`not (has_heading_styles = true)`, which under any consistent propagation of
unknown through `not` does not hold — and **every `.docx` ingests to nothing**.

So the shipped form keeps both fields and adds operators to their terms:

```python
TaskRow(
    TASK_EXTRACT_FACTS,
    requires=(term(option("facts", "enabled")), HAS_TEXT_LAYER),
    forbids=(term(CHAR_COUNT, OP_LT, option("facts", "min_characters")),),
)
```

#### The threshold is an exclusion, and the measurement is why

**This paragraph corrects the draft above.** It wrote the size floor as
`requires=(term("char_count", ">", option(...)),)`, and building it found that
that is the `.docx` regression in a second costume.

Probe's patterns are per-format, and the counts are the narrowest of all.
Measured across the corpus specimens:

```
char_count   emitted for: ['text']
is_damaged   emitted for: ['pdf']
page_count   emitted for: ['pdf']
sheet_count  emitted for: ['xlsx']
```

`char_count` required would therefore stop fact extraction on every PDF, `.docx`
and `.pptx` — not because those documents are short, but because nothing measures
their length. Stated as an exclusion the same number reads as what a caller means
by a floor: do not spend the call on a document measured as too small, and spend
it where nobody measured a size.

**So a caller's threshold belongs in `forbids`, and this is a rule rather than a
preference about this one row.** It is the same asymmetry the `.docx` case turns
on, arriving from the other direction: `requires` is for a precondition that must
be confirmed, and a caller's budget is not one.

`is_damaged` keeps the polarity the draft gave it and gets the same benefit for
free — it is PDF-only, so on every other format the name is absent and the
exclusion leaves the row alone.

#### Two kinds of guard, and only one of them is configuration

The short-circuit framing sorts the unread patterns into two piles that want
different treatment, and conflating them would put a correctness check behind a
caller's switch.

* **The appliance's own.** A task that would fail, or produce nothing usable, on
  a condition probe already measured — `is_damaged` is the clear one. These take
  literals. A caller may not turn them off.
* **The caller's.** Work a caller does not want spent: `char_count`,
  `page_count`, `sheet_count`, `has_macros`. These take an option reference, and
  this pile is what point 2 above exists for.

#### The reason is the product, and it is currently inside an attempt

If a full cascade of no-ops is a *correct* outcome, then the reason chain is the
only thing separating a correct empty document from a broken one. That is a
promotion: diagnostics become the deliverable.

`run_probe` puts `plan.not_applicable` into the probe task's `detail`, which
reaches the node's `attempts` evidence. It persists and it is readable, but it
is a blob inside one attempt record rather than a statement about the document.
**Open, and it belongs to this phase:** should "why this document has no
children" be its own registered evidence name?

### 4.5 Order: topological, with declared alternation groups

Two mechanisms, and confusing them would give the language surprising semantics.

* **Across rules generally:** order is the topological sort over `@self` loci.
  A rule set's list order is a tiebreak *within* one topological layer and
  nothing more.
* **Within an alternation group:** first match wins.

An alternation group is a set of rules producing the same `(name, locus)` at the
same scope, of which exactly one may fire. `INGEST_SPEC.md` 8.4's four shapes are
one: `small_table`, `matrix`, `records`, `unstructured`.

**A group must be declared, not inferred.** Detecting it from the produces map
would make an accidental collision silently drop a rule. Two rules producing the
same pair at the same scope without a declared group is a bind-time error.

---

## Part 5 — Rule sets and bindings

**A rule set** is a named, ordered list of rules. This is the thing that is
authored and selected. One uses PELT, another uses RAPTOR, a third adds keyword
extraction and no summarization.

**A binding** associates a rule set with a scope, and carries parameter
overrides and the request (Part 8). Same table shape as `SPRINT_0_3_0.md` 4.5's
`shape_bindings`: keyed on a scope that is a usetype pattern, a subtree
`parent_id`, or an explicit document set.

The two subsystems arriving at the same scoping mechanism from opposite
directions is the reason to use one table shape for both.

### 5.1 There is a third subsystem, it got there first, and it is a tree

**A collection is not a scope a table has to express. It is a subtree, and a
setting on a node reaches the documents beneath it.** That removes the second
scoping mechanism rather than choosing its shape.

"Corpus" is the word this section needed and could not define. It means either
everything in one appliance — under disjoint RBAC views — or one set of related
documents, and those are different things. A `parent_id` on the upload names the
second one exactly, by putting a node *above* the ingested document that the
configuration can hang on.

**The walk already runs, one level down.** `rollup_tasks.py:229-256`:

```python
def _options_owner(session, node):
    """``node`` itself if it records options, else the nearest ancestor that does."""
```

> **Corrected 2026-10-12: `_options_owner` no longer exists.** `580c1df` (2026-09-17)
> removed it: a partial declaration on a collection, which `0aa14ec` made writable, was
> masked by a nearer one, exactly as point 1 below predicts of nearest-wins.
> `rollup_options` now calls `collection_option_layers` — this section's walk, built — and
> resolves with `inherited=`. The quote is left as what the design was read against.

A segment created under a file node inherits what the upload asked for by
walking `node.path` upward. It never passes above the file node for one reason:
the upload always writes a complete `options` row there
(`services/ingest_service.py:615`, `:784`), so the walk stops on its first read.
Making a collection node's options reach the documents beneath it is the same
walk, started one level higher. `upload_file` already takes `parent_id` and its
docstring already separates "here are some bytes, analyse them" from "put this
file in that folder".

Three things generalising it decides:

1. **Layered merge, not nearest-wins.** `_options_owner` returns ONE owner and
   uses its set wholesale, which is correct while there is only ever one. Under
   a tree a caller will put `structure.max_tokens` on `/papers` and
   `facts.enabled` on `/papers/2026`, and nearest-wins gives them only the
   second. `resolve_options` already merges; this makes the layer count N:

   ```
   TASK_PARAM_DEFAULTS  <-  INGEST_PROFILES[fmt]  <-  each ancestor, root-first  <-  the request
   ```

   Nearest-wins would mean a collection cannot refine its parent without
   restating it, and a restatement is the copy-drift `DECLARED_STRUCTURE` and
   `INGEST_OWNED_KEYS` are both on the record for.

2. **The walk runs at upload, not at plan time.** The same place
   `resolve_options` is already called, with the complete resolved set landing
   in the `options` evidence row exactly as now. **This is what keeps 11.2's
   purity rule intact**: `plan_after_probe(fmt, patterns, options)` never learns
   that ancestors exist, so a guard still reads only patterns and options, and
   `EXPLAIN` and the run stay provably one function. Resolving at plan time
   would put a tree read inside the planner. `resolve_options`' own docstring
   carries the property this depends on — resolving an already-resolved set is
   the identity.

3. **A collection is a default for new uploads, not a live policy.** A document
   ingested before the setting changed keeps what it was ingested with. That is
   the honest consequence of 2, and it is preferable to existing trees silently
   disagreeing with their own recorded `options`. Re-applying is Part 9's
   re-derivation, and the recorded row is what makes the diff computable.

**What this buys that a bindings table would not: authorization, free.** Access
is already subtree-scoped — `AccessGrant`, an access-control root, and the
principal contextvar the repositories read. "Who may configure this collection"
becomes "who may write to this node", with no second answer to a question RBAC
has already answered. The interaction is on the record already: `upload_file`
refuses `private=True` together with `parent_id`, because grants are additive
(`services/ingest_service.py:465`).

**So the paragraph above this subsection is amended.** Two subsystems arriving
at one scoping mechanism was the argument for one table shape. There are three,
and the third is a tree.

What is actually new is small: a route that writes an `options` row to a node
that has none — a collection node is an asserted document (`produced_by` NULL)
and nothing writes options to one today — plus the ancestor walk at upload.
Validation is already there; `resolve_options` raises `ValueError` for an
unknown group, an unknown key or a wrong type, and the service maps it to 400.

---

## Part 6 — The plan

Evaluating a rule set against a node and its current evidence yields a plan: a
tree of vertices, each holding `(atom, scope, params, multiplicity)`.

### 6.1 The plan is computed and never stored

Planning is a pure function of `(rule set, node, evidence)`. A plan is therefore
cheap, always consistent with the tree, and has nothing to invalidate.

Storing one recreates exactly the failure `settling.py` opens by describing.
Every level becomes late-bound, not just the rollup, and the rollup's special
case disappears into the rule.

### 6.2 The frontier

Plan vertices whose scope nodes exist now are **instantiable** — they can be
enumerated, displayed, and selected individually.

Vertices below an unresolved multiplicity are **latent**. There is no sheet node
yet, so there is no `profile:sheet` instance to select. This is not a new
limitation; it is why `plan_after_probe` cannot enqueue the sheet tier today.

### 6.3 Recursion is a marked edge

A recursive production — an oversized PELT segment that becomes a container and
is segmented again — is stored as an edge carrying a branching factor and a
bound, never as an expansion. The plan stays finite and the file is read once.

A recursive atom must declare a descent measure: each recursive application
strictly reduces the quantity that triggered it. This makes termination a
property of the atom, checkable at registration, rather than a property of the
rule set.

### 6.4 The walk travels upward, so a fan-out atom enqueues its children's frontier

**This corrects a claim an earlier draft made twice** — that the planner replaces
the handler-side enqueue entirely (Part 0), and that there is "no second entry
point and no separate launcher" (Part 9). Neither is achievable, and building
toward them would waste a phase.

`settle_walk` (`settling.py:316-360`) advances to `step.parent_id` and never
descends. `run_structure_semantic` records the consequence at
`rollup_tasks.py:266-268`: a node created settled and given no work is never
visited, never gets an embedding, and nothing ever notices.

So a rule that creates a child must enqueue that child's own work at the moment
it creates it. **The rule is: a fan-out atom, having created a child, asks the
planner for that child's frontier and enqueues what comes back.** One shared
function, called from six sites (Part 0). `rollup_tasks.py:432` is already
written this way and is the model for the other five.

What changes is where the DECLARATION lives, which is the whole point: the sheet
tier stops being a literal `TaskSpec` tuple inside `sheet_tasks` and becomes two
rules scoped to `structure:sheets`'s children (4.1 form 2). The enqueue call
survives; the hand-written plan does not.

---

## Part 7 — Cost

One traversal of the plan tree with a choice of accumulator. Counting tasks,
child nodes, embeddings and LLM calls are the same fold.

### 7.1 Every accumulator returns an interval

Because fan-out atoms declare bound functions (2.4), the fold returns
`[low, high]` rather than a floor. A multiplicity whose evidence has not been
measured stays symbolic and names the atom that would resolve it.

A workbook with three sheets, before any sheet is profiled:

```
tasks:      1 + 3 × (1 + [2, 34])
llm calls:  0
?           rows — produced by profile:sheet
```

The interval comes from `extract:sheet`'s ceiling under the bound `max_rows`
together with the floor of one record; the `?` names what would collapse it.

**A recursive atom's ceiling is loose, and the budget is what actually binds.**
`partition`'s bound is `⌊N / min_segment⌋` per level, and `min_segment` defaults
to 3 (`ingest_options.ROLLUP_PARAMS`). Over a ten-thousand-row sheet the summed
ceiling on `summarize` calls is in the thousands while the real number is likely
tens. Reporting `[1, 5000]` is honest and it is not actionable on its own. So
8.1's budget is the control and the interval is context for it; the `?` line
naming what would collapse the interval is the part that carries the value.
This is a property of recursion, not a defect in the fold — a bound that has to
hold for every input cannot be tight for the common one.

### 7.2 An unmeasurable count is reported, never guessed

`measure_sheet` already distinguishes `interior_cardinality_exact` from
`interior_cardinality_at_least`, and carries `rendered_unbounded_reason` beside
`rendered_tokens`. The cost fold returns the same kind of object for the same
reason.

---

## Part 8 — Requests

What is enqueued after looking at a plan. Two parts, because the plan has two
regions.

* **An exact list over the frontier.** Those vertices exist and can be named.
* **A predicate over latent vertices**, matched on atom, depth, `cost_class`, or
  produced evidence name.

Both are stored on the binding. The predicate is re-evaluated each time new
nodes appear. A stored list of latent vertices would be the stale task list of
Part 0 again.

**A stored list needs vertex identity, and 6.1 says plans are never stored — so
say what a vertex is named by.** It is `(rule name, scope node id)`. Both halves
are durable: a rule name is authored, and a node id survives a re-run by 9.1.
Nothing about a plan has to persist for a stored frontier list to still mean
what it meant when it was written, which is what keeps 6.1 and this part from
contradicting each other. It is also the same pair 4.2's stamp records, so a
request, a stamp and a plan vertex all name a thing the same way.

Both cases fall out of one object. Ingesting a file: the frontier is one node,
so the request is almost all predicate. Casting keyword extraction at a built
subtree: the frontier is the whole subtree, so it is almost all list.

### 8.1 A request carries a budget, and it is a first-class term

A predicate stored on a binding, re-evaluated whenever new nodes appear, over a
region whose size is admittedly unbounded, is **the one part of this design that
can run away.**

`depth` is a weak axis for bounding it. The one that binds is `cost_class`
together with cumulative spend.

So a request carries a budget: a count of admitted `llm`-class (and optionally
`model`-class) vertices, decremented at enqueue.

### 8.2 Exhaustion parks the binding

On exhaustion the binding is **parked** — a defined state carrying what remains,
what it would cost, and what exhausted it — and it waits for review. It does not
error, and it does not silently stop.

This is inside the project's Fail Early rule rather than an exception to it. That
rule states that user ambivalence is not a failure and that reporting a
condition with a corrected value is what enables success. A parked binding
reports its condition and names its own resolution. Erroring would discard the
plan; continuing silently would be the swallowed failure.

### 8.3 One named request mode is worth shipping

A request admitting `cpu` and `model` but not `llm` runs a complete structural
pass — probe, extract, partition, chunk, profile, keyword sketch, embed — and
leaves every `llm` vertex latent and countable.

The fold over that parked set is then a concrete number that can be shown to
somebody before it is spent. This should be a named mode rather than a predicate
each caller writes by hand.

It also matches what the appliance already supports: a blank `JMFTS_LLM_*` means
everything except summarization, RAPTOR, fact extraction and synthesis works.

---

## Part 9 — Staleness, the walk, and idempotent re-derivation

Evidence is stale when its fingerprint disagrees with its current inputs.

**This is what makes rearrangement safe.** PELT and RAPTOR are one atom —
partition N children, insert containers, reparent — differing only in whether
the partition must respect sibling order. Inserting a level changes a node's
child set, so its `summary@self` and its rolled-up sketch are stale by
fingerprint, and the walk recomputes them. Nothing needs a special case for
rearrangement.

**And this is what "apply a rule set to an existing subtree" is.** Binding a
rule set at a subtree marks the evidence its rules produce as stale; the walk
re-derives.

An earlier draft added "there is no second entry point and no separate
launcher", which is wrong for the reason 6.4 gives: the walk goes up, so a rule
that creates a child still enqueues that child's frontier where it creates it.
What is true is the useful half — binding a rule set is not a launcher, and
there is no second planner.

The walk itself is the existing settling walk with its scope widened from
rollups to every rule: at each node it touches, recompute the subplan rooted
there (4.3), enqueue the frontier the request admits, advance.

### 9.1 A re-run matches, keeps and rewrites — DECIDED

The question an earlier draft did not ask: re-deriving `summary@self` overwrites
one value, but re-deriving `chunk` creates children, and the design said nothing
about the children the previous run created.

It cannot arise today. The attempt diff (`settling.py:134-163`,
`rollup_tasks.py:148-157`) never offers the same `(task, param_fingerprint)`
pair twice, and only a new upload moves a fingerprint. Part 9 removes that
protection on purpose, because re-deriving on demand is the point.

**The rule is idempotence, not replacement.** A re-run of rule R at scope S
produces a set of prospective children and matches them against the nodes
stamped `(R, S)`:

| Case | Action |
|---|---|
| prospective child matches an existing stamped node | **keep the node and its id** |
| matches, but the content moved | rewrite that node in place, keep the id |
| existing stamped node has no prospective match | delete it and its subtree |
| prospective child matches nothing | create it |

**Keeping the id is what makes a re-run affordable, and the saving is the
re-embed.** An unchanged child's own evidence fingerprints are unchanged, so
nothing below it is stale, so the walk does not descend and no descendant
re-embeds. Replacing every child would re-embed a whole subtree to change a
threshold — which is exactly the pass `SPRINT_0_3_0.md` step 7 needs to run
hundreds of times.

### 9.2 A child key is what "matches" means

Deciding a match needs an identity, and `child_key` (2.1) is where the atom
declares it. Three forms, and every fan-out atom that ships today is served by
the first:

| Form | Atoms |
|---|---|
| position within the rule's output | `chunk`, `partition`, `extract:sheet`, `structure:sheets` |
| a natural key the atom names | a sheet's name; a row's `is_unique` column (`SPRINT_0_3_0.md` 7.2) |
| `content_hash` | reserved; `Document.content_hash` already exists |

Position works for all four because all four are order-deterministic: the
chunker walks text forward, PELT preserves sibling order, `extract:sheet` walks
rows, `structure:sheets` walks `workbook.xml`. Position is therefore the default
and a natural key is an atom's declaration that it has something better.

**Two constraints on an atom fall out of this.** Its output must not depend on
the node ids it is about to be assigned, or an unchanged input would produce
changed bytes. And deciding a match must be cheaper than the work it avoids —
which holds in every shipped case, because the decision costs a blob read and a
streaming pass with no model, and what it avoids is the embedding.

### 9.3 A re-run that deletes a child is a `subtree` write

Deleting a node rewrites its descendants' `path`, which `INGEST_SPEC.md` 5.3
reserves for `subtree` and the claim query enforces at
`repositories/task_queue.py:298-315`. `chunk` declares `children`.

So a fan-out atom's write mode is not the same on a first run and on a re-run,
and **the write mode moves from the atom onto the plan vertex.** The planner
knows which case it is: it counts the nodes stamped `(R, S)` before it emits the
vertex. A first run declares what the atom declares; a re-run at a scope that
already holds stamped children declares `subtree`.

Declaring `subtree` always would be the alternative and it is worse: it would
serialise the whole file against every first ingest, which is the common case,
to hold a reservation the common case never needs.

### 9.4 A person editing a produced node — RECOMMENDED, not decided

Idempotence cannot distinguish "my output changed" from "somebody edited my
output". Both read as a mismatch at 9.1's second row, so a re-run would silently
overwrite the edit.

**Part 4.2's own symmetry answers it: clear `produced_by` when a person writes
to the node.** `NULL` already means asserted, exactly as it does on
`Triple.derived_by`. An edited node is then no longer stamped `(R, S)`, so the
re-run neither keeps it nor deletes it — it falls out of the match entirely and
the rule creates a sibling. The person's edit survives, the machine's output is
regenerated, and both are visible.

What this leaves open is the divergence: the tree now holds an asserted node and
a derived node covering the same span, and nothing reconciles them. That is a
review task in `INGEST_SPEC.md` Part 7's sense rather than something the walk
should decide. Recorded, not built.

---

## Part 10 — Bind-time checks

A rule set that fails any of these is rejected when it is bound, not partway
through a run.

1. Every `(name, locus)` a rule consumes is produced by some rule in the set, or
   by `probe`.
2. The within-node graph over `@self` pairs is acyclic. No carve-out (2.2).
3. Every recursive atom declares a descent measure (6.3).
4. Every `fanout: true` atom declares a bound function (2.4).
5. Every multiplicity names evidence that some rule produces.
6. Every guard names only registered evidence and literals, and every comparison
   type-checks against the registry (3.1, 4.4).
7. Two rules producing the same `(name, locus)` at the same scope are declared as
   an alternation group (4.5).
8. No two plan vertices hold reservations that make each other unclaimable —
   a CYCLE, not an overlap (10.1).

### 10.1 A reservation that serialises is the queue working

**An earlier draft made check 8 "no two plan vertices hold conflicting
reservations", and that rejects the pipeline running on `master` today.**
`citation` declares `WRITE_SUBTREE` scoped to the file node
(`ingest_tasks.py:544-549`). `summarize` declares `WRITE_SELF` on the same file
node (`rollup_tasks.py:180-186`). Under the old wording the default rule set
fails its own bind-time check.

The queue already handles that pair correctly. `claim_next`
(`repositories/task_queue.py:298-315`) does not refuse either task; it takes one
and makes the other wait. Serialisation is the mechanism working, and a
bind-time check has no business refusing it.

**What is worth rejecting at bind time is a cycle**: two vertices each ordered
after the other, one by a topological edge and one by a reservation, so neither
can ever be claimed. That is a stall no runtime check reports, because a stalled
task looks exactly like a waiting one.

Reservations are still `(write_mode, scope, produces)`, and the finer overlap is
still worth having — it is what would let `summarize` and `keywords` run
together on one node instead of queueing. **But that relaxation belongs to the
claim query, and it forces Part 13's first open question rather than being
independent of it.** Every evidence write today is a read-modify-write on one
JSONB column (`settling.py:269-275`, `sheet_tasks.py:243-250`), so two
concurrent `self` writers on one node would lose one of the two writes with
nothing raised. See Part 13.1: relaxing the claim query and keeping evidence in
one JSONB column cannot both be done.

`subtree` still blocks anything below it, which is what `SPRINT_0_3_0.md` and
`citation`'s `WRITE_SUBTREE` already reason about, and what 9.3 now also relies
on.

---

## Part 11 — What maps to what

| Today | Becomes | Phase |
|---|---|---|
| `TaskRow` | a rule with `scope = the binding root` | 3 — DONE, as `TaskRow.scope` defaulting to `ROOT_SCOPE` |
| `TaskSpec` literal `params` | rule `params`, resolved through the binding | 3 — DONE for the three that existed; the binding is 6 |
| `SHEET_TASK_SPECS` in `run_structure_sheets` | two rules scoped to `structure:sheets`'s children | 3 — DONE |
| the six literal `enqueue_batch` sites (Part 0) | six calls to one planner (6.4) | 3 — DONE, and there were eight (14.3) |
| `IngestRollupPlanner` | rules consuming `@children` | 7 |
| `after` / `after_any` | derived from `produces` / `consumes` | not yet; 2.3 is proved, nothing acts on it |
| `forbids` | itself, with operators on its terms — 4.4 reverses this row | 4 |
| `enabled_by` | a `requires` term naming an option | 4 |
| `params_key` + `ingest_options` groups | rule `params` + ancestor overrides (5.1) | 4 for the option reference, 6 for the route |
| `plan_after_probe` | the planner, called at every node rather than once | 3 — DONE for the scopes the table declares; the walk is 7 |
| `explain_plan` | the cost fold over a plan, with intervals | 5 |
| `DEFERRED_REASON` | an atom with no registration is a plan vertex marked deferred | 3 — DONE; `Frontier.deferred` carries it per scope |

`INGEST_SPEC.md` Part 4's table survives as a rule set. It stops being the only
one.

### 11.1 The `summarize:llm` handoff maps cleanly, and that is worth recording

`rollup_tasks.py:535-556` is the case most likely to have broken this model, so
it is the one to write down. `summarize` concatenates its children, tokenises,
and only then learns whether the result fits — a fact no enqueue-time decision
could have had. It enqueues `summarize:llm` from inside the handler, carrying
its own priority forward.

Under atoms it is `summarize produces fit@self`, plus a rule for `summarize:llm`
guarded on `fit_truncated = true` (4.4). Nothing about it needs a carve-out.

Two consequences, one of each sign. It costs one extra walk round-trip per node,
because the guard is evaluated on the walk after `summarize` completes rather
than inside it. And it puts the handoff in `EXPLAIN`, where today it is
invisible until it happens — a `conditional` row naming the evidence that
decides it, which is exactly what 11.2's `EXPLAIN` was built to report.

---

## Part 12 — Recorded, not built

### 12.1 Comparing two subtrees is a query, not a rule

Rolling keyword sketches up a tree and then descending two trees together,
pruning where sketches are disjoint, is a join with a hierarchical pruning
predicate. Its unit of work is a **pair** of nodes and its fan-out is a product.

A rule's scope is one node and its subtree. Work scoped to a pair wants a query
planner — join order, pruning selectivity, cardinality estimation over a product
— and a task planner has none of that vocabulary. Stretching one across that
line produces a task queue that is secretly a query engine.

What the job system owes such a query is an index, and producing an index is a
rule. That part is in scope for this design; the join is not.

### 12.2 The soundness trap in that index, recorded now because it is cheap now

Pruning is only sound if a node's rolled-up keyword set is a **superset** of the
union of its descendants'. A decay function that drops low-weight keywords
destroys that property silently: two chapters sharing a rare term get pruned at
the root, and the result reads as "no association" rather than "not checked".

So a node needs two artifacts:

| Artifact | Purpose | Property required |
|---|---|---|
| decayed weighted set | ranking, display | compact, meaningful |
| bounded complete sketch | pruning | superset of all descendants |

`jmfts_core/sketch.py` is already the second one — MinHash, built so a
high-cardinality column is comparable rather than invisible, with
`MinHashLSHEnsemble` for containment. Sketch union is associative, so it is the
roll-up operator unchanged.

Containment rather than Jaccard, for the reason `SPRINT_0_3_0.md` 6.4 already
gives: a chapter's set against a whole book's is asymmetric, and Jaccard reads
near zero under total containment.

---

## Part 13 — Open questions

### 13.1 Where evidence lives — Phase 2 decides, and here is what decides it

`structured_content` is JSONB on `Document` and already holds most of it. A
separate table keyed `(document_id, name)` would make staleness a row update and
make "what is stale" a query, at the cost of a join on every read.

An earlier draft left this as a preference. It is not one. **Two facts decide
it, and both were measured in the tree:**

1. **Concurrency.** 10.1's finer overlap exists to let two `self` writers run on
   one node. Every evidence write today is a read-modify-write on one JSONB
   column — `structured = dict(node.structured_content or {})`, mutate,
   reassign (`settling.py:269-275`, `sheet_tasks.py:243-250`). Two concurrent
   writers lose one write with nothing raised, which is the swallowed failure
   this project's Fail Early rule exists to prevent. So relaxing the claim query
   REQUIRES per-name rows.
2. **Bulk staleness.** Part 9's "binding a rule set at a subtree marks the
   evidence its rules produce as stale" is one `UPDATE ... WHERE document_id =
   ANY(subtree) AND name = ANY(...)` against a table, and a JSONB rewrite of
   every node in the subtree without one.

The two live options are therefore **a separate table plus the relaxed claim
query**, or **JSONB plus the coarse claim query kept exactly as it is** — in
which case 10.1's finer overlap is a bind-time nicety with no runtime effect and
should say so. What cannot be done is relaxing the claim query over one JSONB
column.

Deferred to Phase 2 rather than settled here, because Phase 1's atom
declarations produce the list of who writes what, and that list is the input to
the join-cost half of the trade. The join cost is the one thing above that was
NOT measured.

#### Measured. `scripts/evidence_bench.py`, and the answer is the table

Both shapes built from the same generated tree in a throwaway schema, the JSONB
side carrying the GIN index the real column carries. 100,000 nodes, 620,000
evidence values, 6.2 blocks per node, PostgreSQL 16, medians over 60 runs:

| Read | JSONB | Table | |
|---|---|---|---|
| one node, every value | 0.165 ms | 0.172 ms | the join costs 7 µs |
| one node, one name | 0.143 ms | 0.161 ms | 18 µs |
| guard over a 2,673-node subtree, ids resolved first | 9.56 ms | 10.12 ms | 6% |
| guard over the whole corpus | 18.97 ms | 1.97 ms | **table 10x** |
| stale a subtree's evidence | 83.7 ms | 83.2 ms | parity |

**The join cost is not the obstacle this section worried it might be.** On the
case it should be worst for — one node, every value, one row against six — it is
seven microseconds, and the corpus-wide guard that Part 4.4 will run for every
rule is ten times faster against rows than against a JSONB path.

**And the advantage grows with the corpus, on the read Phase 4 runs most.** The
same script at 10,000 nodes reproduces every line's direction, but the
corpus-wide guard is 3.6x there against 10x at 100,000. JSONB's cost on that
read scales with the number of nodes scanned; the table's scales with the number
of rows carrying the name. So the read that gets worse as the appliance fills is
exactly the one the table fixes, and a measurement taken on a small corpus
understates the reason to move.

**One number is worse and it is a planner effect, not a storage one.** Written
naively — the subtree predicate and the evidence join in one statement — the
table form is 4.1x slower, because with the join present the planner stops using
`idx_documents_path` for the subtree and sequentially scans `documents` instead.
That costs more than the join does. Resolving the subtree to an id list first
removes it entirely. It is a fact about how the query is written, and Phase 4
should write it the second way.

The bulk-staleness number came out at parity rather than the win the second fact
above predicted, and the reason is that both are dominated by the same subtree
scan. The table's advantage there is not speed: it is that `state = 'stale'` on
a row is a state the JSONB column has nowhere to put, so the JSONB form has to
DELETE the blocks — which loses 3.2's distinction between "stale" and "never
attempted" every time a rule set is rebound.

**And the race is not a benchmark.** Two connections, one node, two different
evidence names, each doing exactly what `settling.py:269` and
`sheet_tasks.py:243` do — read the column, copy it, add a key, assign it back:

```
    jsonb   wrote 2 names, 1 survived: ['matched']
    table   wrote 2 names, 2 survived: ['extraction', 'matched']
```

**What the measurement actually settled is narrower than it looks.** This
section was written as a performance trade, and it is not one. The two arguments
for the table are correctness arguments and both were true before the script
ran: a read-modify-write on one column loses concurrent writes to different
names, and `stale` has nowhere to live in a column, so the JSONB form must
DELETE. The join cost was the one thing that could have vetoed the correct
answer on grounds of speed. It does not. Read the numbers above as a veto that
failed, not as the reason to move.

**Decision: a separate table keyed `(document_id, name)`, with `fingerprint` and
`state` columns beside the value.** The two facts this section already measured
point that way and the third does not pull against them.

**The migration is not Phase 2's, and the reason is a contract, not effort.**
`structured_content` is in the API: it is on `DocumentResponse`, it comes back
from search, and clients read `matched.patterns` out of it today. Moving evidence
out of that column changes what every client sees, which is a versioned change
with its own decision to make — whether the column keeps serving a merged view,
and if so who merges it. Phase 2 ships the registry the migration will be written
against, and 14's rule ("no phase leaves a second code path standing beside the
one it replaces") is why it is not started here and finished later.

### 13.2 The rest, unchanged

1. **Whether `probe` is an atom or the bootstrap.** It produces the evidence
   every guard reads and consumes nothing. Treating it as rule 0 of every rule
   set is tidy; treating it as the thing that runs before a rule set is chosen
   may be more honest, since format detection selects the binding.

2. **Whether a binding may be inherited down a subtree.** A rule set bound at a
   book should presumably apply to its chapters. If so, a child node's effective
   binding is a lookup up the path, and two bindings on one path need a conflict
   rule — the same question `SPRINT_0_3_0.md` open question 6 asks about shapes.

3. **Retry policy per failure state (3.2).** The queue classifies `ImportError`
   subclasses as PERMANENT today. A third evidence state wants a policy per
   atom, and where that policy is declared is undecided.

4. **Whether `position` survives reparenting — ANSWERED, Phase 3.**
   `Document.position` orders siblings. A partition that inserts a level must
   decide whether positions are rewritten within each container or preserved
   globally, and PELT's sequence-preserving guarantee depends on the answer.

   9.2 raised the stakes: position is the default `child_key`, so an answer that
   rewrites positions within each container looked like it would make a
   partition's own children un-matchable across a re-run — every child would look
   new, and 9.1's saving would never fire for the atom that needs it most.

   **Positions ARE rewritten within each container, and that does not cost 9.1's
   saving, because the two are different sibling groups.** `reparent` appends a
   moved child into its new group, and `rollup_tasks._reorder` renumbers the
   parent's remaining children into document order. So a chunk's position changes
   when a partition moves it — but a chunk's `child_key` is matched against the
   output of `structure:declared`, over the sibling group that rung wrote, and
   partition never renumbers that group as a whole; it takes a contiguous span out
   of it. Meanwhile `structure:semantic`'s OWN children are the containers, and
   those are numbered by `_reorder` from the segment sequence PELT returns, in
   document order, deterministically.

   This is the shipped behaviour rather than a change, and the reason for stating
   it is that it is a property two modules have to keep jointly.
   `tests/test_rule_scope.py::TestPositionUnderReparenting` is what would notice
   if either stopped. **`_reorder` is load-bearing and was not obviously so**: a
   new container is appended to the tail of the sibling group while a child left
   in place by 11.4's rule 2 keeps the position it had, so without the renumber a
   document whose second segment was containerised and whose first was not would
   come back with the second span first — and 9.1 would then match against an
   order the document does not have.

5. **Whether `chunk` is an atom or a step** (Part 2). It decides whether
   `STRUCTURE_CHUNK_PARAMS` is a rule's params or stays inside the structure
   handler. Phase 1 answers it.

6. **What reconciles an asserted node beside a derived one** (9.4). Clearing
   `produced_by` on a human edit makes the re-run create a sibling rather than
   overwrite, and nothing then merges the two.

### 13.3 Opened by Phase 2 — what `structured_content` returns afterwards

13.1 decided the storage and deferred the migration, and one decision gates
writing it. It is a contract decision, so it wants an answer before code.

**The column does not disappear, and that is the part worth being clear about.**
`structured_content['importance']` is caller-owned, it is read by hybrid
search's importance term (`repositories/search.py:774`), and it is not evidence
— the registry does not name it. Template metadata is the same. So the column
survives the migration carrying exactly the keys a caller put there. The
question is only whether the twenty-three evidence blocks still appear in it.

1. **Merged on read.** No client changes. Every response carrying
   `structured_content` gains a join, and search returns many nodes at once, so
   this is one batched read per response rather than one per node. It also needs
   an owner — the repository, or the response builder — and whichever it is
   becomes the only place that knows the column is two things stitched together.
2. **Not merged.** The column returns caller keys only, and evidence arrives
   under its own field on `DocumentResponse` or its own route. This is honest and
   it breaks every client reading `matched.patterns` out of the column today, so
   it is a versioned change.
3. **Dual-write for a deprecation window.** Excluded by 14's rule — it is a
   second code path standing beside the one it replaces, which is the thing that
   rule exists to prevent.

#### DECIDED: 2. Ingestion stops writing the column, and no read reassembles it

Ingestion writes evidence rows and nothing else. `structured_content` is not
virtualized: no response stitches the rows back into the column, and a caller
reading `matched.patterns` out of it after the migration reads nothing, because
it is not there. A versioned break, taken deliberately.

**The value column of the evidence table is still `jsonb`, and that is not a
contradiction.** What 13.1 measured losing writes was one column shared by every
name on a node. A row per `(document_id, name)` fixes that whatever the value's
type, and fourteen of the twenty-three blocks are dicts that would otherwise need
a table each. "No JSONB writes" means no writes to `structured_content`, not the
retirement of a Postgres type.

**Three consequences, and two of them delete work this document had recorded.**

* **The metadata gate loses its members.** `ingest_owned_keys()` answers "which
  keys may a `PATCH` not name", and afterwards the answer is none of them: the
  column is wholly the caller's. `_merge_structured_content`'s ownership check
  becomes a check with nothing to check. Phase 2's fix is a bridge that carries
  the tree until the migration, and the migration is what makes it unnecessary.
* **3.1's namespace defect closes without a namespace.** Twenty-two ordinary
  English words stop being reserved from callers because they stop being in the
  caller's dict at all. There is no rename to fold into anything.
* **Server-side readers move to the evidence API, not to a merged view.**
  `DocumentService.read_cells` reads `sheet.name`, `sheet.measurements` and
  `anchor` at request time (`services/document_service.py:701`), and the settling
  walk reads `attempts`. These are reads of evidence, and after the migration
  they go to the rows. That is the seam that makes the swap possible, and it is
  why the migration's first step is a read API rather than a table.

---

## Part 14 — Phasing

Seven phases. **The appliance works after every one**, and no phase leaves a
second code path standing beside the one it replaces.

| # | What | Depends on | Ships |
|---|---|---|---|
| 1 | Atom declarations, no behaviour change | — | nothing user-visible |
| 2 | Evidence registry, types, storage decision | 1 | the metadata gate stops losing blocks |
| 2a | Path A onto the queue, then deleted — Part 15 | 2 | one ingest path |
| 2b | The storage migration: evidence to rows — DONE | 2a | a versioned break; the gate retires |
| 3 | Scope on a rule; `produced_by`; one planner — DONE | 2 | the sheet knobs |
| 4 | Guards with operators — DONE, 4.4 and 14.4 | 3 | a threshold a caller sets |
| 5 | Multiplicity and the cost fold — DONE 2026-09-15, `6565385` (marked 2026-10-12) | 3 | `EXPLAIN` says how much |
| 6 | Rule sets, requests, budget, parking — bindings are 5.1's tree | 4, 5 | re-cast at a subtree |
| 7 | The walk generalised; the rollup special case deleted | 6 | — |

Phases 4 and 5 are independent of each other. Everything else is a chain.

**2a is the prerequisite 13.3 did not know it had.** Two ingest paths write
evidence into `structured_content`, and `execute_pipeline` is the one this
document has been calling deprecated without retiring. A migration that moved
evidence to rows while path A still wrote the column would leave both standing.
Part 15 is the plan, and 2b starts when path A is deleted.

**2b is lettered rather than numbered because it is off the chain.** It depends
on 2a and nothing depends on it: 3 through 7 read and write evidence
through :mod:`jmfts_core.evidence`, so where the values physically sit is behind
that module and the migration is a swap underneath it. It can run before 3,
after 7, or between any two of them. What it must not do is run half — the
column and the rows both holding evidence is the second code path this part
forbids. 13.3 is its decision and its consequences.

**`INGEST_SPEC.md` is amended by the phase that changes it, in the same commit.**
That spec is normative and roughly 117 source comments cite it by part and
section; a phase that changes Part 4, 5.3, 5.5 or 6.1 and leaves the spec
describing the old shape turns every one of those citations into a wrong answer.
<!-- 180 as of 2026-09-15, and 164 for this document; 184 and 185 as of 2026-10-12. `CLAUDE.md`'s Documentation section
     carries the command that counts them; the reading is dated because the number moves
     fast and unevenly. The rule is the claim, not the count. -->

This document stays the companion that says why.

**No phase adds a second list of something the code already knows.** Derive it,
or state a rule and audit the rule against what actually ran. Two instances are
on the record and both drifted before anyone noticed: `tests/corpus/vocabulary.py`
exists because a checked-in copy of probe's pattern names made coverage look
better than it was, and `INGEST_OWNED_KEYS` named seven of the twenty-three blocks
the pipeline writes and silently deleted the other fifteen (3.1). Neither was
caught by a test over the list, because a test over a list only proves the list
equals itself. Both are caught by an audit stated over a finished tree. Phases 3
through 7 each add a registry-shaped thing — `produced_by`, rule sets, bindings,
budgets — and each one is an opportunity to make this mistake a third time.

**Phase 1 is done.** `jmfts_core/atoms.py` holds the vocabulary, the twelve
declarations sit beside their handlers, and `tests/test_atom_declarations.py` is
the audit. It changed no behaviour: nothing in the ingest path reads `ATOMS`.
2.5 is what it found.

**Phase 2 is done.** `jmfts_core/evidence.py` is the registry,
`tests/test_evidence_registry.py` is its audit, and `scripts/evidence_bench.py`
is the measurement that settled 13.1. Unlike Phase 1 it changes behaviour, in
one place: `DocumentRepository.INGEST_OWNED_KEYS` is now derived, so a metadata
`PATCH` stops deleting the fifteen blocks the hand-written list did not name.
3.1 and 13.1 are what it found. `INGEST_SPEC.md` 3.3 is amended in the same
commit, per the rule above.

**Phase 2b is done.** `document_evidence` is the store, migration 015 is the
move, `jmfts_core/repositories/evidence.py` is the one door to it, and
`tests/test_evidence_rows.py` is the audit — including the one that holds the
migration's key list against the registry it was generated from, because SQL
cannot import Python and Part 14 forbids a second list without a ratchet on it.
Five things it changed:

* **`structured_content` is wholly the caller's**, so the metadata gate is
  deleted: `INGEST_OWNED_KEYS` and `_merge_structured_content` are gone and
  `update()` assigns the column. A `PATCH` naming `attempts` is now an ordinary
  caller key that reaches nothing, where it used to be a `ValueError`.
* **A versioned break**, taken deliberately (13.3 option 2). A client reading
  `matched.patterns` out of `structured_content` reads nothing.
  `GET /documents/{id}/evidence` is where it went — a route and not a field on
  `DocumentResponse`, because a field would join this table on every search hit,
  which is the cost option 1 was rejected for.
* **Appending to `attempts` stopped reading first.** One `value || entries`
  statement. `upsert_attempt` is the one read-modify-write left, and it is one
  because "the last non-terminal entry with this task_id" is not a predicate a
  JSONB update expresses.
* **3.1's namespace defect is closed** without a namespace, and **six more names
  were found** doing it — the conversation rung's, which nothing had registered.
* **`ingest_summary.summarize_tree` lost its JSONB projection**, which existed
  only to avoid pulling whole blobs across to count two things.

`INGEST_SPEC.md` 3.3, 5.3, 5.6, 8.3, 8.7, 11.3 and 11.4 are amended in the same
commit, per the rule above, and so are `OFFICE_SPEC.md` Part 0 and Part 5.

**Phase 3 is done.** A rule names its scope (`Scope` in
`jmfts_core/ingest_tasks.py`), a node records which rule produced it
(`Document.produced_by`, migration 016), and the five handlers that carried a
literal task list call one planner (`plan_frontier`). `tests/test_rule_scope.py`
is the audit. `INGEST_SPEC.md` Part 4 gains a `Scope` column and a new 4.1, 8.2
gains a `Scope` column and three recorded divergences, and 5.4 gains the advisory
rule 14.1 asked for — all in the same commit, per the rule above. 14.3 is what it
found.

**13.3 is answered, and answering it moved work out of Phase 3 rather than into
it.** Evidence leaves `structured_content` and no read puts it back. So the
namespace 3.1 wanted for twenty-three ordinary English words is not a migration to
fold into Phase 3: the words leave the caller's dict entirely, and 2b is what
does it. What Phase 3 still inherits is one question Phase 1 raised — under the
walk, `citation` fires after its chunks have settled, and 5.4 does not say
whether that is a write to a settled subtree or a re-derivation of one.

### 14.1 Phase 1 is an audit, and it is where the risk actually is

Declare `consumes`, `produces`, `cost_class`, `fanout` and `child_key` beside
each of the twelve `@register_task_handler` functions. Then assert, in a test,
that the edges derived from those declarations equal today's `after` and
`after_any` for every `TaskRow`.

**That test either confirms 2.3 or falsifies it**, and 2.3 — "`after` and
`after_any` are deleted" — is the single claim the rest of the design rests on.
It ships nothing, it changes no behaviour, and it is about a day.

`citation` is the row to watch. Its `after_any=(structure:declared,
structure:inferred)` means "the chunks exist", and its chunks are children under
an untitled region and grandchildren under a titled one — which
`ingest_tasks.py:530-543` already records as the reason it declares
`WRITE_SUBTREE` rather than the `children` `OFFICE_SPEC.md` Part 5 specifies. So
it consumes at `@subtree`, and under 2.2 that is enforced by the walk rather
than by a within-node edge. **`citation` therefore stops being a probe-time
batch member and becomes a settling rule.** That is a behaviour change, it is
probably an improvement, and it should be intended rather than discovered in
Phase 3.

**That is what happened, and Phase 3 inherits one question the prediction did
not carry.** Under the walk, `citation` fires when the file node is evaluated,
which is after every chunk beneath it has settled. Today it fires while they are
still in flight, because the file node cannot settle with a pending task on it.
So the walk version writes anchors into nodes that are already settled, and
`INGEST_SPEC.md` 5.4 does not say whether that is a write to a settled subtree
or a re-derivation of one. `ADVISORY_TASK_TYPES` already holds `citation` and
already says a chunk with no anchor is a chunk, which is the argument that it is
safe — but the spec has to say so, and 14's rule means Phase 3 amends it.

### 14.2 What each phase closes

* **After 3**, all four of Part 0's measured consequences close: the sheet knobs
  become settable, `sheet_tasks.py:135`'s fingerprint claim becomes true,
  `EXPLAIN` stops stopping at the sheet list, and `ingest_options`' validation
  reaches `max_rows`.
* **After 4**, a threshold moves without a code change, which is most of the
  reason to have rule sets at all. **That holds only because 4.4 point 2 lets a
  guard's right side name an option.** With the literal this document first
  specified, the threshold sits in `TASK_ROWS` and moving it IS a code change —
  so this bullet as first written would have been false at the end of the phase
  it names. It becomes true for a whole subtree once 5.1's walk lands beside it.
* **After 5**, `EXPLAIN` and `ANALYZE` report how much rather than only which.
* **After 6**, `SPRINT_0_3_0.md` steps 7, 8 and 9 unblock.
* **After 7**, `IngestRollupPlanner` is rules consuming `@children` and the
  rollup's special case is gone.

### 14.3 What Phase 3 found

All four of 14.2's consequences closed and the suite is green at 2112 tests.
Seven things came out differently from the plan, and each is a decision rather
than a slip.

1. **4.1's second form is wider in both halves**, and the second widening would
   have shipped a defect otherwise: a rule scoped to a rung's children with no
   usetype puts an `embed` on every `section` container, and `run_embed` refuses
   empty content. 4.1 above is amended with both.

2. **Part 0 counted six literal enqueue sites and there were eight.** S7's
   conversation rung added two after the count was taken. Five of the eight are
   now `plan_frontier` calls. The other three are not literal lists and were
   never the problem: `rollup_tasks.py`'s container enqueue already asked the
   planner (Part 0 names it as the shape the rest should have), the
   `summarize:llm` handoff is 11.1's guarded rule and needs Phase 4's operators to
   become one, and `enqueue_batch` itself survives everywhere — 6.4 is why.

3. **The node kinds had to move to the model, and one of them was already written
   twice.** A scope names a `usetype`, Part 4's table is below every handler
   module, and every one of those modules imports the table's module — so
   `USETYPE_SECTION`, `USETYPE_CHUNK`, `USETYPE_SEGMENT`, `USETYPE_SHEET`,
   `USETYPE_SUMMARY` and `USETYPE_RECORD` joined `USETYPE_FILE` on
   `jmfts_core.models.document`, which is where that constant already lived for
   the same reason. `services/document_service.py` carried its own
   `USETYPE_SHEET = "sheet"` with a comment explaining that importing
   `sheet_tasks` would register every handler as a side effect. That reasoning was
   right about `sheet_tasks` and was not a reason to copy the string; the copy is
   gone and the import is the guard.

4. **Three new option groups, and they are not new knobs.** `embed`,
   `sheet_profile` and `sheet_records` are the literal `params` dicts that were on
   `EMBED_CHUNK_SPEC`, `PROFILE_SHEET_SPEC` and `EXTRACT_SHEET_SPEC`. A rule's
   parameters come from a group, so they arrived where every other task's
   parameters live. Two sheet groups rather than one, because 6.1's diff is the
   point: sharing a group would move `profile:sheet`'s fingerprint every time a
   caller changed `max_rows`, so a re-ingest that only raised the row ceiling
   would re-measure every sheet.

5. **Two handlers stopped defaulting their own parameters.** `run_extract_sheet`
   read `int(params.get("max_rows", DEFAULT))` and `run_profile_sheet` read
   `params.get("sketch_columns", True)`. The group is complete on every queue row
   the planner writes, so a default in the handler is a second source of the
   value and which one ran would depend on what enqueued the task. Both read
   `params[...]` now. This is what makes `ingest_options._positive_int` reach
   `max_rows` — Part 0's fourth consequence — because the value now arrives
   validated instead of arriving from a constant.

6. **`enqueue_frontier` checks the child it is given.** A frontier is planned once
   per fan-out and used N times, so the pair it was planned for and the pair the
   node was stamped with are two statements made a few lines apart. If they drift
   the child gets another kind of node's batch, and nothing downstream could tell:
   the tasks would run, fail on evidence they cannot find, and read as a broken
   document. This is Part 14's "state a rule and audit the rule against what
   actually ran", applied to the one thing the planner cannot verify for itself.

7. **`in_flight` stopped being a constant at four of the five sites.** A node
   created `settled` with work queued on it is a false claim its ancestors roll up
   over; one created `in_flight` with no work parks the tree forever. Which case
   it is has always been "is this node's batch empty", and only
   `run_structure_sheets` computed it — the rest passed `SETTLED_IN_FLIGHT`
   outright and were correct only because `embed` always fires. It is read off the
   frontier everywhere now.

**Two things Phase 3 deliberately did NOT build, both recorded rather than
forgotten.** 9.3's per-vertex write mode is not built: a re-run that deletes an
unmatched child writes `subtree` where a first run writes `children`, and nothing
re-runs a fan-out rule yet — the attempt diff still prevents it — so the
escalation would reserve a region for a delete that cannot happen. 9.1's matching
is not built for the same reason. Both become live in the phase that makes
re-derivation happen, and `produced_by` is the column they were waiting on.

### 14.4 What Phase 4's design settled, and what it was

**Built 2026-09-01.** What the build changed about the design is at the end of
this section; the design as taken is below, unaltered, because two of the three
changes are corrections to it.

Designed 2026-09-01. The method is worth recording because it is what
changed the answer: the scenarios were written first, as sentences a person
would say to an appliance, and the machinery was derived from them. Reading
forwards from 4.4's grammar gave a phase with no customer and a regression in
it; reading backwards gave a smaller phase with four.

**Six decisions, and four of them made the phase smaller.**

1. **A guard's right side may name an option, not only a literal.** This is the
   one that makes 14.2's Phase 4 bullet true. `enabled_by` folds in with it.
2. **There is no refusing guard.** Ingest means "extract as configured", not
   "perform every step", so a cascade of no-ops is a correct outcome and the
   cascade is already built. Probe keeps the only refusal, because probe is the
   only place with no input to plan from.
3. **`requires` and `forbids` both survive, and connectives are dropped.** They
   are two unknown policies rather than a weak boolean algebra, and flattening
   them would empty every `.docx`. 4.4's correction has the plan output that
   shows it.
4. **A guard reads probe's patterns and the resolved options, and nothing
   else.** Every scenario that wants an operator is satisfiable from the 31
   patterns. The two cases that reach further — `sheet.measurements.rows` and
   11.1's `fit_truncated` — are both evaluated at a node after an atom has run
   on it, which is the walk, which is Phase 7. 11.2's purity rule needs no
   amendment.
5. **The unit of configuration is the document, and a collection is a subtree.**
   5.1. This moves work out of Phase 6 rather than into Phase 4.
6. **Two piles of guard, and only one is configuration.** 4.4's second
   subsection. A correctness check must not sit behind a caller's switch.

**Decisions 2, 3 and 4 each deleted something this document had specified**, and
that is the second reason to record the method. Connectives, a refusal outcome,
and an evidence reader inside the planner were all in scope on 2026-08-27 and
none of them survived contact with a stated user scenario.

The phase is now:

1. Amend `INGEST_SPEC.md` Part 4, per 14's rule, in the implementing commit.
2. A term type and a comparison evaluator, replacing the truthiness test in
   `ingest_tasks._blocking_reason`. Both loops keep their shape and their
   unknown policy.
3. One not-applicable sentence per operator, in the family already there.
4. Two checks in `_check_task_rows`: every name registered, and every comparison
   type-checking against `evidence.get(name).type`. These are Part 10's check 6,
   arriving early because the audit already exists to hold them.
5. The appliance's own guards. `is_damaged` first — one measured flag that turns
   a runtime failure into a stated no-op.
6. The caller's guards, with the option keys they name. `facts.min_characters`
   is the one 14.2 rests on.
7. `ExplainedTask.requires` / `.forbids` become term lists;
   `python -m scripts.generate_client`. A third client-visible break in 0.3.0,
   after evidence leaving `structured_content` and `PipelineStageInfo` leaving
   the contracts.

**One question is open and it is 4.4's last subsection**: whether "why this
document has no children" becomes registered evidence rather than a blob inside
the probe attempt's detail. Under decision 2 the reason chain stops being
diagnostics and becomes the deliverable, which is the argument for promoting it.

#### What the build changed

Three things, and two of them are corrections the design could not have reached
without running.

1. **A caller's threshold is a `forbids`, not a `requires`.** Probe's counts are
   per-format and `char_count` is `text`-only, so the step-6 example above,
   written as a requirement, would have stopped fact extraction on every PDF,
   `.docx` and `.pptx`. 4.4's correction subsection carries the measurement. The
   design's own decision 3 is what makes the fix available, which is the second
   time that decision has paid for itself.

2. **The vocabulary decision was not in the design at all.** Step 4 asked for
   "every name registered", and there was nothing to register against:
   `matched.patterns` is an open namespace by design, and `evidence.pattern_type`
   answers `bool` for a name it has never heard of. So `GUARDABLE_PATTERNS` is a
   closed list declared beside the table, with the types the comparisons are
   checked against — `pages_with_tables` is the case that proves the default type
   wrong, since it holds a list and no test could catch that while probe never
   emitted it. `PATTERNS_NOT_PROBED` is the stated rule Part 14 requires in place
   of a second list, and `tests/test_rule_guards.py` audits it against the
   installed probe.

3. **Step 7 did not happen, and it should not have been step 7.**
   `ExplainedTask.requires` and `.forbids` stay `list[str]`; a truthiness term
   renders as the bare pattern name it always did, and a comparison renders as
   the comparison. Whether the wire wants the STRUCTURE of a term is a separate
   question from whether the table has one, and 0.3.0 does not need a third
   client-visible break to answer a question nobody has asked.

The rest landed as designed. `enabled_by` folded into a term and kept its
ordering — `Term.reads_option` is where that went — and `extract:tables` was
recorded rather than resolved, which is what surfaced that nothing re-plans a
node after extraction and so nothing can ever satisfy it.

---

## Part 15 — Path A, and the single path 2b needs

`pipeline.py`'s `execute_pipeline` is path A: the synchronous ingest that runs
to completion inside the request. `ingest_tasks.py` and the queue are path B.
Both write evidence into `structured_content`, so 2b cannot move evidence to
rows without migrating both — and Part 14's rule forbids migrating one and
leaving the other. **Path A moves onto the queue, and then it is deleted.**

Two production callers: `IngestService.ingest_content` (`POST /ingest`) and
`ConversationService` (`POST /conversations/ingest`). Six test modules call it
directly.

### 15.1 What path A has that path B does not

1. **A content-string entry.** Path B starts from uploaded bytes; path A takes
   `content` as a string and never stores a blob.
2. **Seven named pipelines** — `conversation`, `markdown`, `raw`, `transcript`,
   `wiki:url`, `wiki:arxiv`, `wiki:pdf` — each a set of stage defaults.
3. **`extract_facts` as a stage.** Path B has no such task. 736's own docstring
   says facts "belong to rollup (5.4) and are the settling walk's business", and
   the walk does not do it: `IngestRollupPlanner` offers `structure:semantic`
   and `summarize` and stops.
4. **Conversation parsing** — `conversation_ingest.py`, 674 lines, JSONL and
   message arrays into a tree with speaker and timestamp metadata.
5. **Fetching** — a URL, an arXiv id, or a local PDF path fetched and converted
   before any of the above runs.
6. **A synchronous response** carrying `message_count`, `segment_count`,
   `summary_count`, `triple_count`, `tree_depth` and a per-stage list.

### 15.2 Four decisions, taken

1. **`POST /ingest` stays synchronous.** It enqueues and then drains ITS OWN
   document's tasks inside the request, returning the `IngestResponse` it
   returns today. The wire does not move; the work does. This is not a second
   code path — it is the same tasks, run by a worker the request owns.
2. **`usetype` becomes an option profile.** `jmfts_core.ingest_options` already
   resolves per-format profiles; the seven names become seven more, and
   `GET /ingest/pipelines` answers from that table instead of from
   `PipelineDefinition`. The concept survives with one implementation under it.
3. **A conversation is a probed format.** `probe` learns to recognise adjutant
   JSONL and a message array, and reports it as a pattern. A `TASK_ROWS` row
   requires that pattern. Conversations get the attempt log, the retry
   classification and `EXPLAIN` that every other format already has.
4. **Fetching becomes queue tasks** — `fetch:url` and `fetch:arxiv`. A flaky
   network is then a TRANSIENT retry in machinery that exists, recorded in the
   attempt log, instead of a request that hangs and reports nothing.

### 15.3 Three things that need machinery that does not exist yet

**A drain scoped to one document.** `TaskQueueRepository.claim_next` takes a
worker id and badges and claims the highest-priority claimable row anywhere.
`POST /ingest` must not run another document's work inside a caller's request,
so `claim_next` gains an optional root scope and `IngestWorker` gains a drain
that uses it. Two consequences, both wanted: `FOR UPDATE SKIP LOCKED` already
makes the request and the background worker safe to run at once, and a request
whose task the background worker claimed first must WAIT for it rather than
return early — so the drain ends on "this root has no unfinished task", not on
"I could not claim anything".

**A summary computed from a finished tree.** `IngestResponse`'s six counts come
out of `execute_pipeline`'s own stage results today. Afterwards they are a
function over the settled subtree and its attempt log. That function is the only
new thing `POST /ingest` needs to keep its wire.

**A conversation pattern in probe.** Tier 1 only — `json.loads` on the first
line, and a check for the keys a message carries. No new dependency, and it must
stay in the base install for the reason `OFFICE_SPEC.md` gives for every other
probe: a base install that cannot recognise a conversation would accept one and
report an empty pattern set.

### 15.4 The stages, and the appliance works after each

| # | What | Deletes |
|---|---|---|
| S1 | The seven pipelines become option profiles; `/ingest/pipelines` reads them | — |
| S2 | A content string becomes a file node with stored bytes | — |
| S3 | `claim_next` takes a root scope; `IngestWorker.drain_document` | — |
| S4 | `IngestResponse` computed from a settled subtree | — |
| S5 | `POST /ingest` cuts over for `markdown`, `raw`, `transcript` | `_execute_markdown`, `_execute_text` |
| S6 | `extract:facts`, offered by `IngestRollupPlanner` after `summarize` | the `extract_facts` stage |
| S7 | The conversation pattern, `structure:conversation`, the route cut over | `_execute_conversation` |
| S8 | `fetch:url`, `fetch:arxiv`, and the local-PDF path | `_execute_url`, `_execute_arxiv`, `_execute_pdf` |
| S9 | `execute_pipeline`, `PipelineDefinition`, the registry, and path A's tests | `pipeline.py` |

S1 through S4 add machinery and change no behaviour. S5 through S8 each move one
family of callers and delete the path-A code that served it in the SAME commit —
that is Part 14's rule applied inside this part. S9 is the deletion that makes
the claim true.

**2b starts after S9 and not before.** A migration that moved evidence to rows
while `execute_pipeline` still wrote the column would be exactly the half-done
state 14's rule forbids.

### 15.5 What actually happened

All nine ran. `jmfts_core/pipeline.py` is deleted, every entry point is served by
the queue, and the suite is green at 2034 tests. Five things came out differently
from the table above, and each is a decision rather than a slip:

1. **S5 could not delete `_execute_markdown`.** The three `wiki:` handlers called
   it, so it was renamed `_ingest_fetched_markdown` — no caller could reach it
   with a markdown string of their own, which is the property S5 was for — and
   S8 deleted it with them. The same shape applied to path A's `extract_facts`
   stage, which S6's table said to delete: its last callers were `conversation`
   and the three `wiki:` usetypes, so it went in S7 and S8.

2. **S5 also had to build `INGEST_SPEC.md` 11.5**, which this plan did not name.
   Path A ran `_index_subtree_bm25` at the end of every ingest; moving those
   usetypes onto the queue without a replacement would have taken them out of
   full-text search silently. `index:bm25` reads the file node's `path` and joins
   every index whose registered root is the node or an ancestor — 11.5's rule,
   not path A's unconditional `default`. It fixes `POST /ingest/file` too, which
   had never reached a BM25 index at all.

3. **`extract:facts` is a `TASK_ROWS` row, not a rollup task**, against both this
   plan and `plan_after_probe`'s own docstring. `jmfts_core/fact_tasks.py` gives
   the reason: it is ONE run over the whole tree with shared entity caches, and
   the planner is called per node, so under the walk a `section` and the `file`
   node above it would each start their own run over overlapping leaves. That
   needed `TaskRow.enabled_by` — the first condition in Part 4's table that is
   not a measurement, because whether to spend an LLM call per chunk is a choice
   no property of the bytes decides.

4. **The fetch tasks store what they fetched, not what they converted.** The plan
   said `fetch:url` and `fetch:arxiv`; there are three, with `fetch:path` for a
   local file, and none of them converts anything. Probe measures the stored
   bytes and the existing readers do the rest, so a fetched HTML page gets
   `extract:text`'s markup reader and a fetched PDF gets the real PDF pipeline —
   an outline-derived rung, page geometry and `citation`, none of which survived
   path A flattening it to markdown first. `source` is a new evidence block and
   the registry's pinned counts moved to 37/23/14.

5. **A defect S5 introduced and a later commit fixed.** The inline drain ran on
   the event loop, where `llm_client.complete_sync` refuses to nest — so
   `summarize` would have raised the moment a node's children overflowed the
   embedding window. `ingest_content` hands the drain to `asyncio.to_thread`,
   which also stops a minutes-long drain from stalling every other request.

Two behaviour changes a caller can see, both deliberate and neither in the plan:
the root node's `usetype` is `file` rather than `raw`/`markdown`/`transcript`,
because it is a node holding stored bytes; and a `parent_id` that does not
resolve is a 404 instead of a warning and a silent degrade to a root document.
