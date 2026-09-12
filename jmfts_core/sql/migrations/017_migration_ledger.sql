-- Migration 017: a database says which deltas it has seen.
--
-- `docs/SPRINT_0_4_0.md` Block E step 16, and the argument it inherits from
-- `docs/archive/SPRINT_0_4_0_DRAFT.md` 7.4. `jmfts_core/sql/__init__.py` said, until this
-- migration made it false: "Nothing here tracks which migrations a given database has
-- already seen. JMFTS has no migration ledger, so choosing and applying deltas is still a
-- deliberate act by an operator who knows the target's current state." The operator's
-- knowledge was the whole mechanism, and it lived nowhere the appliance could read.
--
-- WHY THIS RUNS BEFORE 018 AND 019, and not after them. Block E: "a ledger written against
-- a database with no deltas pending is a ledger whose first row is honest." 0.4.0 adds no
-- migration of its own — a `table` node is a row, a header-row index is a measurement, a
-- preset is data — so at the moment this file runs, every database an operator is running
-- is at 016 and nothing is outstanding. The backfill below can therefore assert fifteen
-- rows that are true when it writes them. Run after 018 and 019 instead, and the backfill
-- would have to either claim those two as applied (false on any database that has not had
-- them) or guess which of the seventeen a given target is missing — which is the state this
-- table exists to end. Each delta from 018 onward records ITSELF, in its own transaction,
-- which is the only way a row can be observed rather than assumed.
--
-- WHAT THE COLUMNS SEPARATE, and why there are two timestamps. `recorded_at` is when this
-- row appeared and is never in doubt. `applied_at` is when the DDL the row names actually
-- ran against this database, and for the fifteen backfilled rows NOBODY KNOWS: 002 through
-- 016 were applied by hand at times no one wrote down. Collapsing the two would stamp
-- fifteen migrations with today's date and make the ledger's first act a fabrication, which
-- is the precise opposite of the honest-first-row argument that scheduled it. NULL
-- `applied_at` with `source = 'backfill'` says "present, not observed", and that is the
-- strongest true statement available.
--
-- `source` has three values and they are three different warrants:
--   'schema'   — `schema.sql` built this database and already contained everything the
--                named delta adds. The delta never ran and never should; `applied_at` is
--                the build time, which is honestly when the DDL entered the database.
--   'delta'    — the migration file ran here and wrote its own row. `applied_at` is NOW().
--   'backfill' — this migration asserted the row on an existing database, on the operator's
--                word (guarded below) that the schema was current. `applied_at` is NULL.
--
-- A DATABASE BUILT FROM `schema.sql` IS NOT AT ZERO. `schema.sql` is the complete current
-- DDL, so a fresh database already holds everything 002 through 017 add, and its ledger
-- says so with seventeen `'schema'` rows written by `schema.sql` itself. Without them the
-- ledger of a brand-new database would be empty and would read as "seventeen deltas
-- pending" — an operator following it would apply sixteen deltas to a database that has
-- their effects already, which is the hazard `sql/__init__.py` names as "neither necessary
-- nor safe". The ledger records the STATE of a database, not the route it took there.
--
-- APPLYING 017 TO A `schema.sql`-BUILT DATABASE DOES NOT DOUBLE-COUNT, and it is worth
-- being exact about why. `name` is the primary key and every INSERT here is
-- `ON CONFLICT DO NOTHING`, so the seventeen rows already present win and keep their
-- `'schema'` warrant; the table itself is `IF NOT EXISTS`. Nothing is written twice and
-- nothing is downgraded from observed to asserted. This is not a fallback masking a
-- mistake — a ledger row is an idempotent assertion about the database's shape, and two
-- routes to the same true statement must not produce two rows. Running it there is still
-- pointless, and `jmfts-init-db` does not.
--
-- THE GUARD IS NOT DECORATION. The backfill's fifteen rows are only true if this database
-- really is at 016, and this migration cannot ask an operator. It can ask the schema: 016
-- adds `documents.produced_by`, the numbering is linear, and a database missing that column
-- is not at 016 whatever else is true of it. Missing it, the migration RAISES and writes
-- nothing rather than recording fifteen claims it cannot support. There is no partial mode
-- and no "record what we can" path: a ledger that is right about some rows is worth less
-- than no ledger, because it is believed.
--
-- REVERSIBILITY: purely additive, and inert. Nothing in 0.4.0 reads this table — the
-- reading functions in `jmfts_core/sql/__init__.py` are pure functions over the shipped
-- file set and take the applied names as an argument. A 0.3.x process ignores it entirely.

