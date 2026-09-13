# Sprint 0.6.0: the gates that do not close, and the answers that never come

Written 2026-09-13, against `master` at `4349f59`, which is `v0.5.1`.

**This is the first sprint plan written in the public repository.** Development moved here at
0.5.1, so this document is not a copy of an internal one and there is no internal version of
it to diverge from. It cites `docs/SPRINT_0_4_0.md` and `docs/SPRINT_0_5_0.md`, which are
**not** published — a reader outside the project cannot open them. That is the same held-back
citation the README's "A note on documentation" describes, and Block E is the step that ends
it.

**Where the scope came from.** `docs/SPRINT_0_4_0.md` and `docs/SPRINT_0_5_0.md` were read end
to end and every item neither finished is collected here: 0.4.0's Block C, deferred whole;
0.4.0's step 14, which diverged and left a residue; 0.5.0's stretch, which neither trigger
fired for; and the numbered gaps in `ROADMAP.md` that acquired no owner. Nothing was dropped
for being inconvenient — what is not a step is in Part 3 or Part 4 with the reason.

**Theme: what this appliance does not stop, and what it does not return.** Two access gates
are open and a principal can act through both. Three retrieval paths answer with nothing and
say nothing about why. The two halves are the same shape — a check that is absent, and an
absence that looks like an answer — and Fail Early is the rule both violate.

---

## Part 0 — How a defect enters this sprint

Unchanged from `docs/SPRINT_0_4_0.md` Part 0, restated because this is the first plan a public
reader will meet:

**An open defect is a numbered step in the current sprint plan, and the entry condition is a
failing test.** Not an argument that something could go wrong, and not a measurement of how
often it does.

There is no standing defect file. `docs/archive/KNOWN-DEFECTS.md` holds D1–D7, all resolved,
and records history. `ROADMAP.md`'s numbered gaps are not defects: a gap there is something
somebody has to decide or measure before it could be a step, and the gaps that became steps
here say so in their rows.

The rule decides three live cases in this sprint's own scope, and it decides them
differently:

* **Block A's two gates are steps.** Each is a missing check at a named line, and the test
  that fails is one a reader can write from the row without a measurement: a principal acts
  on a document it may not act on, and the call succeeds.
* **Block B's step 4 is a step.** `docs/STRESS_CORPUS.md` 4.4 measured zero `index:bm25` tasks
  across a 21-workbook subtree, and a fixture that ingests one workbook and searches BM25 for
  a string in it fails today.
* **`ROADMAP.md` gaps 7, 10 and 11 are NOT steps.** Each is a decision with a measurement
  behind it and no failing test — the appliance does what it was built to do in all three,
  and what is wrong is the thing it was built to do. They are Part 4 open questions. Gap 7 in
  particular is not a defect against `jmfts_core` at all: `CHANGELOG.md`'s 0.5.1 entry
  records that the dead-node behaviour under it is pgvector deferring graph repair, which is
  a documented design choice of that extension.

---

## Part 1 — What verified this scope

Every row is a `grep` or a read over this tree at `4349f59`, on 2026-09-13.

| Claim | Where |
|---|---|
| `DocumentRepository.create_link` constructs and flushes a `DocumentLink` with no principal check | `jmfts_core/repositories/document.py:1119`, flush at `:1136` |
| `DocumentService.create_link` checks only that `request.source_id` equals the id in the URL | `jmfts_core/services/document_service.py:1287`, check at `:1289` |
| `get_centrality`, `get_subtree_authority`, `get_spines` and `get_communities` take no principal | `jmfts_core/services/graph_service.py:137`, `:179`, `:234`, `:381` |
| `can_read` is called ONCE in that file, in `get_neighbors` | import at `:58`, call at `:335` |
| `index:bm25` names three prose rungs in `after_any` and not `structure:sheets`, and requires `HAS_TEXT_LAYER` | `jmfts_core/ingest_tasks.py:1173`–`:1182` |
| That row's own comment says "exactly one of the two rungs is ever eligible for a document" | `jmfts_core/ingest_tasks.py:1161`–`:1162` |
| `HEADER_ROW_NUMBER = 1` is a module constant, not a measurement | `jmfts_core/sheet_records.py:60`, skipped at `:161` |
| `_header_evidence` requires the whole used extent to be text | `jmfts_core/office/sheets.py:391` and its docstring |
| `rendered_markdown` is built, counted, reported as a boolean and discarded | `jmfts_core/sheet_tasks.py:437`, `:442`, `:499` |
| `MINIMUM_RECALL = 0.90` and `RECALL_FLOOR` is 13 ULP under it | `tests/test_maxsim_recall.py:80`, `:98` |
| `HNSW_ITERATIVE_SCAN = "strict_order"`, set with `SET LOCAL` on two paths | `jmfts_core/repositories/search.py:62`, `:455`, `:1136` |
| `search_exclude_usetypes` defaults to four names, one of which is what hides the derived tree | `jmfts_core/config.py:219` |
| `shacl_max_scope_documents` exists and defaults to 512 | `jmfts_core/config.py:307` |
| No `POST /documents/batch` anywhere | `git grep documents/batch` over `jmfts_core jmfts-client tests` returns nothing |
| 695 citations from shipped files name a document this repository does not carry | the `git grep -hoE` command in `CLAUDE.md`'s Documentation section, summed over the held-back names |

