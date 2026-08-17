"""Search JMFTS via the REST API. Drop-in replacement for the deprecated MCP tool.

Usage:
    python -m scripts.jmfts_search "query string" --method auto --limit 10
    python -m scripts.jmfts_search "query" --pretty
    python -m scripts.jmfts_search "query" --method bm25 --usetype markdown

See AGENTIC_KNOWLEDGEBASE.md §"MCP deprecation" for the full migration plan.
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

ALLOWED_METHODS = {"auto", "vector", "bm25", "fulltext", "maxsim", "hybrid"}


def _format_pretty(data: dict) -> str:
    lines = []
    routing = data.get("routing")
    if routing:
        lines.append(
            f"# routing: {routing.get('method')} ({routing.get('reason')})"
        )
    lines.append(
        f"# {data.get('total', 0)} results "
        f"({data.get('latency_ms', 0):.1f} ms)"
    )
    for r in data.get("results", []):
        doc = r.get("document") or {}
        score = r.get("score")
        score_str = f"{score:.4f}" if isinstance(score, (int, float)) else str(score)
        lines.append(
            f"\n[{score_str}] #{doc.get('id')} {doc.get('title') or '(untitled)'}"
            f"  ({doc.get('usetype') or 'no-usetype'})"
        )
        content = doc.get("content")
        if content:
            snippet = content[:300].replace("\n", " ")
            lines.append(f"  {snippet}{'…' if len(content) > 300 else ''}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Search JMFTS via the REST API.")
    parser.add_argument("query", help="Search query string")
    parser.add_argument(
        "--method",
        default="auto",
        choices=sorted(ALLOWED_METHODS),
        help="Retrieval method (default: auto).",
    )
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--usetype", default=None, help="Filter to one usetype.")
    parser.add_argument(
        "--exclude-types",
        nargs="*",
        default=None,
        help="Exclude documents with these usetypes (server default applies if omitted).",
    )
    parser.add_argument("--parent-id", type=int, default=None)
    parser.add_argument("--index-name", default="default")
    add_base_url_arg(parser)
    add_pretty_arg(parser)
    args = parser.parse_args(argv)

    with client_from_args(args) as client:
        try:
            data = client.search(
                args.query,
                method=args.method,
                limit=args.limit,
                usetype=args.usetype,
                parent_id=args.parent_id,
                index_name=args.index_name,
                exclude_types=args.exclude_types,
            )
        except httpx.HTTPStatusError as e:
            die(f"{e.response.status_code} {e.response.text[:200]}")
        except httpx.HTTPError as e:
            die(f"transport error: {e}")

    if args.pretty:
        print(_format_pretty(data))
    else:
        emit_json(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
