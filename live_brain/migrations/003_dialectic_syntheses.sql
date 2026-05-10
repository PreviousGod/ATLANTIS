-- Migration 003: Dialectic Reasoning Layer
-- This enables Feature 3: Cross-session synthesis and two-layer context

CREATE TABLE IF NOT EXISTS dialectic_syntheses (
    synthesis_id TEXT PRIMARY KEY,
    query_fingerprint TEXT NOT NULL,
    synthesis_text TEXT NOT NULL,
    source_sessions_json TEXT NOT NULL DEFAULT '[]',
    source_facts_json TEXT NOT NULL DEFAULT '[]',
    source_beliefs_json TEXT NOT NULL DEFAULT '[]',
    confidence REAL DEFAULT 0.8,
    scope_key TEXT NOT NULL,
    scope_tags_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    last_accessed_at REAL NOT NULL,
    access_count INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_dialectic_query ON dialectic_syntheses(query_fingerprint);
CREATE INDEX IF NOT EXISTS idx_dialectic_scope ON dialectic_syntheses(scope_key);
CREATE INDEX IF NOT EXISTS idx_dialectic_accessed ON dialectic_syntheses(last_accessed_at DESC);
