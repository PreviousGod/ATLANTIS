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
    parser = argparse.ArgumentParser(description='Inspect or decide gated Live Brain self-evolution proposals.')
    parser.add_argument('--db', default=default_db())
    parser.add_argument('--status', default='', help='Optional status filter for listing proposals.')
    parser.add_argument('--include-applied', action='store_true')
    parser.add_argument('--limit', type=int, default=20)
    parser.add_argument('--approve', default='', help='Proposal ID to approve.')
    parser.add_argument('--reject', default='', help='Proposal ID to reject.')
    parser.add_argument('--approve-latest', action='store_true', help='Approve the highest-risk/latest pending proposal.')
    parser.add_argument('--reject-latest', action='store_true', help='Reject the highest-risk/latest pending proposal.')
    parser.add_argument('--reason', default='')
    args = parser.parse_args()

    store = LiveBrainStore(args.db)
    store.initialize_schema()
    try:
        decision_flags = [bool(args.approve), bool(args.reject), args.approve_latest, args.reject_latest]
        if sum(1 for flag in decision_flags if flag) > 1:
            raise SystemExit('Use only one approval/rejection flag')
        if args.approve:
            result = store.decide_self_evolution_proposal(args.approve, 'approved', args.reason)
        elif args.reject:
            result = store.decide_self_evolution_proposal(args.reject, 'rejected', args.reason)
        elif args.approve_latest:
            result = store.decide_self_evolution_proposal('', 'approved', args.reason)
        elif args.reject_latest:
            result = store.decide_self_evolution_proposal('', 'rejected', args.reason)
        else:
            result = {
                'proposals': store.list_self_evolution_proposals(
                    status=args.status,
                    include_applied=args.include_applied,
                    limit=args.limit,
                )
            }
        print(json.dumps(result, indent=2, ensure_ascii=False))
    finally:
        store.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
