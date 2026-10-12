# A real corpus, ingested with every capability on

> **Reviewed for publication 2026-09-15** (`SPRINT_0_6_0.md` Block E step 5). Three kinds of
> change were made and nothing else was touched.
>
> **1. Redaction.** The host is called "the stress host" rather than by its name. Two
> personal filenames and one sheet name are redacted — the corpus is the author's own
> documents, and a defect report does not need to disclose what is in them. The machine
> specs, the collection names (obsidian, logseq, KDE Connect) and every number are
> unchanged, because they are the measurement.
>
> **2. The harness is not in this repository.** Part 0 names ten scripts under `~/stress/`.
> They were written for this run and are not shipped; `scripts/reindex_workbooks.py`, named
> in §5.5, is the one that is. A reader cannot reproduce Part 0 from the tree.
>
> **3. Four findings have since been fixed and the document still reports them as live.**
> The convention §4's preamble states — *"a defect report that is edited into agreement with
> the patch stops being evidence that the patch was needed"* — is why each is marked in
> place rather than rewritten:
>
> | Where | What has changed |
> |---|---|
> | §1.4 | `.env.example` and `config.py` both document the `/v1` trap now, and both cite this section |
> | §4.4 / §4.4c, §7 q1, the 2026-09-10 addendum | **`index:bm25` moved to the settling boundary** and is no longer a `TASK_ROWS` row. New workbooks are indexed; `MEASURE_BM25_BOUNDARY.md` §5.2 is the argument and `jmfts_core/index_tasks.py` is where it now lives. The `EXPECTED_DIVERGENCE` entry that predicted the move is gone because it happened. |
> | §4.11 | `extract:sheet` chunks now — `plan_record(record, fits=…, chunk=chunk_prose)` in `jmfts_core/sheet_tasks.py`. The paragraph describing it as the one text producer that skips the chunker describes the state this section then fixed. |
> | §1.2 | An uncovered upload no longer produces an `index:bm25` task at all, so there is no `skipped` record with `candidate_roots` to inspect. The advice — create the indexes before the first upload — still holds, for the different reason that a catch-up rule needs a settling boundary to fire and a fully settled tree has none left. |
>
> One number outside that table also moved and is corrected in place: `tests/corpus/` held
> twenty generated files when this was written and holds 23.
>
> **Still open and still accurate:** §4.3 (`ErrorType.DEPENDENCY` is defined and assigned
> nowhere), §4.5 (the `FOR UPDATE` hold), §5.4's embed cache, and Part 7's remaining
> questions. §4.1, §4.2, §4.4b, §4.6 and §4.7 are marked fixed in the text already.
>
> **Line numbers throughout have drifted** — `repositories/search.py` was ~1600 lines when
> this was measured and is over 2000 now. Every citation was spot-checked; the substance
> holds in each case and only the numbers moved. Grep for the quoted text, not the line.
>
> **A key to the markers, which this document uses and never defines.** `ran` means the
> claim below it was produced by running something. `read` means it was read out of the
> code. `inferred` means neither — it is a conclusion drawn from the two.

877 files of the author's own documents — papers, notes, drafts, spreadsheets and voice
transcripts — put through the queued pipeline on one machine with every optional extra
installed, measured at every rung. Run 2026-09-07 on the stress host (RTX 4090, i5-12400,
125 GB RAM) against commit `1440535`.

**Why this document exists.** Every other measurement in `docs/` builds its own fixture.
`ANN_INDEX_HEALTH.md` measured recall on synthetic vectors; `MEASURE_SHACL_SCOPE.md` had to
say out loud that the largest bound scope in the tree is two documents; the corpus in
`tests/corpus/` is 23 generated files (twenty when this was written). This is the first time the appliance has been
asked to hold something that was not built for it, and several conclusions drawn from
fixtures do not survive it.

**The short answer, in four numbers.**

| | |
|---|---|
| 151 MB of source became | **2988 MB** of database — a 19.8× expansion |
| 829 `index:bm25` tasks cost more than | 29,827 `embed` tasks (4468 s vs 3476 s) |
| The shipped MaxSim index returns | **75%** of the true nearest neighbours |
| The spreadsheet corpus reached BM25 | **not at all** |

---

## Part 0 — What was built, and where it is

`ran`.

| Thing | Where |
|---|---|
| Database | container `jmfts-stress-pg` on the stress host, port 5433, volume `jmfts-stress-pgdata`, `restart unless-stopped`, `--shm-size=16g` |
| Server tree | `~/Development/jmfts-stress` — a detached `git worktree` at `1440535`, so the dirty `jmfts-needle` checkout in `~/Development/jmfts` is untouched |
| Environment | `~/Development/jmfts-stress/.venv`, 5.7 GB, `[dev,convert]` — every extra `/capabilities` can report |
| Harness | `~/stress/` on the stress host: `scaffold.py`, `upload.py`, `metrics.py`, `sample.py`, `probe_api.py`, `quality.py`, `reingest.py`, `embed_cache.py`, `indexlab.py`, `fleet.sh` |

PostgreSQL 16.14, pgvector 0.8.5. `pg_stat_statements` loaded, because "which statement
cost the ingest its time" turned out to be the question that mattered.

**Seven collections, one root document each**, exactly as requested — plus a BM25 index per
collection and a `universal` index holding all seven roots, and a named search context per
collection binding `parent_id` and `index_name` together.

---

## Part 1 — Standing it up: what a first-time operator hits

`ran` unless marked. These are in the order they bit.

### 1.1 `pip install 'jmfts[embed]'` silently gives you CPU

`uv pip install -e ".[dev,convert]"` resolved **torch 2.14.0+cu130**. The stress host's driver is
570.86.10, which is CUDA 12.8. `torch.cuda.is_available()` returned `False`, the install
reported success, and nothing in JMFTS would have said otherwise — `/capabilities` reports
`device` from `Settings`, which said `cuda` because that is what was configured, not what
was reachable.

The fix is one pin: `uv pip install torch==2.11.0 --index-url https://download.pytorch.org/whl/cu126`.

**`inferred`:** `/capabilities`' `embedding.device` reports the *setting*, not the *live
device*. An appliance configured for `cuda` on a box where torch cannot see the GPU reports
`"device": "cuda"` and embeds on CPU eight times slower (1.4 below). `local_model_available`
is `True` in both cases.

### 1.2 The indexes must exist before the first byte is uploaded

This is the one ordering mistake that cannot be repaired by re-running anything.

`index_tasks.covering_indexes` reads the file node's `path` and indexes into every BM25
index whose root is that node or one of its ancestors — **at the moment `index:bm25` runs**,
which is during ingestion. An index created afterwards covers nothing already ingested; the
task has already recorded `skipped` with its `candidate_roots`, and no later `POST /indexes`
reopens it.

So `scaffold.py` writes all eight indexes and all fourteen index-root bindings before
`upload.py` sends anything. That is not a stylistic choice, and the module docstring says so.

### 1.3 `root_document_id` is a query parameter

`POST /indexes/{index_name}/roots` declares `root_document_id` keyword-only and scalar, so
FastAPI binds it to the query string. Sent as a JSON body it is a 422 that reads like a
schema problem. `jmfts-client/jmfts_client/_verbs.py:981` is the authority — it puts it in
`query`, and reading the generated client is faster than guessing.

