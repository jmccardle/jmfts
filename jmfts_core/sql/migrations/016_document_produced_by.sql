-- Migration 016: a node records which rule produced it.
--
-- `docs/SPRINT_JOBS.md` Phase 3, section 4.2. A rule's scope has exactly two forms, and the
-- second one — "the children another named rule produced" — is unanswerable without this
-- column. A multiplicity gives a NUMBER; a scope needs an IDENTITY. When a node has
-- children from `chunk` and children from `partition`, "the children produced by rule R"
-- can only be resolved if each child records what made it, and the manually rearranged
-- tree — the case Part 9 exists for — is exactly the one that misattributes without it.
--
-- WHY `usetype` DOES NOT SERVE. `usetype` says what a node IS; this says what MADE it, and
-- the two are not the same question. One `structure:declared` writes both `section` and
-- `chunk` nodes, and three different rungs all write `chunk`. The rule table's scope names
-- both halves for that reason.
--
-- NULL MEANS ASSERTED, exactly as it does on `triples.derived_by`: a person, an importer or
-- an upload created this node, not a rule. That is why the backfill below writes nothing.
-- Every node that predates this migration was produced by a rule that did not stamp it, so
-- there is no honest value to write — and inventing one would claim, for every chunk in the
-- store, that a re-run may delete and rebuild it (9.1). "Not stamped" is the correct record
-- of a node created before the stamp existed, and it is the same state 9.4 puts an edited
-- node into: the re-run neither keeps it nor deletes it.
--
-- REVERSIBILITY: safe to ignore. Nothing reads the column except the frontier planner, and
-- a 0.2.x process neither writes nor reads it.

BEGIN;

ALTER TABLE documents ADD COLUMN IF NOT EXISTS produced_by VARCHAR(100);

-- 4.3: the frontier lookup is "the children of THIS node that rule R produced", so the
-- equality on `parent_id` leads. Deliberately NOT partial on `produced_by IS NOT NULL`,
-- which is where this differs from `ix_triples_derived_by`: derived rows are the minority
-- of a triple store and the majority of an ingested tree, and the exclusion would also stop
-- the index answering 9.4's question — which children under this node did a person assert.
CREATE INDEX IF NOT EXISTS idx_documents_produced_by ON documents(parent_id, produced_by);

COMMIT;
