from __future__ import annotations

import json
import logging
import re
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, List
from .scopes_config import TOOL_SIGNAL_TERMS
from .utils import is_noisy_episode_memory, stable_id
from .reality import RealityEngine, SCHEMA_SQL
from .epistemic import EpistemicManager
from .audit import ensure_schema as ensure_audit_schema, record_evidence_packet, record_memory_event, record_revision, row_to_dict


logger = logging.getLogger(__name__)


class LockedConnection:
    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock):
        self._conn = conn
        self._lock = lock

    def _with_retry(self, fn, *args, **kwargs):
        delay = 0.05
        last_error = None
        for _ in range(6):
            try:
                return fn(*args, **kwargs)
            except sqlite3.OperationalError as exc:
                if 'locked' not in str(exc).lower() and 'busy' not in str(exc).lower():
                    raise
                last_error = exc
                time.sleep(delay)
                delay = min(delay * 2, 1.0)
        raise last_error

    def execute(self, *args, **kwargs):
        with self._lock:
            return self._with_retry(self._conn.execute, *args, **kwargs)

    def executemany(self, *args, **kwargs):
        with self._lock:
            return self._with_retry(self._conn.executemany, *args, **kwargs)

    def executescript(self, *args, **kwargs):
        with self._lock:
            return self._with_retry(self._conn.executescript, *args, **kwargs)

    def commit(self) -> None:
        with self._lock:
            self._with_retry(self._conn.commit)

    def rollback(self) -> None:
        with self._lock:
            self._conn.rollback()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


class LiveBrainStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        raw_conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30.0)
        raw_conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.conn = LockedConnection(raw_conn, self._lock)
        self._configure_sqlite()

    def _configure_sqlite(self) -> None:
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA temp_store=MEMORY")

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
        """Run all pending migrations in order."""
        # Create migrations tracking table
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                migration_id TEXT PRIMARY KEY,
                applied_at REAL NOT NULL
            )
        """)

        migrations_dir = Path(__file__).parent / "migrations"
        if not migrations_dir.exists():
            return

        # Get already applied migrations
        applied = {
            row[0] for row in
            self.conn.execute("SELECT migration_id FROM schema_migrations").fetchall()
        }

        # Apply pending migrations in order
        for migration_file in sorted(migrations_dir.glob("*.sql")):
            migration_id = migration_file.stem
            if migration_id in applied:
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
                logger.error("[live_brain] migration %s failed: %s", migration_id, exc)
                self.conn.rollback()
                raise

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

    def record_memory_event(self, **kwargs: Any) -> str:
        return record_memory_event(self.conn, **kwargs)

    def record_revision(self, **kwargs: Any) -> str:
        return record_revision(self.conn, **kwargs)

    def record_evidence_packet(self, **kwargs: Any) -> str:
        return record_evidence_packet(self.conn, **kwargs)

    def _stale_self_evolution_proposal_rows(
        self,
        *,
        now: float,
        stale_hours: float = 24.0,
        e2e_seed_hours: float = 1.0,
        limit: int = 200,
    ) -> list[sqlite3.Row]:
        stale_cutoff = now - max(0.1, float(stale_hours or 24.0)) * 3600
        e2e_cutoff = now - max(0.0, float(e2e_seed_hours if e2e_seed_hours is not None else 1.0)) * 3600
        return self.conn.execute(
            """
            SELECT * FROM self_evolution_proposals
            WHERE status='needs_approval'
              AND (
                updated_at < ?
                OR created_at < ?
                OR (
                  updated_at < ?
                  AND (
                    lower(trigger_text) LIKE '%ack-seed%'
                    OR lower(trigger_text) LIKE '%e2e seed%'
                    OR lower(trigger_text) LIKE '%capability e2e%'
                    OR lower(rationale) LIKE '%e2e%'
                    OR lower(evidence_json) LIKE '%e2e%'
                    OR lower(evidence_json) LIKE '%capability_e2e%'
                  )
                )
                OR (
                  (
                    lower(trigger_text) LIKE '%review%'
                    OR lower(trigger_text) LIKE '%pregled%'
                    OR lower(trigger_text) LIKE '%verdikt%'
                    OR lower(trigger_text) LIKE '%šta fali%'
                    OR lower(trigger_text) LIKE '%sta fali%'
                    OR lower(trigger_text) LIKE '%hard blocker%'
                    OR lower(trigger_text) LIKE '%nice-to-have%'
                  )
                  AND lower(trigger_text) NOT LIKE '%implement%'
                  AND lower(trigger_text) NOT LIKE '%patch%'
                  AND (lower(trigger_text) NOT LIKE '%fix%' OR lower(trigger_text) LIKE '%must_fix_next%')
                  AND lower(trigger_text) NOT LIKE '%sredi%'
                  AND lower(trigger_text) NOT LIKE '%poprav%'
                )
              )
            ORDER BY updated_at ASC
            LIMIT ?
            """,
            (stale_cutoff, stale_cutoff, e2e_cutoff, max(1, min(int(limit or 200), 1000))),
        ).fetchall()

    def _self_evolution_expiry_reason(self, row: sqlite3.Row, *, now: float, stale_hours: float, e2e_seed_hours: float) -> str:
        trigger_text = str(row['trigger_text'] or '').lower()
        row_text = ' '.join(str(row[key] or '') for key in ('trigger_text', 'rationale', 'evidence_json')).lower()
        trigger_for_change_terms = trigger_text.replace('must_fix_next', '')
        review_only = any(token in trigger_text for token in ('review', 'pregled', 'verdikt', 'šta fali', 'sta fali', 'hard blocker', 'nice-to-have')) and not any(token in trigger_for_change_terms for token in ('implement', 'patch', 'fix', 'sredi', 'poprav'))
        if review_only:
            return 'review_only_false_positive_pending_approval'
        e2e_marker = any(token in row_text for token in ('ack-seed', 'e2e seed', 'capability e2e', 'capability_e2e'))
        e2e_cutoff = now - max(0.0, float(e2e_seed_hours if e2e_seed_hours is not None else 1.0)) * 3600
        if e2e_marker and float(row['updated_at'] or 0) < e2e_cutoff:
            return 'stale_e2e_seed_pending_approval'
        return f'stale_pending_approval>{float(stale_hours or 24.0):g}h'

    def _expire_self_evolution_rows(
        self,
        rows: list[sqlite3.Row],
        *,
        now: float,
        stale_hours: float = 24.0,
        e2e_seed_hours: float = 1.0,
        commit: bool = True,
    ) -> dict:
        expired_ids: list[str] = []
        reasons: dict[str, str] = {}
        for row in rows:
            proposal_id = str(row['proposal_id'])
            reason = self._self_evolution_expiry_reason(row, now=now, stale_hours=stale_hours, e2e_seed_hours=e2e_seed_hours)
            before = row_to_dict(row)
            try:
                apply_result = json.loads(row['apply_result_json'] or '{}')
                if not isinstance(apply_result, dict):
                    apply_result = {}
            except Exception:
                apply_result = {}
            apply_result.update({'expired_at': now, 'expired_reason': reason})
            self.conn.execute(
                """
                UPDATE self_evolution_proposals
                SET status='expired', decided_at=COALESCE(decided_at, ?), updated_at=?, apply_result_json=?
                WHERE proposal_id=? AND status='needs_approval'
                """,
                (now, now, json.dumps(apply_result, ensure_ascii=False, sort_keys=True), proposal_id),
            )
            after = row_to_dict(self.conn.execute("SELECT * FROM self_evolution_proposals WHERE proposal_id=?", (proposal_id,)).fetchone())
            if after.get('status') == 'expired':
                expired_ids.append(proposal_id)
                reasons[proposal_id] = reason
                self.conn.execute(
                    "INSERT OR REPLACE INTO audit_log (audit_id, object_type, object_id, action, reason, details_json, created_at) VALUES (?, 'self_evolution_proposal', ?, 'expired', ?, ?, ?)",
                    (
                        stable_id('audit', proposal_id, 'expired', str(int(now))),
                        proposal_id,
                        reason[:240],
                        json.dumps({'previous_status': before.get('status', ''), 'risk_score': before.get('risk_score', 0)}, ensure_ascii=False, sort_keys=True),
                        now,
                    ),
                )
                record_revision(self.conn, object_type='self_evolution_proposal', object_id=proposal_id, action='expired', reason=reason, before=before, after=after, created_at=now)
        if commit:
            self.conn.commit()
        return {'status': 'ok', 'expired': len(expired_ids), 'proposal_ids': expired_ids, 'reasons': reasons}

    def expire_stale_self_evolution_proposals(
        self,
        *,
        dry_run: bool = False,
        now: float | None = None,
        stale_hours: float = 24.0,
        e2e_seed_hours: float = 1.0,
        limit: int = 200,
    ) -> dict:
        current_time = float(now or time.time())
        rows = self._stale_self_evolution_proposal_rows(now=current_time, stale_hours=stale_hours, e2e_seed_hours=e2e_seed_hours, limit=limit)
        if dry_run:
            return {
                'status': 'dry_run',
                'expired': len(rows),
                'proposal_ids': [str(row['proposal_id']) for row in rows],
            }
        return self._expire_self_evolution_rows(rows, now=current_time, stale_hours=stale_hours, e2e_seed_hours=e2e_seed_hours)

    def run_lifecycle_hygiene(
        self,
        *,
        dry_run: bool = True,
        now: float | None = None,
        pending_impression_days: int = 7,
        stale_work_days: int = 45,
        low_confidence_belief_days: int = 30,
        stale_pending_proposal_hours: float = 24.0,
        e2e_seed_pending_hours: float = 1.0,
    ) -> dict:
        """Run conservative non-destructive memory maintenance.

        Dry-run only counts candidates. Apply mode expires stale pending feedback,
        supersedes old low-priority work items, invalidates stale weak hypotheses,
        ages recipe candidates, and records every mutation in the audit spine.
        """
        ensure_audit_schema(self.conn)
        EpistemicManager(self.conn).ensure_schema()
        started_at = float(now or time.time())
        run_id = stable_id('maintenance', 'lifecycle_hygiene', str(int(started_at * 1000)), str(int(bool(dry_run))))
        summary: dict[str, Any] = {
            'run_id': run_id,
            'dry_run': bool(dry_run),
            'pending_impression_days': pending_impression_days,
            'stale_work_days': stale_work_days,
            'low_confidence_belief_days': low_confidence_belief_days,
            'stale_pending_proposal_hours': stale_pending_proposal_hours,
            'e2e_seed_pending_hours': e2e_seed_pending_hours,
            'expired_context_impressions': 0,
            'superseded_work_items': 0,
            'invalidated_low_confidence_beliefs': 0,
            'expired_rules': 0,
            'expired_epistemic_facts': 0,
            'expired_self_evolution_proposals': 0,
            'recipe_ageing': {},
            'recipe_archiving': {},
        }
        self.conn.execute(
            "INSERT OR REPLACE INTO maintenance_runs (run_id, run_type, dry_run, status, summary_json, started_at, finished_at) VALUES (?, 'lifecycle_hygiene', ?, 'started', '{}', ?, NULL)",
            (run_id, 1 if dry_run else 0, started_at),
        )
        try:
            pending_cutoff = started_at - max(1, int(pending_impression_days or 7)) * 86400
            pending_rows = self.conn.execute(
                "SELECT * FROM context_impressions WHERE outcome='pending' AND updated_at < ? ORDER BY updated_at ASC LIMIT 500",
                (pending_cutoff,),
            ).fetchall()
            summary['expired_context_impressions'] = len(pending_rows)

            work_cutoff = started_at - max(1, int(stale_work_days or 45)) * 86400
            work_rows = self.conn.execute(
                """
                SELECT * FROM work_items
                WHERE status IN ('active','blocked')
                  AND updated_at < ?
                  AND priority <= 0.2
                  AND work_item_id NOT IN (SELECT work_item_id FROM working_set)
                ORDER BY updated_at ASC LIMIT 200
                """,
                (work_cutoff,),
            ).fetchall()
            summary['superseded_work_items'] = len(work_rows)

            belief_cutoff = started_at - max(1, int(low_confidence_belief_days or 30)) * 86400
            belief_rows = self.conn.execute(
                """
                SELECT * FROM beliefs
                WHERE status='open'
                  AND confidence < 0.45
                  AND updated_at < ?
                ORDER BY updated_at ASC LIMIT 300
                """,
                (belief_cutoff,),
            ).fetchall()
            summary['invalidated_low_confidence_beliefs'] = len(belief_rows)

            expired_rule_rows = self.conn.execute(
                "SELECT * FROM rules WHERE status='active' AND expires_at IS NOT NULL AND expires_at <= ? ORDER BY expires_at ASC LIMIT 500",
                (started_at,),
            ).fetchall()
            summary['expired_rules'] = len(expired_rule_rows)

            epistemic_rows = self.conn.execute(
                "SELECT * FROM epistemic_learned_facts WHERE status='active' AND expires_at IS NOT NULL AND expires_at <= ? ORDER BY expires_at ASC LIMIT 500",
                (started_at,),
            ).fetchall()
            summary['expired_epistemic_facts'] = len(epistemic_rows)

            proposal_rows = self._stale_self_evolution_proposal_rows(
                now=started_at,
                stale_hours=stale_pending_proposal_hours,
                e2e_seed_hours=e2e_seed_pending_hours,
            )
            summary['expired_self_evolution_proposals'] = len(proposal_rows)

            summary['recipe_ageing'] = self.age_stale_recipes(dry_run=True)
            summary['recipe_archiving'] = self.archive_stale_review_recipes(dry_run=True)

            if not dry_run:
                for row in pending_rows:
                    before = row_to_dict(row)
                    self.conn.execute(
                        "UPDATE context_impressions SET outcome='expired', feedback_text=CASE WHEN feedback_text='' THEN 'no_feedback_window_elapsed' ELSE feedback_text END, updated_at=? WHERE impression_id=?",
                        (started_at, row['impression_id']),
                    )
                    after = row_to_dict(self.conn.execute("SELECT * FROM context_impressions WHERE impression_id=?", (row['impression_id'],)).fetchone())
                    record_revision(self.conn, object_type='context_impression', object_id=row['impression_id'], action='expire', reason='no_feedback_window_elapsed', before=before, after=after, created_at=started_at)

                for row in work_rows:
                    before = row_to_dict(row)
                    self.conn.execute(
                        "UPDATE work_items SET status='superseded', priority=0.05, resolved_at=COALESCE(resolved_at, ?), updated_at=? WHERE work_item_id=?",
                        (started_at, started_at, row['work_item_id']),
                    )
                    self.conn.execute("DELETE FROM working_set WHERE work_item_id=?", (row['work_item_id'],))
                    after = row_to_dict(self.conn.execute("SELECT * FROM work_items WHERE work_item_id=?", (row['work_item_id'],)).fetchone())
                    record_revision(self.conn, object_type='work_item', object_id=row['work_item_id'], action='supersede', reason='stale_low_priority_not_in_working_set', before=before, after=after, created_at=started_at)

                for row in belief_rows:
                    before = row_to_dict(row)
                    self.conn.execute(
                        "UPDATE beliefs SET status='invalidated', confidence=MIN(confidence, 0.2), updated_at=? WHERE belief_id=?",
                        (started_at, row['belief_id']),
                    )
                    after = row_to_dict(self.conn.execute("SELECT * FROM beliefs WHERE belief_id=?", (row['belief_id'],)).fetchone())
                    record_revision(self.conn, object_type='belief', object_id=row['belief_id'], action='invalidate', reason='stale_low_confidence_open_hypothesis', before=before, after=after, created_at=started_at)

                for row in expired_rule_rows:
                    before = row_to_dict(row)
                    self.conn.execute(
                        "UPDATE rules SET status='expired', updated_at=? WHERE rule_id=?",
                        (started_at, row['rule_id']),
                    )
                    after = row_to_dict(self.conn.execute("SELECT * FROM rules WHERE rule_id=?", (row['rule_id'],)).fetchone())
                    record_revision(self.conn, object_type='rule', object_id=row['rule_id'], action='expire', reason='ttl_elapsed', before=before, after=after, created_at=started_at)

                for row in epistemic_rows:
                    before = row_to_dict(row)
                    self.conn.execute(
                        "UPDATE epistemic_learned_facts SET status='expired', updated_at=? WHERE fact_id=?",
                        (started_at, row['fact_id']),
                    )
                    after = row_to_dict(self.conn.execute("SELECT * FROM epistemic_learned_facts WHERE fact_id=?", (row['fact_id'],)).fetchone())
                    record_revision(self.conn, object_type='epistemic_learned_fact', object_id=row['fact_id'], action='expire', reason='validity_window_elapsed', before=before, after=after, created_at=started_at)

                proposal_expiry = self._expire_self_evolution_rows(
                    proposal_rows,
                    now=started_at,
                    stale_hours=stale_pending_proposal_hours,
                    e2e_seed_hours=e2e_seed_pending_hours,
                    commit=False,
                )
                summary['expired_self_evolution_proposals'] = proposal_expiry['expired']

                summary['recipe_ageing'] = self.age_stale_recipes(dry_run=False)
                summary['recipe_archiving'] = self.archive_stale_review_recipes(dry_run=False)

            status = 'dry_run' if dry_run else 'ok'
            finished_at = time.time()
            self.conn.execute(
                "UPDATE maintenance_runs SET status=?, summary_json=?, finished_at=? WHERE run_id=?",
                (status, json.dumps(summary, ensure_ascii=False, sort_keys=True), finished_at, run_id),
            )
            self.conn.commit()
            summary['status'] = status
            summary['finished_at'] = finished_at
            return summary
        except Exception as exc:
            self.conn.rollback()
            summary['error'] = str(exc)[:500]
            finished_at = time.time()
            self.conn.execute(
                "UPDATE maintenance_runs SET status='error', summary_json=?, finished_at=? WHERE run_id=?",
                (json.dumps(summary, ensure_ascii=False, sort_keys=True), finished_at, run_id),
            )
            self.conn.commit()
            raise

    def run_init_maintenance(
        self,
        *,
        scope_key: str = '',
        hermes_home: str = '',
        min_interval_seconds: float = 21600.0,
        now: float | None = None,
    ) -> dict:
        ensure_audit_schema(self.conn)
        current_time = float(now or time.time())
        min_interval_seconds = max(0.0, float(min_interval_seconds or 0.0))
        previous = self.conn.execute(
            "SELECT run_id, started_at, finished_at FROM maintenance_runs WHERE run_type='init_maintenance' AND status='ok' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        if previous and min_interval_seconds > 0 and current_time - float(previous['started_at'] or 0) < min_interval_seconds:
            return {
                'status': 'skipped',
                'reason': 'rate_limited',
                'previous_run_id': previous['run_id'],
                'age_seconds': round(current_time - float(previous['started_at'] or 0), 3),
                'min_interval_seconds': min_interval_seconds,
            }

        run_id = stable_id('maintenance', 'init_maintenance', str(int(current_time * 1000)))
        summary: dict[str, Any] = {
            'run_id': run_id,
            'scope_key': scope_key or '',
            'min_interval_seconds': min_interval_seconds,
        }
        self.conn.execute(
            "INSERT OR REPLACE INTO maintenance_runs (run_id, run_type, dry_run, status, summary_json, started_at, finished_at) VALUES (?, 'init_maintenance', 0, 'started', '{}', ?, NULL)",
            (run_id, current_time),
        )
        self.conn.commit()
        try:
            summary['expired_rules'] = self.gc_expired_rules(now=current_time)
            summary['archived_stale_episodes'] = self.archive_stale_episodes()
            summary['destructive_episode_memory'] = self.suppress_destructive_episode_memory()
            summary['meta_work_items_deleted'] = self.cleanup_meta_work_items()
            summary['noisy_memory'] = self.cleanup_noisy_memory()
            summary['backfilled_work_items'] = self.backfill_work_items_from_recaps()
            if scope_key and hermes_home:
                self.backfill_causal_activations(scope_key=scope_key, hermes_home=hermes_home)
                summary['backfilled_causal_activations'] = 'attempted'
            summary['lifecycle_hygiene'] = self.run_lifecycle_hygiene(dry_run=False, now=current_time)
            summary['backup_rotation'] = self.rotate_backups(max_age_hours=48.0, max_keep=8)
            summary['wal_checkpoint'] = self.checkpoint_wal(truncate=True)
            finished_at = time.time()
            self.conn.execute(
                "UPDATE maintenance_runs SET status='ok', summary_json=?, finished_at=? WHERE run_id=?",
                (json.dumps(summary, ensure_ascii=False, sort_keys=True), finished_at, run_id),
            )
            self.conn.commit()
            summary['status'] = 'ok'
            summary['finished_at'] = finished_at
            return summary
        except Exception as exc:
            summary['error'] = str(exc)[:500]
            finished_at = time.time()
            try:
                self.conn.execute(
                    "UPDATE maintenance_runs SET status='error', summary_json=?, finished_at=? WHERE run_id=?",
                    (json.dumps(summary, ensure_ascii=False, sort_keys=True), finished_at, run_id),
                )
                self.conn.commit()
            except Exception:
                logger.exception("[live_brain] failed to record init maintenance error")
            raise

    def compile_epistemic_brief(self, scope_key: str, query: str = '', *, max_facts: int = 4) -> str:
        return EpistemicManager(self.conn).compile_brief(scope_key, query, max_facts=max_facts)

    def debug_epistemic(self, scope_key: str, query: str = '') -> dict:
        return EpistemicManager(self.conn).debug(scope_key, query)

    def record_epistemic_source(self, **kwargs: Any) -> dict:
        return EpistemicManager(self.conn).record_source(**kwargs)

    def record_epistemic_fact(self, **kwargs: Any) -> dict:
        return EpistemicManager(self.conn).record_fact(**kwargs)

    def record_epistemic_tool_result(self, **kwargs: Any) -> dict:
        return EpistemicManager(self.conn).record_tool_result(**kwargs)

    def ingest_reality_event(self, **kwargs: Any) -> dict:
        return RealityEngine(self.conn).ingest_event(**kwargs)

    def compile_reality_brief(self, scope_key: str, query: str = '', *, max_lines: int = 12) -> str:
        return RealityEngine(self.conn).compile_brief(scope_key, query, max_lines=max_lines)

    def debug_reality(self, scope_key: str, query: str = '') -> dict:
        return RealityEngine(self.conn).debug(scope_key, query)

    def action_gate(self, scope_key: str, action_type: str, payload: dict | None = None) -> dict:
        return RealityEngine(self.conn).action_gate(scope_key, action_type, payload or {})

    def propose_self_evolution(self, **kwargs: Any) -> dict:
        from .evolution import SelfEvolutionManager
        return SelfEvolutionManager(self.conn).propose(**kwargs)

    def list_self_evolution_proposals(self, *, status: str = '', include_applied: bool = False, limit: int = 10) -> List[dict]:
        from .evolution import SelfEvolutionManager
        return SelfEvolutionManager(self.conn).list(status=status, include_applied=include_applied, limit=limit)

    def decide_self_evolution_proposal(self, proposal_id: str, decision: str, reason: str = '') -> dict:
        from .evolution import SelfEvolutionManager
        return SelfEvolutionManager(self.conn).decide(proposal_id, decision, reason)

    def gc_expired_rules(self, now: float | None = None) -> int:
        now = now or time.time()
        rows = self.conn.execute(
            "SELECT * FROM rules WHERE status = 'active' AND expires_at IS NOT NULL AND expires_at <= ?",
            (now,),
        ).fetchall()
        for row in rows:
            before = row_to_dict(row)
            self.conn.execute(
                "UPDATE rules SET status = 'expired', updated_at = ? WHERE rule_id = ?",
                (now, row['rule_id']),
            )
            after = row_to_dict(self.conn.execute("SELECT * FROM rules WHERE rule_id=?", (row['rule_id'],)).fetchone())
            record_revision(self.conn, object_type='rule', object_id=row['rule_id'], action='expire', reason='ttl_elapsed', before=before, after=after, created_at=now)
        self.conn.commit()
        return len(rows)

    def archive_stale_episodes(self, max_active: int = 3, max_active_hours: float = 72.0) -> int:
        now = time.time()
        cutoff = now - (max_active_hours * 3600)
        rows = self.conn.execute(
            "SELECT episode_id, updated_at FROM episodes WHERE status = 'active' ORDER BY updated_at DESC"
        ).fetchall()
        archived = 0
        for index, row in enumerate(rows):
            if index < max_active and row['updated_at'] >= cutoff:
                continue
            self.conn.execute(
                "UPDATE episodes SET status = 'archived', updated_at = ? WHERE episode_id = ?",
                (now, row['episode_id']),
            )
            archived += 1
        if archived:
            self.conn.commit()
        return archived

    def suppress_destructive_episode_memory(self, *, dry_run: bool = False) -> dict:
        """Archive stale destructive episodes so old delete requests cannot become active context.

        Explicit current user commands are handled by the current turn. Historical episodes that
        merely say "delete/remove/rm" are preserved in the DB but removed from active/dormant
        context unless they are explicit safety negations such as "ne brisi".
        """
        destructive_re = re.compile(r'\b(?:izbriši|izbrisi|obriši|obrisi|briši|brisi|delete|remove|rm)\b', re.IGNORECASE)
        negated_re = re.compile(r"\b(?:ne|nemoj|nikad|never|do\s+not|don'?t|dont)\s+(?:da\s+)?(?:izbriši|izbrisi|obriši|obrisi|briši|brisi|delete|remove|rm)\b", re.IGNORECASE)
        rows = self.conn.execute(
            "SELECT episode_id, title, current_summary, status FROM episodes WHERE status IN ('active','dormant')"
        ).fetchall()
        candidates = []
        for row in rows:
            text = f"{row['title'] or ''} {row['current_summary'] or ''}"
            if not destructive_re.search(text):
                continue
            if negated_re.search(text):
                continue
            candidates.append(dict(row))
        if dry_run:
            return {'status': 'dry_run', 'candidates': len(candidates), 'archived': 0, 'episode_ids': [r['episode_id'] for r in candidates]}
        now = time.time()
        archived = 0
        for row in candidates:
            self.conn.execute(
                "UPDATE episodes SET status='archived', updated_at=? WHERE episode_id=?",
                (now, row['episode_id']),
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO audit_log (audit_id, object_type, object_id, action, reason, details_json, created_at) VALUES (?, 'episode', ?, 'archived', 'destructive_stale_memory_guard', ?, ?)",
                (
                    stable_id('audit', f"destructive_episode:{row['episode_id']}:{now}"),
                    row['episode_id'],
                    json.dumps({'title': row.get('title', ''), 'previous_status': row.get('status', '')}, ensure_ascii=False),
                    now,
                ),
            )
            archived += 1
        if archived:
            self.conn.commit()
        return {'status': 'ok', 'candidates': len(candidates), 'archived': archived, 'episode_ids': [r['episode_id'] for r in candidates]}

    def cleanup_meta_work_items(self) -> int:
        cur = self.conn.execute(
            "DELETE FROM work_items WHERE lower(title) LIKE 'sumarizuj%' OR lower(title) LIKE 'what did you do%' OR lower(title) LIKE 'recap%' OR lower(title) LIKE 'pregled%' OR lower(title) LIKE 'review the conversation above%' OR lower(title) IN ('da','ne','ok','okej','sve','yes','no','continue','nastavi')"
        )
        deleted = int(cur.rowcount or 0)
        if deleted:
            self.conn.commit()
        return deleted

    def checkpoint_wal(self, *, truncate: bool = True) -> dict:
        mode = 'TRUNCATE' if truncate else 'PASSIVE'
        try:
            rows = self.conn.execute(f'PRAGMA wal_checkpoint({mode})').fetchall()
            row = rows[0] if rows else None
            result = {'status': 'ok', 'mode': mode.lower()}
            if row is not None:
                keys = list(getattr(row, 'keys', lambda: [])())
                if keys:
                    result.update({str(key): row[key] for key in keys})
                else:
                    values = tuple(row)
                    for key, value in zip(('busy', 'log', 'checkpointed'), values):
                        result[key] = value
            return result
        except Exception as exc:
            logger.warning("[live_brain] WAL checkpoint failed: %s", exc)
            return {'status': 'error', 'mode': mode.lower(), 'error': str(exc)[:300]}

    def rotate_backups(self, *, max_age_hours: float = 48.0, max_keep: int = 8, dry_run: bool = False) -> dict:
        source = Path(self.db_path)
        backup_dir = source.parent
        pattern = f"{source.stem}_backup_*{source.suffix}"
        now = time.time()
        cutoff = now - max(0.1, float(max_age_hours or 48.0)) * 3600
        max_keep = max(1, int(max_keep or 8))
        backup_files = sorted(
            [candidate for candidate in backup_dir.glob(pattern) if candidate.is_file()],
            key=lambda candidate: candidate.stat().st_mtime,
            reverse=True,
        )
        deleted: list[str] = []
        kept: list[str] = []
        errors: list[dict[str, str]] = []
        for index, candidate in enumerate(backup_files):
            should_delete = candidate.stat().st_mtime < cutoff or index >= max_keep
            if not should_delete:
                kept.append(str(candidate))
                continue
            deleted.append(str(candidate))
            if dry_run:
                continue
            try:
                candidate.unlink()
            except Exception as exc:
                errors.append({'path': str(candidate), 'error': str(exc)[:300]})
        return {
            'status': 'dry_run' if dry_run else ('error' if errors else 'ok'),
            'pattern': pattern,
            'max_age_hours': max_age_hours,
            'max_keep': max_keep,
            'seen': len(backup_files),
            'deleted': len(deleted) - len(errors),
            'deleted_paths': deleted[:20],
            'kept': len(kept),
            'errors': errors,
        }

    def backup_database(self, label: str = 'cleanup') -> str:
        self.conn.commit()
        self.checkpoint_wal(truncate=False)
        source = Path(self.db_path)
        backup_path = source.with_name(f"{source.stem}_backup_{label}_{int(time.time())}{source.suffix}")
        shutil.copy2(source, backup_path)
        return str(backup_path)

    def cleanup_noisy_memory(self, *, dry_run: bool = False, backup: bool = False) -> dict:
        noisy_like = [
            '%## summary%',
            '%### situacija%',
            '%the user sent an image%',
            '%gave me his selfie%',
            '%personal trust%',
            '%openrouter api key%',
            '%api key (active%',
            '%review the conversation above%',
            '%runtime test%',
            '%model was just switched%',
        ]
        stats = {'facts': 0, 'beliefs': 0, 'episodes': 0, 'work_items': 0, 'rules': 0, 'fix_recipes': 0, 'dry_run': dry_run}
        backup_path = ''
        if backup and not dry_run:
            backup_path = self.backup_database('noisy-memory')
        for pattern in noisy_like:
            if dry_run:
                stats['facts'] += self.conn.execute(
                    "SELECT COUNT(*) FROM facts WHERE status='active' AND lower(fact_text) LIKE ?",
                    (pattern,),
                ).fetchone()[0]
                stats['beliefs'] += self.conn.execute(
                    "SELECT COUNT(*) FROM beliefs WHERE status IN ('open','validated') AND lower(claim_text) LIKE ?",
                    (pattern,),
                ).fetchone()[0]
                stats['episodes'] += self.conn.execute(
                    "SELECT COUNT(*) FROM episodes WHERE status IN ('active','dormant') AND (lower(title) LIKE ? OR lower(current_summary) LIKE ?)",
                    (pattern, pattern),
                ).fetchone()[0]
                stats['work_items'] += self.conn.execute(
                    "SELECT COUNT(*) FROM work_items WHERE status IN ('active','blocked') AND (lower(title) LIKE ? OR lower(root_cause) LIKE ? OR lower(evidence_json) LIKE ?)",
                    (pattern, pattern, pattern),
                ).fetchone()[0]
                stats['rules'] += self.conn.execute(
                    "SELECT COUNT(*) FROM rules WHERE status='active' AND (lower(action_json) LIKE ? OR lower(condition_json) LIKE ?)",
                    (pattern, pattern),
                ).fetchone()[0]
                stats['fix_recipes'] += self.conn.execute(
                    "SELECT COUNT(*) FROM fix_recipes WHERE status IN ('active','candidate') AND lower(problem_pattern) LIKE ?",
                    (pattern,),
                ).fetchone()[0]
                continue
            cur = self.conn.execute(
                "UPDATE facts SET status='archived' WHERE status='active' AND lower(fact_text) LIKE ?",
                (pattern,),
            )
            stats['facts'] += cur.rowcount
            cur = self.conn.execute(
                "UPDATE beliefs SET status='invalidated', updated_at=? WHERE status IN ('open','validated') AND lower(claim_text) LIKE ?",
                (time.time(), pattern),
            )
            stats['beliefs'] += cur.rowcount
            cur = self.conn.execute(
                "UPDATE episodes SET status='archived', updated_at=? WHERE status IN ('active','dormant') AND (lower(title) LIKE ? OR lower(current_summary) LIKE ?)",
                (time.time(), pattern, pattern),
            )
            stats['episodes'] += cur.rowcount
            cur = self.conn.execute(
                "UPDATE work_items SET status='superseded', resolved_at=COALESCE(resolved_at, ?), priority=0.05 WHERE status IN ('active','blocked') AND (lower(title) LIKE ? OR lower(root_cause) LIKE ? OR lower(evidence_json) LIKE ?)",
                (time.time(), pattern, pattern, pattern),
            )
            stats['work_items'] += cur.rowcount
            cur = self.conn.execute(
                "UPDATE rules SET status='superseded', updated_at=? WHERE status='active' AND (lower(action_json) LIKE ? OR lower(condition_json) LIKE ?)",
                (time.time(), pattern, pattern),
            )
            stats['rules'] += cur.rowcount
            cur = self.conn.execute(
                "UPDATE fix_recipes SET status='needs_review', promotion_status='needs_review', last_reviewed_at=?, confidence=MIN(confidence, 0.4), updated_at=? WHERE status IN ('active','candidate') AND lower(problem_pattern) LIKE ?",
                (time.time(), time.time(), pattern),
            )
            stats['fix_recipes'] += cur.rowcount
        recipe_meta_like = [
            '%review conversation above%', '%live brain sistem%', '%live brain plugin%', '%10/10 gate%',
            '%arhitekturu trenutne live baze%', '%analiziraj live brain%', '%ukupan utisak%',
            '%implemented measurement layer%', '%precision ratio%', '%attribution modes%', '%promotion helper%',
            '%feedback loop%', '%hermes restart%', '%package rebuilt%', '%smoke ok%', '%eval ok%',
            '%metrics healthy%', '%manual recipe compiler%', '%gotovo implementirao%', '%loop mnogo stroži%',
            '%loop mnogo strozi%', '%compiler pamti%', '%were right metrics%',
        ]
        for pattern in recipe_meta_like:
            if dry_run:
                stats['fix_recipes'] += self.conn.execute(
                    "SELECT COUNT(*) FROM fix_recipes WHERE status IN ('active','candidate') AND lower(problem_pattern) LIKE ?",
                    (pattern,),
                ).fetchone()[0]
                continue
            cur = self.conn.execute(
                "UPDATE fix_recipes SET status='needs_review', promotion_status='needs_review', last_reviewed_at=?, confidence=MIN(confidence, 0.4), updated_at=? WHERE status IN ('active','candidate') AND lower(problem_pattern) LIKE ?",
                (time.time(), time.time(), pattern),
            )
            stats['fix_recipes'] += cur.rowcount
        if dry_run:
            stats['fix_recipes'] += self.conn.execute(
                "SELECT COUNT(*) FROM fix_recipes WHERE status='active' AND (artifact_verified=0 OR promotion_status!='active')"
            ).fetchone()[0]
        else:
            cur = self.conn.execute(
                "UPDATE fix_recipes SET status='candidate', promotion_status='candidate', confidence=MIN(confidence, 0.6), updated_at=? WHERE status='active' AND (artifact_verified=0 OR promotion_status!='active')",
                (time.time(),),
            )
            stats['fix_recipes'] += cur.rowcount
        episode_rows = self.conn.execute(
            "SELECT episode_id, title, current_summary, status FROM episodes WHERE status IN ('active','dormant')"
        ).fetchall()
        noisy_episode_rows = [row for row in episode_rows if is_noisy_episode_memory(row['title'] or '', row['current_summary'] or '')]
        if dry_run:
            stats['episodes'] += len(noisy_episode_rows)
        else:
            now = time.time()
            for row in noisy_episode_rows:
                cur = self.conn.execute(
                    "UPDATE episodes SET status='archived', updated_at=? WHERE episode_id=? AND status IN ('active','dormant')",
                    (now, row['episode_id']),
                )
                stats['episodes'] += cur.rowcount or 0
                if cur.rowcount:
                    self.conn.execute(
                        "INSERT OR REPLACE INTO audit_log (audit_id, object_type, object_id, action, reason, details_json, created_at) VALUES (?, 'episode', ?, 'archived', 'noisy_meta_episode_guard', ?, ?)",
                        (
                            stable_id('audit', 'noisy_episode', row['episode_id'], str(now)),
                            row['episode_id'],
                            json.dumps({'title': row['title'] or '', 'summary': row['current_summary'] or ''}, ensure_ascii=False),
                            now,
                        ),
                    )

        work_rows = self.conn.execute(
            "SELECT work_item_id, title, root_cause, evidence_json, status FROM work_items WHERE status IN ('active','blocked')"
        ).fetchall()
        noisy_work_rows = [
            row for row in work_rows
            if is_noisy_episode_memory(row['title'] or '', f"{row['root_cause'] or ''} {row['evidence_json'] or ''}")
        ]
        if dry_run:
            stats['work_items'] += len(noisy_work_rows)
        else:
            now = time.time()
            for row in noisy_work_rows:
                cur = self.conn.execute(
                    "UPDATE work_items SET status='superseded', resolved_at=COALESCE(resolved_at, ?), priority=0.05 WHERE work_item_id=? AND status IN ('active','blocked')",
                    (now, row['work_item_id']),
                )
                stats['work_items'] += cur.rowcount or 0
        if backup_path:
            stats['backup_path'] = backup_path
        if not dry_run:
            self.conn.commit()
        return stats

    def backfill_work_items_from_recaps(self) -> int:
        rows = self.conn.execute(
            "SELECT session_id, scope_key, task, root_cause, current_status, next_step, objective, what_changed, created_at, updated_at FROM canonical_recaps WHERE scope_key != '' ORDER BY updated_at DESC LIMIT 200"
        ).fetchall()
        inserted = 0
        for row in rows:
            task = (row['task'] or '').strip()
            lowered = task.lower()
            if not task:
                continue
            if lowered.startswith('sumarizuj') or lowered.startswith('what did you do') or lowered.startswith('recap') or lowered.startswith('pregled'):
                continue
            if lowered.startswith('review the conversation above'):
                continue
            if task in {'Nastavi', 'nastavi', 'ok', 'okej', 'da', 'ne'}:
                continue
            work_item_id = stable_id('work_item', row['scope_key'], task)
            status = (row['current_status'] or 'active').strip().lower() or 'active'
            if status not in {'active', 'blocked', 'resolved'}:
                status = 'active' if status != 'blocked' else 'blocked'
            priority = 1.0 if status == 'active' else (0.7 if status == 'blocked' else 0.2)
            evidence = {
                'objective': row['objective'] or '',
                'what_changed': row['what_changed'] or '',
                'source': 'canonical_recap',
            }
            resolved_at = row['updated_at'] if status == 'resolved' else None
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO work_items (work_item_id, scope_key, session_id, title, status, priority, evidence_json, next_step, root_cause, supersedes_work_item_id, created_at, updated_at, resolved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)",
                (work_item_id, row['scope_key'], row['session_id'], task[:160], status, priority, json.dumps(evidence), (row['next_step'] or '')[:300], (row['root_cause'] or '')[:500], row['created_at'], row['updated_at'], resolved_at),
            )
            inserted += int(cur.rowcount or 0)
        if inserted:
            self.conn.commit()
        return inserted

    def backfill_causal_activations(self, scope_key: str, hermes_home: str) -> None:
        import json as _json
        from pathlib import Path as _Path
        sessions_dir = _Path(hermes_home) / 'sessions'
        tool_map = dict(TOOL_SIGNAL_TERMS)
        for jsonl in sorted(sessions_dir.glob('*.jsonl'), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
            try:
                msgs = [_json.loads(l) for l in jsonl.read_text().splitlines() if l.strip()]
            except Exception:
                continue
            user_ctx = ''
            for m in msgs:
                if m.get('role') == 'user':
                    user_ctx = str(m.get('content') or '')[:120]
                if m.get('role') != 'tool':
                    continue
                content = str(m.get('content') or '').lower()
                tool_used = next((v for k, v in tool_map.items() if k in content), '')
                if not tool_used:
                    continue
                success = any(s in content for s in ['output:', 'size:', 'saved', 'success', 'ok', 'done', 'radi', 'generated'])
                if not success:
                    continue
                activation_id = f"activation:{stable_id('act', scope_key, user_ctx, tool_used)}"
                existing = self.conn.execute("SELECT times_confirmed FROM causal_activations WHERE activation_id = ?", (activation_id,)).fetchone()
                if existing:
                    self.conn.execute("UPDATE causal_activations SET times_confirmed = ? WHERE activation_id = ?", (existing[0] + 1, activation_id))
                else:
                    import time as _time
                    now = _time.time()
                    self.conn.execute(
                        "INSERT OR IGNORE INTO causal_activations (activation_id, scope_key, trigger_text, action_taken, tool_used, outcome, success, confidence, times_confirmed, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 1, 0.8, 1, ?, ?)",
                        (activation_id, scope_key, user_ctx, content[:200], tool_used, content[:200], now, now),
                    )
        self.conn.commit()

    def log_recipe_rejection(self, scope_key: str, trigger_pattern: str, tool_name: str, reason: str, artifact_verified: bool, created_at: float, source: str = 'candidate_gate') -> None:
        rejection_id = stable_id('recipe_rejection', scope_key, trigger_pattern, tool_name, reason, str(int(created_at)))
        self.conn.execute(
            "INSERT OR REPLACE INTO recipe_rejections (rejection_id, scope_key, trigger_pattern, tool_name, reason, artifact_verified, source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (rejection_id, scope_key, trigger_pattern[:240], tool_name, reason[:80], 1 if artifact_verified else 0, source, created_at),
        )

    def age_stale_recipes(self, *, active_days: int = 45, candidate_days: int = 30, dry_run: bool = False) -> dict:
        """Conservatively age recipe population without deleting history.

        Active recipes that have not been injected recently are demoted to
        candidate so they stop being presented as PROVEN FIX until reconfirmed.
        Candidate recipes that have not earned promotion are moved to
        needs_review. Every mutation is written to audit_log.
        """
        now = time.time()
        active_cutoff = now - active_days * 86400
        candidate_cutoff = now - candidate_days * 86400
        active_rows = self.conn.execute(
            """
            SELECT recipe_id, problem_pattern, status, promotion_status, confidence, updated_at, promoted_at
            FROM fix_recipes r
            WHERE r.status='active'
              AND COALESCE(r.promoted_at, r.updated_at, r.created_at) < ?
              AND NOT EXISTS (
                  SELECT 1 FROM context_impressions i
                  WHERE i.recipe_ids_json LIKE '%' || r.recipe_id || '%'
                    AND i.created_at >= ?
              )
            """,
            (active_cutoff, active_cutoff),
        ).fetchall()
        candidate_rows = self.conn.execute(
            """
            SELECT recipe_id, problem_pattern, status, promotion_status, confidence, updated_at, candidate_since
            FROM fix_recipes r
            WHERE r.status='candidate'
              AND COALESCE(r.candidate_since, r.updated_at, r.created_at) < ?
              AND NOT EXISTS (
                  SELECT 1 FROM context_impressions i
                  WHERE i.recipe_ids_json LIKE '%' || r.recipe_id || '%'
                    AND i.outcome='success'
                    AND i.created_at >= ?
              )
            """,
            (candidate_cutoff, candidate_cutoff),
        ).fetchall()
        stats = {
            'active_days': active_days,
            'candidate_days': candidate_days,
            'matched_active': len(active_rows),
            'matched_candidate': len(candidate_rows),
            'demoted_active': 0,
            'reviewed_candidate': 0,
            'dry_run': dry_run,
        }
        if dry_run:
            return stats
        for row in active_rows:
            before = row_to_dict(row)
            details = {
                'problem_pattern': row['problem_pattern'],
                'previous_status': row['status'],
                'previous_promotion_status': row['promotion_status'],
                'previous_confidence': row['confidence'],
                'active_days': active_days,
                'reason': 'no_recent_impressions',
            }
            self.conn.execute(
                "INSERT OR REPLACE INTO audit_log (audit_id, object_type, object_id, action, reason, details_json, created_at) VALUES (?, 'fix_recipe', ?, 'degrade', 'stale_active_no_recent_impressions', ?, ?)",
                (stable_id('audit', row['recipe_id'], 'degrade', str(int(now))), row['recipe_id'], json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            self.conn.execute(
                "UPDATE fix_recipes SET status='candidate', promotion_status='candidate', confidence=MAX(confidence - 0.1, 0.3), updated_at=? WHERE recipe_id=?",
                (now, row['recipe_id']),
            )
            after = row_to_dict(self.conn.execute("SELECT * FROM fix_recipes WHERE recipe_id=?", (row['recipe_id'],)).fetchone())
            record_revision(self.conn, object_type='fix_recipe', object_id=row['recipe_id'], action='degrade', reason='stale_active_no_recent_impressions', before=before, after=after, created_at=now)
            stats['demoted_active'] += 1
        for row in candidate_rows:
            before = row_to_dict(row)
            details = {
                'problem_pattern': row['problem_pattern'],
                'previous_status': row['status'],
                'previous_promotion_status': row['promotion_status'],
                'previous_confidence': row['confidence'],
                'candidate_days': candidate_days,
                'reason': 'candidate_not_promoted',
            }
            self.conn.execute(
                "INSERT OR REPLACE INTO audit_log (audit_id, object_type, object_id, action, reason, details_json, created_at) VALUES (?, 'fix_recipe', ?, 'review', 'stale_candidate_not_promoted', ?, ?)",
                (stable_id('audit', row['recipe_id'], 'review', str(int(now))), row['recipe_id'], json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            self.conn.execute(
                "UPDATE fix_recipes SET status='needs_review', promotion_status='needs_review', last_reviewed_at=?, confidence=MAX(confidence - 0.15, 0.2), updated_at=? WHERE recipe_id=?",
                (now, now, row['recipe_id']),
            )
            after = row_to_dict(self.conn.execute("SELECT * FROM fix_recipes WHERE recipe_id=?", (row['recipe_id'],)).fetchone())
            record_revision(self.conn, object_type='fix_recipe', object_id=row['recipe_id'], action='review', reason='stale_candidate_not_promoted', before=before, after=after, created_at=now)
            stats['reviewed_candidate'] += 1
        self.conn.commit()
        return stats

    def archive_stale_review_recipes(self, days: int = 30, *, dry_run: bool = False) -> dict:
        cutoff = time.time() - days * 86400
        rows = self.conn.execute(
            "SELECT recipe_id, problem_pattern, status, promotion_status FROM fix_recipes WHERE status='needs_review' AND updated_at < ? AND (last_reviewed_at IS NULL OR last_reviewed_at < ?)",
            (cutoff, cutoff),
        ).fetchall()
        stats = {'days': days, 'matched': len(rows), 'archived': 0, 'dry_run': dry_run}
        if dry_run:
            return stats
        now = time.time()
        for row in rows:
            before = row_to_dict(row)
            self.conn.execute(
                "INSERT OR REPLACE INTO audit_log (audit_id, object_type, object_id, action, reason, details_json, created_at) VALUES (?, 'fix_recipe', ?, 'archive', 'stale_needs_review', ?, ?)",
                (stable_id('audit', row['recipe_id'], 'archive', str(int(now))), row['recipe_id'], json.dumps({'problem_pattern': row['problem_pattern'], 'previous_status': row['status'], 'previous_promotion_status': row['promotion_status']}), now),
            )
            self.conn.execute(
                "UPDATE fix_recipes SET status='archived', promotion_status='archived', updated_at=? WHERE recipe_id=?",
                (now, row['recipe_id']),
            )
            after = row_to_dict(self.conn.execute("SELECT * FROM fix_recipes WHERE recipe_id=?", (row['recipe_id'],)).fetchone())
            record_revision(self.conn, object_type='fix_recipe', object_id=row['recipe_id'], action='archive', reason='stale_needs_review', before=before, after=after, created_at=now)
            stats['archived'] += 1
        self.conn.commit()
        return stats

    def close(self) -> None:
        self.conn.close()

    def attribution_report(self, scope_key: str = '', days: int = 30) -> dict:
        cutoff = time.time() - days * 86400
        params: list = [cutoff]
        where = "created_at >= ? AND outcome IN ('success','failure')"
        if scope_key:
            where += " AND scope_key = ?"
            params.append(scope_key)
        rows = self.conn.execute(
            f"SELECT attribution_mode, COUNT(*) c FROM context_impressions WHERE {where} GROUP BY attribution_mode",
            params,
        ).fetchall()
        counts = {'precise': 0, 'broad': 0, 'fallback': 0, 'none': 0}
        for row in rows:
            key = row['attribution_mode'] or 'none'
            counts[key] = int(row['c'] or 0)
        denominator = counts['precise'] + counts['broad'] + counts['fallback']
        precision_ratio = round(counts['precise'] / denominator, 4) if denominator else None
        return {'days': days, 'scope_key': scope_key, 'counts': counts, 'precision_ratio': precision_ratio, 'sample_size': denominator}

    # ─── Working Set ───────────────────────────────────────────────────────────


    def upsert_fix_recipe(self, scope_key: str, problem_pattern: str, tool_name: str, steps: List[str], args_template: dict | None, success_criteria: str, confidence: float, source: str, scope_tags_json: str, now: float, *, artifact_verified: bool = False, artifact_path: str = '', error_type: str = '', promotion_status: str = 'candidate') -> dict:
        recipe_id = stable_id('recipe', scope_key, problem_pattern, tool_name, json.dumps(args_template or {}, sort_keys=True))
        before = row_to_dict(self.conn.execute("SELECT * FROM fix_recipes WHERE recipe_id = ?", (recipe_id,)).fetchone())
        existing = self.conn.execute(
            "SELECT times_confirmed, confidence, artifact_verified FROM fix_recipes WHERE recipe_id = ?",
            (recipe_id,),
        ).fetchone()
        times_confirmed = 1
        if existing:
            times_confirmed = int(existing['times_confirmed'] or 0) + 1
            confidence = max(float(existing['confidence'] or 0), confidence)
            artifact_verified = bool(artifact_verified or existing['artifact_verified'])
        status = 'active' if promotion_status == 'active' and artifact_verified else 'candidate'
        self.conn.execute(
            "INSERT OR REPLACE INTO fix_recipes (recipe_id, scope_key, problem_pattern, tool_name, steps_json, args_template_json, success_criteria, artifact_verified, artifact_path, error_type, promotion_status, candidate_since, promoted_at, last_reviewed_at, confidence, times_confirmed, status, source, scope_tags_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT candidate_since FROM fix_recipes WHERE recipe_id = ?), ?), CASE WHEN ?='active' THEN COALESCE((SELECT promoted_at FROM fix_recipes WHERE recipe_id = ?), ?) ELSE (SELECT promoted_at FROM fix_recipes WHERE recipe_id = ?) END, CASE WHEN ?='needs_review' THEN ? ELSE (SELECT last_reviewed_at FROM fix_recipes WHERE recipe_id = ?) END, ?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM fix_recipes WHERE recipe_id = ?), ?), ?)",
            (recipe_id, scope_key, problem_pattern[:240], tool_name, json.dumps(steps or [], ensure_ascii=False), json.dumps(args_template or {}, sort_keys=True), success_criteria[:240], 1 if artifact_verified else 0, artifact_path[:240], error_type[:80], promotion_status, recipe_id, now, status, recipe_id, now, recipe_id, status, now, recipe_id, confidence, times_confirmed, status, source, scope_tags_json or '{}', recipe_id, now, now),
        )
        after = row_to_dict(self.conn.execute("SELECT * FROM fix_recipes WHERE recipe_id = ?", (recipe_id,)).fetchone())
        record_revision(self.conn, object_type='fix_recipe', object_id=recipe_id, action='upsert', reason=source or 'store_upsert', before=before, after=after, created_at=now)
        return {'recipe_id': recipe_id, 'times_confirmed': times_confirmed, 'confidence': confidence, 'artifact_verified': artifact_verified, 'status': status}

    def get_working_set(self, scope_key: str, max_size: int = 5) -> List[Dict]:
        """Return the current working set (active work items) for a scope."""
        rows = self.conn.execute(
            """SELECT w.work_item_id, w.title, w.status, w.priority, w.updated_at, w.root_cause, w.next_step
               FROM working_set ws
               JOIN work_items w ON w.work_item_id = ws.work_item_id
               WHERE ws.scope_key = ?
               ORDER BY ws.slot ASC
               LIMIT ?""",
            (scope_key, max_size),
        ).fetchall()
        return [dict(row) for row in rows]

    def set_working_set(self, scope_key: str, work_item_ids: List[str]) -> None:
        """Atomically replace the working set for a scope."""
        now = time.time()
        self.conn.execute("DELETE FROM working_set WHERE scope_key = ?", (scope_key,))
        for slot, wid in enumerate(work_item_ids[:5]):
            self.conn.execute(
                "INSERT INTO working_set (scope_key, work_item_id, added_at, slot) VALUES (?, ?, ?, ?)",
                (scope_key, wid, now, slot),
            )
        self.conn.commit()

    def add_to_working_set(self, scope_key: str, work_item_id: str) -> None:
        """Add a work item to the working set if not already present."""
        existing = self.conn.execute(
            "SELECT slot FROM working_set WHERE scope_key = ? AND work_item_id = ?",
            (scope_key, work_item_id),
        ).fetchone()
        if existing:
            return
        max_slot_row = self.conn.execute(
            "SELECT MAX(slot) FROM working_set WHERE scope_key = ?", (scope_key,),
        ).fetchone()
        next_slot = (max_slot_row[0] + 1) if max_slot_row and max_slot_row[0] is not None else 0
        if next_slot >= 5:
            next_slot = 4
        self.conn.execute(
            "INSERT OR IGNORE INTO working_set (scope_key, work_item_id, added_at, slot) VALUES (?, ?, ?, ?)",
            (scope_key, work_item_id, time.time(), next_slot),
        )
        self.conn.commit()

    def refresh_working_set_slot(self, scope_key: str, work_item_id: str) -> None:
        """Bump a work item to front of working set (most recent = slot 0)."""
        self.conn.execute(
            "UPDATE working_set SET added_at = ? WHERE scope_key = ? AND work_item_id = ?",
            (time.time(), scope_key, work_item_id),
        )
        rows = self.conn.execute(
            "SELECT work_item_id FROM working_set WHERE scope_key = ? ORDER BY added_at DESC LIMIT 5",
            (scope_key,),
        ).fetchall()
        for slot, row in enumerate(rows):
            self.conn.execute(
                "UPDATE working_set SET slot = ? WHERE scope_key = ? AND work_item_id = ?",
                (slot, scope_key, row['work_item_id']),
            )
        self.conn.commit()

    def resolve_stale_items(self, scope_key: str, max_stale_hours: float = 72.0) -> int:
        """Mark active work items older than max_stale_hours as resolved. Returns count."""
        cutoff = time.time() - (max_stale_hours * 3600)
        cur = self.conn.execute(
            """UPDATE work_items SET status = 'resolved', resolved_at = ?
               WHERE scope_key = ? AND status = 'active' AND updated_at < ?""",
            (time.time(), scope_key, cutoff),
        )
        self.conn.commit()
        return cur.rowcount

    def invalidate_belief(self, belief_id: str) -> List[str]:
        """Invalidate a belief and cascade to all dependent beliefs.
        Returns list of invalidated belief IDs."""
        invalidated = []
        queue = [belief_id]
        while queue:
            bid = queue.pop(0)
            row = self.conn.execute(
                "SELECT belief_id FROM beliefs WHERE supersedes_belief_id = ?", (bid,)
            ).fetchall()
            for r in row:
                now = time.time()
                before = row_to_dict(self.conn.execute("SELECT * FROM beliefs WHERE belief_id = ?", (r['belief_id'],)).fetchone())
                self.conn.execute(
                    "UPDATE beliefs SET status = 'invalidated', updated_at = ? WHERE belief_id = ?",
                    (now, r['belief_id']),
                )
                after = row_to_dict(self.conn.execute("SELECT * FROM beliefs WHERE belief_id = ?", (r['belief_id'],)).fetchone())
                record_revision(self.conn, object_type='belief', object_id=r['belief_id'], action='invalidate', reason=f'cascade_from:{bid}', before=before, after=after, created_at=now)
                invalidated.append(r['belief_id'])
                queue.append(r['belief_id'])
        now = time.time()
        before = row_to_dict(self.conn.execute("SELECT * FROM beliefs WHERE belief_id = ?", (belief_id,)).fetchone())
        self.conn.execute(
            "UPDATE beliefs SET status = 'invalidated', updated_at = ? WHERE belief_id = ?",
            (now, belief_id),
        )
        after = row_to_dict(self.conn.execute("SELECT * FROM beliefs WHERE belief_id = ?", (belief_id,)).fetchone())
        record_revision(self.conn, object_type='belief', object_id=belief_id, action='invalidate', reason='explicit_invalidate_belief', before=before, after=after, created_at=now)
        invalidated.append(belief_id)
        self.conn.commit()
        return invalidated
