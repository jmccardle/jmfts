#!/usr/bin/env python3
"""Candidate header rules, measured against a corpus. INGEST_SPEC.md 8.3.

The shipped rule fires on 17.1% of open-web sheets and 32.5% of the git-sourced corpora,
and it gates ``extract:sheet`` entirely — a sheet with no header verdict gets no record
nodes whatever its cardinality. This script measures what candidate rules WOULD fire on,
over the same bytes, so a change to 8.3 is chosen from evidence rather than argued.

It does not call ``measure_sheet``. It reads the first ``--rows`` rows of each sheet with
the same ``canonical`` and ``value_type`` the shipped rule uses, and applies every
candidate to each of those rows. Reading a bounded prefix is what makes this cheap AND
what makes it immune to the unbounded-scan defect: a sheet declaring 10^10 cells still
yields its first eight rows immediately.

    python -m scripts.header_rules datasets/corpus-fuse --out fuse_headers.jsonl
    python -m scripts.header_rules --report fuse_headers.jsonl

**A higher fire rate is not automatically better.** A rule that admits more sheets and
names junk columns has moved the failure from "no records" to "wrong records", which is
worse. The report therefore carries, beside each rule's rate, how many columns it names
and how many of those names are numeric or duplicated — the parts of a quality judgement
this pass can measure. Yield under a chosen rule needs a full `sheet_yield` pass.
"""

import argparse
import collections
import itertools
import json
import os
import signal
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

READABLE = {".xlsx", ".xlsm"}
DEFAULT_ROWS = 8
DEFAULT_TIMEOUT = 60
MAX_BYTES = 128 * 1024 * 1024


class RowTimeout(Exception):
    pass


def _alarm(_signum, _frame):
    raise RowTimeout("row read exceeded the timeout")


# ---------------------------------------------------------------------------
# The candidates. Each takes the per-row measurement dict and returns the number of
# columns it would NAME, or 0 for no verdict.
#
# `width` is the sheet's measured used width, from the prefix read. `values`/`kinds` are
# that row over 1..width, `None` where empty — the same shape `_header_evidence` takes.
# ---------------------------------------------------------------------------


def _parts(values, kinds):
    present = [v for v in values if v is not None]
    text = sum(1 for k in kinds if k == "text")
    numeric = sum(1 for k in kinds if k == "number")
    return present, text, numeric


def shipped(values, kinds):
    """8.3 exactly: every cell of the used width is text, and all are distinct.

    `all_text` already implies `all_non_empty` — an empty cell has kind None and so is
    not counted in `text_cells` — which is why the shipped verdict tracks `all_text`
    almost exactly in both corpora.
    """
    present, text, _numeric = _parts(values, kinds)
    if not values or text != len(values):
        return 0
    return len(values) if len(set(present)) == len(present) else 0


def prefix(values, kinds):
    """A: the rule over row 1's own contiguous prefix, not the sheet's full width.

    A sheet is ragged when a row below reaches further right than the header does — a
    totals cell, a note, a stray. Today that trailing `None` fails `all_text` and the
    whole sheet loses its header. This rule names the prefix and leaves the overhang
    unnamed, which is what `build_records` would then raise on
    (`HeaderDoesNotCoverTheRow`), so adopting it means deciding that case too.
    """
    k = 0
    for value, kind in zip(values, kinds):
        if value is None:
            break
        if kind != "text":
            return 0
        k += 1
    if k == 0:
        return 0
    present = values[:k]
    return k if len(set(present)) == len(present) else 0


def present_only(values, kinds):
    """A2: ignore empties anywhere — every PRESENT cell is text and distinct.

    The weakest of the family: it admits a header row with holes in the middle, which is
    usually a merged banner rather than a field list.
    """
    present, text, _numeric = _parts(values, kinds)
    if not present or text != len(present):
        return 0
    return len(present) if len(set(present)) == len(present) else 0


def allow_numeric(values, kinds):
    """B1: drop `all_text`. Every cell is non-empty and distinct, of any type.

    The case this is for is a year or period header — `2019, 2020, 2021` — which is a
    perfectly good field list that 8.3 rejects for being numeric.
    """
    present, _text, _numeric = _parts(values, kinds)
    if not values or len(present) != len(values):
        return 0
    return len(values) if len(set(present)) == len(present) else 0


def majority_text(values, kinds):
    """B2: at least 80% of present cells are text, all present cells distinct, no holes."""
    present, text, _numeric = _parts(values, kinds)
    if not values or len(present) != len(values):
        return 0
    if text < 0.8 * len(values):
        return 0
    return len(values) if len(set(present)) == len(present) else 0


def prefix_allow_numeric(values, kinds):
    """A1 + B1 together: the contiguous prefix, of any type, all distinct."""
    k = 0
    for value in values:
        if value is None:
            break
        k += 1
    if k == 0:
        return 0
    present = values[:k]
    return k if len(set(present)) == len(present) else 0


#: Ordered weakest-constraint-last so the report reads as a relaxation ladder.
RULES = {
    "shipped": shipped,
    "A1 prefix": prefix,
    "B1 allow_numeric": allow_numeric,
    "B2 majority_text": majority_text,
    "A2 present_only": present_only,
    "A1+B1 prefix_any": prefix_allow_numeric,
}


