-- JMFTS Schema: John McCardle's Fusion Tree Search
-- PostgreSQL with pgvector extension

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;  -- For fuzzy text matching

-- ============================================================================
-- CORE DOCUMENT STORAGE
-- ============================================================================

CREATE TABLE documents (
    id SERIAL PRIMARY KEY,
    parent_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,

    -- Content
    title TEXT,
    content TEXT,
    structured_content JSONB DEFAULT '{}'::jsonb,

    -- Matryoshka embedding (768-dim for modernbert-embed-base)
    embed vector(768),

    -- Tree navigation: JSONB array of ancestor IDs for efficient subtree queries
    path JSONB DEFAULT '[]'::jsonb,

    -- Document classification
    usetype VARCHAR(100),

    -- Explicit sibling ordering (CR-1). Sparse/nullable: only set for ordered
    -- subtrees (sections, conversation branches). Ordering contract is
    -- `ORDER BY position ASC NULLS LAST, created_at ASC, id ASC`, so NULL falls
    -- back to created_at. Not unique per (parent_id, position) — ties resolve via
    -- the created_at/id tail. Roots never carry a position. See migration 004.
    position INTEGER,

    -- Timestamps (SYSTEM time: when the row entered/last changed in this store)
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),

    -- DOMAIN time (sparse): when the thing this document records actually happened,
    -- for imported content whose ingest time says nothing useful (transcripts,
    -- backfills, benchmark corpora). NULL for documents authored in place. Read as
    -- COALESCE(event_time, created_at). Mirrors the bi-temporal split `triples`
    -- already makes (valid_from/valid_until vs created_at/recorded_at). Not an
    -- access clock, and not interchangeable with updated_at. See migration 005.
    event_time TIMESTAMPTZ,

    -- Content deduplication
    content_hash VARCHAR(64),

    -- INGEST LIFECYCLE. Separates "a record being shuffled between pipeline stages"
    -- from "a record that is finished, embedded, searchable and referenced elsewhere".
    --   in_flight — the node exists; tasks may still be pending for it or its subtree,
    --               so its content and its children may still change.
    --   settled   — its own work is done AND every child is settled. Nothing changes it
    --               except an explicit correction.
    --   failed    — a task for this node failed permanently; no retry is scheduled.
    -- `failed` is not optional: without it a permanently dead node is indistinguishable
    -- from one still in progress, and any sweeper that settles "nodes with no pending
    -- tasks" would eventually publish it.
    -- TEXT + CHECK rather than a Postgres ENUM: this repo's migrations all run inside
    -- BEGIN/COMMIT and `ALTER TYPE ... ADD VALUE` cannot, so an enum would make adding a
    -- fourth state a schema-wide rewrite. Unlike `usetype` (deliberately an open string)
    -- this IS a closed set, so it is constrained here rather than left to convention.
    -- Default 'settled': every row that predates the lifecycle was written by a
    -- synchronous pipeline that had already finished with it. See migration 008.
    settled TEXT NOT NULL DEFAULT 'settled'
        CONSTRAINT ck_documents_settled CHECK (settled IN ('in_flight', 'settled', 'failed'))
);

-- ============================================================================
-- LATE INTERACTION: TOKEN-LEVEL EMBEDDINGS
-- Store top 10% most significant tokens at reduced matryoshka dimensions
-- ============================================================================

CREATE TABLE token_embeddings (
    id SERIAL PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    token_idx INTEGER NOT NULL,
    token_text TEXT,

    -- Token importance score (IDF * attention weight)
    importance_score FLOAT NOT NULL,

    -- Tier for percentage-based filtering (5, 10, ... 50): tier<=X selects the
    -- top X% of tokens by importance. Written by embed_document(); required by
    -- the ORM model. (Was missing here — schema.sql had drifted from the model.)
    tier INTEGER,

    -- Matryoshka embeddings at different dimensions (256+ validated for base model)
    -- Store whichever dimensions are configured for this deployment
    embed_256 halfvec(256),
    embed_384 halfvec(384),
    embed_512 halfvec(512),

    created_at TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE (document_id, token_idx)
);

