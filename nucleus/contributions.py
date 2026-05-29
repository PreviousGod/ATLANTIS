"""P3.1 — Nucleus's context contributions to the live_brain_ctx bridge.

Previously, ``nucleus._pre_llm_hook`` returned ``{"context": str}`` directly
to Hermes core, which concatenated it with live_brain_ctx's own context.
That's the double-injection vector this module replaces.

Now nucleus REGISTERS this module's ``compute_contributions`` function with
the live_brain_ctx bridge. live_brain_ctx pulls contributions during its
pre_llm_call, merges them as named sections, and runs the unified
assembler (byte caps, dedup, budget). nucleus's own hook is reduced to
bookkeeping.

The emissions modelled here mirror the six categories the old hook could
produce — they remain MUTUALLY EXCLUSIVE except for ``NUCLEUS WARN``,
which can ride alongside any of the others.
"""
from __future__ import annotations

import logging
from typing import List

log = logging.getLogger("nucleus.contributions")


def compute_contributions(**kwargs):
    """Return the list of ContextContribution objects for this turn.

    Signature is open-ended (kwargs) so the bridge can pass new fields
    later without breaking the contract.
    """
    # Imported lazily so this module can be imported in tests without the
    # full live_brain_ctx package present.
    from live_brain_ctx.modules.bridge import ContextContribution

    session_id = str(kwargs.get("session_id") or "")
    user_message = str(kwargs.get("user_message") or "")
    if not session_id or not user_message:
        return []

    from .session_state import get_session_state
    state = get_session_state()

    out: List[ContextContribution] = []

    # 0. Stuck detection (P2.6). Runs before warnings so the escape prompt
    # always ships — even if warnings also fire. Universal (all lanes).
    stuck_msg = _detect_stuck_loop(state, session_id)
    if stuck_msg:
        out.append(ContextContribution(
            plugin="nucleus",
            section="NUCLEUS STUCK",
            body=stuck_msg,
            priority=2,
            dedupe_key=f"nucleus.stuck:{session_id}",
        ))

    # 1. Drained warnings (P1.2). Universally applicable — corrective signals.
    pending_warnings = state.drain_warnings(session_id)
    if pending_warnings:
        body = "\n\n".join(w["message"] for w in pending_warnings if w.get("message"))
        if body.strip():
            out.append(ContextContribution(
                plugin="nucleus",
                section="NUCLEUS WARN",
                body=body,
                priority=5,
                dedupe_key=f"nucleus.warn:{session_id}",
            ))

    # 2. Autonomous actions requiring approval. These are session-scoped and
    # one-shot: after live_brain_ctx receives the contribution, the user-facing
    # prompt now carries the approval request instead of silently dropping it.
    pending_actions = state.get_and_clear_pending_actions(session_id)
    if pending_actions:
        lines = ["Nucleus queued autonomous action(s) requiring user approval:"]
        for idx, action in enumerate(pending_actions[:5], 1):
            lines.append(
                f"{idx}. {action.get('type') or 'action'}"
                f" target={action.get('target') or 'unknown'}"
                f" risk={float(action.get('risk_score') or 0):.2f}"
            )
            desc = str(action.get("description") or "").strip()
            if desc:
                lines.append(f"   reason: {desc}")
            proposed = str(action.get("proposed_action") or "").strip()
            if proposed:
                lines.append(f"   proposed: {proposed}")
        lines.append("Ask the user to approve, reject, or modify before executing.")
        out.append(ContextContribution(
            plugin="nucleus",
            section="PENDING APPROVAL",
            body="\n".join(lines),
            priority=3,
            dedupe_key=f"nucleus.pending_approval:{session_id}",
        ))

    # The rest of the cascade preserves the prior-implementation early-return
    # semantics: at most one of (learn, suggest, proactive, continuation,
    # graph) per turn.
    one_shot = _compute_one_shot(state, session_id, user_message, kwargs)
    if one_shot is not None:
        out.append(one_shot)

    return out


def _detect_stuck_loop(state, session_id: str) -> str:
    """Return an escape prompt if the session has 3+ consecutive same-tool failures.

    Reads the tool-call history from session_state. Only fires on deep_execution
    and continuation_or_resume lanes where tool loops are most damaging.
    """
    if not session_id:
        return ""
    snap = state.snapshot(session_id)
    if not snap:
        return ""

    # Only fire on tool-heavy lanes (not chit_chat, not research)
    from .session_state import SessionState
    tool_calls = getattr(state, '_tool_calls_by_session', {}).get(session_id, [])
    if not tool_calls:
        return ""

    # Count consecutive failures of the same tool
    consecutive = 0
    last_tool = ""
    for tc in reversed(tool_calls):
        if tc.success:
            break  # success resets the streak
        if tc.tool_name == last_tool or last_tool == "":
            consecutive += 1
            last_tool = tc.tool_name
        else:
            break  # different tool breaks the streak

    if consecutive < 3:
        return ""

    log.info("STUCK DETECTED: %d consecutive failures of '%s' in session %s",
             consecutive, last_tool, session_id[:8])

    # Build the escape prompt — priority 2 to override informational sections
    return (
        f"NUCLEUS STUCK DETECTED:\n"
        f"- {consecutive} consecutive failures of tool '{last_tool}'.\n"
        f"- STOP all tool calls IMMEDIATELY.\n"
        f"- DIAGNOSE the error: what EXACTLY failed, and WHY?\n"
        f"- \"command not found\" means the tool is NOT installed — find an alternative.\n"
        f"- \"INSTALL_FAILED\" means your packaging is broken — don't retry, fix the method.\n"
        f"- \"ModuleNotFoundError\" means pip install or different approach.\n"
        f"- Do NOT retry '{last_tool}' — it will fail again for the SAME reason.\n"
        f"- Explain the ROOT CAUSE to the user and propose a DIFFERENT approach."
    )


