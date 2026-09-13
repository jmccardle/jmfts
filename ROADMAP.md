# JMFTS Roadmap

Last updated: 2026-09-13. **This file is open work; `CHANGELOG.md` is what shipped.** Where
the two would say the same thing, this one says nothing and points there.

**Where this file came from.** It was internal until now. Development moved into the public
repository at 0.5.1, and this is the first version of it anybody outside the project can
read. What arrived is the open half: current status, the numbered gaps, what is deferred and
what gates it, and the Experiment Log. What did not arrive is the dated status narrative —
five "prior entry" paragraphs and two "what shipped since" tables that `CHANGELOG.md` already
covers, and a thirteen-row index into `docs/archive/ROADMAP_HISTORY.md`, which is not
published. Nothing open was dropped.

**Gap numbers are load-bearing.** Sprint plans cite them as "`ROADMAP.md` known gap 5", so a
closed gap keeps its number and its one-line epitaph rather than being deleted and the rest
renumbered.

**Status lines are dated.** A claim dated 2026-09-13 was re-read against this tree while this
file was assembled, and the citation is where it was read. Anything else is carried from the
internal file at its own date and has not been re-checked.

---

## Status

| | |
|---|---|
| Released | **0.5.1**, 2026-09-13. `master` is the tag; `jmfts` and `jmfts-client` are both on PyPI |
| Next | **0.6.0** — `docs/SPRINT_0_6_0.md`. Headline: a **web front end**. Seven blocks, thirty-four steps |
| Version rule | The two distributions release in lockstep: one number, two wheels, one tag |
| Changing at 0.6.0 | A **third** distribution, `jmfts-web`, joins the lockstep. `jmfts[web]` depends on it |

**0.6.0's headline is eyes and hands on the appliance**, added to that plan on 2026-09-13: a
page that takes a dropped file, searches it, and shows the answer boxed on the source
document's own page. Everything else in the sprint is placed by whether that path runs
through it — the two access gates because every view is principal-scoped, the sheet header
work because a region view displays it, `index:bm25` over workbook records because searching
an uploaded spreadsheet is the first thing anybody will try. `docs/SPRINT_0_6_0.md` Part 5 is
the schedule at worktree granularity, with the interface contracts named and the merge gates
written down.

**Two long-standing questions were answered to make it schedulable**, both on 2026-09-13, and
both in that plan rather than here: which end of an edge a write gate checks (write on the
source, read on the target — 4.1), and whether office renditions are affordable. The second
retires `docs/OFFICE_SPEC.md` Part 12 question 1 on a measurement that was already in
`docs/STRESS_CORPUS.md` 2.5: original bytes are 144 MB of a 2988 MB database, 4.8%, against
`token_embeddings` at 1672 MB. "Renditions roughly double blob storage" is true and measures
the wrong denominator.

There is no 0.4.0. Most of what `docs/SPRINT_0_4_0.md` planned shipped alongside the 0.5.0
work rather than in a release of its own; `CHANGELOG.md`'s 0.5.0 entry says so. The steps
neither plan finished are the 0.6.0 plan.

**An open defect is a numbered step in the current sprint plan, and the entry condition is a
failing test.** Not an argument that something could go wrong, and not a measurement of how
often it does. This file is not a defect list and does not become one: a gap below is a thing
somebody has to decide or measure before it can be a step. `docs/archive/KNOWN-DEFECTS.md`
holds D1–D7, all resolved, and is history.

---

## Known gaps

Items 1–6 were opened by an audit in 2026-08 and re-checked 2026-08-31. Items 7–11 were
opened 2026-09-07 by `docs/ANN_INDEX_HEALTH.md` and `docs/STRESS_CORPUS.md` — the first time
this appliance held a corpus it did not build for itself, and the first time six workers
drained a queue at once.

1. ~~Constructive segmentation is conversation-only.~~ **CLOSED 2026-08-29.**
   `jmfts_core/pipeline.py` no longer exists; `structure:semantic` in `rollup_tasks.py` runs
   over any node's children regardless of format.

2. **Entity resolution is string-similarity only. STILL OPEN.**
   `fact_extraction.py:resolve_entity` has no co-occurrence or temporal-proximity signal.
   Owned by `docs/SPRINT_0_3_0.md` step 10, which is blocked on a calibration corpus.

