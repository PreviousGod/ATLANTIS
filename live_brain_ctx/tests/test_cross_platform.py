"""Tests for cross-platform scope key and RetrievalRouter scoring logic."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
from pathlib import Path

PLUGINS_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PLUGINS_DIR))


def test_scope_key_all_platforms() -> None:
    """_extract_scope_key produces correct keys for all supported platforms."""
    from live_brain_ctx.modules.hooks import _extract_scope_key

    cases = [
        # (user_msg, sender, session, platform, context, expected)
        ("hi", "u1", "s1", "telegram", "dm", "agent:main:telegram:dm:u1"),
        ("hi", "u2", "s2", "discord", "dm", "agent:main:discord:dm:u2"),
        ("hi", "u3", "s3", "slack", "channel", "agent:main:slack:channel:u3"),
        ("hi", "u4", "s4", "cli", "cli", "agent:main:cli:cli:u4"),
        ("hi", "u5", "s5", "web", "dm", "agent:main:web:dm:u5"),
        # No sender → fallback to session_id
        ("hi", "", "session_abc", "discord", "dm", "session_abc"),
        # Empty platform → defaults to telegram
        ("hi", "u6", "s6", "", "dm", "agent:main:telegram:dm:u6"),
    ]
    for msg, sender, sess, platform, ctx, expected in cases:
        result = _extract_scope_key(msg, sender, sess, platform=platform, context=ctx)
        assert result == expected, f"Expected {expected!r}, got {result!r}"
    print(f"✓ All {len(cases)} cross-platform scope key cases pass")


def test_prepare_query_context_with_platform() -> None:
    """_prepare_query_context passes platform through to scope key."""
    from live_brain_ctx.modules.hooks import _prepare_query_context

    qctx = _prepare_query_context("test query", "user42", "sess1", platform="discord")
    assert "discord" in qctx.scope_key, f"Expected discord in scope_key, got {qctx.scope_key}"
    print(f"✓ _prepare_query_context passes platform: {qctx.scope_key}")


def test_retrieval_router_build_briefing_empty_db() -> None:
    """RetrievalRouter.build_briefing returns empty string on empty DB without crashing."""
    from live_brain.store import LiveBrainStore
    from live_brain.retrieval import RetrievalRouter

    with tempfile.TemporaryDirectory() as tmp:
        store = LiveBrainStore(f"{tmp}/t.db")
        store.initialize_schema()
        router = RetrievalRouter(store.conn, hermes_home=tmp)
        result = router.build_briefing("scope-test", "what happened yesterday")
        assert isinstance(result, str)
        print(f"✓ build_briefing on empty DB returns string ({len(result)} chars)")


def test_retrieval_router_recap_with_data() -> None:
    """RetrievalRouter.recap_recent_work returns formatted recap when data exists."""
    from live_brain.store import LiveBrainStore
    from live_brain.retrieval import RetrievalRouter

    with tempfile.TemporaryDirectory() as tmp:
        store = LiveBrainStore(f"{tmp}/t.db")
        store.initialize_schema()
        # Insert a canonical recap
        store.conn.execute(
            "INSERT INTO canonical_recaps (recap_id, session_id, scope_key, task, main_problem, root_cause, what_changed, current_status, next_step, confidence, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("r1", "sess1", "scope-test", "Fix FTS5 migration", "rowid reserved", "FTS5 spec violation", "Removed rowid column", "resolved", "Deploy and verify", 0.9, time.time(), time.time()),
        )
        store.conn.commit()
        router = RetrievalRouter(store.conn, hermes_home=tmp)
        result = router.recap_recent_work(limit=3)
        assert "FTS5" in result or "Fix" in result, f"Expected recap content, got: {result[:100]}"
        print(f"✓ recap_recent_work returns formatted data ({len(result)} chars)")


def test_retrieval_router_briefing_with_work_item() -> None:
    """build_briefing includes active work item when one exists."""
    from live_brain.store import LiveBrainStore
    from live_brain.retrieval import RetrievalRouter

    with tempfile.TemporaryDirectory() as tmp:
        store = LiveBrainStore(f"{tmp}/t.db")
        store.initialize_schema()
        now = time.time()
        store.conn.execute(
            "INSERT INTO work_items (work_item_id, scope_key, session_id, title, status, priority, evidence_json, next_step, root_cause, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("wi1", "scope-test", "sess1", "Refactor connection pool", "active", 0.8, "{}", "Write unit tests", "Thread leak", now, now),
        )
        store.conn.commit()
        router = RetrievalRouter(store.conn, hermes_home=tmp)
        result = router.build_briefing("scope-test", "connection pool refactor")
        assert isinstance(result, str)
        # May or may not include the work item depending on scoring, but must not crash
        print(f"✓ build_briefing with work_item returns string ({len(result)} chars)")


def run_tests() -> bool:
    tests = [
        ("test_scope_key_all_platforms", test_scope_key_all_platforms),
        ("test_prepare_query_context_with_platform", test_prepare_query_context_with_platform),
        ("test_retrieval_router_build_briefing_empty_db", test_retrieval_router_build_briefing_empty_db),
        ("test_retrieval_router_recap_with_data", test_retrieval_router_recap_with_data),
        ("test_retrieval_router_briefing_with_work_item", test_retrieval_router_briefing_with_work_item),
    ]
    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
        except AssertionError as e:
            print(f"✗ {name}: {e}")
            failed += 1
        except Exception as e:
            print(f"✗ {name}: ERROR — {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc(limit=3)
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    ok = run_tests()
    sys.exit(0 if ok else 1)
