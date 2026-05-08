from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List

from agent.context_compressor import ContextCompressor

try:
    from live_brain.artifacts import ArtifactRegistry
except Exception:
    try:
        import importlib.util as _artifact_importlib_util
        _artifact_base = Path(__file__).resolve().parent.parent / 'live_brain'
        _utils_spec = _artifact_importlib_util.spec_from_file_location('live_brain.utils', _artifact_base / 'utils.py')
        _utils_mod = _artifact_importlib_util.module_from_spec(_utils_spec)
        _utils_spec.loader.exec_module(_utils_mod)
        import sys as _artifact_sys
        _artifact_sys.modules.setdefault('live_brain.utils', _utils_mod)
        _artifact_spec = _artifact_importlib_util.spec_from_file_location('live_brain.artifacts', _artifact_base / 'artifacts.py')
        _artifact_mod = _artifact_importlib_util.module_from_spec(_artifact_spec)
        _artifact_spec.loader.exec_module(_artifact_mod)
        ArtifactRegistry = _artifact_mod.ArtifactRegistry
    except Exception:
        ArtifactRegistry = None

try:
    from live_brain.utils import is_low_signal_thread_title, is_noisy_episode_memory
except Exception:
    def is_low_signal_thread_title(title: str) -> bool:
        return re.sub(r'\s+', ' ', (title or '').strip().lower()).strip(' .,!?:;') in {
            'da', 'ne', 'ok', 'okej', 'hmm', 'hm', 'yes', 'no', 'continue', 'nastavi',
            'cekaj', 'čekaj', 'naravno', 'moze', 'može', 'vazi', 'važi', 'ajde', 'dobro',
        }

    def is_noisy_episode_memory(title: str, summary: str = '', user_text: str = '', assistant_text: str = '') -> bool:
        combined = '\n'.join(part for part in (title or '', summary or '', user_text or '', assistant_text or '') if part)
        return bool(re.search(r'(review\s+the\s+conversation\s+above|consider\s+saving\s+or\s+updating\s+a\s+skill|skill\s+(updated|a[zž]uriran)|pending\s+self[- ]?evolution|current_summary|scope_tags_json|reality_state|open_loops)', combined, re.I)) or is_low_signal_thread_title(title)


_CONSTRAINT_TTL_DAYS = 7
_MAX_ACTIVE_EPISODES = 3
_MAX_FACT_LEN = 200
_CHIT_CHAT_PATTERNS = {'zdravo', 'hello', 'hi', 'ok', 'da', 'ne', 'hmm', 'hm', 'ajde', 'nastavi', 'cekaj', 'čekaj', 'naravno', 'sta ima', 'kako si'}
_LOW_SIGNAL_WORDS = {'problem', 'plugin', 'memory', 'brain', 'generation', 'generate', 'napravi', 'uradi', 'kako', 'sta', 'what', 'which', 'with', 'video', 'image', 'radi', 'recap', 'poslednje', 'uradjeno'}
_SECRET_RE = re.compile(r'\b(?:sk-[A-Za-z0-9_-]{12,}|sk-or-v1-[A-Za-z0-9_-]{12,}|[A-Za-z0-9_]*(?:api[_-]?key|token|secret)[A-Za-z0-9_]*\s*[:=]\s*\S+)', re.IGNORECASE)
_NOISY_MEMORY_RE = re.compile(
    r'(##\s*summary|###\s*situacija|the user sent an image|the user sent a voice message|selfie photo|personal trust|'
    r'gave me his selfie|openrouter api key|api key \(active|client_secret|review the conversation above)',
    re.IGNORECASE,
)
_LOW_VALUE_FACT_RE = re.compile(r'(dobra pitanje|refaktorisao live brain|evo kako bih|na osnovu memory context)', re.IGNORECASE)
_RUN_MARKER_RE = re.compile(r'\b(?:run|lbcap|codename)[-_][a-z0-9]+\b', re.IGNORECASE)
_DESTRUCTIVE_MEMORY_RE = re.compile(r'\b(?:izbriši|izbrisi|obriši|obrisi|briši|brisi|delete|remove|rm)\b', re.IGNORECASE)
_NEGATED_DESTRUCTIVE_RE = re.compile(r"\b(?:ne|nemoj|never|do\s+not|don'?t|dont)\s+(?:da\s+)?(?:izbriši|izbrisi|obriši|obrisi|briši|brisi|delete|remove|rm)\b", re.IGNORECASE)
_MEDIA_PROJECT_MEMORY_RE = re.compile(
    r'\b(?:enoch|media\s+delivery|messagemediadocument|artifact\s+selection|wrong\s+artifact|video\s+attachments?|video\s+delivery|mp4|pošalji\s+mi\s+ona\s+dva|posalji\s+mi\s+ona\s+dva)\b',
    re.IGNORECASE,
)
_MEDIA_PROJECT_QUERY_RE = re.compile(
    r'\b(?:enoch|media|video|mp4|attachment|artifact|artefact|delivery|messagemediadocument|pošalji|posalji)\b',
    re.IGNORECASE,
)
_SECTION_LIMITS = {
    'MUST FOLLOW': 3,
    'VERIFIED ARTIFACTS': 5,
    'ACTIVE TASK': 1,
    'KNOWN FACTS': 4,
    'OPEN BUG': 2,
    'PROVEN FIX': 3,
    'NEXT REQUIRED ACTION': 1,
    'RECENT EPISODES': 3,
    'PENDING APPROVAL': 3,
    'EPISTEMIC STATUS': 8,
}
_LAST_CONTEXT_METADATA: Dict[str, Any] = {'recipe_ids': []}
_AUTO_SURFACE_PENDING_APPROVALS = True


def _load_context_config() -> Dict[str, Any]:
    paths = [
        Path(os.environ.get('HERMES_HOME', str(Path.home() / '.hermes'))) / 'live_brain' / 'context_config.json',
        Path(__file__).resolve().with_name('context_config.json'),
    ]
    merged: Dict[str, Any] = {}
    for path in paths:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            continue
        if isinstance(data, dict):
            merged.update(data)
    return merged


def _apply_context_config() -> None:
    global _CHIT_CHAT_PATTERNS, _LOW_SIGNAL_WORDS, _SECTION_LIMITS, _AUTO_SURFACE_PENDING_APPROVALS
    config = _load_context_config()
    chit_chat = config.get('chit_chat_patterns')
    if isinstance(chit_chat, list):
        _CHIT_CHAT_PATTERNS = {str(item).strip().lower() for item in chit_chat if str(item).strip()}
    low_signal = config.get('low_signal_words')
    if isinstance(low_signal, list):
        _LOW_SIGNAL_WORDS = {str(item).strip().lower() for item in low_signal if str(item).strip()}
    section_limits = config.get('section_limits')
    if isinstance(section_limits, dict):
        updated = dict(_SECTION_LIMITS)
        for key, value in section_limits.items():
            try:
                limit = int(value)
            except Exception:
                continue
            if isinstance(key, str) and limit >= 0:
                updated[key.upper()] = limit
        _SECTION_LIMITS = updated
    auto_surface = config.get('auto_surface_pending_approvals')
    if isinstance(auto_surface, bool):
        _AUTO_SURFACE_PENDING_APPROVALS = auto_surface


_apply_context_config()