**One number above is much larger than the reading `CLAUDE.md` carries.** That file says
"around four hundred source comments cite a `docs/` file", read 2026-09-10. Re-running its own
command on 2026-09-13 gives 695 over the held-back names, the three largest being
`SPRINT_JOBS.md` at 158, `INGEST_SPEC.md` at 149 and `SPRINT_0_5_0.md` at 87. The command is
in `CLAUDE.md` precisely because the number moves; Block E is what the number argues for.

---

## Part 2 — The scope

**Five blocks, fifteen steps.** The blocks are a schedule and the order is argued here.

* **A runs first** because it is the only block where the appliance permits an action rather
  than merely failing to perform one. Everything else in this sprint is something that does
  not happen; Block A is something that does.
* **E runs second**, out of size order, because it is what makes the rest of this document
  readable to anybody who did not write it. A plan whose steps cite two specifications a
  reader cannot open is a plan that can only be executed by its author.
* **B, then C, then D.** B is defects, C is a deferred feature with measurements already
  paid for, and D is a chain whose later links are not in this sprint at all.

**This sprint moves the client wheel zero times and adds no migration, as scoped.** No step
below changes a contract class in `jmfts-client/jmfts_client/contracts/`, and no step below
adds DDL. Both halves are claims about the steps as written; a step that turns out to need
either is a step that has to come back here and say so, because `docs/SPRINT_0_4_0.md` Part 2
made the same claim twice and was wrong both times.

### Block A — the two access gates that do not close

`ROADMAP.md`'s "Subtree RBAC — remaining follow-ups" is the source, and both items are marked
CONFIRMED OPEN there against two separate re-checks, 2026-08-28 and 2026-09-13.

| Step | What | Entry condition | Size |
|---|---|---|---|
| 1 | Gate edge WRITE on the principal, at the repository and the service | A principal with read-only access to document *B* calls `POST /documents/{A}/links` with `target_id = B` and it succeeds | medium |
| 2 | Gate the four graph-analytics verbs on the principal | A principal with no grant calls `GET /graph/centrality` and reads back ids and titles of documents it cannot read | medium |
| 3 | A standing audit: every `@expose`d method either takes a principal or is on a list that says why not | The audit does not exist, so the next ungated verb is added the same way these two were | small |

**Step 1 is a write gate and the question is which document it checks.** An edge has two
ends. Requiring write on the source alone is what the URL already implies and would close
nothing; requiring write on both ends is the narrowest rule and would refuse a legitimate
"cite this public document from mine". **Absent a decision, require write on the source and
READ on the target**, which refuses the leak — attaching an edge to a document you cannot see
— without refusing citation. Part 4 question 4.1 carries the argument and the cost of the
other answers.

**Step 2's cost is not the check, it is where the check goes.** `get_neighbors` is gated
because `walk_neighbors` hides unreadable nodes *during* the walk, which is a filter inside
the traversal. The four analytics verbs call `build_graph` and then compute over the whole
result, so gating them means either filtering the graph before the metric runs — which
changes every score, because centrality is a property of the graph you computed it on — or
filtering the returned ids afterwards, which leaks nothing but reports numbers derived from
rows the caller cannot see. **These are different products and the plan does not get to
pretend otherwise.** Part 4 question 4.2.

