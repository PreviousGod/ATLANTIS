from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List

from .utils import stable_id
from .audit import record_revision, row_to_dict


HIGH_RISK_TARGETS = {'code', 'config', 'db_schema', 'filesystem', 'credentials', 'messaging', 'network'}
LOW_RISK_TARGETS = {'recipe', 'context', 'memory_metadata', 'artifact_metadata'}
HIGH_RISK_TYPES = {'code_patch', 'schema_migration', 'file_delete', 'credential_change', 'send_media', 'config_change'}
LOW_RISK_AUTO_TYPES = {'demote_fix_recipe'}
HIGH_RISK_TERMS = re.compile(
    r'\b(delete|remove|rm\s+-|unlink|drop\s+table|alter\s+table|migration|schema|credential|secret|token|api[_ -]?key|send\s+media|send_message|chmod|chown|sudo|curl\s+\|)\b',
    re.IGNORECASE,
)


class SelfEvolutionManager:
    def __init__(self, conn):
        self.conn = conn

    def score_risk(self, proposal_type: str, target_area: str, proposed_action: str, evidence: Dict[str, Any] | None = None) -> Dict[str, Any]:
        proposal_type = (proposal_type or '').strip().lower()
        target_area = (target_area or '').strip().lower()
        proposed_action = proposed_action or ''
        evidence = evidence or {}
        reasons: List[str] = []
        score = 0.25

        if proposal_type in HIGH_RISK_TYPES:
            score += 0.6
            reasons.append('high_risk_proposal_type')
        if target_area in HIGH_RISK_TARGETS:
            score += 0.5
            reasons.append('high_risk_target_area')
        if HIGH_RISK_TERMS.search(proposed_action):
            score += 0.35
            reasons.append('dangerous_action_terms')
        if target_area in LOW_RISK_TARGETS:
            score -= 0.1
            reasons.append('metadata_only_target')
        if proposal_type in LOW_RISK_AUTO_TYPES and isinstance(evidence.get('recipe_ids'), list):
            score -= 0.15
            reasons.append('bounded_recipe_ids')
        if evidence.get('requires_code_change'):
            score += 0.45
            reasons.append('requires_code_change')

        score = max(0.0, min(1.0, score))
        if score >= 0.7:
            risk_level = 'high'
        elif score >= 0.4:
            risk_level = 'medium'
        else:
            risk_level = 'low'
        auto_apply_allowed = risk_level == 'low' and proposal_type in LOW_RISK_AUTO_TYPES
        requires_approval = not auto_apply_allowed
        return {
            'risk_level': risk_level,
            'risk_score': round(score, 3),
            'requires_approval': requires_approval,
            'auto_apply_allowed': auto_apply_allowed,
            'reasons': reasons,
        }

    def propose(
        self,
        *,
        scope_key: str,
        session_id: str = '',
        trigger_text: str,
        proposal_type: str,
        target_area: str,
        rationale: str,
        proposed_action: str,
        evidence: Dict[str, Any] | None = None,
        suggested_tests: List[str] | None = None,
        auto_apply: bool = False,
    ) -> Dict[str, Any]:
        evidence = evidence or {}
        suggested_tests = suggested_tests or []
        risk = self.score_risk(proposal_type, target_area, proposed_action, evidence)
        now = time.time()
        proposal_id = stable_id('self_evolution', scope_key, proposal_type, target_area, trigger_text, proposed_action)
        before = row_to_dict(self.conn.execute("SELECT * FROM self_evolution_proposals WHERE proposal_id=?", (proposal_id,)).fetchone())
        status = 'needs_approval' if risk['requires_approval'] else 'proposed'
        apply_result: Dict[str, Any] = {}
        if auto_apply and risk['auto_apply_allowed']:
            apply_result = self._apply_low_risk(proposal_id, proposal_type, evidence, now)
            status = 'auto_applied' if apply_result.get('status') == 'applied' else 'apply_error'

        self.conn.execute(
            """
            INSERT INTO self_evolution_proposals (
                proposal_id, scope_key, session_id, trigger_text, proposal_type, target_area,
                rationale, proposed_action, evidence_json, suggested_tests_json,
                risk_level, risk_score, status, auto_apply_allowed, requires_approval,
                apply_result_json, created_at, updated_at, decided_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(proposal_id) DO UPDATE SET
                session_id=excluded.session_id,
                rationale=excluded.rationale,
                evidence_json=excluded.evidence_json,
                suggested_tests_json=excluded.suggested_tests_json,
                risk_level=excluded.risk_level,
                risk_score=excluded.risk_score,
                status=CASE
                    WHEN self_evolution_proposals.status IN ('auto_applied','approved','rejected') THEN self_evolution_proposals.status
                    ELSE excluded.status
                END,
                auto_apply_allowed=excluded.auto_apply_allowed,
                requires_approval=excluded.requires_approval,
                apply_result_json=CASE
                    WHEN excluded.apply_result_json != '{}' THEN excluded.apply_result_json
                    ELSE self_evolution_proposals.apply_result_json
                END,
                updated_at=excluded.updated_at
            """,
            (
                proposal_id,
                scope_key,
                session_id,
                (trigger_text or '')[:500],
                (proposal_type or '')[:80],
                (target_area or '')[:80],
                (rationale or '')[:800],
                (proposed_action or '')[:1000],
                json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                json.dumps(suggested_tests, ensure_ascii=False),
                risk['risk_level'],
                risk['risk_score'],
                status,
                1 if risk['auto_apply_allowed'] else 0,
                1 if risk['requires_approval'] else 0,
                json.dumps(apply_result, ensure_ascii=False, sort_keys=True),
                now,
                now,
            ),
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO audit_log (audit_id, object_type, object_id, action, reason, details_json, created_at) VALUES (?, 'self_evolution_proposal', ?, ?, ?, ?, ?)",
            (
                stable_id('audit', proposal_id, status, str(int(now))),
                proposal_id,
                status,
                'gated_self_evolution',
                json.dumps({'risk': risk, 'apply_result': apply_result}, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )
        after = row_to_dict(self.conn.execute("SELECT * FROM self_evolution_proposals WHERE proposal_id=?", (proposal_id,)).fetchone())
        record_revision(self.conn, object_type='self_evolution_proposal', object_id=proposal_id, action=status, reason='gated_self_evolution', before=before, after=after, created_at=now)
        self.conn.commit()
        return self.get(proposal_id) or {'proposal_id': proposal_id, 'status': status, **risk}

    def _apply_low_risk(self, proposal_id: str, proposal_type: str, evidence: Dict[str, Any], now: float) -> Dict[str, Any]:
        if proposal_type != 'demote_fix_recipe':
            return {'status': 'skipped', 'reason': 'unsupported_low_risk_type'}
        recipe_ids = [str(item) for item in evidence.get('recipe_ids', []) if str(item).strip()]
        if not recipe_ids:
            return {'status': 'skipped', 'reason': 'no_recipe_ids'}
        placeholders = ','.join('?' for _ in recipe_ids)
        before_rows = {row['recipe_id']: row_to_dict(row) for row in self.conn.execute(f"SELECT * FROM fix_recipes WHERE recipe_id IN ({placeholders})", recipe_ids).fetchall()}
        cur = self.conn.execute(
            f"UPDATE fix_recipes SET status='needs_review', promotion_status='needs_review', last_reviewed_at=?, confidence=MAX(confidence - 0.2, 0.1), updated_at=? WHERE recipe_id IN ({placeholders})",
            [now, now] + recipe_ids,
        )
        for recipe_id in recipe_ids:
            after = row_to_dict(self.conn.execute("SELECT * FROM fix_recipes WHERE recipe_id=?", (recipe_id,)).fetchone())
            record_revision(self.conn, object_type='fix_recipe', object_id=recipe_id, action='self_evolution_demote', reason=f'proposal:{proposal_id}', before=before_rows.get(recipe_id, {}), after=after, created_at=now)
        self.conn.execute(
            "INSERT OR REPLACE INTO audit_log (audit_id, object_type, object_id, action, reason, details_json, created_at) VALUES (?, 'fix_recipe', ?, 'self_evolution_demote', 'feedback_failure', ?, ?)",
            (
                stable_id('audit', proposal_id, 'demote_fix_recipe', str(int(now))),
                ','.join(recipe_ids)[:240],
                json.dumps({'proposal_id': proposal_id, 'recipe_ids': recipe_ids, 'updated': cur.rowcount}, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )
        return {'status': 'applied', 'updated_recipes': cur.rowcount, 'recipe_ids': recipe_ids}

    def get(self, proposal_id: str) -> Dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM self_evolution_proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
        return self._row_to_dict(row) if row else None

    def list(self, *, status: str = '', include_applied: bool = False, limit: int = 10) -> List[Dict[str, Any]]:
        params: List[Any] = []
        where = []
        if status:
            where.append('status=?')
            params.append(status)
        elif not include_applied:
            where.append("status NOT IN ('auto_applied','approved','rejected','expired')")
        sql = 'SELECT * FROM self_evolution_proposals'
        if where:
            sql += ' WHERE ' + ' AND '.join(where)
        sql += ' ORDER BY updated_at DESC LIMIT ?'
        params.append(max(1, min(int(limit or 10), 50)))
        return [self._row_to_dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def decide(self, proposal_id: str, decision: str, reason: str = '') -> Dict[str, Any]:
        decision = (decision or '').strip().lower()
        if decision not in {'approved', 'rejected'}:
            return {'error': 'decision must be approved or rejected'}
        proposal_id = (proposal_id or '').strip()
        if not proposal_id:
            row = self.conn.execute(
                "SELECT proposal_id FROM self_evolution_proposals WHERE status='needs_approval' ORDER BY risk_score DESC, updated_at DESC LIMIT 1"
            ).fetchone()
            if not row:
                return {'error': 'no pending self-evolution proposal to decide'}
            proposal_id = row['proposal_id']
        now = time.time()
        before = row_to_dict(self.conn.execute("SELECT * FROM self_evolution_proposals WHERE proposal_id=?", (proposal_id,)).fetchone())
        self.conn.execute(
            "UPDATE self_evolution_proposals SET status=?, decided_at=?, updated_at=? WHERE proposal_id=?",
            (decision, now, now, proposal_id),
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO audit_log (audit_id, object_type, object_id, action, reason, details_json, created_at) VALUES (?, 'self_evolution_proposal', ?, ?, ?, '{}', ?)",
            (stable_id('audit', proposal_id, decision, str(int(now))), proposal_id, decision, reason[:240], now),
        )
        after = row_to_dict(self.conn.execute("SELECT * FROM self_evolution_proposals WHERE proposal_id=?", (proposal_id,)).fetchone())
        record_revision(self.conn, object_type='self_evolution_proposal', object_id=proposal_id, action=decision, reason=reason[:240], before=before, after=after, created_at=now)
        self.conn.commit()
        return self.get(proposal_id) or {'proposal_id': proposal_id, 'status': decision}

    def _row_to_dict(self, row) -> Dict[str, Any]:
        data = dict(row)
        for key in ('evidence_json', 'suggested_tests_json', 'apply_result_json'):
            try:
                data[key.replace('_json', '')] = json.loads(data.get(key) or '{}')
            except Exception:
                data[key.replace('_json', '')] = [] if key == 'suggested_tests_json' else {}
        return data