### 1.4 `JMFTS_LLM_BASE_URL` must not contain `/v1`

`llm_client.py:119` builds `f"{base_url.rstrip('/')}/v1"`. Its docstring at line 103 says
"the server root, with no `/v1`". **Neither `.env.example` nor `config.py` says this**, and
every OpenAI-compatible server advertises itself as `.../v1`, so the natural thing to paste
is the wrong thing. Cost here: 35 `summarize:llm` tasks failed with
<!-- FIXED; read 2026-09-15. Both now say it, and both cite this section by number: `.env.example`'s
     JMFTS_LLM_BASE_URL block ("THE SERVER ROOT, WITH NO `/v1`… Measured
     (docs/STRESS_CORPUS.md 1.4): 35 summarize:llm tasks failed this way") and
     `jmfts_core/config.py` at `llm_base_url`. The finding is left as written because it is
     the evidence the fix was needed; the sentence is no longer true of the tree. -->


```
HTTP 404 ... at 'http://localhost:8080/v1/v1': File Not Found
```

`GET /health/llm` **did** report it, and correctly:

```json
{"reachable": true, "openai_compatible": false,
 "detail": "{\"status\":\"ok\"} | /v1/models → 404 (not OpenAI-compatible)"}
```

— inside an **HTTP 200**. A monitor that checks status codes sees a healthy appliance. That
is a defensible design (the endpoint answered; the *LLM* is what is unwell) but it is worth
knowing before wiring an alert to it.

### 1.5 Workers make eight HuggingFace calls at startup

Each worker process revalidates the tokenizer against `huggingface.co` even though it is in
`~/.cache/huggingface`. Eight calls, once per process, not per task — a startup cost, not a
throughput one, but it makes a worker's start depend on the internet for a file it already
has. `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1` removes it.

### 1.6 Nothing enforces `--worker-id` uniqueness

A `fleet.sh stop` that did not take left two processes each answering to `runner-1`.
`TaskQueueRepository.requeue_stale_claims(worker_id)` keys on that string, so same-id
workers requeue each other's live work. Observed, not theorised — six workers, twelve
processes, six distinct ids.

---

## Part 2 — What the ingestion produced, and what it cost

`ran`.

### 2.1 Upload

877 files, 151.4 MB, **10.5 s** — 35–41 MB/s on the PDF collections. Uploads only create the
file node, store the blob and enqueue `probe`; nothing is parsed.

**Five rejections, all correct**: `{"detail": "Uploaded file is empty"}` on five 0-byte
markdown files in the obsidian vault.

**872 uploads became 852 documents.** `POST /ingest/file` deduplicates by sha256 across
everything the caller may read, so identical bytes collapse to one node under whichever
parent arrived first, and the second parent gets a `contains` link. Eight of those crossings
were between collections — including `2505.19806v1.pdf`, which is in both paper folders, and
five voice transcripts present in the obsidian vault as `.md` and in kde_connect as `.txt`.

**`inferred`, and it matters for the per-collection numbers below:** `covering_indexes`
reads `path`, not links. `2505.19806v1.pdf` has `path = [1]`, so it joined `inforetrieval`
and `universal` and **not** `philosophyofmind`, even though root 5 `contains` it. A document
can be reachable from a collection by a tree walk and absent from that collection's BM25
index.

### 2.2 Expansion, per collection

Files in, nodes out. This is the answer to "how many children does different unstructured
ingestion produce", and the spread is 30×.

| collection | files | sections | chunks | sheets | records | nodes | nodes/file |
|---|---:|---:|---:|---:|---:|---:|---:|
| obsidian | 285 | 5310 | 7023 | 0 | 0 | 12685 | 44.5 |
| logseq | 244 | 44 | 1510 | 0 | 0 | 1859 | **7.6** |
| KDE Connect transcripts | 154 | 4060 | 4507 | 0 | 0 | 8755 | 56.9 |
| philosophy paper | 81 | 1774 | 3056 | 0 | 0 | 4952 | 61.1 |
| philosophy of mind papers | 41 | 666 | 6476 | 0 | 0 | 7501 | **183.0** |
| inforetrieval papers | 26 | 465 | 2687 | 0 | 0 | 3255 | 125.2 |
| JMFTS XLSX corpus | 21 | 0 | 0 | 33 | 4601 | 4811 | **229.1** |

Markdown notes are cheap (logseq 7.6 — short journal files with little structure). PDFs are
expensive (183 nodes per philosophy-of-mind paper). A spreadsheet is the most expensive
input per file and produces a completely different shape: no chunks at all, one `record` per
row.

### 2.3 Vectors and text, per collection

| collection | nodes | with vector | token rows | text |
|---|---:|---:|---:|---:|
| obsidian | 12685 | 12607 | 301,920 | 6845 kB |
| KDE Connect transcripts | 8755 | 8729 | 170,451 | 4175 kB |
| philosophy of mind papers | 7501 | 7489 | 340,250 | 7998 kB |
| philosophy paper | 4952 | 4922 | 130,002 | 3355 kB |
| JMFTS XLSX corpus | 4811 | 4739 | 155,427 | 2095 kB |
| inforetrieval papers | 3255 | 3251 | 165,329 | 3452 kB |
| logseq | 1859 | 1857 | 110,312 | 2326 kB |

### 2.4 What each rung cost

Whole run, all collections, six workers.

| task | ok | failed | avg | total |
|---|---:|---:|---:|---:|
| `summarize` | 13,770 | 0 | 0.345 s | **4744 s** |
| `index:bm25` | 829 | 0 | 5.389 s | **4468 s** |
| `embed` | 29,827 | 66 | 0.116 s | 3476 s |
| `summarize:llm` | 150 | 10 | 18.655 s | 2985 s |
| `summarize:tree` | 13,760 | 0 | 0.018 s | 251 s |
| `extract:text` | 829 | 1 | 0.204 s | 170 s |
| `structure:declared` | 549 | 0 | 0.284 s | 156 s |
| `citation` | 58 | 0 | 2.622 s | 152 s |
| `structure:semantic` | 425 | 0 | 0.245 s | 104 s |
| `structure:inferred` | 280 | 0 | 0.111 s | 31 s |
| `extract:sheet` | 32 | 1 | 0.799 s | 26 s |
| `profile:sheet` | 33 | 0 | 0.756 s | 25 s |
| `probe` | 852 | 0 | 0.015 s | 13 s |
| `structure:sheets` | 21 | 0 | 0.084 s | 2 s |

**829 BM25 tasks cost more than 29,827 embeddings.** Part 4.5 is why.

### 2.5 Storage

| table | total | heap | index |
|---|---:|---:|---:|
| `token_embeddings` | **1672 MB** | 826 MB | 846 MB |
| `search_term_postings` | 459 MB | 195 MB | 264 MB |
| `documents` | 458 MB | 41 MB | 183 MB |
| `document_evidence` | 129 MB | 74 MB | 50 MB |
| `search_term_stats` | 45 MB | 32 MB | 13 MB |
| `task_queue` | 27 MB | 16 MB | 10 MB |
| `document_links` | 12 MB | 5 MB | 7 MB |

Database **2988 MB**; the original bytes are 144 MB of large objects across 852 blobs.
**151 MB in, 2988 MB out — 19.8×.** MaxSim is 56% of it, and half of *that* is the ANN index.

Every figure in this section was read immediately after the first full ingest and a
`VACUUM ANALYZE`, which is the only state in which they mean "what ingesting this corpus
costs". The same database read 4276 MB at the end of the session, after Part 5's delete and
re-ingest of one collection and Part 6's repeated index builds and drops. That difference is
churn from the experiments, not storage the corpus needs, and it is a reminder that a
database size taken at an arbitrary moment overstates the cost by roughly 40% here.

`read`: `idx_token_embed_384` and `idx_token_embed_512` are HNSW indexes on columns nothing
writes (`token_embed_dims=[256]`). They cost nothing measurable — both under 500 kB — but
they are indexes over nothing.

---

## Part 3 — The GPU, and why "don't leave VRAM idle" is the wrong goal here

`ran`. This was the question the run was asked to answer, and the answer is not the one the
question expects.

### 3.1 The two models do not fit on one 24 GB card

| | VRAM |
|---|---:|
| Card | 24,564 MiB |
| llama-server (qwen3-35B IQ4_XS, 131072 ctx, `-np 4`, q8 KV) | 19,994 MiB steady; **16,383 MiB contiguous at load** |
| JMFTS embedder — weights only | 1,066 MiB |
| JMFTS embedder — working set with room to take it | **7,774 MiB** |

Proved in both directions:

* With llama-server up, JMFTS raised `torch.OutOfMemoryError` — *"Tried to allocate 2.31 GiB
  ... 1.77 GiB is free"* — and **601 `summarize` tasks failed**.
* With JMFTS up, **llama-server refused to start**: `cudaMalloc failed: out of memory`
  allocating its 16,383 MiB buffer.

`PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`, which torch's own error text suggests,
did not fix it. The 601 failures happened with it set; it only let the process claim more
when there was more to claim.

The weights coexist comfortably. The **activations on a long document** do not — the
document window is 8192 tokens, and attention at that length is where the gigabytes go.
Measuring the model's resident size while it is idle (1,066 MiB) predicts none of this.

### 3.2 Sharing costs most of the accelerator

Same work, same fleet, same corpus:

| | GPU utilisation |
|---|---|
| Sharing the card with llama-server | 11–19% |
| GPU to itself | **85–100%** |

### 3.3 CPU is a real fallback for one rung and not for the other

Measured on identical work — the logseq collection ingested twice, once with GPU embedding
through `/runner` and once on CPU with llama-server holding the card:

| task | GPU avg | CPU avg | ratio |
|---|---:|---:|---:|
| `embed` (chunks, ≤512 tokens) | 0.116 s | 0.980 s | 8.4× |
| `summarize` (concatenations, ≤8192 tokens) | 0.345 s | 12.918 s | **37×** |

The ratio tracks sequence length, which is the same fact as 3.1 seen from the other side.
An appliance that must share a card can put `embed` on CPU and barely notice; putting
`summarize` there costs 37×.

### 3.4 What to actually do

**Time-slice the card, and schedule by phase rather than by worker.** The embedding phase
takes the GPU alone and finishes at 85–100% utilisation; the LLM phase gives it back and
JMFTS embeds on CPU, where `summarize:llm` only ever embeds a short summary and the LLM is
the bottleneck anyway. Both phases were run that way here and both completed.

**Do not add a second GPU embedding worker.** Each worker process loads its own model, and
the working set is 7.7 GB, not 1 GB. Two do not fit alongside anything.

**A CPU worker fleet is not free either.** The stress host has 6 cores; six worker processes each
running torch on CPU oversubscribe badly. One process holding the model and serving
`/runner` to N thin workers is the shape that worked, and it is what `JMFTS_RUNNER_URL`
is for.

---

## Part 4 — Defects

Each has a reproduction. Numbering is local to this document.

**4.1, 4.2 and 4.6 are fixed in `472c7e9`**, with their entry-condition tests in
`tests/test_stress_defects.py`. The sections below are left as written, because a defect
report that is edited into agreement with the patch stops being evidence that the patch was
needed. The rest are unchanged in the tree; Part 7 says why.

### 4.1 A NUL byte in a PDF text layer aborts `extract:text`

A PDF of a published philosophy book, from the author's own library (the filename is
redacted; the chapter title in the error below is not, because it is the evidence):

