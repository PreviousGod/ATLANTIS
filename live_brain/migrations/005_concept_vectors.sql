-- Migration 005: Compositional Queries
-- This enables Feature 6: Algebraic composition (A + B - C)

CREATE TABLE IF NOT EXISTS concept_vectors (
    concept_id TEXT PRIMARY KEY,
    concept_name TEXT NOT NULL UNIQUE,
    vector_json TEXT NOT NULL,
    dimension INTEGER NOT NULL DEFAULT 128,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_concept_name ON concept_vectors(concept_name);