**Step 3 is the reason the first two are not enough.** Both gaps were found by an audit, both
were re-confirmed twice, and neither was found by a test.
`tests/test_api_parity.py::test_registry_and_generated_routes_are_in_bijection` (`:43`, with
`REGISTRY` imported at `:23`) already reads the registry from a test and asserts a bijection
against the mounted routes; the audit this step adds reads the same registry and asks a
different question of it.
An entry on the "why not" list is a decision with a reason attached, which is the same shape
as `tests/test_readme_links.py::NOT_PUBLISHED`.

### Block E — the working record, republished

Runs second. The block letter is E because this document keeps the letters in the order the
blocks were written, and the schedule is the paragraph above rather than the alphabet.

| Step | What | Entry condition | Size |
|---|---|---|---|
| 4 | Review and publish the specifications shipped code cites: `INGEST_SPEC.md`, `SPRINT_JOBS.md`, `OFFICE_SPEC.md` | 387 citations from shipped files point at these three and resolve to nothing | medium |
| 5 | Review and publish the measurement records: `ANN_INDEX_HEALTH.md`, `STRESS_CORPUS.md`, `MEASURE_*.md`, `CORPUS.md` | 79 citations, and three of this sprint's own Part 4 questions cite them for their numbers | small |
| 6 | Decide, once, what happens to the sprint plans and the archive | 201 citations name `SPRINT_0_3_0.md`, `SPRINT_0_4_0.md` or `SPRINT_0_5_0.md`; this document adds more | small, decision-shaped |

**The counts are the argument and they are not stable.** 695 citations from files this
repository publishes name a document it does not carry. A reader who follows the explanation
layer of this code — which is where the *why* lives, deliberately, rather than in the code —
arrives nowhere 695 times. The README says the held-back list "is meant to shrink" and that a
document joins "when it has had a review pass rather than when a release runs"; from 0.5.1
there is no release-shaped copy step to hide behind, so the pass is the only thing left
between these files and the repository they are already cited from.

**What a review pass is, so the step is not open-ended.** Read for three things and only
three: a private host name or path, a named third party who has not agreed to be named, and a
claim about the tree that has gone false. It is not a rewrite and not a tidy-up. The documents
are the working record and they are allowed to read like one — the value in `INGEST_SPEC.md`
is that a comment can cite 5.3 and mean it.

**Step 6 is a decision and it is deliberately separate.** A shipped sprint plan is a record of
what was decided and why, including the things that were wrong when written and corrected in
place. That is the most useful kind of document to publish and the most uncomfortable one.
Part 4 question 4.5.

### Block B — retrieval that answers with nothing

| Step | What | Entry condition | Size |
|---|---|---|---|
| 7 | `index:bm25` reaches a workbook's `record` nodes | Ingest one workbook, search BM25 for a string in a cell, get nothing | medium |
| 8 | The margin on the MaxSim recall threshold | Public CI read `21.5/24` on 0.5.0 — one document genuinely short, with the ungated control at `0.9333` | medium |

**Step 7 has two independent exclusions and both have to move.** `after_any` on the
`index:bm25` row names `structure:declared`, `structure:inferred` and `structure:conversation`
and not `structure:sheets`; `requires=(HAS_TEXT_LAYER,)` is a PDF property no workbook can
satisfy. Either alone keeps the tasks from being created. The row's own comment says "exactly
one of the two rungs is ever eligible for a document" (`ingest_tasks.py:1160`–`:1162`), which is true
of the prose formats it was written about and false for a workbook, so the comment is part of
the fix.

**Step 7's real cost is what "the content" means for a sheet.** A `record` node's text is
labelled prose built by `sheet_tasks.py`, and a `cell` node's is one value. Indexing every
`cell` would put a corpus of bare numbers into the inverted index and move every IDF in the
index it joins. The narrow version — `record` nodes only — is what the measurement in
`docs/STRESS_CORPUS.md` 4.4 is about (4,601 `record` nodes averaging 457 characters) and is
what this step scopes. Part 4 question 4.3.

**Step 8 is the one step here whose entry condition is a run rather than a fixture, and that
is stated rather than hidden.** `tests/test_maxsim_recall.py` measures `recall@10` against an
exact scan over 24 queries and asserts the mean clears 0.90. Public CI read `21.5/24 = 0.8958`
on 0.5.0 with the ungated control at `0.9333`, which is one document short and genuinely
short — not the floating-point tie that 0.5.1 fixed. Twelve local runs at 0.5.1 read
`217/240` to `224/240`, two to eight documents above the failing reading. So the threshold is
inside the spread of the thing it measures, and the step is to find out which of three
accounts is true: the corpus is too small for a 1/240-resolution threshold, the ANN index is
genuinely losing a neighbour under some condition CI has and this machine does not, or 0.90 is
simply the wrong number for a 24-query fixture. **The step is not "raise the threshold until
it passes"**, and it is not "add retries": a test that is green because it was asked twice
measures nothing. Part 4 question 4.4.

