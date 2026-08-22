"""Append a dated entry to a wiki:log document.

Reads the current content of the target document, appends a markdown entry
with a timestamp header, writes back via PUT /documents/{id}. Intended for
agents that periodically log their activity into the wiki.

Examples:
    python -m scripts.wiki_log_append --doc-id 123 "ran lint, found 3 contradictions"
    cat report.md | python -m scripts.wiki_log_append --doc-id 123
    python -m scripts.wiki_log_append --usetype wiki:log "started Phase 2 implementation"
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


def _resolve_log_doc(client, doc_id: Optional[int], usetype: Optional[str]) -> int:
    if doc_id is not None:
        return doc_id
    if not usetype:
        die("provide --doc-id or --usetype to locate the log document")
    # Find the first document with that usetype (assumes one log doc per project)
    roots = client.get_roots()
    items = roots.get("roots") if isinstance(roots, dict) else roots
    for d in items or []:
        if d.get("usetype") == usetype:
            return int(d["id"])
    die(f"no document with usetype={usetype!r} found at root level")
    return 0  # unreachable


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Append a dated entry to a wiki:log document.")
    parser.add_argument("entry", nargs="?", default=None, help="Entry text. If omitted, read stdin.")
    parser.add_argument("--doc-id", type=int, default=None, dest="doc_id")
    parser.add_argument(
        "--usetype",
        default=None,
        help="Resolve doc by usetype (e.g. 'wiki:log') if --doc-id not given.",
    )
    parser.add_argument(
        "--header-format",
        default="## %Y-%m-%d %H:%M UTC",
        help="strftime format for the entry header.",
    )
    add_base_url_arg(parser)
    add_pretty_arg(parser)
    args = parser.parse_args(argv)

    text = args.entry
    if text is None:
        if sys.stdin.isatty():
            die("provide entry as argument or pipe via stdin")
        text = sys.stdin.read().strip()
    if not text:
        die("entry is empty")

    header = datetime.now(timezone.utc).strftime(args.header_format)
    addition = f"\n\n{header}\n\n{text}\n"

    with client_from_args(args) as client:
        log_id = _resolve_log_doc(client, args.doc_id, args.usetype)
        try:
            doc = client.get_document(log_id)
            existing = doc.get("content") or ""
            new_content = existing.rstrip() + addition
            updated = client._request(  # type: ignore[attr-defined]
                "PUT",
                f"/documents/{log_id}",
                json_body={"content": new_content, "re_embed": True},
            )
        except httpx.HTTPStatusError as e:
            die(f"{e.response.status_code} {e.response.text[:200]}")
        except httpx.HTTPError as e:
            die(f"transport error: {e}")

    if args.pretty:
        print(f"appended entry to #{log_id}: {len(addition)} chars added")
    else:
        emit_json({"document_id": log_id, "appended_chars": len(addition), "document": updated})
    return 0


if __name__ == "__main__":
    sys.exit(main())
