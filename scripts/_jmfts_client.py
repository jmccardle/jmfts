"""Shared httpx-based client for JMFTS scripts.

Replaces the ad-hoc `requests` usage scattered across older scripts. Every
new script under ``scripts/`` should import this module instead of building
its own HTTP calls.

Base URL resolution:
1. ``--base-url`` flag (via ``add_base_url_arg`` + ``client_from_args``)
2. ``JMFTS_API_BASE_URL`` environment variable
3. Default: ``http://localhost:8100``

Authentication: ``JMFTS_API_TOKEN``, sent as ``Authorization: Bearer <token>`` on every
request. Every JMFTS route except ``/health`` and the API docs requires it
(``jmfts_core/rest/auth.py``), and a blank token on the SERVER means "generate one and
print it at startup", never "allow all" -- so there is no deployment in which leaving it
unset here works. Until 2026-10-17 this module sent no header at all, and every script
the plugin skills call answered 401; that is the docs punch list's P0 item 1.

A 401 is therefore turned into an exit that names the variable, rather than left to the
generic ``HTTPStatusError`` handler each script has: "401 Invalid API token" does not say
which environment variable the scripts read, and that is the one thing the caller needs.
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
        token: Optional[str] = None,
    ):
        resolved = base_url or os.environ.get("JMFTS_API_BASE_URL") or DEFAULT_BASE_URL
        self.base_url = resolved.rstrip("/")
        # `None` and "" both mean "not configured". The header is omitted rather than sent
        # empty, so the server's 401 says "missing" instead of "malformed", and `_request`
        # adds the hint naming the variable either way.
        self.token = token if token is not None else os.environ.get("JMFTS_API_TOKEN", "")
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout, headers=headers)

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
        if resp.status_code == 401:
            _die_unauthorized(resp, token_sent=bool(self.token))
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
        llm_model: Optional[str] = None,
        usetype: Optional[str] = None,
        parent_id: Optional[int] = None,
    ) -> dict:
        body: dict[str, Any] = {
            "query": query,
            "search_method": search_method,
            "top_k": top_k,
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

    def get_children(self, doc_id: int, *, usetype: Optional[str] = None, limit: int = 100) -> Any:
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
        return self._request("GET", f"/documents/{doc_id}/links", params={"direction": direction})

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

    # -- ingest ------------------------------------------------------------

    def ingest(
        self,
        *,
        content: str,
        usetype: str,
        title: Optional[str] = None,
        parent_id: Optional[int] = None,
        options: Optional[dict] = None,
        llm_model: Optional[str] = None,
    ) -> dict:
        """``POST /ingest``: store, queue, drain, and return the finished tree.

        ``options`` are ingest option overrides, option group -> parameters, e.g.
        ``{"facts": {"enabled": True}}`` or ``{"structure": {"max_words": 300}}``. The
        groups and their defaults are ``jmfts_core/ingest_options.py``; an unknown group or
        option is a 400 naming it. ``GET /ingest/pipelines`` lists what each usetype
        resolves to.

        There is no ``pipeline_config`` parameter. That was the deleted synchronous
        pipeline's stage vocabulary and the server refuses it with a 400. Until 2026-10-17
        this client still sent it, so every ``wiki_ingest_*`` script failed.
        """
        body: dict[str, Any] = {"content": content, "usetype": usetype}
        if title is not None:
            body["title"] = title
        if parent_id is not None:
            body["parent_id"] = parent_id
        if options:
            body["options"] = options
        if llm_model is not None:
            body["llm_model"] = llm_model
        return self._request("POST", "/ingest", json_body=body)

    def index_document(self, doc_id: int, *, index_name: str = "default") -> Any:
        return self._request("POST", f"/indexes/{index_name}/index-document/{doc_id}")

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

    def find_path(self, *, from_id: int, to_id: int, max_depth: int = 4) -> Any:
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

    def graph_diff(self, *, since: Optional[str] = None, until: Optional[str] = None) -> dict:
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
        return self._request("GET", f"/view/back-references/{document_id}", params={"limit": limit})

    def view_expand_children(self, document_id: int, *, offset: int = 0, limit: int = 20) -> list:
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
            f"JMFTS API base URL. Falls back to JMFTS_API_BASE_URL env " f"or {DEFAULT_BASE_URL}."
        ),
    )


def add_ingest_option_args(parser: argparse.ArgumentParser) -> None:
    """Add ``--options`` and ``--extract-facts/--no-extract-facts`` for ``POST /ingest``.

    These replace ``--pipeline-config``, ``--summarize`` and ``--extract-facts`` as they
    were before 2026-10-17, which spoke the deleted pipeline's stage vocabulary and made
    every ingest a 400. There is deliberately no ``--summarize``: the queue's ``summarize``
    task is what gives a container node its vectors, it always runs, and there is nothing
    for such a flag to switch off.
    """
    parser.add_argument(
        "--options",
        default=None,
        help=(
            "JSON ingest option overrides, option group -> parameters, e.g. "
            '\'{"structure": {"max_words": 300}}\'. Groups: structure, rollup, facts, '
            "embed, sheet_profile, sheet_records (jmfts_core/ingest_options.py). An unknown "
            "group or option is a 400 naming it."
        ),
    )
    parser.add_argument(
        "--extract-facts",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Shorthand for options facts.enabled. Unset keeps the usetype's default: on for "
            "markdown/raw/transcript/conversation, off for wiki:url/wiki:arxiv/wiki:pdf. "
            "Needs an LLM endpoint configured on the server."
        ),
    )


def ingest_options_from_args(args: argparse.Namespace) -> Optional[dict]:
    """The ``options`` body field from :func:`add_ingest_option_args`'s flags, or ``None``.

    Refuses rather than picks when ``--options`` and ``--extract-facts`` disagree about
    ``facts.enabled``: either answer would be one of the two things the caller typed being
    silently dropped.
    """
    options: dict = {}
    if args.options:
        try:
            options = json.loads(args.options)
        except json.JSONDecodeError as e:
            die(f"invalid --options JSON: {e}")
        if not isinstance(options, dict):
            die("--options must be a JSON object of option group -> parameters")
    if args.extract_facts is not None:
        facts = options.setdefault("facts", {})
        if not isinstance(facts, dict):
            die("--options facts must be an object")
        if "enabled" in facts and facts["enabled"] != args.extract_facts:
            die("--options facts.enabled contradicts --extract-facts/--no-extract-facts")
        facts["enabled"] = args.extract_facts
    return options or None


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


def _die_unauthorized(resp: httpx.Response, *, token_sent: bool) -> None:
    """Exit on a 401 with the server's reason and the variable that fixes it.

    Two different mistakes, told apart by whether a header went out: no token configured
    at all, or one the server does not accept. A stale token is the usual second case --
    a client and server that no longer share the same ``JMFTS_API_TOKEN``.
    """
    try:
        detail = resp.json().get("detail", resp.text)
    except ValueError:
        detail = resp.text
    if token_sent:
        hint = (
            "the server rejected JMFTS_API_TOKEN. Check it against the server's "
            "JMFTS_API_TOKEN."
        )
    else:
        hint = (
            "JMFTS_API_TOKEN is not set. Every JMFTS route but /health and the API docs needs "
            "`Authorization: Bearer <token>`; export the server's token as "
            "JMFTS_API_TOKEN and re-run."
        )
    die(f"401 from {resp.request.method} {resp.request.url}: {detail} -- {hint}")
