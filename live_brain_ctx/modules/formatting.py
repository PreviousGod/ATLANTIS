"""Formatting helpers for live_brain_ctx context output.

Functions extracted from the monolithic __init__.py that filter, deduplicate,
and format memory rows into the text sections injected into the LLM context.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Dict, List, Set, Tuple

from .state import (
    SECTION_LIMITS,
    SECRET_RE,
    MEDIA_DOMAIN_WORDS,
    SYNTHETIC_MEMORY_RE,
    MUSIC_MEMORY_ALIASES,
    LOW_SIGNAL_WORDS,
    DESTRUCTIVE_MEMORY_RE,
    NEGATED_DESTRUCTIVE_RE,
)
from .scoring import (
    _has_overlap,
    _overlap_score,
    _row_noisy,
    _row_text,
    _row_updated_at,
    _same_user_message,
    _marker_conflicts,
    _domain_conflicts,
    _causal_score,
)
from .tag_matching import _matches, _causal_matches
from .query_filters import (
    _is_low_signal_episode,
    _meaningful_query_words,
    _is_continuation_query,
    _is_question_like_memory,
    _is_destructive_memory_text,
    _current_turn_allows_destructive_memory,
)
from .query_classification import _is_chit_chat
from .tool_context import _recipe_hint, _tool_relevant, _artifact_required, _default_success_for_tool
from .text_processing import _truncate_fact, _redact, _is_noisy_memory


# Local aliases matching monolith naming
_SECTION_LIMITS = SECTION_LIMITS
_SECRET_RE = SECRET_RE
_MUSIC_MEMORY_ALIASES = MUSIC_MEMORY_ALIASES
_SYNTHETIC_MEMORY_RE = SYNTHETIC_MEMORY_RE
_LOW_SIGNAL_WORDS = LOW_SIGNAL_WORDS

_INTENT_SECTION_ALLOWLIST: Dict[str, Set[str]] = {
    # Plain chat should stay empty; otherwise every greeting drags old state back in.
    'chit_chat': set(),
    # Recap intent needs continuity, not execution noise.
    'continuity_recap': {
        'PENDING APPROVAL', 'APPROVAL ROUTING', 'MUST FOLLOW', 'ACTIVE TASK',
        'CONTINUITY MEMORY', 'RECENT EPISODES', 'KNOWN FACTS', 'LATEST RECAP',
    },
    # Execution/debugging can use the full operational context.
    'task_execution': {
        'MUST FOLLOW', 'VERIFIED ARTIFACTS',
        'PROVEN FIX', 'KNOWN FACTS', 'ACTIVE TASK', 'CONTINUITY MEMORY',
        'RECENT EPISODES', 'OPEN BUG', 'NEXT REQUIRED ACTION', 'DIAGNOSTIC RULE',
    },
    # Local repo lookups should stay factual and file-oriented.
    'local_repo_lookup': {
        'MUST FOLLOW', 'VERIFIED ARTIFACTS',
        'PROVEN FIX', 'KNOWN FACTS',
    },
    # Approval queries should expose only approval routing plus the minimum blocking context.
    'approval_flow': {'PENDING APPROVAL', 'APPROVAL ROUTING'},
}

_INTENT_SECTION_BUDGETS: Dict[str, int] = {
    'chit_chat': 0,
    'continuity_recap': 4,
    'task_execution': 6,
    'local_repo_lookup': 4,
    'approval_flow': 2,
}


def allowed_sections_for_intent(intent: str) -> Set[str]:
    """Single source of truth for section gating by intent."""
    return set(_INTENT_SECTION_ALLOWLIST.get(intent, _INTENT_SECTION_ALLOWLIST['task_execution']))


def section_budget_for_intent(intent: str) -> int:
    """Keep low-signal intents short even when the database has a lot of matching rows."""
    return int(_INTENT_SECTION_BUDGETS.get(intent, _INTENT_SECTION_BUDGETS['task_execution']))


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


def _format_episodes(
    episode_rows: List[sqlite3.Row],
    qctx,
    user_message: str,
    conn=None
) -> List[str]:
    """Filter and format episode lines for output."""
    useful_episodes = []
    for r in episode_rows:
        if _is_chit_chat(r['title']) or not r['current_summary']:
            continue
        if not _matches(r, qctx.active_tags, qctx.scope_key):
            continue
        if qctx.continuation_query and (_same_user_message(r, user_message or '', ['title']) or _is_continuation_query(_row_text(r, ['title', 'current_summary'])) or (_is_question_like_memory(str(r['title'] or '')) and not any(alias in str(r['title'] or '').lower() for alias in _MUSIC_MEMORY_ALIASES))):
            continue
        if _SYNTHETIC_MEMORY_RE.search(_row_text(r, ['title', 'current_summary'])):
            continue
        title_overlap = _has_overlap(r, qctx.query_words, ['title'])
        full_overlap = _has_overlap(r, qctx.query_words, ['title', 'current_summary'])
        if not (title_overlap or full_overlap):
            continue
        if _is_low_signal_episode(r['title'], r['current_summary'], r.get('episode_id', ''), qctx.session_id, conn):
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

        # Record episode query to track re-activation loops
        if conn and r.get('episode_id') and qctx.session_id:
            try:
                import time
                conn.execute(
                    "INSERT INTO episode_queries (episode_id, session_id, queried_at) VALUES (?, ?, ?)",
                    (r['episode_id'], qctx.session_id, time.time())
                )
                conn.commit()
            except Exception:
                pass  # Ignore errors in query tracking

        useful_episodes.append((r, noisy_summary))

    ep_lines = []
    if useful_episodes:
        if qctx.continuation_query:
            useful_episodes.sort(key=lambda item: (_overlap_score(item[0], qctx.query_words, ['title', 'current_summary']), _row_updated_at(item[0])), reverse=True)
        for r, noisy_summary in useful_episodes:
            title = (r['title'] or '')[:60]
            if noisy_summary:
                ep_lines.append(title)
            else:
                ep_lines.append(f"{title}: {(r['current_summary'] or '')[:80]}")

    return ep_lines


def _format_fix_recipes(
    recipe_rows: List[sqlite3.Row],
    causal_rows: List[sqlite3.Row],
    qctx
) -> Tuple[List[str], List[str]]:
    """Filter and format fix recipes/causal activations, return (hints, recipe_ids)."""
    recipe_hints = []
    selected_recipe_ids: List[str] = []

    if recipe_rows:
        recipe_candidates = []
        for r in recipe_rows:
            if not r['tool_name'] or not _tool_relevant(r['tool_name'], qctx.active_tags, qctx.query_lower) or not _causal_matches(r, qctx.active_tags, qctx.scope_key):
                continue
            recipe_candidates.append((_causal_score(r, qctx.active_tags), r))
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
    elif causal_rows:
        candidates = []
        for r in causal_rows:
            if not r['tool_used'] or not _tool_relevant(r['tool_used'], qctx.active_tags, qctx.query_lower) or not _causal_matches(r, qctx.active_tags, qctx.scope_key):
                continue
            if _artifact_required(r['tool_used']) and not int(r['artifact_verified'] or 0):
                continue
            candidates.append((_causal_score(r, qctx.active_tags), r))
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
        recipe_hints = hints

    return (recipe_hints, selected_recipe_ids)


def _format_binding_constraints(
    binding_rules: List[sqlite3.Row],
    qctx,
    user_message: str
) -> List[str]:
    """Filter and format binding constraints for output."""
    constraints = []
    for r in binding_rules:
        try:
            if not _matches(r, qctx.active_tags, qctx.scope_key):
                continue
            action = json.loads(r['action_json'])
            instruction = action.get('instruction', '')
            if not instruction or _is_noisy_memory(instruction):
                continue
            instruction_lower = instruction.lower()
            if _is_destructive_memory_text(instruction) and not _current_turn_allows_destructive_memory(user_message or ''):
                continue
            if _marker_conflicts(qctx.query_lower, instruction_lower):
                continue
            if _domain_conflicts(qctx.query_lower, instruction_lower):
                continue
            instr_words = [w for w in re.findall(r'[\w./-]+', instruction_lower) if len(w) > 4 and w not in _LOW_SIGNAL_WORDS]
            if instr_words and any(w in qctx.query_lower for w in instr_words[:10]):
                constraints.append(_truncate_fact(instruction))
        except Exception:
            pass
    return constraints
