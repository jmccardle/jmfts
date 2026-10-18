# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

JMFTS (John McCardle's Fusion Tree Search) is a research-focused retrieval appliance combining matryoshka embeddings, ColBERT-style late interaction retrieval, and BM25 hybrid search over PostgreSQL with pgvector.

## Common Commands

```bash
# Install (editable mode). Base does NOT include torch — the model stack is the `embed`
# extra, because the only step that needs it is producing vectors and a worker can ask
# another JMFTS for those (JMFTS_RUNNER_URL). See the `embed` extra in pyproject.toml.
# jmfts-client FIRST, always, from this tree. `jmfts` depends on it with `==`, so an
# editable server install would otherwise resolve the PINNED version from PyPI and shadow
# the tree you are working in. (0.2.0 is on PyPI; earlier wording here said the name was
# not published, which stopped being true when step 5 of docs/RELEASING.md first ran.)
pip install -e ./jmfts-client
pip install -e ./jmfts-web  # only for /app; nothing else needs it and [dev] does not imply it

pip install -e .            # base: 586 MB, tokenizer only, cannot embed by itself
pip install -e ".[embed]"   # + torch and sentence-transformers (5.2 GB total)
pip install -e ".[office]"  # + python-docx/python-pptx/openpyxl (44 MB, pulls lxml+Pillow)
pip install -e ".[rdf]"     # + rdflib/pyshacl; Turtle in, Turtle out, SHACL shapes
pip install -e ".[sketch]"  # + datasketch; MinHash per column, for propose:links
pip install -e ".[convert]" # + the unoserver CLIENT only; the LibreOffice tier is an image
pip install -e ".[web]"     # + jmfts-web, the console at /app (install ./jmfts-web first)
pip install -e ".[dev]"     # + pytest/black/ruff; implies [embed] [office] [rdf] [sketch]

# Database setup
jmfts-init-db
jmfts-init-db --pending     # shipped migrations THIS database has not applied (exit 1 if any)

# Run API server
uvicorn jmfts_core.rest.main:app --host 0.0.0.0 --port 8100 --reload
jmfts-server                # the same app, installed entry point; --help shows resolved settings

# Batch summarization through a provider's batch API (jmfts_batch/README.md)
jmfts-batch-worker --help

# Retrieval evaluation (jmfts_core/eval/): BEIR ingest + nDCG@10/MAP/recall/MRR, one JSON
# per dataset in ./eval-results/. Needs --data-dir or JMFTS_EVAL_DATA_DIR, and a database
# holding nothing but BEIR corpora (point JMFTS_DB_NAME at one). Over-window docs: error.
jmfts-eval beir --list
jmfts-eval beir -d scifact --data-dir DIR --methods vector bm25 hybrid 'vector->crossenc@100'

# Interactive API docs, generated from the live route table — Swagger UI at /docs, ReDoc at
# /redoc, the document at /openapi.json. All three answer without a token; Authorize with
# JMFTS_API_TOKEN before trying an operation. Tag descriptions come from the service class
# docstrings via build_openapi_tags(), so there is no separate list to keep current.
#   http://localhost:8100/docs

# What this appliance accepts, asked without sending it anything: installed extras, whether
# it can produce a vector at all, the formats it identifies from the bytes, the ingest entry
# points, the retrieval methods and their weights, and the usetypes held out of every result
# set. `?corpus=true` adds the counts that say whether a method will return anything here.
# Answered from the live registries; it makes no network call.
#   GET /capabilities
#
# Health: `/health` is the CHEAP one (one SELECT 1, no token) and is the probe path.
# `/health/llm` runs the LLM reachability probe — one outbound call at 5 s — and
# needs a token. `/` is the same cheap check but gated. See deploy/k8s/30-api.yaml.
#
# What is NOT protected by any access-control root. A document under no ACR is readable and
# writable by anyone with a token, which is the default; this is how to ask which documents
# are in that state. Owner-only.
#   GET /access/audit

# Ingest worker (drains the task queue; --runner-url points it at another JMFTS for vectors)
jmfts-worker
jmfts-worker --runner-url http://the-gpu-box:8100

# Tests — run against an ISOLATED, empty test DB (never production).
# conftest.py auto-provisions/drops a `jmfts_test` database and points the app
# at it, so the suite can no longer touch the real appliance DB. This needs a
# role that can CREATE DATABASE:
./scripts/run_tests_docker.sh         # easiest: throwaway pgvector + CPU embed
pytest                                # native: needs `ALTER ROLE jmfts CREATEDB`
                                      #   (or point JMFTS_DB_* at the docker stack)
pytest -v -s                          # verbose with output
pytest path/to/test_file.py::test_fn  # single test

# Does the base install really work without torch, and without the office readers? The
# suite cannot answer that from inside an environment that has both, so this builds an
# empty venv and checks there.
./scripts/check_base_install.sh

# Formatting & linting. THESE FIVE PATHS, not `.` — see "The gate" below.
black --line-length 100 jmfts_core jmfts_batch jmfts-client jmfts-web tests
ruff check jmfts_core jmfts_batch jmfts-client jmfts-web tests

# Benchmarks (in scripts/, outside the gate; they need datasets/, which is local)
python -m scripts.benchmark_multihop --quick
python -m scripts.benchmark_multihop
python -m scripts.benchmark_centroid_recall
```

### The gate

`black` and `ruff` run over `jmfts_core jmfts_batch jmfts-client jmfts-web tests` —
everything that goes into one of the three wheels, plus the suite that proves it.
`.githooks/pre-commit` and `.github/workflows/ci.yml` run the same five paths, so the
three cannot disagree. (Until 2026-10-17 it was three paths, and "everything in a wheel"
was false: `jmfts_batch` ships in the `jmfts` wheel and `jmfts-web` is a wheel. Both joined
already clean.)
`scripts/` is outside the gate on purpose — it is tooling and one-off research, not a
wheel — and `ruff check .` reports findings there; that is expected.

Activate the hook once per clone:

```bash
git config core.hooksPath .githooks
```

`docs/RELEASING.md` is the full runbook for cutting a release. It was held back until
2026-10-17, when it had its review pass and was published; the readings of its citation
count further down predate that and still list it as held.

### Auditing for dead code

**Do not run `vulture` directly on this tree.** It reports about 256 findings and almost none
are real: `@expose` and `@register_task_handler` make about a hundred functions reachable
with no in-repo caller, and Pydantic fields, SQLAlchemy columns and the generated client
account for most of the rest.

```bash
pip install vulture                              # not a project dependency, on purpose
python -m scripts.deadcode_scan                  # what nothing in the repo calls
python -m scripts.deadcode_scan --published-only # what nothing in the WHEEL calls
```

It finds `vulture` next to the running interpreter first, then on `PATH`, so
`.venv/bin/python -m scripts.deadcode_scan` works without activating the venv.

**It was missing from this tree from the 0.5.1 history switchover until 2026-10-12.** It was
held back as an internal audit (`NOT_PUBLISHED`), and when it came back it was published,
which retired `NOT_PUBLISHED` and `conftest`'s internal-tree marker with it.

The script builds its allowlist from the **live** registries — it imports the app and
reads `REGISTRY`, `TASK_HANDLERS`, the mounted route table, contract `model_fields`, the
SQLAlchemy mappers, plus the fields and validators of every Pydantic model, and members of
enums a mapped column stores — so deleting an `@expose` drops its name from the allowlist the same
day. A checked-in list of excused names would hide exactly what the audit is for. That
takes 260 findings down to 12, read 2026-09-10 (770 names excused). It was 17 on 2026-09-05
and 12 on 2026-09-06, and the five that cleared are the reason to run the scan rather than
maintain a list: `SPRINT_0_5_0.md`
step 11 landed and gave `jmfts_core/derived_roots.py` and the two ledger readers in
`jmfts_core/sql/__init__.py` their callers on the same day, with no edit to any allowlist.
One survivor is still in that file — `derived_roots.py:157 derived_root_ids`, which says so
at the definition. **The count held at 12 across four days and sixteen commits, and that is
a reading too, not a target**: the raw number moved 258 → 270 over the same window, which is
the allowlist doing its job rather than the tree getting cleaner. `--published-only` reads
42.

Read again 2026-10-12, after the scan was restored and taught the three wheels, validators,
wire-model fields and stored enum members: **4 findings, 29 `--published-only`, 862 names
excused.** Down from 14 and 42 the same day because ten were dead and were deleted (the
arXiv metadata fetch, `JMFTS_EMBEDDING_BATCH_SIZE`, `append_attempts`,
`SearchContextRepository.get_by_id`, `app.state.ingest_worker`, `merged_ranges`, the batch
`input_tokens`, a `pass` loop), and four were false positives the scan now excuses. The
four left are `derived_root_ids` (documented), `CONVERT_POLICY` (a preset operators are
pointed at), and `TASK_STATUSES` / `EVIDENCE_STATES`, copies of two CHECK constraints that
nothing ties to the SQL. Run the scan rather than trusting any of these.

Two questions, two modes. The default counts `tests/` and `scripts/` as callers. The
`--published-only` mode does not, which surfaces code whose only caller lives outside the
WHEELS — all three, read from each distribution's `packages.find` table rather than listed
(`jmfts_core`, `jmfts_batch`, `jmfts_client`, `jmfts_web`), so code reachable only from
`tests/` or `scripts/` is reachable in the repository and dead in an installed appliance.
(Both of those directories ARE in `tests/test_readme_links.py::PUBLISHED` — that constant is
what the public *repository* carries, which is a different question from what a wheel
carries.)

Neither mode is a delete list. Coverage answers a different question again: 0% covered
means untested, not dead — a module reached only through a network fetch is the usual case,
because no test makes a network call.

## Architecture

```
API Layer (FastAPI)          → jmfts_core/rest/main.py, rest/wiring.py, rest/routers/
Contracts (Pydantic)         → jmfts-client/jmfts_client/contracts/ — the single definition
                               of every shape (a SEPARATE distribution; see below)
Services                     → jmfts_core/services/ (document, search, ingest, graph, …)
Repository Layer             → jmfts_core/repositories/ (search.py, document.py, task_queue.py)
Domain logic                 → jmfts_core/embedding.py, token_selection.py, chunking.py
Office readers               → jmfts_core/office/ — the only door to the `office` extra
ORM Models                   → jmfts_core/models/ (document, document_blob, document_evidence,
                               document_rendition, token_embedding, search_index,
                               search_context, task_queue, triple, ontology, principal,
                               derived_root, entity_root, usetype_presentation)
Infrastructure               → jmfts_core/config.py, jmfts_core/database.py, unit_of_work.py
Database                     → PostgreSQL + pgvector (HNSW indexes), jmfts_core/sql/
Batch summarization          → jmfts_batch/ — `jmfts-batch-worker`, ships in the jmfts wheel
Browser console              → jmfts-web/jmfts_web/static/ — a THIRD distribution, mounted at
                               /app when installed; no Python dependencies
```

### Three distributions

This tree builds three wheels. `jmfts` depends on `jmfts-client`, and `jmfts[web]` on
`jmfts-web`. (This heading said "two" until 2026-10-17, a release after `jmfts-web` joined.)

| distribution | holds | dependencies |
|---|---|---|
| `jmfts` | the appliance | the full stack |
| `jmfts-client` | `jmfts_client/contracts/` + a generated `RemoteJmftsClient` | httpx, pydantic |
| `jmfts-web` | the browser console's static files, served at `/app` | none |

The direction is server → client and it is deliberate. The contracts are the SINGLE
definition of every request and response shape, and both `LocalJmftsClient` (in-process)
and `RemoteJmftsClient` (HTTP) must validate against the same classes; a copy on each side
would be equal by value and unequal by `isinstance`, which bites exactly when one process
holds both — the `JMFTS_RUNNER_URL` case.

**Contracts may import neither `jmfts_core` nor a web framework.** A model that needs a
scheduler or prober type is not a contract; its adapter belongs server-side, next to
`jmfts_core/explain_wire.py`. Enforced by
`tests/test_client_codegen.py::test_client_package_does_not_import_the_server`.

**`jmfts-client/jmfts_client/_verbs.py` is generated. Never edit it.** It is rendered from
the route table FastAPI builds — not from the `@expose` declaration, which does not record
body-vs-query-vs-path binding. Regenerate after any change to the exposed surface:

```bash
python -m scripts.generate_client
```

`tests/test_client_codegen.py` fails if it is stale, and `tests/test_client_roundtrip.py`
drives the generated client against the real app to check the wire, not just the shape.

**`docs/reference/` is generated too, and it is the only GENERATED part of `docs/` that
ships.**
Three pages — what can be ingested, what gets indexed by which rung, what can be retrieved
and under which filters — rendered from `INGEST_USETYPES`, `TASK_ROWS`, `ATOMS`, `probe`'s
format tables, `SEARCH_METHODS` and `Settings`. Tables only; no history, no sprint numbers.

```bash
python -m scripts.generate_reference           # rewrite the pages
python -m scripts.generate_reference --check   # exit 1 if any is stale
```

`tests/test_reference_docs.py` runs the check and also refuses a hand-written page in that
directory, because the directory is published wholesale. These pages say WHAT the appliance
accepts; the specifications beside them say WHY, and since 2026-09-15 both ship. The
division still holds and is why both exist: a generated page cannot drift from the code and
carries no argument, and a specification carries nothing but argument and has to be reviewed
by hand before it can be published.

**`jmfts-web/jmfts_web/static/client/` is generated too, and it is the THIRD generated
artefact with the same seal.** Four files — the operation table, the verb class and their two
`.d.ts` declarations — rendered from `/openapi.json` rather than from the route table, because
the document carries the corrected `security` block and all mounted operations (121, read
2026-10-17; 115 when this was written), seven of which are hand-written routes with no `ExposeSpec` and therefore no `_verbs.py` method.

```bash
python -m scripts.generate_ts_client           # rewrite the bundle's client
python -m scripts.generate_ts_client --check   # exit 1 if any file is stale
```

`tests/test_ts_client_codegen.py` runs the check, refuses a file in that directory that is
neither generated nor the named hand-written runtime, and — where `node` is on PATH — executes
the generated module against a stubbed `fetch` to hold IC-6's nine-field call event. There is
no build step: they are ES modules the browser runs as they stand.

**The three release in lockstep: one number, three wheels, one tag.** `./bump-version.sh`
sets all three copies of the version. See `docs/RELEASING.md` step 1 for why — and question
4.10 of `docs/SPRINT_0_6_0.md` for why that runbook still says "two".

### One definition, many transports

`jmfts_core/registry.py` is the spine. A service method decorated with `@expose("POST", "/search/hybrid", ...)` becomes a first-class operation: in-process Python callers invoke the method directly, and `jmfts_core/rest/wiring.py` generates the REST route from the same metadata. There is no hand-written second definition of an endpoint to drift.

Consequences worth knowing before you "clean up" something:

- **`@expose`-decorated service methods are reachable even with no in-repo caller.** They are the REST API. Static "unused function" analysis will flag them; it is wrong.
- **`@register_task_handler`-decorated functions in `*_tasks.py` are likewise reachable** — they are dispatched by task-type string through `TASK_HANDLERS`.
- `registry.py` is deliberately FastAPI-free; contracts may not import `jmfts_core.rest` or `fastapi`. `tests/test_api_parity.py` enforces both, plus a bijection between `REGISTRY` and the mounted routes.
- `jmfts_core/rest/schemas.py` is a backward-compatibility shim that re-exports `jmfts_client.contracts`. New shapes go in the client's contracts package.
- The OpenAPI document's `security` block is corrected after generation, in `rest/main.py`. Generation declares the API token on every route; the correction branches on the same `PUBLIC_PATHS` and `RUNNER_PREFIX` constants the runtime gate branches on, so a path added to either carve-out moves the document with it. Per-route `openapi_extra` cannot express this — FastAPI merges a list by concatenating it, so an override can add an entry and never remove one.

### Data Flow

1. **Ingestion**: Document → `DocumentRepository.create()` → `EmbeddingService` generates embeddings → stored in PostgreSQL
2. **Embedding**: `nomic-ai/modernbert-embed-base` produces 768-dim document embeddings; token-level matryoshka stored at 256-dim halfvec (schema supports 384/512 but only 256 is active)
3. **Token Selection**: `TokenSelector` picks top 50% of tokens via MMR + attention variance + stopword penalties, stored as 256-dim embeddings with importance scores
4. **Search**: Query → embed → repository search (vector/BM25/hybrid/MaxSim) → scored results

### Queued ingest

Large ingests do not run inline. `jmfts_core/ingest_tasks.py` defines the task types and their handler registry; `ingest_worker.py` runs the worker thread; `repositories/task_queue.py` is the claim/retry/write-mode machinery. A document carries a `settled` column — retrieval indexes are partial on it, so in-flight nodes are invisible to search until `settling.py` walks them complete. Adding a rung means registering a handler, not editing a ladder of conditionals.

There is ONE ingest path and it is this one. The synchronous `execute_pipeline` and its
`PipelineDefinition` registry were deleted; the seven entry points `POST /ingest` accepts
survive as `INGEST_USETYPES` in `jmfts_core/ingest_options.py`, which is a table of
defaults rather than a table of stages.

A handler is not only a function. `jmfts_core/atoms.py` has each one declare what it reads
and what it writes, `jmfts_core/evidence.py` records what a run measured as rows in
`document_evidence`, and a rung's eligibility is a rule that names its scope
(`Scope` in `ingest_tasks.py`) with guards that compare a measurement against a constant
or against an option. `Document.produced_by` is what a scope matches on. The point of all
four is that the planner reads declarations instead of a ladder of conditionals, so adding
a rung means registering a handler.

`docs/INGEST_SPEC.md` is the normative specification for the pipeline and
`docs/SPRINT_JOBS.md` for the declaration machinery; between them they are cited by around
390 source comments (391 read 2026-10-17; around 370 on 2026-10-12), by part and section, and **both are in this repository** — so a
citation to 5.3 or to Phase 3 resolves. Read each one's dated review note first: they are the
working record, and what has moved under them is marked in place rather than edited out.

### Office formats, in three dependency tiers

`docs/OFFICE_SPEC.md` Part 1 is the design; `jmfts_core/office/__init__.py` is the seam.

| Tier | What | Installed cost | Where it lives |
|---|---|---|---|
| 1 | `zipfile`, `xml.etree`, `olefile` | in base (586 MB) | base install, in `jmfts_core/probe.py` |
| 2 | `python-docx`, `python-pptx`, `openpyxl` | +44 MB | the `office` extra, behind `jmfts_core/office/` |
| 3 | LibreOffice driven by `unoserver` | ~500 MB system package | a badged worker image; the `convert` extra is the client only |

Tier 2 is **not** pure Python, whatever the three names suggest: `python-docx` and `python-pptx` both pull `lxml`, and `python-pptx` also pulls `Pillow`. Both are C extensions, and together they are 19 of those 44 MB. Sizes measured 2026-08-22 in empty venvs against the 0.2.0 wheels.

**Tier 1 must stay in the base install.** Probe depends on nothing, calls no model, and always runs. If probing a `.docx` needed the `office` extra, a base install would accept the upload, probe it, and report an empty pattern set — which is indistinguishable from a `.docx` that genuinely declares no structure.

**Tier 2 is reached only through `require_docx()` / `require_pptx()` / `require_openpyxl()`,** at the point of use, never at module scope. `OfficeStackNotInstalled` subclasses `ImportError` and therefore classifies PERMANENT. `tests/test_office_packaging.py` fails if a tier-2 import reaches the application's import path, and `scripts/check_base_install.sh` asserts the same against a real base venv.

`jmfts_core/office/extract.py` converts `docx` and `pptx` to markdown, because markdown is the intermediate format every text-bearing input converges on. Office formats are entry points to it, not peers of it.

### Two credentials, kept disjoint

`jmfts_core/rest/auth.py` holds both. `require_token` authenticates the API bearer and binds a principal into a contextvar, which repositories read to enforce subtree access. `require_runner` gates `/runner` with a different key and binds NO principal, because a runner has no subtree and binding one would be a lie about who is asking. A blank `JMFTS_RUNNER_KEY` answers 503 (surface off), never 401 and never allow-all. `tests/test_runner_auth.py` asserts the correspondence in both directions.

### Embedding as a service

`jmfts_core/embedder.py` decides where a vector comes from: this process's model, or another JMFTS's `/runner` surface when `JMFTS_RUNNER_URL` is set. That is what lets a storage-side worker run without torch installed at all. It covers the ingest write path only — `/search` still embeds queries locally, because putting a network hop inside every search request is not a trade worth making. With neither a model nor a runner, asking for a vector raises `ModelStackNotInstalled` naming both ways out; it never degrades quietly.

### Key Abstractions

- **Document model**: Tree-structured with parent-child relationships, JSONB path for hierarchy navigation, and graph edges via `DocumentLink`
- **TokenEmbedding**: Per-token 256-dim embeddings with tier-based storage (5%, 10%, 15%... of tokens) for benchmarking different retention levels
- **BM25 infrastructure**: `SearchIndex` → `SearchTermPosting` (inverted index) + `SearchTermStats` (IDF)
- **Singletons with lazy init**: `get_embedding_service()`, `get_session()` — global instances created on first use

## Environment Configuration

All settings use `JMFTS_` prefix. See `.env.example` for the full list. Key groups:
- `JMFTS_DB_*` — PostgreSQL connection
- `JMFTS_EMBEDDING_*` — model name and device (cuda/cpu)
- `JMFTS_TOKEN_*` — token selection parameters (top_percent, embed_dims)
- `JMFTS_BM25_*` — BM25 tuning (k1, b)
- `JMFTS_API_TOKEN` / `JMFTS_RUNNER_KEY` — the two credentials; see *Two credentials* above
- `JMFTS_RUNNER_URL` — where this process asks for vectors, if not itself
- `JMFTS_LLM_*` — OpenAI-compatible endpoint for synthesis, RAPTOR and fact extraction. Blank by default and blank is supported: everything except summarization, RAPTOR, fact extraction and synthesis works without an LLM.
- `JMFTS_SUMMARIZATION_*` — the shape of the summarization request (context, temperature), not which endpoint serves it

## Search Repository (jmfts_core/repositories/search.py)

This is the most complex file (2075 lines, read 2026-10-17; 1958 on 2026-09-10, ~1666 on
2026-09-06, 1906 before that session's merges — it grows every sprint and the number is a reading, not
a budget). It implements:
- **Vector search**: HNSW-indexed cosine similarity on document embeddings
- **MaxSim**: Late interaction scoring — sum of per-query-token max similarities against document tokens
- **BM25**: Custom inverted index with configurable k1/b parameters
- **Hybrid search**: Weighted combination of vector + BM25 + optional MaxSim reranking
- **Full-text search**: PostgreSQL `ts_vector` with trigram fallback

Every retrieval method filters `settled = 'settled'`, and the gate goes INSIDE the scored
statement rather than after it — a post-filter trims the page after `LIMIT` and returns a
short page instead of a wrong one. BM25's leaf scan was the last path without that gate
(fixed 2026-09-10): its postings CTE never reached `documents`, so the join is now
unconditional rather than tagging along with subtree/`as_of`/access filtering.

## Code Style

- **Line length**: 100 (Black + Ruff)
- **Python**: 3.11+
- **Patterns**: Repository pattern for data access, dependency injection via FastAPI `Depends()`, context managers for DB sessions
- **Naming**: Pydantic schemas use `*Create`/`*Update`/`*Response` suffixes; private globals prefixed with `_`

## Test Corpus

`tests/corpus/` is the fidelity corpus for office formats — one vocabulary of pattern
names interrogated out of `probe`, a manifest with one record per file, and 23
generated fixtures. Nothing binary is committed; `fixtures.py` builds them
deterministically. `docs/CORPUS.md` is the runbook and it is published.

```bash
python -m scripts.corpus report    # coverage, and what shipped code can measure
python -m scripts.corpus check     # manifest against the bytes; non-zero on drift
pytest tests/corpus -q             # no database, no optional dependency
```

## Benchmark Datasets

- MultiHop-RAG dataset, symlinked into `datasets/`. The symlink target is a local
  choice; `datasets/` is gitignored so every machine points it at its own copy.
- ~~Local test documents in `test_docs/` for development~~ — no such directory exists in
  this tree (checked 2026-10-17); a few scripts still name it. Use `tests/corpus/` fixtures.

## Documentation

`docs/` is the working record, and **most of the load-bearing half of it is now published**.
What this repository carries is `README.md`, `CLAUDE.md`, `CHANGELOG.md`, `ROADMAP.md`, the
0.6.0 sprint plan (`docs/SPRINT_0_6_0.md`, shipped; 0.7.0 has a tracker milestone instead), the generated `docs/reference/` pages, and —
since 2026-09-15, `SPRINT_0_6_0.md` Block E — the three specifications and the seven
measurement records:

| | |
|---|---|
| Specifications | `docs/INGEST_SPEC.md`, `docs/SPRINT_JOBS.md`, `docs/OFFICE_SPEC.md` |
| Measurement records | `docs/ANN_INDEX_HEALTH.md`, `docs/STRESS_CORPUS.md`, `docs/MEASURE_BM25_BOUNDARY.md`, `docs/MEASURE_SHACL_SCOPE.md`, `docs/MEASURE_TYPED_WALK.md`, `docs/MEASURE_MAXSIM_MARGIN.md`, `docs/CORPUS.md` |

The subset is `tests/test_readme_links.py::PUBLISHED`, and `docs/RELEASING.md` cites that
constant rather than repeating it.

**Each published document carries a dated review note at its head**, saying what in it had
gone false against the code and was corrected. **Corrections are marked in place, never
edited in.** That is the convention `STRESS_CORPUS.md` §4 states and the others follow: *"a
defect report that is edited into agreement with the patch stops being evidence that the
patch was needed."* A document that says something the tree no longer does, with a dated
note under it saying so, is worth more than one silently brought up to date — and a
citation to a section has to still resolve to the section that was cited.

**The sprint plans and the archive stay internal, permanently, and that is decided rather
than pending.** `docs/SPRINT_0_6_0.md` question 4.5, 2026-09-13: 0.6.0 is already past
`docs/SPRINT_0_3_0.md`, `docs/SPRINT_0_4_0.md` and `docs/SPRINT_0_5_0.md`, so everything
open in them is a numbered step in the current plan, and publishing them would hand a
reader an earlier, worse copy of a plan they can already read. **Around two hundred
citations to those three names therefore stay dangling permanently.** Do not treat them as
broken links and do not delete them; the reason changed from "not published yet" to "not
published", and the rule did not.

**`PUBLISHED` describes rather than drives, from 0.5.1 on.** It was the copy list for a
release step that built a public tree out of an internal one, and that step no longer runs —
development happens here. It is still the definition the tests in
`tests/test_readme_links.py` are written against, so adding a path is still a decision
about what the world sees; what changed is that the decision takes effect by committing the
file.

Around seven hundred source comments cite a `docs/` file by part and section. Count them
rather than trusting the number below, which is a reading and not a rule:

```bash
git grep -hoE "(research/|archive/)?[A-Z][A-Z0-9_-]*\.md" -- \
    jmfts_core jmfts-client jmfts_batch jmfts-web plugin scripts tests .github .githooks |
  sort | uniq -c | sort -rn
```

Read 2026-10-12, on the Block E branch rebased onto 0.5.2. **Resolving now:**
`SPRINT_JOBS.md` 185, `INGEST_SPEC.md` 184, `OFFICE_SPEC.md` 161, `STRESS_CORPUS.md` 37,
`ANN_INDEX_HEALTH.md` 30, `CORPUS.md` 13, `MEASURE_BM25_BOUNDARY.md` 9,
`MEASURE_SHACL_SCOPE.md` 8, `MEASURE_TYPED_WALK.md` 6, `MEASURE_MAXSIM_MARGIN.md` 2 — 635
citations, of which the first nine's 633 pointed at nothing until Block E (600 when first read
on 2026-09-15). Plus `ROADMAP.md` 14 and `SPRINT_0_6_0.md` 100, published 2026-09-13.

