"""Safe URL fetcher + HTML→markdown for the wiki:url ingest pipeline.

SSRF guard rejects RFC1918 / loopback / link-local hosts unless explicitly
allowed via ``JMFTS_ALLOW_LOCAL_FETCHES=1``. Size cap and content-type
allowlist enforced.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
from typing import Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)


DEFAULT_MAX_BYTES = 5_000_000
DEFAULT_TIMEOUT = 30.0
ALLOWED_CONTENT_TYPES = (
    "text/html",
    "text/plain",
    "text/markdown",
    "application/xhtml+xml",
)


class UrlFetchError(Exception):
    """Raised when a URL fetch fails for reasons that should reach the caller."""


def fetch_url(
    url: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout: float = DEFAULT_TIMEOUT,
    allow_local: Optional[bool] = None,
) -> tuple[str, str]:
    """Fetch URL content. Returns ``(content_text, content_type)``.

    Raises ``UrlFetchError`` for SSRF-guard rejections, size cap violations,
    disallowed content types, or HTTP failures.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UrlFetchError(f"unsupported scheme {parsed.scheme!r}")
    if not parsed.hostname:
        raise UrlFetchError("URL has no hostname")

    if allow_local is None:
        allow_local = os.environ.get("JMFTS_ALLOW_LOCAL_FETCHES") == "1"

    if not allow_local and _is_private_host(parsed.hostname):
        raise UrlFetchError(
            f"blocked: {parsed.hostname} resolves to a private/loopback address "
            f"(set JMFTS_ALLOW_LOCAL_FETCHES=1 to override)"
        )

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(url, headers={"User-Agent": "jmfts/0.1 (+wiki)"})
    except httpx.HTTPError as e:
        raise UrlFetchError(f"http error: {e}") from e

    if resp.status_code >= 400:
        raise UrlFetchError(f"HTTP {resp.status_code}: {resp.text[:200]}")

    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
    if not any(ctype.startswith(p) for p in ALLOWED_CONTENT_TYPES):
        raise UrlFetchError(f"disallowed content-type: {ctype!r}")

    if len(resp.content) > max_bytes:
        raise UrlFetchError(
            f"response too large: {len(resp.content)} bytes (cap={max_bytes})"
        )

    return resp.text, ctype


def _is_private_host(host: str) -> bool:
    """Resolve host and reject if any address is private / loopback / link-local."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        # Failed name resolution — let httpx report it later
        return False
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return True
    return False


def html_to_markdown(html: str) -> str:
    """HTML → markdown using ``markdownify``. Falls back to text if unavailable."""
    try:
        from markdownify import markdownify as _mdify
    except ImportError:
        # Fallback: strip tags crudely, just so the pipeline keeps working.
        logger.warning("markdownify not installed; emitting stripped text")
        return _strip_tags(html)

    # Strip <script> and <style> first to avoid leaking JS/CSS into the body.
    html = re.sub(r"<script[\s\S]*?</script>", "", html, flags=re.IGNORECASE)
    html = re.sub(r"<style[\s\S]*?</style>", "", html, flags=re.IGNORECASE)
    return _mdify(html, heading_style="ATX")


def _strip_tags(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    return text.strip()
