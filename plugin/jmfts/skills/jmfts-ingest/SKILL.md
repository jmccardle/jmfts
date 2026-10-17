---
name: jmfts-ingest
description: Use when adding durable content to the knowledgebase — a URL, PDF, arXiv paper, conversation transcript, raw markdown, an uploaded file, or an agent-generated finding worth preserving across sessions. Covers the seven ingest usetypes, ingest options, file upload, and the deduplication rule.
---

# Ingesting into the knowledgebase

Every call below needs the API token: the scripts read `JMFTS_API_TOKEN` and send it as
`Authorization: Bearer <token>`, and a `curl` must send the same header. Without it every
route except `/health` and the API docs answers 401.

## Decision tree

```
What's the source?

URL (article, blog, wiki page)        → wiki_ingest_url
arXiv paper (any URL form or ID)      → wiki_ingest_arxiv
PDF the SERVER can read by path       → wiki_ingest_pdf
Any file you hold (PDF, docx, xlsx…)  → POST /ingest/file (upload; see below)
Markdown / plain text in hand         → jmfts_ingest --usetype markdown
Conversation JSONL                    → jmfts_ingest --usetype conversation
Conversation as a message array       → POST /conversations/ingest (see below)
Voice transcript                      → jmfts_ingest --usetype transcript
Raw text (no structure)               → jmfts_ingest --usetype raw
Append a dated entry to a log doc     → wiki_log_append
```

`GET /ingest/pipelines` lists the seven `POST /ingest` usetypes (`conversation`,
`markdown`, `raw`, `transcript`, `wiki:url`, `wiki:arxiv`, `wiki:pdf`), where each one's
content comes from, and the options it resolves to. Anything else is a 400.

## Canonical invocations

```bash
# Source-fetch usetypes (the server fetches)
python -m scripts.wiki_ingest_url https://example.com/article --parent-id 0 --pretty
python -m scripts.wiki_ingest_arxiv 2310.06770 --parent-id 0 --pretty
python -m scripts.wiki_ingest_pdf ./paper.pdf --parent-id 0 --pretty

# Direct content ingest
python -m scripts.jmfts_ingest --content-file notes.md --usetype markdown --title "Notes" --parent-id 0
echo '## Finding\n\nBody.' | python -m scripts.jmfts_ingest --usetype markdown --title "Finding"

# Append-only log
python -m scripts.wiki_log_append --doc-id 1234 "investigated X, found Y"
```

## Deduplication

Ingest never stores the same bytes twice where you can see them. The rule depends on the
entry point:

- **Content and uploads** (`markdown`, `raw`, `transcript`, `conversation`,
  `POST /ingest/file`, `POST /conversations/ingest`). The content is stored as a `file`
  node, and dedup is by **sha256 of the bytes across every file node you can read**, not
  per parent. On a hit nothing is re-run, and the response carries `was_existing=true`
  (and, from `POST /ingest`, `existing_document_id=N`).
  - No `parent_id`: the existing node is returned.
  - A `parent_id` the node is not already under: the existing node is **linked** into
    that parent with a `contains` link. It is not copied and not moved, so it shows up
    in the parent's links (jmfts-explore `--action links`) but not in its subtree.
  - **Same bytes, different options → 400.** The options compared are the fully
    resolved set, so the same text sent as `markdown` and then as `raw` (different
    chunking defaults) is a 400, and so is the same text with a different `--options`.
    Re-running work under new options is not built yet.
  - A `private=true` upload only matches nodes you hold a grant on, so it never resolves
    to someone else's shared node.
- **Source fetches** (`wiki:url`, `wiki:arxiv`, `wiki:pdf`). The bytes are not known
  until the fetch, so dedup is on the **locator**: re-sending a URL, arXiv id or path
  you can already read returns the existing node and fetches nothing. A changed page is
  not re-fetched.

Only exact duplicates are caught. Semantically similar content under different bytes
won't be — **search first** when in doubt.

## What happens to an ingest

There are no pipeline stages any more. Every entry point does the same three things:

1. **Store.** The content becomes a `file` node holding the bytes (or, for a source
   usetype, a node holding the locator plus a `fetch:url`, `fetch:arxiv` or `fetch:path`
   task that fetches them).
2. **Queue.** `probe` reads the bytes, reports a format and a pattern set, and queues the
   tasks that fit what it measured: extraction for the format, a structure rung that
   splits the text into a tree of chunks, `embed` for each leaf, `summarize` for each
   container, and `extract:facts` if facts are on.
3. **Drain.** `POST /ingest` and `POST /conversations/ingest` run that document's tasks
   inside the request and return the finished tree. `POST /ingest/file` returns as soon
   as `probe` is queued, and the background worker drains it.

What a format would run, under which options, is `POST /ingest/explain`. It reads the same
table the run schedules from and writes nothing:

```bash
curl -s -X POST "$JMFTS_API_BASE_URL/ingest/explain" \
  -H "Authorization: Bearer $JMFTS_API_TOKEN" -H "Content-Type: application/json" \
  -d '{"format": "pdf", "options": {"facts": {"enabled": true}}}' | jq
```

`POST /ingest/analyze` is the same question asked of real bytes (a multipart `file`). It
runs `probe`, says whether the bytes are already stored, and stores nothing.

## Ingest options

Options are grouped by the task that reads them, and every layer has the same shape —
`{"group": {"option": value}}`. The groups are `structure` (`chunk_strategy`,
`max_words`, `min_chunk_length`), `rollup`, `facts` (`enabled`, `llm_model`, `max_facts`,
`confidence_threshold`, `include_summaries`, `min_characters`), `embed` (`with_tokens`),
`sheet_profile` and `sheet_records`. An unknown group or option, or a value of the wrong
type, is a 400 that names it.

