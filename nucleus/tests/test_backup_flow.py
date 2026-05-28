"""Regression tests for the Nucleus backup-detect bug (P1.1).

The original bug: on_post_tool inspected `result` (often the empty/short
tool output for `cp`) instead of `args.command`. The has_backup flag never
flipped, so a second `patch` after the backup got blocked again. Observed
in session_20260518_132503_412f2637 — the agent abandoned the `patch`
tool and rewrote via raw `terminal`.

Each test simulates the hook sequence the gateway emits:
    on_user_message → on_pre_tool(patch) → on_post_tool(patch)
    → on_pre_tool(terminal cp …) → on_post_tool(terminal …)
    → on_pre_tool(patch)  ← must NOT be re-blocked
"""
import sys
import time
from pathlib import Path
from unittest.mock import patch

PLUGIN_PARENT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PLUGIN_PARENT))

# We import the module, not get_session_state(), so each test gets a fresh
# state. The singleton is reset between tests via reset_session_state().
from nucleus.session_state import (  # noqa: E402
    SessionState,
    reset_session_state,
    get_session_state,
    PendingChange,
)
from nucleus.intervention import InterventionEngine  # noqa: E402


SID = "p11-test-session"
TARGET = "/tmp/p11_target.py"


def _fresh_state() -> SessionState:
    reset_session_state()
    return get_session_state()


def test_cp_backup_flips_flag_via_args_not_result():
    """The fix: post_tool reads the stashed args.command, not `result`."""
    s = _fresh_state()
    s.on_user_message(SID, "patch this file")
    # Pretend the file was already read so intervention only fires on the
    # has_unbackedup_changes() rule (P1.1 scope).
    s.on_pre_tool("read_file", {"path": TARGET}, SID)
    s.on_post_tool("read_file", "{}", SID)

    s.on_pre_tool("patch", {"path": TARGET, "old_string": "a", "new_string": "b"}, SID)
    assert s.has_unbackedup_changes(), "patch should create an unbackedup change"

    # cp is the user's terminal command — its stdout is typically empty.
    s.on_pre_tool("terminal", {"command": f"cp {TARGET} {TARGET}.bak"}, SID)
    s.on_post_tool("terminal", {"output": "", "exit_code": 0, "error": None}, SID)

    assert not s.has_unbackedup_changes(), (
        "After successful cp the pending patch must be marked has_backup=True"
    )
    print("✓ test_cp_backup_flips_flag_via_args_not_result")


def test_failed_cp_does_not_flip_flag():
    """If the cp errored, we must NOT mark the change as backed up."""
    s = _fresh_state()
    s.on_user_message(SID, "patch")
    s.on_pre_tool("read_file", {"path": TARGET}, SID)
    s.on_post_tool("read_file", "{}", SID)
    s.on_pre_tool("patch", {"path": TARGET}, SID)
    assert s.has_unbackedup_changes()

    s.on_pre_tool("terminal", {"command": f"cp {TARGET} {TARGET}.bak"}, SID)
    # Simulate a failure result. `success` is derived from "error"/"failed"
    # appearing in the result string.
    s.on_post_tool(
        "terminal",
        {"output": "", "exit_code": 1, "error": "cp: cannot stat: failed"},
        SID,
    )

    assert s.has_unbackedup_changes(), (
        "Failed backup must leave the change still flagged as unbackedup"
    )
    print("✓ test_failed_cp_does_not_flip_flag")


def test_rsync_and_tar_recognised_as_backup():
    for cmd in (
        f"rsync -a {TARGET} {TARGET}.bak",
        f"tar czf {TARGET}.bak.tar.gz {TARGET}",
        f"mv {TARGET} {TARGET}.bak",
    ):
        s = _fresh_state()
        s.on_pre_tool("read_file", {"path": TARGET}, SID)
        s.on_post_tool("read_file", "{}", SID)
        s.on_pre_tool("patch", {"path": TARGET}, SID)
        s.on_pre_tool("terminal", {"command": cmd}, SID)
        s.on_post_tool("terminal", {"output": "", "exit_code": 0}, SID)
        assert not s.has_unbackedup_changes(), f"backup pattern not recognised: {cmd}"
    print("✓ test_rsync_and_tar_recognised_as_backup")


