-- Migration 015: evidence leaves `structured_content` for rows of its own.
--
-- `docs/SPRINT_JOBS.md` Phase 2b. 13.1 decided the storage and 13.3 decided the contract;
-- this is both, and Part 14's rule is why they are one migration rather than two:
-- "what it must not do is run half — the column and the rows both holding evidence is the
-- second code path this part forbids."
--
-- WHY ROWS, and it is not a performance argument. `scripts/evidence_bench.py` measured the
-- join and 13.1 records the numbers, but the two reasons are both correctness:
--
--   1. Every evidence write was a read-modify-write on one JSONB column — read
--      `structured_content`, copy it, add a key, assign it back. Two handlers writing two
--      DIFFERENT names to one node lost one of the writes with nothing raised. The bench's
--      race, run against both shapes: jsonb wrote 2 names and 1 survived; the table wrote 2
--      and 2 survived. That is the swallowed failure this project's Fail Early rule exists
--      to prevent, and a primary key of (document_id, name) is what removes it.
--   2. 3.2 needs a third state. "Never attempted", "written, and the value is null" and
--      "failed" are three different facts and a JSONB column has room for two, so the JSONB
--      form has to DELETE a block to stale it — which destroys the distinction every time.
--      `state` is a column here because it cannot be a key there.
--
-- `fingerprint` and `state` are written by later phases, and 2b writes neither. Every row
-- this migration creates is 'written' with a NULL fingerprint, which is what the column
-- already meant: it is here, and nothing has said whether its inputs have moved since.
-- 3.3 is what fills `fingerprint`; Part 9's rebinding is what sets `state = 'stale'`; 3.2's
-- retry policy is what sets 'failed'. The CHECK names all three now because the set is
-- closed and 3.2 closed it, not because 2b writes them.
--
-- THE COLUMN SURVIVES, CARRYING EXACTLY WHAT A CALLER PUT THERE. `structured_content` is
-- still on `DocumentResponse` and hybrid search still reads `structured_content['importance']`
-- (`repositories/search.py`), which is caller-owned and is not evidence — the registry does
-- not name it. What leaves are the twenty-three keys the ingest pipeline owned.
--
-- AND NOTHING PUTS THEM BACK. 13.3 took option 2: ingestion stops writing the column and no
-- read reassembles it, so a client reading `matched.patterns` out of `structured_content`
-- after this migration reads nothing, because it is not there. Evidence is served by
-- `GET /documents/{id}/evidence`. A versioned break, taken deliberately.
--
-- THE KEY LIST BELOW IS AUDITED, NOT TRUSTED. Part 14: "No phase adds a second list of
-- something the code already knows." SQL cannot read `jmfts_core.evidence.REGISTRY`, so the
-- pairs are written out here and `tests/test_evidence_rows.py::test_migration_matches_registry`
-- parses them back out of this file and fails if they are not exactly the registry's
-- root JSONB entries. The test prints the corrected block when they drift.
--
-- THE NAME AND THE OLD COLUMN KEY DIFFER TWICE, and 2.5's finding 5 is why: evidence is
-- named by what it asserts, not by where it was stored. `anchor` becomes `source_anchor`
-- and `anchor_unresolved` becomes `source_anchor.unresolved`. The dot in the second is part
-- of the name, not a path into the first — 3.2 cites that pair as "two keys, never one with
-- a null", so they are two rows.
--
-- REVERSIBILITY: this one is not reversible by ignoring it. A 0.2.x process reading
-- `structured_content['matched']` after this runs finds nothing there. Take a dump first.

BEGIN;

CREATE TABLE IF NOT EXISTS document_evidence (
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,

    -- The evidence name from `jmfts_core.evidence.REGISTRY`, not the column key it used to
    -- live under. Unbounded TEXT for the reason `entity_roots.access_key` is: the closed
    -- vocabulary lives in Python and no n is defensibly the limit here.
    name TEXT NOT NULL,

    -- Still JSONB, and 13.3 says why that is not a contradiction: "What 13.1 measured
    -- losing writes was one column shared by every name on a node. A row per
    -- (document_id, name) fixes that whatever the value's type, and fourteen of the
    -- twenty-three blocks are dicts that would otherwise need a table each."
    --
    -- NULLABLE, and the null means something. 3.2: an atom writes every name it produces on
    -- success, null included, because producing nothing is a result. A row with a NULL value
    -- is "asked and got nothing"; no row at all is "never attempted".
    value JSONB,

    -- 3.3's input fingerprint: the child node ids read, the source evidence values read, and
    -- the parameters used. Written by Phase 3. NULL here means unfingerprinted, which is
    -- every row 2b creates.
    fingerprint TEXT,

    -- 3.2's third state. TEXT + CHECK rather than an ENUM for the same reason
    -- `documents.settled` is: migrations in this repo run inside BEGIN/COMMIT and
    -- `ALTER TYPE ... ADD VALUE` cannot.
    state TEXT NOT NULL DEFAULT 'written'
        CONSTRAINT ck_document_evidence_state
        CHECK (state IN ('written', 'stale', 'failed')),

    -- One row per (document, name). This is the whole of fix 1 above.
    PRIMARY KEY (document_id, name)
);

