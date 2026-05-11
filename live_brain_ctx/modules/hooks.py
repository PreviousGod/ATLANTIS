"""Hook functions extracted from live_brain_ctx monolith.

Contains: _prepare_query_context, _load_live_brain_context, _debug_live_brain_context,
_latest_tool_context, _configure_ctx_sqlite, _tool_call_is_local_debug, _record_tool_result,
_pre_tool_call, _post_tool_call, _get_active_session_evidence, _context_sections,
_record_context_impression, _pre_llm_call, _get_maintenance_executor, _run_maintenance_bg,
_post_llm_call
"""
from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from live_brain.connection_pool import ConnectionPool
except Exception:
    ConnectionPool = None

from .. import QueryContext, DataSources

from .state import (
    SECTION_LIMITS as _SECTION_LIMITS,
    CHIT_CHAT_PATTERNS as _CHIT_CHAT_PATTERNS,
    LOW_SIGNAL_WORDS as _LOW_SIGNAL_WORDS,
    SECRET_RE as _SECRET_RE,
    CONTINUATION_QUERY_RE as _CONTINUATION_QUERY_RE,
    RUN_MARKER_RE as _RUN_MARKER_RE,
    CONSTRAINT_TTL_DAYS as _CONSTRAINT_TTL_DAYS,
    MAX_ACTIVE_EPISODES as _MAX_ACTIVE_EPISODES,
    MUSIC_MEMORY_ALIASES as _MUSIC_MEMORY_ALIASES,
    AUTO_SURFACE_PENDING_APPROVALS as _AUTO_SURFACE_PENDING_APPROVALS,
    MAINTENANCE_INTERVAL as _STATE_MAINTENANCE_INTERVAL,
)
from .query_filters import (
    _is_chit_chat,
    _is_continuation_query,
    _meaningful_query_words,
    _is_low_signal_episode,
    _is_noisy_memory,
    _expand_query_words,
    _is_review_only_query,
    _is_non_action_work_item_text,
    _current_turn_allows_destructive_memory,
    _is_destructive_memory_text,
    _is_question_like_memory,
)
from .scoring import (
    _overlap_score,
    _has_overlap,
    _row_text,
    _row_noisy,
    _marker_conflicts,
    _domain_conflicts,
    _visible_fact_matches,
    _same_user_message,
    _row_updated_at,
)
from .tag_matching import _active_tags, _row_tags, _matches, _causal_matches
from .approval import (
    _fetch_pending_approval_rows,
    _should_surface_pending_approvals,
    _mark_pending_approvals_surfaced,
    _approval_context_lines,
    _suppressed_approval_reminder_lines,
)
from .tool_context import _artifact_required, _tool_relevant, _recipe_hint
from .formatting import (
    _append_section,
    _format_episodes,
    _format_fix_recipes,
    _format_binding_constraints,
)
from .integrations import (
    _load_reality_engine_class,
    _record_reality_event,
    _load_reality_brief,
    _load_epistemic_manager_class,
    _load_epistemic_brief,
    _load_epistemic_autonomous_context,
    _record_epistemic_answer_if_source_backed,
    _should_load_reality_brief,
    _should_isolate_epistemic_context,
    _epistemic_query_text,
)
from .data_sources import _fetch_all_data_sources, _perform_db_maintenance
from .text_processing import _truncate_fact, _redact
from .query_classification import (
    _is_recap_query,
    _is_diagnostic_query,
    _is_approval_query,
)
from .cognitive_architecture import get_cognitive_context, record_ruled_out, ensure_ruled_out_table

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Mutable module-level globals
# ---------------------------------------------------------------------------
_LAST_MAINTENANCE_TIME = 0.0
_MAINTENANCE_INTERVAL = 3600.0
_MAINTENANCE_EXECUTOR: Optional[ThreadPoolExecutor] = None
_MAINTENANCE_EXECUTOR_LOCK = threading.Lock()
_LAST_CONTEXT_METADATA: Dict[str, Any] = {'recipe_ids': []}

# ---------------------------------------------------------------------------
# Local regex
# ---------------------------------------------------------------------------
_LOCAL_DEBUG_TOOL_RE = re.compile(
    r'\b(?:local|repo|repository|file|fajl|path|putanja|code|kod|script|skript|log|trace|stack|debug|bug|error|pytest|test|sqlite|database|db|schema|plugin|live[_ -]?brain|context|gateway|hermes|session|tool|config|konfig)\b',
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Local utility functions (copied from monolith lines 281-330)
# ---------------------------------------------------------------------------

_connection_pool = None


def _hermes_home() -> str:
    return os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))


