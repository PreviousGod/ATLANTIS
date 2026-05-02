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


def compact_rows(rows, keys):
    out = []
    for row in rows:
        item = {}
        for key in keys:
            if key in row:
                item[key] = row[key]
        out.append(item)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description='Inspect Live Brain Reality Engine state.')
    parser.add_argument('--db', default=default_db())
    parser.add_argument('--scope-key', default='')
    parser.add_argument('--query', default='')
    parser.add_argument('--record', action='store_true', help='Record --query as a user_message event before debugging.')
    parser.add_argument('--event-type', default='user_message')
    parser.add_argument('--subject', default='manual_debug')
    parser.add_argument('--payload-json', default='{}')
    parser.add_argument('--action-type', default='')
    parser.add_argument('--action-payload-json', default='{}')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()

    db_path = Path(args.db).expanduser().resolve()
    store = LiveBrainStore(str(db_path))
    store.initialize_schema()
    scope_key = args.scope_key or 'debug:local'
    try:
        if args.record:
            payload = parse_json(args.payload_json, {})
            if args.query and 'text' not in payload:
                payload['text'] = args.query
            store.ingest_reality_event(
                scope_key=scope_key,
                event_type=args.event_type,
                subject=args.subject,
                payload=payload,
                session_id='debug',
                source='live_brain_reality_debug',
                confidence=0.8,
            )
        result = store.debug_reality(scope_key, args.query)
        if args.action_type:
            result['action_gate'] = store.action_gate(scope_key, args.action_type, parse_json(args.action_payload_json, {}))
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        print(f"Scope: {result['scope_key']}")
        if args.query:
            print(f"Query: {args.query}")
            print('Signals: ' + ', '.join(result.get('signals') or []))
        print('\n' + (result.get('brief') or 'LIVE REALITY: <empty>'))
        loops = compact_rows(result.get('open_loops') or [], ['title', 'status', 'priority', 'next_action'])
        dangers = compact_rows(result.get('danger_zones') or [], ['pattern', 'severity', 'mitigation', 'times_triggered'])
        constraints = compact_rows(result.get('action_constraints') or [], ['action_type', 'decision', 'reason', 'risk_level'])
        if loops:
            print('\nOpen loops:')
            for loop in loops[:8]:
                print(f"- [{loop.get('status')}] {loop.get('title')} -> {loop.get('next_action')}")
        if dangers:
            print('\nDanger zones:')
            for danger in dangers[:8]:
                print(f"- [{danger.get('severity')}] {danger.get('pattern')}: {danger.get('mitigation')}")
        if constraints:
            print('\nAction constraints:')
            for constraint in constraints[:8]:
                print(f"- {constraint.get('action_type')}={constraint.get('decision')}: {constraint.get('reason')}")
        if result.get('action_gate'):
            gate = result['action_gate']
            print(f"\nAction gate: {gate.get('decision')} risk={gate.get('risk_level')}")
            for reason in gate.get('reasons') or []:
                print(f"- {reason}")
        return 0
    finally:
        store.close()


if __name__ == '__main__':
    raise SystemExit(main())
