-- Migration: add usetype_presentations table.
-- Apply against an existing JMFTS database to enable Phase 2 /view/{id} rendering.
-- Idempotent — safe to re-run.

CREATE TABLE IF NOT EXISTS usetype_presentations (
    usetype VARCHAR(100) PRIMARY KEY,
    renderer VARCHAR(50) NOT NULL,
    renderer_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    child_handling VARCHAR(50) NOT NULL,
    link_handling VARCHAR(50) NOT NULL,
    description TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trigger_usetype_presentations_updated_at'
    ) THEN
        CREATE TRIGGER trigger_usetype_presentations_updated_at
            BEFORE UPDATE ON usetype_presentations
            FOR EACH ROW
            EXECUTE FUNCTION update_updated_at();
    END IF;
END $$;

INSERT INTO usetype_presentations
    (usetype, renderer, child_handling, link_handling, description)
VALUES
    ('*',                  'markdown', 'collapsed',         'footnotes',         'Catch-all default'),
    ('wiki:root',          'markdown', 'collapsed',         'sidebar',           'Wiki root index'),
    ('wiki:source',        'markdown', 'collapsed',         'footnotes',         'Source/article'),
    ('wiki:source-chunk',  'plain',    'hidden',            'hidden',            'Source chunk'),
    ('wiki:entity',        'markdown', 'inline-headings',   'footnotes',         'Wiki entity page'),
    ('wiki:concept',       'markdown', 'inline-headings',   'footnotes',         'Wiki concept page'),
    ('wiki:analysis',      'markdown', 'collapsed',         'footnotes',         'Agent-produced analysis'),
    ('wiki:index',         'markdown', 'inline-headings',   'inline-citations',  'Wiki index/TOC'),
    ('wiki:log',           'markdown', 'first-paragraph',   'footnotes',         'Append-only wiki log'),
    ('wiki:schema',        'markdown', 'collapsed',         'footnotes',         'Wiki schema/AGENTS doc'),
    ('markdown',           'markdown', 'collapsed',         'footnotes',         'Generic markdown root'),
    ('chunk',              'plain',    'hidden',            'hidden',            'Chunked sub-document'),
    ('conversation',       'transcript','collapsed',        'sidebar',           'Conversation thread'),
    ('raw',                'plain',    'collapsed',         'footnotes',         'Raw text'),
    ('transcript',         'transcript','collapsed',        'footnotes',         'Voice transcript')
ON CONFLICT (usetype) DO NOTHING;
