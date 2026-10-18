# What is under `tests/test_maxsim_recall.py`'s 0.90 threshold

`docs/SPRINT_0_6_0.md` step 8, question 4.4. Measured 2026-09-15 with
`scripts/measure_maxsim_margin.py` against a throwaway `pgvector/pgvector:pg16` on
port 5496 — postgres 16.15, pgvector 0.8.6, `JMFTS_EMBEDDING_DEVICE=cpu`, which is the
image and the device `.github/workflows/ci.yml` runs the suite on.

> **Reviewed for publication 2026-10-12, after the fact.** This record was committed on
> 2026-09-15 (`b9fcfee`) outside `SPRINT_0_6_0.md` Block E's pass and has been on the public
> remote since, so the review could not decide whether to publish it — only whether it says
> anything it should not. Read for Block E's three things: no private host name or path (the
> database is a throwaway container on a loopback port), no named third party, and no claim
> about the tree that has gone false — `tests/test_maxsim_recall.py`, `RECALL_FLOOR`,
> `scripts/measure_maxsim_margin.py`, `jmfts_core/repositories/search.py` and
> `.github/workflows/ci.yml` have no commits after it. Nothing was changed below this note.

> **Read again 2026-10-18, on 0.7.0 (`0e6027b`) plus the reporting change, and the gate now
> prints what it measured.** Every run of `tests/test_maxsim_recall.py` ends with a
> `readings` section — gated and open hits out of 240, the margin over 216 in documents,
> the token-row count, and one digit per query for the neighbours it lost — and
> `JMFTS_RECALL_REPORT=<path>` appends the same as one JSON line. Three whole-process runs,
> `run_tests_docker.sh`, port 5516, 15,682 token rows each, all 7 tests green:
>
> | run | gated | margin | open | lost per query, gated | wall-clock |
> |---:|---:|---:|---:|---|---:|
> | 1 | 218/240 | +2 | 226/240 | `210021000131001122012011` | 126 s |
> | 2 | 222/240 | +6 | 230/240 | `210021000100001122012011` | 280 s |
> | 3 | 217/240 | +1 | 228/240 | `210031000111001122012400` | 418 s |
>
> The gated readings sit inside the 217–224 band of Part 2, run 3 on its floor. **Run 1's
> open arm read 226/240, which is below finding 1's "never gone below 227/240"**; that
> sentence was true of the 79 readings it counts and is not true of this tree's 82. The
> gated arm still cleared the floor, so the control was not exercised. The eight queries
> 3.1 found short in all 60 builds are short in all three; run 3's 4 at position 22 is on
> a query 3.1 found short in 1 of 60. Wall-clock grew with host load (1-minute load 4.6 before
> run 2, 12.3 before run 3, other sessions live) and is not a reading of the fixture's cost.

4.4 named three accounts of the reading that failed public CI and said the one thing
nobody had done was to **run the fixture enough times to get a distribution rather than a
reading**. This is that distribution: **79 readings**, and no threshold was moved to fit
any of them.

**This is a measurement. It changes no code in `jmfts_core`, no test, and no threshold.**

---

## The answer, before the arithmetic

**Four findings, and the third is the one that decides the step.**

1. **79 readings, none below 0.90.** Sixty HNSW graphs over one corpus read
   `216/240` to `224/240`; nineteen whole-process runs read `217/240` to `224/240`.
   Combined: mean `220.19/240 = 0.9175`, sd **1.67 documents**. The lowest reading in
   the whole sample is exactly `216/240 = 0.9000`, which `RECALL_FLOOR` admits and a
   bare `>= 0.9` would have decided by accumulation order — the tie 0.5.1 fixed, drawn
   once in 79.
2. **The shortfall is mostly a fixed property of the queries, not a lottery on the
   graph.** Of 24 queries, **8 lose a neighbour in all 60 builds and 5 lose one in
   none**. Between-query variance is 0.00513 and the residual is 0.00099 — the query
   matters five times more than the graph.
3. **The 24 queries behave as independent draws, so the standard error is
   `1/sqrt(n)`-reducible and account 1 is arithmetic rather than hope.** Observed sd of
   the trial mean is 0.0067 against 0.0064 predicted from the residual alone, a ratio of
   **1.05**: there is no shared good-build/bad-build term. At 24 queries 0.90 sits 2.75
   se below the fixture's mean; at 96 it sits 5.49 se below.
