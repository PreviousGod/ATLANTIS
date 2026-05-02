#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def run(script: str, *args: str) -> None:
    cmd = [sys.executable, str(ROOT / script), *args]
    subprocess.run(cmd, check=True)


def main() -> int:
    run('tests/live_brain_smoke.py')
    run('tests/live_brain_reality_test.py')
    run('tests/live_brain_reality_e2e.py')
    run('tests/live_brain_epistemic_test.py')
    run('tests/live_brain_epistemic_e2e.py')
    run('tests/live_brain_mempalace_benchmark_test.py')
    run('tests/live_brain_eval.py', '--verbose')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
