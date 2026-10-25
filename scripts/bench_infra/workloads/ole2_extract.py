"""``ole2-extract`` — does JMFTS's legacy-office path work on real files, and how fast?

The path measured is the appliance's own, step for step, so the numbers answer "what would
ingest do" rather than "what would LibreOffice do":

1. ``probe.detect_format`` + ``probe.probe_patterns`` — is it OLE2, is it encrypted, and which
   application's binary is it (``legacy_application``).
2. ``convert.convert_bytes(data, to=OOXML_TARGET[app])`` — the ``convert:ooxml`` task's call,
   through unoserver at ``$JMFTS_CONVERT_URL`` (``Dockerfile.libreoffice`` on Azure).
3. ``office.extract.docx_to_markdown`` / ``pptx_to_markdown``, or ``office.workbook
   .read_sheets`` + ``office.sheets.measure_sheet`` per sheet for a workbook — what the
   extract rungs read.

One JSON line per OLE2 file in ``files.jsonl``, with the time of each step and the error
that stopped it if one did. Files that are not OLE2 are counted by detected format and not
processed, so a shard's denominator is in its own result.

SOURCES, each a shard or a run of shards:

``govdocs1-NNN``  ``zipfiles/NNN.zip`` of the digitalcorpora Govdocs1 million-file corpus,
                  ~333 MB and ~1000 files each, read from public S3 with no credentials.
``poi``           Apache POI's ``test-data/``, at a pinned commit of gitbox's ``poi.git``.
``lo-sw`` etc.    LibreOffice's ``sw/qa``, ``sc/qa``, ``sd/qa`` at a pinned ``core`` commit.

POI and LibreOffice keep test files that are malformed on purpose. Their failure rate is a
reading about the hard tail, not about a corpus; report them apart from Govdocs1.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
import urllib.request
import zipfile
from collections import Counter
from pathlib import Path
from typing import Iterator

from scripts.bench_infra.workloads import ShardContext, Workload

GOVDOCS_URL = "https://digitalcorpora.s3.amazonaws.com/corpora/files/govdocs1/zipfiles/{n:03d}.zip"
POI_GIT = "https://gitbox.apache.org/repos/asf/poi.git"
POI_COMMIT = "5c44755a2a7569a5884dc8f1f680ca3a86fa245f"  # trunk, read 2026-10-25
LO_GIT = "https://git.libreoffice.org/core"
LO_COMMIT = "0ea8f79d15a62dfb5a0325640ed5771fc8b75ef2"  # master, read 2026-10-25
LO_DIRS = {"lo-sw": "sw/qa", "lo-sc": "sc/qa", "lo-sd": "sd/qa"}
OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

#: Larger than any file in the sources; a guard against reading something absurd into RAM.
MAX_FILE_BYTES = 256 << 20


def _parse_range(text: str) -> list[int]:
    out: list[int] = []
    for part in text.split(","):
        lo, _, hi = part.partition("-")
        out.extend(range(int(lo), int(hi or lo) + 1))
    return out


class Ole2Extract(Workload):
    name = "ole2-extract"
    description = "probe → convert:ooxml → extract, per legacy OLE2 file, timed"
    host = {"libreoffice": True, "extras": ["office", "convert"]}

    def add_arguments(self, parser):
        parser.add_argument(
            "--govdocs", default="0-999", help="Govdocs1 zip numbers, e.g. 0-99 or 0,5,7-9"
        )
        parser.add_argument("--no-govdocs", action="store_true")
        parser.add_argument("--poi", action=argparse.BooleanOptionalAction, default=True)
        parser.add_argument("--lo", action=argparse.BooleanOptionalAction, default=True)
        parser.add_argument("--limit", type=int, default=0, help="OLE2 files per shard (0: all)")

    def plan(self, args):
        shards = []
        if not args.no_govdocs:
            shards += [
                {"id": f"govdocs1-{n:03d}", "source": "govdocs1", "n": n}
                for n in _parse_range(args.govdocs)
            ]
        if args.poi:
            shards.append({"id": "poi", "source": "poi"})
        if args.lo:
            shards += [{"id": k, "source": "lo", "dir": v} for k, v in LO_DIRS.items()]
        params = {"limit": args.limit, "poi_commit": POI_COMMIT, "lo_commit": LO_COMMIT}
        return params, shards

    # -- sources ------------------------------------------------------------------------

    def _files(self, shard, params, ctx: ShardContext) -> Iterator[tuple[str, bytes]]:
        if shard["source"] == "govdocs1":
            yield from self._govdocs(shard["n"], ctx)
        elif shard["source"] == "poi":
            root = _clone(ctx.cachedir / "poi", POI_GIT, params["poi_commit"], ["test-data"])
            yield from _walk(root / "test-data", root)
        else:
            root = _clone(ctx.cachedir / "lo", LO_GIT, params["lo_commit"], list(LO_DIRS.values()))
            yield from _walk(root / shard["dir"], root)

    def _govdocs(self, n: int, ctx: ShardContext) -> Iterator[tuple[str, bytes]]:
        path = ctx.cachedir / f"govdocs1-{n:03d}.zip"
        started = time.monotonic()
        ctx.progress(f"downloading govdocs1 {n:03d}")
        with urllib.request.urlopen(GOVDOCS_URL.format(n=n), timeout=600) as response:
            with open(path, "wb") as fh:
                while chunk := response.read(1 << 22):
                    fh.write(chunk)
        (ctx.outdir / "download.json").write_text(
            json.dumps({"bytes": path.stat().st_size, "seconds": time.monotonic() - started})
        )
        try:
            with zipfile.ZipFile(path) as archive:
                for info in archive.infolist():
                    if not info.is_dir() and info.file_size <= MAX_FILE_BYTES:
                        yield info.filename, archive.read(info)
        finally:
            path.unlink(missing_ok=True)

    # -- one file -----------------------------------------------------------------------

    def run(self, shard, params, ctx: ShardContext) -> int:
        formats: Counter = Counter()
        outcomes: Counter = Counter()
        processed = 0
        started = time.monotonic()
        with open(ctx.outdir / "files.jsonl", "w") as out:
            for name, data in self._files(shard, params, ctx):
                if ctx.stop.is_set():
                    break
                if not data.startswith(OLE2_MAGIC):
                    formats[_quick_format(name, data)] += 1
                    continue
                formats["ole2"] += 1
                if params["limit"] and processed >= params["limit"]:
                    continue
                record = measure_one(name, data)
                out.write(json.dumps(record, sort_keys=True) + "\n")
                outcomes[record["outcome"]] += 1
                processed += 1
                if processed % 25 == 0:
                    out.flush()
                    ctx.progress(f"{processed} ole2 files")
        (ctx.outdir / "result.json").write_text(
            json.dumps(
                {
                    "formats": dict(formats),
                    "outcomes": dict(outcomes),
                    "processed": processed,
                    "seconds": round(time.monotonic() - started, 1),
                    "interrupted": ctx.stop.is_set(),
                }
            )
        )
        return 0

    def summarize(self, results):
        outcomes: Counter = Counter()
        formats: Counter = Counter()
        by_source: dict = {}
        for shard_id, record in results.items():
            result = record.get("result") or {}
            source = shard_id.split("-")[0]
            bucket = by_source.setdefault(source, Counter())
            bucket.update(result.get("outcomes") or {})
            outcomes.update(result.get("outcomes") or {})
            formats.update(result.get("formats") or {})
        return {
            "outcomes": dict(outcomes),
            "by_source": {k: dict(v) for k, v in by_source.items()},
            "formats": dict(formats.most_common(20)),
        }


def measure_one(name: str, data: bytes) -> dict:
    """Run one OLE2 file through the appliance's path and say how far it got and how fast.

    ``outcome`` is the furthest step reached: ``probe_error``, ``encrypted``,
    ``not_legacy`` (an MSI, a Thumbs.db, a .msg), ``convert_error``, ``extract_error``,
    ``empty`` (extracted, no text) or ``ok``.
    """
    from jmfts_core.convert import OOXML_TARGET, convert_bytes
    from jmfts_core.probe import detect_format, probe_patterns

    record: dict = {
        "name": name,
        "ext": Path(name).suffix.lower().lstrip("."),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest()[:16],
    }

    def timed(step: str, fn):
        t0 = time.perf_counter()
        try:
            return fn()
        finally:
            record[f"{step}_ms"] = round((time.perf_counter() - t0) * 1000, 1)

    try:
        detection = timed("detect", lambda: detect_format(data, filename=name))
        patterns, _detail = timed("probe", lambda: probe_patterns(data, detection))
    except Exception as exc:  # noqa: BLE001 — the reading IS the exception
        return {**record, "outcome": "probe_error", "error": _err(exc)}
    record["encrypted"] = bool(patterns.get("is_encrypted"))
    application = patterns.get("legacy_application")
    record["application"] = application
    if record["encrypted"]:
        return {**record, "outcome": "encrypted"}
    if application not in OOXML_TARGET:
        return {**record, "outcome": "not_legacy"}

    target = OOXML_TARGET[application]
    try:
        converted = timed("convert", lambda: convert_bytes(data, to=target))
    except Exception as exc:  # noqa: BLE001
        return {**record, "outcome": "convert_error", "error": _err(exc)}
    record["ooxml_bytes"] = len(converted)

    try:
        text, extra = timed("extract", lambda: _extract(target, converted))
    except Exception as exc:  # noqa: BLE001
        return {**record, "outcome": "extract_error", "error": _err(exc)}
    record.update(extra)
    record["chars"] = len(text)
    record["words"] = len(text.split())
    return {**record, "outcome": "ok" if text.strip() else "empty"}


def _extract(target: str, data: bytes) -> tuple[str, dict]:
    if target == "docx":
        from jmfts_core.office.extract import docx_to_markdown

        text, _ = docx_to_markdown(data)
        return text, {}
    if target == "pptx":
        from jmfts_core.office.extract import pptx_to_markdown

        text, _ = pptx_to_markdown(data)
        return text, {}
    from jmfts_core.config import get_settings
    from jmfts_core.office.sheets import measure_sheet
    from jmfts_core.office.workbook import read_sheets

    budget = get_settings().embedding_doc_window
    parts, cells, sheet_errors = [], 0, 0
    sheets = read_sheets(data)
    for sheet in sheets:
        try:
            measured = measure_sheet(
                data, sheet.name, render_cell_budget=budget, with_sketches=False
            )
        except Exception:  # noqa: BLE001 — a chartsheet or an oversized sheet, counted
            sheet_errors += 1
            continue
        cells += measured.non_empty_cells
        parts.append(measured.rendered_markdown or "")
    return "\n\n".join(parts), {"sheets": len(sheets), "sheet_errors": sheet_errors, "cells": cells}


def _err(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]


def _quick_format(name: str, data: bytes) -> str:
    if data.startswith(b"%PDF"):
        return "pdf"
    if data.startswith(b"PK\x03\x04"):
        return "zip/ooxml"
    return Path(name).suffix.lower().lstrip(".") or "none"


def _clone(dest: Path, url: str, commit: str, paths: list[str]) -> Path:
    """A shallow, sparse checkout of ``paths`` at ``commit``, once per host."""
    marker = dest / ".bench-commit"
    if marker.exists() and marker.read_text() == commit:
        return dest
    dest.mkdir(parents=True, exist_ok=True)
    run = lambda *args: subprocess.run(["git", "-C", str(dest), *args], check=True)  # noqa: E731
    if not (dest / ".git").exists():
        run("init", "-q")
        run("remote", "add", "origin", url)
    run("sparse-checkout", "set", *paths)
    run("fetch", "-q", "--depth", "1", "origin", commit)
    run("checkout", "-q", "FETCH_HEAD")
    marker.write_text(commit)
    return dest


def _walk(root: Path, base: Path) -> Iterator[tuple[str, bytes]]:
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.stat().st_size <= MAX_FILE_BYTES:
            yield path.relative_to(base).as_posix(), path.read_bytes()


WORKLOAD = Ole2Extract()
