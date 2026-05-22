"""Query type detection for live_brain_ctx."""

from __future__ import annotations

import re


QUERY_INTENTS = (
    "chit_chat",
    "continuity_recap",
    "task_execution",
    "local_repo_lookup",
    "approval_flow",
)

TURN_LANES = (
    "document_intake",
    "simple_execution",
    "deep_execution",
    "research_or_epistemic",
    "continuation_or_resume",
)


def is_low_signal_thread_title(title: str) -> bool:
    """Check if thread title is low signal."""
    return _normalize_chat_text(title) in {
        'da', 'ne', 'ok', 'okej', 'hmm', 'hm', 'yes', 'no', 'continue', 'nastavi',
        'cekaj', 'čekaj', 'naravno', 'moze', 'može', 'vazi', 'važi', 'ajde', 'dobro',
    }


def _normalize_chat_text(text: str) -> str:
    lowered = (text or '').strip().lower()
    lowered = re.sub(r'[.!?,;:]+', ' ', lowered)
    return re.sub(r'\s+', ' ', lowered).strip()


def _looks_like_short_greeting(text: str, chit_chat_patterns: set[str]) -> bool:
    normalized = _normalize_chat_text(text)
    if not normalized:
        return True
    tokens = [token for token in normalized.split() if token]
    if len(tokens) > 3:
        return False
    greeting_tokens = {
        'cao', 'ćao', 'ciao', 'zdravo', 'hello', 'hi', 'hey', 'hej', 'yo',
        'e', 'ej', 'ee', 'ok', 'okej', 'hm', 'hmm',
    }
    if all(token in greeting_tokens or token in chit_chat_patterns for token in tokens):
        return True
    if len(tokens) <= 2 and any(token in {'cao', 'ćao', 'hello', 'hi', 'hey', 'hej'} for token in tokens):
        return True
    return False


def _looks_like_low_signal_followup(text: str) -> bool:
    normalized = _normalize_chat_text(text)
    if not normalized:
        return False
    tokens = [token for token in normalized.split() if token]
    followup_tokens = {
        'gotovo', 'znaci', 'znači', 'dakle', 'okej', 'ok', 'aha',
        'dobro', 'continue', 'nastavi', 'next', 'dalje',
    }
    if len(tokens) <= 3 and all(token in followup_tokens for token in tokens):
        return True
    return False


def _is_recap_query(text: str) -> bool:
    lowered = (text or "").lower()
    return any(t in lowered for t in ["sumarizuj", "sta si radio", "what did you do", "recap", "pregled"])


def _is_diagnostic_query(text: str) -> bool:
    lowered = (text or "").lower()
    return any(t in lowered for t in ["error", "bug", "problem", "fails", "ne radi", "root cause", "uzrok"])


_NEGATED_APPROVAL_MENTION_RE = re.compile(
    r"(?:ne\s+pominj\w*|nemoj\s+pominj\w*|bez\s+pominjanja|do\s+not\s+mention|don't\s+mention)[^.!?\n]{0,80}\bapproval\b",
    re.IGNORECASE,
)


def _is_approval_query(text: str) -> bool:
    lowered = (text or "").lower()
    if any(t in lowered for t in ["self-evol", "self evolution", "self evolving"]):
        return True
    if any(t in lowered for t in ["approve", "odobri", "odobren", "pending"]):
        return True
    if "approval" not in lowered:
        return False
    if _NEGATED_APPROVAL_MENTION_RE.search(lowered):
        return False
    return True


def _is_local_stack_query(text: str) -> bool:
    lowered = (text or "").lower()
    return any(t in lowered for t in ["telegram", "vision", "image", "analyzer", "gateway", "ffmpeg", "plugin", "memory"])


def _looks_like_document_intake(text: str) -> bool:
    lowered = (text or '').lower()
    return (
        lowered.startswith('[the user sent a document:')
        or ('the file is saved at:' in lowered and '.pdf' in lowered)
        or ('skini ocr' in lowered and '.pdf' in lowered)
        or ('skeniraj tekst sa pdf' in lowered)
    )