**Still dangling, and now permanently:** `SPRINT_0_5_0.md` 91, `SPRINT_0_3_0.md` 82,
`SPRINT_0_4_0.md` 51 — 224 by decision 4.5, not by backlog (221 on 2026-09-15). Then
`RELEASING.md` 11, `KNOWN-DEFECTS.md` 7 (D1–D4 anchors, all resolved; the file moved to
`docs/archive/` on 2026-09-04 and the six citations that carried a path were updated with
it — the bare `KNOWN-DEFECTS D1` form names an anchor, not a path, and was left alone),
`AGENTIC_KNOWLEDGEBASE.md` 4, `archive/ROADMAP_HISTORY.md` 2, `RERANKER_CRITIQUE.md` 1,
`research/INTERMEDIATE_FORMATS.md` 1, `archive/SPRINT_0_4_0_DRAFT.md` 1. Of those seven,
`RELEASING.md`, `AGENTIC_KNOWLEDGEBASE.md`, `RERANKER_CRITIQUE.md` and
`research/INTERMEDIATE_FORMATS.md` have not had a review pass; `KNOWN-DEFECTS.md`,
`ROADMAP_HISTORY.md` and `SPRINT_0_4_0_DRAFT.md` are the archive, which 4.5 holds with the
three sprint plans — the split the README's note makes.