```
DataError: (psycopg2.errors.UntranslatableCharacter) unsupported Unicode escape sequence
DETAIL:  \u0000 cannot be converted to text.
CONTEXT: JSON data, line 1: ...ntroduction: Taking Consciousness Seriously\u0000...
[SQL: INSERT INTO document_evidence (document_id, name, value, state) VALUES ...::JSONB ...]
```

The extracted outline is written to `document_evidence.value`, which is `JSONB`, and
PostgreSQL cannot store `\u0000` in it. NULs are common in PDF text layers. The document is
abandoned with no text, no chunks and no vectors.

### 4.2 `sqlalchemy.exc.DataError` is classified retryable, against the module's own policy

`task_errors.py:112` states the rule — *"Data and programming errors... this includes our own
bugs on purpose, because retrying a bug three times only delays noticing it"* — but the arm
at line 121 catches only `ValueError, TypeError, KeyError, AttributeError, ImportError`.
`sqlalchemy.exc.DataError` falls through to the `RETRYABLE` default at line 130. 4.1 above
therefore burned three 22-second attempts on bytes that cannot change.

### 4.3 `ErrorType.DEPENDENCY` is never assigned, and dependents leak

`ErrorType.DEPENDENCY` is defined and documented — *"A prerequisite is missing or a parent
task failed. No retry"* — and the only reference to it anywhere outside its definition is
`tests/test_task_queue.py:347`. Nothing in production ever sets it.

The claim gate (`repositories/task_queue.py:314`) requires **every** dependency to be
`completed`. So when 4.1's `extract:text` failed terminally, its three dependents —
`structure:declared`, `citation`, `index:bm25` — became permanently `pending` and
unclaimable.

The **node** is handled correctly: `fail()` sets `settled='failed'` on an exhausted retry
cap, so the settle walk is not blocked. The **tasks** are not. `count(*) WHERE
status='pending'` never reaches zero, which makes "is the queue drained?" unanswerable by
the obvious query.

### 4.4 Spreadsheets never reach BM25

The most consequential finding in this run. `ran`: **zero `index:bm25` tasks exist anywhere
in the xlsx subtree**, and the `xlsx` index holds 0 documents, 0 postings and 0 terms —
while 4,601 `record` nodes carry real text averaging 457 characters:

```
SORT-AS: AC-25-00. Control Identifier: AC-25. Control (or Control Enhancement) Name: Reference Monitor.
Month: 1975-01-01. Time: 277. Population: 214931.
```

`ingest_tasks.py:1172`:

```python
TaskRow(
    TASK_INDEX_BM25,
    write_mode=WRITE_SELF,
    after_any=(TASK_STRUCTURE_DECLARED, TASK_STRUCTURE_INFERRED, TASK_STRUCTURE_CONVERSATION),
    requires=(HAS_TEXT_LAYER,),
)
```

Two independent exclusions:

1. `TASK_STRUCTURE_SHEETS` is not in `after_any`. The comment beside the row says *"exactly
   one of the two rungs is ever eligible for a document"* — which is false for a workbook,
   whose rung is `structure:sheets`.
