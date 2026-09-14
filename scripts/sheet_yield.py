#!/usr/bin/env python3
"""Closed-set yield: how database-like is a real spreadsheet, measured over a corpus.

Runs ``measure_sheet`` (INGEST_SPEC.md 8.3) over every workbook under one or more roots
and writes ONE JSONL ROW PER COLUMN. No database, no model, no LLM — the same pass
``profile:sheet`` makes, minus the tokenizer call and the tree write.

The point of the JSONL is that every question is asked at report time, over stored
measurements, rather than by re-reading blobs. That is the same argument
``sheet_evidence_block`` makes for keeping denominators beside ratios: 8.8's thresholds
are unset pending calibration against real workbooks, ``shape_decision.inputs`` gathers
what 8.4's branches read, and this is the corpus side of the same sweep.

Usage:
    python -m scripts.sheet_yield datasets/spreadsheetbench datasets/poi-testdata \\
        --out /tmp/yield.jsonl --workers 8
    python -m scripts.sheet_yield --report /tmp/yield.jsonl

Reading a workbook is fallible in every way an adversarial corpus can arrange, and every
failure is RECORDED BY CLASS rather than skipped: a survey that silently drops the files
it could not read reports a yield over the files it liked. The failure rows carry
``error`` and no measurements, and the report counts them against the denominator.
"""

import argparse
import collections
import io
import json
import os
import signal
import statistics
import sys
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: What ``openpyxl`` reads. ``.xlsb`` is a binary part format it does not open and
#: ``.ods`` is a different package entirely; both are counted as skipped rather than
#: silently absent, because "no yield measured" and "not in the corpus" differ.
READABLE = {".xlsx", ".xlsm"}
UNREADABLE = {".xlsb", ".ods", ".fods", ".xls"}

#: A cap on the bytes handed to `measure_sheet`, so one pathological workbook cannot
#: stall the pass. Files past it are recorded with `error = "too-large"`, not dropped.
MAX_BYTES = 128 * 1024 * 1024

#: Wall-clock seconds one SHEET may take, and file size does not predict it.
#: `datasets/lo-qa/sc/qa/unit/data/xlsx/too-many-cols-rows.xlsx` is 5,526 bytes, holds
#: five cells, and declares `<dimension ref="A1:XFE16777217"/>` — 2.75e11 cells.
#: `measure_sheet` iterates the DECLARED rectangle, so that file consumed 54 minutes of
#: CPU in one worker and had not finished. The timeout is this survey's own bound and
#: says nothing about the appliance, which has no equivalent; see the report.
DEFAULT_SHEET_TIMEOUT = 120


class SheetTimeout(Exception):
    """Raised into the worker by SIGALRM when one sheet outruns the budget."""


def _alarm(_signum, _frame):
    raise SheetTimeout("sheet measurement exceeded the per-sheet timeout")


def walk(roots):
    for root in roots:
        for dirpath, _dirs, names in os.walk(root):
            for name in sorted(names):
                ext = os.path.splitext(name)[1].lower()
                if ext in READABLE:
                    yield os.path.join(dirpath, name), ext
                elif ext in UNREADABLE:
                    yield os.path.join(dirpath, name), ext


