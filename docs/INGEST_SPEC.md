# File Ingestion Specification

Status: draft for review. Nothing here is implemented.

> **That status line was true for about a day and has been false ever since. Corrected
> 2026-09-15** (`SPRINT_0_6_0.md` Block E step 4). **This document is normative, it is what
> the appliance was built from, and 184 source comments cite it by part and section** (read
> 2026-10-12; 181 on 2026-09-15).
> Part 10's phasing shipped through step 6 — `.docx` and `.pptx` read, `.xlsx` profiled and
> extracted per sheet — and steps 7 (images) and 8 (review tasks) did not: `extract:images`
> is a `TASK_ROWS` row with no handler and `ingest_tasks.py` says so in its deferral reason.
> Part 11 records what happened after step 5, in `#### What shipped:` subsections written by
> the phases that ran. The status line is left standing because deleting it would leave a
> reader wondering what else was silently changed, and because this document's value is that
> a comment can cite 5.3 and mean it.
>
> **How to read it.** A `#### What shipped:` heading, a `> ` blockquote, or an HTML comment
> dated 2026-09-15 is a correction; everything else is the design as it was taken. Where
> something was planned and never built, it is marked rather than deleted — a citation from
> shipped source has to still resolve. Seven corrections were made in this review pass beyond
> the ones the phases already wrote: this line, Part 1's whole inventory, 1.4's absolute
> paths, 2.2's third index, 3.3's row count, Part 4's `index:bm25` row, and 4.2's claim about
> `has_heading_styles`.

Scope: how an uploaded file becomes a document tree in JMFTS, what
`structured_content` we keep while that happens, how the work is broken into
tasks that run one at a time instead of one blocking pipeline call, and how a
document in progress is kept separate from a document that is finished.

Names introduced by this spec, so they are not confused with existing code:

| Name | Meaning |
|---|---|
| `settled` | proposed column on `documents`. The node's own work is done and every child is settled. |
| structuring | the downward pass. A node builds and arranges its own children. |
| rollup | the upward pass. Summaries fold from children into parents. |
| structuring complete | no task scoped to this node is pending. Releases the node's children to begin their own work. This is *not* `settled`. |

---

## Part 1 — Inventory

> **Read the whole of Part 1 in the past tense — it is the inventory this spec was written
> against, and every row of it has moved. Marked 2026-09-15.** `jmfts_core/pipeline.py` is
> the clearest case: it appears four times below and in 11.1's parity table, and it **was
> deleted** when `SPRINT_JOBS.md` Part 15 moved all seven entry points onto the queue. 11.1's
> `#### What shipped:` subsection is the record. Every one of 1.2's six "does not have" items
> now exists. The rows are kept because they are what the design was sized against and
> because the argument each one supports is still the argument; a rewritten inventory would
> make this spec look like it was built against the tree it produced.

### 1.1 What JMFTS has today

| Capability | Location |
|---|---|
| Pipeline + per-stage config | `jmfts_core/pipeline.py:40-201` (`StageConfig`, `PipelineDefinition`, `_resolve_stages`) |
| Per-stage result record | `jmfts-client/jmfts_client/contracts/ingest.py` (`IngestStageResult`: stage, status, detail, error) |
| Markdown heading split | `jmfts_core/structural_splitting.py:32` (`split_on_headings`) |
| Chunking | `jmfts_core/chunking.py` (paragraph, sentence) |
| PELT topic segmentation | `jmfts_core/segmentation.py`, `segment` stage, `ruptures` |
| RAPTOR summarization | `jmfts_core/summarization.py` |
| Fact extraction | `jmfts_core/fact_extraction.py` |
| Content-hash dedupe | `jmfts_core/pipeline.py:689` (`_existing_result`) |
| PDF to markdown | `jmfts_core/pdf_extraction.py` — font-size heuristic only |
| HTML to markdown | `jmfts_core/url_fetch.py` |
| Ingest API | `jmfts_core/services/ingest_service.py` — `POST /ingest`, `GET /ingest/pipelines` |

### 1.2 What JMFTS does not have

1. **No file upload.** `IngestRequest.content` is a `str`. `wiki:pdf` takes a
   server-side file path.
2. **No task queue.** `execute_pipeline` runs every stage inside one HTTP
   request. There is no row anywhere that says a stage is pending.
3. **No durable record of what was attempted.** `execute_pipeline` builds a
   `list[StageResult]`, returns it in the HTTP response, and discards it. The
   node's `structured_content` gets `{"section_count": n}` or
   `{"word_count": n}` and nothing else.
4. **No declared-structure extraction.** JMFTS ported
   `vdo_ingest/pdf_to_markdown.py` (the flat converter) and left
   `vdo_ingest/pdf_hierarchical.py` behind.
5. **No Office formats, no image handling, no vision model call.**
6. **No distinction between a document in progress and a finished one.** Every
   row is equally indexed and equally searchable from the moment it is written.

Item 3 is the important one for what to keep. JMFTS already computes the record
we want. It just throws it away. Item 6 is the important one for how work is
scheduled, and it is Part 2.

### 1.3 Facts about the existing schema that this spec depends on

These were read from the code rather than assumed.

**`Document.path` is a denormalized ancestor chain, maintained by a trigger.**
`schema.sql:264-280` defines `update_document_path()`, fired
`BEFORE INSERT OR UPDATE OF parent_id`. It computes one row's path from its
parent's row. It does not cascade to descendants. The descendant cascade is
Python, in `DocumentRepository.move` (`jmfts_core/repositories/document.py:424-431`).

**`path` drives access control.** `schema.sql:398-400`: a principal's effective
right is the highest grant on any access-control root at or above the document
on its `path`, and a document under no access-control root is unprotected. An
empty `path` is therefore not a neutral state.

**BM25 membership is already opt-in.** `search_index_members` keys on
`(index_id, root_document_id)` (`schema.sql:170-175`). A subtree is in a BM25
index because something put it there. A document being ingested does not shift
corpus IDF and does not appear in BM25 results by default.

**Vector and full-text indexes are not opt-in.** `idx_documents_embed`
(`schema.sql:228`, HNSW) and `idx_documents_content_fts` (`schema.sql:221`,
GIN over `ts_vector`) cover the whole table.

**Ingestion walks the tree by `parent_id`, not by `path`.**
`get_children(depth=1)` is `WHERE parent_id = ?` (`document.py:284`), served by
`idx_documents_parent`. The only ingestion-path use of the `path` GIN index is
`get_subtree` at `pipeline.py:856`.

### 1.4 What to port, and from where

> **The two repositories below are private and are not JMFTS.** They are the author's own
> earlier projects, and this section is a record of where a design came from rather than a
> path anybody can follow. Their absolute paths were replaced with their names on
> 2026-09-15; nothing else in the two tables changed, and a reader outside the project
> should treat both as "prior art we had and you do not" — the columns say what each one
> gave us, which is the part that is still worth reading.

**From `triskelion` (private):**

| Component | File | Lines | Gives us |
|---|---|---|---|
| `TaskQueue` model | `vdo_core/models/task_queue.py` | 111 | status, priority, `dependencies` int array, `retry_count`/`max_retries`, `error_type`, `retry_after`, `claimed_by`, `service_badge` |
| Error handling | `vdo_core/task_error_handling.py` | 261 | `ErrorType` (RETRYABLE / PERMANENT / TIMEOUT / DEPENDENCY), `mark_task_failed`, `classify_exception`, `retry_failed_tasks`, `get_retry_stats` |
| Retry DDL | `vdo_core/schema_migration_retry_mechanism.sql` | 196 | the migration |
| `DocumentImage` model | `vdo_core/models/document_image.py` | 77 | `lob_oid`, `mime_type`, `byte_size`, `width`, `height`, `source_page`, `source_bbox` |
| Service loop pattern | `docs/TASK_QUEUE_SERVICES_HOWTO.md` | — | poll / claim / process / store template, `SELECT FOR UPDATE SKIP LOCKED` |

**From `vdo_frontend` (private):**

| Component | File | Lines | Gives us |
|---|---|---|---|
| Structure ladder | `vdo_ingest/pdf_hierarchical.py` | 1036 | `extract_hierarchical_structure()` → `levels_available` |
| TOC analysis | `vdo_ingest/analyzer.py` | 549 | `analyze_toc(detailed=True)` on `get_toc(simple=False)`, `build_hierarchy()` level stack, `extract_all_content_with_positions()`, `assign_content_to_toc_sections()` |
| Image nodes | `vdo_ingest/image_vdo.py` | 293 | image node holds metadata, bytes go to a separate table, embedding comes from `alt_text` |
| Image classification | `vdo_ingest/image_handler.py` | 278 | chart / table / photo classification, title and alt-text generation |
| Blank detection | `vdo_ingest/blank_img_detect.py` | 464 | rejects blank and decorative images |
| `structured_content` taxonomy | `STRUCTURED_CONTENT_USETYPE_REPORT.md` | — | the `processing` and `indexing` object conventions |

`assign_content_to_toc_sections` assigns `toc_section: None` to any content item
that falls outside every declared section. That null count is directly the
coverage measurement this spec depends on. It already exists.

**Not available anywhere, must be written:** upload transport, `.docx` /
`.pptx` / `.xlsx` extraction, vision model call, spreadsheet handling (Part 8),
the `settled` lifecycle (Part 2), and the scheduling model (Part 5).

---

## Part 2 — Document lifecycle

A document being shuffled between pipeline stages and a document that is
finished, embedded, searchable, and referenced by other systems are not the same
kind of record. Today JMFTS treats them identically. This part separates them.

### 2.1 The `settled` column

Proposed: one column on `documents` with three values.

| Value | Meaning |
|---|---|
| `in_flight` | The node exists. Tasks may be pending for it or for its subtree. Its content and its children may still change. |
| `settled` | No task is pending for this node, and every child is settled. Nothing will change it except an explicit correction (Part 6). |
| `failed` | A task for this node failed with a `PERMANENT` error. No retry is scheduled. |

`failed` is not optional. Without it, a node whose ingestion died permanently
looks identical to one still in progress, and a sweeper that settles nodes with
no pending tasks would eventually mark it visible.

`settled` is recursive: a node is settled when its own work is done *and* every
child is settled. A node with no children settles on its own work alone. Part 5
gives the algorithm that computes it.

### 2.2 Indexes become partial

`path` stays correct at all times. The trigger is unchanged and `move`'s
descendant cascade is unchanged. What changes is which rows enter an index.

```sql
CREATE INDEX idx_documents_embed ON documents
    USING hnsw (embed vector_cosine_ops) WITH (m = 16, ef_construction = 64)
    WHERE settled = 'settled';

CREATE INDEX idx_documents_content_fts ON documents
    USING GIN (to_tsvector('english', content))
    WHERE settled = 'settled';

CREATE INDEX idx_documents_path ON documents
    USING GIN (path)
    WHERE settled = 'settled';
```

> **Two of those three shipped; the third did not, and it never should have been in this
> list. Corrected 2026-09-15.** `idx_documents_embed` and `idx_documents_content_fts` are
> partial on `settled = 'settled'` exactly as written (`jmfts_core/sql/schema.sql`), and a
> third partial index this block does not name joined them later —
> `idx_documents_container`, `ON documents (id) WHERE content IS NULL AND settled =
> 'settled'`, which migration 020 added for the container lookup.
>
> **`idx_documents_path` is NOT partial**, and `schema.sql` says so at the index with
> migration 008 behind it: most `path @>` consumers are tree, graph and ACL machinery that
> must see in-flight nodes *precisely because they are the code responsible for managing
> them* — `get_children(depth=-1)`, `get_subtree(include_in_flight=True)`,
> `graph_analysis`'s candidate gather, and the subtree-RBAC containment in
> `jmfts_core/access.py`. None of those can carry a `settled = 'settled'` predicate, so a
> partial index would be unprovable for them and would silently turn every one into a
> sequential scan — which is 2.3's own failure mode arriving through the index instead of
> through the query. The bullet below, "Re-parenting during ingestion writes rows but does
> not churn the path GIN index", is therefore the one claim in *What this buys* that was
> never bought.

- A chunk that is written, embedded, superseded, and rebuilt never causes an
  insert-then-delete in the HNSW graph. It enters the graph once, when it
  settles.
- Re-parenting during ingestion writes rows but does not churn the path GIN
  index.
- Search results never contain a node whose structure is about to be rebuilt.

What it does not cost:

- Access control still resolves, because `path` is still correct on every row.
- There is no second tree-navigation code path. Ingestion already walks by
  `parent_id`.
- BM25 needs no change. It is already opt-in by subtree root.

