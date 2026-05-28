"""Quick tests for InterventionEngine."""
import sys
from pathlib import Path

PLUGIN_PARENT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PLUGIN_PARENT))

from nucleus.intervention import InterventionEngine


def test_intervention_blocks_chmod_777():
    eng = InterventionEngine()
    result = eng.check("terminal", {"command": "chmod 777 /tmp/test"}, "test-session")
    assert result is not None, "Should block chmod 777"
    # P1.2: chmod 777 stays critical → action=block.
    assert result["action"] == "block"
    assert result.get("severity") == "block"
    assert "777" in result["message"]
    print("✓ test_intervention_blocks_chmod_777")


def test_intervention_warns_on_patch_without_context():
    eng = InterventionEngine()
    result = eng.check("patch", {"old_string": "foo", "new_string": "bar"}, "test-session")
    assert result is not None, "Should react to ambiguous patch"
    # P1.2: ambiguous patch is non-critical → action=warn, not block.
    assert result["action"] == "warn"
    assert result.get("severity") == "warn"
    print("✓ test_intervention_warns_on_patch_without_context")


def test_intervention_allows_safe_commands():
    eng = InterventionEngine()
    result = eng.check("terminal", {"command": "ls -la"}, "test-session")
    assert result is None, "Should not block safe command"
    result = eng.check("read_file", {"path": "/etc/hosts"}, "test-session")
    assert result is None, "Should not block read_file"
    print("✓ test_intervention_allows_safe_commands")


def test_repeat_sqlite_error_is_session_isolated():
    from nucleus.session_state import reset_session_state, get_session_state

    reset_session_state()
    state = get_session_state()
    eng = InterventionEngine()

    state.on_user_message("sqlite-a", "run sqlite command")
    state.on_post_tool(
        "terminal",
        {"output": "", "exit_code": 1, "error": "sqlite3.OperationalError: database is locked"},
        "sqlite-a",
    )

    state.on_user_message("sqlite-b", "separate session")
    result = eng.check("terminal", {"command": "sqlite3 app.db '.tables'"}, "sqlite-b")
    assert result is None or result.get("pattern") != "repeat_sqlite_error", result
    print("✓ test_repeat_sqlite_error_is_session_isolated")


def test_repeat_sqlite_error_uses_requested_session_snapshot():
    from nucleus.session_state import reset_session_state, get_session_state

    reset_session_state()
    state = get_session_state()
    eng = InterventionEngine()

    state.on_user_message("sqlite-a", "run sqlite command")
    state.on_post_tool(
        "terminal",
        {"output": "", "exit_code": 1, "error": "sqlite3.OperationalError: database is locked"},
        "sqlite-a",
    )
    state.on_user_message("sqlite-b", "separate session")
    state.on_post_tool(
        "terminal",
        {"output": "", "exit_code": 0, "error": None},
        "sqlite-b",
    )

    result = eng.check("terminal", {"command": "sqlite3 app.db '.tables'"}, "sqlite-b")
    assert result is None or result.get("pattern") != "repeat_sqlite_error", result
    print("✓ test_repeat_sqlite_error_uses_requested_session_snapshot")


def test_empty_session_id_does_not_use_active_session_dynamic_state():
    from nucleus.session_state import reset_session_state, get_session_state

    reset_session_state()
    state = get_session_state()
    eng = InterventionEngine()

    state.on_user_message("active-session", "patch code")
    result = eng.check("patch", {"path": "/tmp/unread.py"}, "")
    assert result is None, result
    print("✓ test_empty_session_id_does_not_use_active_session_dynamic_state")


def test_intervention_stats():
    eng = InterventionEngine()
    stats = eng.get_stats()
    assert stats["patterns"] >= 10, f"Expected 10+ patterns, got {stats['patterns']}"
    print(f"✓ test_intervention_stats: {stats['patterns']} patterns")


if __name__ == "__main__":
    test_intervention_blocks_chmod_777()
    test_intervention_warns_on_patch_without_context()
    test_intervention_allows_safe_commands()
    test_repeat_sqlite_error_is_session_isolated()
    test_repeat_sqlite_error_uses_requested_session_snapshot()
    test_empty_session_id_does_not_use_active_session_dynamic_state()
    test_intervention_stats()
    print("\n✅ ALL INTERVENTION TESTS PASSED")
