#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sqlite3
import sys
import types
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
LIVE_BRAIN = ROOT / '.hermes' / 'plugins' / 'live_brain'
PKG = 'live_brain_gate_backtest_pkg'

META_MARKERS = [
    'review conversation above', 'live brain sistem', 'live brain plugin', '10/10 gate',
    'arhitekturu trenutne live baze', 'analiziraj live brain', 'ukupan utisak',
    'implemented measurement layer', 'precision ratio', 'attribution modes', 'promotion helper',
    'feedback loop', 'hermes restart', 'package rebuilt', 'smoke ok', 'eval ok',
    'metrics healthy', 'manual recipe compiler', 'gotovo implementirao', 'loop mnogo stroži',
    'loop mnogo strozi', 'compiler pamti', 'were right metrics',
]
LOW_VALUE_WORDS = {'implemented', 'measurement', 'layer', 'place', 'added', 'ratio', 'threshold', 'question', 'answer'}
ACTION_TERMS = ['make', 'create', 'generate', 'build', 'fix', 'run', 'render', 'napravi', 'uradi', 'generisi', 'generiši', 'popravi', 'izrender']
ARTIFACT_TERMS = ['image', 'slika', 'video', 'audio', 'voice', 'file', 'fajl', 'mp4', 'png', 'jpg', 'mp3', 'wav', 'ffmpeg', 'seedream', 'tts']


def load_store():
    package = types.ModuleType(PKG)
    package.__path__ = [str(LIVE_BRAIN)]
    sys.modules[PKG] = package
    spec = importlib.util.spec_from_file_location(f'{PKG}.store', LIVE_BRAIN / 'store.py')
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = PKG
    sys.modules[f'{PKG}.store'] = mod
    spec.loader.exec_module(mod)
    return mod


def safe_json(raw: str | None, fallback: Any) -> Any:
    try:
        return json.loads(raw or '')
    except Exception:
        return fallback


def tool_domain(tool: str) -> str:
    lowered = (tool or '').lower()
    if 'image_generate' in lowered or 'seedream' in lowered:
        return 'image'
    if 'ffmpeg' in lowered or 'video' in lowered:
        return 'video'
    if 'tts' in lowered or 'whisper' in lowered or 'audio' in lowered:
        return 'audio'
    return ''


def task_domain(text: str, scope_tags: dict[str, Any]) -> str:
    domains = set(scope_tags.get('domain') or [])
    lowered = (text or '').lower()
    if 'video' in domains or re.search(r'\b(video|mp4|short|reel)\b', lowered):
        return 'video'
    if 'image' in domains or re.search(r'\b(image|slika|picture|photo|png|jpg|seedream)\b', lowered):
        return 'image'
    if 'audio' in domains or re.search(r'\b(audio|voice|glas|tts|mp3|wav|transcript)\b', lowered):
        return 'audio'
    return ''


def evaluate_row(row: sqlite3.Row) -> dict[str, Any]:
    problem = row['problem_pattern'] or ''
    tool = row['tool_name'] or ''
    tags = safe_json(row['scope_tags_json'], {})
    lowered = problem.lower()
    if any(marker in lowered for marker in META_MARKERS):
        reason = 'meta_or_not_real_task'
    elif not (any(term in lowered for term in ACTION_TERMS) and any(term in lowered for term in ARTIFACT_TERMS)):
        reason = 'meta_or_not_real_task'
    else:
        words = [w for w in re.findall(r'[\w./-]+', problem) if len(w) > 3]
        reusable = len([w for w in words if w.lower() not in LOW_VALUE_WORDS]) >= 3
        if not reusable:
            reason = 'not_reusable'
        else:
            td = tool_domain(tool)
            dd = task_domain(problem, tags)
            mismatch = bool(td and dd and td != dd and not (dd == 'video' and td == 'image' and any(t in lowered for t in ['frame', 'thumbnail', 'cover', 'slik', 'image'])))
            if mismatch:
                reason = 'domain_mismatch'
            elif td in {'image', 'video', 'audio'} and not int(row['artifact_verified'] or 0):
                reason = 'artifact_unverified'
            else:
                reason = ''
    return {
        'recipe_id': row['recipe_id'],
        'status': row['status'],
        'tool_name': tool,
        'problem_pattern': problem,
        'artifact_verified': int(row['artifact_verified'] or 0),
        'times_confirmed': int(row['times_confirmed'] or 0),
        'would_pass': not reason,
        'reason': reason or 'pass',
    }


def main() -> int:
    parser = argparse.ArgumentParser(description='Backtest Live Brain recipe gate using stored recipe rows.')
    parser.add_argument('--db', default=str(Path.home() / '.hermes' / 'live_brain' / 'live_brain.db'))
    parser.add_argument('--status', default='needs_review,candidate,active')
    parser.add_argument('--limit', type=int, default=500)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--details', action='store_true')
    args = parser.parse_args()

    store_mod = load_store()
    store = store_mod.LiveBrainStore(args.db)
    store.initialize_schema()
    statuses = [s.strip() for s in args.status.split(',') if s.strip()]
    placeholders = ','.join('?' for _ in statuses)
    rows = store.conn.execute(
        f"SELECT recipe_id, status, problem_pattern, tool_name, scope_tags_json, artifact_verified, times_confirmed FROM fix_recipes WHERE status IN ({placeholders}) ORDER BY updated_at DESC LIMIT ?",
        statuses + [args.limit],
    ).fetchall()
    items = [evaluate_row(row) for row in rows]
    counts: dict[str, int] = {}
    for item in items:
        counts[item['reason']] = counts.get(item['reason'], 0) + 1
    result = {
        'total': len(items),
        'would_pass': sum(1 for item in items if item['would_pass']),
        'would_reject': sum(1 for item in items if not item['would_pass']),
        'reasons': counts,
        'items': items if args.details or args.json else [],
    }
    store.close()
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"total={result['total']} pass={result['would_pass']} reject={result['would_reject']}")
        for reason, count in sorted(counts.items(), key=lambda item: item[1], reverse=True):
            print(f"{reason}: {count}")
        if args.details:
            for item in items:
                print(f"- {item['reason']} | {item['tool_name']} | {item['problem_pattern'][:100]}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
