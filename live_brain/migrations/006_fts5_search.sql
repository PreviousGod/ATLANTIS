-- Migration 006: FTS5 virtual tables for optimized LIKE queries
--
-- NOTE (2026-05-11): Initial version declared `rowid UNINDEXED` as a user column
-- on causal_activations_fts, which is illegal in FTS5 (rowid is reserved and
-- provided automatically). That caused `sqlite3.OperationalError: reserved fts5
-- column name: rowid` on every startup and prevented LiveBrainStore from
-- initializing. This revised version:
--   1. Does NOT declare `rowid` as a user column (FTS5 supplies it implicitly).
--   2. Uses `INSERT INTO <fts>(<fts>) VALUES('rebuild')` — the FTS5 sanctioned
--      way to populate/rebuild a contentless-external table; idempotent on
--      re-run whereas `INSERT ... SELECT` would create duplicates.
--   3. All CREATE statements use IF NOT EXISTS so re-application is a no-op.

-- ---------------------------------------------------------------------------
-- fix_recipes_fts — external-content FTS5 index over fix_recipes
-- ---------------------------------------------------------------------------
CREATE VIRTUAL TABLE IF NOT EXISTS fix_recipes_fts USING fts5(
    recipe_id UNINDEXED,
    problem_pattern,
    content=fix_recipes,
    content_rowid=rowid
);

-- Rebuild from source table (idempotent; safe if already populated).
INSERT INTO fix_recipes_fts(fix_recipes_fts) VALUES('rebuild');

-- Triggers keep the FTS index in sync with future writes.
CREATE TRIGGER IF NOT EXISTS fix_recipes_fts_insert AFTER INSERT ON fix_recipes BEGIN
    INSERT INTO fix_recipes_fts(rowid, recipe_id, problem_pattern)
    VALUES (new.rowid, new.recipe_id, new.problem_pattern);
END;

CREATE TRIGGER IF NOT EXISTS fix_recipes_fts_update AFTER UPDATE ON fix_recipes BEGIN
    INSERT INTO fix_recipes_fts(fix_recipes_fts, rowid, recipe_id, problem_pattern)
    VALUES ('delete', old.rowid, old.recipe_id, old.problem_pattern);
    INSERT INTO fix_recipes_fts(rowid, recipe_id, problem_pattern)
    VALUES (new.rowid, new.recipe_id, new.problem_pattern);
END;

CREATE TRIGGER IF NOT EXISTS fix_recipes_fts_delete AFTER DELETE ON fix_recipes BEGIN
    INSERT INTO fix_recipes_fts(fix_recipes_fts, rowid, recipe_id, problem_pattern)
    VALUES ('delete', old.rowid, old.recipe_id, old.problem_pattern);
END;

-- ---------------------------------------------------------------------------
-- causal_activations_fts — external-content FTS5 index over causal_activations
-- ---------------------------------------------------------------------------
CREATE VIRTUAL TABLE IF NOT EXISTS causal_activations_fts USING fts5(
    trigger_text,
    content=causal_activations,
    content_rowid=rowid
);

INSERT INTO causal_activations_fts(causal_activations_fts) VALUES('rebuild');

CREATE TRIGGER IF NOT EXISTS causal_activations_fts_insert AFTER INSERT ON causal_activations BEGIN
    INSERT INTO causal_activations_fts(rowid, trigger_text)
    VALUES (new.rowid, new.trigger_text);
END;

CREATE TRIGGER IF NOT EXISTS causal_activations_fts_update AFTER UPDATE ON causal_activations BEGIN
    INSERT INTO causal_activations_fts(causal_activations_fts, rowid, trigger_text)
    VALUES ('delete', old.rowid, old.trigger_text);
    INSERT INTO causal_activations_fts(rowid, trigger_text)
    VALUES (new.rowid, new.trigger_text);
END;

CREATE TRIGGER IF NOT EXISTS causal_activations_fts_delete AFTER DELETE ON causal_activations BEGIN
    INSERT INTO causal_activations_fts(causal_activations_fts, rowid, trigger_text)
    VALUES ('delete', old.rowid, old.trigger_text);
END;
