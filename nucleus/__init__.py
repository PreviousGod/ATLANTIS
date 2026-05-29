"""Nucleus Plugin — Autonomni Entitet sa LLM Bypass-om.

Registers with Hermes plugin system and monkey-patches run_agent.AIAgent
to skip LLM calls when Pargod graph has a deterministic answer.
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

log = logging.getLogger("nucleus")

_nucleus_instance = None
_nucleus_lock = threading.Lock()
_patch_applied = False

_NUCLEUS_PREFIXES = ("/nucleus", "nucleus:", "nucleus ")
_NUCLEUS_WEB_PREFIXES = (
    "/nucleus web",
    "/nucleus search",
    "/nucleus research",
    "nucleus web",
    "nucleus search",
    "nucleus research",
    "nucleus: web",
    "nucleus: search",
    "nucleus: research",
)
_NUCLEUS_STATUS_PREFIXES = (
    "/nucleus status",
    "/nucleus metrics",
    "/nucleus queue",
    "nucleus status",
    "nucleus metrics",
    "nucleus queue",
    "nucleus: status",
    "nucleus: metrics",
    "nucleus: queue",
)
_NUCLEUS_DOCTOR_PREFIXES = (
    "/nucleus doctor",
    "/nucleus preflight",
    "nucleus doctor",
    "nucleus preflight",
    "nucleus: doctor",
    "nucleus: preflight",
)
_SYSTEM_PROBLEM_PHRASES = (
    "high cpu",
    "cpu usage",
    "cpu high",
    "high ram",
    "ram usage",
    "memory usage",
    "disk full",
    "disk space critically low",
    "zombie process",
    "port conflict",
    "port already in use",
    "oom risk",
    "out of memory",
    "high load",
    "load average",
    "swap thrash",
    "inode exhaustion",
    "dns failure",
    "service down",
    "log flood",
    "network saturated",
    "temp files bloat",
    "stale connections",
    "time_wait",
)


def _env_enabled(name):
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _is_nucleus_graph_query(message):
    """Only bypass for explicit Nucleus requests; generic ops wording stays with the LLM."""
    if not isinstance(message, str):
        return False
    text = message.strip().lower()
    if not text or len(text) >= 500:
        return False
    if text.startswith(_NUCLEUS_PREFIXES):
        return True
    if text.startswith("nucleus teach"):
        return True
    if text.startswith("nucleus suggest"):
        return True
    if text.startswith("nucleus ego"):
        return True
    return False


def _is_safe_nucleus_context_query(message):
    """Prompt surfacing from Nucleus should only happen on explicit/safe Nucleus turns."""
    if not isinstance(message, str):
        return False
    text = message.strip().lower()
    if not text or len(text) >= 500:
        return False
    return (
        text.startswith(_NUCLEUS_PREFIXES)
        or text.startswith("nucleus teach")
        or text.startswith("nucleus suggest")
        or text.startswith("nucleus ego")
    )


def _is_nucleus_web_query(message):
    """Web bypass is opt-in; generic chat must stay with the LLM."""
    if not isinstance(message, str):
        return False
    text = message.strip().lower()
    if not text or len(text) >= 500:
        return False
    return _env_enabled("NUCLEUS_WEB_BYPASS") or text.startswith(_NUCLEUS_WEB_PREFIXES)


def _is_nucleus_status_query(message):
    if not isinstance(message, str):
        return False
    text = message.strip().lower()
    if not text or len(text) >= 500:
        return False
    return text.startswith(_NUCLEUS_STATUS_PREFIXES)


def _is_nucleus_doctor_query(message):
    if not isinstance(message, str):
        return False
    text = message.strip().lower()
    if not text or len(text) >= 500:
        return False
    return text.startswith(_NUCLEUS_DOCTOR_PREFIXES)


def _strip_nucleus_web_prefix(message):
    raw = message.strip()
    lowered = raw.lower()
    for prefix in _NUCLEUS_WEB_PREFIXES:
        if lowered.startswith(prefix):
            return raw[len(prefix):].strip(" :-") or raw
    return raw


def _get_nucleus():
    """Lazy singleton — starts Nucleus engine in background thread.

    Degraded-mode fallback: if Nucleus() raises during init (most commonly
    when pargod.db is corrupted/missing and Pargod._init_db() chokes), we
    install a no-op `_NucleusDegraded` stub instead of propagating. This
    lets the plugin stay loaded (hooks registered) so it can recover on
    the next restart once pargod.db is repaired. Without this fallback,
    a single corrupted DB takes the whole plugin offline.
    """
    global _nucleus_instance
    if _nucleus_instance is not None:
        return _nucleus_instance
    with _nucleus_lock:
        if _nucleus_instance is not None:
            return _nucleus_instance
        try:
            from .nucleus_engine import Nucleus
            _nucleus_instance = Nucleus()
        except Exception as exc:
            log.warning(
                "Nucleus init failed — installing degraded stub so hooks stay "
                "registered; restart with healthy pargod.db to recover. Cause: %s",
                exc,
            )
            _nucleus_instance = _NucleusDegraded()
            return _nucleus_instance
        # Heartbeat disabled — hooks + bridge contributions provide all value.
        # Pargod lookups still work via lazy singleton.
    return _nucleus_instance


class _NullPargod:
    """No-op Pargod stub. All lookups return None/empty; writes are no-ops.

    Lets hook code call `self.nucleus.pargod.has_answer_for(...)` etc. when
    Pargod itself failed to initialise without forcing every callsite to
    check `if pargod is not None`.
    """

    # Lookups
    def has_answer_for(self, *args, **kwargs):
        return None

    def has_resolution_for(self, *args, **kwargs):
        return None

    def find_tool_for_problem(self, *args, **kwargs):
        return None

    def get_node(self, *args, **kwargs):
        return None

    def list_nodes(self, *args, **kwargs):
        return []

    def list_edges(self, *args, **kwargs):
        return []

    # Writes (no-op)
    def add_node(self, *args, **kwargs):
        pass

    def upsert_node(self, *args, **kwargs):
        pass

    def add_edge(self, *args, **kwargs):
        pass

    def record_use(self, *args, **kwargs):
        pass

    def decay_edges(self, *args, **kwargs):
        pass

    def log_episode(self, *args, **kwargs):
        pass

    def seed_from_json(self, *args, **kwargs):
        pass


class _NullBrainSync:
    """No-op LiveBrainSync stub used inside _NucleusDegraded."""

    def write_fact(self, *args, **kwargs):
        return None

    def write_fix_recipe(self, *args, **kwargs):
        return None

    def write_research_trace(self, *args, **kwargs):
        return None

    def write_artifact(self, *args, **kwargs):
        return None

    def get_facts(self, *args, **kwargs):
        return []

    def get_artifacts(self, *args, **kwargs):
        return []

    def get_fix_recipes(self, *args, **kwargs):
        return []

    def get_recent_work_items(self, *args, **kwargs):
        return []

    def sync_to_pargod(self, *args, **kwargs):
        return None


class _NucleusDegraded:
    """Drop-in replacement for `Nucleus` when initialisation fails.

    Exposes the public attributes the hooks reach for (`pargod`, `brain_sync`,
    `session_state`, `intervention`) as no-op stubs. All hook code therefore
    runs without crashing; it just gets None/empty answers.
    """

    def __init__(self):
        self.pargod = _NullPargod()
        self.brain_sync = _NullBrainSync()
        self.session_state = None
        self.intervention = None
        self.guard = None

    def _execute_instinct(self, tool_label):
        return {"success": False, "error": "Nucleus degraded"}

    def _escalate_web(self, problem):
        return None


def _apply_monkey_patch():
    """Patch AIAgent.run_conversation with guarded Nucleus bypasses."""
    global _patch_applied
    if _patch_applied:
        return

    import run_agent
    original_run_conversation = run_agent.AIAgent.run_conversation

    def _make_bypass_response(user_message, text, reason, model_label):
        return {
            "final_response": text,
            "last_reasoning": None,
            "messages": [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": text},
            ],
            "api_calls": 0,
            "completed": True,
            "turn_exit_reason": reason,
            "partial": False,
            "interrupted": False,
            "response_previewed": False,
            "model": f"nucleus/{model_label}",
            "provider": "nucleus",
            "base_url": "",
            "input_tokens": 0,
            "output_tokens": 0,
            "session_input_tokens": 0,
            "session_output_tokens": 0,
        }

    def patched_run_conversation(self, user_message, *args, **kwargs):
        """Guarded escalation: explicit/confident graph → opt-in web → LLM."""
        if _is_nucleus_doctor_query(user_message):
            from .doctor import format_doctor, run_doctor
            response_text = format_doctor(run_doctor(run_web=" web" in user_message.lower()))
            log.info("BYPASS(doctor): '%s'", user_message[:50])
            return _make_bypass_response(user_message, response_text, "nucleus_doctor", "doctor")

        if _is_nucleus_status_query(user_message):
            nucleus = _get_nucleus()
            from .status import collect_status, format_status
            response_text = format_status(collect_status(nucleus))
            log.info("BYPASS(status): '%s'", user_message[:50])
            return _make_bypass_response(user_message, response_text, "nucleus_status", "status")

        if _is_nucleus_graph_query(user_message) or _is_nucleus_web_query(user_message):
            nucleus = _get_nucleus()

            # --- Level 1: Graph (Pargod) ---
            answer = nucleus.pargod.has_answer_for(user_message) if _is_nucleus_graph_query(user_message) else None
            if answer and not _is_nucleus_web_query(user_message):
                tool_label = answer["tool"]
                result = nucleus._execute_instinct(tool_label)
                if result.get("success") and result.get("stdout"):
                    response_text = result["stdout"].strip()
                    log.info(f"BYPASS(graph): '{user_message[:50]}' → {tool_label} (0 tokens)")
                    return _make_bypass_response(user_message, response_text, "nucleus_bypass", "pargod")

            # --- Level 1.5: nucleus suggest ---
            if user_message.strip().lower().startswith("nucleus suggest"):
                from .proactive_suggester import ProactiveSuggester
                ps = ProactiveSuggester()
                stats = ps.get_stats()
                lines = [
                    "[NUCLEUS SUGGESTIONS]",
                    f"Rules: {stats['total_rules']} | Shown: {stats['total_shown']} | Acted: {stats['times_acted']} | Ignored: {stats['times_ignored']}",
                    f"Effectiveness: {stats['effectiveness']} | Last hour: {stats['last_hour']}",
                ]
                # List top rules
                import sqlite3
                db = Path.home() / ".hermes" / "nucleus_data" / "pargod.db"
                with sqlite3.connect(str(db)) as conn:
                    rows = conn.execute(
                        """SELECT category, severity, message, times_shown, times_acted, confidence
                           FROM suggestions ORDER BY confidence DESC LIMIT 5"""
                    ).fetchall()
                    if rows:
                        lines.append("Top rules:")
                        for r in rows:
                            lines.append(f"- [{r[0]}|{r[1]}] {r[2][:50]}... (shown={r[3]}, acted={r[4]}, conf={r[5]})")
                response_text = "\n".join(lines)
                log.info("BYPASS(suggest): '%s'", user_message[:50])
                return _make_bypass_response(user_message, response_text, "nucleus_suggest", "suggest")

            # --- Level 1.5: nucleus ego ---
            if user_message.strip().lower().startswith("nucleus ego"):
                from .ego_model import EgoModel
                ego = EgoModel()
                stats = ego.get_stats()
                lines = [
                    "[NUCLEUS EGO / CIEL]",
                    f"Current mood: {stats['current_mood']} | Entropy: {stats['current_entropy']:.2f}",
                    f"Reflections: {stats['reflections']} | Autonomous actions: {stats['autonomous_executed']}/{stats['autonomous_total']}",
                    f"Mood history: {stats['mood_distribution']}",
                ]
                # Last 3 reflections
                refs = ego.get_last_reflection(3)
                if refs:
                    lines.append("Recent thoughts:")
                    for r in refs:
                        lines.append(f"- [{r['trigger']}] {r['monologue'][:70]}...")
                response_text = "\n".join(lines)
                log.info("BYPASS(ego): '%s'", user_message[:50])
                return _make_bypass_response(user_message, response_text, "nucleus_ego", "ego")

            # --- Level 1.5: nucleus teach ---
            if user_message.strip().lower().startswith("nucleus teach"):
                parts = user_message.strip().split("|", 3)
                if len(parts) >= 3:
                    _, tool_name, mistake, correction = parts[0].strip(), parts[1].strip(), parts[2].strip(), (parts[3].strip() if len(parts) > 3 else "")
                    from .learning_engine import LearningEngine
                    le = LearningEngine()
                    ph = le.teach(tool_name, mistake[:50], mistake, correction or "")
                    stats = le.get_stats()
                    response_text = (
                        f"Pattern recorded: {ph[:8]}...\n"
                        f"Stats: {stats['total_patterns']} patterns, "
                        f"avg confidence {stats['avg_confidence']}"
                    )
                    log.info("BYPASS(teach): '%s'", user_message[:50])
                    return _make_bypass_response(user_message, response_text, "nucleus_teach", "teach")
                else:
                    response_text = "Usage: nucleus teach | tool_name | mistake | correction"
                    return _make_bypass_response(user_message, response_text, "nucleus_teach", "teach")

            # --- Level 2: Web Search (explicit/opt-in only) ---
            research_result = nucleus._escalate_web(_strip_nucleus_web_prefix(user_message)) if _is_nucleus_web_query(user_message) else None
            if research_result:
                from .researcher import format_research_summary
                response_text = format_research_summary(research_result)
                log.info(
                    "BYPASS(research): '%s' → scope=%s facts=%d",
                    user_message[:50], research_result.get("scope"), len(research_result.get("facts", [])),
                )
                return _make_bypass_response(user_message, response_text, "nucleus_research", "research")

        # --- Level 3: LLM (original) ---
        return original_run_conversation(self, user_message, *args, **kwargs)

    run_agent.AIAgent.run_conversation = patched_run_conversation
    _patch_applied = True
    log.info("Monkey-patch applied: AIAgent.run_conversation → guarded Nucleus bypass (chat→LLM by default)")


_CONTRIBUTOR_REGISTERED = False


def _ensure_contributor_registered():
    """Lazily register nucleus.contributions with the live_brain_ctx bridge.

    Called from register() and again from the first hook firing because
    plugin discovery order means live_brain_ctx may not be importable yet
    at register() time. Idempotent.
    """
    global _CONTRIBUTOR_REGISTERED
    if _CONTRIBUTOR_REGISTERED:
        return
    try:
        from live_brain_ctx.modules.bridge import register_contributor
        from .contributions import compute_contributions
        register_contributor("nucleus", compute_contributions)
        _CONTRIBUTOR_REGISTERED = True
        log.info("Nucleus contributor registered with live_brain_ctx bridge")
    except Exception as exc:
        log.debug("Nucleus contributor registration deferred: %s", exc)


def _pre_llm_hook(session_id=None, user_message=None, **kwargs):
    """Hook: bookkeeping only.

    P3.1: this hook used to return ``{"context": str}`` with up to two
    concatenated emissions (NUCLEUS WARN + one of LEARN/SUGGEST/PROACTIVE/
    CONTINUATION/GRAPH). That bypassed live_brain_ctx's assembler and
    caused double-injection (each plugin returned context, Hermes core
    concatenated both into the user message).

    The emission logic now lives in ``nucleus.contributions.compute_contributions``
    and is registered with the live_brain_ctx bridge at plugin
    ``register()`` time (or lazily on first hook firing — see
    ``_ensure_contributor_registered``). This hook still runs for its
    side effects (``SessionState.on_user_message``) but returns ``None``.
    """
    _ensure_contributor_registered()
    if not user_message or not isinstance(user_message, str):
        return None
    sid = str(session_id or "")
    msg = str(user_message)

    # P2.11: reset in-turn tool call counters on each user message
    _reset_in_turn_counts(sid)
    # P4.4: reset task graph step counter each turn
    with _TASK_GRAPH_STEPS_LOCK:
        _TASK_GRAPH_STEPS_THIS_TURN[sid] = 0
    log.debug("SPIRAL reset counters for session %s", sid[:8])

    # Update SessionState with the user message — emission logic in
    # nucleus.contributions reads from this state via drain_warnings(),
    # ProactiveSuggester, etc.
    from .session_state import get_session_state
    state = get_session_state()
    state.on_user_message(sid, msg)
    return None


def _format_recipe_context(resolution, path):
    content = resolution.get("content") or "{}"
    try:
        import json
        recipe = json.loads(content)
    except Exception:
        recipe = {"raw": content}
    lines = [
        "[NUCLEUS] Research-backed fix recipe available.",
        f"Path: {path}",
    ]
    problem = recipe.get("problem_pattern")
    if problem:
        lines.append(f"Problem pattern: {problem}")
    steps = recipe.get("steps") or []
    if steps:
        lines.append("Steps:")
        for step in steps[:5]:
            lines.append(f"- {step}")
    success = recipe.get("success_criteria")
    if success:
        lines.append(f"Success criteria: {success}")
    sources = recipe.get("source_refs") or []
    if sources:
        lines.append("Sources:")
        for source in sources[:5]:
            lines.append(f"- {source}")
    return "\n".join(lines)


def _resolve_session_id(session_id, state, task_id=None) -> str:
    """Hermes core does not pass session_id to pre/post_tool_call hooks
    (they may only get task_id). Prefer an explicit session_id, then an
    unambiguous task_id/session mapping, and only then a single recent
    session. Never guess across multiple active sessions.
    """
    sid = str(session_id or "")
    if sid:
        return sid
    try:
        resolved = state.resolve_implicit_session_id(str(task_id or ""))
        if resolved:
            return resolved
    except Exception:
        pass
    return ""


# P2.11: per-session, in-turn tool call counter. Reset on each user turn.
_IN_TURN_TOOL_COUNTS: Dict[str, Dict[str, int]] = {}
_IN_TURN_TOOL_COUNTS_LOCK = threading.Lock()

# P4.4: per-session task graph step counter. Prevents auto-chaining all steps.
_TASK_GRAPH_STEPS_THIS_TURN: Dict[str, int] = {}
_TASK_GRAPH_STEPS_LOCK = threading.Lock()
_MAX_TASK_STEPS_PER_TURN = 2

# P4.1: per-session verification flags. Set when a mutation tool finishes.
_PENDING_VERIFICATIONS: Dict[str, list] = {}
_PENDING_VERIFICATIONS_LOCK = threading.Lock()


def _flag_pending_verification(session_id: str, tool_name: str, result: str) -> None:
    """Flag that the next turn should verify a mutation."""
    if not session_id:
        return
    # Only flag if the tool result looks successful (no error)
    result_str = str(result or "")[:500].lower()
    if any(e in result_str for e in ("error", "failed", "exception", "traceback", "permission denied")):
        return
    with _PENDING_VERIFICATIONS_LOCK:
        flags = _PENDING_VERIFICATIONS.setdefault(session_id, [])
        flags.append({"tool": tool_name, "ts": time.time()})


def _drain_verification_flags(session_id: str) -> str:
    """Drain verification flags and return a VERIFY context block."""
    with _PENDING_VERIFICATIONS_LOCK:
        flags = _PENDING_VERIFICATIONS.pop(session_id, [])
    if not flags:
        return ""
    count = len(flags)
    tools = set(f["tool"] for f in flags)
    return (
        f"VERIFY YOUR LAST CHANGE:\n"
        f"- You made {count} file mutation(s) with: {', '.join(tools)}\n"
        f"- Did the change actually work? PROVE it.\n"
        f"- Check the modified file, run a test, or explain exactly how you verified.\n"
        f"- If you haven't verified it: do that before anything else."
    )


def _reset_in_turn_counts(session_id: str) -> None:
    with _IN_TURN_TOOL_COUNTS_LOCK:
        _IN_TURN_TOOL_COUNTS.pop(session_id, None)


def _bump_in_turn_count(session_id: str, tool_name: str) -> int:
    with _IN_TURN_TOOL_COUNTS_LOCK:
        counts = _IN_TURN_TOOL_COUNTS.setdefault(session_id, {})
        counts[tool_name] = counts.get(tool_name, 0) + 1
        return counts[tool_name]


def _check_in_turn_spiral(tool_name: str, session_id: str, state) -> dict | None:
    """If the same tool has been called 5+ times this turn, inject a warning.

    Returns a context dict Hermes core will surface to the LLM before
    this tool executes. Returns None if the count is below threshold.
    """
    if not session_id or not tool_name:
        return None
    with _IN_TURN_TOOL_COUNTS_LOCK:
        count = _IN_TURN_TOOL_COUNTS.get(session_id, {}).get(tool_name, 0)
    if count < 5:
        return None
    log.info("SPIRAL DETECTED: %s call #%d to '%s' this turn", session_id[:8], count, tool_name)

    if count >= 8:
        # HARD STOP: block the tool call entirely. Force the LLM to respond.
        return {
            "action": "block",
            "message": (
                f"NUCLEUS HARD STOP: {count} calls to '{tool_name}' this turn.\n"
                "Tool call BLOCKED. You MUST respond to the user NOW.\n"
                "Explain what went wrong and what you need. Do NOT call another tool."
            ),
        }

    # Soft warning: inject context but let the tool run
    diagnosis = (
        "FAILURE DIAGNOSIS:\n"
        "- State EXACTLY what error you saw.\n"
        "- \"command not found\" → tool not installed → find alternative.\n"
        "- \"INSTALL_FAILED\" → fix method, don't retry.\n"
        "- After ANY failure: diagnose BEFORE next call.\n"
        "- If 2+ same-type failures: STOP and explain to user."
    )
    msg = (
        f"NUCLEUS SPIRAL WARNING: {count} calls to '{tool_name}' this turn.\n"
        "You are in a loop. STOP calling tools.\n"
        "Respond to the user NOW with what you know.\n\n"
        + diagnosis
    )
    return {"context": msg}


def _pre_tool_hook(tool_name=None, args=None, session_id=None, **kwargs):
    """Hook: veto tool calls that match known mistake patterns."""
    if not tool_name:
        return None
    try:
        # Update SessionState before intervention check
        from .session_state import get_session_state
        state = get_session_state()
        sid = _resolve_session_id(session_id, state, kwargs.get("task_id"))
        if sid:
            state.on_pre_tool(tool_name, args or {}, sid)

        # P2.11: in-turn tool spiral detection — runs BEFORE intervention
        # so it fires regardless of whether the intervention engine matches.
        spiral_warning = _check_in_turn_spiral(tool_name, sid, state)
        if spiral_warning:
            return spiral_warning

        from .intervention import InterventionEngine
        eng = InterventionEngine()
        intervention = eng.check(tool_name, args, sid)
        if not intervention:
            return None

        severity = intervention.get("severity") or intervention.get("action") or "block"
        if severity == "block":
            log.info(
                "INTERVENTION blocked tool=%s pattern=%r confidence=%.2f",
                tool_name, intervention.get("pattern"), intervention.get("confidence", 0),
            )
            return intervention

        # P1.2: non-critical interventions become one-shot warnings surfaced on
        # the next pre_llm_call. Hermes core silently ignores actions other than
        # "block", so the tool will run as requested — but the LLM will see the
        # warning on its next turn (unless auto_resolve_on clears it first).
        if not sid:
            log.info(
                "INTERVENTION warn dropped without session tool=%s pattern=%r",
                tool_name, intervention.get("pattern"),
            )
            return None
        state.queue_warning(
            sid,
            {
                "pattern": intervention.get("pattern"),
                "message": intervention.get("message"),
                "auto_resolve_on": intervention.get("auto_resolve_on") or {},
                "confidence": intervention.get("confidence", 0),
            },
        )
        log.info(
            "INTERVENTION warned tool=%s pattern=%r confidence=%.2f",
            tool_name, intervention.get("pattern"), intervention.get("confidence", 0),
        )
        return None
    except Exception as e:
        log.warning("_pre_tool_hook failed: %s", e)
        return None


def _post_tool_hook(tool_name=None, tool_result=None, result=None, session_id=None, **kwargs):
    """Hook: learn from tool results — add successful patterns to graph."""
    tool_output = tool_result if tool_result is not None else result
    if not tool_name or tool_output is None:
        return None
    try:
        # Update SessionState after tool execution
        from .session_state import get_session_state
        state = get_session_state()
        sid = _resolve_session_id(session_id, state, kwargs.get("task_id"))
        if not sid:
            return None
        state.on_post_tool(tool_name, tool_output, sid)

        # P2.11: track in-turn tool call counts for spiral detection
        _bump_in_turn_count(sid, tool_name)

        # P4.1: post-mutation verification flag. After write_file/patch,
        # flag that the next turn should verify the change.
        if tool_name in ("write_file", "patch"):
            _flag_pending_verification(sid, tool_name, tool_output)

        # P4.3: auto-record tool results into active task graph
        _record_to_task_graph(sid, tool_name, tool_output)

        nucleus = _get_nucleus()
        # If a tool succeeded, record it as a known solution pattern
        if not isinstance(tool_output, str):
            tool_output = str(tool_output)
        turn_lane = ''
        try:
            from live_brain_ctx.modules.hooks import get_turn_lane  # type: ignore
            turn_lane = get_turn_lane(sid)
        except Exception:
            turn_lane = ''
        from .failure_trigger import tool_failure_problem
        failure_problem = tool_failure_problem(tool_name, tool_output)
        if not failure_problem:
            node_label = f"hermes_tool_{tool_name}"
            if not nucleus.pargod.get_node(node_label):
                nucleus.pargod.add_node("tool", node_label, f"Hermes tool: {tool_name}")
            nucleus.pargod.record_use(node_label)
        else:
            if turn_lane == 'simple_execution':
                return None
            from .failure_trigger import schedule_epistemic_research
            status = schedule_epistemic_research(
                nucleus,
                failure_problem,
                trigger="tool_failure",
                session_id=sid,
                turn_lane=turn_lane,
            )
            log.info("AUTO-RESEARCH tool_failure status=%s tool=%s", status, tool_name)
        return None
    except Exception as e:
        log.warning("_post_tool_hook failed: %s", e)
        return None


def _record_to_task_graph(session_id: str, tool_name: str, tool_output: Any) -> None:
    """Auto-record tool results into active task graphs for learning.

    Auto-advances the graph: if the current node's tool_hint matches
    the executed tool, mark it complete. If the tool failed, mark it
    failed (blocking dependents). This means the agent doesn't need
    to manually call brain_task_graph complete/fail — it just works.
    """
    if not session_id or not tool_name:
        return

    # P4.4: limit task graph steps per turn to prevent chaining all 7 steps
    with _TASK_GRAPH_STEPS_LOCK:
        steps = _TASK_GRAPH_STEPS_THIS_TURN.get(session_id, 0)
        if steps >= _MAX_TASK_STEPS_PER_TURN:
            return
        _TASK_GRAPH_STEPS_THIS_TURN[session_id] = steps + 1

    try:
        db_path = Path.home() / ".hermes" / "live_brain" / "live_brain.db"
        if not db_path.exists():
            return
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        from live_brain.task_graph import TaskGraph
        tg = TaskGraph(conn)
        graphs = tg.active_graphs()
        if not graphs:
            conn.close()
            return
        for g in graphs:
            node = tg.next_node(g["graph_id"])
            if not node:
                continue
            # Auto-advance: if current node has no tool_hint, complete it
            # when ANY tool succeeds. If it has a tool_hint, match exactly.
            result_str = str(tool_output)[:1000]
            tool_ok = _tool_result_ok(tool_name, tool_output)
            hinted = node.get("tool_hint", "")
            if hinted and hinted == tool_name:
                if tool_ok:
                    tg.complete_node(node["node_id"], result_str, session_id, tool_name, "", 0)
                    log.info("TASK GRAPH auto-complete: %s → %s", tool_name, node["description"][:60])
                else:
                    tg.fail_node(node["node_id"], result_str, session_id, tool_name)
                    log.info("TASK GRAPH auto-fail: %s → %s", tool_name, node["description"][:60])
            elif not hinted and tool_ok:
                # Node has no specific tool hint — any success advances it
                tg.complete_node(node["node_id"], result_str, session_id, tool_name, "", 0)
                log.info("TASK GRAPH auto-complete (any tool): %s → %s", tool_name, node["description"][:60])
        conn.close()
    except Exception:
        pass


def _tool_result_ok(tool_name: str, result: Any) -> bool:
    """Check if a tool result indicates success."""
    s = str(result or "").lower()
    if any(e in s for e in ("error", "failed", "exception", "traceback",
                              "command not found", "cannot stat", "no such file",
                              "timed out", "exit_code\": 1", "exit_code\": 2")):
        return False
    return True


def _check_response_drift(user_message: str, assistant_response: str, session_id: str) -> None:
    """Detect when the LLM's response has nothing to do with the user's message.

    If the user said something short (<150 chars) and the response is long
    (>300 chars) with minimal word overlap, queue a drift warning for the
    next turn. Pure rule-based — 0 LLM calls.
    """
    if len(user_message) > 150 or len(assistant_response) < 300:
        return
    if not session_id:
        return

    import re
    # Extract meaningful words (3+ chars, no stopwords)
    def _words(text):
        stop = {'the', 'and', 'for', 'that', 'this', 'with', 'from', 'your',
                'have', 'are', 'was', 'not', 'you', 'what', 'how', 'can',
                'je', 'si', 'se', 'da', 'ne', 'na', 'za', 'od', 'do', 'u',
                'sam', 'smo', 'ti', 'mi', 'to', 'što', 'sto', 'koji', 'kao',
                'će', 'ce', 'bi', 'li', 'sve', 'sad', 'samo', 'još', 'jos',
                'i', 'a', 'o', 'e', 'ili', 'ali', 'pa', 'te', 'me', 'ga',
                'это', 'что', 'как', 'для', 'все', 'уже', 'еще', 'или',
                'це', 'що', 'як', 'для', 'все', 'вже', 'ще', 'або',
                'the', 'is', 'it', 'of', 'in', 'to', 'be', 'on', 'at'}
        found = set()
        for w in re.findall(r'[\wćčšđžĆČŠĐŽ]{3,}', text.lower()):
            if w not in stop:
                found.add(w)
        return found

    user_words = _words(user_message)
    resp_words = _words(assistant_response)

    if not user_words:
        return

    overlap = user_words & resp_words
    if len(overlap) >= 2:
        return  # enough overlap — response is on-topic

    log.info("DRIFT DETECTED: user=%d words, response=%d words, overlap=%d — queuing warning",
             len(user_words), len(resp_words), len(overlap))

    # Drift detected — queue warning for next turn
    drift_msg = (
        "NUCLEUS DRIFT WARNING:\n"
        f"Your last response ({len(assistant_response)} chars) showed minimal overlap "
        f"with the user's message ({len(user_message)} chars).\n"
        f"User words: {', '.join(sorted(user_words)[:10])}\n"
        f"Response words: {', '.join(sorted(resp_words)[:10])}\n"
        "STAY ON TOPIC. Answer ONLY what the user asked. Do NOT invent new subjects."
    )

    try:
        from .session_state import get_session_state
        state = get_session_state()
        state.queue_warning(session_id, {
            "pattern": "response_drift",
            "message": drift_msg,
            "confidence": 0.85,
        })
    except Exception:
        pass


def _post_llm_hook(user_message=None, assistant_response=None, **kwargs):
    """Hook: trigger background research + detect response drift (hallucination guard)."""
    if not user_message or not assistant_response:
        return None
    try:
        sid = str(kwargs.get("session_id") or "")

        # P2.10: response drift detector. If the user said something short
        # and the response is long + completely unrelated, warn on next turn.
        _check_response_drift(user_message, assistant_response, sid)
        turn_lane = ''
        try:
            from live_brain_ctx.modules.hooks import get_turn_lane  # type: ignore
            turn_lane = get_turn_lane(sid)
        except Exception:
            turn_lane = ''

        # Lane metadata is best-effort across plugin hook order. If it is absent,
        # still let the content classifier decide; only explicit non-research
        # lanes suppress background research.
        if turn_lane in {'chit_chat', 'approval_flow'}:
            return None
        from .failure_trigger import schedule_epistemic_research, should_research_after_llm
        if not should_research_after_llm(str(user_message), str(assistant_response)):
            return None
        nucleus = _get_nucleus()
        status = schedule_epistemic_research(
            nucleus,
            str(user_message),
            trigger="llm_gap",
            session_id=sid,
            turn_lane=turn_lane,
        )
        log.info("AUTO-RESEARCH llm_gap status=%s", status)
        return None
    except Exception as e:
        log.warning("_post_llm_hook failed: %s", e)
        return None


def _on_session_finalize(session_id=None, **kwargs):
    """Clear per-session runtime state only at true Hermes session teardown."""
    from .session_state import get_session_state
    from .failure_trigger import clear_session_research_state

    sid = str(session_id or "")
    state = get_session_state()
    state.on_session_finalize(sid)
    clear_session_research_state(sid)
    return None


def register(ctx):
    """Hermes plugin entry point."""
    # Apply the monkey-patch (exclusive LLM bypass)
    try:
        _apply_monkey_patch()
    except Exception as exc:
        log.warning("Nucleus monkey-patch failed (continuing): %s", exc)

    # Register hooks as secondary mechanism. We do this BEFORE starting the
    # daemon so that even if Nucleus() init crashes (e.g. corrupted pargod.db),
    # the hooks still exist and use the degraded stub set up by _get_nucleus().
    ctx.register_hook("pre_llm_call", _pre_llm_hook)
    ctx.register_hook("pre_tool_call", _pre_tool_hook)
    ctx.register_hook("post_tool_call", _post_tool_hook)
    ctx.register_hook("post_llm_call", _post_llm_hook)
    ctx.register_hook("on_session_finalize", _on_session_finalize)

    # P3.1: register the contribution function with the live_brain_ctx
    # bridge. live_brain_ctx will pull our contributions during its
    # pre_llm_call and merge them into a single assembled context — so
    # Hermes core only sees one context injection per turn.
    #
    # We try the import at register() time, but plugin discovery order
    # means live_brain_ctx may not yet be on sys.path. If that's the case,
    # _ensure_contributor_registered() retries lazily on the first hook
    # invocation, when all plugins are loaded.
    _ensure_contributor_registered()

    log.info("Nucleus plugin registered (heartbeat disabled — hooks only)")