Rejected alternative: leaving `path` empty during ingestion and computing it at
settle time. It makes every in-flight document unprotected by the access-control
rules in `schema.sql:398-400`, which is backward — a freshly uploaded file is
when protection matters most. Keeping the trigger but skipping the cascade is
worse still: it leaves paths that are wrong rather than absent, in a column that
is both indexed and security-relevant.

### 2.3 `get_subtree` must fail rather than under-report

Postgres uses a partial index only when the query proves the predicate. A
`get_subtree` call without a `settled` filter falls back to a sequential scan.

So `get_subtree` gains an explicit parameter for whether in-flight nodes are
included. The default is settled-only, which uses the index. Ingestion passes
the other value, accepts the sequential scan, and only ever does so over a small
local subtree.

The failure to prevent: a caller asks for the subtree of an in-flight root, gets
the settled-only default, and receives an incomplete tree with no error. That is
a silent wrong answer. `get_subtree` raises when the root is `in_flight` and the
caller did not ask for in-flight nodes.

`pipeline.py:856` is the one existing call site that needs review.

### 2.4 Progress

Because tasks below the current frontier do not exist yet (Part 5.5), the total
task count for an ingestion is unknown while it runs. A percentage would move
backward.

Report the frontier instead: nodes settled, nodes in flight, nodes failed,
counted under the file node. Opinion: a moving frontier reads better than a
percentage that regresses, and I have not checked what a downstream progress
display actually wants.

---

## Part 3 — The `file` usetype and the ingest record

### 3.1 Principle

A file node is created immediately from the uploaded bytes, before any parsing,
with `settled = 'in_flight'`. Ingestion is a set of operations performed *on*
that node afterward. The node is the durable record of what has been tried.

The file node is also the root of the tree that ingestion builds. Extraction
tasks attach children directly to it. There is no separate container node, so
there is one place to look for both the bytes and the result.

A file type is not a structure guarantee. A `.pdf` extension tells us how to
open the bytes. Whether the document declares an outline, whether that outline
covers all of the text, and whether there is a text layer at all are separate
facts, discovered by probing. The spec keeps file type and content pattern in
separate fields for that reason.

### 3.2 The ingest record is recursive

The blocks below are not specific to file nodes. A section node, a slide node,
or a sheet node carries the same blocks, scoped to itself. One schema, applied
at any depth.

A node carries an ingest record when a task has been scoped to it. Ordinary leaf
chunks never get one, so this does not bloat every row.

There is deliberately no new usetype for "a node that can be worked on."
"Structural" would describe how the node was made, and `rung` already records
that. A chapter node is `usetype="section"` — which says what it is — and its
claimability comes from carrying an ingest record, not from its usetype.

### 3.3 The evidence on a node under ingestion

**These blocks are ROWS IN `document_evidence`, not keys in `structured_content`.**
`SPRINT_JOBS.md` Phase 2b moved them; migration 015 is the move and 13.3 is the
decision. What follows describes one node's evidence — the JSON below is the
shape `GET /documents/{id}/evidence` returns, keyed by evidence NAME.

Three things changed with the store and each one is load-bearing:

- **A write is a row, not a read-modify-write on a shared column.** Two handlers
  writing two different names to one node used to lose one of the writes with
  nothing raised. `SPRINT_JOBS.md` 13.1 has the measurement.
- **`structured_content` is wholly the caller's.** A `PATCH /documents/{id}`
  replaces it and reaches no evidence, including under a key of the same name.
  The metadata gate that used to be needed for that is gone.
- **Two names differ from the column keys they replaced**, because evidence is
  named by what it asserts: `anchor` is `source_anchor` and `anchor_unresolved`
  is `source_anchor.unresolved`. The dot in the second is part of a name, not a
  path into the first — they are two rows, which is 3.2's "two keys, never one
  with a null".

A client reading `matched.patterns` out of `structured_content` after this
migration reads nothing, because it is not there. That break is deliberate and
`GET /documents/{id}/evidence` is where the values went.

```json
{
  "file": {
    "filename": "annual_report_2025.pdf",
    "byte_size": 4823919,
    "content_hash": "sha256:9f2a...",
    "blob_ref": "blob:sha256:9f2a...",
    "declared_mime": "application/pdf",
    "detected_mime": "application/pdf",
    "detected_by": "magic_bytes",
    "uploaded_at": "2026-08-15T14:21:03Z"
  },

  "matched": {
    "format": "pdf",
    "patterns": {
      "has_text_layer": true,
      "has_outline": true,
      "outline_depth": 4,
      "has_images": true,
      "image_count": 61,
      "page_count": 88,
      "is_scanned": false
    },
    "probed_at": "2026-08-15T14:21:04Z"
  },

  "attempts": [ ... see 3.4 ... ],

  "structure": {
    "primary_rung": "declared",
    "source": "pdf_outline",
    "coverage": 0.87,
    "node_count": 214,
    "max_depth": 4,
    "gap_regions": 3
  }
}
```

**`file`** is what we received. It never changes after upload. It appears only
on the file node; a section node has no `file` block.

**`matched`** is what probing found. `format` is the file type. `patterns` are
content patterns, and they are the input to deciding which tasks to enqueue.
`has_outline: true` enqueues `structure:declared`. `has_outline: false` does
not, and the node records that the declared rung was never tried.

A pattern is not required to be a boolean. A Part 4 condition tests a pattern
for truthiness, so a count, a list, or a flag all work, and a pattern should
carry as much of what the measurement produced as the scheduler can use.
`pages_with_tables` is the worked example: something has to visit every page to
find the tables, and recording *which* pages have them costs nothing over
recording *that* some do. It is what lets `extract:tables` become one task per
page instead of one task that re-scans the document to find out where to work.
An empty list blocks the row exactly as `false` would.

**Not every pattern comes from `probe`, and `pages_with_tables` is the one that
does not.** It is measured by `extract:text` and reported in that task's
`extraction` record. Finding tables is the single most expensive thing in PDF
ingest — 96.5% of `_probe_pdf`'s wall clock over a 124-file corpus — and the
extraction pass has to find them regardless, because it decides which text
blocks are a table's content and must not also be emitted as prose. Only one of
the two scans can be removed, and extraction's is the one that cannot be. See
`jmfts_core.probe._probe_pdf`.

Two consequences follow, and both are load-bearing:

- **An unmeasured pattern is absent, never false.** Nothing substitutes a
  default. At probe time `plan_after_probe` reports `extract:tables` as
  `patterns.pages_with_tables was not measured`, which is a different sentence
  from `patterns.pages_with_tables is false` and a different fact. Saying "this
  document has no tables" when the truth is "nobody has looked" is the same
  error 11.2 refuses for `probe_failed`.
- **A pattern may therefore arrive after probe.** This is not new: Part 4 already
  predicates the lower structure rungs on "coverage gap remains", a measurement
  the rung above produces, and they are enqueued by the task that measures it
  rather than by `probe`. A row in the table declares what a task *needs*; it
  does not promise that `probe` is what supplies it.

**Page numbers in `patterns` are 0-based**, matching `page_offsets` in the
extraction record so that one integer indexes both. The `toc` in a probe's
attempt detail is 1-based, because that is pymupdf's outline convention and it
is recorded as it was reported rather than silently converted.

**`structure`** summarizes the subtree that resulted, written when structuring
for this node completes. `coverage` is the fraction of extracted text assigned
to a declared or inferred section. `gap_regions` counts the spans that no rung
above `semantic` claimed.

**`attempts`** is the append-only log.

**The example above is a file node's evidence, not the whole set, and the whole
set is a registry rather than a list in this document.** A sheet node carries
`sheet`, a summary node carries `profile`, a chunk carries `source_span`,
`source_anchor` and the five keys the chunker writes, a record node carries
`record`, `row_index` and `sheet_name`, a `cell` node under a record too long to
embed carries `cell`, and a conversation turn carries
`speaker`, `turn_index`, `timestamp`, `conversation_id`, `over_token_window`
and `part_index`. `jmfts_core/evidence.py` is where every one of them is named,
typed and located; `SPRINT_JOBS.md` Part 3 is the design. Thirty-six rows at
this writing (2026-09-15; it was thirty when this sentence was written, and the test below
is how anyone knows it moved), and the count is pinned by
`tests/test_evidence_registry.py::TestShape::test_the_counts_the_documents_quote_are_still_true`
so that this sentence cannot go stale unnoticed.

Two rules follow from it, and both are enforced rather than described:

- **A name that is not in the registry is not evidence.**
  `EvidenceRepository.write` raises for one, so a handler that misspells a block
  fails at the write rather than at the read that comes up empty. The old form
  of this rule was a metadata gate — a list of keys a `PATCH` could not name —
  and it is gone, because the column and the rows no longer share a namespace.
- **A value is what the registry says it is.** `evidence.check` is the one
  place that answers, and a null passes only for a name whose registry entry
  says a null is a result — which is 3.2's distinction, applied at the write.

Both replace a defect rather than restating an old rule. The owned-key list was
seven names written by hand here and in the repository while the pipeline wrote
twenty-three blocks; a `PATCH /documents/{id}` adding one caller field to a
sheet node deleted the `sheet` block, and to a chunk deleted its `source_span`
and its `anchor`. Phase 2 derived the list and Phase 2b removed the need for one.
`tests/test_evidence_registry.py::TestStorage` is the regression, stated over a
finished tree: a finished ingest leaves NOTHING in `structured_content`.

### 3.4 The attempt record

One entry per task attempt. This is `IngestStageResult` with the fields the
scheduling and correction model require.

```json
{
  "task": "structure:declared",
  "task_id": 4471,
  "status": "completed",
  "attempt": 1,
  "rung": "declared",
  "scope_document_id": 1000,
  "write_mode": "children",
  "params": {
    "exclude_patterns": ["^Page \\d+$"],
    "min_section_chars": 200
  },
  "param_fingerprint": "sha256:3c81...",
  "started_at": "2026-08-15T14:21:09Z",
  "finished_at": "2026-08-15T14:21:22Z",
  "detail": {
    "sections": 214,
    "max_level": 4,
    "assigned_items": 1809,
    "unassigned_items": 271,
    "coverage": 0.87
  },
  "produced": { "node_count": 214, "child_ids": [1842, 1843, "..."] },
  "superseded_by": null,
  "error": null,
  "error_type": null
}
```

`status` values: `pending`, `running`, `completed`, `skipped`, `failed`.

**`skipped` and a zero-production `completed` are different facts.** `skipped`
means the task was never attempted, and it must carry `detail.reason` —
`{"task": "describe:images", "status": "skipped", "detail": {"reason": "no
vision model configured"}}`. A heuristic that ran and matched nothing is
`completed` with a detail explaining what it looked for. A heuristic that ran
and found nothing must never look like one that never ran.

`rung` records which method produced the result, so hierarchical summarization
can tell a declared section boundary from a guessed one. A summary built over
declared sections and one built over PELT segments are not the same quality of
evidence, and today nothing in JMFTS can distinguish them.

`params` and `param_fingerprint` are what make re-running possible. Part 6.

`produced.child_ids` is the undo record. Superseding an attempt means removing
what it produced.

`error_type` uses triskelion's `ErrorType` values, so retry policy is decided by
classification rather than by a string match on the message.

### 3.5 Structure rungs

Four rungs, best to worst. Each is attempted only for the regions the rung above
left unclaimed.

| Rung | Task | Source of truth |
|---|---|---|
| `declared` | `structure:declared` | PDF/EPUB outline, DOCX `w:pStyle` Heading 1–9, PPTX slide list, XLSX sheets, Markdown ATX, HTML `<h1>`–`<h6>` |
| `inferred` | `structure:inferred` | font size and weight analysis — the current `pdf_extraction.py` behaviour |
| `semantic` | `structure:semantic` | PELT over `ruptures`, existing `segment` stage |
| `flat` | `structure:flat` | chunking only, no hierarchy |

These are fallbacks, not prerequisites. They are also **per region, not per
document**. A PDF whose outline names eight chapters and nothing below gets
`declared` for the chapter boundaries and `semantic` inside each chapter. Each
node records its own `rung`.

`structure.primary_rung` names the highest rung that claimed anything.
`structure.coverage` says how much it claimed.

---

## Part 4 — Task list

