"""Ingest a URL into JMFTS via the wiki:url pipeline.

The server fetches the URL (with SSRF guard + size cap), converts HTML to
markdown, and runs the markdown ingest pipeline.

Examples:
    python -m scripts.wiki_ingest_url https://en.wikipedia.org/wiki/Knowledge_graph
    python -m scripts.wiki_ingest_url https://example.com/article --parent-id 0 --pretty
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
    parser = argparse.ArgumentParser(description="Ingest a URL into JMFTS (wiki:url pipeline).")
    parser.add_argument("url")
    parser.add_argument("--parent-id", type=int, default=None, dest="parent_id")
    parser.add_argument("--title", default=None)
    parser.add_argument(
        "--summarize",
        action="store_true",
        help="Enable RAPTOR summarization (off by default for source ingest).",
    )
    parser.add_argument(
        "--extract-facts",
        action="store_true",
        help="Enable LLM fact extraction.",
    )
    add_base_url_arg(parser)
    add_pretty_arg(parser)
    args = parser.parse_args(argv)

    pipeline_config = {
        "summarize": {"enabled": args.summarize},
        "extract_facts": {"enabled": args.extract_facts},
    }

    with client_from_args(args) as client:
        try:
            data = client.ingest(
                content=args.url,
                usetype="wiki:url",
                title=args.title,
                parent_id=args.parent_id,
                pipeline_config=pipeline_config,
            )
        except httpx.HTTPStatusError as e:
            die(f"{e.response.status_code} {e.response.text[:300]}")
        except httpx.HTTPError as e:
            die(f"transport error: {e}")

    if args.pretty:
        if data.get("was_existing"):
            print(f"already ingested as #{data.get('existing_document_id')}: {args.url}")
        else:
            print(
                f"ingested url → #{data.get('source_document_id')}  "
                f"({data.get('segment_count')} segments)  {args.url}"
            )
    else:
        emit_json(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