**Read again 2026-10-17**, on `claude/awesome-planck-xkqumb`, with `jmfts-web` added to
the command (it ships and its comments cite; until this reading the grep left it out) and
`archive/` recognised as a prefix. Resolving: `INGEST_SPEC.md` 196, `SPRINT_JOBS.md` 195,
`OFFICE_SPEC.md` 174, `SPRINT_0_6_0.md` 141, `STRESS_CORPUS.md` 39, `ANN_INDEX_HEALTH.md` 33,
`ROADMAP.md` 18, `CORPUS.md` 13, `MEASURE_BM25_BOUNDARY.md` 9, `MEASURE_SHACL_SCOPE.md` 8,
`MEASURE_TYPED_WALK.md` 6, `MEASURE_MAXSIM_MARGIN.md` 2. Held by 4.5: `SPRINT_0_5_0.md` 93,
`SPRINT_0_3_0.md` 85, `SPRINT_0_4_0.md` 52 — 230. Held, not yet reviewed: `RELEASING.md` 12,
`AGENTIC_KNOWLEDGEBASE.md` 4, `RERANKER_CRITIQUE.md` 1, `INTERMEDIATE_FORMATS.md` 2 (one
with the `research/` prefix, one without). The archive: `KNOWN-DEFECTS.md` 7 (six with the
`archive/` prefix), `archive/ROADMAP_HISTORY.md` 2, `archive/SPRINT_0_4_0_DRAFT.md` 1. The
2026-10-12 reading above is kept as it was read.

