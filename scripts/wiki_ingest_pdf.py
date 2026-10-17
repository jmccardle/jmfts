"""Ingest a local PDF file into JMFTS via the wiki:pdf pipeline.

The path is read by a ``fetch:path`` task, server-side, so the PDF must be visible to
whichever JMFTS process runs that task (the API's in-process worker, or a ``jmfts-worker``).
For local-only deployments this is fine. From another machine, upload the bytes instead
with ``POST /ingest/file`` (multipart), which needs no shared filesystem.

Examples:
    python -m scripts.wiki_ingest_pdf ./paper.pdf --extract-facts
    python -m scripts.wiki_ingest_pdf /storage/papers/foo.pdf --parent-id 0 --pretty
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
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
    parser = argparse.ArgumentParser(description="Ingest a local PDF into JMFTS.")
    parser.add_argument("pdf_path", help="Path to a PDF file (must be readable by the API server).")
    parser.add_argument("--parent-id", type=int, default=None, dest="parent_id")
    parser.add_argument("--title", default=None)
    add_ingest_option_args(parser)
    add_base_url_arg(parser)
    add_pretty_arg(parser)
    args = parser.parse_args(argv)

    path = Path(args.pdf_path)
    abs_path = str(path.resolve())
    # Local-readability check — gives the user a clearer error than an opaque 400.
    if not path.exists():
        die(f"PDF not found: {path}")

    options = ingest_options_from_args(args)

    with client_from_args(args) as client:
        try:
            data = client.ingest(
                content=abs_path,
                usetype="wiki:pdf",
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
            print(f"already ingested as #{data.get('existing_document_id')}: {path}")
        else:
            print(
                f"ingested pdf {path.name} → #{data.get('source_document_id')}  "
                f"({data.get('segment_count')} segments)"
            )
    else:
        emit_json(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
