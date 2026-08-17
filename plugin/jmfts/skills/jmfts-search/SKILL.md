---
name: jmfts-search
description: Use when looking up information that may exist in the knowledgebase — prior conversations, ingested articles, agent-saved findings, related work the agent might already have notes on. Wraps the five retrieval methods and the synthesis endpoint that can compose an LLM-backed answer from results.
---

# Searching the knowledgebase

Always search before assuming the knowledgebase doesn't have something.
Idempotent ingest only catches exact-content duplicates; a similar topic
might already be there under a different title.

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

Useful flags: `--top-k 5`, `--max-context-tokens 4096`,
`--llm-model <override>`.

## Scope and exclusions

- `--parent-id N` — search inside the subtree rooted at `N`. Useful when
  the agent has a designated root.
- `--exclude-types` — server has a default exclude set (typically
  `chunk`, `summary`); pass an explicit list to override, or `--exclude-types`
  with no values to disable exclusion.
- Chunks rarely have useful titles; if results look noisy, exclude them.

## What you get back

Each result: `document.id`, `title`, `usetype`, `score`, `content`
(possibly truncated), `method`. For deeper context use **jmfts-explore**
on the doc id — `--action get` for the document, `--action ancestors` and
`--action triples` for its relationships.

## When to escalate

- Bad/empty results on `auto`: try `vector` for paraphrase or `bm25` for
  exact terms.
- Too many noisy chunks: add `--exclude-types chunk`.
- Need ground truth from authoritative subtree: pass `--parent-id <wiki-root>`.
- Need an answer, not hits: switch to `jmfts_synthesize --use-llm`.