def test_unrelated_terminal_does_not_flip_flag():
    s = _fresh_state()
    s.on_pre_tool("read_file", {"path": TARGET}, SID)
    s.on_post_tool("read_file", "{}", SID)
    s.on_pre_tool("patch", {"path": TARGET}, SID)
    s.on_pre_tool("terminal", {"command": "ls -la"}, SID)
    s.on_post_tool("terminal", {"output": "drwxr-xr-x …", "exit_code": 0}, SID)
    assert s.has_unbackedup_changes(), "ls must not count as backup"
    print("✓ test_unrelated_terminal_does_not_flip_flag")


def test_backup_marks_only_matching_pending_changes():
    s = _fresh_state()
    sid = SID
    target_a = "/tmp/p11_a.py"
    target_b = "/tmp/p11_b.py"

    s.on_user_message(sid, "patch A and B")
    s.on_pre_tool("read_file", {"path": target_a}, sid)
    s.on_post_tool("read_file", "{}", sid)
    s.on_pre_tool("patch", {"path": target_a}, sid)
    s.on_pre_tool("read_file", {"path": target_b}, sid)
    s.on_post_tool("read_file", "{}", sid)
    s.on_pre_tool("patch", {"path": target_b}, sid)
    assert s.has_unbackedup_changes(sid)

    s.on_pre_tool("terminal", {"command": f"cp {target_a} {target_a}.bak"}, sid)
    s.on_post_tool("terminal", {"output": "", "exit_code": 0, "error": None}, sid)

    changes = s._pending_changes_for_session(sid)
    backed_a = [c for c in changes if c.target_path == target_a and c.has_backup]
    pending_b = [c for c in changes if c.target_path == target_b and not c.has_backup]
    assert backed_a, "Backup command should mark the matching file as backed up"
    assert pending_b, "Unrelated pending change must stay unbacked up"
    assert s.has_unbackedup_changes(sid)
    print("✓ test_backup_marks_only_matching_pending_changes")


def test_backup_does_not_cross_match_same_basename_paths():
    s = _fresh_state()
    sid = SID
    target_a = "/tmp/p11_dir_a/config.yaml"
    target_b = "/tmp/p11_dir_b/config.yaml"

    s.on_user_message(sid, "patch both configs")
    s.on_pre_tool("read_file", {"path": target_a}, sid)
    s.on_post_tool("read_file", "{}", sid)
    s.on_pre_tool("patch", {"path": target_a}, sid)
    s.on_pre_tool("read_file", {"path": target_b}, sid)
    s.on_post_tool("read_file", "{}", sid)
    s.on_pre_tool("patch", {"path": target_b}, sid)

    s.on_pre_tool("terminal", {"command": f"cp {target_a} {target_a}.bak"}, sid)
    s.on_post_tool("terminal", {"output": "", "exit_code": 0, "error": None}, sid)

    changes = s._pending_changes_for_session(sid)
    backed_a = [c for c in changes if c.target_path == target_a and c.has_backup]
    backed_b = [c for c in changes if c.target_path == target_b and c.has_backup]
    assert backed_a, "Target A should be recognized by exact path"
    assert not backed_b, "Same-basename target B must not be cross-matched"
    print("✓ test_backup_does_not_cross_match_same_basename_paths")