Fact extraction (`facts.enabled`) defaults to **on** for `markdown`, `raw`, `transcript`
and `conversation`, and **off** for the three `wiki:` usetypes and for uploads. It needs an
LLM endpoint configured on the server. Container summaries always run; there is no switch
for them, because a container's vectors come from its summary.

```bash
# Turn facts on (or --no-extract-facts to turn them off)
python -m scripts.wiki_ingest_url URL --extract-facts

# Any option, as JSON
python -m scripts.jmfts_ingest --content-file f.md --usetype markdown \
  --options '{"structure": {"max_words": 300}}'
```

`--llm-model` on `jmfts_ingest` sets the model for both summaries and facts.

The old `--pipeline-config`, `--summarize` and `pipeline_config` are gone. The server
refuses `pipeline_config` with a 400, because the stages it named no longer exist.

## Uploading a file: `POST /ingest/file`

For bytes you hold rather than a path the server can read: PDF, docx, pptx, xlsx, HTML,
text. It is multipart. `parent_id` and `private` are query parameters, and `options` is a
form field holding JSON.

```bash
curl -s -X POST "$JMFTS_API_BASE_URL/ingest/file?parent_id=1234" \
  -H "Authorization: Bearer $JMFTS_API_TOKEN" \
  -F "file=@paper.pdf" -F 'options={"facts": {"enabled": true}}' | jq
```

It answers 201 as soon as the node exists and `probe` is queued, with `settled` set to
`in_flight`. The tree is built afterwards by the worker, and the node is not searchable
until it settles. Poll progress with:

```bash
curl -s "$JMFTS_API_BASE_URL/ingest/file/<id>/frontier" \
  -H "Authorization: Bearer $JMFTS_API_TOKEN" | jq
```

`private=true` makes the new node its own access-control root, readable only by you. Use
it for an upload with no `parent_id` that should not be visible to every token holder.

## Per-collection defaults: `/documents/{id}/ingest-options`

A folder node can declare options for files later uploaded under it with
`POST /ingest/file`, and nested folders inherit and refine them:

```bash
# What this node declares, what it inherits, and the merge of the two
curl -s "$JMFTS_API_BASE_URL/documents/1234/ingest-options" \
  -H "Authorization: Bearer $JMFTS_API_TOKEN" | jq

# Replace the declaration; {} clears it. Needs write access to the node.
curl -s -X PUT "$JMFTS_API_BASE_URL/documents/1234/ingest-options" \
  -H "Authorization: Bearer $JMFTS_API_TOKEN" -H "Content-Type: application/json" \
  -d '{"options": {"structure": {"max_words": 200}}}' | jq
```

A PUT changes nothing already ingested. It is refused on an ingested file node, whose
`options` are the record of its own run, and on a node produced by a rule.

`POST /ingest` and `POST /conversations/ingest` do not pick these up in practice. They
pass their usetype's complete resolved option set, which overrides every inherited value,
and the source fetches (`wiki:*`) skip the inherited layers altogether. To get a
collection's options, upload the file.

## Conversations: `POST /conversations/ingest`

This takes a transcript either as `messages` (a JSON array of `{role, content,
timestamp?}`) or as `jsonl` (one `{prompt, response, timestamp}` object per line). It
then runs the same store, queue and drain as `--usetype conversation`, with one chunk per
turn.

```bash
curl -s -X POST "$JMFTS_API_BASE_URL/conversations/ingest" \
  -H "Authorization: Bearer $JMFTS_API_TOKEN" -H "Content-Type: application/json" \
  -d '{"title": "Design chat", "parent_id": 1234, "extract_facts": false,
       "messages": [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}]}' | jq
```

`extract_facts`, `max_facts`, `confidence_threshold`, `include_summaries` and `llm_model`
reach the `facts` options. Some fields are refused with a 400 rather than ignored:
`summarize=false`, `raptor_max_depth`, `raptor_min_cluster_size` and
`max_summary_tokens`. They described RAPTOR clustering, which the queue does not do.

## Choosing a parent

- **Whole-corpus root:** `--parent-id 0` if a `0` root exists, otherwise
  no `--parent-id` (creates a new root).
- **Designated subtree:** the agent's working root id, if it has one.
- **Make-and-remember:** create a fresh root the first time, remember
  the ID. The orientation skill describes this convention.

## What goes in `structured_content`

Only what you put there. The pipeline writes nothing into `structured_content`.
What it measures (page count, outline, the source locator, what extraction
found) is evidence, one row per name, read with
`GET /documents/{id}/evidence`.

**`wiki:arxiv` keeps no paper metadata.** Title, authors and abstract are
not recorded anywhere: the fetch stores the PDF only. If you want them, pass
them yourself.

Pass metadata via `--structured-content '{...}'` (raw-doc mode of
`jmfts_ingest`) or by editing the doc afterwards.

## When NOT to ingest

- The content is trivially derivable from current files — agents can
  re-read the source.
- The content's half-life is one session (e.g., a debugging hypothesis
  that's already wrong by the next message).
- The user explicitly said "don't save this."
- Already saved — search first.

## After ingestion

`POST /ingest` returns `source_document_id`. To read it back with context, use the
**jmfts-read** skill. To verify embeddings landed, use **jmfts-explore** action `get`. A
node whose `settled` is not `settled` is still in flight, or `failed`, and search will not
return it.
