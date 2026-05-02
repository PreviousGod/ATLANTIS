#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIVE_BRAIN = ROOT / '.hermes' / 'plugins' / 'live_brain'
PKG = 'live_brain_cleanup_pkg'


def load_store():
    package = types.ModuleType(PKG)
    package.__path__ = [str(LIVE_BRAIN)]
    sys.modules[PKG] = package
    for name in ['scopes', 'store']:
        spec = importlib.util.spec_from_file_location(f'{PKG}.{name}', LIVE_BRAIN / f'{name}.py')
        mod = importlib.util.module_from_spec(spec)
        mod.__package__ = PKG
        sys.modules[f'{PKG}.{name}'] = mod
        spec.loader.exec_module(mod)
    return sys.modules[f'{PKG}.store']


def main() -> int:
    parser = argparse.ArgumentParser(description='Conservative Live Brain noisy-memory cleanup.')
    parser.add_argument('--db', default=str(Path.home() / '.hermes' / 'live_brain' / 'live_brain.db'))
    parser.add_argument('--dry-run', action='store_true', help='Only count records that would be archived/superseded.')
    parser.add_argument('--backup', action='store_true', help='Create timestamped DB backup before mutating cleanup.')
    parser.add_argument('--archive-stale-review', action='store_true', help='Archive stale needs_review recipes after --review-days.')
    parser.add_argument('--review-days', type=int, default=30)
    parser.add_argument('--age-recipes', action='store_true', help='Demote stale active recipes and review stale candidates.')
    parser.add_argument('--active-days', type=int, default=45, help='Age active recipes with no recent impressions after this many days.')
    parser.add_argument('--candidate-days', type=int, default=30, help='Move unpromoted candidates to needs_review after this many days.')
    args = parser.parse_args()
    store_mod = load_store()
    store = store_mod.LiveBrainStore(args.db)
    store.initialize_schema()
    stats = store.cleanup_noisy_memory(dry_run=args.dry_run, backup=args.backup)
    if args.age_recipes:
        stats['recipe_ageing'] = store.age_stale_recipes(active_days=args.active_days, candidate_days=args.candidate_days, dry_run=args.dry_run)
    if args.archive_stale_review:
        stats['stale_review'] = store.archive_stale_review_recipes(days=args.review_days, dry_run=args.dry_run)
    store.close()
    print(json.dumps(stats, indent=2, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