2. `requires=(HAS_TEXT_LAYER,)`. `ran`: an xlsx file node's evidence is `file`, `matched`,
   `options`, `structure`, `effective_content`, `attempts` — there is no text-layer atom,
   because a text layer is a PDF property.

Consequence: spreadsheet content is retrievable by vector and MaxSim only. It is absent from
its own collection's index **and from `universal`**, so a full-text query over "everything"
silently excludes every spreadsheet.

### 4.4b What a spreadsheet actually produces, and what described it

Investigated 2026-09-07, after 4.4. The question was what chunks a workbook makes and
whether a spreadsheet can be hierarchically summarised. Both answers were already in the
tree; one of them was unreachable.

`ran`, the 21 workbooks under root 2:

| usetype | produced_by | n | with vector | with `content` |
|---|---|---:|---:|---:|
| `file` | — | 21 | 18 | 0 |
| `sheet` | `structure:sheets` | 33 | 30 | 0 |
| `profile` | `profile:sheet` | 33 | 33 | 33 |
| `record` | `extract:sheet` | 4601 | 4535 | 4601 |
| `segment` | `structure:semantic` | 122 | 122 | 0 |

4810 nodes from 21 files, plus 170 roll-up nodes in the derived tree — 122 over segments, 30
over sheets, 18 over files. A `record` is one row rendered as `Key: value.` pairs.

**18 of 33 sheets produced zero records**, each recording *"INGEST_SPEC.md 8.3's `header_row`
is false for this sheet, so its first row does not supply a key for every column and there is
no record to write. The shapes 8.4 gives such a sheet — `matrix` and `unstructured` — read
thresholds 8.8 leaves unset and have no handler."* One more failed on `TooManyRows`. Fourteen
sheets wrote all 4,601 records.

**Hierarchical summarisation already runs, and 5 of 170 roll-ups called the LLM.** The rest
are `concatenated`, because `summarize` defers to `summarize:llm` only when the concatenated
children overflow the 8192-token window (`rollup_tasks.py:668`) and a sheet's rows usually
fit. The five that did are good — one names the columns, the layer, the rotations and the
coordinate ranges of an 80-row pick-and-place file.

**The description was already there and search could not reach it.** `profile:sheet` writes a
measured description of every sheet, 33 of 33, at 0.756 s each — columns, cardinalities,
which one identifies a row, which hold no value at all. For the 18 header-less sheets it is
the ONLY text the pipeline produces. It carried `usetype='summary'`, which is in both
exclusion lists, so `INGEST_SPEC.md` 8.5's *"it is embedded and retrievable like any other
node"* was false. Fixed in `35acfaa` and migration `021`; on the reference corpus the
migration moved 33 rows and the running API returned them immediately, because nothing in
the code branches on that string.

**Why the exclusion of `summary` was not the thing to change.** `ran`: every
`summarize:tree` node's vector is byte-identical to its source node's — 13,755 of 13,755
pairs at cosine distance 0.000000 — and all 14,101 have zero children, so they are
navigational structure duplicated into the vector index. Admitting them would return every
answer twice. An LLM summary's text, meanwhile, is reachable on the node it summarises
through `effective_content`, which 4.7's projection now returns.

### 4.4c The table edit is not the fix, and a third cause is why

Reproduced 2026-09-07 in `tests/test_workbook_bm25.py` — one sheet, a header row, three data
rows, ingested under a folder that a BM25 index names as a root, drained dry.

4.4 named two causes and both are real. Repairing only those two — `TASK_STRUCTURE_SHEETS`
added to `after_any`, `requires=(HAS_TEXT_LAYER,)` dropped — makes the row fire, and `ran`:

```
index:bm25 added 0 documents while the subtree holds 3 record nodes;
it saw a subtree of 2
```

**A workbook's leaves are not written by its structure rung, and every other format's are.**
`index_tasks.py`'s module docstring states the property the row depends on: *"the rung must
be `completed`, and a completed rung has written every chunk with its `content`"*. That is
true of `structure:declared`, `structure:inferred` and `structure:conversation`, which write
their chunks themselves. `structure:sheets` writes only the sheet nodes; `profile:sheet` and
`extract:sheet` are scoped to those children, one rung further down, and neither had run.
The subtree of 2 is the file node and the one sheet node.

So the repaired row is worse than the broken one, and that is not a close call: today the
absence is visible — `EXPLAIN` reports the row as not applicable and no attempt exists —
while the repair records a `completed` attempt whose `documents_indexed` is 0 and consumes
the one chance the task gets. It was measured and reverted rather than committed.

