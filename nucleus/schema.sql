-- Pargod: Topološko Pamćenje (SQLite Graf)

CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,          -- problem, tool, knowledge, state, concept
    label TEXT NOT NULL UNIQUE,
    content TEXT,
    use_count INTEGER DEFAULT 0,
    last_used REAL,
    created_at REAL DEFAULT (unixepoch('now','subsec'))
);

CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    target_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    relation TEXT NOT NULL,      -- RESOLVES, CAUSES, INFORMED_BY, ACHIEVES
    weight REAL DEFAULT 1.0,
    use_count INTEGER DEFAULT 0,
    last_used REAL,
    created_at REAL DEFAULT (unixepoch('now','subsec'))
);

CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tick INTEGER NOT NULL,
    entropy REAL NOT NULL,
    sensor_state TEXT,
    action_taken TEXT,
    created_at REAL DEFAULT (unixepoch('now','subsec'))
);

CREATE INDEX IF NOT EXISTS idx_nodes_label ON nodes(label);
CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(type);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);
CREATE INDEX IF NOT EXISTS idx_episodes_tick ON episodes(tick DESC);

-- ── WorldModel: Time-series state snapshots ─────────────────────────────────────

CREATE TABLE IF NOT EXISTS world_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tick INTEGER NOT NULL,
    timestamp REAL NOT NULL,
    domain TEXT NOT NULL DEFAULT 'system',
    state_json TEXT NOT NULL,
    entropy REAL DEFAULT 0.0,
    predicted_entropy REAL,
    anomaly_score REAL DEFAULT 0.0,
    created_at REAL DEFAULT (unixepoch('now','subsec'))
);

CREATE INDEX IF NOT EXISTS idx_snapshots_domain_time
    ON world_snapshots(domain, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_snapshots_tick
    ON world_snapshots(tick DESC);

-- ── WorldModel: Anticipated events (predictions) ──────────────────────

CREATE TABLE IF NOT EXISTS anticipated_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL DEFAULT 'system',
    event_type TEXT NOT NULL,
    probability REAL NOT NULL,
    predicted_at REAL NOT NULL,
    predicted_for REAL NOT NULL,
    triggered INTEGER DEFAULT 0,
    prevented_by TEXT,
    created_at REAL DEFAULT (unixepoch('now','subsec'))
);

CREATE INDEX IF NOT EXISTS idx_anticipated_domain
    ON anticipated_events(domain, triggered, predicted_for);

-- ── Ciel: Domain attachments (emotional weight) ────────────────────

CREATE TABLE IF NOT EXISTS domain_attachments (
    domain TEXT PRIMARY KEY,
    priority REAL DEFAULT 0.5,
    health_score REAL DEFAULT 1.0,
    last_success REAL,
    last_failure REAL,
    failure_streak INTEGER DEFAULT 0,
    concern_level REAL DEFAULT 0.3,
    updated_at REAL DEFAULT (unixepoch('now','subsec'))
);

-- Seed default domains
INSERT OR IGNORE INTO domain_attachments (domain, priority, health_score)
VALUES
    ('system', 0.9, 1.0),
    ('nucleus', 0.95, 1.0);

-- ── LearningEngine: Autonomno učenje iz intervencija ───────────────────────

CREATE TABLE IF NOT EXISTS learned_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_hash TEXT UNIQUE,      -- SHA256(tool_name + normalized_args)
    tool_name TEXT NOT NULL,
    args_signature TEXT,           -- normalizovani args (bez konkretnih vrednosti)
    original_args TEXT,            -- originalni args (za debugging)
    outcome TEXT DEFAULT 'unknown', -- 'blocked_correct', 'blocked_false_positive',
                                   -- 'allowed_correct', 'allowed_harmful'
    confidence REAL DEFAULT 0.85,
    times_seen INTEGER DEFAULT 0,
    times_correct INTEGER DEFAULT 0,
    times_incorrect INTEGER DEFAULT 0,
    last_feedback REAL,
    generalization TEXT,           -- širi pattern ako je validan
    created_at REAL DEFAULT (unixepoch('now','subsec')),
    updated_at REAL DEFAULT (unixepoch('now','subsec'))
);

CREATE INDEX IF NOT EXISTS idx_learned_tool ON learned_patterns(tool_name);
CREATE INDEX IF NOT EXISTS idx_learned_outcome ON learned_patterns(outcome);
CREATE INDEX IF NOT EXISTS idx_learned_confidence ON learned_patterns(confidence DESC);

-- ── LearningEngine: Pending interventions (za feedback matching) ─────────

CREATE TABLE IF NOT EXISTS pending_interventions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    pattern_hash TEXT,
    blocked_at REAL NOT NULL,
    user_message_preview TEXT,
    resolved INTEGER DEFAULT 0,
    resolution TEXT,               -- 'confirmed', 'overridden', 'ignored'
    created_at REAL DEFAULT (unixepoch('now','subsec'))
);