try:
    from live_brain.scopes import extract_scope_tags, scope_matches, tags_from_json
    from live_brain.scopes_config import ARTIFACT_REQUIRED_TOOL_TOKENS, IMAGE_GENERATION_ALIASES, is_image_generation_tool
except Exception:
    try:
        from .scopes import extract_scope_tags, scope_matches, tags_from_json
        from .scopes_config import ARTIFACT_REQUIRED_TOOL_TOKENS, IMAGE_GENERATION_ALIASES, is_image_generation_tool
    except Exception:
        try:
            import importlib.util as _importlib_util
            _base_path = Path(__file__).resolve().parent.parent / 'live_brain'
            _config_spec = _importlib_util.spec_from_file_location('_live_brain_ctx_scopes_config', _base_path / 'scopes_config.py')
            _config_mod = _importlib_util.module_from_spec(_config_spec)
            _config_spec.loader.exec_module(_config_mod)
            _scopes_spec = _importlib_util.spec_from_file_location('_live_brain_ctx_scopes', _base_path / 'scopes.py')
            _scopes_mod = _importlib_util.module_from_spec(_scopes_spec)
            _scopes_spec.loader.exec_module(_scopes_mod)
            extract_scope_tags = _scopes_mod.extract_scope_tags
            scope_matches = _scopes_mod.scope_matches
            tags_from_json = _scopes_mod.tags_from_json
            ARTIFACT_REQUIRED_TOOL_TOKENS = _config_mod.ARTIFACT_REQUIRED_TOOL_TOKENS
            IMAGE_GENERATION_ALIASES = _config_mod.IMAGE_GENERATION_ALIASES
            is_image_generation_tool = _config_mod.is_image_generation_tool
        except Exception:
            extract_scope_tags = None
            scope_matches = None
            tags_from_json = None
            ARTIFACT_REQUIRED_TOOL_TOKENS = ('image_generate', 'ffmpeg', 'tts', 'google_tts')
            IMAGE_GENERATION_ALIASES = ('seedream', 'bytedance-seed')
            is_image_generation_tool = lambda tool_name: 'image_generate' in (tool_name or '').lower() or any(alias in (tool_name or '').lower() for alias in IMAGE_GENERATION_ALIASES)


class LiveBrainContextEngine(ContextCompressor):
    @property
    def name(self) -> str:
        return "live_brain_ctx"

    def compress(self, messages, current_tokens=None, focus_topic=None):
        return super().compress(messages, current_tokens=current_tokens, focus_topic=focus_topic)


def _hermes_home() -> str:
    return os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))


def _db_path() -> str:
    return str(Path(_hermes_home()) / "live_brain" / "live_brain.db")


def _extract_scope_key(user_message: str, sender_id: str, session_id: str) -> str:
    if sender_id:
        return f"agent:main:telegram:dm:{sender_id}"
    return session_id or (user_message[:80] if user_message else "")


def _is_recap_query(text: str) -> bool:
    lowered = (text or "").lower()
    return any(t in lowered for t in ["sumarizuj", "sta si radio", "what did you do", "recap", "pregled"])


def _is_diagnostic_query(text: str) -> bool:
    lowered = (text or "").lower()
    return any(t in lowered for t in ["error", "bug", "problem", "fails", "ne radi", "root cause", "uzrok"])


def _is_approval_query(text: str) -> bool:
    lowered = (text or "").lower()
    return any(t in lowered for t in ["approval", "approve", "odobri", "odobren", "pending", "self-evol", "self evolution", "self evolving"])


def _is_local_stack_query(text: str) -> bool:
    lowered = (text or "").lower()
    return any(t in lowered for t in ["telegram", "vision", "image", "analyzer", "gateway", "ffmpeg", "plugin", "memory"])


def _is_chit_chat(text: str) -> bool:
    lowered = (text or "").strip().lower()
    return lowered in _CHIT_CHAT_PATTERNS or is_low_signal_thread_title(lowered) or len(lowered) < 5


def _truncate_fact(text: str) -> str:
    return _redact(text or "")[:_MAX_FACT_LEN]


def _redact(text: str) -> str:
    text = _SECRET_RE.sub('[REDACTED_SECRET]', text or '')
    return re.sub(r'\bAPI\s*key\w*\b', 'credential', text, flags=re.IGNORECASE)


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
        approval_lines.append("DB snapshot: no pending self-evolution approvals visible; confirm with brain_self_evolution before answering.")
    return approval_lines


def _is_low_signal_episode(title: str, summary: str) -> bool:
    if is_noisy_episode_memory(title, summary):
        return True
    text = (summary or '').strip()
    upper = text.upper()
    if upper.startswith('SCOPE:') and 'PROBLEM:' in upper and 'FIX:' not in upper and 'ROOT' not in upper:
        return True
    if upper.startswith('SCOPE:') and 'PROBLEM:' in upper and 'FIX:' in upper:
        fix_text = text.upper().split('FIX:', 1)[1].strip()
        useful_tokens = ('TOOL', 'FILE', 'PATH', 'COMMAND', 'RUN', 'USE ', 'ADD ', 'SET ', 'VERIFY', 'IMAGE_GENERATE', 'FFMPEG')
        if not any(token in fix_text for token in useful_tokens):
            return True
    title_words = set(re.findall(r'[\w./-]+', (title or '').lower()))
    summary_words = set(re.findall(r'[\w./-]+', text.lower()))
    meaningful = _meaningful_query_words([w for w in title_words if len(w) > 3])
    if meaningful and len(summary_words - title_words) <= 3 and len(meaningful) >= 3:
        return True
    return False


def _is_noisy_memory(text: str) -> bool:
    if not text:
        return True
    if is_noisy_episode_memory(text, text):
        return True
    lowered = text.lower().strip()
    if _NOISY_MEMORY_RE.search(text):
        return True
    if _LOW_VALUE_FACT_RE.search(text):
        return True
    if lowered.startswith(('[note:', '[system note:', '## summary', '###')):
        return True
    if len(text) > 300 and ('\n' in text or '###' in text or '```' in text or '|' in text):
        return True
    if text.count('\n') >= 2:
        return True
    return False


def _active_tags(user_message: str, scope_key: str) -> Dict[str, List[str]]:
    if extract_scope_tags:
        return extract_scope_tags(user_message, scope_key=scope_key)
    return {'scope_key': [scope_key]} if scope_key else {}


def _row_tags(row: sqlite3.Row) -> Dict[str, List[str]]:
    if not tags_from_json:
        return {}
    try:
        return tags_from_json(row['scope_tags_json'])
    except Exception:
        return {}


def _matches(row: sqlite3.Row, active_tags: Dict[str, List[str]], fallback_scope_key: str = '') -> bool:
    tags = _row_tags(row)
    try:
        row_scope = row['scope_key']
    except Exception:
        row_scope = fallback_scope_key
    if tags and scope_matches:
        if scope_matches(tags, active_tags):
            return True
        if row_scope and row_scope == fallback_scope_key:
            hard_keys = ('tool', 'repo', 'file', 'project')
            for key in hard_keys:
                left = set(tags.get(key) or [])
                right = set(active_tags.get(key) or [])
                if left and right and left.isdisjoint(right):
                    return False
            return True
        return False
    return not row_scope or row_scope == fallback_scope_key




