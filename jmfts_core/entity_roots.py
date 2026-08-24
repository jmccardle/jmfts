"""Entities roots: one root document per distinct ACCESS (``SPRINT_0_3_0.md`` 7.5).

An entity node lives under the entities root whose grants are exactly the effective access
of the document that mentioned it. Not one root per access-control root — one root per
distinct ACCESS, so two ACRs with identical grants share a root and an entity set tracks a
sub-corpus rather than a tree position.

This module owns the mapping and nothing else: :func:`get_or_create_entities_root` is the
only way a root is minted, and :func:`entity_root_ids` is the read side that entity lookup
uses to tell "this root" from "the others".

**A root is an ordinary document with ordinary grants.** There is no new access concept
here — the root is created with ``repo.create(usetype="entities", parent_id=None)`` and
then given ``access_grants`` rows matching its key, which is what makes ``access.py`` treat
it as an access-control root governing exactly the principals the key names. An empty key
writes no grants, so that root is under no ACR and is public: the same rule that makes a
document under no ACR public, not an exception to it.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from jmfts_core.access import AccessKey, access_key, access_key_text
from jmfts_core.models.document import Document
from jmfts_core.models.entity_root import EntityRoot
from jmfts_core.models.principal import AccessGrant
from jmfts_core.repositories.document import DocumentRepository

logger = logging.getLogger(__name__)

#: The usetype carried by an entities root. Distinct from ``"entity"`` (the nodes under
#: it): a root is a container, holds no content and is never a retrieval target, which is
#: why `Settings.bm25_exclude_usetypes` and `search_exclude_usetypes` exclude both.
ENTITIES_ROOT_USETYPE = "entities"

#: The usetype carried by the entity nodes themselves.
ENTITY_USETYPE = "entity"


def get_or_create_entities_root(session: Session, document_id: int) -> int:
    """The id of the entities root for the access of ``document_id``, creating it if new.

    Get-or-create, and the retry is not decoration: fact extraction runs in the ingest
    worker and a fleet drains one queue, so two workers can reach a brand-new key in the
    same instant. ``entity_roots.access_key`` is UNIQUE, so the loser gets an
    ``IntegrityError`` — inside a SAVEPOINT, which is what lets the losing root document
    and its grants disappear with it rather than leaving an orphan root behind. It then
    re-reads the winner's row, which by then is visible: the unique index blocked the
    insert until the winner committed.

    Raises:
        LookupError: ``document_id`` does not exist. An entity has to be keyed by the
            access of the document that mentioned it, and there is no key without one —
            failing here is the whole protection, so there is no unkeyed fallback.
    """
    doc = session.get(Document, document_id)
    if doc is None:
        raise LookupError(
            f"Document {document_id} does not exist; an entity cannot be keyed by the "
            f"access of a document that is not there"
        )
    key: AccessKey = access_key(session, doc)
    key_text = access_key_text(key)
    existing = _root_id_for(session, key_text)
    if existing is not None:
        return existing

    try:
        with session.begin_nested():
            root = DocumentRepository(session).create(
                title="Entities",
                content=None,
                parent_id=None,
                usetype=ENTITIES_ROOT_USETYPE,
                # The key is on the node as well as in `entity_roots` so a root read
                # straight out of `documents` says what it is for.
                structured_content={"access_key": key_text},
                # No content, so nothing to embed. `auto_embed=True` on a contentless
                # document is a no-op, but stating it keeps the model stack out of a path
                # a storage-side worker with no torch has to be able to run.
                auto_embed=False,
            )
            # The grants ARE the access control. `access.py` defines an ACR as a document
            # with at least one grant, so writing these rows is what makes the root
            # governed — and writing none is what makes the public root public.
            for principal_id, level in key:
                session.add(
                    AccessGrant(document_id=root.id, principal_id=principal_id, level=level)
                )
            session.add(EntityRoot(access_key=key_text, document_id=root.id))
            session.flush()
    except IntegrityError:
        raced = _root_id_for(session, key_text)
        if raced is None:
            raise
        logger.debug("Lost the race to mint the entities root for %r; using %d", key_text, raced)
        return raced

    logger.info("Created entities root %d for access key %r", root.id, key_text)
    return root.id


def entity_root_ids(session: Session) -> list[int]:
    """Every entities-root document id.

    No access filter, deliberately. This is the set entity resolution partitions on — "the
    copies under OTHER roots" — and the linking it feeds must see every copy regardless of
    who is asking. See ``resolve_entity``.
    """
    return list(session.execute(select(EntityRoot.document_id)).scalars())


def _root_id_for(session: Session, key_text: str) -> int | None:
    return session.execute(
        select(EntityRoot.document_id).where(EntityRoot.access_key == key_text)
    ).scalar_one_or_none()
