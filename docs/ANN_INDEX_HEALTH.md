# ANN index health: what dead tuples cost, and what the options are

Written 2026-09-07, against `sprint-0-4-0-plan` at `7d746d9`. Measured on
`pgvector/pgvector:pg16` — PostgreSQL 16.14, pgvector 0.8.5 — the versions the
appliance runs, on throwaway containers with `autovacuum = off` so dead-tuple
state is controlled rather than raced by the daemon.

**This file is not a defect list and does not become one.** `docs/SPRINT_0_4_0.md`
Part 0 states the rule: an open defect is a numbered step in the current sprint
plan and the entry condition is a failing test. What is here is a measurement and
a set of options with their costs. A step that comes out of it will name this
file; nothing here is owned by it.

Part 1 is settled measurement. Part 2 is an open strategy surface, and it is
deliberately not a recommendation — the right choice depends on a workload this
appliance does not have one of.

> **Reviewed for publication 2026-09-15** (`SPRINT_0_6_0.md` Block E step 5). One thing
> dominates and it is worth stating once, at the top: **`token_embeddings.embed_256` is an
> HNSW index and has been since migration 022.** §5.8 is where that was decided and it
> carries the argument; §5.6 and §5.7 open with banners pointing at it. **Four other places
> do not, and they still describe the IVFFlat index the migration replaced** — §1.4's
> amendment, Part 2's option E status line, Part 3's "the token path" bullet, and §5.4. Each
> now carries a banner of its own. `idx_token_embed_256_ivf` does not exist; the index is
> `idx_token_embed_256_hnsw`, it has no `lists`, and `maxsim_search` sets
> `hnsw.iterative_scan = strict_order` the way `vector_search` already did.
>
> Three smaller corrections, each marked where it sits: Part 4's question 2 was answered
> differently from the way it proposes (§1.4); §1.4's "the suite's ANN-sensitive tests fail
> intermittently" is no longer true; and §5.8's caveat about a sub-0.90 recall reading sends
> the next reader to the wrong knob — it happened, and the cause was floating-point
> comparison, not the fixture.
>
> **Part 0's table lists seven scripts under the heading "Six scripts", and none of them are
> in this repository.** They were written for these runs against throwaway databases and
> kept out of the tree on purpose. A reader cannot reproduce Part 1 from a clone.
>
> **Every citation's line number has drifted** — `repositories/search.py` has grown by
> several hundred lines since. Every one was spot-checked and the substance holds; grep the
> quoted text rather than opening the line. Two were worse than drift and are fixed inline,
> because `search.py:874` and `:877-879` now point at BM25 container ranking.
>
> **`docs/SPRINT_0_4_0.md` is cited above and throughout and stays held back by decision**
> (`SPRINT_0_6_0.md` 4.5). The rule its Part 0 states — an open defect is a numbered step in
> the current sprint plan with a failing test as its entry condition — is restated in
> `docs/SPRINT_0_6_0.md` Part 0, which **is** published; read it there.

Parts 1 to 4 are about `documents.embed` and HNSW. **Part 5 is the other column**
— `token_embeddings.embed_256` and IVFFlat, which Part 3 lists as unmeasured and
4.3 leaves ownerless — and it was added 2026-09-10 with a failing test behind it.
It supersedes 4.3, and the largest thing it found is not the one 4.3 named.

**EVERY VECTOR BELOW IS SYNTHETIC, AND ONE CONCLUSION DOES NOT SURVIVE REAL ONES.**
`docs/STRESS_CORPUS.md` Part 6 repeated the recall measurement on 57k real document
embeddings and 1.37M real token embeddings, the same way — recall@10 against a
brute-force scan, queries drawn from the corpus, arms paired on one gold set.

* **1.7's "IVFFlat lost nothing" is a property of the fixture, not of IVFFlat.**
  On clustered Gaussian noise `ivf/p1` read 1.0. On real embeddings it reads
  **0.7467** on `documents.embed` (35 of 60 queries degraded) and **0.7533** on
  `token_embeddings.embed_256` at the shipped `lists=1024` (15 of 30 degraded).
  Option B ("Prefer IVFFlat") therefore trades a dead-tuple failure mode for a
  standing 25% recall loss, which the synthetic measurement priced at zero.
* **1.8's VACUUM asymmetry survives intact**, and on real vectors is sharper:
  15.85 s for HNSW against 0.084 s for IVFFlat on the same 57,691 rows with 10%
  deleted — 189×. It also gains an operational qualifier the synthetic run could
  not see, because it set `autovacuum = off`: under the default daemon the same
  table vacuums in 213 ms, because the work has already been amortised. The full
  cost appears after a **bulk** delete, which is what a re-ingest is.
* **1.7's `strict_order` result is unchanged and untested here.** On a clean real
  index it makes no difference (0.9767 either way), which is what 1.7 predicts:
  it is a mitigation for dead entries, and that corpus had none.

---

## Part 0 — The harness, and two ways it lied

Seven scripts, kept out of the tree because they build and drop indexes on a
throwaway database. They live in the session scratchpad and are not in this repository,
so Part 1 is not reproducible from a clone.
<!-- Said "Six" until 2026-09-15; the table under it has always listed seven. -->

| Script | Question |
|---|---|
| `annhealth.py`, `clustered.py` | Does a planted-target scan lose rows? **Superseded — see 1.6.** |
| `deadrecall.py` | Does recall@10 against a brute-force scan fall when dead entries are present? |
| `vacuumcost.py`, `vac100k.py` | What does VACUUM cost, at 1k/10k and at 100k? |
| `retrycost.py` | What does detect-then-retry cost, and does it converge? |
| `truncrate_plugin.py` | How often is `AppliedFilters.truncated` already true when nothing is wrong? |

Two harness faults produced confident wrong numbers before they were caught. Both
are easy to reproduce by accident and both are recorded here so the next person
does not pay for them again.

**A LATERAL subquery that references nothing from the outer row is folded to one
constant.** Generating a corpus with
`FROM generate_series(1, n) g CROSS JOIN LATERAL (SELECT … random() … ) sub`
produced `n` rows carrying ONE vector — verified directly: 5 rows, 1 distinct
vector. That is the degenerate identical-vector condition, which is the thing the
corpus was meant to be a control for. Correlate the subquery
(`generate_series(1, 768 + 0 * g)`) and assert
`count(DISTINCT embed) ≈ count(*)` before measuring anything.

**Gaussian noise of per-component scale `s` has norm `s·√768 ≈ 27.7·s`.** A
"cluster spread" of 0.35 is noise of norm 9.7 around a centre of norm 1, which is
not a cluster. Every spread parameter is divided by `√DIM`.

---

## Part 1 — What was measured

### 1.1 The failure, and its two independent halves

Captured twice from the real suite: `Index Scan using idx_documents_embed`,
Actual Rows 0, `Rows Removed by Filter: 0`, with seven visible settled embedded
rows in the table. `reltuples 0.0`, `relpages 31`/`35`, `n_live_tup 0`,
`n_dead_tup 521`, `hnsw.ef_search 40`, `hnsw.iterative_scan strict_order`,
`hnsw.max_scan_tuples 20000`, and `scan_truncated: True` on both.

Two conditions are each necessary and neither is sufficient.

* **Autoanalyze writes `reltuples = 0.0`**, which makes the planner choose the
  index scan at all. Confirmed by running `ANALYZE` alone, no VACUUM, against
  1000 dead identical entries:

  | ANALYZE ran | reltuples | relpages | plan | rows returned |
  |---|---:|---:|---|---:|
  | no | −1.0 | 0 | Seq Scan | 3, 3, 3, 3 |
  | yes | 0.0 | 15 | Index Scan | 3, 3, 3, **0** |

