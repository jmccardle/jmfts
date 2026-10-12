# Where the BM25 index write belongs, and what each place costs

`docs/STRESS_CORPUS.md` open question 7.1. Measured 2026-09-10 with
`scripts/measure_bm25_boundary.py`, against a throwaway pgvector on port 5445.

7.1 was written as a binary — does `index:bm25` move to the settling boundary, yes or no.
The repository owner refused the binary and named two more: *"why not build the index
iteratively so it's not empty while we wait for stuck stragglers — or build it
hierarchically, when all children are settled?"* Both are real options, both are priced
below, and the code suggests a fourth that is the iterative one done properly.

**This is a measurement and a design argument. It builds nothing.**

> **Both of its two recommendations shipped, and this document has been overtaken by
> itself. Read 2026-09-15** (`SPRINT_0_6_0.md` Block E step 5; the changes landed at Block B
> step 7 and at `bc897c5`). The sentence above is still true of the document, and is now
> misleading about the tree, so it is marked rather than deleted.
>
> | §5.2 step | Shipped as |
> |---|---|
> | 1 — gate the BM25 leaf scan on `settled` | `jmfts_core/repositories/search.py`: the `doc_scores` CTE now joins `documents` unconditionally and carries `d.settled = 'settled'` INSIDE the scored statement. `tests/test_bm25_settled_gate.py` is the test this document said had to be written. |
> | 2 — replace the `TASK_ROWS` row with a catch-up rule at the settling boundary | `jmfts_core/index_tasks.py`, offered by `IngestRollupPlanner`. The row is gone from `TASK_ROWS`; `jmfts_core/ingest_tasks.py` keeps its text as a comment and says why it is gone rather than widened. `PARAM_OUTSTANDING` shipped exactly as specified. |
>
> **Three things the build did differently, each recorded at the code rather than here.**
>
> * **The atom declares `text@self` and `text@subtree`, not the `text@children` §5.2
>   specifies.** `index_tasks.py` names this document and this section while declining the
>   locus, because `text@children` is not what the handler reads.
> * **The anti-join carries two predicates Option I's SQL does not**: a
>   `bm25_exclude_usetypes` filter, and an explicit exception for the boundary node itself,
>   whose `settled` column is written *after* the planner runs — so a strict `settled =
>   'settled'` would drop a prose file node's own extracted markdown. It is `NOT EXISTS`
>   rather than `LEFT JOIN … IS NULL`.
> * **The stale comment §3.3 reports at `refresh_index` was corrected**, so the defect this
>   document records as found-while-checking no longer exists.
>
> **What that makes stale, section by section:** the Part 3.1 table's "none" in the
> predicate column for the leaf scan; §5.2's two steps as prescriptions; the "today's row
> fires after the structure rung" sentences in §4 and at the `include_in_flight` bullet
> (`include_in_flight` no longer appears in `index_tasks.py` at all); the `EXPECTED_DIVERGENCE`
> claim (the entry was deleted when the move landed, which is what the list is for); and the
> passages Options A and C are argued against, which were rewritten in `index_tasks.py` and
> now argue *for* the rollup shape. **Every number in this document is unaffected** — the
> arithmetic is what decided it, and it decided correctly.
>
> `repositories/search.py` has grown from ~1600 lines to over 2000 since this was measured,
> so its line citations are off by 60–90 throughout. Grep the quoted text.

---

## The answer, before the arithmetic

**Two changes, and they are independent.**

1. **Gate the BM25 leaf scan on `settled`,** as every other retrieval path already is.
   `repositories/search.py:698`'s `doc_scores` CTE reads `search_term_postings` with no
   lifecycle predicate at all, while vector search (`:404`), MaxSim (`:1028`) and BM25's own
   *container* pass (`:836`) all carry `settled = 'settled'`. That single asymmetry is why
   "can a caller tell a partial index from a complete one?" has no good answer for **any**
   option below. With the gate it has the same answer for all of them, and it is the
   answer the appliance already gives everywhere else.
2. **Replace the file-node `TASK_ROWS` row with a catch-up rule at every settling
   boundary** — Option I below. It fires wherever the walk goes, and the work it does is
   defined by a set difference (`documents` under a covering root, settled, with `content`,
   with no `search_index_entries` row) rather than by a subtree walk. One permanently stuck
   node then withholds exactly itself, membership is read from the tree for every format
   including ones nobody has written yet, and the happy path costs the same **one** index
   transaction per file that it costs today.

**What it is not.** It is not Option A (move the row to the file node's settling boundary
unchanged), and the reason is measured rather than argued: `STRESS_CORPUS.md` 4.4d found
**three workbooks whose ~2,240 finished `record` nodes were withheld from indexing by
about 70 unsettled siblings**. Option A makes that the schedule — for every format, not
only workbooks — so a single permanently failed `embed` would remove a whole file from
full-text search. Making the one retrieval method that needs no model depend on the model
succeeding is the wrong way round.

---

## Part 0 — What is actually broken, restated so the options can be compared

`read jmfts_core/ingest_tasks.py:1174`: `index:bm25` is a `TASK_ROWS` row on the file node,
`after_any` the three structure rungs, `requires=(HAS_TEXT_LAYER,)`.

