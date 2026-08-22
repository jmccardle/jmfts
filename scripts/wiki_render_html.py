"""Render a single document as a standalone HTML page.

Per-page, on-demand HTML — *not* a bulk export. Useful for sharing a single
view with someone, or quick browser inspection. Pipe to a file or open in
``xdg-open``.

Requires ``markdown-it-py``. If not installed, prints the markdown verbatim
and warns to stderr.

Examples:
    python -m scripts.wiki_render_html 7241 > /tmp/page.html
    python -m scripts.wiki_render_html 7241 --no-children > /tmp/page.html && xdg-open /tmp/page.html
"""

from __future__ import annotations

import argparse
import html
import sys
from typing import Optional

import httpx

from scripts._jmfts_client import (
    add_base_url_arg,
    client_from_args,
    die,
)

try:
    from markdown_it import MarkdownIt
    _MD = MarkdownIt("commonmark", {"html": False, "linkify": True}).enable("table")
except ImportError:
    _MD = None

CSS = """
:root { --fg: #222; --dim: #666; --bg: #fdfdfd; --accent: #0366d6; }
@media (prefers-color-scheme: dark) {
  :root { --fg: #ddd; --dim: #aaa; --bg: #1a1a1a; --accent: #58a6ff; }
}
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       background: var(--bg); color: var(--fg); max-width: 760px;
       margin: 2em auto; padding: 0 1em; line-height: 1.55; }
.crumbs { color: var(--dim); font-size: 0.9em; margin-bottom: 0.4em; }
.crumbs a { color: var(--dim); text-decoration: none; }
.crumbs a:hover { text-decoration: underline; }
.usetype { color: var(--dim); font-size: 0.85em; margin-left: 0.5em; }
hr { border: none; border-top: 1px solid var(--dim); opacity: 0.3; margin: 1.5em 0; }
.children { font-size: 0.95em; }
.children .child { padding: 0.4em 0; border-bottom: 1px dashed var(--dim); }
.children .child .preview { color: var(--dim); font-size: 0.9em; margin-top: 0.2em; }
a { color: var(--accent); }
code { background: rgba(127,127,127,0.15); padding: 0.1em 0.3em; border-radius: 3px;
       font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 0.9em; }
pre code { display: block; padding: 0.8em; overflow-x: auto; }
"""


def _md_to_html(md: str) -> str:
    if _MD is None:
        sys.stderr.write(
            "warning: markdown-it-py not installed; emitting verbatim markdown in <pre>\n"
        )
        return f"<pre>{html.escape(md)}</pre>"
    return _MD.render(md)


def _render_html(view: dict) -> str:
    title = view.get("title") or "(untitled)"
    title_esc = html.escape(title)
    usetype = view.get("usetype") or ""

    crumbs_html = ""
    if view.get("ancestors"):
        parts = []
        for a in view["ancestors"]:
            label = a.get("title") or f"#{a['id']}"
            parts.append(f'<a href="/view/{a["id"]}">{html.escape(label)}</a>')
        crumbs_html = f'<div class="crumbs">{" &raquo; ".join(parts)}</div>'

    body_html = _md_to_html(view.get("rendered_content") or "")

    children_html = ""
    if view.get("children_stubs"):
        items = []
        for c in view["children_stubs"]:
            preview = c.get("preview") or ""
            preview_html = (
                f'<div class="preview">{html.escape(preview)}</div>' if preview else ""
            )
            items.append(
                f'<div class="child">'
                f'<a href="{c["expand_url"]}">{html.escape(c.get("title") or "(untitled)")}</a>'
                f' <span class="usetype">({html.escape(c.get("usetype") or "")} · '
                f'{c.get("child_count", 0)} children)</span>'
                f"{preview_html}</div>"
            )
        children_html = f"<hr><h2>Children</h2><div class='children'>{''.join(items)}</div>"

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title_esc}</title>
<style>{CSS}</style>
</head>
<body>
{crumbs_html}
<h1>{title_esc} <span class="usetype">#{view['id']} · {html.escape(usetype)}</span></h1>
<hr>
{body_html}
{children_html}
</body>
</html>
"""


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Render a /view/{id} as a standalone HTML page.")
    parser.add_argument("document_id", type=int)
    parser.add_argument("--no-children", action="store_true")
    parser.add_argument("--limit-children", type=int, default=20)
    add_base_url_arg(parser)
    args = parser.parse_args(argv)

    include = ["links", "triples"]
    if not args.no_children:
        include.append("children")

    with client_from_args(args) as client:
        try:
            data = client.view(
                args.document_id, include=include, limit_children=args.limit_children
            )
        except httpx.HTTPStatusError as e:
            die(f"{e.response.status_code} {e.response.text[:200]}")
        except httpx.HTTPError as e:
            die(f"transport error: {e}")

    sys.stdout.write(_render_html(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
