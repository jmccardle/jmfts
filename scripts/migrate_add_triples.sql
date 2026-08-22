-- Migration: Add knowledge graph tables (predicates + triples)
-- Run against an existing JMFTS database to add triple support.
-- Idempotent: safe to run multiple times.

CREATE TABLE IF NOT EXISTS predicates (
    id SERIAL PRIMARY KEY,
    name VARCHAR(200) NOT NULL UNIQUE,
    domain VARCHAR(100),
    description TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS triples (
    id SERIAL PRIMARY KEY,
    subject_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    predicate_id INTEGER NOT NULL REFERENCES predicates(id) ON DELETE CASCADE,
    object_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    source_document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(subject_id, predicate_id, object_id)
);

CREATE INDEX IF NOT EXISTS ix_triples_subject ON triples(subject_id);
CREATE INDEX IF NOT EXISTS ix_triples_object ON triples(object_id);
CREATE INDEX IF NOT EXISTS ix_triples_predicate ON triples(predicate_id);
