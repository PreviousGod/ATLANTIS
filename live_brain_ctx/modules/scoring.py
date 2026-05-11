"""Scoring and overlap helpers for live_brain_ctx memory retrieval.

Functions extracted from the monolithic __init__.py that compute relevance
scores, detect domain/marker conflicts, and filter rows by overlap with the
active query.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Dict, List

from .state import (
    MEDIA_PROJECT_MEMORY_RE,
    MEDIA_PROJECT_QUERY_RE,
    MUSIC_DOMAIN_RE,
    OPEN_LOOP_FACT_RE,
    OPEN_LOOP_QUERY_RE,
    PATH_CONFIG_QUERY_RE,
    PATH_CONFIG_RE,
    RAW_TOOL_FACT_RE,
    RAW_TOOL_QUERY_RE,
    RUN_MARKER_RE,
    VOICE_TTS_DOMAIN_RE,
    REVIEW_ONLY_TERMS,
    CHANGE_INTENT_TERMS,
)
from .tag_matching import _row_tags
from .query_filters import _meaningful_query_words
from .text_processing import _is_noisy_memory, _truncate_fact

try:
    from live_brain.scopes_config import IMAGE_GENERATION_ALIASES
except Exception:
    IMAGE_GENERATION_ALIASES = ('seedream', 'bytedance-seed')


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_MUSIC_DOMAIN_RE = MUSIC_DOMAIN_RE
_MEDIA_PROJECT_MEMORY_RE = MEDIA_PROJECT_MEMORY_RE
_MEDIA_PROJECT_QUERY_RE = MEDIA_PROJECT_QUERY_RE
_VOICE_TTS_DOMAIN_RE = VOICE_TTS_DOMAIN_RE
_PATH_CONFIG_RE = PATH_CONFIG_RE
_PATH_CONFIG_QUERY_RE = PATH_CONFIG_QUERY_RE
_RAW_TOOL_FACT_RE = RAW_TOOL_FACT_RE
_RAW_TOOL_QUERY_RE = RAW_TOOL_QUERY_RE
_OPEN_LOOP_FACT_RE = OPEN_LOOP_FACT_RE
_OPEN_LOOP_QUERY_RE = OPEN_LOOP_QUERY_RE
_RUN_MARKER_RE = RUN_MARKER_RE
_REVIEW_ONLY_TERMS = REVIEW_ONLY_TERMS
_CHANGE_INTENT_TERMS = CHANGE_INTENT_TERMS


def _is_review_only_query(text: str) -> bool:
    lowered = (text or "").lower()
    if any(marker in lowered for marker in ('review only', 'samo ocena', 'samo oceni', 'samo analiz', 'ne pravi proposal', 'ne traži approval', 'ne trazi approval')):
        return True
    review_terms = [term for term in _REVIEW_ONLY_TERMS if term not in {'review', 'pregled'}]
    return any(term in lowered for term in review_terms) and not any(term in lowered for term in _CHANGE_INTENT_TERMS)


# ---------------------------------------------------------------------------
# Extracted scoring functions
# ---------------------------------------------------------------------------


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
    query_is_music = bool(_MUSIC_DOMAIN_RE.search(query_text))
    row_is_music = bool(_MUSIC_DOMAIN_RE.search(row_text))
    if _MEDIA_PROJECT_MEMORY_RE.search(row_text) and not _MEDIA_PROJECT_QUERY_RE.search(query_text):
        return True
    if row_is_music and not query_is_music:
        return True
    if _VOICE_TTS_DOMAIN_RE.search(row_text) and not _VOICE_TTS_DOMAIN_RE.search(query_text) and not (query_is_music and row_is_music):
        return True
    if _PATH_CONFIG_RE.search(row_text) and not _PATH_CONFIG_QUERY_RE.search(query_text):
        return True
    if _RAW_TOOL_FACT_RE.search(row_text) and not _RAW_TOOL_QUERY_RE.search(query_text):
        return True
    if _OPEN_LOOP_FACT_RE.search(row_text) and (_is_review_only_query(query_text) or not _OPEN_LOOP_QUERY_RE.search(query_text)):
        return True
    if 'live_brain_capability_e2e' in query_lower:
        query_markers = _marker_tokens(query_text)
        row_markers = _marker_tokens(row_text)
        old_blocker = re.search(r'\b(?:production\s+blocker|operating\s+contract|observer\s+result|normal\s+human\s+chat|verify\s+blocker)\b', row_text, re.IGNORECASE)
        if old_blocker and (not query_markers or not row_markers or query_markers.isdisjoint(row_markers)):
            return True
    return False


def _overlap_score(row: sqlite3.Row, query_words: List[str], fields: List[str]) -> int:
    words = _meaningful_query_words(query_words)
    if not words:
        return 1
    text = _row_text(row, fields)
    query_text = ' '.join(query_words)
    if _marker_conflicts(query_text, text) or _domain_conflicts(query_text, text):
        return 0
    if any(alias in words for alias in IMAGE_GENERATION_ALIASES):
        return 1 if any(alias in text for alias in IMAGE_GENERATION_ALIASES) else 0
    if 'ffmpeg' in words:
        return 1 if 'ffmpeg' in text else 0
    score = sum(1 for word in words if word in text)
    if score <= 0:
        return 0
    strong_words = [word for word in words if len(word) >= 5 and word not in {'live', 'context', 'system', 'sistem', 'sistema', 'plugin', 'brain', 'review', 'pregled', 'analiziraj', 'analiza', 'analyze', 'analysis', 'oceni', 'ocena', 'rate', 'rating', 'score'}]
    if strong_words and not any(word in text for word in strong_words):
        return 0
    return score


def _has_overlap(row: sqlite3.Row, query_words: List[str], fields: List[str]) -> bool:
    return _overlap_score(row, query_words, fields) > 0


def _row_updated_at(row: sqlite3.Row) -> float:
    try:
        return float(row['updated_at'] or 0)
    except Exception:
        return 0.0


def _same_user_message(row: sqlite3.Row, user_message: str, fields: List[str]) -> bool:
    needle = re.sub(r'\W+', ' ', (user_message or '').lower()).strip()
    if not needle:
        return False
    for field in fields:
        try:
            value = re.sub(r'\W+', ' ', str(row[field] or '').lower()).strip()
        except Exception:
            value = ''
        if value and (value == needle or value.startswith(needle[:120]) or needle.startswith(value[:120])):
            return True
    return False


def _specificity_score(row: sqlite3.Row) -> int:
    tags = _row_tags(row)
    return sum(len(values or []) for values in tags.values())


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