def _compute_one_shot(state, sid, msg, kwargs):
    from live_brain_ctx.modules.bridge import ContextContribution
    from . import (
        _is_safe_nucleus_context_query,
        _is_nucleus_graph_query,
        _get_nucleus,
        _format_recipe_context,
    )

    explicit_nucleus_turn = _is_safe_nucleus_context_query(msg)

    # LearningEngine feedback (only on explicit nucleus turns)
    if explicit_nucleus_turn:
        try:
            from .learning_engine import LearningEngine
            learning = LearningEngine()
            feedback_result = learning.apply_feedback(sid, msg)
        except Exception as exc:
            log.warning("compute_contributions: feedback failed: %s", exc)
            feedback_result = None
        if feedback_result:
            log.info("LEARN feedback applied: %s", feedback_result)
            return ContextContribution(
                plugin="nucleus",
                section="NUCLEUS LEARN",
                body=str(feedback_result),
                priority=10,
                dedupe_key=f"nucleus.learn:{sid}",
            )

    # ProactiveSuggester: reaction OR pending delivery (only explicit)
    if explicit_nucleus_turn:
        try:
            from .proactive_suggester import ProactiveSuggester
            suggester = ProactiveSuggester()
            reaction = suggester.record_user_response(sid, msg)
        except Exception as exc:
            log.warning("compute_contributions: suggester reaction failed: %s", exc)
            reaction = None
            suggester = None
        if reaction:
            log.info("SUGGESTION reaction: %s", reaction)
            return ContextContribution(
                plugin="nucleus",
                section="NUCLEUS SUGGEST",
                body=str(reaction),
                priority=12,
                dedupe_key=f"nucleus.suggest:{sid}",
            )
        pending = suggester.read_and_clear_pending() if suggester else None
        if pending:
            log.info("DELIVERING suggestion: %s", pending.get("message", "")[:50])
            body = (
                f"{pending.get('message', '')}\n"
                f"Suggested action: {pending.get('action', 'N/A')}\n"
                f"Reply 'uradio sam' if done, 'ignoriši' to dismiss."
            )
            return ContextContribution(
                plugin="nucleus",
                section="NUCLEUS PROACTIVE",
                body=body,
                priority=14,
                dedupe_key=f"nucleus.proactive:{sid}",
            )

    # Continuation context — universal (not gated to explicit nucleus turn)
    turn_lane = str(kwargs.get("turn_lane") or "")
    if not turn_lane:
        try:
            from live_brain_ctx.modules.hooks import get_turn_lane  # type: ignore
            turn_lane = get_turn_lane(sid) or ""
        except Exception:
            turn_lane = ""
    try:
        from .failure_trigger import get_continuation_context
        continuation = get_continuation_context(sid, msg, turn_lane=turn_lane)
    except Exception as exc:
        log.warning("compute_contributions: continuation failed: %s", exc)
        continuation = None
    if continuation:
        return ContextContribution(
            plugin="nucleus",
            section="NUCLEUS CONTINUATION",
            body=str(continuation),
            priority=8,
            dedupe_key=f"nucleus.continuation:{sid}",
        )

    # Graph resolution — only on explicit nucleus graph queries
    if not explicit_nucleus_turn:
        return None
    if not _is_nucleus_graph_query(msg):
        return None
    try:
        nucleus = _get_nucleus()
        resolution = nucleus.pargod.has_resolution_for(msg)
    except Exception as exc:
        log.warning("compute_contributions: graph resolve failed: %s", exc)
        resolution = None
    if not resolution:
        return None
    path = " → ".join(resolution.get("path", []))
    rtype = resolution.get("type")
    if rtype == "tool":
        body = f"Graph knows: {resolution['tool']} resolves this (path: {path})"
    elif rtype == "fix_recipe":
        body = _format_recipe_context(resolution, path)
    else:
        return None
    return ContextContribution(
        plugin="nucleus",
        section="NUCLEUS GRAPH",
        body=body,
        priority=11,
        dedupe_key=f"nucleus.graph:{sid}:{rtype}",
    )
