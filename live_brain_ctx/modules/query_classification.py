"""Query type detection for live_brain_ctx."""

import re


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
    return any(t in lowered for t in ["telegram", "vision", "image", "analyzer", "gateway", "ffmpeg", "plugin", "memory"])


def _is_chit_chat(text: str, chit_chat_patterns: set) -> bool:
    lowered = (text or "").strip().lower()
    return lowered in chit_chat_patterns or is_low_signal_thread_title(lowered) or len(lowered) < 5
