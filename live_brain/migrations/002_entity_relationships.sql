-- Migration 002: Entity Relationship Graph
-- This enables Feature 2: Entity Relationship Graph with traversal and synthesis

CREATE TABLE IF NOT EXISTS entity_relationships (
    relationship_id TEXT PRIMARY KEY,
    entity_a_id TEXT NOT NULL,
    entity_b_id TEXT NOT NULL,
    relationship_type TEXT NOT NULL,
    strength REAL DEFAULT 1.0,
    first_observed_at REAL NOT NULL,
    last_observed_at REAL NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    scope_key TEXT NOT NULL,
    scope_tags_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (entity_a_id) REFERENCES entities(entity_id),
    FOREIGN KEY (entity_b_id) REFERENCES entities(entity_id)
);

CREATE INDEX IF NOT EXISTS idx_entity_relationships_a ON entity_relationships(entity_a_id);
CREATE INDEX IF NOT EXISTS idx_entity_relationships_b ON entity_relationships(entity_b_id);
CREATE INDEX IF NOT EXISTS idx_entity_relationships_type ON entity_relationships(relationship_type);
CREATE INDEX IF NOT EXISTS idx_entity_relationships_scope ON entity_relationships(scope_key);
CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_relationships_unique ON entity_relationships(entity_a_id, entity_b_id, relationship_type);
