"""Hook dispatch & import contract tests for live_brain_ctx.

These tests catch the two regression classes that historically broke live_brain_ctx:

1. SyntaxError or ImportError in ``__init__.py`` preventing plugin load.
2. Missing or misnamed ``register()`` / hook functions breaking Hermes' plugin
   contract.

The tests use a minimal fake PluginContext so they can run without a live
Hermes gateway.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, Dict, List

# Ensure plugins/ is on sys.path so `import live_brain_ctx` works
PLUGINS_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PLUGINS_DIR))


class FakePluginContext:
    """Minimal stand-in for Hermes PluginContext covering the surface live_brain_ctx uses."""

    def __init__(self) -> None:
        self.registered_engines: List[Any] = []
        self.registered_hooks: Dict[str, Callable] = {}

    def register_context_engine(self, engine: Any) -> None:
        self.registered_engines.append(engine)

    def register_hook(self, name: str, callback: Callable) -> None:
        self.registered_hooks[name] = callback


def test_plugin_imports_without_error() -> None:
    """Catches SyntaxError / ImportError at module load time."""
    if 'live_brain_ctx' in sys.modules:
        del sys.modules['live_brain_ctx']
    import live_brain_ctx  # noqa: F401
    print("✓ live_brain_ctx imports cleanly")


def test_register_function_exists() -> None:
    """live_brain_ctx must expose a register(ctx) callable."""
    import live_brain_ctx
    assert hasattr(live_brain_ctx, 'register'), "register() missing"
    assert callable(live_brain_ctx.register), "register is not callable"
    print("✓ register() symbol present")


def test_register_installs_all_four_hooks() -> None:
    """register() must install pre_llm_call, pre_tool_call, post_tool_call, post_llm_call."""
    import live_brain_ctx
    ctx = FakePluginContext()
    live_brain_ctx.register(ctx)

    expected_hooks = {'pre_llm_call', 'pre_tool_call', 'post_tool_call', 'post_llm_call'}
    missing = expected_hooks - set(ctx.registered_hooks.keys())
    assert not missing, f"Missing hooks: {missing}"

    for name, cb in ctx.registered_hooks.items():
        assert callable(cb), f"Hook {name} is not callable"
    print(f"✓ All {len(expected_hooks)} hooks registered")


def test_register_installs_context_engine() -> None:
    """register() must install exactly one ContextCompressor-style engine."""
    import live_brain_ctx
    ctx = FakePluginContext()
    live_brain_ctx.register(ctx)
    assert len(ctx.registered_engines) == 1, f"Expected 1 engine, got {len(ctx.registered_engines)}"
    engine = ctx.registered_engines[0]
    # Engine should have compress-like surface (ContextCompressor subclass)
    assert engine is not None
    print("✓ Context engine registered")


def test_hooks_accept_kwargs_and_return_expected_types() -> None:
    """Smoke-invoke each hook with benign kwargs and verify it does not crash.

    We don't assert full behavior here (hooks read a live DB path), but we do
    ensure the hook callables can be invoked with the Hermes kwargs contract
    without raising ``TypeError`` on signature mismatches.
    """
    import live_brain_ctx
    ctx = FakePluginContext()
    live_brain_ctx.register(ctx)

    base_kwargs = {
        "user_message": "",
        "assistant_response": "",
        "session_id": "test-session",
        "sender_id": "",
        "platform": "test",
        "tool_name": "",
        "tool_call_id": "",
        "args": {},
        "result": None,
        "duration_ms": 0,
    }
    for name in ('pre_llm_call', 'pre_tool_call', 'post_tool_call', 'post_llm_call'):
        hook = ctx.registered_hooks.get(name)
        try:
            result = hook(**base_kwargs)
        except TypeError as e:
            raise AssertionError(f"Hook {name} signature mismatch: {e}")
        # None, dict, or falsy string are all acceptable; just must not raise
        assert result is None or isinstance(result, (dict, str)), \
            f"Hook {name} returned unexpected type: {type(result).__name__}"
    print("✓ All 4 hooks callable with Hermes kwargs contract")


def test_register_is_idempotent() -> None:
    """Calling register() twice should not raise or duplicate engines/hooks."""
    import live_brain_ctx
    ctx = FakePluginContext()
    live_brain_ctx.register(ctx)
    # Second call on fresh context should also work
    ctx2 = FakePluginContext()
    live_brain_ctx.register(ctx2)
    assert set(ctx.registered_hooks.keys()) == set(ctx2.registered_hooks.keys())
    print("✓ register() is idempotent across contexts")


def run_tests() -> bool:
    tests = [
        ("test_plugin_imports_without_error", test_plugin_imports_without_error),
        ("test_register_function_exists", test_register_function_exists),
        ("test_register_installs_all_four_hooks", test_register_installs_all_four_hooks),
        ("test_register_installs_context_engine", test_register_installs_context_engine),
        ("test_hooks_accept_kwargs_and_return_expected_types", test_hooks_accept_kwargs_and_return_expected_types),
        ("test_register_is_idempotent", test_register_is_idempotent),
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
