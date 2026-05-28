"""P1.2 — non-blocking warn flow via SessionState queue.

Covers:
  - intervention.check() returns severity=warn for non-critical patterns
  - intervention.check() still returns severity=block for chmod 777 etc.
  - SessionState.queue_warning / drain_warnings round-trip
  - auto_resolve_on={"signal":"backup_taken"} drops the warning once the
    backup is in place
  - dedupe: same pattern queued twice surfaces once
"""
import sys
from pathlib import Path

PLUGIN_PARENT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PLUGIN_PARENT))

from nucleus.session_state import reset_session_state, get_session_state  # noqa: E402
from nucleus.intervention import InterventionEngine  # noqa: E402

SID = "p12-test"
TARGET = "/tmp/p12_target.py"


def _fresh():
    reset_session_state()
    return get_session_state()


def test_chmod_777_stays_block():
    eng = InterventionEngine()
    res = eng.check("terminal", {"command": "chmod 777 /etc/passwd"}, SID)
    assert res is not None
    assert res["action"] == "block"
    assert res["severity"] == "block"
    print("✓ test_chmod_777_stays_block")


def test_no_backup_is_warn_not_block():
    s = _fresh()
    eng = InterventionEngine()
    s.on_pre_tool("read_file", {"path": TARGET}, SID)
    s.on_post_tool("read_file", "{}", SID)
    s.on_pre_tool("patch", {"path": TARGET, "old_string": "a", "new_string": "b"}, SID)
    res = eng.check(
        "patch", {"path": TARGET, "old_string": "a2", "new_string": "b2"}, SID
    )
    assert res is not None
    assert res["action"] == "warn", res
    assert res.get("auto_resolve_on", {}).get("signal") == "backup_taken"
    print("✓ test_no_backup_is_warn_not_block")


def test_warning_queue_roundtrip():
    s = _fresh()
    s.queue_warning(SID, {
        "pattern": "no_backup_high_risk",
        "message": "[NUCLEUS WARN] backup missing",
        "auto_resolve_on": {},
    })
    drained = s.drain_warnings(SID)
    assert len(drained) == 1
    assert drained[0]["pattern"] == "no_backup_high_risk"
    # Drain is destructive
    assert s.drain_warnings(SID) == []
    print("✓ test_warning_queue_roundtrip")


def test_warning_dedupes_by_pattern():
    s = _fresh()
    for _ in range(3):
        s.queue_warning(SID, {
            "pattern": "patch_without_read",
            "message": "[NUCLEUS WARN] read first",
        })
    drained = s.drain_warnings(SID)
    assert len(drained) == 1
    print("✓ test_warning_dedupes_by_pattern")


def test_backup_warning_auto_resolves_when_backup_taken():
    """If a backup happened between queue and drain, drop the warning."""
    s = _fresh()
    s.on_pre_tool("read_file", {"path": TARGET}, SID)
    s.on_post_tool("read_file", "{}", SID)
    s.on_pre_tool("patch", {"path": TARGET}, SID)
    assert s.has_unbackedup_changes()

    s.queue_warning(SID, {
        "pattern": "no_backup_high_risk",
        "message": "[NUCLEUS WARN] backup missing",
        "auto_resolve_on": {"signal": "backup_taken"},
    })

    # Now take the backup
    s.on_pre_tool("terminal", {"command": f"cp {TARGET} {TARGET}.bak"}, SID)
    s.on_post_tool("terminal", {"output": "", "exit_code": 0, "error": None}, SID)

    drained = s.drain_warnings(SID)
    assert drained == [], f"auto_resolve_on should have dropped the warning, got {drained}"
    print("✓ test_backup_warning_auto_resolves_when_backup_taken")


def test_unrelated_warning_not_auto_resolved():
    s = _fresh()
    s.queue_warning(SID, {
        "pattern": "patch_without_read",
        "message": "[NUCLEUS WARN] read first",
        "auto_resolve_on": {"signal": "file_was_read", "target": "/nope.py"},
    })
    drained = s.drain_warnings(SID)
    assert len(drained) == 1, "file never read, warning should still fire"
    print("✓ test_unrelated_warning_not_auto_resolved")