### Block C — the office read path, resumed

**These are `docs/SPRINT_0_4_0.md` Block C steps 8 through 12, renumbered here and otherwise
unchanged.** That block was deferred out of 0.4.0 on 2026-09-05 with its measurements intact,
and its text is still the specification for these steps. The mapping, so nothing is ambiguous:

| Here | There | What |
|---|---|---|
| 9 | 0.4.0 step 8 | Scan rows 1–8 for the header; carry the header's row index as a measurement. Amends `INGEST_SPEC.md` 8.3 |
| 10 | 0.4.0 step 9 | A second pass allowing numeric headers: non-empty and distinct, any type. 8.3 |
| 11 | 0.4.0 step 10 | Emit a `table` node — the whole sheet as one markdown table — at the 8192 window. 8.4, 8.8 |
| 12 | 0.4.0 step 11 | Emit both shapes where both match, instead of first-match-wins. 8.4 |
| 13 | 0.4.0 step 12 | A column label for a headerless table, read from the banner row step 9 finds. 8.5 |

**Verified not started, 2026-09-13, by reading the tree rather than by trusting the
deferral.** `HEADER_ROW_NUMBER = 1` is still a module constant (`sheet_records.py:60`);
`_header_evidence` still requires the whole used extent to be text (`office/sheets.py:391`);
`rendered_markdown` is still built, counted, reported as a boolean and discarded
(`sheet_tasks.py:437`, `:442`, `:499`).

**What this block is worth, measured over 30,448 FUSE and 8,652 git-corpus sheets in
2026-09-03:**

| | FUSE (open web) | git corpora |
|---|---|---|
| today: 8.3's rule at row 1 only | 23.9% | 35.5% |
| step 9: the same rule, rows 1–8 | **45.5%** | **44.7%** |
| step 10: plus the numeric second pass | **54.4%** | **54.8%** |

`docs/SPRINT_0_4_0.md` Block C carries the rest: why three cheaper relaxations were measured
and rejected on ordering rather than on rate, why the window is 8192 and not 512, the
contested-population table that argues for emitting both shapes, and why the absolute
percentages are optimistic while the relative gains are not.

**This block's entry condition is a fire rate and not a failing test, which is why it left
0.4.0 and why it is fourth here.** Nothing is broken at 23.9% — the shipped rule does exactly
what `INGEST_SPEC.md` 8.3 says it does. One reading has arrived since the deferral that points
the same way from the appliance side: on a real 21-workbook ingest, 18 of 33 worksheets
produced zero `record` nodes (`docs/STRESS_CORPUS.md` 4.4b).

**Open question 4.3 of `docs/SPRINT_0_4_0.md` comes back with this block** — what rows 1 to
N−1 are, when the header is at row N. It is asked by step 9 and spent by step 13, and it stays
inside the block that pays for it.

### Block D — the job-system chain, the first two links

`docs/SPRINT_JOBS.md` Part 14 is the phase table. Six of seven phases have landed; these are
the next two, and they are the two that are unblocked.

| Step | What | Source | Size |
|---|---|---|---|
| 14 | Phase 5: multiplicity and the cost fold, so `EXPLAIN` says how much rather than only what | `SPRINT_JOBS.md` Part 7 | medium |
| 15 | Phase 4's leftover: the route and the ancestor walk for per-collection configuration | `SPRINT_JOBS.md` 5.1 | medium |

**Neither is blocked and both were deferred for the same reason twice.** `docs/SPRINT_0_5_0.md`
put them in a stretch section that would run only if Block A's step 5 came back cheap; it came
back at 57.5–59.0 s of drain at the bound, which is cheap by its own criterion, and the slot
went elsewhere. They compete on their own merits now rather than on a trigger.

**Step 15 is a route and a walk, not a scoping mechanism.** Phase 4's design moved
per-collection configuration out of phase 6 rather than into itself: a collection is a
subtree, so a setting on a node reaches the documents beneath it, and the ancestor walk that
does it already runs one level down in `rollup_tasks._options_owner`. RBAC, which is already
subtree-scoped, answers who may set it.

