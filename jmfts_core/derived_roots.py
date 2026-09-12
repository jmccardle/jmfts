"""Derived-tree roots: one root document per (access, tree kind) (``SPRINT_0_5_0.md`` 10).

A derived tree — a summary tree, a keyword tree, an argument tree — is a PARALLEL tree: its
leaves resolve, through link edges, to the source tree's leaves, and that leaf projection
(3.1) is total only if no derivation moved its own inputs. Today ``raptor_summarize``
reparents the nodes it summarises under the summary it produced, which is Part 1's defect;
the root this module mints is where a derived tree hangs instead, so that a derivation owns
nothing in the tree it derived from.

This module mirrors :mod:`jmfts_core.entity_roots`, and Part 0.3 says the mirroring IS the
argument: the same decisions were made for entities in ``SPRINT_0_3_0.md`` 7.5, migrated in
``sql/migrations/014_entity_roots.sql``, and written down there. One difference, and it is
open question 6.6 answered: a root is keyed by (access, tree kind) rather than by access
alone, because a shared root would make the tree kind a ``usetype`` filter over a mixed
subtree while the leaf projection is per tree.

**A root is an ordinary document with ordinary grants.** No new access concept: the root is
created with ``repo.create(usetype=DERIVED_ROOT_USETYPE, parent_id=None)`` and then given
``access_grants`` rows matching its key, which is what makes ``access.py`` treat it as an
access-control root governing exactly the principals the key names. An empty key writes no
grants, so that root is under no ACR and is public — the same rule that makes a document
under no ACR public, not an exception to it.

**THE WRITER IS ``rollup_tasks.run_summarize_tree`` (0.5.0 Block C step 11), and it is the
only caller.** An earlier version of this paragraph said nothing called
:func:`get_or_create_derived_root` and that the writer was held back; step 11 landed, so
that record is now here instead: the handler asks for the root, hangs one summary node
under it and links down to the members.

**A ROOT KEYED BY ONE DOCUMENT IS ONLY SAFE FOR MATERIAL WITH THAT DOCUMENT'S ACCESS.**
:func:`widening_descendants` is the check and it is not optional — a summary of restricted
leaves hung under a root keyed by their PUBLIC ancestor is ``SPRINT_0_3_0.md`` 13.9 for the
fourth time, reached by structure rather than by resolution. Every caller of
:func:`get_or_create_derived_root` that summarises material from BELOW ``document_id`` must
call it first and refuse when it returns anything.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from jmfts_core.access import AccessKey, access_key, access_key_text
from jmfts_core.models.derived_root import DerivedRoot
from jmfts_core.models.document import Document
from jmfts_core.models.principal import AccessGrant
from jmfts_core.repositories.document import DocumentRepository

logger = logging.getLogger(__name__)

#: The usetype carried by a derived-tree root. Distinct from the usetypes of the nodes under
#: it, which the tree's own handler chooses: a root is a container, holds no content and is
#: never a retrieval target.
#:
#: IN ``Settings.search_exclude_usetypes`` AND ``bm25_exclude_usetypes`` since step 11,
#: beside ``entity``, ``entities`` and ``summary``. A root is contentless, so it has no
#: embedding and no BM25 postings and could not be retrieved by either method before this
#: — what the exclusion holds out is the FULL-TEXT match on its title, which is
#: ``to_tsvector(title || content)`` and would answer a query for "derived" with a
#: container. The nodes under it carry
#: :data:`~jmfts_core.models.document.USETYPE_SUMMARY`, which those two lists already held
#: out, so the tree as a whole is out of default result sets and reachable by naming it.
DERIVED_ROOT_USETYPE = "derived"

#: The first tree kind. ``SPRINT_0_5_0.md`` 3.1 names the others — keyword, argument and
#: question trees — and each arrives with the handler that builds it, not before it.
SUMMARY_TREE_KIND = "summary"


def get_or_create_derived_root(session: Session, document_id: int, tree_kind: str) -> int:
    """The id of the ``tree_kind`` root for the access of ``document_id``, creating it if new.

    Get-or-create, and the retry is :func:`~jmfts_core.entity_roots.get_or_create_entities_root`'s
    for its reason: derivations run in the ingest worker and a fleet drains one queue, so two
    workers can reach a brand-new key in the same instant. ``UNIQUE (access_key, tree_kind)``
    means the loser gets an ``IntegrityError`` — inside a SAVEPOINT, which is what lets the
    losing root document and its grants disappear with it rather than leaving an orphan root
    behind. It then re-reads the winner's row, which by then is visible: the unique index
    blocked the insert until the winner committed.

    Args:
        document_id: a document in the source subtree being derived from. Its EFFECTIVE
            access is the key; the root's grants are made to match, so the derived tree is
            readable by exactly whoever can read what it derives from.
        tree_kind: which parallel tree, e.g. :data:`SUMMARY_TREE_KIND`.

    Raises:
        LookupError: ``document_id`` does not exist. A derived tree has to be keyed by the
            access of what it derives from, and there is no key without one — failing here
            is the whole protection, so there is no unkeyed fallback.
        ValueError: ``tree_kind`` is blank. The empty string is the ungoverned ACCESS key and
            carries meaning; an empty TREE KIND names no tree, and a root minted under one
            would be the shared root that 6.6 decided against, reached by accident.
    """
    if not tree_kind:
        raise ValueError(
            "tree_kind names which parallel tree the root holds and cannot be empty; "
            "an unnamed root is the shared root SPRINT_0_5_0.md 6.6 decided against"
        )
    doc = session.get(Document, document_id)
    if doc is None:
        raise LookupError(
            f"Document {document_id} does not exist; a derived tree cannot be keyed by the "
            f"access of a document that is not there"
        )
    key: AccessKey = access_key(session, doc)
    key_text = access_key_text(key)
    existing = _root_id_for(session, key_text, tree_kind)
    if existing is not None:
        return existing

    try:
        with session.begin_nested():
            root = DocumentRepository(session).create(
                title=f"Derived: {tree_kind}",
                content=None,
                parent_id=None,
                usetype=DERIVED_ROOT_USETYPE,
                # Both halves of the key are on the node as well as in `derived_roots`, so a
                # root read straight out of `documents` says what it is for. `entity_roots`
                # writes `access_key` alone for the same reason; the pair is this table's
                # key, so the pair is what goes on the node.
                structured_content={"access_key": key_text, "tree_kind": tree_kind},
                # No content, so nothing to embed. `auto_embed=True` on a contentless
                # document is a no-op, but stating it keeps the model stack out of a path a
                # storage-side worker with no torch has to be able to run.
                auto_embed=False,
            )
            # The grants ARE the access control. `access.py` defines an ACR as a document
            # with at least one grant, so writing these rows is what makes the root
            # governed — and writing none is what makes the public root public.
            for principal_id, level in key:
                session.add(
                    AccessGrant(document_id=root.id, principal_id=principal_id, level=level)
                )
            session.add(DerivedRoot(access_key=key_text, tree_kind=tree_kind, document_id=root.id))
            session.flush()
    except IntegrityError:
        raced = _root_id_for(session, key_text, tree_kind)
        if raced is None:
            raise
        logger.debug(
            "Lost the race to mint the %r derived root for %r; using %d",
            tree_kind,
            key_text,
            raced,
        )
        return raced

    logger.info("Created %r derived root %d for access key %r", tree_kind, root.id, key_text)
    return root.id


def derived_root_ids(session: Session, tree_kind: str | None = None) -> list[int]:
    """Every derived-root document id, optionally narrowed to one tree kind.

    No access filter, deliberately, for :func:`~jmfts_core.entity_roots.entity_root_ids`'s
    reason: this is the set a cross-tree walk partitions on — "the copies under OTHER roots"
    — and it must see every root regardless of who is asking. Filtering is the caller's, at
    the point where a result is returned to a principal.

    **NOTHING CALLS THIS, and it is the one function in this module that nothing calls.**
    Step 11 made :func:`get_or_create_derived_root` and :func:`widening_descendants` live;
    the first caller of this one is the cross-tree walk of ``SPRINT_0_5_0.md`` 3.1, which is
    not scoped. ``scripts.deadcode_scan`` reports it at 60% and this paragraph is the record
    of why the answer is "not yet" rather than "delete it".
    """
    stmt = select(DerivedRoot.document_id)
    if tree_kind is not None:
        stmt = stmt.where(DerivedRoot.tree_kind == tree_kind)
    return list(session.execute(stmt).scalars())


def widening_descendants(session: Session, document_id: int) -> list[int]:
    """Documents below ``document_id`` that a root keyed on it would make MORE readable.

    ``SPRINT_0_3_0.md`` 13.9, FOR THE FOURTH TIME AND BY A FOURTH ROUTE. The first three
    were resolution (a fact from a restricted document resolved against public entity
    nodes), derivation (a rule concluding a triple over a scope wider than its inputs) and
    ``raptor_summarize``'s reparent. This one is structure: a derived tree hangs OUTSIDE
    the subtree it derives from, so the grants that governed the source do not reach it,
    and the only thing standing between a restricted leaf and a world-readable summary of
    it is the key the root was minted under.

    **The rule this checks: the summary's readers must be a subset of every summarised
    node's readers.** A root minted for ``document_id`` carries ``document_id``'s effective
    access, so its readers are that key's principals — or EVERYONE when the key is empty,
    because a document under no access-control root is public (``access.py``'s module
    docstring). This returns every access-control root strictly below ``document_id`` whose
    own readers do not contain those.

    In the shipped access model that comes to one case, and stating the general rule rather
    than that case is deliberate. Grants are ADDITIVE — a deeper root widens and never
    restricts — so a governed ``document_id`` always has readers ⊆ its descendants'. The
    exception is the ungoverned one: an empty key means public, a governed node below it is
    readable by FEWER people than its own parent, and a summary of that node keyed by the
    parent would be the widening. Writing the subset test out means the check keeps
    answering correctly if additivity ever stops holding, rather than encoding today's
    consequence of it.

    Empty is the safe answer and it is the common one: a subtree with no grants below it
    costs one indexed query (``path @> [id]`` on ``idx_documents_path``) and no more.

    Raises:
        LookupError: ``document_id`` does not exist. Same reason as
            :func:`get_or_create_derived_root` — there is no key without a document, and a
            check that cannot compute the key must not report "nothing widens".
    """
    doc = session.get(Document, document_id)
    if doc is None:
        raise LookupError(
            f"Document {document_id} does not exist; whether a derived root would widen "
            f"access cannot be answered without the access it would be keyed by"
        )
    readers = {principal_id for principal_id, _ in access_key(session, doc)}
    governed_below = session.execute(
        select(AccessGrant.document_id)
        .join(Document, Document.id == AccessGrant.document_id)
        .where(Document.path.op("@>")(func.jsonb_build_array(document_id)))
        .distinct()
    ).scalars()

    widened: list[int] = []
    for below_id in governed_below:
        below = session.get(Document, below_id)
        if below is None:
            # A grant whose document was deleted between the two queries. It governs
            # nothing, so it widens nothing; the row is the access layer's to clean up.
            continue
        below_readers = {principal_id for principal_id, _ in access_key(session, below)}
        if not readers or not readers <= below_readers:
            widened.append(below_id)
    return sorted(widened)


def _root_id_for(session: Session, key_text: str, tree_kind: str) -> int | None:
    return session.execute(
        select(DerivedRoot.document_id).where(
            DerivedRoot.access_key == key_text,
            DerivedRoot.tree_kind == tree_kind,
        )
    ).scalar_one_or_none()
