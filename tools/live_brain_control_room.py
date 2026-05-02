#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
HERMES_AGENT = Path.home() / '.hermes' / 'hermes-agent'
if HERMES_AGENT.exists() and str(HERMES_AGENT) not in sys.path:
    sys.path.insert(0, str(HERMES_AGENT))

from live_brain.store import LiveBrainStore

APP_NAME = 'Live Brain Control Room'
DEFAULT_LIMIT = 12
LOOPBACK_HOSTS = {'127.0.0.1', 'localhost', '::1'}
MAX_TEXT = 420
SENSITIVE_KEYS = ('token', 'secret', 'api_key', 'apikey', 'authorization', 'password', 'client_secret')


def default_db() -> str:
    hermes_home = os.environ.get('HERMES_HOME', str(Path.home() / '.hermes'))
    return str(Path(hermes_home) / 'live_brain' / 'live_brain.db')


def detect_tailscale_ip() -> str:
    try:
        result = subprocess.run(
            ['tailscale', 'ip', '-4'],
            check=False,
            capture_output=True,
            text=True,
            timeout=2.5,
        )
    except Exception:
        return ''
    for line in (result.stdout or '').splitlines():
        value = line.strip()
        if value.startswith('100.'):
            return value
    return ''


def host_requires_auth(host: str) -> bool:
    return (host or '').strip().lower() not in LOOPBACK_HOSTS


def now() -> float:
    return time.time()


def fmt_ts(value: Any) -> str:
    try:
        if value in (None, '', 0):
            return ''
        return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(float(value)))
    except Exception:
        return str(value or '')


def truncate(value: Any, limit: int = MAX_TEXT) -> str:
    text = '' if value is None else str(value)
    text = text.replace('\x00', '')
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + '…'


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if any(s in str(key).lower() for s in SENSITIVE_KEYS):
                out[key] = '[REDACTED]'
            else:
                out[key] = redact(item)
        return out
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        text = value
        for marker in ('sk-', 'sk-or-v1-'):
            idx = text.find(marker)
            if idx >= 0:
                end = idx + len(marker)
                while end < len(text) and (text[end].isalnum() or text[end] in '_-'):
                    end += 1
                text = text[:idx] + '[REDACTED_SECRET]' + text[end:]
        return text
    return value


def parse_jsonish(raw: Any, default: Any) -> Any:
    if raw in (None, ''):
        return default
    if not isinstance(raw, str):
        return raw
    try:
        return redact(json.loads(raw))
    except Exception:
        return raw


def row_to_dict(row: sqlite3.Row | None) -> Dict[str, Any]:
    if row is None:
        return {}
    result = dict(row)
    for key, value in list(result.items()):
        if key.endswith('_at') or key in {'valid_from', 'valid_to', 'expires_at', 'decided_at', 'last_active_at', 'verified_at'}:
            result[key + '_human'] = fmt_ts(value)
        if key.endswith('_json'):
            result[key[:-5]] = parse_jsonish(value, [] if key.endswith('s_json') else {})
    return redact(result)


