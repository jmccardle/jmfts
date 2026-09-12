# Changelog

Every released version, and what changed in it. Entries are written from the commits in
each release range, not from a plan — a line here is something that shipped.

`jmfts` and `jmfts-client` release in lockstep: one number, two wheels, one tag. A version
below is both distributions.

Dates are the release commit's date. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project uses semantic
versioning with the caveat that it is pre-1.0, so a minor version may break the wire.

## [0.5.0] — 2026-09-12

**This release is 0.5.0 and there is no 0.4.0.** Most of what `docs/SPRINT_0_4_0.md`
planned shipped here alongside the 0.5.0 work rather than in a release of its own — the two
sprints ran together, so cutting a 0.4.0 tag after the fact would name a tree that never
existed. The steps neither plan finished are not lost; they become the 0.6.0 plan. Version
numbers are pre-1.0 and a skipped minor costs nothing.

**Upgrading an existing database: run `jmfts-init-db --pending` and apply what it names.**
`022_token_embed_256_hnsw.sql` is the first delta whose absence is silent — see
*Added* below and "Upgrading an existing appliance" in `docs/RELEASING.md`.

**Known, and shipped anyway: the suite's ANN tests fail intermittently, and which ones
varies by run.** Three readings, and no two of them name the same tests. Locally, at least
one of `tests/test_usetype_filter.py`'s three failed in 7 of 11 full-suite runs, on this
tree and on the commit before it. The first public CI run of this release passed all three
of those and failed five others —
`tests/test_filtered_recall.py::test_shipped_order_by_reaches_the_index` with
`idx_documents_embed is absent from the plan`, and four
`tests/test_search_regression.py::TestEntityPollution` tests with a document missing from a
vector result set. The second passed all eight of those and failed two in
`tests/test_maxsim_recall.py`, which is a different problem and is stated separately below.
This release does not claim the symptoms are one cause.

**And separately: `tests/test_maxsim_recall.py`'s 0.90 threshold has no margin.** It
measures the mean of 24 per-query recall@10 values, each a multiple of 0.1, so one document
in one query's top 10 moves the figure by 0.0042. This tree reads 0.9167 (`22.0/24`); public
CI read 0.8958 (`21.5/24`), which is one document below the line, with the ungated control
arm at 0.9333 confirming the read gate is not the account of it. The recall repair migration
`022` ships is not what is in doubt — 0.4542 to 0.9167 on the fixture and 0.7533 to 0.9667 on
1.37M rows — it is the assertion that is one document wide. **The threshold was deliberately
not moved for this release.** Lowering a gate to make a release pass would leave nothing
measuring the thing the release fixed.

In a failing `test_usetype_filter.py` run the plan is
`Index Scan using idx_documents_embed on documents (actual rows=0 loops=1)` with no
`Rows Removed by Filter` line at all: the index scan emits nothing, for seven live rows
that are `settled`, carry a non-NULL `embed`, and sit at measured cosine distance
`0E-9` from the query. In a passing run the same node emits all seven and the filter
removes four. `enable_indexscan = off` returns the correct 3 every time; every
`hnsw.iterative_scan` mode, `hnsw.ef_search` from 40 to 1000 and `hnsw.max_scan_tuples`
to 10⁶ return 0. It is not reproduced outside the suite — about 130 cells across six
standalone reproducers mirroring `schema.sql`'s index declaration were all correct — so
what is known is that a state the suite reaches triggers it and the state has not been
isolated. The suite's `db_session` rolls every test back without a `VACUUM`, which leaves
seven live rows in a table whose statistics describe thousands, inside a transaction that
never commits; that is not a state a running appliance reaches, and it is not evidence that
the shipped path is sound. **A suite that failed on 9 of its 13 runs across two machines is
not a dependable release gate**, which is the cost of shipping this: a green run on it can
take more than one attempt, and an attempt that goes green says less than it looks like it
says. Both paragraphs become steps in the 0.6.0 plan, and they are the entry conditions.

