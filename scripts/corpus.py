#!/usr/bin/env python3
"""The fidelity corpus, from a terminal — ``docs/OFFICE_SPEC.md`` Part 10.

The harness lives under ``tests/corpus/`` because its job is to be run by the suite, but
three of the things it knows are things a person wants to ask outside a test run:

``report``    what the corpus covers, and — the number that matters — how much of its
              vocabulary shipped code can actually measure today.
``write``     materialise the generated fixtures into a directory, for poking at one with
              ``unzip -l``, or for handing to a reader that needs real files.
``check``     load the manifest and rebuild every fixture, exiting non-zero if anything
              disagrees. The same assertions the suite makes, without pytest.

Usage::

    python -m scripts.corpus report
    python -m scripts.corpus write /tmp/jmfts-corpus
    python -m scripts.corpus check

Nothing here touches a database, a model, or the network.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests.corpus import fixtures  # noqa: E402
from tests.corpus.manifest import ManifestError, load, sha256  # noqa: E402
from tests.corpus.vocabulary import Status, coverage, probe_vocabulary  # noqa: E402


def report() -> int:
    corpus = load()
    grouped = coverage()

    print(f"corpus: {len(corpus)} records\n")
    by_expectation: dict[str, list[str]] = {}
    for record in corpus:
        by_expectation.setdefault(record.expect, []).append(record.name)
    for expectation, names in sorted(by_expectation.items()):
        print(f"  {expectation:<14} {len(names):>3}  {', '.join(sorted(names))}")

    print("\nvocabulary (flags only; measurements are not tags):")
    for status in Status:
        names = grouped[status]
        print(f"  {status.value:<10} {len(names):>3}  {', '.join(names) or '-'}")

    probed = set(grouped[Status.PROBED])
    total = sum(len(names) for names in grouped.values())
    print(
        f"\n  {len(probed)}/{total} tags are measurable by shipped code today. "
        "The rest are OFFICE_SPEC Part 2 (planned) and Part 10's container hazards "
        "(proposed, no spec row yet — see docs/CORPUS.md)."
    )

    print("\ntag usage across the corpus:")
    for status in Status:
        for tag in grouped[status]:
            used = [r.name for r in corpus.tagged(tag)]
            marker = " " if used else "!"
            print(f" {marker} {tag:<26} {status.value:<9} {len(used)}")
    unused = [t for names in grouped.values() for t in names if not corpus.tagged(t)]
    if unused:
        print(
            f"\n  {len(unused)} tag(s) marked '!' are in the vocabulary and on no corpus "
            "file. Part 10: a feature no file carries is a gap, not a pass."
        )
    empty = [r.name for r in corpus.untagged()]
    if empty:
        print(f"  untagged records: {', '.join(empty)}")
    return 0


def write(destination: Path) -> int:
    written = fixtures.write_all(destination)
    for name, path in written.items():
        print(f"{path.stat().st_size:>8}  {name}")
    print(f"\n{len(written)} fixtures written to {destination}")
    return 0


def check() -> int:
    try:
        corpus = load()
    except ManifestError as exc:
        print(exc, file=sys.stderr)
        return 1
    problems = []
    for record in corpus:
        data = corpus.bytes_for(record)
        if sha256(data) != record.sha256 or len(data) != record.size:
            problems.append(f"{record.name}: bytes do not match the record")
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        return 1
    print(f"{len(corpus)} records, {len(probe_vocabulary())} live probe patterns, all consistent")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("report", help="coverage, and what shipped code can measure")
    write_parser = subcommands.add_parser("write", help="materialise the fixtures")
    write_parser.add_argument("destination", type=Path)
    subcommands.add_parser("check", help="manifest against the bytes; non-zero on drift")

    args = parser.parse_args(argv)
    if args.command == "report":
        return report()
    if args.command == "write":
        return write(args.destination)
    return check()


if __name__ == "__main__":
    raise SystemExit(main())
