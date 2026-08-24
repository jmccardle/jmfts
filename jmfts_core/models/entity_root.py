"""The entities-root registry: one root document per distinct ACCESS.

See ``migration 014``, ``jmfts_core/entity_roots.py`` for the get-or-create, and
``SPRINT_0_3_0.md`` 7.5 for why entities are keyed by access rather than by tree position.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from jmfts_core.database import Base


class EntityRoot(Base):
    """Maps an access key to the root document that holds every entity extracted under it.

    Both columns are UNIQUE and the pair is a bijection: an access key names exactly one
    root, and a root serves exactly one access key. ``access_key`` is the canonical text
    of :func:`jmfts_core.access.access_key` — ``""`` for the ungoverned (public) root.

    There is no ``level``-ordering or principal FK here on purpose. The key is a SNAPSHOT
    of the grants at creation time; the grants that actually govern the root are the
    ``access_grants`` rows written alongside it, and those are what ``access.py`` reads.
    Re-keying when grants change is out of scope (``SPRINT_0_3_0.md`` 7.5, "Grants changing
    is out of scope"), so this row is a lookup index, never the enforcement.
    """

    __tablename__ = "entity_roots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    access_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    document_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
