#!/usr/bin/env python3
"""How large is a sheet rendered as one markdown table? INGEST_SPEC.md 8.4 `small_table`.

8.4 says a sheet is a ``small_table`` when "``rendered_tokens`` fits inside the embedding
window", and this appliance has TWO — 512 on the token/MaxSim path and 8192 on the
document-vector path (``sheet_profile.sheet_evidence_block`` records both for exactly this
reason). Which one 8.4 means changes ``small_table``'s reach, and nothing had measured the
difference. This pass does.

It is ``run_profile_sheet`` minus the tree write: ``measure_sheet`` with
``render_cell_budget = embedding_doc_window``, then ``check_fit(rendered, with_tokens=True)``
for the count. The count does not depend on which window is asked about — only the verdict
does — so ONE count per sheet answers both, and the report sweeps the threshold rather than
choosing it.

    python -m scripts.render_tokens datasets/corpus-fuse --out fuse_render.jsonl
    python -m scripts.render_tokens --report fuse_render.jsonl

The tokenizer is loaded per worker and never the model weights (``EmbeddingService.tokenizer``
is a lazy ``AutoTokenizer``), so this runs on a base install with no torch. It does need the
model's tokenizer files in the local Hugging Face cache.

Every failure is recorded by class rather than skipped, for the reason ``sheet_yield`` gives:
a survey that drops the files it could not read reports a rate over the files it liked.
"""

import argparse
import collections
import io
import json
import os
import signal
import statistics
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

READABLE = {".xlsx", ".xlsm"}
MAX_BYTES = 128 * 1024 * 1024

#: Same bound and same reason as `sheet_yield.DEFAULT_SHEET_TIMEOUT`: `measure_sheet`
#: iterates the DECLARED rectangle with no ceiling, so a 5 KB workbook declaring 2.75e11
#: cells pins a worker indefinitely. This is the survey's bound, not the appliance's —
#: the appliance has none, which is Tier 0 item 0.1 of the roadmap.
DEFAULT_SHEET_TIMEOUT = 120

#: The thresholds the report sweeps. 512 and 8192 are this appliance's two windows; the
#: rest are there so the shape of the distribution between them is visible, because if
#: nearly everything that fits 8192 also fits 512 then 8.4's ambiguity does not matter.
SWEEP = (128, 256, 512, 1024, 2048, 4096, 8192)


class SheetTimeout(Exception):
    """Raised into the worker by SIGALRM when one sheet outruns the budget."""


def _alarm(_signum, _frame):
    raise SheetTimeout("sheet measurement exceeded the per-sheet timeout")


_TOKENIZER = None
_PREFIX = "search_document: "


def _tokenizer(model_name):
    """One tokenizer per worker process. No weights; see the module docstring."""
    global _TOKENIZER
    if _TOKENIZER is None:
        from transformers import AutoTokenizer

        _TOKENIZER = AutoTokenizer.from_pretrained(model_name)
    return _TOKENIZER


def _count(model_name, text):
    """Exactly `EmbeddingService.check_fit(text, with_tokens=True).token_count`.

    The retrieval prefix and the special tokens are part of what has to fit, so a bare
    count of the table's own tokens would say a table fits that does not.
    """
    tk = _tokenizer(model_name)
    return len(tk(_PREFIX + text, add_special_tokens=True)["input_ids"])