| Task | Scope | Depends on | Condition to enqueue |
|---|---|---|---|
| `probe` | the file node | — | always |
| `extract:text` | the file node | `probe` | `patterns.has_text_layer`, and not `patterns.is_damaged = true` (4.2) |
| `ocr` | the file node | `probe` | `patterns.is_scanned` — out of scope for v1, recorded as `skipped` |
| `structure:declared` | the file node | `extract:text` | the format's declared-structure pattern is true |
| `structure:inferred` | the file node | `structure:declared` | coverage gap remains |
| `structure:conversation` | the file node | `extract:text` | the format's conversation pattern is true |
| `structure:sheets` | the file node | `probe` | the format's sheet-list pattern is true (8.1) |
| `profile:sheet` | `structure:sheets`'s `sheet` children | — | the scope resolves (8.2) |
| `extract:sheet` | `structure:sheets`'s `sheet` children | `profile:sheet` | the scope resolves (8.2) |
| `structure:semantic` | rollup (Part 5.4) | `structure:inferred` | the node is wider than `rollup.max_children` |
| `structure:flat` | the file node | `structure:semantic` | coverage gap remains |
| `extract:tables` | the file node | `extract:text` | `patterns.pages_with_tables` is non-empty — measured BY `extract:text`, not by `probe` (3.3) |
| `extract:images` | the file node | `probe` | `patterns.has_images` |
| `describe:images` | the file node | `extract:images` | a vision model is configured |
| `embed:images` | the file node | `describe:images` | always, after description |
| `citation` | the file node | either prose rung | the format carries a page geometry (`OFFICE_SPEC.md` Part 5) |
| `index:bm25` | the file node | any structure rung | `patterns.has_text_layer` (11.5) |
| `extract:facts` | the file node | any structure rung | `options.facts.enabled` (11.4), and not `patterns.char_count < options.facts.min_characters` (4.2) |
| `embed` | the `chunk`, `record` and `summary` children of the five rules that write them | — | the scope resolves |
| `summarize` | rollup (Part 5.4) | the subtree has settled | the node has children |

> **`index:bm25` left this table on 2026-09-14** (`SPRINT_0_6_0.md` Block B step 7). The row
> is kept above because 11.5 is what declares the task and because source comments cite
> this table by name; what changed is *where* it is offered. The row read
> `after_any=(structure:declared, structure:inferred, structure:conversation)`,
> `requires=(has_text_layer,)` — and **both exclusions are false for a workbook**, whose
> rung is `structure:sheets` and which has no text layer to require. `STRESS_CORPUS.md` 4.4
> measured the consequence on a real corpus: zero `index:bm25` tasks anywhere in a
> 21-workbook subtree, an `xlsx` index holding nothing, and 4,601 `record` nodes with text
> in them.
>
> It is **gone rather than widened** because a workbook's leaves are not written by its
> structure rung and every other format's are — `structure:sheets` writes the `sheet`
> containers and `extract:sheet` writes the rows, one rung further down and scoped to a
> different node — and `_check_task_rows` refuses to order two rows on different nodes. So
> this table cannot state the dependency at all. `MEASURE_BM25_BOUNDARY.md` §5.2 moved it
> to the settling boundary as a set difference; `jmfts_core/index_tasks.py` carries the
> argument and `IngestRollupPlanner` is what offers it. One consequence, stated there:
> `index:bm25` leaves `EXPLAIN`'s answer, because `explain_plan` reads `TASK_ROWS`.
>
> Two other rows on this table are declared elsewhere and were already recorded as such:
> `structure:semantic` and `summarize` belong to the rollup planner (11.4), and
> `extract:facts` is a row here for the reason `jmfts_core/fact_tasks.py` gives (15.5).

`probe` is cheap, needs no model, and always runs. Everything downstream is
decided by what it wrote into `matched.patterns` — and by the two other inputs
11.2 names, the format and the caller's options.

Every task above except `summarize`, `structure:semantic` and `extract:facts`
belongs to structuring. The first two belong to rollup; `extract:facts` runs
over the finished tree and is scheduled from this table anyway, for the reason
`jmfts_core/fact_tasks.py` gives.

### 4.1 A task's scope — `SPRINT_JOBS.md` Phase 3

**The `Scope` column is new and it is the only structural change to this table
since it was written.** Every task here used to be scoped implicitly to the
uploaded file node, and a task scoped to anything else — a worksheet, a chunk —
had nowhere in this table to be declared. Those tasks were written as literal
task specifications inside the handlers that create their nodes, which meant
their parameters were unreachable from a request, their conditions were
invisible to `EXPLAIN`, and the schedule was stated in two places.

A scope has two forms:

- **the file node**, which is the root the plan is evaluated at;
- **the children another named rule produced**, of named kinds.

A child scope resolves against two columns on the node: `produced_by`, which
names the rule that created it, and `usetype`, which names what it is. Both are
needed. `produced_by` alone cannot separate the `section` containers and the
`chunk` leaves one structure rung writes; `usetype` alone cannot separate a
chunk a prose rung wrote from one a transcript rung wrote. A scope reads those
two columns and never queries the tree.

**A scope is not a queue dependency and the distinction is load-bearing.**
`after` orders two tasks on ONE node and resolves to a real `dependencies` id in
the same batch (5.5). A scope says the task's node does not exist yet, which is
a stronger statement no dependency array can make — the settling walk travels
upward and never descends, so the rule that creates a child enqueues that
child's own batch at the moment it creates it.

**`produced_by` is `NULL` for a node nobody's rule produced**: an upload, an
importer, a person. A person who edits a produced node's `content` clears it, so
a later re-derivation creates a sibling rather than silently overwriting the
edit. See migration 016.

### 4.2 A condition is terms, and there are two lists of them — `SPRINT_JOBS.md` Phase 4

**The Condition column above is two lists, not one expression.** A task declares
what it `requires` and what it `forbids`, and the difference between them is what
each does with a name nobody measured:

| List | An unmeasured name means |
|---|---|
| `requires` | the precondition cannot be confirmed — do not run |
| `forbids` | there is no evidence of a blocker — do not block |

Both are "nothing rather than bad output", pointed in opposite directions. The
first protects against running on absent input; the second against a task
standing down for a reason nobody measured. **They may not be flattened into one
expression.** `structure:declared` requires the format's declared-structure
pattern and `structure:inferred` forbids it; for `docx` that pattern is
`has_heading_styles`, which no prober emits, so the declared rung stands down and
the inferred rung runs. One expression with a negation makes both stand down, and
every `.docx` ingests to nothing.

> **"which no prober emits" has been false since the `docx` prober landed. Corrected
> 2026-09-15.** `probe._probe_docx` measures `has_heading_styles` by intersecting the
> outline style ids in `word/styles.xml` with the `w:pStyle` references in
> `word/document.xml` — a style declared and never used is not a declared outline, which is
> why the intersection is what decides. **The argument this paragraph makes is unaffected
> and is still the reason both fields exist**, because the prober omits the pattern rather
> than defaulting it when `word/styles.xml` is missing: an unmeasured name is still what
> `requires` and `forbids` disagree about, and flattening them would still empty every
> `.docx` whose package has no styles part. Only the example's tense is wrong.

**A term is `name`, or `name op value`.** A bare name is truthiness and is not
`= true`: `pages_with_tables` holds a list of page numbers, and a count means "not
zero". The operators are `=`, `!=`, `<`, `<=`, `>`, `>=`. There are no
connectives — `requires` is already a conjunction and `forbids` a disjunction of
exclusions — and their absence is what keeps one recorded reason per term rather
than one per expression.

**A term's right side may name an option.** Without that, a threshold in the rule
table moves only by a code change; with it, the same threshold resolves through
the three layers `ingest_options` already has, and a plan reports both the option
name and the value it resolved to. `options.facts.min_characters` is the first.

**A term's left side may name an option too**, which is what an entirely
optional task is gated on. These are answered ahead of the dependency gate,
because they are true of the request rather than of the file: a caller who turned
fact extraction off is owed that answer, and not "the structure rung it comes
after did not run".

**A caller's threshold belongs in `forbids`.** Probe's counts are per-format —
`char_count` is emitted for `text` alone — so a threshold stated as a requirement
silently stops the task for every format that measures no size. Stated as an
exclusion it says what a caller means: do not spend this work on a document
measured as too small, and spend it where nobody measured.

**A guard may read `matched.patterns` and the resolved options, and nothing
else.** 11.2's rule is unchanged by this section: the plan stays a pure function
of `(format, patterns, options)`. A condition that wants a measurement a task
makes is a condition evaluated at that task's node after it ran, which is the
walk, not this table.

**The pattern vocabulary a guard may read is closed**, and it is narrower than
`matched.patterns`, which is an open namespace. A guard is the one reader for
which an unknown name and a false one are indistinguishable, so a name a guard
does not declare is refused when the table is loaded rather than standing its task
down forever. Two names in that vocabulary are not written by `probe`:
`has_heading_styles`, which `OFFICE_SPEC.md` Part 2 specifies and nothing emits,
and `pages_with_tables`, which `extract:text` writes (3.3).
<!-- Corrected 2026-09-15: `has_heading_styles` IS emitted by `probe._probe_docx` and has
     been since the docx prober landed, so ONE name in the guardable vocabulary is not
     written by probe, not two. `pages_with_tables` is still `extract:text`'s, and the
     sentence about `extract:tables` below is still accurate — nothing re-plans a node
     after extraction. See `ingest_tasks.py`'s comment on the `extract:tables` row. -->
 Nothing re-plans a
node after extraction, so `extract:tables` is not reachable in the queued path
today and its recorded reason says the honest thing: nobody has looked yet.
<!-- Superseded 2026-10-13: the sentence above, and the 2026-09-15 note's "still accurate",
     stopped being true when `extract:tables` landed. `extract:text` re-plans the node after
     extraction and `extract:tables` is reachable in the queued path; what replaced it is the
     paragraph below. Left standing because the note above cites it. -->
At probe time `extract:tables` is not applicable with the reason
"patterns.pages_with_tables was not measured; extract:text measures it and
re-plans extract:tables". `ingest_tasks.PATTERN_MEASURED_BY` names the task that
measures each non-probe pattern, and that task re-plans the rows gated on it:
`run_extract_text` merges the `pages_with_tables` it wrote into its extraction
record with probe's patterns and calls `plan_after_measurement`, which decides
only the rows reading a pattern it measured and enqueues them on the same node in
the same transaction. The guard still reads a patterns dict and options and
nothing else, so the plan stays a pure function and `EXPLAIN` can be asked the
same question with `pages_with_tables` supplied. A row gated this way must be
ordered `after` its measurer and nothing else (`_check_task_rows` rule 6), because
the re-plan is a batch of its own. Once `extract:text` has run, a format whose
reader does not measure the name (`.md`, `.docx`) records "was not measured:
extract:text ran, and it does not measure it for format …" rather than repeating
probe's promise. The re-plan applies 6.1's attempt diff before enqueuing, so a
second commit of `extract:text` (a worker that outlived its lease) queues no
second `extract:tables` row; the handler matches existing children on the natural
key `p{page}.t{index}` and writes nothing when every key is already there.

#### What shipped: `extract:tables` (2026-10-13)

`jmfts_core/table_tasks.py`. `pdf_to_markdown` records a
`{page, index, bbox, start, end, rows, cols}` span for every table it renders
inline, and `extract:text` persists them in the extraction record as `tables`
(with `tables_found`). The handler slices `content[start:end]` into one
`usetype=table` child per span, keyed `p{page}.t{index}`, with `source_span` and
the page and bbox on its `table` evidence, and plans each child's `embed` through
`plan_frontier` (document vector only when the table is past the token window).
It renders nothing: a span that is not a table of the recorded row count in
`content`, a page set that disagrees with `pages_with_tables`, or a count that
disagrees with `tables_found` raises. The table text stays inline in the file
node's markdown as well, so the chunk around it holds the same cells; that
duplicate is accepted for now and is the next measurement (its cost to BM25).

---

## Part 5 — Scheduling

### 5.1 Two passes

Ingestion runs down the tree and then back up.

**Structuring** is downward. A node builds and arranges its own children. When
structuring for a node completes, its children become startable and begin their
own structuring, in parallel with their siblings.

**Rollup** is upward. A node's summary is built from its children's summaries.
It cannot start until every descendant is finished.

A `.pptx` illustrates both. The file node's structuring splits slides into
children, then looks for title slides and one-sentence slides that act as
section dividers and re-parents the slides between them under those dividers.
When both tasks finish, the file node's structuring is complete: its children
are final. Those children — three section nodes, or thirty slide nodes if the
divider heuristic matched nothing — then start their own structuring, which
might break slides into bullet points and nested bullet points.

The divider task matching nothing is `completed`, not `skipped`. It ran.

### 5.2 A node's job is done when its children are startable

Structuring complete is not the same as settled. Naming them separately removes
the ambiguity:

- **Structuring complete** — no task scoped to this node is pending or running.
  Its children exist and are final. This releases the children.
- **Settled** — this node and everything below it are finished. This enables the
  parent's rollup and makes the node indexable (Part 2).

The `.pptx` node reaches the first state in seconds and the second one much
later.

The queue tests structuring complete with a query, not a stored state: are there
tasks scoped to this node that are pending or running?