def measure_one(args):
    """One workbook -> (rows, error_rows). Runs in a worker process."""
    path, ext, render_cell_budget, timeout = args
    if ext in UNREADABLE:
        return [], [{"path": path, "ext": ext, "error": "unreadable-format"}]
    try:
        size = os.path.getsize(path)
        if size > MAX_BYTES:
            return [], [{"path": path, "ext": ext, "error": "too-large", "bytes": size}]
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        return [], [{"path": path, "ext": ext, "error": f"OSError/{exc.__class__.__name__}"}]

    from jmfts_core.office.sheets import measure_sheet

    try:
        import openpyxl

        book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        names = list(book.sheetnames)
        book.close()
    except Exception as exc:  # noqa: BLE001 - survey: report every class
        return [], [{"path": path, "ext": ext, "error": f"open/{exc.__class__.__name__}",
                     "message": str(exc)[:300]}]

    previous = signal.signal(signal.SIGALRM, _alarm)
    rows, errors = [], []
    for name in names:
        signal.alarm(timeout)
        try:
            # `with_sketches=False`: a MinHash per column costs a hash per value and
            # nothing in this pass compares two sketches. `propose:links` is the consumer
            # and it is not implemented, so paying for them here would measure nothing.
            m = measure_sheet(data, name, render_cell_budget=render_cell_budget,
                              with_sketches=False)
        except SheetTimeout:
            errors.append({"path": path, "ext": ext, "sheet": name, "error": "timeout",
                           "message": f"exceeded {timeout}s in measure_sheet"})
            continue
        except Exception as exc:  # noqa: BLE001
            errors.append({"path": path, "ext": ext, "sheet": name,
                           "error": f"measure/{exc.__class__.__name__}",
                           "message": str(exc)[:300]})
            continue
        finally:
            signal.alarm(0)
        sheet_common = {
            "path": path,
            "ext": ext,
            "sheet": name,
            "sheets_in_book": len(names),
            "rows": m.rows,
            "cols": m.cols,
            "fill_ratio": m.fill_ratio,
            "header_row": m.header_row.verdict,
            # The header scan's two new facts. `header_row` stayed a boolean, so every
            # count below it reads as it did; these say WHERE it was found and under which
            # of the two passes, which is the difference between the 23.9% this script
            # first measured and the 54.4% the scan reaches.
            "header_row_number": m.header_row.row,
            "header_row_rule": m.header_row.rule,
            "header_col": m.header_col.verdict,
            # The components, so a sweep can redefine the verdict with no blob read.
            # `leading_empty` is the crossing-table corner `HeaderEvidence` records.
            "header_row_all_text": m.header_row.evidence.all_text,
            "header_row_all_distinct": m.header_row.evidence.all_distinct,
            "header_row_all_non_empty": m.header_row.evidence.all_non_empty,
            "header_row_leading_empty": m.header_row.evidence.leading_empty,
            "header_row_numeric_cells": m.header_row.evidence.numeric_cells,
            "rendered": m.rendered_markdown is not None,
            "rendered_unbounded_reason": m.rendered_unbounded_reason,
            "interior_cardinality": m.interior_cardinality,
            "interior_cardinality_exact": m.interior_cardinality_exact,
            "merged_cells": m.merged_cells,
            "declared_rows": m.declared_rows,
            "declared_cols": m.declared_cols,
        }
        if not m.columns:
            rows.append(dict(sheet_common, column=None))
            continue
        for c in m.columns:
            rows.append(dict(
                sheet_common,
                column=c.index,
                letter=c.letter,
                name=c.name,
                dominant_type=c.dominant_type,
                non_empty=c.non_empty,
                body_rows=c.body_rows,
                col_fill_ratio=c.fill_ratio,
                distinct_count=c.distinct_count,
                distinct_at_least=c.distinct_at_least,
                distinct_exact=c.distinct_exact,
                is_unique=c.is_unique,
                values_retained=None if c.values is None else len(c.values),
                # The discriminator between a closed set and an identifier, kept as its
                # parts. An open question rides on this ratio (is `Customer_ID` a key or a
                # measurement?) and no threshold is chosen here.
                distinct_ratio=(
                    None if c.distinct_count is None or not c.body_rows
                    else c.distinct_count / c.body_rows
                ),
            ))
    signal.signal(signal.SIGALRM, previous)
    return rows, errors


