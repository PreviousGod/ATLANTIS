"""Eval framework for ATLANTIS tier classification and fact counting.

Run with: python -m pytest test_tier_classification.py -v
Or standalone: python test_tier_classification.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# Add parent modules to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "modules"))

from cognitive_architecture import classify_complexity, _count_facts_in_context, check_attack_quality

# ---------------------------------------------------------------------------
# Tier classification test matrix
# ---------------------------------------------------------------------------

TIER_TESTS = [
    # (user_message, fact_count, ruled_out_count, expected_tier, description)
    ("da", 0, 0, 1, "trivial_one_word"),
    ("hvala", 0, 0, 1, "trivial_thanks"),
    ("nastavi", 0, 0, 1, "trivial_continue"),
    ("", 0, 0, 1, "empty_message"),
    ("ok posle tvojih promena sta sad mislis?", 0, 0, 2, "reflective_srpski"),
    ("kako ti se cini live brain i live brain ctx", 0, 0, 2, "reflective_evaluative"),
    ("tvoje misljenje o atlantis arhitekturi", 0, 0, 2, "reflective_short_srpski"),
    ("debug this error", 0, 0, 3, "two_complex_keywords_debug_error"),
    ("implement authentication system", 0, 0, 3, "complex_no_facts"),
    ("why does this fail and how do I fix it", 0, 0, 3, "two_complex_keywords"),
    ("explain why the system broke", 0, 0, 3, "explain_why_complex"),
    ("popravi ove probleme koje si identifikovao — fact_count, attack korak, tier klasifikacija", 0, 0, 3, "multipart_debug_request"),
    ("review the codebase and tell me what you think", 0, 0, 2, "reflective_multipart"),
    ("what is the weather today?", 0, 0, 1, "simple_factual_low_signal"),
    ("ne radi mi aplikacija, kako da popravim?", 0, 0, 3, "srpski_complex_multipart"),
    # --- Real user messages from session history ---
    ("Zdravo", 0, 0, 1, "real_greeting"),
    ("Popravi ove probleme koje si identifikovao \u2014 fact_count, attack korak, tier klasifikacija.", 0, 0, 3, "real_multipart_fix_request"),
    ("ok posle tvojih promena sta sad mislis? kako ti se cini live brain i live brain ctx", 0, 0, 2, "real_reflective_eval"),
    ("odusevljen sam svaka ti cast. ostajemo na ovome za sad.", 0, 0, 2, "real_conversational_len40_false_pos"),
    ("ustvari odradi na brzinu ova Ako hoceš da guram na 9.5...", 0, 0, 3, "real_multipart_complex"),
    ("Dokle si stigao", 0, 0, 1, "real_short_status_check"),
    ("Jel ima nesto sto si krenuo al nisi zavrsio", 0, 0, 2, "real_len40_conversational"),
]

# ---------------------------------------------------------------------------
# Fact counting test matrix
# ---------------------------------------------------------------------------

FACT_TESTS = [
    # (context, expected_count, description)
    ("", 0, "empty_context"),
    ("KNOWN FACTS:\n- Fact A\n- Fact B\nOTHER:\n- Not counted", 2, "two_facts_then_other_section"),
    ("VERIFIED ARTIFACTS:\n- artifact 1\n- artifact 2\n- artifact 3", 3, "three_artifacts"),
    ("PROVEN FIX:\n- fix A\n  continued line\n- fix B", 2, "multiline_facts"),
    ("Random text without sections\n- bullet here", 0, "no_recognized_section"),
    (
        "KNOWN FACTS:\n- Fact 1\nACTIVE TASK:\n- Task A\n- Task B\nOPEN BUG:\n- Bug 1",
        4,
        "multiple_sections",
    ),
    ("NEXT REQUIRED ACTION:\n- action 1\nMUST FOLLOW:\n- rule 1\n- rule 2", 3, "actions_and_rules"),
]

# ---------------------------------------------------------------------------
# Attack quality test matrix
# ---------------------------------------------------------------------------

ATTACK_TESTS = [
    # (response, expected_valid, description)
    ("", False, "empty_response"),
    ("No attack block here", False, "missing_attack_block"),
    ("<attack>good answer</attack>", False, "praise_only"),
    ("<attack>great work, no flaws</attack>", False, "praise_with_no_flaws"),
    ("<attack>The assumption about X is wrong because Y. Edge case Z breaks it.</attack>", True, "concrete_criticism"),
    ("<attack>Flaw #1: incorrect logic. Flaw #2: missed edge case.</attack>", True, "numbered_flaws"),
    ("<attack>One problem: the approach is fragile.</attack>", True, "single_criticism_no_praise"),
    ("<attack>ok</attack>", False, "too_short"),
    ("<attack>No critical flaws found \u2014 answer is sound.</attack>", False, "sound_but_no_critique"),
    ("<attack>What assumption could be wrong? The assumption about scaling is naive. What edge case breaks it? Concurrent access breaks it.</attack>", True, "prompt_style_with_criticism"),
]

# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_tests():
    passed = 0
    failed = 0

    print("=" * 60)
    print("TIER CLASSIFICATION TESTS")
    print("=" * 60)
    for msg, fact_count, ruled_out, expected, desc in TIER_TESTS:
        result = classify_complexity(msg, fact_count, ruled_out)
        status = "PASS" if result == expected else "FAIL"
        if status == "PASS":
            passed += 1
        else:
            failed += 1
        print(f"  [{status}] {desc}: got={result} expected={expected} | msg={msg[:50]!r}")

    print()
    print("=" * 60)
    print("FACT COUNTING TESTS")
    print("=" * 60)
    for ctx, expected, desc in FACT_TESTS:
        result = _count_facts_in_context(ctx)
        status = "PASS" if result == expected else "FAIL"
        if status == "PASS":
            passed += 1
        else:
            failed += 1
        print(f"  [{status}] {desc}: got={result} expected={expected}")

    print()
    print("=" * 60)
    print("ATTACK QUALITY TESTS")
    print("=" * 60)
    for response, expected, desc in ATTACK_TESTS:
        valid, reason = check_attack_quality(response)
        status = "PASS" if valid == expected else "FAIL"
        if status == "PASS":
            passed += 1
        else:
            failed += 1
        print(f"  [{status}] {desc}: got={valid} expected={expected} | reason={reason}")

    print()
    print("=" * 60)
    print(f"RESULTS: {passed} passed, {failed} failed out of {passed + failed}")
    print("=" * 60)
    return failed == 0


if __name__ == "__main__":
    ok = run_tests()
    sys.exit(0 if ok else 1)
