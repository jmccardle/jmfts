---
name: jmfts-analyze
description: Use when assessing knowledgebase health, finding the most important documents in a topic, identifying must-read paths through a subtree, surfacing community structure, or running lint to find orphans, contradictions, stale claims, and coverage gaps.
---

# Analyzing the knowledgebase

Three centrality primitives plus a bundled lint. All server-side
(`/graph/*` endpoints) — these are heavy in data-shipping cost and would
be too slow if implemented client-side.

Every call sends the API token. The scripts read `JMFTS_API_TOKEN`; the
`curl` lines below pass it as `Authorization: Bearer $JMFTS_API_TOKEN`.

## Centrality — three different questions

| Question | Endpoint | Script |
|---|---|---|
| Which specific docs are hubs? | `/graph/centrality` | (curl) |
| Which subtrees should I read first? | `/graph/subtree-authority` | (curl) |
| Must-know reading path through this region? | `/graph/spines` | (curl) |
| Cluster structure of this corpus | `/graph/communities` | (curl) |

The dual-graph nuance: well-curated wikis link to specific descendants,
not the summarizing ancestor. Naive degree centrality systematically
under-weights summary roots. Use **subtree-authority** when looking for
"important areas" rather than "important specific docs."

```bash
# Top hubs by pagerank
curl -s -H "Authorization: Bearer $JMFTS_API_TOKEN" "$JMFTS_API_BASE_URL/graph/centrality?metric=pagerank&top=10" | jq

# Most important subtrees (rolls descendant centrality up the tree)
curl -s -H "Authorization: Bearer $JMFTS_API_TOKEN" "$JMFTS_API_BASE_URL/graph/subtree-authority?decay=0.7&top=10" | jq

# Best reading paths from a root
curl -s -H "Authorization: Bearer $JMFTS_API_TOKEN" "$JMFTS_API_BASE_URL/graph/spines?root_id=1234&max_paths=3" | jq

# Communities (Leiden over the link graph)
curl -s -H "Authorization: Bearer $JMFTS_API_TOKEN" "$JMFTS_API_BASE_URL/graph/communities?scope=links&min_size=3" | jq
```

### Choosing a metric

- `pagerank` — default. Random-walk weight; rewards being pointed at.
- `degree` — raw connection count. Cheap; good for orphan-detection.
- `betweenness` — bridge-ness. Identifies "if I removed this doc, the
  graph splits." Slow on big graphs; use with `--parent-id` to scope.

### Choosing a scope

- `links` — only the typed `document_links` graph (default).
- `triples` — only the subject-predicate-object graph.
- `both` — combined; edges from both layers, weights summed.

## Lint — bundled health check

```bash
# Pretty-print everything
python -m scripts.wiki_lint --pretty

# Markdown report (publishable)
python -m scripts.wiki_lint --format markdown > /tmp/lint.md

# Re-ingest the report as a markdown page (closes the loop; facts off)
python -m scripts.wiki_lint --format markdown --ingest-as markdown --parent-id 0
```

`/graph/lint` runs four sub-audits in one transaction:

| Audit | What it flags |
|---|---|
| **orphan** | Documents with degree ≤ threshold (default 1) |
| **contradiction** | Same `(subject, predicate)` with overlapping validity and different objects |
| **stale** | `fact_type='dynamic'` triples past `threshold_days` (default 90) without supersession |
| **coverage** | High-centrality docs with no summary descendants — RAPTOR candidates |

Severity ordering: errors → warnings → infos.

### Targeted audits (when you don't need all four)

```bash
python -m scripts.wiki_orphan_audit --threshold 1 --pretty
python -m scripts.wiki_contradiction_audit --pretty
python -m scripts.wiki_stale_claim_audit --threshold-days 30 --pretty
python -m scripts.wiki_concept_coverage --since 2026-04-01 --pretty
```

The targeted scripts are useful when:
- You want a single check and don't want to wait for the others.
- You're feeding the result into another script.
- You need fine-grained tuning (e.g., a different stale threshold per
  topic).

## Corpus statistics

```bash
curl -s -H "Authorization: Bearer $JMFTS_API_TOKEN" "$JMFTS_API_BASE_URL/graph/stats" | jq
curl -s -H "Authorization: Bearer $JMFTS_API_TOKEN" "$JMFTS_API_BASE_URL/graph/diff?since=2026-04-01" | jq
```

`/graph/stats`: totals + per-usetype, per-link-type, per-fact-type
breakdowns. Cheap. Useful as the first call in any analysis.

`/graph/diff`: counts of new/changed/superseded entities in a time
window. Useful for "what changed since last week?" workflows.

## Common patterns

**Onboard yourself to a new corpus:**
1. `/graph/stats` — what's here, in what proportions.
2. `/graph/subtree-authority?top=10` — which areas are dense.
3. For each top area, `/graph/spines?root_id=N` — the must-read paths.

**Triage maintenance:**
1. `wiki_lint --format markdown --ingest-as markdown` — produce
   today's report and file it. `--ingest-as` takes a `POST /ingest`
   usetype; `wiki:analysis` is not one and is a 400.
2. The report has `/view/N` links to every flagged doc; walk them in
   priority order.
3. Fix → re-run lint → diff the reports.

**Find the canonical doc on a topic:**
1. `jmfts-search` for the topic.
2. `/graph/centrality?metric=pagerank` over the subtree containing the
   results.
3. Top result is usually the one to cite.

## When NOT to use this skill

- Need the actual content of a document → **jmfts-read**.
- Looking for documents matching a query → **jmfts-search**.
- Tracing relationships hop-by-hop → **jmfts-explore**.
