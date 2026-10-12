# Does a typed link walk want an index on `document_links.link_type`?

`SPRINT_0_5_0.md` Block D step 18. Measured 2026-09-06 by `scripts/measure_typed_walk.py`.
**This is a new file.** No measurement record existed in `docs/` to append to — the 0.4.0
measurements (`filtered_recall.py`, `maxsim_recall.py`) wrote transcripts to `datasets/`
and carried their conclusions inside `SPRINT_0_4_0.md` Block A. This one is long enough
to want its own page and the sprint plan does not have a Block D results section to hold it.

---

## The answer

**Do not build the index.** Not as its own migration, not as a second column on an existing
one.
<!-- Said "migration `020`" until 2026-09-15, meaning "the next free number" at the time. It
     is no longer free — `020_container_lookup.sql` exists and the tree has run on to 023 —
     so a reader today would take it as a claim about what that migration IS. No number is
     named now, because the recommendation was never about one. -->

Across 48 measured configurations — two degree distributions, two graph sizes, three type
filters, two node caps, two roots — adding `CREATE INDEX ON document_links (link_type)`
made the walk **faster in 3, slower in 8, and indistinguishable in 37**. The largest gain
was **34 ms**, on a walk that took 50 ms. The largest loss was **+2.0 s**, on a walk that
took 11 s. A composite `(link_type, source_id)` scores 2 / 8 / 38 on the same runs.

Two facts behind that, both measured, either of which is on its own sufficient:

1. **`document_links` already has a typed index.** The UNIQUE constraint at
   `sql/schema.sql:290` builds `btree (source_id, target_id, link_type)`. Every typed walk
   whose frontier is small enough for an index plan at all is *already* served by it — the
   planner picks `document_links_source_id_target_id_link_type_key` by name in those cases.
   Step 18's premise ("indexed on `source_id` and `target_id` and **not** on `link_type`")
   reads the two `CREATE INDEX` lines at `:509`–`:510` and not the constraint eleven lines
   into the table, and the constraint is the one that matters here.
2. **At the sizes where the walk is slow, 87–89 % of its wall time is not in the database.**
   At the largest fixture an unbounded depth-3 walk took 17.4–20.8 s and spent 2.0–2.3 s of
   that inside the driver. An index cannot touch the other 87 %.

---

## What the prediction said, and how it failed

> `document_links` is indexed on `source_id` and `target_id` and not on `link_type`. A typed
> walk filters `link_type IN (...)` *after* `source_id IN (frontier)`, so the source index
> should carry the query and the type filter should be a scan of a small set.

**Falsified, in the regime the step was worried about.** Once the frontier grows past a few
thousand ids, the planner abandons the source index entirely and sequentially scans
`document_links`, applying *both* predicates as filters. The type filter is then a scan of
the whole table, not of a small set. Measured, depth-3 statement, shipped index set:

| fixture | depth-3 frontier | plan | rows removed by filter |
|---|---:|---|---:|
| powerlaw, 250,938 edges | 15,325 | **Seq Scan** on `document_links` | 146,276 |
| powerlaw, 1,861,649 edges | 25,062 | **Seq Scan** | 870,186 |
| uniform, 3,677,232 edges | 17,417 | **Seq Scan** | 2,458,693 |
| uniform, 3,677,232 edges | 26,703 | **Seq Scan** | 1,005,080 |

The boundary is not a fixed frontier count; it is the planner's ordinary cost crossover on
the estimated row fraction. The largest frontier still served by an index plan was **14,144**
(uniform, 3.68 M edges); the smallest frontier already on a sequential scan was **8,892**
(powerlaw, 251 k edges).

**But the falsification does not rescue the index**, and that is the part that decides the
step. Where the walk goes sequential, adding `(link_type)` either changes nothing or makes
it worse, because it trades a sequential heap read for a random one:

| fixture, depth-3 statement | shipped | with `(link_type)` |
|---|---|---|
| uniform 3.68 M, common, hub, frontier 17,417 | 430.8 ms Seq Scan, `shared read=18,303` | 463.5 ms Bitmap on the new index, `shared read=35,822` |
| uniform 3.68 M, common, median, frontier 14,144 | 362.5 ms Bitmap on `idx_links_source` | 477.0 ms Bitmap on the new index |
| powerlaw 1.86 M, common, median, frontier 25,062 | 231.9 ms Seq Scan | 317.4 ms Bitmap on the new index |
| powerlaw 251 k, common, hub, frontier 15,325 | 36.9 ms Seq Scan | 25.4 ms Bitmap on the new index |

