"""Ingest a URL into JMFTS via the wiki:url pipeline.

The server stores the URL as an in-flight file node and queues a ``fetch:url`` task (SSRF
guard + size cap; a private or loopback host needs ``JMFTS_ALLOW_LOCAL_FETCHES=1`` on the
process that runs the fetch). The fetched page is probed like any upload -- HTML is
converted to markdown by ``extract:text`` -- and this request waits for the document's
tasks to drain, then prints the finished tree.

Re-sending a URL already fetched returns the existing node (``was_existing``): source
ingests deduplicate on the locator, not the bytes.

Examples:
    python -m scripts.wiki_ingest_url https://en.wikipedia.org/wiki/Knowledge_graph
    python -m scripts.wiki_ingest_url https://example.com/article --parent-id 0 --pretty
    python -m scripts.wiki_ingest_url https://example.com/a --extract-facts
    python -m scripts.wiki_ingest_url https://example.com/a --options '{"structure": {"max_words": 300}}'
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

import httpx

from scripts._jmfts_client import (
    add_base_url_arg,
    add_ingest_option_args,
    add_pretty_arg,
    client_from_args,
    die,
    emit_json,
    ingest_options_from_args,
)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest a URL into JMFTS (wiki:url pipeline).")
    parser.add_argument("url")
    parser.add_argument("--parent-id", type=int, default=None, dest="parent_id")
    parser.add_argument("--title", default=None)
    add_ingest_option_args(parser)
    add_base_url_arg(parser)
    add_pretty_arg(parser)
    args = parser.parse_args(argv)

    options = ingest_options_from_args(args)

    with client_from_args(args) as client:
        try:
            data = client.ingest(
                content=args.url,
                usetype="wiki:url",
                title=args.title,
                parent_id=args.parent_id,
                options=options,
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
