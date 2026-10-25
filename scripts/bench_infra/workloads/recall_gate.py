"""``recall-gate`` — run the recall gates N times on identical hosts and keep every reading.

The Oct-17 question: a gate that goes red now and then is either flaky in itself or flaky
in where it ran. Running it twenty times on one image answers that, as long as each run
keeps what it measured. ``tests/conftest.py`` already appends one JSON line per session to
``$JMFTS_RECALL_REPORT``; this workload points that at the shard's directory and adds the
per-test outcomes from ``--junitxml``.

A red run is a result, not a failure of the harness: the shard's exit status is pytest's,
and ``summarize`` counts reds rather than stopping at the first.

Needs a pgvector Postgres (``JMFTS_DB_*``) and the embedding model; on Azure the bootstrap
provides both.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET

from scripts.bench_infra.spec import REPO_ROOT
from scripts.bench_infra.workloads import ShardContext, Workload

#: The shard status for a run that measured nothing. Outside pytest's own range (0-5).
INVALID_RUN = 90

DEFAULT_TESTS = ["tests/test_maxsim_recall.py", "tests/test_filtered_recall.py"]


class RecallGate(Workload):
    name = "recall-gate"
    description = "the MaxSim and filtered recall gates, N times, every reading kept"
    host = {"postgres": True, "extras": ["dev"]}

    def add_arguments(self, parser):
        parser.add_argument("--repeats", type=int, default=20)
        parser.add_argument("--tests", nargs="+", default=DEFAULT_TESTS)

    def plan(self, args):
        params = {"tests": args.tests}
        return params, [{"id": f"rep-{i:03d}", "repeat": i} for i in range(args.repeats)]

    def run(self, shard, params, ctx: ShardContext) -> int:
        report = ctx.outdir / "readings.jsonl"
        junit = ctx.outdir / "junit.xml"
        env = {**os.environ, "JMFTS_RECALL_REPORT": str(report)}
        env.setdefault("JMFTS_API_TOKEN", "bench-infra")
        with open(ctx.outdir / "pytest.log", "w") as log:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", *params["tests"], "-p", "no:cacheprovider"]
                + ["-q", "-rA", f"--junitxml={junit}"],
                cwd=REPO_ROOT,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        outcomes = {}
        if junit.exists():
            for case in ET.parse(junit).iter("testcase"):
                key = f"{case.get('classname')}::{case.get('name')}"
                if case.find("failure") is not None or case.find("error") is not None:
                    outcomes[key] = "failed"
                elif case.find("skipped") is not None:
                    outcomes[key] = "skipped"
                else:
                    outcomes[key] = "passed"
        readings = []
        if report.exists():
            readings = [json.loads(line) for line in report.read_text().splitlines() if line]
        # A gate that SKIPPED measured nothing, and a run with no readings is not a reading.
        # Both are an incomplete host (no database, no model), never a green run: exiting 0
        # here would count twenty skips as twenty passes.
        invalid = None
        if not outcomes:
            invalid = "pytest reported no test outcomes"
        elif any(o == "skipped" for o in outcomes.values()):
            invalid = "a gate test was skipped; the host is missing something it needs"
        elif not readings:
            invalid = "no readings were appended to $JMFTS_RECALL_REPORT"
        (ctx.outdir / "result.json").write_text(
            json.dumps(
                {
                    "exit": proc.returncode,
                    "invalid": invalid,
                    "outcomes": outcomes,
                    "readings": readings,
                }
            )
        )
        return INVALID_RUN if invalid else proc.returncode

    def summarize(self, results):
        per_test: dict[str, dict[str, int]] = {}
        exits: dict[str, int] = {}
        for record in results.values():
            result = record.get("result") or {}
            exits[str(result.get("exit"))] = exits.get(str(result.get("exit")), 0) + 1
            for test, outcome in (result.get("outcomes") or {}).items():
                counts = per_test.setdefault(test, {})
                counts[outcome] = counts.get(outcome, 0) + 1
        flaky = {t: c for t, c in per_test.items() if len(c) > 1}
        invalid = sum(1 for r in results.values() if (r.get("result") or {}).get("invalid"))
        return {
            "runs": len(results),
            "invalid_runs": invalid,
            "exit_codes": exits,
            "flaky": flaky,
            "per_test": per_test,
        }


WORKLOAD = RecallGate()
