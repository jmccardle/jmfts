"""Search + structural enrichment, optionally invoking the LLM for synthesis.

Replaces the deprecated MCP ``jmfts_synthesize`` tool. Two modes:

1. **--use-llm**: Calls ``POST /search/synthesize`` which retrieves and asks the
   configured LLM to compose an answer. Closes the gap noted in
   AGENTIC_KNOWLEDGEBASE.md §"MCP deprecation" — the old MCP tool returned
   raw search results and called itself "synthesize".
2. **default (no LLM)**: Search and enrich each result with ancestors, triples,
   and (optionally) links — useful for raw context bundles.

Examples:
    python -m scripts.jmfts_synthesize "what are RAPTOR layers?" --use-llm --pretty
    python -m scripts.jmfts_synthesize "list known entities" --top-k 5 --pretty
    python -m scripts.jmfts_synthesize "..." --include-links --top-k 8
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


def _pretty_synthesis(data: dict) -> str:
    lines = []
    if data.get("synthesis"):
        lines.append(data["synthesis"])
        lines.append("")
        lines.append("--- sources ---")
    for s in data.get("sources", []):
        lines.append(
            f"  #{s.get('document_id')}  score={s.get('score'):.4f}  "
            f"({s.get('method')})  {s.get('title') or ''}"
        )
    lines.append(
        f"\n[model={data.get('llm_model')}  search={data.get('search_latency_ms', 0):.1f}ms  "
        f"total={data.get('total_latency_ms', 0):.1f}ms  "
        f"available={data.get('llm_available', True)}]"
    )
    return "\n".join(lines)


def _pretty_enriched(data: dict) -> str:
    lines = []
    routing = data.get("routing")
    if routing:
        lines.append(f"# routing: {routing.get('method')} ({routing.get('reason')})")
    for r in data.get("results", []):
        lines.append(
            f"\n=== #{r.get('id')}  score={r.get('score')}  "
            f"({r.get('usetype')})  {r.get('title') or ''}"
        )
        if content := r.get("content"):
            lines.append(content[:400] + ("…" if len(content) > 400 else ""))
        if anc := r.get("ancestors"):
            chain = " > ".join(f"{a.get('title') or '(untitled)'}" for a in anc)
            lines.append(f"  ancestors: {chain}")
        if triples := r.get("triples"):
            lines.append(f"  triples: {len(triples)}")
            for t in triples[:5]:
                lines.append(f"    {t.get('subject')} -[{t.get('predicate')}]-> {t.get('object')}")
        if links := r.get("links"):
            lines.append(f"  links: {len(links)}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Synthesize or enrich a JMFTS query.")
    parser.add_argument("query")
    parser.add_argument("--method", default="auto")
    parser.add_argument("--top-k", type=int, default=5, dest="top_k")
    parser.add_argument("--usetype", default=None)
    parser.add_argument("--parent-id", type=int, default=None, dest="parent_id")

    parser.add_argument(
        "--use-llm",
        action="store_true",
        help="Call POST /search/synthesize (real LLM synthesis).",
    )
    parser.add_argument(
        "--llm-model",
        default=None,
        help="Override LLM model (only with --use-llm).",
    )
    parser.add_argument(
        "--max-context-tokens",
        type=int,
        default=4096,
        dest="max_context_tokens",
    )

    parser.add_argument("--no-ancestors", action="store_true")
    parser.add_argument("--no-triples", action="store_true")
    parser.add_argument("--include-links", action="store_true")

    add_base_url_arg(parser)
    add_pretty_arg(parser)
    args = parser.parse_args(argv)

    with client_from_args(args) as client:
        try:
            if args.use_llm:
                data = client.synthesize(
                    args.query,
                    search_method=args.method,
                    top_k=args.top_k,
                    max_context_tokens=args.max_context_tokens,
                    llm_model=args.llm_model,
                    usetype=args.usetype,
                    parent_id=args.parent_id,
                )
                if args.pretty:
                    print(_pretty_synthesis(data))
                else:
                    emit_json(data)
                return 0

            # Non-LLM path: search + per-result enrichment
            search_data = client.search(
                args.query,
                method=args.method,
                limit=args.top_k,
                usetype=args.usetype,
                parent_id=args.parent_id,
            )
            enriched: list[dict] = []
            for r in search_data.get("results", []):
                doc = r.get("document") or {}
                doc_id = doc.get("id")
                row = {
                    "id": doc_id,
                    "title": doc.get("title"),
                    "usetype": doc.get("usetype"),
                    "score": r.get("score"),
                    "method": r.get("method"),
                    "content": doc.get("content"),
                }
                if not args.no_ancestors and doc_id is not None:
                    a = client.get_ancestors(doc_id)
                    items = a.get("ancestors") if isinstance(a, dict) else a
                    if items is None and isinstance(a, list):
                        items = a
                    row["ancestors"] = [
                        {
                            "id": x.get("id"),
                            "title": x.get("title"),
                            "usetype": x.get("usetype"),
                        }
                        for x in (items or [])
                    ]
                if not args.no_triples and doc_id is not None:
                    t = client.query_triples(entity_id=doc_id, direction="both", limit=20)
                    items = t.get("triples") if isinstance(t, dict) else t
                    row["triples"] = items or []
                if args.include_links and doc_id is not None:
                    lk = client.get_links(doc_id, direction="both")
                    items = lk.get("links") if isinstance(lk, dict) else lk
                    row["links"] = items or []
                enriched.append(row)

            payload = {
                "query": args.query,
                "total": len(enriched),
                "method": search_data.get("method") or args.method,
                "routing": search_data.get("routing"),
                "results": enriched,
            }
            if args.pretty:
                print(_pretty_enriched(payload))
            else:
                emit_json(payload)

        except httpx.HTTPStatusError as e:
            die(f"{e.response.status_code} {e.response.text[:300]}")
        except httpx.HTTPError as e:
            die(f"transport error: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
