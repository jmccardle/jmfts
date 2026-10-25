"""``--backend local`` — runners as processes on this machine, the store a directory.

For trying a workload before paying for VMs, and for a laptop with spare cores. Host
requirements are this machine's business: a workload that needs Postgres reads
``JMFTS_DB_*`` from the environment, exactly as the test suite does, and one that needs
LibreOffice reads ``JMFTS_CONVERT_URL``.
"""

from __future__ import annotations

import os
import subprocess
import sys

from scripts.bench_infra.backends import Backend
from scripts.bench_infra.spec import REPO_ROOT
from scripts.bench_infra.store import LocalStore

STORE_ENV = "JMFTS_BENCH_STORE"


class LocalBackend(Backend):
    name = "local"

    def add_arguments(self, parser):
        parser.add_argument("--detach", action="store_true", help="start the runners and return")

    def store(self):
        return LocalStore(os.environ.get(STORE_ENV) or REPO_ROOT / "bench-store")

    def launch(self, spec, hosts, args):
        missing = []
        if spec.host.get("postgres") and not os.environ.get("JMFTS_DB_HOST"):
            missing.append("JMFTS_DB_* (a pgvector Postgres the suite may CREATE DATABASE on)")
        if spec.host.get("libreoffice") and not os.environ.get("JMFTS_CONVERT_URL"):
            missing.append("JMFTS_CONVERT_URL (unoserver; see Dockerfile.libreoffice)")
        if missing:
            raise SystemExit(f"{spec.workload} needs, on this machine: " + "; ".join(missing))

        store = self.store()
        procs = []
        for i in range(hosts):
            host = f"local-{i}"
            workdir = REPO_ROOT / "bench-work" / spec.job_id / host
            workdir.mkdir(parents=True, exist_ok=True)
            log = open(workdir / "runner.log", "ab")
            procs.append(
                subprocess.Popen(
                    [sys.executable, "-m", "scripts.bench_infra.runner"]
                    + ["--store", store.describe(), "--job", spec.job_id]
                    + ["--host", host, "--workdir", str(workdir)],
                    cwd=REPO_ROOT,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=args.detach,
                )
            )
            print(f"started {host} (pid {procs[-1].pid}), log {workdir / 'runner.log'}")
        if args.detach:
            return
        codes = [p.wait() for p in procs]
        print(f"runners exited: {codes}")