def scan_book(args):
    path, budget, model_name, timeout = args
    from jmfts_core.office.sheets import measure_sheet

    try:
        if os.path.getsize(path) > MAX_BYTES:
            return [], [{"path": path, "error": "too-large"}]
        with open(path, "rb") as handle:
            data = handle.read()
        import openpyxl

        book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        names = list(book.sheetnames)
        book.close()
    except Exception as exc:  # noqa: BLE001 - survey: every class is a corpus fact
        return [], [{"path": path, "error": f"open/{exc.__class__.__name__}"}]

    previous = signal.signal(signal.SIGALRM, _alarm)
    out, errors = [], []
    try:
        for name in names:
            signal.alarm(timeout)
            try:
                m = measure_sheet(data, name, render_cell_budget=budget, with_sketches=False)
            except SheetTimeout:
                errors.append({"path": path, "sheet": name, "error": "timeout"})
                continue
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    {"path": path, "sheet": name, "error": f"measure/{exc.__class__.__name__}"}
                )
                continue
            finally:
                signal.alarm(0)

            tokens, token_error = None, None
            if m.rendered_markdown is not None:
                signal.alarm(timeout)
                try:
                    tokens = _count(model_name, m.rendered_markdown)
                except SheetTimeout:
                    token_error = "timeout"
                except Exception as exc:  # noqa: BLE001
                    token_error = f"tokenize/{exc.__class__.__name__}"
                finally:
                    signal.alarm(0)

            out.append(
                {
                    "path": path,
                    "sheet": name,
                    "rows": m.rows,
                    "cols": m.cols,
                    "non_empty": m.non_empty_cells,
                    "fill_ratio": m.fill_ratio,
                    "header_row": m.header_row.verdict,
                    "header_row_number": m.header_row.row,
                    "header_col": m.header_col.verdict,
                    "merged": m.merged_cells,
                    # `None` means the sheet never rendered: `measure_sheet` stopped
                    # keeping rows once the token FLOOR passed the budget, and the reason
                    # travels with it. That is not the same as "rendered and too big".
                    "tokens": tokens,
                    "unbounded": m.rendered_unbounded_reason,
                    "token_error": token_error,
                    "chars": None if m.rendered_markdown is None else len(m.rendered_markdown),
                }
            )
    finally:
        signal.signal(signal.SIGALRM, previous)
    return out, errors


def run(roots, out_path, workers, budget, model_name, timeout, limit):
    files = []
    for root in roots:
        for dirpath, _dirs, names in os.walk(root):
            for name in sorted(names):
                if os.path.splitext(name)[1].lower() in READABLE:
                    files.append(os.path.join(dirpath, name))
    if limit:
        files = files[:limit]
    print(
        f"{len(files)} workbook(s); render budget {budget} cells, tokenizer {model_name}",
        file=sys.stderr,
    )
    done = 0
    with open(out_path, "w") as out:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(scan_book, (p, budget, model_name, timeout)) for p in files]
            for future in as_completed(futures):
                done += 1
                rows, errors = future.result()
                for row in rows + errors:
                    out.write(json.dumps(row) + "\n")
                if done % 500 == 0:
                    print(f"  {done}/{len(files)}", file=sys.stderr)
    return out_path


