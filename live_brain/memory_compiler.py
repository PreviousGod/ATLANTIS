from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from .utils import stable_id

logger = logging.getLogger(__name__)


MEMORY_OBJECT_TYPES = {
    'active_task',
    'semantic_fact',
    'user_preference',
    'verified_artifact',
    'fix_recipe',
    'validated_cause',
    'open_hypothesis',
    'open_loop',
    'verification_debt',
    'instruction_proposal',
}

TASK_STATUSES = {'candidate', 'active', 'blocked', 'resolved', 'aborted', 'superseded', 'stale'}
TASK_TRANSITIONS = {
    'candidate': {'active', 'aborted', 'superseded', 'resolved', 'stale'},
    'active': {'blocked', 'resolved', 'aborted', 'superseded', 'stale'},
    'blocked': {'active', 'aborted', 'superseded', 'stale'},
    'resolved': set(),
    'aborted': set(),
    'superseded': set(),
    'stale': {'active', 'aborted', 'superseded', 'resolved'},
}

SECTION_PRECEDENCE = [
    'PENDING APPROVAL',
    'MUST FOLLOW',
    'UNVERIFIED CLAIM',
    'VERIFICATION REQUIRED',
    'VERIFIED ARTIFACTS',
    'ACTIVE TASK',
    'NEXT REQUIRED ACTION',
    'KNOWN FACTS',
    'PROVEN FIX',
    'OPEN BUG',
    'CONTINUITY MEMORY',
    'LATEST RECAP',
    'FILE KNOWLEDGE',
    'INFRASTRUCTURE',
    'AUTHORED THIS SESSION',
]

LANE_SECTION_ALLOWLIST = {
    'chit_chat': set(),
    'approval_flow': {'PENDING APPROVAL', 'MUST FOLLOW'},
    'document_intake': {'VERIFIED ARTIFACTS', 'FILE KNOWLEDGE', 'ACTIVE TASK', 'VERIFICATION REQUIRED'},
    'simple_execution': {'ACTIVE TASK', 'VERIFIED ARTIFACTS', 'KNOWN FACTS', 'VERIFICATION REQUIRED'},
    'deep_execution': set(SECTION_PRECEDENCE),
    'research_or_epistemic': {'MUST FOLLOW', 'KNOWN FACTS', 'VERIFICATION REQUIRED'},
    'continuation_or_resume': {
        'ACTIVE TASK',
        'LATEST RECAP',
        'CONTINUITY MEMORY',
        'VERIFIED ARTIFACTS',
        'VERIFICATION REQUIRED',
    },
}

ONE_SHOT_RE = re.compile(
    r'\b(?:hello|hi|hey|cao|ćao|zdravo|config|konfig|status|where is|gde je|pronadji|pronađi|find|lookup|look up)\b',
    re.IGNORECASE,
)
ONGOING_RE = re.compile(
    r'\b(?:fix|debug|build|implement|edit|patch|migrate|investigate|continue|nastavi|pdf|document|refactor|test)\b',
    re.IGNORECASE,
)
CANCEL_RE = re.compile(r'^\s*/(?:stop|stpp)\b', re.IGNORECASE)
CONTINUE_RE = re.compile(r'\b(?:continue|nastavi|dalje|gde smo stali|gdje smo stali|resume)\b', re.IGNORECASE)
NO_WIDEN_RE = re.compile(r'\b(?:ne\s+siri\s+temu|ne\s+širi\s+temu|do\s+not\s+widen|stay\s+on\s+topic)\b', re.IGNORECASE)
COMPACTION_RE = re.compile(r'^\s*\[CONTEXT COMPACTION', re.IGNORECASE)
LOW_VALUE_WORK_RE = re.compile(
    r'^\s*(?:--message|hello|hi|hey|cao|ćao|zdravo|ok|okej|da|ne|yes|no|continue|nastavi|'
    r'sumarizuj.*|what did you do.*|review the conversation above.*)\s*$',
    re.IGNORECASE,
)
DOCUMENT_RE = re.compile(r'\b(?:pdf|document|ocr|scan|skeniraj|docx)\b', re.IGNORECASE)
GENERIC_INFRA_RE = re.compile(r'\b(?:dashboard|demo|provider|blocker|control room|tailscale)\b', re.IGNORECASE)
TAG_RE = re.compile(r'[\w./-]+', re.UNICODE)


def _dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)


def _loads(value: Any, default: Any = None) -> Any:
    if value is None or value == '':
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def _now() -> float:
    return time.time()


def _fingerprint(text: str) -> str:
    normalized = re.sub(r'\s+', ' ', (text or '').lower()).strip()
    return hashlib.sha256(normalized.encode('utf-8', 'ignore')).hexdigest()[:24]


def _tokens(text: str) -> set[str]:
    return {token.lower() for token in TAG_RE.findall(text or '') if len(token) > 2}


def _json_list(value: Any) -> list[str]:
    parsed = _loads(value, [])
    if isinstance(parsed, dict):
        result: list[str] = []
        for child in parsed.values():
            if isinstance(child, list):
                result.extend(str(item) for item in child)
            elif isinstance(child, str):
                result.append(child)
        return result
    if isinstance(parsed, list):
        return [str(item) for item in parsed]
    return []


def _table_columns(conn, table: str) -> set[str]:
    try:
        columns = set()
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall():
            try:
                columns.add(str(row['name']))
            except Exception:
                columns.add(str(row[1]))
        return columns
    except Exception:
        return set()


def _has_table(conn, table: str) -> bool:
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table,),
        ).fetchone() is not None
    except Exception:
        return False