`read jmfts_core/index_tasks.py:88`: its atom declares
`consumes=(text@self, text@subtree)`.

`read docs/SPRINT_JOBS.md:222` (2.2), the locus table:

| Locus consumed | Enforced by |
|---|---|
| `@self` | the topological sort, within one node |
| `@children` | the settling walk |
| `@subtree` | the settling walk |
| `@ancestor` | nothing |

**The declaration and the schedule disagree, and the declaration is the one that is right.**
`text@subtree` says the settling walk enforces this dependency; the row enforces it with
`after_any` instead. `tests/test_atom_declarations.py:156` records the disagreement as
`EXPECTED_DIVERGENCE` and predicts its resolution in those words: *"INGEST_SPEC.md 11.5 in
fact describes this task AS a rollup task, so the day Phase 3 moves it under the walk, the
spec and the implementation converge rather than diverge."*

For prose the substitution happens to be sound: `structure:declared`,
`structure:inferred` and `structure:conversation` write their own chunks, so a completed
rung *is* a complete subtree. For a workbook it is not: `structure:sheets` writes only
`sheet` containers and the leaves come from `profile:sheet` and `extract:sheet`, which are
scoped to those children one rung further down (`read jmfts_core/ingest_tasks.py:1089`,
`:1104`). `after`/`after_any` cannot reach across a scope — `_check_task_rows` rule 2,
`read jmfts_core/ingest_tasks.py:1296`, *"`after` orders two tasks on ONE node, and these
are on different ones"* — and a second row under the same name is refused by rule 1,
`read :1283`.

So the question is not "which rung does the row wait for". **The question is which of the
four loci this task actually consumes at**, and each answer is a different option.

### The two hazards this repository has already been bitten by

**4.5 — the index row lock.** `read jmfts_core/repositories/search.py:1508`:
`index_document` takes `SELECT ... FOR UPDATE` on the `SearchIndex` row and the worker holds
it to commit (`read jmfts_core/ingest_worker.py:211` — one session context wraps the whole
handler, so **one task is one transaction and therefore one lock hold**, however many
documents it indexes). Splitting an index build into more tasks multiplies the number of
times that lock is taken. Part 2 measures what that costs.

**4.6's neighbourhood — incremental maintenance.** The per-document `DELETE ... WHERE
doc_freq <= 0` that scanned the whole term table is fixed (`read
jmfts_core/repositories/search.py:1637`, now guarded on `dropped_terms` and naming the
terms). Two things nearby are worth stating before anything multiplies them:

* `search_term_postings` **does** have `(index_id, document_id)`, from migration 007
  (`read jmfts_core/sql/schema.sql:526`). `refresh_index`'s comment at
  `repositories/search.py:1815` says it does not; that comment is stale and the per-document
  `old_terms` read is an index lookup, not a scan.
* `search_term_stats` is still upserted **one statement per term**
  (`read jmfts_core/repositories/search.py:1603`). Part 2 shows this, not the lock, is what
  a document's indexing cost is made of — which matters because it is the term that is
  *identical under every option*.

---

## Part 1 — The option space

A baseline, four live candidates, one priced in §2.6, and two the code refuses. Each is
named by the locus it makes `index:bm25` consume at, because that is what decides its trigger.

### Option 0 — leave it, repair by script

`scripts/reindex_workbooks.py`. No pipeline change; an already-ingested corpus is made
searchable by hand and every new workbook arrives unindexed. It is the baseline, and
`STRESS_CORPUS.md` 4.4d is the record of it working: 18 of 21 workbooks, 2,323 nodes, into
`xlsx` and `universal`. The three it refused are the shape of the problem.

### Option A — `text@subtree` at the file node (the move 7.1 asked about)

Delete the `TASK_ROWS` row; `IngestRollupPlanner` offers `index:bm25` when a node with
`usetype == 'file'` reaches its settling boundary, terminated by 6.1's attempt diff like
every other rollup. `index_tasks.py`'s two objections to being a rollup task are both
answered by that `usetype` guard, as its own docstring concedes (`read
jmfts_core/index_tasks.py:29`): one task per file, and a file node has children so the
planner's leaf case never applies.

### Option B — `text@self` at every leaf (the per-leaf iterative build)

`index:bm25` becomes a child-scoped `TASK_ROWS` row with **exactly `embed`'s shape**
(`read jmfts_core/ingest_tasks.py:1243`): `scope=children_of(structure:declared,
structure:inferred, structure:conversation, profile:sheet, extract:sheet,
usetypes=(chunk, record, profile, cell))`, `write_mode=WRITE_SELF`, no condition beyond the
scope. One task per leaf, plus a root-scoped row for the file node's own `content`.

This is the owner's "build it iteratively" read literally, and its strongest argument is
that the appliance already runs a task of exactly this shape over exactly this node set.

### Option C — `text@children` at every container (the hierarchical build)

`IngestRollupPlanner` offers `index:bm25` at **every** node's settling boundary, indexing
that node's subtree. `index_tasks.py:36` raises the obvious objection — *"the same walk that
reaches the file node also reaches each `section` above the chunks, and each of those would
offer its own subtree index task — the same documents, indexed again"* — and it has an
answer the module did not consider: `search_index_entries` is already the per-document
membership row, and `index_document` already reads it (`is_new`, `read
repositories/search.py:1536`). Index the descendants that have no entry, and each document
is written exactly once whatever order the boundaries arrive in.

This is the owner's "when all children are settled", read literally.

### Option I — the catch-up rule: `text@children`, over a set difference

Option C with one change, and the change is what makes it the right answer to the
*iterative* question rather than to the hierarchical one. The task's work is not "my
subtree"; it is

```sql
-- every document a covering index should hold and does not yet
SELECT d.id
FROM documents d
LEFT JOIN search_index_entries e ON e.index_id = :index_id AND e.document_id = d.id
WHERE d.path @> jsonb_build_array(:node_id)
  AND d.settled = 'settled'
  AND d.content IS NOT NULL
  AND e.document_id IS NULL
