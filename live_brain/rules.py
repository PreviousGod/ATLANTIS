from __future__ import annotations

import json
import time
from typing import Any, Dict, List

from .utils import stable_id
from .audit import record_revision, row_to_dict
from .scopes import extract_scope_tags, scope_matches, specificity, tags_from_json, tags_to_json



class RuleEngine:
    def __init__(self, conn):
        self.conn = conn

    def upsert_rule(self, scope: str, category: str, condition: Dict[str, Any], action: Dict[str, Any], confidence: float = 0.8, source: str = 'derived', times_confirmed: int = 1, scope_tags: Dict[str, Any] | None = None, ttl_days: float | None = None) -> dict:
        now = time.time()
        if scope_tags is None:
            scope_tags = extract_scope_tags(json.dumps(condition), json.dumps(action), scope_key=scope if scope.startswith('agent:') else '')
        permanent = source in {'user_binding', 'user_correction'} or any(str(action.get(k, '')).lower().find(marker) >= 0 for k in ('instruction', 'reason') for marker in ['always', 'never', 'nikad', 'uvek', 'uvijek', 'zapamti'])
        expires_at = None if permanent else now + ((ttl_days if ttl_days is not None else 7.0) * 86400)
        rule_specificity = specificity(scope_tags)
        rule_key = json.dumps({'scope': scope, 'category': category, 'condition': condition, 'action': action}, sort_keys=True)
        rule_id = stable_id("rule", rule_key)
        before = row_to_dict(self.conn.execute("SELECT * FROM rules WHERE rule_id = ?", (rule_id,)).fetchone())
        row = self.conn.execute("SELECT times_confirmed, confidence FROM rules WHERE rule_id = ?", (rule_id,)).fetchone()
        if row:
            times_confirmed = max(times_confirmed, int(row[0]) + 1)
            confidence = max(confidence, float(row[1]))
        self.conn.execute(
            "INSERT OR REPLACE INTO rules (rule_id, scope, category, scope_tags_json, condition_json, action_json, confidence, source, times_confirmed, status, expires_at, specificity, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, COALESCE((SELECT created_at FROM rules WHERE rule_id = ?), ?), ?)",
            (rule_id, scope, category, tags_to_json(scope_tags), json.dumps(condition, sort_keys=True), json.dumps(action, sort_keys=True), confidence, source, times_confirmed, expires_at, rule_specificity, rule_id, now, now),
        )
        after = row_to_dict(self.conn.execute("SELECT * FROM rules WHERE rule_id = ?", (rule_id,)).fetchone())
        record_revision(self.conn, object_type='rule', object_id=rule_id, action='upsert', reason=source or 'rule_engine', before=before, after=after, created_at=now)
        superseded = self._resolve_conflicts(rule_id, scope, category, condition, action, scope_tags, confidence, rule_specificity, now)
        self.conn.commit()
        return {
            'rule_id': rule_id,
            'scope': scope,
            'category': category,
            'condition': condition,
            'action': action,
            'confidence': confidence,
            'times_confirmed': times_confirmed,
            'scope_tags': scope_tags,
            'expires_at': expires_at,
            'specificity': rule_specificity,
            'superseded_conflicts': superseded,
        }

    def get_active_rules(self, category: str | None = None) -> List[Dict[str, Any]]:
        if category:
            rows = self.conn.execute("SELECT * FROM rules WHERE status = 'active' AND category = ? ORDER BY specificity DESC, confidence DESC, times_confirmed DESC, updated_at DESC", (category,)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM rules WHERE status = 'active' ORDER BY specificity DESC, confidence DESC, times_confirmed DESC, updated_at DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d['condition'] = json.loads(d.pop('condition_json'))
            d['action'] = json.loads(d.pop('action_json'))
            out.append(d)
        return out

    def _resolve_conflicts(self, rule_id: str, scope: str, category: str, condition: Dict[str, Any], action: Dict[str, Any], scope_tags: Dict[str, Any], confidence: float, rule_specificity: int, now: float) -> int:
        if self._is_generic_condition(condition):
            return 0
        condition_json = json.dumps(condition, sort_keys=True)
        action_json = json.dumps(action, sort_keys=True)
        rows = self.conn.execute(
            "SELECT rule_id, action_json, scope_tags_json, confidence, specificity, updated_at FROM rules WHERE status='active' AND rule_id != ? AND scope = ? AND category = ? AND condition_json = ?",
            (rule_id, scope, category, condition_json),
        ).fetchall()
        superseded = 0
        for row in rows:
            if row['action_json'] == action_json:
                continue
            other_tags = tags_from_json(row['scope_tags_json'])
            if other_tags and scope_tags and not (scope_matches(other_tags, scope_tags) or scope_matches(scope_tags, other_tags)):
                continue
            older_or_weaker = rule_specificity > int(row['specificity'] or 0) or (
                rule_specificity == int(row['specificity'] or 0) and confidence >= float(row['confidence'] or 0)
            )
            if older_or_weaker:
                before = row_to_dict(self.conn.execute("SELECT * FROM rules WHERE rule_id=?", (row['rule_id'],)).fetchone())
                self.conn.execute(
                    "UPDATE rules SET status='superseded', updated_at=? WHERE rule_id=?",
                    (now, row['rule_id']),
                )
                after = row_to_dict(self.conn.execute("SELECT * FROM rules WHERE rule_id=?", (row['rule_id'],)).fetchone())
                record_revision(self.conn, object_type='rule', object_id=row['rule_id'], action='supersede', reason=f'conflicts_with:{rule_id}', before=before, after=after, created_at=now)
                superseded += 1
        return superseded

    def _is_generic_condition(self, condition: Dict[str, Any]) -> bool:
        return condition in ({'trigger': 'always', 'context': 'any'}, {'trigger': 'correction', 'context': 'any'})

    def derive_binding_constraint_from_turn(self, user_text: str, session_id: str, scope_key: str) -> List[dict]:
        derived = []
        u = (user_text or '').strip()
        if not u or len(u) < 5:
            return derived
        lowered = u.lower()
        if self._is_test_or_system_note(lowered):
            return derived
        binding_patterns = [
            'uvijek', 'uvek', 'nikad', 'nikada', 'obavezno', 'ne smiješ', 'ne smes',
            'svaki put', 'always', 'never', "don't", 'do not', 'never ever',
            'you must', 'moras', 'moraš',
            'ne brisi', 'ne menjaj', 'ne mijenjaj', 'ne dodaj', 'ne uklanjaj',
        ]
        if not any(p in lowered for p in binding_patterns):
            return derived
        derived.append(self.upsert_rule(
            scope='user_binding',
            category='binding_constraint',
            condition={'trigger': 'always', 'context': 'any'},
            action={'type': 'enforce', 'instruction': u[:300], 'reason': 'Explicit user binding instruction'},
            confidence=0.99,
            source='user_binding',
            times_confirmed=1,
            scope_tags=extract_scope_tags(u, scope_key=scope_key),
        ))
        return derived

    def derive_correction_constraint_from_turn(self, user_text: str, session_id: str, scope_key: str) -> List[dict]:
        derived = []
        u = (user_text or '').strip()
        if not u or len(u) < 8:
            return derived
        lowered = u.lower()
        if self._is_test_or_system_note(lowered):
            return derived
        correction_patterns = [
            'riješili smo', 'resili smo', 'vec smo', 'već smo', 'zaboravio si',
            'rekao sam ti', 'rekla sam ti', 'vec si to', 'već si to',
            'we already', 'we fixed', 'you forgot', 'i told you', 'remember we',
            'pa smo', 'pa si', 'juče smo', 'juce smo', 'prošli put',
            'zasto ponavljas', 'zašto ponavljaš', 'opet si', 'opet iste',
            'why are you repeating', 'you did this again', 'same mistake',
            'sto ponavljas', 'što ponavljaš', 'opet radis', 'opet radiš',
        ]
        if not any(p in lowered for p in correction_patterns):
            return derived
        derived.append(self.upsert_rule(
            scope='user_correction',
            category='learned_fact',
            condition={'trigger': 'always', 'context': 'any'},
            action={'type': 'remember', 'instruction': u[:300], 'reason': 'User corrected agent — this was already solved/known'},
            confidence=0.95,
            source='auto_correction',
            times_confirmed=1,
            scope_tags=extract_scope_tags(u, scope_key=scope_key),
        ))
        return derived

    def _is_test_or_system_note(self, lowered_text: str) -> bool:
        return (
            lowered_text.startswith('[note: model was just switched')
            or lowered_text.startswith('[system note:')
            or lowered_text.startswith('runtime test')
            or 'runtime test' in lowered_text[:300]
        )