def _add_column(conn, table: str, name: str, definition: str) -> None:
    if name in _table_columns(conn, table):
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def ensure_memory_v2_schema(conn) -> None:
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS memory_events (
            event_id TEXT PRIMARY KEY,
            object_type TEXT NOT NULL DEFAULT '',
            object_id TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            source_turn_id TEXT NOT NULL DEFAULT '',
            source_event_id TEXT NOT NULL DEFAULT '',
            details_json TEXT NOT NULL DEFAULT '{}',
            confidence REAL NOT NULL DEFAULT 1.0,
            created_at REAL NOT NULL
        );
        """
    )
    for name, definition in (
        ('event_type', "TEXT NOT NULL DEFAULT ''"),
        ('session_id', "TEXT NOT NULL DEFAULT ''"),
        ('scope_key', "TEXT NOT NULL DEFAULT ''"),
        ('payload_json', "TEXT NOT NULL DEFAULT '{}'"),
        ('source', "TEXT NOT NULL DEFAULT 'hermes'"),
        ('eligible_for_compile', "INTEGER NOT NULL DEFAULT 1"),
        ('quarantined', "INTEGER NOT NULL DEFAULT 0"),
        ('event_fingerprint', "TEXT NOT NULL DEFAULT ''"),
    ):
        _add_column(conn, 'memory_events', name, definition)

    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_events_event_type ON memory_events(event_type, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_memory_events_session_scope ON memory_events(session_id, scope_key, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_memory_events_scope_type ON memory_events(scope_key, event_type, created_at DESC);

        CREATE TABLE IF NOT EXISTS memory_objects (
            object_id TEXT PRIMARY KEY,
            object_type TEXT NOT NULL,
            scope_key TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL DEFAULT '',
            source_event_ids_json TEXT NOT NULL DEFAULT '[]',
            source_session_ids_json TEXT NOT NULL DEFAULT '[]',
            title TEXT NOT NULL DEFAULT '',
            body TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            confidence REAL NOT NULL DEFAULT 0.5,
            priority REAL NOT NULL DEFAULT 0.5,
            relevance_tags_json TEXT NOT NULL DEFAULT '[]',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            expires_at REAL,
            superseded_by TEXT NOT NULL DEFAULT '',
            nucleus_eligible INTEGER NOT NULL DEFAULT 0,
            source_kind TEXT NOT NULL DEFAULT 'compiler',
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_memory_objects_scope_type_status ON memory_objects(scope_key, object_type, status, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_memory_objects_type_status ON memory_objects(object_type, status, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_memory_objects_nucleus ON memory_objects(scope_key, nucleus_eligible, updated_at DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_objects_one_active_task_scope
            ON memory_objects(scope_key)
            WHERE object_type='active_task' AND status='active' AND superseded_by='';

        CREATE TABLE IF NOT EXISTS task_transitions (
            transition_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            from_status TEXT NOT NULL DEFAULT '',
            to_status TEXT NOT NULL,
            reason_event_id TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_task_transitions_task ON task_transitions(task_id, created_at DESC);
        """
    )
    try:
        row = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE migration_id=? LIMIT 1",
            ('memory_architecture_v2',),
        ).fetchone()
        if not row:
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations (migration_id, summary, applied_at) VALUES (?, ?, ?)",
                ('memory_architecture_v2', 'Add raw event fields, compiled memory_objects, and task transitions.', _now()),
            )
    except Exception:
        pass


def classify_turn_control(user_message: str) -> dict[str, Any]:
    text = user_message or ''
    lowered = text.lower()
    cancel = bool(CANCEL_RE.search(text))
    continue_turn = bool(CONTINUE_RE.search(text))
    no_widen = bool(NO_WIDEN_RE.search(text))
    one_shot = bool(ONE_SHOT_RE.search(text)) and not bool(ONGOING_RE.search(text))
    ongoing = bool(ONGOING_RE.search(text))
    return {
        'cancel': cancel,
        'continue': continue_turn,
        'topic_switch': False if continue_turn or cancel else bool(ongoing and not one_shot),
        'interrupt_recovery': 'interrupted before you could process the last tool result' in lowered,
        'one_shot': one_shot,
        'implies_ongoing_work': bool(ongoing and not one_shot),
        'no_widen': no_widen,
        'fingerprint': _fingerprint(text),
    }


@dataclass
class SectionDecision:
    section: str
    selected_object_ids: list[str] = field(default_factory=list)
    source_sessions: list[str] = field(default_factory=list)
    score: float = 0.0
    selection_reason: str = ''
    rejection_reason: str = ''