* **Dead entries at the query point** are what make that scan return nothing.
  This is the mechanism pgvector issue #244 states in prose — "when the top
  results in the HNSW index are dead tuples, recall could be 0%" — and gives no
  reproduction for.

An earlier reading called autovacuum the cause of the empty page. It is not.
Autoanalyze supplies the plan choice; the dead entries supply the empty page.

### 1.2 Not a small-table artefact

The first suspicion was that this only bites a near-empty table, because every
prior reproduction started from `TRUNCATE` and the table held seven rows — the
suite's shape and no appliance's. It does not hold: with a background corpus
present, the failure persisted at every corpus size measured, and 1.7 reproduces
it on 20 000 rows.

`read` `jmfts_core/sql/schema.sql:481` — the partial predicate
`WHERE settled = 'settled'` on `idx_documents_embed` carries a comment saying a
chunk "enters the graph once, when it settles", so re-embedding churn never
causes an insert-then-delete. That holds for the ingest path. It does not hold
for the test fixture, which writes settled rows inside a transaction that is
rolled back — **and a rolled-back insert leaves index entries behind exactly as a
DELETE does.** That equivalence is not incidental; 1.7 seeds its dead entries by
rollback for precisely this reason.

### 1.3 Superseded: repairs measured on the planted-target metric

Measured at 1000 dead identical entries, 15 runs per cell, on the metric 1.6
retires:

| Action | Failures |
|---|---|
| `VACUUM ANALYZE` | 0/15 |
| `REINDEX INDEX idx_documents_embed` | 0/15 |
| `hnsw.iterative_scan = strict_order` | reduced the rate, never to zero |
| `hnsw.ef_search` at 40 / 200 / 1000 | no effect |

**The last two lines are wrong**, and 1.7 corrects them. On the recall metric
`strict_order` is a near-complete mitigation and `ef_search = 200` is a partial
one. The VACUUM and REINDEX rows stand.

`read` `repositories/search.py:49,432` — the appliance sets
`HNSW_ITERATIVE_SCAN = "strict_order"` with `SET LOCAL` on `vector_search`, and
sets no `hnsw.ef_search`, so it runs at pgvector's default of 40.

### 1.4 The rates are not stable, and that is a property of the index

HNSW assigns element levels randomly at insert, so whether a live element is
reachable within the candidate budget is decided per graph build. The same cell
at the same parameters has read 6/20 and 15/20 across sweeps. **A single run
measures nothing here.**

It is also why the suite's ANN-sensitive tests fail intermittently. Three full
runs on 2026-09-07, same tree, same container image, no code change between them:
<!-- No longer true, 2026-09-15. The four tests this describes were made deterministic on
     2026-09-12 and `tests/test_usetype_filter.py` records how. The three-run table below
     is dated evidence and stands; the present-tense sentence above it does not. -->

| run | result |
|---|---|
| 1 | 2 failed, 2383 passed, 33 skipped |
| 2 | 1 failed, 2384 passed, 33 skipped |
| 3 | **0 failed, 2385 passed, 33 skipped** |

**A green suite here is not evidence that anything was fixed**, and neither is a
red one evidence that something broke. That is the direct cost of Part 4's
question 2: the fixture is currently both the only place this condition is
exercised and the reason the suite cannot be trusted to be deterministic.

**Amended 2026-09-10: at least one of those failures is not this.**
`tests/test_usetype_filter.py::test_maxsim_takes_the_same_set_and_survives_a_quoted_usetype`
was read as part of the intermittency above. It is not intermittent. `ran` four
times on `pgvector/pgvector:pg16`: it passes alone in 0.07 s, every time, and fails
in a full-suite run (`2 failed, 2479 passed, 33 skipped in 451.34s`). It is
deterministic on how many rows the suite has already written to `token_embeddings`,
which is an IVFFlat reading and not an HNSW one. `read`
`repositories/search.py:1123`-`:1129`: `maxsim_search` puts the usetype filter, the
`settled` gate and the RBAC fragment in the `WHERE` of an
`ORDER BY te.embed_256 <=> … LIMIT 100` over `token_embeddings`, and `read` `:1144`
states that no `ivfflat.iterative_scan` is set at that call site. So it is a
post-filtered IVFFlat scan: on a small table the planner does not reach the index
and the answer is exact, and on a large one it takes `probes = 1` worth of a k-means
cell and the usetype filter empties what comes back. The failure is Part 5's
subject, and the table above should not be read as covering it.

> **This whole amendment diagnoses an index that no longer exists. Read 2026-09-15.**
> Migration 022 replaced `idx_token_embed_256_ivf` with `idx_token_embed_256_hnsw`, and
> `maxsim_search` now sets `hnsw.iterative_scan = strict_order` the way `vector_search`
> already did — so "no `iterative_scan` is set at that call site" and "a post-filtered
> IVFFlat scan" are both stale. §5.8 is where that landed and why. **The conclusion the
> amendment reaches survives**: the failure was Part 5's subject and not this section's, and
> the three-run table does not cover it.
>
> **And Part 4's question 2 was answered, differently from the way it proposes.** That
> question asks whether the fixture's seven identical vectors should be jittered, and notes
> that jittering would remove the only place the condition is exercised. What shipped on
> 2026-09-12 is recorded in `tests/test_usetype_filter.py`: distinct vectors alone were
> **not** enough — *"40 rounds of 200 still came up short"* — and the fix was moving the
> neighbourhood per round. The suite is deterministic here now. The file holds 26 tests, not
> the ~34 question 2 estimates.

### 1.5 `AppliedFilters.truncated` is already true almost always

`read` `repositories/search.py:321-334` — `_note_ann_page` sets `scan_truncated`
from `returned < requested`, and its docstring says the cause is not observable
there: "short-because-the-walk-stopped is indistinguishable at this layer from
short-because-nothing-else-matched."

Measured by wrapping `_note_ann_page` for one full suite run against a clean
pgvector container (2384 passed, 1 failed, 33 skipped):

| | count |
|---|---:|
| ANN pages recorded | 51 |
| pages that came back short | **44 (86.3%)** |
| pages that returned zero rows | 4 |
| tests producing at least one ANN page | 37 |
| tests producing at least one short page | 32 |

Every `requested` size of 9 or above was short 100% of the time. The suite's
corpora hold a handful of documents while requests ask for 9 to 100 rows, so
`returned < requested` is nearly guaranteed.

**This bounds what any truncation-triggered policy can be.** 51 pages is a small
sample and the suite is not a production workload, so the 86.3% does not
transfer. What transfers is the shape: the signal fires whenever the corpus is
small relative to `limit`, which is a condition no appliance controls.

### 1.6 Three metrics that measured their own fixture

The first three sweeps asked "does the scan return these seven planted rows",
which is the suite's own assertion. That metric requires the seven targets to be
nearer to the query than everything else in the corpus, which makes them
outliers — and an isolated group with no distance gradient leading to it is the
case a greedy graph walk is worst at, for reasons unrelated to dead tuples. All
three showed a dirty control, failing with **zero** dead entries: 81/100 on a
uniform-random corpus, then 37/50 and 3/10 as the geometry was corrected. Raising
the cluster spread made it worse, because it pushed the nearest background row
from d=0.26 to d=0.68 and made the targets *more* of an island.

