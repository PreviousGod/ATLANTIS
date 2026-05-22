"""P5.1 — provider-cache observability and stable-prefix guard.

Hermes core (``run_agent.py`` + ``agent/prompt_caching.py``) already wires
``cache_control`` markers when the active provider is on the supported list
(native Anthropic, OpenRouter Claude, Nous Portal, MiniMax, opencode-go
Qwen, …). It does NOT do anything for unsupported providers — and gives no
signal whether caching is actually paying off on a given session.

This module sits in the plugin space (no core patch — see
``feedback_avoid_core_patch_hermes``) and:

* fingerprints the stable system prefix at every ``pre_api_request`` so
  drift (which silently invalidates the prefix cache) is detectable.
* records ``cache_read_input_tokens`` and ``cache_creation_input_tokens``
  from ``post_api_request`` usage payloads so the user can verify caching
  is engaging on the live provider.
* appends both signals to ``~/.hermes/logs/context-budget.log`` (same file
  the assembler audit uses) so a single ``tail -f`` shows context bytes,
  drift, and cache hit-rate side by side.

The module never mutates request payloads. Provider cache_control wiring
stays in core where it already lives.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("live_brain_ctx.cache_hints")

# ---------------------------------------------------------------------------
# Fingerprint storage
# ---------------------------------------------------------------------------

_FP_LOCK = threading.Lock()
# session_id -> (stable_hash, last_seen_ts)
_LAST_STABLE_FP: Dict[str, Tuple[str, float]] = {}
# session_id -> consecutive turns with the same fingerprint (cache-friendly)
_FP_RUN_LENGTH: Dict[str, int] = {}
_FP_TTL_SECONDS = 6 * 3600.0


def _now() -> float:
    return time.time()


def _audit_log_path() -> Path:
    home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    log_dir = Path(home) / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return log_dir / "context-budget.log"


def _hash_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def _prune_stale(now: float) -> None:
    stale = [
        sid for sid, (_, ts) in _LAST_STABLE_FP.items() if now - ts > _FP_TTL_SECONDS
    ]
    for sid in stale:
        _LAST_STABLE_FP.pop(sid, None)
        _FP_RUN_LENGTH.pop(sid, None)


# ---------------------------------------------------------------------------
# pre_api_request — fingerprint the stable system prefix
# ---------------------------------------------------------------------------

def _extract_stable_prefix(api_kwargs_messages: Any) -> Optional[str]:
    """Pull the stable-tier text out of the system message.

    Hermes core builds the system message as either a list of content blocks
    (long-lived prefix layout — block[0] is the stable tier) or a single
    string (default). We don't have direct access to ``api_kwargs`` from the
    ``pre_api_request`` hook, so this helper is exposed for reuse by tests
    and by callers that *do* have the messages.

    Returns ``None`` when nothing recognizable is present.
    """
    if not api_kwargs_messages:
        return None
    if not isinstance(api_kwargs_messages, list) or not api_kwargs_messages:
        return None
    first = api_kwargs_messages[0]
    if not isinstance(first, dict) or first.get("role") != "system":
        return None
    content = first.get("content")
    if isinstance(content, str):
        return content or None
    if isinstance(content, list) and content:
        head = content[0]
        if isinstance(head, dict):
            text = head.get("text")
            if isinstance(text, str):
                return text or None
    return None


def record_stable_fingerprint(
    *,
    session_id: str,
    stable_text: Optional[str],
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Record the stable-prefix hash for this turn; report drift.

    Returns a dict with ``hash``, ``run_length`` (consecutive turns with the
    same hash including this one), and ``drifted`` (True when the previous
    fingerprint differed). Always safe to call — never raises.
    """
    if not session_id or not stable_text:
        return {"hash": "", "run_length": 0, "drifted": False}

    h = _hash_text(stable_text)
    now = _now()
    drifted = False
    with _FP_LOCK:
        _prune_stale(now)
        prev = _LAST_STABLE_FP.get(session_id)
        if prev and prev[0] == h:
            run = _FP_RUN_LENGTH.get(session_id, 1) + 1
        else:
            if prev is not None:
                drifted = True
            run = 1
        _LAST_STABLE_FP[session_id] = (h, now)
        _FP_RUN_LENGTH[session_id] = run

    record = {
        "ts": now,
        "kind": "cache_prefix_fp",
        "session": session_id,
        "hash": h,
        "stable_bytes": len(stable_text),
        "run_length": run,
        "drifted": drifted,
    }
    if extra:
        for k, v in extra.items():
            if k not in record:
                record[k] = v
    _append_log(record)
    if drifted:
        logger.info(
            "[cache_hints] stable prefix drifted session=%s hash=%s bytes=%d",
            session_id, h, len(stable_text),
        )
    return {"hash": h, "run_length": run, "drifted": drifted}


# ---------------------------------------------------------------------------
# post_api_request — read cache hit/miss telemetry from usage
# ---------------------------------------------------------------------------

def _coerce_int(v: Any) -> int:
    try:
        return int(v) if v is not None else 0
    except (TypeError, ValueError):
        return 0


