"""Backends put hosts in front of a job and take them away. They never schedule shards.

A backend answers:

* ``store()`` — where its jobs live (a directory; a blob container).
* ``launch(spec, hosts, args)`` — start ``hosts`` runners against a job already in the store.
* ``hosts(job_id)`` — what it has running for the job, as the backend sees it.
* ``teardown(job_id)`` — remove everything it started for the job.

``add_arguments`` adds the backend's own launch options (regions, VM size, spot).
"""

from __future__ import annotations

import argparse

from scripts.bench_infra.spec import JobSpec
from scripts.bench_infra.store import Store


class Backend:
    name = ""

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        pass

    def store(self) -> Store:
        raise NotImplementedError

    def launch(self, spec: JobSpec, hosts: int, args: argparse.Namespace) -> None:
        raise NotImplementedError

    def hosts(self, job_id: str) -> list[dict]:
        return []

    def teardown(self, job_id: str) -> None:
        pass


def get_backend(name: str) -> Backend:
    if name == "local":
        from scripts.bench_infra.backends.local import LocalBackend

        return LocalBackend()
    if name == "azure":
        from scripts.bench_infra.backends.azure import AzureBackend

        return AzureBackend()
    raise SystemExit(f"unknown backend {name!r}; known: local, azure")
