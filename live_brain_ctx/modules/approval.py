"""Approval-queue helpers for the live_brain_ctx plugin.

Functions extracted from the monolithic __init__.py that handle pending
self-evolution proposal surfacing, relevance checks, and context line
generation for the approval workflow.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from typing import List

from .query_filters import _is_review_only_query
from .text_processing import _truncate_fact
from . import state

_SECTION_LIMITS = state.SECTION_LIMITS
_AUTO_SURFACE_PENDING_APPROVALS = state.AUTO_SURFACE_PENDING_APPROVALS
_LOW_SIGNAL_WORDS = state.LOW_SIGNAL_WORDS


def _fetch_pending_approval_rows(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    try:
        return conn.execute(
            "SELECT proposal_id, proposal_type, target_area, rationale, proposed_action, risk_level, risk_score FROM self_evolution_proposals WHERE status='needs_approval' ORDER BY risk_score DESC, updated_at DESC LIMIT ?",
            (_SECTION_LIMITS.get('PENDING APPROVAL', 3),),
        ).fetchall()
    except Exception:
        return []


def _unsurfaced_pending_approval_rows(conn: sqlite3.Connection, pending_approval_rows: List[sqlite3.Row]) -> List[sqlite3.Row]:
    if not pending_approval_rows:
        return []
    proposal_ids = [str(row['proposal_id']) for row in pending_approval_rows]
    placeholders = ','.join('?' for _ in proposal_ids)
    try:
        seen = {
            str(row['object_id'])
            for row in conn.execute(
                f"SELECT object_id FROM audit_log WHERE object_type='self_evolution_proposal' AND action='surfaced_pending_approval' AND object_id IN ({placeholders})",
                proposal_ids,
            ).fetchall()
        }
    except Exception:
        seen = set()
    return [row for row in pending_approval_rows if str(row['proposal_id']) not in seen]


def _mark_pending_approvals_surfaced(conn: sqlite3.Connection, pending_approval_rows: List[sqlite3.Row], reason: str) -> None:
    if not pending_approval_rows:
        return
    now = time.time()
    try:
        for row in pending_approval_rows:
            proposal_id = str(row['proposal_id'])
            audit_id = 'audit:approval_surfaced:' + hashlib.sha256(proposal_id.encode('utf-8')).hexdigest()[:24]
            conn.execute(
                "INSERT OR IGNORE INTO audit_log (audit_id, object_type, object_id, action, reason, details_json, created_at) VALUES (?, 'self_evolution_proposal', ?, 'surfaced_pending_approval', ?, '{}', ?)",
                (audit_id, proposal_id, reason[:240], now),
            )
        conn.commit()
    except Exception:
        pass


def _approval_relevant_to_user_message(pending_approval_rows: List[sqlite3.Row], user_message: str) -> bool:
    if not pending_approval_rows:
        return False
    if _is_review_only_query(user_message):
        return False
    lowered = (user_message or '').lower()
    relevance_terms = [
        'live brain', 'self-evol', 'self evolution', 'self evolving', 'autonomous',
        'memory/context', 'context engine', 'plugin', 'hook', 'schema', 'migration',
        'config', 'configuration', 'code', 'kod', 'patch', 'tool',
    ]
    if any(term in lowered for term in relevance_terms):
        return True
    words = [w for w in re.findall(r'[\w./-]+', lowered) if len(w) > 4 and w not in _LOW_SIGNAL_WORDS]
    if not words:
        return False
    for row in pending_approval_rows:
        row_text = ' '.join(str(row[field] or '') for field in ('proposal_type', 'target_area', 'rationale', 'proposed_action')).lower()
        if any(word in row_text for word in words[:8]):
            return True
    return False


def _should_surface_pending_approvals(
    conn: sqlite3.Connection,
    pending_approval_rows: List[sqlite3.Row],
    user_message: str,
    approval_query: bool,
) -> tuple[bool, str, List[sqlite3.Row]]:
    if approval_query:
        return True, 'explicit_approval_query', pending_approval_rows
    if _is_review_only_query(user_message):
        return False, '', []
    if not _AUTO_SURFACE_PENDING_APPROVALS or not pending_approval_rows:
        return False, '', []
    unsurfaced_rows = _unsurfaced_pending_approval_rows(conn, pending_approval_rows)
    if unsurfaced_rows:
        return True, 'new_pending_approval_once', unsurfaced_rows
    if _approval_relevant_to_user_message(pending_approval_rows, user_message):
        return True, 'relevant_pending_approval', pending_approval_rows
    return False, '', []


def _suppressed_approval_reminder_lines() -> List[str]:
    return [
        "A pending self-evolution approval exists, but it was already surfaced and this turn is unrelated.",
        "Do NOT mention, summarize, hint at, or remind about that approval in the final answer unless the user asks or the request is blocked by it.",
    ]


def _approval_context_lines(pending_approval_rows: List[sqlite3.Row], approval_query: bool) -> List[str]:
    if approval_query:
        approval_lines = [
            "ROUTE: approval query detected; call brain_self_evolution(action='list', status='needs_approval', limit=10) before final answer.",
            "Do not use session_search, cronjob, or brain_state_debug for approval queue answers.",
        ]
    else:
        approval_lines = [
            "NOTICE: pending self-evolution approval exists; surface this briefly without waiting for the user to ask.",
            "Tell the user they can say 'approve latest pending self-evolution' or 'reject latest pending self-evolution'.",
        ]
    if pending_approval_rows:
        for row in pending_approval_rows:
            action = _truncate_fact(row['proposed_action'] or row['rationale'] or '')
            approval_lines.append(
                f"id={row['proposal_id']} risk={row['risk_level']}({row['risk_score']}) type={row['proposal_type']} target={row['target_area']} action={action}; decide with brain_self_evolution action=decide"
            )
    elif approval_query:
        approval_lines = ["none"]
    return approval_lines