### 5.3 Declared write mode

A task declares what it writes. The queue checks this at claim time.

| Mode | Writes | Conflicts with |
|---|---|---|
| `self` | this node's own columns and its own evidence rows | another `self` on the same node |
| `children` | this node, plus creates and moves nodes that have no children of their own | another `children` on the same node |
| `subtree` | anything below, including descendants' `path` | anything scoped inside that subtree |

Initial structuring is `children`. Rollup summarization is `self` on the parent
— it reads descendants and writes only its own row. A late structural correction
is `subtree`, and it is the only mode that reserves a region.

**A `children`-mode task may only move nodes that have no children.** Moving a
node that has descendants rewrites their `path`
(`repositories/document.py:424-431`) and invalidates every rollup above it.
`DocumentRepository.move` already loads descendants to fix their paths, so it
can count them first and refuse.

Refusing is correct. The alternative is a path rewrite underneath live work,
which is exactly the class of silent corruption to prevent. A correction that
genuinely needs to move a populated node routes through Part 6 instead.

The childless check and the move must be in one transaction, or a node can gain
children between the check and the write.

In practice the initial run never trips this. The `.pptx` divider task
re-parents slides that were created moments earlier and have no children yet.
The write set is exactly those slides, nothing reaches into a reserved region,
and recursion begins as soon as the task commits.

**Every mode in that table is a region relative to the node the task is scoped
to, and three shipped tasks write outside every one of them.** This is stated
here rather than left to be found, because the same table is where
`SPRINT_JOBS.md` 2.2 draws the `Fact.locus` vocabulary a handler declares its
reads and writes in — `self`, `children`, `subtree`, plus `ancestor` for an
upward read. A task that writes into a region keyed by something other than tree
position has no term in either vocabulary:

| Task | Writes outside its scope | Where |
|---|---|---|
| `extract:facts` | rows in `triples`/`predicates`, and entity nodes | under the entities root, keyed by ACCESS (`SPRINT_0_3_0.md` 7.5) |
| `derive:rule` | rows in `triples`, stamped `derived_by` | the binding's scope, which is not one subtree (`SPRINT_0_5_0.md` Block B) |
| `summarize:tree` | a summary node and its `summarizes` edges | under the derived-tree root, keyed by ACCESS (`SPRINT_0_5_0.md` Block C step 11) |

All three declare `self`, which is the narrowest reservation available and is
true of everything they write *in their own tree* — nothing, in each case. **The
declaration is therefore incomplete, and it is incomplete on purpose.**
`SPRINT_0_5_0.md` open question 6.1 asks whether the locus vocabulary grows a
fifth term for "a node in another tree"; its recorded default is that the
handler declares `self` and this paragraph is written, because a term added
outside the `SPRINT_JOBS.md` phase that owns `EXPLAIN` would be a term nothing
reports on. Step 11 took that default.

What it costs is bounded and worth naming: `EXPLAIN` reports these three as
touching one node, and `atoms.derive_edges` derives no ordering edge into
anything that later reads what they wrote, because the fact they produce has no
name to be consumed under. What it does **not** cost is concurrency safety —
the regions they write into are not reserved by any claim, and each is keyed so
that two tasks reaching a new key at once resolve by unique constraint rather
than by lock (`entity_roots.get_or_create_entities_root`,
`derived_roots.get_or_create_derived_root`).

The access hazard that comes with writing outside your own subtree is a separate
matter and is *not* deferred: a region keyed by access must be keyed by the
access of everything written into it, which is
`derived_roots.widening_descendants` for the third row and
`fact_extraction.resolve_entity` for the first.

### 5.4 Settling walks up, late-bound

The rollup structure cannot be planned in advance. A task list that names the
tree as it currently stands goes stale the moment a child is added, removed, or
moved. So there are no sentinel tasks and no precomputed dependency lists across
levels. Each task, when it finishes, walks one step up and re-reads the tree.

At the end of every task:

1. Mark the task complete and append its attempt record.
2. Read the node's `parent_id` fresh. Take `SELECT ... FOR UPDATE` on that
   parent row.
3. Ask whether the parent has any pending or running task, and whether every
   *current* child of the parent is settled.
4. If work is now eligible for the parent that was not before — the rollup —
   enqueue it. The parent is not settled, because it now has a pending task.
5. If nothing was enqueued and both conditions in step 3 hold, set the parent to
   `settled`.
6. Commit, releasing the parent row.
7. If the parent settled, repeat from step 2 for the grandparent, **in a new
   transaction**.

Why each piece:

- **The row lock serializes step 3.** Two siblings finishing at the same instant
  cannot both see themselves as last, and cannot both miss. A count-and-check
  without the lock produces either a duplicate rollup or none, intermittently.
- **Reading `parent_id` fresh** means a node re-parented between finishing and
  checking signals its new parent.
- **Reading the current child set** means a child added late is simply not
  settled yet, and the check fails until it is.
- **Committing between levels** prevents deadlock. Holding the parent while
  acquiring the grandparent deadlocks against a downward task that took the
  grandparent first and wants the parent. One row lock at a time.

Step 4 and step 5 in one transaction is what makes the rollup fire exactly once.

The rollup creating a summary node as a child of the node it summarizes
un-settles that node briefly. The summary node is a leaf, settles immediately,
and the parent re-settles on the next walk. The loop terminates because the
summary node produces no further work.

**Adding a child to a settled node un-settles it**, and invalidates its rollup.
That is the same invalidation a correction needs, so it is one mechanism rather
than two.

**An ADVISORY write into a settled subtree does not un-settle it**, and this is
the one case that needed saying. `SPRINT_JOBS.md` 14.1 raised it: under the walk,
`citation` fires when the file node is evaluated, which is after every chunk
beneath it has settled — so it writes anchors into nodes that are already
settled, and this section did not say whether that is a write to a settled
subtree or a re-derivation of one.

It is neither. It is an advisory write, and the property that makes it safe is
already declared: `ADVISORY_TASK_TYPES` names the tasks whose failure does not
fail the node, on the stated ground that a chunk with no anchor is a chunk. The
same ground decides this. `settled` means "retrievable, and its own work is
done"; an anchor changes neither, because nothing retrieves on it and no rollup
reads it. A write that added or removed a CHILD would be different in kind, and
that is what the paragraph above governs.

So: a task in `ADVISORY_TASK_TYPES` may write evidence into settled descendants
without un-settling them or their ancestors, and it may not write nodes. A task
not in that set may not write into a settled subtree at all — a correction is
what re-opens one (Part 6).

`citation` is still a probe-time batch member today, ordered after whichever
prose rung wrote the chunks, and it claims `subtree` so the chunks below it are
not being written while it runs. The move to the walk is the phase that
generalises the walk; this rule is what it will land against.

### 5.5 What this costs

The task set below the current frontier does not exist until the frontier
reaches it. That is the point — the tree is discovered, not planned. The
consequence is Part 2.4: total task count is unknown while an ingestion runs.

The `dependencies` array from triskelion's `TaskQueue` still earns its place for
ordering tasks *within* one node, where the set is known when they are enqueued.
It is only the cross-level rollup that has to be late-bound.

### 5.6 Where the truth lives

Two places, deliberately:

- `task_queue` rows hold **live** state — claimed, running, retry_after. High
  update frequency, purgeable.
- The `attempts` evidence row holds the **durable** record. It survives task
  purging and it is what a search result or a person reads. It was a key in
  `structured_content` until Phase 2b; appending to it is one
  `value || entries` statement now, so two writers cannot lose an entry between
  a read and a write.

A service writes the attempt entry when it finishes a task, successfully or not.
This mirrors triskelion's separation and its stated reason: tasks are ephemeral
infrastructure state, not knowledge.

### 5.7 What `POST /ingest` returns for a file

The call returns once the file node exists with `settled = 'in_flight'`, the
blob is stored, and `probe` is enqueued. It does not wait for extraction. The
response is the file node id plus the current attempt log, which at that moment
is one `pending` entry.

The client polls the node. Existing text pipelines keep their current
synchronous behaviour; the file pipeline is the first asynchronous one.

### 5.8 Workers

Workers run in-process. Triskelion split services into separate processes under
a `ServiceManager` because it had to control GPU VRAM allocation across models.
JMFTS is a single appliance and does not have that problem. The task loop runs
as a worker thread inside the API process.

The `task_queue` schema still keeps `service_badge` and `claimed_by`. They cost
nothing now and they are what a later split would need — and Part 7 needs them
immediately, for a different reason.

---

## Part 6 — Re-running, superseding, invalidation

### 6.1 The dedupe key changes

`_existing_result` (`pipeline.py:689`) short-circuits a repeat ingest on
`content_hash`. Keying the attempt diff on task name alone would mean
`structure:declared` runs once and never again — which makes every correction
either invisible or a manual force-rerun.

The diff key is `(task_name, param_fingerprint)`. The fingerprint hashes every
parameter that affects output: thresholds, exclusion patterns, model name,
chosen shape, column assignments.

Re-ingest of a known file:

1. Same `content_hash` resolves to the same existing file node.
2. Recompute which tasks the current `matched.patterns` and current
   configuration would enqueue, with their current parameters.
3. Diff `(task, fingerprint)` pairs against `attempts`.
4. Enqueue only the difference.

This makes ingestion progressive across time, not only within one run. A file
ingested before a vision model existed has `describe:images` logged as
`skipped`. Configuring a vision model and re-ingesting finds exactly that one
task. No re-parse, no duplicate tree.

### 6.2 Superseding

Re-running a task with different parameters must remove what the previous
attempt produced.

1. Read the superseded attempt's `produced.child_ids`.
2. Delete those nodes and their subtrees.
3. Set `superseded_by` on the old attempt entry to the new attempt's id.
4. Run the new attempt.

The log stays append-only. The superseded entry stays and gains a pointer. That
makes the attempt log an audit trail of corrections, which is what a review
workflow needs anyway.

### 6.3 Invalidation upward and downward

`task_queue.dependencies` gives the DAG forward. Reading it backward — tasks
whose `dependencies` array contains the re-run task's id — gives what is now
stale.

Upward, every ancestor's rollup is stale, because its inputs changed. Setting
the affected node back to `in_flight` and walking up per Part 5.4 handles it:
each ancestor un-settles, and re-settles when the rebuilt subtree finishes.

Un-settling also removes the affected rows from the partial indexes in Part 2.2,
so a stale summary stops being searchable while its subtree is rebuilt. That
falls out of the lifecycle design rather than needing its own mechanism.

### 6.4 Corrections are expensive by construction

A correction that re-parents nodes which now have descendants is a `subtree`
write. It reserves the region, rebuilds it, and re-embeds everything under it.
Changing one column label on a 1,284-row sheet means re-embedding 1,284 nodes.

That is correct rather than a flaw, but it is an argument for offering the
correction *early* — during structuring, when the subtree is one level deep and
the rebuild is cheap. Part 7.

Since the measurements are already computed, a correction can state its cost
before anyone confirms it: "this will rebuild 1,284 nodes."

---

## Part 7 — Review tasks

### 7.1 Thresholds are defaults, not open questions

Every threshold in this spec ships with a default. If a threshold has no
default, every ambiguous document blocks on a human, and a batch of ten thousand
files cannot run unattended.

The rule that replaces "pick the right number":

1. The pipeline always decides.
2. It always records the measurement it decided from.
3. It records how close the call was.
4. A near-boundary decision emits a review task **alongside** doing the default
   thing.

A sheet with a clean header row and a fill ratio of 0.99 is not a judgment call
and generates no review task. A sheet at 0.61 does. The document is complete
either way; the review task offers to improve it.

This is the Fail Early boundary. Proceeding on a default and recording it is a
default. Proceeding on a default and hiding it is a swallowed failure.

### 7.2 A review task produces parameters, not nodes

This is the structural difference from every other task. `structure:declared`
produces 214 chunk nodes. "Ignore page numbers" produces a parameter that makes
`structure:declared` run again with a different fingerprint and produce
different nodes.

So a review task's completion writes `params` for a named task, and the queue
enqueues that task per Part 6.1.

Examples of the labelling work this covers:

- Confirm which chunks are headings to bucket by.
- Select columns across worksheets that share a closed set.
- Tell the structure detector to ignore chapter titles and page numbers.
- Confirm or change a sheet's chosen shape (Part 8.4).

### 7.3 A review task is claimed by a person or an agent, not a worker

Triskelion's `TaskQueue` already has `service_badge` and `claimed_by`, so the
routing exists. What has to change is that retry policy becomes a property of
the badge rather than global.