Any measurement whose control is not clean is measuring its fixture. The metric
was replaced with recall@10 against a brute-force scan.

### 1.7 Recall against exact, paired on dead entries

20 000 rows in 200 clusters on the unit sphere, 100 queries **drawn from the
corpus** so their true neighbours are ordinary corpus points, ground truth by
exhaustive scan (`enable_indexscan = off`), `ivfflat lists = 20`. Dead entries
are planted at the query points and seeded by insert-then-rollback. Every arm is
paired: the finding is the difference between the `dead = 0` column and the
others.

recall@10, with the count of degraded queries in brackets:

| arm | dead = 0 | dead = 50/query | dead = 500/query |
|---|---:|---:|---:|
| `hnsw/off` (ef 40) | 1.0 [0] | **0.100** [100] | **0.105** [100] |
| `hnsw/strict` (ef 40) | 1.0 [0] | 0.999 [1] | 0.979 [3] |
| `hnsw/ef200` | 1.0 [0] | 1.0 [0] | **0.099** [100] |
| `ivf/p1` | 1.0 [0] | 0.999 [1] | 1.0 [0] |
| `ivf/p10` | 1.0 [0] | 1.0 [0] | 1.0 [0] |
| `ivf/p50` | 1.0 [0] | 1.0 [0] | 1.0 [0] |

Every control reads 1.0 with 0 degraded queries, so the differences are the
treatment and nothing else.

* **The unmitigated HNSW collapse is total.** `hnsw/off` at 50 dead per query
  returns 1 of 10 true neighbours, on all 100 queries. The one it keeps is the
  query's own row, at distance 0.
* **`strict_order` — which the appliance already sets — is a near-complete
  mitigation**, holding 0.979 at 500 dead per query. It is not perfect: the worst
  single query still returned 0.0.
* **`ef_search = 200` only moves the threshold.** It is clean at 50 dead and
  collapses to 0.099 at 500. Raising the candidate budget buys a larger dose
  before failure, not immunity.
* **IVFFlat did not lose a row in any cell, at any probes setting.** This is the
  immunity claim, and it is bounded by resolution: 100 queries per cell, so a
  degradation below roughly 1% would not be visible.

Latency and build, same corpus, index sizes within 0.3 MB of each other
(`hnsw` 78.1 MB, `ivf` 78.2–78.3 MB):

| arm | p50 at dead=0 | p50 at dead=500 | build |
|---|---:|---:|---:|
| `hnsw/strict` | 20.5 ms | 21.6 ms | 5.8–6.1 s |
| `ivf/p1` | 21.7 ms | 23.3 ms | 0.5 s |
| `ivf/p10` | 26.2 ms | 40.3 ms | 0.5–0.7 s |
| `ivf/p50` | 31.5 ms | 59.0 ms | 0.5 s |

IVFFlat builds 10× faster and costs 1.06× to 2.7× the query latency depending on
probes. **Recall and probes are a separate axis from the dead-tuple question**:
`ivf/p1` scored 1.0 here because `lists = 20` over 20 000 rows puts ~1000 rows in
each list, so one probe reaches the query's neighbourhood. That is not the
appliance's token-index geometry — see Part 3.

### 1.8 What VACUUM costs

`none` is the same table with no ANN index, and it is what makes the other rows
readable. The 1k and 10k rows are medians of 3 runs; the 100k row is one run from
`vac100k.py`, which loads via numpy COPY rather than SQL `random()`.

| arm | rows | index | 10% deleted | 50% deleted | 100% deleted | build |
|---|---:|---:|---:|---:|---:|---:|
| none | 1 000 | — | 1 ms | 1 ms | 7 ms | — |
| ivfflat | 1 000 | 4.5 MB | 1 ms | 2 ms | 7 ms | 0.04 s |
| hnsw | 1 000 | 3.9 MB | **543 ms** | **473 ms** | 14 ms | 0.25 s |
| none | 10 000 | — | 2 ms | 8 ms | 24 ms | — |
| ivfflat | 10 000 | 39.7 MB | 14 ms | 19 ms | 30 ms | 0.70 s |
| hnsw | 10 000 | 39.1 MB | **11.8 s** | **20.8 s** | 97 ms | 5.4 s |
| none | 100 000 | — | 0.21 s | 0.24 s | — | — |
| ivfflat | 100 000 | 391.2 MB | 0.24 s | 0.30 s | — | 2.3 s |
| hnsw | 100 000 | 390.6 MB | **87.4 s** | **78.0 s** | — | 10.3 s |

Four readings.

* **HNSW's VACUUM cost tracks index size, not how much was deleted.** At 1000
  rows, deleting 10% costs 543 ms and deleting 50% costs 473 ms; at 100 000 rows,
  87.4 s against 78.0 s. In both, the larger delete is the cheaper one.
  **Deleting one document costs what deleting fifty thousand costs.**
* **It grows fast with the index**: 0.54 s at 3.9 MB, 11.8 s at 39 MB, 87.4 s at
  391 MB.
* **Deleting everything is cheap** (14 ms, 97 ms) because there is no graph left
  to repair.
* **IVFFlat's VACUUM is 364× cheaper than HNSW's at 100 000 rows** (0.24 s
  against 87.4 s), at an index size within 0.6 MB of it.

### 1.9 Detect, VACUUM, retry — end to end

Same instrument as 1.7. 20 000 rows, 50 queries, 500 dead entries per query point,
78.1 MB HNSW index. The three costs are reported separately because a policy pays
them differently: every request pays the first, only a tripping request pays all
three.

| scan mode | recall before → after | degraded queries | first query | VACUUM | retry |
|---|---|---:|---:|---:|---:|
| `off` | 0.100 → **1.0** | 50/50 → **0/50** | 32.2 ms | **5 866 ms** | 31.8 ms |
| `strict_order` | 0.978 → **1.0** | 2/50 → **0/50** | 33.8 ms | **5 806 ms** | 31.1 ms |

**The retry converges.** Both modes recover full recall, and this is the first
time convergence has been measured rather than assumed — the earlier repair
evidence (1.3) was collected on an empty table.

**The VACUUM is the whole cost, and it is three orders of magnitude larger than
the query.** 5.8 s on a 78 MB index; 1.8's scaling puts the same operation at
87 s on a 391 MB index. VACUUM time is also unaffected by the scan mode, as it
should be — the repair does not know how the query was run.

One implementation constraint the measurement forced: VACUUM cannot run inside
the caller's transaction, so a retry path needs its own connection.

---

## Part 2 — The strategy surface

Five options. Four were named on 2026-09-06; the fifth is one the measurement
surfaced. **None is chosen here.** The right choice depends on how often documents
are deleted, how large the corpus is, whether a retrieval miss is a nuisance or a
correctness failure, and whether the deployment can afford a maintenance window.
Those differ per deployment and this appliance is not one deployment.

### A — VACUUM after DELETE

Issue `VACUUM documents` on the path that deletes.

*Cost, from 1.8.* On HNSW: 543 ms at 1000 rows, 11.8 s at 10 000, **87.4 s at
100 000**. On IVFFlat at 100 000: 0.24 s. **The cost does not scale with how much
was deleted** — 10% deleted cost 87.4 s and 50% cost 78.0 s at 100 000 rows — so
deleting a single document costs what deleting fifty thousand costs.

*What it does not fix.* Nothing about the window between the delete and the
vacuum. During it, recall is whatever 1.7 says for the dose accumulated.

