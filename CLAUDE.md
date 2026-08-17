# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

JMFTS (John McCardle's Fusion Tree Search) is a research-focused retrieval appliance combining matryoshka embeddings, ColBERT-style late interaction retrieval, and BM25 hybrid search over PostgreSQL with pgvector.

## Common Commands

```bash
# Install (editable mode)
pip install -e .          # production
pip install -e ".[dev]"   # with dev tools

# Database setup
python -m scripts.setup_db

# Run API server
uvicorn api.main:app --host 0.0.0.0 --port 8100 --reload

# Tests — run against an ISOLATED, empty test DB (never production).
# conftest.py auto-provisions/drops a `jmfts_test` database and points the app
# at it, so the suite can no longer touch the real appliance DB. This needs a
# role that can CREATE DATABASE:
./scripts/run_tests_docker.sh         # easiest: throwaway pgvector + CPU embed
pytest                                # native: needs `ALTER ROLE jmfts CREATEDB`
                                      #   (or point JMFTS_DB_* at the docker stack)
pytest -v -s                          # verbose with output
pytest path/to/test_file.py::test_fn  # single test

# Formatting & linting
black --line-length 100 .
ruff check . --fix
```

## Architecture

```
API Layer (FastAPI)          → api/main.py, api/wiring.py, api/routers/
Contracts (Pydantic)         → jmfts_core/contracts/ — the single definition of every shape
Services                     → jmfts_core/services/ (document, search, ingest, graph, …)
Repository Layer             → jmfts_core/repositories/ (search.py, document.py, task_queue.py)
Domain logic                 → jmfts_core/embedding.py, token_selection.py, chunking.py
ORM Models                   → jmfts_core/models/ (document.py, token_embedding.py, search_index.py)
Infrastructure               → jmfts_core/config.py, jmfts_core/database.py, unit_of_work.py
Database                     → PostgreSQL + pgvector (HNSW indexes), schema.sql + migrations/
```

### One definition, many transports

`jmfts_core/registry.py` is the spine. A service method decorated with `@expose("POST", "/search/hybrid", ...)` becomes a first-class operation: in-process Python callers invoke the method directly, and `api/wiring.py` generates the REST route from the same metadata. There is no hand-written second definition of an endpoint to drift.

Consequences worth knowing before you "clean up" something:

- **`@expose`-decorated service methods are reachable even with no in-repo caller.** They are the REST API. Static "unused function" analysis will flag them; it is wrong.
- **`@register_task_handler`-decorated functions in `*_tasks.py` are likewise reachable** — they are dispatched by task-type string through `TASK_HANDLERS`.
- `registry.py` is deliberately FastAPI-free; contracts may not import `api` or `fastapi`. `tests/test_api_parity.py` enforces both, plus a bijection between `REGISTRY` and the mounted routes.
- `api/schemas.py` is a backward-compatibility shim that re-exports `jmfts_core/contracts/`. New shapes go in contracts.

### Data Flow

1. **Ingestion**: Document → `DocumentRepository.create()` → `EmbeddingService` generates embeddings → stored in PostgreSQL
2. **Embedding**: `nomic-ai/modernbert-embed-base` produces 768-dim document embeddings; token-level matryoshka stored at 256-dim halfvec (schema supports 384/512 but only 256 is active)
3. **Token Selection**: `TokenSelector` picks top 50% of tokens via MMR + attention variance + stopword penalties, stored as 256-dim embeddings with importance scores
4. **Search**: Query → embed → repository search (vector/BM25/hybrid/MaxSim) → scored results

### Queued ingest

Large ingests do not run inline. `jmfts_core/ingest_tasks.py` defines the task types and their handler registry; `ingest_worker.py` runs the worker thread; `repositories/task_queue.py` is the claim/retry/write-mode machinery. A document carries a `settled` column — retrieval indexes are partial on it, so in-flight nodes are invisible to search until `settling.py` walks them complete. Adding a rung means registering a handler, not editing a ladder of conditionals.

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
- `JMFTS_LLM_*` — OpenAI-compatible endpoint for synthesis, RAPTOR and fact extraction
- `JMFTS_SUMMARIZATION_*` — local llama.cpp binary + GGUF path, if used instead of the HTTP endpoint

## Search Repository (jmfts_core/repositories/search.py)

This is the most complex file (~1350 lines). It implements:
- **Vector search**: HNSW-indexed cosine similarity on document embeddings
- **MaxSim**: Late interaction scoring — sum of per-query-token max similarities against document tokens
- **BM25**: Custom inverted index with configurable k1/b parameters
- **Hybrid search**: Weighted combination of vector + BM25 + optional MaxSim reranking
- **Full-text search**: PostgreSQL `ts_vector` with trigram fallback

## Code Style

- **Line length**: 100 (Black + Ruff)
- **Python**: 3.11+
- **Patterns**: Repository pattern for data access, dependency injection via FastAPI `Depends()`, context managers for DB sessions
- **Naming**: Pydantic schemas use `*Create`/`*Update`/`*Response` suffixes; private globals prefixed with `_`

## Documentation not in this release

Many source comments cite `INGEST_SPEC.md` (the normative ingest specification, ~60 references), `KNOWN-DEFECTS.md` (D1–D4 anchors, all resolved) and `ROADMAP.md`. Those documents are being refined for separate publication and are not in this repository. Do not treat the citations as broken links to fix, and do not delete them — they are the anchors the documents will be republished against.
