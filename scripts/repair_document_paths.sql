-- Repair documents.path after the update_document_path() trigger bug.
--
-- The old trigger appended the PARENT row's parent_id column (not
-- NEW.parent_id) and jsonb || NULL is NULL, so every document created with a
-- parent got path = NULL — breaking all subtree scoping (path @> [id]) in
-- search, get_subtree, and get_children(depth=-1).
--
-- Run AFTER applying the fixed function from schema.sql:
--     psql -h localhost -U jmfts -d jmfts -f scripts/repair_document_paths.sql
--
-- Recomputes path for the whole table from parent_id linkage. The UPDATE
-- only touches path, so trigger_update_document_path (UPDATE OF parent_id)
-- does not fire; trigger_documents_updated_at does, which is truthful.

WITH RECURSIVE tree AS (
    SELECT id, '[]'::jsonb AS new_path
    FROM documents
    WHERE parent_id IS NULL
    UNION ALL
    SELECT d.id, t.new_path || to_jsonb(d.parent_id)
    FROM documents d
    JOIN tree t ON d.parent_id = t.id
)
UPDATE documents
SET path = tree.new_path
FROM tree
WHERE documents.id = tree.id
  AND documents.path IS DISTINCT FROM tree.new_path;
