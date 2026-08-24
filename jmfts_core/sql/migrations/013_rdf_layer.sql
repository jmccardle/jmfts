-- Migration 013: the RDF layer — literals, provenance, IRIs, and two new tables.
--
-- docs/SPRINT_0_3_0.md Part 4. Five changes to what already exists plus two tables. Three
-- of the five are additive, one is a rename, one relaxes a NOT NULL and replaces it with a
-- CHECK.
--
-- 1. LITERALS (4.1). `triples.object_id` was NOT NULL, so every object had to be a
--    document node. That is why `fact_extraction.resolve_entity("128000")` creates a
--    document whose title and content are both the string `128000`, embeds it at 768
--    dimensions, hangs it in the tree — and puts it in the retrieval index, where it is a
--    hit for anything numerically adjacent. This migration fixes that defect. Being
--    RDF-shaped afterwards is a consequence; it is not the reason.
--
-- 2. `derived_by` (4.2). The column that is expensive to add later. Validation runs
--    against raw data; inference materialises into a separate layer; the materialised
--    layer is never validated. With this column that separation is `WHERE derived_by IS
--    NULL`. Without it the two layers are the same rows and telling them apart later means
--    re-deriving everything. Nothing writes it today and nothing in this sprint will —
--    there is no reasoner (5.1) — which is exactly why it goes in now: the first rule to
--    land must not be indistinguishable from an assertion.
--
-- 3. `predicates.iri` (4.3). A predicate a published vocabulary names, versus a local one.
--
-- 4. `predicates.domain` -> `predicates.namespace` (4.4). `rdfs:domain` means "the class a
--    subject must belong to". This column has always meant "the group this predicate
--    belongs to". While nothing here spoke RDF the collision was harmless; once ontologies
--    land, the wrong reading is the one a reader will apply. Nothing has ever written the
--    column — `fact_extraction.resolve_predicate` calls `get_or_create_predicate(name=...)`
--    with no domain — so every row is NULL and the rename moves no data.
--
-- 5. `ontologies` and `shape_bindings` (4.5), following the `usetype_presentations`
--    pattern: an open string key, policy in JSONB, extended by inserting a row.
--
-- REVERSIBILITY. Everything except the rename is additive. The rename is the one statement
-- that a running 0.2.x process would notice, because `TripleRepository.list_predicates`
-- filters on the old name; deploy the code with it, not around it.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Literals on triples
-- ---------------------------------------------------------------------------

ALTER TABLE triples ALTER COLUMN object_id DROP NOT NULL;

-- The lexical form, verbatim. "1.50" and "1.5" are the same decimal and different
-- literals; a store that parses on the way in cannot round-trip the document it read.
ALTER TABLE triples ADD COLUMN IF NOT EXISTS object_literal TEXT;

-- An `xsd:` IRI. NULL means xsd:string — RDF's plain literal — not "unknown".
ALTER TABLE triples ADD COLUMN IF NOT EXISTS object_datatype VARCHAR(100);

-- Exactly one object. The datatype is refused on a resource object rather than ignored
-- there: "this document node is an xsd:integer" is not a fact anything can act on, and a
-- column that is sometimes meaningless is a column readers stop trusting.
ALTER TABLE triples DROP CONSTRAINT IF EXISTS ck_triples_object_exactly_one;
ALTER TABLE triples ADD CONSTRAINT ck_triples_object_exactly_one CHECK (
    (object_id IS NOT NULL AND object_literal IS NULL AND object_datatype IS NULL)
    OR (object_literal IS NOT NULL AND object_id IS NULL)
);

-- THE LITERAL HALF OF uq_triple. NULLs are distinct in a UNIQUE constraint, so the
-- existing UNIQUE(subject_id, predicate_id, object_id) cannot see a literal row at all and
-- "Acme founded_in 1999" could otherwise be asserted any number of times.
--
-- md5(object_literal) and not the literal: a btree entry is capped near 2704 bytes and
-- object_literal is unbounded TEXT, so indexing it directly turns a long literal into an
-- index-size error at INSERT. COALESCE on the datatype because NULL means xsd:string here
-- and NULLs do not compare equal.
--
-- The cost, stated rather than hidden: two literals sharing a subject, a predicate, a
-- datatype AND an md5 collision would be deduplicated into one fact.
CREATE UNIQUE INDEX IF NOT EXISTS uq_triple_literal
    ON triples (subject_id, predicate_id, md5(object_literal), COALESCE(object_datatype, ''))
    WHERE object_literal IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 2. Provenance