**One debt step 14 inherits and does not pay.** `Fact.locus` has no term for a write into
another tree and three shipped tasks write there. It is recorded at `docs/INGEST_SPEC.md` 5.3,
in `jmfts_core/atoms.py` and in the handlers' own declarations, so the phase that takes
`EXPLAIN` inherits a written statement of what it is not reporting. Step 14 should state what
it does not report rather than quietly report a number that is wrong by three tasks.

---

## Part 3 — Not in this sprint

Named so that a reader who finds one of these elsewhere knows it was scoped out rather than
missed.

| What | Where it went | Why |
|---|---|---|
| **`SPRINT_JOBS.md` phases 6 and 7** | later | Sequential behind steps 14 and 15, and phase 7 deletes the rollup special case, which is a change to a path every ingest runs. Two links of a chain is a sprint; four is a rewrite. |
| **The component query: JSONB filter, census, projection** | unscheduled | The projection's shape is undecided — `docs/SPRINT_0_4_0.md` 4.1 — and shipping the filter without it delivers a capability with no consumer. `jmfts_core/effective_content.py` supplied a worked precedent for the cost side (4.9 ms for a 100-container page) and decided none of the shape. |
| **Entity resolution beyond string similarity** — `ROADMAP.md` gap 2 | blocked | Owned by `docs/SPRINT_0_3_0.md` step 10, which needs a calibration corpus that does not exist. |
| **Acquiring real Excel-saved workbooks** | **starts now, lands later** | Procurement, not engineering. It blocks the calibration chain and no engineering hour shortens it. Block C above does not wait on it: the corpus is only larger whenever it arrives. |
| **`POST /documents/batch`** — `ROADMAP.md` gap 4 | re-examine, do not build | The queued spine accepts a file and fans out from it, so the case a batch endpoint was for may have moved. Confirmed absent 2026-09-13; that is not the same as confirmed wanted. |
| **Seeding a named search-context preset** — `ROADMAP.md` gap 5 residue | gated on 4.6 | Two of the three presets name namespaces nothing writes. The third needs a filter key `SearchContext.config` does not have, and carrying any preset to an existing appliance needs a migration nobody has written. |
| **Does tree agreement carry information?** | unscheduled, and this is the uncomfortable one | `docs/SPRINT_0_5_0.md` 3.1 calls it the load-bearing empirical claim in that document, four of the eight reprojection trees are gated on it, the second tree it needs now exists, and nothing has been scheduled to take the reading. It is not in this sprint because it is a measurement whose result would reshape a later sprint, and taking it inside a sprint it cannot change is the wrong order. |
| OWL 2 RL, SPARQL via Ontop, FerretDB, a free path language, named graphs as a column, a corpus object | gated | Each gate is written in `ROADMAP.md`'s "Deferred, and what gates each" and none has opened. |

---

## Part 4 — Open questions

Each states what the answer changes and what happens absent an answer.

### 4.1 Which end of an edge does a write gate check

Step 1. An edge has a source and a target, and gating on the source alone closes nothing that
the URL does not already imply.

Three candidates. **Write on both ends** is the narrowest and refuses a legitimate citation of
a document you may read and not modify. **Write on source, read on target** refuses the leak —
attaching an edge to a document you cannot see — while permitting citation. **Write on source
only** is the status quo with a check bolted on and closes nothing.

What the answer changes: whether `DocumentLink` can express "this document of mine cites that
document of yours" across an access boundary, which is a product question about what the graph
is for.

**Absent an answer, write on source and read on target**, because it is the one that makes the
existing READ gate on edges (stage 4 of subtree RBAC) coherent: hiding an edge from a reader
while letting that same reader create it is the inconsistency, not the permission.

### 4.2 Do the analytics verbs filter the graph or filter the result

Step 2. `get_neighbors` filters during the walk. The four analytics verbs compute a metric
over a graph and then return ids.

Filtering the graph before the metric runs is the honest one — a centrality score is a
property of the graph it was computed on, and a score computed over documents the caller
cannot see is a number about somebody else's corpus. It is also more expensive and it changes
every number every existing caller reads. Filtering the returned ids leaks nothing and reports
numbers derived from invisible rows.

What the answer changes: whether these verbs are analytics *about the caller's corpus* or
analytics about the appliance that the caller is shown a slice of. Those are different
products.

