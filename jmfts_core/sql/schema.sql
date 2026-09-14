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
    -- CALLER-OWNED, wholly. The ingest pipeline used to own twenty-three keys in here;
    -- migration 015 moved them to `document_evidence` and nothing writes them back. What
    -- is left is what a caller PUT here — `importance`, which hybrid search reads, and
    -- whatever else a `PATCH /documents/{id}` set. See SPRINT_JOBS.md 13.3.
    structured_content JSONB DEFAULT '{}'::jsonb,

    -- Matryoshka embedding (768-dim for modernbert-embed-base)
    embed vector(768),

    -- Tree navigation: JSONB array of ancestor IDs for efficient subtree queries
    path JSONB DEFAULT '[]'::jsonb,

    -- Document classification
    usetype VARCHAR(100),

    -- WHICH RULE PRODUCED THIS NODE. NULL means asserted — a person, an importer, or an
    -- upload created it, not a rule. Anything else names the rule, which today is the task
    -- type of the atom that wrote the node ('structure:declared', 'extract:sheet', ...).
    -- SPRINT_JOBS.md 4.2: a rule scoped to "the children another rule produced" needs an
    -- identity, not a count, and `usetype` cannot serve — that says what a node IS, and one
    -- structure rung writes both `section` and `chunk`. Mirrors `triples.derived_by`,
    -- NULL convention included. See migration 016.
    produced_by VARCHAR(100),

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
-- EVIDENCE — one row per (document, evidence name). SPRINT_JOBS.md Part 3
-- ============================================================================
-- Everything the ingest pipeline learns about a node. This used to be twenty-three keys in
-- `documents.structured_content`; migration 015 moved them here, and nothing merges them
-- back — a client wanting them asks `GET /documents/{id}/evidence`. The column survives
-- carrying exactly what a CALLER put there, which is why `structured_content['importance']`
-- is still read by hybrid search: it is caller-owned and is not evidence.
--
-- The two reasons are correctness, not speed (13.1, and `scripts/evidence_bench.py`):
--   1. A write to the column was a read-modify-write, so two handlers writing two different
--      names to one node lost one of the writes silently. A PK of (document_id, name) is
--      what removes that.
--   2. 3.2 needs three states — never attempted, written (value may be null), failed — and
--      a column has room for two. Staling a JSONB block means DELETING it, which destroys
--      the distinction. `state` is a column here because it cannot be a key there.
--
-- `jmfts_core.evidence.REGISTRY` is the vocabulary of `name`: what each one asserts, its
-- type, and which leaves inside it something schedules on. A name that is not in it is a
-- typo, and `EvidenceRepository` refuses to write one.
CREATE TABLE document_evidence (
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    -- The registry name, not the column key it used to live under. `anchor` became
    -- `source_anchor` and `anchor_unresolved` became `source_anchor.unresolved` — 2.5's
    -- finding 5: evidence is named by what it asserts, not by where it is stored.
    name TEXT NOT NULL,
    -- NULLABLE, and the null is a result. 3.2: an atom writes every name it produces on
    -- success, null included. A NULL value is "asked and got nothing"; no row is
    -- "never attempted".
    value JSONB,
    -- 3.3: the child ids read, the source evidence read, and the parameters used. Written
    -- by Phase 3; NULL until then.
    fingerprint TEXT,
    -- 3.2's third state. Phase 6's rebinding writes 'stale'; 3.2's retry policy writes
    -- 'failed'. TEXT + CHECK, not an ENUM, for the reason documents.settled is.
    state TEXT NOT NULL DEFAULT 'written'
        CONSTRAINT ck_document_evidence_state
        CHECK (state IN ('written', 'stale', 'failed')),
    PRIMARY KEY (document_id, name)
);

