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
    from ...live_brain.connection_pool import ConnectionPool
except Exception:
    ConnectionPool = None

try:
    from live_brain.memory_compiler import (
        MemoryCompiler,
        build_context_from_objects,
        classify_turn_control,
        ensure_memory_v2_schema,
    )
except Exception:
    MemoryCompiler = None
    build_context_from_objects = None
    classify_turn_control = None
    ensure_memory_v2_schema = None

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
    allowed_sections_for_intent,
    section_budget_for_intent,
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
from .text_processing import _truncate_fact, _redact, redact_for_storage
from .query_classification import (
    _classify_query_intent,
    classify_turn_lane,
    _is_recap_query,
    _is_diagnostic_query,
    _is_approval_query,
)
from .cognitive_architecture import get_cognitive_context, record_ruled_out, ensure_ruled_out_table, _count_facts_in_context, get_last_tier, check_attack_quality

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Mutable module-level globals
# ---------------------------------------------------------------------------
_LAST_MAINTENANCE_TIME = 0.0
_MAINTENANCE_INTERVAL = 3600.0
_MAINTENANCE_EXECUTOR: Optional[ThreadPoolExecutor] = None
_MAINTENANCE_EXECUTOR_LOCK = threading.Lock()
_FINGERPRINT_EXECUTOR: Optional[ThreadPoolExecutor] = None
_FINGERPRINT_EXECUTOR_LOCK = threading.Lock()
_LAST_CONTEXT_METADATA: Dict[str, Any] = {'recipe_ids': []}
_SESSION_LANE_STATE: Dict[str, Dict[str, Any]] = {}
_ROUTING_INTENTS = {'task_execution', 'local_repo_lookup', 'continuity_recap'}
_ROUTING_SECTION_ALLOWLIST: Dict[str, set[str]] = {
    'reality_state': {
        'MUST FOLLOW', 'VERIFIED ARTIFACTS', 'ACTIVE TASK', 'ACTIVE OBJECTIVES',
        'NEXT REQUIRED ACTION', 'INFRASTRUCTURE', 'AUTHORED THIS SESSION',
        'KNOWN FACTS', 'PROVEN FIX', 'VERIFICATION REQUIRED',
    },
    'incident_truth': {
        'MUST FOLLOW', 'VERIFIED ARTIFACTS', 'KNOWN FACTS', 'PROVEN FIX',
        'INFRASTRUCTURE', 'AUTHORED THIS SESSION', 'VERIFICATION REQUIRED',
        'OPEN BUG', 'NEXT REQUIRED ACTION',
    },
    'entity_graph': {
        'MUST FOLLOW', 'VERIFIED ARTIFACTS', 'KNOWN FACTS', 'PROVEN FIX',
        'INFRASTRUCTURE', 'AUTHORED THIS SESSION', 'FILE KNOWLEDGE',
    },
}
_LANE_SECTION_ALLOWLIST: Dict[str, set[str]] = {
    'document_intake': {
        'VERIFIED ARTIFACTS', 'FILE KNOWLEDGE', 'VERIFICATION REQUIRED',
        'KNOWN FACTS',
    },
    'simple_execution': {
        'MUST FOLLOW', 'VERIFIED ARTIFACTS', 'KNOWN FACTS', 'FILE KNOWLEDGE',
        'VERIFICATION REQUIRED', 'UNVERIFIED CLAIM', 'LATEST RECAP',
    },
    'deep_execution': {
        'MUST FOLLOW', 'VERIFIED ARTIFACTS', 'KNOWN FACTS', 'PROVEN FIX',
        'ACTIVE TASK', 'OPEN BUG', 'NEXT REQUIRED ACTION', 'FILE KNOWLEDGE',
        'VERIFICATION REQUIRED', 'UNVERIFIED CLAIM', 'INFRASTRUCTURE',
        'AUTHORED THIS SESSION',
    },
    'research_or_epistemic': {
        'MUST FOLLOW', 'KNOWN FACTS', 'EPISTEMIC STATUS', 'VERIFIED ARTIFACTS',
        'VERIFICATION REQUIRED',
    },
    'continuation_or_resume': {
        'LATEST RECAP', 'CONTINUITY MEMORY', 'KNOWN FACTS', 'VERIFIED ARTIFACTS',
        'ACTIVE TASK', 'FILE KNOWLEDGE', 'VERIFICATION REQUIRED',
    },
    'approval_flow': {
        'PENDING APPROVAL', 'APPROVAL ROUTING', 'MUST FOLLOW',
    },
    'chit_chat': set(),
}
_NO_WIDEN_RE = re.compile(r'\b(?:ne\s+siri\s+temu|ne\s+širi\s+temu|do\s+not\s+widen|stay\s+on\s+topic)\b', re.IGNORECASE)
COMPACTION_RE = re.compile(r'^\s*\[CONTEXT COMPACTION', re.IGNORECASE)

# --- Pillar 1: pre-action risk gate (warn-only initial mode) ---
# Destructive shell command patterns. Conservative: anchors like `origin` /
# absolute paths reduce false positives on local-only operations.
_DESTRUCTIVE_TERMINAL_RE = re.compile(
    r'(?:^|\s|&&|;|\|)\s*(?:'
    r'rm\s+-rf?\s+/|sudo\s+rm\s+-rf?|dd\s+if=|mkfs|:\(\)\{|kill\s+-9|killall|shutdown|reboot|'
    r'git\s+push\s+(?:-f|--force)\s+(?:origin|upstream)|'
    r'git\s+reset\s+--hard\s+(?:origin|upstream)|git\s+clean\s+-fd|'
    r'drop\s+(?:table|database|schema)\b|truncate\s+table\b|delete\s+from\s+\w+\s*(?:;|$)|'
    r'docker\s+system\s+prune|docker\s+rm\s+-f|chmod\s+-R\s+777|chown\s+-R\s+root'
    r')',
    re.IGNORECASE,
)
# Tool names that inherently change persistent state in risky ways.
_HIGH_RISK_TOOL_NAMES = {
    'delete_file', 'remove_file', 'unlink', 'drop_table', 'schema_migrate',
}
# Risk gate mode: 'warn' logs to reality_events without blocking, 'enforce'
# blocks and demands approval. Flip to 'enforce' after a week of warn-data
# review to confirm false-positive rate is acceptable. Pillar 5 (approval
# cookie) lands together with the flip.
_RISK_GATE_MODE = 'warn'  # 'warn' | 'enforce'

# Sensitive write paths — match anywhere in the path
_SENSITIVE_WRITE_RE = re.compile(
    r'(?:'
    r'/migrations/[^/]*\.sql$|'
    r'/schema[^/]*\.sql$|'
    r'(?:^|/)\.env(?:\.|$)|'
    r'(?:^|/)secret|'
    r'(?:^|/)credentials'
    r')',
    re.IGNORECASE,
)


def _classify_action_risk(tool_name: str, args: Any) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Return (action_type, payload) when this tool call looks risky, else None.

    Returns the minimal payload needed for reality_event logging — keeps the
    audit trail compact and avoids leaking large strings into the event store.
    """
    args_dict = args if isinstance(args, dict) else {}
    # Destructive shell commands
    if tool_name == 'terminal':
        cmd = str(args_dict.get('command') or args_dict.get('cmd') or '')
        if cmd and _DESTRUCTIVE_TERMINAL_RE.search(cmd):
            return ('terminal_destructive', {'command': cmd[:200]})
    # High-risk tool names
    if tool_name in _HIGH_RISK_TOOL_NAMES:
        path = str(args_dict.get('path') or args_dict.get('file') or '')
        return ('high_risk_tool', {'tool': tool_name, 'path': path[:200]})
    # Sensitive write targets
    if tool_name in _WRITE_TOOLS:
        path = str(args_dict.get('path') or args_dict.get('file') or args_dict.get('filename') or '')
        if path and _SENSITIVE_WRITE_RE.search(path):
            return ('sensitive_write', {'path': path[:200], 'tool': tool_name})
    return None


def _load_recent_risk_warnings_block(scope_key: str, conn, since_seconds: int = 600, *, session_id: str = '') -> str:
    """Surface recent risk_warning reality_events as RECENT RISK ACTIVITY.

    Read-only. Only shows entries from the last `since_seconds` (default 10m)
    so the block doesn't haunt the user forever after one warning.

    P3.2: also merges nucleus's live ``pending_changes`` from the
    cross-plugin bridge when ``session_id`` is provided, so the block is
    a single coherent risk surface instead of two parallel ones.
    """
    bridge_lines: List[str] = []
    if session_id:
        try:
            from .bridge import get_pending_changes
            for ch in get_pending_changes(session_id):
                if ch.get('has_backup'):
                    continue
                desc = str(ch.get('desc') or ch.get('path') or ch.get('type') or 'change')[:140]
                risk = ch.get('risk') or 0.0
                bridge_lines.append(f'- pending {ch.get("type", "?")} (risk={float(risk):.1f}): {desc}')
        except Exception as exc:
            logger.debug('[LIVE_BRAIN_CTX] bridge pending_changes read failed: %s', exc)

    db_lines: List[str] = []
    if scope_key:
        try:
            cutoff = time.time() - since_seconds
            rows = conn.execute(
                "SELECT created_at, subject, payload_json "
                "  FROM reality_events "
                " WHERE scope_key=? AND event_type='risk_warning' "
                "   AND created_at > ? "
                " ORDER BY created_at DESC LIMIT 5",
                (scope_key, cutoff),
            ).fetchall()
            for row in rows:
                try:
                    payload = json.loads(row[2] or '{}')
                except Exception:
                    payload = {}
                age = _format_relative_time(float(row[0] or 0))
                subject = str(row[1] or 'risk')
                cmd = str(payload.get('command') or payload.get('path') or payload.get('tool') or '')
                db_lines.append(f'- {age}: {subject} — {cmd[:140]}')
        except Exception as exc:
            logger.debug('[LIVE_BRAIN_CTX] _load_recent_risk_warnings_block failed: %s', exc)

    if not bridge_lines and not db_lines:
        return ''
    lines = ['RECENT RISK ACTIVITY:']
    lines.extend(bridge_lines)
    lines.extend(db_lines)
    lines.append('Confirm each of these was intended; the system did not block them.')
    return '\n'.join(lines)


# --- Pillar 4: done-claim auditor ---
# Phrases that signal the agent claims completion. Multilingual (en + sr/bs).
_DONE_RE = re.compile(
    r'\b(done|fixed|works(?:\s+now)?|complete(?:d|ly)?|resolved|ready|good\s+to\s+go|'
    r'all\s+set|merged|shipped|popravlj(?:en[oa]?|eno)|zavr[sš]en[oa]?|gotov[oa]?|'
    r're[sš]eno|poslat[oa]?|isporu[cč]en[oa]?)\b',
    re.IGNORECASE,
)
# Process-local turn log: session_id -> [(tool_name, args_blob, success, ts), ...]
_TURN_TOOL_LOG: Dict[str, List[Tuple[str, str, bool, float]]] = {}
_TURN_LOG_LOCK = threading.Lock()
_TURN_LOG_MAX_PER_SESSION = 50

# Tools that write or modify files — trigger fingerprinting after success
_WRITE_TOOLS = {'write_file', 'patch', 'apply_patch', 'edit_file', 'create_file'}
_ARTIFACT_PRODUCING_TOOLS = _WRITE_TOOLS | {'execute_code', 'terminal'}
_VISUAL_ARTIFACT_EXTENSIONS = {'.pdf', '.png', '.jpg', '.jpeg', '.webp'}

# File extensions worth fingerprinting; binaries/images are skipped
_FINGERPRINT_EXTENSIONS = {
    '.py', '.js', '.jsx', '.ts', '.tsx', '.md', '.yaml', '.yml', '.json',
    '.sh', '.bash', '.zsh', '.sql', '.toml', '.rs', '.go', '.c', '.cpp',
    '.h', '.hpp', '.java', '.rb', '.php', '.html', '.css', '.scss', '.txt',
    '.cfg', '.conf', '.ini', '.env', '.example',
}

# Cap fingerprint read at 64 KB — enough for signatures + docstring
_FINGERPRINT_READ_CAP = 64 * 1024

# Per-language signature extractors (compiled lazily)
_SIGNATURE_PATTERNS = {
    'python': re.compile(r'^[ \t]*(?:async\s+)?(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)', re.MULTILINE),
    'javascript': re.compile(r'^[ \t]*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function|class|const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)', re.MULTILINE),
    'shell': re.compile(r'^[ \t]*(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(\s*\)\s*\{', re.MULTILINE),
    'rust': re.compile(r'^[ \t]*(?:pub\s+)?(?:async\s+)?(?:fn|struct|enum|trait|impl)\s+([A-Za-z_][A-Za-z0-9_]*)', re.MULTILINE),
    'go': re.compile(r'^[ \t]*func\s+(?:\([^)]+\)\s+)?([A-Za-z_][A-Za-z0-9_]*)', re.MULTILINE),
}

# Map extension → language key for signature extraction
_EXT_TO_LANGUAGE = {
    '.py': 'python',
    '.js': 'javascript', '.jsx': 'javascript', '.ts': 'javascript', '.tsx': 'javascript',
    '.sh': 'shell', '.bash': 'shell', '.zsh': 'shell',
    '.rs': 'rust',
    '.go': 'go',
}


def get_turn_lane(session_id: str) -> str:
    return str((_SESSION_LANE_STATE.get(session_id or '') or {}).get('turn_lane') or '')

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
_connection_pool_db_path = None


def _hermes_home() -> str:
    return os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))


def _db_path() -> str:
    return str(Path(_hermes_home()) / "live_brain" / "live_brain.db")


def _get_connection_pool():
    """Lazy initialization of connection pool."""
    global _connection_pool, _connection_pool_db_path
    current_db_path = _db_path()
    if (
        _connection_pool is not None
        and _connection_pool_db_path
        and _connection_pool_db_path != current_db_path
    ):
        # Tests and multi-home runs swap HERMES_HOME at runtime; stale pooled handles must not follow.
        try:
            _connection_pool.close_all()
        except Exception:
            pass
        _connection_pool = None
        _connection_pool_db_path = None
    if _connection_pool is None and ConnectionPool is not None:
        _connection_pool = ConnectionPool(current_db_path, max_connections=10)
        _connection_pool_db_path = current_db_path
    return _connection_pool


def _get_connection():
    """Get a database connection, using pool if available.

    Guards against the case where a caller (legacy code) called ``conn.close()``
    on a pooled connection — the thread-local cache still points to the
    now-closed handle, and `pool.get_connection()` would happily return it.
    We probe the cached handle with a cheap PRAGMA; on failure we clear the
    thread-local slot so a fresh connection is allocated.
    """
    pool = _get_connection_pool()
    if pool:
        conn = pool.get_connection()
        try:
            conn.execute("SELECT 1").fetchone()
            return conn
        except sqlite3.ProgrammingError:
            # Cached connection was closed externally — evict and retry once.
            try:
                pool._local.conn = None  # type: ignore[attr-defined]
            except Exception:
                pass
            try:
                pool._active.discard(conn)  # type: ignore[attr-defined]
            except Exception:
                pass
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
    try:
        import importlib.util as _importlib_util
        import sys as _sys
        import types as _types
        package_name = '_live_brain_ctx_live_brain'
        live_brain_dir = Path(__file__).resolve().parent.parent.parent / 'live_brain'
        if not live_brain_dir.exists():
            live_brain_dir = Path(__file__).resolve().parent.parent.parent.parent / 'live_brain'
        if not live_brain_dir.exists():
            return None
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
                return None
            module = _importlib_util.module_from_spec(spec)
            module.__package__ = package_name
            _sys.modules[full_name] = module
            spec.loader.exec_module(module)
        return _sys.modules[f'{package_name}.ingest'].Ingestor
    except Exception:
        return None
        spec.loader.exec_module(module)
    return _sys.modules[f'{package_name}.ingest'].Ingestor


@functools.lru_cache(maxsize=1)
def _load_artifact_registry_class():
    """Lazy-load ArtifactRegistry from the live_brain plugin.

    Mirrors _load_live_brain_ingestor_class — first tries the canonical
    `live_brain` import path, then falls back to spec-based loading from
    the plugins directory if that name isn't on sys.path.
    """
    try:
        from live_brain.artifacts import ArtifactRegistry
        return ArtifactRegistry
    except Exception:
        pass
    try:
        import importlib.util as _importlib_util
        import sys as _sys
        import types as _types
        package_name = '_live_brain_ctx_live_brain'
        live_brain_dir = Path(__file__).resolve().parent.parent.parent / 'live_brain'
        if not live_brain_dir.exists():
            live_brain_dir = Path(__file__).resolve().parent.parent.parent.parent / 'live_brain'
        if not live_brain_dir.exists():
            return None
        if package_name not in _sys.modules:
            package = _types.ModuleType(package_name)
            package.__path__ = [str(live_brain_dir)]
            _sys.modules[package_name] = package
        for module_name in ['utils', 'audit', 'artifacts']:
            full_name = f'{package_name}.{module_name}'
            if full_name in _sys.modules:
                continue
            spec = _importlib_util.spec_from_file_location(full_name, live_brain_dir / f'{module_name}.py')
            if spec is None or spec.loader is None:
                return None
            module = _importlib_util.module_from_spec(spec)
            module.__package__ = package_name
            _sys.modules[full_name] = module
            spec.loader.exec_module(module)
        return _sys.modules[f'{package_name}.artifacts'].ArtifactRegistry
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Feature 1: Auto file fingerprinting helpers
# ---------------------------------------------------------------------------

def _get_fingerprint_executor() -> ThreadPoolExecutor:
    """Lazy-init single-worker executor for background file fingerprinting."""
    global _FINGERPRINT_EXECUTOR
    with _FINGERPRINT_EXECUTOR_LOCK:
        if _FINGERPRINT_EXECUTOR is None:
            _FINGERPRINT_EXECUTOR = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix='lb-ctx-fingerprint',
            )
        return _FINGERPRINT_EXECUTOR


def _extract_signatures(text: str, language: str) -> List[str]:
    """Extract function/class/method names from source text via regex.

    Returns up to 30 unique names in order of appearance. Never raises.
    """
    pattern = _SIGNATURE_PATTERNS.get(language)
    if not pattern:
        return []
    try:
        seen: set = set()
        out: List[str] = []
        for match in pattern.finditer(text):
            name = match.group(1)
            if name and name not in seen and not name.startswith('_'):
                seen.add(name)
                out.append(name)
                if len(out) >= 30:
                    break
        return out
    except Exception:
        return []


def _extract_purpose(text: str, language: str) -> str:
    """Extract a short purpose summary from file content.

    Strategy:
    1. First triple-quoted docstring (Python)
    2. First /** ... */ JSDoc block (JS/TS)
    3. Leading // or # comment block
    4. First 240 non-blank characters as fallback
    """
    if not text:
        return ''
    try:
        # Python docstring
        if language == 'python':
            m = re.search(r'"""(.*?)"""', text, re.DOTALL)
            if not m:
                m = re.search(r"'''(.*?)'''", text, re.DOTALL)
            if m:
                doc = m.group(1).strip()
                if doc:
                    return doc[:240]
        # JSDoc / C-style block
        if language in ('javascript', 'rust', 'go'):
            m = re.search(r'/\*\*?(.*?)\*/', text, re.DOTALL)
            if m:
                doc = re.sub(r'^\s*\*\s?', '', m.group(1), flags=re.MULTILINE).strip()
                if doc:
                    return doc[:240]
        # Leading comment block (# for python/shell, // for others)
        comment_char = '#' if language in ('python', 'shell') else '//'
        lines = text.splitlines()
        collected: List[str] = []
        for line in lines[:40]:
            stripped = line.strip()
            if not stripped:
                if collected:
                    break
                continue
            if stripped.startswith(comment_char):
                collected.append(stripped.lstrip(comment_char).strip())
            elif collected:
                break
            elif not stripped.startswith(('"""', "'''", '/*', '*')):
                break
        if collected:
            return ' '.join(collected)[:240]
        # Fallback: first 240 chars of non-blank content
        condensed = re.sub(r'\s+', ' ', text.strip())
        return condensed[:240]
    except Exception:
        return ''


