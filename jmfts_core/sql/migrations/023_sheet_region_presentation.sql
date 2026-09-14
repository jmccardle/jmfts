-- Migration 023: a worksheet is rendered as cells, not as an almost-empty markdown document.
--
-- `docs/SPRINT_0_6_0.md` Block F step 29. This is the migration half of the cost step 25
-- named when it made `usetype_presentations.renderer` the web front end's dispatch key:
-- "adding a renderer is a tuple edit plus a migration that every client picks up — not a
-- branch in JavaScript that only this front end has". The tuple edit is
-- `jmfts_core/models/usetype_presentation.py:14`, which grew from five values to nine
-- (`pdf-page`, `image`, `sheet-region`, `office`). This file is the row.
--
-- WHY ONLY ONE OF THE FOUR GETS A ROW. This table is keyed by USETYPE, and three of the four
-- new renderers are chosen by FORMAT rather than by usetype: a PDF and a `.docx` and a
-- markdown file are all `usetype = 'file'`, so no row could point that one usetype at
-- `pdf-page` without pointing it there for the other two as well. Those three are named by
-- the caller instead — `rendererFor("pdf-page")` — which the renderer registry has exported
-- since step 25 precisely so that a view can ask for a renderer by name. `sheet` is different:
-- it is a usetype that means exactly one thing, and what it means is a region of cells.
--
-- WHAT IT FIXES, which is a thing a reader can see. A `sheet` node had no row here, so it fell
-- to the `'*'` catch-all and rendered as markdown. Its `content` is thin — `sheet_tasks.py`
-- builds a markdown grid of the sheet, counts it, reports a boolean and discards it
-- (`docs/SPRINT_0_6_0.md` Part 1, `sheet_tasks.py:437`, `:442`, `:499`) — so what a reader got
-- was an almost-empty document where a spreadsheet should be. `sheet-region` reads
-- `GET /documents/{id}/cells`, which serves the node's own region: its `cells` anchor when it
-- has one, the sheet's measured used range otherwise, with `ref_source` saying which of the
-- three the answer came from.
--
-- `link_handling = 'hidden'`: a worksheet node carries no prose and therefore no citations,
-- and a footnote apparatus under a grid of numbers is furniture. `child_handling` stays
-- `collapsed`, which is the table's own default — a sheet's children are its `record` and
-- `table` nodes, and they are worth listing rather than inlining under the grid.
--
-- IDEMPOTENT, AND IT DOES NOT OVERWRITE AN OPERATOR'S CHOICE. `ON CONFLICT DO NOTHING` on the
-- usetype primary key: an appliance whose operator has already pointed `sheet` at something
-- through `PUT /usetype-presentations/sheet` keeps what they chose. A migration that silently
-- replaced a configured row would be this appliance overruling its operator on upgrade.
--
-- REVERSIBILITY: `DELETE FROM usetype_presentations WHERE usetype = 'sheet'` puts sheet nodes
-- back under the `'*'` default. A 0.5.x process reading a database that has this row renders
-- `sheet` nodes with a renderer its front end does not know — `render/renderers.js` answers
-- `UnknownRendererError` naming the tuple, which is the case that error message was written
-- for ("this front end is older than the appliance"). Nothing server-side branches on the
-- string: `repositories/usetype_presentation.py:119` validates it on WRITE, and this row is
-- written here rather than through that path.

BEGIN;

INSERT INTO usetype_presentations
    (usetype, renderer, child_handling, link_handling, description)
VALUES
    ('sheet', 'sheet-region', 'collapsed', 'hidden', 'Worksheet region, read as cells')
ON CONFLICT (usetype) DO NOTHING;

-- Every delta from 017 on ends with these three lines. `ON CONFLICT DO NOTHING` so a re-run
-- against a `schema.sql`-built database leaves that database's own 'schema' row standing.
INSERT INTO schema_migrations (name, applied_at, source) VALUES
    ('023_sheet_region_presentation.sql', NOW(), 'delta')
ON CONFLICT (name) DO NOTHING;

COMMIT;
