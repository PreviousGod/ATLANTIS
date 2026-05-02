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
PKG = 'live_brain_attr_report_pkg'


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


def main() -> int:
    parser = argparse.ArgumentParser(description='Report Live Brain attribution precision ratio.')
    parser.add_argument('--db', default=str(Path.home() / '.hermes' / 'live_brain' / 'live_brain.db'))
    parser.add_argument('--scope-key', default='')
    parser.add_argument('--days', type=int, default=30)
    args = parser.parse_args()
    store_mod = load_store()
    store = store_mod.LiveBrainStore(args.db)
    store.initialize_schema()
    report = store.attribution_report(scope_key=args.scope_key, days=args.days)
    store.close()
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
