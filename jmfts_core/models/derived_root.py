"""The derived-tree root registry: one root document per (access, tree kind).

See ``migration 018``, ``jmfts_core/derived_roots.py`` for the get-or-create, and
``SPRINT_0_5_0.md`` Block C step 10 for why a derived tree gets a root of its own rather
than absorbing the tree it derives from. The pattern is ``models/entity_root.py``'s, which
``SPRINT_0_5_0.md`` Part 0.3 names as the precedent: "a derived tree gets its own root; the
root is keyed by access rather than by tree position".
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from jmfts_core.database import Base


class DerivedRoot(Base):
    """Maps an (access key, tree kind) pair to the root document holding that derived tree.

    ``access_key`` is the canonical text of :func:`jmfts_core.access.access_key` — ``""``
    for the ungoverned (public) root — exactly as in :class:`~jmfts_core.models.entity_root
    .EntityRoot`. ``tree_kind`` names WHICH parallel tree: ``"summary"`` first, with keyword,
    argument and question trees behind it (``SPRINT_0_5_0.md`` 3.1).

    **The key is the pair, and that is the one place this differs from ``EntityRoot``.**
    0.5.0 open question 6.6, answered: a shared root would make the tree kind a ``usetype``
    filter over a mixed subtree, and 3.1's leaf projection is per tree. ``document_id``
    stays separately UNIQUE, and it is not redundant — nothing else stops one document being
    registered as the root for two different pairs, which would give a root two meanings.

    There is no ``level``-ordering or principal FK here, for ``EntityRoot``'s reason: the key
    is a SNAPSHOT of the grants at creation time, and the grants that actually govern the
    root are the ``access_grants`` rows written alongside it. This row is a lookup index,
    never the enforcement.
    """

    __tablename__ = "derived_roots"
    __table_args__ = (UniqueConstraint("access_key", "tree_kind"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    access_key: Mapped[str] = mapped_column(Text, nullable=False)
    #: ``String(100)`` to sit at the width of ``Document.usetype`` and
    #: ``Document.produced_by``, which is the company it keeps: a short declared name, not
    #: free text and not a key derived from data.
    tree_kind: Mapped[str] = mapped_column(String(100), nullable=False)
    document_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
