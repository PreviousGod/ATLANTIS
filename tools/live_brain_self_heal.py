#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live_brain.store import LiveBrainStore


def default_db() -> str:
    hermes_home = os.environ.get('HERMES_HOME', str(Path.home() / '.hermes'))
    return str(Path(hermes_home) / 'live_brain' / 'live_brain.db')


def main() -> int:
    parser = argparse.ArgumentParser(description='Live Brain self-heal scans for dangerous stale memory patterns.')
    parser.add_argument('--db', default=default_db())
    parser.add_argument('--apply', action='store_true', help='Apply safe automated fixes. Default is dry-run report.')
    args = parser.parse_args()

    store = LiveBrainStore(args.db)
    store.initialize_schema()
    try:
        result = {
            'destructive_episode_memory': store.suppress_destructive_episode_memory(dry_run=not args.apply),
        }
        if args.apply:
            store.conn.commit()
        print(json.dumps(result, indent=2, ensure_ascii=False))
    finally:
        store.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
