# bench_infra — sharded measurements on whatever hosts are to hand

Tooling, not a wheel: outside the gate, imported by nothing in `jmfts_core`. It exists to
turn a measurement that would take a day of CPU on one machine into one that takes an hour
on twenty, and to keep every reading together with the environment it was taken on.

```bash
# On this machine: three runner processes, results in ./bench-store
python -m scripts.bench_infra launch --backend local --hosts 3 smoke --shards 6

# On Azure: six spot VMs, one per region, results in a blob container
python -m scripts.bench_infra launch --backend azure --hosts 6 --spot recall-gate --repeats 20
python -m scripts.bench_infra status  --backend azure JOB
python -m scripts.bench_infra collect --backend azure JOB      # → bench-results/JOB/summary.json
python -m scripts.bench_infra sweep                            # delete finished/expired VMs
```

## Pieces

| | |
|---|---|
| `workloads/` | how to split a measurement into shards, and run ONE shard into a directory |
| `store.py` | where a job lives: a directory, or an Azure blob container by SAS URL. Stdlib only |
| `runner.py` | the host side: claim a shard, run it, upload it, repeat |
| `backends/` | put hosts in front of a job and take them away: `local`, `azure` |

**Hosts pull; nothing assigns shards.** Every host runs `runner.py` with the whole job. It
claims a shard by creating `claims/<shard>` only if absent — `O_EXCL` on a directory,
`If-None-Match: *` on a blob — so two hosts cannot both win. A claim carries a heartbeat
every 30 s; one older than `--stale-minutes` (5) belonged to a host that has gone and is
taken over by compare-and-swap (`If-Match`). So a region that never starts costs only its
share of the speed, an evicted host's shard is re-run by whoever is still up, and
`add-hosts` joins hosts to a running job.

A shard that **failed** is still `DONE`, with its exit status: a red recall gate is a
reading. A shard that was **interrupted** (eviction notice, deadline) is released instead,
so the next host takes it immediately.

## Workloads

| name | a shard is | host needs |
|---|---|---|
| `smoke` | sleep and hash; measures the harness and the host | nothing |
| `recall-gate` | one run of `tests/test_maxsim_recall.py` + `tests/test_filtered_recall.py`, readings and per-test outcomes kept | pgvector Postgres, `[dev]` |
| `ole2-extract` | one Govdocs1 zip (~1000 files), POI `test-data/`, or one LibreOffice `qa/` dir; each OLE2 file through probe → `convert:ooxml` → extract, timed | unoserver (`Dockerfile.libreoffice`), `[office,convert]` |
| `beir` | one `jmfts-eval beir` dataset in its own fresh database | pgvector Postgres, `[embed]` |

`collect` writes `summary.json`; each workload's `summarize` decides what is in it
(`recall-gate`: per-test pass/fail counts across runs and which tests flipped).

## Azure

Credentials are `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`,
`AZURE_SUBSCRIPTION_ID` for a service principal with **Contributor on one resource group**
(`$JMFTS_BENCH_RG`, default `jmfts-burst`). Everything is created in that group. The SDK is
not a project dependency:

```bash
pip install azure-identity azure-mgmt-compute azure-mgmt-network azure-mgmt-storage \
    azure-mgmt-resource azure-mgmt-resourcegraph azure-mgmt-msi azure-storage-blob
python -m scripts.bench_infra regions        # quota, spot price, eviction bracket per region
```

What a launch creates: a storage account `jmftsbench<hash of subscription>` with container
`bench` (once); `bench-net-<region>` VNet + NSG with no inbound rules (once per region); per
host a VM, OS disk, NIC and Standard public IP, all with `deleteOption=Delete` and tagged
`bench-job` and `bench-expires`. The public IP is there because a new VNet has no default
outbound access and the host must reach PyPI, Hugging Face and the store.

A host boots stock Ubuntu 24.04, installs what the workload's `host` dict asks for
(`apt`, a venv with the named extras, CPU torch when they need it, `pgvector/pgvector:pg16`,
the `Dockerfile.libreoffice` image), downloads the job's source tarball from the store and
runs the runner. `hosts/<vm>.bootstrap.json` records how long each step took — the number
that says whether a pre-built image would pay for itself.

**Spot:** `--spot` sets `evictionPolicy=Delete`. A deallocated spot VM still counts against
spot quota (3 vCPU per region on the account this was written for), so deallocating would
block the region. Spot quota caps a VM at 2 vCPU; the default size is `Standard_D2as_v6`,
and `--fallback-sizes` are tried in order when a region refuses one for capacity, then the
next region.

**How a VM goes away.** The runner exits when every shard is done, at the job deadline
(`--deadline-hours`, default 6), on an Azure `Preempt` scheduled event, or after idling
past twice the stale period. Then `finish.sh` uploads the logs and:

- deletes the VM itself, **if** a user-assigned identity named `jmfts-burst-vm`
  (`$JMFTS_BENCH_IDENTITY`) exists in the group with Virtual Machine Contributor on it.
  Contributor cannot grant that role, so the owner sets it up once:

  ```bash
  az identity create -g jmfts-burst -n jmfts-burst-vm -l eastus
  az role assignment create --role "Virtual Machine Contributor" \
    --assignee-object-id "$(az identity show -g jmfts-burst -n jmfts-burst-vm --query principalId -o tsv)" \
    --assignee-principal-type ServicePrincipal \
    --scope "/subscriptions/$AZURE_SUBSCRIPTION_ID/resourceGroups/jmfts-burst"
  ```

- otherwise powers off. **A powered-off VM is still billed** until `sweep` deletes it.
  `sweep` deletes host VMs that are stopped, whose runner reported `finished`, or that are
  past `bench-expires`, and then any NIC, IP or disk a half-created host left behind.

`teardown JOB` deletes every VM of one job, whatever state it is in.