def run(roots, out_path, workers, limit, timeout):
    from jmfts_core.config import get_settings

    budget = get_settings().embedding_doc_window
    files = list(walk(roots))
    if limit:
        files = files[:limit]
    print(f"{len(files)} workbook(s) under {', '.join(roots)}; "
          f"render_cell_budget={budget}, per-sheet timeout={timeout}s", file=sys.stderr)

    n_rows = n_err = done = 0
    with open(out_path, "w") as out:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(measure_one, (p, e, budget, timeout)): p
                       for p, e in files}
            for future in as_completed(futures):
                done += 1
                try:
                    rows, errors = future.result()
                except Exception:  # noqa: BLE001 - a worker died; say which file
                    print(f"\nworker died on {futures[future]}", file=sys.stderr)
                    traceback.print_exc()
                    continue
                for row in rows:
                    out.write(json.dumps(row) + "\n")
                for row in errors:
                    out.write(json.dumps(row) + "\n")
                n_rows += len(rows)
                n_err += len(errors)
                if done % 250 == 0:
                    print(f"  {done}/{len(files)} files, {n_rows} column rows, "
                          f"{n_err} failures", file=sys.stderr)
    print(f"wrote {n_rows} column rows and {n_err} failure rows to {out_path}",
          file=sys.stderr)
    return out_path


# ---------------------------------------------------------------------------
# The report. Every threshold here is a SWEEP, not a chosen value: 8.8 leaves them
# unset and this pass is the evidence for choosing them, so it must not pre-empt one.
# ---------------------------------------------------------------------------

CLOSED_SET_SWEEP = (2, 3, 5, 10, 20, 50, 100, 250, 1000)


