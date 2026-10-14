-- Migration 024: `structure.max_tokens` is renamed `structure.max_words` (issue #1).
--
-- The option was always a WHITESPACE WORD COUNT — `_split_sentences_packed` counts
-- `len(sentence.split())`, `_split_token_count` slices `text.split()` — and the tokenizer
-- never saw it. Named `max_tokens`, it read as a subword budget, which is the defect class
-- `tests/test_token_proxy_audit.py` exists for: a word count standing where a token count
-- is meant, off by the text's own exchange rate.
--
-- WHAT THIS REWRITES: the `options` evidence row, and only its `structure` group. That row
-- holds two things — what a COLLECTION configures for everything ingested under it
-- (`PUT /collections/{id}/options`), and what an upload recorded it was ingested with. Both
-- are read back through `resolve_options`, which refuses an undeclared option by name, so
-- without this every later ingest under a collection that set `max_tokens` is a 400, and
-- every re-upload comparison against a recorded set fails the same way. The value moves
-- unchanged; nothing about what it counts has changed.
--
-- WHAT THIS DOES NOT REWRITE, AND WHY:
--
-- * `task_queue.params` of a structure task still queued. `param_fingerprint` is SHA-256 of
--   Python's canonical JSON (`sort_keys`, no spaces), which jsonb's text form does not
--   reproduce — jsonb orders keys by length and prints `": "` — so renaming the key here
--   would leave a row whose fingerprint no longer hashes its params, and the re-run diff
--   (spec 6.1) would be comparing against a value nothing can recompute. Such a task is
--   REFUSED instead: `_build_tree` raises on a `max_tokens` parameter, PERMANENT, naming the
--   rename, rather than reading the default and chunking at a budget nobody asked for.
--   Drain the queue before upgrading and there is nothing to refuse.
-- * Completed attempts and their recorded `params`. They are the record of what ran, under
--   the name it ran with; editing them into agreement would stop them being evidence.
--
-- A set that names BOTH keys has no single right answer and stops the migration, rather
-- than one value silently winning. Unreachable through the API, which never accepted
-- `max_words` before this release.
--
-- REVERSIBILITY: swap the two key names in the same statement. A 0.6.x process reading a
-- migrated row refuses `max_words` as unknown — loudly, the same 400 a typo gets.

BEGIN;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM document_evidence
        WHERE name = 'options'
          AND jsonb_typeof(value -> 'structure') = 'object'
          AND (value -> 'structure') ? 'max_tokens'
          AND (value -> 'structure') ? 'max_words'
    ) THEN
        RAISE EXCEPTION 'an options row names both structure.max_tokens and '
            'structure.max_words; resolve it by hand before applying 024';
    END IF;
END $$;

UPDATE document_evidence
SET value = jsonb_set(
    value #- '{structure,max_tokens}',
    '{structure,max_words}',
    value -> 'structure' -> 'max_tokens'
)
WHERE name = 'options'
  AND jsonb_typeof(value -> 'structure') = 'object'
  AND (value -> 'structure') ? 'max_tokens';

-- Every delta from 017 on ends with these three lines. `ON CONFLICT DO NOTHING` so a re-run
-- against a `schema.sql`-built database leaves that database's own 'schema' row standing.
INSERT INTO schema_migrations (name, applied_at, source) VALUES
    ('024_max_words.sql', NOW(), 'delta')
ON CONFLICT (name) DO NOTHING;

COMMIT;
