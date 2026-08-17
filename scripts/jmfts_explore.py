"""Explore the JMFTS document tree and knowledge graph via the REST API.

Replaces the deprecated MCP ``jmfts_explore`` tool. Same nine actions:
``get``, ``children``, ``subtree``, ``roots``, ``ancestors``, ``siblings``,
``links``, ``triples``, ``path``.

Examples:
    python -m scripts.jmfts_explore --action subtree --doc-id 1
    python -m scripts.jmfts_explore --action triples --doc-id 42 --pretty
    python -m scripts.jmfts_explore --action path --doc-id 12 --target-id 99 --max-depth 5
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

ACTIONS = (
    "get",
    "children",
    "subtree",
    "roots",
    "ancestors",
    "siblings",
    "links",
    "triples",
    "path",
)


def _format_doc_line(d: dict) -> str:
    return (
        f"#{d.get('id'):>5}  "
        f"{d.get('usetype') or '-':<20}  "
        f"depth={d.get('depth', '-'):<3}  "
        f"{(d.get('title') or '(untitled)')[:80]}"
    )


def _pretty(action: str, data) -> str:
    if action == "get" and isinstance(data, dict):
        lines = [_format_doc_line(data)]
        if content := data.get("content"):
            lines.append("\n" + content)
        return "\n".join(lines)
    if action in ("children", "subtree", "roots", "siblings", "ancestors"):
        if isinstance(data, dict):
            items = (
                data.get("children")
                or data.get("descendants")
                or data.get("siblings")
                or data.get("ancestors")
                or data.get("documents")
                or data.get("roots")
                or []
            )
            # SubtreeResponse shape: {root, descendants, total}
            if data.get("root"):
                items = [data["root"]] + list(items)
        else:
            items = data or []
        return "\n".join(_format_doc_line(d) for d in items)
    if action == "links":
        items = data.get("links") if isinstance(data, dict) else data
        return "\n".join(
            f"#{lk.get('id')}  {lk.get('source_id')} -[{lk.get('link_type')}]-> "
            f"{lk.get('target_id')}  score={lk.get('score')}"
            for lk in (items or [])
        )
    if action == "triples":
        items = data.get("triples") if isinstance(data, dict) else data
        out = []
        for t in items or []:
            subj = t.get("subject")
            obj = t.get("object")
            pred = t.get("predicate")
            subj_name = (
                subj.get("title") if isinstance(subj, dict) else (subj or t.get("subject_id"))
            )
            obj_name = (
                obj.get("title") if isinstance(obj, dict) else (obj or t.get("object_id"))
            )
            pred_name = (
                pred.get("name") if isinstance(pred, dict) else (pred or t.get("predicate_id"))
            )
            out.append(
                f"#{t.get('id')}  {subj_name} -[{pred_name}]-> {obj_name}  "
                f"({t.get('fact_type', 'atemporal')})"
            )
        return "\n".join(out)
    if action == "path":
        paths = data.get("paths") if isinstance(data, dict) else data
        out = []
        for i, p in enumerate(paths or []):
            steps = " -> ".join(
                f"{step.get('subject_id')} [{step.get('predicate_name')}] {step.get('object_id')}"
                for step in p
            )
            out.append(f"path {i + 1}: {steps}")
        return "\n".join(out) if out else "(no paths found)"
    return str(data)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Explore the JMFTS tree and graph.")
    parser.add_argument("--action", required=True, choices=ACTIONS)
    parser.add_argument("--doc-id", type=int, default=None, dest="doc_id")
    parser.add_argument("--target-id", type=int, default=None, dest="target_id")
    parser.add_argument("--usetype", default=None)
    parser.add_argument("--max-depth", type=int, default=None, dest="max_depth")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument(
        "--direction",
        choices=("outgoing", "incoming", "both"),
        default="both",
    )
    add_base_url_arg(parser)
    add_pretty_arg(parser)
    args = parser.parse_args(argv)

    needs_doc_id = {"get", "children", "subtree", "ancestors", "siblings", "links", "triples", "path"}
    if args.action in needs_doc_id and args.doc_id is None:
        die(f"--doc-id required for action '{args.action}'")
    if args.action == "path" and args.target_id is None:
        die("--target-id required for action 'path'")

    with client_from_args(args) as client:
        try:
            if args.action == "get":
                data = client.get_document(args.doc_id)
            elif args.action == "children":
                data = client.get_children(args.doc_id, usetype=args.usetype, limit=args.limit)
            elif args.action == "subtree":
                data = client.get_subtree(args.doc_id, max_depth=args.max_depth)
            elif args.action == "roots":
                data = client.get_roots()
            elif args.action == "ancestors":
                data = client.get_ancestors(args.doc_id)
            elif args.action == "siblings":
                data = client.get_siblings(args.doc_id)
            elif args.action == "links":
                data = client.get_links(args.doc_id, direction=args.direction)
            elif args.action == "triples":
                data = client.query_triples(
                    entity_id=args.doc_id, direction=args.direction, limit=args.limit
                )
            elif args.action == "path":
                data = client.find_path(
                    from_id=args.doc_id,
                    to_id=args.target_id,
                    max_depth=args.max_depth or 4,
                )
            else:
                die(f"unimplemented action: {args.action}")
                return 1
        except httpx.HTTPStatusError as e:
            die(f"{e.response.status_code} {e.response.text[:200]}")
        except httpx.HTTPError as e:
            die(f"transport error: {e}")

    if args.pretty:
        print(_pretty(args.action, data))
    else:
        emit_json(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