The last row is the only depth-3 statement in the whole sweep that the new index improved,
and the 11 ms it saved sits inside a walk that took 2.0 s.

---

## The other thing that decides it: the walk is already bounded

`GraphService.get_neighbors` defaults to `limit=200` (`services/graph_service.py:248`).
`walk_neighbors` builds `next_frontier` inside the same loop that fills `reached` and
breaks on `node_cap`, so **the frontier can never exceed
the limit**. At the shipped default the depth-3 query is `source_id IN (≤ 200 ids)` on any
graph, and every measured configuration at `limit=200` on the 368 k-edge fixture ran in
**3.6–8.6 ms** across all three index phases.

The expensive cases in this document all come from the `limit=10,000,000` arm, which exists
to separate the two regimes rather than to describe a shipped call. **The traversal
has exactly two callers in `jmfts_core/` and both pass 200**: `GraphService.get_neighbors`
and the coreference cluster walk (`resolve_coreferent_ids`, `max_depth=5`).

> *Corrected 2026-09-15.* This document is written around a function called
> `compute_neighbors`, and that function was since split in two: `walk_neighbors` is the
> traversal, and `compute_neighbors` is now a thin wrapper over it that adds
> `raise_on_truncation`. `GraphService.get_neighbors` calls `walk_neighbors`;
> `compute_neighbors`' one remaining caller in `jmfts_core/` is `resolve_coreferent_ids`.
> **Every measurement here is still of the shipped traversal** — the script calls
> `compute_neighbors`, which delegates — and both callers still pass `limit=200`, which is
> the claim the answer rests on. Five passages below name `compute_neighbors` for a body it
> no longer has; read them as `walk_neighbors` in `jmfts_core/graph_analysis.py`.

That second caller matters more than the first here, because **it is the only typed walk the
appliance actually runs**: it passes `link_types=[SAME_AS_LINK_TYPE, RBAC_COREF_LINK_TYPE]`
(`graph_analysis.py:961`) — two rare types — with `limit=200`. That is the rare-filter,
bounded-frontier corner of this sweep, and on the shipped index set it measured **2.5–54.4 ms
across all four fixtures** (seven of the eight configurations were under 20 ms; the top of
that range is one of them, power-law 1.86 M from a median-degree root). Every statement in it
that the planner served with an index, it served with
`document_links_source_id_target_id_link_type_key` — unprompted, and without a `link_type`
index existing. It is the case such an index would have been bought for, and it is already
fast for a reason that does not involve one.

**And at that unbounded limit the typed walk is a sequential scan by another name.** On the
1.86 M-edge power-law fixture, an unbounded depth-3 walk from the hub reached **36,822 of
36,823 documents** — the whole graph. On the 3.68 M-edge uniform fixture it reached 36,822
under both the unfiltered and the `mentions` filter. When depth 3 is the whole graph, the
question of which index finds it is not the question.

---

## The fixture, and why it is generated

**There is no real link graph on this machine.** Measured 2026-09-06 by starting a throwaway
Postgres over a *copy* of each volume (the originals were never mounted read-write):

| source | documents | `document_links` rows |
|---|---:|---:|
| `jmfts-dev_pgdata` (`jmfts`) | 36,823 | **5**, all `contains` |
| `jmfts-dev_pgdata` (`jmfts_main_test`) | 8 | 0 |
| `jmfts-chunk_chunk_pgdata` | 860 | 0 |

Five edges cannot distinguish an index scan from a sequential one, so the graph is
generated. Node count is 36,823 — taken from the largest real corpus so the `documents`
side is at least the size of something that exists.

Two distributions, because they do not give the same answer:

| fixture | requested edges | realized | out-degree p50 / p99 / max |
|---|---:|---:|---|
| uniform | 368,230 | 368,166 | 10 / 18 / 26 |
| uniform | 3,682,300 | 3,677,232 | 100 / 124 / 142 |
| power-law, Zipf α = 1.0 | 368,230 | 250,938 | 2 / 85 / **8,891** |
| power-law, Zipf α = 1.0 | 3,682,300 | 1,861,649 | 17 / 537 / **29,630** |