-- ============================================================================
-- UPLOADED FILE BYTES (POSTGRES LARGE OBJECTS)
-- ============================================================================
-- INGEST_SPEC.md Part 9: uploaded bytes live in a Postgres large object and this
-- table holds only its OID, following vdo_frontend's document_images.lob_oid.
--
-- READ THIS BEFORE TOUCHING THE TABLE: a large object is NOT stored in any table.
-- ON DELETE CASCADE below removes the row and leaves the object behind, orphaned
-- and unreachable. Deletion must go through BlobRepository (lo_unlink first);
-- BlobRepository.find_orphaned_lobs() is how one that got away is found.
--
-- BACKUPS: `pg_dump` in PLAIN format does NOT include large objects unless given
-- `-b`. A plain-format dump of this database without -b restores every row here
-- pointing at bytes that no longer exist. Custom/directory formats include them
-- by default. See migration 009.
CREATE TABLE document_blobs (
    id SERIAL PRIMARY KEY,
    -- One blob per document; a second version is a new node, not a second row.
    document_id INTEGER NOT NULL UNIQUE REFERENCES documents(id) ON DELETE CASCADE,
    -- UNIQUE: two rows naming one object would make deleting either destroy the
    -- other's bytes, silently.
    lob_oid OID NOT NULL UNIQUE,
    -- What to serve the bytes back as. The repository prefers the DETECTED type
    -- over the client's declared one; both stay on the node's `file` block.
    mime_type TEXT NOT NULL,
    -- BIGINT, not INTEGER: an uploaded file is not bounded by anything here.
    byte_size BIGINT NOT NULL,
    -- Bare sha256 hex, matching the documents.content_hash convention.
    content_hash VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================================
-- TASK QUEUE — the ingest scheduler's rows (INGEST_SPEC.md Part 5)
-- ============================================================================
-- Ported from triskelion, with the column set spec 1.4 names plus two JMFTS columns:
-- `scope_document_id` and `write_mode`, which together declare what region of the tree
-- a task RESERVES while it runs (spec 5.3):
--
--     self      this node's own columns and structured_content
--     children  this node, plus creating and moving nodes that have no children
--     subtree   anything below, including descendants' `path`
--
-- The claim query (jmfts_core/repositories/task_queue.py) refuses to hand out a task
-- whose region overlaps one already claimed or running. Triskelion's
-- `target_document_id` is deliberately NOT ported: the node a task is about IS the node
-- it is scoped to, and two columns that could disagree leave nothing to say which one
-- the conflict rules meant.
--
-- `claimed_by` and `service_badge` have no job under spec 5.8's in-process worker and
-- stay anyway — they cost a nullable VARCHAR each, they are what a later split back into
-- processes needs, and Part 7's review tasks need the badge immediately.
--
-- The retry POLICY is not here. It is in TaskQueueRepository.fail(), in Python, in one
-- place; triskelion split it across a plpgsql function and a wrapper that only called
-- it. There is also no `retry_failed_tasks()` sweep: retry is folded into the claim
-- query, which treats a failed-but-retryable row under its cap as claimable once
-- `retry_after` has passed. See migration 010.
CREATE TABLE task_queue (
    id SERIAL PRIMARY KEY,
    -- Part 4's task list: 'probe', 'extract:text', 'structure:declared', 'summarize', …
    task_type VARCHAR(50) NOT NULL,
    -- The node this task is scoped to: what it is about AND what it reserves.
    scope_document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    write_mode VARCHAR(10) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    priority INTEGER NOT NULL DEFAULT 0,
    -- Ordering WITHIN one node (spec 5.5). Cross-level ordering is late-bound in the
    -- settle walk instead, because a planned list goes stale when a child is added.
    dependencies INTEGER[],
    -- What the task should run with, and spec 6.1's diff key over it.
    params JSONB NOT NULL DEFAULT '{}'::jsonb,
    param_fingerprint TEXT NOT NULL,
    service_badge VARCHAR(50),
    claimed_by VARCHAR(100),
    -- Server-side clocks: `retry_after` is compared against the server's NOW().
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    error TEXT,
    error_type VARCHAR(20),
    retryable BOOLEAN NOT NULL DEFAULT TRUE,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    retry_after TIMESTAMPTZ,

    CONSTRAINT ck_task_queue_status
        CHECK (status IN ('pending', 'claimed', 'running', 'completed', 'failed')),
    CONSTRAINT ck_task_queue_write_mode
        CHECK (write_mode IN ('self', 'children', 'subtree')),
    CONSTRAINT ck_task_queue_error_type
        CHECK (error_type IS NULL OR error_type IN
               ('retryable', 'permanent', 'timeout', 'dependency'))
);

-- ============================================================================
-- DOCUMENT RELATIONSHIPS (GRAPH EDGES)
-- ============================================================================

CREATE TABLE document_links (
    id SERIAL PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    target_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    link_type VARCHAR(50) NOT NULL,
    score FLOAT DEFAULT 1.0,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE (source_id, target_id, link_type)
);

-- ============================================================================
-- KNOWLEDGE GRAPH: PREDICATES & TRIPLES
-- ============================================================================

CREATE TABLE predicates (
    id SERIAL PRIMARY KEY,
    name VARCHAR(200) NOT NULL UNIQUE,
    domain VARCHAR(100),
    description TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Fact classification per Zep/Graphiti taxonomy
CREATE TYPE fact_type AS ENUM ('atemporal', 'static', 'dynamic');

CREATE TABLE triples (
    id SERIAL PRIMARY KEY,
    subject_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    predicate_id INTEGER NOT NULL REFERENCES predicates(id) ON DELETE CASCADE,
    object_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    source_document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),

    -- Temporal validity window
    valid_from TIMESTAMPTZ,
    valid_until TIMESTAMPTZ,
    recorded_at TIMESTAMPTZ DEFAULT NOW(),

    -- Fact classification (Zep/Graphiti taxonomy)
    fact_type fact_type NOT NULL DEFAULT 'atemporal',

    -- Edge invalidation
    invalidated_at TIMESTAMPTZ,
    invalidated_by INTEGER REFERENCES triples(id) ON DELETE SET NULL,
    invalidation_reason TEXT,

    UNIQUE(subject_id, predicate_id, object_id)
);

CREATE INDEX ix_triples_subject ON triples(subject_id);
CREATE INDEX ix_triples_object ON triples(object_id);
CREATE INDEX ix_triples_predicate ON triples(predicate_id);
CREATE INDEX ix_triples_valid_range ON triples(valid_from, valid_until);
CREATE INDEX ix_triples_fact_type ON triples(fact_type);
CREATE INDEX ix_triples_invalidated ON triples(invalidated_at) WHERE invalidated_at IS NOT NULL;

-- ============================================================================
-- BM25 SEARCH INFRASTRUCTURE
-- ============================================================================

-- Search index definitions
CREATE TABLE search_indexes (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) UNIQUE NOT NULL,
    description TEXT,

    -- BM25 configuration
    config JSONB DEFAULT '{"k1": 1.2, "b": 0.75}'::jsonb,

    -- Corpus statistics for BM25
    total_docs INTEGER DEFAULT 0,
    avg_doc_length FLOAT DEFAULT 0,

    -- Capabilities flags
    capabilities JSONB DEFAULT '{"bm25": true, "maxsim": false}'::jsonb,

    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Many-to-many: which document subtrees belong to which indexes
CREATE TABLE search_index_members (
    index_id INTEGER NOT NULL REFERENCES search_indexes(id) ON DELETE CASCADE,
    root_document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    added_at TIMESTAMPTZ DEFAULT NOW(),

    PRIMARY KEY (index_id, root_document_id)
);

-- Per-document index entries (for doc length normalization)
CREATE TABLE search_index_entries (
    index_id INTEGER NOT NULL REFERENCES search_indexes(id) ON DELETE CASCADE,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    doc_length INTEGER NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),

    PRIMARY KEY (index_id, document_id)
);

