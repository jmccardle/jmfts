"""``python -m scripts.bench_infra`` — launch a sharded measurement, watch it, collect it.

    python -m scripts.bench_infra launch  --backend local --hosts 2 smoke --shards 4
    python -m scripts.bench_infra launch  --backend azure --hosts 6 recall-gate --repeats 20
    python -m scripts.bench_infra status  --backend azure JOB
    python -m scripts.bench_infra collect --backend azure JOB        # → bench-results/JOB/
    python -m scripts.bench_infra add-hosts --backend azure JOB --hosts 2
    python -m scripts.bench_infra teardown --backend azure JOB
    python -m scripts.bench_infra jobs    --backend azure
    python -m scripts.bench_infra sweep                               # azure: delete expired
    python -m scripts.bench_infra regions                             # azure: quota, price

See ``scripts/bench_infra/README.md``.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.bench_infra.backends import get_backend
from scripts.bench_infra.spec import REPO_ROOT, JobSpec, git_state, job_prefix, utcnow
from scripts.bench_infra.workloads import WORKLOADS


def _new_job_id(workload: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    return f"{workload}-{stamp}-{secrets.token_hex(2)}"


def cmd_launch(args) -> int:
    backend = get_backend(args.backend)
    workload = WORKLOADS[args.workload]
    params, shards = workload.plan(args)
    if not shards:
        raise SystemExit("the workload planned no shards")
    sha, dirty = git_state()
    now = datetime.now(timezone.utc)
    spec = JobSpec(
        job_id=args.job_id or _new_job_id(workload.name),
        workload=workload.name,
        params=params,
        shards=shards,
        created=utcnow(),
        git_sha=sha,
        git_dirty=dirty,
        deadline=(now + timedelta(hours=args.deadline_hours)).isoformat(timespec="seconds"),
        host=workload.host,
    )
    store = backend.store()
    if not store.create(job_prefix(spec.job_id) + "job.json", spec.to_json()):
        raise SystemExit(f"job {spec.job_id} already exists in {store.describe()}")
    hosts = min(args.hosts, len(shards))
    print(f"job {spec.job_id}: {len(shards)} shard(s) of {workload.name} on {hosts} host(s)")
    print(
        f"  store {store.describe()}, deadline {spec.deadline}, commit {sha[:12]}{' dirty' if dirty else ''}"
    )
    backend.launch(spec, hosts, args)
    return 0


def _load(backend, job_id: str) -> JobSpec:
    raw = backend.store().get(job_prefix(job_id) + "job.json")
    if raw is None:
        raise SystemExit(f"no job {job_id!r} in {backend.store().describe()}")
    return JobSpec.from_json(raw)


def _shard_states(store, spec: JobSpec) -> dict[str, dict]:
    prefix = job_prefix(spec.job_id)
    keys = set(store.list(prefix))
    now = datetime.now(timezone.utc).timestamp()
    states = {}
    for shard in spec.shards:
        sid = shard["id"]
        if f"{prefix}shards/{sid}/DONE" in keys:
            done = json.loads(store.get(f"{prefix}shards/{sid}/DONE"))
            states[sid] = {"state": "done", **done}
        elif f"{prefix}claims/{sid}" in keys:
            claim = json.loads(store.get(f"{prefix}claims/{sid}"))
            age = now - claim.get("heartbeat", 0)
            state = "released" if claim.get("released") else ("running" if age < 300 else "stale")
            states[sid] = {"state": state, "age_s": round(age), **claim}
        else:
            states[sid] = {"state": "pending"}
    return states


def cmd_status(args) -> int:
    backend = get_backend(args.backend)
    spec = _load(backend, args.job)
    store = backend.store()
    states = _shard_states(store, spec)
    counts = Counter(s["state"] for s in states.values())
    failed = [sid for sid, s in states.items() if s["state"] == "done" and s.get("status")]
    print(f"job {spec.job_id} ({spec.workload}), deadline {spec.deadline}")
    print("  shards: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    if failed:
        print(
            f"  done with non-zero status: {', '.join(failed[:20])}{' …' if len(failed) > 20 else ''}"
        )
    for sid, s in states.items():
        if s["state"] in ("running", "stale", "released"):
            print(
                f"  {sid}: {s['state']} on {s.get('host')} ({s.get('age_s')}s) {s.get('progress') or ''}"
            )
    for key in store.list(job_prefix(spec.job_id) + "hosts/"):
        if not key.endswith(".json") or key.endswith(".bootstrap.json"):
            continue
        host = json.loads(store.get(key))
        boot = store.get(key[: -len(".json")] + ".bootstrap.json")
        boot = f" bootstrap {json.loads(boot)}" if boot else ""
        az = (host.get("env") or {}).get("azure") or {}
        where = f"{az.get('location')} {az.get('vmSize')} {az.get('priority')}" if az else ""
        print(
            f"  host {host['host']}: {host['state']} at {host['updated']}, ran {len(host['ran'])}"
            f" {where}{boot} {('stopped: ' + host['stop_reason']) if host.get('stop_reason') else ''}"
        )
    for vm in backend.hosts(spec.job_id):
        print(f"  vm {vm['name']}: {vm.get('location')} {vm.get('state')}")
    return 0


def cmd_collect(args) -> int:
    backend = get_backend(args.backend)
    spec = _load(backend, args.job)
    store = backend.store()
    out = Path(args.output or REPO_ROOT / "bench-results" / spec.job_id)
    prefix = job_prefix(spec.job_id)
    fetched = 0
    for key in store.list(prefix):
        rel = key[len(prefix) :]
        if rel == "src.tar.gz":
            continue
        dest = out / rel
        if dest.exists() and not rel.startswith(("hosts/", "claims/")):
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(store.get(key))
        fetched += 1
    states = _shard_states(store, spec)
    done = {sid: s for sid, s in states.items() if s["state"] == "done"}
    summary = {
        "job": spec.job_id,
        "workload": spec.workload,
        "git_sha": spec.git_sha,
        "shards": len(spec.shards),
        "done": len(done),
        "states": dict(Counter(s["state"] for s in states.values())),
        "summary": WORKLOADS[spec.workload].summarize(done),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
    print(f"fetched {fetched} file(s) into {out}")
    print(json.dumps(summary, indent=2, sort_keys=True)[:4000])
    return 0


def cmd_add_hosts(args) -> int:
    backend = get_backend(args.backend)
    spec = _load(backend, args.job)
    backend.launch(spec, args.hosts, args)
    return 0


def cmd_teardown(args) -> int:
    get_backend(args.backend).teardown(args.job)
    return 0


def cmd_jobs(args) -> int:
    store = get_backend(args.backend).store()
    for key in store.list("jobs/"):
        if key.endswith("/job.json"):
            spec = JobSpec.from_json(store.get(key))
            print(
                f"{spec.job_id}  {spec.workload}  {len(spec.shards)} shards  deadline {spec.deadline}"
            )
    return 0


def cmd_sweep(args) -> int:
    from scripts.bench_infra.backends.azure import AzureBackend

    AzureBackend().sweep(dry_run=args.dry_run, include_finished=not args.expired_only)
    return 0


def cmd_regions(args) -> int:
    from scripts.bench_infra.backends.azure import AzureBackend

    AzureBackend().report_regions(args.size, args.regions)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.bench_infra",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def with_backend(p, launching=False):
        p.add_argument("--backend", default="local", choices=["local", "azure"])
        if launching:
            for name in ("local", "azure"):
                group = p.add_argument_group(f"{name} backend")
                get_backend_lazy(name).add_arguments(group)
        return p

    launch = with_backend(sub.add_parser("launch", help="plan a job and start hosts on it"), True)
    launch.add_argument("--hosts", type=int, default=1)
    launch.add_argument("--deadline-hours", type=float, default=6.0)
    launch.add_argument("--job-id")
    workloads = launch.add_subparsers(dest="workload", required=True)
    for workload in WORKLOADS.values():
        workload.add_arguments(workloads.add_parser(workload.name, help=workload.description))
    launch.set_defaults(func=cmd_launch)

    for name, func, helptext in (
        ("status", cmd_status, "shards and hosts of a job"),
        ("collect", cmd_collect, "download a job's results and summarize them"),
        ("teardown", cmd_teardown, "delete every resource a backend started for a job"),
    ):
        p = with_backend(sub.add_parser(name, help=helptext))
        p.add_argument("job")
        if name == "collect":
            p.add_argument("-o", "--output")
        p.set_defaults(func=func)

    add = with_backend(
        sub.add_parser("add-hosts", help="start more hosts on an existing job"), True
    )
    add.add_argument("job")
    add.add_argument("--hosts", type=int, default=1)
    add.set_defaults(func=cmd_add_hosts)

    jobs = with_backend(sub.add_parser("jobs", help="list the jobs in a backend's store"))
    jobs.set_defaults(func=cmd_jobs)

    sweep = sub.add_parser(
        "sweep", help="azure: delete bench VMs that finished or outlived their deadline"
    )
    sweep.add_argument("--dry-run", action="store_true")
    sweep.add_argument("--expired-only", action="store_true")
    sweep.set_defaults(func=cmd_sweep)

    regions = sub.add_parser("regions", help="azure: vCPU quota, spot price and eviction rate")
    regions.add_argument("--size", default="Standard_D2as_v6")
    regions.add_argument("--regions", nargs="*")
    regions.set_defaults(func=cmd_regions)
    return parser


def get_backend_lazy(name):
    """A backend's argument definitions without importing its cloud SDK."""
    if name == "azure":
        from scripts.bench_infra.backends.azure_args import add_azure_arguments

        class _Args:
            add_arguments = staticmethod(add_azure_arguments)

        return _Args
    return get_backend(name)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
