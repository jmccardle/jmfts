# Running the ingest worker fleet

The worker is the same `IngestWorker` the API has always run in a thread. What is new is
that it also runs as its own process, so ingestion capacity can be added by starting more
of them, on more machines, instead of by making one appliance bigger.

Nothing in the queue changed to allow this. `claim_next` already serialised claims under
an advisory lock and enforced the write-mode reservation across every claimer, whatever
process it was in, and `service_badge` was already the routing mechanism. Two things were
missing and are covered here: a way to run the loop without also running an HTTP server
(`jmfts_core/worker.py`), and recovery for a worker that never comes back
(`migrations/011_task_queue_heartbeat.sql`).

## What routes where

Measured end to end over a real directory with `scripts/e2e_ingest_corpus.py`
(5 files, 559 nodes, CPU embedding):

| task_type | runs | total_s | mean_s | share |
|---|---|---|---|---|
| structure:declared | 5 | 153.6 | 30.72 | 54% |
| summarize | 5 | 128.5 | 25.69 | 46% |
| extract:text | 5 | 0.1 | 0.01 | 0.04% |
| probe | 5 | 0.02 | 0.02 | 0.03% |

The two task types that cost anything are the two that ran the embedding model, and the two
that cost nothing are the two the code documents as not embedding. So the split is not a
judgement about heavy and light work — it is which scarce resource the handler needs.

`structure:declared` was not a slow text splitter. It created every chunk with
`auto_embed=True`, so its 30 seconds were N transformer forward passes running in a loop
inside one claimed task. **That embedding is now its own task type, `embed`**, scoped to
the node it embeds and declaring `self` — so a document's chunks embed concurrently instead
of in sequence, and the 54% above is redistributed across as many tasks as the document has
chunks. The rungs themselves dropped out of the policy: badging string work for a GPU pool
would pin it to the scarce resource for nothing.

There are two such resources, and they are separately priced:

| badge | resource | task types |
|---|---|---|
| `embed` | the embedding model | `embed`, `summarize` |
| `llm` | a completion | `summarize:llm` |
| *(none)* | neither | `probe`, `extract:text`, `structure:declared`, `structure:inferred`, `structure:semantic` |

**`llm` names the resource, not the hardware.** A host running a model locally on a GPU and
a light process forwarding to a metered web API both answer it, and both are in
`deploy/k8s/22-worker-llm.yaml`. A worker answers `llm` if it can get a completion, however
it gets one. That is also what makes the opportunistic case work: when the local GPU is
idle it absorbs work the API would otherwise have been paid for.

**Why `summarize` and `summarize:llm` are separate task types.** Every `summarize` embeds.
Only the ones whose concatenated children overflow the embedding window need a completion,
and which ones those are is decided by `check_fit` *inside* the handler, after the claim —
the queue cannot know it at enqueue time. So `summarize` does the fit check and either
stores the concatenation itself or enqueues `summarize:llm`, which carries the `llm` badge.
Before the split there was one badge for a handler needing two scarce resources, and no way
to name the expensive one without also claiming the cheap one.

**A worker can answer more than one badge.** `--badge embed --badge llm`, or
`JMFTS_WORKER_BADGE=embed,llm`. Badges are a filter, not a preference order: `claim_next`
still orders by `priority DESC, created_at ASC` across every badge the worker answers, so
an idle expensive worker will start cheap work a moment before urgent work arrives.
`priority` on the row is the lever for that.

**Routing by cost and urgency** is a finer badge, not a new mechanism. Split `llm` into
`llm-fast` and `llm-bulk` in the ConfigMap, give the local pool both and the API pool only
the cheap one. The exact names are yours; nothing in the code knows them.

`jmfts_core/task_routing.py` holds the policy presets.

## The one way this can stall

Badge semantics are asymmetric. A badged worker claims its own badge **plus** un-badged
work; an un-badged worker claims **anything**. Two consequences:

1. A cluster that sets `JMFTS_TASK_BADGES` but runs no worker for some badge it names does
   not run slowly. It stalls — permanently and silently. The tasks sit `pending`, their
   nodes never leave `in_flight`, and nothing reports it, because a worker that does not
   exist cannot fail to report in. Delete a Deployment, delete its badge from the map.
2. A single un-badged worker anywhere in a fleet claims *every* badge's work. It will run
   embedding on a CPU while the GPU idles, and it will spend money on a metered API for
   work the local model would have done for free.

This is why the routing policy is empty in `jmfts_core/config.py` and set in
`deploy/k8s/10-config.yaml`, next to the deployments that define the pools it names.

## A worker that does not hold the model

`--runner-url` sends the `embed` task to another JMFTS's `/runner` surface instead of
running the model locally:

```
jmfts-worker --badge embed --runner-url http://jmfts-api:8100 --worker-id thin-0
```

Both sides read one `JMFTS_RUNNER_KEY`; a URL with no key is refused at startup rather than
tried anonymously. The badge is unchanged and orthogonal — a thin worker *is* answering for
embedding work, it just is not the thing running the model.

**What this changes about capacity.** Without it, every worker that touches ingest owns
weights, so the cards are allocated by which pool you started: an embedding pool with a
card each and a summarization pool with a card each, sized before the corpus arrives.
With it, the storage-side workers scale on CPU and the cards sit behind one runner that
both kinds of work draw from, so a corpus that is mostly chunking and a corpus that is
mostly summarizing get the same hardware without a redeploy.

