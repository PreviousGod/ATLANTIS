"""Query classification and text filtering helpers for live_brain_ctx.

Pure functions that classify user messages and memory text. No DB access,
no side effects — only regex/string matching against the shared state constants.
"""
from __future__ import annotations

import re
from typing import List

from . import state
from .text_processing import is_noisy_episode_memory


def _is_review_only_query(text: str) -> bool:
    lowered = (text or "").lower()
    if any(marker in lowered for marker in (
        'review only', 'samo ocena', 'samo oceni', 'samo analiz',
        'ne pravi proposal', 'ne traži approval', 'ne trazi approval',
    )):
        return True
    review_terms = [t for t in state.REVIEW_ONLY_TERMS if t not in {'review', 'pregled'}]
    return (
        any(t in lowered for t in review_terms)
        and not any(t in lowered for t in state.CHANGE_INTENT_TERMS)
    )


def _is_non_action_work_item_text(text: str) -> bool:
    lowered = re.sub(r'\s+', ' ', (text or '').strip().lower())
    if not lowered:
        return True
    if state.META_WORK_ITEM_RE.search(lowered):
        return True
    if _is_review_only_query(lowered):
        return True
    if _is_question_like_memory(lowered):
        return not any(alias in lowered for alias in state.MUSIC_MEMORY_ALIASES)
    inquiry_markers = (
        'reci mi', 'navedi', 'objasni', 'tell me', 'explain',
        'šta ne radi', 'sta ne radi', 'šta fali', 'sta fali',
    )
    return (
        any(marker in lowered for marker in inquiry_markers)
        and not any(t in lowered for t in state.CHANGE_INTENT_TERMS)
    )


def _is_local_stack_query(text: str) -> bool:
    lowered = (text or "").lower()
    return any(t in lowered for t in [
        "telegram", "vision", "image", "analyzer", "gateway", "ffmpeg", "plugin", "memory",
    ])


def _is_chit_chat(text: str) -> bool:
    from .query_classification import _is_chit_chat as _module_is_chit_chat
    return _module_is_chit_chat(text, state.CHIT_CHAT_PATTERNS)


def _is_continuation_query(text: str) -> bool:
    return bool(state.CONTINUATION_QUERY_RE.search(text or ''))


def _is_question_like_memory(text: str) -> bool:
    lowered = (text or '').strip().lower()
    if lowered.endswith('?'):
        return True
    question_starters = (
        'šta ', 'sta ', 'što ', 'sto ', 'koji ', 'koja ', 'koje ', 'kako ',
        'zašto ', 'zasto ', 'gde ', 'gdje ', 'what ', 'how ', 'why ', 'where ',
        'which ', 'when ', 'who ', 'can you ', 'could you ', 'da li ',
    )
    return any(lowered.startswith(s) for s in question_starters)


def _is_destructive_memory_text(text: str) -> bool:
    return bool(state.DESTRUCTIVE_MEMORY_RE.search(text or ''))


def _current_turn_allows_destructive_memory(text: str) -> bool:
    if not state.DESTRUCTIVE_MEMORY_RE.search(text or ''):
        return False
    if state.NEGATED_DESTRUCTIVE_RE.search(text or ''):
        return False
    return True


def _expand_query_words(words: List[str]) -> List[str]:
    expanded: List[str] = []
    for word in words:
        value = (word or '').lower().strip('.,!?;:…')
        if not value or value in state.LOW_SIGNAL_WORDS or value in state.RECALL_QUERY_WORDS:
            continue
        expanded.append(value)
        if value.startswith(('pesm', 'pjesm')) or value in {'song', 'songs', 'music', 'muzika', 'muziku', 'cover'}:
            expanded.extend(state.MUSIC_MEMORY_ALIASES)
        elif value == 'suno':
            expanded.extend(('suno', *state.MUSIC_MEMORY_ALIASES))
        elif value.startswith(('muzik', 'triler', 'flamenco', 'serbez', 'esmeralda')):
            expanded.extend(state.MUSIC_MEMORY_ALIASES)
    deduped: List[str] = []
    seen: set = set()
    for value in expanded:
        if value and value not in seen:
            seen.add(value)
            deduped.append(value)
    return deduped


def _meaningful_query_words(words: List[str]) -> List[str]:
    return _expand_query_words(words)


def _is_low_signal_episode(
    title: str,
    summary: str,
    episode_id: str = '',
    session_id: str = '',
    conn=None,
) -> bool:
    if episode_id and session_id and conn:
        try:
            query_count = conn.execute(
                "SELECT COUNT(*) FROM episode_queries WHERE episode_id = ? AND session_id = ?",
                (episode_id, session_id),
            ).fetchone()[0]
            if query_count > 2:
                return True
        except Exception:
            pass

    if is_noisy_episode_memory(title, summary):
        return True
    text = (summary or '').strip()
    upper = text.upper()
    if upper.startswith('SCOPE:') and 'PROBLEM:' in upper and 'FIX:' not in upper and 'ROOT' not in upper:
        return True
    if upper.startswith('SCOPE:') and 'PROBLEM:' in upper and 'FIX:' in upper:
        fix_text = text.upper().split('FIX:', 1)[1].strip()
        useful_tokens = (
            'TOOL', 'FILE', 'PATH', 'COMMAND', 'RUN', 'USE ', 'ADD ', 'SET ',
            'VERIFY', 'IMAGE_GENERATE', 'FFMPEG',
        )
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
    if state.SYNTHETIC_MEMORY_RE.search(text):
        return True
    if is_noisy_episode_memory(text, text):
        return True
    if state.NOISY_MEMORY_RE.search(text):
        return True
    if state.LOW_VALUE_FACT_RE.search(text):
        return True
    lowered = text.lower().strip()
    if lowered.startswith(('[note:', '[system note:', '## summary', '###')):
        return True
    if len(text) > 300 and ('\n' in text or '###' in text or '```' in text or '|' in text):
        return True
    if text.count('\n') >= 2:
        return True
    return False