class LiveBrainDashboard:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def table_exists(self, conn: sqlite3.Connection, table: str) -> bool:
        row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        return bool(row)

    def count(self, conn: sqlite3.Connection, table: str, where: str = '', params: Iterable[Any] = ()) -> int:
        if not self.table_exists(conn, table):
            return 0
        sql = f'SELECT COUNT(*) c FROM {table}'
        if where:
            sql += ' WHERE ' + where
        try:
            return int(conn.execute(sql, tuple(params)).fetchone()['c'])
        except Exception:
            return 0

    def rows(self, conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
        try:
            return [row_to_dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]
        except Exception:
            return []

    def scalar(self, conn: sqlite3.Connection, sql: str, params: Iterable[Any] = (), default: Any = None) -> Any:
        try:
            row = conn.execute(sql, tuple(params)).fetchone()
            if not row:
                return default
            return row[0]
        except Exception:
            return default

    def overview(self) -> Dict[str, Any]:
        with self.connect() as conn:
            pending = self.count(conn, 'self_evolution_proposals', "status='needs_approval'")
            high_risk = self.count(conn, 'self_evolution_proposals', "status='needs_approval' AND risk_score >= 0.7")
            active_work = self.count(conn, 'work_items', "status IN ('active','blocked')")
            blocked_work = self.count(conn, 'work_items', "status='blocked'")
            verified = self.count(conn, 'verified_artifacts', "status='verified'")
            candidates = self.count(conn, 'verified_artifacts', "status IN ('candidate','missing')")
            open_beliefs = self.count(conn, 'beliefs', "status IN ('open','validated')")
            validated_beliefs = self.count(conn, 'beliefs', "status='validated'")
            active_rules = self.count(conn, 'rules', "status='active'")
            context_total = self.count(conn, 'context_impressions')
            recent_failures = self.count(conn, 'context_impressions', "outcome='failure' AND updated_at > ?", (now() - 86400 * 7,))
            reality_event_count = self.count(conn, 'reality_events')
            active_open_loops = self.count(conn, 'open_loops', "status IN ('active','blocked')")
            danger_zone_count = self.count(conn, 'danger_zones')
            active_constraints = self.count(conn, 'action_constraints', "expires_at IS NULL OR expires_at > ?", (now(),))

            proposals = self.rows(
                conn,
                """
                SELECT * FROM self_evolution_proposals
                WHERE status IN ('needs_approval','proposed','apply_error')
                ORDER BY CASE status WHEN 'needs_approval' THEN 0 ELSE 1 END, risk_score DESC, updated_at DESC
                LIMIT 12
                """,
            )
            work_items = self.rows(
                conn,
                """
                SELECT work_item_id, scope_key, session_id, title, status, priority, next_step, root_cause,
                       evidence_json, supersedes_work_item_id, created_at, updated_at, resolved_at
                FROM work_items
                ORDER BY CASE status WHEN 'blocked' THEN 0 WHEN 'active' THEN 1 WHEN 'resolved' THEN 2 ELSE 3 END,
                         priority DESC, updated_at DESC
                LIMIT 14
                """,
            )
            artifacts = self.rows(
                conn,
                """
                SELECT artifact_id, project_key, role, path, label, status, confidence, source, mime_type, size_bytes,
                       duration_seconds, supersedes_artifact_id, updated_at, verified_at
                FROM verified_artifacts
                ORDER BY CASE status WHEN 'verified' THEN 0 WHEN 'candidate' THEN 1 WHEN 'missing' THEN 2 ELSE 3 END,
                         confidence DESC, updated_at DESC
                LIMIT 18
                """,
            )
            facts = self.rows(
                conn,
                """
                SELECT fact_id, fact_type, fact_text, confidence, source_kind, status, evidence_count, valid_from, valid_to
                FROM facts
                WHERE status='active'
                ORDER BY confidence DESC, evidence_count DESC, valid_from DESC
                LIMIT 10
                """,
            )
            beliefs = self.rows(
                conn,
                """
                SELECT belief_id, claim_text, belief_kind, confidence, status, tool_name, caused_by_work_item_id, created_at, updated_at
                FROM beliefs
                WHERE status IN ('open','validated')
                ORDER BY CASE status WHEN 'validated' THEN 0 ELSE 1 END, confidence DESC, updated_at DESC
                LIMIT 10
                """,
            )
            rules = self.rows(
                conn,
                """
                SELECT rule_id, scope, category, condition_json, action_json, confidence, times_confirmed, specificity, status, updated_at, expires_at
                FROM rules
                WHERE status='active'
                ORDER BY specificity DESC, confidence DESC, times_confirmed DESC, updated_at DESC
                LIMIT 10
                """,
            )
            activations = self.rows(
                conn,
                """
                SELECT activation_id, scope_key, trigger_text, trigger_pattern, tool_used, test_result, artifact_verified,
                       artifact_path, error_type, success, confidence, times_confirmed, updated_at
                FROM causal_activations
                ORDER BY success DESC, times_confirmed DESC, confidence DESC, updated_at DESC
                LIMIT 10
                """,
            )
            recaps = self.rows(
                conn,
                """
                SELECT recap_id, scope_key, task, objective, main_problem, root_cause, current_status, next_step, confidence, updated_at
                FROM canonical_recaps
                ORDER BY updated_at DESC
                LIMIT 6
                """,
            )
            context_impressions = self.rows(
                conn,
                """
                SELECT impression_id, scope_key, session_id, query_text, sections_json, outcome, attribution_mode,
                       feedback_text, created_at, updated_at
                FROM context_impressions
                ORDER BY created_at DESC
                LIMIT 12
                """,
            )
            reality_state = self.rows(
                conn,
                """
                SELECT scope_key, state_key, value_json, confidence, source_event_ids_json, updated_at, expires_at
                FROM reality_state
                ORDER BY updated_at DESC
                LIMIT 16
                """,
            )
            open_loops = self.rows(
                conn,
                """
                SELECT loop_id, scope_key, title, status, priority, next_action, blockers_json, source_event_ids_json, created_at, updated_at, resolved_at
                FROM open_loops
                ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'blocked' THEN 1 WHEN 'resolved' THEN 2 ELSE 3 END, priority DESC, updated_at DESC
                LIMIT 16
                """,
            )
            danger_zones = self.rows(
                conn,
                """
                SELECT danger_id, scope_key, pattern, severity, mitigation, times_triggered, source_event_ids_json, created_at, updated_at, last_triggered_at
                FROM danger_zones
                ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, updated_at DESC
                LIMIT 12
                """,
            )
            action_constraints = self.rows(
                conn,
                """
                SELECT constraint_id, scope_key, action_type, decision, reason, risk_level, ttl_seconds, source_event_ids_json, created_at, updated_at, expires_at
                FROM action_constraints
                ORDER BY CASE decision WHEN 'deny' THEN 0 WHEN 'needs_approval' THEN 1 WHEN 'warn' THEN 2 ELSE 3 END, updated_at DESC
                LIMIT 12
                """,
            )
            timeline = self.timeline(conn, limit=32)

            return {
                'db_path': self.db_path,
                'generated_at': fmt_ts(now()),
                'stats': {
                    'pending_approvals': pending,
                    'high_risk_pending': high_risk,
                    'active_work': active_work,
                    'blocked_work': blocked_work,
                    'verified_artifacts': verified,
                    'candidate_artifacts': candidates,
                    'open_beliefs': open_beliefs,
                    'validated_beliefs': validated_beliefs,
                    'active_rules': active_rules,
                    'context_impressions': context_total,
                    'recent_context_failures': recent_failures,
                    'reality_events': reality_event_count,
                    'active_open_loops': active_open_loops,
                    'danger_zones': danger_zone_count,
                    'action_constraints': active_constraints,
                },
                'autonomy': self.autonomy_state(pending, recent_failures),
                'proposals': proposals,
                'work_items': work_items,
                'artifacts': artifacts,
                'facts': facts,
                'beliefs': beliefs,
                'rules': rules,
                'activations': activations,
                'recaps': recaps,
                'context_impressions': context_impressions,
                'reality_state': reality_state,
                'open_loops': open_loops,
                'danger_zones': danger_zones,
                'action_constraints': action_constraints,
                'timeline': timeline,
            }

    def autonomy_state(self, pending: int, recent_failures: int) -> Dict[str, Any]:
        stages = [
            {'key': 'observe', 'label': 'Observe', 'status': 'on', 'note': 'record turns + tool evidence'},
            {'key': 'learn', 'label': 'Learn', 'status': 'on', 'note': 'facts, beliefs, workflows'},
            {'key': 'propose', 'label': 'Propose', 'status': 'on', 'note': 'self-evolution queue'},
            {'key': 'safe_apply', 'label': 'Safe Apply', 'status': 'guarded', 'note': 'low-risk only; high-risk gated'},
        ]
        headline = 'Gated autonomy online'
        if pending:
            headline = f'{pending} approval gate{"s" if pending != 1 else ""} waiting'
        if recent_failures:
            headline += f' · {recent_failures} recent context failure{"s" if recent_failures != 1 else ""}'
        return {'headline': headline, 'stages': stages}

    def timeline(self, conn: sqlite3.Connection, limit: int = 28) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        for row in conn.execute(
            "SELECT audit_id, object_type, object_id, action, reason, details_json, created_at FROM audit_log ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall():
            item = row_to_dict(row)
            events.append({
                'kind': 'audit',
                'title': f"{item.get('action', '')} · {item.get('object_type', '')}",
                'subtitle': item.get('object_id', ''),
                'body': item.get('reason', ''),
                'at': item.get('created_at', 0),
                'at_human': item.get('created_at_human', ''),
                'payload': item,
            })
        for row in conn.execute(
            "SELECT proposal_id, proposal_type, target_area, status, risk_level, risk_score, trigger_text, updated_at FROM self_evolution_proposals ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall():
            item = row_to_dict(row)
            events.append({
                'kind': 'proposal',
                'title': f"{item.get('status')} · {item.get('proposal_type')} → {item.get('target_area')}",
                'subtitle': item.get('proposal_id', ''),
                'body': f"risk={item.get('risk_level')} ({item.get('risk_score')}) · {truncate(item.get('trigger_text', ''), 160)}",
                'at': item.get('updated_at', 0),
                'at_human': item.get('updated_at_human', ''),
                'payload': item,
            })
        for row in conn.execute(
            "SELECT work_item_id, title, status, next_step, updated_at FROM work_items ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall():
            item = row_to_dict(row)
            events.append({
                'kind': 'work',
                'title': f"{item.get('status')} · work item",
                'subtitle': item.get('work_item_id', ''),
                'body': truncate(item.get('title') or item.get('next_step') or '', 180),
                'at': item.get('updated_at', 0),
                'at_human': item.get('updated_at_human', ''),
                'payload': item,
            })
        for row in conn.execute(
            "SELECT impression_id, query_text, outcome, attribution_mode, created_at FROM context_impressions ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall():
            item = row_to_dict(row)
            events.append({
                'kind': 'context',
                'title': f"context · {item.get('outcome')}",
                'subtitle': item.get('impression_id', ''),
                'body': truncate(item.get('query_text') or item.get('attribution_mode') or '', 180),
                'at': item.get('created_at', 0),
                'at_human': item.get('created_at_human', ''),
                'payload': item,
            })
        if self.table_exists(conn, 'reality_events'):
            for row in conn.execute(
                "SELECT event_id, event_type, subject, signals_json, created_at FROM reality_events ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall():
                item = row_to_dict(row)
                signals = item.get('signals') or []
                events.append({
                    'kind': 'reality',
                    'title': f"reality · {item.get('event_type')}",
                    'subtitle': item.get('event_id', ''),
                    'body': truncate((item.get('subject') or '') + (' · ' + ', '.join(signals[:5]) if signals else ''), 180),
                    'at': item.get('created_at', 0),
                    'at_human': item.get('created_at_human', ''),
                    'payload': item,
                })
        events.sort(key=lambda item: float(item.get('at') or 0), reverse=True)
        return events[:limit]

    def decide(self, proposal_id: str, decision: str, reason: str) -> Dict[str, Any]:
        if decision not in {'approved', 'rejected'}:
            raise ValueError('decision must be approved or rejected')
        store = LiveBrainStore(self.db_path)
        store.initialize_schema()
        try:
            result = store.decide_self_evolution_proposal(proposal_id, decision, reason or f'{decision} from Live Brain Control Room')
            return redact(result)
        finally:
            store.close()

    def context_for_query(self, query: str, session_id: str = '', sender_id: str = '') -> Dict[str, Any]:
        try:
            from live_brain_ctx import _debug_live_brain_context, _extract_scope_key, _load_live_brain_context, _load_reality_brief
        except Exception as exc:
            return {'error': f'live_brain_ctx import failed: {exc}', 'trace': traceback.format_exc(limit=3)}
        old_home = os.environ.get('HERMES_HOME')
        db_path = Path(self.db_path).resolve()
        inferred_home = db_path.parent.parent if db_path.name == 'live_brain.db' else None
        if inferred_home:
            os.environ['HERMES_HOME'] = str(inferred_home)
        try:
            context = _load_live_brain_context(query or '', session_id or '', sender_id or '')
            scope_key = _extract_scope_key(query or '', sender_id or '', session_id or '')
            reality_brief = _load_reality_brief(scope_key, query or '')
            if reality_brief and not (context or '').startswith('LIVE REALITY:'):
                context = (reality_brief + '\n\n' + context) if context else reality_brief
            debug = _debug_live_brain_context(query or '', session_id or '', sender_id or '')
            if reality_brief:
                debug = dict(debug or {})
                debug['reality_brief'] = reality_brief
            return {'query': query, 'context': context, 'debug': redact(debug)}
        finally:
            if old_home is None:
                os.environ.pop('HERMES_HOME', None)
            else:
                os.environ['HERMES_HOME'] = old_home


class ControlRoomHandler(BaseHTTPRequestHandler):
    dashboard: LiveBrainDashboard
    token: str
    require_auth: bool = False

    server_version = 'LiveBrainControlRoom/0.1'

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write('[control-room] ' + fmt % args + '\n')

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == '/':
                if not self.is_authorized(parsed):
                    self.send_html(render_login())
                    return
                self.send_html(render_index(self.token))
            elif parsed.path == '/api/overview':
                if not self.require_authorized(parsed):
                    return
                self.send_json(self.dashboard.overview())
            elif parsed.path == '/api/context':
                if not self.require_authorized(parsed):
                    return
                params = parse_qs(parsed.query)
                query = params.get('q', [''])[0]
                session_id = params.get('session_id', [''])[0]
                sender_id = params.get('sender_id', [''])[0]
                self.send_json(self.dashboard.context_for_query(query, session_id=session_id, sender_id=sender_id))
            elif parsed.path == '/api/health':
                if not self.require_authorized(parsed):
                    return
                self.send_json({'ok': True, 'app': APP_NAME, 'db_path': self.dashboard.db_path, 'time': fmt_ts(now())})
            else:
                self.send_error(HTTPStatus.NOT_FOUND, 'not found')
        except Exception as exc:
            self.send_json({'error': str(exc), 'trace': traceback.format_exc(limit=5)}, status=500)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path != '/api/decide':
                self.send_error(HTTPStatus.NOT_FOUND, 'not found')
                return
            body = self.read_json()
            if not self.is_authorized(parsed, body=body):
                self.send_json({'error': 'invalid token'}, status=403)
                return
            proposal_id = str(body.get('proposal_id') or '').strip()
            decision = str(body.get('decision') or '').strip().lower()
            reason = str(body.get('reason') or '').strip()
            result = self.dashboard.decide(proposal_id, decision, reason)
            self.send_json({'ok': True, 'result': result})
        except Exception as exc:
            self.send_json({'error': str(exc), 'trace': traceback.format_exc(limit=5)}, status=500)

    def request_token(self, parsed=None, body: Optional[Dict[str, Any]] = None) -> str:
        if body and body.get('token'):
            return str(body.get('token') or '')
        header_token = self.headers.get('X-Live-Brain-Token', '').strip()
        if header_token:
            return header_token
        auth = self.headers.get('Authorization', '').strip()
        if auth.lower().startswith('bearer '):
            return auth[7:].strip()
        parsed = parsed or urlparse(self.path)
        return (parse_qs(parsed.query).get('token') or [''])[0]

    def is_authorized(self, parsed=None, body: Optional[Dict[str, Any]] = None) -> bool:
        if not self.require_auth:
            return True
        supplied = self.request_token(parsed, body=body)
        return bool(supplied) and secrets.compare_digest(supplied, self.token)

    def require_authorized(self, parsed=None) -> bool:
        if self.is_authorized(parsed):
            return True
        self.send_json({'error': 'authorization required'}, status=403)
        return False

    def read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get('Content-Length') or '0')
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode('utf-8'))

    def send_html(self, text: str) -> None:
        payload = text.encode('utf-8')
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(payload)

    def send_json(self, data: Any, status: int = 200) -> None:
        payload = json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(payload)


def render_login() -> str:
    return f"""<!doctype html>
<html lang=\"en\">
<head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><title>{APP_NAME} Locked</title>
<style>body{{margin:0;min-height:100vh;display:grid;place-items:center;background:radial-gradient(circle at 20% 0%,rgba(99,231,255,.22),transparent 32rem),#070912;color:#edf4ff;font-family:Inter,system-ui,sans-serif}}.box{{width:min(520px,calc(100vw - 32px));border:1px solid rgba(153,186,255,.22);background:rgba(18,25,48,.82);border-radius:26px;padding:28px;box-shadow:0 24px 80px rgba(0,0,0,.45)}}h1{{margin:0 0 8px;font-size:24px}}p{{color:#93a4c7;line-height:1.5}}input{{width:100%;border:1px solid rgba(153,186,255,.22);background:#070b17;color:#edf4ff;border-radius:14px;padding:12px;margin:8px 0 12px}}button{{border:1px solid rgba(99,231,255,.5);background:rgba(99,231,255,.14);color:#edf4ff;border-radius:14px;padding:10px 13px;font-weight:800;cursor:pointer}}</style></head>
<body><div class=\"box\"><h1>Live Brain Control Room</h1><p>This control surface is locked. Paste the token printed in the server terminal, or open the printed URL containing <code>?token=...</code>.</p><input id=\"t\" placeholder=\"Access token\" autofocus><button onclick=\"location.href='/?token='+encodeURIComponent(document.getElementById('t').value)\">Unlock</button></div></body></html>"""


def render_index(token: str) -> str:
    safe_token = html.escape(token, quote=True)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{APP_NAME}</title>
<style>
:root {{
  color-scheme: dark;
  --bg: #070912;
  --bg2: #090f1f;
  --panel: rgba(18, 25, 48, .74);
  --panel2: rgba(11, 18, 36, .92);
  --line: rgba(153, 186, 255, .16);
  --line2: rgba(153, 186, 255, .28);
  --text: #edf4ff;
  --muted: #93a4c7;
  --faint: #667799;
  --cyan: #63e7ff;
  --blue: #7aa2ff;
  --violet: #af7aff;
  --green: #74f2a7;
  --yellow: #ffd166;
  --red: #ff6b8a;
  --orange: #ff9f43;
  --shadow: 0 24px 80px rgba(0,0,0,.45);
  --radius: 22px;
}}
* {{ box-sizing: border-box; }}
html {{ scroll-behavior: smooth; }}
body {{
  margin: 0;
  background:
    radial-gradient(circle at 18% -10%, rgba(99,231,255,.22), transparent 28rem),
    radial-gradient(circle at 82% 2%, rgba(175,122,255,.18), transparent 30rem),
    linear-gradient(135deg, var(--bg), var(--bg2) 50%, #05070d);
  color: var(--text);
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  min-height: 100vh;
}}
body::before {{
  content: '';
  position: fixed;
  inset: 0;
  pointer-events: none;
  background-image:
    linear-gradient(rgba(255,255,255,.035) 1px, transparent 1px),
    linear-gradient(90deg, rgba(255,255,255,.035) 1px, transparent 1px);
  background-size: 44px 44px;
  mask-image: radial-gradient(circle at 50% 8%, black, transparent 74%);
}}
a {{ color: inherit; }}
.app {{ width: min(1480px, calc(100vw - 32px)); margin: 0 auto; padding: 26px 0 60px; }}
.topbar {{ display:flex; align-items:center; justify-content:space-between; gap:18px; margin-bottom: 18px; }}
.brand {{ display:flex; align-items:center; gap:14px; }}
.logo {{
  width: 46px; height: 46px; border-radius: 15px;
  background: conic-gradient(from 220deg, var(--cyan), var(--violet), var(--blue), var(--green), var(--cyan));
  box-shadow: 0 0 34px rgba(99,231,255,.25);
  position: relative;
}}
.logo::after {{ content:''; position:absolute; inset:8px; border-radius:11px; background:#09101f; border:1px solid rgba(255,255,255,.22); }}
h1 {{ font-size: 18px; margin:0; letter-spacing:.04em; text-transform: uppercase; }}
.sub {{ color: var(--muted); font-size: 13px; margin-top: 3px; }}
.pillrow {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; justify-content:flex-end; }}
.pill {{ border:1px solid var(--line); background:rgba(255,255,255,.045); color:var(--muted); border-radius:999px; padding:8px 11px; font-size:12px; }}
.hero {{
  position: relative;
  overflow: hidden;
  border: 1px solid var(--line);
  background: linear-gradient(135deg, rgba(16,27,55,.88), rgba(10,13,25,.76));
  border-radius: 30px;
  padding: 26px;
  box-shadow: var(--shadow);
}}
.hero::after {{
  content:''; position:absolute; right:-12%; top:-55%; width:62%; height:140%;
  background: radial-gradient(circle, rgba(99,231,255,.16), rgba(175,122,255,.08), transparent 66%);
  transform: rotate(-12deg);
}}
.hero-inner {{ position:relative; z-index:1; display:grid; grid-template-columns: 1.25fr .75fr; gap:24px; align-items:stretch; }}
.headline {{ font-size: clamp(34px, 5vw, 72px); line-height:.94; letter-spacing:-.055em; margin: 6px 0 14px; max-width: 900px; }}
.gradient {{ background: linear-gradient(90deg, var(--cyan), var(--blue), var(--violet)); -webkit-background-clip:text; background-clip:text; color: transparent; }}
.lede {{ color:#c5d4f5; font-size: 16px; line-height:1.55; max-width: 780px; margin: 0 0 18px; }}
.hero-actions {{ display:flex; flex-wrap:wrap; gap:10px; margin-top:20px; }}
button, .button {{
  border: 1px solid var(--line2);
  background: rgba(255,255,255,.08);
  color: var(--text);
  border-radius: 14px;
  padding: 10px 13px;
  cursor: pointer;
  transition: .18s transform ease, .18s background ease, .18s border-color ease;
  font-weight: 700;
}}
button:hover, .button:hover {{ transform: translateY(-1px); border-color: rgba(99,231,255,.55); background: rgba(99,231,255,.12); }}
button.primary {{ background: linear-gradient(135deg, rgba(99,231,255,.28), rgba(122,162,255,.20)); border-color: rgba(99,231,255,.5); }}
button.good {{ background: rgba(116,242,167,.12); border-color: rgba(116,242,167,.45); }}
button.bad {{ background: rgba(255,107,138,.10); border-color: rgba(255,107,138,.42); }}
button.small {{ padding:7px 10px; font-size:12px; border-radius:11px; }}
.status-card {{ border:1px solid var(--line); background:rgba(7,11,23,.54); border-radius:24px; padding:18px; backdrop-filter: blur(18px); }}
.status-title {{ color: var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.14em; }}
.status-headline {{ font-size:24px; font-weight:800; margin:10px 0 14px; }}
.dial {{ display:grid; gap:10px; }}
.dial-step {{ display:flex; gap:10px; align-items:center; padding:10px; border-radius:15px; background:rgba(255,255,255,.04); border:1px solid rgba(255,255,255,.07); }}
.dot {{ width:10px; height:10px; border-radius:999px; background:var(--green); box-shadow:0 0 18px rgba(116,242,167,.8); flex:none; }}
.dot.guarded {{ background:var(--yellow); box-shadow:0 0 18px rgba(255,209,102,.7); }}
.step-label {{ font-weight:800; }}
.step-note {{ color:var(--muted); font-size:12px; margin-top:2px; }}
.nav {{ position:sticky; top:0; z-index:10; display:flex; gap:10px; overflow:auto; padding:14px 0; margin: 12px 0 8px; backdrop-filter: blur(16px); }}
.nav a {{ text-decoration:none; white-space:nowrap; border:1px solid var(--line); background:rgba(7,9,18,.72); color:var(--muted); border-radius:999px; padding:9px 12px; font-size:13px; }}
.nav a:hover {{ color:var(--text); border-color:rgba(99,231,255,.45); }}
.grid-stats {{ display:grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap:12px; margin: 16px 0; }}
.stat {{ border:1px solid var(--line); background:var(--panel); border-radius:20px; padding:15px; min-height:108px; }}
.stat b {{ display:block; font-size:30px; letter-spacing:-.04em; }}
.stat span {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.09em; }}
.stat em {{ display:block; color:var(--faint); font-size:12px; font-style:normal; margin-top:7px; }}
.layout {{ display:grid; grid-template-columns: 1.08fr .92fr; gap:14px; align-items:start; }}
.panel {{ border:1px solid var(--line); background:var(--panel); border-radius:var(--radius); padding:18px; box-shadow: 0 12px 40px rgba(0,0,0,.20); backdrop-filter: blur(18px); margin-bottom:14px; }}
.panel h2 {{ margin:0 0 4px; font-size:18px; letter-spacing:-.02em; display:flex; align-items:center; gap:8px; }}
.panel-desc {{ color:var(--muted); margin:0 0 16px; font-size:13px; }}
.card {{ border:1px solid rgba(255,255,255,.08); background:rgba(7,12,26,.58); border-radius:18px; padding:14px; margin:10px 0; }}
.card-head {{ display:flex; justify-content:space-between; gap:12px; align-items:flex-start; margin-bottom:10px; }}
.title {{ font-weight:850; letter-spacing:-.015em; }}
.mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size:12px; color:#b9c8e8; overflow-wrap:anywhere; }}
.muted {{ color:var(--muted); }}
.faint {{ color:var(--faint); }}
.badge {{ display:inline-flex; align-items:center; gap:6px; border:1px solid rgba(255,255,255,.12); background:rgba(255,255,255,.06); border-radius:999px; padding:5px 8px; color:#dce7ff; font-size:12px; margin:2px 4px 2px 0; }}
.badge.high, .badge.needs_approval, .badge.blocked {{ color:#ffd9e2; border-color:rgba(255,107,138,.38); background:rgba(255,107,138,.10); }}
.badge.medium, .badge.candidate {{ color:#fff0bd; border-color:rgba(255,209,102,.35); background:rgba(255,209,102,.10); }}
.badge.low, .badge.verified, .badge.approved, .badge.active, .badge.validated {{ color:#c8ffdb; border-color:rgba(116,242,167,.35); background:rgba(116,242,167,.10); }}
.badge.rejected, .badge.deprecated {{ color:#cbd5e8; border-color:rgba(147,164,199,.25); background:rgba(147,164,199,.07); }}
.actions {{ display:flex; gap:8px; flex-wrap:wrap; margin-top:12px; }}
input, textarea {{ width:100%; border:1px solid var(--line); background:rgba(4,8,18,.74); color:var(--text); border-radius:14px; padding:10px 12px; outline:none; }}
textarea {{ min-height: 74px; resize:vertical; }}
input:focus, textarea:focus {{ border-color:rgba(99,231,255,.48); box-shadow:0 0 0 3px rgba(99,231,255,.08); }}
.table {{ width:100%; border-collapse: collapse; font-size:13px; }}
.table th {{ text-align:left; color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.09em; font-weight:800; padding:0 8px 8px; }}
.table td {{ border-top:1px solid rgba(255,255,255,.07); padding:10px 8px; vertical-align:top; }}
.timeline {{ position:relative; padding-left:18px; }}
.timeline::before {{ content:''; position:absolute; left:6px; top:8px; bottom:8px; width:1px; background:linear-gradient(var(--cyan), transparent); }}
.event {{ position:relative; padding:0 0 14px 14px; }}
.event::before {{ content:''; position:absolute; left:-16px; top:6px; width:10px; height:10px; border-radius:50%; background:var(--cyan); box-shadow:0 0 18px rgba(99,231,255,.7); }}
.event-title {{ font-weight:800; }}
.event-body {{ color:var(--muted); font-size:13px; margin-top:3px; }}
.ctx {{ white-space:pre-wrap; font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size:12px; line-height:1.5; background:rgba(3,7,15,.8); border:1px solid rgba(255,255,255,.08); border-radius:16px; padding:14px; max-height:520px; overflow:auto; }}
.split {{ display:grid; grid-template-columns: 1fr 1fr; gap:12px; }}
.empty {{ color:var(--muted); border:1px dashed rgba(255,255,255,.18); border-radius:18px; padding:18px; text-align:center; }}
.footer {{ color:var(--faint); text-align:center; margin-top:24px; font-size:12px; }}
@media (max-width: 1100px) {{ .hero-inner, .layout, .split {{ grid-template-columns:1fr; }} .grid-stats {{ grid-template-columns: repeat(3, minmax(0,1fr)); }} }}
@media (max-width: 680px) {{ .app {{ width: min(100vw - 18px, 1480px); }} .grid-stats {{ grid-template-columns: repeat(2, minmax(0,1fr)); }} .hero {{ padding:18px; }} .topbar {{ align-items:flex-start; flex-direction:column; }} }}
</style>
</head>
<body>
<div class="app">
  <div class="topbar">
    <div class="brand">
      <div class="logo"></div>
      <div><h1>Live Brain Control Room</h1><div class="sub">Operational memory · safety gates · provenance · context flight recorder</div></div>
    </div>
    <div class="pillrow"><span class="pill" id="dbPill">local SQLite</span><span class="pill" id="timePill">loading…</span><button class="small" onclick="refreshAll()">Refresh</button></div>
  </div>

  <section class="hero">
    <div class="hero-inner">
      <div>
        <div class="pill">Beyond vector memory</div>
        <div class="headline">Semantic memory remembers text.<br><span class="gradient">Live Brain maintains operational truth.</span></div>
        <p class="lede">Every memory has provenance. Every context injection has a reason. Every self-change has a risk score, approval trail, and rollback surface.</p>
        <div class="hero-actions"><a class="button primary" href="#approvals">Review Gates</a><a class="button" href="#context">Inspect Context</a><a class="button" href="#timeline">Open Flight Recorder</a></div>
      </div>
      <div class="status-card">
        <div class="status-title">Autonomy State</div>
        <div class="status-headline" id="autonomyHeadline">Loading…</div>
        <div class="dial" id="autonomyDial"></div>
      </div>
    </div>
  </section>

  <nav class="nav"><a href="#overview">Overview</a><a href="#reality">Reality</a><a href="#approvals">Approvals</a><a href="#work">Work Graph</a><a href="#memory">Beliefs</a><a href="#artifacts">Artifacts</a><a href="#rules">Rules</a><a href="#context">Why Context?</a><a href="#timeline">Timeline</a></nav>

  <section id="overview" class="grid-stats"></section>

  <div class="layout">
    <main>
      <section id="reality" class="panel"><h2>🌐 What Live Brain Thinks Is Going On</h2><p class="panel-desc">Persistent situational awareness: current objective, open loops, danger zones, and action constraints derived from events.</p><div id="realityList"></div></section>
      <section id="approvals" class="panel"><h2>⚡ Approval Gates</h2><p class="panel-desc">High-risk self-evolution waits here. Low-risk bounded cleanup may auto-apply; code/config/schema stays gated.</p><div id="proposalList"></div></section>
      <section id="work" class="panel"><h2>🧭 Work Graph</h2><p class="panel-desc">Active, blocked, resolved, and superseded tasks extracted from real sessions.</p><div id="workList"></div></section>
      <section id="memory" class="panel"><h2>🧠 Operational Beliefs</h2><p class="panel-desc">Validated facts, open hypotheses, and causal/workflow activations — not raw transcript soup.</p><div class="split"><div id="factsList"></div><div id="beliefsList"></div></div><div id="activationsList"></div></section>
      <section id="artifacts" class="panel"><h2>📦 Verified Artifacts</h2><p class="panel-desc">Project files with explicit roles and statuses, so old/candidate/rejected outputs do not get sent by accident.</p><div id="artifactList"></div></section>
    </main>
    <aside>
      <section id="context" class="panel"><h2>🔎 Why This Context?</h2><p class="panel-desc">Compile the exact Live Brain context for a query and inspect what sections were injected.</p><input id="contextQuery" placeholder="e.g. koji su enoch fajlovi" value="Show pending approvals"><div class="actions"><button class="primary" onclick="inspectContext()">Compile Context</button><button onclick="document.getElementById('contextQuery').value=''">Clear</button></div><div id="contextOutput" class="ctx" style="margin-top:12px;">No query compiled yet.</div></section>
      <section id="rules" class="panel"><h2>📜 Binding Rules</h2><p class="panel-desc">Durable constraints and learned rules currently affecting retrieval/context.</p><div id="rulesList"></div></section>
      <section class="panel"><h2>🧾 Context Impressions</h2><p class="panel-desc">Recent prompt injections and feedback attribution.</p><div id="impressionList"></div></section>
      <section id="timeline" class="panel"><h2>🛫 Flight Recorder</h2><p class="panel-desc">Audit events, proposals, work changes, and context impressions in one stream.</p><div id="timelineList"></div></section>
    </aside>
  </div>
  <div class="footer">Local-only control surface · bind to 127.0.0.1 unless you know exactly what you are doing.</div>
</div>
<script>
const TOKEN = "{safe_token}";
let DATA = null;
const $ = (id) => document.getElementById(id);
function esc(value) {{ return String(value ?? '').replace(/[&<>'"]/g, ch => ({{'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}}[ch])); }}
function short(value, n=180) {{ const s = String(value ?? ''); return s.length > n ? s.slice(0, n-1) + '…' : s; }}
function cls(value) {{ return String(value ?? '').toLowerCase().replace(/[^a-z0-9_-]+/g, '_'); }}
function badge(value, extra='') {{ return `<span class="badge ${{cls(value)}} ${{extra}}">${{esc(value || '—')}}</span>`; }}
function mono(value) {{ return `<span class="mono">${{esc(value || '')}}</span>`; }}
function jsonBlock(value) {{ if (!value || (Array.isArray(value) && !value.length) || (typeof value === 'object' && !Object.keys(value).length)) return ''; return `<div class="ctx" style="max-height:190px;margin-top:8px;">${{esc(JSON.stringify(value, null, 2))}}</div>`; }}
async function api(path, opts={{}}) {{ opts.headers = Object.assign({{'X-Live-Brain-Token': TOKEN}}, opts.headers || {{}}); const res = await fetch(path, opts); const json = await res.json(); if(!res.ok || json.error) throw new Error(json.error || res.statusText); return json; }}
async function refreshAll() {{
  try {{
    DATA = await api('/api/overview');
    render(DATA);
  }} catch (err) {{ console.error(err); alert(err.message); }}
}}
function render(data) {{
  $('dbPill').textContent = data.db_path;
  $('timePill').textContent = data.generated_at;
  renderAutonomy(data.autonomy);
  renderStats(data.stats);
  renderReality(data);
  renderProposals(data.proposals || []);
  renderWork(data.work_items || []);
  renderArtifacts(data.artifacts || []);
  renderMemory(data);
  renderRules(data.rules || []);
  renderImpressions(data.context_impressions || []);
  renderTimeline(data.timeline || []);
}}
function renderAutonomy(auto) {{
  $('autonomyHeadline').textContent = auto?.headline || 'Unknown';
  $('autonomyDial').innerHTML = (auto?.stages || []).map(s => `<div class="dial-step"><span class="dot ${{s.status === 'guarded' ? 'guarded' : ''}}"></span><div><div class="step-label">${{esc(s.label)}}</div><div class="step-note">${{esc(s.note)}}</div></div></div>`).join('');
}}
function renderStats(stats) {{
  const items = [
    ['pending_approvals','Pending Gates','self-evolution decisions'], ['high_risk_pending','High Risk','requires approval'],
    ['active_work','Active Work','open/blocked items'], ['verified_artifacts','Verified Files','safe to use'],
    ['open_beliefs','Beliefs','open + validated'], ['active_rules','Active Rules','binding constraints'],
    ['context_impressions','Context Runs','flight recorder'], ['recent_context_failures','Failures 7d','learning signals'],
  ];
  $('overview').innerHTML = items.map(([key,label,sub]) => `<div class="stat"><span>${{esc(label)}}</span><b>${{esc(stats[key] ?? 0)}}</b><em>${{esc(sub)}}</em></div>`).join('');
}}
function renderReality(data) {{
  const states = data.reality_state || [], loops = data.open_loops || [], dangers = data.danger_zones || [], constraints = data.action_constraints || [];
  const stateCards = states.slice(0, 5).map(s => `<div class="card"><div>${{badge(s.state_key)}}<span class="badge">${{esc(s.confidence)}}</span></div>${{jsonBlock(s.value || s.value_json)}}<div class="faint">${{esc(s.scope_key || '')}} · ${{esc(s.updated_at_human || '')}}</div></div>`).join('');
  const loopCards = loops.slice(0, 5).map(l => `<div class="card"><div class="card-head"><div><div class="title">${{esc(l.title)}}</div><div class="mono">${{esc(l.loop_id)}}</div></div><div>${{badge(l.status)}}<span class="badge">p=${{esc(l.priority)}}</span></div></div><div class="muted">Next: ${{esc(short(l.next_action || '—', 180))}}</div><div class="faint">${{(l.blockers || []).map(b => badge(b)).join('')}} ${{esc(l.updated_at_human || '')}}</div></div>`).join('');
  const dangerCards = dangers.slice(0, 4).map(d => `<div class="card"><div>${{badge(d.severity)}}<span class="badge">${{esc(d.times_triggered)}}x</span></div><div class="title">${{esc(d.pattern)}}</div><div class="muted">${{esc(short(d.mitigation || '', 180))}}</div></div>`).join('');
  const constraintCards = constraints.slice(0, 4).map(c => `<div class="card"><div>${{badge(c.decision)}}${{badge(c.action_type)}}<span class="badge">${{esc(c.risk_level)}}</span></div><div class="muted">${{esc(short(c.reason || '', 200))}}</div></div>`).join('');
  $('realityList').innerHTML = (stateCards || loopCards || dangerCards || constraintCards)
    ? `<h3>Reality State</h3>${{stateCards || '<div class="empty">No reality state yet.</div>'}}<h3>Open Loops</h3>${{loopCards || '<div class="empty">No active loops.</div>'}}<h3>Danger Zones</h3>${{dangerCards || '<div class="empty">No danger zones.</div>'}}<h3>Action Constraints</h3>${{constraintCards || '<div class="empty">No active constraints.</div>'}}`
    : '<div class="empty">No reality events yet. Send a few messages or tool results to activate situational awareness.</div>';
}}
function renderProposals(rows) {{
  if (!rows.length) {{ $('proposalList').innerHTML = '<div class="empty">No active approval gates. The runway is clear.</div>'; return; }}
  $('proposalList').innerHTML = rows.map(p => `<div class="card">
    <div class="card-head"><div><div class="title">${{esc(p.proposal_type)}} → ${{esc(p.target_area)}}</div><div class="mono">${{esc(p.proposal_id)}}</div></div><div>${{badge(p.status)}}${{badge(p.risk_level)}}<span class="badge">risk ${{esc(p.risk_score)}}</span></div></div>
    <div class="muted">${{esc(short(p.rationale, 260))}}</div>
    <p>${{esc(short(p.proposed_action, 320))}}</p>
    <div>${{(p.suggested_tests || []).map(t => badge(t)).join('')}}</div>
    ${{jsonBlock(p.evidence)}}
    ${{p.status === 'needs_approval' ? `<div class="actions"><input id="reason_${{esc(p.proposal_id)}}" placeholder="Decision reason (optional)"><button class="good" onclick="decide('${{esc(p.proposal_id)}}','approved')">Approve</button><button class="bad" onclick="decide('${{esc(p.proposal_id)}}','rejected')">Reject</button></div>` : ''}}
  </div>`).join('');
}}
async function decide(id, decision) {{
  const input = document.getElementById('reason_' + id);
  const reason = input?.value || `${{decision}} from Live Brain Control Room`;
  if (!confirm(`${{decision.toUpperCase()}} ${{id}}?`)) return;
  await api('/api/decide', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{token:TOKEN, proposal_id:id, decision, reason}})}});
  await refreshAll();
}}
function renderWork(rows) {{
  if (!rows.length) {{ $('workList').innerHTML = '<div class="empty">No work items yet.</div>'; return; }}
  $('workList').innerHTML = rows.map(w => `<div class="card"><div class="card-head"><div><div class="title">${{esc(w.title)}}</div><div class="mono">${{esc(w.work_item_id)}}</div></div><div>${{badge(w.status)}}<span class="badge">p=${{esc(w.priority)}}</span></div></div><div class="muted">Next: ${{esc(short(w.next_step || '—', 180))}}</div><div class="faint">Root: ${{esc(short(w.root_cause || '—', 180))}}</div><div class="faint">Updated: ${{esc(w.updated_at_human || '')}}</div></div>`).join('');
}}
function renderArtifacts(rows) {{
  if (!rows.length) {{ $('artifactList').innerHTML = '<div class="empty">No verified artifacts recorded.</div>'; return; }}
  $('artifactList').innerHTML = `<table class="table"><thead><tr><th>Project</th><th>Role</th><th>Status</th><th>Path</th><th>Signal</th></tr></thead><tbody>${{rows.map(a => `<tr><td>${{esc(a.project_key)}}</td><td>${{esc(a.role)}}</td><td>${{badge(a.status)}}</td><td>${{mono(short(a.path, 120))}}</td><td class="muted">conf=${{esc(a.confidence)}} · ${{esc(a.source || '')}}<br>${{esc(a.updated_at_human || '')}}</td></tr>`).join('')}}</tbody></table>`;
}}
function renderMemory(data) {{
  const facts = data.facts || [], beliefs = data.beliefs || [], acts = data.activations || [];
  $('factsList').innerHTML = `<h3>Validated facts</h3>` + (facts.length ? facts.map(f => `<div class="card"><div>${{badge(f.fact_type)}}${{badge(f.status)}}<span class="badge">${{esc(f.confidence)}}</span></div><p>${{esc(short(f.fact_text, 220))}}</p></div>`).join('') : '<div class="empty">No active facts.</div>');
  $('beliefsList').innerHTML = `<h3>Beliefs / hypotheses</h3>` + (beliefs.length ? beliefs.map(b => `<div class="card"><div>${{badge(b.status)}}${{badge(b.belief_kind)}}<span class="badge">${{esc(b.confidence)}}</span></div><p>${{esc(short(b.claim_text, 240))}}</p><div class="faint">${{esc(b.tool_name || b.updated_at_human || '')}}</div></div>`).join('') : '<div class="empty">No open beliefs.</div>');
  $('activationsList').innerHTML = `<h3>Causal / workflow activations</h3>` + (acts.length ? acts.map(a => `<div class="card"><div class="card-head"><div><div class="title">${{esc(a.tool_used || 'tool')}}</div><div class="muted">${{esc(short(a.trigger_pattern || a.trigger_text, 150))}}</div></div><div>${{badge(a.success ? 'success' : 'failed')}}<span class="badge">${{esc(a.times_confirmed)}}x</span></div></div><div class="faint">${{esc(a.test_result || '')}} ${{a.artifact_path ? '· ' + esc(short(a.artifact_path, 90)) : ''}}</div></div>`).join('') : '<div class="empty">No causal activations.</div>');
}}
function renderRules(rows) {{
  $('rulesList').innerHTML = rows.length ? rows.map(r => `<div class="card"><div>${{badge(r.category)}}${{badge(r.scope)}}<span class="badge">${{esc(r.confidence)}}</span></div><div class="muted">confirmed=${{esc(r.times_confirmed)}} · specificity=${{esc(r.specificity)}} · ${{esc(r.updated_at_human || '')}}</div>${{jsonBlock(r.action || r.action_json)}}${{jsonBlock(r.condition || r.condition_json)}}</div>`).join('') : '<div class="empty">No active rules.</div>';
}}
function renderImpressions(rows) {{
  $('impressionList').innerHTML = rows.length ? rows.map(i => `<div class="card"><div>${{badge(i.outcome)}}${{badge(i.attribution_mode || 'compiler')}}</div><p>${{esc(short(i.query_text, 160))}}</p><div class="muted">${{(i.sections || []).map(s => badge(s)).join('')}} ${{esc(i.created_at_human || '')}}</div></div>`).join('') : '<div class="empty">No context impressions.</div>';
}}
function renderTimeline(rows) {{
  $('timelineList').innerHTML = rows.length ? `<div class="timeline">${{rows.map(e => `<div class="event"><div class="event-title">${{esc(e.title)}}</div><div class="mono">${{esc(e.subtitle || '')}}</div><div class="event-body">${{esc(short(e.body || '', 210))}}</div><div class="faint">${{esc(e.at_human || '')}}</div></div>`).join('')}}</div>` : '<div class="empty">No timeline events.</div>';
}}
async function inspectContext() {{
  const q = $('contextQuery').value;
  $('contextOutput').textContent = 'Compiling…';
  try {{
    const data = await api('/api/context?q=' + encodeURIComponent(q));
    if (data.error) throw new Error(data.error);
    const sections = data.debug?.sections ? `Sections: ${{data.debug.sections.join(', ')}}\n\n` : '';
    $('contextOutput').textContent = sections + (data.context || '<EMPTY>');
  }} catch (err) {{ $('contextOutput').textContent = err.message; }}
}}
refreshAll();
setInterval(refreshAll, 15000);
</script>
</body>
</html>"""


def run_server(db_path: str, host: str, port: int, *, require_auth: bool = False, token: str = '') -> None:
    dashboard = LiveBrainDashboard(db_path)
    token = token or secrets.token_urlsafe(24)

    class Handler(ControlRoomHandler):
        pass

    Handler.dashboard = dashboard
    Handler.token = token
    Handler.require_auth = require_auth
    server = ThreadingHTTPServer((host, port), Handler)
    base_url = f'http://{host}:{port}/'
    print(f'{APP_NAME} running: {base_url}')
    if require_auth:
        print(f'Access URL: {base_url}?token={token}')
    print(f'DB: {db_path}')
    print('Bind is local by default; use --tailscale for tailnet access with token auth.')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nShutting down.')
    finally:
        server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description='Run the Live Brain Control Room dashboard.')
    parser.add_argument('--db', default=default_db(), help='Path to live_brain.db')
    parser.add_argument('--host', default='127.0.0.1', help='Bind host. Default: 127.0.0.1')
    parser.add_argument('--port', type=int, default=8765, help='Bind port. Default: 8765')
    parser.add_argument('--tailscale', action='store_true', help='Auto-detect Tailscale IPv4, bind to it, and require token auth.')
    parser.add_argument('--auth-token', default=os.environ.get('LIVE_BRAIN_CONTROL_ROOM_TOKEN', ''), help='Require this token for UI/API access. Defaults to env LIVE_BRAIN_CONTROL_ROOM_TOKEN.')
    parser.add_argument('--no-auth', action='store_true', help='Disable UI/API auth even on non-loopback binds. Not recommended.')
    parser.add_argument('--check', action='store_true', help='Print JSON overview and exit.')
    args = parser.parse_args()

    if args.tailscale:
        tailscale_ip = detect_tailscale_ip()
        if not tailscale_ip:
            raise SystemExit('Could not detect Tailscale IPv4. Check `tailscale status` / `tailscale up`, or pass --host <tailscale-ip>.')
        args.host = tailscale_ip

    db_path = str(Path(args.db).expanduser())
    if not Path(db_path).exists():
        raise SystemExit(f'Live Brain DB not found: {db_path}')

    if args.check:
        print(json.dumps(LiveBrainDashboard(db_path).overview(), indent=2, ensure_ascii=False))
        return 0

    require_auth = (not args.no_auth) and (bool(args.auth_token) or args.tailscale or host_requires_auth(args.host))
    if host_requires_auth(args.host) and args.no_auth:
        print('WARNING: non-loopback dashboard is running without auth. This exposes private memory data.', file=sys.stderr)
    run_server(db_path, args.host, args.port, require_auth=require_auth, token=args.auth_token)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