def _db_path() -> str:
    return str(Path(_hermes_home()) / "live_brain" / "live_brain.db")


def _get_connection_pool():
    """Lazy initialization of connection pool."""
    global _connection_pool
    if _connection_pool is None and ConnectionPool is not None:
        _connection_pool = ConnectionPool(_db_path(), max_connections=10)
    return _connection_pool


def _get_connection():
    """Get a database connection, using pool if available."""
    pool = _get_connection_pool()
    if pool:
        return pool.get_connection()
    return sqlite3.connect(_db_path(), timeout=5.0)


def _extract_scope_key(
    user_message: str,
    sender_id: str,
    session_id: str,
    *,
    platform: str = 'telegram',
    context: str = 'dm',
) -> str:
    """Compose a stable scope key for Live Brain memory partitioning."""
    if sender_id:
        platform = (platform or 'telegram').strip().lower() or 'telegram'
        context = (context or 'dm').strip().lower() or 'dm'
        return f"agent:main:{platform}:{context}:{sender_id}"
    return session_id or (user_message[:80] if user_message else "")


@functools.lru_cache(maxsize=1)
def _load_live_brain_ingestor_class():
    try:
        from live_brain.ingest import Ingestor
        return Ingestor
    except Exception:
        pass
    import importlib.util as _importlib_util
    import sys as _sys
    import types as _types
    package_name = '_live_brain_ctx_live_brain'
    live_brain_dir = Path(__file__).resolve().parent.parent.parent / 'live_brain'
    if package_name not in _sys.modules:
        package = _types.ModuleType(package_name)
        package.__path__ = [str(live_brain_dir)]
        _sys.modules[package_name] = package
    for module_name in ['scopes', 'ingest']:
        full_name = f'{package_name}.{module_name}'
        if full_name in _sys.modules:
            continue
        spec = _importlib_util.spec_from_file_location(full_name, live_brain_dir / f'{module_name}.py')
        if spec is None or spec.loader is None:
            raise ImportError(f'Cannot load {module_name} from {live_brain_dir}')
        module = _importlib_util.module_from_spec(spec)
        module.__package__ = package_name
        _sys.modules[full_name] = module
        spec.loader.exec_module(module)
    return _sys.modules[f'{package_name}.ingest'].Ingestor


# ---------------------------------------------------------------------------
# Extracted hook functions
# ---------------------------------------------------------------------------


def _prepare_query_context(user_message: str, sender_id: str, session_id: str, *, platform: str = 'telegram') -> QueryContext:
    """Extract and prepare all query-related metadata."""
    scope_key = _extract_scope_key(user_message, sender_id, session_id, platform=platform)
    now = time.time()
    ttl_cutoff = now - _CONSTRAINT_TTL_DAYS * 86400
    query_lower = (user_message or "").lower()
    continuation_query = _is_continuation_query(user_message or "")
    query_words = [w for w in re.findall(r'[\w./-]+', query_lower) if len(w) > 3]
    active_tags = _active_tags(user_message, scope_key)
    return QueryContext(scope_key, query_lower, query_words, active_tags,
                       continuation_query, now, ttl_cutoff)


