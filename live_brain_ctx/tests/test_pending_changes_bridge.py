"""P3.2 — bridge module round-trip test (portable subset).

The full P3.2 wiring in Hermes also covers:
  - SessionState publishing pending_changes after add_pending_change.
  - _load_recent_risk_warnings_block merging bridge entries with DB rows.

Those tests are not portable to ATLANTIS — they require the `nucleus`
plugin and a `_load_recent_risk_warnings_block` helper that don't exist
in this tree. The bridge module itself is standalone, so the round-trip
contract is what we lock in here.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def test_pending_change_flows_to_bridge():
    from live_brain_ctx.modules.bridge import (
        get_pending_changes,
        share_pending_changes,
        reset_all_state,
    )
    reset_all_state()

    share_pending_changes("sess-3", [
        {"type": "patch", "path": "/x.py", "desc": "patch /x.py", "risk": 0.6, "has_backup": False, "ts": 1.0},
    ])
    out = get_pending_changes("sess-3")
    assert len(out) == 1 and out[0]["path"] == "/x.py"
    print("✓ test_pending_change_flows_to_bridge")


if __name__ == "__main__":
    test_pending_change_flows_to_bridge()
    print("\nbridge round-trip test passed.")
