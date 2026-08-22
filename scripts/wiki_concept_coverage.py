"""Diff entities mentioned in recent sources against existing entity pages.

Workflow:
1. Fetch recent triples (since some date) — these expose subject and object IDs.
2. For each unique entity ID, look up its document. If its usetype is *not*
   one of the entity-page usetypes, it has no dedicated wiki page.
3. Score by mention frequency.

The output is "what entities are showing up in your recent ingests but don't
have a wiki page yet."

Examples:
    python -m scripts.wiki_concept_coverage --since 2026-04-01 --pretty
    python -m scripts.wiki_concept_coverage --since 2026-01-01 --entity-usetypes wiki:entity,wiki:concept
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
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


def _parse_dt(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _fetch_recent_triples(client, since: Optional[datetime], page_size: int = 200) -> list[dict]:
    out: list[dict] = []
    offset = 0
    while True:
        params: dict = {
            "limit": page_size,
            "offset": offset,
            "direction": "both",
            "include_invalidated": False,
        }
        try:
            chunk = client._request("GET", "/triples/query", params=params)  # type: ignore[attr-defined]
        except httpx.HTTPError:
            break
        if not chunk:
            break
        out.extend(chunk)
        if len(chunk) < page_size:
            break
        offset += page_size
        if offset > 100000:
            break
    if since is not None:
        out = [
            t
            for t in out
            if (
                _parse_dt(t.get("created_at")) or _parse_dt(t.get("recorded_at")) or datetime.min.replace(tzinfo=timezone.utc)
            )
            >= since
        ]
    return out


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Audit entity coverage in recent sources.")
    parser.add_argument(
        "--since",
        default=None,
        help="ISO 8601 date — only triples created on/after this are counted.",
    )
    parser.add_argument(
        "--entity-usetypes",
        default="wiki:entity,wiki:concept,entity",
        help="CSV of usetypes considered 'has a wiki page'.",
    )
    parser.add_argument("--max-findings", type=int, default=200)
    add_base_url_arg(parser)
    add_pretty_arg(parser)
    args = parser.parse_args(argv)

    since = _parse_dt(args.since) if args.since else None
    if since is not None and since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    entity_usetypes = {s.strip() for s in args.entity_usetypes.split(",") if s.strip()}

    with client_from_args(args) as client:
        try:
            triples = _fetch_recent_triples(client, since=since)
        except httpx.HTTPStatusError as e:
            die(f"{e.response.status_code} {e.response.text[:200]}")

        # Tally entity references; record (id, title, usetype) once
        mentions: Counter[int] = Counter()
        meta: dict[int, dict] = {}
        for t in triples:
            for role in ("subject", "object"):
                ent = t.get(role)
                if isinstance(ent, dict) and ent.get("id"):
                    eid = int(ent["id"])
                    mentions[eid] += 1
                    if eid not in meta:
                        meta[eid] = {
                            "id": eid,
                            "title": ent.get("title"),
                            "usetype": ent.get("usetype"),
                        }

        # Filter to entities WITHOUT a wiki page (their usetype isn't a known entity usetype)
        gaps = []
        for eid, count in mentions.most_common():
            m = meta.get(eid, {})
            if m.get("usetype") in entity_usetypes:
                continue
            gaps.append(
                {
                    "document_id": eid,
                    "title": m.get("title"),
                    "usetype": m.get("usetype"),
                    "mention_count": count,
                }
            )
            if len(gaps) >= args.max_findings:
                break

    payload = {
        "since": args.since,
        "entity_usetypes": sorted(entity_usetypes),
        "scanned_triples": len(triples),
        "gap_count": len(gaps),
        "gaps": gaps,
    }

    if args.pretty:
        print(
            f"# {payload['gap_count']} entities with no wiki page "
            f"(scanned {payload['scanned_triples']} triples, "
            f"since={args.since or 'all-time'})"
        )
        for g in gaps[:50]:
            print(
                f"  #{g['document_id']:>7}  mentions={g['mention_count']:>3}  "
                f"{g.get('usetype') or '-':<20}  {(g.get('title') or '(untitled)')[:80]}"
            )
        if len(gaps) > 50:
            print(f"  ... and {len(gaps) - 50} more")
    else:
        emit_json(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
