from __future__ import annotations

import json
import time
from typing import Any, Dict, List

from .audit import record_revision, row_to_dict
from .utils import stable_id


def _dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)


def _loads(value: Any, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


class TurnTraceManager:
    """Replay-grade trace storage for routing, context, tool, and response decisions."""

    def __init__(self, conn):
        self.conn = conn

    def upsert_trace(
        self,
        *,
        scope_key: str,
        session_id: str,
        trace_key: str,
        turn_kind: str,
        user_message: str = '',
        assistant_response: str = '',
        intent: str = '',
        routing_summary: Dict[str, Any] | None = None,
        context_sections: List[str] | None = None,
        trace_data: Dict[str, Any] | None = None,
        created_at: float | None = None,
    ) -> str:
        now = float(created_at or time.time())
        trace_id = stable_id('turn_trace', scope_key, session_id, trace_key)
        before = row_to_dict(self.conn.execute("SELECT * FROM turn_traces WHERE trace_id=?", (trace_id,)).fetchone())
        self.conn.execute(
            """
            INSERT OR REPLACE INTO turn_traces
            (trace_id, scope_key, session_id, turn_kind, user_message, assistant_response, intent,
             routing_summary_json, context_sections_json, trace_data_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM turn_traces WHERE trace_id=?), ?), ?)
            """,
            (
                trace_id,
                scope_key,
                session_id,
                turn_kind,
                user_message[:4000],
                assistant_response[:4000],
                intent[:120],
                _dumps(routing_summary or {}),
                _dumps(context_sections or []),
                _dumps(trace_data or {}),
                trace_id,
                now,
                now,
            ),
        )
        after = row_to_dict(self.conn.execute("SELECT * FROM turn_traces WHERE trace_id=?", (trace_id,)).fetchone())
        record_revision(
            self.conn,
            object_type='turn_trace',
            object_id=trace_id,
            action='upsert',
            reason=turn_kind,
            before=before,
            after=after,
            created_at=now,
        )
        return trace_id

    def append_tool_event(
        self,
        *,
        scope_key: str,
        session_id: str,
        user_message: str,
        tool_name: str,
        args: Dict[str, Any],
        result_text: str,
        success: bool,
        duration_ms: int = 0,
        created_at: float | None = None,
    ) -> str:
        now = float(created_at or time.time())
        trace_key = f"tool:{tool_name}:{int(now)}"
        return self.upsert_trace(
            scope_key=scope_key,
            session_id=session_id,
            trace_key=trace_key,
            turn_kind='tool',
            user_message=user_message,
            intent='tool_result',
            routing_summary={
                'source': 'post_tool_call',
                'tool_name': tool_name,
                'success': bool(success),
                'resolution_tier': 'tool_execution',
            },
            context_sections=[],
            trace_data={
                'tool_name': tool_name,
                'args': args,
                'result_preview': result_text[:4000],
                'success': bool(success),
                'duration_ms': int(duration_ms or 0),
            },
            created_at=now,
        )

    def latest_for_session(self, session_id: str, *, limit: int = 10) -> List[dict]:
        rows = self.conn.execute(
            "SELECT * FROM turn_traces WHERE session_id=? ORDER BY created_at DESC LIMIT ?",
            (session_id, int(limit)),
        ).fetchall()
        return [self._row_to_debug(dict(row)) for row in rows]

    def debug(self, scope_key: str, query: str = '', *, session_id: str = '', limit: int = 10) -> Dict[str, Any]:
        if session_id:
            rows = self.conn.execute(
                "SELECT * FROM turn_traces WHERE session_id=? ORDER BY created_at DESC LIMIT ?",
                (session_id, int(limit)),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM turn_traces WHERE scope_key=? ORDER BY created_at DESC LIMIT ?",
                (scope_key, int(limit)),
            ).fetchall()
        parsed = [self._row_to_debug(dict(row)) for row in rows]
        return {
            'scope_key': scope_key,
            'session_id': session_id,
            'query': query,
            'traces': parsed,
            'summary': [self._summary_line(dict(row)) for row in rows],
            'timeline': self._timeline(parsed),
        }

    def _row_to_debug(self, row: Dict[str, Any]) -> Dict[str, Any]:
        row['routing_summary'] = _loads(row.get('routing_summary_json'), {})
        row['context_sections'] = _loads(row.get('context_sections_json'), [])
        row['trace_data'] = _loads(row.get('trace_data_json'), {})
        return row

    def _summary_line(self, row: Dict[str, Any]) -> str:
        parsed = self._row_to_debug(row)
        turn_kind = parsed.get('turn_kind') or 'unknown'
        intent = parsed.get('intent') or ''
        routing = parsed.get('routing_summary') or {}
        chosen = routing.get('chosen_tier') or routing.get('resolution_tier') or ''
        sections = parsed.get('context_sections') or []
        user_message = str(parsed.get('user_message') or '')[:90]
        return f"{turn_kind} intent={intent} tier={chosen} sections={len(sections)} user={user_message}"

    def _timeline(self, traces: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        timeline = []
        for trace in traces:
            routing = trace.get('routing_summary') or {}
            trace_data = trace.get('trace_data') or {}
            timeline.append({
                'turn_kind': trace.get('turn_kind'),
                'intent': trace.get('intent'),
                'tier': routing.get('chosen_tier') or routing.get('resolution_tier') or '',
                'sections': trace.get('context_sections') or [],
                'section_decisions': trace_data.get('section_decisions') or [],
                'tool_name': trace_data.get('tool_name') or '',
                'success': trace_data.get('success'),
                'user_message': str(trace.get('user_message') or '')[:240],
                'assistant_response': str(trace.get('assistant_response') or '')[:320],
                'result_preview': str(trace_data.get('result_preview') or '')[:320],
            })
        return timeline
