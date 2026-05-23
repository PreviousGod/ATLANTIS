from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, Iterable, List

from .audit import record_revision, row_to_dict
from .utils import stable_id


def _dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)


def _loads(value: Any, default: Any) -> Any:
    if not value:
        return default


def _is_low_signal_incident_text(text: str) -> bool:
    lowered = (text or '').strip().lower()
    return lowered in {
        'gotovo znaci', 'ok super', 'dobro', 'znaci', 'ok znaci',
        'cao', 'e cao', 'ok', 'okej', 'hmm', 'hm',
    }


def _is_transient_execution_failure(tool_name: str, result_text: str) -> bool:
    lowered = f"{tool_name} {(result_text or '')}".lower()
    transient_markers = (
        'http error 404',
        'apt-get: command not found',
        'you cannot perform this operation unless you are root',
        'target not found',
        'failed to connect',
        'cannot connect to camofox',
        'duckduckgo (ddgs) is a search-only backend',
        'unknown command line argument',
        'traceback',
    )
    if tool_name in {'execute_code', 'terminal', 'browser_navigate', 'web_extract'}:
        return any(marker in lowered for marker in transient_markers)
    return False
    try:
        return json.loads(value)
    except Exception:
        return default


class IncidentTruthManager:
    """Compiled truth layer for recurring operational/coding incidents."""

    def __init__(self, conn):
        self.conn = conn

    def _issue_key(self, scope_key: str, user_text: str, tool_name: str, error_type: str, artifact_path: str = '') -> str:
        task = self._task_key(user_text)
        file_key = artifact_path.rsplit('/', 1)[-1][:80] if artifact_path else ''
        return stable_id('incident', scope_key, task, tool_name or '', error_type or '', file_key)

    def _task_key(self, text: str) -> str:
        lowered = (text or '').lower()
        tokens = [token for token in re.findall(r'[\w./-]+', lowered) if len(token) > 3]
        stop = {'what', 'which', 'that', 'this', 'with', 'from', 'have', 'your', 'about', 'please', 'find', 'show', 'tell'}
        tokens = [token for token in tokens if token not in stop]
        return ' '.join(tokens[:8]) or lowered[:80]

    def _pick_entities(self, args_template: Dict[str, Any], artifact_path: str = '') -> tuple[list[str], list[str], list[str]]:
        paths = [str(path) for path in (args_template.get('paths') or []) if isinstance(path, str)]
        if artifact_path:
            paths.insert(0, artifact_path)
        files = []
        services = []
        entities = []
        for path in paths[:8]:
            if path not in files:
                files.append(path)
                entities.append(path)
        tool_name = str(args_template.get('tool') or '')
        if tool_name:
            services.append(tool_name)
            entities.append(tool_name)
        return entities[:12], files[:8], services[:6]

    def _match_score(self, row: Dict[str, Any], *, task: str, tool_name: str, artifact_path: str) -> int:
        score = 0
        issue_key = str(row.get('issue_key') or '')
        related_artifacts = str(row.get('related_artifacts_json') or '')
        related_services = str(row.get('related_services_json') or '')
        related_files = str(row.get('related_files_json') or '')
        if task and task in issue_key:
            score += 3
        if artifact_path and artifact_path in related_artifacts:
            score += 3
        if artifact_path and artifact_path in related_files:
            score += 2
        if tool_name and tool_name in related_services:
            score += 1
        return score

    def _summary(self, user_text: str, tool_name: str, error_type: str, result_text: str) -> str:
        task = self._task_key(user_text)
        if error_type:
            return f"{task}: last failure via {tool_name} ({error_type})"
        return f"{task}: operational incident through {tool_name}"

    def _next_action(self, tool_name: str, error_type: str, artifact_path: str = '') -> str:
        if error_type == 'auth':
            return 'Verify credentials, auth path, and provider/account access before retrying.'
        if error_type == 'not_found':
            return 'Verify the path, file ownership, and whether the target was created in the expected location.'
        if error_type == 'transient':
            return 'Retry the exact failing action after confirming network/provider health and rate limits.'
        if artifact_path:
            return f'Inspect and verify {artifact_path} before attempting a broader fix.'
        return f'Replay the last failing {tool_name} call with exact inputs and inspect the result before guessing.'

    def record_failure(
        self,
        *,
        scope_key: str,
        session_id: str,
        user_text: str,
        tool_name: str,
        args_template: Dict[str, Any],
        error_type: str,
        result_text: str,
        artifact_path: str = '',
        created_at: float | None = None,
    ) -> Dict[str, Any]:
        if _is_low_signal_incident_text(user_text):
            return {}
        if _is_transient_execution_failure(tool_name, result_text):
            return {}
        now = float(created_at or time.time())
        issue_key = self._issue_key(scope_key, user_text, tool_name, error_type, artifact_path)
        incident_id = stable_id('incident_truth', scope_key, issue_key)
        before = row_to_dict(self.conn.execute("SELECT * FROM incident_truths WHERE incident_id=?", (incident_id,)).fetchone())
        existing = before or {}
        affected_entities, related_files, related_services = self._pick_entities(args_template, artifact_path)
        supporting = list(_loads(existing.get('supporting_evidence_json'), []))
        supporting.append({
            'kind': 'tool_failure',
            'tool_name': tool_name,
            'error_type': error_type,
            'result_preview': (result_text or '')[:240],
            'created_at': now,
        })
        supporting = supporting[-10:]
        confidence = min(0.95, max(float(existing.get('confidence') or 0.45), 0.55) + 0.05)
        title = existing.get('title') or self._summary(user_text, tool_name, error_type, result_text)
        self.conn.execute(
            """
            INSERT OR REPLACE INTO incident_truths
            (incident_id, scope_key, session_id, issue_key, title, status, diagnosis_summary, confidence,
             affected_entities_json, related_files_json, related_services_json, related_artifacts_json,
             recommended_next_action, supporting_evidence_json, contradicting_evidence_json,
             last_verified_at, last_invalidated_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM incident_truths WHERE incident_id=?), ?), ?)
            """,
            (
                incident_id,
                scope_key,
                session_id,
                issue_key,
                title[:220],
                'active',
                self._summary(user_text, tool_name, error_type, result_text)[:500],
                confidence,
                _dumps(affected_entities),
                _dumps(related_files),
                _dumps(related_services),
                _dumps([artifact_path] if artifact_path else []),
                self._next_action(tool_name, error_type, artifact_path)[:500],
                _dumps(supporting),
                existing.get('contradicting_evidence_json') or '[]',
                existing.get('last_verified_at'),
                existing.get('last_invalidated_at'),
                incident_id,
                now,
                now,
            ),
        )
        after = row_to_dict(self.conn.execute("SELECT * FROM incident_truths WHERE incident_id=?", (incident_id,)).fetchone())
        record_revision(
            self.conn,
            object_type='incident_truth',
            object_id=incident_id,
            action='upsert',
            reason='tool_failure',
            before=before,
            after=after,
            created_at=now,
        )
        return after

    def record_success(
        self,
        *,
        scope_key: str,
        session_id: str,
        user_text: str,
        tool_name: str,
        args_template: Dict[str, Any],
        result_text: str,
        artifact_path: str = '',
        created_at: float | None = None,
    ) -> Dict[str, Any] | None:
        if _is_low_signal_incident_text(user_text):
            return None
        now = float(created_at or time.time())
        candidate_rows = self.conn.execute(
            "SELECT * FROM incident_truths WHERE scope_key=? AND status='active' ORDER BY updated_at DESC LIMIT 12",
            (scope_key,),
        ).fetchall()
        task = self._task_key(user_text)
        target = None
        best_score = 0
        for row in candidate_rows:
            score = self._match_score(dict(row), task=task, tool_name=tool_name, artifact_path=artifact_path)
            if score > best_score:
                best_score = score
                target = row
        if not target or best_score <= 0:
            return None
        incident_id = str(target['incident_id'])
        before = row_to_dict(target)
        supporting = _loads(target['supporting_evidence_json'], [])
        if not isinstance(supporting, list):
            supporting = []
        supporting.append({
            'kind': 'verified_success',
            'tool_name': tool_name,
            'result_preview': (result_text or '')[:240],
            'created_at': now,
        })
        self.conn.execute(
            """
            UPDATE incident_truths
            SET status='verified',
                confidence=MIN(confidence + 0.08, 0.99),
                last_verified_at=?,
                recommended_next_action='If this issue recurs, start from the last verified fix path before broader diagnosis.',
                supporting_evidence_json=?,
                updated_at=?
            WHERE incident_id=?
            """,
            (now, _dumps(supporting[-10:]), now, incident_id),
        )
        after = row_to_dict(self.conn.execute("SELECT * FROM incident_truths WHERE incident_id=?", (incident_id,)).fetchone())
        record_revision(
            self.conn,
            object_type='incident_truth',
            object_id=incident_id,
            action='verify',
            reason='tool_success',
            before=before,
            after=after,
            created_at=now,
        )
        return after

    def context_lines_for_query(self, scope_key: str, query_text: str, *, limit: int = 2) -> List[str]:
        query_lower = (query_text or '').lower()
        if not query_lower:
            return []
        rows = self.conn.execute(
            "SELECT * FROM incident_truths WHERE scope_key=? AND status IN ('active','verified') ORDER BY updated_at DESC LIMIT 12",
            (scope_key,),
        ).fetchall()
        lines: List[str] = []
        for row in rows:
            blob = ' '.join(
                [
                    str(row['title'] or ''),
                    str(row['diagnosis_summary'] or ''),
                    str(row['recommended_next_action'] or ''),
                    str(row['related_files_json'] or ''),
                    str(row['related_services_json'] or ''),
                ]
            ).lower()
            if not any(token in blob for token in re.findall(r'[\w./-]+', query_lower) if len(token) > 3):
                continue
            lines.append(
                f"{str(row['title'] or '')[:90]} | diagnosis={str(row['diagnosis_summary'] or '')[:140]} | next={str(row['recommended_next_action'] or '')[:120]}"
            )
            if len(lines) >= limit:
                break
        return lines

    def debug(self, scope_key: str, query: str = '') -> Dict[str, Any]:
        rows = self.conn.execute(
            "SELECT * FROM incident_truths WHERE scope_key=? ORDER BY updated_at DESC LIMIT 20",
            (scope_key,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            for key in (
                'affected_entities_json',
                'related_files_json',
                'related_services_json',
                'related_artifacts_json',
                'supporting_evidence_json',
                'contradicting_evidence_json',
            ):
                item[key[:-5]] = _loads(item.get(key), [])
            result.append(item)
        return {
            'scope_key': scope_key,
            'query': query,
            'matches': self.context_lines_for_query(scope_key, query, limit=5),
            'incidents': result,
        }
