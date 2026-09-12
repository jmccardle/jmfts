-- Migration 019: an edge records which rule produced it.
--
-- `docs/SPRINT_0_5_0.md` Block D step 15. `triples.derived_by` (migration `013`, column at
-- `models/triple.py:167`, index at `:222`) already carries this for facts; this is the same
-- column, the same NULL convention and the same partial index on the table that will carry
-- every reprojection edge. The mirroring is the argument: the decision was made once for
-- triples, against the same hazard, and is written down there.
--
-- NULL MEANS ASSERTED. A person, an importer or an ingest handler created this edge, not a
-- rule — and "asserted only" is then `WHERE derived_by IS NULL`. The backfill below writes
-- nothing for exactly `016`'s reason: every edge that predates this migration was written
-- by something that did not stamp it, so there is no honest value to write, and inventing
-- one would claim of every `mentions` and `summarizes` edge in the store that a re-run may
-- delete and rebuild it.
--
-- WHAT IT BUYS, WHICH IS NOT A BUG FIX. `WHERE derived_by = :rule` is a complete
-- description of what one rule produced, so a re-run deletes that set and rebuilds it: no
-- diffing, no reconciliation, no question about what a changed rule leaves behind. That is
-- Block B step 8's discipline for triples, made available on links.
--
-- ONE WRITER STAMPS IT as of Block C step 11, AND THAT WRITER REBUILDS. `summarize:tree`
-- (`rollup_tasks._rewrite_member_links`) sets `summarize:tree` on the `summarizes` edges a
-- roll-up mints, and a second run over a changed member set deletes that node's edges and
-- writes them again — so `WHERE derived_by = 'summarize:tree'` is a complete description of
-- what the roll-up pass produced, over a set that is genuinely re-derived rather than
-- appended to. An earlier draft of this comment said no writer rebuilt a link set yet and
-- that Part 3.1's reprojection would be the first to; that was true when it was written and
-- false by the end of the same pass, and Block D corrects it in its own text.
--
-- THE REBUILD IS SCOPED TO ONE NODE, NOT TO THE RULE, and `DocumentRepository.rederive_links`
-- (step 16) is therefore still uncalled. It deletes everything a rule produced across the
-- whole store; `summarize:tree` produces edges for every container in it, so a per-node
-- rebuild through that method would delete every other node's edges. Block C finding 5
-- records the mismatch: covering per-node rules needs a scope argument, or a per-scope
-- suffix on the rule identity.
--
-- EVERY OTHER WRITER LEAVES IT NULL, and that is the asserted-edge case rather than an
-- omission: `bridge` (`summarization.py:354`, `:624`), `summarizes` from RAPTOR
-- (`summarization.py:472`), `LINK_CONTAINS` (`services/ingest_service.py:918`), and
-- `MENTIONS_LINK_TYPE` plus `RBAC_COREF_LINK_TYPE`, both through
-- `fact_extraction._upsert_link` (`:401`). Each writes once at ingest and never rebuilds,
-- so there is no rule identity to record.
--
-- THAT IS FIVE TYPES, NOT FOUR. Block D step 15 counts four and omits `rbac_coref`, which
-- `resolve_entity` writes when an entity gets a copy under a second entities root. It
-- leaves the column NULL for the same reason as the rest, so the count is the only thing
-- wrong and nothing follows from it — corrected here rather than copied.
--
-- Without this column, regenerating a keyword tree would mean either deleting every
-- `contains-keyword` edge including hand-created ones, or diffing, which step 8 already
-- declined.
--
-- WIDTH. `VARCHAR(200)`, matching `triples.derived_by` rather than `documents.produced_by`
-- (`VARCHAR(100)`). A rule identity is the same string in both tables and the wider of the
-- two shipped widths is the one to match; a link and a triple produced by one rule must be
-- findable under the same name.
--
-- THE INDEX IS PARTIAL, for `ix_triples_derived_by`'s reason and NOT `016`'s. Derived edges
-- are the minority of a link graph today (they are none of it), so the index holds only the
-- derived rows and the asserted majority is served by not being in it. `016` deliberately
-- went the other way for `documents.produced_by`, because produced nodes are the MAJORITY
-- of an ingested tree and the exclusion would also have stopped the index answering "which
-- children under this node did a person assert".
--
-- REVERSIBILITY: purely additive. A 0.4.x process neither writes nor reads the column.

BEGIN;

ALTER TABLE document_links ADD COLUMN IF NOT EXISTS derived_by VARCHAR(200);

-- "Asserted only" and "derived by this rule" are both single-column lookups on a column
-- that is NULL for every row today.
CREATE INDEX IF NOT EXISTS ix_links_derived_by ON document_links(derived_by)
    WHERE derived_by IS NOT NULL;

-- Every delta from 017 on ends with these three lines. `ON CONFLICT DO NOTHING` so a re-run
-- against a `schema.sql`-built database leaves that database's own 'schema' row standing.
INSERT INTO schema_migrations (name, applied_at, source) VALUES
    ('019_link_derived_by.sql', NOW(), 'delta')
ON CONFLICT (name) DO NOTHING;

COMMIT;
