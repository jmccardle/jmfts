"""Pretty-print a /view/{id} response to the terminal.

Renders ancestors as a breadcrumb, content as raw markdown, children as a
collapsed list, and link/triple footnotes inline. Uses ``rich`` if
available; falls back to plain text otherwise.

Examples:
    python -m scripts.wiki_view_terminal 7241
    python -m scripts.wiki_view_terminal 7241 --no-children
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

import httpx

from scripts._jmfts_client import (
    add_base_url_arg,
    client_from_args,
    die,
)

try:
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel
    _HAS_RICH = True
except ImportError:  # rich is optional
    _HAS_RICH = False


def _print_plain(view: dict) -> None:
    crumbs = view.get("ancestors") or []
    if crumbs:
        chain = " > ".join(a.get("title") or f"#{a.get('id')}" for a in crumbs)
        chain += " > "
    else:
        chain = ""
    print(f"# {chain}{view.get('title') or '(untitled)'}  (#{view['id']}, {view.get('usetype')})")
    print()
    print(view.get("rendered_content", ""))
    print()
    children = view.get("children_stubs") or []
    if children:
        print("--- children ---")
        for c in children:
            preview = (c.get("preview") or "").replace("\n", " ")[:120]
            count = c.get("child_count") or 0
            print(
                f"  [+{count}] #{c['id']}  {c.get('title') or '(untitled)'}  "
                f"({c.get('usetype')})  → {c['expand_url']}"
            )
            if preview:
                print(f"      {preview}{'…' if len(preview) >= 120 else ''}")


def _print_rich(view: dict) -> None:
    console = Console()
    crumbs = view.get("ancestors") or []
    if crumbs:
        chain = " ❯ ".join(a.get("title") or f"#{a.get('id')}" for a in crumbs)
        console.print(f"[dim]{chain}[/dim]")
    title_line = f"[bold]#{view['id']}[/bold]  {view.get('title') or '(untitled)'}"
    if view.get("usetype"):
        title_line += f"  [dim]({view['usetype']})[/dim]"
    console.print(title_line)
    console.rule()
    md = view.get("rendered_content", "")
    if md.strip():
        console.print(Markdown(md))
    children = view.get("children_stubs") or []
    if children:
        console.rule("children")
        for c in children:
            preview = (c.get("preview") or "").replace("\n", " ")[:120]
            count = c.get("child_count") or 0
            line = (
                f"  [cyan][+{count}][/cyan] [bold]#{c['id']}[/bold]  "
                f"{c.get('title') or '(untitled)'}  [dim]({c.get('usetype')})[/dim]  "
                f"[dim]→ {c['expand_url']}[/dim]"
            )
            console.print(line)
            if preview:
                console.print(f"      [dim]{preview}{'…' if len(preview) >= 120 else ''}[/dim]")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Pretty-print /view/{id} to terminal.")
    parser.add_argument("document_id", type=int)
    parser.add_argument("--no-children", action="store_true")
    parser.add_argument("--no-links", action="store_true")
    parser.add_argument("--no-triples", action="store_true")
    parser.add_argument("--limit-children", type=int, default=20)
    parser.add_argument("--plain", action="store_true", help="Disable rich rendering even if available.")
    add_base_url_arg(parser)
    args = parser.parse_args(argv)

    include = []
    if not args.no_children:
        include.append("children")
    if not args.no_links:
        include.append("links")
    if not args.no_triples:
        include.append("triples")

    with client_from_args(args) as client:
        try:
            data = client.view(
                args.document_id, include=include, limit_children=args.limit_children
            )
        except httpx.HTTPStatusError as e:
            die(f"{e.response.status_code} {e.response.text[:200]}")
        except httpx.HTTPError as e:
            die(f"transport error: {e}")

    if _HAS_RICH and not args.plain:
        _print_rich(data)
    else:
        _print_plain(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