Realized counts are lower than requested because duplicate `(source, target)` draws are
dropped rather than topped up — topping up would bias the tail. The power-law draws collide
much more, which is what a Zipf ranking on both endpoints does.

The uniform fixture is the graph `bridge` produces (`summarization.py:353` — pairwise
similarity inside one RAPTOR layer, no hub). The power-law fixture is the graph `mentions`
produces: an entity document is linked from every document that mentions it, and mention
frequency is not uniform. A uniform walk's frontier grows as `k**depth` and stays small at
sensible `k`; a power-law walk reaches a hub at depth 1 and its depth-3 frontier is most of
the graph. Reporting one and claiming it covers the other would be the error.

`link_type` is assigned **per source node** by default — every out-edge of a node carries
the same type — because that is what the shipped writers do (five, by file and line, in
`scripts/measure_typed_walk.py`; this sentence said four). Shares are a modelling
choice and are labelled as one in the script: `mentions` 55 %, `contains` 30 %,
`summarizes` 10 %, `bridge` 4 %, `same_as` 1 %. What the sweep needs from them is one common
type and one rare one.

**The correlation control.** Because `by-source` makes `link_type` nearly a function of
`source_id`, a cheap type filter could be cheap *for that reason alone*. Re-run with
`--type-assign random` (types drawn independently per edge, which no shipped writer does),
16 configurations at 250,938 / 368,166 edges: `(link_type)` was **faster in 0, slower in 3,
indistinguishable in 13**. The conclusion does not depend on the correlation.

---

## Method

* **The walk is the shipped one.** `jmfts_core.graph_analysis.compute_neighbors` is called
  directly — which since 2026-09-15 delegates to `walk_neighbors`, the function
  `GraphService.get_neighbors` calls — with `direction="both"`, `max_depth=3`. Every statement it issues is captured by a
  SQLAlchemy `before_cursor_execute` hook and then re-run under
  `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` **verbatim, same text and same parameters**.
  There is no hand-written walk SQL anywhere in the script.
* **The schema is `sql/schema.sql`, loaded verbatim**, not `Base.metadata.create_all` —
  the mapper deliberately does not declare `idx_links_source` / `idx_links_target`
  (`models/document.py:278`–`:284`), so `create_all` would have measured a table that
  exists nowhere.
* **Frontier sizes are read, not inferred.** `next_frontier` at depth *d* is exactly the set
  of nodes the walk emits at depth *d*, so the depth-(*d*+1) statement is issued with that
  many ids. The script derives the frontier from `compute_neighbors`' own return value and
  then cross-checks it against the parameters actually bound, aborting on a mismatch.
* **Six runs per configuration**; run 1 is reported separately as the cold one and runs 2–6
  as a min..max range. A range that overlaps the baseline's range is reported as no movement,
  which is how the 37 / 8 / 3 split above is counted.
* **Cache state, from `Buffers` rather than from a claim.** At ≤ 368,166 edges every EXPLAIN
  reports `shared read=0` — the table is entirely inside the 128 MB `shared_buffers` and the
  timings are fully warm. At 1,861,649 edges the heap is 136 MB and at 3,677,232 edges it is
  larger still, so those statements report `shared read` of 1,086–38,441 blocks: warm OS
  cache, cold shared buffers. Postgres 16.14, `shared_buffers=128MB`, `work_mem=4MB`,
  `effective_cache_size=4GB`, pgvector image, defaults otherwise.
* **The hydration query is reported and is not the answer.** `compute_neighbors` ends with
  one `SELECT id, title, usetype FROM documents WHERE id IN (…)` (now in
  `walk_neighbors`).
  It never exceeded **8.3 ms** in any configuration.
* **Two roots per configuration, chosen per filter**: the highest-out-degree node *among
  sources carrying an edge of the filtered type*, and a median-degree one. Choosing roots
  before the filter puts most walks at depth 0 with nothing traversed, which would have
  reported "the type filter is free" for the reason that nothing was walked.

Regenerate:

