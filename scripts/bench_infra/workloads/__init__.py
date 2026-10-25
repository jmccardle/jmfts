"""The measurements bench_infra knows how to shard. One module each; ``WORKLOADS`` is the table.

A workload answers three questions and no others:

* ``plan(args)`` — what are the shards? Returns ``(params, shards)``; every shard is a dict
  with a unique ``id`` that is safe as a path component.
* ``host`` — what must a host have before it can run one? A dict the Azure bootstrap reads
  (``postgres``, ``libreoffice``, ``extras``) and the local backend only checks.
* ``run(shard, params, ctx)`` — run ONE shard into ``ctx.outdir`` and return an exit status.

A shard must be safe to run twice: an evicted host's shard is re-run from the start by
whoever takes it over, so a workload writes into ``ctx.outdir`` (fresh each attempt) and
nowhere else that matters.
"""

from __future__ import annotations

import argparse
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class ShardContext:
    #: Fresh, empty, and uploaded wholesale when the shard ends.
    outdir: Path
    #: Survives across shards on one host: downloads, clones, model weights.
    cachedir: Path
    #: Set when the host is going away (eviction notice, deadline). Long loops check it.
    stop: threading.Event
    #: Call with a short string now and then; it lands in the claim's heartbeat.
    progress: Callable[[str], None] = field(default=lambda _msg: None)


class Workload:
    name: str = ""
    description: str = ""
    host: dict = {}

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        pass

    def plan(self, args: argparse.Namespace) -> tuple[dict, list[dict]]:
        raise NotImplementedError

    def run(self, shard: dict, params: dict, ctx: ShardContext) -> int:
        raise NotImplementedError

    def summarize(self, results: dict[str, dict]) -> dict:
        """Fold ``{shard_id: done_record}`` into a job-level summary. Optional."""
        return {}


def _load() -> dict[str, Workload]:
    from scripts.bench_infra.workloads import beir, ole2_extract, recall_gate, smoke

    table = {}
    for module in (smoke, recall_gate, ole2_extract, beir):
        workload = module.WORKLOAD
        table[workload.name] = workload
    return table


WORKLOADS: dict[str, Workload] = _load()