### Added

- **`jmfts-init-db --pending` — ask a database which shipped deltas it has not applied.**
  `jmfts-init-db` loads `schema.sql` and never a delta, and the only reading it offered
  before this was `--list-migrations`, which prints every shipped name and compares it
  against nothing; an operator upgrading an appliance had to know the target's state from
  memory. This reads the target's `schema_migrations` ledger and prints the difference. It
  applies nothing — choosing to apply a delta stays a deliberate act. Names go to stdout and
  everything else to stderr, so `--pending 2>/dev/null` pipes, and the exit code follows
  `diff`: 0 nothing outstanding, 1 some, 2 the question could not be answered. **Three
  states are not "you are current" and none of them exits 0**: a database with no ledger
  table predates `017` and cannot say which of `002`–`016` it holds, so it is told to apply
  `017` first rather than being handed a list of twenty-one; an absent database is told to
  run `jmfts-init-db` with no flags; a ledger naming a file this package does not ship is a
  database ahead of the code, and nothing is guessed. **Migration `022` is why this was
  worth building now**: every earlier delta is a schema change and a database missing one
  raises at the first query, while `022` changes an index and a database missing it returns
  rows — 0.4542 of the ones it ranked, with no error anywhere.
- **SHACL shapes run.** `POST /ontologies/{name}/validate` enqueues `validate:shape`, which
  builds an RDF data graph from the triple store bounded twice — by the binding's document
  scope and by the caller's access filter — runs the shape over it, and stores the violation
  report as a node under the ontology. A rung and not a synchronous endpoint because the
  scope is a document set: 138,000 documents build comfortably and ~288,000 is an OOM with
  no partial progress. An empty scope and a scope whose documents do not share one access
  key are both refused (422) rather than reported as conforming.
- **`sh:rule` derivation.** `POST /ontologies/{name}/derive` enqueues `derive:rule`, which
  runs the shape's rules over the same bounded graph and writes the inferred triples with
  `triples.derived_by` set to the rule's identity. One pass over asserted data
  (`derived_by IS NULL`), not a fixpoint. Re-derivation is delete-then-insert
  `WHERE derived_by = :rule`, so a rule owns its own output and touches nobody else's.
- **`JMFTS_SHACL_MAX_SCOPE_DOCUMENTS`, default 512 — a validation or derivation over a
  larger scope is refused.** `pyshacl` has no streaming mode and a run has no partial
  progress, so a scope that does not fit in memory is an OOM kill followed by a retry that
  allocates the same graph and dies the same way. Measured through a real worker: 138,000
  documents peaks at 2,039 MiB, which is the whole memory *request* of the cpu worker pod,
  and the graph build rather than `pyshacl` holds 95% of it. 512 is deliberately far below
  every rung anybody ran — a conservative default an operator raises, not a capacity limit —
  and `ScopeTooLargeError` names both the setting and the environment variable so the
  refusal teaches the knob. It is read once, at the request, so one edit binds both
  `validate:shape` and `derive:rule`; the handlers still pass no bound, because the request
  already applied it and re-applying it in the worker would refuse a run the request
  accepted.
- `tests/test_maxsim_recall.py` — the entry condition for the MaxSim ANN shortfall, driven
  through `maxsim_search` against the appliance's own token index with real model vectors
  and a granted non-owner principal. It went in red, measuring 0.4542 of the documents an
  exact scan ranks in its top 10 against a threshold of 0.90, and the index change under
  *Changed* below is what turned it green — **not one of its assertions moved**. It ships
  green at 0.9167. The other two tests exist so the third cannot be believed for the wrong
  reason: one fails if the planner chose a sort instead of the index, one is the ungated
  control, written as an implication so it did not go red when the shortfall was fixed.
