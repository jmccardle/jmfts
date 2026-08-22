"""Find triples that contradict each other.

A contradiction is two or more triples sharing the same (subject, predicate)
with different objects, where validity windows overlap and neither has
been superseded.

Pulls all triples (paginated) from /triples/query and groups in-process. For
very large corpora this could move server-side; for now it's fine as a script.

Examples:
    python -m scripts.wiki_contradiction_audit --pretty
    python -m scripts.wiki_contradiction_audit --include-invalidated --pretty
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime
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
        # Postgres-style ISO 8601 strings
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _windows_overlap(a_from, a_until, b_from, b_until) -> bool:
    """Two open-ended validity windows overlap iff each starts before the other ends."""
    a_from = _parse_dt(a_from)
    a_until = _parse_dt(a_until)
    b_from = _parse_dt(b_from)
    b_until = _parse_dt(b_until)

    # If both ends of either window are None, treat as "always valid" → always overlaps.
    a_start = a_from
    a_end = a_until
    b_start = b_from
    b_end = b_until

    if a_end is not None and b_start is not None and a_end < b_start:
        return False
    if b_end is not None and a_start is not None and b_end < a_start:
        return False
    return True


def _fetch_all_triples(client, include_invalidated: bool, page_size: int = 200) -> list[dict]:
    """Pull all triples via /triples/query (entity_id=None) with pagination."""
    out: list[dict] = []
    offset = 0
    while True:
        params: dict = {
            "limit": page_size,
            "offset": offset,
            "direction": "both",
            "include_invalidated": include_invalidated,
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
            break  # safety
    return out


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Find contradictory triples.")
    parser.add_argument(
        "--include-invalidated",
        action="store_true",
        help="Include triples that have been invalidated (default: skip them).",
    )
    parser.add_argument("--max-findings", type=int, default=200)
    parser.add_argument("--page-size", type=int, default=200, dest="page_size")
    add_base_url_arg(parser)
    add_pretty_arg(parser)
    args = parser.parse_args(argv)

    with client_from_args(args) as client:
        try:
            all_triples = _fetch_all_triples(
                client, include_invalidated=args.include_invalidated, page_size=args.page_size
            )
        except httpx.HTTPStatusError as e:
            die(f"{e.response.status_code} {e.response.text[:200]}")

    if not args.include_invalidated:
        all_triples = [t for t in all_triples if not t.get("invalidated_at")]

    # Group by (subject_id, predicate_id)
    groups: dict[tuple[int, int], list[dict]] = defaultdict(list)
    for t in all_triples:
        subj = t.get("subject")
        pred = t.get("predicate")
        sid = subj.get("id") if isinstance(subj, dict) else t.get("subject_id")
        pid = pred.get("id") if isinstance(pred, dict) else t.get("predicate_id")
        if sid is None or pid is None:
            continue
        groups[(sid, pid)].append(t)

    findings: list[dict] = []
    for (sid, pid), triples in groups.items():
        if len(triples) < 2:
            continue
        # Different object_ids?
        obj_ids = set()
        for t in triples:
            obj = t.get("object")
            oid = obj.get("id") if isinstance(obj, dict) else t.get("object_id")
            if oid is not None:
                obj_ids.add(oid)
        if len(obj_ids) < 2:
            continue
        # Check overlapping validity windows pairwise
        contradicting_pairs: list[tuple[dict, dict]] = []
        for i, a in enumerate(triples):
            for b in triples[i + 1 :]:
                obj_a = (a.get("object") or {}).get("id") if isinstance(a.get("object"), dict) else a.get("object_id")
                obj_b = (b.get("object") or {}).get("id") if isinstance(b.get("object"), dict) else b.get("object_id")
                if obj_a == obj_b:
                    continue
                if not _windows_overlap(
                    a.get("valid_from"),
                    a.get("valid_until"),
                    b.get("valid_from"),
                    b.get("valid_until"),
                ):
                    continue
                contradicting_pairs.append((a, b))
        if not contradicting_pairs:
            continue
        # Subject / predicate name extraction for display
        subj_obj = triples[0].get("subject") or {}
        pred_obj = triples[0].get("predicate") or {}
        subj_name = subj_obj.get("title") if isinstance(subj_obj, dict) else None
        pred_name = pred_obj.get("name") if isinstance(pred_obj, dict) else None
        findings.append(
            {
                "subject_id": sid,
                "subject_title": subj_name,
                "predicate_id": pid,
                "predicate_name": pred_name,
                "pair_count": len(contradicting_pairs),
                "triples": [
                    {
                        "id": t.get("id"),
                        "object_id": (t.get("object") or {}).get("id")
                        if isinstance(t.get("object"), dict)
                        else t.get("object_id"),
                        "object_title": (t.get("object") or {}).get("title")
                        if isinstance(t.get("object"), dict)
                        else None,
                        "valid_from": t.get("valid_from"),
                        "valid_until": t.get("valid_until"),
                        "fact_type": t.get("fact_type"),
                    }
                    for t in triples
                ],
            }
        )

    findings = findings[: args.max_findings]
    payload = {
        "scanned_triples": len(all_triples),
        "contradiction_groups": len(findings),
        "findings": findings,
    }

    if args.pretty:
        print(f"# scanned {payload['scanned_triples']} triples; "
              f"{payload['contradiction_groups']} contradiction groups")
        for f in findings[:20]:
            print(
                f"\n  subject #{f['subject_id']} ({f['subject_title'] or '?'}) "
                f"--[{f['predicate_name'] or f['predicate_id']}]--> ?"
            )
            for t in f["triples"]:
                window = f"[{t.get('valid_from') or '-∞'} .. {t.get('valid_until') or '+∞'}]"
                print(
                    f"    triple #{t['id']}  obj=#{t['object_id']} ({t['object_title'] or '?'}) "
                    f"{window}  {t.get('fact_type', 'atemporal')}"
                )
        if len(findings) > 20:
            print(f"\n  ... and {len(findings) - 20} more")
    else:
        emit_json(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