**What the row cannot express.** `after`/`after_any` order two rows on ONE node
(`ingest_tasks.py`'s validation, rule 2), so a file-node row cannot name `extract:sheet`,
which is sheet-scoped. A second row named `index:bm25` is refused by rule 1, because every
outcome the table reports is keyed by task name. The condition "every leaf below me exists"
is the settling boundary, and the settling boundary is where `INGEST_SPEC.md` 11.5 puts this
task in the first place — *"the index has to see the whole subtree, and the subtree does not
exist until structuring finishes"*. `index_tasks.py` argues the divergence from that wording
on two grounds, and both are answered by a `usetype == 'file'` guard in
`IngestRollupPlanner`: one task per file, and the file node has children so it is never the
leaf the planner returns nothing for. `tests/test_atom_declarations.py`'s
`EXPECTED_DIVERGENCE` entry for `index:bm25` predicts exactly this move and says the spec and
the implementation converge when it happens.

**The corpus can be repaired without the pipeline, and that is a property of the defect.**
It is a defect of ORDER, and an ingested workbook has no order left: its tree is complete, so
walking it now writes exactly the postings a correctly scheduled task would have.
`scripts/reindex_workbooks.py` does that walk, reads membership from `path` per 11.5, creates
no index, and refuses a subtree with an unsettled node. `ran`, against a scratch appliance
holding the fixture above:

```
2 'instruments.xlsx': indexed 4 row(s) of 4 with text into ['workbook-corpus']
quadraphonic -> [(5, 'record', 1.72), (1, None, 0.541), (2, 'file', 0.541), (3, 'sheet', 0.541)]
```

The three 0.541s are the container pass from `778fe6f` scoring a sheet, a file and a folder
on text none of them stores — the first time it has run over a workbook, because until now no
workbook had a posting anywhere to aggregate.

### 4.4d The reference corpus, reindexed

`ran` 2026-09-07 on the reference appliance, `python -m scripts.reindex_workbooks --apply`.
**18 of 21 workbooks, 2,323 nodes, into `xlsx` and `universal` both:**

| index | documents | postings | terms | `avg_doc_length` |
|---|---:|---:|---:|---:|
| `xlsx` before | 0 | 0 | 0 | 0.0 |
| `xlsx` after | **2,323** | **43,330** | **5,146** | 24.7 |
| `universal` before | 27,674 | 2,008,630 | — | not read |
| `universal` after | **29,997** | **2,051,960** | 61,660 | **145.3** |

`universal`'s `avg_doc_length` was not read before the run, so the move is `inferred` rather
than measured: holding the total token count fixed, 29,997 × 145.3 less 2,323 × 24.7 over
27,674 documents puts it at about 155 before. Either way the direction is the corpus
statistic changing because the corpus changed — a spreadsheet row is 24.7 tokens against a
prose chunk's ~155, so 2,323 of them pull the mean down and every BM25 score in `universal`
shifts with it. That is the length normalisation working, not a side effect to correct.

**What it returns**, through the live API, `POST /search/bm25?index_name=universal`:

```
designator footprint mid X mid Y
  32.149  record   stored     'Designator: R59. Device: 0603WAF4700T5E. Footprint: R0603. M'
  31.937  record   stored     'Designator: R63. Device: 0603WAF2200T5E. Footprint: R0603. M'

population month time
  18.939  segment  effective  'Month: 1953-01-01. Time: 13. Population: 158973.\n\nMonth: 1953-02…'
```

The second is the whole 4.7 stack answering at once, and it could not have run before today:
the `segment` carries `content: null`, the container pass scored it by aggregating the
postings of the 12 records below it, and `content_source: "effective"` is the projection
rendering what it stands for. A prose query over the same index still returns the same
`chunk` nodes it did before, so the workbooks joined `universal` without displacing it.

**Three workbooks were refused, and the reason is two other defects.** `ran`:

| id | file | unsettled | blocked by |
|---|---|---:|---|
| 50 | `data.xlsx` | 16 of 1122 | `embed` × permanent (4.10) |
| 53 | `webfetch-…-mjjnad.xlsx` | 54 of 1192 | `embed` × permanent (4.10) |
| 55 | a 19 MB personal workbook (name and sheet redacted) | 2 of 3 | `extract:sheet` × permanent, `TooManyRows` |

66 `embed` tasks failed `permanent` across 50 and 53 — the same 66 records 4.10 counts, over
the **512-token token/maxsim window**, not the 8192-token document window, which is the
distinction 4.10 draws and an earlier draft of this paragraph lost. Their sheet nodes
therefore never settled, their file nodes never settled, and the script refuses a subtree
with an unsettled node. So roughly 2,240 record nodes whose text is final are withheld from
the index by about 70 nodes whose vectors are missing. Loosening the guard is not the fix:
`settled` is the appliance's own statement that a subtree is complete, and a script that
indexes past it is asserting something the appliance denies.

**4.10 is now resolved and §4.11 is the fix**, so 50 and 53 index without the script being
touched. 55 is still refused, by `TooManyRows` on a configured bound.

### 4.5 `index_document` serialises the whole fleet on one row

`repositories/search.py:1309` takes `with_for_update()` on the `SearchIndex` row and holds
it to commit. Its own comment predicted the consequence:

> *"It holds until commit, so all indexing of one index is serial — fine at single-writer
> throughput; revisit if bulk parallel indexing of one index lands."*

It has landed. Every document joins `universal`, so six workers contend for one row.
Caught live in `pg_locks`: **five of six workers blocked**, one inserting, a tuple-lock
chain on `search_indexes`. `pg_stat_statements` for that `SELECT ... FOR UPDATE`: 31,008
calls, **1908 s total, 61.5 ms mean** — the mean *is* the lock wait.

The task's own cost degrades accordingly, by decile of completion order: 1.94 s (single
worker) → 8.18 s → 14.86 s, with maxima of 71.7 s and 48.5 s.

### 4.6 A per-document DELETE scans the whole term table and deletes nothing

`repositories/search.py:1424` runs, once per document per covering index:

```sql
DELETE FROM search_term_stats WHERE index_id = :index_id AND doc_freq <= 0
```

`EXPLAIN (ANALYZE, BUFFERS)`:

```
Delete on search_term_stats (actual time=6.884..6.885 rows=0 loops=1)
  Buffers: shared hit=20107
  ->  Index Scan using search_term_stats_pkey  (actual time=6.883..6.883 rows=0 loops=1)
        Index Cond: (index_id = 6)
        Filter: (doc_freq <= 0)
        Rows Removed by Filter: 16295
```

`search_term_stats` is keyed `(index_id, term)` with nothing on `doc_freq`, so this reads
every term row for the index. On a **first** ingest `old_terms` is empty, so `old_terms -
new_terms` is empty and no row can ever have reached zero — the statement cannot delete
anything, by construction. 30,812 executions, **249 s**, and the per-call cost grows with
the index.

Alongside it, `search_term_stats` is upserted **one term at a time**: 2,311,676 single-row
`INSERT ... ON CONFLICT` statements.

### 4.7 22.5% of the corpus is retrievable and blank

**Fixed 2026-09-07 in `188e973`, and the number was an undercount.** The section below
counted `section`, `segment` and `sheet`. The real figure across every settled node is
**26,870 of 57,492 — 46.7%** — because 13,944 `summarize:tree` nodes are in the same state
and were excluded from search rather than rendered. `ran`, ten queries at limit 10 before
and after: 27 of 100 vector results were blank, and 0 are now, at a median 28.2 ms against
25-50 ms before.

The fix is a projection, not a stored copy: `jmfts_core/effective_content.py` computes what
a container stands for, for a whole page in one query, and `DocumentResponse.content_source`
says `'effective'` so a caller can tell computed text from a column. `store_effective_content`'s
reason for not storing it — *"storing it at every level would duplicate the whole document
once per level"* — is untouched.

**Why it mattered more than "a blank line in a result".** These are not filler nodes. `ran`,
10 queries against the top 12 container nodes each, scored against every descendant in the
subtree rather than a page sample: **the container outscored every descendant it contains 53
times out of 59**, mean margin +0.0503. The nodes this appliance ranks best were the ones it
could not render. Three sites read the column directly — `_build_response`, `auto_search`,
and the LLM synthesis context, where a container reached the model as `""` with no reader to
notice.

The original 4.7 text follows unedited.

`search_exclude_usetypes` is `["entity", "entities", "summary", "derived"]`. It does not
contain `section`, `segment` or `sheet`, and those nodes carry their text as
`effective_content` — which is *computed on demand by `effective_text()` and never stored*
— so `documents.content` is `NULL` while `documents.embed` is populated.

| usetype | settled | with vector | with content | retrievable and empty |
|---|---:|---:|---:|---:|
| `section` | 12,319 | 12,179 | 0 | **12,179** |
| `segment` | 704 | 704 | 0 | **704** |
| `sheet` | 30 | 30 | 0 | **30** |
| `summary` | 13,793 | 13,793 | 183 | 13,610 *(excluded from search)* |

12,913 nodes — **22.5% of the settled corpus** — can be returned by vector, MaxSim and
hybrid search with nothing to display. Confirmed against the live API:

```json
{"id": 57499, "title": null, "content": null, "usetype": "segment",
 "produced_by": "structure:semantic"}
```

Its five children hold 3,416 characters. Seen in real results: a `segment` scoring 0.598 on
"what is it like to be a bat qualia", rendering as an empty line.

### 4.8 The LLM client erases HTTP status — known, now costed

`llm_client.py:35–50` already records this: τ 0.9.3 reports every transport failure as a
bare `Exception` carrying a message, so `classify_exception` cannot branch on status and
*"a 4xx that was PERMANENT now burns the retry budget before failing terminally"*. The fix
is placed in τ, deliberately.

`ran`, so the cost is no longer hypothetical: 7 `summarize:llm` tasks sent 133,758 tokens
into a 131,072-token window and got HTTP 400. llama-server rejects an over-context request
**without prefilling**, in 1.3 s, so three wasted retries across seven tasks cost **9.4 s**.
The waste is negligible. What is delayed is the terminal state that `summarize_span`'s
docstring relies on — *"the worker classifies that refusal and records it, and the answer is
to segment further"* — which is a correctness dependency on a classification that does not
happen.