def _causal_matches(row: sqlite3.Row, active_tags: Dict[str, List[str]], fallback_scope_key: str = '') -> bool:
    tags = _row_tags(row)
    if not tags:
        return _matches(row, active_tags, fallback_scope_key)
    relaxed = {k: v for k, v in tags.items() if k in ('scope_key', 'tool', 'domain', 'repo', 'file')}
    if relaxed and scope_matches:
        return scope_matches(relaxed, active_tags)
    return _matches(row, active_tags, fallback_scope_key)

def _append_section(parts: List[str], title: str, lines: List[str]) -> None:
    clean = []
    seen = set()
    for line in lines:
        text = _redact(line or '').strip()
        if not text or text in seen:
            continue
        seen.add(text)
        clean.append(text)
        if len(clean) >= _SECTION_LIMITS.get(title, 3):
            break
    if clean:
        prefix = f"{title}:\n- "
        for index, part in enumerate(parts):
            if part.startswith(prefix):
                existing = [p.strip() for p in part[len(prefix):].split("\n- ") if p.strip()]
                merged = []
                for item in existing + clean:
                    if item not in merged:
                        merged.append(item)
                    if len(merged) >= _SECTION_LIMITS.get(title, 3):
                        break
                parts[index] = prefix + "\n- ".join(merged)
                return
        parts.append(prefix + "\n- ".join(clean))


def _current_turn_allows_destructive_memory(text: str) -> bool:
    # Historical delete instructions are dangerous: only the latest user turn can
    # authorize deletion, and explicit negation like "ne brisi" must block it.
    value = text or ''
    if not _DESTRUCTIVE_MEMORY_RE.search(value):
        return False
    if _NEGATED_DESTRUCTIVE_RE.search(value):
        return False
    return True


def _is_destructive_memory_text(text: str) -> bool:
    return bool(_DESTRUCTIVE_MEMORY_RE.search(text or ''))


def _meaningful_query_words(words: List[str]) -> List[str]:
    return [w for w in words if w not in _LOW_SIGNAL_WORDS]


def _row_text(row: sqlite3.Row, fields: List[str]) -> str:
    values = []
    for field in fields:
        try:
            values.append(str(row[field] or ''))
        except Exception:
            pass
    return ' '.join(values).lower()


def _row_noisy(row: sqlite3.Row, fields: List[str]) -> bool:
    return _is_noisy_memory(_row_text(row, fields))


def _marker_tokens(text: str) -> set[str]:
    return {m.group(0).lower() for m in _RUN_MARKER_RE.finditer(text or '')}


def _marker_conflicts(query_text: str, row_text: str) -> bool:
    query_tokens = _marker_tokens(query_text)
    row_tokens = _marker_tokens(row_text)
    return bool(query_tokens and row_tokens and query_tokens.isdisjoint(row_tokens))


def _domain_conflicts(query_text: str, row_text: str) -> bool:
    # Old project/media blockers are high-signal only for media/artifact tasks.
    # Do not let them pollute Live Brain/plugin capability self-reviews.
    query_text = query_text or ''
    row_text = row_text or ''
    query_lower = query_text.lower()
    row_lower = row_text.lower()
    if re.search(r'\b(?:codename[-_][a-z0-9]+|tajni\s+codename|secret\s+codename)\b', row_lower) and 'codename' not in query_lower and not ('live_brain_capability_e2e' in query_lower and 'self-review' in query_lower):
        return True
    if _MEDIA_PROJECT_MEMORY_RE.search(row_text) and not _MEDIA_PROJECT_QUERY_RE.search(query_text):
        return True
    if 'live_brain_capability_e2e' in query_lower:
        query_markers = _marker_tokens(query_text)
        row_markers = _marker_tokens(row_text)
        old_blocker = re.search(r'\b(?:production\s+blocker|operating\s+contract|observer\s+result|normal\s+human\s+chat|verify\s+blocker)\b', row_text, re.IGNORECASE)
        if old_blocker and (not query_markers or not row_markers or query_markers.isdisjoint(row_markers)):
            return True
    return False


def _has_overlap(row: sqlite3.Row, query_words: List[str], fields: List[str]) -> bool:
    words = _meaningful_query_words(query_words)
    if not words:
        return True
    text = _row_text(row, fields)
    query_text = ' '.join(query_words)
    if _marker_conflicts(query_text, text):
        return False
    if _domain_conflicts(query_text, text):
        return False
    if any(alias in words for alias in IMAGE_GENERATION_ALIASES):
        return any(alias in text for alias in IMAGE_GENERATION_ALIASES)
    if 'ffmpeg' in words:
        return 'ffmpeg' in text
    return any(w in text for w in words)


def _artifact_required(tool_used: str) -> bool:
    tool = (tool_used or '').lower()
    return any(token in tool for token in ARTIFACT_REQUIRED_TOOL_TOKENS)


def _tool_relevant(tool_used: str, active_tags: Dict[str, List[str]], query_lower: str) -> bool:
    tool = (tool_used or '').lower()
    active_tools = set(active_tags.get('tool', []))
    active_domains = set(active_tags.get('domain', []))
    if not tool:
        return False
    if any(t and t in tool for t in active_tools):
        return True
    if 'image' in active_domains or 'image' in query_lower or any(alias in query_lower for alias in IMAGE_GENERATION_ALIASES):
        return is_image_generation_tool(tool)
    if 'audio' in active_domains or 'tts' in query_lower or 'voice' in query_lower:
        return 'tts' in tool or 'whisper' in tool
    if 'video' in active_domains or 'ffmpeg' in query_lower:
        return 'ffmpeg' in tool or 'video' in tool
    return tool in query_lower




def _specificity_score(row: sqlite3.Row) -> int:
    tags = _row_tags(row)
    return sum(len(values or []) for values in tags.values())


def _default_args_for_tool(tool_name: str) -> Dict[str, Any]:
    tool = (tool_name or '').lower()
    if is_image_generation_tool(tool):
        return {'tool': 'image_generate', 'input': 'local file', 'output': 'absolute path'}
    if 'ffmpeg' in tool:
        return {'tool': 'ffmpeg', 'input': 'video', 'output': 'mp4'}
    if 'tts' in tool:
        return {'tool': tool_name, 'input': 'text', 'output': 'absolute path'}
    if 'whisper' in tool:
        return {'tool': 'whisper', 'input': 'audio', 'output': 'transcript'}
    return {}


def _default_success_for_tool(tool_name: str) -> str:
    tool = (tool_name or '').lower()
    if is_image_generation_tool(tool):
        return 'image file exists at absolute output path'
    if 'ffmpeg' in tool:
        return 'video file exists and has non-zero size'
    if 'tts' in tool:
        return 'audio file exists and has non-zero size'
    if 'whisper' in tool:
        return 'transcript text is non-empty'
    return 'expected artifact exists'


def _args_hint(args: Dict[str, Any]) -> str:
    if not args:
        return ''
    parts = []
    for key in ('tool', 'input', 'output', 'model'):
        value = args.get(key)
        if value:
            parts.append(f"{key}={value}")
    paths = args.get('paths')
    if paths:
        parts.append('paths=' + ','.join(str(p) for p in paths[:2]))
    return '; '.join(parts)