| | Machine task | Review task |
|---|---|---|
| Claimed within | seconds | possibly weeks |
| `max_retries` | applies | meaningless |
| Failure | classified via `ErrorType` | the reviewer declined |
| Stale claim | worker died, requeue | must expire on a lease |
| Failed-task alerting | included | excluded |

A review task needs a claim lease with an expiry. Without one, an abandoned
claim holds a task forever.

Claiming has to be reachable from outside the worker thread, so it is an API,
not an internal loop: `GET /tasks` with filters, `POST /tasks/{id}/claim`,
`POST /tasks/{id}/complete`. That is the same surface a CLI or TUI would use.

### 7.4 Review tasks never block

Rollup and settling wait on machine tasks only. A review task that can arrive
months later must not hold a document `in_flight` forever, or nothing in a large
batch ever becomes searchable.

Review tasks are advisory. The document settles without them. Completing one
triggers the re-run and invalidation in Part 6, which un-settles the affected
region and re-settles it when the rebuild finishes.

### 7.5 Pre-fill every review task

A task that says "pick the closed-set columns" makes the reviewer redo work the
profile already did. `profile:sheet` already knows which columns have small
distinct counts.

So a review task carries a proposed answer. Approval takes seconds, and an LLM
agent can do it from a short prompt. Measurement always runs first; a review is
never offered before there is something to review.

### 7.6 The payload is the contract

Downstream applications build their own interfaces. That means the review task
payload is a public contract, and it has to be self-describing enough that a
downstream form renders without knowing anything about PDFs or spreadsheets.

```json
{
  "question": "Which columns hold a closed set of values?",
  "field_type": "multi_select",
  "options": ["Stage", "Region", "Owner", "Account", "Deal ID"],
  "proposed": ["Stage", "Region"],
  "target_task": "extract:sheet",
  "target_param": "closed_set_columns",
  "estimated_rebuild_nodes": 1284
}
```

Four field types: `single_select`, `multi_select`, `text`, `confirm`. Opinion:
those four cover the labelling jobs listed in 7.2, and I have not tested them
against a fifth case. Keeping the set small is what stops every downstream
application from hardcoding JMFTS's internal task names.

---

## Part 8 — Worksheets

No prior art exists. I searched vdo_frontend, triskelion, and meta_agentics for
`openpyxl`, `xlsx`, `spreadsheet`, and `worksheet`. There are no Python hits in
any of them. This part is new work.

### 8.1 The guaranteed tree stops at the sheet

A workbook declares exactly one structural fact: it contains named sheets. That
is the whole `declared` rung for `.xlsx`. Everything below a sheet is inferred.

```
file node (usetype="file")
└── sheet node (usetype="sheet")        ← declared, guaranteed
    └── everything below                ← inferred, per-sheet decision
```

**Each sheet is evaluated independently.** A workbook routinely holds a data
table, a pivot summary, a lookup list, and a notes page. One representation for
the whole workbook would be wrong for most of it.

This makes the task granularity per sheet, not per file. A 40-sheet workbook
becomes 40 profile tasks and 40 extraction tasks. One malformed sheet fails its
own two tasks and leaves the other 38 sheets settled. It is the clearest case
for the scheduling model in Part 5.

### 8.2 Two tasks per sheet

| Task | Scope | Depends on | Write mode | Uses an LLM |
|---|---|---|---|---|
| `profile:sheet` | `structure:sheets`'s `sheet` children | — | `children` | no |
| `extract:sheet` | `structure:sheets`'s `sheet` children | `profile:sheet` | `children` | no |

`profile:sheet` measures the sheet, writes the measurements, chooses a
representation, and writes a profile node (8.5).

`extract:sheet` materializes child nodes according to the chosen
representation.

**Two corrections to this table, both recorded rather than quietly applied.**

*The dependency column said `structure:declared` and the code diverged from it.*
The two prose rungs consume TEXT, and a workbook has no text layer and cannot
acquire one without first choosing a rendering for its cells — which is 8.4's
decision and is exactly what 8.1 excludes from the declared rung. The task that
builds a workbook's declared rung is `structure:sheets`, and the nodes it writes
still carry `primary_rung = "declared"`, which is the part 8.1 actually asserts.
The divergence is recorded in `jmfts_core/sheet_tasks.py`.

*The dependency is a SCOPE, not a queue dependency*, and 4.1 is what let this
table say so. Both tasks run on a node `structure:sheets` created, so "depends
on the declared rung" means the node does not exist until that rung made it. The
`after` column now holds only the one real within-node ordering: `extract:sheet`
reads the header verdict and the column labels `profile:sheet` measured, on the
same node, in the same batch.

*`profile:sheet` writes `children` where this table says `self`*, and that is a
deliberate divergence too. A write mode is a concurrency declaration the claim
query acts on (5.3): a `self` task conflicts only with another `self` on the
same node. 8.5 makes the profile a CHILD of the sheet node, so this task writes
children, and declaring otherwise would tell the queue it is safe to run a
children-writer beside it.

**The parameters of both are ingest options.** `sheet_profile.sketch_columns`,
`sheet_records.max_rows` and `sheet_records.with_cell_notes` resolve through
`jmfts_core.ingest_options` like every other task's, so they land in the queue
row's `param_fingerprint` and 6.1's re-run diff can see them move. Two groups
rather than one, so that raising the row ceiling does not also re-measure every
sheet.

Neither task calls a model. Every signal below is counted, not inferred. The
existing `summarize` stage can run over the result during rollup if enabled.

### 8.3 Measurements

`profile:sheet` computes these per sheet and stores them under
`sheet.measurements` — the `measurements` key of the `sheet` evidence row:

| Signal | Definition |
|---|---|
| `rows`, `cols` | used range dimensions |
| `fill_ratio` | non-empty cells ÷ (rows × cols) |
| `header_row` | row 1 is all text, all distinct, no numerics |
| `header_col` | column A is all text and all distinct below row 1 |
| `interior_cardinality` | distinct values in the region below and right of the headers |
| `merged_cells` | count |
| `rendered_tokens` | token count of the sheet rendered as one markdown table |
| per column | `name`, `dominant_type`, `fill_ratio`, `distinct_count`, `is_unique` |

### 8.4 Choosing a representation

Four shapes. The first match wins. The chosen shape is a parameter, so a review
task can override it (Part 7.2), and the decision records its margin (Part 7.1).

**`small_table`** — `rendered_tokens` fits inside the embedding window.

One node holding the whole sheet as a markdown table, embedded as-is. A small
table is often exactly the retrieval unit we want, and splitting it destroys it.
The test is the token budget, not a row count, because the reason to keep it
whole is that it fits.

**`matrix`** — `header_row` and `header_col` are both true, `fill_ratio` is low,
and `interior_cardinality` is small.

The sparse crossing table. Interior cardinality of 1 or 2 is the `X` mark case;
a slightly higher count covers scales like `Y`/`N`/`N/A`. One node per non-empty
interior cell:

```
content:  "Payload Handling, Chassis Rev C: X"
evidence `cell`:      {"row": "Payload Handling",
                       "col": "Chassis Rev C",
                       "value": "X"}
```

Empty cells produce nothing. The absence is real information, but it is
recoverable from the profile node and does not need 40,000 nodes saying so.

**`records`** — `header_row` is true and `fill_ratio` is high.

Rows are instances, column labels are keys. One node per row:

```
content:  "Deal ID: D-4471. Account: Northwind Freight. Stage: Proposal.
           Owner: J. Okafor. Value: 128000. Close Date: 2026-09-30."
evidence `record`:    {"Deal ID": "D-4471",
                       "Account": "Northwind Freight",
                       "Stage": "Proposal",
                       "Owner": "J. Okafor",
                       "Value": 128000,
                       "Close Date": "2026-09-30"}
evidence `row_index`: 47
```

`content` is what we embed, so it is written as labelled prose rather than JSON.
Braces and quotes carry no meaning for the embedding model and consume tokens.
The `record` evidence keeps the typed values, so retrieval returns real
data rather than a re-parse of the rendered string.

Opinion, needs calibration against real files: the labelled-prose form embeds
better than JSON for this. I have not measured it. It is worth an experiment
before the format is fixed, because changing it later means re-embedding.

**A row whose prose does not fit the token/maxsim window is a CONTAINER over its
columns, and the node count above is unchanged: still one `record` per row.** What
changes is whether that node carries `content`. It does while the row fits. When it
does not, `content` is null and one `cell` node is written per column, each holding
that column's share of the same labelled prose — `"Discussion: …"` — and a `cell`
that does not fit either carries no content and gets `chunk` children. Every leaf
therefore fits, and every container gets its document vector from `summarize` over
the concatenation (11.4), which is the treatment `section` has had since that part
was written. `jmfts_core.sheet_records.plan_record` is the rule, and
`docs/STRESS_CORPUS.md` 4.11 is the corpus that forced it: 66 of 4,601 record nodes
ended with no vector, no settled ancestor and a permanently failed `embed` each,
because a sheet can be a container for documents rather than for values.

Splitting by column and not by character is what the measured header buys. A chunk
boundary drawn through the prose lands inside `Discussion:` and produces a node that
names no field; a boundary drawn at a column carries the label on every piece.

```
content:  null
evidence `record`:    {…}          # unchanged: the whole row, typed
  child, usetype `cell`
    content:  "Discussion: The organization defines account types…"
    evidence `cell`:  {"column": "Discussion",
                       "value": "The organization defines account types…",
                       "row_index": 47,
                       "sheet_name": "SP 800-53 Revision 5",
                       "position": 4}
```

**`matrix` above sketches a `cell` evidence with different keys** — `row`, `col`,
`value` — and that shape is unbuilt, so the keys here are the ones the registry
declares. Whichever is written second reconciles them rather than adding a second
name: both assert "which cell of the worksheet is this", and a name that means two
shapes is the ambiguity 3.2 refuses.

**`unstructured`** — nothing above matched.

Free-form layout, a notes page, a form. Read the used range as text blocks and
chunk. Recorded with `rung: "flat"`.

### 8.5 The profile node

One child of the sheet node, `usetype="profile"`, holding what a person notices
when they open a sheet before reading any row. It is embedded and retrievable
like any other node.

The purpose is to let an agent decide what to select next. A retrieval hit on
the profile tells the agent which columns exist, which ones are closed sets, and
which one identifies a row — so its next query can name a column and a value
instead of guessing.

**`profile`, and it was `summary` until 2026-09-07.** The sentence above was
false for as long as it was: `summary` is in both `search_exclude_usetypes` and
`bm25_exclude_usetypes`, so the profile was retrievable by no method at all. The
exclusion is right for what else carries that string — a `summarize:tree` node's
vector is byte-identical to its source node's, so admitting them returns every
answer twice — and wrong for this node, which is measured rather than authored
and is the only text a header-less sheet produces at all. Migration `021` moves
the existing rows; `produced_by = 'profile:sheet'` names them exactly.

`content` is written as plain sentences so it embeds and reads well:

> Sheet "Q3 Pipeline" has 1,284 rows and 11 columns, stored as one node per row.
> Column "Deal ID" holds a distinct value in every row and identifies a row.
> Column "Stage" has 5 distinct values: Prospect, Qualified, Proposal,
> Negotiation, Closed. Column "Region" has 4 distinct values: North, South,
> East, West. Column "Owner" has 23 distinct values. Columns "Value" and
> "Close Date" hold numbers and dates.

**Which factoids we emit.** Only ones that come from counting. No model, no
inference about meaning:

| Factoid | Rule |
|---|---|
| identifier column | `is_unique` is true across all rows |
| closed set column | `distinct_count` is small in both absolute terms and relative to `rows`; the profile lists the actual values |
| high-cardinality column | `distinct_count` is large; the profile gives the count and no values |
| type per column | dominant cell type |
| sparse column | `fill_ratio` well below the sheet's |

We do not name what a column *means*, do not assert relationships between
sheets, and do not classify the sheet's purpose. Those are over-interpretation.
If a sheet's meaning is worth having, the existing `summarize` stage produces it
with a model during rollup, as a separate opt-in task with its own attempt
record.

### 8.6 Cross-sheet closed sets

Two columns whose distinct value sets are equal, or where one is a subset of the
other, is a set comparison over the `columns[].values` already stored by
`profile:sheet`. Counting can find the candidate.

A workbook-level task `propose:links` depends on every `profile:sheet` in the
workbook, compares the stored value sets, and emits a review task per candidate
pair. The machine proposes; the reviewer confirms the meaning. That keeps the
8.5 rule intact — counting never asserts a relationship on its own.

This is the first workbook-level task after the per-sheet fan-out, which is why
it needs all sheets profiled first.

### 8.7 The sheet node's `sheet` evidence