*Choose it if* the corpus is small, or the index is IVFFlat, or deletes are
already batched into a window where 90 seconds is affordable.

*Against it:* on HNSW at any real corpus size, a per-delete VACUUM makes a
document deletion a minute-long operation.

### B — Prefer IVFFlat

Change `idx_documents_embed` from HNSW to IVFFlat.

*Cost, from 1.7 and 1.8.* IVFFlat lost no rows in any dead-entry cell at any
probes setting; VACUUM is 364× cheaper at 100 000 rows; build is 10× faster;
index size is within 0.6 MB. Query latency is 1.06× HNSW at `probes = 1`, 1.28×
at `probes = 10`, 1.54× at `probes = 50`, and the gap widens under dead-entry
load (up to 2.7× at `probes = 50`).

*What it does not fix.* IVFFlat's own recall is a function of `lists` and
`probes`, and that is an entirely separate failure mode with its own history
here — `4dadb62`, and `read` `repositories/search.py:874`. Switching index type
replaces a stateful, invisible loss with a static, tunable one. It does not
remove loss.

*Choose it if* deletes are frequent and VACUUM windows are unaffordable, and the
`lists`/`probes` pair is chosen and measured rather than left at defaults.

*Against it:* IVFFlat needs rebuilding as the corpus grows, because its centroids
are fixed at build time. Nothing here measured that.

### C — Detect truncation, VACUUM, retry

Treat `AppliedFilters.truncated` as the signal to repair and re-query.

*Cost, from 1.5 and 1.9.* The repair works — recall recovers to 1.0. The repair
costs 5.8 s at 78 MB and, extrapolating 1.8, about 87 s at 391 MB. **The trigger
fired on 86.3% of the suite's ANN pages when nothing was wrong.**

*What it does not fix.* The trigger is ambiguous by construction, and
`_note_ann_page`'s own docstring says so. A short page is
short-because-nothing-matched far more often than short-because-the-walk-stopped.

*Against it, strongly, as stated.* A policy keyed on `truncated` alone would fire
a multi-second VACUUM on most searches. To be viable it needs a trigger that
separates the two causes, and the obvious candidate — an exact count confirming
matching rows exist — is the sequential scan the index was there to avoid.

*What would make it work:* a cheaper discriminator. Not identified here.

### D — Prefer application patterns that do not DELETE

Soft-delete, or supersede-and-exclude, instead of removing rows.

*Cost.* Unmeasured, and it is a schema and semantics question rather than a
timing one.

*What it does not fix.* Two things reach the index anyway. A rolled-back
transaction leaves dead entries exactly as a DELETE does (1.2) — which is what
the test fixture does today. And genuine deletion is a requirement, not a
pattern choice: an appliance that cannot remove a document has a different
problem.

*Note:* the appliance already does a version of this. The partial predicate
`WHERE settled = 'settled'` (`schema.sql:481`) keeps re-embedding churn out of the
graph, and its comment says so. This option is that idea extended.

### E — The scan mode, which is already half-applied

`hnsw.iterative_scan = strict_order`.

*Cost, from 1.7.* It takes recall at 50 dead entries per query from 0.100 to
0.999, and at 500 from 0.105 to 0.979. Latency cost is inside noise: 20.5 ms
against 20.5 ms at rest, 21.6 ms against 20.2 ms under load. It is a GUC, not a
migration.

*Status.* `read` `repositories/search.py:432` — the appliance already sets it, with
`SET LOCAL`, on `vector_search`, which is the only site that issues an ANN
`ORDER BY` against `documents.embed`. The MaxSim path deliberately sets no
`ivfflat.iterative_scan` (`read` `repositories/search.py:877-879`), and 1.7 says
that path did not need one: IVFFlat lost nothing in any cell.

> **The MaxSim half of that status line is superseded. Read 2026-09-15; §5.8 is where it
> landed.** Migration 022 made `token_embeddings.embed_256` an HNSW index, and
> `maxsim_search` now sets `hnsw.iterative_scan = strict_order` from the same
> `HNSW_ITERATIVE_SCAN` constant `vector_search` uses. So the appliance sets the mitigation
> on **both** ANN paths, not one. The cited line no longer points at the MaxSim query at
> all — `search.py:877` is BM25 container ranking now. The `vector_search` half of the
> sentence, and everything above and below it, is unchanged and correct.

**So the appliance's current configuration is already the strongest single
mitigation measured, and the reason the collapse shows up in the suite rather
than in production is that the suite's fixture supplies an unusually
concentrated dose of dead entries at one point.**

*What it does not fix.* `strict_order` is not immunity. At 500 dead entries per
query, 3 of 100 queries were still degraded and **one returned 0 of its 10 true
neighbours**. A deployment where a miss is a correctness failure cannot stop here.

*Not a substitute:* `ef_search = 200` looks like the same fix and is not. It was
clean at 50 dead and collapsed to 0.099 at 500 (1.7). Raising the candidate
budget buys a larger dose before failure; changing the scan mode changes how the
walk terminates.

---

## Part 3 — What was not measured

* **Concurrency.** VACUUM takes no exclusive lock but competes for I/O. What it
  costs a concurrent query is a separate experiment.
* **Real embeddings.** *(Superseded by this document's own header block and by all of
  Part 5: `STRESS_CORPUS.md` Part 6 repeated the measurement on 57k real document
  embeddings and 1.37M real token embeddings, and §5.2 runs on 47,242 real
  `nomic-ai/modernbert-embed-base` token rows.)* Every corpus here is synthetic. The clustered generator is
  much closer to embedding geometry than uniform noise — and 1.6 is what happens
  when it is not — but no corpus of `nomic-ai/modernbert-embed-base` vectors over
  real documents was built.
* **The token path.** *(Superseded 2026-09-15 — Part 5 measured it and migration 022
  changed it. `idx_token_embed_256_ivf` no longer exists; the index is
  `idx_token_embed_256_hnsw`, it declares no `lists`, and `maxsim_search` sets
  `hnsw.iterative_scan = strict_order`. The cited `search.py:874` now points at BM25
  container ranking. Read the bullet as the statement of what Part 5 then went and did.)*
  `idx_token_embed_256_ivf` is IVFFlat with `lists = 1024`
  over `token_embeddings`, and `read` `repositories/search.py:874` says the
  appliance runs it at pgvector's default `ivfflat.probes = 1` — one list of
  1024, where 1.7's `ivf/p1` was one list of 20. That comment already records the
  cost ("a readable share at or below 5% loses most of its neighbours"), and
  `4dadb62` measured it. **That is a probes finding, not an IVFFlat finding**, and
  1.7 separates the two: at `lists = 20` the same index type scored 1.0. Nothing
  here measured MaxSim end to end.
* **Autovacuum's own scheduling.** Everything here ran with `autovacuum = off`
  and issued VACUUM explicitly. When the daemon would fire on a real workload,
  and whether tuning its thresholds is another option, is not measured.
* **`hnsw.max_scan_tuples` and `hnsw.scan_mem_multiplier`.** Left at defaults
  (20000, 2) throughout.
* **`hnsw.iterative_scan = relaxed_order`.** Only `off` and `strict_order` were
  carried into the recall measurement.
* **IVFFlat centroid drift.** IVFFlat fixes its centroids at build time, so a
  corpus that grows or shifts after the build loses recall until it is rebuilt.
  That is the main standing cost of option B and nothing here measured it.

---

## Part 4 — Open questions

These are decisions, not measurements, and each one changes what gets built.