def test_pending_changes_are_session_isolated():
    """Risk state from one interleaved session must not warn another session."""
    s = _fresh_state()
    eng = InterventionEngine()
    sid_a = "p11-session-a"
    sid_b = "p11-session-b"
    target_a = "/tmp/p11_a.py"
    target_b = "/tmp/p11_b.py"

    s.on_user_message(sid_a, "patch A")
    s.on_pre_tool("read_file", {"path": target_a}, sid_a)
    s.on_pre_tool("patch", {"path": target_a, "old_string": "a", "new_string": "b"}, sid_a)
    assert s.has_unbackedup_changes(sid_a)

    s.on_user_message(sid_b, "patch B")
    s.on_pre_tool("read_file", {"path": target_b}, sid_b)
    intervention = eng.check(
        "patch",
        {"path": target_b, "old_string": "x", "new_string": "y"},
        sid_b,
    )
    assert intervention is None or intervention.get("pattern") != "no_backup_high_risk", (
        f"Session B inherited Session A pending risk: {intervention}"
    )
    assert not s.has_unbackedup_changes(sid_b)
    print("✓ test_pending_changes_are_session_isolated")


def test_legacy_empty_session_pending_change_does_not_bleed_into_named_session():
    """Legacy orphaned PendingChange(session_id='') must not taint a real session."""
    s = _fresh_state()
    sid = "p11-real-session"
    s.on_user_message(sid, "patch this file")
    s.pending_changes.append(PendingChange(
        change_type="patch",
        target_path="/tmp/orphan.py",
        description="legacy orphan pending change",
        risk_score=0.95,
        timestamp=time.time(),
        has_backup=False,
        session_id="",
    ))

    assert s.pending_change_count(sid) == 0
    assert not s.has_unbackedup_changes(sid)
    assert s.pending_change_count() == 0
    assert not s.has_unbackedup_changes()
    assert s.snapshot("")["pending_changes"] == []
    assert len(s.pending_changes) == 1
    print("✓ test_legacy_empty_session_pending_change_does_not_bleed_into_named_session")


def test_active_files_are_session_isolated():
    """Reading a file in one session must not satisfy patch context in another."""
    s = _fresh_state()
    eng = InterventionEngine()
    target = "/tmp/p11_shared.py"

    s.on_user_message("p11-reader", "inspect file")
    s.on_pre_tool("read_file", {"path": target}, "p11-reader")
    assert s.has_file_been_read(target, "p11-reader")

    s.on_user_message("p11-patcher", "patch file")
    intervention = eng.check(
        "patch",
        {"path": target, "old_string": "x", "new_string": "y"},
        "p11-patcher",
    )
    assert intervention is not None
    assert intervention.get("pattern") == "patch_without_read", intervention
    print("✓ test_active_files_are_session_isolated")


def test_pending_actions_are_session_isolated():
    s = _fresh_state()
    s.on_user_message("p11-actions-a", "session A")
    s.queue_pending_action({
        "type": "heal",
        "description": "repair A",
        "target": "svc-a",
    }, "p11-actions-a")
    s.on_user_message("p11-actions-b", "session B")
    s.queue_pending_action({
        "type": "monitor",
        "description": "watch B",
        "target": "svc-b",
    }, "p11-actions-b")

    assert s.has_pending_actions("p11-actions-a")
    assert s.has_pending_actions("p11-actions-b")
    only_a = s.get_and_clear_pending_actions("p11-actions-a")
    assert len(only_a) == 1 and only_a[0]["target"] == "svc-a"
    assert not s.has_pending_actions("p11-actions-a")
    assert s.has_pending_actions("p11-actions-b")
    print("✓ test_pending_actions_are_session_isolated")


def test_tool_calls_and_intent_are_session_isolated():
    s = _fresh_state()
    sid_a = "p11-history-a"
    sid_b = "p11-history-b"

    s.on_user_message(sid_a, "fix the config")
    s.on_pre_tool("read_file", {"path": "/tmp/a.py"}, sid_a)
    s.on_post_tool("read_file", "{}", sid_a)

    s.on_user_message(sid_b, "show logs")
    snapshot = s.snapshot()
    assert snapshot["session_id"] == sid_b
    assert snapshot["tool_calls_count"] == 0
    assert snapshot["user_intent"] == "query"

    s.on_user_message(sid_a, "fix the config")
    snapshot_a = s.snapshot()
    assert snapshot_a["session_id"] == sid_a
    assert snapshot_a["tool_calls_count"] == 1
    assert snapshot_a["user_intent"] == "fix"
    print("✓ test_tool_calls_and_intent_are_session_isolated")


