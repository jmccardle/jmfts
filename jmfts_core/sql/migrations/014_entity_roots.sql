-- Migration 014: entities are keyed by access, not by tree position.
--
-- docs/SPRINT_0_3_0.md 7.5, and the defect it closes is 13.9: a fact extracted from a
-- restricted document is world-readable today. `TripleRepository.query_triples` scopes a
-- fact by the readability of its ENDPOINTS, and `fact_extraction.resolve_entity` resolved
-- a name against every entity node in the store with no access filter at all — so a
-- restricted document's two entities resolved to public nodes and the triple between them
-- was readable by everyone. The placement rule that was the whole of the protection (a NEW
-- entity goes under its source document) never ran, because nothing was created.
--
-- The fix is one table. An entity lives under an ENTITIES ROOT whose grants are exactly
-- the effective access of the document that mentioned it, and resolution only ever looks
-- under the root for the mentioning document's access. One root per distinct ACCESS, not
-- one per access-control root: two ACRs with identical grants share a root, which is what
-- makes an entity set track a sub-corpus rather than a tree position.
--
-- `access_key` is the canonical text of `jmfts_core.access.access_key` —
-- "7:read,12:write", principal id ascending, one pair per principal, `write` beating
-- `read`. The empty string is the ungoverned key, and its root carries no grants, so it is
-- public by exactly the rule that makes a document under no ACR public. In the single-user
-- default there is one entities root, it is that one, and nothing else changes.
--
-- Both columns are UNIQUE and neither is redundant. `access_key` is the get-or-create
-- conflict key (two workers racing one key must not mint two roots); `document_id` says a
-- root serves one key, so a root can never accumulate a second meaning.
--
-- NO BACKFILL, and that is a decision rather than an omission. Entity nodes created before
-- this migration hang under the documents that mentioned them, and this lookup does not
-- look there — a name that resolved yesterday mints a fresh copy under the right root
-- today. Re-parenting them would have to re-derive each one's access from a parent that
-- may since have moved, and would change document paths in a data migration. The pre-13
-- corpus keeps its old entities as ordinary orphaned nodes; the graph they were in is not
-- destroyed, it just stops growing. Related and also deliberate: re-keying entities when
-- GRANTS change is out of scope (7.5, "Grants changing is out of scope").
--
-- REVERSIBILITY: purely additive. A 0.2.x process ignores the table.

BEGIN;

CREATE TABLE IF NOT EXISTS entity_roots (
    id SERIAL PRIMARY KEY,
    -- Unbounded TEXT rather than VARCHAR(n) because the key's length is the number of
    -- principals granted on one chain and no n is defensibly the limit. The btree entry
    -- cap near 2704 bytes is the real bound: reaching it takes a few hundred distinct
    -- grants above a single document, and an INSERT that hits it fails loudly.
    access_key TEXT NOT NULL UNIQUE,
    document_id INTEGER NOT NULL UNIQUE REFERENCES documents(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

COMMIT;