- **`summarize:tree` — a summary gets a node in its own tree.** The RAPTOR roll-up is now a
  registered rung, so `EXPLAIN` can see it. Its summaries live under a derived root keyed by
  `(access_key, tree_kind)` (migration `018`) instead of in the source tree, and the
  `summarizes` edges carry `derived_by`. A roll-up whose summary would be readable by
  principals that cannot read every member is refused before the root is minted.
- **`document_links.derived_by`** (migration `019`), NULL meaning asserted, mirroring
  `triples.derived_by` down to the width. `DocumentRepository.rederive_links` is
  delete-then-insert scoped by rule; an edge already held under NULL or another rule raises
  rather than being silently skipped.
- **A migration ledger.** `schema_migrations` (migration `017`) — one row per delta, so a
  database can say which deltas it holds. `017` backfills `002`–`016` with `applied_at`
  NULL, because a database built before the ledger existed cannot know when it took each
  delta and an invented timestamp is worse than an absent one. A test asserts that
  `schema.sql`'s ledger fence and the `migrations/` directory name the same set.
- **A container's text reaches the wire.** A node whose text is its children's carries
  `content: NULL` by design, and 46.7% of a 57,492-node corpus was in that state — ranked
  first by vector search and rendered blank. `jmfts_core/effective_content.py` computes what
  a container stands for, for a whole page in one query, and `DocumentResponse.content_source`
  (`'stored'` or `'effective'`) says the text was computed at read time and is not a column.
  The LLM synthesis context goes through the same projection, where a container used to
  reach the model as an empty string.
- **BM25 scores a container on the text it stands for**, without indexing that text twice: a
  container's term frequencies and length are summed from the postings its frontier leaves
  already wrote, and `doc_freq`, `total_docs` and `avg_doc_length` are untouched. Migration
  `020` adds the partial index that finds containers without reading the whole table.
- **A spreadsheet row too long to embed is a container, not a dead node.** A `record` that
  does not fit the embedding window becomes a container over `cell` nodes — one per column,
  carrying the column name and typed value — and a `cell` that still does not fit becomes a
  container over `chunk` nodes. New usetype `cell`; the container's own vector is written
  over the concatenation, by `summarize` at the settling boundary and with no LLM.
- `usetype = 'profile'` for the sheet profile (migration `021`), which takes a measured
  description of a worksheet's columns out of the exclusion lists it was silently held in.
  A profile is counted from cells with no model involved; a summary is authored by one, and
  sharing a usetype string meant sharing an exclusion.
- `truncated` on `AppliedFilters` — the page came back short, and explicitly not why:
  pgvector emits no stopped-early signal, so short-because-stopped is indistinguishable from
  short-because-exhausted at that layer. **It is not a health signal and must not be read as
  one.** It was true on 44 of 51 ANN pages in one suite run, and on the MaxSim path a
  rebuilt index returns a *shorter* page at higher recall — on that axis the flag runs
  backwards. Its whole content is "an ANN scan bounded this page"; where a page is cut by an
  ordinary SQL predicate there is no flag, because `LIMIT` counts visible rows and a short
  page genuinely is the end.
- `GET /capabilities` — what this appliance accepts, asked without sending it anything:
  which optional extras are installed, whether it can produce a vector at all (its own
  model, or another JMFTS via `JMFTS_RUNNER_URL`), which formats it identifies from the
  bytes, which ingest entry points it accepts, which retrieval methods it fuses and at what
  weights, and which usetypes it holds out of every result set. `?corpus=true` adds the
  counts that say whether a method will return anything on this corpus. Answered from the
  live registries, and it makes no network call.
- `GET /access/audit` — what is NOT protected by any access-control root. A document under
  no ACR is readable and writable by anyone with a token; that is the documented default,
  and until now there was no way to ask which documents were in that state, so
  open-by-default and open-by-accident gave identical answers. Owner-only.
- `governed` and `governing_acrs` on `FileUploadResponse` and `IngestResponse`: whether an
  access-control root sits at or above the node this request created.
