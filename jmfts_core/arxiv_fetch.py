"""arXiv fetch helper: normalise an id, download the PDF.

The PDF only. The Atom-feed metadata fetch (title, authors, abstract) was deleted on
2026-10-12: nothing had called it since ``fetch_tasks._fetch_arxiv_bytes`` stopped folding
it into evidence, which that function's docstring records as a loss. Recover it from git
history (``fetch_arxiv_metadata``) if a GRAPH/triples home for it is built.
"""

from __future__ import annotations

import logging
import re

import httpx

logger = logging.getLogger(__name__)


_ARXIV_PDF_BASE = "https://arxiv.org/pdf"
_ARXIV_ID_RE = re.compile(r"^[\d\.]+(?:v\d+)?$")


class ArxivFetchError(Exception):
    pass


def normalize_arxiv_id(value: str) -> str:
    """Accept full URLs, abs/pdf paths, or bare IDs; return a clean arXiv ID."""
    value = value.strip()
    # Strip URL prefix
    m = re.search(r"arxiv\.org/(?:abs|pdf)/([^?\s#]+)", value)
    if m:
        value = m.group(1)
    if value.endswith(".pdf"):
        value = value[:-4]
    if not _ARXIV_ID_RE.match(value):
        raise ArxivFetchError(f"invalid arxiv id: {value!r}")
    return value


def fetch_arxiv_pdf(arxiv_id: str, *, timeout: float = 60.0) -> bytes:
    """Download the PDF bytes for an arXiv paper."""
    arxiv_id = normalize_arxiv_id(arxiv_id)
    url = f"{_ARXIV_PDF_BASE}/{arxiv_id}.pdf"
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(url)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise ArxivFetchError(f"arxiv PDF download failed: {e}") from e
    if not resp.content.startswith(b"%PDF"):
        raise ArxivFetchError(f"response from {url} is not a PDF")
    return resp.content
