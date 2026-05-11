"""Data source fetching and DB maintenance for live_brain_ctx.

Functions extracted from the monolithic __init__.py that handle all database
queries for context assembly: work items, fix recipes, causal activations,
and periodic maintenance tasks.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import List, Optional, Tuple

from .state import (
    CONSTRAINT_TTL_DAYS,
    MAX_ACTIVE_EPISODES,
    SECTION_LIMITS,
    AUTO_SURFACE_PENDING_APPROVALS,
    SYNTHETIC_MEMORY_RE,
    MUSIC_MEMORY_ALIASES,
)
from .scoring import (
    _has_overlap,
    _overlap_score,
    _is_review_only_query,
    _row_noisy,
    _row_text,
    _row_updated_at,
    _same_user_message,
)
from .tag_matching import _matches
from .query_filters import (
    _is_non_action_work_item_text,
    _meaningful_query_words,
    _is_continuation_query,
    _is_question_like_memory,
)
from .approval import (
    _fetch_pending_approval_rows,
    _should_surface_pending_approvals,
    _mark_pending_approvals_surfaced,
)

logger = logging.getLogger(__name__)


def _fetch_all_data_sources(conn, qctx, user_message, approval_query,
                            *, ArtifactRegistry=None, DataSources=None):
    """Execute all database queries and return structured results."""
    # Binding constraints
    binding_rules = conn.execute(
        "SELECT action_json, scope_tags_json, updated_at, specificity FROM rules WHERE scope IN ('user_binding','user_correction') AND category IN ('binding_constraint','learned_fact') AND status='active' AND updated_at > ? ORDER BY specificity DESC, confidence DESC, times_confirmed DESC LIMIT 20",
        (qctx.ttl_cutoff,),
    ).fetchall()

    # Work items
    work_item_row, continuity_work_rows = _fetch_work_items(conn, qctx, user_message)
    if _is_review_only_query(user_message or ''):
        work_item_row = None
        continuity_work_rows = []

    # Episodes
    episode_rows = conn.execute(
        "SELECT episode_id, title, current_summary, scope_tags_json, updated_at FROM episodes WHERE status IN ('active','dormant') AND scope_key = ? AND session_id = ? AND length(current_summary) > 20 ORDER BY updated_at DESC LIMIT 120",
        (qctx.scope_key, qctx.session_id),
    ).fetchall()

    # Facts
    fact_rows = conn.execute(
        "SELECT DISTINCT fact_text, source_kind, scope_tags_json, scope_key, valid_from FROM facts WHERE status='active' AND confidence >= 0.75 AND NOT (source_kind LIKE 'epistemic:%' AND source_kind NOT IN ('epistemic:official','epistemic:primary_or_institutional','epistemic:primary_or_support')) ORDER BY CASE WHEN scope_key=? THEN 0 ELSE 1 END, valid_from DESC LIMIT 120",
        (qctx.scope_key,),
    ).fetchall()

    # Beliefs
    belief_rows = conn.execute(
        "SELECT claim_text, belief_kind, status, scope_tags_json, scope_key FROM beliefs WHERE status IN ('open','validated') ORDER BY CASE WHEN scope_key=? THEN 0 ELSE 1 END, updated_at DESC LIMIT 20",
        (qctx.scope_key,),
    ).fetchall()

    # Fix recipes and causal
    recipe_rows, causal_rows = _fetch_fix_recipes_and_causal(conn, qctx)

    # Knowledge
    knowledge_rows = conn.execute(
        "SELECT principle_text FROM crystallised_knowledge WHERE scope_key=? OR scope_key='' ORDER BY created_at DESC LIMIT 3",
        (qctx.scope_key,),
    ).fetchall()

    # Artifacts
    artifact_lines = []
    if ArtifactRegistry is not None:
        try:
            artifact_lines = ArtifactRegistry(conn).context_lines_for_query(
                user_message or '',
                limit=SECTION_LIMITS.get('VERIFIED ARTIFACTS', 5),
            )
            if artifact_lines:
                conn.commit()
        except Exception:
            artifact_lines = []

    # Recap
    recap_row = conn.execute(
        "SELECT task, root_cause, current_status, next_step FROM canonical_recaps WHERE scope_key=? ORDER BY updated_at DESC LIMIT 1",
        (qctx.scope_key,),
    ).fetchone()

    # Pending approvals
    pending_approval_rows = []
    approval_surface_reason = ''
    should_surface_approval = False
    if approval_query or AUTO_SURFACE_PENDING_APPROVALS:
        pending_approval_rows = _fetch_pending_approval_rows(conn)
        should_surface_approval, approval_surface_reason, pending_approval_rows = _should_surface_pending_approvals(conn, pending_approval_rows, user_message or '', approval_query)
        if should_surface_approval:
            _mark_pending_approvals_surfaced(conn, pending_approval_rows, approval_surface_reason)

    return DataSources(
        binding_rules=binding_rules,
        work_item_row=work_item_row,
        continuity_work_rows=continuity_work_rows,
        episode_rows=episode_rows,
        fact_rows=fact_rows,
        belief_rows=belief_rows,
        recipe_rows=recipe_rows,
        causal_rows=causal_rows,
        knowledge_rows=knowledge_rows,
        artifact_lines=artifact_lines,
        recap_row=recap_row,
        pending_approval_rows=pending_approval_rows,
        should_surface_approval=should_surface_approval,
        approval_surface_reason=approval_surface_reason,
    )


def _fetch_fix_recipes_and_causal(
    conn: sqlite3.Connection,
    qctx,
) -> Tuple[List[sqlite3.Row], List[sqlite3.Row]]:
    """Query fix recipes and causal activations with dynamic SQL."""
    recipe_rows = []
    causal_rows = []
    causal_words = _meaningful_query_words(qctx.query_words) or qctx.query_words
    if causal_words:
        # Use FTS5 for optimized search instead of LIKE
        fts_query = ' OR '.join(causal_words[:6])
        try:
            recipe_rows = conn.execute(
                """SELECT r.recipe_id, r.problem_pattern, r.tool_name, r.steps_json, r.args_template_json,
                   r.success_criteria, r.artifact_verified, r.promotion_status, r.confidence, r.times_confirmed, r.scope_tags_json
                   FROM fix_recipes r
                   JOIN fix_recipes_fts fts ON r.recipe_id = fts.recipe_id
                   WHERE fts.problem_pattern MATCH ? AND r.scope_key=? AND r.status='active'
                   AND r.promotion_status='active' AND r.artifact_verified=1
                   ORDER BY r.confidence DESC, r.times_confirmed DESC, r.updated_at DESC LIMIT 12""",
                (fts_query, qctx.scope_key),
            ).fetchall()
            causal_rows = conn.execute(
                """SELECT c.tool_used, c.trigger_pattern, c.args_template_json, c.test_result,
                   c.artifact_verified, c.times_confirmed, c.confidence, c.scope_tags_json
                   FROM causal_activations c
                   JOIN causal_activations_fts fts ON c.rowid = fts.rowid
                   WHERE fts.trigger_text MATCH ? AND c.scope_key=? AND c.success=1
                   ORDER BY c.times_confirmed DESC, c.confidence DESC, c.updated_at DESC LIMIT 12""",
                (fts_query, qctx.scope_key),
            ).fetchall()
        except Exception:
            pass
    return (recipe_rows, causal_rows)


def _fetch_work_items(
    conn: sqlite3.Connection,
    qctx,
    user_message: str,
) -> Tuple[Optional[sqlite3.Row], List[sqlite3.Row]]:
    """Query and filter work items, return (primary_work_item, continuity_items)."""
    # Add session isolation and 24h TTL for objectives
    ttl_cutoff = qctx.now - 86400  # 24 hours
    work_item_row = conn.execute(
        "SELECT title, status, root_cause, next_step, evidence_json, scope_tags_json, updated_at FROM work_items WHERE scope_key=? AND session_id=? AND (created_at > ? OR status='active') AND title NOT LIKE 'sumarizuj%' AND title NOT LIKE 'what did you do%' ORDER BY CASE WHEN status='active' THEN 0 WHEN status='blocked' THEN 1 ELSE 2 END, priority DESC, updated_at DESC LIMIT 50",
        (qctx.scope_key, qctx.session_id, ttl_cutoff),
    ).fetchall()
    scoped_work_rows = [r for r in work_item_row if _matches(r, qctx.active_tags, qctx.scope_key) and not _row_noisy(r, ['title', 'root_cause', 'next_step', 'evidence_json'])]
    scoped_work_rows = [
        r for r in scoped_work_rows
        if not _same_user_message(r, user_message or '', ['title'])
        and not _is_non_action_work_item_text(str(r['title'] or ''))
    ]
    if qctx.continuation_query:
        scoped_work_rows = [
            r for r in scoped_work_rows
            if not _same_user_message(r, user_message or '', ['title'])
            and not _is_continuation_query(str(r['title'] or ''))
            and not (_is_question_like_memory(str(r['title'] or '')) and not any(alias in str(r['title'] or '').lower() for alias in MUSIC_MEMORY_ALIASES))
            and not SYNTHETIC_MEMORY_RE.search(_row_text(r, ['title', 'root_cause', 'next_step', 'evidence_json']))
        ]
    overlapped_work_rows = [r for r in scoped_work_rows if _has_overlap(r, qctx.query_words, ['title', 'root_cause', 'next_step', 'evidence_json'])]
    continuity_work_rows = []
    if qctx.continuation_query and overlapped_work_rows:
        ranked_work_rows = sorted(
            overlapped_work_rows,
            key=lambda r: (_overlap_score(r, qctx.query_words, ['title', 'root_cause', 'next_step', 'evidence_json']), _row_updated_at(r)),
            reverse=True,
        )
        seen_titles = set()
        for row in list(overlapped_work_rows[:2]) + ranked_work_rows:
            title_key = (row['title'] or '').strip().lower()
            if title_key and title_key not in seen_titles:
                seen_titles.add(title_key)
                continuity_work_rows.append(row)
            if len(continuity_work_rows) >= SECTION_LIMITS.get('CONTINUITY MEMORY', 5):
                break
    work_candidates = overlapped_work_rows if qctx.continuation_query else overlapped_work_rows
    work_item_row = (work_candidates or ([] if _meaningful_query_words(qctx.query_words) else scoped_work_rows))
    work_item_row = work_item_row[0] if work_item_row else None
    return (work_item_row, continuity_work_rows)


def _perform_db_maintenance(conn: sqlite3.Connection, now: float) -> None:
    """Expire old rules and archive inactive episodes."""
    conn.execute(
        "UPDATE rules SET status='expired', updated_at=? WHERE status='active' "
        "AND expires_at IS NOT NULL AND expires_at <= ?",
        (now, now),
    )
    conn.execute(
        "UPDATE episodes SET status='archived', updated_at=? WHERE status='active' "
        "AND episode_id NOT IN (SELECT episode_id FROM episodes WHERE status='active' "
        "ORDER BY updated_at DESC LIMIT ?)",
        (now, MAX_ACTIVE_EPISODES),
    )
    conn.commit()