1. **Is `strict_order` enough?** It holds mean recall at 0.979 under 500 dead
   entries per query, but one query in a hundred returned nothing. Whether that
   is acceptable is a product decision about whether a retrieval miss is a
   nuisance or a correctness failure. If it is acceptable, the appliance needs no
   change and this file is a record. If it is not, options A, B and C are the
   candidates and B is the only one whose cost does not grow with the corpus.

2. **Does the test fixture get changed?** `tests/test_usetype_filter.py::_seed`
   writes seven rows carrying one identical vector inside a rolled-back
   transaction, and roughly 34 tests in that file do it, which is where the
   measured `n_dead_tup = 521` comes from. Jittering those vectors would make the
   suite deterministic. It would also remove the only place this condition is
   currently exercised. That is a trade between a green suite and a canary.

3. **Should `ivfflat.probes` be set at all?** The appliance runs the token index
   at pgvector's default of 1 over `lists = 1024`. `read`
   `repositories/search.py:874` already records that this loses most neighbours
   for a readable share at or below 5%, and `4dadb62` measured it. That is an open
   recall defect on its own terms, independent of everything in this file, and it
   has no owner.

   **Superseded by Part 5, 2026-09-10.** It now has the entry condition it lacked
   — `tests/test_maxsim_recall.py` fails on a clean tree — and the question as
   stated turns out to be the wrong one. `probes` is the second-largest term.
   The largest is that `schema.sql` builds this index before any row exists, so
   it never holds centroids fitted to the data at all: 5.4.

---

## Part 5 — The MaxSim path, through `maxsim_search` rather than beside it

Written 2026-09-10, against `sprint-0-4-0-plan` at `39d5f20`. Same server as
Part 1 — `pgvector/pgvector:pg16`, PostgreSQL 16.14, pgvector 0.8.5 — but for
the first time on the **appliance's own schema**, loaded from
`jmfts_core/sql/schema.sql` by the same DDL `jmfts-init-db` runs, holding real
`nomic-ai/modernbert-embed-base` vectors, queried through the shipped
`maxsim_search` by a real non-owner principal whose grant makes `readable_sql`
emit a fragment.

4.3 asked whether `ivfflat.probes` should be set at all and recorded that the
candidate "has no owner". Part 3 recorded the same gap from the other side:
*"Nothing here measured MaxSim end to end."* This part does. **It chooses
nothing** — 5.6 is the decision that is left, and the measurement makes it a
larger decision than 4.3 framed.

**This does not make the file a defect list.** `docs/SPRINT_0_4_0.md` Part 0's
rule holds: a defect is a numbered step in the current sprint plan whose entry
condition is a failing test. What changed is that the entry condition now
exists. No step is opened here.

### 5.1 What was missing, and why only the real path could supply it

Block A kept this a candidate in one sentence: *"the entry condition is a failing
test through `maxsim_search`, and `scripts/maxsim_recall.py` is a throwaway-table
harness rather than that test."* `STRESS_CORPUS.md` 6.2 then measured the real
index on 1.37M real rows — but with raw SQL against a scratch table, not through
the method. Three things only the shipped path can answer, and all three moved
the answer:

* **`maxsim_search` is not one ANN query.** It issues one per content token of
  the query and sums per-token maxima (`read`
  `repositories/search.py:1068`-`:1111`). Whether a 25% per-token loss becomes a
  25% document-level loss, less — several tokens vote, and a document one token
  drops another may catch — or more, does not follow from a single-vector recall
  number. Measured below: **less**. 6.2's 0.7533 per-vector at 1.37M rows is
  0.7300 per-document here at 47k, which is the same neighbourhood, so the
  aggregation neither rescues the path nor compounds the loss.
* **The shipped index is built on an empty table.** No scratch-table harness can
  see this, because building the index after loading the rows is the natural
  thing to do in a harness and the opposite of what `schema.sql` does. 5.4, and
  it is the largest term in the whole measurement.
* **The model is in the call.** `maxsim_search` embeds its query before it
  touches the index, and on CPU that dominates. Timing the method would have
  reported the model and called it the index; 5.3 separates them.

### 5.2 The fixture, and the trap it fell into first

`ran` `JMFTS_EMBEDDING_DEVICE=cpu python -m scripts.maxsim_probes --dsn ...
--chunks 600 --queries 30 --probes 1,2,5,10,25,50,100 --reindex`.

**47,242 token rows over 600 documents**, seeded in 517 s by the real model
through `EmbeddingService.embed_with_tokens` at `top_percent = 0.5` — the same
call and the same 256-dim truncation the ingest path stores. 30 queries per arm,
151 ANN pages per sweep row, `LIMIT 10` to the caller, `k = 100` per query token
(`read` `repositories/search.py:1018`). One probe of 1024 lists reaches about 46
of those rows, against that request for 100.

Ground truth is the **same `maxsim_search` call**, on the same rows, in the same
transaction, with the same query embedding and every index path denied so the
planner sorts. Every number below is therefore ANN-against-exact through one code
path, and nothing depends on a judgement about what the right answer was.

**The fixture failed Part 0's second fault before it worked.** A first version
generated clustered Gaussian vectors with a per-component spread of 0.25; in 256
dimensions that noise has norm `0.25 * sqrt(256) = 4.0` around unit-norm cluster
centres, which is isotropic noise wearing a cluster's clothes. Measured against
it the shipped index read **0.0155** and a fitted one read **0.0090** — numbers
that say nothing about IVFFlat and everything about the generator. That is why
the corpus is now topical English through the real model: the geometry is the
model's, so there is nothing left to get wrong. `tests/maxsim_corpus.py` carries
the same warning at the top.

### 5.3 What the shipped appliance gives, and what probes buys

`gated` is the granted non-owner; `open` is the owner, for whom `readable_sql`
returns None and no ACL fragment is built at all. Each arm is scored against its
own exact answer over its own readable half. `ann ms` is the summed server time of
exactly the statements `maxsim_search` issued against `token_embeddings` — the
part `probes` moves — and `call ms` is the whole method, model included. `page` is
the median rows one ANN page returned of the 100 it asked for.

**Index as `schema.sql` leaves it** (built empty, populated after — see 5.4):

| arm | probes | recall@10 | ann ms | call ms | page | short pages |
|---|---:|---:|---:|---:|---:|---:|
| gated | **1 (shipped)** | **0.7300** | 11.6 | 152.2 | 38 | 111/151 |
| gated | 2 | 0.7667 | 13.3 | 171.1 | 98 | 76/151 |
| gated | 5 | 0.8333 | 13.9 | 174.9 | 100 | 20/151 |
| gated | 10 | 0.8800 | 19.0 | 167.6 | 100 | 0/151 |
| gated | 25 | 0.9200 | 19.9 | 181.6 | 100 | 0/151 |
| gated | 50 | 0.9467 | 25.0 | 180.7 | 100 | 0/151 |
| gated | 100 | 0.9467 | 34.4 | 313.6 | 100 | 0/151 |
| open | **1 (shipped)** | **0.6033** | 9.7 | 159.4 | 71 | 82/151 |
| open | 10 | 0.7733 | 13.5 | 156.9 | 100 | 0/151 |
| open | 25 | 0.8333 | 16.0 | 242.4 | 100 | 0/151 |
| open | 50 | 0.8867 | 22.5 | 264.7 | 100 | 0/151 |
| open | 100 | 0.9400 | 33.7 | 275.8 | 100 | 0/151 |

Four readings.

