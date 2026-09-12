-- Migration 021: the sheet profile stops being invisible.
--
-- `INGEST_SPEC.md` 8.5 says of the profile node that it "is embedded and retrievable like
-- any other node", and that "a retrieval hit on the profile tells the agent which columns
-- exist, which ones are closed sets, and which one identifies a row". It carried
-- `usetype = 'summary'`, and `summary` is in BOTH `Settings.search_exclude_usetypes` and
-- `bm25_exclude_usetypes` — so it was retrievable by no method at all. The spec and the
-- shipped config disagreed, and the config won silently.
--
-- WHY THE FIX IS A NEW USETYPE RATHER THAN A SHORTER EXCLUSION LIST. The exclusion of
-- `summary` is correct for the two things that actually carry it. A `summarize:tree` node's
-- vector is byte-identical to its source node's — 13,755 of 13,755 pairs at cosine distance
-- 0.000000 on the reference corpus — so admitting them returns every answer twice; and an
-- LLM summary's text is already reachable on the node it summarises, through
-- `effective_content` and the projection added in `188e973`. Dropping `summary` from the
-- lists would fix the profile by breaking those.
--
-- WHAT A PROFILE IS, AND WHY IT IS NOT A SUMMARY. It is MEASURED: counted from cells, no
-- model, no inference about meaning — 8.5's factoid table is the whole of it. And for a
-- sheet whose first row is not a header it is the ONLY text the pipeline produces: 18 of 33
-- sheets on the reference corpus wrote zero records, each recording `INGEST_SPEC.md 8.3's
-- header_row is false for this sheet`, and their profile is what this appliance knows about
-- them. A summary is authored by a model or is a duplicate of another node's vector.
--
-- THE BACKFILL IS EXACT, unlike `016`'s and `019`'s. `produced_by` names the writer, so
-- `produced_by = 'profile:sheet'` IS the set of profile nodes and there is no guessing to
-- do — every row it selects was written by `run_profile_sheet` and by nothing else. On a
-- database that has never ingested a workbook it updates zero rows.
--
-- REVERSIBILITY: a 0.4.x process reading this database sees `usetype = 'profile'` where it
-- expected `summary`. It does not crash — no code branches on that string, the exclusion
-- lists are a membership test and `profile` is simply not in them — but the profile nodes
-- become visible to search on the old code too, which is the intended behaviour arriving
-- early rather than a fault. Reverting is `UPDATE ... SET usetype = 'summary'` over the
-- same predicate.

BEGIN;

UPDATE documents SET usetype = 'profile'
WHERE produced_by = 'profile:sheet' AND usetype = 'summary';

-- Every delta from 017 on ends with these three lines. `ON CONFLICT DO NOTHING` so a re-run
-- against a `schema.sql`-built database leaves that database's own 'schema' row standing.
INSERT INTO schema_migrations (name, applied_at, source) VALUES
    ('021_profile_usetype.sql', NOW(), 'delta')
ON CONFLICT (name) DO NOTHING;

COMMIT;