The command also reports
`README.md`, `CLAUDE.md`, `CHANGELOG.md` and `INVENTORY-2026-04-05.md`; the first three
ship, and the fourth is a corpus filename in `scripts/ingest_missing_steelman.py` rather
than a `docs/` citation. It also reports `INDEXING.md`, `INGEST.md` and `RETRIEVAL.md`,
which are the generated `docs/reference/` pages and ship. A held-back document is absent
from this tree by design; there is no second tree it is present in any more, only a
directory on the author's machine. **Do not treat
the citations as broken links to fix, and do not delete them** — for the seven still
pending they are the anchors the documents will be republished against, and for the three
plans they are anchors into a record that stays internal.

The numbers move fast and unevenly — `SPRINT_0_5_0.md` went 41 → 80 in four days — which is
why the command is here and the reading is dated. `docs/PROPOSE_TASK_RESOURCES.md` has
**zero** citations from any shipped file, checked again 2026-10-12; it is the one recent
document the redaction rule does not reach, and that is the rule working rather than an
omission. It therefore does not join the README's list and is not published.

The public README's "A note on documentation" names the held-back list in prose, so a
reader who follows a citation finds out why it goes nowhere. A release that adds a new
`docs/` file which source comments cite adds its name there too; that is the redaction
step, and it is the only one — no shipped file is rewritten at release time. **`scripts/`
is in `PUBLISHED`**, which is the step that was missed twice, and it is why the rule says
"shipped file" rather than "source comment": a document cited only from `scripts/` is cited
from a shipped file. The count never mattered — `MEASURE_SHACL_SCOPE.md` joined the prose
list on ONE citation.

