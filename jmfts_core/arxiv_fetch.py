"""arXiv fetch helper: metadata + PDF download.

Uses the public arXiv API (export.arxiv.org) and CDN. Returns a ``(pdf_bytes,
metadata)`` tuple ready for the PDF extractor.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


_ARXIV_API_BASE = "http://export.arxiv.org/api/query"
_ARXIV_PDF_BASE = "https://arxiv.org/pdf"
_ARXIV_ID_RE = re.compile(r"^[\d\.]+(?:v\d+)?$")
_ATOM_NS = "{http://www.w3.org/2005/Atom}"
_ARXIV_NS = "{http://arxiv.org/schemas/atom}"


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


def fetch_arxiv_metadata(arxiv_id: str, *, timeout: float = 15.0) -> dict:
    """Query the arXiv API for a single paper's metadata."""
    arxiv_id = normalize_arxiv_id(arxiv_id)
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(_ARXIV_API_BASE, params={"id_list": arxiv_id})
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise ArxivFetchError(f"arxiv API error: {e}") from e

    return _parse_arxiv_atom(resp.text, arxiv_id)


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


def fetch_arxiv(arxiv_id: str) -> tuple[bytes, dict]:
    """Combined fetch: metadata + PDF, in two requests."""
    metadata = fetch_arxiv_metadata(arxiv_id)
    pdf_bytes = fetch_arxiv_pdf(arxiv_id)
    return pdf_bytes, metadata


def _parse_arxiv_atom(xml_text: str, arxiv_id: str) -> dict:
    root = ET.fromstring(xml_text)
    entry = root.find(f"{_ATOM_NS}entry")
    if entry is None:
        raise ArxivFetchError(f"no entry returned for {arxiv_id}")

    def text(elem: Optional[ET.Element]) -> Optional[str]:
        return elem.text.strip() if elem is not None and elem.text else None

    title = text(entry.find(f"{_ATOM_NS}title"))
    summary = text(entry.find(f"{_ATOM_NS}summary"))
    published = text(entry.find(f"{_ATOM_NS}published"))
    updated = text(entry.find(f"{_ATOM_NS}updated"))

    authors = []
    for a in entry.findall(f"{_ATOM_NS}author"):
        name = text(a.find(f"{_ATOM_NS}name"))
        if name:
            authors.append(name)

    primary = entry.find(f"{_ARXIV_NS}primary_category")
    primary_category = primary.attrib.get("term") if primary is not None else None
    categories = [c.attrib.get("term") for c in entry.findall(f"{_ATOM_NS}category")]
    doi_elem = entry.find(f"{_ARXIV_NS}doi")
    doi = text(doi_elem) if doi_elem is not None else None

    return {
        "arxiv_id": arxiv_id,
        "title": title,
        "abstract": summary,
        "authors": authors,
        "categories": categories,
        "primary_category": primary_category,
        "doi": doi,
        "published": published,
        "updated": updated,
        "url_abs": f"https://arxiv.org/abs/{arxiv_id}",
        "url_pdf": f"{_ARXIV_PDF_BASE}/{arxiv_id}.pdf",
    }