```

offered at every settling boundary. Three consequences follow and none of them follows from
C alone:

* **A stuck straggler withholds itself and nothing else.** Its settled siblings are in the
  set difference and get written by the next boundary that fires anywhere above them. There
  is no subtree to be "not settled yet"; there is a set of nodes that are each individually
  settled or not.
* **It catches up.** A boundary that fires when nothing has changed does one anti-join and
  exits. A boundary that fires after four more sheets settled writes those four sheets.
* **It is format-blind.** The set difference reads `documents.path` and
  `search_index_members`. It contains no rung name, no usetype, and no format, so
  "a BM25 index per folder" becomes a property of the tree rather than a property of the
  table of rungs — which is exactly what 7.1 says the answer changes.

### Option G — `refresh_index`, debounced at the index root — priced and rejected in §2.6

Do not write incrementally at all: when anything under an index's roots settles, schedule a
full `refresh_index` of that index, debounced. It is the only option that gets the batched
write path (`ran`: ~10× cheaper per document, §2.6), and it is the only one whose cost does
not fall with the size of the change. §2.6 puts a single rebuild of `universal` at about
100 s. It is listed because the batched path is real and worth knowing about — just not as a
trigger.

### Option D — widen `after_any` across scopes — REFUSED by the code

Name `extract:sheet` in the file-node row's `after_any`. `_check_task_rows` rule 2 raises at
import: *"`after` orders two tasks on ONE node, and these are on different ones."* 7.1
already records this; it is listed so the table is complete.

### Option E — a second, sheet-scoped row under another name — REFUSED on generality

`index:bm25:sheet`, scoped to `children_of(structure:sheets, usetypes=(sheet,))`,
`after=(extract:sheet,)`. It passes rule 1 (a different name) and it would work — for
spreadsheets. It fails 7.1's actual question, which is about *any* format whose leaves land
below its structure rung, and it is the drift `Scope`'s own docstring refuses in those words
(`read jmfts_core/ingest_tasks.py:792`): *"thirteen near-identical rows would let the copies
drift apart"*.

---

## Part 2 — What each shape costs, measured

### 2.0 The corpus, and what it is not

`scripts/measure_bm25_boundary.py` builds **6 workbooks × 2 sheets × 75 records = 900
content-bearing nodes**, shaped exactly as `STRESS_CORPUS.md` 4.4b measured one: `file` and
`sheet` carry no `content`, every `record` is one row rendered `Key: value.`, and the
vocabulary is eight common terms plus a 20,000-token tail so the term statistics behave like
a corpus's rather than like a phrasebook's. `ran`: 900 documents → **49,709 postings, 17,888
distinct terms, `avg_doc_length` 58.0**, identical under every shape, which is the check that
the three shapes produce the *same index* and differ only in how it was written.

**It is a twentieth of the reference corpus's index** (`STRESS_CORPUS.md` 4.4d: `universal`
holds 29,997 documents, 2,051,960 postings, 61,660 terms). Every absolute figure below is
therefore a lower bound on the per-document work and an *upper* bound on the overhead
*fraction* — §2.4 turns that around.

No node gets a vector; nothing on this path imports torch. Machine: Postgres 16 + pgvector
in a container on the same host, the appliance's own `SearchRepository` on the other end of
a loopback TCP connection.

**The machine was busy, and the absolute numbers carry that.** `ran`, `uptime` during the
run: **load average 8.39 on 8 cores**, because six sibling agents were working in parallel
worktrees. Identical work — the `whole` shape, 900 documents, one writer — was measured at
25.8 s, 26.1 s, 29.4 s and 34.0 s across the session. **Every absolute figure below is
therefore soft by up to ~30%, and every comparison between shapes is not**, because the
shapes are interleaved *within* each repeat: a load spike moves all three together. Where a
direction holds in 3 of 3 repeats it is reported as a finding; where a gap is inside the
within-shape spread it is reported as not resolvable, and §2.2 does exactly that to one of
them.

### 2.1 The ladder

`ran`, three repeats, interleaved by repeat rather than grouped by shape, with
`search_indexes` and `search_term_postings` truncated before every run. (The first draft did
neither, and reported the shapes in run order, slowest last. That was the ordering and not
the shape: every earlier shape's postings were still in the table, and each insert was
maintaining their index too. The correction is in the script's `_fresh_index` docstring.)

| shape | transactions | serial, median | fleet (6 threads), median |
|---|---:|---:|---:|
| **whole** — one task per file | 6 | **26.075 s** | 31.045 s |
| **per-sheet** — one task per container | 12 | 28.577 s | 29.635 s |
| **per-leaf** — one task per record | 900 | 31.566 s | **24.465 s** |

Raw, because the spread matters more than the medians here:

```
  rep0 whole      serial  25.796 s /     6 tx   fleet  31.045 s
  rep0 per-sheet  serial  28.577 s /    12 tx   fleet  29.211 s
  rep0 per-leaf   serial  31.566 s /   900 tx   fleet  24.428 s
  rep1 whole      serial  29.433 s /     6 tx   fleet  34.375 s
  rep1 per-sheet  serial  30.272 s /    12 tx   fleet  30.124 s
  rep1 per-leaf   serial  33.235 s /   900 tx   fleet  28.149 s
  rep2 whole      serial  26.075 s /     6 tx   fleet  28.518 s
  rep2 per-sheet  serial  27.121 s /    12 tx   fleet  29.635 s
  rep2 per-leaf   serial  30.451 s /   900 tx   fleet  24.465 s
