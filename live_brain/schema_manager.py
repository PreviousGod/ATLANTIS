from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

from .reality import SCHEMA_SQL
from .audit import ensure_schema as ensure_audit_schema

logger = logging.getLogger(__name__)


class SchemaManager:
    def __init__(self, conn):
        self.conn = conn

    def _table_columns(self, table: str) -> set[str]:
        try:
            return {str(row['name']) for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
        except Exception as exc:
            logger.debug("[live_brain] could not inspect table %s: %s", table, exc)
            return set()

    def _add_column_if_missing(self, table: str, name: str, definition: str) -> bool:
        columns = self._table_columns(table)
        if name in columns:
            return False
        try:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            logger.info("[live_brain] schema migration added column %s.%s", table, name)
            return True
        except sqlite3.OperationalError as exc:
            if 'duplicate column' in str(exc).lower():
                logger.debug("[live_brain] schema column already exists %s.%s", table, name)
                return False
            logger.warning("[live_brain] schema migration failed for %s.%s: %s", table, name, exc)
            raise

    def _run_migrations(self) -> None:
        """Run all pending migrations in order.

        On failure, logs ERROR, records a row ``FAILED:<migration_id>`` in
        ``schema_migrations`` so the same migration is not retried forever on
        every startup, and continues with the next migration. This prevents a
        single broken migration (especially an optional one like an FTS index)
        from taking down the whole memory provider on init.
        """
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                migration_id TEXT PRIMARY KEY,
                summary TEXT NOT NULL DEFAULT '',
                applied_at REAL NOT NULL
            )
        """)
        # Back-fill summary column for older DBs that had the 2-column layout.
        try:
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(schema_migrations)").fetchall()}
            if 'summary' not in cols:
                self.conn.execute("ALTER TABLE schema_migrations ADD COLUMN summary TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass

        migrations_dir = Path(__file__).parent / "migrations"
        if not migrations_dir.exists():
            return

        applied = {
            row[0] for row in
            self.conn.execute("SELECT migration_id FROM schema_migrations").fetchall()
        }

        for migration_file in sorted(migrations_dir.glob("*.sql")):
            migration_id = migration_file.stem
            failed_marker = f"FAILED:{migration_id}"
            if migration_id in applied:
                continue
            if failed_marker in applied:
                # Previously failed; skip to avoid restart-loop errors. Operator
                # must manually clear the FAILED marker to retry after a fix.
                logger.warning(
                    "[live_brain] skipping migration %s (previously failed; "
                    "remove '%s' from schema_migrations to retry after fix)",
                    migration_id, failed_marker,
                )
                continue

            logger.info("[live_brain] applying migration %s", migration_id)
            try:
                sql = migration_file.read_text(encoding='utf-8')
                self.conn.executescript(sql)
                self.conn.execute(
                    "INSERT INTO schema_migrations (migration_id, applied_at) VALUES (?, ?)",
                    (migration_id, time.time())
                )
                self.conn.commit()
            except Exception as exc:
                logger.error(
                    "[live_brain] migration %s failed: %s (marking as FAILED and continuing)",
                    migration_id, exc,
                )
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                # Record FAILED sentinel so we don't retry on every startup.
                try:
                    self.conn.execute(
                        "INSERT OR REPLACE INTO schema_migrations (migration_id, applied_at) VALUES (?, ?)",
                        (failed_marker, time.time()),
                    )
                    self.conn.commit()
                except Exception:
                    logger.exception(
                        "[live_brain] could not record FAILED marker for %s",
                        migration_id,
                    )
                # Do NOT re-raise — let provider continue to initialize.
                continue

    def run_migrations(self) -> None:
        """Public entrypoint (LiveBrainStore calls this)."""
        self._run_migrations()

    def initialize_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                parent_session_id TEXT,
                platform TEXT,
                agent_identity TEXT,
                agent_context TEXT,
                user_id TEXT,
                gateway_session_key TEXT,
                started_at REAL,
                ended_at REAL
            );

            CREATE TABLE IF NOT EXISTS turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                turn_index INTEGER NOT NULL,
                user_text TEXT NOT NULL,
                assistant_text TEXT NOT NULL,
                created_at REAL NOT NULL,
                ingest_status TEXT NOT NULL DEFAULT 'raw',
                hash TEXT UNIQUE
            );
            CREATE INDEX IF NOT EXISTS idx_turns_session_turn ON turns(session_id, turn_index);
            CREATE INDEX IF NOT EXISTS idx_turns_created ON turns(created_at DESC);

            CREATE TABLE IF NOT EXISTS episodes (
                episode_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL DEFAULT 'general',
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                opened_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                closed_at REAL,
                current_summary TEXT NOT NULL DEFAULT '',
                priority_score REAL NOT NULL DEFAULT 0,
                recency_score REAL NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_episodes_updated ON episodes(updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_episodes_status ON episodes(status);

            CREATE TABLE IF NOT EXISTS episode_turns (
                episode_id TEXT NOT NULL,
                turn_id INTEGER NOT NULL,
                role_in_episode TEXT NOT NULL,
                PRIMARY KEY (episode_id, turn_id)
            );

            CREATE TABLE IF NOT EXISTS entities (
                entity_id TEXT PRIMARY KEY,
                entity_type TEXT NOT NULL,
                canonical_name TEXT NOT NULL,
                display_name TEXT NOT NULL,
                attributes_json TEXT NOT NULL DEFAULT '{}',
                last_seen_at REAL NOT NULL,
                salience_score REAL NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type);
            CREATE INDEX IF NOT EXISTS idx_entities_last_seen ON entities(last_seen_at DESC);

            CREATE TABLE IF NOT EXISTS entity_mentions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_id TEXT NOT NULL,
                turn_id INTEGER,
                episode_id TEXT,
                mention_text TEXT NOT NULL,
                mention_role TEXT NOT NULL,
                weight REAL NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_entity_mentions_entity ON entity_mentions(entity_id);
            CREATE INDEX IF NOT EXISTS idx_entity_mentions_episode ON entity_mentions(episode_id);

            CREATE TABLE IF NOT EXISTS facts (
                fact_id TEXT PRIMARY KEY,
                subject_entity_id TEXT,
                fact_type TEXT NOT NULL,
                fact_text TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.5,
                source_kind TEXT NOT NULL,
                valid_from REAL NOT NULL,
                valid_to REAL,
                status TEXT NOT NULL DEFAULT 'active',
                evidence_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_facts_type ON facts(fact_type);
            CREATE INDEX IF NOT EXISTS idx_facts_status ON facts(status);

            CREATE TABLE IF NOT EXISTS beliefs (
                belief_id TEXT PRIMARY KEY,
                episode_id TEXT,
                claim_text TEXT NOT NULL,
                belief_kind TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.5,
                status TEXT NOT NULL DEFAULT 'open',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                validated_by TEXT,
                supersedes_belief_id TEXT,
                caused_by_work_item_id TEXT,
                tool_name TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_beliefs_episode ON beliefs(episode_id);
            CREATE INDEX IF NOT EXISTS idx_beliefs_status ON beliefs(status);
            CREATE INDEX IF NOT EXISTS idx_beliefs_kind ON beliefs(belief_kind);

            CREATE TABLE IF NOT EXISTS episode_files (
                episode_id TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                relationship TEXT NOT NULL,
                weight REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (episode_id, entity_id, relationship)
            );

            CREATE TABLE IF NOT EXISTS briefings (
                briefing_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                query_fingerprint TEXT NOT NULL,
                packet_type TEXT NOT NULL,
                content TEXT NOT NULL,
                token_estimate INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                used INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS work_state (
                scope_key TEXT PRIMARY KEY,
                scope_type TEXT NOT NULL,
                state_json TEXT NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS canonical_recaps (
                recap_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                scope_key TEXT NOT NULL,
                task TEXT NOT NULL,
                objective TEXT NOT NULL DEFAULT '',
                main_problem TEXT NOT NULL DEFAULT '',
                root_cause TEXT NOT NULL DEFAULT '',
                ruled_out_causes TEXT NOT NULL DEFAULT '',
                what_changed TEXT NOT NULL DEFAULT '',
                current_status TEXT NOT NULL DEFAULT '',
                next_step TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 0.5,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_recaps_scope ON canonical_recaps(scope_key, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_recaps_session ON canonical_recaps(session_id);
            CREATE INDEX IF NOT EXISTS idx_recaps_updated ON canonical_recaps(updated_at DESC);

            CREATE TABLE IF NOT EXISTS rules (
                rule_id TEXT PRIMARY KEY,
                scope TEXT NOT NULL,
                category TEXT NOT NULL,
                scope_tags_json TEXT NOT NULL DEFAULT '{}',
                condition_json TEXT NOT NULL,
                action_json TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.5,
                source TEXT NOT NULL DEFAULT 'derived',
                times_confirmed INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                expires_at REAL,
                specificity INTEGER NOT NULL DEFAULT 0,
                last_matched_at REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_rules_scope ON rules(scope, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_rules_category ON rules(category, updated_at DESC);

            CREATE TABLE IF NOT EXISTS work_items (
                work_item_id TEXT PRIMARY KEY,
                scope_key TEXT NOT NULL,
                session_id TEXT NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                priority REAL NOT NULL DEFAULT 0.5,
                evidence_json TEXT NOT NULL DEFAULT '{}',
                next_step TEXT NOT NULL DEFAULT '',
                root_cause TEXT NOT NULL DEFAULT '',
                supersedes_work_item_id TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                resolved_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_work_items_scope ON work_items(scope_key, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_work_items_status ON work_items(status, updated_at DESC);

            CREATE TABLE IF NOT EXISTS episode_clusters (
                cluster_id TEXT PRIMARY KEY,
                scope_key TEXT NOT NULL,
                project_name TEXT NOT NULL,
                member_work_item_ids_json TEXT NOT NULL DEFAULT '[]',
                last_active_at REAL NOT NULL,
                summary TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_clusters_scope ON episode_clusters(scope_key, last_active_at DESC);

            CREATE TABLE IF NOT EXISTS crystallised_knowledge (
                id TEXT PRIMARY KEY,
                scope_key TEXT NOT NULL,
                principle_text TEXT NOT NULL,
                source_work_item_id TEXT,
                confidence REAL NOT NULL DEFAULT 0.8,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_knowledge_scope ON crystallised_knowledge(scope_key, created_at DESC);

            CREATE TABLE IF NOT EXISTS fix_recipes (
                recipe_id TEXT PRIMARY KEY,
                scope_key TEXT NOT NULL,
                problem_pattern TEXT NOT NULL,
                tool_name TEXT NOT NULL DEFAULT '',
                steps_json TEXT NOT NULL DEFAULT '[]',
                args_template_json TEXT NOT NULL DEFAULT '{}',
                success_criteria TEXT NOT NULL DEFAULT '',
                artifact_verified INTEGER NOT NULL DEFAULT 0,
                artifact_path TEXT NOT NULL DEFAULT '',
                error_type TEXT NOT NULL DEFAULT '',
                promotion_status TEXT NOT NULL DEFAULT 'candidate',
                candidate_since REAL,
                promoted_at REAL,
                last_reviewed_at REAL,
                confidence REAL NOT NULL DEFAULT 0.7,
                times_confirmed INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'candidate',
                source TEXT NOT NULL DEFAULT 'causal_activation',
                scope_tags_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_fix_recipes_scope ON fix_recipes(scope_key, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_fix_recipes_tool ON fix_recipes(tool_name, confidence DESC);

            CREATE TABLE IF NOT EXISTS causal_activations (
                activation_id TEXT PRIMARY KEY,
                scope_key TEXT NOT NULL,
                trigger_text TEXT NOT NULL,
                trigger_pattern TEXT NOT NULL DEFAULT '',
                action_taken TEXT NOT NULL,
                tool_used TEXT NOT NULL DEFAULT '',
                args_template_json TEXT NOT NULL DEFAULT '{}',
                outcome TEXT NOT NULL DEFAULT '',
                test_result TEXT NOT NULL DEFAULT '',
                artifact_verified INTEGER NOT NULL DEFAULT 0,
                artifact_path TEXT NOT NULL DEFAULT '',
                error_type TEXT NOT NULL DEFAULT '',
                success INTEGER NOT NULL DEFAULT 0,
                confidence REAL NOT NULL DEFAULT 0.7,
                times_confirmed INTEGER NOT NULL DEFAULT 1,
                scope_tags_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_activations_scope ON causal_activations(scope_key, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_activations_trigger ON causal_activations(trigger_text, success DESC);

            CREATE TABLE IF NOT EXISTS tool_results (
                result_id TEXT PRIMARY KEY,
                tool_name TEXT NOT NULL,
                success INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                error_type TEXT NOT NULL DEFAULT '',
                artifact_verified INTEGER NOT NULL DEFAULT 0,
                artifact_path TEXT NOT NULL DEFAULT '',
                duration_ms INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tool_results_tool ON tool_results(tool_name, created_at DESC);

            CREATE TABLE IF NOT EXISTS verified_artifacts (
                artifact_id TEXT PRIMARY KEY,
                project_key TEXT NOT NULL,
                role TEXT NOT NULL,
                path TEXT NOT NULL,
                label TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'verified',
                confidence REAL NOT NULL DEFAULT 1.0,
                source TEXT NOT NULL DEFAULT 'manual',
                mime_type TEXT NOT NULL DEFAULT '',
                size_bytes INTEGER NOT NULL DEFAULT 0,
                duration_seconds REAL,
                checksum TEXT NOT NULL DEFAULT '',
                supersedes_artifact_id TEXT NOT NULL DEFAULT '',
                evidence_json TEXT NOT NULL DEFAULT '{}',
                scope_tags_json TEXT NOT NULL DEFAULT '{}',
                verified_at REAL NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_verified_artifacts_lookup ON verified_artifacts(project_key, role, status, confidence DESC, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_verified_artifacts_path ON verified_artifacts(path);

            CREATE TABLE IF NOT EXISTS context_impressions (
                impression_id TEXT PRIMARY KEY,
                scope_key TEXT NOT NULL,
                session_id TEXT NOT NULL DEFAULT '',
                query_text TEXT NOT NULL DEFAULT '',
                context_hash TEXT NOT NULL DEFAULT '',
                sections_json TEXT NOT NULL DEFAULT '[]',
                recipe_ids_json TEXT NOT NULL DEFAULT '[]',
                outcome TEXT NOT NULL DEFAULT 'pending',
                attribution_mode TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'compiler',
                feedback_text TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_context_impressions_scope ON context_impressions(scope_key, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_context_impressions_outcome ON context_impressions(outcome, updated_at DESC);

            CREATE TABLE IF NOT EXISTS recipe_rejections (
                rejection_id TEXT PRIMARY KEY,
                scope_key TEXT NOT NULL,
                trigger_pattern TEXT NOT NULL DEFAULT '',
                tool_name TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                artifact_verified INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'candidate_gate',
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_recipe_rejections_scope ON recipe_rejections(scope_key, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_recipe_rejections_reason ON recipe_rejections(reason, created_at DESC);

            CREATE TABLE IF NOT EXISTS audit_log (
                audit_id TEXT PRIMARY KEY,
                object_type TEXT NOT NULL,
                object_id TEXT NOT NULL,
                action TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_audit_object ON audit_log(object_type, object_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS self_evolution_proposals (
                proposal_id TEXT PRIMARY KEY,
                scope_key TEXT NOT NULL,
                session_id TEXT NOT NULL DEFAULT '',
                trigger_text TEXT NOT NULL DEFAULT '',
                proposal_type TEXT NOT NULL,
                target_area TEXT NOT NULL,
                rationale TEXT NOT NULL DEFAULT '',
                proposed_action TEXT NOT NULL DEFAULT '',
                evidence_json TEXT NOT NULL DEFAULT '{}',
                suggested_tests_json TEXT NOT NULL DEFAULT '[]',
                risk_level TEXT NOT NULL DEFAULT 'medium',
                risk_score REAL NOT NULL DEFAULT 0.5,
                status TEXT NOT NULL DEFAULT 'needs_approval',
                auto_apply_allowed INTEGER NOT NULL DEFAULT 0,
                requires_approval INTEGER NOT NULL DEFAULT 1,
                apply_result_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                decided_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_self_evolution_status ON self_evolution_proposals(status, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_self_evolution_scope ON self_evolution_proposals(scope_key, updated_at DESC);

            CREATE TABLE IF NOT EXISTS working_set (
                scope_key TEXT NOT NULL,
                work_item_id TEXT NOT NULL,
                added_at REAL NOT NULL,
                slot INTEGER NOT NULL,
                PRIMARY KEY (scope_key, work_item_id)
            );
            CREATE INDEX IF NOT EXISTS idx_working_set_scope ON working_set(scope_key, slot);
            """
        )
        self.conn.executescript(SCHEMA_SQL)
        ensure_audit_schema(self.conn)
        # Safe additive migrations for schema evolution. Keep this explicit and
        # inspect columns before ALTER so duplicate-column noise does not hide
        # real migration failures.
        for col, dtype, default in [
            ('objective', 'TEXT', "''"),
            ('ruled_out_causes', 'TEXT', "''"),
        ]:
            self._add_column_if_missing('canonical_recaps', col, f"{dtype} NOT NULL DEFAULT {default}")
        for col, dtype, default in [
            ('session_id', 'TEXT', "''"),
            ('scope_key', 'TEXT', "''"),
        ]:
            self._add_column_if_missing('facts', col, f"{dtype} NOT NULL DEFAULT {default}")
            self._add_column_if_missing('beliefs', col, f"{dtype} NOT NULL DEFAULT {default}")
        self._add_column_if_missing('beliefs', 'caused_by_work_item_id', 'TEXT')
        self._add_column_if_missing('beliefs', 'tool_name', 'TEXT')
        self.conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_facts_scope_valid_from ON facts(scope_key, valid_from DESC);
            CREATE INDEX IF NOT EXISTS idx_beliefs_scope_updated ON beliefs(scope_key, updated_at DESC);
            """
        )
        for col, dtype, default in [
            ('scope_tags_json', 'TEXT', "'{}'"),
            ('specificity', 'INTEGER', '0'),
        ]:
            self._add_column_if_missing('rules', col, f"{dtype} NOT NULL DEFAULT {default}")
        for col, dtype in [
            ('expires_at', 'REAL'),
            ('last_matched_at', 'REAL'),
        ]:
            self._add_column_if_missing('rules', col, dtype)
        for table in ('facts', 'beliefs', 'work_items', 'episodes'):
            self._add_column_if_missing(table, 'scope_tags_json', "TEXT NOT NULL DEFAULT '{}'")
        for col, dtype, default in [
            ('trigger_pattern', 'TEXT', "''"),
            ('args_template_json', 'TEXT', "'{}'"),
            ('test_result', 'TEXT', "''"),
            ('scope_tags_json', 'TEXT', "'{}'"),
            ('artifact_verified', 'INTEGER', '0'),
            ('artifact_path', 'TEXT', "''"),
            ('error_type', 'TEXT', "''"),
        ]:
            self._add_column_if_missing('causal_activations', col, f"{dtype} NOT NULL DEFAULT {default}")
        for col, dtype, default in [
            ('artifact_verified', 'INTEGER', '0'),
            ('artifact_path', 'TEXT', "''"),
            ('error_type', 'TEXT', "''"),
            ('promotion_status', 'TEXT', "'candidate'"),
        ]:
            self._add_column_if_missing('fix_recipes', col, f"{dtype} NOT NULL DEFAULT {default}")
        for col in ['candidate_since', 'promoted_at', 'last_reviewed_at']:
            self._add_column_if_missing('fix_recipes', col, 'REAL')
        for col, dtype, default in [
            ('error_type', 'TEXT', "''"),
            ('artifact_verified', 'INTEGER', '0'),
            ('artifact_path', 'TEXT', "''"),
            ('duration_ms', 'INTEGER', '0'),
        ]:
            self._add_column_if_missing('tool_results', col, f"{dtype} NOT NULL DEFAULT {default}")
        for col, dtype, default in [
            ('attribution_mode', 'TEXT', "''"),
            ('source', 'TEXT', "'compiler'"),
        ]:
            self._add_column_if_missing('context_impressions', col, f"{dtype} NOT NULL DEFAULT {default}")

        # Run pending migrations
        self._run_migrations()

        self.conn.commit()

