"""Ingest an arXiv paper into JMFTS via the wiki:arxiv pipeline.

Server fetches metadata + PDF, extracts markdown, ingests as a markdown root.
The arxiv_id may be a full URL, an abs/pdf path, or a bare ID — the server
normalizes.

Examples:
    python -m scripts.wiki_ingest_arxiv 2310.06770
    python -m scripts.wiki_ingest_arxiv https://arxiv.org/abs/2310.06770 --parent-id 0
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
    parser = argparse.ArgumentParser(description="Ingest an arXiv paper into JMFTS.")
    parser.add_argument("arxiv_id", help="Bare ID (2310.06770), abs URL, or pdf URL.")
    parser.add_argument("--parent-id", type=int, default=None, dest="parent_id")
    parser.add_argument("--title", default=None)
    parser.add_argument("--summarize", action="store_true")
    parser.add_argument("--extract-facts", action="store_true")
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
                content=args.arxiv_id,
                usetype="wiki:arxiv",
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
            print(f"already ingested as #{data.get('existing_document_id')}")
        else:
            print(
                f"ingested arxiv {args.arxiv_id} → #{data.get('source_document_id')}  "
                f"({data.get('segment_count')} segments)"
            )
    else:
        emit_json(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
