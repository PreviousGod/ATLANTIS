#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TOOLS = ROOT / 'tools'
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from live_brain_mempalace_benchmark import run_benchmark


def test_live_brain_beats_mempalace_style_baseline() -> None:
    result = run_benchmark()
    scores = result['scores']
    assert scores['live_brain'] >= 90, scores
    assert scores['mempalace_style_baseline'] <= 35, scores
    assert scores['live_wins'] >= 6, scores
    numeric_case = next(case for case in result['cases'] if case['name'] == 'numeric_claim_requires_extraction')
    assert numeric_case['live_score'] == 100, numeric_case
    assert numeric_case['baseline_score'] == 0, numeric_case


if __name__ == '__main__':
    test_live_brain_beats_mempalace_style_baseline()
    print('live_brain_mempalace_benchmark_test: PASS')