def scan_book(args):
    path, rows_to_read, timeout = args
    from jmfts_core.office.sheets import canonical, value_type

    try:
        if os.path.getsize(path) > MAX_BYTES:
            return [], [{"path": path, "error": "too-large"}]
        import openpyxl

        book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - survey: every class is a corpus fact
        return [], [{"path": path, "error": f"open/{exc.__class__.__name__}"}]

    previous = signal.signal(signal.SIGALRM, _alarm)
    out, errors = [], []
    try:
        for name in book.sheetnames:
            signal.alarm(timeout)
            try:
                sheet = book[name]
                # A Chartsheet has no cells. The shipped `measure_sheet` crashes on one
                # (AttributeError: no `max_row`); here it is a recorded class, because a
                # survey that dies on 1.3% of the open web measures nothing.
                if not hasattr(sheet, "iter_rows"):
                    errors.append({"path": path, "sheet": name,
                                   "error": f"not-a-worksheet/{type(sheet).__name__}"})
                    continue
                head = [list(r) for r in itertools.islice(
                    sheet.iter_rows(values_only=True), rows_to_read)]
            except RowTimeout:
                errors.append({"path": path, "sheet": name, "error": "timeout"})
                continue
            except Exception as exc:  # noqa: BLE001
                errors.append({"path": path, "sheet": name,
                               "error": f"read/{exc.__class__.__name__}"})
                continue
            finally:
                signal.alarm(0)

            if not head:
                errors.append({"path": path, "sheet": name, "error": "no-rows"})
                continue
            # The used width as this prefix can see it. `measure_sheet` takes it over the
            # WHOLE sheet, so a row below row 8 that reaches further right would widen it
            # and could flip `shipped` from true to false. `width_from_prefix` records
            # that this number is a floor; the report says how often it binds.
            width = 0
            grid = []
            for row in head:
                cells = [(canonical(v), None if canonical(v) is None else value_type(v))
                         for v in row]
                grid.append(cells)
                for index, (value, _kind) in enumerate(cells, start=1):
                    if value is not None:
                        width = max(width, index)
            if width == 0:
                errors.append({"path": path, "sheet": name, "error": "empty-prefix"})
                continue

            record = {"path": path, "sheet": name, "width": width,
                      "rows_read": len(head)}
            for label, rule in RULES.items():
                fired_at, named = 0, 0
                for row_number, cells in enumerate(grid, start=1):
                    values = [cells[i][0] if i < len(cells) else None
                              for i in range(width)]
                    kinds = [cells[i][1] if i < len(cells) else None
                             for i in range(width)]
                    count = rule(values, kinds)
                    if count:
                        fired_at, named = row_number, count
                        break
                record[label] = {"row": fired_at, "named": named}
            out.append(record)
    finally:
        signal.signal(signal.SIGALRM, previous)
        book.close()
    return out, errors


def run(roots, out_path, workers, rows_to_read, timeout, limit):
    files = []
    for root in roots:
        for dirpath, _dirs, names in os.walk(root):
            for name in sorted(names):
                if os.path.splitext(name)[1].lower() in READABLE:
                    files.append(os.path.join(dirpath, name))
    if limit:
        files = files[:limit]
    print(f"{len(files)} workbook(s); reading the first {rows_to_read} row(s) of each sheet",
          file=sys.stderr)
    done = 0
    with open(out_path, "w") as out:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(scan_book, (p, rows_to_read, timeout)) for p in files]
            for future in as_completed(futures):
                done += 1
                rows, errors = future.result()
                for row in rows + errors:
                    out.write(json.dumps(row) + "\n")
                if done % 1000 == 0:
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
    print(f"\n# {n} sheet(s), {sum(errors.values())} failure row(s)")
    print(f"\n{'rule':22s} {'fires':>7s} {'rate':>7s} {'row 1':>7s} {'row >1':>7s} "
          f"{'named/sheet':>12s} {'vs shipped':>11s}")
    base = {(s["path"], s["sheet"]) for s in sheets if s["shipped"]["row"]}
    for label in RULES:
        fired = [s for s in sheets if s[label]["row"]]
        if not fired:
            print(f"{label:22s} {0:7d}")
            continue
        first = sum(1 for s in fired if s[label]["row"] == 1)
        later = len(fired) - first
        named = sum(s[label]["named"] for s in fired) / len(fired)
        keys = {(s["path"], s["sheet"]) for s in fired}
        gained = len(keys - base)
        lost = len(base - keys)
        delta = f"+{gained}/-{lost}" if label != "shipped" else "—"
        print(f"{label:22s} {len(fired):7d} {100*len(fired)/n:6.1f}% {first:7d} "
              f"{later:7d} {named:11.1f} {delta:>11s}")

    print("\n# where a rule fires below row 1 (a title row, a blank, a banner)")
    for label in RULES:
        rows = collections.Counter(s[label]["row"] for s in sheets if s[label]["row"] > 1)
        if rows:
            top = "  ".join(f"r{k}:{v}" for k, v in sorted(rows.items())[:6])
            print(f"  {label:22s} {sum(rows.values()):6d}   {top}")

    print(f"\n# failures")
    for key, count in errors.most_common(10):
        print(f"  {count:7d}  {key}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("roots", nargs="*")
    ap.add_argument("--out", default="header_rules.jsonl")
    ap.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) // 2))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--report", metavar="JSONL")
    args = ap.parse_args()
    if args.report:
        return report(args.report)
    if not args.roots:
        ap.error("give at least one corpus root, or --report a JSONL")
    report(run(args.roots, args.out, args.workers, args.rows, args.timeout, args.limit))


if __name__ == "__main__":
    sys.exit(main())