3. ~~No relative-date anchoring in fact extraction.~~ **CLOSED 2026-09-05.** The extraction
   prompt carries a document clock — `Document.event_time` where the caller set one,
   `created_at` otherwise — so "last Tuesday" resolves against the document rather than
   against the wall clock at extraction time. `tests/test_fact_extraction_clock.py`.

4. **No `POST /documents/batch`. STILL OPEN, and worth re-examining rather than building.**
   The queued ingest spine accepts a file and fans out from it, so the case a batch endpoint
   was for may have moved.

5. **No named search-context preset is seeded, and the reason changed.** The mechanism defect
   this gap used to be about is fixed: `UsetypeFilter` accepts a set of globs in either
   spelling and `usetype_globs` normalises both, an empty filter raises rather than quietly
   widening, and all four retrieval methods carry it
   (`jmfts-client/jmfts_client/contracts/search.py`). `tests/test_search_context_presets.py`
   asserts of every row in `search_contexts` that its filter normalises to a non-empty set
   and that every glob in it could match — and asserts the table is empty, so an absent row
   records a decision instead of leaving no trace.

   Three presets were specified and none is seedable today, for three different reasons.
   `personal-notes` and `hardware` name four namespaces nothing in `jmfts_core` writes.
   `agent-sessions` is expressible and was **withheld on the owner's decision**: `adjutant:*`
   reaches the adjutant namespace and not adjutant sessions, because a transcript stopped
   being a usetype when conversation ingestion moved onto the queue, and a named downstream
   consumer does not belong in the schema every fork starts from. What would close the first
   half is a filter key `SearchContext.config` does not have — `produced_by`, which is
   `structure:conversation` on exactly those turns — and that is a change to
   `SearchContextRepository.resolve_params` and the search methods, not a seed. Separately, a
   seed in `schema.sql` reaches only a freshly built database; carrying one to an existing
   appliance is a migration nobody has written.

6. ~~`docs/AGENTIC_KNOWLEDGEBASE.md` prescribes a mechanism that no longer exists.~~ **CLOSED
   2026-09-05.** Both passages now say a `TASK_ROWS` row plus a registered handler.

7. **Dead index entries cost HNSW recall, and no policy decides what to do about it.**
   Measured in `docs/ANN_INDEX_HEALTH.md`: dead entries at a query point take recall@10 from
   1.0 to 0.100 at pgvector's default scan mode. The appliance runs the strongest mitigation
   measured — `HNSW_ITERATIVE_SCAN = "strict_order"` in `repositories/search.py` — which
   holds recall at 0.979 under 500 dead entries per query. **This gap is the residue that
   setting does not cover**: at that dose 3 queries in 100 were still degraded and one
   returned nothing. It is a decision rather than a build, and which answer is right depends
   on delete frequency and on whether a miss is a nuisance or a correctness failure.

8. **A spreadsheet's content never reaches BM25 or full-text search. STILL OPEN, re-read
   2026-09-13.** Measured by `docs/STRESS_CORPUS.md` 4.4: zero `index:bm25` tasks exist
   anywhere in a 21-workbook subtree, while thousands of `record` nodes under it carry real
   text. Two independent exclusions in one `TaskRow` (`jmfts_core/ingest_tasks.py:1173`):
   `after_any` names the three prose structure rungs and not `structure:sheets`, and
   `requires=(HAS_TEXT_LAYER,)` is a PDF property no workbook can satisfy. The comment on
   that row — *"exactly one of the two rungs is ever eligible for a document"* — is false for
   a workbook. The content is still reachable by vector and MaxSim, so this is a
   partial-retrieval gap rather than a loss, and it is silent: the index exists, answers, and
   returns nothing.

9. ~~22.5% of a real corpus is retrievable and has nothing to display.~~ **CLOSED 2026-09-07,
   and the count was an undercount.** The real figure was 26,870 of 57,492 settled nodes.
   The decision went to projection rather than exclusion, settled by measuring what the
   containers are worth: over 10 queries against the top 12 container nodes each, the
   container outscored every descendant it contains 53 times out of 59. Cost as shipped: one
   query per page, 4.9 ms for a worst-case 100-container page, and 0 of 100 blank results
   where there had been 27.

