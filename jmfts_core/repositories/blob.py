"""BlobRepository — uploaded bytes in Postgres large objects. ``INGEST_SPEC.md`` Part 9.

A large object is not stored in any table. ``document_blobs`` only holds its OID, so the
bytes are invisible to every ordinary tool: ``DELETE`` does not remove them, ``ON DELETE
CASCADE`` does not remove them, and ``pg_dump`` in plain format does not back them up
without ``-b``. Everything in this module exists because of that asymmetry.

Three failure modes, three methods:

* deliberate deletion leaks the object unless something calls ``lo_unlink`` first —
  :meth:`delete` and :meth:`unlink_subtree`;
* an object whose row is already gone is invisible and unreachable —
  :meth:`find_orphaned_lobs` / :meth:`cleanup_orphaned_lobs`;
* a file node whose bytes never landed looks identical to one whose bytes are fine —
  :meth:`find_blobless_documents`.

The SQL-function form (``lo_from_bytea`` / ``lo_get`` / ``lo_unlink``) is used rather
than psycopg2's ``connection.lobject()``. Both work today, but ``lobject`` is a
psycopg2-only API and psycopg3 has no equivalent; the SQL functions are portable, and
this module is the only place in JMFTS that would have to change.

Large objects are transactional: an object created here is only durable if the
surrounding transaction commits, and one unlinked here comes back if it rolls back. That
is what keeps the OID and its row consistent — they are written in the same transaction
or neither is.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from jmfts_core.models.document import Document
from jmfts_core.models.document_blob import DocumentBlob

logger = logging.getLogger(__name__)

#: Every table that stores a large-object OID. ``find_orphaned_lobs`` treats an object
#: named by NONE of these as garbage and unlinks it, so a new OID-holding table that is
#: missing from this list will have its bytes destroyed by the next cleanup run. Adding a
#: table here is not optional bookkeeping — it is part of creating the table.
#: (vdo_frontend's version hard-coded a single table in the subquery, which is exactly
#: the bug this constant is shaped to prevent.)
_OID_SOURCES: tuple[tuple[str, str], ...] = (("document_blobs", "lob_oid"),)


class BlobLeakError(RuntimeError):
    """A large object was expected and is not there.

    Raised when reading a blob whose row exists but whose OID no longer resolves. That
    combination is not a missing blob — the row asserts the bytes exist — so it is a
    corruption report, not a normal "not found".
    """


class BlobRepository:
    """Store, read and unlink the large objects behind ``document_blobs`` rows."""

    def __init__(self, session: Session):
        self.session = session

    # =========================================================================
    # Write
    # =========================================================================

    def store(
        self,
        document_id: int,
        data: bytes,
        *,
        mime_type: str,
        content_hash: str,
    ) -> DocumentBlob:
        """Write ``data`` into a new large object and record it against ``document_id``.

        Raises ``ValueError`` if the document already has a blob. A second call is a
        caller bug — one file node holds one set of bytes, and silently replacing them
        would orphan the first object and rewrite history the attempt log claims is
        immutable (spec 3.3: the ``file`` block "never changes after upload").
        """
        if self.get(document_id) is not None:
            raise ValueError(
                f"Document {document_id} already has a blob; replacing uploaded bytes "
                "is a new node (spec 6.2 supersedes), not an overwrite"
            )

        # lo_from_bytea(0, ...) asks the server to allocate the OID. The object and the
        # row below are written in one transaction so they cannot exist without each
        # other: a rollback takes both.
        lob_oid = self.session.execute(
            text("SELECT lo_from_bytea(0, :data)"), {"data": data}
        ).scalar_one()

        blob = DocumentBlob(
            document_id=document_id,
            lob_oid=lob_oid,
            mime_type=mime_type,
            byte_size=len(data),
            content_hash=content_hash,
        )
        self.session.add(blob)
        self.session.flush()
        return blob

    # =========================================================================
    # Read
    # =========================================================================

    def get(self, document_id: int) -> Optional[DocumentBlob]:
        """The metadata row for a document's bytes, or None if it has none."""
        return self.session.execute(
            select(DocumentBlob).where(DocumentBlob.document_id == document_id)
        ).scalar_one_or_none()

    def read_bytes(self, document_id: int) -> Optional[bytes]:
        """The stored bytes, or None when the document has no blob row at all.

        Reads the whole object into memory. That is adequate for the sizes this appliance
        ingests and it is the shape every current consumer wants (probe and extraction
        both need the full bytes); ``lo_get(oid, offset, length)`` is the streaming form
        if a consumer ever needs it.

        Raises :class:`BlobLeakError` when the row exists but the object does not — the
        row is an assertion that the bytes are there, so its being wrong is a corruption
        report and not a quiet ``None``.
        """
        blob = self.get(document_id)
        if blob is None:
            return None
        if not self._lob_exists(blob.lob_oid):
            raise BlobLeakError(
                f"document_blobs row {blob.id} (document {document_id}) names large "
                f"object {blob.lob_oid}, which does not exist"
            )
        raw = self.session.execute(text("SELECT lo_get(:oid)"), {"oid": blob.lob_oid}).scalar_one()
        # psycopg2 hands back a memoryview for bytea; callers want bytes.
        return bytes(raw)

    # =========================================================================
    # Delete — the half that ON DELETE CASCADE cannot do
    # =========================================================================

    def delete(self, document_id: int) -> bool:
        """Unlink a document's large object and delete its row. False if it had none."""
        blob = self.get(document_id)
        if blob is None:
            return False
        self._unlink(blob.lob_oid)
        self.session.delete(blob)
        self.session.flush()
        return True

    def unlink_subtree(self, root_id: int) -> int:
        """Unlink every large object under ``root_id`` (inclusive). Returns the count.

        Called by :meth:`DocumentRepository.delete` BEFORE the rows go, because after the
        cascade there is nothing left that knows the OIDs. The rows themselves are left
        alone here — ``ON DELETE CASCADE`` removes them — so this method is only ever
        correct immediately before the documents are deleted.
        """
        # `path @> [root_id]` finds the descendants (GIN-indexed, see migration 008's
        # header for why that index stays non-partial); the root is added separately
        # because a node's own path holds its ancestors, not itself.
        rows = self.session.execute(
            text("""
                SELECT b.lob_oid
                FROM document_blobs b
                JOIN documents d ON d.id = b.document_id
                WHERE d.id = :root
                   OR d.path @> jsonb_build_array(:root)
                """),
            {"root": root_id},
        ).scalars()

        unlinked = 0
        for oid in rows:
            if self._unlink(oid):
                unlinked += 1
        return unlinked

    # =========================================================================
    # Orphan detection — both directions
    # =========================================================================

    def find_orphaned_lobs(self) -> list[int]:
        """Large objects that no row names: bytes with no record. Carried over from
        vdo_frontend's ``cleanup_orphaned_lobs`` (``vdo_ingest/image_vdo.py:215-255``),
        with its two defects fixed.

        ``NOT EXISTS`` rather than vdo's ``NOT IN``: ``NOT IN`` against a nullable column
        yields nothing at all the moment one OID is NULL, which turns the whole sweep
        into a silent no-op. And the referencing tables come from :data:`_OID_SOURCES`
        rather than being hard-coded, because vdo's single-table subquery would classify
        every OID belonging to any *other* table as garbage.

        This scans ``pg_largeobject_metadata``, i.e. every large object in the database —
        including any written by something that is not JMFTS. Read the list before acting
        on it.
        """
        not_exists = " AND ".join(
            f"NOT EXISTS (SELECT 1 FROM {table} t WHERE t.{column} = m.oid)"
            for table, column in _OID_SOURCES
        )
        rows = self.session.execute(
            text(f"SELECT m.oid FROM pg_largeobject_metadata m WHERE {not_exists}")
        ).scalars()
        return list(rows)

    def cleanup_orphaned_lobs(self) -> int:
        """Unlink every object :meth:`find_orphaned_lobs` reports. Returns the count.

        Deliberately not automatic and not scheduled: it is destructive, it is global to
        the database, and the correct time to run it is when an operator has looked at
        :meth:`find_orphaned_lobs` first.
        """
        oids = self.find_orphaned_lobs()
        for oid in oids:
            self._unlink(oid)
        return len(oids)

    def find_blobless_documents(self, usetype: str = "file") -> list[int]:
        """File nodes with no blob row: a record with no bytes — the other direction.

        The counterpart of :meth:`find_orphaned_lobs`, and a different failure: this one
        means an upload half-landed, so the node claims to be a file and nothing can open
        it.
        """
        rows = self.session.execute(
            select(Document.id)
            .outerjoin(DocumentBlob, DocumentBlob.document_id == Document.id)
            .where(Document.usetype == usetype, DocumentBlob.id.is_(None))
            .order_by(Document.id)
        ).scalars()
        return list(rows)

    # =========================================================================
    # Internals
    # =========================================================================

    def _lob_exists(self, oid: int) -> bool:
        return (
            self.session.execute(
                text("SELECT 1 FROM pg_largeobject_metadata WHERE oid = :oid"), {"oid": oid}
            ).scalar()
            is not None
        )

    def _unlink(self, oid: int) -> bool:
        """``lo_unlink`` one object. True if it was there, False if it already was not.

        ``lo_unlink`` on a missing OID raises, and that error would abort the whole
        transaction — so a document whose bytes had already vanished could never be
        deleted at all. The existence check turns that into a WARNING naming the OID: the
        inconsistency is reported, not swallowed, and the delete the operator asked for
        still happens. (vdo_frontend wrote this as ``except: pass``, which reports
        nothing.)
        """
        if not self._lob_exists(oid):
            logger.warning(
                "large object %s was already gone at unlink time — a row named it but "
                "the object did not exist",
                oid,
            )
            return False
        self.session.execute(text("SELECT lo_unlink(:oid)"), {"oid": oid})
        return True
