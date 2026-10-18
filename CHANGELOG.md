# Changelog

Every released version, and what changed in it. Entries are written from the commits in
each release range, not from a plan — a line here is something that shipped.

`jmfts`, `jmfts-client` and — from 0.6.0 — `jmfts-web` release in lockstep: one number,
three wheels, one tag. A version below is every distribution that existed at it; 0.5.2 and
earlier are two.

Dates are the release commit's date. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project uses semantic
versioning with the caveat that it is pre-1.0, so a minor version may break the wire.

## [Unreleased]

### Fixed

- **`PUT /templates/{id}` no longer confirms that a hidden template exists.** It validated
  the category before the access gate, so a caller with no read access who sent an invalid
  category got 422 for a template under an access-control root and 404 for an id that does
  not exist. The gate now runs first: hidden → 404, read-only → 403, and only a caller who
  may write the template is told its category is invalid. Tests now cover every template
  verb in `tests/test_access_template_reads.py`, including search with a category and an
  empty `PUT` (which returns the body and is therefore also a read).

## [0.7.0] — 2026-10-17

**Chunks are measured in words, and a summary stops outliving its source.** The `max_words`
rename and the chunking work around it, PDF tables and spreadsheet cells as anchored nodes,
summary-tree nodes with no vector of their own, ten operations brought under subtree access,
and a standalone console (`jmfts-ui`, unpackaged) with a page preview of a node's anchor.

### Breaking

