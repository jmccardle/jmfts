---
name: jmfts-explore
description: Use when navigating knowledgebase structure rather than reading content — a document's tree neighbors (children, ancestors, siblings, full subtree), its outbound or inbound links, its triples, or finding paths between two entities through the knowledge graph.
---

# Exploring relationships and structure

Two graph layers run in parallel over the document tree:

- **Link graph** — typed cross-references via `document_links`
  (e.g. `cites`, `derived_from`, `summarizes`). Source-of-truth for
  "this doc refers to that doc."
- **Triple graph** — temporal subject-predicate-object claims via
  `triples`. Source-of-truth for "this is a fact about the world that
  the corpus mentions."

Plus the **tree** itself (parent/child/path), which is "this doc is part
of that doc."

## Quick reference

`jmfts_explore` exposes nine actions; pick by intent:

| Intent | Action | Notes |
|---|---|---|
| Get one doc | `--action get --doc-id N` | Includes content |
| Immediate kids | `--action children --doc-id N` | `--usetype` filter |
| Whole subtree | `--action subtree --doc-id N` | Optional `--max-depth` |
| Top-level docs | `--action roots` | Whole corpus |
| Path back to root | `--action ancestors --doc-id N` | Root-first |
| Same parent | `--action siblings --doc-id N` | |
| Outgoing/incoming links | `--action links --doc-id N` | `--direction` |
| Triples touching this doc | `--action triples --doc-id N` | `--direction` |
| Multi-hop path entity→entity | `--action path --doc-id A --target-id B` | `--max-depth` |

```bash
python -m scripts.jmfts_explore --action subtree --doc-id 1234 --pretty
python -m scripts.jmfts_explore --action triples --doc-id 1234 --direction outgoing --pretty
python -m scripts.jmfts_explore --action path --doc-id 12 --target-id 99 --max-depth 5
```

## Links vs. triples — when to use which

| Question | Use |
|---|---|
| "Does this doc cite this other doc?" | links |
| "What's referenced from this passage?" | links |
| "Is X-the-thing related to Y-the-thing in some way?" | triples |
| "What does the corpus claim about X?" | triples (`subject_id=X`) |
| "Who has worked on X?" | triples (predicate-aware) |
| "Was this fact ever true / when?" | triples (`valid_from`/`valid_until`) |

Links are *between specific documents*. Triples are *about the entities
those documents represent.* Use both together when an answer wants
both — find the citing docs (links), then check the claims they make
about each other (triples).

## Path-finding through the triple graph

`/triples/path?from_id=A&to_id=B&max_depth=4` returns up to N paths
between two entities, each path a sequence of triples. Useful for
"how is X connected to Y?" questions.

```bash
python -m scripts.jmfts_explore --action path --doc-id 12 --target-id 99 --max-depth 4 --pretty
```

If no path is found within `max_depth`, the entities aren't connected at
that depth — try increasing it before concluding they're unrelated.

## Subtree boundaries

The `path` field on each document is a JSONB array of ancestor IDs.
Subtree queries use the GIN index on `path` and are cheap. `--max-depth`
caps how deep to traverse.

## When NOT to use this skill

- "Read this document with full context" → **jmfts-read** (single-call
  composite, includes children + links + triples in one shot).
- "Find documents about X" → **jmfts-search**.
- "Which documents are most important / hubs" → **jmfts-analyze**.

## Common patterns

**Build a context bundle for a question:**
1. `jmfts-search` to find relevant doc(s).
2. For each, `--action ancestors` to get tree context.
3. `--action triples` to get relevant claims.
4. Hand the bundle to a synthesizer.

**Trace a chain of evidence:**
1. Pick the conclusion's document.
2. `--action links --direction incoming` — what cites it.
3. For each citer, `--action subtree` to see what topic surrounds it.
