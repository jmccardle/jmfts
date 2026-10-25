"""``beir`` — ``jmfts-eval beir``, one dataset per shard, each in its own fresh database.

The harness is ``jmfts_core/eval/`` as it stands; this workload adds nothing to what it
measures. It gives each shard an empty database (``jmfts-eval`` refuses one that holds
anything but BEIR corpora, and a fresh one per dataset makes that trivially true), runs
``jmfts-init-db`` on it, and points ``--output`` into the shard's directory.

What it does NOT do yet is spread one dataset's embedding over several hosts: a shard is a
whole dataset on one host. That is right for scifact and nfcorpus and slow for fiqa and
above on a 2-vCPU VM; the split is the ``JMFTS_RUNNER_URL`` topology (one database host,
spot runners for vectors), which needs the claim gate in ``IngestWorker`` and comes next.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

from scripts.bench_infra.spec import REPO_ROOT
from scripts.bench_infra.workloads import ShardContext, Workload

DEFAULT_DATASETS = ["scifact", "nfcorpus"]
DEFAULT_METHODS = ["vector", "bm25", "hybrid"]


class Beir(Workload):
    name = "beir"
    description = "jmfts-eval beir, a dataset per shard, a fresh database each"
    host = {"postgres": True, "extras": ["embed"]}

    def add_arguments(self, parser):
        parser.add_argument("--datasets", nargs="+", default=DEFAULT_DATASETS)
        parser.add_argument("--methods", nargs="+", default=DEFAULT_METHODS)
        parser.add_argument("--over-window", default="error", choices=["error", "skip", "truncate"])

    def plan(self, args):
        params = {"methods": args.methods, "over_window": args.over_window}
        return params, [{"id": f"beir-{d}", "dataset": d} for d in args.datasets]

    def run(self, shard, params, ctx: ShardContext) -> int:
        dataset = shard["dataset"]
        db_name = "beir_" + dataset.replace("-", "_")
        env = {**os.environ, "JMFTS_DB_NAME": db_name}
        env.setdefault("JMFTS_API_TOKEN", "bench-infra")
        _fresh_database(db_name)
        data_dir = ctx.cachedir / "beir-data"
        bin_dir = os.path.dirname(sys.executable)
        with open(ctx.outdir / "eval.log", "w") as log:
            for argv in (
                [os.path.join(bin_dir, "jmfts-init-db")],
                [os.path.join(bin_dir, "jmfts-eval"), "beir", "-d", dataset]
                + ["--data-dir", str(data_dir), "--methods", *params["methods"]]
                + ["--over-window", params["over_window"]]
                + ["--output", str(ctx.outdir / f"{dataset}.json")],
            ):
                log.write(f"$ {' '.join(argv)}\n")
                log.flush()
                proc = subprocess.run(
                    argv, cwd=REPO_ROOT, env=env, stdout=log, stderr=subprocess.STDOUT
                )
                if proc.returncode:
                    break
        produced = ctx.outdir / f"{dataset}.json"
        result = json.loads(produced.read_text()) if produced.exists() else None
        (ctx.outdir / "result.json").write_text(
            json.dumps({"exit": proc.returncode, "eval": result})
        )
        shutil.rmtree(data_dir / dataset, ignore_errors=True)
        return proc.returncode

    def summarize(self, results):
        return {sid: (r.get("result") or {}).get("exit") for sid, r in results.items()}


def _fresh_database(name: str) -> None:
    """DROP and CREATE ``name`` on the server ``JMFTS_DB_*`` names, connecting to ``postgres``."""
    import psycopg2

    from jmfts_core.config import get_settings

    s = get_settings()
    conn = psycopg2.connect(
        host=s.db_host, port=s.db_port, user=s.db_user, password=s.db_password, dbname="postgres"
    )
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            cur.execute(f'CREATE DATABASE "{name}"')
    finally:
        conn.close()


WORKLOAD = Beir()