```json
{
  "sheet": {
    "name": "Q3 Pipeline",
    "index": 2,
    "shape": "records",
    "shape_margin": "clear",
    "rung": "inferred",
    "measurements": {
      "rows": 1284, "cols": 11,
      "fill_ratio": 0.94,
      "header_row": true, "header_col": false,
      "interior_cardinality": 3319,
      "merged_cells": 0,
      "rendered_tokens": 71400
    },
    "columns": [
      {"name": "Deal ID", "dominant_type": "text", "fill_ratio": 1.0,
       "distinct_count": 1284, "is_unique": true},
      {"name": "Stage", "dominant_type": "text", "fill_ratio": 1.0,
       "distinct_count": 5,
       "values": ["Prospect","Qualified","Proposal","Negotiation","Closed"]}
    ],
    "profile_node_id": 2094
  }
}
```

`shape` records which representation was chosen. `shape_margin` records whether
the decision was clear or near a boundary; near-boundary emits a review task per
Part 7.1. `rung` is `inferred` for every shape except `unstructured`, which is
`flat`. The sheet node itself was produced at the `declared` rung; the shape
decision below it was not.

### 8.8 Thresholds

The thresholds in 8.4 — what counts as a low `fill_ratio`, a small
`interior_cardinality`, a small `distinct_count` — need calibration against a
set of real workbooks before defaults are written down.

They will be defaults, per Part 7.1, not open questions. Numbers picked now
would look settled when they are guesses, so they are left for measurement.

---

## Part 9 — Decisions already made

**Blob storage: Postgres large objects.** Uploaded bytes follow the
`document_images.lob_oid` precedent from vdo_frontend. `file.blob_ref` holds the
OID. Two consequences to carry over rather than rediscover: deletion must call
`lo_unlink`, and orphan cleanup needs the equivalent of vdo_frontend's
`find_orphaned_images()` / `cleanup_orphaned_lobs()`
(`vdo_ingest/image_vdo.py:215-255`). Backups must include large objects;
`pg_dump` in plain format needs `-b`.

**Workers run in-process.** See Part 5.8.

**Usetype stays an open string.** `file`, `image`, and `sheet` become
base-system supported values, alongside the existing `chunk`, `summary`,
`segment`, `entity`, `conversation`, `markdown`. There is no enum and no
validation list. An application built on JMFTS defines its own usetypes for
record types worth distinguishing — `adjutant:container` is already an example.
vdo_frontend's 14-value Postgres enum was too restrictive for that.

**`path` stays correct at all times.** See Part 2.2.

**`usetype` says what a node IS; `produced_by` says what MADE it**, and the two
are not interchangeable. One structure rung writes both `section` containers and
`chunk` leaves, and three different rungs all write `chunk`, so neither column
answers the other's question. `produced_by` is `NULL` for a node no rule
produced — an upload, an importer, a person — which is the same convention
`triples.derived_by` uses, and it is what a rule's scope resolves against
(4.1). Added by migration 016; `SPRINT_JOBS.md` 4.2 is the reasoning.

---

## Part 10 — Phasing

This spec describes an interactive workflow engine. Building it all at once is
not the recommendation. The order below front-loads what everything else needs
and keeps each step independently useful.

1. **Lifecycle.** The `settled` column, the three states, the partial indexes,
   the `get_subtree` parameter. No new tasks, no queue. This is useful on its
   own — it stops half-written trees from appearing in search results.
2. **Attempt records.** Persist the `list[StageResult]` that `execute_pipeline`
   already builds, with the fields from 3.4. Still synchronous, still one HTTP
   request. This makes ingestion auditable without changing how it runs.
3. **Upload and probe.** The `file` usetype, blob storage, `probe`,
   `matched.patterns`. Still synchronous.
4. **The queue.** Port triskelion's `TaskQueue` and error handling. Add
   `scope_document_id` and write mode. Implement the walk in 5.4. The file
   pipeline becomes asynchronous.
5. **Declared structure.** Port `pdf_hierarchical.py` and `analyzer.py`. The
   rung ladder and coverage.
6. **Office formats.** `.docx`, `.pptx`, `.xlsx`. Part 8.
7. **Images.** Port the vdo_frontend image handling, add the vision model call.
8. **Review tasks.** Part 7. The schema fields from steps 2 and 4 already exist
   by this point, so this adds an API surface and a task badge, not a new
   subsystem.

Steps 1 through 4 ship with every threshold at its default and no review task
ever emitted. Step 8 is then additive.

Steps 1 through 5 are shipped as of 2026-08-16. What sits between step 5 and
step 6 is not another format — it is the configuration model that stops each
format from costing what the last one cost. Part 11.

---

## Part 11 — Direction after step 5

Written 2026-08-16, after the declared and inferred rungs landed and the whole
pipeline ran end to end over a 26-paper corpus. Everything in Part 11 is
direction, not specification: it names the decisions, the constraints on them,
and what has to be measured before any of it is written down as a default.

### 11.1 The synchronous pipeline is deprecated

There are two ingest pipelines and they share no code.

| | A — `execute_pipeline` | B — the queue |
|---|---|---|
| file | `jmfts_core/pipeline.py` | `ingest_tasks` + `structure_tasks` + `ingest_worker` + `settling` |
| input | text you already have | bytes |
| runs | synchronously, inside the request | asynchronously, in the worker |
| unit of work | `StageConfig` in a `PipelineDefinition` | a `task_queue` row plus a registered handler |
| configurable per call | yes, `pipeline_config` | partly — `options` (`4e1d416`), but no per-task `enabled` |
| retries | none | classified, with backoff |
| a failure takes down | the request | one node |
| formats | text you converted yourself | PDF only — 11.3 |
| summarize / extract_facts | yes | no — 11.4 |
| BM25 index entry | yes, unconditionally into `default` | no — 11.5 |

**A is deprecated.** B is the target. When B reaches parity, A is deleted.

The rows where B is behind are each a section of this part. Two of them are not
straight ports: B's BM25 membership should follow the tree rather than default to
one corpus (11.5), and B's summarization should segment in document order rather
than cluster (11.4). A row that says "no" is therefore not always a row that says
"copy what A does".

The reason is not that A is badly built. It is that a request cannot own
chunking, embedding, LLM summarization and fact extraction and still have a
timeout that is both honest and useful. The failure mode is a slow model
returning an HTTP error over a tree that was in fact built correctly.

`POST /ingest` keeps its URL after cut-over and changes what it means: create
the node, enqueue, return the node id and the attempt log. That is the shape
`POST /ingest/file` already has, and 5.7 already specifies it. The client polls
`GET /ingest/file/{id}/frontier`, which already counts a subtree by state.
Server-sent events over those same counts are the obvious next step for a UI
that should not poll, and they add a transport rather than a subsystem.

Until cut-over, the divergence is visible to a caller. The same markdown bytes
sent to `POST /ingest/file` produce an empty file node — `probe` has no text
prober, so nothing is eligible — while `POST /ingest` with `usetype="markdown"`
produces a chunked, embedded, summarized, indexed tree. One content, two
outcomes, decided by which endpoint was picked. Closing that is 11.3's first
deliverable, not a separate bug.

#### What shipped: the cut-over, and one thing this section predicted wrongly.

`SPRINT_JOBS.md` Part 15 (S1–S9) moved all seven entry points onto the queue and
deleted `jmfts_core/pipeline.py`. Every row of the parity table above is closed:
11.3's text entry point, 11.4's `summarize` and `extract:facts`, 11.5's BM25
membership, and the three `wiki:` fetches, which became `fetch:url`,
`fetch:arxiv` and `fetch:path`.

**`POST /ingest` did not become asynchronous.** This section says it would
"create the node, enqueue, return the node id and the attempt log", the shape
`POST /ingest/file` has. 15.2 decision 1 chose otherwise: the request enqueues
and then DRAINS that document's own tasks before returning, so the wire is
unchanged and the caller still gets a finished tree. It is not a second code
path — the tasks, the handlers, the attempt log and the settling walk are the
worker's, and the request is acting as a worker
(`IngestWorker.drain_document`, scoped by `claim_next(root_document_id=...)`).

Two things follow, and both are on purpose. A caller who wants the asynchronous
shape this section describes already has it, unchanged, at `POST /ingest/file`
plus `GET /ingest/file/{id}/frontier` — so the choice is the client's rather
than the endpoint's. And a drain that cannot finish inside
`JMFTS_INGEST_SYNC_TIMEOUT_SECONDS` raises rather than reporting a partial
tree; the work stays queued and the background worker finishes it.

### 11.2 ANALYZE — the plan is a first-class answer

Ingestion should be able to say what it will do before it does it, in the way
SQL's `EXPLAIN` and `ANALYZE` do. Give a document type and a set of options; get
back a description of the pipeline that document will undergo.

Two modes, and the difference between them is the whole point:

| | Input | Answers |
|---|---|---|
| `EXPLAIN` | format + options, no bytes | "what would a `.pptx` with these options do?" |
| `ANALYZE` | real bytes | "what will *this* file do?" |

`ANALYZE` runs `probe` and stops. That is affordable precisely because Part 4
specifies `probe` as cheap and model-free, and it is the same measurement the
real run would make.

`plan_after_probe` is already most of `EXPLAIN`. It is a pure function of
`(format, patterns)` and it already returns three separate lists — eligible,
skipped with a reason, not-applicable with the condition that was false. What is
missing is options as an input, the rungs below `declared`, and a surface to
call it from.

#### What shipped: both modes.

`explain_plan(fmt, options, patterns)` in `jmfts_core/ingest_tasks.py`, exposed
as `POST /ingest/explain`. It is read-only in the strongest sense: no document,
no blob, no queue row, no attempt record, no transaction work.

`ANALYZE` is `IngestService.analyze_ingest`, exposed as `POST /ingest/analyze` —
a multipart upload that is not an upload. It runs the two pure functions `probe`
runs, `detect_format` over the bytes and then `probe_patterns`, and hands what
they measured to that same `explain_plan` with `patterns_source: probed`. **One
planner answers both modes**, which is what stops the forecast and the run from
describing different pipelines.

It stops where this section says it stops. No document is created, no blob is
stored, no large object is written, no queue row is enqueued, no attempt is
recorded; the bytes are read into memory, measured and dropped. That is
affordable for exactly the reason Part 4 gives for `probe` running first, and it
is what makes "analyse the corpus before ingesting it" a cheap check.

Two things it reports that `EXPLAIN` cannot:

- **`already_stored`** — whether these bytes are already in a file node this
  caller may read, from the same lookup `upload_file` uses (`private` included,
  because a private upload asks the narrower question). Not a convenience: an
  upload of already-stored bytes resolves to the existing node and runs **no
  plan at all**, so an answer that omitted this would forecast a run that is not
  going to happen. It carries the existing node's recorded options, which is the
  one refusal a caller cannot otherwise predict — differing options are a 400
  (6.1).
- **`probe_failed`** — a file `probe` would raise on has no plan, and gets none.
  The exception and the classification `classify_exception` would give it are
  reported instead, because that is what the run would record. Emitting a plan
  built on an empty pattern set would say "nothing applies to this file" when the
  truth is "we could not look", and those are different answers.

`EXPLAIN` has no bytes, therefore no patterns, and the third input of
`(format, patterns, options)` is not invented to fill the gap. It answers in one
of two ways and `patterns_source` states which, because an answer that did not
say would be indistinguishable from one that made its patterns up:

| `patterns_source` | When | Answer |
|---|---|---|
| `probed` | `ANALYZE`: `probe` ran over real bytes and measured them | concrete: every task decided, from evidence about this FILE rather than about a format |
| `supplied` | the caller gave patterns — a hypothesis, or a real node's `matched.patterns` pasted back | concrete: every task decided |
| `no_prober` | the format is not in `probe.PROBERS_AVAILABLE` | concrete: `probe_patterns` returns `{}` at runtime, so the empty pattern set is a measured fact about this appliance, not an assumption about the file |
| `unknown` | the format has a prober and nobody said what it would find | conditional: undecidable rows report the resolved patterns that decide them and what they would become |

The `no_prober` row is the honest answer to this section's own example question,
"what would a `.pptx` with these options do?" — today: `probe` identifies it, and
nothing downstream can become eligible, because nothing reports a pattern.

Under `ANALYZE` that same format comes back `probed`, not `no_prober`, with an
empty `patterns` and a `probe_detail` naming the prober it does not have. The
distinction is real: `no_prober` says nothing was opened, `probed` says these
bytes were read and identified and there was nothing further to measure. It is
also what a caller needs in order to tell "this file has no text layer" from
"we cannot look inside this format" — which is exactly the question a `.md` or
`.txt` file raises today.