**Cost.** Each `embed` becomes an HTTP round trip carrying a base64 float16 token matrix —
measured at 171 KB for a 256×256 matrix, against 1398 KB for the same numbers as JSON. That
is a LAN cost, not a WAN one. The tokenizer stays local: `check_fit` is called per candidate
piece by the chunker, and forwarding it would put a round trip inside every chunking
decision.

**What it does not cover.** Search embeds its queries locally, whatever this is set to. A
query embed is one forward pass in the request path and routing it over HTTP would put a
network hop inside every search, so a process that serves `/search` still needs the model.
The process this empties out is a worker.

The code side is `jmfts_core/embedder.py`, and no handler, repository or task knows where
the model is — they ask `get_embedder()`, which is local unless `JMFTS_RUNNER_URL` is set.

**And it does not have to ship the model either.** `torch` and `sentence-transformers` are
the `embed` extra rather than base dependencies, so a worker image built without them is
genuinely small:

```
docker build -f Dockerfile.worker -t jmfts-worker-thin:latest --build-arg EXTRAS= .
```

Measured with `docker images`: **689 MB against 3.98 GB** for the CPU image built from the
same file. The thin image still bakes the *tokenizer* — a worker that does not embed still
measures, and fetching the tokenizer on first use would reintroduce the cold start the bake
exists to remove.

A worker built that way **must** be given `--runner-url`. Without one, its first `embed`
task raises `ModelStackNotInstalled`, which names both ways out and is classified PERMANENT
— so the node fails visibly with a readable reason rather than retrying three times against
a package that is not going to appear. `./scripts/check_base_install.sh` builds a clean venv
and asserts the whole arrangement; `tests/test_thin_worker.py` covers it in the suite.

There is no runner Deployment in `deploy/k8s/` yet — an embedding pool plus a thin pool
needs a second ScaledObject and a Service, and that has not been written.

## Local: two hosts, no Kubernetes

Useful for checking that two machines really do share the queue before any of the
Kubernetes machinery is involved. Both hosts need the database reachable — Postgres on the
GPU host already listens on `0.0.0.0:5432`.

1. Apply the migration once, against the shared database:
   `psql -f migrations/011_task_queue_heartbeat.sql`
2. On the GPU host, start a worker that answers both the embedding and the LLM pool:
   `JMFTS_EMBEDDING_DEVICE=cuda jmfts-worker --badge embed --badge llm --worker-id gpu-0`
3. On the second host, start a CPU worker:
   `JMFTS_DB_HOST=<gpu-host> jmfts-worker --badge cpu --worker-id cpu-0`
4. Export the routing policy to both, or leave it unset and let either claim anything:
   `JMFTS_TASK_BADGES='{"embed":"embed","summarize":"embed","summarize:llm":"llm"}'`
   Give one of them the LLM badge as well, or `summarize:llm` will stall:
   `jmfts-worker --badge embed --badge llm`
5. Upload a directory and watch both `claimed_by` values appear:
   `SELECT claimed_by, task_type, count(*) FROM task_queue GROUP BY 1, 2;`

`jmfts-worker --drain` runs until the queue is empty and exits, which is the form to use
for a one-shot batch or a smoke test.

## Kubernetes

```
docker build -f Dockerfile.worker -t jmfts-worker-cpu:latest .
docker build -f Dockerfile.worker -t jmfts-worker-gpu:latest \
       --build-arg TORCH_INDEX=https://download.pytorch.org/whl/cu126 .

kubectl -n jmfts create secret generic jmfts-worker-db \
        --from-literal=JMFTS_DB_PASSWORD='...'
kubectl apply -k deploy/k8s/
```

The manifests carry no registry, no storage class and no node labels, so they apply to any
cluster; `deploy/k8s/kustomization.yaml` is the single place to point them at your images.
The database is assumed reachable at `postgres.jmfts.svc.cluster.local` — change it in
`10-config.yaml` if yours lives elsewhere.

Autoscaling needs [KEDA](https://keda.sh) and is applied separately:

```
kubectl apply -f deploy/k8s/40-keda-scaledobject.yaml
```

Two pools scale: the CPU pool and the web-API LLM pool, on separate queries, because a
backlog of `summarize:llm` says nothing about how much `probe` work is waiting. The `embed`
and `llm-local` pools are bounded by physical devices, so an autoscaler on either could
only produce Pending pods.

The scalers read queue depth rather than CPU percent, because a worker blocked on the
database or on an LLM looks idle by CPU, and scaling on that would shrink the pool exactly
when the queue is deepest. For the API pool, `maxReplicaCount` is a **cost ceiling** — each
replica is calls against a metered service.

### Talking to a metered API

`JMFTS_LLM_API_KEY` is sent as `Authorization: Bearer ...` and **only when non-empty**, so
one worker image serves both an unauthenticated llama-server on the LAN and a paid API.

HTTP 429 classifies as retryable, so the existing exponential backoff absorbs rate limiting.
A 4xx that is a statement about the request — a model name that does not exist, for
instance — stays PERMANENT and fails the node, because it will not fix itself.

### k3s on the GPU host

The image store lives under the k3s data directory, and a CUDA torch image is several GB.
On a host whose root filesystem is nearly full, put it somewhere with room:

```
curl -sfL https://get.k3s.io | sh -s - server --data-dir /path/with/room/k3s
```

Then install the NVIDIA device plugin, which is what makes `nvidia.com/gpu` schedulable.
k3s's containerd detects the nvidia runtime when `nvidia-container-toolkit` is already
installed on the host. Verify before deploying the GPU pool:

```
kubectl get nodes -o jsonpath='{.items[*].status.allocatable.nvidia\.com/gpu}'
```

A pod requesting `nvidia.com/gpu: 1` stays Pending forever if that comes back empty.