def _infer_project_key(path: str) -> str:
    """Infer a project key from the file path.

    Walks up from the file looking for a .git directory; if found, uses the
    directory's basename. Falls back to the immediate parent directory's
    basename. Returns '' for paths that resolve to obvious system locations.
    """
    try:
        p = Path(path).expanduser().resolve()
        # Walk up to find a git root
        for parent in [p] + list(p.parents):
            if (parent / '.git').exists():
                return parent.name
        # Fall back to parent directory name (e.g., /home/user/tmp/foo.py → 'tmp')
        if p.parent != p:
            return p.parent.name
        return ''
    except Exception:
        return ''


def _fingerprint_file(path: str) -> Optional[Dict[str, Any]]:
    """Read the file and extract checksum, signatures, purpose, and metadata.

    Returns None if the file is missing, binary, or too large to inspect.
    Caps reads at 64 KB. sha256 is computed over the full file via stream.
    """
    try:
        abs_path = Path(path).expanduser().resolve()
    except Exception:
        return None
    if not abs_path.exists() or not abs_path.is_file():
        return None
    suffix = abs_path.suffix.lower()
    if suffix and suffix not in _FINGERPRINT_EXTENSIONS:
        return None
    try:
        stat = abs_path.stat()
    except OSError:
        return None
    if stat.st_size > 5 * 1024 * 1024:  # 5 MB hard cap
        return None
    # Stream sha256 in 64 KB chunks
    try:
        h = hashlib.sha256()
        with open(abs_path, 'rb') as fh:
            while True:
                chunk = fh.read(_FINGERPRINT_READ_CAP)
                if not chunk:
                    break
                h.update(chunk)
        checksum = h.hexdigest()
    except Exception:
        return None
    # Read first 64 KB as text for signatures and purpose
    try:
        with open(abs_path, 'r', encoding='utf-8', errors='replace') as fh:
            text = fh.read(_FINGERPRINT_READ_CAP)
    except Exception:
        return None
    language = _EXT_TO_LANGUAGE.get(suffix, '')
    return {
        'path': str(abs_path),
        'checksum': checksum,
        'size': stat.st_size,
        'mtime': stat.st_mtime,
        'language': language,
        'signatures': _extract_signatures(text, language) if language else [],
        'summary': _extract_purpose(text, language),
    }


