#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIVE_BRAIN = ROOT / '.hermes' / 'plugins' / 'live_brain'
PKG = 'live_brain_promote_pkg'


def load_modules():
    package = types.ModuleType(PKG)
    package.__path__ = [str(LIVE_BRAIN)]
    sys.modules[PKG] = package
    for name in ['scopes', 'store']:
        spec = importlib.util.spec_from_file_location(f'{PKG}.{name}', LIVE_BRAIN / f'{name}.py')
        mod = importlib.util.module_from_spec(spec)
        mod.__package__ = PKG
        sys.modules[f'{PKG}.{name}'] = mod
        spec.loader.exec_module(mod)
    return sys.modules[f'{PKG}.store'], sys.modules[f'{PKG}.scopes']


def default_criteria(tool: str) -> str:
    lowered = tool.lower()
    if 'image_generate' in lowered or 'seedream' in lowered:
        return 'image file exists at absolute output path and is deliverable'
    if 'ffmpeg' in lowered:
        return 'video file exists, non-zero size, playable'
    if 'tts' in lowered:
        return 'audio file exists, non-zero size, playable'
    return 'tool returns success and expected artifact exists'


def main() -> int:
    parser = argparse.ArgumentParser(description='Promote a manually verified Live Brain fix recipe.')
    parser.add_argument('--db', default=str(Path.home() / '.hermes' / 'live_brain' / 'live_brain.db'))
    parser.add_argument('--scope-key', default='agent:main:telegram:dm:1280801428')
    parser.add_argument('--tool', required=True)
    parser.add_argument('--problem', required=True)
    parser.add_argument('--artifact-path', required=True)
    parser.add_argument('--success-criteria', default='')
    parser.add_argument('--seed-impression', action='store_true')
    args = parser.parse_args()

    artifact = Path(args.artifact_path)
    if not artifact.exists() or not artifact.is_file() or artifact.stat().st_size <= 0:
        print(json.dumps({'ok': False, 'error': 'artifact missing or empty', 'artifact_path': str(artifact)}, ensure_ascii=False))
        return 2

    store_mod, scopes_mod = load_modules()
    store = store_mod.LiveBrainStore(args.db)
    store.initialize_schema()
    now = time.time()
    scope_tags = scopes_mod.extract_scope_tags(args.problem, args.tool, scope_key=args.scope_key)
    result = store.upsert_fix_recipe(
        args.scope_key,
        args.problem,
        args.tool,
        [f'use {args.tool}', 'verify expected artifact exists'],
        {'tool': args.tool, 'paths': [str(artifact)], 'output': 'absolute path'},
        args.success_criteria or default_criteria(args.tool),
        0.95,
        'manual',
        scopes_mod.tags_to_json(scope_tags),
        now,
        artifact_verified=True,
        artifact_path=str(artifact),
        promotion_status='active',
    )
    if args.seed_impression:
        impression_id = f"impression:manual:{result['recipe_id'].split(':', 1)[-1]}"
        store.conn.execute(
            "INSERT OR REPLACE INTO context_impressions (impression_id, scope_key, session_id, query_text, context_hash, sections_json, recipe_ids_json, outcome, attribution_mode, source, feedback_text, created_at, updated_at) VALUES (?, ?, 'manual', ?, '', '[\"PROVEN FIX\"]', ?, 'pending', 'manual_seed', 'manual_promotion', '', ?, ?)",
            (impression_id, args.scope_key, args.problem[:500], json.dumps([result['recipe_id']]), now, now),
        )
        store.conn.commit()
    store.close()
    print(json.dumps({'ok': True, **result, 'source': 'manual', 'artifact_path': str(artifact)}, indent=2, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
