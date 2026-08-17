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

This is `0.1.0` — a first public cut of a system that has been running as a
personal appliance. It works, and it is not yet a product. Retrieval quality
numbers are not published with this release; the BEIR evaluation harness that
produces them follows separately. One thing worth knowing before you rely on it:

- **`?rerank=true` is unmeasured.** The cross-encoder second stage loads a
  standard `CrossEncoder` (`JMFTS_RERANKER_MODEL`, default
  `cross-encoder/ms-marco-MiniLM-L-6-v2`), and it surfaces load/scoring errors
  rather than silently falling back to the first-stage ranking. But that
  default was chosen for size and CPU viability, not for measured retrieval
  quality — `vector→crossenc@100` has never been swept against
  `vector→maxsim@200`. Treat reranked ordering as unvalidated.

## Install and run

```bash
pip install -e ".[dev]"          # editable install with dev tools
python -m scripts.setup_db       # create the database and apply schema.sql
uvicorn api.main:app --host 0.0.0.0 --port 8100 --reload
```

`docker-compose.yml` brings up PostgreSQL + pgvector and the API together if
you would rather not provision a database by hand.

Configuration is environment-variable driven (`JMFTS_` prefix) — see
`.env.example` for the full list, grouped by concern (DB, embedding model,
token selection, BM25 tuning, auth/CORS).

Run the test suite against an isolated, throwaway database (never the real
appliance DB):

```bash
./scripts/run_tests_docker.sh    # easiest: throwaway pgvector + CPU embed
pytest                           # native: needs `ALTER ROLE jmfts CREATEDB`
```

## Layout

```
api/          FastAPI surface — routers generated from the @expose registry
jmfts_core/   the library: services, repositories, ORM models, contracts
scripts/      CLI clients over the REST API, plus database setup
migrations/   incremental SQL; schema.sql is the whole thing at once
plugin/       Claude Code plugin exposing JMFTS as an agent's durable memory
tests/        pytest suite (~1200 tests) against an ephemeral database
```

## Where to go next

- **Architecture and contributing**: `CLAUDE.md` — the layer diagram, data
  flow, code style, and the search repository's internals.
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
