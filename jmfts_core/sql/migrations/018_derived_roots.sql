-- Migration 018: a derived tree gets its own root, keyed by (access, tree kind).
--
-- `docs/SPRINT_0_5_0.md` Block C step 10. This file is a copy of
-- `014_entity_roots.sql` with one difference, and Part 0.3 says the copy IS the argument:
-- "Three transferable pieces: a derived tree gets its own root; the root is keyed by access
-- rather than by tree position; and the migration declines to move existing rows and says
-- so." 014 made every one of those decisions for entities, against the same hazard, in this
-- repository — so what follows cites it rather than re-deriving it.
--
-- WHAT A DERIVED ROOT IS FOR. `raptor_summarize` today REPARENTS the nodes it summarises
-- under the summary it produced, so a derivation mutates the structure it derived from
-- (0.5.0 Part 1). A derived tree is a parallel tree: its leaves resolve, through link edges,
-- to the source tree's leaves — the leaf projection of 0.5.0 Part 3.1 — and that projection
-- is total only if no derivation moved its own inputs. The root is where the parallel tree
-- hangs so that it can own nothing in the source tree.
--
-- THE ONE DIFFERENCE FROM 014: KEYED PER (ACCESS, TREE KIND), NOT PER ACCESS. 0.5.0 open
-- question 6.6, answered. 014 mints one entities root per distinct access because there is
-- exactly one kind of thing under it. A summary tree, a keyword tree and an argument tree
-- over the same access are three kinds, and a shared root would make the tree kind a
-- `usetype` filter over a mixed subtree — while Part 3.1's leaf projection is PER TREE, so
-- "everything the summary tree says about this sub-corpus" would stop being a subtree query
-- and become a subtree query plus a filter. The cost is more roots: adding a tree kind
-- mints one per existing access. 014's get-or-create discipline already carries that
-- safely, which is why the cost is affordable.
--
-- So `UNIQUE (access_key, tree_kind)` replaces 014's `access_key TEXT NOT NULL UNIQUE`,
-- and `document_id` stays UNIQUE on its own. Both are still load-bearing and neither is
-- redundant, in exactly 014's words: the pair "(access_key, tree_kind)" is the
-- get-or-create conflict key (two workers racing one key must not mint two roots);
-- `document_id` says a root serves one key, so a root can never accumulate a second
-- meaning. The second constraint is the one that would silently stop mattering if it were
-- dropped as "already covered", and it is not covered — nothing else stops one document
-- being registered as the root for two different (access, kind) pairs.
--
-- `access_key` is the canonical text of `jmfts_core.access.access_key` — "7:read,12:write",
-- principal id ascending, one pair per principal, `write` beating `read`. The empty string
-- is the ungoverned key, and its root carries no grants, so it is public by exactly the rule
-- that makes a document under no ACR public. In the single-user default there is one
-- derived root per tree kind, all of them under the empty key, and nothing else changes.
--
-- NO BACKFILL, AND HERE IT IS NOT EVEN AVAILABLE. 014 declined a backfill and gave reasons
-- — re-deriving access from a parent that may since have moved, changing document paths in
-- a data migration. 0.5.0's case is stronger than a decision: THE REPARENT DID NOT RECORD
-- THE PREVIOUS PARENT. `raptor_summarize` moved each member under the summary node and kept
-- no note of where it came from, so there is no column, no evidence row and no link that
-- says what the tree looked like before. Undoing it is not declined, it is not possible.
-- Summaries produced before this migration keep their absorbed subtrees; the derived root
-- holds what is produced after it, and 0.5.0 step 14 is where that split is recorded rather
-- than deferred.
--
-- NOTHING WRITES TO THIS TABLE YET, and that is not an oversight. The writer is 0.5.0
-- Block C step 11 — a rollup handler that creates the summary node under this root and
-- links down to the members — and step 11 is a design pass that is deliberately not in this
-- workflow. Open question 6.1 (whether `Fact.locus` grows a term for a node in another tree,
-- or the cross-tree write stays invisible to `EXPLAIN`) is unanswered and step 11 cannot be
-- built through it. `jmfts_core/derived_roots.py` ships the get-or-create so that step 11 is
-- a handler and not a handler plus a table plus a lookup; nothing in `jmfts_core` calls it.
--
-- REVERSIBILITY: purely additive. A 0.4.x process ignores the table.

BEGIN;

CREATE TABLE IF NOT EXISTS derived_roots (
    id SERIAL PRIMARY KEY,
    -- Unbounded TEXT rather than VARCHAR(n) for 014's reason: the key's length is the
    -- number of principals granted on one chain and no n is defensibly the limit. The btree
    -- entry cap near 2704 bytes is the real bound, and an INSERT that hits it fails loudly.
    access_key TEXT NOT NULL,
    -- WHICH parallel tree. VARCHAR(100) to sit at the same width as `documents.usetype` and
    -- `documents.produced_by`, which is the company it keeps: a short name from a
    -- vocabulary the handler declares ('summary' first, then keyword, argument, question
    -- trees), not free text and not a key derived from data.
    tree_kind VARCHAR(100) NOT NULL,
    document_id INTEGER NOT NULL UNIQUE REFERENCES documents(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (access_key, tree_kind)
);

-- The first delta to record itself. If 017 has not run here, `schema_migrations` does not
-- exist and this statement aborts the transaction, taking the table above with it — which
-- is the intended coupling, not an accident: a database that applies 018 without 017 would
-- be one migration further from the state 017's backfill asserts, and 017 would then have
-- to guess. Apply them in number order, as `migration_names()` returns them.
INSERT INTO schema_migrations (name, applied_at, source) VALUES
    ('018_derived_roots.sql', NOW(), 'delta')
ON CONFLICT (name) DO NOTHING;

COMMIT;