def _load_live_brain_context(user_message: str, session_id: str, sender_id: str) -> str:
    db_path = _db_path()
    if not Path(db_path).exists():
        return ""

    approval_query = _is_approval_query(user_message or "")
    if _is_review_only_query(user_message or '') and not approval_query:
        return ""
    if _is_chit_chat(user_message or "") and not approval_query:
        if _AUTO_SURFACE_PENDING_APPROVALS:
            conn = _get_connection()
            conn.row_factory = sqlite3.Row
            pending_approval_rows = _fetch_pending_approval_rows(conn)
            should_surface, surface_reason, rows_to_surface = _should_surface_pending_approvals(conn, pending_approval_rows, user_message, approval_query)
            if should_surface:
                _mark_pending_approvals_surfaced(conn, rows_to_surface, surface_reason)
                parts: List[str] = []
                _append_section(parts, "PENDING APPROVAL", _approval_context_lines(rows_to_surface, approval_query=False))
                return "\n\n".join(parts)
            if pending_approval_rows:
                parts: List[str] = []
                _append_section(parts, "APPROVAL ROUTING", _suppressed_approval_reminder_lines())
                return "\n\n".join(parts)
        return ""

    qctx = _prepare_query_context(user_message, sender_id, session_id)
    _LAST_CONTEXT_METADATA['recipe_ids'] = []

    conn = _get_connection()
    conn.row_factory = sqlite3.Row
    data = _fetch_all_data_sources(conn, qctx, user_message, approval_query)

    parts: List[str] = []

    # PENDING APPROVAL — surface only when explicit, newly pending, or relevant to this turn.
    if data.should_surface_approval:
        _append_section(parts, "PENDING APPROVAL", _approval_context_lines(data.pending_approval_rows, approval_query=approval_query))
    elif data.pending_approval_rows:
        _append_section(parts, "APPROVAL ROUTING", _suppressed_approval_reminder_lines())

    # BINDING CONSTRAINTS — deterministic scope match, TTL enforced
    if data.binding_rules:
        constraints = _format_binding_constraints(data.binding_rules, qctx, user_message)
        if constraints:
            _append_section(parts, "MUST FOLLOW", constraints)

    # VERIFIED ARTIFACTS — deterministic project file choices before fuzzy episodes/search.
    try:
        if data.artifact_lines:
            _append_section(parts, "VERIFIED ARTIFACTS", data.artifact_lines)
    except Exception:
        pass

    # FIX RECIPES / CAUSAL ACTIVATIONS — proven tool approaches
    recipe_hints, selected_recipe_ids = _format_fix_recipes(data.recipe_rows, data.causal_rows, qctx)
    if recipe_hints:
        _LAST_CONTEXT_METADATA['recipe_ids'] = selected_recipe_ids[:_SECTION_LIMITS.get('PROVEN FIX', 3)]
        _append_section(parts, "PROVEN FIX", recipe_hints)

    # LEARNED PRINCIPLES — useful facts, not free-form fixes
    if data.knowledge_rows:
        principles = [_truncate_fact(r[0]) for r in data.knowledge_rows if r[0] and not _SECRET_RE.search(r[0]) and not _is_noisy_memory(r[0]) and not _domain_conflicts(qctx.query_lower, r[0]) and _has_overlap(r, qctx.query_words, ['principle_text'])]
        if principles:
            _append_section(parts, "KNOWN FACTS", principles)

    # ACTIVE WORK ITEM
    if data.work_item_row and not _is_recap_query(user_message or ""):
        lines = [f"Task: {data.work_item_row['title']}"]
        if data.work_item_row['status']:
            lines.append(f"Status: {data.work_item_row['status']}")
        root_cause = (data.work_item_row['root_cause'] or '').strip()
        if root_cause and root_cause not in {'.', '-', 'unknown'} and len(root_cause) > 3 and not _marker_conflicts(qctx.query_lower, root_cause.lower()):
            lines.append(f"Root cause: {_truncate_fact(root_cause)}")
        _append_section(parts, "ACTIVE TASK", ["; ".join(lines)])

    if qctx.continuation_query and data.continuity_work_rows:
        continuity_lines = []
        for row in data.continuity_work_rows:
            title = (row['title'] or '').strip()
            if not title or _is_continuation_query(title) or _is_question_like_memory(title) and not any(alias in title.lower() for alias in _MUSIC_MEMORY_ALIASES):
                continue
            continuity_lines.append(f"User previously said: {_truncate_fact(title)}")
        if continuity_lines:
            _append_section(parts, "CONTINUITY MEMORY", continuity_lines)

    # ACTIVE EPISODES — max 3, 1 line each, no chit-chat.
    ep_lines = _format_episodes(data.episode_rows, qctx, user_message)
    if ep_lines:
        _append_section(parts, "RECENT EPISODES", ep_lines)

    # VALIDATED FACTS — atomic, max 200 chars
    if data.fact_rows:
        facts = [_truncate_fact(r['fact_text']) for r in data.fact_rows if r['fact_text'] and not _SECRET_RE.search(r['fact_text']) and not _is_noisy_memory(r['fact_text']) and not _is_question_like_memory(r['fact_text']) and not _domain_conflicts(qctx.query_lower, r['fact_text']) and _visible_fact_matches(r['fact_text'], qctx.query_words) and _matches(r, qctx.active_tags, qctx.scope_key) and _has_overlap(r, qctx.query_words, ['fact_text'])]
        if facts:
            _append_section(parts, "KNOWN FACTS", facts)

    # OPEN HYPOTHESES — only if there's a real signal
    open_beliefs = [r['claim_text'] for r in data.belief_rows if r['status'] == 'open' and len(r['claim_text']) > 20 and not _is_noisy_memory(r['claim_text']) and _matches(r, qctx.active_tags, qctx.scope_key) and _has_overlap(r, qctx.query_words, ['claim_text'])]
    if open_beliefs:
        _append_section(parts, "OPEN BUG", [_truncate_fact(b) for b in open_beliefs[:2]])

    # VALIDATED CAUSES — facts only; PROVEN FIX is reserved for executable recipes
    validated_causes = [r['claim_text'] for r in data.belief_rows if r['status'] == 'validated' and r['belief_kind'] == 'validated_cause' and not _is_noisy_memory(r['claim_text']) and _matches(r, qctx.active_tags, qctx.scope_key) and _has_overlap(r, qctx.query_words, ['claim_text'])]
    if validated_causes:
        _append_section(parts, "KNOWN FACTS", [f"Cause: {_truncate_fact(c)}" for c in validated_causes[:2]])

    # NEXT BEST ACTIONS — only if there's a real signal (not "answer user")
    if data.work_item_row and data.work_item_row['next_step']:
        next_step = data.work_item_row['next_step']
        generic_next = ['diagnose the problem using exact entities', 'before guessing', 'answer the user']
        lowered_next = next_step.lower()
        if next_step and 'continue' not in lowered_next and 'answer' not in lowered_next and not any(token in lowered_next for token in generic_next):
            _append_section(parts, "NEXT REQUIRED ACTION", [next_step[:200]])

    # RECAP — only for recap queries
    if _is_recap_query(user_message or "") and data.recap_row and not any(_is_noisy_memory(data.recap_row[field] or '') for field in ['task', 'root_cause', 'current_status', 'next_step']):
        recap_lines = []
        if data.recap_row['task']:
            recap_lines.append(f"Task: {data.recap_row['task'][:80]}")
        if data.recap_row['root_cause']:
            recap_lines.append(f"Root cause: {_truncate_fact(data.recap_row['root_cause'])}")
        if data.recap_row['current_status']:
            recap_lines.append(f"Status: {data.recap_row['current_status']}")
        if data.recap_row['next_step']:
            recap_lines.append(f"Next: {data.recap_row['next_step'][:100]}")
        if recap_lines:
            parts.append("LATEST RECAP:\n- " + "\n- ".join(recap_lines))

    # DIAGNOSTIC GUIDANCE — only for diagnostic queries
    if _is_diagnostic_query(user_message or ""):
        parts.append("DIAGNOSTIC RULE: Do not present hypotheses as confirmed causes. Give one concrete next debugging step if evidence is insufficient.")

    if not parts:
        logger.debug(f"[LIVE_BRAIN_CONTEXT] No context generated for query: {user_message[:50]}")
        return ""

    result = "LIVE BRAIN\n" + "\n".join(parts)
    logger.info(f"[LIVE_BRAIN_CONTEXT] Generated {len(result)} chars for query: {user_message[:50]}")
    logger.debug(f"[LIVE_BRAIN_CONTEXT_FULL]\n{result}")
    return result