- `applied` on every search response — the resolved methods, the resolved RRF weights and
  the usetype exclusions that were applied. Omitting `exclude_types` applies
  `JMFTS_SEARCH_EXCLUDE_USETYPES`, which used to shorten a result list with no explanation
  anywhere on the wire.
- `GET /health/llm` — the LLM reachability probe, moved off `/health`. Needs a token.
- `auto_index_bm25` on `DocumentCreate`, default true: `POST /documents` now writes the
  document to the `default` BM25 index inline, the way `auto_embed` writes its vectors
  inline.
- `docs/reference/` — three generated pages that ship: what can be ingested, what gets
  indexed by which rung, what can be retrieved and under which filters. Rendered from the
  registries the appliance reads at runtime by `python -m scripts.generate_reference`; a
  test refuses a stale one.
- This file.

### Changed

- **MaxSim returns the documents it ranked. The token index is HNSW, and it was IVFFlat.**
  `idx_token_embed_256_ivf` becomes `idx_token_embed_256_hnsw` (migration
  `022_token_embed_256_hnsw.sql`), and `maxsim_search` now sets
  `hnsw.iterative_scan = strict_order` the way vector search already did. Measured through
  the real method on real vectors, recall@10 against an exact scan of the same rows goes
  **0.4542 to 0.9167**, and on the 1.37M-row reference corpus 0.7533 to 0.9667 at unchanged
  query latency. A caller reads a MaxSim result as the top N by score, and it was returning
  fewer than half of them. **The cause was not the index type and not a tuning knob:** the
  IVFFlat index was created by `schema.sql` against a table holding zero rows, so its 1024
  k-means centroids were fitted to nothing and no code path ever recomputed them. An HNSW
  graph is built by insertion, so it cannot be built in the wrong order — which is why the
  repair is a different index rather than a rebuild somebody has to remember to schedule.
  **This costs disk and build time on an existing database: +46% on that index (1050 MB
  against 720 MB at 1.37M rows) and 2.7x the build.** Applying the migration takes a write
  lock on `token_embeddings` for the length of the build; the file says how to trade that
  for `CONCURRENTLY` by hand. `scripts/create_ivf_index.py` is now
  `scripts/create_token_index.py` and builds the HNSW index — running the old one after
  migrating would have silently put the IVFFlat index back.
