"""View Repository — fetch the bundle of data behind /view/{id}.

One repository, multiple SQL queries (not one mega-join). Each query is
straightforward and the joins live close to the model relationships.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from jmfts_core.access import can_read, filter_readable, readable_id_subset
from jmfts_core.models.document import Document, DocumentLink
from jmfts_core.models.triple import Triple
from jmfts_core.repositories.document import _sibling_order


@dataclass
class ViewBundle:
    document: Document
    ancestors: list[Document] = field(default_factory=list)
    children: list[dict] = field(default_factory=list)  # dicts with content + child_count
    outbound_links: list[dict] = field(default_factory=list)
    inbound_links: list[dict] = field(default_factory=list)
    triples: list[dict] = field(default_factory=list)


class ViewRepository:
    """Pulls a Document + ancestors + children + links + triples in one place."""

    def __init__(self, session: Session):
        self.session = session

    # -- main fetch --------------------------------------------------------

    def get_bundle(
        self,
        doc_id: int,
        *,
        include: Optional[set[str]] = None,
        limit_children: int = 20,
        children_offset: int = 0,
        link_direction: str = "both",
    ) -> Optional[ViewBundle]:
        include = include or {"children", "links", "triples"}
        doc = self.session.get(Document, doc_id)
        # Subtree RBAC: an unreadable document is indistinguishable from missing.
        if not doc or not can_read(self.session, doc):
            return None

        bundle = ViewBundle(document=doc)

        # Ancestors via Document.path (filtered to the ones the principal may read).
        if doc.path:
            ancestor_rows = filter_readable(
                self.session,
                self.session.execute(select(Document).where(Document.id.in_(doc.path)))
                .scalars()
                .all(),
            )
            by_id = {a.id: a for a in ancestor_rows}
            bundle.ancestors = [by_id[i] for i in doc.path if i in by_id]

        # Children — paginate, then cheap child_count subquery for each
        if "children" in include:
            child_rows = list(
                self.session.execute(
                    select(Document)
                    .where(Document.parent_id == doc_id)
                    .order_by(*_sibling_order())  # CR-1 sibling ordering
                    .offset(children_offset)
                    .limit(limit_children)
                )
                .scalars()
                .all()
            )
            # Subtree RBAC: drop children the principal cannot read (a nested ACR can hide
            # a child even when its parent is readable).
            child_rows = filter_readable(self.session, child_rows)
            if child_rows:
                child_ids = [c.id for c in child_rows]
                count_rows = self.session.execute(
                    select(Document.parent_id, func.count(Document.id))
                    .where(Document.parent_id.in_(child_ids))
                    .group_by(Document.parent_id)
                ).all()
                count_map = dict(count_rows)
            else:
                count_map = {}
            bundle.children = [
                {
                    "id": c.id,
                    "title": c.title,
                    "usetype": c.usetype,
                    "content": c.content,
                    "child_count": int(count_map.get(c.id, 0)),
                }
                for c in child_rows
            ]

        # Links
        if "links" in include:
            if link_direction in ("outbound", "both"):
                bundle.outbound_links = self._fetch_links(doc_id, direction="outbound")
            if link_direction in ("inbound", "both"):
                bundle.inbound_links = self._fetch_links(doc_id, direction="inbound")

        # Triples (subject or object = doc)
        if "triples" in include:
            bundle.triples = self._fetch_triples(doc_id)

        return bundle

    def _fetch_links(self, doc_id: int, *, direction: str) -> list[dict]:
        if direction == "outbound":
            cond = DocumentLink.source_id == doc_id
            other_attr = DocumentLink.target
        else:
            cond = DocumentLink.target_id == doc_id
            other_attr = DocumentLink.source

        stmt = (
            select(DocumentLink)
            .where(cond)
            .options(joinedload(other_attr))
            .order_by(DocumentLink.score.desc())
        )
        rows = self.session.execute(stmt).scalars().all()
        out: list[dict] = []
        for lk in rows:
            other = lk.target if direction == "outbound" else lk.source
            target_id = lk.target_id if direction == "outbound" else lk.source_id
            out.append(
                {
                    "id": lk.id,
                    "source_id": lk.source_id,
                    "target_id": lk.target_id,
                    "direction": direction,
                    "link_type": lk.link_type,
                    "score": float(lk.score) if lk.score is not None else 1.0,
                    "title": other.title if other is not None else None,
                    "_other_id": target_id,
                }
            )
        # Subtree RBAC: hide edges pointing to a document the principal cannot read.
        other_ids = {r["_other_id"] for r in out}
        readable = readable_id_subset(self.session, other_ids)
        if len(readable) != len(other_ids):
            out = [r for r in out if r["_other_id"] in readable]
        return out

    def _fetch_triples(self, doc_id: int) -> list[dict]:
        stmt = (
            select(Triple)
            .where((Triple.subject_id == doc_id) | (Triple.object_id == doc_id))
            .where(Triple.invalidated_at.is_(None))
            .options(
                joinedload(Triple.subject),
                joinedload(Triple.object),
                joinedload(Triple.predicate),
            )
        )
        rows = self.session.execute(stmt).scalars().all()
        out: list[dict] = []
        for t in rows:
            subj = t.subject
            obj = t.object
            pred = t.predicate
            out.append(
                {
                    "id": t.id,
                    "subject_id": t.subject_id,
                    "subject_title": subj.title if subj else None,
                    "predicate_name": pred.name if pred else None,
                    "object_id": t.object_id,
                    "object_title": obj.title if obj else None,
                    # Null unless the object IS a literal, in which case object_id is null
                    # and this is the whole object.
                    "object_literal": t.object_literal,
                    "object_datatype": t.object_datatype,
                    "fact_type": t.fact_type.value if t.fact_type else None,
                    "valid_from": t.valid_from,
                    "valid_until": t.valid_until,
                }
            )
        # Subtree RBAC: hide facts whose subject or object the principal cannot read. A
        # literal object has no document and therefore no access rule of its own — the
        # subject's is the whole check — so its null id must not reach readable_id_subset.
        endpoint_ids = {r["subject_id"] for r in out} | {
            r["object_id"] for r in out if r["object_id"] is not None
        }
        readable = readable_id_subset(self.session, endpoint_ids)
        if len(readable) != len(endpoint_ids):
            out = [
                r
                for r in out
                if r["subject_id"] in readable
                and (r["object_id"] is None or r["object_id"] in readable)
            ]
        return out

    # -- companion endpoints ----------------------------------------------

    def get_breadcrumbs(self, doc_id: int) -> Optional[list[Document]]:
        doc = self.session.get(Document, doc_id)
        # Subtree RBAC: unreadable == missing; ancestors filtered to the readable ones.
        if not doc or not can_read(self.session, doc):
            return None
        if not doc.path:
            return []
        rows = filter_readable(
            self.session,
            self.session.execute(select(Document).where(Document.id.in_(doc.path))).scalars().all(),
        )
        by_id = {a.id: a for a in rows}
        return [by_id[i] for i in doc.path if i in by_id]

    def get_back_references(self, doc_id: int, *, limit: int = 50) -> list[dict]:
        """Documents whose links or triples target this document."""
        out: list[dict] = []

        # Inbound links
        link_stmt = (
            select(DocumentLink)
            .where(DocumentLink.target_id == doc_id)
            .options(joinedload(DocumentLink.source))
            .order_by(DocumentLink.score.desc())
            .limit(limit)
        )
        for lk in self.session.execute(link_stmt).scalars().all():
            src = lk.source
            out.append(
                {
                    "document_id": lk.source_id,
                    "title": src.title if src else None,
                    "usetype": src.usetype if src else None,
                    "via": "link",
                    "relation": lk.link_type,
                    "snippet": _snippet_around(src.content, max_chars=160) if src else None,
                    "url": f"/view/{lk.source_id}",
                }
            )

        # Inbound triples (this doc is the object)
        triple_stmt = (
            select(Triple)
            .where(Triple.object_id == doc_id, Triple.invalidated_at.is_(None))
            .options(joinedload(Triple.subject), joinedload(Triple.predicate))
            .limit(limit)
        )
        for t in self.session.execute(triple_stmt).scalars().all():
            subj = t.subject
            pred = t.predicate
            out.append(
                {
                    "document_id": t.subject_id,
                    "title": subj.title if subj else None,
                    "usetype": subj.usetype if subj else None,
                    "via": "triple",
                    "relation": pred.name if pred else "?",
                    "snippet": None,
                    "url": f"/view/{t.subject_id}",
                }
            )

        # Subtree RBAC: hide referrers the principal cannot read (they point AT doc_id, so
        # each referrer's own id is the one to check).
        ref_ids = {r["document_id"] for r in out}
        readable = readable_id_subset(self.session, ref_ids)
        if len(readable) != len(ref_ids):
            out = [r for r in out if r["document_id"] in readable]
        return out


def _snippet_around(content: Optional[str], *, max_chars: int = 160) -> Optional[str]:
    if not content:
        return None
    text = " ".join(content.split())
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "…"
