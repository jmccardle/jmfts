"""DocumentEvidence — one named fact about one node.

``SPRINT_JOBS.md`` Part 3, and Phase 2b. :mod:`jmfts_core.evidence` says what each ``name``
asserts and what type its value is; this is where the value physically sits. Before 2b every
one of these was a key in ``documents.structured_content``, and 13.1 gives the two reasons
they are rows instead — both correctness, neither speed:

* **A write to the column was a read-modify-write.** ``structured = dict(...)``, mutate,
  assign back. Two handlers writing two DIFFERENT names to one node lost one write with
  nothing raised. ``scripts/evidence_bench.py`` runs that race against both shapes: the
  column wrote two names and one survived. The primary key here is what removes it.
* **3.2 needs three states and a column has room for two.** "Never attempted", "written,
  and the value is null" and "failed" are different facts. Staling a JSONB block means
  deleting it, which collapses the first two every time a rule set is rebound. Hence
  :attr:`state`.

**There is deliberately no ``relationship()`` from :class:`~jmfts_core.models.document.Document`.**
The same reason ``DocumentBlob`` has none: a lazy collection here would make every ORM load
of a node an evidence read, which is exactly the join 13.3 decided no response would pay.
Evidence is read through :class:`~jmfts_core.repositories.evidence.EvidenceRepository`, by a
caller that has said which names it wants. ``ON DELETE CASCADE`` is on the FK, so deleting a
node still takes its evidence with it.
"""

from typing import Any, Optional

from sqlalchemy import ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from jmfts_core.database import Base

#: The value is here and nothing has said its inputs moved. Every row Phase 2b writes.
STATE_WRITTEN = "written"

#: Part 9: a rule set was rebound at a subtree and this name is due for re-derivation. The
#: state the JSONB column had nowhere to put — it had to DELETE the block instead, which is
#: indistinguishable from never having run.
STATE_STALE = "stale"

#: 3.2's third state: the atom ran and raised. Carries its reason and attempt count in
#: :attr:`value`. Written by the retry policy 13.2 item 3 leaves undecided.
STATE_FAILED = "failed"

#: What the CHECK constraint allows. Closed, and 3.2 is what closed it.
EVIDENCE_STATES: tuple[str, ...] = (STATE_WRITTEN, STATE_STALE, STATE_FAILED)


class DocumentEvidence(Base):
    """One evidence value on one node, keyed by the registry name that asserts it."""

    __tablename__ = "document_evidence"

    document_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )

    # The registry name, NOT the `structured_content` key this used to live under. 2.5's
    # finding 5 is the rule and it bites twice: `anchor` is `source_anchor` here, and
    # `anchor_unresolved` is `source_anchor.unresolved`. The dot in the second is part of a
    # name, not a path into the first — 3.2 cites that pair as "two keys, never one with a
    # null", so they are two rows and neither is inside the other.
    name: Mapped[str] = mapped_column(Text, primary_key=True)

    # JSONB, and 13.3 says why that is not a contradiction: what lost writes was one column
    # SHARED by every name on a node, and a row per name fixes that whatever the value's
    # type. Fourteen of the twenty-nine would otherwise need a table each.
    #
    # NULLABLE, and the null is a result rather than a gap (3.2). `evidence.check` refuses a
    # null for a name that is not declared nullable, so "the reader returned nothing" and
    # "the handler forgot" cannot reach a guard as the same value.
    #
    # `none_as_null=True` IS LOAD-BEARING AND ITS DEFAULT IS NOT. A JSONB column can hold a
    # null two ways — SQL NULL, or the JSON scalar `null` — and SQLAlchemy's default turns a
    # Python `None` into the second. Both read back as `None`, so nothing at any read would
    # ever show the difference; what would show it is `WHERE value IS NULL`, the query
    # Part 9 wants for "which names produced nothing", answering for some rows and not
    # others. One representation, and it is the one SQL can ask about. Migration 015's
    # `NULLIF(..., 'null'::jsonb)` puts migrated rows on the same side.
    value: Mapped[Optional[Any]] = mapped_column(JSONB(none_as_null=True), nullable=True)

    # 3.3: the child node ids read, the source evidence values read, and the parameters
    # used. Phase 3 writes it. NULL means unfingerprinted, which is every row 2b creates —
    # the column already meant "it is here, and nothing has said whether its inputs moved".
    fingerprint: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    state: Mapped[str] = mapped_column(Text, nullable=False, default=STATE_WRITTEN)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<DocumentEvidence doc={self.document_id} name={self.name!r} state={self.state}>"