-- Part 4.4's guard reads one name across the corpus: 10x the JSONB path at 100,000 nodes,
-- and the gap grows with the corpus.
CREATE INDEX idx_document_evidence_name ON document_evidence (name);
-- What idx_documents_structured gave the column: a guard that predicates on the value.
CREATE INDEX idx_document_evidence_value ON document_evidence USING GIN (value);
-- Part 9's frontier as a query. The read the column could not express at all.
CREATE INDEX idx_document_evidence_stale ON document_evidence (name, document_id)
    WHERE state = 'stale';

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
    -- Worker liveness while the task is held, NOT a bound on how long it may run. The
    -- worker touches this on a fixed interval, so a slow task keeps its claim as long as
    -- it keeps beating; see migration 011 for why a lease DURATION was rejected.
    heartbeat_at TIMESTAMPTZ,
    -- Where a `batched` task is parked: the external provider's batch id, and when it went
    -- there. A column rather than a key in `params`, because param_fingerprint is derived
    -- from params at enqueue and keys spec 6.1's re-run diff. See migration 012 — and note
    -- that a `batched` row is reachable by NOTHING else, so batched_at is the only way its
    -- stall is ever detected.
    batch_id TEXT,
    batched_at TIMESTAMPTZ,
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
        CHECK (status IN ('pending', 'claimed', 'running', 'batched', 'completed', 'failed')),
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
    -- Which rule produced this edge; NULL means asserted (migration 019). Same column,
    -- same convention and same width as `triples.derived_by`: a rule identity is one
    -- string, and a link and a triple that one rule produced must be findable under the
    -- same name. `WHERE derived_by = :rule` is a complete description of one rule's
    -- output, which is what makes re-derivation a delete-then-insert rather than a diff.
    derived_by VARCHAR(200),
    created_at TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE (source_id, target_id, link_type)
);

-- ============================================================================
-- KNOWLEDGE GRAPH: PREDICATES & TRIPLES
-- ============================================================================

