---
name: jmfts-read
description: Use when reading a knowledgebase document with full surrounding context — its position in the tree, its children's previews, what cites or references it, related triples — for in-depth comprehension rather than a snippet retrieved by search.
---

# Reading a document with context

`jmfts-search` returns snippets. **jmfts-read** is for "I have an ID, give
me everything I'd need to understand this page."

## Quick reference

```bash
# Pretty-print to terminal (markdown, breadcrumbs, footnotes, child stubs)
python -m scripts.wiki_view_terminal 1234

# Standalone HTML page (good for sharing or eyeballing in a browser)
python -m scripts.wiki_render_html 1234 > /tmp/page.html

# Just the breadcrumb chain
curl "$JMFTS_API_BASE_URL/view/breadcrumbs/1234" | jq

# Who cites / triples-into this document
curl "$JMFTS_API_BASE_URL/view/back-references/1234?limit=20" | jq

# Lazy-load more children
curl "$JMFTS_API_BASE_URL/view/1234/expand-children?offset=20&limit=20" | jq
```

## What `/view/{id}` returns

A composite envelope:

- `rendered_content` — markdown with `[[doc:N]]` and `[[N]]` references
  resolved to `[Title](/view/N)`. Outbound links and triples are
  appended as a `## Footnotes` (or `## References`) section, depending
  on the document's `link_handling` rule.
- `ancestors` — root → parent chain.
- `children_stubs` — paginated children with previews. Whether each
  preview is hidden / one-paragraph / full-with-headings is set by the
  document's `child_handling` rule.
- `outbound_links`, `inbound_links` — typed cross-references.
- `triples` — non-invalidated triples involving this document.
- `presentation` — the resolved rendering rule (renderer, child_handling,
  link_handling).

## When to use each endpoint

| Need | Endpoint |
|---|---|
| Read this page properly | `GET /view/{id}` |
| Just the breadcrumb (e.g. for UI / logging) | `GET /view/breadcrumbs/{id}` |
| Find what links/refers to this | `GET /view/back-references/{id}` |
| Children lazy-load | `GET /view/{id}/expand-children?offset=N&limit=M` |

## Presentation rules

The `usetype_presentations` table maps each document `usetype` to:

- **renderer**: `markdown` (default) | `code` | `json-table` | `transcript` | `plain`
- **child_handling**: `collapsed` | `inline-headings` | `hidden` | `first-paragraph`
- **link_handling**: `footnotes` | `inline-citations` | `sidebar` | `hidden`

The `*` row is the catch-all default. Admin via `/usetype-presentations`
endpoints if rules need adjusting.

## Output format choice

- **Agent reading inline:** ask for the JSON via `wiki_view_terminal --plain`
  or hit `/view/{id}` directly and consume the `rendered_content` field.
  The footnote section gives an inline citation map.
- **Sharing with a human:** `wiki_render_html` produces a standalone
  styled HTML page (light/dark CSS, breadcrumbs, link rendering).

## Limits and tuning

- `?limit_children=N` (default 20). Increase only when you genuinely need
  more children — the response grows linearly.
- `?include=children,links,triples,siblings` — opt out of any block when
  you only want a slice (e.g. `?include=triples`).
- `?link_direction=outbound|inbound|both` — default both.

## When to escalate

- The document is dense and you need its hub-ness or community: hand off
  to **jmfts-analyze**.
- Need related documents by relationship rather than children: **jmfts-explore**.
- Need to find more like this: feed `title` or a content snippet to
  **jmfts-search** with `--method vector`.
