---
name: jmfts-search
description: Use when looking up information that may exist in the knowledgebase — prior conversations, ingested articles, agent-saved findings, related work the agent might already have notes on. Wraps the five retrieval methods plus the `auto` router, and the synthesis endpoint that can compose an LLM-backed answer from results.
---

# Searching the knowledgebase

Always search before assuming the knowledgebase doesn't have something.
Ingest deduplication only catches exact duplicates (same bytes, or the same
URL); a similar topic might already be there under a different title.

The scripts read `JMFTS_API_TOKEN` and send it as `Authorization: Bearer
<token>`; without it the server answers 401.

## Quick reference

```bash
# Default: let the router pick the method
python -m scripts.jmfts_search "query string" --pretty

# Force a method when you have a reason
python -m scripts.jmfts_search "query" --method bm25       # short keyword
python -m scripts.jmfts_search "query" --method vector     # paraphrased
python -m scripts.jmfts_search "query" --method maxsim     # high-precision rerank
python -m scripts.jmfts_search "query" --method hybrid     # weighted combo

# Scope filters
python -m scripts.jmfts_search "query" --usetype markdown
python -m scripts.jmfts_search "query" --parent-id 1234
python -m scripts.jmfts_search "query" --exclude-types chunk summary
```

## Picking a method

Five methods plus `auto`, which routes to one of them:

| Query shape | Method | Why |
|---|---|---|
| Don't know / first try | `auto` | Heuristic router with reasoning in `routing.reason` |
| 1–3 keyword terms, exact phrasing matters | `bm25` | Term-frequency wins on short queries |
| Natural-language paraphrase / conceptual | `vector` | Semantic similarity |
| Need precision; want token-level matching | `maxsim` | Late-interaction late ranking, slower |
| Mixed: terms + concepts | `hybrid` | Reciprocal rank fusion |
| Looking for headings / structured chunks | `fulltext` | PostgreSQL `ts_vector` |

The `auto` router emits `routing.reason` explaining why it chose what it
chose — read it on the first query in a session, then trust it.

## Synthesis (search + LLM answer)

When you want an answer rather than a list of hits:

```bash
python -m scripts.jmfts_synthesize "what does the corpus say about X" --use-llm --pretty
```

`--use-llm` calls `POST /search/synthesize`, which retrieves the top-K and
asks the configured LLM to compose. Without `--use-llm`, the script
returns enriched search results (with ancestors and triples expanded) but
does not invoke the LLM.

Useful flags: `--top-k 5`, `--llm-model <override>`. Every one of the
top-K documents is sent to the LLM whole; if they do not fit its context
window the endpoint refuses and the response says `llm_available: false`,
so lower `--top-k` rather than expecting the context to be trimmed.

## Scope and exclusions

- `--parent-id N` — search inside the subtree rooted at `N`. Useful when
  the agent has a designated root.
- `--exclude-types` — the server holds out `entity`, `entities`,
  `summary` and `derived` by default (`JMFTS_SEARCH_EXCLUDE_USETYPES`,
  `Settings.search_exclude_usetypes` in `jmfts_core/config.py`). Those are
  derived nodes, and a `summary` carries the same vector as its source, so
  including them returns answers twice. Pass an explicit list to replace the
  default, or `--exclude-types` with no values to disable exclusion.
  Chunks are NOT excluded by default.
- Chunks rarely have useful titles; if results look noisy, exclude them
  (`--exclude-types chunk entity entities summary derived` keeps the default
  set as well).

## What you get back

Each result: `document.id`, `title`, `usetype`, `score`, `content`
(possibly truncated), `method`. For deeper context use **jmfts-read** on
the doc id, or **jmfts-explore** for relationships.

## When to escalate

- Bad/empty results on `auto`: try `vector` for paraphrase or `bm25` for
  exact terms.
- Too many noisy chunks: add `--exclude-types chunk`.
- Need ground truth from authoritative subtree: pass `--parent-id <wiki-root>`.
- Need an answer, not hits: switch to `jmfts_synthesize --use-llm`.