### 4.9 The queue has no notion of which resource a task needs

`ran`. Twice during this run the fleet stalled with work available, and both times the fix
was a hand-written `UPDATE` against `task_queue.priority`. Recording it here because it is
the workaround the run actually depended on, and it is not documented anywhere.

`repositories/task_queue.py` claims by `ORDER BY t.priority DESC, t.created_at ASC, t.id
ASC`, and every task is created at priority 0. A worker therefore takes the oldest eligible
task, whatever it needs.

**First stall.** `nvidia-smi` reported 0% utilisation with 13,758 `embed` tasks pending and
`blocked: 0` — every one of them claimable. Six workers were all on older rungs, and the
GPU had nothing to do because nothing was asking it for a vector:

```sql
UPDATE task_queue SET priority = 10 WHERE task_type = 'embed' AND status = 'pending';
```

**Second stall.** Six workers then delivered roughly one worker's throughput. `pg_locks`
showed five of six waiting on a tuple lock on `search_indexes` — 4.5 — so the fleet was
serialised on the one rung that cannot run in parallel:

```sql
UPDATE task_queue SET priority = -10 WHERE task_type = 'index:bm25' AND status = 'pending';
UPDATE task_queue SET priority =   5 WHERE task_type <> 'index:bm25' AND status = 'pending';
```

Both statements say the same thing in the only vocabulary the queue has: *this rung wants
the GPU*, *this rung wants a lock no two workers can hold*. Priority is a single integer,
so it can rank tasks but cannot express that six workers on `index:bm25` is worse than one
while six on `embed` is better than one. Whether that belongs in the queue, in `TaskRow`
alongside `write_mode`, or in the worker's claim filter is a design question this run does
not answer.

### 4.10 Not defects

Recorded so they are not re-investigated:

* **Five 0-byte uploads rejected** with `"Uploaded file is empty"`. Correct.
* **`TooManyRows`** on the 19 MB workbook of row 55: *"sheet '…' holds more than
  10,000 rows with a value in them; row 10001 is past the bound this read was given"*,
  classified `permanent`. A configured bound naming itself — exactly right.
* **66 `TextTooLongError`** on wide spreadsheet `record` nodes: *"Text is 1150 tokens (5998
  chars), over the 512-token token/maxsim window. Chunk it first — embedding it here would
  silently discard 638 tokens."* The refusal is honest. **But** the document window is 8192
  and would have accepted these; the 512-token MaxSim window fails the whole task, so the
  node ends with *no* vector rather than a document vector and no token vectors.
  **MOVED OUT OF THIS LIST, 2026-09-08.** "Chunk it first" names a caller who did not exist:
  nothing between `extract:sheet` and `embed` chunks a record. §4.11 is the fix and it makes
  that caller `extract:sheet` itself.
* **`runner.info` 401** with the API token. Correct — the two credentials are disjoint.
* **A root marked `failed` makes `GET /documents/{id}/subtree` return 409.** The message
  names the workaround (`include_in_flight`). Deliberate.

**One thing I contaminated and will not claim.** Two requeue statements in this run wrote
`documents.settled` directly, to put OOM-failed nodes back in flight. Collection roots ended
in inconsistent states (root 1 `failed` with one failed descendant, root 2 `settled` with 67,
root 7 `in_flight` with none). That is my edit, not an observation about JMFTS. A clean
re-run would be needed to say anything about root settling.

### 4.11 A row too long to embed is a container, not a failure

**What the 66 actually are.** `ran`, against the reference appliance:

| | value |
|---|---:|
| `record` nodes in the corpus | 4,601 |
| with no vector | **66** (1.4%) |
| their `content` length | 1,618 – 5,998 chars, mean 3,269 |
| every other record's mean | 457 chars |
| distinct sheets they come from | **2** |

Both sheets are the same shape, and it is not the one 4.10's phrase "wide spreadsheet
`record` nodes" implies. Sheet 4043 is the NIST `SP 800-53 Revision 5` control catalogue as
a workbook — 52 failures of 1,189 rows — and sheet 4036 is a data.gov catalogue export, 14
of 1,119. The largest failing record, document 42539, holds five cells:

```
Discussion [3637] | Control Text [1980] | Related Controls [232] |
Control Identifier [4] | Control (or Control Enhancement) Name [33]
```

**One prose column and four identifiers.** A row of `SP 800-53` is a whole security
control. The spreadsheet is a container for documents and the row is the document, so 8.4's
"one node per row" put a document in a node sized for a value.

**`chunk_max_chars` is 1,800 and these rows are 1,618 to 5,998.** `extract:sheet` is the one
text producer in the tree that writes `content` without passing it through the chunker. It
even measures the problem as it goes — `sheet_tasks.py` calls
`service.fits_token_window(record.content)` per record and counts the misses into
`records_over_token_window` — and then enqueues the `embed` task anyway.

**Summarization is not the instrument, and four things say so.** The first is mechanical:
`IngestRollupPlanner.__call__` returns `()` for a node with no children, so no rung a leaf
can reach summarizes anything. Then: BM25 indexes `content`, and a record's value here is
the verbatim control text, so a summary drops exactly the terms a query for a clause
matches. `CLAUDE.md` states that everything except summarization, RAPTOR, fact extraction
and synthesis works with `JMFTS_LLM_*` blank, and a summarize-to-fit rung would put an
optional service on the base leaf path for 1.4% of records. And §5.2 measured the embed
cache at 1,754 hits of 1,754, which rests on `content_hash` being deterministic over
unchanged bytes — an LLM-written `content` is not. The one that decides it: `summarize`
writes a DERIVED node and leaves `content` alone, so even if a leaf could reach it the
record would still have no vector and its `embed` would still fail.

**The rule the rest of the tree already follows.** A `section` holds no prose and its chunks
do; `store_effective_content` gives it a document vector over the concatenation and no token
vectors, "because [token embeddings] would put this text into the MaxSim index under a node
whose `content` it is not". `read jmfts_core/rollup_tasks.py`. A record is that shape one
level down and a cell is that shape below it:

| node | fits the 512-token window | does not fit |
|---|---|---|
| `record` | leaf, `content`, vector + maxsim | container over `cell` |
| `cell` | leaf, `content`, vector + maxsim | container over `chunk` |
| `chunk` | always, by `chunk_to_fit`'s predicate | — |

**Splitting by COLUMN and not by character**, because the columns are named. 8.4 already
keeps the typed values in the `record` evidence, so each piece can carry the label of the
field it came from; a blind chunk of the row's prose puts a boundary inside `Discussion:`
and produces a node that names no column at all. **The second level is load-bearing**:
`ran`, on the re-ingested corpus, the 66 rows became 344 cells and **20 of those cells are
themselves over the window**, in 20 different rows. A split that stopped at the column
boundary would leave 20 of 66 rows still unable to embed — not a corner case, and not the
62% an earlier draft of this paragraph claimed. That number came from counting cells over
1,800 CHARACTERS, which is `chunk_max_chars` and not the predicate: the predicate is 512
tokens measured by the tokenizer, and the two disagree by a factor of two here. The mistake
is exactly what `KNOWN-DEFECTS` D7 is about, made in a document rather than in code.

