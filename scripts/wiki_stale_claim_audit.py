"""Find dynamic-fact triples that haven't been refreshed in a while.

A triple with ``fact_type='dynamic'`` represents a frequently-changing claim
(server uptime, a person's title, etc.). If its ``valid_from`` is older than
the threshold and it hasn't been superseded (``invalidated_at IS NULL``), it
is likely stale.

Examples:
    python -m scripts.wiki_stale_claim_audit --threshold-days 90 --pretty
    python -m scripts.wiki_stale_claim_audit --threshold-days 30
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
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


def _fetch_dynamic_triples(client, page_size: int = 200) -> list[dict]:
    out: list[dict] = []
    offset = 0
    while True:
        params: dict = {
            "limit": page_size,
            "offset": offset,
            "direction": "both",
            "fact_type": "dynamic",
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
    return out


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Audit stale dynamic-fact triples.")
    parser.add_argument(
        "--threshold-days",
        type=int,
        default=90,
        help="Triples whose valid_from (or recorded_at) is older than this are flagged.",
    )
    parser.add_argument("--page-size", type=int, default=200, dest="page_size")
    parser.add_argument("--max-findings", type=int, default=500)
    add_base_url_arg(parser)
    add_pretty_arg(parser)
    args = parser.parse_args(argv)

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.threshold_days)

    with client_from_args(args) as client:
        try:
            triples = _fetch_dynamic_triples(client, page_size=args.page_size)
        except httpx.HTTPStatusError as e:
            die(f"{e.response.status_code} {e.response.text[:200]}")

    findings: list[dict] = []
    for t in triples:
        ref = _parse_dt(t.get("valid_from")) or _parse_dt(t.get("recorded_at")) or _parse_dt(
            t.get("created_at")
        )
        if ref is None:
            continue
        if ref >= cutoff:
            continue
        subj = t.get("subject") or {}
        obj = t.get("object") or {}
        pred = t.get("predicate") or {}
        findings.append(
            {
                "triple_id": t.get("id"),
                "age_days": (datetime.now(timezone.utc) - ref).days,
                "valid_from": t.get("valid_from"),
                "recorded_at": t.get("recorded_at"),
                "subject_id": subj.get("id") if isinstance(subj, dict) else t.get("subject_id"),
                "subject_title": subj.get("title") if isinstance(subj, dict) else None,
                "predicate_name": pred.get("name") if isinstance(pred, dict) else None,
                "object_id": obj.get("id") if isinstance(obj, dict) else t.get("object_id"),
                "object_title": obj.get("title") if isinstance(obj, dict) else None,
            }
        )

    findings.sort(key=lambda f: f["age_days"], reverse=True)
    findings = findings[: args.max_findings]

    payload = {
        "threshold_days": args.threshold_days,
        "scanned_dynamic_triples": len(triples),
        "stale_count": len(findings),
        "findings": findings,
    }

    if args.pretty:
        print(
            f"# {payload['stale_count']} stale dynamic claims (threshold={args.threshold_days} days, "
            f"scanned {payload['scanned_dynamic_triples']})"
        )
        for f in findings[:50]:
            print(
                f"  triple #{f['triple_id']:>5}  age={f['age_days']:>4}d  "
                f"{f['subject_title'] or '?'} -[{f['predicate_name'] or '?'}]-> "
                f"{f['object_title'] or '?'}"
            )
        if len(findings) > 50:
            print(f"  ... and {len(findings) - 50} more")
    else:
        emit_json(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