10. **The task queue cannot express which resource a rung needs.** Claiming is `ORDER BY
    t.priority DESC, t.created_at ASC, t.id ASC` and every task is created at priority 0, so
    a worker takes the oldest eligible task whatever it needs. Measured twice in one run
    (`docs/STRESS_CORPUS.md` 4.5, 4.9): the GPU sat at 0% with 13,758 claimable `embed` tasks
    pending, and separately six workers delivered about one worker's throughput with five of
    them waiting on the `search_indexes` tuple lock that `index_document` holds to commit
    (31,008 calls, 1908 s, 61.5 ms mean — the mean *is* the wait). Both were cleared by
    hand-written `UPDATE task_queue SET priority` statements, which is the whole vocabulary
    available: one integer that ranks tasks and cannot say that six workers on `index:bm25`
    is worse than one while six on `embed` is better. Whether the answer is a per-rung
    concurrency cap, a resource class on `TaskRow` beside `write_mode`, or a narrower lock in
    `index_document` is undecided.

11. **The summary tree stores 13,755 duplicate vectors and indexes them.** `summarize:tree`
    writes a node carrying its own 768-dim embedding, and every one of those vectors is
    byte-identical to its source node's — 13,755 of 13,755 pairs at cosine distance
    0.000000, because both are written from the same effective text. All 14,101 derived nodes
    have zero children, so what the node adds over its source is `member_ids`, `tree_kind`
    and an access-keyed root: navigation, not retrieval. The vectors are stored, HNSW-indexed
    and kept out of every result set by one string in `search_exclude_usetypes` — so the
    exclusion is load-bearing in a way it does not say, and lifting it would return every
    answer twice. A decision, not a build: a derived node could carry no embedding and be
    reached through its `source_node_id`, or the duplication could stay and the exclusion be
    documented as required rather than default.

---

## Subtree RBAC — remaining follow-ups

RBAC shipped 2026-07-23 in four canary-safe stages, and stage 4 closed the READ leak on
edges. These are still open; the first two were re-read against this tree on 2026-09-13.

- **Edge WRITE gating. CONFIRMED OPEN.** `DocumentRepository.create_link`
  (`jmfts_core/repositories/document.py:1119`) constructs and flushes a `DocumentLink` with
  no principal check, and `DocumentService.create_link`
  (`jmfts_core/services/document_service.py:1287`) checks only that `request.source_id`
  matches the id in the URL. A principal can attach an edge incident to a document it cannot
  write.
- **Graph-analytics verbs. CONFIRMED OPEN.** `get_centrality` (`:137`),
  `get_subtree_authority` (`:179`), `get_spines` (`:234`) and `get_communities` (`:381`) in
  `jmfts_core/services/graph_service.py` build a graph from `scope`, `parent_id` and
  `exclude_usetypes` with no principal, then return document ids and titles. They enumerate
  across the whole graph and can leak existence. `get_neighbors` (`:295`) is the one that is
  gated — it calls `can_read` on the root and `walk_neighbors` hides unreadable nodes reached
  during the walk.
- **Delegated (non-owner) grant management**, and **OAuth token minting**.
- **Restrictive (non-additive) nested ACRs remain out of scope.** The additive model can
  widen access down the tree but cannot carve a more-restricted island inside a shared
  subtree. Accepted for the cooperating-agents-under-one-human model.

---

## The job system — six phases done, three open

`docs/SPRINT_JOBS.md` Part 14 is the phase table.

| # | What | Status |
|---|---|---|
| 1 | Atom declarations | **DONE** 2026-08-28 |
| 2 | Evidence registry, types, storage decision | **DONE** 2026-08-29 |
| 2a | Path A onto the queue, then deleted | **DONE** 2026-08-29 |
| 2b | The storage migration: evidence to rows | **DONE** 2026-08-31 |
| 3 | Scope on a rule; `produced_by`; one planner | **DONE** 2026-09-01 |
| 4 | Guards with operators | **DONE** 2026-09-01 |
| 5 | Multiplicity and the cost fold | open; depends on 3, independent of 4 |
| 6 | Rule sets, requests, budget, parking | open; depends on 4 and 5 |
| 7 | The walk generalised; the rollup special case deleted | open; depends on 6 |

**Phase 4's design moved work out of phase 6 rather than into itself.** A collection is a
subtree, not a bindings table: a setting on a node reaches the documents beneath it, and the
ancestor walk that does it already runs one level down in `rollup_tasks._options_owner`. So
per-collection configuration needs a route and a walk at upload, not a new scoping mechanism —
and RBAC, which is already subtree-scoped, answers who may set it. **That route and that walk
are not built**, and they are a fourth open item beside phases 5, 6 and 7.

