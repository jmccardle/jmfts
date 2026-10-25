"""``smoke`` — prove the loop, and measure the host, without measuring JMFTS.

Each shard sleeps a little, hashes for a fixed time to give a crude single-core throughput,
and writes both. It needs nothing installed, so a smoke job's wall clock on a fresh VM is
the cost of the bootstrap itself — which is the number that decides whether a pre-built
image is worth making.
"""

from __future__ import annotations

import hashlib
import json
import time

from scripts.bench_infra.workloads import ShardContext, Workload


class Smoke(Workload):
    name = "smoke"
    description = "sleep, hash, report the host — tests the harness, not JMFTS"
    host = {}

    def add_arguments(self, parser):
        parser.add_argument("--shards", type=int, default=4)
        parser.add_argument("--seconds", type=float, default=10.0)

    def plan(self, args):
        params = {"seconds": args.seconds}
        return params, [{"id": f"smoke-{i:03d}"} for i in range(args.shards)]

    def run(self, shard, params, ctx: ShardContext) -> int:
        deadline = time.monotonic() + params["seconds"]
        rounds, block = 0, b"\0" * (1 << 20)
        digest = hashlib.sha256()
        start = time.monotonic()
        while time.monotonic() < deadline and not ctx.stop.is_set():
            digest.update(block)
            rounds += 1
        elapsed = time.monotonic() - start
        (ctx.outdir / "result.json").write_text(
            json.dumps({"sha256_mb_per_s": round(rounds / elapsed, 1), "seconds": elapsed})
        )
        return 0

    def summarize(self, results):
        rates = [r["result"]["sha256_mb_per_s"] for r in results.values() if r.get("result")]
        return {"sha256_mb_per_s": sorted(rates)}


WORKLOAD = Smoke()