BEGIN;

CREATE TABLE IF NOT EXISTS schema_migrations (
    -- The FILE NAME, not the number, because the file name is what identifies a migration
    -- everywhere else: `jmfts_core.sql.migration_sql(name)` takes it, `migration_names()`
    -- returns it, and `Migration.number` is derived FROM it rather than the other way
    -- around. A renamed file is a different migration as far as the shipped package is
    -- concerned, and the ledger should say so rather than quietly match on an integer.
    name TEXT PRIMARY KEY,

    -- When the DDL this row names entered THIS database. NULL means the row was backfilled
    -- and the time is genuinely unknown; see the two-timestamp note above. Deliberately
    -- nullable, and deliberately not defaulted.
    applied_at TIMESTAMPTZ,

    -- When this ROW was written. Always known, never null.
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- The warrant behind the row: 'schema', 'delta' or 'backfill'. CHECKed rather than left
    -- open, because a fourth value would be a fourth kind of claim and nothing reads a
    -- string it has no rule for.
    source TEXT NOT NULL CHECK (source IN ('schema', 'delta', 'backfill')),

    -- A backfilled row has no application time; an observed one does. Stating the
    -- correspondence as a constraint stops a later writer stamping NOW() on an assertion.
    CONSTRAINT schema_migrations_backfill_has_no_applied_at CHECK (
        (source = 'backfill' AND applied_at IS NULL)
        OR (source <> 'backfill' AND applied_at IS NOT NULL)
    )
);

-- The guard. See "THE GUARD IS NOT DECORATION" above: this migration asserts fifteen rows
-- about a schema it did not build, and 016's column is the cheapest true test that the
-- assertion holds. `current_schema()` rather than a bare table name so a search_path
-- pointing at a second copy of the tables cannot answer for the one being migrated.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'documents'
          AND column_name = 'produced_by'
    ) THEN
        RAISE EXCEPTION
            'documents.produced_by is missing, so this database is not at migration 016. '
            'The 002-016 backfill in 017_migration_ledger.sql would be false here. Apply '
            'the outstanding deltas first, then re-run 017.';
    END IF;
END $$;

-- BEGIN LEDGER BACKFILL -- 002 through 016; see `jmfts_core.sql.migration_names()`
-- There is no 001. The original schema was never a delta (`sql/__init__.py`), so the
-- ledger has nothing to say about it and inventing a row would claim a file exists.
INSERT INTO schema_migrations (name, applied_at, source) VALUES
    ('002_temporal_triples.sql', NULL, 'backfill'),
    ('003_purge_ghost_predicates.sql', NULL, 'backfill'),
    ('004_document_position.sql', NULL, 'backfill'),
    ('005_document_event_time.sql', NULL, 'backfill'),
    ('006_access_control.sql', NULL, 'backfill'),
    ('007_term_postings_doc_index.sql', NULL, 'backfill'),
    ('008_document_settled_lifecycle.sql', NULL, 'backfill'),
    ('009_document_blobs.sql', NULL, 'backfill'),
    ('010_task_queue.sql', NULL, 'backfill'),
    ('011_task_queue_heartbeat.sql', NULL, 'backfill'),
    ('012_task_queue_batched.sql', NULL, 'backfill'),
    ('013_rdf_layer.sql', NULL, 'backfill'),
    ('014_entity_roots.sql', NULL, 'backfill'),
    ('015_evidence_rows.sql', NULL, 'backfill'),
    ('016_document_produced_by.sql', NULL, 'backfill')
ON CONFLICT (name) DO NOTHING;
-- END LEDGER BACKFILL

-- 017 records itself, and it is the first row that can be observed rather than asserted:
-- the DDL above ran in this transaction, so NOW() is true. Every delta from here on ends
-- with the same three lines. `ON CONFLICT DO NOTHING` so a re-run of 017 against a
-- `schema.sql`-built database leaves that database's own 'schema' row standing.
INSERT INTO schema_migrations (name, applied_at, source) VALUES
    ('017_migration_ledger.sql', NOW(), 'delta')
ON CONFLICT (name) DO NOTHING;

COMMIT;
