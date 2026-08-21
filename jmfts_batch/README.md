# The reference batch worker

Consumes `summarize:llm` tasks through an external batch provider at roughly half the
synchronous price. It is a **reference implementation**, not a feature of the appliance.
Nothing in `jmfts_core` imports it, and deleting this directory leaves JMFTS working with
`summarize:llm` served the direct way.

It uses only APIs `jmfts_core` already exposes: `claim_next`, `mark_running`,
`mark_batched`, `outstanding_batches`, `with_batch_lock`, `batched_tasks`,
`stalled_batches`, `touch_heartbeat`, `complete`, `fail`, and `store_effective_content`.

## The state machine

| State | Task row says | Who holds it | If the worker dies here |
|---|---|---|---|
| Gathered | `running` | this worker, beating | the lease requeues it, nothing spent |
| Submitted | `running` | this worker, beating | **the accepted window** — see below |
| Batched | `batched` | nobody | any worker adopts it |
| Applied | `completed` / `failed` | nobody | done |

`batched` is not claimable, not reapable, and does not consume a retry. It holds its node's
`self` reservation the whole time, so nothing else can write that node while the provider
has the work.

### The accepted window

Between the provider accepting a batch and `mark_batched` committing, the work is paid for
and nothing durable says so. A worker that dies in that gap leaves its rows `running`; the
lease requeues them, and a later worker submits a **second** batch for answers already
bought.

The window is milliseconds wide and the duplicate result overwrites an identical one, so it
is expensive rather than corrupting. It is accepted deliberately.

One thing has changed since that decision was made, and it is worth knowing:

- **Anthropic** has no caller-supplied idempotency key and no `metadata` field on a batch.
  The window is irreducible there.
- **OpenAI** has `metadata` — 16 key-value pairs on the batch, readable via
  `GET /v1/batches`. A gather key written there would make the crash recoverable by lookup
  rather than by resubmission. `OpenAIBatchProvider` **writes** metadata; nothing reads it
  back. Closing the window is a decision about spending money twice, so it is not a
  default.

## Providers

| | `mock` | `openai` | `anthropic` |
|---|---|---|---|
| Calls to submit | 1 | 2 (upload file, then create) | 1 (inline) |
| Batch id | `mockbatch_…` | `batch_…` | `msgbatch_…` |
| Status field | — | `status` | `processing_status` |
| Batch-level failure | cancel only | `failed` / `expired` / `cancelled` | none — always `ended` |
| Per-request outcome | text or error | `error`, or `response.status_code` | `result.type` |
| Cap | configurable | 50,000 requests / 200 MB | 100,000 requests / 256 MB |
| Turnaround | when you finalize it | 24h window | usually <1h, expires at 24h |
| Results kept | until you delete them | file storage | 29 days |

Provider facts verified against the current docs on 2026-08-20.

`custom_id` is the only mapping. It is set to `task-{id}`, which satisfies Anthropic's
`^[a-zA-Z0-9_-]{1,64}$` — the tighter of the two rules — so one format works everywhere.
A result whose `custom_id` this appliance did not write is **skipped, never applied**.

Neither the `openai` nor the `anthropic` SDK is a dependency. Four HTTP calls each.

## The mock

It fakes the protocol, not the model. Summaries come from whatever llama-server or vLLM you
point it at. What it fakes is the part that costs money and takes a day: submit returns a
durable id, then nothing happens until the batch is finalized.

```bash
# Submit and park. The model is NOT called.
jmfts-batch-worker run --provider mock --badge llm \
    --store /var/lib/jmfts/batches --llm-url http://llm-host:8080 --once

# See what is parked.
jmfts-batch-worker status

# Bring the wall down. Still does not call the model.
jmfts-batch-worker finalize mockbatch_… --store /var/lib/jmfts/batches

# THIS calls the model, then writes effective_content and completes the tasks.
jmfts-batch-worker run --provider mock --badge llm --store /var/lib/jmfts/batches --once
```