**A measurement corrected the first implementation.** `EmbeddingService.chunk_to_fit` takes
the library's bare defaults — `ChunkStrategy.sentence`, unpacked — and that is the strategy
`STRUCTURE_CHUNK_PARAMS` was calibrated against and beat. `ran`, on the reference corpus's
42 over-window cells before the correction: **311 chunks averaging 203 characters, 8 of them
under eight words**, against the 0.1% under-eight-words that 120-word packing measured over
25 papers. `jmfts_core.sheet_tasks.chunk_prose` reads `STRUCTURE_CHUNK_PARAMS` instead. A
cell's prose is prose, and that constant's own comment already records `structure_tasks`
reading it directly for the same reason.

---

## Part 5 — Re-ingesting without paying for embeddings twice

`ran`.

### 5.1 The key already exists

`documents.content_hash` is populated on **every** chunk — 25,259 of 25,259 — and on every
file node. A re-ingest of unchanged bytes produces the same chunk text under new document
ids, so the vector is recoverable by hash even though nothing about node identity survives.

`scripts` in `~/stress/embed_cache.py` build two tables keyed
`(content_hash, model, dims)`:

```
saved 29468 document vectors, 1322400 token rows in 9.4 s
```

The entire embedding output of a multi-hour ingest, captured in **9.4 seconds**.

### 5.2 The hit rate is 100%, measured rather than assumed

The logseq collection (244 files, 1,858 nodes) was deleted and ingested again from the same
bytes:

| step | cost |
|---|---:|
| `DELETE` of 1,858 nodes (cascading across 12 tables) | **0.92 s** |
| Re-upload of 245 files | 2.21 s |
| Full re-ingest, six workers | 1,168 s wall / **6,689 task-seconds** |
| Nodes with a hash produced by the re-ingest | 1,754 |
| **Of those, already in the cache** | **1,754 — 100%** |

The chunker is deterministic over unchanged bytes. Every embedding the re-ingest computed
had already been computed.

### 5.3 What that is worth

Of the 6,689 task-seconds, embedding accounts for:

| task | n | avg | total |
|---|---:|---:|---:|
| `summarize` (embeds concatenations) | 342 | 12.918 s | 4418 s |
| `embed` | 1,510 | 0.980 s | 1480 s |
| everything else | — | — | 791 s |

**5,898 s of 6,689 — 88% — is embedding**, and all of it was recoverable. (This re-ingest
ran with CPU embedding, per 3.3; on GPU the same work is 8–37× cheaper and the cache is
correspondingly less valuable in absolute seconds, but the *fraction* is unchanged.)

### 5.4 What the script deliberately does not do

`embed_cache.py restore` fills in vectors for nodes that have none. It does **not** touch
the task queue. Marking an `embed` task `completed` because a vector arrived by another
route is a lie about what ran, and `document_evidence` would disagree with it.

**Proposal.** The correct form is a lookup at the top of the `embed` handler, keyed
`(content_hash, model, dims)`, which records in the attempt detail that the vector was
served from cache rather than computed. Cost: one table, one index, one lookup per embed
task, and a decision about invalidation when the model changes — which the key already
encodes. This is a design note, not something this run built.

---

## Part 6 — HNSW against IVFFlat on real vectors

`ran`. Recall@10 against a brute-force scan (`enable_indexscan=off`), queries drawn from the
corpus itself, every arm measured against the same query set and the same gold.

**This is where `ANN_INDEX_HEALTH.md` needs correcting.** That document measured synthetic
clustered Gaussian vectors, and concluded IVFFlat "lost nothing" at every probes setting.
Real embeddings are not that shape.

### 6.1 `documents.embed` — 57k rows, `vector(768)`

| arm | build | index size | probe | recall@10 | ms/query | degraded |
|---|---:|---:|---|---:|---:|---:|
| HNSW m16 ef64 | 4.45 s | 143 MB | ef 40 | **0.9767** | 0.60 | 10/60 |
| HNSW m16 ef64 | | | ef 40, strict_order | 0.9767 | 0.56 | 10/60 |
| HNSW m16 ef64 | | | ef 200 | **0.9833** | 1.56 | 10/60 |
| IVFFlat lists=100 | 1.38 s | 226 MB | probes 1 | **0.7467** | 0.69 | 35/60 |
| IVFFlat lists=100 | | | probes 10 | 0.9600 | 4.49 | 13/60 |
| IVFFlat lists=100 | | | probes 32 | 0.9633 | 12.54 | 16/60 |
| IVFFlat lists=1000 | 20.83 s | 231 MB | probes 1 | **0.7333** | 0.52 | 40/60 |
| IVFFlat lists=1000 | | | probes 10 | 0.9283 | 0.85 | 19/60 |
| IVFFlat lists=1000 | | | probes 32 | 0.9567 | 1.78 | 14/60 |

**HNSW wins on every axis at once** — higher recall, lower latency, 40% smaller, and a build
faster than `lists=1000`. The shipped choice for this column is already HNSW.

`strict_order` changes nothing here (0.9767 either way), which is consistent with
`ANN_INDEX_HEALTH.md`: it is a mitigation for **dead index entries**, and this table has
none worth speaking of. It costs nothing to leave on.

Note that even HNSW does not reach 1.0. 10 of 60 queries lose at least one of their ten true
neighbours on a clean index. That is ordinary ANN approximation and is the floor any recall
claim about this appliance has to be read against.

### 6.2 `token_embeddings.embed_256` — 1.37M rows, `halfvec(256)`

This is the MaxSim path, and the shipped configuration is the first row.

| arm | build | index size | probe | recall@10 | ms/query | degraded |
|---|---:|---:|---|---:|---:|---:|
| **IVFFlat lists=1024 (shipped)** | 20.6 s | 720 MB | **probes 1 (pgvector default)** | **0.7533** | 0.65 | **15/30** |
| IVFFlat lists=1024 | | | probes 10 | 0.9433 | 4.86 | 8/30 |
| IVFFlat lists=1024 | | | probes 50 | 0.9767 | 21.05 | 5/30 |
| **HNSW m16 ef64** | 55.9 s | 1050 MB | **ef 40** | **0.9667** | **0.65** | **1/30** |
| HNSW m16 ef64 | | | ef 40, strict_order | 0.9667 | 0.59 | 1/30 |

**The shipped MaxSim index returns 75% of the true nearest neighbours, and half of all
queries are degraded.** HNSW reaches 0.9667 **at identical query latency** — 0.65 ms in both
— with 1 degraded query instead of 15.

The price is 2.7× build time (55.9 s vs 20.6 s at 1.37M rows) and 46% more disk (1050 MB vs
720 MB, on a table already 1672 MB).

**This settles what `4dadb62` opened.** That commit recorded "the IVFFlat path loses half its
neighbours before the gate is reached" and left open whether it was a property of IVFFlat or
of `probes`. It is `probes` — and on real data raising `probes` to fix it costs 7–32× the
query latency, whereas changing the index type costs nothing at query time.

### 6.3 VACUUM, and why the first reading was misleading

On the live corpus, with autovacuum at its default, VACUUM is trivial:

| table | rows live / dead | VACUUM |
|---|---|---:|
| `documents` (HNSW) | 57,913 / 6,858 | 213 ms |
| `token_embeddings` (IVFFlat) | 1,370,494 / 113,313 | 227 ms |
| `search_term_postings` | 4,017,260 / 343,468 | 222 ms |