- **`structure.max_tokens` is now `structure.max_words`** (#1). The option always counted
  whitespace-separated words, never subword tokens. A request or collection option set
  still naming `max_tokens` is refused with a hint to rename it, and `/chunk` answers 422
  to unknown fields. Migration 024 rewrites the stored `options` evidence row; a structure
  task already queued with `max_tokens` is not rewritten (its param fingerprint would stop
  matching) and fails PERMANENT with the rename message, so re-ingest the document. The
  migration refuses to run while such a task could still run (pending, claimed, running,
  batched, or failed and retryable): drain the queue or delete those rows first.
- **A queued `structure:conversation` row from before this release fails PERMANENT** (#4). Such
  rows carry `{}` for their parameters, and the task now reads the whole `structure` group from
  its row; one missing a key is refused, naming the keys and saying to re-ingest the document,
  instead of quietly running on defaults. The prose rungs refuse the same way.
- **`EmbeddingService.chunk_to_fit` is removed.** Its only production caller was the
  conversation rung. Call `chunk_text(text, fits=service.fits_token_window)`, which gives the
  same guarantee.

### Added

- **`jmfts-ui`, a standalone Vue console** (#11): Corpus, a node inspector, Search (the
  retrieval methods side by side) and Ingest (explain before upload, then watch it settle).
  It is not packaged and not part of any wheel; `jmfts-web` at `/app` is unchanged. CI gains
  a `console` job (typecheck, test, build).
- **A page preview of a node's anchor** (`/node/:id/preview` in `jmfts-ui`): the source
  page with the anchor's rectangle drawn on it, beside the appliance's own crop. A refused
  anchor shows the server's 400 detail verbatim and no picture.
- **`JMFTS_EMBEDDING_REVISION`** pins the default embedding model to a Hub commit
  (`d556a88e332558790b210f7bdbe87da2fa94a8d8`), for the model and its tokenizer. An
  overridden `JMFTS_EMBEDDING_MODEL` with no revision loads unpinned and logs one WARNING.
  The reranker is not pinned yet.

- **`sentence_window` chunk strategy.** Packs whole sentences up to the embedding model's
  token window, measured with the real tokenizer through `fits`; it refuses to run without
  one.

- **Spreadsheet nodes carry `cells` source anchors** (#29). `extract:sheet` writes the measured
  used range on the table node and each record's worksheet row (e.g. `A5:C5`) on the record;
  `profile:sheet` writes the used range on the profile node; an empty sheet gets
  `source_anchor.unresolved` with code `empty_sheet`. `/region?anchor=true` on one of them
  now names the call that works, `GET /documents/<sheet id>/cells?ref=…`, where it used to
  name the node's own `/cells`, which refuses every node that is not a sheet. Anchors start at
  `A1` (the existing used-range rule), and a document ingested before this release has none:
  re-ingest it.

- **PDF tables become `table` nodes (`extract:tables`).** A table the text layer renders
  inline is written as a `usetype=table` child of the file node, keyed `p{page}.t{index}`,
  holding exactly the markdown the file node holds for it, with its page, bbox and
  `source_span`, and its own `embed`. `extract:text` measures `pages_with_tables` and
  re-plans the node, which is what makes the row reachable at all: probe cannot measure the
  pattern, so before this the task was "not measured" forever. A re-run matches existing
  children on the key and writes no duplicates, and a second commit of `extract:text`
  enqueues no second `extract:tables`. The table text also stays in the chunk around it, so
  BM25 sees those cells twice; that is accepted for now. One table whose span does not check
  out fails the whole document's `extract:tables`.

- **Two evidence leaves on the `extraction` row**: `extraction.section_ceiling`, the most
  sections either structure rung can cut the text into (the larger of headings and outline
  entries, plus one), and `extraction.tables_found`, the tables a PDF's text layer rendered.
  The first is what the per-section chunk ceiling below reads; the second is what
  `extract:tables` is planned on. A document extracted before this release has neither until
  it is re-extracted.

### Changed

- **A `summarize:tree` node carries no vector** (ROADMAP gap 11). It held a bit-identical
  copy of its source node's: the same text, the same access, and the source is in every
  default result set the summary node is held out of, so the copy answered nothing the
  source does not while taking about a quarter of `idx_documents_embed` and of an
  unfiltered top-40 on a 59k-document corpus. Reach a summary node through its source
  (`structured_content.source_node_id`). **Apply migration 025**, which clears the stored
  copies, deletes orphaned summary nodes (below) and rebuilds `idx_documents_embed` under
  a write lock on `documents`; it aborts, changing nothing, if any summary vector differs
  from its source's. `summary` stays in `search_exclude_usetypes` as an ordinary default.
- **The `extract:tables` not-applicable reason after `extract:text` has run** now says the
  reader ran and does not measure `pages_with_tables` for that format (`.md`, `.docx`),
  instead of repeating probe's "extract:text measures it and re-plans extract:tables".

- **Conversation turns are cut with the `structure` ingest options** (#4), the same
  `chunk_text` call the prose rungs make. With the defaults (`sentence_packed`, 120 words) 300
  one-word sentences become 3 parts, not 300. A transcript ingested before this release keeps
  its one-part-per-sentence tree; nothing re-plans it yet.
- **The `input_tokens` evidence of an LLM summary is the prompt size the endpoint reported**
  (#7), cache reads and writes included, and `null` when the server sent no usage. It used to
  hold the embedding tokenizer's count, which the model never saw; that count is kept as
  `input_embedding_tokens`. Rows written before this release still hold the old number under
  the old key.
- **`/image`, `/region` and `/blob` declare their refusals in OpenAPI** (400/404/409/413, and
  404/409), and a bounding box containing `nan` or `inf` is a 400 where it used to render an
  arbitrary rectangle, as is a finite one beyond float32 range (about 3.4e38; it was a 500)
  (#38).
- **The import report's `unsupported_terms` is documented as "not summarized in the digest"**,
  not "not enforced": SHACL validation reads the stored source, so `sh:pattern` is enforced
  while listed there.

- **EXPLAIN's chunk ceiling counts one `chunk_text` call per section** (#5).
  `_write_chunks` chunks each section separately, so 0.6.2's bound, which assumed one call
  over the whole text, under-reported: it declared 59 for 200 one-letter sections that write
  200. `chunk_count_ceiling` takes `calls`, and the structure rungs pass
  `extraction.section_ceiling`; `calls=1` is exactly 0.6.2's bound. Chunk output is
  unchanged.
- **Retry policy for an LLM failure inside a queued task is decided by the endpoint's HTTP
  status** (#8). `classify_exception` reads `LlmCallError.status` with the same table as an
  httpx status error: a 4xx other than 408 and 429 is PERMANENT, so a prompt over the
  model's context size fails its task on the first attempt instead of spending three
  retries being refused identically. 408 is TIMEOUT; 429 and 5xx are RETRYABLE. A failure
  where nothing answered (refused connection, timeout) carries no status and stays
  RETRYABLE, as before. The status is still read out of τ 0.12's message text, in one
  place; it becomes typed with τ 0.13 (#45).
- **Security floors on three dependencies.** `python-multipart>=0.0.31` (two HIGH DoS
  advisories below it, GHSA-pp6c-gr5w-3c5g and GHSA-5rvq-cxj2-64vf, and `POST /ingest/file`
  is a multipart route), `starlette>=1.3.1`, now declared directly (GHSA-82w8-qh3p-5jfq,
  HIGH, and GHSA-86qp-5c8j-p5mr), and `transformers>=5.10` (GHSA-x9r9-c232-4q39 and
  GHSA-xrqw-3rrv-vx5w, both HIGH). A fresh resolve already picked safe versions; these stop
  an existing environment, or a resolver held back by something else, from keeping one that
  is not.

### Fixed

- **Deleting a document deletes the summary-tree nodes that stood for it** (ROADMAP gap 11).
  A summary node lives under a derived root, so the tree cascade never reached it: the
  source and its `summarizes` edges went, and the node stayed, titled after the deleted
  document and holding its vector — retained content, reachable by any search naming
  `usetype=summary`. 821 such nodes on the stress corpus; migration 025 deletes the ones
  already stored.
- **Ten operations that ignored subtree access now respect it** (#30, ROADMAP known gap 12).
  The five `IndexService` verbs that name a document (`index_document`, add and remove a root,
  `get_index_roots`, `refresh_index`), template get, render **and list**, `file_frontier`, and
  `create_binding`: a caller with no grant sees a governed document as missing (404, or 400 for
  `index_document`), a read-only caller is refused a write (403), and `add_root_to_index` on an
  id that does not exist is a 404 where it was a 500. Owners, unbound callers and deployments
  with no access-control root behave as before. Two counts still include documents the caller
  cannot read (`refresh_index`, `file_frontier`).
- **A workbook no longer ends ingest with a failed `anchor:render`** (#37). The missing-evidence
  sentinel is compared by identity at the three sites that read it with `or {}`, and the
  planner offers `anchor:render` only for sources the handler reads (OOXML).
- **PDF rectangles on rotated pages are stored in the displayed page's space** (#38).
  `get_text` measures the unrotated page while `/region`, `/image` and `page_geometry` read
  the page as displayed, so on `/Rotate 90/180/270` pages every text anchor cropped blank
  paper and `/region` answered 200 with a white PNG. Text rectangles are now turned by the
  page's rotation; table spans are not turned twice (`find_tables` already de-rotates), and
  a table now owns its cells on a turned page instead of emitting them a second time as
  prose. The matrix is read before table detection deletes the CropBox, so a CropBox page's
  anchors no longer land off the page when `extract_tables` is on. **Anchors on rotated
  pages written before this change carry no marker and are not migrated: re-derive them by
  re-running `citation` (text anchors) and `extract:text` then `extract:tables` (table
  spans).**
- **`/region` on a `cells` anchor with no worksheet node above it is a 409, not a 500**
  (#29). It raised an exception neither `/region` nor `/page` maps. The 409 keeps the
  anchor's `ref`. A sheet node ingested before cells anchors existed now says to re-ingest
  it (`extract:sheet`, `profile:sheet`) instead of pointing at `citation`, which reads PDFs
  only.
- **`/region?anchor=true` refuses an anchor that crops blank paper** with a 400 that says
  so, and on a rotated page names the re-derivation above. Only a stored anchor that renders
  entirely white is refused: a rectangle a caller names, or a crop that is one non-white
  colour (inside a filled box), is still a 200. The check is one bytes comparison;
  `Pixmap.is_unicolor` took 17–40 s on a blank 8.4 Mpx crop.

## [0.6.2] — 2026-10-12

**A refused request says why, and a stale field says it was ignored.** The bugs and small
fixes from the 0.6.1 cutover and the issue triage after it, the dependency floors brought up
to what the suite runs on, and ten dead items the restored dead-code scan found.

### Breaking

- **`JMFTS_EMBEDDING_BATCH_SIZE` is gone, because nothing read it.** `.env.example` set it
  uncommented, so a copied `.env` carried a knob that did nothing. If a `.env` FILE still
  sets it, `Settings()` now refuses to start with
  `jmfts_embedding_batch_size — Extra inputs are not permitted`; delete the line. Set as a
  process environment variable it is ignored, which is pydantic-settings' own asymmetry.
- **Synthesis no longer turns an unexpected error into `llm_available: false`.** It catches
  only LLM failures now; any other exception reaches the caller as a 500 rather than a 200
  that looked like a missing LLM (#32).
- **Dependency floors rose to the versions the suite runs against**, among them fastapi
  0.135, transformers 5.5, numpy 2.4, sentence-transformers 5.3, pgvector 0.4 and
  `ffwf-tau-llm` 0.12.0 (#10, #31). There are still no upper bounds; `pyproject.toml` says
  why. The old `pgvector>=0.2.4` floor admitted a version with no `HALFVEC`, which fails at
  import.

### Added

- **`X-JMFTS-Ignored-Fields`.** A JSON request body carrying fields the operation's contract
  does not declare still succeeds, but the response now lists them (`max_context_tokens`,
  `inner.shade`, `items[].hue`) and the server logs one WARNING per route and field set.
  tectum's `max_context_tokens`, dropped silently since 0.6.0, is the case that found it.
  Rejecting such fields (`extra="forbid"`) is planned for 0.7.0 (#33).
  *(Corrected 2026-10-17: moved to 0.8.0, for every request contract — #47.)*
- **`llm_error` on the synthesize response:** `{reason: not_configured|refused|failed,
  status, message, attempts}`. `llm_available` is unchanged on the wire (#32).
- **A 502, 503 or 504 from the LLM is retried during synthesis**, the wait doubling from
  0.5 s, within `JMFTS_SYNTHESIS_RETRY_BUDGET` (default 4 s; 0 disables). EnsoNet's
  "model is loading" 503 is what made midlife's first synthesis after the 0.6.1 cutover
  report `llm_available: false`. A 400, including over-context, is reported and not retried.
- `tests/test_token_proxy_audit.py`: a character or word count may not stand in for a token
  count outside an allowlist keyed by symbol, not line (#9).

### Fixed

- **An uncached tokenizer on an offline host raises `TokenizerNotCached`**, which names the
  model and the three ways out, classifies PERMANENT in the queue and answers 501 over REST.
  It was a raw `OSError` from `transformers`, and a bare 500 (#6; bundling the tokenizer
  stays open).
- **`POST /documents/{id}/embed` takes the subtree write gate**: 404 for a document the
  caller cannot read, 403 for a read-only one, like the other write verbs. One of the ten
  verbs of ROADMAP gap 12 (#30).
- **EXPLAIN's chunk ceiling is an upper bound for one `chunk_text` call** (`chunk_count_ceiling`).
  `characters // min_chunk_length` was exceeded twice over: a merge never crosses
  `max_chars`, and `fits` splits below `min_chunk_length`. Chunk output is unchanged. The
  bound is loose, and a document with many sections can still exceed it (#5).
- `search_exclude_usetypes` is documented as load-bearing: it is what keeps the summary
  tree's 13,755 duplicate vectors out of results (#17).
- The `--dsn` examples in five measurement scripts name the `psycopg2` driver (#25), and the
  README's held-back list no longer names a document nothing cites (#26).
- `scripts/run_tests_docker.sh` waited for postgres over the unix socket, which the image's
  temporary initdb server also answers; the re-check could land in the restart and abort the
  run with `TIMED OUT`. It checks over TCP now.

- **`wiki:arxiv` no longer claims to keep paper metadata.** The usetype description, the
  generated `docs/reference/INGEST.md` and the ingest skill said it fetched "metadata +
  PDF" and filled in title, authors and abstract; it has stored the PDF only since the
  evidence migration. The skill's account of what the pipeline writes into
  `structured_content` was false more widely: it writes nothing there, and measurements are
  read from `GET /documents/{id}/evidence`.

### Removed

- Dead code found by `scripts/deadcode_scan.py`, restored to the tree on 2026-10-12: the arXiv
  Atom-feed metadata fetch, `DocumentRepository.append_attempts`,
  `SearchContextRepository.get_by_id`, `app.state.ingest_worker`,
  `SheetMeasurement.merged_ranges`, `Prepared.input_tokens` in `jmfts_batch`, and a loop in
  fact extraction whose body was `pass`.
- `uv.lock`. It described jmfts 0.1.0, nothing installed from it, and pip is the installer
  CI, both Dockerfiles and the README use (#31).

## [0.6.1] — 2026-10-12

**The LLM URL is the one your server advertises, and EnsoNet becomes reachable.** One fix,
found by the first production cutover to 0.6.0.

### Breaking

- **`JMFTS_LLM_BASE_URL` is used as given; JMFTS no longer appends `/v1`.** Set it to the
  base URL the server advertises, version segment included — `http://gpu-box:8080/v1` for
  llama-server, vLLM or Ollama, `http://host:8853/v1d` for EnsoNet. JMFTS appends only
  `/chat/completions` and `/models`, which is the OpenAI SDK's convention. Before, it
  wanted the server root: pasting the advertised URL 404'd at `/v1/v1`, which
  `.env.example` had to warn about, and a server that versions under any other segment
  could not be reached by any value of the setting — EnsoNet serves `/v1d`, so a midlife
  deployment had no working LLM configuration at all. **A config that gives a server root
  now 404s**; `GET /health/llm` names the URL it tried and suggests the `/v1` form. The
  `jmfts_batch` mock provider follows the same rule.

### Changed

- **`GET /health/llm` makes one outbound call, not two.** It asks `{base}/models`. The
  `{base}/health` call it also made is an EnsoNet extension at the server root, which a base
  URL carrying its version segment no longer reaches.

## [0.6.0] — 2026-10-12

**A web front end, and the appliance becomes something you can look at.** Drop a file on a
page, see what the ingest will do before it does it, search it with seven methods side by
side, click a hit, and see the passage boxed on the page it came from — for a PDF, and now
for a `.docx`, `.xlsx` or `.pptx` too. `jmfts-web` is a third distribution: built static
files and no dependencies, mounted at `/app` when it is installed. Everything it does goes
through the generated client, and the call-log page shows each request as `curl`,
`RemoteJmftsClient` and `fetch`, so "it thinly wraps the API" is a claim you can read off the
page.

Behind it: two access gates that did not close now close, a third was found by a standing
audit and closed, a spreadsheet's rows reach BM25, the job system's `EXPLAIN` says how many
rather than only which, and the specifications and measurement records the source cites by
section are published.

Built in parallel worktrees under `docs/SPRINT_0_6_0.md` Part 5's merge gates M0–M6; that
plan is published and is the long version of everything below.

### Breaking

- **The graph-analytics verbs compute over the caller's graph, not the corpus.**
  `GET /graph/centrality`, `/graph/subtree-authority`, `/graph/spines` and
  `/graph/communities` took no principal and returned ids and titles of documents that were
  404 to the same caller everywhere else. They now build the graph from the documents the
  caller can read, so **every score a governed caller reads moves** — question 4.2: a
  centrality score is a property of the graph it was computed on, and filtering the result
  instead would have reported numbers derived from rows the caller cannot see. Owner,
  unbound and ungoverned callers get byte-identical answers, held by
  `test_centrality_is_unchanged_where_nothing_is_governed`.
- **Writing an edge or a triple takes write access.** `POST /documents/{id}/links` needs
  write on the source and read on the target; the five `TripleService` write verbs need
  write on the subject and read on the object. Both succeeded for a read-only principal.
  An ungoverned node is still open — access control stays opt-in.
- **`SynthesizeRequest.max_context_tokens` is removed.** It was a budget converted to
  characters at four per token and used to drop results from the tail, so an answer could
  cite four of twelve retrieved documents and read as though all twelve were considered.
  Every `top_k` document is now sent whole; `top_k` is the control. Contracts ignore unknown
  fields, so an old client that still sends it is accepted and the value does nothing.
- **`/search/auto` routing signals are renamed** `token_count` → `word_count` and
  `content_token_count` → `content_word_count`. They were always word counts.
- **`JMFTS_SUMMARIZATION_CONTEXT` is gone, and a `.env` that still sets it stops startup**
  (`Settings` forbids unknown keys). Delete the line. Set in the process environment it is
  ignored.
- **Fact extraction and RAPTOR send their input whole instead of cutting it.** Input over
  240,000 characters (fact extraction) or 16,384 (a RAPTOR cluster) used to be truncated
  silently and the result stored as if it covered everything. An input too large for the
  model's context now fails: in the queue, after three retries, because the LLM client cannot
  yet tell a context-length refusal from a transient error; through
  `POST /documents/{id}/raptor`, as a 500. Synthesis catches the refusal and answers
  `llm_available: false`, which is indistinguishable from "no LLM configured" — recorded
  as open, not fixed.

### Added

- **`jmfts-web`**, the browser front end (`pip install jmfts[web]`, or the `jmfts-web`
  wheel). Views are files registered by their own module, so adding one touches no router:
  - **Ingest** — drag and drop runs `POST /ingest/analyze` (probe, and nothing else) and
    shows `EXPLAIN` before the button that ingests exists; afterwards the settling
    frontier, which never reports a draining queue as done.
  - **Search** — one query in seven columns, the count read off the route table; contexts;
    synthesis as a panel. **Presets** saves a search configuration and runs it.
  - **Tree browser** and **document detail** — every child shape, including the several a
    sheet emits; the detail view renders the source page and draws the cited rectangle.
  - **Call log** — every request the page made, newest first.
  - Renderers for markdown and transcripts render them as documents rather than as source.
- **Three byte routes**: `GET /documents/{id}/blob` (the original bytes), `/image` (one
  rendered page) and `/region` (a rectangle, from an anchor or explicit bounds).
  `ExposeSpec.media_type` lets an exposed operation answer with something that is not JSON.
- **A generated TypeScript client** in `jmfts-web/jmfts_web/static/client/`, rendered from
  `/openapi.json` (`python -m scripts.generate_ts_client`), and the call event every view
  emits.
- **A search hit carries its anchor.** `source_anchor` and, for office formats,
  `render_anchor`, each with an `.unresolved` companion; the typed anchor contract is
  `contracts/anchor.py`. `ViewResponse.can_write` says whether the caller may write.
- **Office citations.** `render:pdf` and `convert:ooxml` (a legacy `.doc`/`.xls`/`.ppt` to
  its OOXML package) through LibreOffice, behind `jmfts_core/convert.py` and the `convert`
  extra (the unoserver client). `Dockerfile.libreoffice` is a LibreOffice-only container and
  `deploy/k8s/23-worker-convert.yaml` puts a pool of ordinary workers in front of it.
  Renditions get their own table, `document_renditions` (migration 023), because uploaded
  bytes are immutable. `anchor:source` writes an exact `ooxml` address (part and XPath) on
  every office chunk; `anchor:render` recovers a rectangle against the rendition and writes
  it to a separate row, labelled `of: "rendition"`, so a recovered rectangle is never
  mistaken for an exact one.
- **Leaf-to-parent projection.** `SearchRequest` and `HybridSearchRequest` take `project`:
  match over `match_limit` leaves (default 5 × limit), then return their parents scored by
  `max`, `sum` or `count`, each carrying `via`, the leaves that put it there.
  `applied.projection` accounts for every leaf. `/search/auto` and `/search/synthesize` do
  not take it.
- **`POST /graph/seeded-rank`** — personalized PageRank restarting at seed documents over
  their `hops`-neighbourhood, with `relations` filtering by `link_type` and triple
  predicate. An unknown relation name is a 400, because an empty edge set reads like "not
  related". Over the same access-gated graph as the analytics verbs.
- **A collection configures what is ingested under it.** `GET` and
  `PUT /documents/{id}/ingest-options` on a collection node; the setting reaches every
  document beneath it, and RBAC's "may write this node" answers who may set it. No new
  table. `POST /ingest/analyze` takes `parent_id` so the forecast sees those options.
- **`EXPLAIN` says how many.** The cost fold returns intervals for tasks, child nodes,
  embeddings and LLM calls, and `PlanCost.unmeasured` names what it could not count and
  who would measure it, instead of guessing.
- **A transcript's `EXPLAIN` has a number.** `structure:conversation` declares a ceiling of
  `line_count + char_count` chunks (one per turn, and at most one per character of an
  over-window turn), and `Atom` now refuses any atom that writes children without a bound,
  so "writes `?`" can no longer be declared by omission. The ceiling is loose by design: an
  over-window turn is currently split one sentence per part, and the tight bound waits on
  packing those parts to the window.
- **Spreadsheet header detection** (0.4.0's deferred Block C): the header is found in rows
  1–8 and carried as a measurement, numeric headers are allowed, a whole sheet is also
  emitted as one `table` node at the 8192 window, both shapes are written where both match,
  and a headerless table takes its column labels from a merged banner row.
- **`propose:links` finds nested columns** by an inclusion-dependency search over the MinHash
  sketches, verified exactly on the survivors.
- **Published documentation.** `docs/INGEST_SPEC.md`, `docs/SPRINT_JOBS.md`,
  `docs/OFFICE_SPEC.md` and seven measurement records, each with a dated review note; 633
  citations from shipped files now resolve. The sprint plans before 0.6.0 stay unpublished
  by decision, so 224 citations to them stay dangling — see the README's note.
- **A standing access audit**: every `@expose`d verb either takes a principal or is listed
  with the reason it does not. Its first run found the triple-write gap above.

### Fixed

- **A workbook's rows reach BM25.** `index:bm25` is planned at the settling boundary, so a
  string in a cell is found; before, an ingested spreadsheet returned nothing.
- **A `PUT /search-contexts/{id}` that omitted `description` set it to null.**
- **A collection's own ingest options no longer mask the ones set above it.**
- **The six tests gated on an internal-tree marker ran nowhere**, because the marker file
  is not in this tree. They are ungated and run.
- **`docker compose up` serves `/app`.** The dev image did not install `[web]`.
- **The browser keeps the API token for the browser, not one tab.**
- Migration 015's evidence key list names `table`.
- **`scripts/benchmark_multihop.py` chunks the way production does.** It called its own word
  window, not `chunk_text`, and described 256 words as matching a reference whose 256 is
  subword tokens. Chunk counts differ from runs recorded before 0.6.0.

### Measured

- **The MaxSim recall threshold has a distribution, and it did not move.** 79 readings: 60
  HNSW graphs over one corpus and 19 whole-process runs, 216/240 to 224/240, mean 0.9175,
  sd 1.67 documents, none below 0.90. `docs/MEASURE_MAXSIM_MARGIN.md`.

## [0.5.2] — 2026-10-03

**A fresh install connects to its database again, and a test now holds the driver name.**
0.5.1 built its connection string as `postgresql://`, which means "SQLAlchemy's default
driver". That default was psycopg2 through SQLAlchemy 2.0 and is psycopg 3 from 2.1, and
`pyproject.toml` declares `psycopg2-binary` while leaving `sqlalchemy` unbounded above. An
install that resolved SQLAlchemy 2.1.3 therefore failed its first connection with
`No module named 'psycopg'`, and `/health` answered `status: degraded` with that text.
Found by building the development image on 2026-10-03; an environment that still holds
SQLAlchemy 2.0 never saw it.

### Fixed

- **`Settings.database_url` names its driver**, `postgresql+psycopg2://`. The URL has one
  consumer, `create_engine` in `jmfts_core/database.py`, so the change reaches every caller.
- **`tests/test_database_url.py`** asserts the driver name and that the driver imports in
  the environment under test. The second assertion is the one that fails on the old URL,
  and only where SQLAlchemy is 2.1 or later.

## [0.5.1] — 2026-09-12

**Development moves into this repository.** Up to 0.5.0 the public tree was a per-release
squash — one commit per version, copied out of a private tree by a publish step, with the
working record left behind. From this release there is ONE history on two remotes, a
private git host and `github.com/jmccardle/jmfts`, and a commit reaches both as it is made.
The squash chain below is kept as it stands; nothing already published is rewritten. What
changes for a reader: the commits are the ones that were actually written, the branches are
real, and the release history stops being a thing you are told about. A pass over the
held-back documents is still outstanding, so a source comment may still cite a file this
tree does not carry — see "A note on documentation" in the README, which is now a shrinking
list rather than a standing policy.

### Fixed

- **The ANN gate test that failed public CI on 0.5.0 was asserting a cost estimate, not the
  ORDER BY it was written for.** `tests/test_filtered_recall.py::test_shipped_order_by_reaches_the_index`
  read the planner's UNFORCED choice of plan, and on its 230-row fixture that choice is a
  cost comparison whose answer depends on whether `documents` carries statistics — which
  `jmfts_test` keeps for the whole run, outside the fixture's rollback, at autovacuum's
  discretion. Measured on pgvector 0.8.6 / PostgreSQL 16.15, prices for the shipped
  statement in the three states the suite can put the database in:

  | state | HNSW scan | cheapest sorting plan | unforced pick |
  |---|---|---|---|
  | no statistics | 8.04 | 14.72 seq | HNSW |
  | `ANALYZE`, no residue | 517.75 | 16.60 seq | seq + sort |
  | `ANALYZE`, 3000 dead rows | 6675.13 | 609.52 bitmap | bitmap + sort |

  Only the first reaches the index, and it is the state a fresh database is in — which is
  why this passed on a development machine running one file and failed on a runner running
  the whole suite into one database. **There was nothing to bisect**: the suite is green
  here at the same commit that is red there. **The first repair was wrong and CI refuted
  it**, which is recorded rather than quietly replaced: `enable_seqscan = off` moves only
  the first row of that table, and the runner came back with `Bitmap Heap Scan on documents
  (cost=565.28..626.27 rows=31 width=620)`. The test now makes its statistics rather than
  finding them, and disables sequential scans, bitmap scans and sorts together — every
  route that produces distance order by SORTING — so that the only way left to answer is an
  index that yields `embed <=> q` order natively, which is what step 1 asks. The 0.3.0
  `ORDER BY score DESC` is kept in the test as a control arm and must NOT reach the index
  under the same forcing, so a forcing that stopped discriminating fails loudly instead of
  passing. Re-gated in all three states, plus a fourth at 12000 dead rows. What the test no
  longer claims is that PostgreSQL will CHOOSE HNSW at this size; asserting that needs
  thousands of rows, which is a benchmark and not a gate.

- **The intermittent ANN failures under 0.5.0's "Known, and shipped anyway" were the test
  fixtures, not the appliance.** Both files stored ONE vector in every document and then
  queried with that same vector. Each test rolls its transaction back, and a rolled-back row
  leaves its node in the HNSW graph until `VACUUM` runs — removing a node means repairing
  its neighbours' adjacency lists, so pgvector defers it. Repeating the fixture piles dead
  nodes onto the exact point the next query enters at, and the scan walks them instead of
  the live rows. Reproduced standalone in about forty lines of SQL against
  `pgvector/pgvector:pg16` (PostgreSQL 16.15, pgvector 0.8.6), with no application code:
  seven rows carrying one vector, inserted, queried, rolled back, 200 times.

  | 200 rounds, same partial HNSW index | rounds returning fewer than 7 live rows |
  |---|---|
  | one shared vector, `hnsw.iterative_scan = off` | 59 |
  | one shared vector, `relaxed_order` | 24 |
  | one shared vector, `strict_order` | 2 |
  | seven DISTINCT vectors at the same cosine distance | 0 |

  A sequential scan returned seven every time. **Rollback is not the ingredient**: committed
  `DELETE`s reproduce it at the same rate or higher (80 rounds short against 52 at `off`),
  so this is dead-node density rather than a transactional edge case. The realistic shape —
  one document re-ingested 800 times, 1550 dead nodes, committed, never vacuumed — found its
  live row on every attempt in both scan modes. `tests/test_usetype_filter.py` now seeds a
  fresh random neighbourhood per fixture and gives each document its own vector on it, and
  `tests/test_search_regression.py` does the same for the seven documents it stored the
  query vector in. Measured over 200 rolled-back rounds in one database: 67 rounds short with
  the old shared vector, 40 with distinct vectors at fixed points, **0** with the moving
  neighbourhood. Three full-suite runs afterwards: `2480 passed, 40 skipped` twice, and a
  third that failed only the two tests below. **This is not a defect against `jmfts_core`**,
  so it is not a 0.6.0 step; it is a documented pgvector limitation that the fixtures were
  walking into.

- **`tests/test_maxsim_recall.py` could fail on a measurement that met its threshold.** The
  third run above read `assert 0.8999999999999999 >= 0.9`. That value is one ULP below 0.9,
  and the mean cannot land near it by measurement: each `recall@10` is a multiple of 1/10
  over 24 queries, so every achievable exact mean is a multiple of 1/240 and the nearest one
  below 0.9 is `21.5/24 = 0.8958333333333334`. The measurement was exactly 0.90 and
  `sum(values) / len(values)` lost it to accumulation order — over 200,000 shuffles of a
  24-value multiset whose exact mean is 0.9, that expression ranges from `0.8999999999999996`
  to `0.9000000000000004`, straddling the comparison, and the order is the order the queries
  came back in. The tests now compare against `RECALL_FLOOR`, which is the bottom of that
  range less 1e-15 — about 13 ULP under 0.9. **The threshold is still 0.90**: one query
  losing one neighbour reads 0.8958333333333334, which is 0.0042 below the floor and still
  red, four hundred times the tolerance. Twelve consecutive runs of the file read 0.9042 to
  0.9333 (217/240 to 224/240), none of them on the tie, so the twelve are evidence about the
  margin on this tree rather than about the repair.

**Still open, and unchanged by this release:** the zero-row index scans described under
0.5.0, and the *margin* on `tests/test_maxsim_recall.py`'s 0.90 threshold, which is a
different question from the tie fixed above and is not fixed. Public CI read `21.5/24` on
0.5.0 — one document short, genuinely short — and the twelve runs here bottom out at
`217/240`, two documents above that. Both remain 0.6.0 entry conditions. The statistics
mechanism above is a plausible account of the first and has not been measured against it.

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

[Unreleased]: https://github.com/jmccardle/jmfts/compare/v0.6.2...HEAD
[0.6.2]: https://github.com/jmccardle/jmfts/compare/v0.6.1...v0.6.2
[0.6.1]: https://github.com/jmccardle/jmfts/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/jmccardle/jmfts/compare/v0.5.2...v0.6.0
[0.5.2]: https://github.com/jmccardle/jmfts/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/jmccardle/jmfts/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/jmccardle/jmfts/compare/v0.3.0...v0.5.0
[0.3.0]: https://github.com/jmccardle/jmfts/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/jmccardle/jmfts/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/jmccardle/jmfts/compare/0.1.1...v0.2.0
[0.1.1]: https://github.com/jmccardle/jmfts/compare/0.1.0...0.1.1