4. **The reading that failed CI, `215/240`, did not occur here and the local
   distribution makes it a 1-in-400 event.** That is not comfortable and it is not
   proof of anything. **The CI condition was not reproduced** — see Part 4, which says
   what was closed, what is left, and why the honest verdict on account 2 is *not
   needed rather than refuted*.

**What each account needs, and what the data says.**

| Account (4.4) | Verdict | Work it implies |
|---|---|---|
| The corpus is too small | **Supported, and the knob is `QUERIES`, not `CHUNKS`** | Raise `QUERIES` from 24; Part 3 prices 48/96/192 |
| The ANN index is losing a neighbour under a condition CI has | **Not needed; two channels closed, one open** | Part 4.4 — make the failure say which pgvector it measured |
| 0.90 is the wrong number | **Wrong as a gate at n=24, right as a statement** | Nothing, if `QUERIES` moves; see Part 5 |

**Do not move the threshold.** It is 2.75 se below a mean of 0.9176 because the mean is
measured with 24 draws, not because 0.90 says the wrong thing about what a caller may
assume. Shrinking the standard error fixes the gate and leaves the claim intact; moving
the number fixes the gate by withdrawing the claim.

---

## Part 0 — What is measured, and the sampling unit

`tests/test_maxsim_recall.py` seeds `CHUNKS = 200` chunks of generated topical prose
through the real model into the appliance's own `documents` and `token_embeddings`,
asks `maxsim_search` for the top `LIMIT = 10` documents for each of `QUERIES = 24`
queries, and compares against the same call with every index path denied. Two arms: a
non-owner principal whose grant makes `readable_sql` emit a fragment (`gated`), and the
owner, for whom none is built (`open`).

**The sampling unit is one (arm, index build): the mean of 24 per-query `recall@10`
values.** Each per-query value is a multiple of 1/10, so the unit is a multiple of
1/240 and this document reports it as an integer count of documents out of 240. That is
not a presentation choice — `RECALL_FLOOR` exists because the float rendering of that
integer can land on either side of 0.90.

The fixture is 100 readable chunks under one access-control root and 100 unreadable ones
under another, **15,682 token rows over 200 documents** at every reading below.

`tests/test_filtered_recall.py` does **not** share this fixture, and `SPRINT_0_6_0.md`
step 8's brief says `:19` claims it does. `:19` says steps 2 and 3 of `SPRINT_0_4_0.md`
Block A are prerequisites of step 1; that file builds its own exact, planted-cluster
corpus over `documents.embed` and loads no model at all. `tests/maxsim_corpus.py` is
imported by exactly two files — `tests/test_maxsim_recall.py` and
`scripts/maxsim_probes.py` — and `scripts/measure_maxsim_margin.py` is now the third.
**Reported rather than corrected: `SPRINT_0_6_0.md` is published and Part 5.3 is the
gate.**

## Part 1 — What cannot vary, and what that leaves

Account 2 says the index loses a neighbour "under a condition CI has and this machine
does not". Before sampling anything it is worth knowing which inputs could carry such a
condition, because most of them cannot.

### 1.1 The corpus and the queries are two fixed integers

`tests/maxsim_corpus.py` generates every chunk and every query from `CORPUS_SEED` and
`QUERY_SEED` through `random.Random`. Same bytes on every machine, every run.

### 1.2 The vectors are machine-independent at storage precision

`nomic-ai/modernbert-embed-base` on CPU, embedded at 2, 4 and 8 torch threads in four
separate processes: **the same 82 token rows and the same vector components to every
digit printed**. That is more robust than it looks, because two quantisations sit
between the model and the comparison:

* `seed_corpus` renders each component with `f"{v:.6f}"` before the `INSERT`, so
  anything below 5e-7 is gone before postgres sees it;
* the column is `halfvec(256)` — IEEE half precision, about 11 bits of mantissa — so the
  stored value resolves to roughly 5e-4 relative. The query side is cast to the same
  type by the `<=>` operator.

A different CPU would have to move a component by four orders of magnitude more than
CPU-level floating-point differences do before a single stored number changed.

### 1.3 The index is the same index

`pgvector/pgvector:pg16` is the image CI's service container runs and the image
`scripts/run_tests_docker.sh` starts. Local: postgres 16.15, pgvector 0.8.6. Built by
`schema.sql` on an empty table, which is the shipped build order and the one
`ANN_INDEX_HEALTH.md` 5.7 priced against the alternative.

### 1.4 What is left is the graph

