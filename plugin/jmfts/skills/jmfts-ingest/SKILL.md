---
name: jmfts-ingest
description: Use when adding durable content to the knowledgebase — a URL, PDF, arXiv paper, conversation transcript, raw markdown, or an agent-generated finding worth preserving across sessions. Covers the seven ingest pipelines and the idempotency model.
---

# Ingesting into the knowledgebase

## Decision tree

```
What's the source?

URL (article, blog, wiki page)        → jmfts_ingest --usetype wiki:url
arXiv paper (any URL form or ID)      → jmfts_ingest --usetype wiki:arxiv
PDF on local disk                     → jmfts_ingest --usetype wiki:pdf
Markdown / plain text in hand         → jmfts_ingest --usetype markdown
Conversation JSONL or messages        → jmfts_ingest --usetype conversation
Voice transcript                      → jmfts_ingest --usetype transcript
Raw text (no structure)               → jmfts_ingest --usetype raw
```

`--usetype` names the pipeline; the content it receives on stdin (or via
`--content-file`) is whatever that pipeline takes as its source. For the
source-fetch pipelines that source is a URL, an arXiv ID, or a local path
— the server does the fetching.

## Canonical invocations

```bash
# Source-fetch pipelines (server fetches & converts)
echo 'https://example.com/article' | python -m scripts.jmfts_ingest --usetype wiki:url --parent-id 0 --pretty
echo '2310.06770' | python -m scripts.jmfts_ingest --usetype wiki:arxiv --parent-id 0 --pretty
echo './paper.pdf' | python -m scripts.jmfts_ingest --usetype wiki:pdf --parent-id 0 --pretty

# Direct content ingest
python -m scripts.jmfts_ingest --content-file notes.md --usetype markdown --title "Notes" --parent-id 0
echo '## Finding\n\nBody.' | python -m scripts.jmfts_ingest --usetype markdown --title "Finding"
```

## Idempotency

Ingest is idempotent on `(content_hash, parent_id)`. If you re-ingest the
same exact bytes under the same parent, the response carries
`was_existing=true` and `existing_document_id=N` — the prior doc is
returned, no work redone.

Source-fetch pipelines hash AFTER fetching, so the URL/arxiv/PDF you
re-request gets short-circuited too.

This catches exact dupes only. Semantically similar content under
different bytes won't be caught — **search first** when in doubt.

## Pipeline stages

Every pipeline runs: `parse` → `chunk` → optional `summarize` (RAPTOR) →
optional `extract_facts` (LLM-backed triples) → `bm25_index`.

Defaults vary by pipeline. Source-fetch pipelines (`wiki:url`,
`wiki:arxiv`, `wiki:pdf`) default `summarize` and `extract_facts` to
**off** because they're slow and the agent often wants to inspect
ingested content first. Override with flags:

```bash
echo URL | python -m scripts.jmfts_ingest --usetype wiki:url \
  --pipeline-config '{"summarize":{"enabled":true},"extract_facts":{"enabled":true}}'
```

Or with a JSON config:

```bash
python -m scripts.jmfts_ingest --content-file f.md --usetype markdown \
  --pipeline-config '{"summarize":{"enabled":true,"params":{"max_depth":3}}}'
```

## Choosing a parent

- **Whole-corpus root:** `--parent-id 0` if a `0` root exists, otherwise
  no `--parent-id` (creates a new root).
- **Designated subtree:** the agent's working root id, if it has one.
- **Make-and-remember:** create a fresh root the first time, remember
  the ID. The orientation skill describes this convention.

## What goes in `structured_content`

The pipeline auto-populates source-shaped metadata:

- `wiki:url` — `source_url`, `fetched_content_type`, `section_count`.
- `wiki:arxiv` — `arxiv_id`, `title`, `abstract`, `authors`, `categories`,
  `doi`, `published`, `updated`, `url_abs`, `url_pdf`.
- `wiki:pdf` — `source_path`, `filename`, `title`, `author`, `page_count`,
  `toc`, `created`, `modified`.

Agents can pass additional metadata via `--structured-content '{...}'`
(raw-doc mode of `jmfts_ingest`) or by editing the doc afterwards.

## When NOT to ingest

- The content is trivially derivable from current files — agents can
  re-read the source.
- The content's half-life is one session (e.g., a debugging hypothesis
  that's already wrong by the next message).
- The user explicitly said "don't save this."
- Already saved — search first.

## After ingestion

`POST /ingest` returns `source_document_id`. To read it back, or to verify
embeddings landed, use **jmfts-explore** action `get`.