**One debt the phases inherit.** The `Fact.locus` vocabulary has no term for a write into
another tree, and three shipped tasks write there. Recorded at `docs/INGEST_SPEC.md` 5.3, in
`jmfts_core/atoms.py`, and in the handlers' own declarations rather than left implicit — so
whichever phase takes `EXPLAIN` next inherits a written statement of what it is not
reporting.

---

## API unification and embedded mode — what is left

Goal 1, one definition and multiple transports, shipped. Goal 2, an in-process library mode,
shipped except for one packaging question.

`fastapi`, `python-multipart` and `uvicorn[standard]` are base dependencies of `jmfts`, so
`pip install jmfts` pulls the HTTP stack even for an embedded-only caller. **Whether that is
worth an extra is an open question rather than a plan**: the base install is 586 MB and the
HTTP stack is a small share of it, and `python-multipart` is checked while the upload route
is built, so its absence would be an import-time crash rather than one dead endpoint.
`LocalJmftsClient`'s `unit_of_work()` already gives an in-process caller atomic multi-verb
writes.

---

## The dual-identity discipline

JMFTS serves two purposes with different success metrics, and must not optimise one blindly
at the other's expense.

- **Retrieval appliance** — static corpus, IR metrics (nDCG, Hits@k) on fixed query sets,
  read-only, benchmark-legible.
- **Memory substrate** — append-heavy from an agent's own activity, so the write path matters
  as much as the read path; quality shows up as downstream task success, which is hard to
  benchmark; recency, importance and decay are first-class.

**Canary rule for drift: a new default-path feature must be justifiable on the
retrieval-research axis, and anything memory-only stays opt-in so it never contaminates the
benchmark path.** `event_time` (imported corpora need domain time regardless) and `position`
(chunking needs ordered children) both pass on retrieval grounds. The recency/importance
rerank is the first purely memory-only feature and is correctly off by default.

The insight the memory framing rests on: a conversation's branch history and a disassembled
document are the *same* tree — a sequence of text nodes where parent→child means
containment-or-derivation. The agent verb catalogue that followed from it has shipped; the
write-path benchmark is the part that is still thin. An initial benchmark landed and is in
`benchmarks/RESULTS.md`; broadening it to cover idempotent replay and incremental-IDF
correctness is what would keep the memory path as honest as BEIR keeps the retrieval path.

---

## Deferred, and what gates each

Named so that a reader who finds one of these elsewhere knows it was assessed rather than
missed. Nothing here is a defect.

| What | Gate |
|---|---|
| **The office read path** — header scan over rows 1–8, a numeric second pass, the `table` shape, emitting both shapes, a label for a headerless column | Deferred out of 0.4.0 with its measurements intact. It is the only block whose value is a fire rate rather than a failing test: `extract:sheet` fires on 23.9% of open-web sheets and could fire on 54.4%, and nothing is broken at 23.9%. `docs/SPRINT_0_4_0.md` Block C, steps 8–12 |
| **The component query**: JSONB filter, census, projection | The projection's shape is undecided — whether a component is a field of `DocumentResponse`, a named evidence row, a child node under a usetype, or a jsonpath into a record decides whether it is one contract change or four. `docs/SPRINT_0_4_0.md` 4.1 |
| **Does tree agreement carry information?** | Nothing. **The gate is open and nobody has walked through it.** Four of the eight reprojection trees are gated on this reading, it needs two trees, and 0.5.0 Block C produced the second. `docs/SPRINT_0_5_0.md` 3.1 calls it the load-bearing empirical claim in that document and the one thing that pass did not schedule |
| The keyword, question, argument and error-string trees | The reading above. The question tree additionally needs a decision about which facts get questions; the argument tree additionally needs the proposer surface |
| OWL 2 RL as SQL | **A written list of named missing derivations.** "OWL is more expressive" is not the entry ticket. The first candidates are `supports` and `refutes`, whose transitivity and inverse structure a single-pass `sh:rule` may not reach — written down as candidates, not findings |
| SPARQL through an Ontop sidecar | An external client that speaks SPARQL. The component query is not one: it speaks no RDF |
| The MongoDB wire protocol through FerretDB | A `pymongo`-shaped client, by the same reasoning |
| A free path language | `docs/SPRINT_0_5_0.md` 3.5's claim being wrong — a named-walk vocabulary in use, and a written list of questions it cannot express |
| Named graphs as a stored column | A second clustering existing. Building the column first would be a schema change with one legal value |
| A corpus object, and unified trees | Each other, and the reading above. Build unified trees only after a corpus is a thing, and build a corpus only when a unified tree is wanted |
| What `/search` answers when it cannot embed | A contract decision. Three candidates exist and none is chosen; the third — running the methods that work, silently — is out on Fail Early grounds |