def _strip_transport_metadata(text: str) -> tuple[str, dict]:
    raw = (text or '').strip()
    meta = {
        'had_system_note': False,
        'had_interruption_note': False,
        'transport_note': '',
    }
    if not raw.startswith('[System note:'):
        return raw, meta
    match = re.match(r'^\[(System note:[^\]]+)\]\s*(.*)$', raw, re.DOTALL)
    if not match:
        return raw, meta
    note = match.group(1).strip()
    remainder = (match.group(2) or '').strip()
    meta['had_system_note'] = True
    meta['transport_note'] = note
    if 'interrupted before you could process the last tool result' in note.lower() or 'previous turn in this session was interrupted' in note.lower():
        meta['had_interruption_note'] = True
    return remainder or raw, meta


def _is_chit_chat(text: str, chit_chat_patterns: set) -> bool:
    normalized = _normalize_chat_text(text)
    return (
        normalized in chit_chat_patterns
        or is_low_signal_thread_title(normalized)
        or _looks_like_short_greeting(normalized, chit_chat_patterns)
        or len(normalized) < 5
    )


def _classify_query_intent(text: str, *, chit_chat_patterns: set) -> str:
    """Map each query into one context policy bucket so section gating stays centralized."""
    stripped, _meta = _strip_transport_metadata(text)
    lowered = _normalize_chat_text(stripped)
    if _is_approval_query(lowered):
        return "approval_flow"
    if _looks_like_low_signal_followup(lowered):
        return "continuity_recap"
    if _is_recap_query(lowered):
        return "continuity_recap"
    if _is_local_stack_query(lowered):
        return "local_repo_lookup"
    if _is_diagnostic_query(lowered):
        return "task_execution"
    if _is_chit_chat(lowered, chit_chat_patterns):
        return "chit_chat"

    # Repo-ish entity lookups should stay factual instead of inheriting active task state.
    repo_lookup_markers = (
        "file", "fajl", "files", "plugin.yaml", "path", "putanja", "repo", "repository",
        "code", "kod", "folder", "directory", "direktorijum", "where is", "gde je",
        "koji fajl", "which file", "which files", "search", "find", "rg ", "grep ",
    )
    if any(marker in lowered for marker in repo_lookup_markers):
        return "local_repo_lookup"

    # Continuation words route to recap-style context instead of full task execution.
    continuity_markers = (
        "sta si radio", "šta si radio", "sta smo radili", "šta smo radili", "today", "danas",
        "gde smo stali", "gdje smo stali", "where were we", "nastavi", "continue",
    )
    if any(marker in lowered for marker in continuity_markers):
        return "continuity_recap"

    return "task_execution"


def classify_turn_lane(
    text: str,
    *,
    chit_chat_patterns: set,
    platform: str = '',
    has_fresh_resume: bool = False,
) -> tuple[str, dict]:
    stripped, meta = _strip_transport_metadata(text)
    normalized = _normalize_chat_text(stripped)
    intent = _classify_query_intent(stripped, chit_chat_patterns=chit_chat_patterns)
    lane_meta = {
        'normalized_message': normalized,
        'semantic_message': stripped,
        'intent': intent,
        **meta,
    }
    if _looks_like_document_intake(stripped):
        lane = 'document_intake'
    elif _is_chit_chat(normalized, chit_chat_patterns):
        lane = 'simple_execution'
    elif has_fresh_resume and (_looks_like_low_signal_followup(normalized) or any(marker in normalized for marker in ('nastavi', 'continue', 'ajde', 'dalje'))):
        lane = 'continuation_or_resume'
    elif any(marker in normalized for marker in (
        'latest', 'most recent', 'najnovija', 'najnovije', 'source', 'sources',
        'izvor', 'izvori', 'pravila', 'rules', 'news', 'vesti', 'research',
        'istra', 'authoritative', 'fresh',
    )):
        lane = 'research_or_epistemic'
    elif intent == 'continuity_recap':
        lane = 'continuation_or_resume'
    elif intent == 'local_repo_lookup':
        lane = 'simple_execution'
    elif any(marker in normalized for marker in (
        'fix', 'debug', 'error', 'bug', 'ne radi', 'root cause', 'uzrok',
        'traceback', 'fails', 'failure', 'architect', 'redesign', 'migrate',
        'compare', 'analyze', 'analiz', 'investigate',
    )):
        lane = 'deep_execution'
    else:
        lane = 'simple_execution'
    lane_meta['turn_lane'] = lane
    return lane, lane_meta