def test_pending_action_contributes_pending_approval_once():
    s = _fresh()
    s.on_user_message(SID, "nucleus approval check")
    s.queue_pending_action({
        "type": "heal",
        "description": "restart helper after validation",
        "target": "hermes-helper",
        "risk_score": 0.72,
        "proposed_action": "systemctl --user restart hermes-helper",
    }, SID)

    from nucleus.contributions import compute_contributions

    out = compute_contributions(
        session_id=SID,
        user_message="continue",
        turn_lane="deep_execution",
    )
    sections = {item.section: item for item in out}
    assert "PENDING APPROVAL" in sections, sections
    body = sections["PENDING APPROVAL"].body
    assert "hermes-helper" in body
    assert "restart helper" in body
    assert not s.has_pending_actions(SID)

    out2 = compute_contributions(
        session_id=SID,
        user_message="continue",
        turn_lane="deep_execution",
    )
    assert all(item.section != "PENDING APPROVAL" for item in out2)
    print("✓ test_pending_action_contributes_pending_approval_once")


def test_pending_action_survives_chit_chat_lane():
    s = _fresh()
    s.on_user_message(SID, "cao")
    s.queue_pending_action({
        "type": "heal",
        "description": "restart helper after validation",
        "target": "hermes-helper",
        "risk_score": 0.72,
        "proposed_action": "systemctl --user restart hermes-helper",
    }, SID)

    from nucleus.contributions import compute_contributions

    out = compute_contributions(
        session_id=SID,
        user_message="cao",
        turn_lane="chit_chat",
    )
    sections = {item.section: item for item in out}
    assert "PENDING APPROVAL" in sections, sections
    assert "hermes-helper" in sections["PENDING APPROVAL"].body
    print("✓ test_pending_action_survives_chit_chat_lane")


def test_pending_action_survives_bridge_lane_filtering():
    s = _fresh()
    s.on_user_message(SID, "cao")
    s.queue_pending_action({
        "type": "heal",
        "description": "restart helper after validation",
        "target": "hermes-helper",
        "risk_score": 0.72,
    }, SID)

    from nucleus.contributions import compute_contributions
    from live_brain_ctx.modules.bridge import (
        clear_contributors,
        gather_contributions,
        register_contributor,
    )

    clear_contributors()
    register_contributor("nucleus-test", compute_contributions)
    out = gather_contributions(
        session_id=SID,
        user_message="cao",
        turn_lane="chit_chat",
    )
    assert any(item.section == "PENDING APPROVAL" for item in out), out
    assert not s.has_pending_actions(SID)
    clear_contributors()
    print("✓ test_pending_action_survives_bridge_lane_filtering")


def test_distinct_pending_actions_with_same_type_are_preserved():
    s = _fresh()
    s.on_user_message(SID, "approval check")
    s.queue_pending_action({
        "type": "heal",
        "description": "restart helper after validation",
        "target": "hermes-helper",
        "risk_score": 0.72,
        "proposed_action": "systemctl --user restart hermes-helper",
    }, SID)
    s.queue_pending_action({
        "type": "heal",
        "description": "restart watcher after validation",
        "target": "hermes-watcher",
        "risk_score": 0.74,
        "proposed_action": "systemctl --user restart hermes-watcher",
    }, SID)

    from nucleus.contributions import compute_contributions

    out = compute_contributions(
        session_id=SID,
        user_message="continue",
        turn_lane="deep_execution",
    )
    body = next(item.body for item in out if item.section == "PENDING APPROVAL")
    assert "hermes-helper" in body
    assert "hermes-watcher" in body
    print("✓ test_distinct_pending_actions_with_same_type_are_preserved")


if __name__ == "__main__":
    test_chmod_777_stays_block()
    test_no_backup_is_warn_not_block()
    test_warning_queue_roundtrip()
    test_warning_dedupes_by_pattern()
    test_backup_warning_auto_resolves_when_backup_taken()
    test_unrelated_warning_not_auto_resolved()
    test_pending_action_contributes_pending_approval_once()
    test_pending_action_survives_chit_chat_lane()
    test_pending_action_survives_bridge_lane_filtering()
    test_distinct_pending_actions_with_same_type_are_preserved()
    print("\n✅ P1.2 WARN-FLOW TESTS PASSED")
