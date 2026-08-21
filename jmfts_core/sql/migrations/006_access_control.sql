-- Migration 006: Subtree RBAC — principals, API tokens, and access grants.
--
-- Background: the appliance's default posture is single-user with no access control —
-- "no password gate between language center and memory." This migration adds an
-- OPT-IN, subtree-scoped RBAC layer on top of that default without changing it.
--
-- Model:
--   * A grant makes its `document_id` an ACCESS-CONTROL ROOT (ACR). Being an ACR is
--     DEFINED as "has ≥1 grant" — there is no flag on `documents`, so marking a root
--     is just creating its first grant and un-marking it is deleting its last.
--   * A principal's effective right on a document is the HIGHEST level granted on any
--     ACR at-or-above it on its tree `path` (max-over-path; grants are additive — a
--     deeper ACR can only widen, never restrict, an ancestor's grant).
--   * A document under NO ACR is unprotected — the single-user default. With zero
--     grants the enforcement engine (jmfts_core/access.py) is a no-op, so the
--     default/benchmark path stays byte-identical (the canary holds by construction).
--   * The shared "owner" bearer (JMFTS_API_TOKEN / ephemeral boot token) is SYNTHETIC:
--     it bypasses all checks, is matched constant-time without a DB round-trip, and is
--     therefore NOT a `principals` row.
--
-- The read gate hides existence (unreadable documents are filtered out of every search
-- path and 404 on direct GET); the write gate covers modify / add-child / reparent.
-- It all rides the pre-existing `idx_documents_path` GIN index via `path @> [acr_id]`.
--
-- Safe to run multiple times (idempotent: IF NOT EXISTS throughout).
--
-- Run: psql $DATABASE_URL -f migrations/006_access_control.sql

BEGIN;

-- Non-owner identities that grants are issued to. The owner is synthetic (see above).
CREATE TABLE IF NOT EXISTS principals (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL UNIQUE,
    is_owner BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Bearer token → principal. Only the SHA-256 hex of the token is stored, never the
-- token itself; auth hashes the presented bearer and looks it up here.
CREATE TABLE IF NOT EXISTS api_tokens (
    id SERIAL PRIMARY KEY,
    principal_id INTEGER NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
    token_hash CHAR(64) NOT NULL UNIQUE,
    label VARCHAR(255),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_api_tokens_hash ON api_tokens(token_hash);

-- Subtree RBAC grants. UNIQUE(document_id, principal_id): one level per (ACR,
-- principal); `write` implies `read`.
CREATE TABLE IF NOT EXISTS access_grants (
    id SERIAL PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    principal_id INTEGER NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
    level VARCHAR(10) NOT NULL CHECK (level IN ('read', 'write')),
    created_at TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE (document_id, principal_id)
);
CREATE INDEX IF NOT EXISTS idx_access_grants_principal ON access_grants(principal_id);
CREATE INDEX IF NOT EXISTS idx_access_grants_document ON access_grants(document_id);

COMMIT;