def _debug_live_brain_context(user_message: str, session_id: str = '', sender_id: str = '', *, platform: str = 'telegram') -> Dict[str, Any]:
    scope_key = _extract_scope_key(user_message, sender_id, session_id, platform=platform)
    query_lower = (user_message or '').lower()
    query_words = [w for w in re.findall(r'[\w./-]+', query_lower) if len(w) > 3]
    active_tags = _active_tags(user_message, scope_key)
    context = _load_live_brain_context(user_message, session_id, sender_id)
    debug: Dict[str, Any] = {
        'scope_key': scope_key,
        'active_tags': active_tags,
        'context': context,
        'line_count': len(context.splitlines()) if context else 0,
        'sections': [line[:-1] for line in context.splitlines() if line.endswith(':')] if context else [],
        'rejections': {
            'recipes_scope': 0,
            'recipes_tool': 0,
            'facts_secret': 0,
            'facts_noisy': 0,
            'facts_scope': 0,
            'facts_overlap': 0,
            'facts_visible': 0,
            'causal_scope': 0,
            'causal_tool': 0,
        },
    }
    db_path = _db_path()
    if not Path(db_path).exists():
        debug['db_exists'] = False
        return debug
    conn = _get_connection()
    conn.row_factory = sqlite3.Row
    facts = conn.execute(
            "SELECT fact_text, source_kind, scope_tags_json, scope_key FROM facts WHERE status='active' AND confidence >= 0.75 AND NOT (source_kind LIKE 'epistemic:%' AND source_kind NOT IN ('epistemic:official','epistemic:primary_or_institutional','epistemic:primary_or_support')) ORDER BY valid_from DESC LIMIT 100"
        ).fetchall()
    for row in facts:
        text = row['fact_text'] or ''
        if _SECRET_RE.search(text):
            debug['rejections']['facts_secret'] += 1
        elif _is_noisy_memory(text):
            debug['rejections']['facts_noisy'] += 1
        elif not _matches(row, active_tags, scope_key):
            debug['rejections']['facts_scope'] += 1
        elif not _has_overlap(row, query_words, ['fact_text']):
            debug['rejections']['facts_overlap'] += 1
        elif not _visible_fact_matches(text, query_words):
            debug['rejections']['facts_visible'] += 1
    causal_words = _meaningful_query_words(query_words) or query_words
    if causal_words:
        fts_query = ' OR '.join(causal_words[:6])
        recipe_rows = conn.execute(
            """SELECT r.recipe_id, r.problem_pattern, r.tool_name, r.steps_json, r.args_template_json,
               r.success_criteria, r.artifact_verified, r.promotion_status, r.confidence, r.times_confirmed, r.scope_tags_json
               FROM fix_recipes r
               JOIN fix_recipes_fts fts ON r.recipe_id = fts.recipe_id
               WHERE fts.problem_pattern MATCH ? AND r.scope_key=? AND r.status='active'
               AND r.promotion_status='active' AND r.artifact_verified=1
               ORDER BY r.confidence DESC, r.times_confirmed DESC LIMIT 50""",
            (fts_query, scope_key),
        ).fetchall()
        for row in recipe_rows:
            if not _causal_matches(row, active_tags, scope_key):
                debug['rejections']['recipes_scope'] += 1
            elif not _tool_relevant(row['tool_name'], active_tags, query_lower):
                debug['rejections']['recipes_tool'] += 1
        rows = conn.execute(
            """SELECT c.tool_used, c.trigger_pattern, c.args_template_json, c.test_result,
               c.artifact_verified, c.times_confirmed, c.confidence, c.scope_tags_json
               FROM causal_activations c
               JOIN causal_activations_fts fts ON c.rowid = fts.rowid
               WHERE fts.trigger_text MATCH ? AND c.scope_key=? AND c.success=1
               ORDER BY c.times_confirmed DESC LIMIT 50""",
            (fts_query, scope_key),
        ).fetchall()
        for row in rows:
            if not _causal_matches(row, active_tags, scope_key):
                debug['rejections']['causal_scope'] += 1
            elif not _tool_relevant(row['tool_used'], active_tags, query_lower):
                debug['rejections']['causal_tool'] += 1
    return debug


