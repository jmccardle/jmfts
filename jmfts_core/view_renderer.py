"""Pure-Python helpers for /view/{id} rendering.

Markdown stays markdown. The server resolves cross-references and footnote
markers; clients render to HTML / terminal / whatever they like.

Two helpers:

- ``resolve_references`` — replace ``[[doc:N]]`` and ``[[N]]`` patterns in
  the body with ``[Title](/view/N)`` and append a footnote section per
  ``link_handling`` rule.
- ``build_children_stubs`` — derive the per-child preview metadata depending
  on ``child_handling``.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional


_REF_PATTERN = re.compile(r"\[\[(?:doc:)?(\d+)\]\]")


def resolve_references(
    content: Optional[str],
    *,
    link_handling: str,
    title_lookup: dict[int, Optional[str]],
    outbound_links: Iterable[dict],
    triples: Iterable[dict],
) -> str:
    """Resolve [[N]] / [[doc:N]] references and append a footnote section.

    - Wiki-style references inside the body are always converted to
      ``[Title](/view/N)`` markdown links so the rendered output is
      navigable regardless of ``link_handling``.
    - The footnote section appended at the end depends on ``link_handling``:
      ``footnotes`` (default) emits a numbered list of links + triples;
      ``inline-citations`` produces a "References" header without numbers;
      ``sidebar`` prefixes ``<!-- sidebar -->`` so a renderer can split it
      out; ``hidden`` skips the section entirely.
    """
    if not content:
        body = ""
    else:
        body = _REF_PATTERN.sub(
            lambda m: _wikilink(int(m.group(1)), title_lookup),
            content,
        )

    if link_handling == "hidden":
        return body

    refs: list[str] = []
    n = 0
    for lk in outbound_links:
        n += 1
        target_id = lk.get("target_id")
        title = lk.get("title") or title_lookup.get(target_id) or f"#{target_id}"
        relation = lk.get("link_type") or "links_to"
        prefix = f"[{n}] " if link_handling == "footnotes" else "- "
        refs.append(
            f"{prefix}**{relation}** [{title}](/view/{target_id})"
        )
    for tp in triples:
        n += 1
        obj_id = tp.get("object_id")
        obj_title = tp.get("object_title") or title_lookup.get(obj_id) or f"#{obj_id}"
        pred = tp.get("predicate_name") or "?"
        prefix = f"[{n}] " if link_handling == "footnotes" else "- "
        refs.append(f"{prefix}**{pred}** → [{obj_title}](/view/{obj_id})")

    if not refs:
        return body

    header = "## References" if link_handling == "inline-citations" else "## Footnotes"
    sidebar_marker = "<!-- sidebar -->\n" if link_handling == "sidebar" else ""

    sep = "\n\n" if body and not body.endswith("\n") else "\n"
    return f"{body}{sep}\n{sidebar_marker}{header}\n\n" + "\n".join(refs) + "\n"


def _wikilink(doc_id: int, title_lookup: dict[int, Optional[str]]) -> str:
    title = title_lookup.get(doc_id) or f"doc {doc_id}"
    return f"[{title}](/view/{doc_id})"


def build_children_stubs(
    children: Iterable[dict],
    *,
    child_handling: str,
    preview_chars: int = 200,
) -> list[dict]:
    """Build the ``children_stubs`` array for a ViewResponse.

    ``child_handling``:
      - ``hidden``: returns ``[]`` (caller should skip).
      - ``collapsed``: short preview only.
      - ``inline-headings``: preview + first heading line if present.
      - ``first-paragraph``: includes the first paragraph (used for log feeds).

    Each child dict in input must include ``id``, ``title``, ``usetype``,
    ``content`` (may be None), and ``child_count`` (computed elsewhere).
    """
    if child_handling == "hidden":
        return []

    out: list[dict] = []
    for child in children:
        content = child.get("content") or ""
        if child_handling == "first-paragraph":
            preview = _first_paragraph(content)
        elif child_handling == "inline-headings":
            heading = _first_heading(content)
            preview = (heading or "") + ("\n\n" if heading else "")
            preview += _truncate(content, preview_chars)
            preview = preview.strip()
        else:  # collapsed (default)
            preview = _truncate(content, preview_chars)

        out.append(
            {
                "id": child.get("id"),
                "title": child.get("title"),
                "usetype": child.get("usetype"),
                "preview": preview or None,
                "child_count": int(child.get("child_count", 0)),
                "expand_url": f"/view/{child.get('id')}",
            }
        )
    return out


def _truncate(text: Optional[str], n: int) -> str:
    if not text:
        return ""
    text = text.strip()
    if len(text) <= n:
        return text
    return text[:n].rstrip() + "…"


def _first_paragraph(text: Optional[str]) -> str:
    if not text:
        return ""
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    return paras[0] if paras else ""


def _first_heading(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped
    return None