def _verify_hint(text: str) -> str:
    lowered = (text or '').lower()
    if not lowered:
        return ''
    if 'image' in lowered and 'file' in lowered:
        return 'verify=image file exists'
    if 'video' in lowered and ('non-zero' in lowered or 'playable' in lowered or 'exists' in lowered):
        return 'verify=video exists+nonzero'
    if 'audio' in lowered and ('non-zero' in lowered or 'playable' in lowered or 'exists' in lowered):
        return 'verify=audio exists+nonzero'
    if 'transcript' in lowered:
        return 'verify=transcript nonempty'
    if 'artifact' in lowered or 'file' in lowered:
        return 'verify=artifact exists'
    if len(text) <= 48:
        return f'verify={text}'
    return ''


def _recipe_hint(tool_name: str, args: Dict[str, Any], success_criteria: str, times_confirmed: int) -> str:
    pieces = [f'Use {tool_name}']
    effective_args = args or _default_args_for_tool(tool_name)
    effective_success = success_criteria or _default_success_for_tool(tool_name)
    args_hint = _args_hint(effective_args)
    verify = _verify_hint(effective_success)
    if args_hint:
        pieces.append(args_hint)
    if verify:
        pieces.append(verify)
    if times_confirmed:
        pieces.append(f'confirmed={times_confirmed}x')
    return '; '.join(pieces)[:180]


def _causal_score(row: sqlite3.Row, active_tags: Dict[str, List[str]]) -> float:
    tags_score = _specificity_score(row)
    confirmed = float(row['times_confirmed'] or 0)
    try:
        args_quality = 1.0 if json.loads(row['args_template_json'] or '{}') else 0.0
    except Exception:
        args_quality = 0.0
    tool = ''
    try:
        tool = row['tool_used'] or ''
    except Exception:
        try:
            tool = row['tool_name'] or ''
        except Exception:
            tool = ''
    tool_bonus = 1.0 if any(t and t in tool.lower() for t in active_tags.get('tool', [])) else 0.0
    return tags_score * 3.0 + min(confirmed, 20.0) * 0.5 + args_quality + tool_bonus

def _visible_fact_matches(fact_text: str, query_words: List[str]) -> bool:
    visible = _truncate_fact(fact_text).lower()
    words = _meaningful_query_words(query_words)
    if 'ffmpeg' in words:
        return 'ffmpeg' in visible
    if any(alias in words for alias in IMAGE_GENERATION_ALIASES):
        return any(alias in visible for alias in IMAGE_GENERATION_ALIASES)
    return True