def _latest_tool_context(conn: sqlite3.Connection, session_id: str, created_at: float) -> tuple[str, str]:
    if session_id:
        row = conn.execute(
            "SELECT scope_key, query_text FROM context_impressions WHERE session_id=? AND created_at >= ? ORDER BY created_at DESC LIMIT 1",
            (session_id, created_at - 1800),
        ).fetchone()
        if row:
            return str(row[0] or ''), str(row[1] or '')
    row = conn.execute(
        "SELECT scope_key, query_text FROM context_impressions WHERE created_at >= ? ORDER BY created_at DESC LIMIT 1",
        (created_at - 300,),
    ).fetchone()
    if row:
        return str(row[0] or ''), str(row[1] or '')
    return '', ''


def _configure_ctx_sqlite(conn: sqlite3.Connection) -> None:
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA busy_timeout=30000')
    conn.execute('PRAGMA temp_store=MEMORY')


def _tool_call_is_local_debug(tool_name: str, args: Any, user_text: str) -> bool:
    if not isinstance(args, dict):
        args = {}
    combined = ' '.join(str(v) for v in [user_text, args.get('query'), args.get('q'), args.get('path'), args.get('glob'), args.get('pattern')] if v)
    if tool_name == 'search_files' and (args.get('path') or args.get('glob') or args.get('pattern')):
        return True
    return bool(_LOCAL_DEBUG_TOOL_RE.search(combined))