**Publishing works the other way round and it is one-way.** Ten names left that list on
2026-09-15 rather than joining it — nine through Block E's pass, and
`MEASURE_MAXSIM_MARGIN.md` by being committed to a public branch the same day, which is the
one-way door taken without the pass; its review note came after, on 2026-10-12. 4.5 says
explicitly that the decision cannot be retracted: publishing later is always available,
unpublishing is not. **The entry condition
is a review pass, not a citation count** — read for exactly three things, which is
`SPRINT_0_6_0.md` Block E's definition: a private host name or path, a named third party
who has not agreed to be named, and a claim about the tree that has gone false. It is not a
rewrite and not a tidy-up. The documents are the working record and are allowed to read
like one.

### Where the plans are, and where defects are

| Question | File |
|---|---|
| What is 0.6.0 | `docs/SPRINT_0_6_0.md`, **published** |
| What is open, deferred, or gated — and the Experiment Log | `ROADMAP.md`, **published** |
| How the ingest pipeline is specified | `docs/INGEST_SPEC.md`, **published 2026-09-15** |
| How a task declares what it reads, writes and is scoped to | `docs/SPRINT_JOBS.md`, **published 2026-09-15** |
| Office formats, the anchor, renditions, and the unbuilt write half | `docs/OFFICE_SPEC.md`, **published 2026-09-15** |
| What the measurements behind a decision actually read | the four `docs/MEASURE_*`, `ANN_INDEX_HEALTH.md`, `STRESS_CORPUS.md`, `CORPUS.md` — **all published 2026-09-15** |
| What is 0.4.0 | `docs/SPRINT_0_4_0.md` — **held, permanently** (4.5) |
| What is 0.5.0 | `docs/SPRINT_0_5_0.md` — **held, permanently** (4.5) |
| What was cut, and what verified a scope decision | `docs/ROADMAP_PLANS_AFTER_0_3_0.md` — **held back** |
| What is beyond 0.5.0 | the same file, tiers 3 through 6 — **held back** |
| What shipped | `CHANGELOG.md`, and `docs/archive/ROADMAP_HISTORY.md` behind it — the archive is **held** (4.5) |
| What is 0.7.0 | the 0.7.0 milestone on the issue tracker; there is no sprint document |
| What the appliance accepts, indexes and retrieves | `docs/reference/`, generated and published |

**An open defect is a numbered step in the current sprint plan, and the entry condition
is a failing test.** Not an argument that something could go wrong, and not a measurement
of how often it does. There is no standing defect file: `docs/archive/KNOWN-DEFECTS.md`
(held, with the archive) holds D1–D7, all resolved, and is history. `docs/SPRINT_0_6_0.md` Part 0 states the rule and
works three live cases through it, and it is published — so the rule is now readable from
outside the project rather than only cited.

`docs/archive/SPRINT_0_4_0_DRAFT.md` and `docs/archive/SPRINT_0_5_0_DRAFT.md` (both held,
with the archive) are 2026-08-24 drafts of those release numbers that plan different sprints. Their step
numbers are not the shipped ones, and neither shipped plan uses them.