```

### 2.2 The single-writer reading: the split costs something, and it is under the noise floor

`per-leaf` was slower than `whole` in **all three repeats of this run**, by 5.5 s over 900
documents — +21%, or +6.1 ms per document. `ran`: an uncontended `BEGIN` + `SELECT … FOR
UPDATE` + `COMMIT` against this database was **2.83 ms**.

**That figure did not reproduce, and §2.4 is where it fell over.** A second run on a quieter
machine measured the same uncontended round trip at **0.91 ms** and put `per-leaf`'s serial
arm *faster* than `whole`'s in both its repeats. The two runs disagree about the sign.

So the honest statement is a bound rather than a number:

* **A transaction boundary costs 0.9–2.8 ms**, measured directly, twice, on a busy machine
  and a quiet one. A per-leaf split adds one per document.
* **A document's indexing costs 20–37 ms** on this corpus, measured across both runs.
* **So splitting to one task per document costs between 2% and 14% of the write on a single
  writer, and cannot be resolved more finely than that here.** It is certainly positive —
  a transaction is not free — and it is certainly small.

`per-sheet`'s +2.5 s in this run is not resolvable at all and should not be reported as a
cost: six extra transactions cannot cost 417 ms each when one costs 2.83 ms, and §2.4's run
put `per-sheet` *faster* than `whole` in both repeats. `inferred`: the per-sheet and whole
shapes are indistinguishable on a single writer, which is exactly what a per-transaction
cost model predicts — 6 extra transactions is under 20 ms, invisible under 3 s of spread.

**4.5's lock is not what a document's indexing costs.** 26.075 s / 900 = **29 ms per
document** on a single writer with no contention at all, for 55 postings and 55 distinct
terms each. `read jmfts_core/repositories/search.py:1603`: `search_term_stats` is upserted
one `INSERT … ON CONFLICT` statement per term — so a document costs roughly one round trip
per distinct term it contains, plus five. That term is **identical under every option in
Part 1** and it is 83% of the single-writer cost. Whatever this decision is about, it is not
about that.

### 2.3 The fleet reading: fine granularity WINS under contention

Under six concurrent writers against **one** index name — `STRESS_CORPUS.md` 4.5's live
case, where five of six workers were caught blocked in `pg_locks` — the order **inverts**:
`per-leaf` is fastest in all three repeats and `whole` is slowest in two of three.
`whole` at 6 writers (31.0 s) is 19% *slower* than the same work on one writer (26.1 s);
`per-leaf` at 6 writers (24.5 s) is 22% *faster* than on one.

`inferred`, from the lock's own semantics: six long holds serialise completely and the other
five writers idle through each one, while 900 short holds let each writer's un-locked work —
fetching the row, tokenising, building the 55 posting objects — overlap another writer's
held lock. **Coarse granularity does not avoid 4.5's serialisation; it maximises it.** That
is the opposite of what the brief's first hazard predicted, and it is the single most
decision-relevant finding in Part 2.

### 2.4 The same arm in six processes, because threads were the obvious objection

The §2.1 fleet arm runs six *threads* in one interpreter, and a real fleet is six
`jmfts-worker` *processes*. A blocked thread has released the GIL, so a thread pool could be
overstating how much of one writer's Python work overlaps another's. `ran`, the same ladder
with `--processes` (`ProcessPoolExecutor`, each child disposing the inherited connection
pool with `close=False` — the first attempt died with `server closed the connection
unexpectedly`, which is `fork` handing two processes one libpq socket):

```
  rep0 whole      serial  34.028 s /     6 tx   fleet  29.532 s
  rep0 per-sheet  serial  30.998 s /    12 tx   fleet  28.786 s
  rep0 per-leaf   serial  33.320 s /   900 tx   fleet  23.026 s
  rep1 whole      serial  29.824 s /     6 tx   fleet  27.823 s
  rep1 per-sheet  serial  28.694 s /    12 tx   fleet  25.037 s
  rep1 per-leaf   serial  18.374 s /   900 tx   fleet  17.258 s

