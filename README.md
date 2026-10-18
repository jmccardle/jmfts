# JMFTS

JMFTS (John McCardle's Fusion Tree Search) is a retrieval appliance: a
research-focused PostgreSQL + pgvector service combining matryoshka
embeddings, ColBERT-style late interaction (MaxSim), and BM25 into hybrid
search over a tree-structured document store.

Documents form a tree (`parent_id` + a materialized `path`), carry typed
cross-references to each other, and can hold temporal subject-predicate-object
triples with supersession instead of deletion. Five retrieval methods sit on
top: `vector`, `bm25`, `fulltext`, `maxsim`, and `hybrid` (a weighted/RRF
combination), plus an `auto` router. Ingestion has seven entry points — the
`usetype` a request names: `markdown`, `conversation`, `raw`, `transcript`,
`wiki:url`, `wiki:arxiv`, `wiki:pdf`. Sending bytes that are already stored, and
that you can read, stores nothing: you get the existing node back, attached to the
`parent_id` you named by a `contains` link rather than copied. The same bytes with
different ingest options are a 400, not a silent hit. An entry point selects
defaults, not a sequence of stages: which tasks run is decided from what probing
the bytes measured.

Uploaded files are identified by their bytes, not their extension, and each
format is read into markdown before anything indexes it. Today that is `.pdf`,
plain text and markdown, HTML, and — with the `office` extra — `.docx` and
`.pptx`. An `.xlsx` (also `office`, through `openpyxl`) is the exception: it is
read into a table node per sheet and a record node per row, each anchored to its
cells, and is not turned into markdown. What a given
file would do is a question you can ask before uploading it: `POST /ingest/explain`
returns the plan, task by task, with the reason for every row that will not run.

## Status

Alpha. The retrieval and ingestion paths are exercised by a large test suite and
by benchmark runs; the interfaces are still moving.

**Measured, nDCG@10 on four standard BEIR datasets:**

| dataset | vector | bm25 | hybrid |
|---|---|---|---|
| SciFact | 0.6769 | 0.6595 | **0.7090** |
| NFCorpus | 0.3077 | 0.3043 | **0.3255** |
| FiQA | **0.3908** | 0.2363 | 0.3992 |
| TREC-COVID | **0.841** | 0.580 | 0.837 |
| *average* | 0.554 | 0.445 | **0.568** |

Hybrid wins three of the four; vector alone wins TREC-COVID, where our BM25
(0.580) sits below the canonical Anserini BEIR baseline of roughly 0.656.
Per-dataset weight tuning moves SciFact and NFCorpus by under a point. The full
breakdown, the caveats, and the comparisons against published ColBERTv2 and
SPLADE++ numbers are recorded in the development repository, not in this tree.

**One thing worth knowing before you rely on it.** `?rerank=true` is unmeasured.
The cross-encoder second stage loads a standard `CrossEncoder`
(`JMFTS_RERANKER_MODEL`, default `cross-encoder/ms-marco-MiniLM-L-6-v2`), and it
surfaces load and scoring errors rather than silently falling back to the
first-stage ranking. But that default was chosen for size and CPU viability, not
for measured retrieval quality — `vector→crossenc@100` has never been swept
against `vector→maxsim@200`. Treat reranked ordering as unvalidated. The
`maxsim` rerank method reads token embeddings you already stored and loads no
model at all.

## Install and run

You need PostgreSQL 14 or newer with the `pgvector` extension available. JMFTS
does not install or manage it.

```bash
pip install jmfts                # base: no torch, cannot embed by itself
pip install 'jmfts[embed]'       # + the model stack, for a single appliance
pip install 'jmfts[office]'      # + .docx/.pptx/.xlsx readers (python-docx, python-pptx, openpyxl)
pip install 'jmfts[rdf]'         # + Turtle in and out, and SHACL shapes
pip install 'jmfts[sketch]'      # + MinHash per column (datasketch), for propose:links
pip install 'jmfts[convert]'     # + the unoserver CLIENT; LibreOffice itself is a worker image
pip install 'jmfts[web]'         # + the browser console, served at /app
jmfts-init-db                    # create the database and load the schema
jmfts-server                     # serve on 0.0.0.0:8100
jmfts-server --port 9000         # ...or override one setting for this run
jmfts-server --help              # what the JMFTS_* defaults currently resolve to
jmfts-worker                     # drain the ingest queue in its own process
jmfts-batch-worker --help        # summarize:llm through a provider's batch API
```

With `[web]` installed, `http://localhost:8100/app/` is a console over the same REST API any
client calls: search, upload and watch an ingest, read a document with its source anchors
boxed on the page, browse the tree, edit search presets, read what the appliance can do, and
a log of every call the page made. It has no Python dependencies and computes nothing the
call log does not show; `jmfts-web/README.md` says how that is held.

### Upgrading a database that already holds data

`jmfts-init-db` loads `jmfts_core/sql/schema.sql` and never a migration. That file is the
complete current DDL, so a database it builds is already current and has nothing
outstanding. A database from an earlier version is the other case, and the upgrade deltas
ship inside the package as `jmfts_core/sql/migrations/`.

```bash
jmfts-init-db --pending           # which shipped deltas has THIS database not applied?
jmfts-init-db --list-migrations   # which deltas does this package ship at all?

jmfts-init-db --pending 2>/dev/null   # just the names, for a script
```

`--pending` applies nothing; choosing to apply a delta is yours. It prints the names to
stdout and everything a human reads to stderr, so discarding stderr leaves a plain list,
and it exits the way `diff` does — 0 nothing outstanding, 1 some, 2 the question could not
be answered. Apply what it names, in the order it names them, with
`psql -v ON_ERROR_STOP=1 -f <file>`; each delta is one transaction and records itself, so
re-running `--pending` is the check that it took.

**Run it after every upgrade, not only when something looks wrong.** Most deltas add a
column, and a database missing one of those raises at the first query that needs it. At
least one changes an *index* — a database missing that delta answers, and answers worse,
with nothing in a log to say so.

For a working PostgreSQL and API in one command, the compose file brings up
`pgvector/pgvector:pg16` alongside the API:

```bash
docker compose up
```

From a checkout, for development:

```bash
pip install -e ./jmfts-client    # the client distribution; jmfts depends on it
pip install -e ./jmfts-web       # the console distribution, for /app; [dev] does not imply it
pip install -e ".[dev]"          # editable, with pytest/black/ruff; implies [embed],
                                 # [office], [rdf] and [sketch] — the suite exercises all four
uvicorn jmfts_core.rest.main:app --host 0.0.0.0 --port 8100 --reload
```

This tree builds three distributions, and the first line is not optional. `jmfts` declares
`jmfts-client==0.7.0` with `==`, so the third line alone resolves that exact version
from PyPI and shadows the checkout you meant to work in. The second line is optional for
the same reason in reverse: without it there is no `/app`, and nothing else is missing.

### Reading and driving the API

`/docs` is Swagger UI over the live route table — 121 operations, grouped by tag, with the
request and response schemas. `/redoc` is the same document laid out for reading, and
`/openapi.json` is the document itself.

**Start at `GET /capabilities`.** It answers, without your sending anything: which optional
extras are installed, whether this process can produce a vector at all (its own model, or
another JMFTS via `JMFTS_RUNNER_URL`), which formats it identifies from the bytes, which
ingest entry points it accepts, which retrieval methods it fuses and at what weights, and
which usetypes it holds out of every result set. Add `?corpus=true` for the counts that say
whether a method will return anything here — MaxSim ranks only documents that carry token
vectors, and on a corpus with none it returns nothing whatever is installed.

**A generated client already exists — do not write your own against `/openapi.json`.**
`pip install jmfts-client` gives you every one of those operations as a Python method, with
the request and response models the appliance itself validates against:

```python
from jmfts_client import RemoteJmftsClient
from jmfts_client.contracts import DocumentCreate, HybridSearchRequest

with RemoteJmftsClient("http://localhost:8100", token="...") as jmfts:
    jmfts.create_document(DocumentCreate(title="Ada", content="Ada Lovelace"))
    hits = jmfts.hybrid_search(HybridSearchRequest(query="Ada", limit=10))
```

It carries `httpx` and `pydantic` and nothing else, so calling an appliance does not mean
installing one. Nobody writes those methods: a service method marked `@expose` becomes a
REST route, an entry in this OpenAPI document, a method on the in-process
`LocalJmftsClient`, and a method there — four views of one definition, with a test holding
each of them to it. See `jmfts-client/README.md`.

**Every integrated system is a consumer of this API, including one that writes into it.**
JMFTS is the foundation and does not test its callers: no caller's contract tests run in
this repository's CI, and a change to the API is accepted here together with the cost of
updating whatever calls it. A caller that hand-writes its own client takes on keeping that
client current; one that uses `jmfts-client` at the matching version gets the change as a
typed error at import or validation time instead. Breaking changes are listed in
`CHANGELOG.md`, and a request field the server does not read is reported back in the
`X-JMFTS-Ignored-Fields` response header.

Press **Authorize** and paste `JMFTS_API_TOKEN` before trying an operation; the page names
which of the two credentials each one takes, since `/runner/*` uses `JMFTS_RUNNER_KEY`
instead and `GET /health` needs neither. All three pages answer without a token — a browser
navigating to a page cannot send an `Authorization` header, so gating them would close the
page rather than protect it. They expose the interface; reaching anything they describe
still costs a token.

Both pages load Swagger UI and ReDoc from `cdn.jsdelivr.net`, which is FastAPI's default. A
host with no route to the internet renders a blank page and must read `/openapi.json`
directly, or be given locally served copies of those assets.

### The office readers are an extra

**`pip install jmfts` reads PDF, text, markdown and HTML. `.docx` and `.pptx` need
`[office]`:**

```bash
pip install 'jmfts[office]'      # python-docx, python-pptx, openpyxl
```

The split is the same one the model stack draws, for the same reason. A base install
already *identifies* an office file and reports what it declares — how many slides, which
sheets, whether it carries macros or an unplaced member — because that runs on `zipfile`
alone and `probe` must always run. Opening one to get the text out is what needs the
readers. So a storage-side worker that never ingests office files is correctly installed
and correctly has no `python-docx`, and asking one to read a `.docx` raises
`OfficeStackNotInstalled`, which names the extra rather than reading as a broken
environment.

### The model is an extra

**`pip install jmfts` does not install torch.** Base JMFTS is storage, retrieval, the
tree, BM25, the queue and the whole ingest pipeline; the only step that needs an
accelerator is producing vectors, and that step can be somebody else's. Measured against
the 0.2.1 wheels: 586 MB of site-packages, against 5.2 GB with the model stack.

Add `[embed]` when *this* process should run the model — because it serves `/search`,
because it serves `/runner` for others, or because it is a single appliance doing both:

```bash
pip install 'jmfts[embed]'                                    # CUDA build of torch
# or, for CPU — the wheel index is an install-time choice, so it is two steps:
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install 'jmfts[embed]'
```

Without it, an install can still *measure* text — `check_fit`, the chunker, matryoshka
truncation are all tokenizer and numpy — and asks another JMFTS for the vectors:

```bash
JMFTS_RUNNER_URL=http://the-gpu-box:8100 JMFTS_RUNNER_KEY=<shared secret> jmfts-worker
```

That is the intended shape for an ingest worker, and the reason the split exists: a fleet
is mostly storage-side workers, and none of them need several GB of CUDA to split text and
write rows. Asking for vectors with no model and no runner raises `ModelStackNotInstalled`,
which names both ways out — it never silently degrades. See `deploy/README.md`.

`./scripts/check_base_install.sh` proves the claim rather than asserting it: it builds an
empty virtualenv, installs base JMFTS into it, and checks there that the API and the
worker import, the tokenizer half works, asking for a vector raises, and no office reader
is reachable.

### Configuration

Configuration is environment-variable driven (`JMFTS_` prefix) — see
`.env.example` for the full list, grouped by concern (DB, embedding model,
token selection, BM25 tuning, auth/CORS, the worker and the runner).

Run the test suite against an isolated, throwaway database (never the real
appliance DB):

```bash
./scripts/run_tests_docker.sh    # easiest: throwaway pgvector + CPU embed
pytest                           # native: needs `ALTER ROLE jmfts CREATEDB`
```

## Layout

```
jmfts_core/        the library: services, repositories, ORM models, office readers
jmfts_core/rest/   FastAPI surface — routers generated from the @expose registry
jmfts_core/sql/    schema.sql and the incremental migrations, shipped in the package
jmfts-client/      the second distribution: wire contracts + the generated HTTP client
jmfts-web/         the third distribution: the browser console mounted at /app
jmfts_batch/       batch summarization over an OpenAI- or Anthropic-style batch API
scripts/           CLI clients over the REST API
deploy/            Kubernetes manifests for a worker fleet, and KEDA scaling on queue depth
plugin/            Claude Code plugin exposing JMFTS as an agent's durable memory
tests/             pytest suite against an ephemeral database, plus the fidelity corpus
```

## Where to go next

- **What this appliance accepts**: three generated reference pages, tables only.
  `docs/reference/INGEST.md` (entry points, formats, optional dependencies),
  `docs/reference/INDEXING.md` (every ingest rung, its conditions, what it reads
  and writes), `docs/reference/RETRIEVAL.md` (every retrieval method, what a
  document must carry to be reachable by it, and every filter — including the
  usetype exclusions applied when a request names none). They are rendered from
  the same registries the appliance reads at runtime, by
  `python -m scripts.generate_reference`, and a test refuses a stale one.
- **What changed between releases**: `CHANGELOG.md`.
- **Running a worker fleet**: `deploy/README.md` — what routes where, badges, the
  thin worker that does not hold the model, and the one way a badged fleet can
  stall.
- **Contributing / architecture**: `CLAUDE.md` — the architecture diagram,
  data flow, code style, and the search repository's internals (vector, BM25,
  MaxSim, hybrid).