CREATE TABLE predicates (
    id SERIAL PRIMARY KEY,
    name VARCHAR(200) NOT NULL UNIQUE,
    -- WAS `domain`, renamed in migration 013. `rdfs:domain` means "the class a subject
    -- must belong to", which is NOT what this column means — it is the group a predicate
    -- belongs to. `rdfs:domain` is expressed in the ontology, not here.
    namespace VARCHAR(100),
    -- The IRI a published vocabulary knows this predicate by. NULL = local to this
    -- appliance. UNIQUE, so "which predicate does this vocabulary term mean" has one
    -- answer; NULLs do not conflict, so any number of local predicates coexist.
    iri TEXT UNIQUE,
    description TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Fact classification per Zep/Graphiti taxonomy
CREATE TYPE fact_type AS ENUM ('atemporal', 'static', 'dynamic');

CREATE TABLE triples (
    id SERIAL PRIMARY KEY,
    subject_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    predicate_id INTEGER NOT NULL REFERENCES predicates(id) ON DELETE CASCADE,
    -- NULLABLE since migration 013: null here means the object is the literal below.
    -- Before that every object had to be a document node, which is why extracting the
    -- fact "revenue 128000" created a document titled `128000`, embedded it, and put it
    -- in the retrieval index.
    object_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
    -- The literal object's LEXICAL form, verbatim: "1.50" and "1.5" are the same decimal
    -- and different literals. object_datatype is an `xsd:` IRI; NULL means xsd:string —
    -- RDF's plain literal — not "unknown".
    object_literal TEXT,
    object_datatype VARCHAR(100),
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

    -- WHICH RULE PRODUCED THIS ROW. NULL means asserted. "Asserted only" is then
    -- `WHERE derived_by IS NULL` — without this column an inference layer and an asserted
    -- layer are the same rows and cannot be separated later. Nothing writes it yet, and
    -- that is the point: the first rule to land must not be indistinguishable from an
    -- assertion. See migration 013.
    derived_by VARCHAR(200),

    -- Exactly one object. A datatype belongs to a literal, so it is refused on a resource
    -- object rather than ignored there.
    CONSTRAINT ck_triples_object_exactly_one CHECK (
        (object_id IS NOT NULL AND object_literal IS NULL AND object_datatype IS NULL)
        OR (object_literal IS NOT NULL AND object_id IS NULL)
    ),

    -- Resource objects only: NULLs are distinct in a UNIQUE constraint, so a literal row
    -- (object_id NULL) never conflicts here. uq_triple_literal below is its counterpart.
    UNIQUE(subject_id, predicate_id, object_id)
);

-- The literal half of the uniqueness above. md5() because a btree entry is capped near
-- 2704 bytes and object_literal is unbounded TEXT; COALESCE because a NULL datatype means
-- xsd:string and NULLs do not compare equal. See migration 013 for the collision cost.
CREATE UNIQUE INDEX uq_triple_literal
    ON triples (subject_id, predicate_id, md5(object_literal), COALESCE(object_datatype, ''))
    WHERE object_literal IS NOT NULL;

CREATE INDEX ix_triples_subject ON triples(subject_id);
CREATE INDEX ix_triples_object ON triples(object_id);
CREATE INDEX ix_triples_predicate ON triples(predicate_id);
CREATE INDEX ix_triples_valid_range ON triples(valid_from, valid_until);
CREATE INDEX ix_triples_fact_type ON triples(fact_type);
CREATE INDEX ix_triples_invalidated ON triples(invalidated_at) WHERE invalidated_at IS NOT NULL;
-- Partial: derived_by is NULL for every row an assertion writes, so the index holds only
-- the derived minority.
CREATE INDEX ix_triples_derived_by ON triples(derived_by) WHERE derived_by IS NOT NULL;

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
-- SPRINT_JOBS.md 4.3: a frontier scope resolves by (parent_id, produced_by), so the walk at
-- a node recomputes only the subplan rooted at the vertex whose scope resolves to it rather
-- than the whole plan — without which the walk is O(n^2) in tree size. `parent_id` leads
-- because that is the equality every such lookup carries. NOT partial on
-- `produced_by IS NOT NULL`, unlike ix_triples_derived_by: the derived nodes are the
-- MAJORITY of an ingested tree, so the exclusion would buy nothing and would stop the index
-- answering "which children here did a person assert" (9.4). See migration 016.
CREATE INDEX idx_documents_produced_by ON documents(parent_id, produced_by);

-- Containers — a settled node with no `content` of its own, whose text is its children's.
-- PARTIAL because the query only ever asks this direction, and `content` is the widest
-- column on a table that reaches 458 MB on a real corpus, so answering "is this a
-- container" by visiting the row is the expensive way. The predicate must stay identical to
-- `SearchRepository._container_candidates`'s or PostgreSQL will not use it. See migration
-- 020 for the 34.8 ms -> 23.5 ms this bought.
CREATE INDEX idx_documents_container
    ON documents (id) WHERE content IS NULL AND settled = 'settled';

-- Full-text search (GIN) — PARTIAL: only settled rows are retrievable, so only
-- settled rows are indexed. fulltext_search carries the matching predicate.
CREATE INDEX idx_documents_content_fts ON documents
    USING GIN (to_tsvector('english', COALESCE(title, '') || ' ' || COALESCE(content, '')))
    WHERE settled = 'settled';

-- Caller metadata queries. Evidence is NOT in here any more (migration 015); the index
-- that answers a guard is idx_document_evidence_name.
CREATE INDEX idx_documents_structured ON documents USING GIN (structured_content);

-- Vector search (HNSW) - cosine similarity — PARTIAL: a chunk that is written,
-- embedded, superseded and rebuilt during ingestion never causes an insert-then-delete
-- in the HNSW graph. It enters the graph once, when it settles.
--
-- The predicate does NOT cover a rolled-back transaction, which leaves index entries
-- behind exactly as a DELETE does. `docs/ANN_INDEX_HEALTH.md` 1.7 measures what dead
-- entries at a query point cost this index: recall@10 falls from 1.0 to 0.100 at
-- pgvector's default scan mode, and holds at 0.979 under the `strict_order` that
-- `repositories/search.py:432` sets. IVFFlat lost nothing in the same cells.
--
-- Two costs of the choice of HNSW here, both from `ANN_INDEX_HEALTH.md` 1.8, and both
-- to weigh before anyone reaches for a VACUUM-on-delete policy: VACUUM of this index
-- takes 87 s at 100 000 rows against IVFFlat's 0.24 s at the same size, and it costs
-- the same whether one row was deleted or fifty thousand were.
CREATE INDEX idx_documents_embed ON documents
    USING hnsw (embed vector_cosine_ops)
    WITH (m = 16, ef_construction = 64)
    WHERE settled = 'settled';

-- Token embeddings - for late interaction queries
CREATE INDEX idx_token_embeddings_doc ON token_embeddings(document_id);
CREATE INDEX idx_token_embeddings_importance ON token_embeddings(document_id, importance_score DESC);

-- Token embedding vector indexes - halfvec for ~2x storage savings.
--
-- `embed_256` IS HNSW AND WAS IVFFlat UNTIL MIGRATION 022. The IVFFlat index this file
-- built was created here, against a table holding zero rows, and `git grep REINDEX --
-- jmfts_core` returns nothing — so its 1024 k-means centroids were fitted to an empty
-- table and never recomputed. `docs/ANN_INDEX_HEALTH.md` 5.4 measured what that costs and
-- 5.7 measured the property that decided the replacement: an HNSW graph is built by
-- INSERTION, so creating it here and filling it later produces the same index as creating
-- it on a full table. IVFFlat cannot do that, and no `lists` value and no `probes` value
-- repairs it — only a rebuild somebody has to remember to run.
--
-- What it bought, on the real corpus (`docs/STRESS_CORPUS.md` 6.2, 1.37M rows): recall@10
-- 0.7533 -> 0.9667 at IDENTICAL query latency (0.65 ms both), 15 of 30 queries degraded
-- down to 1 of 30. What it cost: 2.7x build (55.9 s vs 20.6 s) and 46% more disk
-- (1050 MB vs 720 MB).
--
-- THE COST THAT IS NOT ON THAT LIST, and the reason `repositories/search.py` sets a scan
-- mode on this path: `token_embeddings` has no `settled` column, so unlike
-- `idx_documents_embed` above this index CANNOT be partial and cannot keep rewritten rows
-- out of the graph. `repositories/document.py:1069` deletes every token row for a document
-- and rewrites them on each re-embed, which left 113,313 dead against 1,370,494 live on
-- the reference corpus (`STRESS_CORPUS.md` 6.3). That is the Parts 1-2 failure mode, and
-- `ANN_INDEX_HEALTH.md` 1.7 measures it at recall 0.100 under pgvector's default scan.
-- `search.py`'s `HNSW_ITERATIVE_SCAN` is what holds it at 0.979.
CREATE INDEX idx_token_embed_256_hnsw ON token_embeddings
    USING hnsw (embed_256 halfvec_cosine_ops) WITH (m = 16, ef_construction = 64);
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
-- "Asserted only" and "derived by this rule", on a column that is NULL for every row
-- today. Partial for `ix_triples_derived_by`'s reason: derived edges are the minority of a
-- link graph, so the index holds only them and the asserted majority is served by not
-- being in it. See migration 019.
CREATE INDEX ix_links_derived_by ON document_links(derived_by) WHERE derived_by IS NOT NULL;

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
-- `batched` is here and NOT in idx_task_queue_lease below: a task parked at a batch
-- provider still owns its node's `effective_content`, but nothing is beating for it and
-- the lease must never reap it. See migration 012.
CREATE INDEX idx_task_queue_active
    ON task_queue(scope_document_id)
    WHERE status IN ('claimed', 'running', 'batched');
-- The poll pass: every task in one outstanding batch, and how long it has been out.
CREATE INDEX idx_task_queue_batch
    ON task_queue(batch_id, batched_at)
    WHERE status = 'batched';
-- Spec 6.3 reads the DAG backward — "tasks whose dependencies contain this id" — which
-- is an array containment query.
CREATE INDEX idx_task_queue_dependencies ON task_queue USING GIN (dependencies);
-- Part 7 routing: a badged worker or review queue asking for its own work.
CREATE INDEX idx_task_queue_service ON task_queue(service_badge, status, priority DESC);
-- The lease reaper's scan: rows holding a reservation, oldest heartbeat first. Partial
-- for the same reason idx_task_queue_active is.
CREATE INDEX idx_task_queue_lease
    ON task_queue(heartbeat_at)
    WHERE status IN ('claimed', 'running');

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
-- ONTOLOGIES AND SHAPE BINDINGS (see migration 013, jmfts_core/models/ontology.py)
-- ============================================================================
-- The same shape as usetype_presentations above: an open string key, policy in JSONB,
-- extended by inserting a row rather than by altering the schema.

CREATE TABLE ontologies (
    -- A second upload under the same name REPLACES that vocabulary; it is not a second
    -- copy of it.
    name VARCHAR(200) PRIMARY KEY,
    -- Not derived from @base or from the first prefix: an ontology may declare neither,
    -- and every locally-minted IRI would then rest on a guess.
    base_iri TEXT NOT NULL,
    -- Exactly the bytes that were uploaded. `shapes` below is a parsed digest of the SHACL
    -- subset this release reads, and a digest is lossy; when the subset widens it is
    -- rebuilt from here rather than from a file somebody has to find again.
    source_turtle TEXT NOT NULL,
    -- {prefix: namespace IRI}, as declared, so Turtle renders back out with the names the
    -- author chose instead of rdflib's invented ns1:.
    prefixes JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- {shape IRI: {targetClass, properties: [...]}}. A cache of a parse, never the
    -- authority over the Turtle above.
    shapes JSONB NOT NULL DEFAULT '{}'::jsonb,
    description TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TRIGGER trigger_ontologies_updated_at
    BEFORE UPDATE ON ontologies
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at();

-- Which shape applies to which documents. Until a shape is bound it constrains nothing,
-- which is what makes uploading an ontology safe: it cannot change how anything already in
-- the tree is validated or extracted.
CREATE TABLE shape_bindings (
    id SERIAL PRIMARY KEY,
    ontology_name VARCHAR(200) NOT NULL REFERENCES ontologies(name) ON DELETE CASCADE,
    -- TEXT and not a foreign key: shapes live in the ontology's JSONB digest, and giving
    -- them their own table would make the digest the authority over the source Turtle.
    shape_iri TEXT NOT NULL,
    -- A CLOSED set, unlike usetype: each value names a different query the resolver runs.
    --   usetype    scope = {"pattern": "profile:sheet"}
    --   subtree    scope = {"parent_id": 42}
    --   documents  scope = {"document_ids": [1, 2, 3]}
    scope_type VARCHAR(20) NOT NULL,
    scope JSONB NOT NULL DEFAULT '{}'::jsonb,
    description TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),

    CONSTRAINT ck_shape_bindings_scope_type
        CHECK (scope_type IN ('usetype', 'subtree', 'documents')),
    CONSTRAINT uq_shape_binding UNIQUE (ontology_name, shape_iri, scope_type, scope)
);

CREATE INDEX ix_shape_bindings_ontology ON shape_bindings(ontology_name);
CREATE INDEX ix_shape_bindings_scope ON shape_bindings USING GIN (scope);

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

-- One entities root per distinct ACCESS (migration 014). An entity node lives under the
-- root whose grants are the effective access of the document that mentioned it, and
-- resolution only looks under that root — which is what stops a restricted document's
-- entities resolving to public nodes and making its facts world-readable. `access_key` is
-- the canonical text of `jmfts_core.access.access_key` ("7:read,12:write"); the empty
-- string is the ungoverned key, whose root carries no grants and is therefore public.
-- Both columns are UNIQUE: a key names one root, a root serves one key.
CREATE TABLE entity_roots (
    id SERIAL PRIMARY KEY,
    access_key TEXT NOT NULL UNIQUE,
    document_id INTEGER NOT NULL UNIQUE REFERENCES documents(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- One derived-tree root per (ACCESS, TREE KIND) (migration 018). Same shape as
-- `entity_roots` and for the same reason — a derived tree hangs under a root whose grants
-- are the effective access of the sub-corpus it derives from, so a derivation owns nothing
-- in the tree it derived from. The ONE difference is the key: `entity_roots` is per access,
-- this is per (access, kind), because a summary tree, a keyword tree and an argument tree
-- over the same access sharing one root would make the kind a `usetype` filter over a mixed
-- subtree, and SPRINT_0_5_0.md 3.1's leaf projection is per tree. `document_id` is
-- separately UNIQUE: a root serves one (access, kind) pair and can never accumulate a
-- second meaning. Nothing writes here yet; the writer is 0.5.0 Block C step 11.
CREATE TABLE derived_roots (
    id SERIAL PRIMARY KEY,
    access_key TEXT NOT NULL,
    tree_kind VARCHAR(100) NOT NULL,
    document_id INTEGER NOT NULL UNIQUE REFERENCES documents(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (access_key, tree_kind)
);

-- ============================================================================
-- MIGRATION LEDGER
-- ============================================================================

-- Which deltas a database has seen (migration 017). Until 017 nothing tracked this, and
-- choosing which files in `jmfts_core/sql/migrations/` to apply was an operator's
-- undocumented knowledge. `name` is the FILE name because that is what identifies a
-- migration everywhere else in the package; `recorded_at` is when the row appeared and is
-- always known; `applied_at` is when the named DDL actually ran here and is NULL exactly
-- for backfilled rows, where nobody knows. `source` is the warrant behind the row —
-- 'schema' (this file built the database and already contained the delta's effect),
-- 'delta' (the migration ran here and wrote its own row), 'backfill' (017 asserted it about
-- an existing database). See 017_migration_ledger.sql for the full argument.
CREATE TABLE schema_migrations (
    name TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source TEXT NOT NULL CHECK (source IN ('schema', 'delta', 'backfill')),
    CONSTRAINT schema_migrations_backfill_has_no_applied_at CHECK (
        (source = 'backfill' AND applied_at IS NULL)
        OR (source <> 'backfill' AND applied_at IS NOT NULL)
    )
);

-- A DATABASE BUILT FROM THIS FILE IS NOT AT ZERO. `schema.sql` is the complete current DDL,
-- so everything 002 through 018 add is already here and the ledger has to say so. Leave
-- these rows out and a brand-new database reads as "seventeen deltas pending", and an
-- operator following it applies deltas to a database that has their effects already — which
-- `jmfts_core/sql/__init__.py` calls "neither necessary nor safe". The ledger records the
-- STATE of a database, not the route it took there, so `applied_at` is the build time: that
-- is honestly when the DDL entered this database.
--
-- EVERY SHIPPED MIGRATION MUST APPEAR HERE. The fence is machine-readable —
-- `jmfts_core.sql.schema_ledger_names()` parses it and `migration_names()` lists the files —
-- so a delta added without its row here is drift a test can catch, rather than a fresh
-- database quietly claiming a migration is outstanding. There is no 001: the original
-- schema was never a delta.
-- BEGIN MIGRATION LEDGER -- mirrors jmfts_core/sql/migrations/; keep in step
INSERT INTO schema_migrations (name, applied_at, source) VALUES
    ('002_temporal_triples.sql', NOW(), 'schema'),
    ('003_purge_ghost_predicates.sql', NOW(), 'schema'),
    ('004_document_position.sql', NOW(), 'schema'),
    ('005_document_event_time.sql', NOW(), 'schema'),
    ('006_access_control.sql', NOW(), 'schema'),
    ('007_term_postings_doc_index.sql', NOW(), 'schema'),
    ('008_document_settled_lifecycle.sql', NOW(), 'schema'),
    ('009_document_blobs.sql', NOW(), 'schema'),
    ('010_task_queue.sql', NOW(), 'schema'),
    ('011_task_queue_heartbeat.sql', NOW(), 'schema'),
    ('012_task_queue_batched.sql', NOW(), 'schema'),
    ('013_rdf_layer.sql', NOW(), 'schema'),
    ('014_entity_roots.sql', NOW(), 'schema'),
    ('015_evidence_rows.sql', NOW(), 'schema'),
    ('016_document_produced_by.sql', NOW(), 'schema'),
    ('017_migration_ledger.sql', NOW(), 'schema'),
    ('018_derived_roots.sql', NOW(), 'schema'),
    ('019_link_derived_by.sql', NOW(), 'schema'),
    ('020_container_lookup.sql', NOW(), 'schema'),
    ('021_profile_usetype.sql', NOW(), 'schema'),
    ('022_token_embed_256_hnsw.sql', NOW(), 'schema');
-- END MIGRATION LEDGER

-- ============================================================================
-- DEFAULT DATA
-- ============================================================================

-- Create default search index
INSERT INTO search_indexes (name, description, capabilities)
VALUES ('default', 'Default search index for all documents', '{"bm25": true, "maxsim": true}'::jsonb);

-- NO named search-context preset is seeded (ROADMAP.md known gap 5), and this comment is
-- the record of why, because an absent row leaves no other trace.
--
-- THREE WERE SPECIFIED 2026-08. `docs/archive/ROADMAP_HISTORY.md:697` named
-- `agent-sessions` -> `adjutant:*`, `personal-notes` -> `transcript:*,obsidian:*` and
-- `hardware` -> `kicad:*,freecad:*`. A preset is data with no validator: nothing checks at
-- write time that its `usetype` names anything, so a preset naming a usetype this
-- appliance never writes ships to every install as a named filter that returns an empty
-- page and gives no reason.
--
-- `personal-notes` and `hardware` are UNEXPRESSIBLE: their four namespaces are written by
-- nothing in `jmfts_core`, so either would ship that empty page.
--
-- `agent-sessions` is expressible and WITHHELD, which is a different fact and is a
-- deliberate decision rather than a gap in the filter. `adjutant:*` matches two usetypes
-- (`adjutant:template`, `services/template_service.py:51`, and `adjutant:container`,
-- `:104`) — the prompt-template library and the node it hangs under. It does NOT reach an
-- adjutant session: a transcript stopped being a usetype when conversation ingestion moved
-- onto the queue (`conversation_tasks.py:9`), so a session's turns are `chunk` nodes
-- indistinguishable by usetype from a PDF's leaves. Seeding it would put a preset in every
-- install whose name says sessions and whose filter selects templates, and would carry a
-- caller-visible reference to one downstream consumer of this appliance into the schema
-- every fork of it starts from. Neither is a trade worth a convenience filter.
--
-- WHAT WOULD CLOSE THIS is a filter key `config` does not have: `produced_by`, which is
-- `structure:conversation` on exactly the turns `agent-sessions` was meant to select
-- (`models/document.py:117`). That is a change to `SearchContextRepository.resolve_params`
-- and to the search methods, not a seed, and it is what makes the preset mean its name.
--
-- The filter mechanism this gap used to be about is FIXED and is not what is missing.
-- `config.usetype` accepts either spelling of `UsetypeFilter`
-- (`jmfts-client/jmfts_client/contracts/search.py:81`) and `usetype_globs` normalises
-- both, so a multi-glob preset written as a JSON list is no longer the silent
-- `LIKE 'transcript:%,obsidian:%'` that stopped the 2026-09-05 attempt
-- (`_usetype_to_like`'s docstring, `repositories/search.py:126`).
--
-- `SearchContextRepository.resolve_params` hands a preset's blob to the search methods
-- unvalidated — it passes through no contract at all — so the seal is a test:
-- `tests/test_search_context_presets.py` asserts of every row seeded here that its globs
-- match a usetype `jmfts_core` can produce, reading the producible set out of the source
-- with `ast` rather than from a checked-in copy. That gate is armed against an empty
-- table, so it is the first thing the next seeding attempt meets.
