#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(ROOT))

from live_brain.artifacts import ArtifactRegistry
from live_brain.store import LiveBrainStore


def default_db() -> str:
    hermes_home = os.environ.get('HERMES_HOME', str(Path.home() / '.hermes'))
    return str(Path(hermes_home) / 'live_brain' / 'live_brain.db')


def open_store(db_path: str) -> LiveBrainStore:
    store = LiveBrainStore(db_path)
    store.initialize_schema()
    return store


def print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def seed_enoch(registry: ArtifactRegistry) -> list[dict[str, Any]]:
    seeded = []
    entries = [
        {
            'project_key': 'enoch',
            'role': 'part_1',
            'path': '/home/deyaan666/Desktop/New Folder/enoch_part1_1_v13.mp4',
            'label': 'Enoch Part 1 latest',
            'source': 'seed_verified',
        },
        {
            'project_key': 'enoch',
            'role': 'part_2',
            'path': '/home/deyaan666/enoch_final/enoch_part2_CORRECT.mp4',
            'label': 'Enoch Part 2 verified correct',
            'source': 'seed_verified',
        },
        {
            'project_key': 'enoch',
            'role': 'part_2_alt',
            'path': '/home/deyaan666/enoch_final/enoch_part2_NEW_IMAGES.mp4',
            'label': 'Enoch Part 2 alternate/new images',
            'status': 'candidate',
            'confidence': 0.7,
            'source': 'seed_candidate',
        },
        {
            'project_key': 'enoch',
            'role': 'combined_or_full',
            'path': '/home/deyaan666/Desktop/Enoch_Full_Part1_2_COMPLETE.mp4',
            'label': 'Enoch combined/full candidate',
            'source': 'seed_verified',
        },
    ]
    for entry in entries:
        seeded.append(registry.upsert_artifact(**entry))
    rejected = '/home/deyaan666/Desktop/New Folder/enoch_part1_1.mp4'
    registry.upsert_artifact(
        project_key='enoch',
        role='part_1_old',
        path=rejected,
        label='Old Part 1; never use as Part 2',
        status='rejected',
        confidence=0.0,
        source='seed_rejected',
        evidence={'reason': 'was incorrectly selected as Part 2; filename indicates part1'},
    )
    seeded.append({'status': 'ok', 'path': rejected, 'new_status': 'rejected'})
    return seeded


def main() -> int:
    parser = argparse.ArgumentParser(description='Manage Live Brain verified artifacts.')
    parser.add_argument('--db', default=default_db())
    sub = parser.add_subparsers(dest='cmd', required=True)

    list_p = sub.add_parser('list')
    list_p.add_argument('--project', required=True)
    list_p.add_argument('--include-inactive', action='store_true')

    resolve_p = sub.add_parser('resolve')
    resolve_p.add_argument('--project', required=True)
    resolve_p.add_argument('--role', required=True)

    verify_p = sub.add_parser('verify')
    verify_p.add_argument('--project', required=True)
    verify_p.add_argument('--role', required=True)
    verify_p.add_argument('--path', required=True)
    verify_p.add_argument('--label', default='')
    verify_p.add_argument('--status', default='verified', choices=['verified', 'candidate', 'deprecated', 'rejected', 'missing'])
    verify_p.add_argument('--source', default='cli')

    mark_p = sub.add_parser('mark')
    mark_p.add_argument('--path', required=True)
    mark_p.add_argument('--status', required=True, choices=['verified', 'candidate', 'deprecated', 'rejected', 'missing'])
    mark_p.add_argument('--reason', default='')

    sub.add_parser('seed-enoch')

    args = parser.parse_args()
    store = open_store(args.db)
    registry = ArtifactRegistry(store.conn)
    try:
        if args.cmd == 'list':
            print_json({'artifacts': registry.list_project(args.project, include_inactive=args.include_inactive)})
        elif args.cmd == 'resolve':
            print_json(registry.resolve(args.project, args.role))
        elif args.cmd == 'verify':
            print_json(registry.upsert_artifact(
                project_key=args.project,
                role=args.role,
                path=args.path,
                label=args.label,
                status=args.status,
                source=args.source,
            ))
            store.conn.commit()
        elif args.cmd == 'mark':
            print_json(registry.mark_status(path=args.path, status=args.status, reason=args.reason))
            store.conn.commit()
        elif args.cmd == 'seed-enoch':
            print_json({'seeded': seed_enoch(registry)})
            store.conn.commit()
    finally:
        store.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
