"""Ingest content into JMFTS via the REST API.

Replaces the deprecated MCP ``jmfts_ingest`` tool. Two modes:

1. Pipeline ingest (``--usetype markdown|conversation|raw|transcript``) — goes
   through ``POST /ingest`` and runs parse/chunk/summarize/extract_facts.
2. Raw document create (``--mode raw-doc``) — calls ``POST /documents`` directly,
   no chunking or summarization.

Content source: ``--content-file PATH`` or stdin.

Examples:
    cat README.md | python -m scripts.jmfts_ingest --usetype markdown --title "README"
    python -m scripts.jmfts_ingest --content-file notes.md --usetype markdown --parent-id 0
    echo 'raw text' | python -m scripts.jmfts_ingest --mode raw-doc --usetype note
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

import httpx

from scripts._jmfts_client import (
    add_base_url_arg,
    add_pretty_arg,
    client_from_args,
    die,
    emit_json,
)


def _read_content(args: argparse.Namespace) -> str:
    if args.content_file:
        path = Path(args.content_file)
        if not path.exists():
            die(f"content file not found: {path}")
        return path.read_text()
    if sys.stdin.isatty():
        die("provide --content-file or pipe content via stdin")
    return sys.stdin.read()


def _format_pretty(data: dict, mode: str) -> str:
    if mode == "raw-doc":
        return (
            f"created document #{data.get('id')}: {data.get('title')!r} "
            f"(usetype={data.get('usetype')}, parent_id={data.get('parent_id')})"
        )
    lines = [
        f"ingested document #{data.get('source_document_id')}: "
        f"{data.get('title')!r} (usetype={data.get('usetype')})",
        f"  segments: {data.get('segment_count', 0)}",
        f"  summaries: {data.get('summary_count', 0)}",
        f"  triples: {data.get('triple_count', 0)}",
        f"  tree depth: {data.get('tree_depth', 1)}",
    ]
    for s in data.get("stages", []):
        status = s.get("status")
        symbol = {"completed": "✓", "skipped": "·", "failed": "✗"}.get(status, "?")
        lines.append(f"  [{symbol}] {s.get('stage')}: {status}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest content into JMFTS.")
    parser.add_argument(
        "--mode",
        choices=("pipeline", "raw-doc"),
        default="pipeline",
        help="pipeline: POST /ingest with stages; raw-doc: POST /documents.",
    )
    parser.add_argument(
        "--usetype",
        required=True,
        help=(
            "Pipeline name (markdown|conversation|raw|transcript|wiki:url|...) "
            "in pipeline mode, or document usetype label in raw-doc mode."
        ),
    )
    parser.add_argument("--content-file", default=None)
    parser.add_argument("--title", default=None)
    parser.add_argument("--parent-id", type=int, default=None)
    parser.add_argument(
        "--structured-content",
        default=None,
        help="JSON string to attach as structured_content metadata.",
    )
    parser.add_argument(
        "--pipeline-config",
        default=None,
        help="JSON string of stage overrides (pipeline mode only).",
    )
    parser.add_argument("--llm-model", default=None)
    parser.add_argument(
        "--no-auto-embed",
        action="store_true",
        help="raw-doc mode only: skip embedding generation.",
    )
    add_base_url_arg(parser)
    add_pretty_arg(parser)
    args = parser.parse_args(argv)

    content = _read_content(args)

    structured_content = None
    if args.structured_content:
        try:
            structured_content = json.loads(args.structured_content)
        except json.JSONDecodeError as e:
            die(f"invalid --structured-content JSON: {e}")

    pipeline_config = None
    if args.pipeline_config:
        try:
            pipeline_config = json.loads(args.pipeline_config)
        except json.JSONDecodeError as e:
            die(f"invalid --pipeline-config JSON: {e}")

    with client_from_args(args) as client:
        try:
            if args.mode == "pipeline":
                data = client.ingest(
                    content=content,
                    usetype=args.usetype,
                    title=args.title,
                    parent_id=args.parent_id,
                    pipeline_config=pipeline_config,
                    llm_model=args.llm_model,
                )
            else:
                data = client.create_document(
                    content=content,
                    title=args.title,
                    parent_id=args.parent_id,
                    usetype=args.usetype,
                    structured_content=structured_content,
                    auto_embed=not args.no_auto_embed,
                )
        except httpx.HTTPStatusError as e:
            die(f"{e.response.status_code} {e.response.text[:300]}")
        except httpx.HTTPError as e:
            die(f"transport error: {e}")

    if args.pretty:
        print(_format_pretty(data, args.mode))
    else:
        emit_json(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
