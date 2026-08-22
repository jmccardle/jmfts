---
name: jmfts
description: Use whenever the user references the knowledgebase, JMFTS, durable memory across sessions, or prior accumulated knowledge that may have been ingested. Provides orientation about a tree-structured document store with typed cross-references and a temporal knowledge graph; routes to more specific skills for searching, ingesting, reading, exploring, and analyzing.
---

# JMFTS — the agent's knowledgebase

JMFTS (Fusion Tree Search) is a retrieval substrate the agent can use as
durable memory across sessions, as a research surface when working on a
task, and as a place to file conclusions worth keeping.

## Concept

- **Documents form a tree** — each doc has a `parent_id` and a `path`
  (array of ancestor IDs). Roots have no parent.
- **Typed cross-references** — documents can link to each other with a
  `link_type` (e.g. `cites`, `derived_from`, `summarizes`).
- **Temporal triples** — subject-predicate-object claims with optional
  `valid_from` / `valid_until` and a `fact_type` (atemporal | static |
  dynamic). Supersession invalidates older claims rather than deleting.
- **Five retrieval methods** — `auto` (router picks), `vector`, `bm25`,
  `fulltext`, `maxsim` (late interaction), `hybrid` (RRF combination).
- **Pipelines** for ingestion: `markdown`, `conversation`, `raw`,
  `transcript`, `wiki:url`, `wiki:arxiv`, `wiki:pdf`. All idempotent on
  `(content_hash, parent_id)`.

## Connection

- `JMFTS_API_BASE_URL` — which instance (default `http://localhost:8100`).
- Scripts live under `$JMFTS_HOME/scripts/` (default
  `~/Development/jmfts/scripts/`); invoke with `python -m scripts.<name>`
  from that directory.

## Working scope (convention, not a feature)

The whole corpus is a valid scope. Many agents prefer a smaller scope:

- **Whole corpus** — pass nothing, default behavior.
- **Designated root** — the agent has been told (or learned) that doc
  `#N` is its working area; pass `--parent-id N` to scope searches and
  ingest under it.
- **Make-and-remember** — an agent can create a fresh root once
  (`jmfts_ingest --usetype markdown --title "Project X notes"`) and
  remember the ID for the rest of its work.

If unsure which root applies, ask the user or default to whole-corpus.
Don't assume `parent_id=0` exists.

## Capabilities — when to load which skill

| Intent | Skill |
|---|---|
| "Find / look up / recall …" | **jmfts-search** |
| "Add / save / remember …" | **jmfts-ingest** |
| "Read this document properly" | **jmfts-read** |
| "What's near / cites / connects to …" | **jmfts-explore** |
| "What's important here / lint / health-check" | **jmfts-analyze** |

The capability skills are auto-discovered too — Claude will load whichever
matches the task. Multiple may load if the work spans intents (e.g. read
something, then ingest a derived analysis).

## Architectural reference

`docs/AGENTIC_KNOWLEDGEBASE.md` is the design document (status: shipped).
Read it when the user asks why JMFTS is shaped a particular way, or when
deciding whether new functionality belongs server-side or client-side.

## Operating principles

1. **Search before ingesting.** Idempotency catches exact dupes, but
   semantically-similar duplicates won't be caught — check first.
2. **Specific over abstract.** Link to the most specific document that
   makes sense; tree paths handle the path-to-root.
3. **Triples for claims, not for content.** Use triples when the
   subject-predicate-object shape adds something a search can't recover
   (temporal validity, structured relationships). Don't extract them
   from arbitrary prose just to have triples.
4. **Don't pollute.** A note worth keeping has a half-life > one session
   and isn't trivially derivable from current files. If in doubt, don't.