-- ---------------------------------------------------------------------------

ALTER TABLE triples ADD COLUMN IF NOT EXISTS derived_by VARCHAR(200);

-- Partial: the column is NULL for every row that exists today and for every row an
-- assertion will ever write, so the index holds only the derived minority.
CREATE INDEX IF NOT EXISTS ix_triples_derived_by
    ON triples (derived_by)
    WHERE derived_by IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 3 & 4. Predicates: an IRI, and a column that was named after the wrong thing
-- ---------------------------------------------------------------------------

ALTER TABLE predicates ADD COLUMN IF NOT EXISTS iri TEXT;

-- UNIQUE: two predicate rows claiming one IRI makes "which predicate does this vocabulary
-- term mean" ambiguous at the exact point an ontology import must answer it. NULLs do not
-- conflict, so any number of local predicates coexist.
--
-- A CONSTRAINT rather than a bare unique index, and named `predicates_iri_key` on purpose:
-- that is what `iri TEXT UNIQUE` in schema.sql produces, and what `create_all()` produces
-- from the model. A migrated database and a fresh one have to carry the same DDL, names
-- included, or the next migration cannot be written against both.
ALTER TABLE predicates DROP CONSTRAINT IF EXISTS predicates_iri_key;
ALTER TABLE predicates ADD CONSTRAINT predicates_iri_key UNIQUE (iri);

ALTER TABLE predicates RENAME COLUMN domain TO namespace;

-- ---------------------------------------------------------------------------
-- 5. Ontologies and shape bindings
-- ---------------------------------------------------------------------------

-- The vocabulary itself. `source_turtle` holds exactly what was uploaded because `shapes`
-- is a parsed digest of the SHACL subset this sprint reads (5.1), and a digest is lossy:
-- when the subset widens, the digest is rebuilt from these bytes rather than from a file
-- somebody has to find again.
CREATE TABLE IF NOT EXISTS ontologies (
    -- An open string key, as usetype_presentations.usetype is. A second upload under the
    -- same name REPLACES that vocabulary; it is not a second copy of it.
    name VARCHAR(200) PRIMARY KEY,
    -- Not derived from @base or from the first prefix: an ontology may declare neither,
    -- and every locally-minted IRI would then rest on a guess.
    base_iri TEXT NOT NULL,
    source_turtle TEXT NOT NULL,
    -- {prefix: namespace IRI}, as declared — so Turtle can be rendered back out with the
    -- names the author chose instead of rdflib's invented ns1:.
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
CREATE TABLE IF NOT EXISTS shape_bindings (
    id SERIAL PRIMARY KEY,
    ontology_name VARCHAR(200) NOT NULL REFERENCES ontologies(name) ON DELETE CASCADE,
    -- TEXT and not a foreign key: shapes live in the ontology's JSONB digest, and giving
    -- them their own table would make the digest the authority over the source Turtle.
    -- Whether the IRI names a shape the ontology declares is checked at bind time, where
    -- the answer can be reported to the caller.
    shape_iri TEXT NOT NULL,
    -- A CLOSED set, unlike usetype: each value names a different query the resolver runs,
    -- and a value with no query behind it would silently match nothing.
    --   usetype    scope = {"pattern": "profile:sheet"}
    --   subtree    scope = {"parent_id": 42}
    --   documents  scope = {"document_ids": [1, 2, 3]}
    scope_type VARCHAR(20) NOT NULL,
    scope JSONB NOT NULL DEFAULT '{}'::jsonb,
    description TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),

    CONSTRAINT ck_shape_bindings_scope_type
        CHECK (scope_type IN ('usetype', 'subtree', 'documents')),
    -- One shape bound to one scope twice is one binding asserted twice, and the second row
    -- would double every rule that iterates bindings.
    CONSTRAINT uq_shape_binding UNIQUE (ontology_name, shape_iri, scope_type, scope)
);

CREATE INDEX IF NOT EXISTS ix_shape_bindings_ontology ON shape_bindings(ontology_name);
-- "Which bindings mention this usetype / this parent / this document" is a containment
-- query against the scope object.
CREATE INDEX IF NOT EXISTS ix_shape_bindings_scope ON shape_bindings USING GIN (scope);

COMMIT;