That looks like it contradicts `ANN_INDEX_HEALTH.md` 1.8, which measured 87.4 s for an HNSW
vacuum. It does not. Repeating the measurement under 1.8's conditions — a scratch table
holding the **real** document vectors, `autovacuum_enabled = false`, a bulk delete:

| index | rows | build | deleted | VACUUM |
|---|---|---:|---|---:|
| HNSW m16 ef64 | 57,691 | 14.44 s | 10% (5,769) | **15.85 s** |
| HNSW m16 ef64 | | | +40% (23,076) | **24.53 s** |
| IVFFlat lists=100 | 57,691 | 1.82 s | 10% (5,769) | **0.084 s** |

HNSW vacuum costs **189×** IVFFlat's on real vectors, confirming 1.8. The 213 ms reading is
autovacuum having already amortised the work incrementally, which is the operationally
useful fact: **the cost is invisible under default autovacuum and arrives in full after a
bulk delete** — which is exactly what a re-ingest (Part 5.2) is.

---

## Part 7 — Open questions

Decisions this run cannot make.

1. **Does `index:bm25` move to the settling boundary?** (4.4, and 4.4c is why the question
   changed.) It was asked as "does the row get `structure:sheets` and a guard a spreadsheet
   can satisfy" — that repair was built, measured and reverted: it indexes 0 documents,
   because a workbook's leaves come from `extract:sheet` two rungs below the rung the row
   would wait on. No arrangement of `after_any` expresses "every leaf below me exists"; the
   settling boundary is that condition, and `INGEST_SPEC.md` 11.5 already specifies the task
   there. What the answer changes: whether "a BM25 index per folder" is a claim the appliance
   can make for any format whose leaves are written below its structure rung. If nothing is
   decided, `scripts/reindex_workbooks.py` keeps an already-ingested corpus searchable and
   every new workbook arrives unindexed. The reference corpus has been reindexed that way
   (4.4d), so the appliance answers today and the pipeline still does not.

2. ~~**Should the token index become HNSW?**~~ **ANSWERED 2026-09-11: yes, and it shipped
   as migration `022_token_embed_256_hnsw.sql`.** (6.2.) It buys recall 0.75 → 0.97 at
   unchanged query latency, for 2.7× build and +330 MB. What the answer changed: MaxSim
   results can now be described as approximately complete, which is the claim
   `tests/test_maxsim_recall.py` asserts at 0.90 and which read 0.4542 before.

   **The reservation in the original wording was the right one and is worth keeping
   visible: one corpus's evidence was not enough, and it was not what decided this.**
   6.2 compared recall and cost; it could not say whether the recall gap was a property of
   the index type or of how the index was built. `ANN_INDEX_HEALTH.md` 5.4 found that the
   shipped IVFFlat was built against an empty table and never refitted, 5.7 found that an
   HNSW graph is built by insertion and therefore has no build order at all, and 5.8 made
   the decision on that property rather than on 6.2's numbers — then checked it on the
   appliance's own tables, where rebuilding the HNSW index on the loaded rows moved recall
   by one document out of 240. The cost side of 6.2 is what the migration's comment
   budgets against, unchanged.

3. ~~**Should a node too long for the MaxSim window still get its document vector?**~~
   **ANSWERED, and the answer is that the question was the wrong one.** It offered two ways
   to make one node retrievable — a document vector with no token vectors, or neither — and
   both accept that a row holding a whole NIST control is one node. It is not. §4.11 splits
   it by column, so every leaf gets BOTH vectors and the row gets the document vector over
   the concatenation, which is what a `section` has done since 11.4. Nothing had to choose
   between the two retrieval methods; the node just had the wrong boundaries.

4. ~~**Do `section`, `segment` and `sheet` join `search_exclude_usetypes`, or does the search
   response project `effective_text`?** (4.7.) 22.5% of the corpus is at stake. Excluding
   them is one line and loses mid-scale retrieval targets; projecting the text is a walk per
   result and changes the cost of every search response.~~
   **ANSWERED 2026-09-07 in `188e973`: the PROJECTION branch won, and the exclusion branch
   was not taken.** `jmfts_core/effective_content.py` computes what a container stands for
   for a whole page in one query, `search_service` runs every page through it, and
   `DocumentResponse.content_source` says `'effective'` where the text was computed rather
   than read from a column. `Settings.search_exclude_usetypes` is unchanged at
   `["entity", "entities", "summary", "derived"]` (`config.py:219`) — `section`, `segment`
   and `sheet` did not join it, and `derived` is there for `summarize:tree`'s roots rather
   than for anything this question asked about. The stake was also larger than the question
   stated: 4.7's correction puts it at 26,870 of 57,492 nodes, 46.7%. The cost the question
   worried about was measured rather than accepted — the worst realistic page, 100 results
   all containers, projects in 4.9 ms against a 25–50 ms search, and a page of leaves costs
   one `IS NULL` check and no query. 4.7 already carries the fix; this is the half of the
   document that still asked it as open.

Three defects in Part 4 — 4.1, 4.2 and 4.6 — are fixed in `472c7e9`, because each is a
patch with one right answer and a test that fails without it. Nothing else in Part 4 has
been changed, and nothing in Part 6 has: the four questions above are decisions, and this
document is the measurement rather than the decision.

**Two of the four questions have since been answered by implementation rather than by a
decision taken here, and both are struck above rather than deleted: question 3 by §4.11's
splitting of the row, and question 4 by `188e973`'s projection. ~~Questions 1 and 2 are open
as written.~~**

**2026-09-10: question 1 was priced and question 2 acquired a fourth option. Neither is
decided, and both have moved enough that "open as written" is no longer accurate.**

*Question 1* was measured over six options in `docs/MEASURE_BM25_BOUNDARY.md`, and the
measurement found something none of the three candidates predicted. Single writer, splitting
the index write into one transaction per document costs 0.9–2.8 ms a document and a second
run could not establish the sign of it; with six concurrent writers against one index the
order **inverts** and per-leaf is fastest in five of five repeats. So 4.5's lock is not
avoided by a coarse hold, it is maximised by one. The document also found the thing that
blocks every option equally: BM25's leaf scan had no `settled` gate at all, where vector
search, MaxSim and BM25's own container pass all have one — so no option could distinguish a
partial index from a complete one. **That gate shipped** (`bc897c5`); with it, every option
can. What is still undecided is the boundary itself, and the recommendation on record is the
gate plus a catch-up rule at every settling boundary whose work is a set difference rather
than a subtree walk.

*Question 2* — "should the token index become HNSW?" — is now a four-way decision rather
than a yes/no, and it is owned by `ANN_INDEX_HEALTH.md` 5.6. The new term is that
`schema.sql` builds `idx_token_embed_256_ivf` **before any row exists** and nothing ever
rebuilds it, so the shipped index has never held centroids fitted to the data. Every
measurement in Part 6 of this document, 6.2 included, built it after loading its rows, which
is the favourable order and the one the appliance never reaches. One `REINDEX` moves recall
from 0.7300 to 0.9300 at the shipped `probes = 1`, and is also faster. 6.2's own number is
not falsified by that — a *fitted* index at 1.37M rows still reads 0.7533 at one probe — but
the option set now includes "rebuild the index the appliance already has", which 6.2 could
not see because it never measured an unfitted one.