def _extract_candidate_artifact_paths(args: Any, result_text: str) -> List[str]:
    """Find local artifact paths mentioned by a tool call or result."""
    candidates: List[str] = []
    args_dict = args if isinstance(args, dict) else {}
    for key in ('path', 'file', 'filename', 'output', 'output_path', 'dest', 'destination'):
        value = args_dict.get(key)
        if isinstance(value, str) and value:
            candidates.append(value)
    for match in re.finditer(
        r'(?P<path>(?:/home/[^\s\'"<>]+|/tmp/[^\s\'"<>]+)\.(?:pdf|png|jpe?g|webp|txt|json|mp4|mov|mkv|wav|mp3|m4a|ogg))',
        result_text or '',
        re.IGNORECASE,
    ):
        candidates.append(match.group('path').rstrip('.,;:)'))

    resolved: List[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            path = str(Path(candidate).expanduser().resolve())
        except Exception:
            continue
        if path in seen:
            continue
        seen.add(path)
        try:
            if Path(path).is_file():
                resolved.append(path)
        except Exception:
            continue
    return resolved


def _tool_verifies_pending_path(tool_name: str, pending_path: str) -> bool:
    """Return True when this tool is an independent verifier for a path."""
    suffix = Path(pending_path).suffix.lower()
    if suffix in _VISUAL_ARTIFACT_EXTENSIONS:
        return tool_name in {'vision_analyze', 'brain_mark_artifact'}
    return tool_name in _VERIFIER_TOOLS


def _fingerprint_and_store(
    path: str,
    scope_key: str,
    session_id: str,
    source: str = 'auto_fingerprint',
    confidence: float = 0.9,
) -> None:
    """Background-thread entrypoint: fingerprint the file and upsert it.

    Opens its own sqlite connection (same pattern as _run_maintenance_bg).
    Never raises — all errors logged at debug level.
    """
    fp = _fingerprint_file(path)
    if not fp:
        return
    ArtifactRegistry = _load_artifact_registry_class()
    if ArtifactRegistry is None:
        return
    conn = None
    try:
        conn = sqlite3.connect(_db_path(), timeout=30.0)
        conn.row_factory = sqlite3.Row
        project_key = _infer_project_key(fp['path']) or 'unscoped'
        role = Path(fp['path']).stem or 'file'
        # Label: first line of summary, truncated to 80 chars for index efficiency
        label = (fp['summary'].splitlines()[0] if fp['summary'] else '')[:80]
        ArtifactRegistry(conn).upsert_artifact(
            project_key=project_key,
            role=role,
            path=fp['path'],
            label=label,
            status='verified',
            confidence=confidence,
            source=source,
            evidence={
                'checksum': fp['checksum'],
                'signatures': fp['signatures'],
                'summary': fp['summary'],
                'size': fp['size'],
                'mtime': fp['mtime'],
                'language': fp['language'],
            },
            scope_tags={
                'language': fp['language'],
                'session_id': session_id,
                'scope_key': scope_key,
            },
        )
        conn.commit()
    except Exception as exc:
        logger.debug('[LIVE_BRAIN_CTX] fingerprint_and_store failed for %s: %s', path, exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Feature 2: Reflexive FILE KNOWLEDGE injection
# ---------------------------------------------------------------------------

# Match path-like tokens with known code/text extensions
_FILE_TOKEN_RE = re.compile(
    r'[\w./-]+\.(?:py|js|jsx|ts|tsx|md|yaml|yml|json|sh|bash|zsh|sql|toml|rs|go|c|cpp|h|hpp|java|rb|php|html|css|scss|txt|cfg|conf|ini|env)\b',
    re.IGNORECASE,
)

# Common filenames that shouldn't trigger FILE KNOWLEDGE lookup on their own
# (too generic, would match many files)
_GENERIC_FILE_NAMES = {
    'utils.py', 'helpers.py', 'main.py', 'index.js', 'index.ts',
    'config.py', 'settings.py', 'app.py', '__init__.py',
    'readme.md', 'package.json', 'tsconfig.json',
}


def _extract_file_tokens(text: str) -> List[str]:
    """Extract file path / basename tokens from user message.

    Returns a list of distinct tokens (de-duplicated, lowercased for matching).
    Generic basenames are kept only if a path separator was present.
    """
    if not text:
        return []
    found: List[str] = []
    seen: set = set()
    for match in _FILE_TOKEN_RE.finditer(text):
        tok = match.group(0)
        # Require at least 4 chars or a path separator to reduce noise
        if len(tok) < 4 and '/' not in tok and '.' not in tok:
            continue
        # Generic names without a path component are skipped
        if tok.lower() in _GENERIC_FILE_NAMES and '/' not in tok:
            continue
        if tok not in seen:
            seen.add(tok)
            found.append(tok)
    return found[:5]  # cap at 5 tokens per query


def _format_relative_time(then: float, now: Optional[float] = None) -> str:
    """Human-friendly relative time (e.g. '2h ago', '5m ago')."""
    if not then:
        return ''
    now = now or time.time()
    delta = max(0.0, now - then)
    if delta < 60:
        return f'{int(delta)}s ago'
    if delta < 3600:
        return f'{int(delta // 60)}m ago'
    if delta < 86400:
        return f'{int(delta // 3600)}h ago'
    return f'{int(delta // 86400)}d ago'


def _load_file_knowledge_block(user_message: str, scope_key: str, conn) -> str:
    """Build the FILE KNOWLEDGE context section for files mentioned in *user_message*.

    Queries verified_artifacts for fingerprinted files whose path contains one
    of the extracted tokens. For each hit, also fetches the last few
    tool_results touching that path. Returns formatted text or '' if no hits.
    Never raises.
    """
    if not user_message:
        return ''
    try:
        # Don't inject for chit-chat
        if _is_chit_chat(user_message):
            return ''
        tokens = _extract_file_tokens(user_message)
        if not tokens:
            return ''
        # Build LIKE patterns for each token
        hits: List[Dict[str, Any]] = []
        seen_paths: set = set()
        for tok in tokens:
            # If the token contains a path separator, match it as a suffix;
            # otherwise match it as a filename ending
            if '/' in tok:
                pattern = f'%{tok}'
            else:
                pattern = f'%/{tok}'
            try:
                rows = conn.execute(
                    """SELECT path, label, evidence_json, updated_at, project_key
                         FROM verified_artifacts
                        WHERE source IN ('auto_fingerprint','auto_fingerprint_read')
                          AND status='verified'
                          AND path LIKE ?
                        ORDER BY updated_at DESC LIMIT 3""",
                    (pattern,),
                ).fetchall()
            except Exception:
                continue
            for row in rows:
                path = str(row[0] or '')
                if not path or path in seen_paths:
                    continue
                seen_paths.add(path)
                try:
                    ev = json.loads(row[2] or '{}')
                except Exception:
                    ev = {}
                hits.append({
                    'path': path,
                    'label': str(row[1] or ''),
                    'evidence': ev,
                    'updated_at': float(row[3] or 0),
                    'project_key': str(row[4] or ''),
                })
                if len(hits) >= 5:
                    break
            if len(hits) >= 5:
                break
        if not hits:
            return ''
        # Pull recent tool_results for each path
        for hit in hits:
            try:
                rows = conn.execute(
                    """SELECT tool_name, success, error_type, created_at
                         FROM tool_results
                        WHERE artifact_path=?
                        ORDER BY created_at DESC LIMIT 3""",
                    (hit['path'],),
                ).fetchall()
                hit['recent'] = [
                    {
                        'tool': str(r[0] or ''),
                        'success': bool(r[1]),
                        'error_type': str(r[2] or ''),
                        'when': float(r[3] or 0),
                    }
                    for r in rows
                ]
            except Exception:
                hit['recent'] = []
        # Format the block — keep it tight, agent should still feel like it KNOWS
        lines: List[str] = ['FILE KNOWLEDGE:']
        for hit in hits:
            ev = hit['evidence']
            sigs = ev.get('signatures') or []
            summary = (ev.get('summary') or hit['label'] or '').replace('\n', ' ').strip()[:160]
            age = _format_relative_time(hit['updated_at'])
            header = f'- {hit["path"]}' + (f' (fingerprinted {age})' if age else '')
            lines.append(header)
            if summary:
                lines.append(f'  purpose: {summary}')
            if sigs:
                lines.append(f'  defines: {", ".join(sigs[:10])}')
            if hit.get('recent'):
                events = []
                for ev_row in hit['recent']:
                    mark = '✓' if ev_row['success'] else '✗'
                    when = _format_relative_time(ev_row['when'])
                    err = f' ({ev_row["error_type"]})' if not ev_row['success'] and ev_row['error_type'] else ''
                    events.append(f'{mark} {ev_row["tool"]} {when}{err}')
                if events:
                    lines.append(f'  recent: {" | ".join(events[:3])}')
        return '\n'.join(lines)
    except Exception as exc:
        logger.debug('[LIVE_BRAIN_CTX] _load_file_knowledge_block failed: %s', exc)
        return ''


# ---------------------------------------------------------------------------
# Feature 4: Session read cache (via fingerprint reuse)
# ---------------------------------------------------------------------------

# Tools whose successful results should opportunistically fingerprint the file
_READ_TOOLS = {'read_file', 'view_file', 'cat_file'}

# Track (session_id, abs_path) pairs we've already logged a CACHE HIT for, so
# observability stays at one log line per session+path.
_CACHE_HITS_LOGGED: set = set()


def _try_cached_read(path: str, session_id: str) -> Optional[str]:
    """Return a formatted cached summary if the file has a fresh fingerprint.

    A fingerprint is considered fresh when both stored size and mtime match
    the current on-disk values. If anything is stale or missing, returns None
    so the real read_file tool runs.
    """
    if not path:
        return None
    try:
        abs_path = Path(path).expanduser().resolve()
    except Exception:
        return None
    if not abs_path.exists() or not abs_path.is_file():
        return None
    try:
        stat = abs_path.stat()
    except OSError:
        return None
    conn = None
    try:
        db_path = _db_path()
        if not Path(db_path).exists():
            return None
        conn = sqlite3.connect(db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT label, evidence_json, source, updated_at
                 FROM verified_artifacts
                WHERE path=? AND source IN ('auto_fingerprint','auto_fingerprint_read')
                  AND status='verified'
                ORDER BY updated_at DESC LIMIT 1""",
            (str(abs_path),),
        ).fetchone()
        if not row:
            return None
        try:
            ev = json.loads(row['evidence_json'] or '{}')
        except Exception:
            return None
        cached_size = ev.get('size')
        cached_mtime = ev.get('mtime')
        # Cache is fresh only if both size and mtime match within 1 second tolerance.
        if cached_size is None or cached_mtime is None:
            return None
        if int(cached_size) != int(stat.st_size):
            return None
        if abs(float(cached_mtime) - float(stat.st_mtime)) > 1.0:
            return None
        # Build the cached view
        summary = (ev.get('summary') or '').strip()
        sigs = ev.get('signatures') or []
        language = ev.get('language') or ''
        size_kb = stat.st_size / 1024.0
        lines = [
            '[CACHED FILE KNOWLEDGE — checksum verified, mtime unchanged]',
            f'Path: {abs_path}',
        ]
        if language:
            lines.append(f'Language: {language}')
        if summary:
            lines.append(f'Purpose: {summary}')
        if sigs:
            lines.append(f'Signatures: {", ".join(sigs[:20])}')
        lines.append(f'Size: {size_kb:.1f} KB')
        lines.append('')
        lines.append('To force a full re-read, call read_file with full=true.')
        # Log one HIT per session+path
        log_key = (session_id, str(abs_path))
        if log_key not in _CACHE_HITS_LOGGED:
            _CACHE_HITS_LOGGED.add(log_key)
            logger.info('[LIVE_BRAIN_CTX] [CACHE HIT] %s session=%s', abs_path, session_id[:16])
        return '\n'.join(lines)
    except Exception as exc:
        logger.debug('[LIVE_BRAIN_CTX] _try_cached_read failed for %s: %s', path, exc)
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Feature 3: Failure → recipe seeding + replay
# ---------------------------------------------------------------------------

# Generic error-type buckets we'll match queries against for recipe recall
_ERROR_TYPE_KEYWORDS = {
    'file_not_found': ['filenotfound', 'no such file', 'no such directory', 'not found'],
    'permission_denied': ['permission denied', 'eacces', 'access denied'],
    'module_not_found': ['modulenotfound', 'no module named', 'cannot find module'],
    'import_error': ['importerror', 'cannot import name'],
    'syntax_error': ['syntaxerror', 'unexpected token', 'parse error'],
    'connection_refused': ['connection refused', 'econnrefused', 'failed to connect'],
    'timeout': ['timed out', 'timeout', 'deadline exceeded'],
    'attribute_error': ['attributeerror', "has no attribute"],
    'type_error': ['typeerror', 'unexpected type'],
    'key_error': ['keyerror'],
    'value_error': ['valueerror'],
    'database_locked': ['database is locked', 'sqlite_busy'],
    'file_is_not_db': ['file is not a database'],
}


def _classify_error_text(text: str) -> str:
    """Best-effort error-type classifier for failure→recipe seeding.

    Uses the same keyword buckets the recipe-recall step looks up. Returns
    '' when nothing matches so the caller can skip seeding noisy failures.
    """
    if not text:
        return ''
    lowered = text.lower()
    for et, keywords in _ERROR_TYPE_KEYWORDS.items():
        for kw in keywords:
            if kw in lowered:
                return et
    return ''


def _build_problem_pattern(tool_name: str, args: Any, error_type: str, result_text: str) -> str:
    """Build a short, searchable problem-pattern key for the fix_recipes row."""
    args_dict = args if isinstance(args, dict) else {}
    key_args = []
    for k in ('path', 'file', 'url', 'command', 'pattern', 'query'):
        v = args_dict.get(k)
        if isinstance(v, str) and v:
            key_args.append(f'{k}={v[:60]}')
            break  # one key arg is enough
    args_repr = ' '.join(key_args) if key_args else ''
    snippet = re.sub(r'\s+', ' ', (result_text or ''))[:80]
    parts = [tool_name or 'tool']
    if error_type:
        parts.append(error_type)
    if args_repr:
        parts.append(args_repr)
    if snippet:
        parts.append(snippet)
    return ' | '.join(parts)[:240]


def _record_failure_recipe(
    conn,
    scope_key: str,
    tool_name: str,
    args: Any,
    result_text: str,
    error_type: str,
    created_at: float,
) -> None:
    """Insert (or bump) a candidate fix_recipes row from a tool failure.

    Re-occurrences increment times_confirmed and bump updated_at. When
    times_confirmed reaches 2, status flips to 'active' so recall can
    surface the recipe.
    """
    if not error_type:
        # Skip unclassified noise — too easy to spam the table
        return
    problem_pattern = _build_problem_pattern(tool_name, args, error_type, result_text)
    if not problem_pattern:
        return
    # Stable recipe_id keyed by scope + pattern so reoccurrences hit the same row
    recipe_id = 'recipe:' + hashlib.sha256(
        f'{scope_key}|{problem_pattern}'.encode('utf-8', 'ignore')
    ).hexdigest()[:24]
    args_dict = args if isinstance(args, dict) else {}
    try:
        # Try insert; if it conflicts, bump the existing row.
        existing = conn.execute(
            'SELECT times_confirmed, promotion_status FROM fix_recipes WHERE recipe_id=?',
            (recipe_id,),
        ).fetchone()
        if existing is None:
            conn.execute(
                """INSERT INTO fix_recipes (
                    recipe_id, scope_key, problem_pattern, tool_name,
                    steps_json, args_template_json, success_criteria,
                    confidence, times_confirmed, status, source, scope_tags_json,
                    created_at, updated_at,
                    artifact_verified, artifact_path, error_type, promotion_status,
                    candidate_since
                ) VALUES (?, ?, ?, ?, '[]', ?, '', 0.5, 1, 'candidate',
                          'auto_failure_seed', '{}', ?, ?, 0, ?, ?, 'candidate', ?)""",
                (
                    recipe_id, scope_key, problem_pattern, tool_name,
                    json.dumps(args_dict, ensure_ascii=False, default=str)[:400],
                    created_at, created_at,
                    str(args_dict.get('path') or args_dict.get('file') or '')[:240],
                    error_type, created_at,
                ),
            )
        else:
            new_times = int(existing[0] or 0) + 1
            # Promote to 'active' once seen at least twice
            new_status = 'active' if new_times >= 2 else 'candidate'
            new_promotion = 'active' if new_times >= 2 else 'candidate'
            conn.execute(
                """UPDATE fix_recipes
                      SET times_confirmed=?,
                          status=?,
                          promotion_status=?,
                          updated_at=?,
                          promoted_at=CASE WHEN ?='active' AND promoted_at IS NULL THEN ? ELSE promoted_at END
                    WHERE recipe_id=?""",
                (new_times, new_status, new_promotion, created_at, new_status, created_at, recipe_id),
            )
        conn.commit()
    except Exception as exc:
        logger.debug('[LIVE_BRAIN_CTX] _record_failure_recipe failed: %s', exc)


def _load_recipe_hint_block(user_message: str, scope_key: str, conn) -> str:
    """Return a RECALLED FIX context block for previously seen failures.

    Matches by (a) error-type keyword in the user message, or (b) substring
    match of problem_pattern against the message. Only surfaces recipes with
    times_confirmed >= 1 and status in {'active','candidate'}.
    """
    if not user_message or _is_chit_chat(user_message):
        return ''
    try:
        lowered = user_message.lower()
        # Detect error-type keywords mentioned by the user
        detected_types: List[str] = []
        for et, keywords in _ERROR_TYPE_KEYWORDS.items():
            if any(kw in lowered for kw in keywords):
                detected_types.append(et)
        # Also try a few meaningful query words as substring matches against problem_pattern
        words = [w for w in re.findall(r'[\w./-]+', lowered) if len(w) > 4][:6]
        if not detected_types and not words:
            return ''
        clauses: List[str] = []
        params: List[Any] = [scope_key]
        if detected_types:
            clauses.append('error_type IN ({})'.format(','.join('?' * len(detected_types))))
            params.extend(detected_types)
        for w in words:
            clauses.append('LOWER(problem_pattern) LIKE ?')
            params.append(f'%{w}%')
        if not clauses:
            return ''
        sql = (
            "SELECT problem_pattern, tool_name, args_template_json, error_type, "
            "       times_confirmed, confidence, status, success_criteria "
            "  FROM fix_recipes "
            " WHERE scope_key IN (?, '') "
            "   AND status IN ('active','candidate') "
            "   AND ({}) "
            " ORDER BY times_confirmed DESC, confidence DESC, updated_at DESC "
            " LIMIT 3"
        ).format(' OR '.join(clauses))
        rows = conn.execute(sql, params).fetchall()
        if not rows:
            return ''
        lines = ['RECALLED FIX:']
        for row in rows:
            pattern = (str(row[0] or '')[:120]).replace('\n', ' ')
            tool = str(row[1] or 'unknown')
            try:
                args_repr = json.loads(row[2] or '{}')
            except Exception:
                args_repr = {}
            args_summary = ''
            if isinstance(args_repr, dict):
                for k in ('path', 'file', 'url', 'command', 'pattern', 'query'):
                    v = args_repr.get(k)
                    if isinstance(v, str) and v:
                        args_summary = f' {k}={v[:60]}'
                        break
            et = str(row[3] or '')
            times = int(row[4] or 0)
            status = str(row[6] or '')
            tail = f' (×{times}'
            if et:
                tail += f', {et}'
            if status == 'active':
                tail += ', confirmed'
            tail += ')'
            lines.append(f'- "{pattern}"{tail}')
            lines.append(f'  last fix: {tool}{args_summary}')
            # Pillar 3: surface success_criteria so the agent knows how to verify
            sc = (str(row[7] or '')).strip()
            if sc:
                lines.append(f'  verify with: {sc[:200]}')
        return '\n'.join(lines)
    except Exception as exc:
        logger.debug('[LIVE_BRAIN_CTX] _load_recipe_hint_block failed: %s', exc)
        return ''


# ---------------------------------------------------------------------------
# Pillar 2: Post-action verification trigger (VERIFICATION REQUIRED)
# ---------------------------------------------------------------------------

# Verifier-tool names whose successful invocation counts as "verified"
_VERIFIER_TOOLS = {'terminal', 'execute_code', 'pytest', 'brain_mark_artifact', 'vision_analyze'}


def _infer_verify_cmd(path: str) -> str:
    """Conservative verifier command for a freshly-edited file.

    Returns '' when we can't propose a high-confidence smoke check —
    silence beats wrong-verifier nagging.
    """
    try:
        p = Path(path).expanduser().resolve()
    except Exception:
        return ''
    if not p.exists() or not p.is_file():
        return ''
    suffix = p.suffix.lower()
    name = p.name
    stem = p.stem
    parent = p.parent

    if suffix == '.py':
        # Look for a paired test file in common locations
        candidates = [
            parent / 'tests' / f'test_{stem}.py',
            parent / f'test_{stem}.py',
            parent / 'tests' / f'{stem}_test.py',
            parent.parent / 'tests' / f'test_{stem}.py',
        ]
        for cand in candidates:
            try:
                if cand.exists() and cand.is_file():
                    return f'pytest -q {cand}'
            except Exception:
                continue
        # No paired test — offline AST parse (no imports → no side effects)
        return (
            f"python -c \"import ast, sys; ast.parse(open('{p}').read()); "
            f"print('ast ok: {name}')\""
        )
    if suffix in ('.sh', '.bash', '.zsh'):
        return f'bash -n {p}'
    if suffix == '.sql':
        return f'sqlite3 :memory: ".read {p}"'
    if suffix == '.json':
        return f"python -c \"import json; json.load(open('{p}')); print('json ok: {name}')\""
    if suffix in ('.yaml', '.yml'):
        return (
            f"python -c \"import yaml; yaml.safe_load(open('{p}')); "
            f"print('yaml ok: {name}')\""
        )
    if suffix in ('.pdf', '.png', '.jpg', '.jpeg', '.webp'):
        return f"vision_analyze {p}"
    return ''  # conservative: no verifier for unknown types


def _enqueue_pending_verification(
    conn,
    scope_key: str,
    session_id: str,
    abs_path: str,
    tool_name: str,
    suggested_command: str,
    created_at: float,
) -> None:
    """Insert (or refresh) a pending_verifications row. Never raises."""
    if not suggested_command:
        return
    verif_id = 'verif:' + hashlib.sha256(
        f'{scope_key}|{session_id}|{abs_path}'.encode('utf-8', 'ignore')
    ).hexdigest()[:24]
    try:
        conn.execute(
            "INSERT OR REPLACE INTO pending_verifications "
            "(verification_id, scope_key, session_id, path, tool_name, "
            " suggested_command, created_at, status, satisfied_at, satisfied_by_tool_call_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', NULL, '')",
            (verif_id, scope_key, session_id, abs_path, tool_name,
             suggested_command, created_at),
        )
        conn.commit()
    except Exception as exc:
        logger.debug('[LIVE_BRAIN_CTX] _enqueue_pending_verification failed: %s', exc)


def _maybe_satisfy_pending_verifications(
    conn,
    tool_name: str,
    args: Any,
    success: bool,
    session_id: str,
    scope_key: str,
    tool_call_id: str,
    created_at: float,
) -> List[str]:
    """If this tool call looks like a verifier touching a pending path, satisfy it.

    Returns the list of paths that were just satisfied (so the caller can also
    clear matching done_without_verify reality_events from Pillar 4).
    """
    if not success or tool_name not in _VERIFIER_TOOLS:
        return []
    args_dict = args if isinstance(args, dict) else {}
    # Materialize the "verification text" — whatever the agent put into the tool call
    parts: List[str] = []
    for k in ('command', 'code', 'path', 'file', 'filename', 'cmd'):
        v = args_dict.get(k)
        if isinstance(v, str) and v:
            parts.append(v)
    blob = ' | '.join(parts) if parts else ''
    if not blob:
        return []
    satisfied: List[str] = []
    try:
        rows = conn.execute(
            "SELECT verification_id, path FROM pending_verifications "
            "WHERE scope_key=? AND session_id=? AND status='pending'",
            (scope_key, session_id),
        ).fetchall()
        for row in rows:
            vid = str(row[0] or '')
            pending_path = str(row[1] or '')
            if not pending_path:
                continue
            # Match by basename presence in the verifier args blob — pytest
            # tests/test_idiot.py contains 'idiot' so this is robust.
            base = Path(pending_path).name
            stem = Path(pending_path).stem
            if not _tool_verifies_pending_path(tool_name, pending_path):
                continue
            if base and (base in blob or pending_path in blob or
                         (len(stem) >= 4 and stem in blob)):
                conn.execute(
                    "UPDATE pending_verifications "
                    "   SET status='satisfied', satisfied_at=?, "
                    "       satisfied_by_tool_call_id=? "
                    " WHERE verification_id=?",
                    (created_at, tool_call_id or '', vid),
                )
                satisfied.append(pending_path)
        if satisfied:
            conn.commit()
    except Exception as exc:
        logger.debug('[LIVE_BRAIN_CTX] _maybe_satisfy_pending_verifications failed: %s', exc)
    return satisfied


def _load_unverified_claim_block(scope_key: str, conn, since_seconds: int = 600) -> str:
    """Build the UNVERIFIED CLAIM context block from recent done_without_verify
    reality_events. This is the "shame loop" surfaced after Pillar 4 detects a
    completion claim without a verifier-tool invocation.
    """
    if not scope_key:
        return ''
    try:
        cutoff = time.time() - since_seconds
        rows = conn.execute(
            "SELECT created_at, payload_json "
            "  FROM reality_events "
            " WHERE scope_key=? AND event_type='done_without_verify' "
            "   AND created_at > ? "
            " ORDER BY created_at DESC LIMIT 2",
            (scope_key, cutoff),
        ).fetchall()
        if not rows:
            return ''
        lines = ['UNVERIFIED CLAIM:']
        for row in rows:
            try:
                payload = json.loads(row[1] or '{}')
            except Exception:
                payload = {}
            age = _format_relative_time(float(row[0] or 0))
            phrase = str(payload.get('phrase') or 'done')
            paths = payload.get('pending_paths') or []
            cmds = payload.get('suggested_commands') or []
            lines.append(f'- You said "{phrase}" {age} but never ran the verifier.')
            for i, p in enumerate(paths[:2]):
                cmd = cmds[i] if i < len(cmds) else ''
                lines.append(f'  Path: {p}')
                if cmd:
                    lines.append(f'  Run: {cmd}')
        lines.append('Run the verifier NOW before responding further. Do not say "done" again until it succeeds.')
        return '\n'.join(lines)
    except Exception as exc:
        logger.debug('[LIVE_BRAIN_CTX] _load_unverified_claim_block failed: %s', exc)
        return ''


def _load_pending_verification_block(scope_key: str, session_id: str, conn) -> str:
    """Build the VERIFICATION REQUIRED context block for unsatisfied edits."""
    if not session_id:
        return ''
    try:
        cutoff = time.time() - 3600
        rows = conn.execute(
            "SELECT path, suggested_command, created_at, tool_name "
            "  FROM pending_verifications "
            " WHERE scope_key=? AND session_id=? AND status='pending' "
            "   AND created_at > ? "
            " ORDER BY created_at DESC LIMIT 3",
            (scope_key, session_id, cutoff),
        ).fetchall()
        if not rows:
            return ''
        lines = ['VERIFICATION REQUIRED:']
        for row in rows:
            path = str(row[0] or '')
            cmd = str(row[1] or '').strip()
            ts = float(row[2] or 0)
            tool = str(row[3] or '')
            age = _format_relative_time(ts)
            lines.append(f'- {path} ({tool} {age})')
            if cmd:
                lines.append(f'  Run: {cmd}')
        lines.append('Do NOT say "done/fixed/works" until the verifier runs AND succeeds.')
        lines.append("When verified, call brain_mark_artifact(path=..., status='verified') to clear.")
        return '\n'.join(lines)
    except Exception as exc:
        logger.debug('[LIVE_BRAIN_CTX] _load_pending_verification_block failed: %s', exc)
        return ''


# ---------------------------------------------------------------------------
# Extracted hook functions
# ---------------------------------------------------------------------------


def _prepare_query_context(user_message: str, sender_id: str, session_id: str, *, platform: str = 'telegram') -> QueryContext:
    """Extract and prepare all query-related metadata."""
    scope_key = _extract_scope_key(user_message, sender_id, session_id, platform=platform)
    now = time.time()
    ttl_cutoff = now - _CONSTRAINT_TTL_DAYS * 86400
    lane_state = _SESSION_LANE_STATE.get(session_id or '', {})
    turn_lane, lane_meta = classify_turn_lane(
        user_message or "",
        chit_chat_patterns=_CHIT_CHAT_PATTERNS,
        platform=platform,
        has_fresh_resume=bool(lane_state.get('resume_pending')),
    )
    semantic_message = str(lane_meta.get('semantic_message') or user_message or '')
    query_lower = semantic_message.lower()
    intent = str(lane_meta.get('intent') or _classify_query_intent(semantic_message, chit_chat_patterns=_CHIT_CHAT_PATTERNS))
    continuation_query = _is_continuation_query(semantic_message or "")
    query_words = [w for w in re.findall(r'[\w./-]+', query_lower) if len(w) > 3]
    active_tags = _active_tags(semantic_message, scope_key)
    _LAST_CONTEXT_METADATA['lane_meta'] = lane_meta
    _LAST_CONTEXT_METADATA['turn_lane'] = turn_lane
    return QueryContext(scope_key, query_lower, intent, turn_lane, query_words, active_tags,
                       continuation_query, now, ttl_cutoff, session_id=session_id)


def _section_score(lines: List[str]) -> int:
    """Cheap score for logs: more surviving lines means the section had stronger support."""
    return len([line for line in lines if (line or '').strip()])


def _log_section_decision(section: str, intent: str, allowed: bool, reason: str, score: int) -> None:
    logger.debug(
        "[LIVE_BRAIN_SECTION] section=%s intent=%s allowed=%s reason=%s score=%s",
        section,
        intent,
        allowed,
        reason,
        score,
    )


def _record_section_decision(section: str, allowed: bool, reason: str, score: int) -> None:
    decisions = _LAST_CONTEXT_METADATA.setdefault('section_decisions', [])
    if isinstance(decisions, list):
        decisions.append({
            'section': section,
            'allowed': bool(allowed),
            'reason': reason,
            'score': int(score),
        })


def _try_append_intent_section(
    parts: List[str],
    *,
    section: str,
    lines: List[str],
    intent: str,
    allowed_sections: set[str],
    section_count: int,
    section_budget: int,
    reason: str,
) -> int:
    """Central section gate so all prompt surfacing goes through one policy path."""
    score = _section_score(lines)
    if not lines:
        _record_section_decision(section, False, f"{reason}:empty", score)
        _log_section_decision(section, intent, False, f"{reason}:empty", score)
        return section_count
    if section not in allowed_sections:
        _record_section_decision(section, False, f"{reason}:intent_blocked", score)
        _log_section_decision(section, intent, False, f"{reason}:intent_blocked", score)
        return section_count
    if section_budget >= 0 and section_count >= section_budget:
        _record_section_decision(section, False, f"{reason}:budget_exhausted", score)
        _log_section_decision(section, intent, False, f"{reason}:budget_exhausted", score)
        return section_count
    _append_section(parts, section, lines)
    _record_section_decision(section, True, reason, score)
    _log_section_decision(section, intent, True, reason, score)
    return section_count + 1



def _infrastructure_context() -> list:
    """Generate INFRASTRUCTURE section: agent must KNOW itself completely without querying docs.
    
    Like proprioception — you know which hand you write with, what color your eyes are,
    what you did today. This function gives the agent that same automatic self-knowledge.
    Only injected when the question touches infrastructure/capability topics (REACTIVE, not constant).
    """
    lines = []
    try:
        hermes_home = os.environ.get('HERMES_HOME', os.path.expanduser('~/.hermes'))
        config_path = os.path.join(hermes_home, 'config.yaml')
        cfg = {}
        if os.path.exists(config_path):
            try:
                import yaml
                with open(config_path, 'r') as f:
                    cfg = yaml.safe_load(f) or {}
            except Exception:
                pass
        
        # ---- MEMORY ----
        mem_cfg = cfg.get('memory', {}) or {}
        provider = mem_cfg.get('provider', 'default')
        if provider == 'live_brain':
            lines.append("memory: live_brain provider — memory tool and brain_* tools share the SAME database. Do NOT treat them as separate systems.")
        else:
            lines.append(f"memory provider: {provider}")
        char_limit = mem_cfg.get('memory_char_limit', 4000)
        user_limit = mem_cfg.get('user_char_limit', 2000)
        limit_note = "injection limit for prompt context, NOT a storage limit" if provider == 'live_brain' else "character limit"
        lines.append(f"memory_char_limit: {char_limit} — {limit_note}")
        lines.append(f"user_char_limit: {user_limit} — {limit_note}")
        
        # ---- MODEL & PROVIDER ----
        model_cfg = cfg.get('model', {}) or {}
        if model_cfg:
            model_name = model_cfg.get('name', model_cfg.get('model', 'unknown'))
            model_provider = model_cfg.get('provider', 'unknown')
            lines.append(f"current model: {model_name} via {model_provider}")
        
        providers_cfg = cfg.get('providers', {}) or {}
        provider_names = [p for p in providers_cfg if isinstance(providers_cfg[p], dict)]
        custom_providers = cfg.get('custom_providers', []) or []
        custom_names = [cp.get('name', '?') for cp in custom_providers if isinstance(cp, dict)]
        all_providers = sorted(set(provider_names + custom_names))
        if all_providers:
            lines.append(f"providers: {', '.join(all_providers)}")
        
        # fallback providers
        fallbacks = cfg.get('fallback_providers', []) or []
        if fallbacks:
            lines.append(f"fallback providers: {', '.join(str(f) for f in fallbacks)}")
        
        # smart model routing
        smr = cfg.get('smart_model_routing', {}) or {}
        if smr.get('enabled'):
            lines.append(f"smart_model_routing: enabled")
        
        # ---- CAPABILITIES (what the agent CAN do) ----
        capabilities = []
        
        # Tools from config
        toolsets = cfg.get('toolsets', []) or []
        if toolsets:
            capabilities.append(f"toolsets: {', '.join(str(t) for t in toolsets)}")
        
        # Key boolean capabilities
        cap_keys = {
            'browser': 'browser',
            'code_execution.sandbox': 'code_execution',
            'vision': 'vision',
            'tts': 'tts',
            'stt': 'stt',
            'image_model': 'image_generation',
        }
        for key, label in cap_keys.items():
            val = cfg
            for part in key.split('.'):
                val = (val or {}).get(part) if isinstance(val, dict) else None
            if val:
                capabilities.append(f"can {label}: {val}")
        
        # Jail/dangerous capabilities
        terminal_cfg = cfg.get('terminal', {}) or {}
        if terminal_cfg.get('enabled', True):
            capabilities.append("can terminal: yes")
        delegation_cfg = cfg.get('delegation', {}) or {}
        if delegation_cfg.get('enabled', True):
            max_children = delegation_cfg.get('max_concurrent_children', 3)
            capabilities.append(f"can delegate_task: max {max_children} subagents")
        
        for cap in capabilities:
            lines.append(cap)
        
        # ---- PLUGINS ----
        # Proprioception: know what each plugin DOES, not just that it exists
        # Like knowing what your hands can do, not just that you have hands
        PLUGIN_DESCRIPTIONS = {
            'live_brain': 'cognitive kernel — persistent beliefs, facts, episodes, rules DB + causal learning + epistemic reasoning + self-evolution proposals',
            'live_brain_ctx': 'context engine — injects relevant knowledge into every prompt (facts, rules, beliefs, proven fixes, infrastructure, authored content)',
            'nucleus': 'continuous substrate — 30-module Python loop with cognitive bus, prediction feedback, SelfModel, Pargod causal graph, instruction queue',
        }
        plugins_dir = os.path.join(hermes_home, 'plugins')
        if os.path.isdir(plugins_dir):
            plugin_names = sorted([d for d in os.listdir(plugins_dir) 
                                 if os.path.isdir(os.path.join(plugins_dir, d)) and not d.startswith('.')])
            for pname in plugin_names:
                desc = PLUGIN_DESCRIPTIONS.get(pname, '')
                if desc:
                    lines.append(f"plugin {pname}: {desc}")
                else:
                    # Try reading __init__.py docstring for description
                    init_path = os.path.join(plugins_dir, pname, '__init__.py')
                    try:
                        if os.path.exists(init_path):
                            with open(init_path, 'r') as ipf:
                                first_line = ipf.readline().strip()
                                if first_line.startswith('"""') or first_line.startswith("'''"):
                                    # Multi-line docstring — read until closing
                                    doc_lines = [first_line[3:]]
                                    for _ in range(5):
                                        dl = ipf.readline().strip()
                                        if dl.endswith('"""') or dl.endswith("'''"):
                                            break
                                        doc_lines.append(dl)
                                    desc = ' '.join(doc_lines).strip()[:120]
                                    if desc:
                                        lines.append(f"plugin {pname}: {desc}")
                                        continue
                    except Exception:
                        pass
                    lines.append(f"plugin {pname}: (unknown description)")
        
        # ---- LIVE BRAIN ----
        db_path = os.path.join(hermes_home, 'live_brain', 'live_brain.db')
        if not os.path.exists(db_path):
            db_path = os.path.join(hermes_home, 'live_brain.db')
        if os.path.exists(db_path):
            try:
                import sqlite3
                conn = sqlite3.connect(db_path, timeout=5.0)
                conn.execute("PRAGMA query_only = ON")
                c = conn.cursor()
                db_stats = []
                for table in ['beliefs', 'facts', 'episodes', 'rules']:
                    try:
                        c.execute(f"SELECT COUNT(*) FROM {table}")
                        count = c.fetchone()[0]
                        if count > 0:
                            db_stats.append(f"{table}={count}")
                    except Exception:
                        pass
                conn.close()
                if db_stats:
                    lines.append(f"live_brain db: {', '.join(db_stats)}")
            except Exception:
                pass
        
        # ---- NUCLEUS ----
        nucleus_path = os.path.join(hermes_home, 'nucleus')
        if os.path.isdir(nucleus_path):
            # Count modules and read self_model registry for architecture summary
            py_files = [f for f in os.listdir(nucleus_path) if f.endswith('.py') and not f.startswith('__')]
            n_modules = len(py_files)
            
            # Try to load architecture summary from self_model.py MODULE_REGISTRY
            arch_summary = []
            try:
                self_model_path = os.path.join(nucleus_path, 'self_model.py')
                if os.path.exists(self_model_path):
                    with open(self_model_path, 'r') as smf:
                        sm_content = smf.read()
                    # Extract MODULE_REGISTRY top-level keys (module filenames like "nucleus_engine.py")
                    import re as _re
                    registry_matches = _re.findall(r'"\.py":\s*\{', sm_content)
                    if not registry_matches:
                        # Alternative: extract quoted keys before ": {" pattern
                        registry_matches = _re.findall(r'"([a-z_]+\.py)":\s*\{', sm_content)
                    # Get role descriptions for each module
                    module_roles = {}
                    for key in registry_matches[:8]:
                        # Find role for this module
                        role_match = _re.search(rf'"{_re.escape(key)}"\s*:\s*\{{[^}}]*"role"\s*:\s*"([^"]*)"', sm_content)
                        if role_match:
                            module_roles[key] = role_match.group(1)
                        else:
                            module_roles[key] = key.replace('.py', '').replace('_', ' ')
                    if module_roles:
                        arch_summary = [f"{k.replace('.py','')}({v.split('.')[0].split(',')[0]})" for k, v in module_roles.items()]
            except Exception:
                pass
            
            # Try to count total LOC
            total_loc = 0
            try:
                for pf in py_files:
                    with open(os.path.join(nucleus_path, pf), 'r') as pfh:
                        total_loc += sum(1 for _ in pfh)
            except Exception:
                total_loc = 0
            
            nucleus_line = f"nucleus: active — {n_modules} modules, {total_loc} LOC"
            if arch_summary:
                nucleus_line += f" | key modules: {', '.join(arch_summary)}"
            lines.append(nucleus_line)
            
            # Core architecture knowledge — proprioception: know thyself
            # Include key methods for surgical precision — know WHERE things are
            try:
                methods_info = []
                for key in registry_matches[:8]:
                    # Find key_methods for this module
                    methods_match = _re.search(
                        rf'"{_re.escape(key)}"\s*:\s*\{{[^}}]*"key_methods"\s*:\s*\[([^\]]+)\]',
                        sm_content
                    )
                    if methods_match:
                        raw_methods = methods_match.group(1)
                        method_list = _re.findall(r'"([^"]+)"', raw_methods)
                        module_short = key.replace('.py', '')
                        methods_info.append(f"{module_short}: {', '.join(method_list[:5])}")
                if methods_info:
                    lines.append("nucleus key methods: " + " | ".join(methods_info))
            except Exception:
                pass
            lines.append("nucleus architecture: continuous Python loop (nucleus_engine.py) with cognitive bus, "
                         "prediction→outcome→learning_signal feedback loop, Pargod causal graph DB, "
                         "SelfModel persistent index, metacognition layer, GoalEngine, "
                         "instruction queue (JSONL, not DB), no_agent:true queue gate cron")
            lines.append("nucleus feedback loop: predict → observe outcome → compute learning_signal → update rules/trust → new predict")
        
        # ---- CRONJOBS ----
        cron_dir = os.path.join(hermes_home, 'cron')
        jobs_file = os.path.join(cron_dir, 'jobs.json')
        if os.path.exists(jobs_file):
            try:
                with open(jobs_file, 'r') as f:
                    jobs_data = json.load(f)
                jobs_list = jobs_data.get('jobs', []) if isinstance(jobs_data, dict) else jobs_data
                active_jobs = [j for j in jobs_list if isinstance(j, dict) and j.get('enabled', True)]
                if active_jobs:
                    job_names = [j.get('name', j.get('job_id', '?')) for j in active_jobs]
                    lines.append(f"cronjobs ({len(active_jobs)} active): {', '.join(job_names)}")
            except Exception:
                pass
        
        # ---- SKILLS ----
        skills_dir = os.path.join(hermes_home, 'skills')
        if os.path.isdir(skills_dir):
            skill_cats = sorted([d for d in os.listdir(skills_dir) 
                                if os.path.isdir(os.path.join(skills_dir, d)) and not d.startswith('.')])
            if skill_cats:
                lines.append(f"skill categories ({len(skill_cats)}): {', '.join(skill_cats)}")
        
        # ---- GATEWAY ----
        gw = cfg.get('gateway', {}) or {}
        platform = gw.get('platform', 'unknown')
        if platform != 'unknown':
            lines.append(f"gateway platform: {platform}")
        
        # ---- CONTEXT LIMITS ----
        ctx_cfg = cfg.get('context', {}) or {}
        if ctx_cfg:
            max_tokens = ctx_cfg.get('max_tokens')
            if max_tokens:
                lines.append(f"context max_tokens: {max_tokens}")
            compression = ctx_cfg.get('compression')
            if compression:
                lines.append(f"context compression: {compression}")
                
    except Exception:
        pass
    
    return lines


def _authored_content_context(session_id: str = '') -> list:
    """Generate AUTHORED CONTENT section: files the agent wrote/modified this session.
    
    Like proprioception — you know what you wrote without re-reading it.
    Scans recent session files for write_file and patch tool calls to extract paths.
    Only surfaces paths + action type, never full file content (too expensive for context).
    """
    lines = []
    sessions_dir = Path(os.path.join(_hermes_home(), 'sessions'))
    if not sessions_dir.is_dir():
        return lines
    
    try:
        session_files = sorted(sessions_dir.glob('*.jsonl'), key=lambda p: p.stat().st_mtime, reverse=True)
        authored = {}  # path -> action type
        
        for sf in session_files[:5]:  # Last 5 sessions (~24h)
            try:
                with open(sf) as f:
                    for line in f:
                        if not line.strip():
                            continue
                        try:
                            msg = json.loads(line)
                        except Exception:
                            continue
                        
                        # Extract paths from tool_calls in assistant messages
                        tool_calls = msg.get('tool_calls', [])
                        if isinstance(tool_calls, list):
                            for tc in tool_calls:
                                if not isinstance(tc, dict):
                                    continue
                                fn = tc.get('function', {})
                                fn_name = fn.get('name', '')
                                fn_args = fn.get('arguments', '')
                                if isinstance(fn_args, str):
                                    try:
                                        fn_args = json.loads(fn_args)
                                    except Exception:
                                        fn_args = {}
                                if fn_name in ('write_file', 'patch') and isinstance(fn_args, dict):
                                    path = fn_args.get('path', '')
                                    if path and len(path) > 5:
                                        path = path.replace('~', os.path.expanduser('~'))
                                        if path not in authored:
                                            action = 'wrote' if fn_name == 'write_file' else 'patched'
                                            authored[path] = action
                        
                        # Also extract from write_file tool results
                        if msg.get('role') == 'tool' and msg.get('name') == 'write_file':
                            content = str(msg.get('content', ''))
                            paths_found = re.findall(r'(?:/home/[^\s"]+\.(?:py|json|yaml|yml|sql|sh|md|txt|cfg|toml))', content)
                            for p in paths_found:
                                if p not in authored:
                                    authored[p] = 'wrote'
            except Exception:
                continue
        
        if authored:
            for path, action in sorted(authored.items()):
                short_path = path.replace(os.path.expanduser('~'), '~')
                lines.append(f"{action}: {short_path}")
    except Exception:
        pass
    
    return lines


def _active_objectives_context() -> list:
    """Generate ACTIVE OBJECTIVES section from Live Brain open_loops.
    
    Semantic continuity: after context compaction, the agent must know
    what it was working on. This reads active open_loops and injects them
    so the agent can pick up where it left off — like waking up and remembering
    what you were doing before you fell asleep.
    """
    lines = []
    try:
        db_path = _db_path()
        if not os.path.exists(db_path):
            return lines
        
        conn = sqlite3.connect(db_path, timeout=3.0)
        conn.execute("PRAGMA query_only = ON")
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        
        # Get top 5 active open_loops by priority
        c.execute("""
            SELECT title, priority, next_action, status
            FROM open_loops 
            WHERE status = 'active'
            ORDER BY priority DESC, updated_at DESC
            LIMIT 5
        """)
        rows = c.fetchall()
        conn.close()
        
        for row in rows:
            title = (row['title'] or '').strip()
            next_action = (row['next_action'] or '').strip()
            priority = row['priority']
            if title:
                entry = f"[p={priority:.2f}] {title}"
                if next_action and next_action not in ('.', '-'):
                    entry += f" → {next_action}"
                lines.append(entry)
    except Exception:
        pass
    
    return lines


def _load_live_brain_context(user_message: str, session_id: str, sender_id: str) -> str:
    db_path = _db_path()
    if not Path(db_path).exists():
        return ""

    approval_query = _is_approval_query(user_message or "")
    if _is_review_only_query(user_message or '') and not approval_query:
        return ""

    qctx = _prepare_query_context(user_message, sender_id, session_id)
    _LAST_CONTEXT_METADATA['recipe_ids'] = []
    _LAST_CONTEXT_METADATA['section_decisions'] = []
    _LAST_CONTEXT_METADATA['routing_summary'] = (
        {'intent': qctx.intent, 'tiers_checked': [], 'chosen_tier': 'latest_session_reply', 'matches': {}}
        if _is_low_signal_followup_query(user_message)
        else _state_first_routing_summary(qctx.scope_key, qctx, user_message)
    )
    if _capability_e2e_query(user_message):
        fast = _load_capability_e2e_context(user_message, qctx.scope_key)
        if fast:
            return fast
    if build_context_from_objects is not None and MemoryCompiler is not None:
        compiled_conn = None
        try:
            compiled_conn = _get_connection()
            compiled_conn.row_factory = sqlite3.Row
            if ensure_memory_v2_schema is not None:
                ensure_memory_v2_schema(compiled_conn)
            compiled_context, compiled_trace = build_context_from_objects(
                compiled_conn,
                scope_key=qctx.scope_key,
                session_id=session_id,
                lane='approval_flow' if qctx.intent == 'approval_flow' else qctx.turn_lane,
                user_message=user_message or '',
                now=qctx.now,
            )
            compiled_conn.commit()
            _LAST_CONTEXT_METADATA['compiled_trace'] = compiled_trace
            _LAST_CONTEXT_METADATA['section_decisions'] = list(compiled_trace.get('section_decisions') or [])
            if compiled_context:
                _LAST_CONTEXT_METADATA['routing_summary'] = {
                    'intent': qctx.intent,
                    'tiers_checked': ['memory_objects'],
                    'chosen_tier': 'compiled_memory_objects',
                    'matches': {
                        'selected_object_ids': [
                            item.get('object_id') for item in compiled_trace.get('selected', [])
                        ],
                    },
                }
                return compiled_context
            if qctx.turn_lane == 'chit_chat':
                return ''
        except Exception as exc:
            logger.debug('[LIVE_BRAIN_CTX] compiled-object context failed, falling back: %s', exc)
        finally:
            if compiled_conn is not None:
                try:
                    compiled_conn.close()
                except Exception:
                    pass
    allowed_sections = allowed_sections_for_intent(qctx.intent)
    allowed_sections = allowed_sections.intersection(_LANE_SECTION_ALLOWLIST.get(qctx.turn_lane, allowed_sections))
    if qctx.intent == 'continuity_recap' and _is_low_signal_followup_query(user_message):
        allowed_sections = {'LATEST RECAP', 'CONTINUITY MEMORY', 'KNOWN FACTS'}
    if _NO_WIDEN_RE.search(user_message or ''):
        allowed_sections = allowed_sections.difference({'CONTINUITY MEMORY', 'ACTIVE OBJECTIVES', 'INFRASTRUCTURE', 'RECENT EPISODES'})
    chosen_tier = str((_LAST_CONTEXT_METADATA.get('routing_summary') or {}).get('chosen_tier') or '')
    tier_allowlist = _ROUTING_SECTION_ALLOWLIST.get(chosen_tier)
    if tier_allowlist:
        allowed_sections = allowed_sections.intersection(tier_allowlist)
    section_budget = section_budget_for_intent(qctx.intent)

    conn = _get_connection()
    conn.row_factory = sqlite3.Row
    data = _fetch_all_data_sources(conn, qctx, user_message, approval_query)

    parts: List[str] = []
    section_count = 0

    # Approval banners are intent-gated so normal chat does not become an approval inbox.
    if data.should_surface_approval:
        section_count = _try_append_intent_section(
            parts,
            section="PENDING APPROVAL",
            lines=_approval_context_lines(data.pending_approval_rows, approval_query=approval_query),
            intent=qctx.intent,
            allowed_sections=allowed_sections,
            section_count=section_count,
            section_budget=section_budget,
            reason=f"approval_surface:{data.approval_surface_reason or 'explicit_or_relevant'}",
        )
    elif data.pending_approval_rows:
        section_count = _try_append_intent_section(
            parts,
            section="APPROVAL ROUTING",
            lines=_suppressed_approval_reminder_lines(),
            intent=qctx.intent,
            allowed_sections=allowed_sections,
            section_count=section_count,
            section_budget=section_budget,
            reason="approval_suppressed_repeat",
        )

    # Binding rules survive across intents because they are deterministic safety constraints.
    if data.binding_rules:
        constraints = _format_binding_constraints(data.binding_rules, qctx, user_message)
        if constraints:
            section_count = _try_append_intent_section(
                parts,
                section="MUST FOLLOW",
                lines=constraints,
                intent=qctx.intent,
                allowed_sections=allowed_sections,
                section_count=section_count,
                section_budget=section_budget,
                reason="binding_constraints",
            )

    # Verified artifacts are valuable only when the intent is file/repo or execution-oriented.
    try:
        if data.artifact_lines:
            section_count = _try_append_intent_section(
                parts,
                section="VERIFIED ARTIFACTS",
                lines=data.artifact_lines,
                intent=qctx.intent,
                allowed_sections=allowed_sections,
                section_count=section_count,
                section_budget=section_budget,
                reason="artifact_registry_match",
            )
    except Exception:
        pass

    # Proven fixes stay out of recap/chat unless the intent is explicitly operational.
    recipe_hints, selected_recipe_ids = _format_fix_recipes(data.recipe_rows, data.causal_rows, qctx)
    if recipe_hints:
        _LAST_CONTEXT_METADATA['recipe_ids'] = selected_recipe_ids[:_SECTION_LIMITS.get('PROVEN FIX', 3)]
        section_count = _try_append_intent_section(
            parts,
            section="PROVEN FIX",
            lines=recipe_hints,
            intent=qctx.intent,
            allowed_sections=allowed_sections,
            section_count=section_count,
            section_budget=section_budget,
            reason="verified_recipe_or_causal_match",
        )

    # Facts are allowed broadly, but still pass through the central budget gate.
    if data.knowledge_rows:
        principles = [_truncate_fact(r[0]) for r in data.knowledge_rows if r[0] and not _SECRET_RE.search(r[0]) and not _is_noisy_memory(r[0]) and not _domain_conflicts(qctx.query_lower, r[0]) and _has_overlap(r, qctx.query_words, ['principle_text'])]
        if principles:
            section_count = _try_append_intent_section(
                parts,
                section="KNOWN FACTS",
                lines=principles,
                intent=qctx.intent,
                allowed_sections=allowed_sections,
                section_count=section_count,
                section_budget=section_budget,
                reason="knowledge_overlap",
            )

    routing_summary = _LAST_CONTEXT_METADATA.get('routing_summary') or {}
    truth_lines = routing_summary.get('matches', {}).get('incident_truth') or []
    marker_tokens = [token.lower() for token in _RUN_MARKER_RE.findall(qctx.query_lower)]
    if truth_lines and qctx.turn_lane == 'deep_execution' and not _is_low_signal_followup_query(user_message):
        section_count = _try_append_intent_section(
            parts,
            section="KNOWN FACTS",
            lines=[f"Compiled incident truth: {line}" for line in truth_lines],
            intent=qctx.intent,
            allowed_sections=allowed_sections,
            section_count=section_count,
            section_budget=section_budget,
            reason="compiled_incident_truth",
        )

    # Active task is no longer a default; only execution/recap intents get it.
    if data.work_item_row and qctx.intent in {'task_execution', 'continuity_recap'}:
        lines = [f"Task: {data.work_item_row['title']}"]
        if data.work_item_row['status']:
            lines.append(f"Status: {data.work_item_row['status']}")
        root_cause = (data.work_item_row['root_cause'] or '').strip()
        if root_cause and root_cause not in {'.', '-', 'unknown'} and len(root_cause) > 3 and not _marker_conflicts(qctx.query_lower, root_cause.lower()):
            lines.append(f"Root cause: {_truncate_fact(root_cause)}")
        section_count = _try_append_intent_section(
            parts,
            section="ACTIVE TASK",
            lines=["; ".join(lines)],
            intent=qctx.intent,
            allowed_sections=allowed_sections,
            section_count=section_count,
            section_budget=section_budget,
            reason="active_work_item",
        )

    # Active objectives are continuity scaffolding, not default chat context.
    objective_lines = _active_objectives_context() if qctx.turn_lane == 'deep_execution' else []
    if objective_lines:
        section_count = _try_append_intent_section(
            parts,
            section="ACTIVE OBJECTIVES",
            lines=objective_lines,
            intent=qctx.intent,
            allowed_sections=allowed_sections,
            section_count=section_count,
            section_budget=section_budget,
            reason="objective_continuity",
        )

    if qctx.continuation_query and data.continuity_work_rows:
        continuity_lines = []
        for row in data.continuity_work_rows:
            title = (row['title'] or '').strip()
            if not title or _is_continuation_query(title) or _is_question_like_memory(title) and not any(alias in title.lower() for alias in _MUSIC_MEMORY_ALIASES):
                continue
            continuity_lines.append(f"User previously said: {_truncate_fact(title)}")
        if continuity_lines:
            section_count = _try_append_intent_section(
                parts,
                section="CONTINUITY MEMORY",
                lines=continuity_lines,
                intent=qctx.intent,
                allowed_sections=allowed_sections,
                section_count=section_count,
                section_budget=section_budget,
                reason="continuity_match",
            )

    if _is_low_signal_followup_query(user_message):
        recap_lines = _latest_session_reply_lines(session_id, limit=1)
        if recap_lines:
            section_count = _try_append_intent_section(
                parts,
                section="LATEST RECAP",
                lines=recap_lines,
                intent=qctx.intent,
                allowed_sections=allowed_sections,
                section_count=section_count,
                section_budget=section_budget,
                reason="latest_session_reply",
            )

    # Recent episodes are helpful for recap/execution but too noisy for casual chat.
    ep_lines = _format_episodes(data.episode_rows, qctx, user_message)
    if ep_lines:
        section_count = _try_append_intent_section(
            parts,
            section="RECENT EPISODES",
            lines=ep_lines,
            intent=qctx.intent,
            allowed_sections=allowed_sections,
            section_count=section_count,
            section_budget=section_budget,
            reason="episode_overlap",
        )

    # Atomic facts stay useful for repo lookup and execution, but still obey intent budget.
    if data.fact_rows:
        facts = [_truncate_fact(r['fact_text']) for r in data.fact_rows if r['fact_text'] and not _SECRET_RE.search(r['fact_text']) and not _is_noisy_memory(r['fact_text']) and not _is_question_like_memory(r['fact_text']) and not _domain_conflicts(qctx.query_lower, r['fact_text']) and _visible_fact_matches(r['fact_text'], qctx.query_words) and _matches(r, qctx.active_tags, qctx.scope_key) and _has_overlap(r, qctx.query_words, ['fact_text'])]
        if facts:
            section_count = _try_append_intent_section(
                parts,
                section="KNOWN FACTS",
                lines=facts,
                intent=qctx.intent,
                allowed_sections=allowed_sections,
                section_count=section_count,
                section_budget=section_budget,
                reason="fact_overlap",
            )

    # Open bugs are reserved for operational intents so recap/chat stays factual.
    open_beliefs = [r['claim_text'] for r in data.belief_rows if r['status'] == 'open' and len(r['claim_text']) > 20 and not _is_noisy_memory(r['claim_text']) and _matches(r, qctx.active_tags, qctx.scope_key) and _has_overlap(r, qctx.query_words, ['claim_text'])]
    if open_beliefs:
        section_count = _try_append_intent_section(
            parts,
            section="OPEN BUG",
            lines=[_truncate_fact(b) for b in open_beliefs[:2]],
            intent=qctx.intent,
            allowed_sections=allowed_sections,
            section_count=section_count,
            section_budget=section_budget,
            reason="open_belief_overlap",
        )

    # Validated causes get merged into facts to avoid yet another section family.
    validated_causes = [r['claim_text'] for r in data.belief_rows if r['status'] == 'validated' and r['belief_kind'] == 'validated_cause' and not _is_noisy_memory(r['claim_text']) and _matches(r, qctx.active_tags, qctx.scope_key) and _has_overlap(r, qctx.query_words, ['claim_text'])]
    if validated_causes:
        section_count = _try_append_intent_section(
            parts,
            section="KNOWN FACTS",
            lines=[f"Cause: {_truncate_fact(c)}" for c in validated_causes[:2]],
            intent=qctx.intent,
            allowed_sections=allowed_sections,
            section_count=section_count,
            section_budget=section_budget,
            reason="validated_cause_overlap",
        )

    if marker_tokens:
        marker_ruled_out = [
            r['claim_text'] for r in data.belief_rows
            if r['belief_kind'] == 'ruled_out_cause'
            and any(token in str(r['claim_text'] or '').lower() for token in marker_tokens)
        ]
        if marker_ruled_out:
            section_count = _try_append_intent_section(
                parts,
                section="OPEN BUG",
                lines=[f"Ruled out cause: {_truncate_fact(text)}" for text in marker_ruled_out[:2]],
                intent=qctx.intent,
                allowed_sections=allowed_sections,
                section_count=section_count,
                section_budget=section_budget,
                reason="run_scoped_ruled_out_cause",
            )
        marker_validated = [
            r['claim_text'] for r in data.belief_rows
            if r['belief_kind'] == 'validated_cause'
            and any(token in str(r['claim_text'] or '').lower() for token in marker_tokens)
        ]
        if marker_validated:
            section_count = _try_append_intent_section(
                parts,
                section="KNOWN FACTS",
                lines=[f"Run-scoped cause: {_truncate_fact(text)}" for text in marker_validated[:2]],
                intent=qctx.intent,
                allowed_sections=allowed_sections,
                section_count=section_count,
                section_budget=section_budget,
                reason="run_scoped_validated_cause",
            )
        marker_next = [
            r['fact_text'] for r in data.fact_rows
            if str(r['fact_type'] or '') == 'next_action_memory'
            and any(token in str(r['fact_text'] or '').lower() for token in marker_tokens)
        ]
        if marker_next:
            section_count = _try_append_intent_section(
                parts,
                section="NEXT REQUIRED ACTION",
                lines=[_truncate_fact(text) for text in marker_next[:2]],
                intent=qctx.intent,
                allowed_sections=allowed_sections,
                section_count=section_count,
                section_budget=section_budget,
                reason="run_scoped_next_action",
            )

    # Next action is useful only when the user is in task mode, not when they ask for recap or files.
    if data.work_item_row and data.work_item_row['next_step']:
        next_step = data.work_item_row['next_step']
        generic_next = ['diagnose the problem using exact entities', 'before guessing', 'answer the user']
        lowered_next = next_step.lower()
        if next_step and 'continue' not in lowered_next and 'answer' not in lowered_next and not any(token in lowered_next for token in generic_next):
            section_count = _try_append_intent_section(
                parts,
                section="NEXT REQUIRED ACTION",
                lines=[next_step[:200]],
                intent=qctx.intent,
                allowed_sections=allowed_sections,
                section_count=section_count,
                section_budget=section_budget,
                reason="work_item_next_step",
            )

    # Recap block is isolated so "šta si radio danas" does not drag in full execution context.
    if qctx.intent == 'continuity_recap' and data.recap_row and not any(_is_noisy_memory(data.recap_row[field] or '') for field in ['task', 'root_cause', 'current_status', 'next_step']):
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
            section_count = _try_append_intent_section(
                parts,
                section="LATEST RECAP",
                lines=recap_lines,
                intent=qctx.intent,
                allowed_sections=allowed_sections,
                section_count=section_count,
                section_budget=section_budget,
                reason="canonical_recap",
            )

    # Diagnostic rule is tiny but still gated centrally so repo/recap/chat prompts stay clean.
    if _is_diagnostic_query(user_message or ""):
        section_count = _try_append_intent_section(
            parts,
            section="DIAGNOSTIC RULE",
            lines=["Do not present hypotheses as confirmed causes. Give one concrete next debugging step if evidence is insufficient."],
            intent=qctx.intent,
            allowed_sections=allowed_sections,
            section_count=section_count,
            section_budget=section_budget,
            reason="diagnostic_guardrail",
        )

    # Infrastructure is useful for self-debugging, not for normal conversation.
    infra_trigger_words = ('memory', 'brain', 'plugin', 'config', 'provider', 'infrastructure',
                          'limit', 'db', 'database', 'tool', 'capability', 'backend',
                          'nucleus', 'pargod', 'system', 'setup', 'arhitekt', 'sistem',
                          'memor', 'plugin', 'korišti', 'koristi', 'zašto', 'zasto')
    query_lower_infra = (user_message or '').lower()
    if any(w in query_lower_infra for w in infra_trigger_words):
        infra_lines = _infrastructure_context()
        if infra_lines:
            section_count = _try_append_intent_section(
                parts,
                section="INFRASTRUCTURE",
                lines=infra_lines,
                intent=qctx.intent,
                allowed_sections=allowed_sections,
                section_count=section_count,
                section_budget=section_budget,
                reason="infrastructure_trigger",
            )

    # Authored-content recall should appear only when the user asks about prior edits/work.
    authored_trigger_words = ('šta si', 'what did you', 'uradio', 'napravio', 'promenio', 'changed', 
                             'wrote', 'napisao', 'napisala', 'kod', 'code', 'fajl', 'file',
                             'šta si radio', 'šta si radio', 'šta si uradio', 'what have you',
                             'skript', 'script', 'patch', 'hook', 'modifikovao', 'edited')
    query_lower_auth = (user_message or '').lower()
    if any(w in query_lower_auth for w in authored_trigger_words):
        authored_lines = _authored_content_context(session_id=session_id)
        if authored_lines:
            section_count = _try_append_intent_section(
                parts,
                section="AUTHORED THIS SESSION",
                lines=authored_lines,
                intent=qctx.intent,
                allowed_sections=allowed_sections,
                section_count=section_count,
                section_budget=section_budget,
                reason="authored_content_trigger",
            )

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
    intent = _classify_query_intent(user_message or '', chit_chat_patterns=_CHIT_CHAT_PATTERNS)
    context = _load_live_brain_context(user_message, session_id, sender_id)
    debug: Dict[str, Any] = {
        'scope_key': scope_key,
        'intent': intent,
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
        try:
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
        except sqlite3.OperationalError:
            # Debug mode should stay readable even when the raw query tokens are not valid FTS syntax.
            debug['rejections']['recipes_tool'] += 0
    return debug


def _latest_tool_context(conn: sqlite3.Connection, session_id: str, created_at: float) -> tuple[str, str]:
    if session_id:
        row = conn.execute(
            "SELECT scope_key, query_text FROM context_impressions WHERE session_id=? AND created_at >= ? ORDER BY created_at DESC LIMIT 1",
            (session_id, created_at - 1800),
        ).fetchone()
        if row:
            return str(row[0] or ''), str(row[1] or '')
        # Fallback: same session, but the user has been idle longer than the
        # 30-min window. Without this, post-tool hooks resolve scope_key to the
        # session_id itself, while the send_message gate (which has no time
        # cutoff) still resolves to the platform scope — leaving
        # pending_verifications rows un-satisfiable across long idle periods.
        row = conn.execute(
            "SELECT scope_key, query_text FROM context_impressions WHERE session_id=? ORDER BY created_at DESC LIMIT 1",
            (session_id,),
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


@functools.lru_cache(maxsize=1)
def _load_live_brain_store_class():
    try:
        from live_brain.store import LiveBrainStore
        return LiveBrainStore
    except Exception:
        pass
    try:
        import importlib.util as _importlib_util
        import sys as _sys
        import types as _types
        package_name = '_live_brain_ctx_store_pkg'
        live_brain_dir = Path(__file__).resolve().parent.parent.parent / 'live_brain'
        if not live_brain_dir.exists():
            live_brain_dir = Path(__file__).resolve().parent.parent.parent.parent / 'live_brain'
        if not live_brain_dir.exists():
            return None
        if package_name not in _sys.modules:
            package = _types.ModuleType(package_name)
            package.__path__ = [str(live_brain_dir)]
            _sys.modules[package_name] = package
        for module_name in ['utils', 'audit', 'schema_manager', 'backup_manager', 'maintenance_manager', 'reality', 'epistemic', 'incident_truth', 'turn_trace', 'store']:
            full_name = f'{package_name}.{module_name}'
            if full_name in _sys.modules:
                continue
            spec = _importlib_util.spec_from_file_location(full_name, live_brain_dir / f'{module_name}.py')
            if spec is None or spec.loader is None:
                return None
            module = _importlib_util.module_from_spec(spec)
            module.__package__ = package_name
            _sys.modules[full_name] = module
            spec.loader.exec_module(module)
        return _sys.modules[f'{package_name}.store'].LiveBrainStore
    except Exception:
        return None


def _state_first_routing_summary(scope_key: str, qctx, user_message: str) -> Dict[str, Any]:
    summary: Dict[str, Any] = {
        'intent': qctx.intent,
        'tiers_checked': [],
        'chosen_tier': '',
        'matches': {},
    }
    if qctx.intent not in _ROUTING_INTENTS:
        summary['chosen_tier'] = 'non_operational'
        return summary
    if not Path(_db_path()).exists():
        summary['chosen_tier'] = 'missing_db'
        return summary
    conn = None
    try:
        conn = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        truth_rows = conn.execute(
            "SELECT title, diagnosis_summary, recommended_next_action FROM incident_truths WHERE scope_key=? AND status IN ('active','verified') ORDER BY updated_at DESC LIMIT 12",
            (scope_key,),
        ).fetchall()
        query_tokens = [token for token in re.findall(r'[\w./-]+', (user_message or '').lower()) if len(token) > 3]
        truth_matches = []
        for row in truth_rows:
            blob = ' '.join(str(row[key] or '') for key in ('title', 'diagnosis_summary', 'recommended_next_action')).lower()
            if query_tokens and not any(token in blob for token in query_tokens):
                continue
            truth_matches.append(
                f"{str(row['title'] or '')[:90]} | diagnosis={str(row['diagnosis_summary'] or '')[:140]} | next={str(row['recommended_next_action'] or '')[:120]}"
            )
            if len(truth_matches) >= 2:
                break

        run_markers = [marker for marker in _RUN_MARKER_RE.findall(user_message or '')]
        active_state = [
            dict(row) for row in conn.execute(
                "SELECT state_key, value_json, confidence, updated_at FROM reality_state WHERE scope_key=? AND state_key IN ('current_objective','active_project','safe_next_action') ORDER BY updated_at DESC LIMIT 10",
                (scope_key,),
            ).fetchall()
        ]
        active_loops = [
            dict(row) for row in conn.execute(
                "SELECT title, status, priority, next_action, blockers_json, updated_at FROM open_loops WHERE scope_key=? AND status IN ('active','blocked') ORDER BY priority DESC, updated_at DESC LIMIT 6",
                (scope_key,),
            ).fetchall()
        ]
        if run_markers:
            filtered_state = []
            for row in active_state:
                blob = str(row.get('value_json') or '').lower()
                if any(marker.lower() in blob for marker in run_markers):
                    filtered_state.append(row)
            active_state = filtered_state
            filtered_loops = []
            for row in active_loops:
                blob = f"{row.get('title','')} {row.get('next_action','')} {row.get('blockers_json','')}".lower()
                if any(marker.lower() in blob for marker in run_markers):
                    filtered_loops.append(row)
            active_loops = filtered_loops
        graph_result = {'matched_entities': [], 'relationships': [], 'summary_lines': []}
        entity_rows = []
        for token in query_tokens[:6]:
            entity_rows.extend(
                conn.execute(
                    "SELECT canonical_name, entity_type FROM entities WHERE lower(canonical_name) LIKE ? ORDER BY salience_score DESC, last_seen_at DESC LIMIT 3",
                    (f"%{token}%",),
                ).fetchall()
            )
        seen = set()
        summary_lines = []
        for row in entity_rows:
            key = str(row['canonical_name'] or '')
            if key in seen:
                continue
            seen.add(key)
            summary_lines.append(f"{key} ({row['entity_type']})")
            if len(summary_lines) >= 5:
                break
        graph_result['summary_lines'] = summary_lines
        summary['tiers_checked'] = ['artifacts', 'reality_state', 'incident_truth', 'entity_graph', 'fuzzy_recall']
        summary['matches'] = {
            'incident_truth': truth_matches,
            'reality_state': active_state,
            'open_loops': active_loops[:3],
            'entity_graph': graph_result.get('summary_lines', [])[:5],
            'entity_graph_detail': graph_result,
        }
        if truth_matches and run_markers:
            summary['chosen_tier'] = 'incident_truth'
        elif active_state or active_loops:
            summary['chosen_tier'] = 'reality_state'
        elif truth_matches:
            summary['chosen_tier'] = 'incident_truth'
        elif graph_result.get('summary_lines'):
            summary['chosen_tier'] = 'entity_graph'
        else:
            summary['chosen_tier'] = 'fuzzy_recall'
        return summary
    except Exception as exc:
        summary['chosen_tier'] = 'error'
        summary['error'] = str(exc)[:240]
        return summary
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _strict_reply_control_query(user_message: str) -> bool:
    lowered = (user_message or '').lower()
    if 'live_brain_capability_e2e' not in lowered:
        return False
    markers = (
        'odgovori samo',
        'odgovori tačno',
        'odgovori tacno',
        'respond only',
        'answer exactly',
        'strictly in sections',
        'strogo u sekcijama',
    )
    return any(marker in lowered for marker in markers)


def _strict_ack_query(user_message: str) -> bool:
    lowered = (user_message or '').lower()
    return (
        ('live_brain_capability_e2e' in lowered or 'full_stress_e2e' in lowered)
        and any(token in lowered for token in ('ack-seed', 'ack-cause', 'ack-infer', 'ack-full-stress-seed'))
    )


# P1.4: lane-gated policy rules — moved out of the cached system preamble in
# live_brain.LiveBrainProvider.system_prompt_block() so they ship only on
# turns where they apply.
_APPROVAL_QUERY_TOKENS = (
    "approval", "approvals", "approve", "reject", "odobren", "odobrenj",
    "queue", "pending",
)
_HIGH_RISK_CHANGE_TOKENS = (
    "patch", "edit ", "modify", "rewrite", "delete", "remove",
    "menjaj", "promeni", "obrisi", "izmeni",
    "config", "schema", "migrate", "credential", "secret", "api key",
)


def _user_message_mentions(user_message: str, tokens) -> bool:
    if not user_message:
        return False
    lowered = user_message.lower()
    return any(t in lowered for t in tokens)


def _live_brain_policy_rules(user_message: str, turn_lane: str, isolate_epistemic_context: bool) -> str:
    """Return only the policy rules that apply to this turn.

    Each rule is a single line so the LLM doesn't reframe the whole turn
    around policy. Empty string for chit-chat / no-relevance turns — most
    common case, ships zero bytes.
    """
    if not user_message:
        return ""

    rules = []

    # Approval-queue routing — only when the message mentions approvals.
    if _user_message_mentions(user_message, _APPROVAL_QUERY_TOKENS):
        rules.append(
            "Approval queue routing: call brain_self_evolution(action='list', "
            "status='needs_approval') before answering; do not use session_search, "
            "cronjob, or brain_state_debug for approval queue answers."
        )

    # Never-auto-apply — only on deep_execution and only when the message
    # touches a high-risk-change verb.
    if turn_lane == 'deep_execution' and _user_message_mentions(
        user_message, _HIGH_RISK_CHANGE_TOKENS
    ):
        rules.append(
            "Before changing Live Brain code, config, DB schema, files, "
            "credentials, or media behavior: create a brain_self_evolution "
            "proposal and ask for approval; do not auto-apply high-risk changes."
        )

    # Never-infer-secrets — applies to any execution-bearing lane that could
    # be tempted to fabricate identifiers (codenames, hashes). Skip chit-chat
    # and document_intake (where it doesn't apply).
    if turn_lane in {'simple_execution', 'deep_execution', 'continuation_or_resume'}:
        rules.append(
            "Do not infer hidden codenames, secrets, or remembered values from "
            "run IDs, suffixes, hashes, filenames, or the current prompt; answer "
            "UNKNOWN unless Live Brain context contains the value."
        )

    # Epistemic autonomy — only on research lane (EPISTEMIC ISOLATION block
    # already covers isolate_epistemic_context elsewhere, avoid double-emit).
    if turn_lane == 'research_or_epistemic' and not isolate_epistemic_context:
        rules.append(
            "Epistemic autonomy: for current/high-stakes claims, use "
            "web_search/web_extract or brain_epistemic(action='search_web') and "
            "only authoritative sources; do not record facts from search-result "
            "titles alone."
        )

    if not rules:
        return ""
    return "LIVE BRAIN POLICY:\n- " + "\n- ".join(rules)


def _capability_e2e_query(user_message: str) -> bool:
    lowered = (user_message or '').lower()
    return 'live_brain_capability_e2e' in lowered or 'full_stress_e2e' in lowered


def _capability_e2e_step(user_message: str) -> str:
    lowered = (user_message or '').lower()
    if ' baseline ' in f' {lowered} ':
        return 'baseline'
    if ' recall ' in f' {lowered} ':
        return 'recall'
    if ' correction ' in f' {lowered} ':
        return 'correction'
    if ' continue ' in f' {lowered} ':
        return 'continue'
    if ' inference-seed ' in f' {lowered} ':
        return 'inference_seed'
    if ' inference-check ' in f' {lowered} ':
        return 'inference_check'
    if ' self-review ' in f' {lowered} ':
        return 'self_review'
    if ' research ' in f' {lowered} ':
        return 'research'
    return 'generic'


def _is_full_stress_capability_prompt(user: str) -> bool:
    return 'full_stress_e2e' in (user or '').lower()


def _self_review_step_observed(step_name: str, user: str, reply: str) -> bool:
    lowered_reply = reply.lower()
    if step_name == 'baseline':
        return ' baseline ' in f' {user} ' and any(token in lowered_reply for token in ('manual', 'unknown', 'approvals'))
    if step_name == 'seed':
        return ' seed ' in f' {user} ' and 'ack-full-stress-seed' in lowered_reply
    if step_name == 'recall':
        return ' recall ' in f' {user} ' and 'codename' in lowered_reply and 'topic' in lowered_reply
    if step_name == 'noise_guard':
        return ' ne-siri ' in f' {user} ' and '.codex' in lowered_reply
    if step_name == 'safe_execution':
        return 'safe-exec' in user and re.search(r'\b4\b', reply) is not None
    if step_name == 'approval_gate':
        return ' approval ' in f' {user} ' and any(token in lowered_reply for token in ('needs_approval', 'pending approval', 'čeka odobrenje', 'ceka odobrenje'))
    if step_name == 'cancel_stop':
        return 'cancel-start' in user and ('stopped' in lowered_reply or 'prekin' in lowered_reply or 'zaustav' in lowered_reply)
    if step_name == 'cancel_fresh':
        return 'cancel-check' in user and re.search(r'\b12\b', reply) is not None
    if step_name == 'topic_switch':
        return 'topic-switch' in user and any(token in lowered_reply for token in ('berlin', 'vreme', 'weather'))
    if step_name == 'research_isolation':
        return ' research ' in f' {user} ' and 'cmegroup.com' in lowered_reply
    if step_name == 'media_attachment':
        return ' media ' in f' {user} ' and any(token in lowered_reply for token in ('native', 'attachment', 'poslat'))
    if step_name == 'nucleus_safe_chat':
        return 'nucleus-normal' in user and not any(token in lowered_reply for token in ('nucleus status', 'pargod', 'heartbeat', 'nucleus_engine', 'doctor'))
    return False


def _is_low_signal_followup_query(user_message: str) -> bool:
    lowered = (user_message or '').strip().lower()
    normalized = re.sub(r'\s+', ' ', re.sub(r'[.!?,;:]+', ' ', lowered)).strip()
    return normalized in {'gotovo znaci', 'ok super', 'dobro', 'znaci', 'ok znaci'}


def _latest_session_reply_lines(session_id: str, *, limit: int = 2) -> list[str]:
    if not session_id or not Path(_db_path()).exists():
        return []
    conn = None
    try:
        conn = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT assistant_response FROM turn_traces WHERE session_id=? AND turn_kind='post_llm' AND assistant_response != '' ORDER BY created_at DESC LIMIT ?",
            (session_id, int(limit)),
        ).fetchall()
        lines = []
        for row in rows:
            text = str(row['assistant_response'] or '').strip()
            if not text:
                continue
            lines.append(_truncate_fact(text))
        return lines
    except Exception:
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _recent_run_seed_for_run(run_marker: str, *, sessions_limit: int = 12) -> tuple[str, str]:
    if not run_marker:
        return '', ''
    codename = ''
    topic = ''
    db_path = _db_path()
    if Path(db_path).exists():
        conn = None
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
            rows = conn.execute(
                "SELECT fact_type, fact_text FROM facts WHERE fact_type IN ('run_codename','run_topic') AND lower(fact_text) LIKE ? AND status='active' ORDER BY valid_from DESC LIMIT 4",
                (f"%{run_marker.lower()}%",),
            ).fetchall()
            for row in rows:
                text = str(row['fact_text'] if isinstance(row, sqlite3.Row) else row[1])
                if not codename:
                    match = re.search(r'codename[-_][a-z0-9_-]+', text, re.IGNORECASE)
                    if match:
                        codename = match.group(0)
                if not topic:
                    match = re.search(r'topic[-_][a-z0-9_-]+', text, re.IGNORECASE)
                    if match:
                        topic = match.group(0)
                if codename and topic:
                    return codename, topic
        except Exception:
            pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    sessions_dir = Path(_hermes_home()) / 'sessions'
    if not sessions_dir.is_dir():
        return codename, topic
    codename_pattern = re.compile(r'codename[-_][a-z0-9_-]+', re.IGNORECASE)
    topic_pattern = re.compile(r'topic[-_][a-z0-9_-]+', re.IGNORECASE)
    session_files = sorted(sessions_dir.glob('*.jsonl'), key=lambda p: p.stat().st_mtime, reverse=True)[:sessions_limit]
    for path in session_files:
        try:
            text = path.read_text(errors='replace')
        except Exception:
            continue
        for line in reversed(text.splitlines()):
            lowered = line.lower()
            if run_marker.lower() not in lowered:
                continue
            if not codename:
                match = codename_pattern.search(line)
                if match:
                    codename = match.group(0)
            if not topic:
                match = topic_pattern.search(line)
                if match:
                    topic = match.group(0)
            if codename and topic:
                return codename, topic
    return codename, topic


def _recent_codename_for_run(run_marker: str, *, sessions_limit: int = 12) -> str:
    return _recent_run_seed_for_run(run_marker, sessions_limit=sessions_limit)[0]


def _persist_capability_e2e_codename(scope_key: str, session_id: str, user_message: str) -> None:
    lowered = (user_message or '').lower()
    if 'ack-seed' not in lowered and 'ack-full-stress-seed' not in lowered:
        return
    run_markers = [marker.lower() for marker in _RUN_MARKER_RE.findall(user_message or '')]
    codename_match = re.search(r'codename[-_][a-z0-9_-]+', user_message or '', re.IGNORECASE)
    topic_match = re.search(r'topic[-_][a-z0-9_-]+', user_message or '', re.IGNORECASE)
    if not run_markers or (not codename_match and not topic_match) or not Path(_db_path()).exists():
        return
    Ingestor = _load_live_brain_ingestor_class()
    if Ingestor is None:
        return
    conn = None
    try:
        conn = _get_connection()
        conn.row_factory = sqlite3.Row
        created_at = time.time()
        ingestor = Ingestor(conn)
        if codename_match:
            ingestor.store_fact(
                fact_type='run_codename',
                fact_text=f"{run_markers[0]}: secret codename is {codename_match.group(0)}",
                confidence=0.99,
                source_kind='strict_ack_seed',
                created_at=created_at,
                session_id=session_id,
                scope_key=scope_key,
            )
        if topic_match:
            ingestor.store_fact(
                fact_type='run_topic',
                fact_text=f"{run_markers[0]}: active topic is {topic_match.group(0)}",
                confidence=0.99,
                source_kind='strict_ack_seed',
                created_at=created_at,
                session_id=session_id,
                scope_key=scope_key,
            )
        conn.commit()
    except Exception:
        try:
            if conn:
                conn.rollback()
        except Exception:
            pass
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass


def _full_stress_self_review_context(conn: sqlite3.Connection, run_marker: str, fact_texts: list[str], codename: str) -> str:
    trace_rows = conn.execute(
        """
        SELECT user_message, assistant_response
        FROM turn_traces
        WHERE lower(user_message) LIKE ?
        ORDER BY created_at ASC
        LIMIT 80
        """,
        (f"%{run_marker.lower()}%",),
    ).fetchall()
    evidence = {
        'baseline': False,
        'seed': bool(codename),
        'recall': False,
        'noise_guard': False,
        'safe_execution': False,
        'approval_gate': False,
        'cancel_stop': False,
        'cancel_fresh': False,
        'topic_switch': False,
        'research_isolation': False,
        'media_attachment': False,
        'nucleus_safe_chat': False,
    }
    media_ids: list[str] = []
    topic = ''
    for text in fact_texts:
        topic_match = re.search(r'topic[-_][a-z0-9_-]+', text, re.IGNORECASE)
        if topic_match and not topic:
            topic = topic_match.group(0)
    for row in trace_rows:
        user = str(row['user_message'] or '').lower()
        reply = str(row['assistant_response'] or '')
        for step_name in evidence:
            if _self_review_step_observed(step_name, user, reply):
                evidence[step_name] = True
        if ' recall ' in f' {user} ' and codename and codename.lower() in reply.lower() and (not topic or topic.lower() in reply.lower()):
            evidence['recall'] = True
        if ' media ' in f' {user} ' and _self_review_step_observed('media_attachment', user, reply):
            media_ids.extend(re.findall(r'message_id[:= ]+([0-9]+)', reply, re.IGNORECASE))
    passed = [name for name, ok in evidence.items() if ok]
    failed = [name for name, ok in evidence.items() if not ok]
    lines = [
        "FULL STRESS CURRENT RUN EVIDENCE:",
        f"- Run marker: {run_marker}",
    ]
    if codename:
        lines.append(f"- Codename: {codename}")
    if topic:
        lines.append(f"- Topic: {topic}")
    lines.append(f"- PASSED checks from this run only: {', '.join(passed) if passed else 'none'}")
    lines.append(f"- FAILED or missing checks from this run only: {', '.join(failed) if failed else 'none'}")
    if media_ids:
        lines.append(f"- Native media message ids observed: {', '.join(media_ids[:3])}")
    if evidence['cancel_fresh'] and not evidence['cancel_stop']:
        lines.append("- Telegram stop acknowledgement was observed by the E2E harness reply, while hook traces only store assistant post-LLM turns; treat cancel_fresh plus harness stop reply as cancel firewall coverage.")
    lines.extend([
        "STRICT SELF-REVIEW RULES:",
        "- Use only this block and facts containing the exact run marker above.",
        "- Do not use other runs or older reports.",
        "- Do not print forbidden-marker lists or replay-token names; say replay markers absent if needed.",
        "- Never say a check was absent or skipped if it is listed in PASSED above.",
        "- Treat the Telegram stop acknowledgement note plus cancel_fresh as cancel firewall evidence.",
        "- Keep the reply under 1200 characters so Telegram emits one final message.",
        "- Output sections exactly: VERDICT, PASSED, FAILED, EVIDENCE_GAPS.",
    ])
    return "\n".join(lines)


def _load_capability_e2e_context(user_message: str, scope_key: str) -> str:
    run_markers = [marker.lower() for marker in _RUN_MARKER_RE.findall(user_message or '')]
    if not run_markers or not Path(_db_path()).exists():
        return ''
    run_marker = run_markers[0]
    step = _capability_e2e_step(user_message)
    if step == 'recall':
        codename, topic = _recent_run_seed_for_run(run_marker)
        if codename and topic:
            return f"STRICT OUTPUT CONTRACT:\n- Output exactly: codename: {codename}; tema: {topic}"
        if codename:
            return f"STRICT OUTPUT CONTRACT:\n- Output exactly {codename} and nothing else."
        return "STRICT OUTPUT CONTRACT:\n- Output exactly UNKNOWN and nothing else."
    conn = None
    try:
        conn = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        facts = conn.execute(
            "SELECT fact_type, fact_text FROM facts WHERE scope_key=? AND lower(fact_text) LIKE ? AND status='active' ORDER BY valid_from DESC LIMIT 20",
            (scope_key, f"%{run_marker}%"),
        ).fetchall()
        beliefs = conn.execute(
            "SELECT belief_kind, status, claim_text FROM beliefs WHERE scope_key=? AND lower(claim_text) LIKE ? ORDER BY updated_at DESC LIMIT 20",
            (scope_key, f"%{run_marker}%"),
        ).fetchall()
        fact_texts = [str(row['fact_text'] or '') for row in facts]
        belief_texts = [str(row['claim_text'] or '') for row in beliefs]
        codename = ''
        for text in fact_texts:
            match = re.search(r'secret codename is ([a-z0-9_-]+)', text, re.IGNORECASE)
            if match:
                codename = match.group(1)
                break
        ruled_out = [text for text in belief_texts if 'ruled_out cause' in text.lower()]
        validated = [text for text in belief_texts if 'validated cause' in text.lower()]
        next_actions = [text for text in fact_texts if 'next action is' in text.lower()]

        if step == 'baseline':
            if codename:
                return f"STRICT OUTPUT CONTRACT:\n- Output exactly {codename} and nothing else."
            return "STRICT OUTPUT CONTRACT:\n- Output exactly UNKNOWN and nothing else.\n- Do not mention any other run, codename, suffix, or hash."
        if step == 'continue':
            parts = ["LIVE BRAIN"]
            if ruled_out:
                parts.append("OPEN BUG:\n- " + "\n- ".join(ruled_out[:2]))
            if validated:
                parts.append("KNOWN FACTS:\n- " + "\n- ".join(validated[:2]))
            if next_actions:
                parts.append("NEXT REQUIRED ACTION:\n- " + "\n- ".join(next_actions[:2]))
            return "\n".join(parts)
        if step == 'inference_check':
            inference_lines = [text for text in fact_texts if any(token in text.lower() for token in ('depends on adapter', 'is blocked because flag', 'blocked for deploy'))]
            if inference_lines:
                return "LIVE BRAIN\nKNOWN FACTS:\n- " + "\n- ".join(inference_lines[:4])
        if step == 'self_review':
            if 'full_stress_e2e' in (user_message or '').lower():
                return _full_stress_self_review_context(conn, run_marker, fact_texts, codename)
            lines = []
            if codename:
                lines.append(f"Run codename: {codename}")
            lines.extend(validated[:2])
            lines.extend(next_actions[:2])
            if lines:
                return "LIVE BRAIN\nKNOWN FACTS:\n- " + "\n- ".join(lines)
        return ''
    except Exception:
        return ''
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


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
        if Ingestor is not None:
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
        turn_lane = str((_SESSION_LANE_STATE.get(session_id or '') or {}).get('turn_lane') or '')
        success = True
        try:
            parsed_result = json.loads(result_text) if isinstance(result_text, str) else result_text
            if isinstance(parsed_result, dict):
                if parsed_result.get('success') is False or parsed_result.get('ok') is False or parsed_result.get('error'):
                    success = False
        except Exception:
            success = not bool(re.search(r'\b(traceback|exception|error executing|failed|permission denied|connection refused|modulenotfounderror)\b', result_text or '', re.IGNORECASE))
        # --- Feature 1 & 4: Auto-fingerprint files after writes and reads ---
        # Writes get high confidence (0.9); reads get lower (0.7) since the
        # agent didn't author the file but we still want it cached for next time.
        if success and tool_name in (_WRITE_TOOLS | _READ_TOOLS):
            args_dict = args if isinstance(args, dict) else {}
            fpath = args_dict.get('path') or args_dict.get('file') or args_dict.get('filename')
            if isinstance(fpath, str) and fpath:
                try:
                    if Path(fpath).expanduser().exists():
                        if tool_name in _WRITE_TOOLS:
                            _get_fingerprint_executor().submit(
                                _fingerprint_and_store, fpath, scope_key, session_id,
                                'auto_fingerprint', 0.9,
                            )
                        else:
                            _get_fingerprint_executor().submit(
                                _fingerprint_and_store, fpath, scope_key, session_id,
                                'auto_fingerprint_read', 0.7,
                            )
                except Exception:
                    pass

        # --- Pillar 2: Enqueue VERIFICATION REQUIRED on successful artifact creation ---
        if success and tool_name in _ARTIFACT_PRODUCING_TOOLS:
            for abs_path in _extract_candidate_artifact_paths(args, result_text):
                try:
                    cmd = _infer_verify_cmd(abs_path)
                    if not cmd:
                        continue
                    _enqueue_pending_verification(
                        conn, scope_key, session_id, abs_path,
                        tool_name, cmd, created_at,
                    )
                    # Pillar 3: opportunistically populate empty success_criteria
                    # on existing fix_recipes for this scope+tool so RECALLED FIX
                    # blocks gain a "verify with:" hint on future matches.
                    try:
                        conn.execute(
                            "UPDATE fix_recipes "
                            "   SET success_criteria=?, updated_at=? "
                            " WHERE scope_key=? AND tool_name=? "
                            "   AND (success_criteria IS NULL OR success_criteria='')",
                            (cmd, created_at, scope_key, tool_name),
                        )
                    except Exception:
                        pass
                except Exception:
                    pass

        # --- Pillar 2: Satisfy pending verifications on verifier-tool success ---
        if success and tool_name in _VERIFIER_TOOLS:
            try:
                satisfied_paths = _maybe_satisfy_pending_verifications(
                    conn, tool_name, args, success, session_id,
                    scope_key, tool_call_id, created_at,
                )
                # Pillar 4 cleanup: clear UNVERIFIED CLAIM events for satisfied paths.
                # Pillar 4 hasn't landed yet — this is a no-op until then, kept here
                # so the satisfaction path is the single point of truth.
                if satisfied_paths:
                    try:
                        for sp in satisfied_paths:
                            conn.execute(
                                "DELETE FROM reality_events "
                                " WHERE event_type='done_without_verify' "
                                "   AND scope_key=? "
                                "   AND payload_json LIKE ?",
                                (scope_key, f'%{sp}%'),
                            )
                        conn.commit()
                    except Exception:
                        pass
            except Exception:
                pass

        RealityEngine = _load_reality_engine_class()
        if RealityEngine is not None:
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
            if EpistemicManager is not None:
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
            # Categorise: development noise (patch, file edits) vs reasoning failures
            dev_tools = {'patch', 'write_file', 'execute_code', 'terminal'}
            category = "development" if tool_name in dev_tools else "reasoning"
            record_ruled_out(session_id, approach, error_snippet, db_conn=conn, category=category)

        # --- Feature 3: Seed a candidate fix_recipe from this failure ---
        if not success and turn_lane == 'deep_execution':
            try:
                error_type = _classify_error_text(result_text)
                if error_type:
                    _record_failure_recipe(
                        conn, scope_key, tool_name,
                        args if isinstance(args, dict) else {},
                        result_text, error_type, created_at,
                    )
            except Exception:
                pass

        # --- Pillar 4: Append to the process-local turn log so the done-claim
        # auditor in _post_llm_call can see which tools this turn invoked.
        if session_id:
            try:
                args_blob = json.dumps(
                    args if isinstance(args, dict) else {},
                    ensure_ascii=False, default=str,
                )[:1000]
                with _TURN_LOG_LOCK:
                    log = _TURN_TOOL_LOG.setdefault(session_id, [])
                    log.append((tool_name, args_blob, bool(success), created_at))
                    if len(log) > _TURN_LOG_MAX_PER_SESSION:
                        del log[:-_TURN_LOG_MAX_PER_SESSION]
            except Exception:
                pass
    except Exception:
        try:
            if conn:
                conn.rollback()
        except Exception:
            pass


def _pre_tool_call(**kwargs):
    tool_name = str(kwargs.get('tool_name') or '')
    session_id = str(kwargs.get('session_id') or kwargs.get('task_id') or '')
    lane_state = _SESSION_LANE_STATE.get(session_id or '', {})
    turn_lane = str(lane_state.get('turn_lane') or '')

    if tool_name in {'send_message', 'telegram'}:
        try:
            db_path = _db_path()
            if Path(db_path).exists():
                conn = _get_connection()
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT scope_key FROM context_impressions WHERE session_id=? ORDER BY created_at DESC LIMIT 1",
                    (session_id,),
                ).fetchone()
                scope_key = str(row['scope_key'] or '') if row else (session_id or 'global')
                pending_rows = conn.execute(
                    "SELECT path, suggested_command FROM pending_verifications WHERE scope_key=? AND session_id=? AND status='pending' ORDER BY created_at DESC LIMIT 5",
                    (scope_key, session_id),
                ).fetchall()
                conn.close()
                if pending_rows:
                    pending_paths = [str(r['path'] or '') for r in pending_rows if r['path']]
                    verify_cmds = [str(r['suggested_command'] or '') for r in pending_rows if r['suggested_command']]
                    return {
                        'action': 'block',
                        'message': (
                            "Blocked by Live Brain validation gate: this turn still has unverified outputs. "
                            f"Verify current artifacts before sending. Pending paths: {pending_paths[:3]}. "
                            + (f"Suggested verify commands: {verify_cmds[:2]}" if verify_cmds else "")
                        ).strip(),
                    }
        except Exception:
            pass

    # --- Pillar 1: Pre-action risk gate ---
    # Classify the tool call. Warn-only initially: log to reality_events but
    # don't block. Flip _RISK_GATE_MODE to 'enforce' after a week to harden.
    try:
        risk = _classify_action_risk(tool_name, kwargs.get('args'))
        if risk:
            action_type, payload = risk
            # Resolve scope_key cheaply for the warning record
            risk_scope = ''
            try:
                if Path(_db_path()).exists():
                    rc = sqlite3.connect(_db_path(), timeout=2.0)
                    try:
                        row = rc.execute(
                            "SELECT scope_key FROM context_impressions "
                            "WHERE session_id=? AND created_at >= ? "
                            "ORDER BY created_at DESC LIMIT 1",
                            (session_id, time.time() - 1800),
                        ).fetchone()
                        if row and row[0]:
                            risk_scope = str(row[0])
                    finally:
                        rc.close()
            except Exception:
                pass
            if not risk_scope:
                risk_scope = session_id or 'global'
            # Record asynchronously so we never block tool dispatch on the
            # risk-event write (which may contend with Nucleus).
            try:
                _get_maintenance_executor().submit(
                    _record_risk_warning_bg,
                    _db_path(), risk_scope, session_id, action_type, payload,
                )
            except Exception:
                pass
            if _RISK_GATE_MODE == 'enforce':
                # Pillar 5 cookie consumption goes here; for now this branch
                # is dormant — _RISK_GATE_MODE stays 'warn' until flipped.
                return {
                    'action': 'block',
                    'message': (
                        f"BLOCKED by Live Brain risk gate ({action_type}). "
                        "Re-issue with explicit user approval."
                    ),
                }
            # warn mode: fall through; the next pre_llm_call surfaces the warning
    except Exception:
        pass

    # --- Feature 4: Session read cache (short-circuit read_file via cache) ---
    if tool_name in _READ_TOOLS:
        args = kwargs.get('args') if isinstance(kwargs.get('args'), dict) else {}
        # Bypass cache for explicit full reads or partial-range requests
        if args.get('full') is True or args.get('offset') or args.get('limit'):
            return None
        path = args.get('path') or args.get('file') or args.get('filename')
        if isinstance(path, str) and path:
            cached = _try_cached_read(path, session_id)
            if cached:
                return {'action': 'block', 'message': cached}
        return None

    if tool_name not in {'session_search', 'search_files'}:
        return None
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
        if EpistemicManager is None:
            return None
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


def _mark_recent_context_impression(scope_key: str, session_id: str, *, outcome: str, feedback_text: str = '', attribution_mode: str = '') -> None:
    db_path = _db_path()
    if not Path(db_path).exists():
        return
    conn = None
    try:
        conn = _get_connection()
        row = conn.execute(
            "SELECT impression_id FROM context_impressions WHERE scope_key=? AND session_id=? AND outcome='pending' ORDER BY created_at DESC LIMIT 1",
            (scope_key, session_id),
        ).fetchone()
        if not row:
            return
        conn.execute(
            "UPDATE context_impressions SET outcome=?, feedback_text=?, attribution_mode=CASE WHEN ? != '' THEN ? ELSE attribution_mode END, updated_at=? WHERE impression_id=?",
            (outcome, _redact(feedback_text)[:500], attribution_mode, attribution_mode, time.time(), row[0]),
        )
        conn.commit()
    except Exception:
        try:
            if conn is not None:
                conn.rollback()
        except Exception:
            pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _record_context_impression(scope_key: str, session_id: str, user_message: str, context: str, recipe_ids: List[str] | None = None, *, allow_empty: bool = False, outcome: str = 'pending', feedback_text: str = '', attribution_mode: str = '') -> None:
    if not context and not allow_empty:
        return
    db_path = _db_path()
    if not Path(db_path).exists():
        return
    now = time.time()
    context_hash = hashlib.sha256(context.encode('utf-8', 'ignore')).hexdigest()[:24]
    safe_message = str(redact_for_storage(user_message[:500]))
    impression_id = 'impression:' + hashlib.sha256(f'{scope_key}{session_id}{safe_message}{context_hash}{int(now)}'.encode()).hexdigest()[:24]
    try:
        conn = _get_connection()
        conn.execute(
            "INSERT OR REPLACE INTO context_impressions (impression_id, scope_key, session_id, query_text, context_hash, sections_json, recipe_ids_json, outcome, feedback_text, created_at, updated_at, attribution_mode) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (impression_id, scope_key, session_id, safe_message, context_hash, json.dumps(_context_sections(context)), json.dumps(recipe_ids or []), outcome, _redact(feedback_text)[:500], now, now, attribution_mode),
        )
        conn.commit()
        conn.close()
    except Exception:
        try:
            conn.close()
        except Exception:
            pass


def _build_turn_economy_section(*, session_id: str, scope_key: str, turn_lane: str) -> str:
    """Inject escalating turn-economy awareness to prevent death spirals."""
    if not session_id or not scope_key:
        return ""
    db_path = _db_path()
    if not Path(db_path).exists():
        return ""
    conn = None
    try:
        conn = _get_connection()
        now = time.time()
        cutoff = now - 3600
        row = conn.execute(
            "SELECT COUNT(*) AS turn_count FROM context_impressions WHERE session_id=? AND created_at >= ?",
            (session_id, cutoff),
        ).fetchone()
        turn_count = int(row["turn_count"]) if row else 0
        if turn_count < 3:
            return ""
        if turn_count >= 15:
            return (
                f"CRITICAL TURN ECONOMY:\n- {turn_count}+ turns spent. STOP all tool calls IMMEDIATELY.\n"
                "- Summarize what you know and ask the user for direction.\n"
                "- If a tool failed 2+ times, do NOT retry it — explain the failure."
            )
        if turn_count >= 8:
            return (
                f"TURN ECONOMY WARNING:\n- {turn_count} turns spent. You may be stuck.\n"
                "- If the last 2+ tool calls failed, STOP and explain.\n"
                "- Prefer a clear status update over another attempt."
            )
        return (
            f"TURN ECONOMY:\n- {turn_count} turns used. Be efficient.\n"
            "- If a tool failed, do NOT retry with the same arguments.\n"
            "- Consider whether you can answer without more tool calls."
        )
    except Exception:
        return ""
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


_LANE_PREFILLS = {
    "simple_execution": (
        "TASK MODE:\n"
        "CHECK: [what do I know from the context above?]\n"
        "DO: [one action — the simplest thing that moves this forward]\n"
        "Do NOT research, explore, or overthink. Execute and report."
    ),
    "deep_execution": (
        "BUILD MODE:\n"
        "DECOMPOSE: [break into sub-problems]\n"
        "VERIFY: [what facts support each step?]\n"
        "BUILD: [implement the solution]\n"
        "If a tool fails: diagnose WHY before retrying."
    ),
    "research_or_epistemic": (
        "RESEARCH MODE:\n"
        "QUESTION: [what specific question needs answering?]\n"
        "SOURCES: [where to find authoritative answers]\n"
        "SYNTHESIZE: [combine findings into a verified answer]\n"
        "Use brain_epistemic for current/high-stakes facts."
    ),
    "continuation_or_resume": (
        "CONTINUE MODE:\n"
        "STATE: [where did we leave off?]\n"
        "NEXT: [one concrete step forward]\n"
        "Ask the user if direction is unclear."
    ),
    "document_intake": (
        "DOCUMENT MODE:\n"
        "EXTRACT: [what information is in this document?]\n"
        "TRANSFORM: [what needs to change?]\n"
        "DO NOT widen into unrelated tasks."
    ),
}


def _build_lane_prefill(turn_lane: str) -> str:
    """Return lane-specific prefill block to focus the LLM on the right approach."""
    return _LANE_PREFILLS.get(turn_lane, "")


# P3.6: skill index — lazily built from SKILL.md files
_SKILL_INDEX = None
_SKILL_INDEX_LOCK = threading.Lock()
_SKILL_INDEX_MTIME = 0.0


def _build_skill_index():
    """Lazily scan ~/.hermes/skills/ for SKILL.md files and build keyword→skill index."""
    global _SKILL_INDEX, _SKILL_INDEX_MTIME
    skills_dir = Path(_hermes_home()) / "skills"
    if not skills_dir.exists():
        return
    try:
        st = skills_dir.stat().st_mtime
    except OSError:
        return
    with _SKILL_INDEX_LOCK:
        if _SKILL_INDEX is not None and st == _SKILL_INDEX_MTIME:
            return
        index = {}
        import yaml
        for skill_md in skills_dir.rglob("SKILL.md"):
            try:
                text = skill_md.read_text(encoding="utf-8")
                # Extract YAML frontmatter
                if text.startswith("---"):
                    parts = text.split("---", 2)
                    if len(parts) >= 3:
                        fm = yaml.safe_load(parts[1]) or {}
                        name = fm.get("name", "")
                        desc = fm.get("description", "")
                        aliases = fm.get("aliases", [])
                        tags = fm.get("tags", [])
                        # Build keyword set
                        keywords = set()
                        if name:
                            keywords.add(name.lower())
                            keywords.update(name.lower().split("-"))
                        if desc:
                            keywords.update(w.lower() for w in desc.split()
                                            if len(w) > 3 and w.isalpha())
                        if aliases:
                            keywords.update(str(a).lower() for a in aliases)
                        if tags:
                            keywords.update(str(t).lower() for t in tags)
                        # Strip noise words
                        noise = {"the", "and", "for", "with", "that", "this", "from",
                                 "your", "have", "what", "when", "where", "which"}
                        keywords = {k for k in keywords if k not in noise and len(k) > 2}
                        if keywords and name:
                            index[name] = {"description": desc[:120], "keywords": keywords}
            except Exception:
                pass
        _SKILL_INDEX = index
        _SKILL_INDEX_MTIME = st
        logger.info("[LIVE_BRAIN_CTX] skill index built: %d skills", len(index))


def _build_skill_hints_section(user_message: str) -> str:
    """Scan user message against skill index and return RELEVANT SKILLS section."""
    if not user_message or len(user_message) < 3:
        return ""
    _build_skill_index()
    if not _SKILL_INDEX:
        return ""
    msg_lower = user_message.lower()
    msg_words = set(msg_lower.split())
    matches = []
    for name, info in _SKILL_INDEX.items():
        keywords = info["keywords"]
        # Match if any keyword is in the message
        overlap = keywords & msg_words
        # Also check substring matches for multi-word keywords
        if not overlap:
            for kw in keywords:
                if " " in kw and kw in msg_lower:
                    overlap.add(kw)
                elif len(kw) > 4 and kw in msg_lower:
                    overlap.add(kw)
        if overlap:
            score = len(overlap)
            matches.append((score, name, info["description"]))
    if not matches:
        return ""
    matches.sort(reverse=True)
    top = matches[:5]
    lines = ["RELEVANT SKILLS:"]
    for score, name, desc in top:
        lines.append(f"- {name}: {desc}")
    lines.append("Use skill_view to read the full skill before acting.")
    return "\n".join(lines)


def _build_task_graph_context(scope_key: str) -> str:
    """Inject current task graph state so agent always knows next step."""
    if not scope_key:
        return ""
    db_path = _db_path()
    if not Path(db_path).exists():
        return ""
    try:
        from live_brain.task_graph import TaskGraph
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        tg = TaskGraph(conn)
        result = tg.current_task_context(scope_key)
        conn.close()
        return result
    except Exception:
        return ""


def _build_continuity_section(*, session_id: str, scope_key: str) -> str:
    """Inject active task continuity for new/fresh sessions.

    Queries live_brain work_state for active objectives/open_loops.
    Only fires when the session is fresh (< 3 context_impressions)
    so we don't re-inject stale state on every turn.
    """
    if not session_id or not scope_key:
        return ""
    db_path = _db_path()
    if not Path(db_path).exists():
        return ""
    conn = None
    try:
        conn = _get_connection()
        # Only for fresh sessions
        row = conn.execute(
            "SELECT COUNT(*) FROM context_impressions WHERE session_id=?",
            (session_id,),
        ).fetchone()
        turn_count = int(row[0]) if row else 0
        if turn_count > 3:
            return ""

        # Get work state
        state_row = conn.execute(
            "SELECT state_json FROM work_state WHERE scope_key=?",
            (scope_key,),
        ).fetchone()
        if not state_row or not state_row[0]:
            return ""

        import json
        state = json.loads(state_row[0]) if isinstance(state_row[0], str) else {}
        if not isinstance(state, dict):
            return ""

        objective = state.get("current_objective", "")
        open_loops = state.get("open_loops", [])
        blockers = state.get("blockers", [])
        if not objective and not open_loops and not blockers:
            return ""

        lines = ["CONTINUE FROM PREVIOUS SESSION:"]
        if objective:
            lines.append(f"- Objective: {str(objective)[:200]}")
        if open_loops:
            for loop in (open_loops if isinstance(open_loops, list) else [])[:3]:
                lines.append(f"- Open loop: {str(loop)[:150]}")
        if blockers:
            for b in (blockers if isinstance(blockers, list) else [])[:3]:
                lines.append(f"- Blocker: {str(b)[:150]}")
        lines.append("Resume from where you left off, or ask the user for direction.")
        return "\n".join(lines)
    except Exception:
        return ""
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _pre_llm_call(**kwargs):
    try:
        return _pre_llm_call_inner(**kwargs)
    except Exception as e:
        logger.warning("[LIVE_BRAIN_CTX] _pre_llm_call failed: %s", e)
        return None


def _pre_llm_call_inner(**kwargs):
    raw_user_message = str(kwargs.get("user_message") or "")
    session_id = str(kwargs.get("session_id") or "")
    sender_id = str(kwargs.get("sender_id") or "")
    platform = str(kwargs.get("platform") or "")
    qctx = _prepare_query_context(raw_user_message, sender_id, session_id, platform=platform or 'telegram')
    scope_key = qctx.scope_key
    lane_meta = dict(_LAST_CONTEXT_METADATA.get('lane_meta') or {})
    user_message = str(lane_meta.get('semantic_message') or raw_user_message or '')
    control = classify_turn_control(user_message) if classify_turn_control is not None else {}
    event_id = ''
    if MemoryCompiler is not None and Path(_db_path()).exists():
        event_conn = None
        try:
            event_conn = _get_connection()
            if ensure_memory_v2_schema is not None:
                ensure_memory_v2_schema(event_conn)
            compiler = MemoryCompiler(event_conn)
            event_type = 'cancel_event' if control.get('cancel') else ('interruption_event' if control.get('interrupt_recovery') else 'user_turn')
            event_id = compiler.record_event(
                event_type,
                {
                    'text': user_message,
                    'raw_text': raw_user_message,
                    'lane': qctx.turn_lane,
                    'intent': qctx.intent,
                    'control': control,
                    'sender_id': sender_id,
                    'platform': platform,
                },
                session_id=session_id,
                scope_key=scope_key,
                source='live_brain_ctx.pre_llm_call',
            )
            if control.get('cancel'):
                compiler.abort_scope_tasks(scope_key, event_id, reason='user_cancel_command')
            elif control.get('topic_switch') and not control.get('continue'):
                compiler.record_event(
                    'topic_switch',
                    {'text': user_message, 'fingerprint': control.get('fingerprint')},
                    session_id=session_id,
                    scope_key=scope_key,
                    source='live_brain_ctx.turn_control',
                )
                compiler.create_or_refresh_task(
                    scope_key=scope_key,
                    session_id=session_id,
                    user_message=user_message,
                    event_id=event_id,
                    status='active',
                    priority=0.75,
                    now=qctx.now,
                )
            elif control.get('implies_ongoing_work') and not control.get('one_shot'):
                compiler.create_or_refresh_task(
                    scope_key=scope_key,
                    session_id=session_id,
                    user_message=user_message,
                    event_id=event_id,
                    status='active',
                    priority=0.65,
                    now=qctx.now,
                )
            event_conn.commit()
        except Exception as exc:
            logger.debug('[LIVE_BRAIN_CTX] v2 event record failed: %s', exc)
            try:
                if event_conn is not None:
                    event_conn.rollback()
            except Exception:
                pass
        finally:
            if event_conn is not None:
                try:
                    event_conn.close()
                except Exception:
                    pass
    _SESSION_LANE_STATE[session_id] = {
        'turn_lane': qctx.turn_lane,
        'resume_pending': bool(lane_meta.get('had_interruption_note')),
        'updated_at': time.time(),
        'last_event_id': event_id,
        'turn_control': control,
    }
    # P3.2 + P3.3: publish scope/lane/intent to the bridge so nucleus and
    # any other future plugin can read them without re-deriving.
    try:
        from .bridge import share_scope as _bridge_share_scope
        _bridge_share_scope(
            session_id,
            scope_key=scope_key,
            turn_lane=qctx.turn_lane,
            intent=qctx.intent,
        )
    except Exception as _exc:
        logger.debug("[LIVE_BRAIN_CTX] bridge.share_scope failed: %s", _exc)

    # P3.4: proactive task continuation — inject active objectives
    # when starting a fresh session so the agent resumes where it left off.
    continuity = _build_continuity_section(session_id=session_id, scope_key=scope_key)
    if continuity:
        context = continuity

    # P4.2: task graph context — inject current task plan so the agent
    # always knows what step to take next. No more guessing.
    if qctx.turn_lane in {'deep_execution', 'simple_execution', 'continuation_or_resume'}:
        try:
            task_ctx = _build_task_graph_context(scope_key)
            if task_ctx:
                context = (context + "\n\n" + task_ctx) if context else task_ctx
        except Exception:
            pass

    # P3.6: skill hints — inject matching skill names so the agent
    # never has to search for relevant skills.
    if user_message and qctx.turn_lane != 'chit_chat':
        skill_hints = _build_skill_hints_section(user_message)
        if skill_hints:
            context = (context + "\n\n" + skill_hints) if context else skill_hints

    if control.get('cancel'):
        _record_context_impression(
            scope_key,
            session_id,
            raw_user_message,
            '',
            [],
            allow_empty=True,
            outcome='ignored',
            feedback_text='cancel_event_aborted_pending_state',
            attribution_mode='suppressed',
        )
        return None
    if user_message and qctx.turn_lane == 'simple_execution' and _is_chit_chat(user_message):
        _record_context_impression(
            scope_key,
            session_id,
            raw_user_message,
            '',
            [],
            allow_empty=True,
            outcome='ignored',
            feedback_text='chat_turn_no_context',
            attribution_mode='suppressed',
        )
        return None
    if user_message and _strict_ack_query(user_message):
        _persist_capability_e2e_codename(scope_key, session_id, user_message)
        _record_context_impression(
            scope_key,
            session_id,
            raw_user_message,
            '',
            [],
            allow_empty=True,
            outcome='ignored',
            feedback_text='strict_ack_no_context',
            attribution_mode='suppressed',
        )
        return None
    capability_control = _capability_e2e_query(user_message)
    capability_step = _capability_e2e_step(user_message) if capability_control else ''
    if capability_control and capability_step == 'recall':
        context = _load_capability_e2e_context(user_message, scope_key)
        _record_context_impression(
            scope_key,
            session_id,
            raw_user_message,
            context,
            [],
            allow_empty=not bool(context),
            outcome='pending' if context else 'ignored',
            feedback_text='' if context else 'capability_recall_no_context',
            attribution_mode='generated' if context else 'suppressed',
        )
        if context:
            try:
                StoreCls = _load_live_brain_store_class()
                if StoreCls is not None and Path(_db_path()).exists():
                    store = StoreCls(_db_path())
                    try:
                        store.initialize_schema()
                        store.record_turn_trace(
                            scope_key=scope_key,
                            session_id=session_id,
                            trace_key=f"pre_llm:{session_id}:{int(time.time())}",
                            turn_kind='pre_llm',
                            user_message=raw_user_message,
                            intent='capability_recall',
                            routing_summary={'intent': 'capability_recall', 'matches': {'run_marker': True}},
                            context_sections=_context_sections(context),
                            trace_data={'context_preview': context[:1200], 'turn_lane': qctx.turn_lane, 'lane_meta': lane_meta},
                        )
                    finally:
                        store.close()
            except Exception:
                pass
            return {"context": context}
        return None
    isolate_epistemic_context = (
        (qctx.turn_lane == 'research_or_epistemic' or _should_isolate_epistemic_context(user_message))
        and not (capability_control and capability_step == 'self_review')
    )
    if user_message:
        _record_reality_event(
            scope_key,
            'user_message',
            'user_message',
            {'text': _redact(user_message), 'sender_id': sender_id, 'platform': platform},
            session_id=session_id,
            source='pre_llm_call',
            confidence=0.78,
        )
    epistemic_query = _epistemic_query_text(user_message) if isolate_epistemic_context else user_message
    context = '' if (isolate_epistemic_context or qctx.turn_lane == 'document_intake') else _load_live_brain_context(user_message, session_id, sender_id)
    if (
        not isolate_epistemic_context
        and not capability_control
        and qctx.turn_lane in {'deep_execution', 'research_or_epistemic'}
        and _should_load_reality_brief(user_message)
    ):
        reality_brief = _load_reality_brief(scope_key, user_message)
        if reality_brief:
            context = (reality_brief + "\n\n" + context) if context else reality_brief
    epistemic_brief = '' if capability_control and not isolate_epistemic_context else _load_epistemic_brief(scope_key, epistemic_query, session_id)
    if epistemic_brief:
        context = (epistemic_brief + "\n\n" + context) if context else epistemic_brief
    epistemic_autonomous_context = (
        _load_epistemic_autonomous_context(scope_key, epistemic_query, session_id)
        if qctx.turn_lane == 'research_or_epistemic' and (not capability_control or isolate_epistemic_context)
        else ''
    )
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

    # --- Feature 2 + Feature 3 + Pillar 1 + Pillar 2 + Pillar 4: FILE
    #     KNOWLEDGE / RECALLED FIX / VERIFICATION REQUIRED / UNVERIFIED CLAIM
    #     / RECENT RISK ACTIVITY injection ---
    file_knowledge = ''
    recipe_hint = ''
    verification_required = ''
    unverified_claim = ''
    risk_activity = ''
    if not isolate_epistemic_context and user_message and qctx.turn_lane in {'document_intake', 'simple_execution', 'deep_execution', 'continuation_or_resume'}:
        fk_conn = None
        try:
            db_path = _db_path()
            if Path(db_path).exists():
                fk_conn = _get_connection()
                file_knowledge = _load_file_knowledge_block(user_message, scope_key, fk_conn)
                if qctx.turn_lane == 'deep_execution':
                    recipe_hint = _load_recipe_hint_block(user_message, scope_key, fk_conn)
                verification_required = _load_pending_verification_block(
                    scope_key, session_id, fk_conn,
                )
                if qctx.turn_lane == 'deep_execution':
                    unverified_claim = _load_unverified_claim_block(scope_key, fk_conn)
                    risk_activity = _load_recent_risk_warnings_block(scope_key, fk_conn, session_id=session_id)
        except Exception as exc:
            logger.debug('[LIVE_BRAIN_CTX] file_knowledge/recipe/verif/claim/risk load failed: %s', exc)
        finally:
            if fk_conn is not None:
                try:
                    fk_conn.close()
                except Exception:
                    pass
    # Final order (top → bottom of injected context):
    #   RECENT RISK ACTIVITY (safety, top)
    #   UNVERIFIED CLAIM (corrective, demands action)
    #   VERIFICATION REQUIRED (pending edits)
    #   FILE KNOWLEDGE (reflexive recall)
    #   RECALLED FIX (past solutions)
    #   existing live brain context
    # We build by prepending; the LAST prepend appears at the top.
    if recipe_hint:
        context = (recipe_hint + "\n\n" + context) if context else recipe_hint
    if file_knowledge:
        context = (file_knowledge + "\n\n" + context) if context else file_knowledge
    if verification_required:
        context = (verification_required + "\n\n" + context) if context else verification_required
    if unverified_claim:
        context = (unverified_claim + "\n\n" + context) if context else unverified_claim
    if risk_activity:
        context = (risk_activity + "\n\n" + context) if context else risk_activity

    # P3.1: pull bridge contributions BEFORE the empty-context early-return,
    # so nucleus emissions (NUCLEUS WARN/LEARN/PROACTIVE/GRAPH) still ship
    # on turns where the live_brain cascade has nothing to add.
    _bridge_contribs = []
    try:
        from .bridge import gather_contributions as _bridge_gather
        _bridge_contribs = _bridge_gather(
            session_id=session_id,
            user_message=user_message,
            turn_lane=qctx.turn_lane,
            sender_id=sender_id,
            platform=platform,
            scope_key=scope_key,
        )
        for _c in _bridge_contribs:
            _section = f"{_c.section}:\n{_c.body.strip()}"
            context = (context + "\n\n" + _section) if context else _section
    except Exception as _exc:
        logger.warning("[LIVE_BRAIN_CTX] bridge.gather_contributions failed: %s", _exc)

    if not context:
        if qctx.turn_lane == 'document_intake' and user_message:
            context = (
                "DOCUMENT INTAKE MODE:\n"
                "- Focus only on extracting or transforming the uploaded document.\n"
                "- Prefer local PDF/text/OCR tools over research or broad memory.\n"
                "- Do not widen into unrelated objectives, incidents, or epistemic digressions.\n"
                "- Return a human summary after tool work; do not leave the user with raw tool progress."
            )
        elif qctx.turn_lane in {'simple_execution', 'continuation_or_resume'} and user_message and not _is_chit_chat(user_message):
            context = (
                "EXECUTION MODE:\n"
                "- Focus only on the current user request and the latest local task state.\n"
                "- Do not widen into unrelated memory, incidents, research, or old objectives.\n"
                "- Validate outputs before sending or claiming completion."
            )
        if not context and user_message and not _is_chit_chat(user_message):
            _record_context_impression(
                scope_key,
                session_id,
                raw_user_message,
                '',
                [],
                allow_empty=True,
                outcome='ignored',
                feedback_text='no_relevant_context',
                attribution_mode='suppressed',
            )
            return None

    # --- Cognitive Architecture injection ---
    if (
        qctx.turn_lane in {'deep_execution', 'research_or_epistemic'}
        and not _strict_reply_control_query(user_message)
        and not isolate_epistemic_context
        and not (capability_control and capability_step == 'self_review')
    ):
        try:
            fact_count = _count_facts_in_context(context)
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

    _record_context_impression(
        scope_key,
        session_id,
        raw_user_message,
        context,
        list(_LAST_CONTEXT_METADATA.get('recipe_ids') or []),
        outcome='pending',
        attribution_mode='generated',
    )
    # P1.4: lane-gated policy rules moved out of the cached system preamble.
    # These ship only when relevant, so chit-chat turns no longer carry the
    # full approval/epistemic/no-auto-apply paragraph.
    policy_block = _live_brain_policy_rules(
        user_message=user_message,
        turn_lane=qctx.turn_lane,
        isolate_epistemic_context=isolate_epistemic_context,
    )
    if policy_block:
        context = (context + "\n\n" + policy_block) if context else policy_block

    # P3.5: dynamic lane prefill — each lane gets a task-appropriate structure
    if context or qctx.turn_lane != 'chit_chat':
        lane_prefill = _build_lane_prefill(qctx.turn_lane)
        if lane_prefill:
            context = (lane_prefill + "\n\n" + context) if context else lane_prefill

    # P2.6: turn-economy stuck detector — escalating warnings at 3/8/15 turns
    if context and qctx.turn_lane in {'deep_execution', 'simple_execution', 'continuation_or_resume'}:
        try:
            turn_economy = _build_turn_economy_section(
                session_id=session_id,
                scope_key=scope_key,
                turn_lane=qctx.turn_lane,
            )
            if turn_economy:
                context = (turn_economy + "\n\n" + context) if context else turn_economy
        except Exception:
            pass

    # NOTE: bridge.gather_contributions already ran earlier (before the
    # empty-context early-return). Do NOT call it again here — that would
    # drain warnings twice.

    # P2.1 + P2.3 + P2.4 + P2.x: run the assembled cascade through the
    # single-source assembler. This enforces per-section byte caps, drops
    # lowest-priority sections until under the lane byte budget, replaces
    # sections identical to the previous turn with a 1-line pointer, and
    # emits an audit record to ~/.hermes/logs/context-budget.log.
    try:
        from .assembler import assemble as _lb_assemble, log_audit as _lb_log_audit
        if context:
            context, _audit = _lb_assemble(
                context,
                turn_lane=qctx.turn_lane,
                session_id=session_id,
                dedupe=True,
            )
            _lb_log_audit(_audit)
    except Exception as _exc:
        logger.warning("[LIVE_BRAIN_CTX] assembler failed (fallback to unassembled): %s", _exc)
    try:
        StoreCls = _load_live_brain_store_class()
        if StoreCls is not None and Path(_db_path()).exists():
            store = StoreCls(_db_path())
            try:
                store.initialize_schema()
                store.record_turn_trace(
                    scope_key=scope_key,
                    session_id=session_id,
                    trace_key=f"pre_llm:{session_id}:{int(time.time())}",
                    turn_kind='pre_llm',
                    user_message=raw_user_message,
                    intent=str((_LAST_CONTEXT_METADATA.get('routing_summary') or {}).get('intent') or ''),
                    routing_summary=_LAST_CONTEXT_METADATA.get('routing_summary') or {},
                    context_sections=_context_sections(context),
                    trace_data={
                        'context_preview': context[:1200],
                        'recipe_ids': list(_LAST_CONTEXT_METADATA.get('recipe_ids') or []),
                        'section_decisions': list(_LAST_CONTEXT_METADATA.get('section_decisions') or []),
                        'turn_lane': qctx.turn_lane,
                        'lane_meta': lane_meta,
                        'semantic_message': user_message[:500],
                    },
                )
            finally:
                store.close()
    except Exception:
        pass
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


def _record_risk_warning_bg(
    db_path: str,
    scope_key: str,
    session_id: str,
    action_type: str,
    payload: Dict[str, Any],
) -> None:
    """Pillar 1 background writer for risk_warning reality_events.

    Runs on the maintenance executor with a dedicated connection and long
    busy_timeout. Never raises.
    """
    conn = None
    try:
        if not Path(db_path).exists():
            return
        conn = sqlite3.connect(db_path, timeout=30.0)
        conn.execute('PRAGMA busy_timeout=30000')
        RealityEngineCls = _load_reality_engine_class()
        if RealityEngineCls is None:
            return
        RealityEngineCls(conn).ingest_event(
            scope_key=scope_key,
            event_type='risk_warning',
            subject=action_type,
            payload=payload,
            session_id=session_id,
            source='pre_tool_call_risk_gate',
            confidence=0.9,
        )
    except Exception as exc:
        logger.warning('[LIVE_BRAIN_CTX] background risk-warning failed: %s', exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _run_done_audit_bg(
    db_path: str,
    scope_key: str,
    session_id: str,
    phrase: str,
    turn_snapshot: List[Tuple[str, str, bool, float]],
) -> None:
    """Pillar 4 background audit worker.

    Runs on the maintenance executor with a dedicated connection and long
    busy_timeout. If a pending_verifications row exists for the
    scope+session AND no verifier-tool call this turn touched the pending
    path, write a done_without_verify reality_event. Never raises.
    """
    conn = None
    try:
        if not Path(db_path).exists():
            return
        conn = sqlite3.connect(db_path, timeout=60.0)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA busy_timeout=60000')
        pending_rows = conn.execute(
            "SELECT verification_id, path, suggested_command "
            "  FROM pending_verifications "
            " WHERE scope_key=? AND session_id=? AND status='pending'",
            (scope_key, session_id),
        ).fetchall()
        if not pending_rows:
            return
        verified = False
        for tname, ablob, ok, _ts in turn_snapshot[-20:]:
            if not ok or tname not in _VERIFIER_TOOLS:
                continue
            for r in pending_rows:
                pending_path = str(r[1] or '')
                if not _tool_verifies_pending_path(tname, pending_path):
                    continue
                base = Path(pending_path).name
                stem = Path(pending_path).stem
                if base and (base in ablob or pending_path in ablob or
                              (len(stem) >= 4 and stem in ablob)):
                    verified = True
                    break
            if verified:
                break
        if verified:
            return
        RealityEngineCls = _load_reality_engine_class()
        if RealityEngineCls is None:
            return
        RealityEngineCls(conn).ingest_event(
            scope_key=scope_key,
            event_type='done_without_verify',
            subject='unverified_claim',
            payload={
                'phrase': phrase,
                'pending_paths': [str(r[1] or '') for r in pending_rows],
                'suggested_commands': [str(r[2] or '') for r in pending_rows],
            },
            session_id=session_id,
            source='post_llm_audit',
            confidence=0.95,
        )
    except Exception as exc:
        logger.warning("[LIVE_BRAIN_CTX] background done-audit failed: %s", exc)
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
    if MemoryCompiler is not None and Path(_db_path()).exists():
        compiler_conn = None
        try:
            compiler_conn = _get_connection()
            if ensure_memory_v2_schema is not None:
                ensure_memory_v2_schema(compiler_conn)
            compiler = MemoryCompiler(compiler_conn)
            event_id = compiler.record_event(
                'assistant_turn',
                {
                    'text': assistant_response,
                    'user_message': user_message,
                    'platform': platform,
                },
                session_id=session_id,
                scope_key=scope_key,
                source='live_brain_ctx.post_llm_call',
            )
            if COMPACTION_RE.search(assistant_response or ''):
                compiler.record_event(
                    'context_compaction',
                    {'summary': assistant_response},
                    session_id=session_id,
                    scope_key=scope_key,
                    source='live_brain_ctx.post_llm_call',
                    eligible_for_compile=False,
                )
            control = (_SESSION_LANE_STATE.get(session_id or '') or {}).get('turn_control') or {}
            if not control.get('implies_ongoing_work') and not control.get('cancel'):
                active_task_id = compiler.select_active_task(
                    scope_key,
                    str((_SESSION_LANE_STATE.get(session_id or '') or {}).get('turn_lane') or ''),
                    [w for w in re.findall(r'[\w./-]+', (user_message or '').lower()) if len(w) > 2],
                    now=time.time(),
                )
                if active_task_id and assistant_response and not _DONE_RE.search(user_message or ''):
                    compiler.transition_task(active_task_id, 'resolved', event_id, reason='one_shot_turn_answered')
            compiler_conn.commit()
        except Exception as exc:
            logger.debug('[LIVE_BRAIN_CTX] assistant_turn v2 event failed: %s', exc)
            try:
                if compiler_conn is not None:
                    compiler_conn.rollback()
            except Exception:
                pass
        finally:
            if compiler_conn is not None:
                try:
                    compiler_conn.close()
                except Exception:
                    pass
    _record_reality_event(
        scope_key,
        'assistant_response',
        'assistant_response',
        {'text': _redact(user_message), 'assistant_response': _redact(assistant_response[:4000]), 'platform': platform},
        session_id=session_id,
        source='post_llm_call',
        confidence=0.72,
    )
    try:
        StoreCls = _load_live_brain_store_class()
        if StoreCls is not None and Path(_db_path()).exists():
            store = StoreCls(_db_path())
            try:
                store.initialize_schema()
                store.record_turn_trace(
                    scope_key=scope_key,
                    session_id=session_id,
                    trace_key=f"post_llm:{session_id}:{int(time.time())}",
                    turn_kind='post_llm',
                    user_message=user_message,
                    assistant_response=assistant_response,
                    intent=str((_LAST_CONTEXT_METADATA.get('routing_summary') or {}).get('intent') or ''),
                    routing_summary=_LAST_CONTEXT_METADATA.get('routing_summary') or {},
                    context_sections=[],
                    trace_data={'response_preview': assistant_response[:1600]},
                )
            finally:
                store.close()
    except Exception:
        pass
    if session_id and user_message and not _is_chit_chat(user_message):
        _mark_recent_context_impression(
            scope_key,
            session_id,
            outcome='used',
            feedback_text='llm_turn_completed',
            attribution_mode='used',
        )
    _record_epistemic_answer_if_source_backed(scope_key, user_message, assistant_response, session_id)

    # --- Pillar 4: Done-claim auditor ---
    # If the agent claims "done" while a pending_verifications row exists for
    # this scope+session AND no verifier-tool call this turn touched the
    # pending path, record a done_without_verify reality_event. The next
    # _pre_llm_call_inner will surface it as an UNVERIFIED CLAIM block.
    #
    # We schedule the audit on the background maintenance executor with its
    # own direct connection (long busy_timeout) so it can patiently wait for
    # writers like Nucleus or the gateway maintenance thread to release the
    # DB lock without blocking _post_llm_call's return.
    if scope_key and session_id and _DONE_RE.search(assistant_response or ''):
        try:
            with _TURN_LOG_LOCK:
                turn_snapshot = list(_TURN_TOOL_LOG.get(session_id, []))
            match = _DONE_RE.search(assistant_response or '')
            done_phrase = match.group(0) if match else 'done'
            _get_maintenance_executor().submit(
                _run_done_audit_bg,
                _db_path(), scope_key, session_id, done_phrase, turn_snapshot,
            )
        except Exception as exc:
            logger.debug('[LIVE_BRAIN_CTX] Pillar 4 audit submit failed: %s', exc)

    # Reset this session's turn log so the next turn starts clean.
    if session_id:
        with _TURN_LOG_LOCK:
            _TURN_TOOL_LOG.pop(session_id, None)

    # --- Attack verification (Tier 3 enforcement) ---
    if get_last_tier(session_id) == 3:
        if 'ATTACK_COMPLETED:' not in assistant_response:
            record_ruled_out(
                session_id,
                "skipped_attack_verification",
                "Tier 3 response missing mandatory ATTACK_COMPLETED marker",
                category="attack",
            )
        else:
            valid, reason = check_attack_quality(assistant_response)
            if not valid:
                record_ruled_out(
                    session_id,
                    "weak_attack_content",
                    f"ATTACK_COMPLETED present but content failed quality check: {reason}",
                    category="attack",
                )
    return None