```bash
docker run -d --name walk-pg -e POSTGRES_PASSWORD=jmfts -e POSTGRES_USER=jmfts \
    -e POSTGRES_DB=jmfts -p 127.0.0.1:5457:5432 pgvector/pgvector:pg16
mkdir -p datasets
python -m scripts.measure_typed_walk --dsn postgresql+psycopg2://jmfts:jmfts@127.0.0.1:5457/jmfts \
    --json datasets/typed_walk_2026-09-06.json
```

<!-- Corrected 2026-09-15: was `./scripts/measure_typed_walk.py`, which has a shebang and
     no execute bit in git, so it fails with Permission denied; and `datasets/` is
     gitignored and absent from a fresh clone, so the run died at the write. `python -m` is
     the form `MEASURE_SHACL_SCOPE.md` uses. -->
<!-- Corrected 2026-10-12: the DSN was `postgresql://`. The script hands it to
     `create_engine`, and under SQLAlchemy 2.1 that names psycopg 3, which the project does
     not install — the same failure 0.5.2 fixed in `Settings.database_url` (`1178d3b`). A
     fresh install resolves 2.1 and fails at the first connection with "No module named
     'psycopg'"; an install still on 2.0 runs either form. -->

Transcripts (gitignored, per `datasets/README.md`): `typed_walk_2026-09-06.json` (the main
sweep), `typed_walk_control_random_2026-09-06.json` (the correlation control),
`typed_walk_split_2026-09-06.json` (the SQL-versus-Python split).

---

## Where the time actually goes

At the largest fixture, unbounded, the split between "inside the driver" and "inside
`compute_neighbors`" is:

| configuration | walk, warm | of which SQL | SQL share |
|---|---|---|---|
| uniform 3.68 M, no filter, hub | 19,478–20,822 ms | 2,059–2,272 ms | **11.2 %** |
| uniform 3.68 M, no filter, median | 17,407–19,017 ms | 2,008–2,097 ms | **11.6 %** |
| uniform 3.68 M, `mentions`, hub | 10,314–11,185 ms | 1,402–1,463 ms | **13.1 %** |
| uniform 3.68 M, `mentions`, median | 9,078–9,679 ms | 1,167–1,218 ms | **12.7 %** |

The remainder is the per-edge Python loop in the traversal — the `visited` set, the
`NeighborNode` construction, the `readable` membership test — running once per edge incident
to the frontier. **If an unbounded typed walk ever needs to be fast, the thing to change is
that the walk is a Python loop over N round trips, not which index the loop's queries use.**
A recursive CTE would move the whole 100 %; an index competes for at most 13 % and won none
of it here.

---

## The conditions under which the answer flips

1. **A query that filters on `link_type` with no `source_id` / `target_id` anchor.** This is
   the real use for the index, and it is not the typed walk. Measured on the 1,999,948-edge
   fixture, `SELECT id, source_id, target_id, link_type FROM document_links WHERE link_type
   = 'same_as'` (20,048 rows, 1.0 % of the table):

   | | plan | execution, 5 runs |
   |---|---|---|
   | shipped | Parallel Seq Scan | 43.4–44.2 ms |
   | `+ (link_type)` | Bitmap Index Scan | 15.4–18.2 ms |

   2.8×, for 13 MB of index against a 146 MB heap. **The selectivity is load-bearing**: the
   same query for `link_type = 'mentions'` (55 % of the table) is a Seq Scan at 190.7–192.5 ms
   *with the index present* — the planner declines it.

   Two candidates for such a query exist and neither is built. `build_link_graph` reading one
   edge type (`graph_analysis.py:111`–`:114`, which `SPRINT_0_5_0.md` Part 5 cites as
   `:110`–`:113` and lists as cut), and a `rederive_links` variant scoped by type rather than
   by rule. **The index becomes worth a
   migration the day one of those ships and its type is a rare one** — and it should be
   measured against that query, not against this one.

   > **Corrected 2026-10-12: the first of those two has shipped, for one verb.** `5af8775`
   > (2026-09-30) gave `POST /graph/seeded-rank` a `relations` filter that reaches
   > `build_link_graph` as `link_type IN (...)`. That edge statement is still anchored by
   > `source_id IN` and `target_id IN` the vertex set. What is NOT anchored is
   > `graph_analysis.unknown_relations`, which runs `SELECT DISTINCT link_type FROM
   > document_links WHERE link_type IN (...)` to refuse an unknown name — the query shape this
   > item describes. Nobody has measured it; the condition above is met and the measurement
   > it asks for has not been taken.