class MemoryCompiler:
    def __init__(self, conn):
        self.conn = conn
        ensure_memory_v2_schema(conn)

    def record_event(
        self,
        event_type: str,
        payload: Optional[dict[str, Any]] = None,
        *,
        session_id: str = '',
        scope_key: str = '',
        source: str = 'hermes',
        created_at: Optional[float] = None,
        eligible_for_compile: bool = True,
    ) -> str:
        payload = payload or {}
        now = float(created_at or _now())
        text = _dumps(payload)
        quarantined = 1 if (
            event_type in {'assistant_turn', 'context_compaction'}
            and COMPACTION_RE.search(str(payload.get('text') or payload.get('summary') or ''))
        ) else 0
        event_id = stable_id('event', event_type, session_id, scope_key, _fingerprint(text), str(int(now * 1000)))
        self.conn.execute(
            """
            INSERT OR IGNORE INTO memory_events
            (event_id, object_type, object_id, action, reason, source_turn_id, source_event_id,
             details_json, confidence, created_at, event_type, session_id, scope_key, payload_json,
             source, eligible_for_compile, quarantined, event_fingerprint)
            VALUES (?, 'raw_event', ?, ?, '', '', '', '{}', 1.0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                event_id,
                event_type,
                now,
                event_type,
                session_id or '',
                scope_key or '',
                _dumps(payload),
                source or 'hermes',
                1 if eligible_for_compile else 0,
                quarantined,
                _fingerprint(text),
            ),
        )
        return event_id

    def upsert_object(
        self,
        *,
        object_type: str,
        object_id: str = '',
        scope_key: str = '',
        session_id: str = '',
        source_event_ids: Optional[Iterable[str]] = None,
        source_session_ids: Optional[Iterable[str]] = None,
        title: str = '',
        body: str = '',
        status: str = 'active',
        confidence: float = 0.5,
        priority: float = 0.5,
        relevance_tags: Optional[Iterable[str]] = None,
        expires_at: Optional[float] = None,
        superseded_by: str = '',
        nucleus_eligible: bool = False,
        source_kind: str = 'compiler',
        metadata: Optional[dict[str, Any]] = None,
        now: Optional[float] = None,
    ) -> str:
        if object_type not in MEMORY_OBJECT_TYPES:
            raise ValueError(f'unsupported memory object_type: {object_type}')
        now = float(now or _now())
        source_event_ids = [str(x) for x in (source_event_ids or []) if str(x)]
        source_session_ids = [str(x) for x in (source_session_ids or [session_id]) if str(x)]
        tags = sorted({str(tag).lower() for tag in (relevance_tags or []) if str(tag).strip()})
        object_id = object_id or stable_id('memobj', object_type, scope_key, title, body[:500])

        if object_type == 'active_task' and status == 'active':
            self._supersede_other_active_tasks(scope_key, object_id, now, reason='one_active_task_per_scope')

        self.conn.execute(
            """
            INSERT INTO memory_objects
            (object_id, object_type, scope_key, session_id, source_event_ids_json,
             source_session_ids_json, title, body, status, confidence, priority,
             relevance_tags_json, created_at, updated_at, expires_at, superseded_by,
             nucleus_eligible, source_kind, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(object_id) DO UPDATE SET
              source_event_ids_json=excluded.source_event_ids_json,
              source_session_ids_json=excluded.source_session_ids_json,
              title=excluded.title,
              body=excluded.body,
              status=excluded.status,
              confidence=excluded.confidence,
              priority=excluded.priority,
              relevance_tags_json=excluded.relevance_tags_json,
              updated_at=excluded.updated_at,
              expires_at=excluded.expires_at,
              superseded_by=excluded.superseded_by,
              nucleus_eligible=excluded.nucleus_eligible,
              source_kind=excluded.source_kind,
              metadata_json=excluded.metadata_json
            """,
            (
                object_id,
                object_type,
                scope_key or '',
                session_id or '',
                _dumps(source_event_ids),
                _dumps(source_session_ids),
                str(title or '')[:500],
                str(body or '')[:5000],
                status,
                float(confidence),
                float(priority),
                _dumps(tags),
                now,
                now,
                expires_at,
                superseded_by or '',
                1 if nucleus_eligible else 0,
                source_kind,
                _dumps(metadata or {}),
            ),
        )
        return object_id

    def _supersede_other_active_tasks(self, scope_key: str, keep_id: str, now: float, reason: str) -> None:
        rows = self.conn.execute(
            "SELECT object_id, status FROM memory_objects WHERE object_type='active_task' AND scope_key=? AND status='active' AND object_id<>?",
            (scope_key or '', keep_id),
        ).fetchall()
        for row in rows:
            self.conn.execute(
                "UPDATE memory_objects SET status='superseded', superseded_by=?, updated_at=? WHERE object_id=?",
                (keep_id, now, row['object_id']),
            )
            self._record_task_transition(row['object_id'], 'active', 'superseded', '', reason, now)

    def _record_task_transition(self, task_id: str, from_status: str, to_status: str, reason_event_id: str, reason: str, now: float) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO task_transitions
            (transition_id, task_id, from_status, to_status, reason_event_id, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (stable_id('task_transition', task_id, from_status, to_status, reason_event_id, str(int(now * 1000))), task_id, from_status or '', to_status, reason_event_id or '', reason[:300], now),
        )

    def compile_events(self, scope_key: str, session_id: str, event_ids: Iterable[str]) -> list[str]:
        ids = [str(event_id) for event_id in event_ids if str(event_id)]
        if not ids:
            return []
        placeholders = ','.join('?' for _ in ids)
        rows = self.conn.execute(
            f"SELECT * FROM memory_events WHERE event_id IN ({placeholders}) AND eligible_for_compile=1 ORDER BY created_at ASC",
            ids,
        ).fetchall()
        compiled: list[str] = []
        for row in rows:
            event_type = str(row['event_type'] or row['action'] or '')
            payload = _loads(row['payload_json'] or row['details_json'], {})
            text = str(payload.get('text') or payload.get('user_message') or payload.get('summary') or '')
            if not text or int(row['quarantined'] or 0):
                continue
            if event_type == 'user_turn':
                control = classify_turn_control(text)
                if control['cancel']:
                    self.abort_scope_tasks(scope_key or row['scope_key'], row['event_id'], reason='cancel_event')
                    continue
                if control['implies_ongoing_work']:
                    obj_id = self.create_or_refresh_task(
                        scope_key=scope_key or row['scope_key'],
                        session_id=session_id or row['session_id'],
                        user_message=text,
                        event_id=row['event_id'],
                        status='candidate',
                        now=float(row['created_at'] or _now()),
                    )
                    compiled.append(obj_id)
            elif event_type == 'context_compaction':
                obj_id = self.upsert_object(
                    object_type='open_loop',
                    object_id=stable_id('memobj', 'latest_recap', scope_key, session_id),
                    scope_key=scope_key,
                    session_id=session_id,
                    source_event_ids=[row['event_id']],
                    title='Latest recap',
                    body=text,
                    status='active',
                    confidence=0.7,
                    priority=0.3,
                    relevance_tags=_tokens(text),
                    source_kind='compaction_metadata',
                    metadata={'section': 'LATEST RECAP'},
                    now=float(row['created_at'] or _now()),
                )
                compiled.append(obj_id)
        return compiled

    def create_or_refresh_task(
        self,
        *,
        scope_key: str,
        session_id: str,
        user_message: str,
        event_id: str = '',
        status: str = 'active',
        priority: float = 0.7,
        now: Optional[float] = None,
    ) -> str:
        now = float(now or _now())
        tags = _tokens(user_message)
        object_id = stable_id('task', scope_key, _fingerprint(user_message))
        return self.upsert_object(
            object_type='active_task',
            object_id=object_id,
            scope_key=scope_key,
            session_id=session_id,
            source_event_ids=[event_id] if event_id else [],
            title=user_message[:180],
            body=user_message,
            status=status,
            confidence=0.8,
            priority=priority,
            relevance_tags=tags,
            source_kind='turn_control',
            metadata={'fingerprint': _fingerprint(user_message)},
            now=now,
        )

    def transition_task(self, task_id: str, new_status: str, reason_event_id: str = '', reason: str = '') -> bool:
        if new_status not in TASK_STATUSES:
            raise ValueError(f'unsupported task status: {new_status}')
        row = self.conn.execute(
            "SELECT object_id, status FROM memory_objects WHERE object_id=? AND object_type='active_task'",
            (task_id,),
        ).fetchone()
        if not row:
            return False
        old_status = str(row['status'] or '')
        if new_status not in TASK_TRANSITIONS.get(old_status, set()) and new_status != old_status:
            raise ValueError(f'invalid task transition: {old_status} -> {new_status}')
        now = _now()
        self.conn.execute(
            "UPDATE memory_objects SET status=?, updated_at=? WHERE object_id=?",
            (new_status, now, task_id),
        )
        self._record_task_transition(task_id, old_status, new_status, reason_event_id, reason, now)
        return True

    def abort_scope_tasks(self, scope_key: str, reason_event_id: str = '', reason: str = 'cancel_event') -> int:
        now = _now()
        rows = self.conn.execute(
            "SELECT object_id, status FROM memory_objects WHERE object_type='active_task' AND scope_key=? AND status IN ('candidate','active','blocked')",
            (scope_key or '',),
        ).fetchall()
        for row in rows:
            self.conn.execute(
                "UPDATE memory_objects SET status='aborted', updated_at=? WHERE object_id=?",
                (now, row['object_id']),
            )
            self._record_task_transition(row['object_id'], row['status'], 'aborted', reason_event_id, reason, now)
        return len(rows)

    def select_active_task(self, scope_key: str, lane: str, query_tags: Iterable[str], now: Optional[float] = None) -> Optional[str]:
        now = float(now or _now())
        max_age = 72 * 3600 if lane == 'continuation_or_resume' else 24 * 3600
        query_tag_set = {str(tag).lower() for tag in query_tags if str(tag)}
        rows = self.conn.execute(
            """
            SELECT * FROM memory_objects
            WHERE object_type='active_task'
              AND scope_key=?
              AND status='active'
              AND superseded_by=''
              AND updated_at >= ?
            ORDER BY priority DESC, updated_at DESC
            LIMIT 20
            """,
            (scope_key or '', now - max_age),
        ).fetchall()
        candidates = []
        for row in rows:
            tags = set(_json_list(row['relevance_tags_json']))
            if query_tag_set and tags and not (query_tag_set & tags) and lane != 'continuation_or_resume':
                continue
            if self._blocked_by_cancel_or_switch(row['scope_key'], float(row['updated_at'] or 0)):
                continue
            overlap = len(query_tag_set & tags) if tags else 0
            candidates.append((float(row['priority'] or 0), overlap, float(row['updated_at'] or 0), row['object_id']))
        if not candidates:
            return None
        candidates.sort(reverse=True)
        return candidates[0][3]

    def _blocked_by_cancel_or_switch(self, scope_key: str, updated_at: float) -> bool:
        row = self.conn.execute(
            """
            SELECT 1 FROM memory_events
            WHERE scope_key=? AND created_at > ?
              AND event_type IN ('cancel_event','topic_switch')
            ORDER BY created_at DESC LIMIT 1
            """,
            (scope_key or '', updated_at),
        ).fetchone()
        return row is not None

    def can_replay_unfinished_tool_result(self, session_id: str, scope_key: str, active_task_id: str) -> bool:
        task = self.conn.execute(
            "SELECT metadata_json, updated_at FROM memory_objects WHERE object_id=? AND object_type='active_task' AND scope_key=? AND status='active'",
            (active_task_id, scope_key or ''),
        ).fetchone()
        if not task:
            return False
        updated_at = float(task['updated_at'] or 0)
        row = self.conn.execute(
            """
            SELECT 1 FROM memory_events
            WHERE session_id=? AND scope_key=? AND created_at > ?
              AND event_type IN ('cancel_event','topic_switch')
            ORDER BY created_at DESC LIMIT 1
            """,
            (session_id or '', scope_key or '', updated_at),
        ).fetchone()
        return row is None

    def backfill_compiled_objects(self, scope_key: str = '', *, limit: int = 400) -> dict[str, int]:
        stats = {
            'active_task': 0,
            'semantic_fact': 0,
            'user_preference': 0,
            'verified_artifact': 0,
            'fix_recipe': 0,
            'validated_cause': 0,
            'open_hypothesis': 0,
            'open_loop': 0,
            'verification_debt': 0,
            'instruction_proposal': 0,
        }
        self._backfill_work_items(scope_key, limit, stats)
        self._backfill_facts(scope_key, limit, stats)
        self._backfill_beliefs(scope_key, limit, stats)
        self._backfill_artifacts(scope_key, limit, stats)
        self._backfill_recipes(scope_key, limit, stats)
        self._backfill_open_loops(scope_key, limit, stats)
        self._backfill_pending_verifications(scope_key, limit, stats)
        self._backfill_instruction_proposals(scope_key, limit, stats)
        return stats

    def _backfill_work_items(self, scope_key: str, limit: int, stats: dict[str, int]) -> None:
        if not _has_table(self.conn, 'work_items'):
            return
        where = "WHERE scope_key=?" if scope_key else ""
        params: list[Any] = [scope_key] if scope_key else []
        rows = self.conn.execute(
            f"SELECT * FROM work_items {where} ORDER BY updated_at DESC LIMIT ?",
            (*params, int(limit)),
        ).fetchall()
        for row in rows:
            title = str(row['title'] or '')
            if LOW_VALUE_WORK_RE.match(title):
                continue
            status = row['status'] if row['status'] in TASK_STATUSES else ('active' if row['status'] in {'active', 'blocked'} else 'resolved')
            object_type = 'active_task' if status in {'candidate', 'active', 'blocked', 'resolved', 'aborted', 'superseded', 'stale'} else 'open_loop'
            object_id = f"compat:{object_type}:{row['work_item_id']}"
            body = '\n'.join(part for part in (str(row['root_cause'] or ''), str(row['next_step'] or ''), str(row['evidence_json'] or '')) if part)
            self.upsert_object(
                object_type=object_type,
                object_id=object_id,
                scope_key=row['scope_key'],
                session_id=row['session_id'],
                source_event_ids=[str(row['source_event_id'] or '')],
                source_session_ids=[row['session_id']],
                title=title,
                body=body,
                status=status,
                confidence=0.72,
                priority=float(row['priority'] or 0.5),
                relevance_tags=_tokens(title + ' ' + body),
                source_kind='compat:work_items',
                metadata={'legacy_id': row['work_item_id'], 'next_step': row['next_step'] or '', 'root_cause': row['root_cause'] or ''},
                now=float(row['updated_at'] or _now()),
            )
            stats[object_type] += 1

    def _backfill_facts(self, scope_key: str, limit: int, stats: dict[str, int]) -> None:
        if not _has_table(self.conn, 'facts'):
            return
        where = "WHERE status='active' AND confidence>=0.75"
        params: list[Any] = []
        if scope_key:
            where += " AND scope_key=?"
            params.append(scope_key)
        rows = self.conn.execute(
            f"SELECT * FROM facts {where} ORDER BY valid_from DESC LIMIT ?",
            (*params, int(limit)),
        ).fetchall()
        for row in rows:
            fact_type = str(row['fact_type'] or '')
            if fact_type == 'ruled_out_cause':
                object_type = 'open_hypothesis'
            elif fact_type == 'next_action_memory':
                object_type = 'open_loop'
            elif fact_type == 'validated_cause':
                object_type = 'validated_cause'
            elif fact_type in {'user_preference', 'preference'}:
                object_type = 'user_preference'
            else:
                object_type = 'semantic_fact'
            metadata = {'legacy_id': row['fact_id'], 'fact_type': fact_type, 'source_kind': row['source_kind'] or ''}
            if fact_type == 'next_action_memory':
                metadata['section'] = 'NEXT REQUIRED ACTION'
            self.upsert_object(
                object_type=object_type,
                object_id=f"compat:{object_type}:{row['fact_id']}",
                scope_key=row['scope_key'],
                session_id=row['session_id'],
                source_event_ids=[str(row['source_event_id'] or '')],
                source_session_ids=[row['session_id']],
                title=fact_type or 'Fact',
                body=row['fact_text'],
                status='active',
                confidence=float(row['confidence'] or 0.75),
                priority=0.5,
                relevance_tags=_tokens(row['fact_text']),
                source_kind='compat:facts',
                metadata=metadata,
                now=float(row['valid_from'] or _now()),
            )
            stats[object_type] += 1

    def _backfill_beliefs(self, scope_key: str, limit: int, stats: dict[str, int]) -> None:
        if not _has_table(self.conn, 'beliefs'):
            return
        where = "WHERE status IN ('open','validated') AND confidence>=0.45"
        params: list[Any] = []
        if scope_key:
            where += " AND scope_key=?"
            params.append(scope_key)
        rows = self.conn.execute(
            f"SELECT * FROM beliefs {where} ORDER BY updated_at DESC LIMIT ?",
            (*params, int(limit)),
        ).fetchall()
        for row in rows:
            kind = str(row['belief_kind'] or '')
            if kind == 'ruled_out_cause':
                object_type = 'open_hypothesis'
            elif row['status'] == 'validated' or kind == 'validated_cause':
                object_type = 'validated_cause'
            else:
                object_type = 'open_hypothesis'
            self.upsert_object(
                object_type=object_type,
                object_id=f"compat:{object_type}:{row['belief_id']}",
                scope_key=row['scope_key'],
                session_id=row['session_id'],
                source_event_ids=[str(row['source_event_id'] or '')],
                source_session_ids=[row['session_id']],
                title=kind or object_type,
                body=row['claim_text'],
                status='active',
                confidence=float(row['confidence'] or 0.5),
                priority=0.65 if object_type == 'validated_cause' else 0.45,
                relevance_tags=_tokens(row['claim_text']),
                source_kind='compat:beliefs',
                nucleus_eligible=object_type == 'validated_cause',
                metadata={'legacy_id': row['belief_id'], 'belief_kind': kind},
                now=float(row['updated_at'] or _now()),
            )
            stats[object_type] += 1

    def _backfill_artifacts(self, scope_key: str, limit: int, stats: dict[str, int]) -> None:
        if not _has_table(self.conn, 'verified_artifacts'):
            return
        rows = self.conn.execute(
            "SELECT * FROM verified_artifacts WHERE status='verified' ORDER BY updated_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        for row in rows:
            obj_scope = scope_key or str(row['project_key'] or '')
            body = f"{row['role']}: {row['path']}"
            if row['label']:
                body = f"{body} ({row['label']})"
            self.upsert_object(
                object_type='verified_artifact',
                object_id=f"compat:verified_artifact:{row['artifact_id']}",
                scope_key=obj_scope,
                session_id='',
                title=row['label'] or row['role'] or 'Verified artifact',
                body=body,
                status='active',
                confidence=float(row['confidence'] or 1.0),
                priority=0.75,
                relevance_tags=_tokens(body + ' ' + str(row['scope_tags_json'] or '')),
                source_kind='compat:verified_artifacts',
                nucleus_eligible=True,
                metadata={'legacy_id': row['artifact_id'], 'path': row['path'], 'role': row['role']},
                now=float(row['updated_at'] or _now()),
            )
            stats['verified_artifact'] += 1

    def _backfill_recipes(self, scope_key: str, limit: int, stats: dict[str, int]) -> None:
        if not _has_table(self.conn, 'fix_recipes'):
            return
        where = "WHERE status='active' AND promotion_status='active' AND (artifact_verified=1 OR times_confirmed>=2)"
        params: list[Any] = []
        if scope_key:
            where += " AND scope_key=?"
            params.append(scope_key)
        rows = self.conn.execute(
            f"SELECT * FROM fix_recipes {where} ORDER BY confidence DESC, times_confirmed DESC, updated_at DESC LIMIT ?",
            (*params, int(limit)),
        ).fetchall()
        for row in rows:
            body = f"{row['problem_pattern']}\nTool: {row['tool_name']}\nVerify: {row['success_criteria']}"
            self.upsert_object(
                object_type='fix_recipe',
                object_id=f"compat:fix_recipe:{row['recipe_id']}",
                scope_key=row['scope_key'],
                session_id='',
                title=row['problem_pattern'],
                body=body,
                status='active',
                confidence=float(row['confidence'] or 0.7),
                priority=0.7 + min(float(row['times_confirmed'] or 0), 5.0) / 20.0,
                relevance_tags=_tokens(body + ' ' + str(row['tool_name'] or '')),
                source_kind='compat:fix_recipes',
                nucleus_eligible=True,
                metadata={'legacy_id': row['recipe_id'], 'tool_name': row['tool_name'], 'times_confirmed': row['times_confirmed']},
                now=float(row['updated_at'] or _now()),
            )
            stats['fix_recipe'] += 1

    def _backfill_open_loops(self, scope_key: str, limit: int, stats: dict[str, int]) -> None:
        if not _has_table(self.conn, 'open_loops'):
            return
        where = "WHERE status IN ('active','blocked')"
        params: list[Any] = []
        if scope_key:
            where += " AND scope_key=?"
            params.append(scope_key)
        rows = self.conn.execute(
            f"SELECT * FROM open_loops {where} ORDER BY priority DESC, updated_at DESC LIMIT ?",
            (*params, int(limit)),
        ).fetchall()
        cutoff = _now() - 86400
        for row in rows:
            title = str(row['title'] or '')
            if float(row['updated_at'] or 0) < cutoff and GENERIC_INFRA_RE.search(title):
                continue
            body = str(row['next_action'] or '')
            self.upsert_object(
                object_type='open_loop',
                object_id=f"compat:open_loop:{row['loop_id']}",
                scope_key=row['scope_key'],
                session_id='',
                source_event_ids=_json_list(row['source_event_ids_json']),
                title=title,
                body=body,
                status='active',
                confidence=0.7,
                priority=float(row['priority'] or 0.5),
                relevance_tags=_tokens(title + ' ' + body),
                source_kind='compat:open_loops',
                nucleus_eligible=bool(int(row['priority'] or 0) >= 1),
                metadata={'legacy_id': row['loop_id'], 'next_action': body},
                now=float(row['updated_at'] or _now()),
            )
            stats['open_loop'] += 1

    def _backfill_pending_verifications(self, scope_key: str, limit: int, stats: dict[str, int]) -> None:
        if not _has_table(self.conn, 'pending_verifications'):
            return
        where = "WHERE status='pending'"
        params: list[Any] = []
        if scope_key:
            where += " AND scope_key=?"
            params.append(scope_key)
        rows = self.conn.execute(
            f"SELECT * FROM pending_verifications {where} ORDER BY created_at DESC LIMIT ?",
            (*params, int(limit)),
        ).fetchall()
        for row in rows:
            body = f"{row['path']}\nVerify with: {row['suggested_command']}"
            self.upsert_object(
                object_type='verification_debt',
                object_id=f"compat:verification_debt:{row['verification_id']}",
                scope_key=row['scope_key'],
                session_id=row['session_id'],
                title=row['path'],
                body=body,
                status='active',
                confidence=0.8,
                priority=0.75,
                relevance_tags=_tokens(body),
                source_kind='compat:pending_verifications',
                metadata={'legacy_id': row['verification_id'], 'tool_name': row['tool_name'], 'suggested_command': row['suggested_command']},
                now=float(row['created_at'] or _now()),
            )
            stats['verification_debt'] += 1

    def _backfill_instruction_proposals(self, scope_key: str, limit: int, stats: dict[str, int]) -> None:
        if not _has_table(self.conn, 'self_evolution_proposals'):
            return
        where = "WHERE status IN ('approved','needs_approval')"
        params: list[Any] = []
        if scope_key:
            where += " AND scope_key=?"
            params.append(scope_key)
        rows = self.conn.execute(
            f"SELECT * FROM self_evolution_proposals {where} ORDER BY updated_at DESC LIMIT ?",
            (*params, int(limit // 2 or 1)),
        ).fetchall()
        for row in rows:
            approved = row['status'] == 'approved'
            self.upsert_object(
                object_type='instruction_proposal',
                object_id=f"compat:instruction_proposal:{row['proposal_id']}",
                scope_key=row['scope_key'],
                session_id=row['session_id'],
                title=row['proposal_type'],
                body=row['proposed_action'] or row['rationale'],
                status='active' if approved else 'candidate',
                confidence=0.65 if approved else 0.35,
                priority=float(row['risk_score'] or 0.5),
                relevance_tags=_tokens(f"{row['proposal_type']} {row['target_area']} {row['proposed_action']}"),
                source_kind='compat:self_evolution_proposals',
                nucleus_eligible=approved,
                metadata={'legacy_id': row['proposal_id'], 'requires_approval': row['requires_approval']},
                now=float(row['updated_at'] or _now()),
            )
            stats['instruction_proposal'] += 1

    def cleanup_backfill(self, *, dry_run: bool = True, now: Optional[float] = None) -> dict[str, Any]:
        now = float(now or _now())
        report: dict[str, Any] = {
            'dry_run': bool(dry_run),
            'low_value_work_items': [],
            'stale_document_tasks': [],
            'generic_old_open_loops': [],
            'stale_active_tasks': [],
            'compiled_objects': {},
        }
        if _has_table(self.conn, 'work_items'):
            for row in self.conn.execute(
                "SELECT * FROM work_items WHERE status IN ('active','blocked','candidate') ORDER BY updated_at ASC LIMIT 1000"
            ).fetchall():
                title = str(row['title'] or '')
                if LOW_VALUE_WORK_RE.match(title):
                    report['low_value_work_items'].append(row['work_item_id'])
                elif DOCUMENT_RE.search(title) and float(row['updated_at'] or 0) < now - 86400:
                    report['stale_document_tasks'].append(row['work_item_id'])
            if not dry_run:
                for work_id in report['low_value_work_items']:
                    self.conn.execute(
                        "UPDATE work_items SET status='resolved', resolved_at=COALESCE(resolved_at, ?), updated_at=? WHERE work_item_id=?",
                        (now, now, work_id),
                    )
                for work_id in report['stale_document_tasks']:
                    self.conn.execute(
                        "UPDATE work_items SET status='aborted', resolved_at=COALESCE(resolved_at, ?), updated_at=? WHERE work_item_id=?",
                        (now, now, work_id),
                    )
        if _has_table(self.conn, 'open_loops'):
            rows = self.conn.execute(
                "SELECT loop_id, title FROM open_loops WHERE status IN ('active','blocked') AND updated_at < ? ORDER BY updated_at ASC LIMIT 500",
                (now - 86400,),
            ).fetchall()
            for row in rows:
                if GENERIC_INFRA_RE.search(str(row['title'] or '')):
                    report['generic_old_open_loops'].append(row['loop_id'])
            if not dry_run:
                for loop_id in report['generic_old_open_loops']:
                    self.conn.execute(
                        "UPDATE open_loops SET status='stale', updated_at=? WHERE loop_id=?",
                        (now, loop_id),
                    )
        for row in self.conn.execute(
            "SELECT object_id FROM memory_objects WHERE object_type='active_task' AND status='active' AND updated_at < ?",
            (now - 86400,),
        ).fetchall():
            report['stale_active_tasks'].append(row['object_id'])
        if not dry_run:
            for object_id in report['stale_active_tasks']:
                self.transition_task(object_id, 'stale', reason='stale_objective>24h')
        report['compiled_objects'] = self.backfill_compiled_objects(limit=800)
        return report

    def get_nucleus_feed(self, scope_key: str = '', limit: int = 50, min_confidence: float = 0.75) -> list[dict[str, Any]]:
        allowed = ('validated_cause', 'verified_artifact', 'fix_recipe', 'instruction_proposal', 'open_loop')
        params: list[Any] = [float(min_confidence)]
        where = "object_type IN ({}) AND status='active' AND confidence>=? AND superseded_by=''".format(','.join('?' for _ in allowed))
        params = list(allowed) + params
        if scope_key:
            where += " AND scope_key=?"
            params.append(scope_key)
        where += " AND (object_type!='open_loop' OR nucleus_eligible=1)"
        rows = self.conn.execute(
            f"SELECT * FROM memory_objects WHERE {where} ORDER BY priority DESC, updated_at DESC LIMIT ?",
            (*params, int(limit)),
        ).fetchall()
        return [dict(row) for row in rows]

    def record_nucleus_output(self, object_type: str, payload: dict[str, Any], confidence: float = 0.35, source: str = 'nucleus') -> str:
        if object_type not in {'open_hypothesis', 'instruction_proposal', 'semantic_fact', 'fix_recipe'}:
            raise ValueError(f'Nucleus cannot write object_type={object_type}')
        scope_key = str(payload.get('scope_key') or 'nucleus')
        session_id = str(payload.get('session_id') or '')
        title = str(payload.get('title') or payload.get('problem') or object_type)
        body = str(payload.get('body') or payload.get('fact_text') or payload.get('proposed_action') or payload.get('steps') or '')
        return self.upsert_object(
            object_type=object_type,
            object_id=str(payload.get('object_id') or ''),
            scope_key=scope_key,
            session_id=session_id,
            title=title,
            body=body,
            status='candidate',
            confidence=min(float(confidence), 0.65),
            priority=float(payload.get('priority') or 0.4),
            relevance_tags=_tokens(f'{title} {body}'),
            source_kind=source,
            nucleus_eligible=False,
            metadata={'payload': payload, 'trust': 'low_until_verified'},
        )


def build_context_from_objects(
    conn,
    *,
    scope_key: str,
    session_id: str,
    lane: str,
    user_message: str,
    now: Optional[float] = None,
) -> tuple[str, dict[str, Any]]:
    compiler = MemoryCompiler(conn)
    now = float(now or _now())
    compiler.backfill_compiled_objects(scope_key=scope_key, limit=200)
    query_tokens = _tokens(user_message)
    control = classify_turn_control(user_message)
    no_widen = bool(control.get('no_widen'))
    allowed_sections = set(LANE_SECTION_ALLOWLIST.get(lane, set(SECTION_PRECEDENCE)))
    if no_widen:
        allowed_sections -= {'CONTINUITY MEMORY', 'OPEN BUG', 'INFRASTRUCTURE'}
    if lane == 'chit_chat':
        return '', {'selected': [], 'rejected': [], 'section_decisions': [], 'control': control}

    selected: dict[str, list[tuple[float, Any, str]]] = {section: [] for section in SECTION_PRECEDENCE}
    rejected: list[dict[str, Any]] = []

    active_task_id = compiler.select_active_task(scope_key, lane, query_tokens, now=now)
    if active_task_id:
        row = conn.execute("SELECT * FROM memory_objects WHERE object_id=?", (active_task_id,)).fetchone()
        if row:
            selected['ACTIVE TASK'].append((1.0, row, 'active_task_selection'))
            metadata = _loads(row['metadata_json'], {})
            next_step = str(metadata.get('next_step') or '')
            if next_step:
                selected['NEXT REQUIRED ACTION'].append((0.95, row, 'active_task_next_step'))

    rows = conn.execute(
        """
        SELECT * FROM memory_objects
        WHERE scope_key=?
          AND status='active'
          AND superseded_by=''
          AND (expires_at IS NULL OR expires_at>?)
        ORDER BY priority DESC, updated_at DESC
        LIMIT 400
        """,
        (scope_key or '', now),
    ).fetchall()
    for row in rows:
        object_id = str(row['object_id'] or '')
        if object_id == active_task_id:
            continue
        section = _section_for_object(row)
        reason = _eligible_reason(row, lane, query_tokens, user_message, now, no_widen)
        if not reason:
            rejected.append({'object_id': object_id, 'object_type': row['object_type'], 'section': section, 'rejection_reason': 'lane_or_relevance_filter'})
            continue
        if section not in allowed_sections:
            rejected.append({'object_id': object_id, 'object_type': row['object_type'], 'section': section, 'rejection_reason': f'section_not_allowed_for_lane:{lane}'})
            continue
        score = _object_score(row, query_tokens, now)
        selected.setdefault(section, []).append((score, row, reason))

    section_limits = {
        'VERIFIED ARTIFACTS': 5,
        'KNOWN FACTS': 5,
        'PROVEN FIX': 3,
        'OPEN BUG': 2,
        'CONTINUITY MEMORY': 4,
        'VERIFICATION REQUIRED': 4,
        'ACTIVE TASK': 1,
        'NEXT REQUIRED ACTION': 2,
        'MUST FOLLOW': 4,
    }
    parts: list[str] = []
    decisions: list[dict[str, Any]] = []
    for section in SECTION_PRECEDENCE:
        if section not in allowed_sections:
            continue
        items = selected.get(section) or []
        if not items:
            continue
        items.sort(key=lambda item: item[0], reverse=True)
        lines: list[str] = []
        object_ids: list[str] = []
        source_sessions: set[str] = set()
        for score, row, reason in items[:section_limits.get(section, 3)]:
            line = _line_for_object(section, row)
            if not line or line in lines:
                continue
            lines.append(line)
            object_ids.append(row['object_id'])
            for source_session in _json_list(row['source_session_ids_json']):
                if source_session:
                    source_sessions.add(source_session)
        if not lines:
            continue
        parts.append(f"{section}:\n- " + "\n- ".join(lines))
        decisions.append({
            'section': section,
            'selected_object_ids': object_ids,
            'source_sessions': sorted(source_sessions),
            'age_seconds': [round(now - float((row['updated_at'] or now)), 3) for _, row, _ in items[:len(object_ids)]],
            'score': [round(score, 4) for score, _, _ in items[:len(object_ids)]],
            'selection_reason': [reason for _, _, reason in items[:len(object_ids)]],
        })

    context = "LIVE BRAIN\n" + "\n".join(parts) if parts else ''
    trace = {
        'selected': [
            {
                'object_id': row['object_id'],
                'object_type': row['object_type'],
                'section': section,
                'score': round(score, 4),
                'selection_reason': reason,
                'age_seconds': round(now - float(row['updated_at'] or now), 3),
                'source_sessions': _json_list(row['source_session_ids_json']),
            }
            for section, items in selected.items()
            for score, row, reason in items
            if section in allowed_sections
        ],
        'rejected': rejected[:80],
        'section_decisions': decisions,
        'control': control,
        'selected_active_task_id': active_task_id,
    }
    return context, trace


def _section_for_object(row: Any) -> str:
    object_type = str(row['object_type'] or '')
    if object_type == 'active_task':
        return 'ACTIVE TASK'
    if object_type == 'semantic_fact' or object_type == 'validated_cause':
        return 'KNOWN FACTS'
    if object_type == 'user_preference':
        return 'MUST FOLLOW'
    if object_type == 'verified_artifact':
        return 'VERIFIED ARTIFACTS'
    if object_type == 'fix_recipe':
        return 'PROVEN FIX'
    if object_type == 'open_hypothesis':
        return 'OPEN BUG'
    if object_type == 'open_loop':
        metadata = _loads(row['metadata_json'], {})
        if metadata.get('section') == 'LATEST RECAP':
            return 'LATEST RECAP'
        if metadata.get('section') == 'NEXT REQUIRED ACTION':
            return 'NEXT REQUIRED ACTION'
        return 'CONTINUITY MEMORY'
    if object_type == 'verification_debt':
        return 'VERIFICATION REQUIRED'
    if object_type == 'instruction_proposal':
        return 'PENDING APPROVAL'
    return 'KNOWN FACTS'


def _eligible_reason(row: Any, lane: str, query_tokens: set[str], user_message: str, now: float, no_widen: bool) -> str:
    object_type = str(row['object_type'] or '')
    updated_at = float(row['updated_at'] or 0)
    age = now - updated_at
    tags = set(_json_list(row['relevance_tags_json']))
    text = f"{row['title'] or ''} {row['body'] or ''}".lower()
    overlap = bool(query_tokens & tags) or any(token in text for token in query_tokens if len(token) > 3)
    if object_type in {'semantic_fact', 'validated_cause'}:
        if float(row['confidence'] or 0) < 0.75:
            return ''
        return 'fact_overlap' if overlap else ('bounded_scope_fact' if lane in {'simple_execution', 'deep_execution'} and age < 86400 else '')
    if object_type == 'fix_recipe':
        metadata = _loads(row['metadata_json'], {})
        if int(metadata.get('times_confirmed') or 0) < 2 and float(row['confidence'] or 0) < 0.8:
            return ''
        return 'verified_recipe_match' if overlap else ''
    if object_type == 'open_hypothesis':
        return 'open_hypothesis_match' if overlap and age < 86400 and lane == 'deep_execution' and not no_widen else ''
    if object_type == 'open_loop':
        metadata = _loads(row['metadata_json'], {})
        if metadata.get('section') == 'NEXT REQUIRED ACTION':
            return 'next_action_match' if (overlap or lane == 'continuation_or_resume') else ''
        if no_widen or age > 86400:
            return ''
        return 'continuity_match' if (overlap or lane == 'continuation_or_resume') else ''
    if object_type == 'verified_artifact':
        return 'artifact_match' if (overlap or lane in {'document_intake', 'continuation_or_resume', 'deep_execution'}) else ''
    if object_type == 'verification_debt':
        return 'pending_verification'
    if object_type == 'user_preference':
        return 'user_preference'
    if object_type == 'instruction_proposal':
        return 'pending_approval' if lane == 'approval_flow' else ''
    return ''


def _object_score(row: Any, query_tokens: set[str], now: float) -> float:
    tags = set(_json_list(row['relevance_tags_json']))
    overlap = len(query_tokens & tags)
    age_hours = max((now - float(row['updated_at'] or now)) / 3600.0, 0.0)
    recency = 1.0 / (1.0 + age_hours / 24.0)
    return float(row['priority'] or 0) + float(row['confidence'] or 0) + overlap * 0.25 + recency * 0.2


def _line_for_object(section: str, row: Any) -> str:
    title = str(row['title'] or '').strip()
    body = str(row['body'] or '').strip()
    metadata = _loads(row['metadata_json'], {})
    if section == 'ACTIVE TASK':
        line = f"Task: {title[:180]}"
        if row['status']:
            line += f"; Status: {row['status']}"
        next_step = str(metadata.get('next_step') or '').strip()
        if next_step:
            line += f"; Next: {next_step[:180]}"
        return line
    if section == 'NEXT REQUIRED ACTION':
        next_step = str(metadata.get('next_step') or body or title).strip()
        return next_step[:220]
    if section == 'VERIFIED ARTIFACTS':
        path = str(metadata.get('path') or '').strip()
        return f"{title}: {path}"[:240] if path else (body or title)[:240]
    if section == 'PROVEN FIX':
        tool = str(metadata.get('tool_name') or '').strip()
        return f"{title[:140]} ({tool})" if tool else title[:180]
    if section == 'VERIFICATION REQUIRED':
        cmd = str(metadata.get('suggested_command') or '').strip()
        return f"{title}: verify with {cmd}"[:240] if cmd else (body or title)[:240]
    if section == 'MUST FOLLOW':
        return (body or title)[:240]
    return (body or title)[:240]
