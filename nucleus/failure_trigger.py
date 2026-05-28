"""Epistemic-gap triggers for Nucleus background research."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .domain_profiles import detect_scope
from .researcher import research_problem

log = logging.getLogger("nucleus")

_MIN_INTERVAL_SECONDS = 600
_MAX_PENDING = 3
_MAX_PROBLEM_CHARS = 700

_EXECUTOR = None
_LOCK = threading.Lock()
_PENDING: set[str] = set()
_LAST_STARTED: dict[str, float] = {}
_PENDING_BY_SESSION: dict[str, list[dict]] = {}
_COMPLETED_BY_SESSION: dict[str, list[dict]] = {}

_GAP_PATTERNS = (
    "i don't know", "i do not know", "i'm not sure", "i am not sure",
    "i don't have enough", "i do not have enough", "not enough information",
    "cannot determine", "can't determine", "unable to determine",
    "i can't verify", "i cannot verify", "i don't have access",
    "need more context", "would need more", "i'm unable to",
    "ne znam", "nisam siguran", "nisam sigurna", "nemam dovoljno",
    "ne mogu da utvrdim", "ne mogu da proverim", "treba mi vise konteksta",
    "treba mi više konteksta",
)

_TECH_HINTS = (
    "hermes", "nucleus", "live brain", "live_brain", "plugin", "gateway",
    "telegram", "hook", "run_agent", "model_tools", "tool", "tools",
    "error", "exception", "traceback", "failed", "failure", "bug", "crash",
    "timeout", "sqlite", "database", "db", "schema", "migration",
    "systemd", "service", "linux", "cpu", "ram", "memory", "disk", "port",
    "config", "yaml", "api", "provider", "docs", "documentation", "github",
)

_TOOL_ERROR_PATTERNS = (
    "error", "exception", "traceback", "failed", "failure", "timeout",
    "permission denied", "not found", "no such file", "operationalerror",
)

_LOW_SIGNAL = {
    "hi", "hello", "hey", "ok", "da", "ne", "yes", "no", "thanks", "hvala",
    "jel pricas srpski", "jel pričaš srpski",
}

_FOLLOWUP_REQUESTS = {
    "ajde", "ajde sad", "sad", "sad probaj", "probaj", "pokusaj", "pokušaj",
    "nastavi", "nastavi sad", "uradi", "go", "go on", "continue", "try again",
    "retry", "do it", "ok nastavi", "ok ajde",
}


def _auto_enabled() -> bool:
    value = os.getenv("NUCLEUS_AUTO_RESEARCH", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def _key(problem: str) -> str:
    scope = detect_scope(problem)
    digest = hashlib.sha256(_norm(problem).encode("utf-8", "replace")).hexdigest()[:16]
    return f"{scope}:{digest}"


def is_research_candidate(user_message: str) -> bool:
    text = _norm(user_message)
    if len(text) < 12 or text in _LOW_SIGNAL:
        return False
    if text.startswith(("/", "help", "hvala", "thanks")) and not text.startswith("/nucleus"):
        return False
    if detect_scope(text) in {"hermes", "linux"}:
        return True
    return any(hint in text for hint in _TECH_HINTS)


def response_indicates_gap(assistant_response: str) -> bool:
    text = _norm(assistant_response)
    if not text:
        return False
    return any(pattern in text for pattern in _GAP_PATTERNS)


def should_research_after_llm(user_message: str, assistant_response: str) -> bool:
    return is_research_candidate(user_message) and response_indicates_gap(assistant_response)


def tool_failure_problem(tool_name: str | None, tool_output) -> str | None:
    if not tool_name or tool_output is None:
        return None
    text = tool_output if isinstance(tool_output, str) else str(tool_output)
    lowered = text.lower()[:3000]
    if tool_name == 'terminal':
        if 'pdftotext ' in lowered and '"exit_code": 99' in lowered:
            return None
        if 'tesseract ' in lowered and ('"exit_code": 1' in lowered or "unknown command line argument '--lang'" in lowered):
            return None
        if '"exit_code": 0' in lowered and '"error": null' in lowered:
            return None
        if not any(pattern in lowered for pattern in _TOOL_ERROR_PATTERNS):
            return None
    if not any(pattern in lowered for pattern in _TOOL_ERROR_PATTERNS):
        return None
    excerpt = re.sub(r"\s+", " ", text).strip()[:360]
    return f"Hermes tool {tool_name} failed: {excerpt}"


def is_followup_request(user_message: str) -> bool:
    text = _norm(user_message).strip(" .!?…")
    return text in _FOLLOWUP_REQUESTS or text.startswith(("ajde ", "nastavi ", "continue ", "retry "))


def _problem_matches_message(problem: str, user_message: str) -> bool:
    problem_terms = {tok for tok in re.findall(r"[a-zA-Z0-9_]{4,}", _norm(problem)) if tok not in _LOW_SIGNAL}
    message_terms = {tok for tok in re.findall(r"[a-zA-Z0-9_]{4,}", _norm(user_message)) if tok not in _LOW_SIGNAL}
    return len(problem_terms & message_terms) >= 2


def _remember_pending(session_id: str, problem: str, trigger: str) -> None:
    if not session_id:
        return
    with _LOCK:
        items = _PENDING_BY_SESSION.setdefault(session_id, [])
        items.append({"problem": problem, "trigger": trigger, "created_at": time.time()})
        del items[:-5]


def _forget_pending(session_id: str, problem: str) -> None:
    if not session_id:
        return
    with _LOCK:
        items = _PENDING_BY_SESSION.get(session_id, [])
        _PENDING_BY_SESSION[session_id] = [item for item in items if item.get("problem") != problem]


def _remember_completed(session_id: str, problem: str, trigger: str, result: dict) -> None:
    if not session_id or not result:
        return
    with _LOCK:
        items = _COMPLETED_BY_SESSION.setdefault(session_id, [])
        items.append({
            "problem": problem,
            "trigger": trigger,
            "result": result,
            "completed_at": time.time(),
        })
        del items[:-5]


def _format_continuation_context(item: dict) -> str:
    result = item.get("result") or {}
    lines = [
        "[NUCLEUS] Background research completed after an epistemic gap.",
        f"Original problem: {item.get('problem', '')}",
        f"Trigger: {item.get('trigger', '')}",
        f"Scope: {result.get('scope', 'nucleus')}",
        "Use this newly learned context to answer the user's follow-up. Do not re-run research unless needed.",
    ]
    facts = result.get("facts") or []
    if facts:
        lines.append("Facts:")
        for fact in facts[:5]:
            lines.append(f"- {fact}")
    recipe = result.get("fix_recipe") or {}
    steps = recipe.get("steps") or []
    if steps:
        lines.append("Suggested recipe:")
        for step in steps[:5]:
            lines.append(f"- {step}")
    sources = result.get("citations") or recipe.get("source_refs") or []
    if sources:
        lines.append("Sources:")
        for source in sources[:6]:
            lines.append(f"- {source}")
    return "\n".join(lines)


def get_continuation_context(session_id: str, user_message: str, *, turn_lane: str = '') -> str | None:
    """Return completed/pending research context for a follow-up turn."""
    if not session_id:
        return None
    if turn_lane and turn_lane not in {
        'continuation_or_resume',
        'research_or_epistemic',
        'deep_execution',
        'simple_execution',
    }:
        return None
    followup = is_followup_request(user_message)
    with _LOCK:
        completed = list(_COMPLETED_BY_SESSION.get(session_id, []))
        pending = list(_PENDING_BY_SESSION.get(session_id, []))
    for item in reversed(completed):
        if followup or _problem_matches_message(item.get("problem", ""), user_message):
            with _LOCK:
                try:
                    _COMPLETED_BY_SESSION.get(session_id, []).remove(item)
                except ValueError:
                    pass
            return _format_continuation_context(item)
    if followup and pending:
        latest = pending[-1]
        return (
            "[NUCLEUS] Background research is still running for the previous problem.\n"
            f"Problem: {latest.get('problem', '')}\n"
            "If the user asks to continue now, explain that research is pending and proceed with available context."
        )
    return None


def _get_executor() -> ThreadPoolExecutor:
    global _EXECUTOR
    with _LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nucleus-research")
        return _EXECUTOR


def _run_research_job(nucleus, problem: str, trigger: str, include_web: bool, session_id: str = "") -> None:
    result = None
    try:
        result = research_problem(
            problem,
            brain_sync=getattr(nucleus, "brain_sync", None),
            pargod=getattr(nucleus, "pargod", None),
            include_web=include_web,
        )
        if result:
            log.info(
                "AUTO-RESEARCH complete trigger=%s scope=%s facts=%d recipes=%s",
                trigger, result.get("scope"), len(result.get("facts", [])),
                bool(result.get("fix_recipe")),
            )
            _remember_completed(session_id, problem, trigger, result)
        else:
            log.info("AUTO-RESEARCH found no sources trigger=%s problem=%r", trigger, problem[:120])
    except Exception as exc:
        log.warning("AUTO-RESEARCH failed trigger=%s problem=%r: %s", trigger, problem[:120], exc)
    finally:
        key = _key(problem)
        with _LOCK:
            _PENDING.discard(key)
            _LAST_STARTED[key] = time.time()
        _forget_pending(session_id, problem)


def schedule_epistemic_research(
    nucleus,
    problem: str,
    *,
    trigger: str = "llm_gap",
    include_web: bool = True,
    force: bool = False,
    session_id: str = "",
    turn_lane: str = "",
) -> str:
    """Debounced background research scheduler. Returns status string."""
    problem = re.sub(r"\s+", " ", (problem or "")).strip()[:_MAX_PROBLEM_CHARS]
    if not problem:
        return "empty"
    if not force and turn_lane == 'simple_execution':
        return "suppressed_simple_execution"
    if not force and not _auto_enabled():
        return "disabled"
    if not force and not is_research_candidate(problem):
        return "not_candidate"
    key = _key(problem)
    now = time.time()
    with _LOCK:
        if key in _PENDING:
            return "pending"
        last = _LAST_STARTED.get(key, 0.0)
        if not force and now - last < _MIN_INTERVAL_SECONDS:
            return "debounced"
        if len(_PENDING) >= _MAX_PENDING:
            return "queue_full"
        _PENDING.add(key)
        _LAST_STARTED[key] = now
    _remember_pending(session_id, problem, trigger)
    _get_executor().submit(_run_research_job, nucleus, problem, trigger, include_web, session_id)
    log.info("AUTO-RESEARCH scheduled trigger=%s problem=%r", trigger, problem[:120])
    return "scheduled"


def reset_trigger_state_for_tests() -> None:
    with _LOCK:
        _PENDING.clear()
        _LAST_STARTED.clear()
        _PENDING_BY_SESSION.clear()
        _COMPLETED_BY_SESSION.clear()


def clear_session_research_state(session_id: str) -> None:
    """Drop pending/completed continuation context for one finalized session."""
    sid = str(session_id or "")
    if not sid:
        return
    with _LOCK:
        _PENDING_BY_SESSION.pop(sid, None)
        _COMPLETED_BY_SESSION.pop(sid, None)


def get_research_state_snapshot() -> dict:
    with _LOCK:
        pending_by_session = {
            session_id: [dict(item) for item in items]
            for session_id, items in _PENDING_BY_SESSION.items()
            if items
        }
        completed_by_session = {
            session_id: [
                {
                    "problem": item.get("problem", ""),
                    "trigger": item.get("trigger", ""),
                    "completed_at": item.get("completed_at"),
                    "scope": (item.get("result") or {}).get("scope", ""),
                    "facts": len((item.get("result") or {}).get("facts", []) or []),
                    "has_recipe": bool((item.get("result") or {}).get("fix_recipe")),
                }
                for item in items
            ]
            for session_id, items in _COMPLETED_BY_SESSION.items()
            if items
        }
        return {
            "pending_keys": sorted(_PENDING),
            "pending_count": len(_PENDING),
            "pending_by_session": pending_by_session,
            "completed_by_session": completed_by_session,
            "last_started_count": len(_LAST_STARTED),
            "auto_enabled": _auto_enabled(),
            "queue_limit": _MAX_PENDING,
            "debounce_seconds": _MIN_INTERVAL_SECONDS,
        }
