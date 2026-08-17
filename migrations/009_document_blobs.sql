-- Migration 009: `document_blobs` — uploaded file bytes in Postgres large objects.
--
-- Background: INGEST_SPEC.md Part 3.1 makes an uploaded file a first-class node, created
-- from the bytes before any parsing and carrying `settled = 'in_flight'` (migration 008)
-- while ingestion builds a tree beneath it. That node needs somewhere to keep the bytes.
-- Part 9 settles the question by precedent rather than re-deciding it: uploaded bytes go
-- in a Postgres LARGE OBJECT, following vdo_frontend's `document_images.lob_oid`, and
-- `file.blob_ref` on the node holds the OID.
--
-- WHY LARGE OBJECTS AND NOT bytea: a bytea column is TOASTed, read whole, and rewritten
-- whole; large objects are chunked in pg_largeobject and support offset/length reads
-- (`lo_get(oid, offset, length)`), which is what a 200 MB PDF wants when a later stage
-- needs page 40. The cost is everything below.
--
-- THE ASYMMETRY THAT DRIVES THIS WHOLE FILE. A large object is not stored in any table.
-- It is owned by nothing. Three consequences, each of which has a counterpart in
-- jmfts_core/repositories/blob.py:
--
--   1. DELETION LEAKS BY DEFAULT. The `ON DELETE CASCADE` below removes the row when its
--      document goes; the object it named survives, unreferenced and unreachable, taking
--      disk space forever. This is the leak Part 9 names outright. Nothing in the
--      database prevents it — the FK cannot point at pg_largeobject_metadata — so
--      correctness lives in `DocumentRepository.delete`, which calls
--      `BlobRepository.unlink_subtree` (lo_unlink over the subtree) BEFORE the cascade
--      runs, while the OIDs are still readable.
--   2. ORPHANS MUST BE FINDABLE AFTER THE FACT. `BlobRepository.find_orphaned_lobs()`
--      scans pg_largeobject_metadata for objects no row names. It uses NOT EXISTS over a
--      LIST of OID-holding tables, not vdo_frontend's hard-coded single-table NOT IN:
--      that form would classify every OID belonging to a future second table as garbage
--      and unlink it. Any new table storing an OID must be added to `_OID_SOURCES` in
--      that module.
--   3. BACKUPS DO NOT INCLUDE THEM BY DEFAULT. `pg_dump` in PLAIN format (`-Fp`, the
--      default when writing to a file with -f and no -F) OMITS large objects unless
--      given `-b`/`--large-objects`. A plain dump taken without -b restores every row in
--      this table pointing at bytes that no longer exist. Custom (`-Fc`), directory
--      (`-Fd`) and tar (`-Ft`) formats include them by default. Check whatever backup
--      command this appliance runs before the first upload lands.
--
-- Column choices that differ from the vdo precedent, deliberately:
--   * `byte_size BIGINT`. vdo stored page images and used INTEGER, capping at 2 GB. This
--     table stores whatever was uploaded, which is not bounded by anything JMFTS knows.
--   * `lob_oid OID`, not INTEGER. vdo's SQL said OID and its model said Integer; that
--     works by accident (OID is a 4-byte unsigned int) but means a create_all() from the
--     model produces different DDL from the schema. Both sides say OID here.
--   * `lob_oid UNIQUE`. Two rows naming one large object would make deleting either one
--     destroy the other's bytes with no error anywhere. The constraint makes that state
--     unrepresentable.
--   * `document_id UNIQUE`. One file node holds one set of bytes. Re-uploading is a new
--     node (spec 6.2 supersedes nodes), not an overwrite — the `file` block on the node
--     is specified as never changing after upload (3.3).
--
-- No `updated_at` and no update trigger, unlike vdo's document_images: nothing here is
-- ever updated. The row is written once with the bytes and deleted with them.
--
-- Safe to run multiple times (idempotent: CREATE TABLE / CREATE INDEX IF NOT EXISTS).
-- Note that unlike migration 008 no index DEFINITION changes here, so IF NOT EXISTS is
-- genuinely sufficient — nothing can silently no-op over a differing prior version.
--
-- Run: psql $DATABASE_URL -f migrations/009_document_blobs.sql

BEGIN;

CREATE TABLE IF NOT EXISTS document_blobs (
    id SERIAL PRIMARY KEY,
    -- One blob per document; a second version is a new node, not a second row.
    document_id INTEGER NOT NULL UNIQUE REFERENCES documents(id) ON DELETE CASCADE,
    -- UNIQUE: two rows naming one object would make deleting either destroy the
    -- other's bytes, silently.
    lob_oid OID NOT NULL UNIQUE,
    -- What to serve the bytes back as. The repository prefers the DETECTED type
    -- over the client's declared one; both stay on the node's `file` block.
    mime_type TEXT NOT NULL,
    -- BIGINT, not INTEGER: an uploaded file is not bounded by anything here.
    byte_size BIGINT NOT NULL,
    -- Bare sha256 hex, matching the documents.content_hash convention.
    content_hash VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- document_id and lob_oid are already indexed by their UNIQUE constraints; the content
-- hash is not, and it is the lookup that answers "have we already stored these exact
-- bytes" (spec 6.1's dedupe key).
CREATE INDEX IF NOT EXISTS idx_document_blobs_hash ON document_blobs(content_hash);

COMMIT;
