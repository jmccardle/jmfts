"""A job, the keys it occupies in its store, and what a host records about itself.

Store layout, one job per prefix::

    jobs/<job>/job.json                 the JobSpec, written once by ``plan``/``launch``
    jobs/<job>/src.tar.gz               the source tree at the job's commit (remote hosts)
    jobs/<job>/claims/<shard>           who is running a shard, and their last heartbeat
    jobs/<job>/shards/<shard>/...       everything the shard wrote
    jobs/<job>/shards/<shard>/DONE      written last; its presence is "this shard finished"
    jobs/<job>/hosts/<host>.json        a host's environment and progress, rewritten often

A shard that FAILED is still DONE — a red recall gate is a reading, not an error in the
harness. ``DONE`` carries the exit status; ``status`` reports both.
"""

from __future__ import annotations

import json
import os
import platform
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[2]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class JobSpec:
    job_id: str
    workload: str
    params: dict
    shards: list[dict]
    created: str
    git_sha: str
    git_dirty: bool
    #: Hosts stop claiming after this, and the Azure backend tags every resource with it so
    #: ``sweep`` can delete whatever outlived it.
    deadline: str
    #: What a host must install before it can run a shard. From the workload; see
    #: ``workloads.Workload.host``.
    host: dict = field(default_factory=dict)

    def to_json(self) -> bytes:
        return json.dumps(asdict(self), indent=2, sort_keys=True).encode()

    @classmethod
    def from_json(cls, data: bytes) -> "JobSpec":
        return cls(**json.loads(data))

    @property
    def prefix(self) -> str:
        return job_prefix(self.job_id)


def job_prefix(job_id: str) -> str:
    return f"jobs/{job_id}/"


def git_state() -> tuple[str, bool]:
    """(sha, dirty) of the working tree, from ``git`` or from a ``SOURCE_SHA`` file.

    A remote host runs from a tarball with no ``.git``; the tarball carries ``SOURCE_SHA``.
    """
    stamp = REPO_ROOT / "SOURCE_SHA"
    if stamp.exists():
        sha, _, dirty = stamp.read_text().strip().partition(" ")
        return sha, dirty == "dirty"
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    return sha, dirty


def _imds() -> Optional[dict]:
    """Azure's instance metadata, or None off Azure. One short request, never retried."""
    request = urllib.request.Request(
        "http://169.254.169.254/metadata/instance/compute?api-version=2021-02-01",
        headers={"Metadata": "true"},
    )
    try:
        with urllib.request.urlopen(request, timeout=1.0) as response:
            compute = json.loads(response.read())
    except (OSError, ValueError):
        return None
    keys = ("vmSize", "location", "zone", "priority", "vmId", "name", "storageProfile")
    out = {k: compute.get(k) for k in keys if k in compute}
    image = (compute.get("storageProfile") or {}).get("imageReference") or {}
    out["image"] = {k: image.get(k) for k in ("publisher", "offer", "sku", "version")}
    out.pop("storageProfile", None)
    return out


def _cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def _mem_total_mb() -> Optional[int]:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) // 1024
    except OSError:
        pass
    return None


def environment() -> dict:
    """What a reading was taken ON. Written beside every shard's results.

    The Oct-17 lesson in one function: a number without the environment it came from cannot
    be compared with another number, so this is recorded per shard rather than per job.
    """
    sha, dirty = git_state()
    freeze = subprocess.run(
        [sys.executable, "-m", "pip", "freeze", "--exclude-editable"],
        capture_output=True,
        text=True,
    ).stdout
    wanted = ("torch", "sentence-transformers", "transformers", "psycopg", "pgvector", "numpy")
    packages = {
        line.split("==")[0].lower(): line.split("==", 1)[1]
        for line in freeze.splitlines()
        if "==" in line and line.split("==")[0].lower() in wanted
    }
    return {
        "recorded": utcnow(),
        "host": socket.gethostname(),
        "git_sha": sha,
        "git_dirty": dirty,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu": _cpu_model(),
        "cpus": os.cpu_count(),
        "mem_mb": _mem_total_mb(),
        "packages": packages,
        "pip_freeze": freeze.splitlines(),
        "azure": _imds(),
        "monotonic_at_record": time.monotonic(),
    }
