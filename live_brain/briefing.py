from __future__ import annotations

import json
import time
from typing import Any, Dict, List
from .utils import stable_id



class CompressionManager:
    def __init__(self, conn):
        self.conn = conn

    def update_canonical_recap(self, session_id: str, scope_key: str, state: Dict[str, Any], updated_at: float) -> None:
        task = (state.get('current_thread') or '')[:300]
        if not task:
            return
        lowered_task = task.lower()
        if any(x in lowered_task for x in ['sumarizuj', 'summarize', '[system note:', '[context compaction', 'test ping', 'zdravo']):
            return

        objective = ''
        nba = state.get('next_best_actions') or []
        if nba:
            objective = nba[0][:300]
        elif task:
            objective = task[:300]

        main_problem = (state.get('latest_failed_experiment') or '')[:500]
        root_cause = ''
        ruled_out_causes = ''
        current_status = 'unknown'
        next_step = nba[0][:300] if nba else ''
        confidence = 0.6

        row_cause = self.conn.execute(
            "SELECT claim_text FROM beliefs WHERE scope_key = ? AND status = 'validated' AND belief_kind = 'validated_cause' ORDER BY updated_at DESC LIMIT 1",
            (scope_key,),
        ).fetchone()
        if row_cause:
            root_cause = row_cause[0][:500]
            confidence = 0.85

        ruled_rows = self.conn.execute(
            "SELECT claim_text FROM beliefs WHERE scope_key = ? AND belief_kind = 'ruled_out_cause' AND status = 'validated' ORDER BY updated_at DESC LIMIT 3",
            (scope_key,),
        ).fetchall()
        if ruled_rows:
            ruled_out_causes = ' | '.join(r[0][:160] for r in ruled_rows)

        if state.get('latest_successful_experiment'):
            current_status = 'partially resolved' if state.get('latest_failed_experiment') else 'resolved'
        elif state.get('latest_failed_experiment'):
            current_status = 'blocked'

        what_changed_parts = []
        if state.get('latest_successful_experiment'):
            what_changed_parts.append(state['latest_successful_experiment'])
        if state.get('validated_facts'):
            what_changed_parts.extend(state['validated_facts'][:2])
        what_changed = ' | '.join(what_changed_parts)[:500]

        recap_id = f'recap:{session_id}'
        self.conn.execute(
            "INSERT OR REPLACE INTO canonical_recaps (recap_id, session_id, scope_key, task, objective, main_problem, root_cause, ruled_out_causes, what_changed, current_status, next_step, confidence, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM canonical_recaps WHERE recap_id = ?), ?), ?)",
            (recap_id, session_id, scope_key, task, objective, main_problem, root_cause, ruled_out_causes, what_changed, current_status, next_step, confidence, recap_id, updated_at, updated_at),
        )
        self.conn.commit()

    def preserve_from_messages(self, scope_key: str, messages: List[Dict[str, Any]]) -> str:
        state = self._load_state(scope_key)
        unresolved_goal = self._latest_user_goal(messages)
        latest_success = self._latest_message_snippet(messages, role='assistant', contains_any=['works', 'fixed', 'resolved', 'radi', 'uspješno'])
        latest_failure = self._latest_message_snippet(messages, role='assistant', contains_any=['error', 'problem', 'fails', 'ne mogu', 'cannot'])

        if unresolved_goal:
            state['current_thread'] = unresolved_goal[:160]
        if latest_success:
            state['latest_successful_experiment'] = latest_success[:240]
        if latest_failure:
            state['latest_failed_experiment'] = latest_failure[:240]

        self.conn.execute(
            "INSERT OR REPLACE INTO work_state (scope_key, scope_type, state_json, updated_at) VALUES (?, ?, ?, ?)",
            (scope_key, 'session', json.dumps(state), time.time()),
        )
        self.conn.commit()

        hints = [
            'Preserve active goal and unresolved thread.',
            'Preserve exact files/tools involved.',
            'Preserve latest successful experiment and latest failed experiment.',
            'Preserve validated causes separately from open hypotheses and ruled-out causes.',
        ]
        return ' '.join(hints)

    def finalize_session(self, session_id: str, scope_key: str, ended_at: float) -> None:
        self.conn.execute(
            "UPDATE sessions SET ended_at = ? WHERE session_id = ?",
            (ended_at, session_id),
        )
        row = self.conn.execute(
            "SELECT state_json FROM work_state WHERE scope_key = ?",
            (scope_key,),
        ).fetchone()
        state = json.loads(row[0]) if row and row[0] else {}
        summary_parts = []
        if state.get('current_thread'):
            summary_parts.append(state['current_thread'])
        if state.get('latest_successful_experiment'):
            summary_parts.append(f"latest success: {state['latest_successful_experiment']}")
        if state.get('latest_failed_experiment'):
            summary_parts.append(f"latest failure: {state['latest_failed_experiment']}")
        if state.get('validated_facts'):
            summary_parts.append(f"facts: {', '.join(state['validated_facts'][:2])}")
        if state.get('open_hypotheses'):
            summary_parts.append(f"hypotheses: {', '.join(state['open_hypotheses'][:2])}")
        summary = ' | '.join(summary_parts)[:500]
        self.conn.execute(
            "UPDATE episodes SET current_summary = ?, status = CASE WHEN status = 'active' THEN 'dormant' ELSE status END, updated_at = ? WHERE episode_id IN (SELECT et.episode_id FROM episode_turns et JOIN turns t ON t.id = et.turn_id WHERE t.session_id = ?)",
            (summary, ended_at, session_id),
        )

        # Canonical recap for recap-style queries
        self.update_canonical_recap(session_id, scope_key, state, ended_at)
        self.crystallise_from_work_item(scope_key, ended_at)

    def crystallise_from_work_item(self, scope_key: str, now: float) -> None:
        rows = self.conn.execute(
            "SELECT work_item_id, title, root_cause, next_step FROM work_items WHERE scope_key = ? AND status = 'resolved' AND root_cause != '' ORDER BY updated_at DESC LIMIT 10",
            (scope_key,),
        ).fetchall()
        for row in rows:
            if not row['root_cause']:
                continue
            principle = f"When working on '{row['title'][:60]}': root cause was '{row['root_cause'][:120]}'. Next: {row['next_step'][:80]}" if row['next_step'] else f"Root cause for '{row['title'][:60]}': {row['root_cause'][:160]}"
            knowledge_id = stable_id('knowledge', scope_key, row['work_item_id'])
            self.conn.execute(
                "INSERT OR IGNORE INTO crystallised_knowledge (id, scope_key, principle_text, source_work_item_id, confidence, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (knowledge_id, scope_key, principle[:400], row['work_item_id'], 0.85, now),
            )
        self.conn.commit()

    def _load_state(self, scope_key: str) -> Dict[str, Any]:
        row = self.conn.execute(
            "SELECT state_json FROM work_state WHERE scope_key = ?",
            (scope_key,),
        ).fetchone()
        if not row:
            return {}
        try:
            return json.loads(row[0])
        except Exception:
            return {}

    def _latest_user_goal(self, messages: List[Dict[str, Any]]) -> str:
        for msg in reversed(messages):
            if msg.get('role') != 'user':
                continue
            content = msg.get('content', '')
            if isinstance(content, list):
                content = ' '.join(part.get('text', '') for part in content if isinstance(part, dict))
            content = str(content).strip()
            if len(content) >= 5:
                return content
        return ''

    def _latest_message_snippet(self, messages: List[Dict[str, Any]], role: str, contains_any: List[str]) -> str:
        contains_any = [c.lower() for c in contains_any]
        for msg in reversed(messages):
            if msg.get('role') != role:
                continue
            content = msg.get('content', '')
            if isinstance(content, list):
                content = ' '.join(part.get('text', '') for part in content if isinstance(part, dict))
            content = str(content).strip()
            lower = content.lower()
            if any(c in lower for c in contains_any):
                return content
        return ''
