from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from .utils import stable_id

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS reality_events (
    event_id TEXT PRIMARY KEY,
    scope_key TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    signals_json TEXT NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL DEFAULT 0.7,
    source TEXT NOT NULL DEFAULT 'reality_engine',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reality_events_scope ON reality_events(scope_key, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reality_events_type ON reality_events(event_type, created_at DESC);

CREATE TABLE IF NOT EXISTS reality_state (
    scope_key TEXT NOT NULL,
    state_key TEXT NOT NULL,
    value_json TEXT NOT NULL DEFAULT '{}',
    confidence REAL NOT NULL DEFAULT 0.7,
    source_event_ids_json TEXT NOT NULL DEFAULT '[]',
    updated_at REAL NOT NULL,
    expires_at REAL,
    PRIMARY KEY (scope_key, state_key)
);
CREATE INDEX IF NOT EXISTS idx_reality_state_scope ON reality_state(scope_key, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_reality_state_expiry ON reality_state(expires_at);

CREATE TABLE IF NOT EXISTS open_loops (
    loop_id TEXT PRIMARY KEY,
    scope_key TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    priority REAL NOT NULL DEFAULT 0.5,
    next_action TEXT NOT NULL DEFAULT '',
    blockers_json TEXT NOT NULL DEFAULT '[]',
    source_event_ids_json TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    resolved_at REAL
);
CREATE INDEX IF NOT EXISTS idx_open_loops_scope ON open_loops(scope_key, status, priority DESC, updated_at DESC);

CREATE TABLE IF NOT EXISTS danger_zones (
    danger_id TEXT PRIMARY KEY,
    scope_key TEXT NOT NULL,
    pattern TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'medium',
    mitigation TEXT NOT NULL DEFAULT '',
    times_triggered INTEGER NOT NULL DEFAULT 1,
    source_event_ids_json TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    last_triggered_at REAL
);
CREATE INDEX IF NOT EXISTS idx_danger_zones_scope ON danger_zones(scope_key, severity, updated_at DESC);

CREATE TABLE IF NOT EXISTS action_constraints (
    constraint_id TEXT PRIMARY KEY,
    scope_key TEXT NOT NULL,
    action_type TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    risk_level TEXT NOT NULL DEFAULT 'medium',
    ttl_seconds INTEGER,
    source_event_ids_json TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    expires_at REAL
);
CREATE INDEX IF NOT EXISTS idx_action_constraints_scope ON action_constraints(scope_key, action_type, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_action_constraints_expiry ON action_constraints(expires_at);

CREATE TABLE IF NOT EXISTS attention_triggers (
    trigger_id TEXT PRIMARY KEY,
    scope_key TEXT NOT NULL,
    trigger_type TEXT NOT NULL,
    condition_json TEXT NOT NULL DEFAULT '{}',
    response_policy TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attention_triggers_scope ON attention_triggers(scope_key, enabled, updated_at DESC);

CREATE TABLE IF NOT EXISTS reality_reductions (
    reduction_id TEXT PRIMARY KEY,
    source_event_id TEXT NOT NULL,
    target_table TEXT NOT NULL,
    target_key TEXT NOT NULL,
    operation TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reality_reductions_event ON reality_reductions(source_event_id);
CREATE INDEX IF NOT EXISTS idx_reality_reductions_target ON reality_reductions(target_table, target_key, created_at DESC);
"""

_SIGNAL_PATTERNS = [
    ('request_link', re.compile(r'\b(a\s+link\??|link\??|url|dashboard\s+link|daj\s+link|posalji\s+link|pošalji\s+link)\b', re.I)),
    ('request_demo', re.compile(r'\b(demo|teaser|video)\b.*\b(treba|posalj|pošalj|send|napravi|gotov|gotovo)\b|\b(treba|posalj|pošalj|send|napravi)\b.*\b(demo|teaser|video)\b', re.I)),
    ('request_telegram_delivery', re.compile(r'\b(telegram|telwgram|telegrm)\b.*\b(posalj|pošalj|send|preko)\b|\b(posalj|pošalj|send)\b.*\b(telegram|telwgram|telegrm)\b', re.I)),
    ('implement_change', re.compile(r'\b(implementiraj|napravi|dodaj|uradi|realizuj|popravi|fix|patch)\b', re.I)),
    ('request_dashboard', re.compile(r'\b(dashboard|control\s*room|tailscale|100\.\d+\.\d+\.\d+)\b', re.I)),
    ('request_approval', re.compile(r'\b(approval|approve|odobri|odobrenj|pending|self[- ]?evol)\b', re.I)),
    ('short_reference', re.compile(r'^\s*(to|ovo|taj|ta|a link\??|hmm\??|ok|ajde|uradi to)\s*$', re.I)),
    ('auth_friction', re.compile(r'\b(token\s+(nece|neće|ne radi|nece|won\'?t|fails?)|auth\s+problem|login\s+problem)\b', re.I)),
    ('approval_visibility_problem', re.compile(r'\b(ne\s+vidim\s+approval|approval\s+.*ne\s+vidim|kako\s+.*approval|pa\s+kako\s+.*dam\s+approval)\b', re.I)),
    ('last_session_required', re.compile(r'\b(last\s+session|poslednj[aeu]\s+sesij|nije\s+pronasao|nije\s+pronašao|pogledaj\s+last)\b', re.I)),
    ('interrupt_preference', re.compile(r'\b(ne\s+na\s+svaku\s+poruku|samo\s+kad\s+treba|only\s+when\s+needed)\b', re.I)),
    ('quality_pushback', re.compile(r'\b(nije\s+revolucionarno|mozemo\s+bolje|možemo\s+bolje|zar\s+ne\s+mozemo|zar\s+ne\s+možemo|sta\s+mu\s+fali)\b', re.I)),
    ('youtube_monetization', re.compile(r'\b(youtube\s+shorts?|shorts?|pregled[aei]?|pregleda|zarad\w*|monetiz\w*|pravim[oi]\s+pare|affiliate|merch|patreon)\b', re.I)),
    ('decision_fatigue', re.compile(r'\b(ne\s+znam|vise\s+ne\s+znam|više\s+ne\s+znam|sta\s+da\s+radimo|šta\s+da\s+radimo|stvarno\s+vise|stvarno\s+više)\b', re.I)),
    ('financial_trading_request', re.compile(r'\b(funded\s+account|funded|trejd\w*|trading|trade\w*|broker|alpaca|interactive\s+brokers|forex|prop\s*firm|stop[-\s]?loss|take[-\s]?profit|portfolio|riskujemo)\b', re.I)),
    ('connection_refused', re.compile(r'\b(connection\s+refused|err_connection_refused|refused\s+to\s+connect)\b', re.I)),
    ('missing_dependency', re.compile(r'\b(ModuleNotFoundError|No\s+module\s+named|not\s+installed|missing\s+dependency)\b', re.I)),
    ('network_provider_error', re.compile(r'\b(APIConnectionError|connection\s+error|max\s+retries|provider\s+down|endpoint)\b', re.I)),
    ('permission_blocked', re.compile(r'\b(permission\s+denied|requires\s+approval|sandbox|not\s+authorized|approval\s+required)\b', re.I)),
    ('success_delivery', re.compile(r'\b(sent\s+successfully|message_id|poslato|delivered|send_message.*success)\b', re.I)),
    ('service_reachable', re.compile(r'\b(health.*200|status\s*200|reachable|listening|started|active\s+\(running\))\b', re.I)),
]

_TOOL_RESULT_SIGNAL_ALLOWLIST = {
    'connection_refused', 'missing_dependency', 'network_provider_error', 'permission_blocked',
    'success_delivery', 'service_reachable',
}

_PROJECT_PATTERNS = {
    'live_brain': re.compile(r'\b(live\s*brain|reality\s+engine|control\s*room|dashboard|hermes|tailscale)\b', re.I),
    'enoch': re.compile(r'\benoch\b', re.I),
    'mempalace': re.compile(r'\bmempalace\b', re.I),
}

_HIGH_RISK_ACTIONS = {
    'code_patch', 'config_change', 'db_schema', 'db_schema_migration', 'file_delete', 'credential_change',
    'network_exposure', 'expose_real_dashboard_public', 'send_private_db', 'media_send',
    'financial_trade_execution', 'broker_account_access',
}

_DECISION_RANK = {'allow': 0, 'warn': 1, 'needs_approval': 2, 'deny': 3}
_ACTION_ALIASES = {
    'schema': 'db_schema',
    'database_schema': 'db_schema',
    'db_migration': 'db_schema_migration',
    'schema_migration': 'db_schema_migration',
}


def _canonical_action_type(action_type: str) -> str:
    normalized = (action_type or 'unknown').strip().lower().replace('-', '_')
    return _ACTION_ALIASES.get(normalized, normalized or 'unknown')


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def loads(raw: Any, default: Any) -> Any:
    if raw in (None, ''):
        return default
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return default


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _text_from_payload(payload: Dict[str, Any]) -> str:
    parts: List[str] = []
    for key in ('text', 'user_message', 'assistant_response', 'result', 'error', 'content', 'message'):
        value = payload.get(key)
        if value:
            parts.append(str(value))
    args = payload.get('args')
    if isinstance(args, dict):
        for key in ('query', 'path', 'url', 'command', 'message'):
            if args.get(key):
                parts.append(str(args[key]))
    return '\n'.join(parts)


def extract_signals(event_type: str, subject: str, payload: Dict[str, Any]) -> List[str]:
    text = _text_from_payload(payload)
    signals = set()
    for name, pattern in _SIGNAL_PATTERNS:
        if event_type == 'tool_result' and name not in _TOOL_RESULT_SIGNAL_ALLOWLIST:
            continue
        if pattern.search(text):
            signals.add(name)
    tool_name = str(payload.get('tool_name') or subject or '').lower()
    if tool_name:
        if 'send_message' in tool_name or 'telegram' in tool_name:
            signals.add('messaging_action')
        if 'brain_mark_artifact' in tool_name or 'brain_resolve_artifact' in tool_name or 'brain_list_artifacts' in tool_name:
            signals.add('artifact_action')
    lowered = text.lower()
    if event_type != 'tool_result':
        for project, pattern in _PROJECT_PATTERNS.items():
            if pattern.search(lowered):
                signals.add(f'project:{project}')
    if event_type == 'tool_result':
        result = payload.get('result')
        success = payload.get('success')
        if success is True or (isinstance(result, str) and re.search(r'"success"\s*:\s*true|"ok"\s*:\s*true', result, re.I)):
            signals.add('tool_success')
        if success is False or any(s in signals for s in ('connection_refused', 'missing_dependency', 'network_provider_error', 'permission_blocked')):
            signals.add('tool_failure')
    if event_type in ('assistant_response', 'delivery_result') and 'success_delivery' in signals:
        signals.add('completion_signal')
    return sorted(signals)


_DOMAIN_RELEVANCE = {
    'financial': (
        {'financial_trading_request'},
        ('funded', 'trading', 'trejd', 'trade', 'broker', 'forex', 'prop firm', 'apex', 'cme', 'nq', 'account'),
    ),
    'youtube': (
        {'youtube_monetization'},
        ('youtube', 'shorts', 'monetiz', 'zarad', 'pare', 'pregled', 'affiliate', 'merch', 'patreon'),
    ),
    'dashboard': (
        {'request_dashboard', 'request_link', 'auth_friction', 'connection_refused', 'service_reachable'},
        ('dashboard', 'tailscale', 'link', 'url', 'token', 'auth', 'service', 'port', 'connection refused', '100.'),
    ),
    'telegram': (
        {'request_telegram_delivery'},
        ('telegram', 'telwgram', 'telegrm', 'posalj', 'pošalj', 'send_message', 'delivery'),
    ),
    'demo': (
        {'request_demo'},
        ('demo', 'teaser', 'public demo', 'synthetic', 'video'),
    ),
    'approval': (
        {'request_approval', 'approval_visibility_problem', 'permission_blocked'},
        ('approval', 'approve', 'odobri', 'pending', 'self-evol', 'self evolution', 'sandbox', 'permission'),
    ),
    'live_brain': (
        {'implement_change', 'quality_pushback', 'project:live_brain'},
        ('live brain', 'reality engine', 'hermes', 'plugin', 'memory', 'context', 'agent', 'dashboard'),
    ),
}

_OBJECTIVE_RELEVANCE = {
    'evaluate_financial_trading_request_safely': ('financial',),
    'find_youtube_shorts_monetization_path': ('youtube',),
    'send_requested_plan_or_artifact_to_telegram': ('telegram',),
    'prepare_or_send_public_demo_package': ('demo',),
    'provide_current_dashboard_or_demo_link': ('dashboard',),
    'implement_live_brain_reality_engine': ('live_brain',),
    'raise_live_brain_positioning_and_design_bar': ('live_brain',),
}

_PROJECT_RELEVANCE = {
    'live_brain': ('live_brain', 'dashboard'),
    'enoch': ('youtube', 'demo'),
    'mempalace': ('live_brain',),
}

_FOLLOWUP_RE = re.compile(
    r'^\s*(?:to|ovo|taj|ta|uradi\s+to|a\s+link\??|link\??|e2e\??|demo\??|gotovo\??|jel\s+gotovo\??|'
    r'(?:[sš]ta|sta)\s+(?:dalje|sad|sada)|(?:[sš]ta|sta)\s+da\s+radimo|a\s+sad\??|i\s+sad\??)\s*$',
    re.I,
)

_WORD_RE = re.compile(r'[\w./-]+', re.I)
_RUN_MARKER_RE = re.compile(r'\b(?:run|lbcap|codename)[-_][a-z0-9]+\b', re.I)


def _query_words(text: str) -> set[str]:
    low_signal = {'sta', 'šta', 'kako', 'kad', 'nešto', 'nesto', 'koji', 'koja', 'koje', 'problem', 'agent', 'moze', 'može'}
    return {w.lower() for w in _WORD_RE.findall(text or '') if len(w) > 3 and w.lower() not in low_signal}


def _has_any_term(text: str, terms: Iterable[str]) -> bool:
    lowered = (text or '').lower()
    return any(term in lowered for term in terms)


def _marker_tokens(text: str) -> set[str]:
    return {m.group(0).lower() for m in _RUN_MARKER_RE.finditer(text or '')}


def _marker_conflicts(query: str, text: str) -> bool:
    query_tokens = _marker_tokens(query)
    text_tokens = _marker_tokens(text)
    return bool(query_tokens and text_tokens and query_tokens.isdisjoint(text_tokens))


def _query_matches_domain(domain: str, query: str, signals: set[str]) -> bool:
    allowed_signals, terms = _DOMAIN_RELEVANCE.get(domain, (set(), ()))
    return bool(signals & allowed_signals) or _has_any_term(query, terms)


def _is_followup_query(query: str, signals: set[str]) -> bool:
    lowered = (query or '').strip().lower()
    if not lowered:
        return False
    if signals & {'request_link', 'request_dashboard', 'request_demo', 'request_telegram_delivery', 'financial_trading_request', 'youtube_monetization'}:
        return False
    if 'short_reference' in signals:
        return True
    return bool(_FOLLOWUP_RE.match(lowered) or _FOLLOWUP_RE.match(lowered.strip(' ?!.,;:')))


def _objective_relevant(objective: str, query: str, signals: set[str], followup: bool) -> bool:
    if not objective:
        return False
    if followup:
        return True
    domains = _OBJECTIVE_RELEVANCE.get(objective, ())
    if domains:
        return any(_query_matches_domain(domain, query, signals) for domain in domains)
    return bool(_query_words(query) & _query_words(objective))


def _project_relevant(project: str, query: str, signals: set[str], followup: bool) -> bool:
    if not project:
        return False
    if f'project:{project}' in signals:
        return True
    if followup:
        return True
    domains = _PROJECT_RELEVANCE.get(project, ())
    return any(_query_matches_domain(domain, query, signals) for domain in domains) or project.lower() in (query or '').lower()


def _brief_text_relevant(text: str, query: str, signals: set[str], followup: bool) -> bool:
    if not text:
        return False
    if _marker_conflicts(query or '', text or ''):
        return False
    if followup:
        return True
    lowered = text.lower()
    matched_domain = False
    for domain, (_, terms) in _DOMAIN_RELEVANCE.items():
        if _has_any_term(lowered, terms) or domain in lowered:
            matched_domain = True
            if _query_matches_domain(domain, query, signals):
                return True
    if matched_domain:
        return False
    return bool(_query_words(query) & _query_words(text))


def _service_relevant(query: str, signals: set[str]) -> bool:
    return _query_matches_domain('dashboard', query, signals)


def _usable_service_evidence(evidence: str) -> bool:
    return bool(re.search(r'(https?://|100\.\d+\.\d+\.\d+|health|status\s*200|reachable|listening|active\s+\(running\))', evidence or '', re.I))


@dataclass
class RealityEvent:
    event_id: str
    scope_key: str
    session_id: str
    event_type: str
    subject: str
    payload: Dict[str, Any]
    signals: List[str]
    confidence: float = 0.7
    source: str = 'reality_engine'
    created_at: float = field(default_factory=time.time)


@dataclass
class Reduction:
    target_table: str
    target_key: str
    operation: str
    value: Dict[str, Any]
    confidence: float
    reason: str
    source_event_ids: List[str]


class RealityEngine:
    def __init__(self, conn):
        self.conn = conn

    def ensure_schema(self) -> None:
        self.conn.executescript(SCHEMA_SQL)

    def ingest_event(
        self,
        *,
        scope_key: str,
        event_type: str,
        subject: str = '',
        payload: Optional[Dict[str, Any]] = None,
        session_id: str = '',
        confidence: float = 0.7,
        source: str = 'reality_engine',
        created_at: Optional[float] = None,
        event_id: str = '',
    ) -> Dict[str, Any]:
        self.ensure_schema()
        now = float(created_at or time.time())
        payload = payload or {}
        scope_key = scope_key or 'global'
        subject = subject or event_type
        signals = extract_signals(event_type, subject, payload)
        if not event_id:
            id_hint = str(payload.get('tool_call_id') or payload.get('message_id') or payload.get('turn_id') or int(now * 10))
            event_id = stable_id('reality_event', scope_key, session_id, event_type, subject, dumps(payload), id_hint)
        event = RealityEvent(
            event_id=event_id,
            scope_key=scope_key,
            session_id=session_id or '',
            event_type=event_type,
            subject=subject,
            payload=payload,
            signals=signals,
            confidence=clamp(float(confidence)),
            source=source,
            created_at=now,
        )
        self.conn.execute(
            """
            INSERT OR IGNORE INTO reality_events
            (event_id, scope_key, session_id, event_type, subject, payload_json, signals_json, confidence, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (event.event_id, event.scope_key, event.session_id, event.event_type, event.subject, dumps(event.payload), dumps(event.signals), event.confidence, event.source, event.created_at),
        )
        reductions = self.reduce_event(event)
        for reduction in reductions:
            self.apply_reduction(reduction, now)
        self.conn.commit()
        return {
            'event_id': event.event_id,
            'scope_key': event.scope_key,
            'event_type': event.event_type,
            'subject': event.subject,
            'signals': event.signals,
            'reductions': [r.__dict__ for r in reductions],
        }

    def reduce_event(self, event: RealityEvent) -> List[Reduction]:
        reductions: List[Reduction] = []
        reductions.extend(self._reduce_objective(event))
        reductions.extend(self._reduce_user_feedback(event))
        reductions.extend(self._reduce_tool_failure(event))
        reductions.extend(self._reduce_artifact_event(event))
        reductions.extend(self._reduce_completion(event))
        reductions.extend(self._reduce_attention(event))
        return reductions

    def _project_from_signals(self, signals: Iterable[str]) -> str:
        for signal in signals:
            if signal.startswith('project:'):
                return signal.split(':', 1)[1]
        return ''

    def _state(self, event: RealityEvent, key: str, value: Dict[str, Any], reason: str, *, confidence: Optional[float] = None, ttl_seconds: Optional[int] = None) -> Reduction:
        if ttl_seconds:
            value = dict(value)
            value['ttl_seconds'] = ttl_seconds
        return Reduction('reality_state', key, 'upsert', value, confidence if confidence is not None else event.confidence, reason, [event.event_id])

    def _open_loop(self, event: RealityEvent, key: str, title: str, next_action: str, reason: str, *, priority: float = 0.7, blockers: Optional[List[str]] = None, status: str = 'active') -> Reduction:
        return Reduction(
            'open_loops',
            stable_id('open_loop', event.scope_key, key),
            'upsert',
            {'title': title, 'status': status, 'priority': priority, 'next_action': next_action, 'blockers': blockers or []},
            event.confidence,
            reason,
            [event.event_id],
        )

    def _close_loop(self, event: RealityEvent, key: str, reason: str) -> Reduction:
        return Reduction(
            'open_loops',
            stable_id('open_loop', event.scope_key, key),
            'close',
            {'status': 'resolved'},
            event.confidence,
            reason,
            [event.event_id],
        )

    def _danger(self, event: RealityEvent, key: str, pattern: str, severity: str, mitigation: str, reason: str) -> Reduction:
        return Reduction(
            'danger_zones',
            stable_id('danger', event.scope_key, key),
            'upsert',
            {'pattern': pattern, 'severity': severity, 'mitigation': mitigation},
            event.confidence,
            reason,
            [event.event_id],
        )

    def _constraint(self, event: RealityEvent, key: str, action_type: str, decision: str, reason_text: str, risk_level: str = 'medium', ttl_seconds: Optional[int] = None) -> Reduction:
        return Reduction(
            'action_constraints',
            stable_id('constraint', event.scope_key, key),
            'upsert',
            {'action_type': action_type, 'decision': decision, 'reason': reason_text, 'risk_level': risk_level, 'ttl_seconds': ttl_seconds},
            event.confidence,
            reason_text,
            [event.event_id],
        )

    def _reduce_objective(self, event: RealityEvent) -> List[Reduction]:
        signals = set(event.signals)
        text = _text_from_payload(event.payload)
        lowered = text.lower()
        project = self._project_from_signals(signals)
        reductions: List[Reduction] = []
        if project and event.event_type in {'user_message', 'query', 'artifact_status_change'}:
            reductions.append(self._state(event, 'active_project', {'project': project, 'evidence': text[:220]}, 'project cue detected', confidence=0.82, ttl_seconds=7 * 86400))
        if event.event_type not in {'user_message', 'query'}:
            return reductions
        objective = ''
        safe_next = ''
        loop_key = ''
        title = ''
        if 'financial_trading_request' in signals:
            objective = 'evaluate_financial_trading_request_safely'
            safe_next = 'Do not claim ability to trade funded/live accounts; discuss only education, paper-trading, risk limits, and explicit human approval/governance.'
            loop_key = 'financial_trading_request'
            title = 'Evaluate funded-account trading request safely'
        elif 'youtube_monetization' in signals:
            objective = 'find_youtube_shorts_monetization_path'
            safe_next = 'Focus on one realistic monetization experiment for YouTube Shorts; avoid broad option dumps and separate growth from revenue tests.'
            loop_key = 'youtube_monetization'
            title = 'Find realistic YouTube Shorts monetization path'
        elif 'request_telegram_delivery' in signals:
            objective = 'send_requested_plan_or_artifact_to_telegram'
            safe_next = 'Use Hermes messaging/send_message path; if unavailable, use verified Telethon fallback and report exact delivery status.'
            loop_key = 'telegram_delivery'
            title = 'Send requested content to Telegram'
        elif 'request_demo' in signals:
            objective = 'prepare_or_send_public_demo_package'
            safe_next = 'Use synthetic public demo assets; avoid real Live Brain DB or private session data.'
            loop_key = 'public_demo_package'
            title = 'Prepare public demo package'
        elif 'request_dashboard' in signals or 'request_link' in signals:
            objective = 'provide_current_dashboard_or_demo_link'
            safe_next = 'Give the current dashboard/demo URL if known; if refused, check service status and port before guessing.'
            loop_key = 'dashboard_link'
            title = 'Provide working dashboard/demo link'
        elif 'implement_change' in signals and ('reality engine' in lowered or 'persistent situational' in lowered or 'ceo plan' in lowered or 'ceo' in lowered and 'plan' in lowered):
            objective = 'implement_live_brain_reality_engine'
            safe_next = 'Apply event-sourced reality schema, deterministic reducers, hooks, dashboard, CLI, and tests following Hermes plugin docs.'
            loop_key = 'implement_reality_engine'
            title = 'Implement Live Brain Reality Engine'
        elif 'quality_pushback' in signals:
            objective = 'raise_live_brain_positioning_and_design_bar'
            safe_next = 'Move beyond retrieval framing toward persistent situational awareness and prove it with deterministic behavior.'
        if objective:
            reductions.append(self._state(event, 'current_objective', {'objective': objective, 'evidence': text[:260]}, 'objective cue detected', confidence=0.86, ttl_seconds=2 * 86400))
            reductions.append(self._state(event, 'safe_next_action', {'action': safe_next}, 'safe next action inferred from objective', confidence=0.78, ttl_seconds=2 * 86400))
        elif 'decision_fatigue' in signals:
            reductions.append(self._state(event, 'user_affect', {'affect': 'decision_fatigue', 'evidence': text[:220]}, 'user reported uncertainty/frustration', confidence=0.8, ttl_seconds=24 * 3600))
            reductions.append(self._state(event, 'safe_next_action', {'action': 'Reduce choice overload: propose one concrete low-risk next step, not a menu of generic options.'}, 'safe next action inferred from decision fatigue', confidence=0.78, ttl_seconds=24 * 3600))
        if loop_key:
            reductions.append(self._open_loop(event, loop_key, title, safe_next, 'user request opened/refreshes work loop', priority=0.82))
        if objective in {'find_youtube_shorts_monetization_path', 'evaluate_financial_trading_request_safely'}:
            reductions.append(self._close_loop(event, 'public_demo_package', 'superseded by current user priority'))
        if 'request_link' in signals and 'short_reference' in signals:
            reductions.append(self._state(event, 'likely_user_intent', {'intent': 'request_current_link', 'do_not_ask_generic_which_link': True}, 'short reference resolved from active reality', confidence=0.76, ttl_seconds=6 * 3600))
        return reductions

    def _reduce_user_feedback(self, event: RealityEvent) -> List[Reduction]:
        if event.event_type not in {'user_message', 'query'}:
            return []
        signals = set(event.signals)
        reductions: List[Reduction] = []
        if 'auth_friction' in signals:
            reductions.append(self._state(event, 'user_preference.auth', {'preference': 'avoid token auth when safe on trusted Tailscale demos'}, 'user reported auth/token friction', confidence=0.86, ttl_seconds=30 * 86400))
            reductions.append(self._constraint(event, 'trusted_tailscale_demo_auth', 'dashboard_auth', 'warn', 'Avoid token-auth friction for trusted Tailscale demo unless explicitly requested.', 'medium', ttl_seconds=30 * 86400))
        if 'approval_visibility_problem' in signals:
            reductions.append(self._danger(event, 'approval_ui_visibility', 'approval_claim_without_visible_ui', 'high', 'Do not claim approval is visible; verify UI or provide exact approval command.', 'user could not see approval UI'))
            reductions.append(self._constraint(event, 'approval_claim_visibility', 'approval_claim', 'warn', 'Verify approval surface before saying the user can approve there.', 'medium', ttl_seconds=30 * 86400))
        if 'last_session_required' in signals:
            reductions.append(self._state(event, 'user_preference.last_session_first', {'preference': 'inspect latest session before claiming something is absent'}, 'user corrected absence claim with last-session instruction', confidence=0.82, ttl_seconds=30 * 86400))
            reductions.append(self._constraint(event, 'absence_claim_requires_recent_session_check', 'absence_claim', 'warn', 'Check latest session/logs before claiming something was not found.', 'medium', ttl_seconds=30 * 86400))
        if 'interrupt_preference' in signals:
            reductions.append(self._constraint(event, 'interrupt_only_when_needed', 'user_interrupt', 'warn', 'Do not interrupt every message; interrupt only when safety, approval, or ambiguity blocks progress.', 'low', ttl_seconds=45 * 86400))
        if 'quality_pushback' in signals:
            reductions.append(self._state(event, 'design_bar', {'bar': 'must feel genuinely novel, not just better retrieval'}, 'user pushed back on weak revolutionary framing', confidence=0.82, ttl_seconds=14 * 86400))
        if 'youtube_monetization' in signals:
            reductions.append(self._state(event, 'business_context', {'context': 'YouTube Shorts monetization/growth problem', 'evidence': _text_from_payload(event.payload)[:220]}, 'user discussed YouTube Shorts money/growth', confidence=0.78, ttl_seconds=7 * 86400))
        if 'decision_fatigue' in signals:
            reductions.append(self._constraint(event, 'avoid_option_dump_when_user_stuck', 'planning_response', 'warn', 'User is stuck; avoid broad menus and pick one reversible next step.', 'low', ttl_seconds=7 * 86400))
        if 'financial_trading_request' in signals:
            reductions.append(self._danger(event, 'financial_trading_overclaim', 'claiming_agent_can_trade_funded_or_live_accounts', 'high', 'Do not promise trading performance or autonomous account control; keep to education, simulation, and explicit risk controls.', 'financial trading/funded account request detected'))
            reductions.append(self._constraint(event, 'no_autonomous_financial_trading', 'financial_trade_execution', 'deny', 'Do not execute trades or control funded/live accounts without explicit audited governance and legal/risk approval.', 'critical', ttl_seconds=30 * 86400))
            reductions.append(self._constraint(event, 'financial_advice_caution', 'financial_advice_claim', 'warn', 'Do not present trading decisions as personalized financial advice or guaranteed capability.', 'high', ttl_seconds=30 * 86400))
        return reductions

    def _reduce_tool_failure(self, event: RealityEvent) -> List[Reduction]:
        signals = set(event.signals)
        reductions: List[Reduction] = []
        if 'connection_refused' in signals:
            reductions.append(self._open_loop(event, 'service_connection_refused', 'Service refused connection', 'Check systemd/service status, host binding, and listening port; then provide the verified working URL.', 'tool/browser reported connection refused', priority=0.94, blockers=['service_not_listening']))
            reductions.append(self._state(event, 'known_blocker.service_connection_refused', {'blocker': 'service_not_listening_or_wrong_port'}, 'connection refused observed', confidence=0.9, ttl_seconds=24 * 3600))
        if 'missing_dependency' in signals:
            reductions.append(self._open_loop(event, 'missing_dependency', 'Missing runtime dependency', 'Install dependency in the exact runtime environment, then retry the failed path.', 'tool reported missing dependency', priority=0.86, blockers=['missing_dependency']))
        if 'network_provider_error' in signals:
            reductions.append(self._open_loop(event, 'provider_connection_error', 'Provider/network connection error', 'Retry with network access/escalation or switch to a local fallback when available.', 'provider API connection failed', priority=0.78, blockers=['network_or_provider_down']))
        if 'permission_blocked' in signals:
            reductions.append(self._open_loop(event, 'permission_or_sandbox_blocker', 'Permission or sandbox blocker', 'Request escalation only for the specific command that needs it; explain why.', 'permission/sandbox blocker observed', priority=0.84, blockers=['permission_blocked']))
        return reductions

    def _reduce_artifact_event(self, event: RealityEvent) -> List[Reduction]:
        signals = set(event.signals)
        reductions: List[Reduction] = []
        payload_text = _text_from_payload(event.payload)
        if 'artifact_action' not in signals and event.event_type != 'artifact_status_change':
            return reductions
        lowered = payload_text.lower()
        if 'rejected' in lowered or 'deprecated' in lowered:
            reductions.append(self._danger(event, 'rejected_artifact_candidate', 'rejected_or_deprecated_artifact', 'high', 'Do not send rejected/deprecated artifacts; resolve a verified artifact by role first.', 'artifact action exposed rejected/deprecated candidate'))
            reductions.append(self._constraint(event, 'send_rejected_artifact', 'media_send', 'deny', 'Rejected/deprecated artifact must not be sent.', 'high', ttl_seconds=None))
        if 'verified' in lowered and ('live_brain_control_room' in lowered or 'demo' in lowered):
            reductions.append(self._state(event, 'latest_public_demo_artifact', {'hint': 'use verified synthetic Live Brain demo asset', 'evidence': payload_text[:260]}, 'verified public demo artifact observed', confidence=0.83, ttl_seconds=7 * 86400))
        return reductions

    def _reduce_completion(self, event: RealityEvent) -> List[Reduction]:
        signals = set(event.signals)
        reductions: List[Reduction] = []
        text = _text_from_payload(event.payload)
        lowered = text.lower()
        if 'success_delivery' in signals or 'completion_signal' in signals:
            reductions.append(self._close_loop(event, 'telegram_delivery', 'delivery succeeded'))
            reductions.append(self._state(event, 'last_delivery_result', {'status': 'success', 'evidence': text[:220]}, 'delivery success observed', confidence=0.9, ttl_seconds=7 * 86400))
        if 'service_reachable' in signals:
            reductions.append(self._close_loop(event, 'service_connection_refused', 'service became reachable'))
            reductions.append(self._state(event, 'last_service_health', {'status': 'reachable', 'evidence': text[:220]}, 'service health success observed', confidence=0.88, ttl_seconds=24 * 3600))
            reductions.append(self._state(event, 'safe_next_action', {'action': 'Provide the verified working service URL from the latest health evidence; if it fails, check binding and port before guessing.'}, 'service is reachable', confidence=0.82, ttl_seconds=24 * 3600))
        if 'demo video written' in lowered or 'teaser voiceover' in lowered:
            reductions.append(self._close_loop(event, 'public_demo_package', 'demo asset generated'))
        if 'reality engine' in lowered and ('pass' in lowered or 'implemented' in lowered or 'gotovo' in lowered):
            reductions.append(self._close_loop(event, 'implement_reality_engine', 'implementation reported complete'))
        return reductions

    def _reduce_attention(self, event: RealityEvent) -> List[Reduction]:
        signals = set(event.signals)
        reductions: List[Reduction] = []
        if signals & {'auth_friction', 'approval_visibility_problem', 'connection_refused', 'permission_blocked'}:
            reductions.append(Reduction(
                'attention_triggers',
                stable_id('attention', event.scope_key, 'friction_or_blocker'),
                'upsert',
                {
                    'trigger_type': 'friction_or_blocker',
                    'condition': {'signals': sorted(signals)},
                    'response_policy': 'surface only when it changes the safe next action or blocks progress',
                    'enabled': True,
                },
                event.confidence,
                'friction/blocker signal should remain attention-worthy',
                [event.event_id],
            ))
        return reductions

    def apply_reduction(self, reduction: Reduction, now: float) -> None:
        source_event_id = reduction.source_event_ids[0] if reduction.source_event_ids else ''
        if reduction.target_table == 'reality_state':
            self._apply_state(reduction, now)
        elif reduction.target_table == 'open_loops':
            self._apply_open_loop(reduction, now)
        elif reduction.target_table == 'danger_zones':
            self._apply_danger(reduction, now)
        elif reduction.target_table == 'action_constraints':
            self._apply_constraint(reduction, now)
        elif reduction.target_table == 'attention_triggers':
            self._apply_attention(reduction, now)
        self.conn.execute(
            """
            INSERT OR REPLACE INTO reality_reductions
            (reduction_id, source_event_id, target_table, target_key, operation, reason, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                stable_id('reduction', source_event_id, reduction.target_table, reduction.target_key, reduction.operation, reduction.reason),
                source_event_id,
                reduction.target_table,
                reduction.target_key,
                reduction.operation,
                reduction.reason[:300],
                dumps(reduction.value),
                now,
            ),
        )

    def _merged_sources(self, existing_raw: Any, new_ids: Iterable[str]) -> str:
        existing = loads(existing_raw, [])
        merged: List[str] = []
        for item in list(existing or []) + list(new_ids or []):
            if item and item not in merged:
                merged.append(str(item))
        return dumps(merged[-20:])

    def _apply_state(self, reduction: Reduction, now: float) -> None:
        ttl = reduction.value.get('ttl_seconds')
        expires_at = now + int(ttl) if ttl else None
        existing = self.conn.execute(
            "SELECT source_event_ids_json FROM reality_state WHERE scope_key=? AND state_key=?",
            (self._scope_from_key(reduction), reduction.target_key),
        ).fetchone()
        sources = self._merged_sources(existing['source_event_ids_json'] if existing else '[]', reduction.source_event_ids)
        value = dict(reduction.value)
        value.pop('ttl_seconds', None)
        self.conn.execute(
            """
            INSERT OR REPLACE INTO reality_state
            (scope_key, state_key, value_json, confidence, source_event_ids_json, updated_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (self._scope_from_key(reduction), reduction.target_key, dumps(value), reduction.confidence, sources, now, expires_at),
        )

    def _apply_open_loop(self, reduction: Reduction, now: float) -> None:
        existing = self.conn.execute("SELECT source_event_ids_json, created_at FROM open_loops WHERE loop_id=?", (reduction.target_key,)).fetchone()
        sources = self._merged_sources(existing['source_event_ids_json'] if existing else '[]', reduction.source_event_ids)
        created_at = float(existing['created_at']) if existing else now
        status = reduction.value.get('status', 'active')
        resolved_at = now if reduction.operation == 'close' or status == 'resolved' else None
        if reduction.operation == 'close':
            self.conn.execute(
                "UPDATE open_loops SET status='resolved', source_event_ids_json=?, updated_at=?, resolved_at=? WHERE loop_id=?",
                (sources, now, resolved_at, reduction.target_key),
            )
            return
        self.conn.execute(
            """
            INSERT OR REPLACE INTO open_loops
            (loop_id, scope_key, title, status, priority, next_action, blockers_json, source_event_ids_json, created_at, updated_at, resolved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                reduction.target_key,
                self._scope_from_key(reduction),
                str(reduction.value.get('title') or ''),
                status,
                float(reduction.value.get('priority') or 0.5),
                str(reduction.value.get('next_action') or ''),
                dumps(reduction.value.get('blockers') or []),
                sources,
                created_at,
                now,
                resolved_at,
            ),
        )

    def _apply_danger(self, reduction: Reduction, now: float) -> None:
        existing = self.conn.execute("SELECT source_event_ids_json, created_at, times_triggered FROM danger_zones WHERE danger_id=?", (reduction.target_key,)).fetchone()
        sources = self._merged_sources(existing['source_event_ids_json'] if existing else '[]', reduction.source_event_ids)
        created_at = float(existing['created_at']) if existing else now
        times_triggered = int(existing['times_triggered']) + 1 if existing else 1
        self.conn.execute(
            """
            INSERT OR REPLACE INTO danger_zones
            (danger_id, scope_key, pattern, severity, mitigation, times_triggered, source_event_ids_json, created_at, updated_at, last_triggered_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                reduction.target_key,
                self._scope_from_key(reduction),
                str(reduction.value.get('pattern') or ''),
                str(reduction.value.get('severity') or 'medium'),
                str(reduction.value.get('mitigation') or ''),
                times_triggered,
                sources,
                created_at,
                now,
                now,
            ),
        )

    def _apply_constraint(self, reduction: Reduction, now: float) -> None:
        ttl = reduction.value.get('ttl_seconds')
        expires_at = now + int(ttl) if ttl else None
        existing = self.conn.execute("SELECT source_event_ids_json, created_at FROM action_constraints WHERE constraint_id=?", (reduction.target_key,)).fetchone()
        sources = self._merged_sources(existing['source_event_ids_json'] if existing else '[]', reduction.source_event_ids)
        created_at = float(existing['created_at']) if existing else now
        self.conn.execute(
            """
            INSERT OR REPLACE INTO action_constraints
            (constraint_id, scope_key, action_type, decision, reason, risk_level, ttl_seconds, source_event_ids_json, created_at, updated_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                reduction.target_key,
                self._scope_from_key(reduction),
                str(reduction.value.get('action_type') or ''),
                str(reduction.value.get('decision') or 'warn'),
                str(reduction.value.get('reason') or reduction.reason),
                str(reduction.value.get('risk_level') or 'medium'),
                int(ttl) if ttl else None,
                sources,
                created_at,
                now,
                expires_at,
            ),
        )

    def _apply_attention(self, reduction: Reduction, now: float) -> None:
        existing = self.conn.execute("SELECT created_at FROM attention_triggers WHERE trigger_id=?", (reduction.target_key,)).fetchone()
        created_at = float(existing['created_at']) if existing else now
        self.conn.execute(
            """
            INSERT OR REPLACE INTO attention_triggers
            (trigger_id, scope_key, trigger_type, condition_json, response_policy, enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                reduction.target_key,
                self._scope_from_key(reduction),
                str(reduction.value.get('trigger_type') or ''),
                dumps(reduction.value.get('condition') or {}),
                str(reduction.value.get('response_policy') or ''),
                1 if reduction.value.get('enabled', True) else 0,
                created_at,
                now,
            ),
        )

    def _scope_from_key(self, reduction: Reduction) -> str:
        event_id = reduction.source_event_ids[0] if reduction.source_event_ids else ''
        row = self.conn.execute("SELECT scope_key FROM reality_events WHERE event_id=?", (event_id,)).fetchone()
        return str(row['scope_key']) if row else 'global'

    def cleanup_expired(self, now: Optional[float] = None) -> int:
        self.ensure_schema()
        now = float(now or time.time())
        count = 0
        for table in ('reality_state', 'action_constraints'):
            cur = self.conn.execute(f"DELETE FROM {table} WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,))
            count += cur.rowcount if cur.rowcount is not None else 0
        self.conn.commit()
        return count

    def compile_brief(self, scope_key: str, query: str = '', *, max_lines: int = 12) -> str:
        self.ensure_schema()
        self.cleanup_expired()
        scope_key = scope_key or 'global'
        now = time.time()
        signals = extract_signals('query', 'query', {'text': query or ''})
        state_rows = self.conn.execute(
            "SELECT state_key, value_json, confidence, updated_at FROM reality_state WHERE scope_key=? ORDER BY updated_at DESC LIMIT 20",
            (scope_key,),
        ).fetchall()
        states = {row['state_key']: loads(row['value_json'], {}) for row in state_rows}
        open_rows = self.conn.execute(
            "SELECT title, status, priority, next_action, blockers_json, updated_at FROM open_loops WHERE scope_key=? AND status IN ('active','blocked') ORDER BY priority DESC, updated_at DESC LIMIT 5",
            (scope_key,),
        ).fetchall()
        danger_rows = self.conn.execute(
            "SELECT pattern, severity, mitigation, times_triggered, updated_at FROM danger_zones WHERE scope_key=? ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, updated_at DESC LIMIT 5",
            (scope_key,),
        ).fetchall()
        constraint_rows = self.conn.execute(
            "SELECT action_type, decision, reason, risk_level, updated_at FROM action_constraints WHERE scope_key=? AND (expires_at IS NULL OR expires_at > ?) ORDER BY CASE decision WHEN 'deny' THEN 0 WHEN 'needs_approval' THEN 1 WHEN 'warn' THEN 2 ELSE 3 END, updated_at DESC LIMIT 6",
            (scope_key, now),
        ).fetchall()
        signal_set = set(signals)
        followup = _is_followup_query(query or '', signal_set)
        lines: List[str] = []
        current = states.get('current_objective') or {}
        active_project = states.get('active_project') or {}
        likely_intent = states.get('likely_user_intent') or {}
        safe_next = states.get('safe_next_action') or {}
        last_service = states.get('last_service_health') or {}
        current_relevant = _objective_relevant(str(current.get('objective') or ''), query or '', signal_set, followup)
        project_relevant = _project_relevant(str(active_project.get('project') or ''), query or '', signal_set, followup)
        service_relevant = _service_relevant(query or '', signal_set)
        open_rows = [
            row for row in open_rows
            if _brief_text_relevant(f"{row['title'] or ''} {row['next_action'] or ''} {row['blockers_json'] or ''}", query or '', signal_set, followup)
        ]
        danger_rows = [
            row for row in danger_rows
            if _brief_text_relevant(f"{row['pattern'] or ''} {row['mitigation'] or ''}", query or '', signal_set, followup)
        ]
        constraint_rows = [
            row for row in constraint_rows
            if _brief_text_relevant(f"{row['action_type'] or ''} {row['reason'] or ''}", query or '', signal_set, followup)
        ]
        safe_next_relevant = current_relevant or _brief_text_relevant(str(safe_next.get('action') or ''), query or '', signal_set, followup)
        if current_relevant and current.get('objective'):
            lines.append(f"Current objective: {current.get('objective')}")
        if project_relevant and active_project.get('project'):
            lines.append(f"Active project: {active_project.get('project')}")
        if 'request_link' in signal_set:
            if current_relevant or open_rows or (service_relevant and last_service):
                lines.append("Likely intent: user is asking for the current active link; do not ask generic 'which link?' unless multiple live links are equally plausible.")
            if service_relevant and last_service.get('evidence') and _usable_service_evidence(str(last_service.get('evidence'))):
                lines.append(f"Known working service/link evidence: {str(last_service.get('evidence'))[:180]}")
        if 'short_reference' in signal_set and current_relevant:
            lines.append("Short reference detected: resolve 'to/ovo/uradi to' from current objective and open loops before asking.")
        if likely_intent.get('do_not_ask_generic_which_link') and 'request_link' in signal_set:
            lines.append("Reference rule: avoid generic clarification when active objective already identifies the object.")
        if open_rows:
            loop_bits = []
            for row in open_rows[:3]:
                title = str(row['title'] or '')
                next_action = str(row['next_action'] or '')
                loop_bits.append(f"{title} -> {next_action}" if next_action else title)
            lines.append("Open loops: " + ' | '.join(loop_bits))
        if danger_rows:
            danger_bits = [f"{row['severity']}:{row['pattern']} => {row['mitigation']}" for row in danger_rows[:3]]
            lines.append("Danger zones: " + ' | '.join(danger_bits))
        if constraint_rows:
            constraint_bits = [f"{row['action_type']}={row['decision']} ({row['reason']})" for row in constraint_rows[:4]]
            lines.append("Action constraints: " + ' | '.join(constraint_bits))
        if safe_next_relevant and safe_next.get('action'):
            lines.append(f"Safe next action: {safe_next.get('action')}")
        if not lines:
            return ''
        compact = lines[:max_lines]
        return "LIVE REALITY:\n- " + "\n- ".join(compact)

    def debug(self, scope_key: str, query: str = '') -> Dict[str, Any]:
        self.ensure_schema()
        signals = extract_signals('query', 'query', {'text': query or ''})
        scope_key = scope_key or 'global'
        rows = lambda sql, params=(): [dict(row) for row in self.conn.execute(sql, tuple(params)).fetchall()]
        state = rows("SELECT * FROM reality_state WHERE scope_key=? ORDER BY updated_at DESC LIMIT 20", (scope_key,))
        open_loops = rows("SELECT * FROM open_loops WHERE scope_key=? ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'blocked' THEN 1 ELSE 2 END, priority DESC, updated_at DESC LIMIT 20", (scope_key,))
        dangers = rows("SELECT * FROM danger_zones WHERE scope_key=? ORDER BY updated_at DESC LIMIT 20", (scope_key,))
        constraints = rows("SELECT * FROM action_constraints WHERE scope_key=? ORDER BY updated_at DESC LIMIT 20", (scope_key,))
        events = rows("SELECT event_id, event_type, subject, signals_json, created_at FROM reality_events WHERE scope_key=? ORDER BY created_at DESC LIMIT 20", (scope_key,))
        for collection in (state, open_loops, dangers, constraints, events):
            for item in collection:
                for key in list(item.keys()):
                    if key.endswith('_json'):
                        item[key[:-5]] = loads(item[key], [] if key.endswith('s_json') else {})
        return {
            'scope_key': scope_key,
            'query': query,
            'signals': signals,
            'brief': self.compile_brief(scope_key, query),
            'state': state,
            'open_loops': open_loops,
            'danger_zones': dangers,
            'action_constraints': constraints,
            'events': events,
        }

    def action_gate(self, scope_key: str, action_type: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self.ensure_schema()
        scope_key = scope_key or 'global'
        original_action_type = action_type or 'unknown'
        action_type = _canonical_action_type(original_action_type)
        payload = payload or {}
        now = time.time()
        reasons: List[str] = []
        decision = 'allow'
        risk_level = 'low'
        path = str(payload.get('path') or payload.get('file') or '')
        if action_type in _HIGH_RISK_ACTIONS:
            decision = 'needs_approval'
            risk_level = 'high'
            reasons.append(f'{action_type} is high-risk by policy')
            if action_type != original_action_type:
                reasons.append(f'action type normalized from {original_action_type}')
        if path:
            lowered_path = path.lower()
            if '/.hermes/live_brain/live_brain.db' in lowered_path or lowered_path.endswith('/live_brain.db') and '/tmp/live_brain_control_room_demo/' not in lowered_path:
                decision = 'deny'
                risk_level = 'critical'
                reasons.append('real Live Brain database must not be exposed or sent')
            if '/tmp/live_brain_control_room_demo/' in lowered_path or '/live_brain_plugin_package/demo/' in lowered_path:
                if decision == 'allow':
                    reasons.append('path looks like synthetic/public demo asset')
                elif decision == 'needs_approval' and bool(payload.get('synthetic_public', True)):
                    decision = 'allow'
                    risk_level = 'low'
                    reasons.append('synthetic public demo asset is allowed')
            try:
                row = self.conn.execute("SELECT status, project_key, role FROM verified_artifacts WHERE path=? ORDER BY updated_at DESC LIMIT 1", (path,)).fetchone()
            except Exception:
                row = None
            if row:
                status = str(row['status'] or '')
                if status in {'rejected', 'deprecated', 'missing'}:
                    decision = 'deny'
                    risk_level = 'high'
                    reasons.append(f'artifact status is {status}')
                elif status == 'verified' and decision == 'allow':
                    reasons.append(f'verified artifact role={row["role"]}')
        for row in self.conn.execute(
            "SELECT action_type, decision, reason, risk_level FROM action_constraints WHERE scope_key=? AND (action_type=? OR action_type='*') AND (expires_at IS NULL OR expires_at > ?) ORDER BY updated_at DESC LIMIT 10",
            (scope_key, action_type, now),
        ).fetchall():
            row_decision = str(row['decision'] or 'warn')
            if _DECISION_RANK.get(row_decision, 1) > _DECISION_RANK.get(decision, 0):
                decision = row_decision
                risk_level = str(row['risk_level'] or risk_level)
            reasons.append(str(row['reason'] or f'constraint {row["action_type"]}={row_decision}'))
        return {
            'scope_key': scope_key,
            'action_type': action_type,
            'decision': decision,
            'risk_level': risk_level,
            'reasons': reasons or ['no blocking constraints matched'],
            'payload': payload,
        }
