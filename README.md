# JMFTS

JMFTS (John McCardle's Fusion Tree Search) is a retrieval appliance: a
research-focused PostgreSQL + pgvector service combining matryoshka
embeddings, ColBERT-style late interaction (MaxSim), and BM25 into hybrid
search over a tree-structured document store.

Documents form a tree (`parent_id` + a materialized `path`), carry typed
cross-references to each other, and can hold temporal subject-predicate-object
triples with supersession instead of deletion. Five retrieval methods sit on
top: `vector`, `bm25`, `fulltext`, `maxsim`, and `hybrid` (a weighted/RRF
combination), plus an `auto` router. Ingestion pipelines (`markdown`,
`conversation`, `raw`, `transcript`, `wiki:url`, `wiki:arxiv`, `wiki:pdf`) are
idempotent on `(content_hash, parent_id)`.

## Status

This is `0.1.1`. It works, and it is not yet a product. Three things changed
since `0.1.0`, and all three are about running JMFTS as more than one process:

- **Ingestion is a queue, not a function call.** A document carries a
  `settled` column, retrieval indexes are partial on it, and an in-flight
  subtree stays invisible to search until it is complete. Task handlers are
  registered by name rather than chained in a conditional, so a worker can
  claim, retry and heartbeat work that another process enqueued.
- **Embedding is a service you can point at.** `/runner` returns vectors for
  text and owns no documents, behind its own credential. A storage-side worker
  set to `JMFTS_RUNNER_URL` never loads the model.
- **The model is an optional extra.** `pip install jmfts` no longer installs
  torch. See *The model is an extra* below for what a base install can and
  cannot do.

One thing worth knowing before you rely on it:

- **`?rerank=true` is unmeasured.** The cross-encoder second stage loads a
  standard `CrossEncoder` (`JMFTS_RERANKER_MODEL`, default
  `cross-encoder/ms-marco-MiniLM-L-6-v2`), and it surfaces load/scoring errors
  rather than silently falling back to the first-stage ranking. But that
  default was chosen for size and CPU viability, not for measured retrieval
  quality — `vector→crossenc@100` has never been swept against
  `vector→maxsim@200`. Treat reranked ordering as unvalidated.

Retrieval quality numbers are not published with this release; the BEIR
evaluation harness that produces them follows separately.

## Install and run

```bash
pip install -e ".[dev]"          # editable install with dev tools (includes the model)
jmfts-init-db                    # create the database and load the schema
uvicorn jmfts_core.rest.main:app --host 0.0.0.0 --port 8100 --reload
```

`docker-compose.yml` brings up PostgreSQL + pgvector and the API together if
you would rather not provision a database by hand.

### Reading and driving the API

`/docs` is Swagger UI over the live route table — every operation, grouped by tag, with
the request and response schemas. `/redoc` is the same document laid out for reading, and
`/openapi.json` is the document itself, for generating a client.

Press **Authorize** and paste `JMFTS_API_TOKEN` before trying an operation; the page names
which of the two credentials each one takes, since `/runner/*` uses `JMFTS_RUNNER_KEY`
instead and `GET /health` needs neither. All three pages answer without a token — a browser
navigating to a page cannot send an `Authorization` header, so gating them would close the
page rather than protect it. They expose the interface; reaching anything they describe
still costs a token.

Both pages load Swagger UI and ReDoc from `cdn.jsdelivr.net`, which is FastAPI's default. A
host with no route to the internet renders a blank page and must read `/openapi.json`
directly, or be given locally served copies of those assets.

### The model is an extra

**`pip install jmfts` does not install torch.** Base JMFTS is storage, retrieval, the
tree, BM25, the queue and the whole ingest pipeline; the only step that needs an
accelerator is producing vectors, and that step can be somebody else's. Measured:
584 MB installed, against 5.2 GB with the model stack.

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
worker import, the tokenizer half works, and asking for a vector raises.

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
jmfts_core/        the library: services, repositories, ORM models, contracts
jmfts_core/rest/   FastAPI surface — routers generated from the @expose registry
jmfts_core/sql/    schema.sql and the incremental migrations, shipped in the package
jmfts_batch/       batch summarization over an OpenAI- or Anthropic-style batch API
scripts/           CLI clients over the REST API
deploy/            Kubernetes manifests for a worker fleet, and KEDA scaling on queue depth
plugin/            Claude Code plugin exposing JMFTS as an agent's durable memory
tests/             pytest suite against an ephemeral database
```

## Where to go next

- **Architecture and contributing**: `CLAUDE.md` — the layer diagram, data
  flow, code style, and the search repository's internals.
- **Running a fleet**: `deploy/README.md` — what a worker is, which credential
  it carries, and why the scaling signal is queue depth.
- **Using JMFTS as an agent's memory**: `plugin/jmfts/` — search, ingest and
  explore skills over a running instance. `plugin/jmfts/skills/jmfts/SKILL.md`
  is the concept overview.

## A note on documentation

The design documents this code was written against — most importantly the
ingest specification that dozens of source comments cite by section number —
are not in this release. They are being refined for publication separately.
Comments referring to `INGEST_SPEC.md`, `KNOWN-DEFECTS.md` and `ROADMAP.md`
point at documents that will land later; the code stands on its own in the
meantime.