def _load_live_brain_context(user_message: str, session_id: str, sender_id: str) -> str:
    db_path = _db_path()
    if not Path(db_path).exists():
        return ""

    approval_query = _is_approval_query(user_message or "")
    if _is_chit_chat(user_message or "") and not approval_query:
        if _AUTO_SURFACE_PENDING_APPROVALS:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
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
            finally:
                conn.close()
        return ""

    scope_key = _extract_scope_key(user_message, sender_id, session_id)
    now = time.time()
    ttl_cutoff = now - _CONSTRAINT_TTL_DAYS * 86400
    query_lower = (user_message or "").lower()
    query_words = [w for w in re.findall(r'[\w./-]+', query_lower) if len(w) > 3]
    active_tags = _active_tags(user_message, scope_key)
    _LAST_CONTEXT_METADATA['recipe_ids'] = []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            "UPDATE rules SET status='expired', updated_at=? WHERE status='active' AND expires_at IS NOT NULL AND expires_at <= ?",
            (now, now),
        )
        conn.execute(
            "UPDATE episodes SET status='archived', updated_at=? WHERE status='active' AND episode_id NOT IN (SELECT episode_id FROM episodes WHERE status='active' ORDER BY updated_at DESC LIMIT ?)",
            (now, _MAX_ACTIVE_EPISODES),
        )
        conn.commit()

        # 1. Binding constraints — scope-matched, TTL-filtered, deterministic
        binding_rules = conn.execute(
            "SELECT action_json, scope_tags_json, updated_at, specificity FROM rules WHERE scope IN ('user_binding','user_correction') AND category IN ('binding_constraint','learned_fact') AND status='active' AND updated_at > ? ORDER BY specificity DESC, confidence DESC, times_confirmed DESC LIMIT 20",
            (ttl_cutoff,),
        ).fetchall()

        # 2. Active work item (non-chit-chat)
        work_item_row = conn.execute(
            "SELECT title, status, root_cause, next_step, evidence_json, scope_tags_json FROM work_items WHERE scope_key=? AND title NOT LIKE 'sumarizuj%' AND title NOT LIKE 'what did you do%' ORDER BY CASE WHEN status='active' THEN 0 WHEN status='blocked' THEN 1 ELSE 2 END, priority DESC, updated_at DESC LIMIT 10",
            (scope_key,),
        ).fetchall()
        scoped_work_rows = [r for r in work_item_row if _matches(r, active_tags, scope_key) and not _row_noisy(r, ['title', 'root_cause', 'next_step', 'evidence_json'])]
        overlapped_work_rows = [r for r in scoped_work_rows if _has_overlap(r, query_words, ['title', 'root_cause', 'next_step', 'evidence_json'])]
        work_item_row = (overlapped_work_rows or ([] if _meaningful_query_words(query_words) else scoped_work_rows))
        work_item_row = work_item_row[0] if work_item_row else None

        # 3. Active episodes — max 3, no chit-chat, summarized to 1 line
        episode_rows = conn.execute(
            "SELECT title, current_summary, scope_tags_json FROM episodes WHERE status IN ('active','dormant') AND length(current_summary) > 20 ORDER BY updated_at DESC LIMIT 80",
        ).fetchall()

        # 4. Validated facts — atomic, max 200 chars, deduped
        fact_rows = conn.execute(
            "SELECT DISTINCT fact_text, scope_tags_json, scope_key FROM facts WHERE status='active' AND confidence >= 0.75 ORDER BY CASE WHEN scope_key=? THEN 0 ELSE 1 END, valid_from DESC LIMIT 30",
            (scope_key,),
        ).fetchall()

        # 5. Open hypotheses with signal
        belief_rows = conn.execute(
            "SELECT claim_text, belief_kind, status, scope_tags_json, scope_key FROM beliefs WHERE status IN ('open','validated') ORDER BY CASE WHEN scope_key=? THEN 0 ELSE 1 END, updated_at DESC LIMIT 20",
            (scope_key,),
        ).fetchall()

        # 6. Fix recipes and causal activations
        recipe_rows = []
        causal_rows = []
        causal_words = _meaningful_query_words(query_words) or query_words
        if causal_words:
            recipe_like_clause = ' OR '.join(['lower(problem_pattern) LIKE ?' for _ in causal_words[:6]])
            recipe_params = [f'%{w}%' for w in causal_words[:6]] + [scope_key]
            like_clause = ' OR '.join(['lower(trigger_text) LIKE ?' for _ in causal_words[:6]])
            params = [f'%{w}%' for w in causal_words[:6]] + [scope_key]
            try:
                recipe_rows = conn.execute(
                    f"SELECT recipe_id, problem_pattern, tool_name, steps_json, args_template_json, success_criteria, artifact_verified, promotion_status, confidence, times_confirmed, scope_tags_json FROM fix_recipes WHERE ({recipe_like_clause}) AND scope_key=? AND status='active' AND promotion_status='active' AND artifact_verified=1 ORDER BY confidence DESC, times_confirmed DESC, updated_at DESC LIMIT 12",
                    recipe_params,
                ).fetchall()
                causal_rows = conn.execute(
                    f"SELECT tool_used, trigger_pattern, args_template_json, test_result, artifact_verified, times_confirmed, confidence, scope_tags_json FROM causal_activations WHERE ({like_clause}) AND scope_key=? AND success=1 ORDER BY times_confirmed DESC, confidence DESC, updated_at DESC LIMIT 12",
                    params,
                ).fetchall()
            except Exception:
                pass

        # 7. Learned principles
        knowledge_rows = conn.execute(
            "SELECT principle_text FROM crystallised_knowledge WHERE scope_key=? OR scope_key='' ORDER BY created_at DESC LIMIT 3",
            (scope_key,),
        ).fetchall()

        # 8. Verified artifacts — deterministic project artifact selection.
        artifact_lines = []
        if ArtifactRegistry is not None:
            try:
                artifact_lines = ArtifactRegistry(conn).context_lines_for_query(
                    user_message or '',
                    limit=_SECTION_LIMITS.get('VERIFIED ARTIFACTS', 5),
                )
                if artifact_lines:
                    conn.commit()
            except Exception:
                artifact_lines = []

        # 9. Recap
        recap_row = conn.execute(
            "SELECT task, root_cause, current_status, next_step FROM canonical_recaps WHERE scope_key=? ORDER BY updated_at DESC LIMIT 1",
            (scope_key,),
        ).fetchone()

        # 10. Gated self-evolution approvals — surface only when explicit, new, or relevant.
        pending_approval_rows = []
        approval_surface_reason = ''
        should_surface_approval = False
        if approval_query or _AUTO_SURFACE_PENDING_APPROVALS:
            pending_approval_rows = _fetch_pending_approval_rows(conn)
            should_surface_approval, approval_surface_reason, pending_approval_rows = _should_surface_pending_approvals(conn, pending_approval_rows, user_message or '', approval_query)
            if should_surface_approval:
                _mark_pending_approvals_surfaced(conn, pending_approval_rows, approval_surface_reason)

    finally:
        conn.close()

    parts: List[str] = []

    # PENDING APPROVAL — surface only when explicit, newly pending, or relevant to this turn.
    if should_surface_approval:
        _append_section(parts, "PENDING APPROVAL", _approval_context_lines(pending_approval_rows, approval_query=approval_query))
    elif pending_approval_rows:
        _append_section(parts, "APPROVAL ROUTING", _suppressed_approval_reminder_lines())

    # BINDING CONSTRAINTS — deterministic scope match, TTL enforced
    if binding_rules:
        constraints = []
        for r in binding_rules:
            try:
                if not _matches(r, active_tags, scope_key):
                    continue
                action = json.loads(r['action_json'])
                instruction = action.get('instruction', '')
                if not instruction or _is_noisy_memory(instruction):
                    continue
                instruction_lower = instruction.lower()
                if _is_destructive_memory_text(instruction) and not _current_turn_allows_destructive_memory(user_message or ''):
                    continue
                if _marker_conflicts(query_lower, instruction_lower):
                    continue
                if _domain_conflicts(query_lower, instruction_lower):
                    continue
                instr_words = [w for w in re.findall(r'[\w./-]+', instruction_lower) if len(w) > 4 and w not in _LOW_SIGNAL_WORDS]
                # Include only when the current task explicitly overlaps the constraint.
                if not instr_words or any(w in query_lower for w in instr_words[:10]):
                    constraints.append(_truncate_fact(instruction))
            except Exception:
                pass
        if constraints:
            _append_section(parts, "MUST FOLLOW", constraints)

    # VERIFIED ARTIFACTS — deterministic project file choices before fuzzy episodes/search.
    try:
        if artifact_lines:
            _append_section(parts, "VERIFIED ARTIFACTS", artifact_lines)
    except Exception:
        pass

    # FIX RECIPES / CAUSAL ACTIVATIONS — proven tool approaches
    recipe_hints = []
    selected_recipe_ids: List[str] = []
    if recipe_rows:
        recipe_candidates = []
        for r in recipe_rows:
            if not r['tool_name'] or not _tool_relevant(r['tool_name'], active_tags, query_lower) or not _causal_matches(r, active_tags, scope_key):
                continue
            recipe_candidates.append((_causal_score(r, active_tags), r))
        recipe_candidates.sort(key=lambda item: item[0], reverse=True)
        seen_tools = set()
        for _, r in recipe_candidates:
            tool_key = (r['tool_name'] or '').split(':')[0]
            if tool_key in seen_tools:
                continue
            seen_tools.add(tool_key)
            args = {}
            try:
                args = json.loads(r['args_template_json'] or '{}')
            except Exception:
                args = {}
            recipe_hints.append(_recipe_hint(r['tool_name'], args, r['success_criteria'], int(r['times_confirmed'] or 0)))
            selected_recipe_ids.append(r['recipe_id'])
    if recipe_hints:
        _LAST_CONTEXT_METADATA['recipe_ids'] = selected_recipe_ids[:_SECTION_LIMITS.get('PROVEN FIX', 3)]
        _append_section(parts, "PROVEN FIX", recipe_hints)
    elif causal_rows:
        candidates = []
        for r in causal_rows:
            if not r['tool_used'] or not _tool_relevant(r['tool_used'], active_tags, query_lower) or not _causal_matches(r, active_tags, scope_key):
                continue
            if _artifact_required(r['tool_used']) and not int(r['artifact_verified'] or 0):
                continue
            candidates.append((_causal_score(r, active_tags), r))
        candidates.sort(key=lambda item: item[0], reverse=True)
        hints = []
        seen_tools = set()
        for _, r in candidates:
            tool_key = (r['tool_used'] or '').split(':')[0]
            if tool_key in seen_tools:
                continue
            seen_tools.add(tool_key)
            args = {}
            try:
                args = json.loads(r['args_template_json'] or '{}')
            except Exception:
                args = {}
            hints.append(_recipe_hint(r['tool_used'], args, r['test_result'] or _default_success_for_tool(r['tool_used']), int(r['times_confirmed'] or 0)))
        if hints:
            _append_section(parts, "PROVEN FIX", hints)

    # LEARNED PRINCIPLES — useful facts, not free-form fixes
    if knowledge_rows:
        principles = [_truncate_fact(r[0]) for r in knowledge_rows if r[0] and not _SECRET_RE.search(r[0]) and not _is_noisy_memory(r[0]) and _has_overlap(r, query_words, ['principle_text'])]
        if principles:
            _append_section(parts, "KNOWN FACTS", principles)

    # ACTIVE WORK ITEM
    if work_item_row and not _is_recap_query(user_message or ""):
        lines = [f"Task: {work_item_row['title']}"]
        if work_item_row['status']:
            lines.append(f"Status: {work_item_row['status']}")
        root_cause = (work_item_row['root_cause'] or '').strip()
        if root_cause and root_cause not in {'.', '-', 'unknown'} and len(root_cause) > 3 and not _marker_conflicts(query_lower, root_cause.lower()):
            lines.append(f"Root cause: {_truncate_fact(root_cause)}")
        _append_section(parts, "ACTIVE TASK", ["; ".join(lines)])

    # ACTIVE EPISODES — max 3, 1 line each, no chit-chat.
    # If an episode summary is noisy but the title itself matches the query,
    # keep the clean title as a title-only hint instead of dropping the memory.
    useful_episodes = []
    for r in episode_rows:
        if _is_chit_chat(r['title']) or not r['current_summary']:
            continue
        if not _matches(r, active_tags, scope_key):
            continue
        title_overlap = _has_overlap(r, query_words, ['title'])
        full_overlap = _has_overlap(r, query_words, ['title', 'current_summary'])
        if not (title_overlap or full_overlap):
            continue
        if _is_low_signal_episode(r['title'], r['current_summary']):
            continue
        summary_text = r['current_summary'] or ''
        title_text = r['title'] or ''
        if _is_destructive_memory_text(f"{title_text} {summary_text}") and not _current_turn_allows_destructive_memory(user_message or ''):
            continue
        noisy_summary = _row_noisy(r, ['current_summary']) or bool(_SECRET_RE.search(summary_text))
        noisy_title = _row_noisy(r, ['title']) or bool(_SECRET_RE.search(title_text))
        if noisy_title:
            continue
        if noisy_summary and not title_overlap:
            continue
        if noisy_summary and len((r['title'] or '').strip()) < 12:
            continue
        useful_episodes.append((r, noisy_summary))
    if useful_episodes:
        ep_lines = []
        for r, noisy_summary in useful_episodes:
            title = (r['title'] or '')[:60]
            if noisy_summary:
                ep_lines.append(title)
            else:
                ep_lines.append(f"{title}: {(r['current_summary'] or '')[:80]}")
        _append_section(parts, "RECENT EPISODES", ep_lines)

    # VALIDATED FACTS — atomic, max 200 chars
    if fact_rows:
        facts = [_truncate_fact(r['fact_text']) for r in fact_rows if r['fact_text'] and not _SECRET_RE.search(r['fact_text']) and not _is_noisy_memory(r['fact_text']) and not _domain_conflicts(query_lower, r['fact_text']) and _visible_fact_matches(r['fact_text'], query_words) and _matches(r, active_tags, scope_key) and _has_overlap(r, query_words, ['fact_text'])]
        if facts:
            _append_section(parts, "KNOWN FACTS", facts)

    # OPEN HYPOTHESES — only if there's a real signal
    open_beliefs = [r['claim_text'] for r in belief_rows if r['status'] == 'open' and len(r['claim_text']) > 20 and not _is_noisy_memory(r['claim_text']) and _matches(r, active_tags, scope_key) and _has_overlap(r, query_words, ['claim_text'])]
    if open_beliefs:
        _append_section(parts, "OPEN BUG", [_truncate_fact(b) for b in open_beliefs[:2]])

    # VALIDATED CAUSES — facts only; PROVEN FIX is reserved for executable recipes
    validated_causes = [r['claim_text'] for r in belief_rows if r['status'] == 'validated' and r['belief_kind'] == 'validated_cause' and not _is_noisy_memory(r['claim_text']) and _matches(r, active_tags, scope_key) and _has_overlap(r, query_words, ['claim_text'])]
    if validated_causes:
        _append_section(parts, "KNOWN FACTS", [f"Cause: {_truncate_fact(c)}" for c in validated_causes[:2]])

    # NEXT BEST ACTIONS — only if there's a real signal (not "answer user")
    if work_item_row and work_item_row['next_step']:
        next_step = work_item_row['next_step']
        generic_next = ['diagnose the problem using exact entities', 'before guessing', 'answer the user']
        lowered_next = next_step.lower()
        if next_step and 'continue' not in lowered_next and 'answer' not in lowered_next and not any(token in lowered_next for token in generic_next):
            _append_section(parts, "NEXT REQUIRED ACTION", [next_step[:200]])

    # RECAP — only for recap queries
    if _is_recap_query(user_message or "") and recap_row and not any(_is_noisy_memory(recap_row[field] or '') for field in ['task', 'root_cause', 'current_status', 'next_step']):
        recap_lines = []
        if recap_row['task']:
            recap_lines.append(f"Task: {recap_row['task'][:80]}")
        if recap_row['root_cause']:
            recap_lines.append(f"Root cause: {_truncate_fact(recap_row['root_cause'])}")
        if recap_row['current_status']:
            recap_lines.append(f"Status: {recap_row['current_status']}")
        if recap_row['next_step']:
            recap_lines.append(f"Next: {recap_row['next_step'][:100]}")
        if recap_lines:
            parts.append("LATEST RECAP:\n- " + "\n- ".join(recap_lines))

    # DIAGNOSTIC GUIDANCE — only for diagnostic queries
    if _is_diagnostic_query(user_message or ""):
        parts.append("DIAGNOSTIC RULE: Do not present hypotheses as confirmed causes. Give one concrete next debugging step if evidence is insufficient.")

    if not parts:
        return ""
    return "LIVE BRAIN\n" + "\n".join(parts)