-- Part 4.4's guard reads one name across the corpus, and 13.1 measured it ten times faster
-- against this index than against a JSONB path at 100,000 nodes — with the gap growing,
-- because JSONB's cost there scales with nodes scanned and this scales with rows carrying
-- the name.
CREATE INDEX IF NOT EXISTS idx_document_evidence_name ON document_evidence (name);

-- The GIN half of what `idx_documents_structured` gave the column: a guard that predicates
-- on the VALUE, not just the name.
CREATE INDEX IF NOT EXISTS idx_document_evidence_value ON document_evidence USING GIN (value);

-- Part 9's frontier: what is stale, as a query. This is the read the JSONB form could not
-- express at all.
CREATE INDEX IF NOT EXISTS idx_document_evidence_stale ON document_evidence (name, document_id)
    WHERE state = 'stale';

-- ---------------------------------------------------------------------------
-- The move. Twenty-nine keys out of the column, into rows, and then off the column.
--
-- 13.3 says twenty-three and that count is from before S7. Six more are the conversation
-- rung's per-turn keys — `speaker`, `turn_index`, `timestamp`, `conversation_id`,
-- `over_token_window`, `part_index` — which S7 wrote beside `rung` on every turn node and
-- did not register. Phase 2's audit did not reach them: its fixture ingests a document, not
-- a transcript. They are registered now and they move with the rest, because "the column is
-- wholly the caller's" has to be true of a transcript too.
-- ---------------------------------------------------------------------------

CREATE TEMPORARY TABLE _evidence_keys (column_key TEXT PRIMARY KEY, name TEXT NOT NULL)
    ON COMMIT DROP;

-- BEGIN EVIDENCE KEYS -- generated from jmfts_core.evidence.REGISTRY; see the audit above
INSERT INTO _evidence_keys (column_key, name) VALUES
    ('anchor', 'source_anchor'),
    ('anchor_unresolved', 'source_anchor.unresolved'),
    ('attempts', 'attempts'),
    ('cells', 'cells'),
    ('chunk_index', 'chunk_index'),
    ('conversation_id', 'conversation_id'),
    ('effective_content', 'effective_content'),
    ('extraction', 'extraction'),
    ('file', 'file'),
    ('matched', 'matched'),
    ('options', 'options'),
    ('over_token_window', 'over_token_window'),
    ('part_index', 'part_index'),
    ('profile', 'profile'),
    ('record', 'record'),
    ('row_index', 'row_index'),
    ('rung', 'rung'),
    ('section_level', 'section_level'),
    ('section_title', 'section_title'),
    ('sheet', 'sheet'),
    ('sheet_name', 'sheet_name'),
    ('source', 'source'),
    ('source_line', 'source_line'),
    ('source_span', 'source_span'),
    ('speaker', 'speaker'),
    ('structure', 'structure'),
    ('timestamp', 'timestamp'),
    ('turn_index', 'turn_index'),
    ('yield', 'yield');
-- END EVIDENCE KEYS

-- `?` rather than `-> IS NOT NULL`: a key present and holding JSON null is 3.2's "written,
-- and the value is null", and it has to survive as a row rather than being skipped as
-- absent. A `source_span` of null on a chunk says `citation` looked and found nothing.
--
-- NULLIF, so that a written null is SQL NULL and not JSONB 'null'. Both read back as Python
-- `None`, which is exactly why the difference would go unnoticed — and then
-- `WHERE value IS NULL`, the query Part 9 wants for "what produced nothing", would answer
-- for freshly written rows and not for migrated ones. `EvidenceRepository.write(..., None)`
-- stores SQL NULL, so this stores SQL NULL.
INSERT INTO document_evidence (document_id, name, value)
SELECT d.id, k.name, NULLIF(d.structured_content -> k.column_key, 'null'::jsonb)
FROM documents d
JOIN _evidence_keys k ON d.structured_content ? k.column_key
ON CONFLICT (document_id, name) DO NOTHING;

-- And off the column. `?|` in the WHERE so this rewrites only the rows that carried one,
-- rather than every document in the store.
UPDATE documents d
SET structured_content = d.structured_content - (SELECT array_agg(column_key) FROM _evidence_keys)
WHERE d.structured_content ?| (SELECT array_agg(column_key) FROM _evidence_keys);

COMMIT;