-- Inverted index: term -> document postings
CREATE TABLE search_term_postings (
    index_id INTEGER NOT NULL,
    term TEXT NOT NULL,
    document_id INTEGER NOT NULL,
    term_freq INTEGER NOT NULL,

    PRIMARY KEY (index_id, term, document_id),
    FOREIGN KEY (index_id, document_id) REFERENCES search_index_entries(index_id, document_id) ON DELETE CASCADE
);

-- Per-term statistics for IDF calculation
CREATE TABLE search_term_stats (
    index_id INTEGER NOT NULL REFERENCES search_indexes(id) ON DELETE CASCADE,
    term TEXT NOT NULL,
    doc_freq INTEGER NOT NULL,

    PRIMARY KEY (index_id, term)
);

-- ============================================================================
-- INDEXES
-- ============================================================================

-- Document tree navigation
CREATE INDEX idx_documents_parent ON documents(parent_id);
-- Sibling-ordering contract (CR-1): position ASC NULLS LAST, created_at, id
CREATE INDEX idx_documents_parent_position
    ON documents (parent_id, position ASC NULLS LAST, created_at ASC, id ASC);
-- DELIBERATELY NOT PARTIAL, unlike the two retrieval indexes below. Most `path @>`
-- consumers are tree/graph/ACL machinery that must see in-flight nodes precisely
-- because they are the code responsible for managing them: get_children(depth=-1),
-- get_subtree(include_in_flight=True), graph_analysis's candidate gather, and the
-- subtree-RBAC containment in jmfts_core/access.py. None of those can carry a
-- `settled = 'settled'` predicate, so a partial index would be unprovable for them
-- and would silently turn every one into a sequential scan. See migration 008.
CREATE INDEX idx_documents_path ON documents USING GIN (path);
CREATE INDEX idx_documents_usetype ON documents(usetype);