2. **`compute_neighbors` stops being a Python loop.** If the walk is rewritten as a recursive
   CTE, the 87 % that an index cannot reach today collapses into the database and the
   arithmetic changes completely. That rewrite is the prerequisite for the index question
   being interesting, not a consequence of it.

3. **The node cap is removed from a shipped call path.** Nothing passes a large `limit`
   today. If something does, re-measure — but note that the unbounded depth-3 walk reaches
   the entire graph on every power-law fixture here, and the fix for that is a bound, not an
   index.

---

## Sizing the ceiling, 2026-09-10

`SPRINT_0_5_0.md` Block D finding 7 is fixed: `/graph/neighbors` now refuses a `limit`
above **1024** or a `max_depth` above **6** rather than taking either from the wire
unbounded. This is what sized them. Uniform fixture, 20,000 documents and 400,000 edges
(out-degree 20, so ~40 incident edges per node at `direction="both"`), pgvector pg16 with
stock settings, `link_types=None`, best of three warm runs:

| `limit` | depth 2 | depth 6 | nodes returned at depth 6 |
|---:|---:|---:|---:|
| 10 | 37 ms | 37 ms | 10 |
| 200 | 93 ms | 93 ms | 200 |
| **1024** | **100 ms** | **100 ms** | 1024 |
| 4096 | 104 ms | 370 ms | 4096 |
| 10,000,000 | 107 ms | **2626 ms** | **19,999** — of 20,000, the whole graph |

**1024 costs 7% more than 200, not 5×.** The `limit × degree` bound from
"The other thing that decides it" above is an upper bound that does not bind: both caps
fire inside the *same* hop, whose edges were fetched and paid for once either way. Any
argument that scales cost linearly in `limit` — including the one that first proposed this
number — overstates it.

**Depth is free exactly while `limit` binds.** At every limit up to 1024, depth 6 costs
what depth 2 costs; only at 4096 does the extra depth start to matter (104 → 370 ms), and
unbounded it is the difference between 107 ms and 2.6 s. This is the same structural fact
as `:78`–`:83`: the frontier cannot exceed the limit, so extra depth buys round trips
rather than expansion. 1024 sits at the knee, below where depth becomes a cost again.

**The ceiling is doing real work.** Removing it costs 26× here and reaches all but one
document — the same shape as the 17.4–20.8 s / 36,822-of-36,823 result on the 3.68M-edge
fixture, at 1/9th the edges.

`max_depth` still needs its own ceiling even though it is nearly free, because nothing
else bounds how many *statements* one request may issue: the walk stops early only when
the frontier empties, so `max_depth=10_000_000` with `limit=1024` is still up to ~2,050
round trips. 6 is past every named walk — the wire default is 2, no in-tree caller of
`/graph/neighbors` passes more, and the deepest walk anywhere is the coreference cluster
at `max_depth=5`, which does not come through this endpoint.

**Refused, not clamped.** A clamped walk answers a question the caller did not ask, and
nothing in the response can say which one: `truncated` reports a property of the *graph*
("more lies behind this page"), so overloading it with a property of the *request* ("your
bound was overruled") would make one bit mean two things. The bounds are declared on the
service signature, so `wiring.py` publishes them in the OpenAPI document and a caller
reads the ceiling without sending anything.

**What does not flip it:** a bigger graph on its own. The sweep spans a 15× range in edge
count (250,938 → 3,677,232) and the verdict is the same at both ends; the losses from the
index are concentrated at the *large* end, not the small one.

---

## Checked and not a defect

An unbounded walk binds the whole frontier into one `IN` list. A depth-6 probe (uniform,
200,000 nodes, 1,999,948 edges) produced frontiers `[1, 37, 770, 14,894, 142,777, 41,521]`
and **bound 142,777 ids into a single statement without error** — psycopg2 interpolates
parameters client-side, so PostgreSQL's 65,535-parameter extended-protocol ceiling does not
apply. This was measured because the opposite was about to be written down as a defect on
inference alone.
