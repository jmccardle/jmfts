"""Subtree RBAC enforcement engine.

Model (see ``migration 006`` and ``jmfts_core/models/principal.py``):

  * An ACCESS-CONTROL ROOT (ACR) is any document with ≥1 row in ``access_grants``.
    Being an ACR is DEFINED by having grants — no flag on ``documents`` — so
    marking/unmarking a root is just creating/deleting its grants.
  * A principal's effective right on a document D is the HIGHEST level granted on any
    ACR at-or-above D on its tree ``path`` (max-over-path). Grants are ADDITIVE: a
    deeper ACR can only widen access, never restrict what an ancestor granted.
  * A document under NO ACR is unprotected — readable and writable by anyone. This is
    the single-user default and is exactly what keeps the benchmark/default path
    byte-identical: with no grants anywhere, every function below is a no-op.
  * The synthetic OWNER (the shared bearer / ephemeral boot token) and the absence of
    a bound principal (in-process callers) both BYPASS every check.

``documents.path`` is a JSONB array of STRICT ancestors (root-first, excludes self),
GIN-indexed as ``idx_documents_path``. "D is within ACR R" is therefore
``R = D.id OR R ∈ D.path`` — the same ``path @> jsonb_build_array(R)`` containment the
rest of the codebase already uses for subtree filtering.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, not_, or_, select
from sqlalchemy.orm import Session

from jmfts_core.database import get_session
from jmfts_core.models.document import Document, DocumentLink
from jmfts_core.models.principal import AccessGrant, ApiToken, Principal
from jmfts_core.principal_context import CurrentPrincipal, get_current_principal


def _bypass(principal: Optional[CurrentPrincipal]) -> bool:
    """Owner and unbound (in-process) callers skip all access control."""
    return principal is None or principal.is_owner


def acr_ids(session: Session) -> list[int]:
    """Every document that is an access-control root (has ≥1 grant)."""
    return list(session.execute(select(AccessGrant.document_id).distinct()).scalars())


def _read_root_ids(session: Session, principal_id: int) -> list[int]:
    """ACRs where this principal has read OR write (write implies read)."""
    return list(
        session.execute(
            select(AccessGrant.document_id).where(AccessGrant.principal_id == principal_id)
        ).scalars()
    )


def _write_root_ids(session: Session, principal_id: int) -> list[int]:
    """ACRs where this principal has write."""
    return list(
        session.execute(
            select(AccessGrant.document_id).where(
                AccessGrant.principal_id == principal_id,
                AccessGrant.level == "write",
            )
        ).scalars()
    )


# --- SQLAlchemy predicate (vector / full-text search over select(Document)) --------


def _within_any(root_ids: list[int]):
    """ORM predicate: Document is at-or-below any of ``root_ids``. None if empty."""
    if not root_ids:
        return None
    return or_(
        Document.id.in_(root_ids),
        *[Document.path.op("@>")(func.jsonb_build_array(r)) for r in root_ids],
    )


def readable_filter(session: Session, principal: Optional[CurrentPrincipal] = None):
    """A SQLAlchemy boolean predicate scoping ``Document`` to what ``principal`` may
    read, or ``None`` when no filtering is needed (owner/unbound, or no ACRs exist).
    """
    if principal is None:
        principal = get_current_principal()
    if _bypass(principal):
        return None
    acrs = acr_ids(session)
    if not acrs:
        return None
    within_gov = _within_any(acrs)  # non-None: acrs is non-empty
    within_read = _within_any(_read_root_ids(session, principal.id))
    if within_read is None:
        return not_(within_gov)  # principal holds no grants → only ungoverned docs
    return or_(within_read, not_(within_gov))


# --- Inlined-SQL fragment (BM25 / MaxSim raw text() queries) -----------------------


def _within_sql(alias: str, root_ids: list[int]) -> str:
    """Raw-SQL form of ``_within_any``. ``root_ids`` are our own integer PKs, so they
    are safe to inline — mirroring how ``parent_id`` is already inlined in these
    queries (BM25 ``jsonb_build_array(:parent_id)``, MaxSim ``{parent_id}``)."""
    if not root_ids:
        return "FALSE"
    ids = ",".join(str(int(r)) for r in root_ids)
    arr = ",".join(f"jsonb_build_array({int(r)})" for r in root_ids)
    return f"({alias}.id IN ({ids}) OR {alias}.path @> ANY(ARRAY[{arr}]))"


def readable_sql(
    session: Session, alias: str = "d", principal: Optional[CurrentPrincipal] = None
) -> Optional[str]:
    """A boolean SQL fragment (referencing ``alias``, a ``documents`` alias) to AND into
    a raw ``WHERE``, or ``None`` when no filtering is needed."""
    if principal is None:
        principal = get_current_principal()
    if _bypass(principal):
        return None
    acrs = acr_ids(session)
    if not acrs:
        return None
    within_read = _within_sql(alias, _read_root_ids(session, principal.id))
    within_gov = _within_sql(alias, acrs)
    return f"({within_read} OR NOT {within_gov})"


# --- Point checks (direct get / write verbs) ---------------------------------------


def _governing_acrs(doc: Document, acr_ids: set[int]) -> set[int]:
    """The ACRs at-or-above ``doc`` — its ``path`` ancestors plus itself."""
    chain = set(doc.path or []) | {doc.id}
    return chain & acr_ids


def governing_acrs(session: Session, doc: Document) -> list[int]:
    """The access-control roots at-or-above ``doc``, sorted. Empty means UNPROTECTED.

    Asked of the tree, not of a principal: an empty list means no grant anywhere covers
    this document, so every authenticated caller may read and write it. That is the
    documented default and it is opt-out, which is why a caller that just created a node
    should be able to see the answer — ``FileUploadResponse.governed`` and
    ``IngestResponse.governed`` are where it is reported, and ``GET /access/audit`` is the
    corpus-wide version.
    """
    return sorted(_governing_acrs(doc, set(acr_ids(session))))


def can_read(session: Session, doc: Document, principal: Optional[CurrentPrincipal] = None) -> bool:
    """Whether ``principal`` may retrieve ``doc`` (existence-level read)."""
    if principal is None:
        principal = get_current_principal()
    if _bypass(principal):
        return True
    gov = _governing_acrs(doc, set(acr_ids(session)))
    if not gov:
        return True  # ungoverned → open
    return bool(gov & set(_read_root_ids(session, principal.id)))


def can_write(
    session: Session, doc: Document, principal: Optional[CurrentPrincipal] = None
) -> bool:
    """Whether ``principal`` may modify ``doc`` / add children under it / reparent it."""
    if principal is None:
        principal = get_current_principal()
    if _bypass(principal):
        return True
    gov = _governing_acrs(doc, set(acr_ids(session)))
    if not gov:
        return True  # ungoverned → open
    return bool(gov & set(_write_root_ids(session, principal.id)))


# --- The access key: a document's governance, as a comparable value ----------------


#: A document's effective access, canonicalised: ``(principal_id, level)`` pairs sorted by
#: principal id, at most one pair per principal. Empty means the document is under no ACR
#: — ungoverned, and therefore readable by everyone.
AccessKey = tuple[tuple[int, str], ...]


def access_key(session: Session, doc: Document) -> AccessKey:
    """The effective access of ``doc``, as a value that can be compared and stored.

    The INVERSE of ``_read_root_ids``: that answers "which ACRs may this principal read",
    this answers "which principals may read this document, and at what level". One query
    over the ACRs at-or-above ``doc`` — the same ``[D.id] + D.path`` chain
    ``_governing_acrs`` intersects — with max-over-path collapsed per principal, because
    grants are additive and ``write`` implies ``read``.

    Two documents with the same key are governed identically even when they hang under
    different access-control roots. That is what lets ``SPRINT_0_3_0.md`` 7.5 key an
    entities root by ACCESS rather than by tree position: one entities root per distinct
    access, not one per ACR.

    The empty key is not a special case. A document under no ACR has no grants on its
    chain, so the key is ``()``, and an entities root carrying no grants is itself under no
    ACR — public, by the same rule that makes its source document public.
    """
    chain = list(doc.path or []) + [doc.id]
    rows = session.execute(
        select(AccessGrant.principal_id, AccessGrant.level).where(
            AccessGrant.document_id.in_(chain)
        )
    ).all()
    best: dict[int, str] = {}
    for principal_id, level in rows:
        if best.get(principal_id) != "write":  # write beats read; max-over-path
            best[principal_id] = level
    return tuple(sorted(best.items()))


def access_key_text(key: AccessKey) -> str:
    """The storage form of an access key: ``"7:read,12:write"``, or ``""`` for ungoverned.

    Text and not JSONB because the whole point of the key is EQUALITY — it is a UNIQUE
    column in ``entity_roots``, and two documents with identical access must collide there.
    ``access_key`` has already sorted and deduplicated the pairs, so the encoding is
    canonical: equal accesses produce equal strings.
    """
    return ",".join(f"{principal_id}:{level}" for principal_id, level in key)


# --- Write-gate guards + list read filter (raise HTTP-mapped errors) ---------------


class AccessDeniedError(Exception):
    """The current principal may READ the target but not perform this WRITE (→ 403).

    Distinct from the hidden case: a target the principal cannot even read raises
    LookupError (→ 404), so a read-only holder gets an honest 403 while a non-reader
    can't tell the document from a missing one (existence-hiding).
    """

    def __init__(self, doc_id: Optional[int], action: str = "modify"):
        self.doc_id = doc_id
        self.action = action
        super().__init__(f"Access denied: principal may not {action} document {doc_id}")


def filter_readable(session: Session, docs, principal: Optional[CurrentPrincipal] = None):
    """The sublist of ``docs`` the principal may read, order preserved. Computes the ACR
    and read-root sets once, so it is two queries regardless of list length."""
    if principal is None:
        principal = get_current_principal()
    if _bypass(principal):
        return list(docs)
    acrs = set(acr_ids(session))
    if not acrs:
        return list(docs)
    read_roots = set(_read_root_ids(session, principal.id))
    out = []
    for d in docs:
        gov = _governing_acrs(d, acrs)
        if not gov or (gov & read_roots):
            out.append(d)
    return out


def readable_id_subset(
    session: Session, ids, principal: Optional[CurrentPrincipal] = None
) -> set[int]:
    """The subset of document ``ids`` the principal may read, resolved in ONE query.

    The id-based counterpart to ``filter_readable`` — for hiding EDGES (document links,
    graph neighbors, triple subject/object endpoints) that point to documents the principal
    cannot retrieve, where only the referenced id is in hand. Owner/unbound callers and the
    no-ACR case return every id unchanged (nothing to hide)."""
    id_set = {i for i in ids if i is not None}
    if not id_set:
        return set()
    pred = readable_filter(session, principal)
    if pred is None:  # owner / unbound / no ACRs → nothing is hidden
        return set(id_set)
    rows = session.execute(select(Document.id).where(Document.id.in_(id_set)).where(pred)).scalars()
    return set(rows)


def readable_id_subquery(session: Session, principal: Optional[CurrentPrincipal] = None):
    """The readable document ids as a SUBQUERY, or ``None`` when nothing is hidden.

    The third form of the same rule, and it exists for one reason: ``readable_id_subset``
    can only filter rows that have already been fetched, so a statement carrying
    ``LIMIT``/``OFFSET`` cuts its page BEFORE the gate is applied and hands back a page the
    filter then empties. That is ``SPRINT_0_5_0.md`` Block A finding 7 on
    ``TripleRepository.query_triples`` and the same family as ``SPRINT_0_4_0.md`` Block A:
    a short page that cannot be told from an exhausted one. ANDed into the statement
    instead, ``LIMIT`` counts rows the principal may read and the ordinary paging contract
    means what it says again.

    A plain ``IN (SELECT …)`` rather than a correlated ``EXISTS`` per endpoint, because the
    predicate ``readable_filter`` builds names ``Document`` itself rather than an alias, and
    a triple has TWO document endpoints to check: one uncorrelated subquery is evaluated
    once for both, where two correlated ``EXISTS`` would need two aliases and two
    evaluations per row. Postgres plans ``IN (SELECT …)`` as a semi-join either way.

    ``None`` has the same meaning it has in ``readable_filter`` and ``readable_sql``:
    owner, unbound, or no ACRs anywhere — the default deployment, where the caller must add
    no clause at all and the query stays byte-identical.
    """
    pred = readable_filter(session, principal)
    if pred is None:
        return None
    return select(Document.id).where(pred).scalar_subquery()


def require_write(
    session: Session,
    doc: Document,
    action: str = "modify",
    principal: Optional[CurrentPrincipal] = None,
) -> None:
    """Enforce write on ``doc``. Hidden (unreadable) → LookupError (404); readable but
    not writable → AccessDeniedError (403). No-op for owner/unbound callers."""
    if principal is None:
        principal = get_current_principal()
    if _bypass(principal):
        return
    if not can_read(session, doc, principal):
        raise LookupError(f"Document {doc.id} not found")
    if not can_write(session, doc, principal):
        raise AccessDeniedError(doc.id, action)


def require_add_child(
    session: Session, parent: Document, principal: Optional[CurrentPrincipal] = None
) -> None:
    """Write gate for adding a child under ``parent``. A parent the principal cannot READ
    raises ValueError mirroring 'parent does not exist' (existence-hiding, preserving
    create()'s 400); readable but not writable raises AccessDeniedError (403)."""
    if principal is None:
        principal = get_current_principal()
    if _bypass(principal):
        return
    if not can_read(session, parent, principal):
        raise ValueError(f"Parent document {parent.id} does not exist")
    if not can_write(session, parent, principal):
        raise AccessDeniedError(parent.id, "add a child to")


# --- Edge write gates: WRITE on the source, READ on the target ---------------------
#
# `SPRINT_0_6_0.md` Block A step 1, and Part 4 question 4.1 is the argument. Three
# candidates were weighed. WRITE ON BOTH ENDS is the narrowest and refuses a legitimate
# citation of a document you may read and not modify. WRITE ON SOURCE ONLY closes nothing
# the URL does not already imply, and it was rejected on a concrete leak rather than on
# principle: a principal may then create an edge to a document it cannot read at all, and
# that row reaches `/graph/neighbors`, `/graph/centrality` and `/graph/spines` — edge
# injection into a graph the injecting principal cannot see. WRITE ON SOURCE, READ ON
# TARGET is the one that makes the READ gate on edges coherent, because
# `DocumentRepository.get_links` already hides an edge whose far endpoint the reader
# cannot read (`repositories/document.py:1246`), and hiding an edge from a reader while
# letting that same reader create it is the inconsistency.


def require_edge_write(
    session: Session,
    source_id: int,
    target_id: int,
    principal: Optional[CurrentPrincipal] = None,
) -> None:
    """Enforce CREATING an edge ``source → target``: write on source, read on target.

    Failure spellings follow :func:`require_add_child`, which is the house style: a
    document the principal cannot READ is spelled as missing (``LookupError`` → 404) so
    the gate leaks no existence, and a readable-but-unwritable SOURCE is
    ``AccessDeniedError`` → 403.

    **The source is settled completely before the target is looked at.** Otherwise a
    principal with no write anywhere could use this verb as an existence oracle for target
    ids, reading 404-vs-403 off a call that was never going to succeed.

    Ungoverned stays open, and that is not a gap: ``can_write`` returns True when no ACR
    governs the node (:172), so a deployment with no grants anywhere never gets past
    ``_bypass`` and this function costs it nothing. Access control here is opt-in.

    Takes ids rather than ``Document`` rows so that the bypass path — owner, unbound
    in-process callers, and every deployment with no grants — does not pay for two loads
    it will not read. That matters: ``summarization.py`` mints one bridge edge per pair.
    """
    if principal is None:
        principal = get_current_principal()
    if _bypass(principal):
        return
    source = session.get(Document, source_id)
    if source is None or not can_read(session, source, principal):
        raise LookupError(f"Document {source_id} not found")
    if not can_write(session, source, principal):
        raise AccessDeniedError(source_id, "create a link from")
    target = session.get(Document, target_id)
    if target is None or not can_read(session, target, principal):
        raise LookupError(f"Document {target_id} not found")


def require_edge_delete(
    session: Session, link: DocumentLink, principal: Optional[CurrentPrincipal] = None
) -> None:
    """Enforce DELETING ``link``. Which end is checked depends on who asserted it.

    ``DocumentLink.derived_by`` (migration 019, and see
    ``DocumentRepository.rederive_links``) is what distinguishes a rule-derived edge from
    one a principal asserted, and it is the only record of authorship an edge carries.

    * ``derived_by IS NOT NULL`` — a rule derived this edge FROM its source, so the source
      is the asserting end and write on the source is what it takes. Write on the target
      is deliberately not enough: the rule would put the row back on its next run, so
      accepting a delete there would be a verb that reports success and changes nothing.
    * ``derived_by IS NULL`` — nothing recorded who asserted it, so write on EITHER end
      is enough. Guessing that the source asserted it would refuse a curator with write on
      the target and no way to tell whether the guess was right.

    Existence-hiding as everywhere else: a principal that can read neither endpoint is
    told the link does not exist rather than that it may not touch it.
    """
    if principal is None:
        principal = get_current_principal()
    if _bypass(principal):
        return
    source = session.get(Document, link.source_id)
    target = session.get(Document, link.target_id)
    if source is None or target is None:
        # A link whose endpoint row is gone is not a link this function can reason about;
        # the FK says it cannot happen, and if it does, "not found" is the honest answer.
        raise LookupError(f"Link {link.id} not found")
    if link.derived_by is not None:
        require_write(session, source, "delete a derived link from", principal)
        return
    if can_write(session, source, principal) or can_write(session, target, principal):
        return
    if can_read(session, source, principal) or can_read(session, target, principal):
        # The source names the denial because an edge is written source-first everywhere
        # else in this module; the caller may have asked through either end, and the
        # message is about the EDGE rather than about which end it was reached by.
        raise AccessDeniedError(link.source_id, "delete a link on")
    raise LookupError(f"Link {link.id} not found")


# --- Identity: token → principal resolution + owner-only management guard ----------


def hash_token(token: str) -> str:
    """The SHA-256 hex stored for a bearer token (never the token itself)."""
    return hashlib.sha256(token.encode()).hexdigest()


def resolve_principal_token(
    token: str, session: Optional[Session] = None
) -> Optional[CurrentPrincipal]:
    """Resolve a NON-owner bearer token to its principal, or None if unknown/expired.

    The owner token is matched separately in the auth layer (constant-time, no DB); this
    only handles DB-backed identities. It fails CLOSED: any DB error resolves to None
    (→ 401) rather than raising, so an unknown token never 500s and the owner path — which
    never reaches here — keeps working even with the database down.
    """
    h = hash_token(token)

    def _lookup(s: Session) -> Optional[CurrentPrincipal]:
        row = s.execute(select(ApiToken).where(ApiToken.token_hash == h)).scalar_one_or_none()
        if row is None:
            return None
        if row.expires_at is not None and row.expires_at < datetime.now(timezone.utc):
            return None
        p = s.get(Principal, row.principal_id)
        if p is None:
            return None
        return CurrentPrincipal(id=p.id, name=p.name, is_owner=p.is_owner)

    try:
        if session is not None:
            return _lookup(session)
        with get_session() as s:
            return _lookup(s)
    except Exception:
        return None


def require_owner(principal: Optional[CurrentPrincipal] = None) -> None:
    """Gate access-control MANAGEMENT (principals, tokens, grants) to the owner. Unbound
    in-process callers (scripts, seeding) count as owner; any bound non-owner is denied."""
    if principal is None:
        principal = get_current_principal()
    if principal is None or principal.is_owner:
        return
    raise AccessDeniedError(None, "manage access control")