`release_after_seconds` on a batch finalizes it on a wall clock instead, for a soak run
nobody is driving.

**The mock's store is a plain directory, so it is single-host.** Whoever holds the volume is
the only one who can poll it. That narrows the queue's own contract — `outstanding_batches`
is deliberately not scoped to a worker so that any worker can adopt an abandoned batch — and
a `ReadWriteOnce` PVC gives that up. Mount `ReadWriteMany` to get it back, or accept that a
mock batch is recoverable only by a pod landing on the same volume. The two commercial
providers do not have this problem because their store is the provider.

## Configuration

The routing policy is **required**, and the worker refuses to start without it.

`claim_next` filters by badge and knows nothing about `task_type`. With `JMFTS_TASK_BADGES`
unset every task in the appliance is un-badged, an un-badged task is claimable by anyone,
and this worker would gather `probe` and `extract:text` rows into an LLM batch and pay to
summarize them. Set the policy before running it:

```bash
JMFTS_TASK_BADGES='{"structure:declared":"embed","structure:inferred":"embed","summarize":"embed","summarize:llm":"llm"}'
```

Tasks enqueued *before* the policy was turned on still carry a NULL badge and remain
claimable by this worker. It checks `task_type` on every claim as a backstop and fails a
wrong one as retryable, so another worker can take it.

| Flag | Default | Notes |
|---|---|---|
| `--provider` | none | required; no provider is guessed |
| `--badge` | none | repeatable; must include whatever routes `summarize:llm` |
| `--gather-size` | 32 | see below |
| `--poll-seconds` | 60 | |
| `--stall-seconds` | 93600 (26h) | past both providers' expiry |
| `--store` | `/var/lib/jmfts/batches` | mock only; `JMFTS_BATCH_STORE` |

Keys come from `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`. The mock uses `JMFTS_LLM_BASE_URL`
and `JMFTS_LLM_API_KEY`, sending an `Authorization` header only when a key is set.

Set `JMFTS_SUMMARIZATION_DISABLE_THINKING=true` when the local runner serves a reasoning
model. Measured against llama-server with `Qwen3.8-27B-Q4_0` on 2026-08-20: without it the
model spent its 200-token budget on the reasoning trace and returned the fragment
`The appliance` as the summary. That fragment is not an error at any layer — it embeds, it
stores, and the node ends up advertising an `effective_content` that says nothing. With the
flag the same two summaries came back complete and the pass ran in 2.2s instead of 8.7s.

### Why the gather size is small

Not because of the provider — both allow tens of thousands per batch. Every gathered task
holds its node's `self` reservation for the life of the batch, up to 24 hours, during which
nothing else may write that node. Gathering broadly freezes a large part of the tree for a
day to save a few HTTP calls. That cost is invisible to the 50,000-request cap.

## What it does not do

- **No batch-eligibility badge.** It claims the same `summarize:llm` work the direct worker
  claims. Routing cheap work to batch and urgent work to a direct runner is a
  `JMFTS_TASK_BADGES` change plus a second badge; no code here needs to change.
- **No scheduling.** The 16:00→06:00 batch window is Kubernetes replica counts on a cron
  scaler. The queue never needs to know the time.
- **No stall recovery.** `jmfts-batch-worker stalled` reports batches parked past their
  turnaround and exits non-zero. Deciding that a paid-for answer is not coming is not this
  worker's call.
- **No resubmission of a dead batch.** A `failed`/`expired`/`cancelled` batch fails its
  tasks as retryable, and the normal retry path picks them up.

## Tests

`tests/test_batch_worker.py`. No network and no API key — the mock covers the state machine
end to end, and the two commercial adapters are tested by parsing recorded payload shapes.

`TestMirrorsTheCoreHandler` is the one to know about. `jmfts_batch/summarize.py` is
`run_summarize_llm` split across the hours a batch takes, and a copy drifts. That test runs
both paths over identical nodes and asserts the stored `effective_content` matches. If you
change `run_summarize_llm`, it will tell you to change this too.
