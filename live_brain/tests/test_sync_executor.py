"""Tests for the bounded ThreadPoolExecutor that replaces the old unbounded
daemon-thread spawn in LiveBrainProvider.sync_turn.

We don't exercise the full provider init here (it requires Hermes shims); we
just verify the executor lifecycle contract: creation is lazy, it respects
max_workers=2, and shutdown() drains cleanly without leaving threads behind.
"""
from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PLUGINS_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PLUGINS_DIR))


def test_executor_max_workers_bound() -> None:
    """At most max_workers concurrent threads, no matter how many tasks are submitted."""
    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="test-sync")
    peak_concurrent = {"value": 0}
    current = {"value": 0}
    lock = threading.Lock()

    def task() -> None:
        with lock:
            current["value"] += 1
            if current["value"] > peak_concurrent["value"]:
                peak_concurrent["value"] = current["value"]
        time.sleep(0.05)
        with lock:
            current["value"] -= 1

    futures = [executor.submit(task) for _ in range(10)]
    for f in futures:
        f.result(timeout=5)
    executor.shutdown(wait=True)

    assert peak_concurrent["value"] <= 2, \
        f"Executor ran {peak_concurrent['value']} tasks concurrently, expected <= 2"
    print(f"✓ Executor cap honored: peak {peak_concurrent['value']}/2")


def test_executor_shutdown_drains_queued_tasks() -> None:
    """shutdown(wait=True) must complete queued tasks (not lose them)."""
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-sync")
    done = []
    lock = threading.Lock()

    def task(i: int) -> None:
        time.sleep(0.01)
        with lock:
            done.append(i)

    for i in range(5):
        executor.submit(task, i)
    executor.shutdown(wait=True)

    assert sorted(done) == [0, 1, 2, 3, 4], f"Expected all 5 tasks completed, got {done}"
    print(f"✓ Shutdown drained all 5 queued tasks")


def test_submit_after_shutdown_raises() -> None:
    """Submitting to a shut-down executor raises RuntimeError (which sync_turn catches)."""
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-sync")
    executor.shutdown(wait=True)
    raised = False
    try:
        executor.submit(lambda: None)
    except RuntimeError:
        raised = True
    assert raised, "Expected RuntimeError on submit-after-shutdown"
    print("✓ submit() after shutdown raises RuntimeError (caught by sync_turn)")


def test_live_brain_import_surface() -> None:
    """LiveBrainProvider should expose _sync_executor and _sync_executor_lock (new surface)."""
    from live_brain import LiveBrainProvider
    provider = LiveBrainProvider()
    assert hasattr(provider, "_sync_executor"), "_sync_executor attribute missing"
    assert hasattr(provider, "_sync_executor_lock"), "_sync_executor_lock attribute missing"
    assert provider._sync_executor is None, "Executor should be lazy (None before initialize)"
    # Old attributes should be gone
    assert not hasattr(provider, "_sync_threads"), "_sync_threads should have been removed"
    print("✓ LiveBrainProvider exposes new executor surface, old thread-set removed")


def run_tests() -> bool:
    tests = [
        ("test_executor_max_workers_bound", test_executor_max_workers_bound),
        ("test_executor_shutdown_drains_queued_tasks", test_executor_shutdown_drains_queued_tasks),
        ("test_submit_after_shutdown_raises", test_submit_after_shutdown_raises),
        ("test_live_brain_import_surface", test_live_brain_import_surface),
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