uncontended BEGIN + SELECT FOR UPDATE + COMMIT: median 0.91 ms over 50
```

**The fleet direction reproduces: `per-leaf` is the fastest shape in both repeats, and in
processes rather than threads. Five measured repeats across two concurrency models, five
agreements.** That is the finding §2.3 rests on and it is not a GIL artefact.

**The serial direction does not reproduce, and the last line says why.** `0.91 ms` against
§2.1's `2.83 ms` for the identical round trip: the machine got materially quieter between
the two sessions, and `rep1 per-leaf`'s 18.374 s serial arm is where it happened mid-run.
Within this run `per-leaf` serial beats `whole` serial in both repeats, which contradicts
§2.1's 3-of-3 the other way. **Two runs disagreeing about the sign of a 5 s difference on a
machine whose own baseline moved 3× is a measurement saying it cannot resolve that
difference, and §2.2 reports it as a bound instead of a number.**

### 2.5 At reference-corpus scale the overhead fraction collapses

`inferred`, and it is the arithmetic that matters most to the decision. A transaction
boundary costs **0.9–2.8 ms** and that is **fixed**; the per-document work is **not** — it
grows with the number of distinct terms a document holds and with the size of the
`search_term_stats` index its upserts maintain.

| | this corpus | reference corpus (`STRESS_CORPUS.md` 2.4, 4.4d) |
|---|---:|---:|
| documents in the index | 900 | 29,997 |
| postings | 49,709 | 2,051,960 |
| per-document indexing cost | 20–37 ms (measured, uncontended) | **161 ms** (4468 s / 27,674, measured under a 6-worker fleet) |
| per-transaction overhead | 0.9–2.8 ms measured, 6.1 ms as an upper bound from §2.2's run | the same, assumed unchanged |
| **splitting to one task per document** | 2–14% (§2.2's bound) | **0.6–3.8%** |

The 161 ms is per DOCUMENT, not per (document, index) write: 4.4d shows a node joining both
its collection index and `universal`, so a task writes each document roughly twice and the
per-write cost is nearer 80 ms. The ratio is unaffected, and that is the point of stating it
— one task per leaf adds **one** transaction per leaf however many indexes cover it, exactly
as one task per file does, because `run_index_bm25` loops the indexes inside the
transaction (`read jmfts_core/index_tasks.py:126`).

So the cost that looked like 21% on a toy index — under a bound that a second run could not
even establish the sign of — is **under 4% at worst** on the corpus the decision is actually
about, and that is before §2.3's inversion, which points the other way again.

**`index:bm25` is the second most expensive rung in the whole ingest** — `read
docs/STRESS_CORPUS.md:186`: 829 tasks, 4468 s, more than 29,827 embeddings cost — so 4% of
it is at most about 170 s on a 3-hour ingest. It is a real number and it is not a blocking
one.

### 2.6 Two numbers taken on the side, and both change something

**The batched rebuild is about 10× cheaper per document than the incremental path.** `ran`,
`refresh_index` over the same 900 documents and the same roots: **2.941 s** against the
26–34 s the same write costs through `index_document`, producing the identical 49,709
postings. Per document: **3.3 ms against 29–37 ms.** `read
jmfts_core/repositories/search.py:1877`: the rebuild batches entries, postings and
`doc_freq` with `execute_values`; the incremental path issues one statement per term
(`:1603`).

Two things follow. First, it prices **Option G** — "run `refresh_index` at a boundary
instead of indexing incrementally" — which is O(the whole index) per firing and is therefore
only ever right for a small, rarely-written index; on `universal` at 30,000 documents a
single rebuild would be about 100 s, and no debounce makes that per-upload behaviour
acceptable. Second, and more usefully, **the same `execute_values` batching applied to
`index_document`'s `doc_freq` upsert is worth about 8× on the second most expensive rung in
the ingest, and it is orthogonal to every option in this document.** That is a patch with one
right answer, not a decision, and it belongs in a sprint step rather than here.

**Option I's anti-join is ~1.7 ms and does not care whether it has work.** `ran`,
`EXPLAIN (ANALYZE)` × 5 over one workbook's subtree:

| state | median execution | rows | plan |
|---|---:|---:|---|
| everything outstanding | **1.681 ms** | 150 | Hash Join |
| nothing to do | **1.765 ms** | 0 | Nested Loop |

That is the number Option I stands on: the rule is offered at *every* settling boundary, so
almost every firing finds nothing, and a firing that finds nothing costs one anti-join. On
the reference corpus's roughly 13,770 container boundaries that is of the order of **25 s**
against a 4468 s rung. `inferred`, and the caveat is stated: this `documents` table holds
5,406 rows against the reference corpus's 57,492, and the predicate is a GIN `path @>`
lookup plus a PK anti-join, both of which grow sublinearly — but "sublinearly" is a
prediction, and §5.3 says what would falsify it.

---

## Part 3 — The partial-index question, and why it has one answer for every option

The brief asks every option to say how a caller tells a partial index from a complete one.
**None of them can today, and the reason is not the schedule.**

### 3.1 BM25 is the only retrieval path with no lifecycle gate

`read jmfts_core/repositories/search.py`:

| path | line | predicate |
|---|---|---|
| vector search | `:404` | `Document.settled == SETTLED_SETTLED` |
| MaxSim | `:1028` | `d.settled = '<settled>'` |
| BM25, **container** pass | `:836` | `a.settled = 'settled'` |
| BM25, **leaf** scan (`doc_scores`) | `:698`–`:714` | **none** |

The vector comment states the rule as a rule: *"`settled = 'settled'` is BOTH the retrieval
rule and the predicate that makes `idx_documents_embed` reachable."* The MaxSim comment
states the reason: *"MaxSim is a retrieval path and must not surface a node whose structure
is about to be rebuilt."* The BM25 leaf scan reads `search_term_postings` and joins
`search_index_entries`, neither of which knows anything about a document's lifecycle.

It is not theoretical. `ran`: index all 900 nodes, then put 450 of them back to
`in_flight`, then query.

```
half the corpus put back in flight (450 of 900), index fully built:
  bm25_search returned 20 results, 12 of them in flight