def extract_cache_tokens(usage: Any) -> Dict[str, int]:
    """Pull cache-relevant token counts from a normalized usage payload.

    The ``post_api_request`` hook receives ``CanonicalUsage`` asdict'd by
    ``_usage_summary_for_api_request_hook`` — canonical field names are
    ``cache_read_tokens`` / ``cache_write_tokens`` (see
    ``agent/usage_pricing.py``). For robustness against future shape
    changes (and direct callers passing raw provider dicts), we also
    probe the Anthropic top-level names and the OpenAI-style nested
    ``prompt_tokens_details.cached_tokens`` form.
    """
    if not isinstance(usage, dict):
        return {"cache_read": 0, "cache_creation": 0, "prompt": 0, "total": 0}

    cache_read = _coerce_int(usage.get("cache_read_tokens"))
    if not cache_read:
        cache_read = _coerce_int(usage.get("cache_read_input_tokens"))
    if not cache_read:
        details = usage.get("prompt_tokens_details") or {}
        if isinstance(details, dict):
            cache_read = _coerce_int(details.get("cached_tokens"))

    cache_creation = _coerce_int(usage.get("cache_write_tokens"))
    if not cache_creation:
        cache_creation = _coerce_int(usage.get("cache_creation_input_tokens"))

    return {
        "cache_read": cache_read,
        "cache_creation": cache_creation,
        "prompt": _coerce_int(usage.get("prompt_tokens")),
        "total": _coerce_int(usage.get("total_tokens")),
    }


def record_post_api_cache(
    *,
    session_id: str,
    provider: str,
    model: str,
    api_mode: str,
    api_call_count: int,
    usage: Any,
) -> Dict[str, Any]:
    """Emit a one-line cache audit record. Returns the recorded dict."""
    tokens = extract_cache_tokens(usage)
    rate = 0.0
    if tokens["prompt"] > 0:
        rate = round(tokens["cache_read"] / tokens["prompt"], 3)
    record = {
        "ts": _now(),
        "kind": "cache_usage",
        "session": session_id or "",
        "provider": provider or "",
        "model": model or "",
        "api_mode": api_mode or "",
        "api_call": int(api_call_count or 0),
        "cache_read": tokens["cache_read"],
        "cache_creation": tokens["cache_creation"],
        "prompt": tokens["prompt"],
        "total": tokens["total"],
        "hit_rate": rate,
    }
    _append_log(record)
    return record


# ---------------------------------------------------------------------------
# Append to the shared audit log
# ---------------------------------------------------------------------------

_LOG_LOCK = threading.Lock()


def _append_log(record: Dict[str, Any]) -> None:
    try:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return
    path = _audit_log_path()
    try:
        with _LOG_LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Hook entry points wired in live_brain_ctx/__init__.py
# ---------------------------------------------------------------------------

def on_pre_api_request(**kwargs) -> None:
    """Plugin ``pre_api_request`` callback.

    The hook fires after Hermes core has assembled ``api_kwargs`` but does
    not pass them through. We don't have direct access to the system
    message bytes here, so the fingerprint is sourced from the live_brain
    static preamble (byte-stable for the user) plus the model/provider
    triple — enough to detect drift when *anything cache-relevant* changes.
    """
    if os.environ.get("LIVE_BRAIN_CTX_CACHE_AUDIT", "1") == "0":
        return
    session_id = str(kwargs.get("session_id") or "")
    if not session_id:
        return

    provider = str(kwargs.get("provider") or "")
    model = str(kwargs.get("model") or "")
    api_mode = str(kwargs.get("api_mode") or "")
    base_url = str(kwargs.get("base_url") or "")

    # Compose a synthetic stable token: the static live_brain preamble +
    # provider/model/api_mode triple. If any of these flip mid-session the
    # provider's prefix cache will miss; we want to log that.
    try:
        from live_brain import LiveBrainProvider  # type: ignore
        preamble = LiveBrainProvider().system_prompt_block() or ""
    except Exception:
        preamble = ""

    synthetic_stable = "\n".join([
        f"provider={provider}",
        f"model={model}",
        f"api_mode={api_mode}",
        f"base_url={base_url}",
        preamble,
    ])
    record_stable_fingerprint(
        session_id=session_id,
        stable_text=synthetic_stable,
        extra={
            "provider": provider,
            "model": model,
            "api_mode": api_mode,
            "preamble_bytes": len(preamble),
            "approx_input_tokens": _coerce_int(kwargs.get("approx_input_tokens")),
            "tool_count": _coerce_int(kwargs.get("tool_count")),
            "message_count": _coerce_int(kwargs.get("message_count")),
        },
    )


def on_post_api_request(**kwargs) -> None:
    """Plugin ``post_api_request`` callback — extract cache hit metrics."""
    if os.environ.get("LIVE_BRAIN_CTX_CACHE_AUDIT", "1") == "0":
        return
    session_id = str(kwargs.get("session_id") or "")
    if not session_id:
        return
    record_post_api_cache(
        session_id=session_id,
        provider=str(kwargs.get("provider") or ""),
        model=str(kwargs.get("model") or ""),
        api_mode=str(kwargs.get("api_mode") or ""),
        api_call_count=_coerce_int(kwargs.get("api_call_count")),
        usage=kwargs.get("usage"),
    )


# ---------------------------------------------------------------------------
# Manual reset (e.g. /new) so a fresh session doesn't inherit drift state
# ---------------------------------------------------------------------------

def clear_session(session_id: str) -> None:
    if not session_id:
        return
    with _FP_LOCK:
        _LAST_STABLE_FP.pop(session_id, None)
        _FP_RUN_LENGTH.pop(session_id, None)