def test_full_backup_flow_end_to_end():
    """The exact loop from session_20260518_132503_412f2637.

    1. read_file
    2. patch        → warn (no backup)        — P1.2: warn, not block
    3. terminal cp  → success
    4. patch        → must NOT re-warn for the same pattern
    """
    s = _fresh_state()
    eng = InterventionEngine()
    s.on_user_message(SID, "change config")

    s.on_pre_tool("read_file", {"path": TARGET}, SID)
    s.on_post_tool("read_file", "{}", SID)

    # First patch — creates the pending change. Then we ASK the engine what
    # a *second* call would produce. Pre-P1.2 that was "block"; post-P1.2
    # it's "warn" with auto_resolve_on={"signal":"backup_taken"}.
    s.on_pre_tool("patch", {"path": TARGET, "old_string": "x", "new_string": "y"}, SID)
    intervention = eng.check(
        "patch", {"path": TARGET, "old_string": "x2", "new_string": "y2"}, SID
    )
    assert intervention is not None, "Pre-backup patch should produce some intervention"
    assert intervention.get("action") == "warn", (
        f"P1.2: no_backup_high_risk must be warn, got {intervention.get('action')}"
    )
    assert intervention.get("pattern") == "no_backup_high_risk"
    assert intervention.get("auto_resolve_on", {}).get("signal") == "backup_taken"

    # Backup
    s.on_pre_tool("terminal", {"command": f"cp {TARGET} {TARGET}.bak"}, SID)
    s.on_post_tool("terminal", {"output": "", "exit_code": 0, "error": None}, SID)

    # Retry patch — must NOT re-warn for no_backup_high_risk
    intervention2 = eng.check(
        "patch", {"path": TARGET, "old_string": "x2", "new_string": "y2"}, SID
    )
    assert intervention2 is None or intervention2.get("pattern") != "no_backup_high_risk", (
        f"Backup taken but no_backup_high_risk still firing: {intervention2}"
    )
    print("✓ test_full_backup_flow_end_to_end")


def test_session_finalize_clears_local_and_bridge_state():
    import live_brain_ctx.modules.bridge as bridge
    import nucleus as nucleus_plugin

    s = _fresh_state()
    bridge.reset_all_state()
    sid = "p11-finalize-session"

    s.on_user_message(sid, "write config")
    s.queue_pending_action({
        "type": "heal",
        "description": "pending action",
        "target": "nucleus_health",
    })
    # Exercises the write_file risk path, including the pathlib import.
    s.on_pre_tool("write_file", {"path": TARGET}, sid)
    bridge.share_scope(sid, "dm:test", "deep_execution", "fix")

    assert s.pending_change_count() == 1
    assert s.has_pending_actions()
    assert sid in bridge._SESSION_STATE

    nucleus_plugin._on_session_finalize(session_id=sid, platform="test")

    snapshot = s.snapshot()
    assert snapshot["session_id"] == ""
    assert snapshot["user_message"] == ""
    assert snapshot["user_intent"] == ""
    assert snapshot["pending_changes"] == []
    assert snapshot["active_files"] == []
    assert snapshot["write_count"] == 0
    assert snapshot["patch_count"] == 0
    assert not s.has_pending_actions()
    assert sid not in bridge._SESSION_STATE
    print("✓ test_session_finalize_clears_local_and_bridge_state")


def test_finalize_clears_compatibility_fallback_fields():
    s = _fresh_state()
    sid = "p11-compat-clear"
    s.on_user_message(sid, "show logs")
    s.on_post_tool("terminal", {"output": "", "exit_code": 1, "error": "boom"}, sid)

    assert s.user_intent == "query"
    assert s.last_error.startswith("terminal:")
    s.on_session_finalize(sid)

    snapshot = s.snapshot()
    assert snapshot["session_id"] == ""
    assert snapshot["user_intent"] == ""
    assert snapshot["last_error"] == ""
    print("✓ test_finalize_clears_compatibility_fallback_fields")