```

**Twelve of twenty results were documents the appliance itself says are not finished.** And
this is the *shipped* schedule, not a proposed one: `read jmfts_core/index_tasks.py:120` —
`run_index_bm25` walks the subtree with `include_in_flight=True`, deliberately, and today's
row fires after the structure rung while every `embed` below is still running. So a prose
file's postings are answerable for the whole duration of its embedding.

### 3.2 The gate is not a cost

`ran`, the `doc_scores` shape with and without `JOIN documents d ON d.id = tp.document_id`
and `AND d.settled = 'settled'`, `EXPLAIN (ANALYZE)` × 3 on the half-in-flight corpus:

| | median execution | rows returned |
|---|---:|---:|
| shipped | 16.817 ms | 20 |
| + `settled` gate | **10.801 ms** | 20 |

The gated form is **faster**, because the join prunes half the postings before the
`GROUP BY` rather than after it, and the page still fills — the gate changes *which*
documents answer, not how many. `inferred`: on a corpus where the in-flight fraction is
small rather than a half, the two converge and the gate is a wash. Either way it is not a
cost to weigh against anything.

### 3.3 What the gate buys every option

With it, the sentence a caller needs becomes true **by construction, under all four
schedules**: *the index answers with exactly the settled documents that have been written to
it.* Partiality then has two components and they are different in kind:

* **Nodes that are not settled.** Invisible, deliberately, exactly as they are to vector and
  MaxSim. This is not partiality a caller has to be told about; it is the appliance's
  existing contract. `Document.settled` is the mechanism, and it is per NODE — which is the
  point §5.1 turns on.
* **Settled nodes not yet written to the index.** This is real lag and it is *computable*:
  it is the row count of Option I's anti-join, per index. A `SearchIndex` needs no new
  column and no "is this complete" flag — a flag would have to be maintained, and a
  maintained flag is the `AppliedFilters.truncated` mistake in reverse.

`read jmfts-client/jmfts_client/contracts/search.py:211`: `AppliedFilters.truncated` is
documented today as *"Null means none was reported: BM25 and full-text run no ANN scan at
all, so a short page from them IS the whole matching set."* **That sentence is false for a
BM25 index while its corpus is being written**, under today's schedule as much as under any
proposed one, and the honest repair is the same shape as the field itself: report the lag
beside the filters, as a number the server computed, not as a flag it stored.

### 3.4 The hazard 4.6 lives in is not multiplied by any of these options

`STRESS_CORPUS.md` 4.6's class of defect is incremental index maintenance — a re-index that
double-counts, or a decrement that scans. `read jmfts_core/repositories/search.py:1563`:
`index_document` diffs `old_terms` against `new_terms` and moves `doc_freq` only for the
symmetric difference, and the zero-row `DELETE` is now guarded on `dropped_terms` and names
the terms. On a **first** write `old_terms` is empty, so `dropped_terms` is empty and neither
the decrement loop nor the delete executes at all — which is every write under every option
in Part 1, because each document is written exactly once (the `search_index_entries` row is
what makes "exactly once" checkable, and `is_new` already reads it).

One stale comment found while checking this, recorded because it would mislead the next
reader: `repositories/search.py:1815` argues the bulk path is needed because the incremental
one scans *"a table with no `(index_id, document_id)` index"*. `read
jmfts_core/sql/schema.sql:526`: `idx_term_postings_doc` is exactly that index and has been
since migration 007. The bulk path is still ~10× faster (§2.6) and the reason is the
per-term round trips, not a missing index.

---

## Part 4 — The table

Every row assumes §3's `settled` gate is in place, because without it the "partial
distinguishable" column reads *no* for all six and the comparison says nothing.

| | **0** script | **A** `@subtree`, file boundary | **B** `@self`, per leaf | **C** `@children`, per container | **I** catch-up at every boundary | **G** `refresh_index`, debounced |
|---|---|---|---|---|---|---|
| **What triggers the write** | an operator runs a script | the file node settles | the leaf's own task batch, ordered by the scope | each container settles | any settling boundary; the work is a set difference | a timer or a watermark on the index |
| **Tasks per 1,122-node workbook** | 0 | 1 | ~1,100 | 3 | 1 in the happy path, plus cheap no-ops | 1 per debounce window |
| **Tasks, reference corpus** | 0 | 829 | ~28,000 | ~13,800 `inferred` from 13,770 `summarize` runs | 829 writes + ~14,000 no-ops at 1.7 ms | 1 per window, each O(whole index) |
| **Per-document DB work** | 20–37 ms | same | same | same | same | **3.3 ms** batched |
| **Extra cost vs today** | none | **none** | one transaction per leaf: **0.6–3.8%** at reference scale (§2.5) | not resolvable at this size | ~25 s of anti-joins per corpus | ~100 s per firing on `universal` |
| **Under a 6-writer fleet** | n/a | slowest measured shape (§2.3) | fastest measured shape (§2.3, §2.4) | middle | same as A in the happy path | one writer by construction |
| **One permanently stuck node** | the script refuses the subtree — **measured: 3 workbooks, ~2,240 finished rows withheld by ~70** | **the whole file is withheld, forever** | that node alone | the smallest container above it | **that node alone** | the whole index refuses, or indexes around it silently |
| **Partial distinguishable** | yes — nothing was indexed | yes | yes, and the lag is the anti-join's row count | yes, same | yes, same | yes |
| **"a BM25 index per folder" for any format** | no — the script knows `sheet` | **yes** | only if the scope lists every leaf-producing rung, forever | yes | **yes, and with no rung name in it at all** | yes |
| **Still in `EXPLAIN` (11.2)** | n/a | **no** | **yes** — a row like `embed` | no | no | no |
| **Latency: postings exist when?** | never, until run | after every `embed` below finishes | as each leaf lands | as each container lands | as each boundary passes | up to one window late |

Two columns need their reasoning stated rather than asserted.

**`EXPLAIN`.** `read jmfts_core/ingest_tasks.py:2390`: `explain_plan` reads `TASK_ROWS` and
nothing else, so the three rollup task types do not appear in it — `read
jmfts_core/ingest_tasks.py:224` states that as a decision and gives the reason (*"A row in
that table would be evaluated once, from `(format, patterns, options)`, and would have to
name a scope for a node that does not exist yet"*). Any option that moves `index:bm25` under
the walk therefore removes it from `EXPLAIN`'s answer. That is a real loss and it has a known
end date: `read docs/SPRINT_JOBS.md:1650` — *"After 7, `IngestRollupPlanner` is rules
consuming `@children` and the rollup's special case is gone."*

**Option A's stuck-node cell is measured, not predicted.** `read
jmfts_core/settling.py:200`: a node whose task failed permanently is `SETTLED_FAILED` and
never settles; `read :214`: its parent is blocked on `unsettled_children`. So the file node's
settling boundary is never reached and the rollup never fires.
`read docs/STRESS_CORPUS.md:532` is the same mechanism observed on the reference corpus —
three workbooks refused, *"roughly 2,240 record nodes whose text is final are withheld from
the index by about 70 nodes whose vectors are missing."* Under Option A that is not a
script's conservatism; it is the pipeline.

And it is worse than the workbook case suggests, because today the row fires **before** the
embeds: `read jmfts_core/index_tasks.py:120` — `include_in_flight=True`, *"this task runs
INSIDE the ingestion that built the tree"* — and `index:bm25` is root-scoped while `embed`
is leaf-scoped, so nothing orders them. **Today a prose file reaches BM25 while its
embeddings are still running. Under Option A it would reach BM25 only if all of them
succeed.** BM25 is the retrieval method that needs no model, no GPU and no vector; making it
conditional on the model succeeding inverts the appliance's own dependency order.

---

## Part 5 — Recommendation, its cost, and what would change it

### 5.1 The argument, in one paragraph

`STRESS_CORPUS.md` 4.4d states the principle the script refuses on: *"`settled` is the
appliance's own statement that a subtree is complete, and a script that indexes past it is
asserting something the appliance denies."* That is right about a script, which indexes a
**subtree**, and it is the sentence that makes Option A look principled. **But the unit of
`settled` is a node, not a subtree** (`read jmfts_core/models/document.py:189` — *"a node is
settled when its own work is done AND every child is settled"*; the column is per row), and
the unit of a BM25 write is also a node — one `search_index_entries` row, one posting set.
A `record` whose text is final and whose vectors landed **is settled**, and its sibling
failing does not make it incomplete. Coupling its posting write to a subtree-wide condition
is not caution; it is a category error, and 4.4d measured its price at 2,240 rows.

So the write should be per node, and the only real question left is **who notices that a
node needs writing**. Option B answers "a queue row created for it in advance"; Option I
answers "a query, run whenever the walk passes". The query is 1.7 ms and finds nothing
almost every time it runs, and it needs no list of leaf-producing rungs to stay correct.

### 5.2 Recommended: §3's gate, then Option I

**Step 1 — gate the BM25 leaf scan on `settled`.** One `JOIN documents d ON d.id =
tp.document_id` and one `AND d.settled = 'settled'` in `doc_scores`
(`repositories/search.py:698`), making the join unconditional where it is conditional today.
Measured **faster**, not slower, on a corpus half in flight (§3.2). It is a patch with one
right answer, it is independent of everything else here, and it is the entry condition for
every other option's honesty. A failing test exists to be written for it: index a document,
set it `in_flight`, assert BM25 does not return it — vector and MaxSim already pass that
test and BM25 does not.

**Step 2 — replace the `TASK_ROWS` row with a catch-up rule at the settling boundary.**
`IngestRollupPlanner` offers `index:bm25` at any boundary where the anti-join in §2.6 finds
rows; the task writes exactly those documents; 6.1's attempt diff needs the outstanding
count in its params so a boundary that finds new work is a new fingerprint. The atom's
`consumes` becomes `text@children` — which is what a catch-up over settled descendants
actually reads, and which `SPRINT_JOBS.md` 2.2's table already routes to the settling walk.
`tests/test_atom_declarations.py`'s `EXPECTED_DIVERGENCE` entry for `index:bm25` is deleted
on the same day, which is the test saying the spec and the implementation converged.

### 5.3 The cost, stated plainly

| | cost |
|---|---|
| **Runtime, happy path** | unchanged. One index-writing transaction per file, one lock hold, the same 29 ms a document. |
| **Runtime, everything else** | ~14,000 anti-joins at 1.7 ms per reference-corpus ingest ≈ **25 s against a 4468 s rung**. |
| **`EXPLAIN`** | `index:bm25` leaves the plan until `SPRINT_JOBS.md` Phase 7. This is the largest cost and it is a regression in an answer the appliance currently gives. |
| **Shipped code touched** | `repositories/search.py` (one join), `index_tasks.py` (the handler body becomes the anti-join), `ingest_tasks.py` (delete one row), `rollup_tasks.py` (one planner rung), `atoms.py` (one locus). Five files, all inside the `black`/`ruff` gate. |
| **Not solved** | 11.5's two named gaps are untouched: an explicit index name on the upload, and reparenting changing membership. Option I makes the second *cheaper* to solve later — a reparent invalidates entries and the next boundary catches up — but does not solve it. |

### 5.4 What I would have to be wrong about

**For Option B to win: the anti-join is not cheap at scale.** §2.6's 1.7 ms is on a
`documents` table of 5,406 rows. If `path @> jsonb_build_array(:node)` plus the
`search_index_entries` anti-join costs, say, 40 ms at 57,492 documents and 30,000 entries,
then 14,000 no-op firings cost 560 s rather than 25 s and Option I's central claim collapses.
Option B then wins outright: a queue row per leaf needs no query to know its work, and §2.4
already prices it at under 4% of the rung. **This is the single measurement I would take
next**, and it is one `EXPLAIN (ANALYZE)` against a corpus-sized `documents` table — an
hour's work on the reference appliance, not a day's.

**For Option A to win: permanent task failure has to be rare enough not to matter.** It is
not, on the evidence in front of me — 66 permanent `embed` failures over one corpus, and 4.11
fixed *that* cause without closing the class. If a future sprint made a permanently failed
node impossible below a file node, A becomes the simplest option in the table and the only
one that keeps the code in one place.

**For Option C to win: the fan-out has to be large.** C's transaction count is the container
count, which is excellent for workbooks (a sheet holds hundreds of records) and poor for
prose (`read docs/STRESS_CORPUS.md:185`: 13,770 `summarize` runs against 27,674 indexed
documents — roughly two documents per container). If the prose trees were wider, C would be
Option I without the query. As they are, it pays nearly B's transaction count for A's blast
radius.

**Three things I did not measure, and what each would settle.**

1. **The queue overhead of a task** — enqueue, claim, heartbeat thread, complete, settle
   walk. Nothing in `STRESS_CORPUS.md` 2.4 separates it from handler time. It is the term
   that decides how bad Option B's 28,000 tasks really are, and §2.5's 0.6-3.8% counts only the
   database side. Measured by running a real `IngestWorker` over a synthetic tree and
   dividing; half a day.
2. **A six-*process* fleet, on more than one host.** §2.4 removes the GIL from the picture
   on one machine; it does not remove the shared connection pool or the single loopback.
3. **The reference corpus itself.** Every absolute number here is from a 900-document index.
   The ratios should carry — they are properties of round trips and transactions rather than
   of the data — but §2.5's most decision-relevant line is `inferred`, not `ran`.

### 5.5 What this does not change

`scripts/reindex_workbooks.py` stays. It repairs a corpus ingested before any of this
lands, and 4.4c's argument for it is untouched: the defect is one of ORDER and an ingested
workbook has no order left. Under Option I it becomes redundant for new uploads and remains
the only way to fix an old one without re-ingesting.