def report(path):
    sheets, columns, errors = {}, [], collections.Counter()
    error_example = {}
    for line in open(path):
        row = json.loads(line)
        if "error" in row:
            errors[row["error"]] += 1
            error_example.setdefault(row["error"], row)
            continue
        key = (row["path"], row["sheet"])
        if key not in sheets:
            sheets[key] = row
        if row.get("column") is not None:
            columns.append(row)

    books = {p for p, _s in sheets}
    print(f"\n# corpus\n  {len(books)} workbook(s), {len(sheets)} sheet(s), "
          f"{len(columns)} column(s), {sum(errors.values())} failure row(s)")

    def pct(n, d):
        return f"{100.0 * n / d:5.1f}%" if d else "    —"

    n = len(sheets)
    hdr = sum(1 for s in sheets.values() if s["header_row"])
    print("\n# the header rule (INGEST_SPEC 8.3) — it gates extract:sheet entirely")
    print(f"  header_row true                 {hdr:7d}  {pct(hdr, n)}")
    for label, key in (
        ("all_non_empty", "header_row_all_non_empty"),
        ("all_text", "header_row_all_text"),
        ("all_distinct", "header_row_all_distinct"),
    ):
        c = sum(1 for s in sheets.values() if s[key])
        print(f"    component {label:15s}     {c:7d}  {pct(c, n)}")
    lead = sum(1 for s in sheets.values() if s["header_row_leading_empty"]
               and not s["header_row"])
    print(f"  false, but leading_empty true   {lead:7d}  {pct(lead, n)}"
          "   <- the crossing-table corner 8.4's `matrix` shape is for")
    numeric = sum(1 for s in sheets.values() if not s["header_row"]
                  and s["header_row_numeric_cells"] > 0)
    print(f"  false, with a numeric in row 1  {numeric:7d}  {pct(numeric, n)}")

    rendered = sum(1 for s in sheets.values() if s["rendered"])
    both = sum(1 for s in sheets.values() if s["header_row"] and s["header_col"])
    print("\n# the other 8.4 inputs")
    print(f"  rendered (fits the budget)      {rendered:7d}  {pct(rendered, n)}")
    print(f"  header_row and header_col       {both:7d}  {pct(both, n)}")
    exact = sum(1 for s in sheets.values() if s["interior_cardinality_exact"])
    print(f"  interior_cardinality exact      {exact:7d}  {pct(exact, n)}")
    merged = sum(1 for s in sheets.values() if s["merged_cells"])
    print(f"  has merged cells                {merged:7d}  {pct(merged, n)}")
    dis = sum(1 for s in sheets.values()
              if s["declared_rows"] is not None and s["declared_rows"] != s["rows"])
    print(f"  <dimension> disagrees with use  {dis:7d}  {pct(dis, n)}")

    # Yield is only defined where the header rule fired: a column with no name is not a
    # field, and `extract:sheet` writes nothing for that sheet whatever its cardinality.
    named = [c for c in columns if c["name"]]
    m = len(named)
    print(f"\n# closed-set yield, over the {m} named column(s) "
          f"({pct(m, len(columns))} of all columns)")
    print("  a swept threshold, because 8.8 leaves it unset:")
    print(f"    {'distinct <= k':16s} {'columns':>9s} {'share':>7s} "
          f"{'sheets with >=1':>16s}")
    for k in CLOSED_SET_SWEEP:
        hit = [c for c in named
               if c["distinct_exact"] and c["distinct_count"] is not None
               and 1 < c["distinct_count"] <= k and not c["is_unique"]]
        sheets_hit = {(c["path"], c["sheet"]) for c in hit}
        print(f"    k = {k:<12d} {len(hit):9d} {pct(len(hit), m):>7s} "
              f"{len(sheets_hit):9d} {pct(len(sheets_hit), n):>6s}")

    uniq = [c for c in named if c["is_unique"]]
    const = [c for c in named if c["distinct_count"] == 1]
    inexact = [c for c in named if not c["distinct_exact"]]
    retained = [c for c in named if c["values_retained"]]
    print(f"\n  identifies a row (is_unique)    {len(uniq):9d}  {pct(len(uniq), m)}")
    print(f"  constant (distinct == 1)        {len(const):9d}  {pct(len(const), m)}")
    print(f"  distinct count is a FLOOR only  {len(inexact):9d}  {pct(len(inexact), m)}")
    print(f"  values retained on the node     {len(retained):9d}  {pct(len(retained), m)}")

    print("\n# dominant type, named columns")
    types = collections.Counter(c["dominant_type"] for c in named)
    for t, count in types.most_common():
        print(f"  {count:9d}  {pct(count, m)}  {t}")

    print("\n# the unique-column question: what shape are the row identifiers?")
    print("  (no rule is applied; these are the parts one would be built from)")
    utypes = collections.Counter(c["dominant_type"] for c in uniq)
    for t, count in utypes.most_common(6):
        print(f"  {count:9d}  {pct(count, len(uniq))}  {t}")
    lowered = [(c["name"] or "").lower() for c in uniq]
    idish = sum(1 for s in lowered if s.endswith("_id") or s.endswith(" id") or s == "id")
    print(f"  {idish:9d}  {pct(idish, len(uniq))}  named `id` / `*_id`")
    print("  most common names among unique columns:")
    for name, count in collections.Counter(n for n in lowered if n).most_common(12):
        print(f"    {count:6d}  {name[:44]}")

    ratios = [c["distinct_ratio"] for c in named if c["distinct_ratio"] is not None]
    if ratios:
        ratios.sort()
        qs = [ratios[int(q * (len(ratios) - 1))] for q in (0.1, 0.25, 0.5, 0.75, 0.9)]
        print("\n# distinct / body_rows, named columns — deciles p10 p25 p50 p75 p90")
        print("  " + "  ".join(f"{q:.3f}" for q in qs)
              + f"   mean {statistics.fmean(ratios):.3f}")

    print(f"\n# failures ({sum(errors.values())} rows)")
    for key, count in errors.most_common():
        print(f"  {count:9d}  {key}")
        ex = error_example[key]
        print(f"             {ex['path']}")
        if ex.get("message"):
            print(f"             {ex['message'][:150]}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("roots", nargs="*", help="corpus directories to walk")
    ap.add_argument("--out", default="sheet_yield.jsonl")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) // 2))
    ap.add_argument("--limit", type=int, default=0, help="stop after N workbooks")
    ap.add_argument("--timeout", type=int, default=DEFAULT_SHEET_TIMEOUT,
                    help="wall-clock seconds one sheet may take (see DEFAULT_SHEET_TIMEOUT)")
    ap.add_argument("--report", metavar="JSONL",
                    help="skip the pass and report over an existing JSONL")
    args = ap.parse_args()

    if args.report:
        return report(args.report)
    if not args.roots:
        ap.error("give at least one corpus root, or --report a JSONL")
    report(run(args.roots, args.out, args.workers, args.limit, args.timeout))


if __name__ == "__main__":
    sys.exit(main())
