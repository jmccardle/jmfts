"""The host side: claim shards from a job's store and run them until none are left.

    python -m scripts.bench_infra.runner --store LOCATION --job JOB_ID [--host NAME]

Every host runs this with the whole job. It walks the shard list, skips what is ``DONE``,
claims the next free shard by creating ``claims/<shard>`` (atomic create-if-absent), runs it,
uploads its directory, writes ``DONE``, and goes on. A claim whose heartbeat is older than
``--stale-minutes`` belonged to a host that has gone, and is taken over by compare-and-swap,
so exactly one taker wins it.

It exits when every shard is done; when the job's deadline passes; when the host is told it
is being evicted (Azure's ``Preempt`` scheduled event); or after ``--idle-minutes`` with
nothing it could claim. That last one is what stops a host waiting forever on a shard
someone else is running. It is longer than ``--stale-minutes`` by default, so a host that
outlives an evicted peer is still there to take its shard over.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import socket
import threading
import time
import traceback
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from scripts.bench_infra.spec import JobSpec, environment, job_prefix, utcnow
from scripts.bench_infra.store import Store, open_store
from scripts.bench_infra.workloads import WORKLOADS, ShardContext

logger = logging.getLogger("bench_infra.runner")

HEARTBEAT_SECONDS = 30.0

#: A shard status at or above this is the HOST failing, not the measurement: 90 for a workload
#: that ran and measured nothing (a skipped gate), 99 for an exception in the harness. Such a
#: shard is released for another host rather than marked DONE, up to MAX_ATTEMPTS, and a host
#: that produces HOST_FAILURE_LIMIT of them in a row stops claiming — otherwise one host
#: missing a package burns through the whole job in seconds, which is what the first live
#: recall-gate job did (twenty "green" runs, every test skipped).
HOST_FAILURE_STATUS = 90
MAX_ATTEMPTS = 3
HOST_FAILURE_LIMIT = 2
SCHEDULED_EVENTS_URL = "http://169.254.169.254/metadata/scheduledevents?api-version=2020-07-01"


def _ts() -> float:
    return datetime.now(timezone.utc).timestamp()


class Runner:
    def __init__(
        self,
        store: Store,
        job_id: str,
        *,
        host: str,
        workdir: Path,
        stale_seconds: float,
        idle_seconds: float,
        max_shards: int = 0,
    ):
        self.store = store
        self.prefix = job_prefix(job_id)
        raw = store.get(self.prefix + "job.json")
        if raw is None:
            raise SystemExit(f"no job {job_id!r} in {store.describe()}")
        self.spec = JobSpec.from_json(raw)
        self.workload = WORKLOADS[self.spec.workload]
        self.host = host
        self.workdir = workdir
        self.stale_seconds = stale_seconds
        self.idle_seconds = idle_seconds
        self.max_shards = max_shards
        self.deadline = datetime.fromisoformat(self.spec.deadline).timestamp()
        self.stop = threading.Event()
        self.stop_reason = ""
        self._current: Optional[dict] = None  # {"id", "tag", "claim", "progress"}
        self._lock = threading.Lock()
        self.ran: list[str] = []
        self.host_failures = 0
        self.env = environment()

    # -- the claim ----------------------------------------------------------------------

    def _claim_key(self, shard_id: str) -> str:
        return f"{self.prefix}claims/{shard_id}"

    def _done(self, shard_id: str) -> bool:
        return self.store.get(f"{self.prefix}shards/{shard_id}/DONE") is not None

    def _try_claim(self, shard_id: str) -> Optional[tuple[dict, str]]:
        """(claim, tag) if this host now owns ``shard_id``, else None."""
        now = _ts()
        claim = {"host": self.host, "claimed": now, "heartbeat": now, "attempt": 1}
        key = self._claim_key(shard_id)
        if self.store.create(key, json.dumps(claim).encode()):
            return claim, self.store.get_tagged(key)[1]
        current = self.store.get_tagged(key)
        if current is None:
            return None
        held, tag = json.loads(current[0]), current[1]
        if held.get("released") or now - held.get("heartbeat", 0) > self.stale_seconds:
            claim["attempt"] = held.get("attempt", 1) + 1
            claim["took_over_from"] = held.get("host")
            if self.store.replace_if(key, json.dumps(claim).encode(), tag):
                return claim, self.store.get_tagged(key)[1]
        return None

    def _claimable_or_active(self) -> tuple[list[str], int]:
        """(shards this host could claim now, shards someone else is actively running)."""
        free, active = [], 0
        now = _ts()
        for shard in self.spec.shards:
            sid = shard["id"]
            if self._done(sid):
                continue
            held = self.store.get(self._claim_key(sid))
            if held is None:
                free.append(sid)
                continue
            claim = json.loads(held)
            if claim.get("released") or now - claim.get("heartbeat", 0) > self.stale_seconds:
                free.append(sid)
            else:
                active += 1
        return free, active

    # -- the heartbeat ------------------------------------------------------------------

    def _beat(self) -> None:
        on_azure = bool(self.env.get("azure"))
        vm_name = (self.env.get("azure") or {}).get("name")
        while not self.stop.wait(HEARTBEAT_SECONDS):
            try:
                self._write_host("running")
                with self._lock:
                    current = self._current
                if current is not None:
                    claim = {
                        **current["claim"],
                        "heartbeat": _ts(),
                        "progress": current["progress"],
                    }
                    key = self._claim_key(current["id"])
                    if self.store.replace_if(key, json.dumps(claim).encode(), current["tag"]):
                        tagged = self.store.get_tagged(key)
                        with self._lock:
                            if self._current is current:
                                current["claim"], current["tag"] = claim, tagged[1]
                    else:
                        logger.warning("lost the claim on %s to another host", current["id"])
                if _ts() > self.deadline:
                    self._halt("deadline")
                if on_azure and _preempt_pending(vm_name):
                    self._halt("eviction notice")
            except Exception:  # noqa: BLE001 — a missed beat is survivable; a dead thread is not
                logger.exception("heartbeat failed")

    def _halt(self, reason: str) -> None:
        if not self.stop.is_set():
            logger.warning("stopping: %s", reason)
            self.stop_reason = reason
            self.stop.set()

    def _write_host(self, state: str) -> None:
        with self._lock:
            current = self._current["id"] if self._current else None
            progress = self._current["progress"] if self._current else None
        record = {
            "host": self.host,
            "state": state,
            "updated": utcnow(),
            "current": current,
            "progress": progress,
            "ran": self.ran,
            "stop_reason": self.stop_reason,
            "env": self.env,
        }
        self.store.put(
            f"{self.prefix}hosts/{self.host}.json", json.dumps(record, indent=1).encode()
        )

    # -- one shard ----------------------------------------------------------------------

    def _run_shard(self, shard: dict, claim: dict, tag: str) -> None:
        sid = shard["id"]
        outdir = self.workdir / "shards" / sid
        shutil.rmtree(outdir, ignore_errors=True)
        outdir.mkdir(parents=True)
        cachedir = self.workdir / "cache"
        cachedir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._current = {"id": sid, "claim": claim, "tag": tag, "progress": "starting"}

        def progress(msg: str) -> None:
            with self._lock:
                if self._current is not None:
                    self._current["progress"] = msg

        ctx = ShardContext(outdir=outdir, cachedir=cachedir, stop=self.stop, progress=progress)
        (outdir / "env.json").write_text(json.dumps(self.env, indent=1))
        started = time.monotonic()
        logger.info("shard %s: starting (attempt %d)", sid, claim["attempt"])
        error = None
        try:
            status = self.workload.run(shard, self.spec.params, ctx)
        except Exception:  # noqa: BLE001 — recorded on the shard
            error = traceback.format_exc()
            (outdir / "error.txt").write_text(error)
            status = 99
        seconds = round(time.monotonic() - started, 1)

        for path in sorted(outdir.rglob("*")):
            if path.is_file():
                rel = path.relative_to(outdir).as_posix()
                self.store.put_file(f"{self.prefix}shards/{sid}/{rel}", path)

        with self._lock:
            self._current = None
        key = self._claim_key(sid)
        if status >= HOST_FAILURE_STATUS:
            self.host_failures += 1
            if self.host_failures >= HOST_FAILURE_LIMIT:
                self._halt(f"{self.host_failures} shards in a row could not be measured here")
        else:
            self.host_failures = 0
        if status >= HOST_FAILURE_STATUS and claim["attempt"] < MAX_ATTEMPTS:
            logger.warning("shard %s: host failure (status %s); releasing", sid, status)
            self.store.put(key, json.dumps({**claim, "released": f"status {status}"}).encode())
            return
        if self.stop_reason:
            # Interrupted: the shard is not finished. Release it so another host can take it
            # straight away instead of waiting out the stale period.
            logger.warning("shard %s: interrupted (%s); releasing", sid, self.stop_reason)
            self.store.put(key, json.dumps({**claim, "released": self.stop_reason}).encode())
            return

        result_file = outdir / "result.json"
        result = None
        if result_file.exists() and result_file.stat().st_size < 4 << 20:
            result = json.loads(result_file.read_text())
        done = {
            "shard": sid,
            "status": status,
            "error": bool(error),
            "host": self.host,
            "attempt": claim["attempt"],
            "seconds": seconds,
            "finished": utcnow(),
            "result": result,
        }
        self.store.put(f"{self.prefix}shards/{sid}/DONE", json.dumps(done).encode())
        self.ran.append(sid)
        logger.info("shard %s: done, status %s, %.1fs", sid, status, seconds)

    # -- the loop -----------------------------------------------------------------------

    def run(self) -> int:
        self._write_host("running")
        beat = threading.Thread(target=self._beat, name="heartbeat", daemon=True)
        beat.start()
        idle_since = None
        try:
            while not self.stop.is_set():
                if _ts() > self.deadline:
                    self._halt("deadline")
                    break
                if self.max_shards and len(self.ran) >= self.max_shards:
                    break
                ran_one = False
                for shard in self.spec.shards:
                    if self.stop.is_set() or (self.max_shards and len(self.ran) >= self.max_shards):
                        break
                    if self._done(shard["id"]):
                        continue
                    claimed = self._try_claim(shard["id"])
                    if claimed:
                        self._run_shard(shard, *claimed)
                        ran_one = True
                if ran_one:
                    idle_since = None
                    continue
                free, active = self._claimable_or_active()
                if not free and not active:
                    logger.info("every shard is done")
                    break
                idle_since = idle_since or time.monotonic()
                if time.monotonic() - idle_since > self.idle_seconds:
                    logger.info(
                        "idle for %.0fs with %d shard(s) held elsewhere", self.idle_seconds, active
                    )
                    break
                self.stop.wait(min(60.0, self.stale_seconds / 2))
        finally:
            self.stop.set()
            beat.join(timeout=5)
            self._write_host("finished")
        return 0


def _preempt_pending(vm_name: Optional[str]) -> bool:
    request = urllib.request.Request(SCHEDULED_EVENTS_URL, headers={"Metadata": "true"})
    try:
        with urllib.request.urlopen(request, timeout=2.0) as response:
            events = json.loads(response.read()).get("Events", [])
    except (OSError, ValueError):
        return False
    for event in events:
        if event.get("EventType") in ("Preempt", "Terminate") and (
            not vm_name or vm_name in event.get("Resources", [])
        ):
            return True
    return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.bench_infra.runner")
    parser.add_argument("--store", required=True, help="directory, or https:// container SAS URL")
    parser.add_argument("--job", required=True)
    parser.add_argument("--host", default=socket.gethostname())
    parser.add_argument("--workdir", default="bench-work")
    parser.add_argument("--stale-minutes", type=float, default=5.0)
    parser.add_argument("--idle-minutes", type=float, default=None)
    parser.add_argument(
        "--max-shards", type=int, default=0, help="stop after this many (0: no limit)"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    idle = args.idle_minutes if args.idle_minutes is not None else args.stale_minutes * 2 + 1
    runner = Runner(
        open_store(args.store),
        args.job,
        host=args.host,
        workdir=Path(args.workdir).resolve(),
        stale_seconds=args.stale_minutes * 60,
        idle_seconds=idle * 60,
        max_shards=args.max_shards,
    )
    return runner.run()


if __name__ == "__main__":
    raise SystemExit(main())
