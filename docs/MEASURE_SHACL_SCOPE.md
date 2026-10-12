# How large a scope a SHACL validation run can hold

`SPRINT_0_5_0.md` Block A step 5, and open question 6.3. Measured 2026-09-06 with
`scripts/measure_shacl_scope.py`, which drives the real `validate:shape` handler.

**A measurement, not a benchmark.** It decides two things outside this sprint:

1. the default of `shacl_max_scope_documents` — 6.3's "bound the operator can move, not a
   constant nobody chose";
2. whether incremental validation is worth building, which 6.3 makes conditional on a full
   run being measured *too slow*.

**The short answer.** A full run is comfortable up to **138,000 documents** at five triples
each, and it is not `pyshacl` that bounds it — **94–98% of the peak resident set is already
allocated before `pyshacl` is called**. The wall is `build_data_graph`, which holds one
SQLAlchemy `Triple` object and one set of `rdflib` terms per triple at the same time. 6.3's
premise ("`pyshacl` has no streaming mode, so a scope large enough to matter is a graph
large enough not to fit in memory") is true and is not the leading term.

---

## Part 0 — The honest answer about the corpus

**Nothing in this tree holds a bound scope large enough to answer the question, and the
largest one that exists is two documents.** Not a turn of phrase:

| What | Size | Why it is not the answer |
|---|---|---|
| The largest *bound* scope anywhere in the tree | **2 documents** | `tests/test_validate_shape.py`'s fixture: A and B, `usetype='vendor'`, three triples between them. It is the largest `ShapeBinding` in the suite. |
| The largest scope in `tests/test_shacl_graph.py` | 4 documents, 8 triples | Built to be awkward at the boundary, not to be large. |
| A live appliance database | **none** | Postgres refuses on `localhost:5432`; `docker ps` is empty. There is no running JMFTS on this machine to measure. |
| `datasets/corpus-fuse` | 10,702 files, 1.1 GB | Raw bytes on disk: not ingested, no `documents` rows, no triples, no ontology, no binding. `datasets/` is gitignored — a per-machine choice, not part of the tree. |
| `tests/corpus/` | 20 generated fixtures | The office-format fidelity corpus. No triples at all. |

So a corpus was **generated**. `scripts/measure_shacl_scope.py::generate_corpus` is the whole
of how, and the two parameters that could have been invented were not:

* **Triples per document: 5.** `Settings.extraction_max_facts` (`jmfts_core/config.py:256`)
  is 5 — the maximum triples fact extraction writes per segment — and a segment is a
  `documents` row. It is the appliance's own ceiling on how dense its triple store gets.
* **Violating fraction: 0.5.** Half the documents lack the `prop:country` triple the shape
  requires. Both halves carry exactly five triples, so they differ in what the validator
  *reports*, not in what it *reads*.

Every object endpoint is inside the scope, so the graph has **no boundary cuts** — the cheap
case for `BoundaryCut` accounting, chosen so the memory curve carries no term that has
nothing to do with what `pyshacl` holds.

**What this corpus cannot tell you.** It is uniform: one shape, five triples on every
document, no literals longer than a title, no `sh:in` lists, no sequence paths, no
`sh:sparql`, no boundary cuts. A real scope is lumpy, and an expensive constraint component
would move the `pyshacl` column and nothing else. What is measured here is the *shape of the
curve* — which phase is the wall and how each scales — which is a property of the code
rather than of the data. The bound below is therefore right in its unit and its order, and
should be re-measured the day a real corpus exists.

**What corpus would answer it properly:** an ingested appliance with of the order of 10^5
documents in one usetype, fact extraction run over them, and a real vocabulary bound to that
usetype. That is a day of ingest, not a script.

---

## Part 1 — What was measured, and how

The script drives the **real handler**: `OntologyService.validate_binding` enqueues, a real
`IngestWorker` with the real `IngestRollupPlanner` claims the task and runs
`run_validate_shape`, and the counts come off the report node the run wrote. Three of the
handler's callees — `build_data_graph`, `bound_shape_graph`, and the `pyshacl` module
`require_pyshacl()` returns — are replaced in `jmfts_core.validate_tasks`'s namespace by
objects that call the real ones and time them. Delete the three wrappers and the same run
happens with no timings.

Each measured run is a **fresh subprocess**, because a peak is a high-water mark that never
falls: a second run in one interpreter would report the first one's peak.

**Peak RSS is `VmHWM` from `/proc/self/status`, and deliberately NOT
`resource.getrusage(RUSAGE_SELF).ru_maxrss`.** Linux keeps `ru_maxrss` in `signal_struct`
and does not reset it across `exec`, so a child spawned by a large parent reports the
*parent's* high-water mark as its own. Measured on this machine — a 900 MiB parent spawning
a trivial child:

```
child  ru_maxrss 933320   VmHWM 11464
parent ru_maxrss 933320   VmHWM 933320
```

The first ladder run of this script used `ru_maxrss`, and its four smallest rungs reported
the parent's corpus-generation footprint instead of the run's. They were re-taken; every
number below is `VmHWM`, and the baseline is a flat 193–194 MiB at every rung, which is what
a correct measurement looks like.

* Peak RSS **counts** every resident page: interpreter, imported modules, `rdflib`'s Python
  objects, the SQLAlchemy rows the build holds, `psycopg2`'s C-side buffers, allocator
  arenas never returned to the OS.
* It **does not count** anything swapped out, anything freed after the peak (it never
  falls), or memory in any other process — **Postgres's own usage is out of frame** and is
  not small at these sizes.
* The 193–194 MiB baseline contains **no `torch`**: nothing on this path imports it
  (`'torch' in sys.modules` is `False` after importing `validate_tasks`, `ontology_service`,
  `ingest_worker` and `rollup_tasks`), so it is representative of a base-install cpu worker.

**`tracemalloc` peak is Python's allocator only** — objects allocated through
`PyMem`/`PyObject`, which is most of `rdflib` and `pyshacl`, both pure Python. It counts
nothing allocated by a C extension outside that allocator and nothing of the interpreter's
baseline. It is taken in its own run, because tracing costs both memory and time (it roughly
doubles peak RSS and quadruples wall time at these sizes) and mixing it in would inflate the
number the bound comes from.

Machine: 8 cores, 23 GiB RAM, Postgres 16 + pgvector in a container on the same host,
`rdflib` 7.6.0, `pyshacl` 0.40.1, Python 3.11.

To re-run the whole thing — it takes about an hour and needs a Postgres it may DROP, which
is why the scratch database's name must end in `_measure`:

```bash
JMFTS_DB_HOST=localhost JMFTS_DB_PORT=5459 JMFTS_DB_USER=jmfts JMFTS_DB_PASSWORD=jmfts \
  ./.venv/bin/python -m scripts.measure_shacl_scope \
    --sizes 100,1000,10000,50000,100000,136000,138000,140000,160000,200000 \
    --repeats 3 --consecutive 3 --json shacl.json
```

---

## Part 2 — The numbers

Ranges are min–max over three repeats of the same rung. `build`, `shape` and `pyshacl` are
the three timed phases; `held` is `VmRSS` after the drain, i.e. what the process still has.

| docs | triples | build s | shape s | pyshacl s | drain s | peak RSS MiB | after build MiB | build % of peak | held MiB | tracemalloc MiB |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 100 | 500 | 0.02 | 0.003 | 0.012 | 0.2 | 218 | 218 | 99.8% | 216 | 15 |
| 1,000 | 5,000 | 0.19 | 0.009 | 0.13 | 0.5–0.6 | 232 | 231 | 99.4% | 230 | 26 |
| 10,000 | 50,000 | 1.88–1.90 | 0.226–0.232 | 1.51–1.54 | 4.2–4.3 | 352–354 | 343 | 97.3% | 348 | 135 |
| 50,000 | 250,000 | 8.76–9.17 | 0.99–1.01 | 8.22–8.51 | 20.7–21.4 | 888–892 | 847–848 | 95.1% | 876 | 630 |
| 100,000 | 500,000 | 18.7–18.9 | 0.690–0.706 | 14.8–14.9 | 39.6–40.3 | 1,559–1,563 | 1,483–1,486 | 95.1% | 1,458 | 1,246 |
| 136,000 | 680,000 | 24.8–25.2 | 2.718–2.720 | 23.3–23.6 | 58.0–58.5 | 2,018–2,021 | 1,966–1,969 | 97.4% | 1,887 | 1,663 |
| **138,000** | 690,000 | 24.5–25.3 | 2.681–2.744 | 22.7–23.8 | 57.5–59.0 | **2,039–2,042** | 1,990 | 97.6% | 1,907 | 1,684 |
| 140,000 | 700,000 | 25.5–26.2 | 0.929–0.963 | 19.8–20.8 | 54.2–55.2 | **2,082–2,083** | 2,031–2,032 | 97.6% | 1,927 | 1,725 |
| 160,000 | 800,000 | 29.5–30.8 | 1.070–1.116 | 22.8–23.8 | 62.2–64.1 | 2,353–2,371 | 2,272–2,276 | 96.2% | 2,222 | 1,976 |
| 200,000 | 1,000,000 | 37.6–38.5 | 1.379–1.429 | 29.4–30.0 | 79.2–80.7 | 2,901–2,909 | 2,747–2,749 | 94.6% | 2,703 | 2,480 |

Node count equals document count at every rung and triple count is exactly 5× documents, by
construction. Violations are half the documents. The enqueueing request
(`validate_binding`: resolve the scope, mint the node, write the task row) is 0.04 s at 100
documents and 1.75–1.79 s at 200,000.

**Memory per document converges and stays converged.** Growth over baseline, per document:

| docs | 10,000 | 50,000 | 100,000 | 136,000 | 138,000 | 140,000 | 160,000 | 200,000 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| KiB / document | 16.25 | 14.26 | 13.99 | 13.74 | 13.70 | 13.81 | 13.88 | 13.89 |
| KiB / triple | 3.25 | 2.85 | 2.80 | 2.75 | 2.74 | 2.76 | 2.78 | 2.78 |

Linear from 50,000 documents up, at **13.7–13.9 KiB per document / 2.74–2.78 KiB per
triple**. The small rungs are higher only because the constant part of the graph
(namespaces, the shapes graph, the connection) has not yet been amortised.

### Two things in the table that are not the headline

**The `shape` column is not monotone, and it reproduces.** 0.69 s at 100,000 documents,
2.72 s at 136,000 and 138,000, back to 0.93 s at 140,000 — each stable to three digits
across its own three repeats, and the 100,000 and 140,000 values reproduced across two
independent ladder runs on different corpora. `bound_shape_graph` parses a constant Turtle
document and then adds one `sh:targetNode` triple per in-scope document, so it should be
linear and is not. **I did not explain it.** It is 2–5% of the run at every rung, so it does
not move the answer; it is recorded rather than smoothed because a number that behaves like
this is a symptom of something, and the next person to look should know it is real and not a
fluke of one run.

**`drain` exceeds the three phases by 7–11 s at the top of the ladder.** At 138,000 the
phases sum to ~51 s and the drain is 57.5–59.0 s. The remainder is the report write and the
settle, which is its own scaling story — see Part 4.

---

## Part 3 — Comfortable, in units

> **Comfortable** = the run's peak resident set stays **at or under 2,048 MiB**, the cpu
> worker pod's memory *request* (`deploy/k8s/20-worker-cpu.yaml:67`).
>
> **Impossible** = peak past **4,096 MiB**, that pod's memory *limit* (`:73`).

Both numbers are read off the shipped manifest rather than chosen. The request is the line
because a process past its request runs on the node's burst capacity and is the first thing
evicted under node pressure. The limit is the *impossible* line and not merely a worse
version of the request, because of what a validation run is: it has no partial progress and
no resumable state, so the OOM kill the manifest calls "a clean death the lease already
handles" is followed by a retry that allocates exactly the same graph and dies exactly the
same way. Past 4,096 MiB a scope is not slow; it cannot be validated at all on that badge.

**Measured: 138,000 documents at 2,039–2,042 MiB is comfortable. 140,000 at 2,082–2,083 MiB
is not.** The bracket is a measurement, not an interpolation — 136,000, 138,000 and 140,000
were each run three times specifically to find it, after the 100,000/140,000 bracket from
the first ladder was narrowed by a linear fit that predicted the crossing at 137,200.

**So the bound is 138,000 documents**, at the corpus's five triples per document. Restated
in the unit that actually drives it: **690,000 triples**, or **1.85 GiB of graph**. The
document count is what the operator can see before a run and is therefore what the setting
is expressed in — the same argument `ScopeTooLargeError`'s docstring already makes.

**The OOM line is INFERRED, not measured: about 288,000 documents.** From baseline 193 MiB
and the converged 13.89 KiB/document, 4,096 MiB is reached at 287,700; the largest scope
actually run is 200,000 at 2,901–2,909 MiB. The extrapolation spans 1.44× beyond the last
measured point over a curve that is linear to three digits across the top five rungs, which
is why it is stated at all — but it is an extrapolation and the ladder is one flag away from
extending.

### The caveat that matters most for a real deployment, and it was measured

**A worker does not give the memory back.** `held` — `VmRSS` after the whole drain, with the
graph out of scope and the task finished — is 93–95% of the peak at every large rung: 1,907
MiB still resident after a 138,000-document run. A cpu worker is a long-lived process that
claims the next task in the same interpreter, so a fleet sized on "one run, 2 GiB" is sized
on a process that then sits at 1.9 GiB for the rest of its life.

Whether a *second* run in that process reuses what the first freed decides whether this
bound is right or twice too generous, so it was measured directly: three consecutive
200,000-document validations in **one** interpreter, three sequential `measure_once` calls
against the ladder's own 200,000-document corpus. `--consecutive N` was added to the script
afterwards so the check is reproducible from it rather than from a throwaway; it runs the
identical loop.

| run | build s | pyshacl s | cumulative peak MiB | held after MiB |
|---:|---:|---:|---:|---:|
| 1 | 37.5 | 29.6 | 2,902 | 2,647 |
| 2 | 42.1 | 29.8 | 2,970 | 2,740 |
| 3 | 43.3 | 35.2 | 3,848 | 3,215 |

**The allocator reuses most of it, but it drifts.** A single 200,000-document run in a fresh
process peaks at 2,901–2,909 MiB; the second run in the same process added only 68 MiB to
that high-water mark, but the third added 878 MiB. Three runs came to **1.33× a single
run's peak** — not the 2× that no reuse would mean, and not the 1.0× that full reuse would.
Held memory drifted the same way, 2,647 → 3,215 MiB.

Two numbers follow, and they are different questions:

* **A single run at the bound is comfortable: 138,000 documents.** That is the criterion
  stated above and it is directly measured.
* **A worker that runs validations back to back is not, at that bound.** It sits at 1,907
  MiB — its whole request — from the first run onward, and the measured 1.33× drift puts
  three runs at roughly 2,715 MiB. Still well inside the 4,096 MiB limit, so nothing dies;
  it is permanently over its request. An operator whose fleet does this should set the bound
  so that the *drifted* peak fits the request: 2,048 / 1.33 = 1,540 MiB of single-run peak,
  which at 13.89 KiB per document is about **99,000 documents**.

The default recommended below is **138,000**, because it is what the stated criterion
measures and it is measured rather than extrapolated from a drift factor taken at one scope.
The 99,000 figure is recorded beside it so that the operator moving the bound — which is the
whole point of 6.3's "a bound the operator can move" — knows which way to move it and why.

---

## Part 4 — What the split says, and it is not what 6.3 assumed

**The graph build is the wall, in memory and in time, and `pyshacl` is not.**

*Memory.* The peak resident set at the moment `build_data_graph` **returns** is 94.6–97.6%
of the run's whole peak at every rung above 10,000 documents. Everything `pyshacl` does —
parse the shapes graph, evaluate two `sh:minCount` constraints against 138,000 focus nodes,
build a results graph holding 69,000 `sh:ValidationResult` nodes — adds 2.4–5.4% on top.
`bound_shape_graph` adds nothing measurable at all.

*Time.* Of the three phases, the build is 49–55%, `pyshacl` 42–46%, `bound_shape_graph` 2–6%.
Both scale linearly and neither dominates, so a single total genuinely does hide which one
is which — but they are within 1.3× of each other, and halving `pyshacl` would buy 20% of
the run.

**Consequence for open question 6.3, stated plainly.** 6.3 reasons from "`pyshacl` has no
streaming mode". That is true, and it is not what this appliance runs into first. What runs
into memory first is `rdf/shacl.py:415-418`: one `select(Triple)` with `joinedload`,
materialised whole, so at 138,000 documents there are 690,000 SQLAlchemy `Triple` instances
*and* 690,000 sets of `rdflib` terms alive simultaneously. The graph itself — what `pyshacl`
must hold, and the only part that a streaming validator could not help with — is the smaller
half of that.

**Therefore: incremental validation is not the cheapest next move, and the measurement is
what says so.** Two things are cheaper and neither needs a new design:

1. **Stream the build.** `.execution_options(yield_per=N)`, or Core rows instead of ORM
   instances, would drop the ORM half. `build_data_graph` currently reads every row into
   `touching`, partitions it into `inside` and `outside_by_triple`, and only then mints
   terms, so three lists of row objects are alive at once; the work each row needs is a
   scope test and a term conversion, and neither needs the row to outlive its own iteration.
   Reordering it so the partition emits terms rather than rows is a change inside one
   function. **Not measured** — this is a reading of the code, and the honest next step is to
   try it and re-run this ladder.
2. **Do not render 11 MiB of markdown.** See below.

Incremental validation earns its place when a full run is *too slow*. At the bound it is
57.5–59.0 s of drain — inside the same order as the queue's 90 s lease
(`config.py:133`), and the worker beats from a background thread throughout
(`ingest_worker.py:274`), so the run is not at risk of being reaped. **A minute is not too
slow for a task somebody asked for and polls.** The case for incremental validation is not
made by these numbers.

### The second wall, which nobody has named yet: the report

The report node is a document, and it scales with the scope:

| docs | violations | `content` chars | `structured_content` stored | tsvector lexeme bytes |
|---:|---:|---:|---:|---:|
| 100 | 50 | 8,535 | ~2 KB | 462 |
| 10,000 | 5,000 | 834,295 | 47 KB | 19,812 |
| 100,000 | 50,000 | 8,439,300 | 536 KB | 244,819 |
| 138,000 | 69,000 | **11,688,300** | 731 KB | 358,814 |
| 200,000 | 100,000 | 16,989,302 | 1,083 KB | 544,821 |

At the bound the run writes **an 11.1 MiB markdown document** into `documents.content`, and
`idx_documents_content_fts` (`schema.sql:473`) is a partial GIN index on
`to_tsvector('english', title || ' ' || content) WHERE settled = 'settled'`. So the *settle*
— the write that flips `settled` — is what builds a tsvector over 11 MiB of text. That is
where the 7–11 s of drain time unaccounted for by the three phases goes.

**Postgres refuses a tsvector whose lexeme strings exceed 1,048,575 bytes.** At 200,000
documents the report is at 544,821 — 52% of the ceiling. Extrapolating at the measured 2.724
bytes per document, the ceiling is reached at about **385,000 documents** (INFERRED), and at
a 100%-violating scope, where a report is roughly twice as long per document, at about
**192,000**. The memory bound of 138,000 binds first in both cases, so nothing is broken
today — but an operator who raises `shacl_max_scope_documents` past roughly 190,000 buys a
`string is too long for tsvector` at the *last* write of a run that has already spent a
minute, classified PERMANENT (`task_errors.py:121` maps `ValueError`), on a node that then
settles `failed` with no report at all.

The task row is the same shape of cost and is not close to any limit: at 138,000 documents
`task_queue.params` holds 138,000 ids, 993,005 bytes as text and 467,880 stored.

---

## Part 5 — The setting this step was supposed to fill in, and the mismatch

> **This Part is a to-do list for work that has shipped, and one of its four items shipped
> with a different number on purpose. Read it as history. Corrected 2026-09-15**
> (`SPRINT_0_6_0.md` Block E step 5). The field landed on 2026-09-10 and every item below is
> closed:
>
> **The default is 512, not the 138,000 this Part recommends, and that is a decision rather
> than a slip.** `jmfts_core/config.py` argues it at the field: *"512 IS DELIBERATELY
> CONSERVATIVE AND IS NOT A CAPACITY LIMIT … 512 is far below every rung anybody ran, which
> is the point: a fresh install refuses long before it can be killed."* The measurement this
> document produced was not thrown away — it is cited there as **where to raise it to**:
> 138,000 for a worker that runs one validation, 99,000 for a fleet that runs them back to
> back. So this document's framing at its own Part 0 ("it decides … the default of
> `shacl_max_scope_documents`") is the sentence that turned out wrong. A measurement of what
> will fit is not a proposal for what a fresh install should permit, and `pyshacl` having no
> streaming mode is why: a scope that does not fit is a refusal rather than a slow success,
> and an OOM kill is followed by a retry that allocates the same graph and dies the same way.
>
> Two smaller differences: the field is typed `int | None`, not `Optional[int]`; and
> `config.py` records two things this Part did not know — `None` is reachable in-process only
> (a blank `JMFTS_SHACL_MAX_SCOPE_DOCUMENTS` is a pydantic parse error at startup, not "no
> bound"), and `0` is reachable and refuses every scope holding a document.
>
> **Items 2, 3 and 4 are all done.** `ontology_service._pin_scope` passes
> `get_settings().shacl_max_scope_documents`; `rdf/shacl.py`'s
> `MAX_SCOPE_DOCUMENTS_SETTING` comment now opens *"The field exists"* and cites this
> document by path; `validate_tasks.py` still passes `None` for the reason item 4 gives.
> **The block quote below no longer exists in the tree** — the comment it quotes was replaced
> by its own negation, so do not go looking for it.
>
> **And the closing note about `README.md` was acted on**: `MEASURE_SHACL_SCOPE.md` joined
> the "A note on documentation" list on 2026-09-06 on that one citation, which `CLAUDE.md`
> records as the rule working as written. As of 2026-09-15 it is no longer on that list
> because it is no longer held back — this file is it.

**Open question 6.3's setting does not exist, so there was no default to fill in.** The
`shacl:graph` lane defined `ScopeTooLargeError` and named the field's spelling, and said so
explicitly at `jmfts_core/rdf/shacl.py:72-83`:

> The field does not exist yet and the value is not chosen … What step 5 has to fill in:
> `shacl_max_scope_documents: Optional[int]` on `jmfts_core/config.py`'s `Settings` (env
> `JMFTS_SHACL_MAX_SCOPE_DOCUMENTS`), and the measured number as its default.

Creating it means editing `config.py` and both call sites, which this lane does not own.
The measurement is done and the exact change is:

1. **`jmfts_core/config.py`**, after the fact-extraction block (`:256-260`):

   ```python
   # SHACL validation — open question 6.3, SPRINT_0_5_0.md Block A. MEASURED, not chosen:
   # docs/MEASURE_SHACL_SCOPE.md. pyshacl has no streaming mode and neither does the graph
   # build, so a run holds the whole scope at once: 13.9 KiB of resident set per document
   # at 5 triples each, and 138,000 documents peaked at 2,039-2,042 MiB against the cpu
   # worker pod's 2 GiB memory request (deploy/k8s/20-worker-cpu.yaml:67). 140,000 measured
   # 2,082 MiB and is past it. A worker does not give it back: 1,907 MiB is still resident
   # after the run, and three back-to-back runs drifted to 1.33x a single run's peak, so a
   # fleet that validates continuously wants ~99,000 here instead. None means no bound,
   # which is what this appliance did before the number existed.
   shacl_max_scope_documents: Optional[int] = 138_000
   ```

2. **`jmfts_core/services/ontology_service.py`, in `_pin_scope` (`:167-175` as of
   2026-09-06)** — `max_scope_documents=None` becomes
   `max_scope_documents=get_settings().shacl_max_scope_documents`, and the comment above it
   ("a Settings field Block A step 5 has to MEASURE, and it does not exist yet") is replaced
   by a pointer here. There is exactly one call site: the Block B lane extracted `_pin_scope`
   as the shared front door for both `validate` and `derive`, so the bound lands on both at
   once — which is right, since a `sh:rule` pass builds the same graph.

3. **`jmfts_core/rdf/shacl.py:72-83`** — the `MAX_SCOPE_DOCUMENTS_SETTING` comment says the
   field does not exist. It does now; the paragraph should cite this document instead.

4. **`jmfts_core/validate_tasks.py:384-387` stays as it is.** Its `None` is correct and its
   reasoning holds: the request already applied whatever bound the operator configured, and
   re-applying it in the handler would refuse a run the request accepted.

Note that step 1 puts a `docs/MEASURE_SHACL_SCOPE.md` citation in a file that **ships**, so
`README.md`'s "A note on documentation" list (`:247-251`) gains this filename by the rule
`CLAUDE.md` states for exactly this case.
