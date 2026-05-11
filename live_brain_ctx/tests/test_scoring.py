"""Unit tests for scoring/filtering helpers in live_brain_ctx.

The external review explicitly called out three helpers that had no direct
coverage:

  - ``_domain_conflicts`` — suppresses cross-domain leakage (music vs media vs
    tool-call vs voice/TTS vs path/config vs open-loop) so that an unrelated
    high-signal memory does not pollute the briefing for a different query.
  - ``_overlap_score`` — scores how well a row's text overlaps the query,
    with special-casing for image-generation and ffmpeg tokens.
  - ``_is_low_signal_episode`` — classifies episodes as low-signal when they
    are noisy, lack FIX: action verbs, or are nearly empty.

These tests lock in the current behavior so future regex tweaks do not
silently change classification.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

PLUGINS_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PLUGINS_DIR))

import live_brain_ctx  # noqa: E402


# ---------------------------------------------------------------------------
# _domain_conflicts
# ---------------------------------------------------------------------------
def test_domain_conflicts_music_row_vs_generic_query() -> None:
    # Row is a music/song memory, query is generic
    assert live_brain_ctx._domain_conflicts(
        "review the live brain plugin", "pesma flamenco cover triler"
    ) is True
    print("✓ music row conflicts with non-music query")


def test_domain_conflicts_music_row_vs_music_query() -> None:
    # Query itself is music — no conflict
    assert live_brain_ctx._domain_conflicts(
        "koja pesma u flamenco stilu", "pesma flamenco cover"
    ) is False
    print("✓ music row does not conflict with music query")


def test_domain_conflicts_media_project_leak() -> None:
    # Row is a media-project memory (enoch/video_attachments), query is unrelated
    assert live_brain_ctx._domain_conflicts(
        "kako handle tool call dispatch",
        "enoch video_delivery wrong artifact",
    ) is True
    print("✓ media-project row conflicts with non-media query")


def test_domain_conflicts_tts_row_vs_tts_query() -> None:
    assert live_brain_ctx._domain_conflicts(
        "which piper voice template",
        "voice piper templates/default.yaml",
    ) is False
    print("✓ TTS row does not conflict with TTS query")


def test_domain_conflicts_tool_raw_fact_vs_generic() -> None:
    # Row is raw tool noise, query is domain-level
    assert live_brain_ctx._domain_conflicts(
        "how do we do self-evolution",
        "successfully used tool browser_scroll payload",
    ) is True
    print("✓ raw-tool-result row conflicts with non-tool query")


def test_domain_conflicts_open_loop_with_open_loop_query() -> None:
    # Open-loop row IS allowed when query asks about open loops / objective.
    # Note: _OPEN_LOOP_QUERY_RE requires the exact singular 'open loop' (with
    # \b\b), or 'objective', 'status', 'dashboard', 'blocker', etc.
    assert live_brain_ctx._domain_conflicts(
        "what is the current objective and status",
        "active open loop: finish refactor; current objective set",
    ) is False
    print("✓ open-loop fact allowed for open-loop query")


# ---------------------------------------------------------------------------
# _overlap_score
# ---------------------------------------------------------------------------
def _fake_row(**cols):
    """Build a sqlite3.Row-lookalike from a dict (plain dict works for __getitem__)."""
    class _R(dict):
        def __getitem__(self, key):
            return super().__getitem__(key) if key in self else None
    return _R(cols)


def test_overlap_score_no_meaningful_words_returns_one() -> None:
    row = _fake_row(body="anything")
    # All words are in _LOW_SIGNAL_WORDS (problem, kako, šta) — meaningful
    # list is empty so _overlap_score returns 1 as a neutral default.
    score = live_brain_ctx._overlap_score(row, ["problem", "kako", "sta"], ["body"])
    assert score == 1, f"Got {score}"
    print("✓ no meaningful words → score 1 (neutral)")


def test_overlap_score_strong_match() -> None:
    row = _fake_row(body="review the migration strategy for fts5 schema")
    score = live_brain_ctx._overlap_score(row, ["migration", "strategy", "fts5"], ["body"])
    assert score >= 2, f"Expected high score, got {score}"
    print(f"✓ strong match yields score {score}")


def test_overlap_score_zero_on_domain_conflict() -> None:
    row = _fake_row(body="pesma flamenco cover triler serbezovski")
    # generic LB query should get zero due to music-vs-non-music conflict
    score = live_brain_ctx._overlap_score(row, ["live_brain", "refactor"], ["body"])
    assert score == 0, f"Expected 0 (domain conflict), got {score}"
    print("✓ music-vs-live_brain domain conflict → score 0")


def test_overlap_score_ffmpeg_special_case() -> None:
    # ffmpeg in query — row must contain ffmpeg literal token to score.
    # Avoid .mp4 in body because _PATH_CONFIG_RE would flag a domain conflict.
    row_hit = _fake_row(body="use ffmpeg to encode with libx264")
    row_miss = _fake_row(body="the ffmpg alternative is different")  # typo on purpose
    assert live_brain_ctx._overlap_score(row_hit, ["ffmpeg"], ["body"]) == 1
    assert live_brain_ctx._overlap_score(row_miss, ["ffmpeg"], ["body"]) == 0
    print("✓ ffmpeg token special-case")


def test_overlap_score_image_generation_aliases() -> None:
    # IMAGE_GENERATION_ALIASES — row with seedream matches query with image_generate
    aliases = live_brain_ctx.IMAGE_GENERATION_ALIASES
    if aliases:
        alias = aliases[0]
        row_hit = _fake_row(body=f"we used {alias} for the render")
        row_miss = _fake_row(body="just text no aliases")
        query_words = list(aliases)
        assert live_brain_ctx._overlap_score(row_hit, query_words, ["body"]) == 1
        assert live_brain_ctx._overlap_score(row_miss, query_words, ["body"]) == 0
        print(f"✓ image-generation alias ({alias}) special-case")
    else:
        print("  (skipped — no IMAGE_GENERATION_ALIASES available)")


# ---------------------------------------------------------------------------
# _is_low_signal_episode
# ---------------------------------------------------------------------------
def test_low_signal_scope_problem_without_fix() -> None:
    # SCOPE + PROBLEM but no FIX / ROOT → low signal
    title = "episode"
    summary = "SCOPE: chat\nPROBLEM: user said nothing useful"
    assert live_brain_ctx._is_low_signal_episode(title, summary) is True
    print("✓ SCOPE+PROBLEM without FIX is low signal")


def test_low_signal_scope_problem_with_weak_fix() -> None:
    # FIX: present but no actionable token (TOOL/FILE/COMMAND/RUN/...)
    summary = "SCOPE: chat\nPROBLEM: broken\nFIX: think about it"
    assert live_brain_ctx._is_low_signal_episode("episode", summary) is True
    print("✓ FIX: without actionable token is low signal")


def test_low_signal_scope_problem_with_strong_fix() -> None:
    # FIX: contains an action token
    summary = "SCOPE: code\nPROBLEM: bug\nFIX: RUN pytest to VERIFY the patch"
    assert live_brain_ctx._is_low_signal_episode("good episode", summary) is False
    print("✓ FIX with RUN/VERIFY is NOT low signal")


def test_low_signal_noisy_memory() -> None:
    # Noisy: contains the "review the conversation above" seed
    assert live_brain_ctx._is_low_signal_episode(
        "review", "review the conversation above and consider saving or updating a skill"
    ) is True
    print("✓ noisy-memory phrasing is low signal")


def test_low_signal_with_query_count_suppression() -> None:
    # Uses an in-memory DB to simulate episode_queries count > 2
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE episode_queries (episode_id TEXT, session_id TEXT, queried_at REAL)")
    for i in range(3):
        conn.execute("INSERT INTO episode_queries VALUES ('ep1', 'sess1', ?)", (float(i),))
    conn.commit()
    result = live_brain_ctx._is_low_signal_episode(
        "normal title", "normal summary with enough distinct words",
        episode_id="ep1", session_id="sess1", conn=conn,
    )
    assert result is True, "Episode queried >2 times should be low signal"
    conn.close()
    print("✓ query-count >2 suppression triggers low-signal")


def run_tests() -> bool:
    tests = [
        ("_domain_conflicts music vs generic", test_domain_conflicts_music_row_vs_generic_query),
        ("_domain_conflicts music vs music", test_domain_conflicts_music_row_vs_music_query),
        ("_domain_conflicts media leak", test_domain_conflicts_media_project_leak),
        ("_domain_conflicts tts vs tts", test_domain_conflicts_tts_row_vs_tts_query),
        ("_domain_conflicts raw tool", test_domain_conflicts_tool_raw_fact_vs_generic),
        ("_domain_conflicts open-loop", test_domain_conflicts_open_loop_with_open_loop_query),
        ("_overlap_score no meaningful", test_overlap_score_no_meaningful_words_returns_one),
        ("_overlap_score strong match", test_overlap_score_strong_match),
        ("_overlap_score domain conflict=0", test_overlap_score_zero_on_domain_conflict),
        ("_overlap_score ffmpeg", test_overlap_score_ffmpeg_special_case),
        ("_overlap_score image aliases", test_overlap_score_image_generation_aliases),
        ("_is_low_signal_episode scope-problem", test_low_signal_scope_problem_without_fix),
        ("_is_low_signal_episode weak fix", test_low_signal_scope_problem_with_weak_fix),
        ("_is_low_signal_episode strong fix", test_low_signal_scope_problem_with_strong_fix),
        ("_is_low_signal_episode noisy", test_low_signal_noisy_memory),
        ("_is_low_signal_episode query-count", test_low_signal_with_query_count_suppression),
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