* **The shipped configuration returns 0.73 of the documents it ranked**, and the
  `page` column says why: the median ANN page holds 38 rows where the statement
  asked for 100, and 111 of 151 pages came back short. This is the row
  `maxsim_recall.py` predicted from arithmetic — `lists` is a hardcoded 1024 and
  does not track table size, so below about 102,400 token rows one probe cannot
  hold the `LIMIT 100` — measured through the method for the first time.
* **The read gate is not what is lost, and the control is emphatic.** Removing
  the ACL fragment entirely does not improve recall; it makes it *worse*, 0.6033
  against 0.7300, because the ungated arm ranks the whole 600-document corpus
  while the gated one ranks the readable 300 and a smaller field is an easier
  top-10. So the gate costs nothing recoverable here. This is `4dadb62`'s finding
  reproduced on the appliance: whatever is wrong, RBAC is a bystander, and a
  harness carrying one control would have charged it.
* **`probes` works, and it is the axis 4.3 named.** 0.73 → 0.88 at 10, 0.92 at
  25, 0.9467 at 50, and nothing after that: 100 reads the same 0.9467 the whole
  probed space can supply against an index whose centroids are wrong.
* **Through `maxsim_search` the latency argument is weaker than it looks.**
  `STRESS_CORPUS.md` 6.2 priced `probes = 50` at 32× the query (0.65 ms → 21.05
  ms) and that reading is intact — `ann ms` here goes 11.6 → 25.0, a 2.2× over a
  smaller corpus. But the caller waits `call ms`, and on CPU the model pass is
  ~140 ms of it, so `probes = 50` costs 152 → 181 ms end to end, **1.19×**.
  *That qualifier is device-dependent and must not be read as general*: on a GPU,
  or behind `JMFTS_RUNNER_URL` with a warm model, the embedding shrinks and the
  ANN cost is back to being most of the call. The honest statement is that the
  ratio depends on where the vector comes from, and 6.2 measured one end of it
  while this measures the other.

### 5.4 The index is never fitted to the data, and that is the largest term

> **Superseded by §5.8 — which is what this section argued for. Banner added 2026-09-15**,
> because §5.6 and §5.7 carry one and this, the section a reader is most likely to act on,
> did not. Migration 022 replaced `idx_token_embed_256_ivf` with `idx_token_embed_256_hnsw`;
> the schema no longer creates an IVFFlat index over `token_embeddings`, so a schema load
> produces none of the NOTICE below. **The finding is why the index changed, and the
> argument is the whole point of the section.** Read it as the diagnosis, not as the state.
>
> One incidental: `git grep -n "REINDEX" -- jmfts_core` now returns two hits, both comments
> quoting this very claim, in `022_token_embed_256_hnsw.sql` and `sql/schema.sql`.

`read` `sql/schema.sql:514`-`:515`. `CREATE INDEX idx_token_embed_256_ivf` sits in
the schema file, so `jmfts-init-db` builds it **before a single row exists**.
IVFFlat fixes its centroids at build time. Nothing in `jmfts_core/` ever rebuilds
it — `ran` `git grep -n "REINDEX" -- jmfts_core`, which returns nothing.

pgvector says so itself, at that moment. `ran` the schema load against an empty
database:

```
psql:jmfts_core/sql/schema.sql:515: NOTICE:  ivfflat index created with little data
DETAIL:  This will cause low recall.
HINT:  Drop the index until the table has more data.
```

**Every prior measurement of this index, including `STRESS_CORPUS.md` 6.2 and
`scripts/maxsim_recall.py`, built it after loading its rows** — which is the
favourable order and the one the appliance never reaches. The `--reindex` arm is
what that difference is worth. Same corpus, same queries, same transaction; the
only change is `REINDEX INDEX idx_token_embed_256_ivf`, which took **15.7 s** on
47,242 rows:

| arm | probes | recall@10, as shipped | recall@10, REINDEXed | ann ms, REINDEXed |
|---|---:|---:|---:|---:|
| gated | **1 (shipped)** | **0.7300** | **0.9300** | 8.5 |
| gated | 2 | 0.7667 | 0.9467 | 8.7 |
| gated | 5 | 0.8333 | 0.9733 | 10.7 |
| gated | 10 | 0.8800 | 0.9767 | 11.6 |
| gated | 25 | 0.9200 | 0.9933 | 13.9 |
| gated | 50 | 0.9467 | 0.9967 | 16.4 |
| gated | 100 | 0.9467 | 1.0000 | 20.0 |
| open | **1 (shipped)** | **0.6033** | **0.9067** | 6.6 |
| open | 5 | 0.6833 | 0.9867 | 8.8 |
| open | 10 | 0.7733 | 0.9967 | 8.7 |
| open | 25 | 0.8333 | 1.0000 | 9.7 |
| open | 50 | 0.8867 | 1.0000 | 11.1 |

* **One `REINDEX` moves the shipped default from 0.7300 to 0.9300**, gated, with
  no GUC set and no index type changed. Ungated it moves 0.6033 to 0.9067. At
  this corpus size that single statement clears the threshold the test asserts,
  on its own.
* **It is also FASTER**: `ann ms` at `probes = 1` falls 11.6 → 8.5, because a
  fitted cell holds the neighbours and an unfitted one holds rows that then have
  to be discarded.
* **The `page` column inverts, and reading it wrongly would be easy.** REINDEXed
  at `probes = 1` the median page is 16 rows and 151 of 151 are short, against
  38 rows and 111 short as shipped — a *shorter* page at *higher* recall. Sixteen
  right rows beat thirty-eight wrong ones. This is 1.5's point arriving from a
  new direction: `AppliedFilters.truncated` does not mean what a policy would want
  it to mean, and here it is close to anti-correlated with recall.
* **REINDEX is not a fix, and 6.2 is why.** A fitted index at 1.37M rows still
  read **0.7533** at `probes = 1` (6.2 built it after loading). At 47k rows each
  of 1024 lists holds ~46 rows and one probe is enough; at 1.37M it holds ~1338
  and one probe is a 7.5% sample of a k-means cell. So the two regimes fail for
  two different reasons, `probes` is the term that survives into the large one,
  and neither number alone describes the appliance.
* **Centroid drift, which Part 3 lists as unmeasured, is the standing cost of any
  REINDEX policy** and is still unmeasured. A rebuild is correct for the corpus
  it saw.

### 5.5 The entry condition

`tests/test_maxsim_recall.py`, `ran` against a clean `jmfts_test` on
`pgvector/pgvector:pg16`:

```
tests/test_maxsim_recall.py::test_the_fixture_reaches_the_index PASSED
tests/test_maxsim_recall.py::test_a_shortfall_here_is_never_explained_by_the_read_gate PASSED
tests/test_maxsim_recall.py::test_maxsim_returns_the_documents_it_ranked FAILED
    maxsim_search returned 0.4542 of the documents an exact scan ranks in its top 10,
    against a threshold of 0.9.
      corpus         15682 token rows over 200 documents
      ivfflat.probes 1 — pgvector's default, and nothing in jmfts_core issues a SET
      recall@10      0.4542 gated, 0.6208 ungated
      degraded       24/24 queries
```

1 failed, 2 passed in 107.80s. Two runs against two separate provisionings of
`jmfts_test` read the same 0.4542 gated and 0.5917 then 0.6208 ungated, so 1.4's
warning applies to the second column and the margin to 0.90 is what carries the
assertion, not the fourth decimal.