def _debug_live_brain_context(user_message: str, session_id: str = '', sender_id: str = '') -> Dict[str, Any]:
    scope_key = _extract_scope_key(user_message, sender_id, session_id)
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
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        facts = conn.execute(
            "SELECT fact_text, scope_tags_json, scope_key FROM facts WHERE status='active' AND confidence >= 0.75 ORDER BY valid_from DESC LIMIT 100"
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
            recipe_like_clause = ' OR '.join(['lower(problem_pattern) LIKE ?' for _ in causal_words[:6]])
            recipe_rows = conn.execute(
                f"SELECT recipe_id, problem_pattern, tool_name, steps_json, args_template_json, success_criteria, artifact_verified, promotion_status, confidence, times_confirmed, scope_tags_json FROM fix_recipes WHERE ({recipe_like_clause}) AND scope_key=? AND status='active' AND promotion_status='active' AND artifact_verified=1 ORDER BY confidence DESC, times_confirmed DESC LIMIT 50",
                [f'%{w}%' for w in causal_words[:6]] + [scope_key],
            ).fetchall()
            for row in recipe_rows:
                if not _causal_matches(row, active_tags, scope_key):
                    debug['rejections']['recipes_scope'] += 1
                elif not _tool_relevant(row['tool_name'], active_tags, query_lower):
                    debug['rejections']['recipes_tool'] += 1
            like_clause = ' OR '.join(['lower(trigger_text) LIKE ?' for _ in causal_words[:6]])
            rows = conn.execute(
                f"SELECT tool_used, trigger_pattern, args_template_json, test_result, artifact_verified, times_confirmed, confidence, scope_tags_json FROM causal_activations WHERE ({like_clause}) AND scope_key=? AND success=1 ORDER BY times_confirmed DESC LIMIT 50",
                [f'%{w}%' for w in causal_words[:6]] + [scope_key],
            ).fetchall()
            for row in rows:
                if not _causal_matches(row, active_tags, scope_key):
                    debug['rejections']['causal_scope'] += 1
                elif not _tool_relevant(row['tool_used'], active_tags, query_lower):
                    debug['rejections']['causal_tool'] += 1
    finally:
        conn.close()
    return debug




def _load_reality_engine_class():
    try:
        from live_brain.reality import RealityEngine
        return RealityEngine
    except Exception:
        pass
    import importlib.util as _importlib_util
    import sys as _sys
    import types as _types
    package_name = '_live_brain_ctx_reality_pkg'
    live_brain_dir = Path(__file__).resolve().parent.parent / 'live_brain'
    if package_name not in _sys.modules:
        package = _types.ModuleType(package_name)
        package.__path__ = [str(live_brain_dir)]
        _sys.modules[package_name] = package
    for module_name in ['utils', 'reality']:
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
    return _sys.modules[f'{package_name}.reality'].RealityEngine


def _record_reality_event(scope_key: str, event_type: str, subject: str, payload: Dict[str, Any], *, session_id: str = '', source: str = 'live_brain_ctx', confidence: float = 0.75, created_at: float | None = None) -> dict:
    db_path = _db_path()
    if not Path(db_path).exists():
        return {}
    conn = None
    try:
        conn = sqlite3.connect(db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA busy_timeout=10000')
        RealityEngine = _load_reality_engine_class()
        return RealityEngine(conn).ingest_event(
            scope_key=scope_key or session_id or 'global',
            event_type=event_type,
            subject=subject,
            payload=payload,
            session_id=session_id,
            source=source,
            confidence=confidence,
            created_at=created_at,
        )
    except Exception:
        try:
            if conn:
                conn.rollback()
        except Exception:
            pass
        return {}
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass


def _load_reality_brief(scope_key: str, user_message: str) -> str:
    db_path = _db_path()
    if not Path(db_path).exists():
        return ''
    conn = None
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        RealityEngine = _load_reality_engine_class()
        return RealityEngine(conn).compile_brief(scope_key or 'global', user_message or '')
    except Exception:
        return ''
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass



def _load_epistemic_manager_class():
    try:
        from live_brain.epistemic import EpistemicManager
        return EpistemicManager
    except Exception:
        pass
    import importlib.util as _importlib_util
    import sys as _sys
    package_name = '_live_brain_ctx_epistemic_pkg'
    live_brain_dir = Path(__file__).resolve().parent.parent / 'live_brain'
    if package_name not in _sys.modules:
        import types as _types
        package = _types.ModuleType(package_name)
        package.__path__ = [str(live_brain_dir)]
        _sys.modules[package_name] = package
    for module_name in ['utils', 'epistemic']:
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
    return _sys.modules[f'{package_name}.epistemic'].EpistemicManager


def _load_epistemic_brief(scope_key: str, user_message: str, session_id: str = '') -> str:
    db_path = _db_path()
    if not Path(db_path).exists():
        return ''
    if _is_chit_chat(user_message or ''):
        return ''
    conn = None
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        EpistemicManager = _load_epistemic_manager_class()
        return EpistemicManager(conn, session_id=session_id, scope_key=scope_key or 'global').compile_brief(scope_key or 'global', user_message or '')
    except Exception:
        return ''
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass


_AUTHORITATIVE_EPISTEMIC_AUTHORITIES = {'official', 'primary_or_institutional', 'primary_or_support'}
_URL_RE = re.compile(r'https?://[^\s)\]}>,"\']+')
_UNVERIFIED_ANSWER_RE = re.compile(
    r"\b(?:ne\s+mogu\s+(?:da\s+)?(?:potvrdim|verifikujem)|nisam\s+prona|bez\s+izvora|"
    r"cannot\s+verify|could\s+not\s+verify|unable\s+to\s+verify|no\s+source|unverified)\b",
    re.IGNORECASE,
)


def _extract_urls_from_text(text: str) -> List[str]:
    urls: List[str] = []
    seen = set()
    for match in _URL_RE.findall(text or ''):
        clean = match.rstrip('.,;:!?')
        if clean and clean not in seen:
            seen.add(clean)
            urls.append(clean)
    return urls[:8]


def _epistemic_job_sources(conn: sqlite3.Connection, scope_key: str, job_id: str, *, limit: int = 6) -> List[Dict[str, Any]]:
    if not job_id:
        return []
    rows = conn.execute(
        """
        SELECT source_id, url, title, summary, authority, confidence, created_at
        FROM epistemic_web_sources
        WHERE scope_key=? AND job_id=? AND url!=''
        ORDER BY CASE WHEN authority='official' THEN 0 WHEN authority IN ('primary_or_institutional','primary_or_support') THEN 1 ELSE 2 END, confidence DESC, created_at DESC
        LIMIT ?
        """,
        (scope_key, job_id, int(limit)),
    ).fetchall()
    return [dict(row) for row in rows]


def _format_autonomous_research_context(search_result: Dict[str, Any], sources: List[Dict[str, Any]]) -> str:
    lines = [
        'AUTONOMOUS WEB RESEARCH:',
        '- Live Brain detected an unknown/current/high-stakes question and searched before the LLM call.',
    ]
    authoritative = [source for source in sources if source.get('authority') in _AUTHORITATIVE_EPISTEMIC_AUTHORITIES]
    chosen = authoritative or sources
    if chosen:
        for source in chosen[:4]:
            title = str(source.get('title') or '').strip()
            url = str(source.get('url') or '').strip()
            authority = str(source.get('authority') or 'unknown')
            confidence = float(source.get('confidence') or 0.0)
            label = f'{title} — {url}' if title else url
            lines.append(f'- Source: {label} ({authority}, confidence={confidence:.2f})')
    else:
        lines.append('- Search attempted, but no source was found.')
    status = str(search_result.get('status') or '')
    if status and status != 'sources_found':
        lines.append(f'- Research status: {status}; do not answer from stale memory or secondary-only evidence.')
    lines.append('- Safe rule: answer only from listed official/primary sources; if evidence is insufficient, call web_extract/web_search; do not answer from stale memory.')
    lines.append('- Evidence rule: if pages are discovered but not extracted, cite the URLs and say exact current values require the CME page/bulletin; do not invent numeric or contract-specific limits.')
    lines.append('- Persistence rule: after the final answer, Live Brain records source-backed facts automatically.')
    return '\n'.join(lines)


def _load_epistemic_autonomous_context(scope_key: str, user_message: str, session_id: str = '') -> str:
    if os.environ.get('LIVE_BRAIN_AUTONOMOUS_RESEARCH', '1') == '0':
        return ''
    db_path = _db_path()
    if not Path(db_path).exists() or _is_chit_chat(user_message or ''):
        return ''
    conn = None
    try:
        conn = sqlite3.connect(db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        EpistemicManager = _load_epistemic_manager_class()
        manager = EpistemicManager(conn, session_id=session_id, scope_key=scope_key or 'global')
        plan = manager.plan_if_needed(scope_key or 'global', user_message or '', session_id=session_id)
        if not plan.get('needs_research'):
            return ''
        job_id = str(plan.get('job_id') or manager.latest_job(scope_key or 'global', user_message or ''))
        existing_sources = _epistemic_job_sources(conn, scope_key or 'global', job_id, limit=4)
        if any(source.get('authority') in _AUTHORITATIVE_EPISTEMIC_AUTHORITIES for source in existing_sources):
            return _format_autonomous_research_context({'status': 'sources_found', 'job_id': job_id, 'discovery': 'cached'}, existing_sources)
        timeout = float(os.environ.get('LIVE_BRAIN_AUTONOMOUS_RESEARCH_TIMEOUT', '1.5'))
        max_queries = int(os.environ.get('LIVE_BRAIN_AUTONOMOUS_RESEARCH_MAX_QUERIES', '2'))
        result = manager.search_web(
            scope_key=scope_key or 'global',
            question=user_message or '',
            job_id=job_id,
            limit=4,
            max_queries=max_queries,
            timeout=timeout,
        )
        sources = list(result.get('authoritative_sources') or result.get('sources') or [])
        if not sources:
            sources = _epistemic_job_sources(conn, scope_key or 'global', job_id, limit=4)
        return _format_autonomous_research_context(result, sources)
    except Exception:
        return ''
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass


def _record_epistemic_answer_if_source_backed(scope_key: str, user_message: str, assistant_response: str, session_id: str = '') -> None:
    if os.environ.get('LIVE_BRAIN_AUTONOMOUS_LEARNING', '1') == '0':
        return
    if not user_message or not assistant_response or _UNVERIFIED_ANSWER_RE.search(assistant_response):
        return
    db_path = _db_path()
    if not Path(db_path).exists() or _is_chit_chat(user_message or ''):
        return
    conn = None
    try:
        conn = sqlite3.connect(db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        EpistemicManager = _load_epistemic_manager_class()
        manager = EpistemicManager(conn, session_id=session_id, scope_key=scope_key or 'global')
        plan = manager.plan_if_needed(scope_key or 'global', user_message, session_id=session_id)
        job_id = str(plan.get('job_id') or manager.latest_job(scope_key or 'global', user_message))
        if not job_id:
            return
        job_sources = _epistemic_job_sources(conn, scope_key or 'global', job_id, limit=8)
        authoritative_sources = [source for source in job_sources if source.get('authority') in _AUTHORITATIVE_EPISTEMIC_AUTHORITIES]
        known_urls = [str(source.get('url') or '') for source in authoritative_sources if source.get('url')]
        answer_urls = _extract_urls_from_text(assistant_response)
        if answer_urls:
            known_domains = {url.split('/')[2].lower().removeprefix('www.') for url in known_urls if url.startswith('http') and len(url.split('/')) > 2}
            source_urls = []
            for url in answer_urls:
                parts = url.split('/')
                domain = parts[2].lower().removeprefix('www.') if len(parts) > 2 else ''
                if not known_domains or domain in known_domains or any(domain.endswith('.' + item) or item.endswith('.' + domain) for item in known_domains):
                    source_urls.append(url)
        else:
            answer_lower = assistant_response.lower()
            source_urls = [url for url in known_urls if url.split('/')[2].lower().removeprefix('www.') in answer_lower] if known_urls else []
        if not source_urls and authoritative_sources and re.search(r'\b(source|sources|izvor|izvori)\b', assistant_response, re.IGNORECASE):
            source_urls = known_urls[:3]
        if not source_urls:
            return
        ttl_seconds = int(plan.get('ttl_seconds') or 24 * 3600)
        confidence = 0.84 if answer_urls else 0.78
        fact_text = re.sub(r'\s+', ' ', assistant_response).strip()[:800]
        manager.record_fact(
            scope_key=scope_key or 'global',
            question=user_message,
            job_id=job_id,
            fact_text=fact_text,
            source_urls=source_urls[:5],
            confidence=confidence,
            ttl_seconds=ttl_seconds,
        )
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


def _should_load_reality_brief(user_message: str) -> bool:
    lowered = (user_message or '').strip().lower()
    if not lowered:
        return False
    if lowered in {'ok', 'da', 'ne', 'hmm', 'hm'}:
        return False
    if lowered in {'to', 'ovo', 'taj', 'ta', 'uradi to', 'a link', 'a link?'}:
        return True
    return not _is_chit_chat(user_message or '')


def _should_isolate_epistemic_context(user_message: str) -> bool:
    lowered = (user_message or '').strip().lower()
    if not lowered or _is_chit_chat(lowered):
        return False
    if 'live_brain_capability_e2e research' in lowered:
        return True
    current_terms = (
        'latest', 'current', 'today', 'now', 'najnovij', 'aktueln', 'trenutn',
        'danas', 'sada', 'sad', 'source url', 'authoritative', 'official source',
        'zvanič', 'zvanic', 'izvor', 'izvore',
    )
    high_stakes_terms = (
        'price limit', 'price limits', 'cme', 'nq', 'nasdaq', 'futures', 'trading',
        'funded account', 'broker', 'margin', 'risk', 'rulebook', 'pravila',
    )
    return any(term in lowered for term in current_terms) and any(term in lowered for term in high_stakes_terms)


def _epistemic_query_text(user_message: str) -> str:
    text = user_message or ''
    text = re.sub(r'\bLIVE_BRAIN_CAPABILITY_E2E\s+research\s+run[-_][a-z0-9]+\s*:\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\brun[-_][a-z0-9]+\b', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\bcodename[-_][a-z0-9]+\b', '', text, flags=re.IGNORECASE)
    return re.sub(r'\s+', ' ', text).strip() or (user_message or '')

def _load_live_brain_ingestor_class():
    try:
        from live_brain.ingest import Ingestor
        return Ingestor
    except Exception:
        pass
    import importlib.util as _importlib_util
    import sys as _sys
    package_name = '_live_brain_ctx_live_brain'
    live_brain_dir = Path(__file__).resolve().parent.parent / 'live_brain'
    if package_name not in _sys.modules:
        import types as _types
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


def _record_tool_result(tool_name: str, args: Any, result: Any, session_id: str = '', tool_call_id: str = '', duration_ms: int | None = None) -> None:
    db_path = _db_path()
    if not tool_name or not Path(db_path).exists():
        return
    conn = None
    try:
        created_at = time.time()
        conn = sqlite3.connect(db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA busy_timeout=10000')
        try:
            duration_ms = max(0, int(duration_ms or 0))
        except (TypeError, ValueError):
            duration_ms = 0
        scope_key, user_text = _latest_tool_context(conn, session_id, created_at)
        if not scope_key:
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
        conn = sqlite3.connect(db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        scope_key, user_text = _latest_tool_context(conn, session_id, time.time())
        if not user_text:
            args = kwargs.get('args') if isinstance(kwargs.get('args'), dict) else {}
            user_text = str(args.get('query') or args.get('q') or '')
        if not user_text:
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
    impression_id = 'impression:' + hashlib.sha256(f'{scope_key}{session_id}{user_message}{context_hash}{int(now)}'.encode()).hexdigest()[:24]
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
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
    scope_key = _extract_scope_key(user_message, sender_id, session_id)
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
    _record_context_impression(scope_key, session_id, user_message, context, list(_LAST_CONTEXT_METADATA.get('recipe_ids') or []))
    return {"context": context}


def _post_llm_call(**kwargs):
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
            conn = sqlite3.connect(db_path, timeout=5.0)
            row = conn.execute(
                "SELECT scope_key FROM context_impressions WHERE session_id=? AND created_at >= ? ORDER BY created_at DESC LIMIT 1",
                (session_id, created_at - 1800),
            ).fetchone()
            if row:
                scope_key = str(row[0] or '')
            conn.close()
            conn = None
    except Exception:
        try:
            if conn:
                conn.close()
        except Exception:
            pass
    if not scope_key:
        scope_key = _extract_scope_key(user_message, '', session_id)
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


def register(ctx) -> None:
    ctx.register_context_engine(LiveBrainContextEngine(model="", quiet_mode=True, config_context_length=200000))
    ctx.register_hook("pre_llm_call", _pre_llm_call)
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_hook("post_tool_call", _post_tool_call)
    ctx.register_hook("post_llm_call", _post_llm_call)
