-- Migration 004: User Alignment Tracking
-- This enables Feature 5: User preferences, communication patterns, and feedback

CREATE TABLE IF NOT EXISTS user_profiles (
    profile_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    preference_key TEXT NOT NULL,
    preference_value TEXT NOT NULL,
    preference_type TEXT NOT NULL,
    confidence REAL DEFAULT 1.0,
    source_turn_id INTEGER,
    scope_key TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(user_id, preference_key, scope_key)
);

CREATE TABLE IF NOT EXISTS communication_patterns (
    pattern_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    pattern_type TEXT NOT NULL,
    pattern_description TEXT NOT NULL,
    examples_json TEXT NOT NULL DEFAULT '[]',
    frequency REAL DEFAULT 0.0,
    scope_key TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS user_feedback (
    feedback_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    turn_id INTEGER NOT NULL,
    feedback_type TEXT NOT NULL,
    feedback_content TEXT,
    sentiment REAL,
    scope_key TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_user_profiles_user ON user_profiles(user_id);
CREATE INDEX IF NOT EXISTS idx_communication_patterns_user ON communication_patterns(user_id);
CREATE INDEX IF NOT EXISTS idx_user_feedback_user ON user_feedback(user_id);