-- Full-text search (GIN) — PARTIAL: only settled rows are retrievable, so only
-- settled rows are indexed. fulltext_search carries the matching predicate.
CREATE INDEX idx_documents_content_fts ON documents
    USING GIN (to_tsvector('english', COALESCE(title, '') || ' ' || COALESCE(content, '')))
    WHERE settled = 'settled';

-- Structured content queries
CREATE INDEX idx_documents_structured ON documents USING GIN (structured_content);

-- Vector search (HNSW) - cosine similarity — PARTIAL: a chunk that is written,
-- embedded, superseded and rebuilt during ingestion never causes an insert-then-delete
-- in the HNSW graph. It enters the graph once, when it settles.
CREATE INDEX idx_documents_embed ON documents
    USING hnsw (embed vector_cosine_ops)
    WITH (m = 16, ef_construction = 64)
    WHERE settled = 'settled';

-- Token embeddings - for late interaction queries
CREATE INDEX idx_token_embeddings_doc ON token_embeddings(document_id);
CREATE INDEX idx_token_embeddings_importance ON token_embeddings(document_id, importance_score DESC);

-- Token embedding vector indexes (IVFFlat) - halfvec for ~2x storage savings
CREATE INDEX idx_token_embed_256_ivf ON token_embeddings
    USING ivfflat (embed_256 halfvec_cosine_ops) WITH (lists = 1024);
