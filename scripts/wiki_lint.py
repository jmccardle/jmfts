"""Wiki lint orchestrator — runs /graph/lint and prints / files the findings.

Three output modes:
- ``--pretty`` (default for terminal): grouped, color-coded summary.
- bare: raw JSON to stdout.
- ``--format markdown``: produces a wiki:analysis-shaped markdown report.
  With ``--ingest-as wiki:analysis --parent-id <id>`` it ingests the report
  back into the wiki it just audited (closing the loop).

Examples:
    python -m scripts.wiki_lint --pretty
    python -m scripts.wiki_lint --format markdown > /tmp/lint.md
    python -m scripts.wiki_lint --format markdown --ingest-as wiki:analysis --parent-id 0
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from typing import Optional

import httpx

from scripts._jmfts_client import (
    add_base_url_arg,
    add_pretty_arg,
    client_from_args,
    die,
    emit_json,
)


_SEVERITY_ORDER = ["error", "warning", "info"]


def _to_markdown(report: dict, base_url: str) -> str:
    lines: list[str] = []
    when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"# Wiki Lint Report — {when}")
    lines.append("")
    lines.append(f"Scope: `{report.get('scope')}`")
    if report.get("parent_id") is not None:
        lines.append(f"Subtree: `#{report['parent_id']}`")
    counts = report.get("counts") or {}
    if counts:
        parts = [f"{k}={v}" for k, v in sorted(counts.items())]
        lines.append(f"Counts: {', '.join(parts)}")
    lines.append("")

    by_cat: dict[str, list[dict]] = {}
    for f in report.get("findings", []):
        by_cat.setdefault(f["category"], []).append(f)

    for category in ("contradiction", "orphan", "stale", "coverage"):
        items = by_cat.get(category) or []
        if not items:
            continue
        lines.append(f"## {category.title()} ({len(items)})")
        lines.append("")
        for f in items[:50]:
            doc_links = ", ".join(
                f"[#{d}]({base_url}/view/{d})" for d in f.get("document_ids", [])
            )
            triple_ids = f.get("triple_ids") or []
            triple_str = (
                f" — triples: {', '.join(str(t) for t in triple_ids)}"
                if triple_ids
                else ""
            )
            lines.append(f"- **{f['severity']}** — {f['message']}")
            if doc_links:
                lines.append(f"  - docs: {doc_links}{triple_str}")
        if len(items) > 50:
            lines.append(f"- … and {len(items) - 50} more")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _print_pretty(report: dict) -> None:
    counts = report.get("counts") or {}
    parts = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "(no findings)"
    print(f"# Wiki lint — {parts}")
    by_cat: dict[str, list[dict]] = {}
    for f in report.get("findings", []):
        by_cat.setdefault(f["category"], []).append(f)
    for category in ("contradiction", "orphan", "stale", "coverage"):
        items = by_cat.get(category) or []
        if not items:
            continue
        print(f"\n## {category} ({len(items)})")
        for f in items[:30]:
            sym = {"error": "✗", "warning": "!", "info": "·"}.get(f["severity"], "?")
            print(f"  [{sym}] {f['message']}")
        if len(items) > 30:
            print(f"  … and {len(items) - 30} more")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run /graph/lint against the corpus.")
    parser.add_argument("--scope", choices=("links", "triples", "both"), default="links")
    parser.add_argument("--parent-id", type=int, default=None, dest="parent_id")
    parser.add_argument("--exclude-usetypes", default=None, help="CSV of usetypes to exclude")
    parser.add_argument("--orphan-threshold", type=int, default=1, dest="orphan_threshold")
    parser.add_argument("--stale-threshold-days", type=int, default=90, dest="stale_threshold_days")
    parser.add_argument("--coverage-top-k", type=int, default=20, dest="coverage_top_k")
    parser.add_argument(
        "--summary-usetypes",
        default="summary",
        help="CSV of usetypes considered 'summaries' for coverage.",
    )
    parser.add_argument(
        "--format",
        choices=("json", "markdown"),
        default=None,
        help="Output format. Defaults to JSON unless --pretty.",
    )
    parser.add_argument(
        "--ingest-as",
        default=None,
        help="With --format markdown: ingest the report back as a wiki document.",
    )
    add_base_url_arg(parser)
    add_pretty_arg(parser)
    args = parser.parse_args(argv)

    excludes = (
        [s.strip() for s in args.exclude_usetypes.split(",") if s.strip()]
        if args.exclude_usetypes
        else None
    )
    summary_usetypes = [s.strip() for s in args.summary_usetypes.split(",") if s.strip()]

    with client_from_args(args) as client:
        try:
            report = client.graph_lint(
                scope=args.scope,
                parent_id=args.parent_id,
                exclude_usetypes=excludes,
                orphan_threshold=args.orphan_threshold,
                stale_threshold_days=args.stale_threshold_days,
                coverage_top_k=args.coverage_top_k,
                include_summaries_usetype=summary_usetypes,
            )
        except httpx.HTTPStatusError as e:
            die(f"{e.response.status_code} {e.response.text[:300]}")
        except httpx.HTTPError as e:
            die(f"transport error: {e}")

        if args.format == "markdown":
            md = _to_markdown(report, base_url=client.base_url)
            if args.ingest_as:
                ingest_resp = client.ingest(
                    content=md,
                    usetype=args.ingest_as,
                    title=f"Wiki Lint Report {datetime.now(timezone.utc).date().isoformat()}",
                    parent_id=args.parent_id,
                    pipeline_config={
                        "summarize": {"enabled": False},
                        "extract_facts": {"enabled": False},
                    },
                )
                if args.pretty:
                    print(
                        f"ingested as #{ingest_resp.get('source_document_id')} "
                        f"({ingest_resp.get('was_existing')=})"
                    )
                else:
                    emit_json({"report": report, "ingest": ingest_resp})
            else:
                sys.stdout.write(md)
            return 0

    if args.pretty:
        _print_pretty(report)
    else:
        emit_json(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