def _record_tool_result(tool_name: str, args: Any, result: Any, session_id: str = '', tool_call_id: str = '', duration_ms: int | None = None) -> None:
    db_path = _db_path()
    if not tool_name or not Path(db_path).exists():
        return
    conn = None
    try:
        created_at = time.time()
        conn = _get_connection()
        conn.row_factory = sqlite3.Row
        _configure_ctx_sqlite(conn)
        try:
            duration_ms = max(0, int(duration_ms or 0))
        except (TypeError, ValueError):
            duration_ms = 0
        scope_key, user_text = _latest_tool_context(conn, session_id, created_at)
        if not scope_key:
            # No sender_id available in tool-call context (only session_id),
            # so platform is irrelevant here — _extract_scope_key falls back
            # to session_id when sender is empty regardless of platform.
            scope_key = _extract_scope_key(user_text, '', session_id)
        Ingestor = _load_live_brain_ingestor_class()
        Ingestor(conn).store_tool_result_event(
            tool_name,
            args if isinstance(args, dict) else {},
            result,
            session_id=session_id,
            tool_call_id=tool_call_id,
            scope_key=scope_key,
            user_text=user_text,
            created_at=created_at,
            duration_ms=duration_ms,
        )
        result_text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
        success = True
        try:
            parsed_result = json.loads(result_text) if isinstance(result_text, str) else result_text
            if isinstance(parsed_result, dict):
                if parsed_result.get('success') is False or parsed_result.get('ok') is False or parsed_result.get('error'):
                    success = False
        except Exception:
            success = not bool(re.search(r'\b(traceback|exception|error executing|failed|permission denied|connection refused|modulenotfounderror)\b', result_text or '', re.IGNORECASE))
        RealityEngine = _load_reality_engine_class()
        RealityEngine(conn).ingest_event(
            scope_key=scope_key,
            event_type='tool_result',
            subject=tool_name,
            payload={
                'tool_name': tool_name,
                'args': args if isinstance(args, dict) else {},
                'result': result_text[:4000],
                'success': success,
                'tool_call_id': tool_call_id,
                'duration_ms': duration_ms,
                'user_message': user_text,
            },
            session_id=session_id,
            source='post_tool_call',
            confidence=0.82 if success else 0.9,
            created_at=created_at,
        )
        if tool_name in {'web_search', 'web_extract'} and success:
            EpistemicManager = _load_epistemic_manager_class()
            EpistemicManager(conn, session_id=session_id, scope_key=scope_key).record_tool_result(
                scope_key=scope_key,
                tool_name=tool_name,
                args=args if isinstance(args, dict) else {},
                result=result_text,
                session_id=session_id,
            )
        # --- Anti-downgrade: record failed approaches ---
        if not success and session_id:
            key_args = ', '.join(f'{k}={v}' for k, v in (args if isinstance(args, dict) else {}).items() if k in ('query', 'path', 'pattern', 'url', 'command'))[:100]
            approach = f"{tool_name}({key_args})" if key_args else tool_name
            error_snippet = (result_text or '')[:150].replace('\n', ' ')
            record_ruled_out(session_id, approach, error_snippet, db_conn=conn)
    except Exception:
        try:
            if conn:
                conn.rollback()
        except Exception:
            pass


def _pre_tool_call(**kwargs):
    tool_name = str(kwargs.get('tool_name') or '')
    if tool_name not in {'session_search', 'search_files'}:
        return None
    session_id = str(kwargs.get('session_id') or kwargs.get('task_id') or '')
    db_path = _db_path()
    if not Path(db_path).exists():
        return None
    conn = None
    try:
        conn = _get_connection()
        conn.row_factory = sqlite3.Row
        scope_key, user_text = _latest_tool_context(conn, session_id, time.time())
        if not user_text:
            args = kwargs.get('args') if isinstance(kwargs.get('args'), dict) else {}
            user_text = str(args.get('query') or args.get('q') or '')
        if not user_text:
            return None
        args = kwargs.get('args') if isinstance(kwargs.get('args'), dict) else {}
        if _tool_call_is_local_debug(tool_name, args, user_text):
            return None
        EpistemicManager = _load_epistemic_manager_class()
        classification = EpistemicManager(conn, session_id=session_id, scope_key=scope_key or 'global').classify(user_text)
        if classification.should_research:
            return {
                'action': 'block',
                'message': (
                    'Blocked by Live Brain epistemic guard: session_search/search_files are stale for current/high-stakes questions. '
                    'Use brain_epistemic(action=search_web) authoritative_sources; if extraction is unavailable, answer with safe_answer and do not invent details.'
                ),
            }
    except Exception:
        return None
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass
    return None


def _post_tool_call(**kwargs):
    _record_tool_result(
        str(kwargs.get('tool_name') or ''),
        kwargs.get('args') if isinstance(kwargs.get('args'), dict) else {},
        kwargs.get('result'),
        session_id=str(kwargs.get('session_id') or ''),
        tool_call_id=str(kwargs.get('tool_call_id') or ''),
        duration_ms=kwargs.get('duration_ms'),
    )
    return None