pgvector draws each element's level from the backend PRNG at insert, so every build over
the same rows is a different graph. `ANN_INDEX_HEALTH.md` 1.4 recorded the consequence in
2026-09-07's words — *"the same cell at the same parameters has read 6/20 and 15/20
across sweeps. A single run measures nothing here."* **That is the one term this
measurement resamples**, and `scripts/measure_maxsim_margin.py` resamples it the way the
appliance produces it: `TRUNCATE` and re-`INSERT` the same rows through the live index,
never `REINDEX`, because a rebuild is a different construction (5.7: 0.987 against
0.998).

That also lets the model run once. Ground truth is taken once for the same reason and a
stronger one — it is an exact scan with every index path denied, so no graph can move
it. `--verify` recomputed all 48 (arm, query) truth lists after the sixtieth trial and
**none had changed**, which is the check that the shortcut is sound rather than the
assumption that it is.

## Part 2 — The distribution

### 2.1 Sixty graphs, one corpus, one process

`JMFTS_EMBEDDING_DEVICE=cpu python -m scripts.measure_maxsim_margin --dsn … --trials 60
--verify`

| arm | min | max | mean | median | sd |
|---|---:|---:|---:|---:|---:|
| **gated** | **216/240** | **224/240** | **220.23** (0.9176) | 220 | **1.61** |
| open | 227/240 | 232/240 | 228.85 (0.9535) | 229 | 1.06 |

```
gated/240   recall   trials
      216   0.9000   #                 1
      217   0.9042   #                 1
      218   0.9083   ######            6
      219   0.9125   ############     12
      220   0.9167   ##############   14
      221   0.9208   #############    13
      222   0.9250   ########          8
      223   0.9292   ####              4
      224   0.9333   #                 1
```

Symmetric, unimodal, nine of the ten achievable values between the extremes occupied.
**0 of 60 below 0.90.** The single `216/240` is exactly 0.90: the reading `RECALL_FLOOR`
was written for, drawn once.

### 2.2 Nineteen whole-process runs

A rebuild trial holds the embeddings fixed. A process run does not — it reloads the
model, re-embeds all 200 chunks and re-provisions the database — so the two together say
whether anything outside the graph moves the number.

| source | n | readings |
|---|---:|---|
| 2026-09-12/13, recorded in `CHANGELOG.md` under 0.5.1 | 12 | 217, 217, 218, 218, 219, 220, 220, 220, 221, 222, 222, 224 |
| 2026-09-15, this step, file alone | 5 | 218, 220, 220, 221, 221 |
| 2026-09-15, this step, inside a full `pytest tests/` | 2 | 221, 222 |

min 217, max 224, mean 220.05, sd 1.87. **The process runs and the rebuild trials do not
describe distinguishable distributions** at these sample sizes — 220.23 ± 1.61 over 60
against 220.05 ± 1.87 over 19 — which is the evidence that re-embedding the corpus adds
nothing the graph was not already contributing. It is not a proof that it adds exactly
nothing; 19 readings cannot carry that.

### 2.3 All 79

| | min | max | mean | sd | below 0.90 |
|---|---:|---:|---:|---:|---:|
| gated | 216/240 (0.9000) | 224/240 (0.9333) | 220.19 (**0.9175**) | 1.67 | **0 of 79** |

Public CI read `215/240 = 0.8958` on 0.5.0. That is **3.1 sd** below this mean; a normal
approximation puts it at about **1 in 400** per run. It did not occur in 79 draws.

### 2.4 The control says what it has always said

The ungated arm was above the gated arm in all 60 trials and in every process run, at a
mean of 228.85/240 = 0.9535 against 220.23/240. The read gate costs about nine documents in 240 and
it is not the account of the shortfall; `ANN_INDEX_HEALTH.md` 5.8 read 0.9500 against
0.9167 for the same pair. **The ungated arm has never gone below 227/240**, so the
implication in `test_a_shortfall_here_is_never_explained_by_the_read_gate` has never been
exercised on this tree — it would fire only on a gated reading below the floor, and there
has not been one here.

## Part 3 — The shortfall is structural, and that is why more queries works

### 3.1 The same queries lose the same neighbours

Trials (of 60) in which each of the 24 gated queries lost at least one document:

```
60 60  0  1 60 52  0  0  5 60 51 52  2  0 55 59 60 60  0 60 60  1 59 55
```

**8 queries are short in all 60 builds. 5 are short in none. 11 move.** Fourteen or
fifteen queries are short in a typical build, and about eight of those were going to be
short whatever graph was drawn. The mean reading of 220/240 is therefore not twenty
documents lost at random; it is a stable deficit with a small stochastic skin on it.

