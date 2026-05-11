from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Dict, Iterable, Optional

from .utils import stable_id

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    migration_id TEXT PRIMARY KEY,
    summary TEXT NOT NULL DEFAULT '',
    applied_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_events (
    event_id TEXT PRIMARY KEY,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    source_turn_id TEXT NOT NULL DEFAULT '',
    source_event_id TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}',
    confidence REAL NOT NULL DEFAULT 1.0,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_events_object ON memory_events(object_type, object_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_events_action ON memory_events(action, created_at DESC);

CREATE TABLE IF NOT EXISTS object_revisions (
    revision_id TEXT PRIMARY KEY,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    source_turn_id TEXT NOT NULL DEFAULT '',
    source_event_id TEXT NOT NULL DEFAULT '',
    before_json TEXT NOT NULL DEFAULT '{}',
    after_json TEXT NOT NULL DEFAULT '{}',
    event_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_object_revisions_object ON object_revisions(object_type, object_id, created_at DESC);

CREATE TABLE IF NOT EXISTS evidence_packets (
    evidence_packet_id TEXT PRIMARY KEY,
    scope_key TEXT NOT NULL DEFAULT '',
    object_type TEXT NOT NULL DEFAULT '',
    object_id TEXT NOT NULL DEFAULT '',
    claim TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL DEFAULT '',
    source_urls_json TEXT NOT NULL DEFAULT '[]',
    source_ids_json TEXT NOT NULL DEFAULT '[]',
    authority TEXT NOT NULL DEFAULT 'unknown',
    raw_excerpt_hash TEXT NOT NULL DEFAULT '',
    raw_excerpt_preview TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.5,
    valid_until REAL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evidence_packets_object ON evidence_packets(object_type, object_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_evidence_packets_scope ON evidence_packets(scope_key, created_at DESC);

CREATE TABLE IF NOT EXISTS maintenance_runs (
    run_id TEXT PRIMARY KEY,
    run_type TEXT NOT NULL,
    dry_run INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'started',
    summary_json TEXT NOT NULL DEFAULT '{}',
    started_at REAL NOT NULL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_maintenance_runs_type ON maintenance_runs(run_type, started_at DESC);
"""

_AUDIT_COLUMNS = {
    'facts': [('evidence_packet_id', 'TEXT', "''"), ('source_turn_id', 'TEXT', "''"), ('source_event_id', 'TEXT', "''")],
    'beliefs': [('evidence_packet_id', 'TEXT', "''"), ('source_turn_id', 'TEXT', "''"), ('source_event_id', 'TEXT', "''")],
    'rules': [('source_turn_id', 'TEXT', "''"), ('source_event_id', 'TEXT', "''")],
    'work_items': [('source_turn_id', 'TEXT', "''"), ('source_event_id', 'TEXT', "''")],
    'fix_recipes': [('source_turn_id', 'TEXT', "''"), ('source_event_id', 'TEXT', "''")],
    'verified_artifacts': [('source_turn_id', 'TEXT', "''"), ('source_event_id', 'TEXT', "''")],
    'epistemic_learned_facts': [('evidence_packet_id', 'TEXT', "''")],
}


def dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)


def row_to_dict(row: Any) -> Dict[str, Any]:
    if not row:
        return {}
    try:
        return dict(row)
    except Exception:
        return {}


def _table_columns(conn, table: str) -> set[str]:
    try:
        return {str(row['name']) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception as exc:
        logger.debug("[live_brain] audit schema could not inspect %s: %s", table, exc)
        return set()


def _add_column_if_missing(conn, table: str, name: str, definition: str) -> bool:
    columns = _table_columns(conn, table)
    if not columns:
        logger.debug("[live_brain] audit schema table not ready for column migration: %s", table)
        return False
    if name in columns:
        return False
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        logger.info("[live_brain] audit schema added column %s.%s", table, name)
        return True
    except Exception as exc:
        if 'duplicate column' in str(exc).lower():
            logger.debug("[live_brain] audit schema column already exists %s.%s", table, name)
            return False
        logger.warning("[live_brain] audit schema migration failed for %s.%s: %s", table, name, exc)
        raise


def ensure_schema(conn) -> None:
    conn.executescript(SCHEMA_SQL)
    for table, columns in _AUDIT_COLUMNS.items():
        for name, dtype, default in columns:
            _add_column_if_missing(conn, table, name, f"{dtype} NOT NULL DEFAULT {default}")
    try:
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (migration_id, summary, applied_at) VALUES (?, ?, ?)",
            ('audit_spine_v1', 'Add memory events, object revisions, evidence packets, and maintenance runs.', time.time()),
        )
    except Exception as exc:
        logger.debug("[live_brain] audit schema migration marker skipped: %s", exc)


def record_memory_event(
    conn,
    *,
    object_type: str,
    object_id: str,
    action: str,
    reason: str = '',
    details: Optional[Dict[str, Any]] = None,
    source_turn_id: str = '',
    source_event_id: str = '',
    confidence: float = 1.0,
    created_at: Optional[float] = None,
) -> str:
    ensure_schema(conn)
    now = float(created_at or time.time())
    details = details or {}
    event_id = stable_id('memory_event', object_type, object_id, action, reason, dumps(details), str(int(now * 1000)))
    conn.execute(
        """
        INSERT OR REPLACE INTO memory_events
        (event_id, object_type, object_id, action, reason, source_turn_id, source_event_id, details_json, confidence, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (event_id, object_type, object_id, action, reason[:300], source_turn_id or '', source_event_id or '', dumps(details), float(confidence), now),
    )
    try:
        conn.execute(
            "INSERT OR IGNORE INTO audit_log (audit_id, object_type, object_id, action, reason, details_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (stable_id('audit', event_id), object_type, object_id, action, reason[:240], dumps({'memory_event_id': event_id, **details}), now),
        )
    except Exception:
        pass
    return event_id


def record_revision(
    conn,
    *,
    object_type: str,
    object_id: str,
    action: str,
    before: Optional[Dict[str, Any]] = None,
    after: Optional[Dict[str, Any]] = None,
    reason: str = '',
    source_turn_id: str = '',
    source_event_id: str = '',
    created_at: Optional[float] = None,
) -> str:
    ensure_schema(conn)
    now = float(created_at or time.time())
    before = before or {}
    after = after or {}
    event_id = record_memory_event(
        conn,
        object_type=object_type,
        object_id=object_id,
        action=action,
        reason=reason,
        details={'has_before': bool(before), 'has_after': bool(after)},
        source_turn_id=source_turn_id,
        source_event_id=source_event_id,
        created_at=now,
    )
    revision_id = stable_id('revision', object_type, object_id, action, event_id)
    conn.execute(
        """
        INSERT OR REPLACE INTO object_revisions
        (revision_id, object_type, object_id, action, reason, source_turn_id, source_event_id, before_json, after_json, event_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (revision_id, object_type, object_id, action, reason[:300], source_turn_id or '', source_event_id or '', dumps(before), dumps(after), event_id, now),
    )
    return revision_id


def record_evidence_packet(
    conn,
    *,
    claim: str,
    source_urls: Optional[Iterable[str]] = None,
    source_ids: Optional[Iterable[str]] = None,
    authority: str = 'unknown',
    raw_excerpt: str = '',
    confidence: float = 0.5,
    valid_until: Optional[float] = None,
    source_kind: str = '',
    scope_key: str = '',
    object_type: str = '',
    object_id: str = '',
    created_at: Optional[float] = None,
) -> str:
    ensure_schema(conn)
    now = float(created_at or time.time())
    urls = [str(url) for url in (source_urls or []) if str(url).strip()]
    ids = [str(source_id) for source_id in (source_ids or []) if str(source_id).strip()]
    excerpt = raw_excerpt or ''
    excerpt_hash = hashlib.sha256(excerpt.encode('utf-8', 'ignore')).hexdigest()[:24] if excerpt else ''
    evidence_packet_id = stable_id('evidence_packet', scope_key, object_type, object_id, claim, ','.join(urls[:5]), excerpt_hash)
    conn.execute(
        """
        INSERT OR REPLACE INTO evidence_packets
        (evidence_packet_id, scope_key, object_type, object_id, claim, source_kind, source_urls_json, source_ids_json, authority, raw_excerpt_hash, raw_excerpt_preview, confidence, valid_until, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM evidence_packets WHERE evidence_packet_id=?), ?))
        """,
        (evidence_packet_id, scope_key or '', object_type or '', object_id or '', claim[:800], source_kind[:80], dumps(urls), dumps(ids), authority or 'unknown', excerpt_hash, excerpt[:280], float(confidence), valid_until, evidence_packet_id, now),
    )
    record_memory_event(
        conn,
        object_type=object_type or 'evidence_packet',
        object_id=object_id or evidence_packet_id,
        action='evidence_recorded',
        reason='structured evidence packet recorded',
        details={'evidence_packet_id': evidence_packet_id, 'authority': authority, 'source_count': len(urls)},
        confidence=confidence,
        created_at=now,
    )
    return evidence_packet_id


def audit_log_insert(
    conn,
    object_type: str,
    object_id: str,
    action: str,
    reason: str,
    details: dict = None
):
    """Insert audit log entry."""
    audit_id = f"audit:{object_type}:{object_id}:{action}:{int(time.time())}"
    conn.execute(
        """INSERT OR REPLACE INTO audit_log 
           (audit_id, object_type, object_id, action, reason, details_json, created_at) 
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (audit_id, object_type, object_id, action, reason, 
         json.dumps(details or {}), time.time())
    )