- **Using JMFTS as an agent's memory**: `plugin/jmfts/` — a Claude Code
  plugin exposing JMFTS as durable cross-session memory via search/ingest/
  read/explore/analyze skills. `plugin/jmfts/skills/jmfts/SKILL.md` is the
  concept overview.
- **The batch worker**: `jmfts_batch/README.md` — consuming `summarize:llm`
  through an external batch API.
- **The browser console**: `jmfts-web/README.md` — install `[web]` and open `/app/`;
  why it is a separate distribution, and the one generated client it talks through.
- **Calling JMFTS from Python**: `jmfts-client/README.md` — the second
  distribution, and what a generated verb is generated from.

## A note on documentation

**Ten of the documents this code was written against are now in this
repository, under docs/ — including the ingest specification that nearly two
hundred source comments cite by section number.** Nine landed on 2026-09-15, each after the
review pass described below. The tenth, `docs/MEASURE_MAXSIM_MARGIN.md`, was
committed the same day as a new measurement record, outside that pass, and has
been public since; its review note is dated 2026-10-12. What is here:

- `docs/INGEST_SPEC.md` — the normative specification for the ingest pipeline.
- `docs/SPRINT_JOBS.md` — the declaration machinery behind it: atoms, evidence,
  rules, scopes and the settling walk.