This is the fixture, and it is what `ANN_INDEX_HEALTH.md` 5.8 said in its own words —
*"the margin to 0.90 is four documents out of 240, and that is a property of the fixture
rather than of the appliance"*. What the 60 builds add is that the property is
**reproducible**: 0.9176 is where this fixture sits at the shipped `hnsw.ef_search`.

### 3.2 The variance decomposition

Writing `recall[trial][query] = mu + query_effect[query] + residual`, over 60 × 24
gated readings:

| term | variance | sd |
|---|---:|---:|
| between-query (the same in every build) | 0.00513 | 0.0716 |
| residual (build × query) | 0.00099 | 0.0315 |

The query effect is fixed across builds and therefore contributes **nothing** to the
run-to-run spread of the mean. Only the residual does:

| | sd of the trial mean |
|---|---:|
| predicted from the residual, if residuals are independent across queries | 0.0064 |
| observed | **0.0067** |
| ratio | 1.05 |

**There is no common per-build term.** A build is not globally good or globally bad; it
loses a neighbour here and gains one there, independently. That is the assumption account
1 needs, and it is measured rather than assumed.

### 3.3 What `QUERIES` buys

`se = sqrt(residual variance / n)`, which the ratio above licenses:

| `QUERIES` | se of the mean | 0.90 is … below the mean | fixture cost |
|---:|---:|---:|---|
| 24 (today) | 0.0064 | **2.75 se** | see below |
| 48 | 0.0045 | 3.88 se | |
| 96 | 0.0032 | **5.49 se** | |
| 192 | 0.0023 | 7.77 se | |

**The cost is sublinear in `QUERIES` and the exact factor was not measured.** The
expensive half of the fixture is seeding 200 chunks through the model, and `QUERIES` does
not touch it: this harness paid that cost once and then ran 60 trials on it. What
`QUERIES` scales is the search half — per query per arm, one `maxsim_search` for truth
with every index path denied and one for the arm, each embedding the query once and
issuing one statement per content token of it. The file runs in 107 s
alone on an unloaded machine; the split between the two halves was not taken, so the
column above is deliberately empty rather than estimated.

**One thing this table assumes and does not measure.** It holds the population mean at
0.9176 while `n` grows. `query_texts` draws four terms from one topic's vocabulary, round
robin over the same 24 topics, so queries 25 to 96 are more queries of the same kind —
but whether their mean recall is the same 0.9176 is an extrapolation from 24 draws, not a
reading. Anyone raising `QUERIES` should take the new mean before trusting the new
margin.

## Part 4 — The CI condition, and what could not be reproduced

**Stated plainly: public CI was not reproduced. No reading in this document was taken on
a hosted runner.** What follows is which channels between "CI" and "this machine" were
closed by measurement and which are still open.

### 4.1 Closed: the inputs

Part 1.2. The corpus, the queries and the vectors are the same bytes on any machine the
model runs on, and two quantisations stand between CPU-level floating point and the
stored value.

### 4.2 Closed: the suite context

CI runs `pytest tests/ -q` — the whole suite in one process — and every local reading
before this step was the file on its own. That is a real difference, and
`ANN_INDEX_HEALTH.md` 1.4's 2026-09-10 amendment attributes a *different* intermittent
failure (`tests/test_usetype_filter.py`) to "how many rows the suite has already written
to `token_embeddings`". So it had to be checked.

**It is not a channel for this fixture.** Measured inside two full-suite runs, at the
moment the `measured` fixture finished, in both: `token_embeddings` held **15,682 rows**
and `documents` **202** — the fixture's own 200 chunks and its two roots, and nothing
else. The suite leaves nothing committed that this measurement can see, and it runs late
in an alphabetical collection, so most of the suite had already run. The readings were
`221/240` and `222/240`, inside the distribution. (One run took 51 minutes and failed one
unrelated test, `test_web_distribution.py::test_the_appliance_starts_with_no_front_end_installed`;
the other passed 2917.)

### 4.3 Not a channel: machine load

Most of the readings here were taken on a machine running four other agents' test suites,
at a load average of about 20 on 8 cores; the wall times in this document are three to
fifteen times what the same work costs alone, and a full-suite run read 51 minutes loaded
against 15 minutes unloaded. **The readings do not separate that way.** The one run taken
on an idle machine read `220/240`, which is the mode of the loaded sample, and the loaded
readings span 216 to 224 around it. Recall here is a function of the graph and the rows
and nothing about how long a statement waited for a core — stated because the wall times
below look wrong beside `ANN_INDEX_HEALTH.md` 5.8's, not because load was measured as a
variable.

