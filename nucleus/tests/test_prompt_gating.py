"""Quick tests for Nucleus prompt/bypass gating."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from nucleus import (
    _is_nucleus_graph_query,
    _is_safe_nucleus_context_query,
)


def test_generic_chat_does_not_trigger_nucleus_surface():
    # Regression guard: ordinary chat must not become a Nucleus turn.
    assert _is_nucleus_graph_query("Sta si radio danas") is False
    assert _is_safe_nucleus_context_query("Sta si radio danas") is False
    print("✓ test_generic_chat_does_not_trigger_nucleus_surface")


def test_explicit_nucleus_command_still_triggers():
    # Regression guard: explicit Nucleus commands must keep working.
    assert _is_nucleus_graph_query("nucleus status") is True
    assert _is_safe_nucleus_context_query("nucleus status") is True
    print("✓ test_explicit_nucleus_command_still_triggers")


def test_ops_phrase_alone_no_longer_bypasses():
    # Regression guard: system-problem phrasing without nucleus prefix stays with the LLM.
    assert _is_nucleus_graph_query("high cpu on hermes box") is False
    assert _is_safe_nucleus_context_query("high cpu on hermes box") is False
    print("✓ test_ops_phrase_alone_no_longer_bypasses")


if __name__ == "__main__":
    test_generic_chat_does_not_trigger_nucleus_surface()
    test_explicit_nucleus_command_still_triggers()
    test_ops_phrase_alone_no_longer_bypasses()
    print("\n✅ ALL NUCLEUS PROMPT GATING TESTS PASSED")