### Recorded, not built

* **Rule chaining and fixpoint.** Chaining needs a scheduler, and the tree architecture does
  not need it.
* **Incremental validation.** Measured 2026-09-05 and the answer is *not yet*: a full run at
  the measured bound is 57.5–59.0 s of drain, inside the same order as the queue's 90 s lease
  and with the worker beating throughout, so "too slow" is not reached. The measurement also
  found the item was aimed at the wrong half — 94.6–97.6% of peak resident set is allocated
  before `pyshacl` is called, so streaming the graph build is the cheap move and incremental
  validation is the second thing to try. `docs/MEASURE_SHACL_SCOPE.md` Part 4.
* **Shapes emitted automatically from a profile.** Needs calibration thresholds that do not
  exist.
* **`link_type` reaching `build_link_graph`.** Every centrality metric currently runs over a
  graph where all edge types are the same relation. It is wrong now and nothing depends on it
  being right; it becomes load-bearing the moment a walk or a centrality score feeds
  retrieval ranking. Whether `compute_subtree_authority` is reachable from any search path
  today is **unchecked**.
* **`build_combined_graph`'s weight sum** — a link's `score` plus `1.0` per triple, producing
  a number with no unit. Same gate as the line above.
* **A `link_type` index on `document_links`. ANSWERED 2026-09-05: do not build it.** Measured
  across 48 configurations: faster in 3, slower in 8, indistinguishable in 37; a composite
  `(link_type, source_id)` scored 2 / 8 / 38 on the same runs. The UNIQUE constraint already
  builds `btree (source_id, target_id, link_type)` and the planner uses it.
  `docs/MEASURE_TYPED_WALK.md`. Three conditions flip it and each was measured or named: a
  query filtering on `link_type` with **no** source or target anchor **and** a rare type
  (measured 2.8× at 1.0% selectivity, while the same query at 55% selectivity is a Seq Scan
  *with the index present*, because the planner declines it); `compute_neighbors` becoming a
  recursive CTE; or a shipped call path starting to pass a large `limit`. **A bigger graph on
  its own does not flip it.**
* **Incremental reclustering for unified trees.** A research problem; periodic rebuild with a
  staleness window is the position until somebody wants otherwise.
* **A sibling-uniqueness constraint on content hashes. DECIDED 2026-07-23: never.** Not a
  bug; final call.
* **Bitemporal re-assertion under `UNIQUE(s,p,o)`.** A fact invalidated and then re-asserted
  hits the identity constraint, and the question is now asked of two keys rather than one —
  `uq_triple_literal` is a second partial unique index on the literal side. A
  validity-interval-aware key versus (s,p,o) identity is the design question, deferred.
  `upsert_triple`'s do-nothing-on-conflict behaviour deliberately steers clear of
  pre-empting it.
* **Memory tiers and eviction.** There is no core-memory or working-set concept and no
  `evict` / `forget` / `ttl` / `expire_at`: decay demotes but never removes, so the store is
  append-only in practice. Tiering is a layer to define on top; what JMFTS would need is at
  least a `forget` or prune verb, and possibly a TTL column.
* **Per-agent or multi-tenant namespacing.** There is no `user_id` / `agent_id` / `tenant` /
  `owner` column anywhere. Isolation today is query-filter discipline — usetype conventions,
  subtree, named search contexts — not enforcement. Same axis as subtree RBAC, and the two
  should be designed together if namespacing is picked up.

---

## Future considerations

Surfaced during research; each needs evaluation before it could be committed to.

### Retrieval

- Restore NLI reranking with steerable hypotheses.
- Restore query classification with dynamic context budgets.
- Spreading activation retrieval over the knowledge graph.
- **MaxSim over image patches (the ColPali family).** JMFTS already implements late
  interaction, and ColPali is the same operation over page-image patch embeddings — patches
  instead of tokens. That maps onto the existing `TokenEmbedding` machinery rather than
  sitting beside it, and it skips text extraction entirely, which is the failure mode for
  scanned and heavily formatted documents. Blocked on a page rendition, which is tier 3 of
  the office stack. A *separate* image encoder as a second index is a different and weaker
  idea: the blob side holds up, but ranking across two encoders with incomparable score
  scales is unsolved here.
