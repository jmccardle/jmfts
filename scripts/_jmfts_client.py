"""Shared httpx-based client for JMFTS scripts.

Replaces the ad-hoc `requests` usage scattered across older scripts. Every
new script under ``scripts/`` should import this module instead of building
its own HTTP calls.

Base URL resolution:
1. ``--base-url`` flag (via ``add_base_url_arg`` + ``client_from_args``)
2. ``JMFTS_API_BASE_URL`` environment variable
3. Default: ``http://localhost:8100``
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Optional

import httpx

DEFAULT_BASE_URL = "http://localhost:8100"
DEFAULT_TIMEOUT = 60.0


class JMFTSClient:
    """Minimal sync HTTP client over the JMFTS REST API."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        resolved = base_url or os.environ.get("JMFTS_API_BASE_URL") or DEFAULT_BASE_URL
        self.base_url = resolved.rstrip("/")
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout)

    # -- transport ----------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
    ) -> Any:
        resp = self._client.request(method, path, params=params, json=json_body)
        resp.raise_for_status()
        if not resp.content:
            return {}
        return resp.json()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "JMFTSClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # -- search -------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        method: str = "auto",
        limit: int = 10,
        usetype: Optional[str] = None,
        parent_id: Optional[int] = None,
        index_name: str = "default",
        exclude_types: Optional[list[str]] = None,
    ) -> dict:
        body: dict[str, Any] = {"query": query, "limit": limit}
        if usetype is not None:
            body["usetype"] = usetype
        if parent_id is not None:
            body["parent_id"] = parent_id
        if exclude_types is not None:
            body["exclude_types"] = exclude_types
        if method in ("bm25", "hybrid"):
            body["index_name"] = index_name
        return self._request("POST", f"/search/{method}", json_body=body)

    def synthesize(
        self,
        query: str,
        *,
        search_method: str = "auto",
        top_k: int = 5,
        max_context_tokens: int = 4096,
        llm_model: Optional[str] = None,
        usetype: Optional[str] = None,
        parent_id: Optional[int] = None,
    ) -> dict:
        body: dict[str, Any] = {
            "query": query,
            "search_method": search_method,
            "top_k": top_k,
            "max_context_tokens": max_context_tokens,
        }
        if llm_model is not None:
            body["llm_model"] = llm_model
        if usetype is not None:
            body["usetype"] = usetype
        if parent_id is not None:
            body["parent_id"] = parent_id
        return self._request("POST", "/search/synthesize", json_body=body)

    # -- documents ----------------------------------------------------------

    def get_document(self, doc_id: int) -> dict:
        return self._request("GET", f"/documents/{doc_id}")

    def get_children(
        self, doc_id: int, *, usetype: Optional[str] = None, limit: int = 100
    ) -> Any:
        params: dict[str, Any] = {"limit": limit}
        if usetype:
            params["usetype"] = usetype
        return self._request("GET", f"/documents/{doc_id}/children", params=params)

    def get_subtree(self, doc_id: int, *, max_depth: Optional[int] = None) -> Any:
        params = {"max_depth": max_depth} if max_depth is not None else None
        return self._request("GET", f"/documents/{doc_id}/subtree", params=params)

    def get_roots(self) -> Any:
        return self._request("GET", "/documents/roots")

    def get_ancestors(self, doc_id: int) -> Any:
        return self._request("GET", f"/documents/{doc_id}/ancestors")

    def get_siblings(self, doc_id: int) -> Any:
        return self._request("GET", f"/documents/{doc_id}/siblings")

    def get_links(self, doc_id: int, *, direction: str = "both") -> Any:
        return self._request(
            "GET", f"/documents/{doc_id}/links", params={"direction": direction}
        )

    def create_document(
        self,
        *,
        content: str,
        title: Optional[str] = None,
        parent_id: Optional[int] = None,
        usetype: Optional[str] = None,
        structured_content: Optional[dict] = None,
        auto_embed: bool = True,
    ) -> dict:
        body: dict[str, Any] = {"content": content, "auto_embed": auto_embed}
        if title is not None:
            body["title"] = title
        if parent_id is not None:
            body["parent_id"] = parent_id
        if usetype is not None:
            body["usetype"] = usetype
        if structured_content is not None:
            body["structured_content"] = structured_content
        return self._request("POST", "/documents", json_body=body)

    # -- ingest pipeline ---------------------------------------------------

    def ingest(
        self,
        *,
        content: str,
        usetype: str,
        title: Optional[str] = None,
        parent_id: Optional[int] = None,
        pipeline_config: Optional[dict] = None,
        llm_model: Optional[str] = None,
    ) -> dict:
        body: dict[str, Any] = {"content": content, "usetype": usetype}
        if title is not None:
            body["title"] = title
        if parent_id is not None:
            body["parent_id"] = parent_id
        if pipeline_config is not None:
            body["pipeline_config"] = pipeline_config
        if llm_model is not None:
            body["llm_model"] = llm_model
        return self._request("POST", "/ingest", json_body=body)

    def index_document(self, doc_id: int, *, index_name: str = "default") -> Any:
        return self._request(
            "POST", f"/indexes/{index_name}/index-document/{doc_id}"
        )

    # -- triples -----------------------------------------------------------

    def query_triples(
        self,
        *,
        entity_id: Optional[int] = None,
        direction: str = "both",
        limit: int = 50,
    ) -> Any:
        params: dict[str, Any] = {"direction": direction, "limit": limit}
        if entity_id is not None:
            params["entity_id"] = entity_id
        return self._request("GET", "/triples/query", params=params)

    def list_triples(self, *, limit: int = 100, offset: int = 0) -> Any:
        return self._request(
            "GET", "/triples", params={"limit": limit, "offset": offset}
        )

    def find_path(
        self, *, from_id: int, to_id: int, max_depth: int = 4
    ) -> Any:
        return self._request(
            "GET",
            "/triples/path",
            params={"from_id": from_id, "to_id": to_id, "max_depth": max_depth},
        )

    # -- graph (Phase 1A) --------------------------------------------------

    def graph_centrality(
        self,
        *,
        metric: str = "pagerank",
        scope: str = "links",
        parent_id: Optional[int] = None,
        exclude_usetypes: Optional[list[str]] = None,
        top: int = 20,
    ) -> dict:
        params: dict[str, Any] = {"metric": metric, "scope": scope, "top": top}
        if parent_id is not None:
            params["parent_id"] = parent_id
        if exclude_usetypes:
            params["exclude_usetypes"] = ",".join(exclude_usetypes)
        return self._request("GET", "/graph/centrality", params=params)

    def graph_subtree_authority(
        self,
        *,
        metric: str = "pagerank",
        scope: str = "links",
        decay: float = 0.7,
        min_subtree_size: int = 3,
        parent_id: Optional[int] = None,
        exclude_usetypes: Optional[list[str]] = None,
        top: int = 20,
    ) -> dict:
        params: dict[str, Any] = {
            "metric": metric,
            "scope": scope,
            "decay": decay,
            "min_subtree_size": min_subtree_size,
            "top": top,
        }
        if parent_id is not None:
            params["parent_id"] = parent_id
        if exclude_usetypes:
            params["exclude_usetypes"] = ",".join(exclude_usetypes)
        return self._request("GET", "/graph/subtree-authority", params=params)

    def graph_spines(
        self,
        *,
        root_id: int,
        metric: str = "pagerank",
        scope: str = "links",
        branching_threshold: float = 0.85,
        max_paths: int = 5,
        max_depth: Optional[int] = None,
    ) -> dict:
        params: dict[str, Any] = {
            "root_id": root_id,
            "metric": metric,
            "scope": scope,
            "branching_threshold": branching_threshold,
            "max_paths": max_paths,
        }
        if max_depth is not None:
            params["max_depth"] = max_depth
        return self._request("GET", "/graph/spines", params=params)

    def graph_communities(
        self,
        *,
        scope: str = "links",
        parent_id: Optional[int] = None,
        resolution: float = 1.0,
        min_size: int = 2,
    ) -> dict:
        params: dict[str, Any] = {
            "scope": scope,
            "resolution": resolution,
            "min_size": min_size,
        }
        if parent_id is not None:
            params["parent_id"] = parent_id
        return self._request("GET", "/graph/communities", params=params)

    def graph_diff(
        self, *, since: Optional[str] = None, until: Optional[str] = None
    ) -> dict:
        params: dict[str, Any] = {}
        if since:
            params["since"] = since
        if until:
            params["until"] = until
        return self._request("GET", "/graph/diff", params=params)

    def graph_stats(self) -> dict:
        return self._request("GET", "/graph/stats")

    def graph_lint(
        self,
        *,
        scope: str = "links",
        parent_id: Optional[int] = None,
        exclude_usetypes: Optional[list[str]] = None,
        orphan_threshold: int = 1,
        stale_threshold_days: int = 90,
        coverage_top_k: int = 20,
        include_summaries_usetype: Optional[list[str]] = None,
    ) -> dict:
        body: dict[str, Any] = {
            "scope": scope,
            "orphan_threshold": orphan_threshold,
            "stale_threshold_days": stale_threshold_days,
            "coverage_top_k": coverage_top_k,
        }
        if parent_id is not None:
            body["parent_id"] = parent_id
        if exclude_usetypes is not None:
            body["exclude_usetypes"] = exclude_usetypes
        if include_summaries_usetype is not None:
            body["include_summaries_usetype"] = include_summaries_usetype
        return self._request("POST", "/graph/lint", json_body=body)

    # -- view (Phase 2) ----------------------------------------------------

    def view(
        self,
        document_id: int,
        *,
        include: Optional[list[str]] = None,
        limit_children: int = 20,
        link_direction: str = "both",
    ) -> dict:
        params: dict[str, Any] = {
            "limit_children": limit_children,
            "link_direction": link_direction,
        }
        if include:
            params["include"] = ",".join(include)
        return self._request("GET", f"/view/{document_id}", params=params)

    def view_breadcrumbs(self, document_id: int) -> dict:
        return self._request("GET", f"/view/breadcrumbs/{document_id}")

    def view_back_references(self, document_id: int, *, limit: int = 50) -> dict:
        return self._request(
            "GET", f"/view/back-references/{document_id}", params={"limit": limit}
        )

    def view_expand_children(
        self, document_id: int, *, offset: int = 0, limit: int = 20
    ) -> list:
        return self._request(
            "GET",
            f"/view/{document_id}/expand-children",
            params={"offset": offset, "limit": limit},
        )


# --------------------------------------------------------------------------
# argparse helpers
# --------------------------------------------------------------------------


def add_base_url_arg(parser: argparse.ArgumentParser) -> None:
    """Add a ``--base-url`` flag with the standard default-resolution chain."""
    parser.add_argument(
        "--base-url",
        default=None,
        help=(
            f"JMFTS API base URL. Falls back to JMFTS_API_BASE_URL env "
            f"or {DEFAULT_BASE_URL}."
        ),
    )


def add_pretty_arg(parser: argparse.ArgumentParser) -> None:
    """Add a ``--pretty`` flag for human-readable output."""
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Human-readable output instead of compact JSON.",
    )


def client_from_args(args: argparse.Namespace) -> JMFTSClient:
    return JMFTSClient(base_url=getattr(args, "base_url", None))


def emit_json(payload: Any, pretty: bool = False) -> None:
    """Print a JSON payload (or human-shape) to stdout."""
    if pretty:
        json.dump(payload, sys.stdout, indent=2, default=str, sort_keys=False)
        sys.stdout.write("\n")
    else:
        json.dump(payload, sys.stdout, default=str)
        sys.stdout.write("\n")


def die(message: str, exit_code: int = 1) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(exit_code)