### 4.4 Open, and the one with a mechanism: which pgvector answered

`maxsim_search` issues `SET LOCAL hnsw.iterative_scan = strict_order` **only if**
`_hnsw_iterative_scan_available(self.session)` says the server has it, and
`ANN_INDEX_HEALTH.md` 5.8 records that setting as load-bearing on this path rather than
decoration — 1.7 measured pgvector's default scan mode at recall 0.100 under a dead-entry
dose that `strict_order` held at 0.979. `pgvector/pgvector:pg16` is a **mutable tag**.
This measurement ran on 0.8.6; the image CI pulled on 2026-09-12 is not recorded
anywhere, and neither is the one behind the twelve runs in `CHANGELOG.md`.

**So the single most useful thing that could be done about account 2 costs nothing and is
not a threshold change: make the failure say which index it was measured on.**
`_diagnosis` in `tests/test_maxsim_recall.py` already prints `hnsw.ef_search`,
`hnsw.iterative_scan`, the token-row count and the index name at the point of failure. It
does not print `server_version` or the `vector` extension version, and if it had, the
0.5.0 CI failure would have said whether it was measured on the same index this document
measured. **Not done here** — this step's brief is to measure, and a test edit belongs to
whoever decides Part 5's question.

### 4.5 What the numbers actually license

A 1-in-400 event is not impossible, and CI has run this file more than once. It is also
not comfortable: it did not happen in 79 local draws, and "we were unlucky" is a weaker
account than the file deserves. **The data does not settle account 2 and this document
does not claim it does.** What it does is remove two candidate explanations and name the
third precisely enough to be checked the next time the gate goes red.

## Part 5 — What this changes, and what it does not

**Nothing was changed.** No threshold, no test, no `jmfts_core`. The tree gains
`scripts/measure_maxsim_margin.py` and this file.

**Three things a reader should take from it.**

1. **`0.90` is not wrong, and `QUERIES = 24` is too few to defend it.** The fixture's
   mean is 0.9176 and the gate sits 2.75 standard errors below that mean, which is a
   test that will go red about once in every few hundred runs for no reason at all. Part
   3.3 prices the repair.
2. **`CHUNKS` is not the knob, whatever 5.8 says.** `ANN_INDEX_HEALTH.md` 5.8 closes with
   *"if a suite run reads below 0.90 here, the fixture is the first thing to check and
   `CHUNKS` is the knob"*. `CHUNKS` moves the **mean** — more documents make `limit = 10`
   a narrower target — and nothing here measures which way or how far. `QUERIES` moves
   the **spread**, measurably, at `1/sqrt(n)`. The margin is a spread problem.
3. **`hnsw.ef_search` is still the open question it was in 5.8, and it is where the mean
   comes from.** `maxsim_search` asks the index for `K_PER_TOKEN = 100` rows per query
   token while pgvector's default candidate budget is 40 and nothing in `jmfts_core/`
   raises it. 5.8's sweep on this same fixture read 0.8792 at `ef_search = 10`, 0.9167 at
   40, 0.9333 at 100 and 0.9958 at 400 — so the 0.9176 measured here is a reading about
   that default, and Part 3.1's eight permanently short queries are what a candidate list
   smaller than the LIMIT looks like. **Nothing here recommends raising it**: 5.8 declined
   to, for the reason `SPRINT_0_6_0.md` Part 0 gives, and no measurement of that trade
   exists at 1.37M rows.

## Part 6 — What was not measured

Named rather than left for a reader to discover.

* **Public CI.** No reading was taken on a GitHub-hosted runner. Everything about the CI
  condition in Part 4 is an argument from what could carry a difference, not a comparison
  of two samples.
* **`QUERIES` above 24.** Part 3.3 is arithmetic on the measured residual variance. The
  population mean at 96 queries was not read.
* **`CHUNKS` other than 200.** Its effect on the mean is asserted to be unknown, and it
  is.
* **An `ef_search` sweep on pgvector 0.8.6.** Part 5's ladder is 5.8's, taken on 0.8.5.
  `scripts/maxsim_probes.py` is the instrument and it was not re-run.
* **Any pgvector before 0.8.6.** Part 4.4's channel is named, not tested.
* **A third full-suite run.** Part 4.2 rests on two observations of the table contents.
  Two agreeing readings are not a proof that no ordering of the suite ever commits a
  token row; they are what was affordable at 51 minutes a run.
* **The `open` arm as anything but a control.** It was measured beside every one of the
  79 readings and reported; no question here turns on it.
