"""List documents with low connectivity (potential orphans).

Calls /graph/centrality with metric=degree, then filters to documents whose
total degree is at or below the threshold. Useful for surfacing wiki pages
that should probably be linked or pruned.

Examples:
    python -m scripts.wiki_orphan_audit --threshold 1 --pretty
    python -m scripts.wiki_orphan_audit --parent-id 42 --threshold 0
    python -m scripts.wiki_orphan_audit --exclude-usetypes chunk,summary --pretty
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

import httpx

from scripts._jmfts_client import (
    add_base_url_arg,
    add_pretty_arg,
    client_from_args,
    die,
    emit_json,
)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit for low-connectivity (orphan) documents."
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=1,
        help="Documents with total degree <= this are flagged (default 1).",
    )
    parser.add_argument(
        "--scope",
        choices=("links", "triples", "both"),
        default="links",
    )
    parser.add_argument("--parent-id", type=int, default=None, dest="parent_id")
    parser.add_argument(
        "--exclude-usetypes",
        default=None,
        help="CSV of usetypes to exclude.",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=100,
        help="Max orphans to return.",
    )
    add_base_url_arg(parser)
    add_pretty_arg(parser)
    args = parser.parse_args(argv)

    excludes = (
        [s.strip() for s in args.exclude_usetypes.split(",") if s.strip()]
        if args.exclude_usetypes
        else None
    )

    with client_from_args(args) as client:
        try:
            data = client.graph_centrality(
                metric="degree",
                scope=args.scope,
                parent_id=args.parent_id,
                exclude_usetypes=excludes,
                top=10000,
            )
        except httpx.HTTPStatusError as e:
            die(f"{e.response.status_code} {e.response.text[:200]}")
        except httpx.HTTPError as e:
            die(f"transport error: {e}")

    orphans = [
        r
        for r in data.get("results", [])
        if (r.get("in_degree", 0) + r.get("out_degree", 0)) <= args.threshold
    ]
    orphans = orphans[: args.max_results]

    payload = {
        "threshold": args.threshold,
        "scope": args.scope,
        "parent_id": args.parent_id,
        "total_vertices": data.get("total_vertices"),
        "orphan_count": len(orphans),
        "orphans": orphans,
    }

    if args.pretty:
        print(
            f"# {len(orphans)} orphans (degree<={args.threshold}, scope={args.scope}, "
            f"vertices={payload['total_vertices']})"
        )
        for r in orphans[:50]:
            deg = r.get("in_degree", 0) + r.get("out_degree", 0)
            print(
                f"  #{r.get('document_id'):>7}  deg={deg}  "
                f"{r.get('usetype') or '-':<20}  {(r.get('title') or '(untitled)')[:80]}"
            )
        if len(orphans) > 50:
            print(f"  ... and {len(orphans) - 50} more")
    else:
        emit_json(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
