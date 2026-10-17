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
- **Five retrieval methods plus `auto`** — `vector`, `bm25`, `fulltext`,
  `maxsim` (late interaction), `hybrid` (RRF combination); `auto` routes to
  one of them and says why.
- **Seven ingest usetypes** for `POST /ingest`: `markdown`, `conversation`,
  `raw`, `transcript`, `wiki:url`, `wiki:arxiv`, `wiki:pdf`, plus file upload
  (`POST /ingest/file`). Every one stores the content, queues its tasks and
  drains them. Dedup is by sha256 across every file node the caller can read,
  not per parent: the same bytes under a new parent are linked there rather
  than copied, and the same bytes with different options are a 400. URL,
  arXiv and path sources dedup on the locator instead. See **jmfts-ingest**.

## Connection

- `JMFTS_API_BASE_URL` — which instance (default `http://localhost:8100`).
- `JMFTS_API_TOKEN` — the API token, sent as `Authorization: Bearer <token>`.
  Required: every route except `/health` and the API docs answers 401 without
  it. A server whose own `JMFTS_API_TOKEN` is blank prints a fresh token at
  every startup, so a token that worked yesterday may not work today.
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

`docs/AGENTIC_KNOWLEDGEBASE.md` is the design document behind this skill
set, and it is **held back**: it is not in the public repository (see the
README's "A note on documentation"), so do not go looking for it. For why
JMFTS is shaped the way it is, read `docs/INGEST_SPEC.md` and
`docs/SPRINT_JOBS.md` for ingest, and `docs/reference/` for what the
appliance accepts, indexes and retrieves.

## Operating principles

1. **Search before ingesting.** Deduplication catches exact dupes, but
   semantically-similar duplicates won't be caught — check first.
2. **Specific over abstract.** Link to the most specific document that
   makes sense; tree paths handle the path-to-root.
3. **Triples for claims, not for content.** Use triples when the
   subject-predicate-object shape adds something a search can't recover
   (temporal validity, structured relationships). Don't extract them
   from arbitrary prose just to have triples.
4. **Don't pollute.** A note worth keeping has a half-life > one session
   and isn't trivially derivable from current files. If in doubt, don't.
