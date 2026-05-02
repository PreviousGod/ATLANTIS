#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live_brain.store import LiveBrainStore


def default_db() -> str:
    hermes_home = os.environ.get('HERMES_HOME', str(Path.home() / '.hermes'))
    return str(Path(hermes_home) / 'live_brain' / 'live_brain.db')


def parse_json(raw: str, default: Dict[str, Any]) -> Dict[str, Any]:
    if not raw:
        return default
    try:
        value = json.loads(raw)
    except Exception as exc:
        raise SystemExit(f'Invalid JSON: {exc}')
    if not isinstance(value, dict):
        raise SystemExit('JSON value must be an object')
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description='Inspect Live Brain Epistemic Autonomy state.')
    parser.add_argument('--db', default=default_db())
    parser.add_argument('--scope-key', default='debug:local')
    parser.add_argument('--query', default='')
    parser.add_argument('--record-fact', default='')
    parser.add_argument('--source-url', action='append', default=[])
    parser.add_argument('--confidence', type=float, default=0.8)
    parser.add_argument('--ttl-seconds', type=int, default=0)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()

    store = LiveBrainStore(str(Path(args.db).expanduser().resolve()))
    store.initialize_schema()
    try:
        if args.record_fact:
            result = store.record_epistemic_fact(
                scope_key=args.scope_key,
                question=args.query,
                fact_text=args.record_fact,
                source_urls=args.source_url,
                confidence=args.confidence,
                ttl_seconds=args.ttl_seconds or None,
            )
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print('Recorded:', json.dumps(result, ensure_ascii=False))
        result = store.debug_epistemic(args.scope_key, args.query)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str))
            return 0
        print(f"Scope: {result['scope_key']}")
        print(f"Query: {args.query}")
        print(store.compile_epistemic_brief(args.scope_key, args.query) or 'EPISTEMIC STATUS: <empty>')
        jobs = result.get('jobs') or []
        if jobs:
            print('\nResearch jobs:')
            for job in jobs[:5]:
                print(f"- [{job.get('status')}] {job.get('job_id')} :: {job.get('question')}")
        facts = result.get('facts') or []
        if facts:
            print('\nLearned facts:')
            for fact in facts[:5]:
                print(f"- {fact.get('fact_text')} ({fact.get('authority')}, {fact.get('confidence')})")
        return 0
    finally:
        store.close()


if __name__ == '__main__':
    raise SystemExit(main())