- **`GET /graph/neighbors` bounds both of its arguments, and refuses rather than clamps.**
  `limit` is capped at 1024 and `max_depth` at 6; an ask above either is a 422 naming the
  bound. This breaks a caller that used to pass a larger number and get an answer — which
  was the defect: the endpoint accepted ten million and spent 17.4–20.8 s of server time on
  one request. Refusing rather than clamping keeps `truncated` meaning one thing; a clamped
  walk would answer a question the caller did not ask, with nothing in the response saying
  which one. The ceilings are measured, not argued (`docs/MEASURE_TYPED_WALK.md`, "Sizing
  the ceiling"): 1024 costs 7% more than 200 rather than 5×, because both caps fire inside
  the same hop whose edges were fetched once, and depth 6 costs what depth 2 costs at every
  limit up to 1024.
- **`NeighborsResponse.truncated` is measured instead of inferred.** It was
  `len(nodes) >= limit`, which is wrong in both directions: a walk that ran dry on exactly
  `limit` nodes read as cut, and a walk that stopped one node short of thousands read the
  same as one that saw everything. The walk now overshoots its node cap by exactly one and
  reads the answer back. Depth is deliberately not part of the flag — a caller who asked for
  `max_depth` hops and received every node within `max_depth` hops was answered, not cut.
- **A usetype filter names a set of globs.** `UsetypeFilter` accepts a JSON list or the
  comma string a URL query parameter has to use, and `usetype_globs` normalises both. All
  four retrieval methods carry it, and the MaxSim path stopped interpolating the pattern
  into its SQL on the way. An empty filter now raises instead of quietly widening to an
  unfiltered page.
- `usetype = 'derived'` joins `JMFTS_SEARCH_EXCLUDE_USETYPES` and
  `JMFTS_BM25_EXCLUDE_USETYPES`. A derived-tree root is a contentless container titled
  "Derived: summary", and nothing carried the usetype before migration `018`, so no existing
  install loses anything it had.
- **One row→RDF mapping.** `triples_to_turtle` now calls `rdf.shacl.triple_terms()` and
  `_literal_for` is gone. The exporter and the validator had built terms from a triple row
  independently, which means a validator could have passed a graph nobody can export.
- **One computation of "only the bound shape runs".** Validation walked a reachable
  subgraph through a maintained set of `sh:` terms whose object is a shape — open-ended, and
  it dropped a named rule node — while derivation stripped the five SHACL target terms from
  the whole vocabulary. SHACL's targets are a closed vocabulary and "terms whose object is a
  shape" is not, so the second wins: one `rdf.shacl.bound_shape_graph`, called by both.
- **A violation report's rendered list is capped at 512, and the text says it was capped, by
  how much, and where the rest is.** Measured at the memory bound the rendered report was
  11,688,300 characters, and `idx_documents_content_fts` is a GIN index over
  `to_tsvector(...)`: a large enough report buys `string is too long for tsvector`,
  classified PERMANENT, on the last write of a run that already spent a minute. The full
  list stays in `structured_content`. A truncated report that did not say it was truncated
  is the failure this tree has already fixed twice.
- `contracts/ontology.py` is deleted and its two classes —
  `RuleDerivationRequest`/`RuleDerivationRunResponse` — move into `contracts/rdf.py` beside
  their validation counterparts, which are the same two shapes for the sibling operation.
  Likewise `ShapeMissingFromOntologyError` and `ShapeNotInOntologyError` were one condition
  reached from the write side and the read side; the survivor is
  `rdf.shacl.ShapeNotInOntologyError`. `_verbs.py` regenerated; no wire path changed.
- Thirteen class-based Pydantic `Config` classes across seven contract files and
  `config.py` become `ConfigDict`. v2 still honours the nested class and warns; v3 removes
  the shim. `_verbs.py` regenerates byte-identical.
- `.env.example` and `config.py` now state that `JMFTS_LLM_BASE_URL` is the server root:
  `llm_client.py` appends `/v1` itself, so the `https://host/v1` address every
  OpenAI-compatible server advertises 404s at `/v1/v1`, once per task and hours in. They
  also state that `GET /health/llm` reports that failure in the body, as
  `"openai_compatible": false`, while answering HTTP 200 — a probe reading the status code
  alone calls a dead endpoint healthy.
- **`HybridSearchRequest.methods` now defaults to `null`, meaning "not specified".** It was
  a list compared against its own default with `!=`, which made the request
  order-dependent: the three declared names in the declared order ran `vector`+`bm25`,
  and the same three names in any other order ran all three with `fulltext` fused at 1.0
  against vector's 0.86. Same meaning, different ranking. The effective default is
  unchanged (`vector`, `bm25`).
- **`GET /health` is now the cheap probe.** It ran up to two outbound HTTP calls at a 5 s
  timeout each, so a Kubernetes liveness probe on it could block for ~10 s on somebody
  else's LLM host — and it is the one health path reachable without a token. A client that
  read `llm` off this response now reads `null` and must call `GET /health/llm`.

### Fixed

- **`vector_search` ordered by the similarity label, so the HNSW index was never read.** It
  selected `(1 - embed <=> q)` as `score` and ordered by `score DESC`; pgvector's HNSW index
  answers `ORDER BY col <=> q` ascending and Postgres does not rewrite one expression into
  the other, so `idx_documents_embed` was built, maintained on every write and read by
  nothing — every vector search was O(rows). Measured on the fixture: 2675.91 for the
  shipped form against 8.03 for the native one. Ordering by the distance operator reaches
  the index; the `SELECT` list is untouched, so callers still read `1 - distance` as score.
- **A filtered vector search could return an empty page with thousands of matching readable
  documents in the table.** Correcting the `ORDER BY` above is what made the truncation live,
  so `hnsw.iterative_scan = strict_order` ships with it, `SET LOCAL` before the scan and
  guarded on the server registering the GUC. `strict_order` rather than `relaxed_order`
  because hybrid search fuses by rank position, so a page shuffled inside itself silently
  reweights the fusion. Raising `hnsw.ef_search` is not a substitute: ten times the default
  returned the same zero rows.
- **BM25's leaf scan returned in-flight documents the other three methods hid.** Vector
  search, MaxSim and BM25's own container pass all filter `settled = 'settled'`; the leaf
  scan filtered nothing, because `search_term_postings → search_index_entries` never reaches
  `documents` and the scored CTE had nowhere to put the predicate. The ingest pipeline's
  indexing rung runs before `settling.py` reaches a node, so a posting exists for an
  unfinished document the same as for a finished one — measured with half a 30,000-document
  corpus put back in flight, 12 of 20 results were nodes the appliance says are unfinished.
  The `documents` join is now unconditional and the gate goes inside the scored CTE rather
  than after it, so a page is never trimmed after `LIMIT`. Measured on 1.72M postings: with
  half the corpus in flight the gate is 8–26% *faster*, because it prunes before the
  `GROUP BY`; with nothing in flight it costs 18–27% unscoped for a join that was not there
  before, and is at parity scoped.
- **`TripleRepository.query_triples` applied its endpoint access filter after
  `LIMIT`/`OFFSET`**, so a page the filter emptied could not be told from an exhausted
  store. Three consequences, each measured against the real repository: an outsider holding
  six readable facts asked for one row and got `[]`; `offset` counted rows belonging to
  another principal's grants, so page two of the outsider's own facts was decided by
  somebody else's access-control roots; and a walk that pages until a short page arrives
  returned 0 of 6 at page size 1 and 2 of 6 at page size 3, with nothing marking the loss.
  The filter is now a condition on the statement that carries `LIMIT`/`OFFSET`. No
  truncation flag was added and none is needed: inside the statement `LIMIT` counts visible
  rows, so a short page genuinely is the end of the result.
- **The settling walk offered a shared derived root an unasked-for LLM summarize.**
  `settle_after_task` runs after every completed task and consults the rollup planner at
  every ancestor; the planner offered `summarize` to any node with children whose
  `(task, param_fingerprint)` was not already attempted, and the fingerprint carries
  `child_count`, so a new child always made a new one. A root minted by `derived_roots`
  holds whatever derivations filed under one access key, so the first node filed under a
  shared root bought an LLM summary of a container nobody assembled. `IngestRollupPlanner`
  now returns nothing for a node outside an ingest tree, and the predicate is the tree
  root's `usetype` — the same fact that holds the root out of retrieval — rather than
  `produced_by`, which `DocumentRepository.create` leaves NULL on a root. The test is
  negative: everything is an ingest tree unless its root says otherwise, so a tree built by
  hand through `POST /documents` still rolls up.
- **A missing shape was retried three times.** `ShapeNotInOntologyError` subclassed
  `LookupError` and a comment claimed a `LookupError` is PERMANENT;
  `classify_exception`'s permanent tuple names `KeyError`, not `LookupError`, so it fell
  through to the retryable default and a binding whose shape had been replaced away burned
  three backoffs before settling failed. It is a `ValueError` now. The tuple was
  deliberately not widened — `LookupError` would take `IndexError` with it, and an index
  error is as likely to be transient as permanent.
- `"summarizes"` was spelled twice, as a literal in `summarization.py` and as a constant in
  `rollup_tasks.py`. One derived-tree projection follows one edge type, and two spellings
  would make it ask which derivation produced a node before it could follow anything. The
  name now lives on the model.
- `refresh_index`'s comment claimed `search_term_postings` has no `(index_id, document_id)`
  index. Migration `007` added one and `schema.sql` creates it on a fresh install; the
  set-based rebuild is still right, for the round trips rather than for the scan.
- **A RAPTOR summary took its members' parent slot.** `raptor_summarize` reparented every
  member under the summary; the `summarizes` link already recorded that relation
  many-to-many, and one `parent_id` cannot represent a node Leiden placed in two clusters.
  The summary now owns its members by link alone.
- **A relative date in fact extraction resolved against the clock of the run, not of the
  document.** Re-ingesting the same source a month later produced a different date for "last
  Tuesday" and neither was flagged. The prompt now carries `Document.event_time` where the
  caller set one and `created_at` otherwise, and instructs the model to leave the field null
  rather than guess when an expression cannot be resolved against it.
- **Three office defects in released code**, each with a deterministic corpus fixture: a
  sheet declaring 275 billion cells was scanned until the lease expired and took the worker
  with it (`measure_sheet` now refuses a declared extent past 100,000,000 cells, a bound read
  off 35,547 real worksheet parts); a chartsheet raised
  `'Chartsheet' object has no attribute 'max_row'`; and `probe_patterns` caught
  `zipfile.BadZipFile` but not the `zlib.error` a damaged deflate stream raises one layer
  down, which graded a permanent failure as retryable.
- **A NUL byte in a PDF text layer cost a 900-page book every vector, three times over.**
  PostgreSQL stores that character in neither destination `extract:text` writes to. It is now
  removed from the text and from every string in the evidence record, with the count reported
  as `nul_characters_removed`. The write that failed was also classified retryable:
  `DataError` and `IntegrityError` fell through to the retryable default against
  `task_errors`' own stated policy, so the document spent three attempts on a write that
  could not have succeeded. `OperationalError` keeps its retries.
- `index_document` ran a `DELETE FROM search_term_stats ... WHERE doc_freq <= 0` per document
  per covering index over a table with no index on `doc_freq`. On a first ingest no row can
  have reached zero, so it read every term row and deleted nothing: 30,812 executions and
  249 s on one corpus. It now runs only over terms actually decremented.
- **The sheet profile said which columns exist and no search could reach it.** It carried
  `usetype = 'summary'`, which is in both exclusion lists, so the specification's promise
  that it is "embedded and retrievable like any other node" was false for as long as it had
  been written — and for the 18 of 33 worksheets in one corpus whose first row is not a
  header, the profile is the only text the pipeline produces at all.
- A named search-context preset written `"transcript:*,obsidian:*"` became
  `LIKE 'transcript:%,obsidian:%'` and matched nothing, on every install, with no error to
  say why. See the usetype-filter entry above; no preset is seeded, and a test asserts that
  the table is empty so the absence records a decision.
- The BM25 container pass expanded `documents.path` once per matching posting rather than
  once per matching document; an inner CTE collapses first. Measured on a query matching
  25,420 postings across 18,435 documents: 94.6 ms → 45.5 ms, same result set, same ordering.
- An unrecognised retrieval method name is rejected instead of ignored. The fusion loop is
  a series of `if "name" in methods` tests, so `"maxsim "` with a trailing space, or
  `"hybrid"`, used to contribute nothing and raise nothing. Checked in the contract (so a
  `RemoteJmftsClient` caller fails before the request leaves the process), in the service
  after a named search context resolves, and in the repository for in-process callers.
- `GET /search/?method=` likewise: an unknown name fell through the `elif` ladder into the
  `else` branch and ran a hybrid search under a 200.
- `POST /documents` no longer produces a half-indexed document. It enqueues nothing —
  `index:bm25` belongs to the queued ingest path — so a document created this way carried
  vectors and no postings: findable by `/search/vector` and `/search/fulltext`, invisible
  to the bm25 leg of `/search/hybrid`, whose tuned weight is 0.14 of the fusion.
- `.env.example` omitted `JMFTS_INGEST_SYNC_TIMEOUT_SECONDS` while promising it omits
  nothing; the README named a stale `jmfts-client` pin and a stale operation count. All
  three now have guard tests (`tests/test_doc_drift.py`).

## [0.3.0] — 2026-09-03

### Added

- **One ingest path.** `execute_pipeline` and its `PipelineDefinition` registry are
  deleted; every entry point runs on the task queue. The seven entry points `POST /ingest`
  accepts survive as `INGEST_USETYPES`, a table of defaults rather than a table of stages.
- A content string becomes a file node with stored bytes, so the text and file paths differ
  in where the bytes come from and nowhere else. Fetching is three queue tasks
  (`fetch:url`, `fetch:arxiv`, `fetch:path`) and the fetched bytes are kept.
- A conversation is a probed format rather than a declared usetype.
- `extract:facts`, gated on an option rather than on a measurement.
- Declaration machinery for the ingest pipeline: every handler declares what it reads and
  writes (`jmfts_core/atoms.py`), evidence is rows in `document_evidence` rather than a
  JSON column, a rule names its scope and a node names the rule that produced it
  (`Document.produced_by`), and a guard is a comparison whose right side may be an option.
  The planner reads declarations instead of a ladder of conditionals.

### Fixed

- A missing optional stack answers 501 on every operation, not 501 in one place and 500
  everywhere else.
- The inline ingest drain no longer runs on the event loop.

## [0.2.1] — 2026-08-24

### Added

- Office readers: `docx` and `pptx` become markdown, so an office upload ingests.
- Spreadsheets: a workbook's declared rung is its sheet list; each sheet is measured
  (`profile:sheet`) and its rows extracted as typed JSON (`extract:sheet`);
  `GET /documents/{id}/cells` serves a region from the source blob.
- RDF: a triple store that can say what it holds, in Turtle, both directions. A triple can
  carry a literal object. Vocabularies arrive as the file they are.
- All LLM call sites go through one door.
- Entity nodes live under a root keyed by access rather than by tree position.

### Fixed

- `jmfts-server` parses its arguments, so `--help` is no longer a running server.
- `find_path` walked through literals, which are leaves.
- `app.routes` is not a flat list, and three parity seals had stopped checking.

## [0.2.0] — 2026-08-21

### Added

- **`jmfts-client`, a second distribution.** The wire contracts plus a generated
  `RemoteJmftsClient`; it carries `httpx` and `pydantic` and nothing else, so calling an
  appliance does not mean installing one. The two distributions release in lockstep.
- Office format support in three dependency tiers: probe in the base install, the readers
  behind the `office` extra, LibreOffice as a badged worker image.
- A fidelity corpus for office formats — one vocabulary of pattern names, a manifest, and
  twenty deterministically generated fixtures, none of them committed as bytes.
- `citation`: advisory tasks, and the rectangle a PDF chunk came from.
- PDF tables that parse, with a pattern naming which pages carry them.
- `paragraph_packed` chunking.
- CI: a gate that runs on every push, and a workflow that publishes.

### Fixed

- HTML gets a reader instead of a refusal.
- `probe` stopped scanning for tables; extraction reports them.

## [0.1.1] — 2026-08-20

First release with packaging metadata and the tests that make a release possible.

[Unreleased]: https://github.com/jmccardle/jmfts/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/jmccardle/jmfts/releases/tag/v0.5.0
[0.3.0]: https://github.com/jmccardle/jmfts/releases/tag/v0.3.0
[0.2.1]: https://github.com/jmccardle/jmfts/releases/tag/v0.2.1
[0.2.0]: https://github.com/jmccardle/jmfts/releases/tag/v0.2.0
[0.1.1]: https://github.com/jmccardle/jmfts/releases/tag/v0.1.1