def report(path):
    sheets, errors = [], collections.Counter()
    for line in open(path):
        row = json.loads(line)
        if "error" in row:
            errors[row["error"]] += 1
        else:
            sheets.append(row)
    n = len(sheets)
    if not n:
        print("no sheets")
        return
    rendered = [s for s in sheets if s["tokens"] is not None]
    never = [s for s in sheets if s["tokens"] is None and s["unbounded"]]
    failed = [s for s in sheets if s["tokens"] is None and not s["unbounded"]]

    print(f"\n# {n} sheet(s), {sum(errors.values())} failure row(s)")
    print(f"  rendered and counted        {len(rendered):7d} {100*len(rendered)/n:6.1f}%")
    print(f"  never rendered (over floor) {len(never):7d} {100*len(never)/n:6.1f}%")
    if failed:
        print(f"  rendered, count failed      {len(failed):7d} {100*len(failed)/n:6.1f}%")

    counts = sorted(s["tokens"] for s in rendered)
    if counts:
        qs = statistics.quantiles(counts, n=100) if len(counts) > 2 else counts
        print("\n# rendered token count, over the sheets that rendered")
        marks = [("p10", 9), ("p25", 24), ("p50", 49), ("p75", 74), ("p90", 89), ("p99", 98)]
        line = "  " + "  ".join(
            f"{label} {qs[i] if len(qs) > i else counts[-1]:>7.0f}" for label, i in marks
        )
        print(line)
        print(f"  min {counts[0]}  max {counts[-1]}  mean {statistics.mean(counts):.0f}")

    print("\n# `small_table` reach, by which window 8.4 means")
    print(f"  {'window':>8s} {'sheets':>8s} {'of all':>8s}  {'no header_row':>14s} {'gain':>8s}")
    no_header = [s for s in rendered if not s["header_row"]]
    for limit in SWEEP:
        fits = [s for s in rendered if s["tokens"] <= limit]
        new = [s for s in fits if not s["header_row"]]
        print(
            f"  {limit:8d} {len(fits):8d} {100*len(fits)/n:7.1f}% "
            f"{len(new):14d} {100*len(new)/n:7.1f}%"
        )
    print(
        f"  (all rendered sheets with no header_row: {len(no_header)}, "
        f"{100*len(no_header)/n:.1f}% of all sheets)"
    )

    print("\n# the two windows against each other")
    fits512 = {(s["path"], s["sheet"]) for s in rendered if s["tokens"] <= 512}
    fits8192 = {(s["path"], s["sheet"]) for s in rendered if s["tokens"] <= 8192}
    between = fits8192 - fits512
    print(f"  fits 512                  {len(fits512):7d} {100*len(fits512)/n:6.1f}%")
    print(f"  fits 8192                 {len(fits8192):7d} {100*len(fits8192)/n:6.1f}%")
    print(f"  fits 8192 but not 512     {len(between):7d} {100*len(between)/n:6.1f}%")

    print("\n# what a sheet in the 512..8192 band looks like (median)")
    band = [s for s in rendered if 512 < s["tokens"] <= 8192]
    small = [s for s in rendered if s["tokens"] <= 512]
    for label, group in (("<= 512", small), ("512..8192", band)):
        if not group:
            continue
        print(
            f"  {label:>10s}  n={len(group):6d}  rows={statistics.median(s['rows'] for s in group):6.0f}"
            f"  cols={statistics.median(s['cols'] for s in group):5.0f}"
            f"  non_empty={statistics.median(s['non_empty'] for s in group):7.0f}"
            f"  header_row={100*sum(1 for s in group if s['header_row'])/len(group):5.1f}%"
        )

    print("\n# the ordering question: `small_table` against `records`, per sheet")
    for limit in (512, 8192):
        fits = [s for s in rendered if s["tokens"] <= limit]
        both = sum(1 for s in fits if s["header_row"])
        only_small = sum(1 for s in fits if not s["header_row"])
        header_all = sum(1 for s in sheets if s["header_row"])
        neither = n - len({(s["path"], s["sheet"]) for s in fits}) - (header_all - both)
        print(
            f"  window {limit:5d}:  both shapes match {both:6d} ({100*both/n:4.1f}%)   "
            f"small_table only {only_small:6d} ({100*only_small/n:4.1f}%)   "
            f"records only {header_all-both:6d} ({100*(header_all-both)/n:4.1f}%)   "
            f"neither {neither:6d} ({100*neither/n:4.1f}%)"
        )

    print("\n# why a sheet never rendered")
    for key, count in collections.Counter(s["unbounded"] for s in never).most_common(5):
        print(f"  {count:7d}  {key}")

    print("\n# failures")
    for key, count in errors.most_common(10):
        print(f"  {count:7d}  {key}")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("roots", nargs="*")
    ap.add_argument("--out", default="render_tokens.jsonl")
    ap.add_argument("--timeout", type=int, default=DEFAULT_SHEET_TIMEOUT)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) // 2))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--report", metavar="JSONL")
    args = ap.parse_args()
    if args.report:
        return report(args.report)
    if not args.roots:
        ap.error("give at least one corpus root, or --report a JSONL")
    from jmfts_core.config import get_settings

    settings = get_settings()
    report(
        run(
            args.roots,
            args.out,
            args.workers,
            settings.embedding_doc_window,
            settings.embedding_model,
            args.timeout,
            args.limit,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