def test_last_activity_tracks_active_session_only():
    s = _fresh_state()
    sid_a = "p11-activity-a"
    sid_b = "p11-activity-b"

    s.on_user_message(sid_a, "fix config")
    first = s.snapshot()["last_activity"]
    assert first > 0.0

    s.on_user_message(sid_b, "show logs")
    second = s.snapshot()["last_activity"]
    assert second > 0.0
    assert second >= first

    s.on_session_finalize(sid_b)
    assert s.snapshot()["last_activity"] == 0.0
    print("✓ test_last_activity_tracks_active_session_only")


def test_session_finalize_clears_only_target_session_risk():
    import live_brain_ctx.modules.bridge as bridge
    import nucleus as nucleus_plugin

    s = _fresh_state()
    bridge.reset_all_state()
    sid_a = "p11-finalize-a"
    sid_b = "p11-finalize-b"

    s.on_user_message(sid_a, "patch A")
    s.on_pre_tool("read_file", {"path": "/tmp/a.py"}, sid_a)
    s.on_pre_tool("patch", {"path": "/tmp/a.py"}, sid_a)
    bridge.share_scope(sid_a, "dm:a", "deep_execution", "fix")

    s.on_user_message(sid_b, "patch B")
    s.on_pre_tool("read_file", {"path": "/tmp/b.py"}, sid_b)
    s.on_pre_tool("patch", {"path": "/tmp/b.py"}, sid_b)
    bridge.share_scope(sid_b, "dm:b", "deep_execution", "fix")

    assert s.pending_change_count(sid_a) == 1
    assert s.pending_change_count(sid_b) == 1
    nucleus_plugin._on_session_finalize(session_id=sid_a, platform="test")

    assert s.pending_change_count(sid_a) == 0
    assert s.pending_change_count(sid_b) == 1
    assert sid_a not in bridge._SESSION_STATE
    assert sid_b in bridge._SESSION_STATE
    print("✓ test_session_finalize_clears_only_target_session_risk")


def test_register_exposes_session_finalize_hook():
    import nucleus as nucleus_plugin

    class _Ctx:
        def __init__(self):
            self.hooks = {}

        def register_hook(self, name, callback):
            self.hooks[name] = callback

    ctx = _Ctx()
    with patch.object(nucleus_plugin, "_apply_monkey_patch", lambda: None), \
         patch.object(nucleus_plugin, "_ensure_contributor_registered", lambda: None), \
         patch.object(nucleus_plugin, "_get_nucleus", return_value=object()):
        nucleus_plugin.register(ctx)

    assert "on_session_finalize" in ctx.hooks
    assert callable(ctx.hooks["on_session_finalize"])
    print("✓ test_register_exposes_session_finalize_hook")


if __name__ == "__main__":
    test_cp_backup_flips_flag_via_args_not_result()
    test_failed_cp_does_not_flip_flag()
    test_rsync_and_tar_recognised_as_backup()
    test_unrelated_terminal_does_not_flip_flag()
    test_backup_marks_only_matching_pending_changes()
    test_backup_does_not_cross_match_same_basename_paths()
    test_pending_changes_are_session_isolated()
    test_legacy_empty_session_pending_change_does_not_bleed_into_named_session()
    test_active_files_are_session_isolated()
    test_pending_actions_are_session_isolated()
    test_tool_calls_and_intent_are_session_isolated()
    test_full_backup_flow_end_to_end()
    test_session_finalize_clears_local_and_bridge_state()
    test_finalize_clears_compatibility_fallback_fields()
    test_last_activity_tracks_active_session_only()
    test_session_finalize_clears_only_target_session_risk()
    test_register_exposes_session_finalize_hook()
    print("\n✅ P1.1 BACKUP-FLOW TESTS PASSED")