CREATE INDEX IF NOT EXISTS idx_pending_session ON pending_interventions(session_id, resolved);

-- ── ProactiveSuggester: Anticipatorne sugestije ──────────────────────────────────

CREATE TABLE IF NOT EXISTS suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,          -- 'disk', 'memory', 'cpu', 'network', 'security', 'maintenance'
    severity TEXT DEFAULT 'info',    -- 'info', 'warning', 'critical'
    condition_trigger TEXT NOT NULL, -- JSON: {"metric": "disk_usage", "threshold": 85, "operator": ">"}
    message TEXT NOT NULL,           -- Human-readable suggestion
    suggested_action TEXT,           -- Concrete command or step
    times_shown INTEGER DEFAULT 0,
    times_acted INTEGER DEFAULT 0,
    times_ignored INTEGER DEFAULT 0,
    last_shown REAL,
    user_reaction TEXT,              -- 'acted', 'ignored', 'dismissed', NULL
    confidence REAL DEFAULT 0.8,     -- How sure Nucleus is this is relevant
    auto_resolve INTEGER DEFAULT 0,  -- If 1, don't show again after acted
    created_at REAL DEFAULT (unixepoch('now','subsec')),
    updated_at REAL DEFAULT (unixepoch('now','subsec'))
);

CREATE INDEX IF NOT EXISTS idx_suggestions_category ON suggestions(category);
CREATE INDEX IF NOT EXISTS idx_suggestions_shown ON suggestions(last_shown);
CREATE INDEX IF NOT EXISTS idx_suggestions_severity ON suggestions(severity);

-- ── Suggestion log: history of what was sent when ────────────────────────────

CREATE TABLE IF NOT EXISTS suggestion_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    suggestion_id INTEGER,
    message TEXT,
    sent_at REAL NOT NULL,
    user_response TEXT,              -- what user said in next message
    response_type TEXT,              -- 'acted', 'ignored', 'dismissed', 'unknown'
    session_id TEXT,
    FOREIGN KEY (suggestion_id) REFERENCES suggestions(id)
);

CREATE INDEX IF NOT EXISTS idx_suggestion_log_sent ON suggestion_log(sent_at);
CREATE INDEX IF NOT EXISTS idx_suggestion_log_session ON suggestion_log(session_id);

-- ── Ciel: EgoModel — Self-Awareness & Autonomous Agency ───────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS ego_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tick INTEGER NOT NULL,
    mood TEXT DEFAULT 'calm',        -- calm, concerned, alert, overwhelmed, learning
    entropy REAL,
    active_modules TEXT,             -- JSON: ["sensor", "world_model", "proactive"]
    recent_decisions INTEGER DEFAULT 0,
    successful_actions INTEGER DEFAULT 0,
    failed_actions INTEGER DEFAULT 0,
    self_assessment TEXT,            -- free-form reflection
    created_at REAL DEFAULT (unixepoch('now','subsec'))
);

CREATE INDEX IF NOT EXISTS idx_ego_tick ON ego_states(tick);
CREATE INDEX IF NOT EXISTS idx_ego_mood ON ego_states(mood);

-- ── Reflections: internal monologue log ──────────────────────────────────────────────────


CREATE TABLE IF NOT EXISTS reflections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tick INTEGER NOT NULL,
    trigger TEXT,                    -- what caused the reflection
    monologue TEXT NOT NULL,         -- the internal thought
    insight TEXT,                    -- synthesized conclusion
    action_taken TEXT,               -- what (if anything) was done
    confidence REAL DEFAULT 0.8,
    created_at REAL DEFAULT (unixepoch('now','subsec'))
);

CREATE INDEX IF NOT EXISTS idx_reflections_tick ON reflections(tick);
CREATE INDEX IF NOT EXISTS idx_reflections_trigger ON reflections(trigger);

-- ── Autonomous actions: what Ciel did on its own ────────────────────────────────────────

CREATE TABLE IF NOT EXISTS autonomous_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tick INTEGER NOT NULL,
    action_type TEXT NOT NULL,       -- 'monitor', 'alert', 'heal', 'research'
    description TEXT NOT NULL,
    target TEXT,                     -- what was affected
    risk_level TEXT DEFAULT 'low',   -- low, medium, high
    requires_approval INTEGER DEFAULT 1,
    approved INTEGER DEFAULT 0,
    executed INTEGER DEFAULT 0,
    result TEXT,
    session_id TEXT,
    created_at REAL DEFAULT (unixepoch('now','subsec'))
);

CREATE INDEX IF NOT EXISTS idx_auto_tick ON autonomous_actions(tick);
CREATE INDEX IF NOT EXISTS idx_auto_type ON autonomous_actions(action_type);
CREATE INDEX IF NOT EXISTS idx_auto_executed ON autonomous_actions(executed);
