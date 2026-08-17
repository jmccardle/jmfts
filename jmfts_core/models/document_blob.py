"""DocumentBlob — the metadata row for a document's uploaded bytes.

``INGEST_SPEC.md`` Part 9 fixes the storage decision: uploaded bytes live in a Postgres
**large object**, and this row is what names it. The pattern is lifted from
vdo_frontend's ``document_images.lob_oid``, with three deliberate changes:

* ``byte_size`` is ``BIGINT``, not ``INTEGER``. vdo stored page images, which cannot
  reach 2 GB; this table stores whatever was uploaded, which can.
* ``lob_oid`` is the SQL ``OID`` type rather than ``INTEGER``. vdo's model mapped it as
  ``Integer`` and only worked because OID happens to be a 4-byte unsigned int; declaring
  the real type means a ``create_all()`` from this model produces the same DDL as
  ``schema.sql``.
* ``lob_oid`` is **UNIQUE**. Two rows naming one large object would make deleting either
  one destroy the other's bytes, and nothing else in the system would notice.

**The row does not own the bytes.** ``ON DELETE CASCADE`` removes this row when its
document goes, and the large object it names survives — that is the leak Part 9 warns
about. Deletion must go through :meth:`BlobRepository.delete` /
:meth:`BlobRepository.unlink_subtree`, which call ``lo_unlink`` while the OID is still
readable. There is deliberately **no** ``relationship()`` from ``Document`` to this
table: an ORM cascade would delete the row silently and leak the object, which is exactly
the mistake the repository exists to prevent.
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import OID
from sqlalchemy.orm import Mapped, mapped_column

from jmfts_core.database import Base


class DocumentBlob(Base):
    """Metadata for one document's uploaded bytes, held in a Postgres large object."""

    __tablename__ = "document_blobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # One blob per document. A file node holds exactly the bytes it was uploaded with;
    # a second version is a new node, not a second row here (spec 6.2 supersedes nodes).
    document_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("documents.id", ondelete="CASCADE"), unique=True, nullable=False
    )

    # The large object. NOT a foreign key — pg_largeobject_metadata cannot be referenced —
    # so this pointer is maintained by the repository, and find_orphaned_lobs() is how a
    # dangling one is found after the fact.
    lob_oid: Mapped[int] = mapped_column(OID, nullable=False, unique=True)

    # What to serve the bytes back as. The repository prefers the DETECTED type over the
    # client's declared one; both are kept on the node's `file` block (spec 3.3), so a
    # disagreement stays visible rather than being resolved by silently picking one.
    mime_type: Mapped[str] = mapped_column(Text, nullable=False)

    # BIGINT: an uploaded file is not bounded by anything this system controls.
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # Bare sha256 hex, matching the `documents.content_hash` convention. Spec 3.3 renders
    # it as "sha256:<hex>" in JSON; the prefix is presentation, not storage.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<DocumentBlob doc={self.document_id} oid={self.lob_oid} "
            f"bytes={self.byte_size} mime={self.mime_type!r}>"
        )