def _get_active_session_evidence(session_id: str, user_message: str) -> List[str]:
    if os.environ.get('LIVE_BRAIN_ACTIVE_SESSION_EVIDENCE', '0') != '1':
        return []
    sessions_dir = Path(_hermes_home()) / 'sessions'
    candidates = []
    if session_id:
        p = sessions_dir / f'{session_id}.jsonl'
        if p.exists():
            candidates.append(p)
    if not candidates:
        candidates = sorted(sessions_dir.glob('*.jsonl'), key=lambda p: p.stat().st_mtime, reverse=True)[:1]
    if not candidates:
        return []
    try:
        import json as _json
        query_words = [w for w in (user_message or '').lower().split() if len(w) > 3]
        evidence = []
        with open(candidates[0]) as f:
            msgs = [_json.loads(l) for l in f if l.strip()]
        for m in reversed(msgs):
            if m.get('role') != 'tool':
                continue
            content = str(m.get('content') or '')
            if len(content) < 20:
                continue
            stripped = content.strip()
            if stripped.startswith(('{', '[', '```')) or re.search(r'\b(?:def|class)\s+\w+\s*\(|"proposals"\s*:|"tool_calls"\s*:|Traceback \(most recent call last\)', stripped):
                continue
            if query_words and not any(w in content.lower() for w in query_words):
                continue
            evidence.append(content[:200].replace('\n', ' '))
            if len(evidence) >= 3:
                break
        return list(reversed(evidence))
    except Exception:
        return []


def _context_sections(context: str) -> List[str]:
    return [line[:-1] for line in (context or '').splitlines() if line.endswith(':') and line[:-1].isupper()]


def _record_context_impression(scope_key: str, session_id: str, user_message: str, context: str, recipe_ids: List[str] | None = None, *, allow_empty: bool = False) -> None:
    if not context and not allow_empty:
        return
    db_path = _db_path()
    if not Path(db_path).exists():
        return
    now = time.time()
    context_hash = hashlib.sha256(context.encode('utf-8', 'ignore')).hexdigest()[:24]
    impression_id = 'impression:' + hashlib.sha256(f'{scope_key}{session_id}{user_message}{context_hash}{int(now)}'.encode()).hexdigest()[:24]
    try:
        conn = _get_connection()
        conn.execute(
            "INSERT OR REPLACE INTO context_impressions (impression_id, scope_key, session_id, query_text, context_hash, sections_json, recipe_ids_json, outcome, feedback_text, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', '', ?, ?)",
            (impression_id, scope_key, session_id, user_message[:500], context_hash, json.dumps(_context_sections(context)), json.dumps(recipe_ids or []), now, now),
        )
        conn.commit()
        conn.close()
    except Exception:
        try:
            conn.close()
        except Exception:
            pass


def _pre_llm_call(**kwargs):
    user_message = str(kwargs.get("user_message") or "")
    session_id = str(kwargs.get("session_id") or "")
    sender_id = str(kwargs.get("sender_id") or "")
    platform = str(kwargs.get("platform") or "")
    scope_key = _extract_scope_key(
        user_message, sender_id, session_id, platform=platform or 'telegram',
    )
    isolate_epistemic_context = _should_isolate_epistemic_context(user_message)
    if user_message:
        _record_reality_event(
            scope_key,
            'user_message',
            'user_message',
            {'text': user_message, 'sender_id': sender_id, 'platform': platform},
            session_id=session_id,
            source='pre_llm_call',
            confidence=0.78,
        )
    epistemic_query = _epistemic_query_text(user_message) if isolate_epistemic_context else user_message
    context = '' if isolate_epistemic_context else _load_live_brain_context(user_message, session_id, sender_id)
    if not isolate_epistemic_context and _should_load_reality_brief(user_message):
        reality_brief = _load_reality_brief(scope_key, user_message)
        if reality_brief:
            context = (reality_brief + "\n\n" + context) if context else reality_brief
    epistemic_brief = _load_epistemic_brief(scope_key, epistemic_query, session_id)
    if epistemic_brief:
        context = (epistemic_brief + "\n\n" + context) if context else epistemic_brief
    epistemic_autonomous_context = _load_epistemic_autonomous_context(scope_key, epistemic_query, session_id)
    if epistemic_autonomous_context:
        context = (epistemic_autonomous_context + "\n\n" + context) if context else epistemic_autonomous_context
    if isolate_epistemic_context:
        isolation = (
            "EPISTEMIC ISOLATION:\n"
            "- Answer only the current/high-stakes research question from official/primary sources.\n"
            "- Do not mention Live Brain run ids, codenames, active tasks, prior diagnostic causes, or next actions unless the user explicitly asks for them in this turn."
        )
        context = (isolation + "\n\n" + context) if context else isolation
    active_evidence = [] if isolate_epistemic_context else _get_active_session_evidence(session_id, user_message)
    if active_evidence:
        evidence_block = "ACTIVE SESSION EVIDENCE:\n- " + "\n- ".join(active_evidence)
        context = (context + "\n" + evidence_block) if context else evidence_block
    if not context:
        if user_message and not _is_chit_chat(user_message):
            _record_context_impression(scope_key, session_id, user_message, '', [], allow_empty=True)
        return None

    # --- Cognitive Architecture injection ---
    try:
        fact_count = context.count('\n') if 'KNOWN FACTS' in context else 0
        query_words = [w for w in re.findall(r'[\w./-]+', (user_message or '').lower()) if len(w) > 3]
        conn = _get_connection() if Path(_db_path()).exists() else None
        cognitive_ctx = get_cognitive_context(
            user_message, session_id, fact_count,
            scope_key=scope_key, query_words=query_words, db_conn=conn,
        )
        if conn:
            conn.close()
        if cognitive_ctx:
            context = cognitive_ctx + "\n\n" + context
    except Exception:
        pass

    _record_context_impression(scope_key, session_id, user_message, context, list(_LAST_CONTEXT_METADATA.get('recipe_ids') or []))
    return {"context": context}


