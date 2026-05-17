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


def is_low_signal_thread_title(title: str) -> bool:
    """Check if thread title is low signal."""
    return re.sub(r'\s+', ' ', (title or '').strip().lower()).strip(' .,!?:;') in {
        'da', 'ne', 'ok', 'okej', 'hmm', 'hm', 'yes', 'no', 'continue', 'nastavi',
        'cekaj', 'čekaj', 'naravno', 'moze', 'može', 'vazi', 'važi', 'ajde', 'dobro',
    }


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
    repo_artifact_markers = (
        "plugin.yaml", "plugin yml", "manifest", "repo", "repository",
        "path", "putanja", "directory", "direktorijum", "folder",
        "which file", "which files", "koji fajl", "koji fajlovi",
        "gde je", "gdje je", "where is", "find", "search", "grep ", "rg ",
    )
    stack_subjects = (
        "telegram", "vision", "image", "analyzer", "gateway", "ffmpeg", "plugin", "memory",
    )
    return (
        any(marker in lowered for marker in repo_artifact_markers)
        and any(subject in lowered for subject in stack_subjects)
    )


def _is_chit_chat(text: str, chit_chat_patterns: set) -> bool:
    lowered = (text or "").strip().lower()
    return lowered in chit_chat_patterns or is_low_signal_thread_title(lowered) or len(lowered) < 5


def _classify_query_intent(text: str, *, chit_chat_patterns: set) -> str:
    """Map each query into one context policy bucket so section gating stays centralized."""
    lowered = (text or "").strip().lower()
    if _is_approval_query(lowered):
        return "approval_flow"
    if _is_recap_query(lowered):
        return "continuity_recap"
    if _is_diagnostic_query(lowered):
        return "task_execution"
    if _is_chit_chat(lowered, chit_chat_patterns):
        return "chit_chat"
    if _is_local_stack_query(lowered):
        return "local_repo_lookup"

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
