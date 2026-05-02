#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CTX = ROOT / '.hermes' / 'plugins' / 'live_brain_ctx' / '__init__.py'


def load_ctx():
    spec = importlib.util.spec_from_file_location('live_brain_context_debug_ctx', CTX)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    parser = argparse.ArgumentParser(description='Render and explain Live Brain context packet selection.')
    parser.add_argument('query')
    parser.add_argument('--session-id', default='')
    parser.add_argument('--sender-id', default='1280801428')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    ctx = load_ctx()
    data = ctx._debug_live_brain_context(args.query, args.session_id, args.sender_id)
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(data.get('context') or '<EMPTY>')
        print('\n--- DEBUG ---')
        print(json.dumps({k: v for k, v in data.items() if k != 'context'}, indent=2, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
