from __future__ import annotations

import logging
import time
from typing import Any, List

logger = logging.getLogger(__name__)


class MaintenanceManager:
    def __init__(self, conn, epistemic_mgr, reality_engine, evolution_mgr):
        self.conn = conn
        self.epistemic_mgr = epistemic_mgr
        self.reality_engine = reality_engine
        self.evolution_mgr = evolution_mgr

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