CREATE INDEX idx_token_embed_384 ON token_embeddings
    USING hnsw (embed_384 halfvec_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX idx_token_embed_512 ON token_embeddings
    USING hnsw (embed_512 halfvec_cosine_ops) WITH (m = 16, ef_construction = 64);

-- BM25 term lookup
CREATE INDEX idx_term_postings_lookup ON search_term_postings(index_id, term);
-- BM25 per-document lookup: serves index_document()'s (index_id, document_id)
-- SELECT/DELETE and the reverse FK check on search_index_entries deletion, which
-- would otherwise scan all of an index's postings. See migration 007.
CREATE INDEX idx_term_postings_doc ON search_term_postings(index_id, document_id);

-- Document links
CREATE INDEX idx_links_source ON document_links(source_id);
CREATE INDEX idx_links_target ON document_links(target_id);

-- Uploaded blobs. document_id and lob_oid are already indexed by their UNIQUE
-- constraints; the content hash is not, and it is the lookup that answers "have
-- we already stored these exact bytes" (spec 6.1's dedupe key).
CREATE INDEX idx_document_blobs_hash ON document_blobs(content_hash);

-- Task queue (see migration 010 for the full reasoning on each).
-- Spec 5.2's "structuring complete" query — asked once per node per settle walk.
CREATE INDEX idx_task_queue_scope ON task_queue(scope_document_id, status);
-- The claim query's candidate scan and its ORDER BY. Partial: 'completed' rows
-- accumulate until purged and are never claim candidates.
CREATE INDEX idx_task_queue_claimable
    ON task_queue(priority DESC, created_at ASC, id ASC)
    WHERE status IN ('pending', 'failed');
-- The conflict predicate's inner NOT EXISTS: which tasks hold a reservation right now.
CREATE INDEX idx_task_queue_active
    ON task_queue(scope_document_id)
    WHERE status IN ('claimed', 'running');
-- Spec 6.3 reads the DAG backward — "tasks whose dependencies contain this id" — which
-- is an array containment query.
CREATE INDEX idx_task_queue_dependencies ON task_queue USING GIN (dependencies);
-- Part 7 routing: a badged worker or review queue asking for its own work.
CREATE INDEX idx_task_queue_service ON task_queue(service_badge, status, priority DESC);

-- ============================================================================
-- HELPER FUNCTIONS
-- ============================================================================

-- Update path array when document is moved/created.
-- NB: the appended id must be NEW.parent_id — a bare parent_id here resolves
-- to the PARENT row's parent_id column (NULL for children of roots), and
-- jsonb || NULL is NULL, which used to null out path for the entire tree.
-- COALESCE guards against legacy rows whose path is still NULL.
CREATE OR REPLACE FUNCTION update_document_path()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.parent_id IS NULL THEN
        NEW.path = '[]'::jsonb;
    ELSE
        SELECT COALESCE(path, '[]'::jsonb) || to_jsonb(NEW.parent_id) INTO NEW.path
        FROM documents WHERE id = NEW.parent_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_update_document_path
    BEFORE INSERT OR UPDATE OF parent_id ON documents
    FOR EACH ROW
    EXECUTE FUNCTION update_document_path();

-- Auto-update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_documents_updated_at
    BEFORE UPDATE ON documents
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trigger_search_indexes_updated_at
    BEFORE UPDATE ON search_indexes
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at();

-- ============================================================================
-- SEARCH CONTEXTS (named filter presets)
-- ============================================================================

CREATE TABLE search_contexts (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) UNIQUE NOT NULL,
    description TEXT,

    -- Bundled search parameters (method, weights, usetype, parent_id, etc.)
    config JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TRIGGER trigger_search_contexts_updated_at
    BEFORE UPDATE ON search_contexts
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at();

-- ============================================================================
-- USETYPE PRESENTATIONS (per-usetype rendering rules for /view/{id})
-- ============================================================================

CREATE TABLE usetype_presentations (
    usetype VARCHAR(100) PRIMARY KEY,

    -- markdown | code | json-table | transcript | plain
    renderer VARCHAR(50) NOT NULL,

    -- Renderer-specific extras (e.g. {"language": "python"} for code).
    renderer_config JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- collapsed | inline-headings | hidden | first-paragraph
    child_handling VARCHAR(50) NOT NULL,

    -- footnotes | inline-citations | sidebar | hidden
    link_handling VARCHAR(50) NOT NULL,

    description TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TRIGGER trigger_usetype_presentations_updated_at
    BEFORE UPDATE ON usetype_presentations
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at();

-- Seed defaults for the wiki:* taxonomy and existing core usetypes.
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
    ('transcript',         'transcript','collapsed',        'footnotes',         'Voice transcript');

-- ============================================================================
-- ACCESS CONTROL (subtree RBAC — see migration 006, jmfts_core/access.py)
-- ============================================================================

-- Non-owner identities that access grants are issued to. The shared "owner" bearer
-- (JMFTS_API_TOKEN / the ephemeral boot token) is SYNTHETIC: it bypasses all checks,
-- is matched constant-time without a DB round-trip, and is NOT a row here.
CREATE TABLE principals (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL UNIQUE,
    is_owner BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Bearer token → principal. Only the SHA-256 hex of the token is stored, never the
-- token itself; auth hashes the presented bearer and looks it up here.
CREATE TABLE api_tokens (
    id SERIAL PRIMARY KEY,
    principal_id INTEGER NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
    token_hash CHAR(64) NOT NULL UNIQUE,
    label VARCHAR(255),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ
);
CREATE INDEX idx_api_tokens_hash ON api_tokens(token_hash);

-- Subtree RBAC grants. A grant makes `document_id` an ACCESS-CONTROL ROOT (ACR):
-- being an ACR is DEFINED as appearing here — no flag on `documents`. A principal's
-- effective right on a document is the HIGHEST level granted on any ACR at-or-above it
-- on its `path` (max-over-path; grants are additive). A document under no ACR is
-- unprotected (the single-user default). `write` implies `read`.
CREATE TABLE access_grants (
    id SERIAL PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    principal_id INTEGER NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
    level VARCHAR(10) NOT NULL CHECK (level IN ('read', 'write')),
    created_at TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE (document_id, principal_id)
);
CREATE INDEX idx_access_grants_principal ON access_grants(principal_id);
CREATE INDEX idx_access_grants_document ON access_grants(document_id);

-- ============================================================================
-- DEFAULT DATA
-- ============================================================================

-- Create default search index
INSERT INTO search_indexes (name, description, capabilities)
VALUES ('default', 'Default search index for all documents', '{"bm25": true, "maxsim": true}'::jsonb);