Three tests, and two of them exist to stop the third being believed for the wrong
reason. `test_the_fixture_reaches_the_index` fails if the planner chose a sort,
because a recall number off a sequential scan would assert the defect *absent* —
this is the `plan` column `maxsim_recall.py` tells its reader to check first, as
an assertion. `test_a_shortfall_here_is_never_explained_by_the_read_gate` is the
`open` control, written as an implication so that it does not itself go red on the
day the shortfall is fixed.

**The threshold is 0.90 and the file argues for it rather than asserting it.** Not
1.0: ANN is approximate by construction and 6.1 records a clean HNSW index on real
vectors at 0.9767. Not 0.5: `4dadb62` measured 0.51 and that is recorded as a
defect, not as a specification. 0.90 is where a caller's reading of the result
stops being wrong, and it is reachable three separate ways already measured —
`probes = 25` as shipped (0.9200), `REINDEX` alone at the shipped `probes = 1`
(0.9300), and HNSW at identical latency (6.2, 0.9667). It therefore prejudges
none of them.

The corpus is 200 chunks rather than 600 because the test has to run in a suite;
the shortfall is larger there (0.4542, one probe reaching ~15 rows) than at 47k
(0.7300) or at 1.37M (0.7533, 6.2). **Three corpus sizes spanning two orders of
magnitude all fail the same threshold**, which is what makes 0.90 an assertion
about the appliance rather than about one fixture.

### 5.6 The decision that is left

> **Answered in 5.8 on 2026-09-11: row 4, HNSW for `embed_256`.** This section is kept
> as the statement of the choice and the four costs, which is what 5.8 decides against.
> Read its "None of these is chosen here" as true of 5.6 rather than of the document.

One decision, and it is now a four-way choice rather than 4.3's yes/no.

**Which of these does the appliance adopt, and does the answer have to hold at
both corpus sizes?**

| | what it costs | what it is worth | what it does not fix |
|---|---|---|---|
| Set `ivfflat.probes` | 2.2× `ann ms`, 1.19× `call ms` on CPU, worse on GPU | 0.73 → 0.9200 at 25 | nothing at 1.37M was measured through `maxsim_search` |
| `REINDEX` after ingest | 15.7 s at 47k rows, unmeasured above that | 0.73 → 0.9300 at `probes = 1`, and *faster* | drift; and 6.2 says a fitted index still reads 0.7533 at 1.37M |
| Make `lists` track table size | a rebuild, same as above | unmeasured here | same drift question |
| HNSW for `embed_256` | 2.7× build, 46% disk (6.2) | 0.9667 at identical latency (6.2) | brings the dead-tuple failure mode of Parts 1–2 onto this column |

Three sub-questions the table does not settle, stated because each one changes
which row wins:

1. **Is the empty build a defect in its own right, separate from `probes`?** It is
   the largest term measured and the cheapest to address, and nothing in the
   repository records it before this part. It is also the only one of the four
   that requires no decision about retrieval quality — pgvector's own NOTICE
   already calls the current state low-recall.
2. **Does a policy own the rebuild, or does an operator?** A `REINDEX` on the
   ingest path pays 1.8's asymmetry in reverse (IVFFlat vacuums and rebuilds
   cheaply, which is option B's whole argument), but "after ingest" has no
   definition while ingest is a queue that never ends.
3. **At 1.37M rows, is `probes` still the answer?** 6.2 says a fitted index reads
   0.7533 at one probe and 0.9433 at ten. Nothing here reached that size through
   `maxsim_search`, and 5.3's latency qualifier is exactly the one that stops
   being generous as the corpus grows.

**None of these is chosen here, and 4.3's reservation stands:** setting the GUC in
`jmfts_core/` and changing the index type are the owner's. What this part supplies
is that the choice can now be made against a measurement and a failing test rather
than against an argument.

### 5.7 HNSW does not have a build order, and that is what separates the fourth row

5.6's table gives the HNSW row a build cost, a disk cost and a failure mode it
inherits from Parts 1–2. It does not say whether HNSW shares the defect 5.4 names,
and 5.6's sub-question 1 — "is the empty build a defect in its own right?" — cannot
be answered for the fourth row without that. This measures it.

**Synthetic corpus, and the limit that puts on the numbers.** `ran` against a
throwaway `pgvector/pgvector:pg16`: 50,000 `halfvec(256)` rows drawn around 400
cluster centres at spread 0.35, 100 queries drawn the same way, recall@10 against a
brute-force scan of the same table with `enable_indexscan` and `enable_bitmapscan`
off. **This corpus is more cleanly clustered than real token embeddings and every
absolute number below is therefore optimistic** — 5.4's 0.7533 at 1.37M rows is the
better figure for how a fitted IVFFlat actually reads. What survives the fixture is
the CONTRAST between two build orders at a fixed `lists` and `probes`, because
nothing else differs between the pair.

| index | build order | setting | recall@10 | ms/query |
|---|---|---|---:|---:|
| IVFFlat `lists=37` | built empty | `probes=1` | 0.603 | 0.7 |
| IVFFlat `lists=37` | built on data | `probes=1` | **0.998** | 0.6 |
| IVFFlat `lists=223` | built empty | `probes=1` | 0.425 | 0.3 |
| IVFFlat `lists=223` | built on data | `probes=1` | **0.998** | 0.2 |
| IVFFlat `lists=1024` | built empty | `probes=1` | 0.404 | 0.3 |
| IVFFlat `lists=1024` | built on data | `probes=1` | **0.693** | 0.3 |
| HNSW | built on data | `ef_search=40` | 0.998 | 0.4 |
| HNSW | **built empty, rows inserted after** | `ef_search=40` | **0.987** | 0.4 |
| HNSW | built empty, rows inserted after | `ef_search=100` | **0.998** | 0.4 |

**`lists=37` is the row that mirrors the appliance and the other two do not.**
`STRESS_CORPUS.md` 6.2 reads `token_embeddings` at 1,370,494 rows, and
`sql/schema.sql:515` sets `lists = 1024` — 1,338 rows per list, which is near
<!-- `lists` does not appear in `sql/schema.sql` at all since migration 022 (2026-09-15).
     The paragraph's arithmetic is what justified the `lists=37` fixture and is unaffected;
     its premise is stated as a current fact and is not one. See 5.8. -->
pgvector's own guidance. At 50,000 rows `lists = 1024` is 49 rows per list, so the
0.404 in that row is arithmetic about the fixture rather than a reading about the
shipped configuration. `lists = 37` reproduces the appliance's ratio at this N.

* **The empty build costs 0.603 against 0.998 at the appliance's ratio, at equal
  latency.** That is 5.4's finding reproduced at the rows-per-list the appliance
  actually runs, where 5.4 measured it at 47k rows and 1024 lists.
* **HNSW built on an empty table and then filled reads 0.987, against 0.998 built
  on the data.** The gap closes entirely at `ef_search = 100`. An HNSW graph is
  constructed by insertion in both orders, so "built empty" is not a different
  index; IVFFlat fixes its centroids at `CREATE INDEX` and cannot be.
* **That is the property 5.6's table has no column for.** Rows 2 and 3 — `REINDEX`
  after ingest, and `lists` tracking table size — are both repairs that somebody has
  to remember to run, and 5.6's sub-question 2 is the observation that "after
  ingest" has no definition while ingest is a queue that never ends. Row 4 does not
  need the definition. This does not make row 4 correct; the dead-tuple failure mode
  of Parts 1–2 is real and Part 2 E's `iterative_scan` mitigation is already
  applied on the other column. It makes row 4 the only one that is not
  conditional on a policy nobody has written.

