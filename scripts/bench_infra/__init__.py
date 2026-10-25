"""Run a measurement as many independent shards, on whatever hosts are to hand.

Tooling, not a wheel: this lives under ``scripts/`` and outside the gate, and nothing in
``jmfts_core`` imports it. ``scripts/bench_infra/README.md`` is the runbook.

Three ideas, kept apart so each can change without the others:

* **A workload** (``workloads/``) knows how to split a measurement into shards and how to
  run ONE shard into a directory. It knows nothing about where it runs.
* **A store** (``store.py``) is where a job lives: its spec, the source it runs, who has
  claimed which shard, and every shard's results. A local directory or an Azure blob
  container, behind the same six calls.
* **A backend** (``backends/``) puts hosts in front of a job and takes them away again.
  ``local`` is processes on this machine; ``azure`` is VMs. A backend never decides which
  host runs which shard.

Hosts pull. Every host runs ``runner.py`` with the whole shard list and claims shards from
the store by creating ``claims/<shard>`` only if it does not exist — an atomic
create-if-absent on both stores — so a region that never starts costs nothing but its share
of the speed, a host that is evicted leaves a claim whose heartbeat stops, and a host
started late joins the same queue. That is the whole scheduler.
"""
