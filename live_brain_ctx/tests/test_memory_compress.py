"""P2.5 — unit tests for memory_compress module."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def test_compress_no_duplicates_passthrough():
    from live_brain_ctx.modules.memory_compress import compress_entries
    entries = [
        "Alpha notes about deployment.",
        "Beta facts about the database schema.",
        "Gamma operations runbook for incidents.",
    ]
    out, collapsed = compress_entries(entries)
    assert out == entries
    assert collapsed == 0
    print("✓ test_compress_no_duplicates_passthrough")


def test_compress_collapses_paraphrases():
    from live_brain_ctx.modules.memory_compress import compress_entries
    entries = [
        "User = Deya/Dusa, Serbian. FULL AUTONOMY — act first, verify after.",
        "User = Deya/Dusa (Serbian). Wants FULL AUTONOMY — execute without asking.",
        "User = Deya/Dusa (Serbian). Speaks Serbian. Wants FULL AUTONOMY — execute without asking, verify after.",
        "Completely unrelated entry about dependency upgrades.",
    ]
    out, collapsed = compress_entries(entries, threshold=0.4)
    assert collapsed == 2
    assert len(out) == 2
    longest = max(entries[:3], key=len)
    rep = next(e for e in out if longest.split(".")[0] in e)
    assert "(+2 similar)" in rep
    assert any("dependency upgrades" in e for e in out)
    print("✓ test_compress_collapses_paraphrases")


def test_compress_empty_inputs():
    from live_brain_ctx.modules.memory_compress import compress_entries
    assert compress_entries([]) == ([], 0)
    assert compress_entries(["", "  "]) == ([], 0)
    print("✓ test_compress_empty_inputs")


def test_compress_threshold_env(monkeypatch=None):
    from live_brain_ctx.modules.memory_compress import compress_entries
    entries = [
        "deployment configures Kubernetes ingress for the staging cluster",
        "deployment configures Kubernetes ingress for the production cluster",
    ]
    high, _ = compress_entries(entries, threshold=0.95)
    low, collapsed_low = compress_entries(entries, threshold=0.5)
    assert len(high) == 2
    assert len(low) == 1
    assert collapsed_low == 1
    print("✓ test_compress_threshold_env")


def test_compress_keeps_longest_representative():
    from live_brain_ctx.modules.memory_compress import compress_entries
    entries = [
        "short note about caches",
        "a much longer note about caches with extra context and detail",
        "tiny",
    ]
    out, collapsed = compress_entries(entries, threshold=0.25)
    assert collapsed == 1
    assert any("much longer note" in e for e in out)
    print("✓ test_compress_keeps_longest_representative")


def test_compress_real_user_md_shape():
    """USER.md from the live profile has 3 entries about 'User = Deya/Dusa
    FULL AUTONOMY' that are paraphrases. They must collapse at default
    threshold (0.75)."""
    from live_brain_ctx.modules.memory_compress import compress_entries
    entries = [
        "User = Deya/Dusa, Serbian. FULL AUTONOMY — act first, verify after. Detesta advisory-only i over-explanation. Direct imperative = execute immediately. Tests competence. Frustrated by excessive explanation. Prefers concise. X login blocked. PDF: Times New Roman.",
        "X login blocked: server IP (data-center/Hetzner). Stealth setup correct; IP reputacija je blok.",
        "User = Deya/Dusa (Serbian). Wants FULL AUTONOMY — built Live Brain + Nucleus specifically so I execute without asking. Detesta advisory-only mode. Act first, verify after. Speaks Serbian + some English/Portuguese.",
        "User speaks Serbian + some English. Frustrated by over-explanation — direct imperative = immediate action required.",
        "User = Deya/Dusa (Serbian). Speaks Serbian + some English. Wants FULL AUTONOMY — execute without asking, verify after. Frustrated by over-analysis and excessive explanation. Built Live Brain + Nucleus for autonomous closed-loop execution. X login blocked. PDF editing: Times New Roman.",
    ]
    out, collapsed = compress_entries(entries)
    # The X-login-only entry is distinct; the 3 "User = Deya/Dusa" entries
    # should collapse to 1. The "speaks Serbian, frustrated" short entry
    # is similar enough to one of them at default threshold.
    assert collapsed >= 2
    assert len(out) <= 3
    print("✓ test_compress_real_user_md_shape")


def test_install_is_idempotent():
    import importlib
    from live_brain_ctx.modules import memory_compress
    importlib.reload(memory_compress)
    first = memory_compress.install()
    second = memory_compress.install()
    # First call returns True iff the core memory_tool import succeeded;
    # second call must always return False.
    assert second is False
    print(f"✓ test_install_is_idempotent (first={first})")


if __name__ == "__main__":
    test_compress_no_duplicates_passthrough()
    test_compress_collapses_paraphrases()
    test_compress_empty_inputs()
    test_compress_threshold_env()
    test_compress_keeps_longest_representative()
    test_compress_real_user_md_shape()
    test_install_is_idempotent()
    print("\nAll memory_compress tests passed.")