- `docs/OFFICE_SPEC.md` — office formats in three dependency tiers, the anchor,
  renditions, and the write half that is not built.
- `docs/ANN_INDEX_HEALTH.md`, `docs/STRESS_CORPUS.md`,
  `docs/MEASURE_BM25_BOUNDARY.md`, `docs/MEASURE_SHACL_SCOPE.md`,
  `docs/MEASURE_TYPED_WALK.md`, `docs/MEASURE_MAXSIM_MARGIN.md`, `docs/CORPUS.md` —
  the measurement records, each one the evidence behind a decision the code carries.

**They are the working record and they read like one.** Each carries a dated
review note at the top saying what in it had gone false against the code and was
corrected, and corrections are marked in place rather than edited in — a defect
report rewritten into agreement with its patch stops being evidence the patch
was needed. They cite each other, and they cite documents that are still held
back; those citations are anchors, not broken links.

**What is still held back.** A comment naming SPRINT_0_3_0.md, SPRINT_0_4_0.md,
SPRINT_0_5_0.md, SPRINT_0_4_0_DRAFT.md, ROADMAP_HISTORY.md, KNOWN-DEFECTS.md,
AGENTIC_KNOWLEDGEBASE.md, RERANKER_CRITIQUE.md
or research/INTERMEDIATE_FORMATS.md points at
a document that is not here. Three of them — AGENTIC_KNOWLEDGEBASE.md,
RERANKER_CRITIQUE.md and research/INTERMEDIATE_FORMATS.md — have not had a review pass
yet. (RELEASING.md was in this list until 2026-10-17, when it was reviewed and published.) The other six
are the three sprint plans and the archive behind them, and they are a different
case:

**The sprint plans stay internal, and that is a decision rather than a backlog.**
It was taken on 2026-09-13 and the reasoning is in `docs/SPRINT_0_6_0.md`
question 4.5: 0.6.0 is already past the plans for 0.3.0, 0.4.0 and 0.5.0, so
everything still open in them is a numbered step in the current plan, and
publishing them would hand a reader an earlier, worse copy of a plan they can
already read. Around two hundred citations to those three names will therefore
stay dangling permanently. That is a real cost and it is a decided one. The same
decision holds the archive behind them, which is history that `CHANGELOG.md`
already summarises.

**`ROADMAP.md` left the held-back list on 2026-09-13 and was the first name to
do so.** It is here, at the top level: what is open, what is deferred and what
gates each, and the Experiment Log — including the negative results, which are
the reason several things are *not* in the tree. `docs/SPRINT_0_6_0.md` is the
plan 0.6.0 shipped against and is here for the same reason; 0.7.0's plan is its milestone
on the issue tracker, not a document.

Held-back names are deliberately not written as links. There is nothing in this
tree for them to point at, and marking them up as paths would promise
otherwise.

**The remaining list is meant to shrink, less the three sprint plans.** Up to
0.5.0 it could not: the public tree was a per-release squash of a private one
and holding a document back was the same act as publishing the code. From 0.5.1
development happens here, so a document joins this repository when it has had a
review pass rather than when a release runs. A name still held back in a later
version is one that has not had that pass — or, for the three plans, one that is
not getting it.

**`docs/reference/` answers a different question and is generated.** The three
pages under it say what the appliance accepts, indexes and retrieves, in tables,
with no history and no argument. Nothing in them is written by hand — they are
rendered from the registries the appliance itself reads, so they cost no
editorial pass and cannot drift from the code. They were the first part of the
working record to ship, back when almost seven hundred source comments cited a
document an outside reader could not open. The ten documents listed above are
the *why* behind those same registries; these three are the *what*.

## Licence

MIT. Copyright (c) 2026 Fight Fire with Fire Robotics, LLC. See `LICENSE`.
