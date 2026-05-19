"""P3.1 + P3.2 + P3.3 — plugin-side bridge between live_brain_ctx and nucleus.

Lives inside live_brain_ctx because that plugin is the single per-turn
context publisher. nucleus registers as a *contributor* here; it no longer
returns context from its own pre_llm_call hook.

This module gives us three things:

1. **ContextContribution dataclass + registry** (P3.1). Any plugin can
   register a callback that returns `List[ContextContribution]` for the
   current turn. live_brain_ctx pulls all contributions, appends them as
   sections to its assembled context, and runs the assembler over the
   merged result — so dedup, byte caps, and budgets apply uniformly.

2. **Shared scope / pending_changes** (P3.2). Both plugins maintain
   per-session state. The bridge exposes a thread-safe key/value store so
   live_brain_ctx can publish its turn_lane / scope_key and nucleus can
   read it without re-deriving (or vice versa for pending_changes from
   nucleus's SessionState).

3. **Intent cache** (P3.3). live_brain_ctx is the authority for intent
   classification. nucleus reads the cached intent here instead of running
   its own `_extract_intent`.

All state is **plugin-side only** — Hermes core is not touched. State is
TTL-bounded so long-idle sessions don't leak memory.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger("live_brain_ctx.bridge")

_STATE_TTL_SECONDS = 3600.0  # drop per-session entries older than this


# ---------------------------------------------------------------------------
# ContextContribution
# ---------------------------------------------------------------------------

@dataclass
class ContextContribution:
    """One block of context contributed by a plugin for the current turn.

    The body is rendered into a section ``f"{section}:\\n{body}"`` by the
    assembler; assembler-side per-section caps + lane budget + cross-turn
    dedup apply uniformly to contributed sections.
    """
    plugin: str
    section: str
    body: str
    priority: int = 20
    dedupe_key: str = ""
    lane_gate: Optional[Set[str]] = None  # None = applies to all lanes

    def applies_to_lane(self, lane: str) -> bool:
        if not self.lane_gate:
            return True
        return lane in self.lane_gate


# ---------------------------------------------------------------------------
# Contributor registry (P3.1)
# ---------------------------------------------------------------------------

Contributor = Callable[..., List[ContextContribution]]

_CONTRIBUTORS: Dict[str, Contributor] = {}
_CONTRIBUTORS_LOCK = threading.Lock()


def register_contributor(plugin: str, fn: Contributor) -> None:
    """Register a context contributor under a plugin name.

    Re-registration replaces the prior callback — useful for hot-reload
    in dev. Threading: the registry mutates under a lock; gather() takes
    a snapshot before invoking callbacks.
    """
    with _CONTRIBUTORS_LOCK:
        _CONTRIBUTORS[plugin] = fn
    logger.info("[bridge] registered contributor: %s", plugin)


def unregister_contributor(plugin: str) -> None:
    with _CONTRIBUTORS_LOCK:
        _CONTRIBUTORS.pop(plugin, None)


def clear_contributors() -> None:
    """Test-only: drop all registered contributors."""
    with _CONTRIBUTORS_LOCK:
        _CONTRIBUTORS.clear()


def _discover_loaded_peer_contributors() -> None:
    """Pull contributors from already-loaded peer plugins in sys.modules.

    Hermes loads plugins under ``hermes_plugins.<slug>`` synthetic names.
    We look for those (not top-level ``nucleus``) so we share the same
    module instance — and the same ``session_state`` singleton — as the
    plugin's own hooks. Top-level imports would create a SECOND copy
    with its own state.

    Idempotent. Solves the hook-order race where live_brain_ctx's
    ``_pre_llm_call`` runs before nucleus's hook on turn 1.
    """
    import sys
    peers = {
        # contributor name -> (synthetic module suffix, attribute)
        "nucleus": ("hermes_plugins.nucleus.contributions", "compute_contributions"),
    }
    with _CONTRIBUTORS_LOCK:
        already = set(_CONTRIBUTORS.keys())
    for name, (mod_name, attr) in peers.items():
        if name in already:
            continue
        mod = sys.modules.get(mod_name)
        if mod is None:
            # Try loading the plugin's package which is already in sys.modules
            # (Hermes loaded it). That triggers the submodule import in the
            # SAME namespace as the hooks — no dual singleton.
            pkg_name = mod_name.rsplit(".", 1)[0]
            pkg = sys.modules.get(pkg_name)
            if pkg is None:
                logger.debug("[bridge] peer pkg %s not loaded", pkg_name)
                continue
            try:
                import importlib
                mod = importlib.import_module(mod_name)
            except Exception as exc:
                logger.debug("[bridge] peer import %s failed: %s", mod_name, exc)
                continue
        fn = getattr(mod, attr, None)
        if fn is None:
            continue
        register_contributor(name, fn)


def gather_contributions(
    *,
    session_id: str,
    user_message: str,
    turn_lane: str,
    sender_id: str = "",
    platform: str = "",
    scope_key: str = "",
) -> List[ContextContribution]:
    """Invoke all registered contributors and collect their output.

    Each contributor is wrapped in try/except — a misbehaving plugin must
    not poison the turn. Contributions filtered by lane_gate.
    """
    _discover_loaded_peer_contributors()
    with _CONTRIBUTORS_LOCK:
        snapshot = list(_CONTRIBUTORS.items())

    out: List[ContextContribution] = []
    for plugin_name, fn in snapshot:
        try:
            result = fn(
                session_id=session_id,
                user_message=user_message,
                turn_lane=turn_lane,
                sender_id=sender_id,
                platform=platform,
                scope_key=scope_key,
            )
        except Exception as exc:
            logger.warning("[bridge] contributor %s failed: %s", plugin_name, exc)
            continue
        if not result:
            continue
        for contrib in result:
            # Duck-type check (accept ContextContribution from any import
            # path — Hermes' synthetic plugin namespace + top-level paths
            # would otherwise treat the same dataclass as different types).
            if not (
                hasattr(contrib, "section")
                and hasattr(contrib, "body")
                and hasattr(contrib, "applies_to_lane")
            ):
                continue
            if not contrib.applies_to_lane(turn_lane):
                continue
            if not contrib.body or not contrib.body.strip():
                continue
            out.append(contrib)
    return out


# ---------------------------------------------------------------------------
# Shared per-session state (P3.2 + P3.3)
# ---------------------------------------------------------------------------

@dataclass
class _SessionStateRecord:
    scope_key: str = ""
    turn_lane: str = ""
    intent: str = ""
    pending_changes: List[Dict[str, Any]] = field(default_factory=list)
    updated_at: float = 0.0


_SESSION_STATE: Dict[str, _SessionStateRecord] = {}
_SESSION_STATE_LOCK = threading.Lock()


def _prune_stale(now: float) -> None:
    stale = [sid for sid, rec in _SESSION_STATE.items() if now - rec.updated_at > _STATE_TTL_SECONDS]
    for sid in stale:
        _SESSION_STATE.pop(sid, None)


def share_scope(session_id: str, scope_key: str, turn_lane: str, intent: str = "") -> None:
    """Publish the live_brain_ctx-derived scope/lane/intent for a session."""
    if not session_id:
        return
    now = time.time()
    with _SESSION_STATE_LOCK:
        _prune_stale(now)
        rec = _SESSION_STATE.setdefault(session_id, _SessionStateRecord())
        rec.scope_key = scope_key or rec.scope_key
        rec.turn_lane = turn_lane or rec.turn_lane
        if intent:
            rec.intent = intent
        rec.updated_at = now


def get_scope(session_id: str) -> Dict[str, str]:
    """Return {'scope_key', 'turn_lane', 'intent'} as last published.

    Empty strings when the session has no record yet. Never raises.
    """
    if not session_id:
        return {"scope_key": "", "turn_lane": "", "intent": ""}
    with _SESSION_STATE_LOCK:
        rec = _SESSION_STATE.get(session_id)
        if not rec:
            return {"scope_key": "", "turn_lane": "", "intent": ""}
        return {"scope_key": rec.scope_key, "turn_lane": rec.turn_lane, "intent": rec.intent}


def share_pending_changes(session_id: str, changes: List[Dict[str, Any]]) -> None:
    """Publish nucleus's pending-change list (from SessionState.snapshot()).

    Stored as a copy; nucleus may mutate its own list freely after this.
    """
    if not session_id:
        return
    now = time.time()
    with _SESSION_STATE_LOCK:
        _prune_stale(now)
        rec = _SESSION_STATE.setdefault(session_id, _SessionStateRecord())
        rec.pending_changes = list(changes or [])
        rec.updated_at = now


def get_pending_changes(session_id: str) -> List[Dict[str, Any]]:
    if not session_id:
        return []
    with _SESSION_STATE_LOCK:
        rec = _SESSION_STATE.get(session_id)
        return list(rec.pending_changes) if rec else []


def clear_session_state(session_id: str) -> None:
    with _SESSION_STATE_LOCK:
        _SESSION_STATE.pop(session_id, None)


def reset_all_state() -> None:
    """Test-only: drop everything."""
    with _SESSION_STATE_LOCK:
        _SESSION_STATE.clear()