Per task: `outcome` is one of `enqueued`, `skipped`, `deferred`,
`not_applicable`, `impossible`, `conditional`; `if_condition_holds` is set only
on `conditional`; `requires`/`forbids` come back RESOLVED for the format, with
the declared-structure sentinel replaced by the pattern it names. `impossible`
is the outcome the table shape bought: a row whose requirement names a pattern
the format has no entry for can never fire whatever the bytes are, and that is a
different answer from "false this time". Every `TASK_ROWS` entry appears, plus
`probe` — a task omitted from a plan is a wrong answer, not a short one.

**`scope` is on each task too, since `SPRINT_JOBS.md` Phase 3, and it is not
decoration.** A forecast that listed `structure:sheets` beside `profile:sheet`
with nothing between them would read as two tasks on one node, and the second is
one task per sheet. `@root` is the file node; `@children_of(rule|rule):kind|kind`
is one instance per child a named rule wrote. It also closes what Part 0 of that
document recorded as "`EXPLAIN` stops at the sheet list": the two per-sheet tasks
were not rows, so a workbook's forecast ended at the rung that creates the sheets
and said nothing about what happens to each one.

A task at a child scope is 6.2's LATENT vertex: there is no sheet node yet, so
there is no `profile:sheet` INSTANCE to name, and the forecast reports the rule
and not a count. Reporting how many is Phase 5's, and it is what the
`multiplicity` half of a rule is for.

The plan is NOT re-derived. The concrete case calls `plan_after_probe` and
`_split_by_handler`, the same two calls `run_probe` makes, and
`tests/test_ingest_explain.py` asserts agreement over a grid of
`(format, patterns, options)` — same enqueued tasks in the same order, same
params, same skip/deferral/not-applicable reasons, spelled the same way. A second
evaluator of the same table would be free to disagree with the first, and the
disagreement would surface as an `EXPLAIN` that confidently describes a run that
does something else.

Still missing, both listed above as prerequisites and neither closed by this
step: the rungs below `declared` (`semantic`, `flat`) are predicated on "coverage
gap remains", a measurement the rung above produces rather than a pattern, so
they are still outside what any pure function of `(format, patterns, options)`
can decide; and Part 8's two-per-sheet worksheet tasks are not in `TASK_ROWS`.

**The constraint this places on everything else:** every enqueue decision must
be a pure function of `(format, patterns, options)`. A condition that can only
be evaluated by running the task cannot be explained, and a plan that omits it
is a wrong answer rather than a partial one. This was the argument for expressing
Part 4 as a table of rows — `(task, depends_on, predicate, write_mode, params)`
— evaluated by one loop, rather than as the `if`/`elif` ladder it was. The ladder
wrote each `not_applicable` string by hand, which is tolerable for six tasks and
is not for the thirteen Part 4 lists plus Part 8's two per sheet. `TASK_ROWS` and
`plan_after_probe` are that table, as of `c65c289`.

Configuration-driven scheduling was therefore the prerequisite for ANALYZE, not a
separate feature that followed it — and both modes above are what the table
bought.

### 11.3 Markdown is the intermediate format, not a peer format

PDF is converted to markdown before anything parses it. So markdown is not one
format among several in this pipeline — it is the representation every
text-bearing format converges on, and each format is an *entry point* into steps
that are already being tested by another one. `.txt` is degenerate markdown: no
headings, one region, the `flat` rung.

That is worth stating as a contract rather than leaving as an accident. The
proposal is that every `extract:*` task produces one extraction record, and
everything below it consumes only that record:

```json
{
  "text": "...",
  "page_offsets": [0, 1472, 3310],
  "toc": [[1, "Introduction", 2], [2, "Late Interaction", 2]],
  "source": "pdf_text_layer"
}
```

| Format | `text` | `page_offsets` | `toc` |
|---|---|---|---|
| pdf | markdown from the text layer | page starts | the PDF outline |
| markdown | the bytes | — | empty |
| txt | the bytes | — | empty |
| html | `url_fetch.html_to_markdown` | — | empty |
| docx | ATX from heading styles | — | from `w:pStyle` Heading 1–9 |
| pptx | one region per slide | slide starts | one entry per slide |
| xlsx | does not converge on text — Part 8 |||

`structure:declared`, `structure:inferred`, `structure:semantic` and
`structure:flat` are then written once. `nest`, `chunk_text`, the tree writer
and the coverage measure already consume only `Section` objects and know nothing
above them, so they need no change at all.

Two things must be settled by measurement before this is committed to, and
neither should be assumed:

1. **Is markdown lossless enough?** Tables, speaker notes, footnotes and merged
   cells may carry structure markdown cannot express. Measure on real `.docx`
   and `.pptx` files, the way the declared rung was measured on the 26-paper
   corpus, and report what does not survive the conversion.
2. **Does `declared` mean the same thing per format?** For PDF the declared rung
   is `split_on_outline`. For docx and for authored markdown it is
   `split_on_headings` — the same function that is PDF's *inferred* rung. The
   rung names the quality of the evidence and the splitter names the mechanism,
   and they are not in one-to-one correspondence. `structure:declared` needs a
   per-format splitter, not a per-format task.

#### What `ANALYZE` measured, 2026-08-16

292 files under `agent-harness-py/docs/autonomous-planning/`, each run through
`POST /ingest/analyze` — real bytes, real `probe`, no ingestion:

| detected | count | what would be enqueued |
|---|---|---|
| text | 156 | `probe`, and nothing else |
| pdf | 81 | `probe` + `extract:text` + `structure:inferred` |
| pdf | 29 | `probe` + `extract:text` + `structure:declared` |
| pdf | 3 | `probe` only — all three `is_scanned`, so `ocr` skips with Part 4's reason |
| db / ps / gz / zip / html | 23 | `probe` only |

Zero probe failures across 113 PDFs. 60 of them carry tables, all currently
`deferred`. **156 files — every `.md` and `.txt` in the corpus — get a file node
and nothing under it.** That is the number this section exists to change, and it
is now a measurement rather than an expectation.
<!-- Marked 2026-10-13: "all currently `deferred`" was true of the 2026-08-16 run and is no
     longer true of the tree. `extract:tables` is decided after `extract:text` now, by the
     re-plan 4.2 describes, and a PDF whose text layer renders a table gets `table` nodes.
     The 60 were not re-measured. -->

Two findings that change what gets built, both from the detection half rather
than the pattern half:

- **There is no `markdown` format.** `detect_format` has no markdown branch —
  there is no magic number for it, and the extension is not evidence — so
  authored markdown arrives as `text` from the UTF-8 sniff. `text` and
  `markdown` are nevertheless separate keys in `DECLARED_STRUCTURE_PATTERN`,
  both mapping to `has_headings`, and the `markdown` key is currently
  unreachable. Either the sniff learns to look for ATX structure, or the two
  keys collapse into one. **The second, unless measurement says otherwise:** a
  `.txt` file that happens to open with `# Title` is not distinguishable from a
  markdown file by any evidence in the bytes, and 11.3's own contract says `.txt`
  is degenerate markdown. One format, one prober, one entry point.
- **HTML is not detected either.** 19 of the corpus's 20 `.html` files sniff as
  `text`; the twentieth detects as `html` only because it is not valid UTF-8 and
  fell through to the extension. So `text` is doing double duty today, and an
  HTML file ingested as text would be chunked with its tags in place. The text
  prober has to answer this before an extractor consumes its verdict.

#### What ships first, and the one hazard in it

The `.md`/`.txt` path, in this order:

1. A prober for text: `has_text_layer` (it decoded), `has_headings` (it carries
   ATX structure). This is what moves the 156 off `probe` alone.
2. A decode branch in `extract:text`. Not a conversion — the bytes already are
   the text — writing the same extraction record 11.3's table specifies, with
   `source: "utf8_text"`, no `page_offsets`, and an empty `toc`.
3. **`structure:declared` dispatches on `extraction.source`, not on a PDF
   assumption.** This is question 2 above, settled: today the handler hardcodes
   `split_on_outline` and raises when `toc` is empty. Once markdown reaches the
   declared rung — and it will, because `DECLARED_STRUCTURE_PATTERN` maps it to
   `has_headings` — that raise fires on a file that is perfectly well formed.
   The extraction record already says what produced it; the handler reads that
   and picks the splitter.

The empty-`toc` raise is otherwise correct and stays. A PDF with no outline and
no headings never reaches that task: `has_outline` is false, `structure:declared`
is not applicable, `structure:inferred` runs, and a document with no boundaries
becomes one untitled region whose chunks attach to the file node with
`coverage: 0.0` and one `gap_region`. That is the shipped behaviour and it is the
right one. The raise fires only when `probe` reported an outline and
`extract:text` then found none — both call `doc.get_toc()` on the same bytes, so
that is a genuine contradiction and not a document that merely lacks a table of
contents.

**The hazard: this step turns loud failures into silent successes.** Today an
HTML or markdown file produces an empty node whose `yield` evidence is
`{documents: 0}` — visible, queryable, obviously
wrong. After an extractor exists, the same file produces a tree, and a bad
extraction produces a *plausible* tree. An HTML file chunked with its tags
intact, or a markdown file whose headings the splitter missed, would settle
looking exactly like a success. So the extractor lands with a minimum-yield
measurement in its attempt detail — characters out against bytes in, and
sections found against headings the prober counted — and a yield far below what
the prober predicted is recorded as such. Per 7.1 the threshold is a default with
its measurement in the log, not a question deferred to the caller.

#### What shipped: `.md` and `.txt`

All three steps, plus the two open questions above, settled by the measurement
rather than by preference.

**1. A prober for text**, in `probe._probe_text`, and `text` joins
`PROBERS_AVAILABLE`. It reports `has_text_layer` (the bytes decode to something
other than whitespace), `has_headings` with `heading_count` and
`max_heading_level`, and `has_markup`. Decoding is strict: `UnicodeDecodeError`
is a `ValueError`, so `classify_exception` calls it PERMANENT and a file that
will not decode is recorded once instead of retried three times.

**2. `text` and `markdown` are one format**, which is the first open question
answered as the measurement recommended. There is no `markdown` key in
`DECLARED_STRUCTURE_PATTERN` any more. Nothing in the bytes tells authored
markdown apart from a `.txt` file that opens with `#`, and this section's own
contract says `.txt` is degenerate markdown.

**3. HTML is held back by a pattern, not by a format.** This is the second open
question answered, and differently from the way it was posed. `detect_format`
was NOT taught to sniff HTML — the module's split is that detection says how to
open the bytes, and "open it as UTF-8 text" is correct for an HTML file. What is
wrong is reading it as *prose*, so that verdict belongs to the prober:
`has_markup` is true when the first non-whitespace character is `<` and a tag
matches in the first 4 KB, and `extract:text` now carries `forbids:
("has_markup",)`. An HTML file therefore gets `probe`, an empty node, and this
in its log:

```
patterns.has_markup is true, and extract:text runs only when it is false
```

which is the same loud failure it had before, with the reason attached. The rule
was measured over the same 292 files: no `.md` or `.txt` file is called markup,
18 of 19 `.html` files are (the nineteenth is empty), and so are 11 files with a
`.pdf` extension that are really saved web pages. When an HTML converter lands
it arrives as a new extraction source and this row's condition changes with it.

**4. The rungs dispatch on `extraction.source`.** `structure:declared` reads
`pdf_text_layer` with `split_on_outline` and `utf8_text` with
`split_on_headings`; `structure:inferred` reads the same two sources and labels
the second `atx_headings` instead of `font_size_headings`. A source neither of
them knows raises and names itself. The empty-`toc` raise moved into the outline
splitter, where it is still the contradiction it always was, and no longer fires
on a markdown file that reaches the declared rung.

**The yield check is a pair of numbers, not a threshold.** The prober counts
headings with `find_headings`, the same function `split_on_headings` uses, so
`extract:text` records `bytes_in` / `characters` / `characters_probed` and each
structure attempt records `headings_probed` beside `sections_titled`. Numbers
that must agree and do not are a stronger signal than a fraction nobody has
measured, and there is no constant here to be wrong about. This satisfies the
minimum-yield commitment above; a fraction can still be added when a lossy
converter makes one meaningful.

So a `.md` file now gets `probe` + `extract:text` + `structure:declared`, and a
`.txt` file gets `probe` + `extract:text` + `structure:inferred` — which
produces one untitled region, `coverage: 0.0`, one gap region, and a wide flat
node full of chunks. That last shape is exactly 11.4's input.

### 11.4 The rollup planner

`AttemptDiffPlanner` and the walk in 5.4 exist; nothing is wired to them.
`build_worker_from_settings()` passes `NO_ROLLUP`, so an uploaded file is never
summarized and never has facts extracted. The reference implementation is path
A's `_run_summarize` and `raptor_summarize`.

The constraints the queue adds that the synchronous version did not have:

1. **Termination.** Solved, by the diff on `(task, param_fingerprint)` in 6.1.
   Any planner must produce specs whose completion appends an attempt carrying
   the pair it was enqueued for, or the walk re-enqueues forever.
2. **A summary node un-settles what it summarizes.** Specified in 5.4 and
   already correct: the summary is a leaf, settles on its own walk, and the
   parent re-settles on the next pass.
3. **A section node has no `content`.** Its prose lives in its chunks, because
   putting the same text on the container and on its leaves would place it in
   the retrieval indexes twice and answer one query with both. So the rollup has
   a per-node choice: concatenate the children and embed that directly when the
   concatenation fits the model's window, or summarize with an LLM when it does
   not. The deciding measurement is a token count, and per 7.1 it belongs in the
   attempt detail whichever way it goes.
4. **No model is a `skipped`, not a failure.** Per 3.4, a `summarize` task with
   no LLM configured records `skipped` with `detail.reason`. A missing summary
   must never be silent.
5. **The rung is evidence about the summary.** A rollup over `declared` sections
   and one over `flat` chunks are not the same quality of input, and 3.4 already
   records `rung` per attempt so a later reader can tell them apart. Whatever
   the planner writes should carry it forward.

#### PELT hierarchical summarization is not RAPTOR

Both build a tree above a flat set of children and both call an LLM, which is
where the resemblance stops.

RAPTOR clusters. It embeds the children, groups them by similarity, and
summarizes each group — so a cluster is a *set*, and a node built from one holds
material from wherever in the document it happened to be. That is the right
operation for the question RAPTOR answers, and it is the wrong one for
ingestion, because ingestion is precisely the moment when the document's own
order is still available and free.

PELT segments. The children are a *sequence*, in the order the author wrote
them, and PELT finds the changepoints in it. A segment is therefore a contiguous
run of siblings, a structural node built from one is a span of the document, and
the tree that comes out reads the way the document reads. `jmfts_core/
segmentation.py` already implements this; the conversation pipeline uses it as
an opt-in `segment` stage over message turns.

**This is why segmentation belongs to ingestion rather than to retrieval.** A
retrieval-time process has a bag of nodes. Ingestion has the sequence. Nothing
downstream can recover an order that was discarded here.

RAPTOR over ingested documents is still wanted. It is a different operation on a
tree that already exists, and it needs its own design — not a stage of this one.

#### The mechanism

A node has ~100 children in document order, each embedded, and their text does
not fit one LLM context. The rollup planner then:

1. Runs PELT over the child embedding sequence, giving K changepoints and K+1
   contiguous segments.
2. Creates K+1 structural nodes and reparents the children under them, keeping
   order. The tree is one level deeper and, at this node, ~1/K as wide.
3. Gives each new structural node its own rollup job.

**Recursion runs in both directions, and the second direction is the point.**

- *Downward* is ordinary. PELT does not return even segments; one of the K+1 may
  hold 40 children and still be too wide. That node segments again on its own
  walk, and 5.4 already delivers this for free — the new node is unsettled, its
  children settle, the walk reaches it, and its planner finds it still over the
  limit.
- *Upward* is the one that has to be designed for. The leaves finish, the walk
  returns to the original parent, and that parent now has K+1 children — which
  may itself still be too many. It segments *those*. Same operation, one level
  up, on nodes that did not exist when the first pass ran.

So this is a property of the planner, evaluated every time the walk arrives, and
not a one-shot pass at ingest time. 5.4's walk is what makes both directions the
same mechanism.

#### `effective_content`: represent before interpreting

A structural node carries no `content` — the same rule 3.5's section nodes
follow, and for the same reason: text on both the container and its leaves enters
the retrieval indexes twice and answers one query with both. It skews BM25 and
GIN statistics with material that is not new.

It does carry a text embedding, and where that embedding comes from is the
decision:

- **Concatenate** the children's text, in document order, and embed that —
  while it fits the embedding window.
- **Summarize with an LLM** only when the concatenation no longer fits.

Concatenated length grows quickly walking upward, so a few levels up
summarization becomes unavoidable. The rule is that it does not happen before
then: **the document's original content is not summarized until it must be.**
Embedding progressively larger spans in document order means JMFTS is doing less
interpretation and more representation, and a span that was concatenated rather
than summarized is a stronger record of what the document said than any
paraphrase of it.

This is constraint 3 of the list above, stated as the rule it implies. The
deciding measurement is a token count and it belongs in the attempt detail either
way, so a reader can tell a concatenated node from a summarized one without
inferring it from the text.

Whole-document embeddings become rare under this rule, and that is the correct
outcome rather than a loss. An 8k-token span is a weak retrieval result on its
own; its value to the knowledgebase is in other uses, of which hierarchical
summarization is one. Ingestion that chunks by default is the stronger default,
and the long spans remain available where they are wanted.

#### Termination, and the shapes to watch

The degenerate case is specific and it must be a hard rule, not a heuristic:

1. **A segmentation that returns one segment covering every child is a
   non-result. Create nothing.** Otherwise the next pass sees the same children
   under a new parent, segments them the same way, and the tree extends forever
   as a linked list — depth growing, width never falling, an LLM call per level.
2. **A segment of length 1 gets no container.** One child under a new node adds a
   level and no information.
3. **A node that cannot be segmented stays wide, and records why.** Wide and
   unsummarized is a correct state: the node is retrievable, its children are
   retrievable, and nothing is hidden. It is the one place where quietly making
   progress would be the actual defect.

There is also a real tension with 6.1's termination diff, and it has to be
resolved when this is built. The walk terminates because a completed task appends
an attempt carrying its `(task, param_fingerprint)` pair and the planner then
skips it. But PELT's true input is the *child set*, which changes — a node
segmented once, then given more children, is over the limit again with an
identical fingerprint, and the diff would refuse to re-run it. So the params a
segmentation task is enqueued with must include the measurement that triggered
it (the child count, or the concatenated length), which changes when the input
changes and stays put when it does not.

The shapes this produces over a real corpus are worth measuring rather than
predicting. Three to look for specifically: the single-segment degeneracy above;
maximum fragmentation, where PELT returns a changepoint between every pair of
children and rule 2 has to absorb it; and the asymmetric split, where one segment
holds most of the children and the recursion depth on that branch is much greater
than on its siblings. The last is not necessarily wrong — a document with one
long chapter genuinely is that shape — which is why the rules above bound the
degenerate cases rather than trying to force the tree to be balanced.

#### What shipped: the rollup planner

`jmfts_core/rollup_tasks.py`, and `build_worker_from_settings()` now carries
`IngestRollupPlanner()` where it carried `NO_ROLLUP`. `NO_ROLLUP` remains a named
policy for a caller that wants structuring only.

**Two tasks.** `structure:semantic` segments a node's children;
`summarize` gives a node an `effective_content` embedding. Both are planned at
the settling boundary and neither is in Part 4's table.

**`structure:semantic` moved from structuring to rollup**, which is a change to
Part 4. The reason is 5.4's own: its input is the child sequence *and their
embeddings*, and neither exists until the rung above has finished and its nodes
have settled. Nothing `probe` measures could decide it. Part 4's condition —
"coverage gap remains" — is unchanged in substance; it is simply evaluated when
the subtree is complete rather than when the bytes were first read.

**Write mode is `subtree`.** The upward direction moves containers that have
children of their own, and 5.3 reserves a path rewrite for `subtree`, which is
the only mode that reserves a region. The claim gate already enforces that in
SQL, so this is a real reservation and not a label.

**The planner returns at most one task and the walk brings it back.** That is
what makes 11.4's two recursion directions one mechanism. A wide node is offered
segmentation; when the containers have settled, the walk returns and the node —
now a different width — is re-evaluated. 6.1's diff is applied *inside* the
planner rather than by wrapping `AttemptDiffPlanner`, because the choice between
the two tasks depends on the diff: a node whose segmentation ran and produced
nothing must fall through to `summarize`, not be offered the same segmentation
again.

**A container's first task comes from the same planner**, not from a hardcoded
`summarize`. A container holding forty children must segment before it
summarizes; summarizing first would concatenate all forty — an LLM call at that
size — and then be asked to do it again over the five containers that replaced
them.

**Containers are created in flight and carry work.** The walk only travels
upward, so a node created settled and given no task would never be visited and
would never get an embedding.

**Three things the implementation found, none of which were in the design:**

1. **`ruptures.Pelt` defaults to `jump=5`**, so no breakpoint below index 5 is
   ever considered. A sequence of six children has exactly one candidate
   position, and if `min_size` rules it out the sequence comes back as one
   segment however sharp the change in it is. `pelt_segment` now names `jump` as
   a parameter; the default is unchanged, so the conversation pipeline behaves
   as it did, and that silent resolution limit is now stated where it applies.
   The rollup passes `jump=1`.
2. **`enforce_segment_bounds`' split phase defeats rule 1 by arithmetic.** It
   divides an oversized segment into equal parts, so a run with no changepoint
   in it comes back as two segments and gets containerised — the non-result rule
   1 exists to prevent, arrived at from the other direction. The rollup uses the
   merge phase only, and there is deliberately no `max_segment` option: an
   oversized segment is handled by recursion, which is what §*The mechanism*
   said in the first place.
3. **The trigger is fan-out, not context.** "Segment when the children no longer
   fit one LLM call" is the natural reading of the design, and it was measured
   against the appliance's model at ~190k tokens of context: almost no single
   document reaches that, so a context-driven trigger would never fire and the
   hierarchy would never be built. `max_children` (16) is the trigger and it is a
   BOUND rather than a measurement — the one number here derived from nothing.
   The measurement that would replace it is the fan-out distribution over an
   ingested corpus against retrieval quality per level.

**`effective_content` is stored, and the concatenation is not.** A concatenation
is derivable from the subtree it came from, so storing it at every level would
duplicate the whole document once per level for a fact that can be recomputed.
A summary is not derivable, so it is written down — into the
`effective_content` evidence row, which the full-text index does not read
(`to_tsvector(title || content)`), so a
summary cannot skew the BM25 statistics of the corpus it summarizes. The node
gets a document vector and no token embeddings: MaxSim over text that is not
this node's own `content` would be the same double-count by another route.

**The known limit of this pass.** Section containers written by the structure
rungs settle at the moment they are created, so they are never embedded, and a
node whose children are sections cannot be segmented — segmenting the embedded
subset would build a tree over a different set of children than the node has.
`structure:semantic` records that with a count rather than doing nothing
quietly. So the feature is complete for the wide-flat shape 11.3 produces (a
`.txt` file, one untitled region, N chunks) and does not yet reach a `.md` file's
declared sections. Closing it means making those containers in flight, which
couples settling to the planner being wired and is its own decision.

### 11.5 BM25 membership follows the tree

Nothing ingested through path B reaches a BM25 index. `_index_subtree_bm25` is
called from exactly one place, `execute_pipeline`, so a queued ingestion produces
a tree that vector search can find and full-text search cannot. It is the last
row of 11.1's parity table.

The obvious port is wrong. Path A indexes into `"default"` unconditionally,
creating that index if it is absent, which makes every ingestion a member of one
corpus whether or not anybody asked. 1.3 already records the shape that makes a
better answer available: `search_index_members` keys on
`(index_id, root_document_id)`, so **membership is a property of a subtree root**,
and a node's ancestors are already in its `path`.

So: when an upload names no index, read the new file node's `path` and update
every BM25 index whose `root_document_id` is the node itself or one of its
ancestors. A file dropped into a folder that somebody has indexed joins that
index. A file dropped anywhere else joins nothing.

That last sentence is the deliberate part, and it is a change from path A.
A file uploaded with no `parent_id` has no ancestors, therefore no ancestor
index, therefore no BM25 membership until somebody indexes something above it.
It is invisible to full-text search and visible to vector search, which is a
real asymmetry and not an oversight — it is the same shape as the rest of the
appliance, where indexing a subtree is a thing an operator does on purpose
(1.3: "does not appear in BM25 results by default"). Creating a corpus as a
side effect of an upload is the behaviour being removed, not preserved.

An explicit index name on the upload remains available and overrules the walk.

Two consequences to build for:

- **It is a rollup task, not an inline step.** The index has to see the whole
  subtree, and the subtree does not exist until structuring finishes. So it
  belongs to 11.4's planner at the file node's settling boundary, with
  `write_mode: self` — it creates no nodes — and it terminates on 6.1's diff like
  any other rollup.
- **A moved node changes its answer.** Membership derived from `path` is derived
  from something that can change: reparenting a file into an indexed folder
  should add it, and out of one should remove it. That is a `reparent`
  consequence rather than an ingest one, and it is out of scope here, but a
  membership rule read from the tree cannot pretend the tree is immutable.