**Absent an answer, filter the graph**, and accept that every score moves — with the change
recorded in `CHANGELOG.md` as a behaviour break rather than a fix, because for an ungoverned
single-principal deployment nothing moves at all and for a governed one the old numbers were
never the caller's to read.

### 4.3 Does a `cell` node reach BM25, or only a `record` node

Step 7. `record` nodes carry labelled prose. `cell` nodes carry one value each, and `39d5f20`
made `cell` children of a `record` for rows too long to embed.

Indexing every `cell` puts a corpus of bare values into the inverted index and moves every IDF
in the index it joins — and this codebase has measured ranking changes going wrong twice.

What the answer changes: whether a search for a distinctive single value in a wide row finds
it. That is a real retrieval case for a spreadsheet and it is exactly the case `record`-only
does not reach.

**Absent an answer, `record` nodes only**, because that is the population
`docs/STRESS_CORPUS.md` 4.4 measured and the only one this step has a number for.

### 4.4 What is the MaxSim recall threshold a threshold on

Step 8. The fixture runs 24 queries, so every achievable mean is a multiple of 1/240 and the
threshold sits one or two of those steps above the reading that failed public CI.

Three accounts and they need different work. If the **corpus is too small**, the fix is more
queries and the threshold stays; that is a slower test and a better one. If the **ANN index
is genuinely losing a neighbour** under a condition CI has and this machine does not, the fix
is in `jmfts_core` and the test is doing its job. If **0.90 is the wrong number** for what
this fixture can measure, the fix is a threshold derived from the fixture rather than chosen.

What the answer changes: whether `v0.5.1` can go red on public CI on real grounds, which it
can today.

**Absent an answer, measure before changing anything** — run the fixture enough times on the
CI image to get a distribution rather than a reading, which is the one thing nobody has done.
**A retry is not an answer** and neither is a threshold moved to fit the last failure.

### 4.5 What happens to the sprint plans and the archive

Step 6. 201 citations from shipped files name `SPRINT_0_3_0.md`, `SPRINT_0_4_0.md` or
`SPRINT_0_5_0.md`, and this document adds more.

A shipped sprint plan is a record of what was decided and why, including what was wrong when
written and corrected in place. `docs/SPRINT_0_4_0.md` Block C is the clearest case: it is the
only surviving record of a measurement over 39,100 sheets, and it is attached to a block that
was deferred.

What the answer changes: whether the 695 citations become links or stay as names, and whether
this repository is the working record or a publication of it.

**Absent an answer, publish the specifications and the measurements (steps 4 and 5) and hold
the sprint plans**, because the first two are about the appliance and the third is about how
the work was run. That is the smaller decision and it is reversible in one direction only.

### 4.6 Is a seeded preset worth a migration

`ROADMAP.md` gap 5. A seed in `schema.sql` reaches a freshly built database and no existing
one.

What the answer changes: whether "named search contexts" is a feature with examples or a
mechanism with none. It has been a mechanism with none since it shipped.

**Absent an answer, nothing is seeded**, which is where 0.4.0's step 14 left it deliberately,
and `tests/test_search_context_presets.py` keeps asserting the table is empty so the absence
stays a decision rather than an oversight.

### 4.7 The three gaps that are decisions and not steps

Carried here whole so they are not lost between plans. None is in Part 2.

* **Gap 7 — the dead-entry residue.** `strict_order` holds recall at 0.979 under 500 dead
  entries per query; at that dose 3 queries in 100 were still degraded and one returned
  nothing. `docs/ANN_INDEX_HEALTH.md` Part 2 lays out five options with their costs and picks
  none, because the choice depends on delete frequency and on whether a miss is a nuisance or
  a correctness failure. **Absent an answer, nothing changes**, and that is a real position:
  the appliance already runs the strongest mitigation measured.
* **Gap 10 — the task queue cannot say which resource a rung needs.** A per-rung concurrency
  cap, a resource class on `TaskRow` beside `write_mode`, or a narrower lock in
  `index_document` are three different answers with three different blast radii.
  **Absent an answer, an operator keeps writing `UPDATE task_queue SET priority` by hand**,
  which is what happened twice in one run.
* **Gap 11 — the summary tree stores 13,755 duplicate vectors.** Either a derived node carries
  no embedding and is reached through `source_node_id`, or the duplication stays and
  `search_exclude_usetypes` is documented as required rather than default. **Absent an answer,
  the second**, because it is what ships today and the exclusion being load-bearing is a
  documentation defect rather than a storage one.