- ~~A RaBitQ-based `rbvec` pgvector type~~ — `tqvec` degradation is confirmed acceptable at
  −1 pt, so the RaBitQ fallback is not needed for MaxSim. It may still matter for point-query
  vector search if scale demands it.

### Open evaluation questions

- Does PELT on 768-dim embeddings outperform 1024-dim?
- Benchmark segmentation against the NAACL 2025 dialogue-segmentation approaches.
- Collapsed-tree retrieval versus tree-traversal retrieval for RAPTOR summaries.
- A Zep-style bitemporal model versus Hindsight-style confidence reinforcement for the
  temporal knowledge graph.
- Single-pass fact extraction versus a reflexion-based two-pass.
- Does learned query routing outperform the current heuristic router on BEIR queries?

### Developer ergonomics

- A key-prefix namespacing convention for document metadata.
- Snapshot and restore for search-index state, PostgreSQL-backed.

### Additional ingestion sources

Logseq, Obsidian and transcript ingestion each existed in an earlier system. Which are worth
restoring is unevaluated.

---

## Experiment Log

Permanent. A negative result here is why something is *not* in the tree, which is the reason
the table is not pruned.

| Date | Experiment | Result | Commit |
|------|-----------|--------|--------|
| 2026-03 | PLAID-style two-stage MaxSim | Ineffective: no speedup, −10% quality | 147013e → 2ede04b |
| 2026-03 | Local LLM summarization quality | Gemma 3 27B best quality, Phi 3.5 best speed | 17217dc |
| 2026-03 | TurboQuant vs RaBitQ | RaBitQ 1.6× better recall at 4-bit; TurboQuant wins for KV cache | — |
| 2026-03 | Qwen3-32B KV cache compression | 80K context, 1.9% PPL cost, 96% NIAH retrieval | — |
| 2026-03 | BEIR baseline: SciFact, NFCorpus, FiQA | 3-dataset baselines; hybrid beats ColBERTv2 and BM25+CE on SciFact | — |
| 2026-03 | Hybrid weight tuning (successive halving) | Optimal: vector 0.86 / bm25 0.14; fulltext dropped; FiQA +11% | — |
| 2026-07 | BEIR TREC-COVID (completes the 4-dataset suite) | Vector-dominant: vector 0.841 / hybrid-tuned 0.837 / rrf 0.783 / bm25 0.580 nDCG@10; equal-weight RRF costs ~5.4 pts | 200c5fe |
| 2026-03 | MaxSim reranking evaluation | Helps NFCorpus +3.4%, hurts FiQA; eliminated from hybrid by the tuner | — |
| 2026-03 | MaxSim tier sweep on BEIR (5–50% tokens) | Monotonic degradation; no sweet spot; ~80 ms/tier; never beats hybrid | — |
| 2026-03 | MultiHop-RAG benchmark | MaxSim +13.8 pts H@4 over vector; dominates on multi-hop queries | c3eb0ac |
| 2026-03 | MaxSim tier sweep on MultiHop-RAG | t10 59.5% @ 300 ms, t25 62.9% @ 641 ms, t50 72.2% @ 1242 ms | c3eb0ac |
| 2026-04 | Phase A: halfvec token embeddings | **Zero quality loss**, 1.74× compression (16.5 → 9.5 GB) | — |
| 2026-04 | Phase B: tqvec 4-bit token embeddings | **−1.0 pt H@4** (72.2 → 71.2%), 3.78× compression (16.5 → 4.4 GB) | — |
| 2026-04 | Quantization tolerance hypothesis | **Confirmed**: MaxSim aggregation absorbs 4-bit noise; 0.260 point-query recall ≠ 1 pt multi-hop loss | — |
| 2026-09 | `link_type` index on `document_links`, 48 configurations | **Do not build it**: faster in 3, slower in 8, indistinguishable in 37 | — |
| 2026-09 | Container nodes versus their descendants, 10 queries × 12 containers | The container outscored every descendant it contains 53 of 59 times — which is why containers are projected rather than excluded | 188e973 |

**One decision this log exists to explain: JMFTS owns no GPU model.** Summarization,
read-side synthesis and fact extraction are calls to an injectable external
OpenAI-compatible endpoint, configured by `JMFTS_LLM_*`. The endpoint defaults to blank and
blank is supported — everything except summarization, RAPTOR, fact extraction and synthesis
works without an LLM, and asking for one when none is configured raises naming both settings
rather than degrading quietly. The Qwen3-32B feasibility work in the table above is therefore
moot for JMFTS's own purposes; it may still matter to whoever hosts the agent's model.
