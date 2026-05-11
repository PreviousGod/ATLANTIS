"""Unit tests for LiveBrainProvider.handle_tool_call dispatch map.

Verifies the ``_tool_handlers()`` map covers every schema in
``get_tool_schemas()``, handles unknown tools gracefully, and returns a
proper error when the store is not initialized.
"""
from __future__ import annotations

import sys
from pathlib import Path

PLUGINS_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PLUGINS_DIR))

from live_brain import LiveBrainProvider  # noqa: E402


def test_unknown_tool_returns_error() -> None:
    provider = LiveBrainProvider()
    # store is None by default (before initialize) — first check
    result = provider.handle_tool_call("no_such_tool", {})
    assert "not initialized" in result.lower()
    print("✓ Uninitialized store returns 'not initialized'")


def test_dispatch_map_covers_every_schema() -> None:
    """Every tool declared in get_tool_schemas must have a handler."""
    provider = LiveBrainProvider()
    schemas = provider.get_tool_schemas()
    declared = {s["name"] for s in schemas}

    # Temporarily set store so _tool_handlers can be built
    class _FakeStore:
        pass
    provider._store = _FakeStore()  # type: ignore[assignment]
    handlers = provider._tool_handlers()

    mapped = set(handlers.keys())
    missing = declared - mapped
    extra = mapped - declared
    assert not missing, f"Schemas without handlers: {missing}"
    assert not extra, f"Handlers without schemas: {extra}"
    print(f"✓ All {len(declared)} schemas have matching handlers, no orphans")


def test_unknown_tool_with_store_returns_error() -> None:
    """Unknown tool name should return a 'Unknown tool' error after init."""
    provider = LiveBrainProvider()
    class _FakeStore:
        pass
    provider._store = _FakeStore()  # type: ignore[assignment]
    result = provider.handle_tool_call("brain_no_such", {})
    assert "unknown tool" in result.lower()
    print("✓ Unknown tool with initialized store returns 'Unknown tool' error")


def test_handlers_are_lazy() -> None:
    """Tool handler map is built on first access, not in __init__."""
    provider = LiveBrainProvider()
    assert provider._tool_handler_map is None, "Handler map should be lazy"
    class _FakeStore:
        pass
    provider._store = _FakeStore()  # type: ignore[assignment]
    _ = provider._tool_handlers()
    assert provider._tool_handler_map is not None
    # Second call returns same reference (cached)
    first = provider._tool_handlers()
    second = provider._tool_handlers()
    assert first is second, "Handler map should be cached"
    print("✓ Handler map is lazy + cached")


def run_tests() -> bool:
    tests = [
        ("test_unknown_tool_returns_error", test_unknown_tool_returns_error),
        ("test_dispatch_map_covers_every_schema", test_dispatch_map_covers_every_schema),
        ("test_unknown_tool_with_store_returns_error", test_unknown_tool_with_store_returns_error),
        ("test_handlers_are_lazy", test_handlers_are_lazy),
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
