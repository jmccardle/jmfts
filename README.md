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
`wiki:url`, `wiki:arxiv`, `wiki:pdf` — and is idempotent on
`(content_hash, parent_id)`. An entry point selects defaults, not a sequence of
stages: which tasks run is decided from what probing the bytes measured.

Uploaded files are identified by their bytes, not their extension, and each
format is read into markdown before anything indexes it. Today that is `.pdf`,
plain text and markdown, HTML, and — with the `office` extra — `.docx` and
`.pptx`. An `.xlsx` is identified and described but not yet read. What a given
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
pip install 'jmfts[rdf]'         # + Turtle in and out, and SHACL shapes
jmfts-init-db                    # create the database and load the schema
jmfts-server                     # serve on 0.0.0.0:8100
jmfts-server --port 9000         # ...or override one setting for this run
jmfts-server --help              # what the JMFTS_* defaults currently resolve to
```

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
pip install -e ".[dev]"          # editable, with pytest/black/ruff; implies [embed],
                                 # [office], [rdf] and [sketch] — the suite exercises all four
uvicorn jmfts_core.rest.main:app --host 0.0.0.0 --port 8100 --reload
```

This tree builds two distributions, and the first line is not optional. `jmfts` declares
`jmfts-client==0.5.0` with `==`, so the second line alone resolves that exact version
from PyPI and shadows the checkout you meant to work in.

### Reading and driving the API

`/docs` is Swagger UI over the live route table — 115 operations, grouped by tag, with the
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
- **Calling JMFTS from Python**: `jmfts-client/README.md` — the second
  distribution, and what a generated verb is generated from.

## A note on documentation

The design documents this code was written against — most importantly the
ingest specification that a hundred source comments cite by section number —
are not in this release. They are being refined for publication separately.
The whole working record is held back, not a chosen few files, so a comment
naming any of INGEST_SPEC.md, SPRINT_JOBS.md, OFFICE_SPEC.md, SPRINT_0_3_0.md,
SPRINT_0_4_0.md, SPRINT_0_5_0.md, SPRINT_0_4_0_DRAFT.md, CORPUS.md, RELEASING.md,
KNOWN-DEFECTS.md, MEASURE_SHACL_SCOPE.md, MEASURE_TYPED_WALK.md,
MEASURE_BM25_BOUNDARY.md, ANN_INDEX_HEALTH.md, STRESS_CORPUS.md,
ROADMAP.md, ROADMAP_HISTORY.md, AGENTIC_KNOWLEDGEBASE.md, RERANKER_CRITIQUE.md,
API_UNIFICATION_CONTRACT_NOTES.md or research/INTERMEDIATE_FORMATS.md points at
a document that will land later.

Those names are deliberately not written as links. There is nothing in this
tree for them to point at, and marking them up as paths would promise
otherwise.

**`docs/reference/` is the exception, and it is here now.** Around four hundred
source comments cite a held-back document, so for a reader outside this project
the explanation layer of the code pointed at files they could not open. The
three pages under `docs/reference/` are the answer to that: they say what the
appliance accepts, indexes and retrieves, in tables, with no history and no
argument. They ship because nothing in them is written by hand — they are
generated from the registries the appliance itself reads, so publishing them
costs no editorial pass and cannot drift from the code. What stays internal is
the *why*, which is what those citations are anchors for.

## Licence

MIT. Copyright (c) 2026 Fight Fire with Fire Robotics, LLC. See `LICENSE`.