def _get_maintenance_executor() -> ThreadPoolExecutor:
    """Lazy-init single-worker executor for background DB maintenance."""
    global _MAINTENANCE_EXECUTOR
    with _MAINTENANCE_EXECUTOR_LOCK:
        if _MAINTENANCE_EXECUTOR is None:
            _MAINTENANCE_EXECUTOR = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="lb-ctx-maintenance",
            )
        return _MAINTENANCE_EXECUTOR


def _run_maintenance_bg(db_path: str, started_at: float) -> None:
    """Run DB maintenance on a fresh background connection. Never raises."""
    conn = None
    try:
        conn = sqlite3.connect(db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        _perform_db_maintenance(conn, started_at)
    except Exception as exc:
        logger.warning("[LIVE_BRAIN_CTX] background maintenance failed: %s", exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _post_llm_call(**kwargs):
    global _LAST_MAINTENANCE_TIME
    user_message = str(kwargs.get("user_message") or "")
    assistant_response = str(kwargs.get("assistant_response") or "")
    session_id = str(kwargs.get("session_id") or "")
    platform = str(kwargs.get("platform") or "")
    if not assistant_response:
        return None
    scope_key = ''
    conn = None
    try:
        db_path = _db_path()
        if Path(db_path).exists():
            created_at = time.time()
            conn = _get_connection()
            row = conn.execute(
                "SELECT scope_key FROM context_impressions WHERE session_id=? AND created_at >= ? ORDER BY created_at DESC LIMIT 1",
                (session_id, created_at - 1800),
            ).fetchone()
            if row:
                scope_key = str(row[0] or '')

            # Schedule maintenance in a background thread if the throttle
            # interval has elapsed. We set _LAST_MAINTENANCE_TIME BEFORE
            # submitting so concurrent post_llm_call invocations do not
            # double-submit.
            if created_at - _LAST_MAINTENANCE_TIME >= _MAINTENANCE_INTERVAL:
                _LAST_MAINTENANCE_TIME = created_at
                try:
                    _get_maintenance_executor().submit(
                        _run_maintenance_bg, db_path, created_at,
                    )
                except RuntimeError:
                    # Executor was shut down during teardown — drop silently.
                    logger.debug("[LIVE_BRAIN_CTX] maintenance submit skipped (executor closed)")

            conn.close()
            conn = None
    except Exception:
        try:
            if conn:
                conn.close()
        except Exception:
            pass
    if not scope_key:
        scope_key = _extract_scope_key(
            user_message, '', session_id, platform=platform or 'telegram',
        )
    _record_reality_event(
        scope_key,
        'assistant_response',
        'assistant_response',
        {'text': user_message, 'assistant_response': assistant_response[:4000], 'platform': platform},
        session_id=session_id,
        source='post_llm_call',
        confidence=0.72,
    )
    _record_epistemic_answer_if_source_backed(scope_key, user_message, assistant_response, session_id)
    return None