**What HNSW costs on the write path, measured here rather than inherited from 6.2.**
6.2's "2.7× build" compares a bulk HNSW build against a bulk IVFFlat build. The
appliance does neither: it inserts through a live index. `ran`, same 50,000 rows:

| | seconds |
|---|---:|
| `CREATE INDEX … USING hnsw` on rows already present | 11.8 |
| `COPY` of the same rows into a live HNSW index | 48.2 |

**4.1×, and the comparison it invites is the wrong one.** A bulk build is not
available to a queue that never ends, so the number that would settle the write-path
cost is IVFFlat's incremental insert over the same rows, and this run did not take
it. Recorded as not measured rather than estimated.

**Nothing here is chosen either.** 5.6's reservation stands unchanged. What 5.7 adds
is one fact: of the four rows, exactly one has no build-order term, and it is the
row 5.6 priced only on build cost and disk.

> **5.8 is the choice, taken 2026-09-11 on this fact.** It also re-measures this
> section's central claim on the appliance's own tables and real vectors rather than
> on the synthetic corpus above, and reads 0.9167 built empty against 0.9208 rebuilt —
> so the caveat at the top of 5.7 did not turn out to be hiding anything.

### 5.8 The decision, and the control that says it was the right one

5.6 left a four-way choice and reserved it. It is made: **`embed_256` is an HNSW
index** (`sql/schema.sql:537`, migration `022_token_embed_256_hnsw.sql`), and
`maxsim_search` sets `hnsw.iterative_scan = strict_order` the way `vector_search`
already did.

**What decided it was 5.7's property, not a recall number.** All four rows of 5.6's
table clear the 0.90 threshold `tests/test_maxsim_recall.py` asserts. Three of them
clear it only while somebody remembers to rebuild the index, and 5.6's sub-question
2 is that "after ingest" has no definition while ingest is a queue that never ends.
The fourth needs nobody to remember anything.

**`ran` on the appliance's own tables**, through the real `maxsim_search`, 200
chunks of real `nomic-ai/modernbert-embed-base` vectors, 24 queries per arm, ground
truth from the same call with every index path denied
(`JMFTS_EMBEDDING_DEVICE=cpu python -m scripts.maxsim_probes --dsn ... --chunks 200
--queries 24 --reindex`, pgvector 0.8.5 on `pgvector/pgvector:pg16`):

| index state | arm | `ef_search` | recall@10 | ann ms | short pages |
|---|---|---:|---:|---:|---:|
| **as shipped** | **gated** | **40 (pgvector's default)** | **0.9167** | 10.3 | 0/119 |
| as shipped | gated | 10 | 0.8792 | 11.0 | 0/119 |
| as shipped | gated | 100 | 0.9333 | 10.4 | 0/119 |
| as shipped | gated | 400 | 0.9958 | 16.4 | 0/119 |
| as shipped | open | 40 | 0.9500 | 7.5 | 0/119 |
| **REINDEXed after load** | **gated** | **40** | **0.9208** | 11.5 | 0/119 |
| REINDEXed after load | open | 40 | 0.9542 | 9.3 | 0/119 |

**The first row is the finding: 0.4542 → 0.9167 on the same fixture, with no setting
changed by the caller.** 5.5's entry-condition run is the number it replaces.

**The `--reindex` rows are the control, and they are why this part exists rather
than just 5.7.** 5.7 measured "HNSW has no build order" on a synthetic corpus and
warned that every absolute number there was optimistic. Here the same claim is
checked on the appliance's own tables and the model's own geometry: **0.9167 built
empty against 0.9208 rebuilt on the loaded rows, which is one document out of 240.**
Under IVFFlat this arm was the whole defect — 5.4 measured the same control moving
recall 0.73 → 0.93. It now moves nothing, which is the property being bought.

**Three readings that are not the headline, and one that is a caveat.**

* **`ef_search` below the statement's `LIMIT` is not free.** `maxsim_search` issues
  `LIMIT 100` per query token, and `ef_search = 10` still reads lower than 40
  (0.8792 against 0.9167). A candidate budget smaller than the LIMIT is therefore
  not silently raised to it. Nothing sets this GUC and nothing should; the row is
  here so the next reader does not assume the clamp.
* **`AppliedFilters.truncated` fired on nothing.** 0 of 119 ANN pages came back
  short of `k_per_token`, in both arms, at every setting. 1.5 measured that same
  signal firing on 44 of 51 pages under the old index. Block A step 3 is not made
  redundant by this — it is the residue detector, and the residue is now empty on
  this corpus.
* **The gated arm is below the open arm at every setting** (0.9167 against 0.9500 at
  the default), so the read gate still costs something. It is no longer the
  difference between a page and no page, which is what Block A measured before the
  scan mode was applied to this path.
* **The margin to 0.90 is four documents out of 240, and that is a property of the
  fixture rather than of the appliance.** 200 chunks is a small graph and `limit =
  10` over 200 documents is a narrow target; 6.2 read 0.9667 on the appliance's real
  1.37M rows. 1.4's warning about stability applies to this column and the honest
  statement is the margin, not the fourth decimal. If a suite run reads below 0.90
  here, the fixture is the first thing to check and `CHUNKS` is the knob.

  > **It happened on 2026-09-12, and it was neither of those.** The full suite failed on
  > `assert 0.8999999999999999 >= 0.9` — the margin is so thin that float summation lands a
  > few ULP under the constant. The fix was a `RECALL_FLOOR` of `0.8999999999999996 - 1e-15`
  > beside `MINIMUM_RECALL = 0.90` in `tests/test_maxsim_recall.py`, and the assertions
  > compare against the floor. So **check the arithmetic before the fixture**: a reading of
  > 0.8999999999999999 is 0.90. The advice above is right for a reading that is actually
  > below it. Recorded 2026-09-15.

**What it cost.** From 6.2, on 1,370,494 real token rows: 2.7× build time (55.9 s
against 20.6 s) and 46% more disk (1050 MB against 720 MB, on a table already
1672 MB). Query latency was unchanged there — 0.65 ms both — and the `ann ms` column
above is dominated by 24 queries' worth of per-token statements rather than by the
index, so it is not a second reading of that.

**What it brings, and it is named rather than discovered later.** `token_embeddings`
has no `settled` column, so this index cannot be partial the way `idx_documents_embed`
is, and `repositories/document.py:1069` deletes every token row for a document and
rewrites them on each re-embed — 113,313 dead against 1,370,494 live on the reference
corpus (6.3). That is Parts 1–2's regime, arriving on this column for the first time.
Two consequences follow and neither is speculative:

1. `strict_order` on this path is load-bearing, not decoration. 1.7 measures
   pgvector's default scan mode at recall 0.100 under 50 dead entries per query
   point and this line holding 0.979 at 500. It is a mitigation and not immunity: at
   that dose 3 of 100 queries were still degraded and one returned nothing.
2. HNSW vacuums 189× slower than IVFFlat on real vectors, and 6.3's operationally
   useful reading is that the cost is invisible under default autovacuum and arrives
   in full after a bulk delete — which a re-ingest is.

**What this does not settle.** Whether `hnsw.ef_search` should be raised from
pgvector's default. Nothing in `jmfts_core/` sets it, the table above says 400 buys
0.9958 for about 1.6× the ANN time on this fixture, and no measurement of that trade
exists at 1.37M rows through `maxsim_search`. That is the same shape as the question
this part closes, and it gets the same treatment: it is not a defect until a failing
test says so.
