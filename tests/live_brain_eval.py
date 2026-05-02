#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sqlite3
import shutil
import tempfile
import time
import types
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LIVE_BRAIN = ROOT / 'live_brain'
CTX_PATH = ROOT / 'live_brain_ctx' / '__init__.py'
FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'live_brain_eval_cases.json'
PKG = 'live_brain_eval_pkg'
SCOPE = 'agent:main:telegram:dm:1280801428'


def _load_pkg_module(name: str):
    if PKG not in sys.modules:
        package = types.ModuleType(PKG)
        package.__path__ = [str(LIVE_BRAIN)]
        sys.modules[PKG] = package
    spec = importlib.util.spec_from_file_location(f'{PKG}.{name}', LIVE_BRAIN / f'{name}.py')
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = PKG
    sys.modules[f'{PKG}.{name}'] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_ctx():
    spec = importlib.util.spec_from_file_location('live_brain_eval_ctx', CTX_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _seed_eval_db(db_path: str) -> None:
    store_mod = _load_pkg_module('store')
    scopes_mod = _load_pkg_module('scopes')
    store = store_mod.LiveBrainStore(db_path)
    store.initialize_schema()
    now = time.time()
    seed_tags = scopes_mod.tags_to_json({'scope_key': [SCOPE], 'domain': ['image'], 'tool': ['image_generate']})
    ffmpeg_tags = scopes_mod.tags_to_json({'scope_key': [SCOPE], 'domain': ['video'], 'tool': ['ffmpeg']})
    store.conn.execute(
        "INSERT INTO rules (rule_id, scope, category, scope_tags_json, condition_json, action_json, confidence, source, times_confirmed, status, expires_at, specificity, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ('rule:seedream', 'user_binding', 'binding_constraint', seed_tags, '{}', json.dumps({'instruction': "Za generisanje slika koristi image_generate tool sa Seedream 4.5; ne traži credential od korisnika."}), 0.99, 'fixture', 3, 'active', None, 5, now, now),
    )
    store.conn.execute(
        "INSERT INTO facts (fact_id, subject_entity_id, fact_type, fact_text, confidence, source_kind, valid_from, valid_to, status, evidence_count, session_id, scope_key, scope_tags_json) VALUES (?, NULL, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)",
        ('fact:seedream', 'validated_fact', 'User prefers bytedance-seed/seedream-4.5 through image_generate for image generation.', 0.9, 'fixture', now, 'active', 1, 's', SCOPE, seed_tags),
    )
    store.conn.execute(
        "INSERT INTO fix_recipes (recipe_id, scope_key, problem_pattern, tool_name, steps_json, args_template_json, success_criteria, artifact_verified, artifact_path, promotion_status, confidence, times_confirmed, status, source, scope_tags_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ('recipe:seedream', SCOPE, 'seedream image problem', 'image_generate', json.dumps(['use image_generate', 'use local input files, not remote URLs', 'set output_path to an absolute path']), json.dumps({'tool': 'image_generate', 'input': 'local file', 'output': 'absolute path'}), 'image file exists at absolute output path and is deliverable', 1, '/tmp/seedream.png', 'active', 0.95, 9, 'active', 'fixture', seed_tags, now, now),
    )
    store.conn.execute(
        "INSERT INTO fix_recipes (recipe_id, scope_key, problem_pattern, tool_name, steps_json, args_template_json, success_criteria, artifact_verified, artifact_path, promotion_status, confidence, times_confirmed, status, source, scope_tags_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ('recipe:ffmpeg', SCOPE, 'ffmpeg video problem', 'ffmpeg', json.dumps(['run ffmpeg with explicit input/output paths', 'check exit code', 'verify output file exists and has non-zero size']), json.dumps({'tool': 'ffmpeg', 'input': 'video', 'output': 'mp4'}), 'video file exists, non-zero size, playable', 1, '/tmp/out.mp4', 'active', 0.92, 6, 'active', 'fixture', ffmpeg_tags, now, now),
    )
    store.conn.execute(
        "INSERT INTO causal_activations (activation_id, scope_key, trigger_text, trigger_pattern, action_taken, tool_used, args_template_json, outcome, test_result, artifact_verified, artifact_path, success, confidence, times_confirmed, scope_tags_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ('act:seedream', SCOPE, 'seedream image problem', 'seedream image problem', 'used image_generate', 'image_generate', json.dumps({'tool': 'image_generate', 'input': 'local file', 'output': 'absolute path'}), 'success', 'success', 1, '/tmp/seedream.png', 1, 0.9, 7, seed_tags, now, now),
    )
    store.conn.execute(
        "INSERT INTO causal_activations (activation_id, scope_key, trigger_text, trigger_pattern, action_taken, tool_used, args_template_json, outcome, test_result, artifact_verified, artifact_path, success, confidence, times_confirmed, scope_tags_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ('act:ffmpeg', SCOPE, 'ffmpeg video problem', 'ffmpeg video problem', 'used ffmpeg', 'ffmpeg', json.dumps({'tool': 'ffmpeg', 'input': 'video', 'output': 'mp4'}), 'success', 'success', 1, '/tmp/out.mp4', 1, 0.8, 5, ffmpeg_tags, now, now),
    )
    store.conn.execute(
        "INSERT INTO facts (fact_id, subject_entity_id, fact_type, fact_text, confidence, source_kind, valid_from, valid_to, status, evidence_count, session_id, scope_key, scope_tags_json) VALUES (?, NULL, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)",
        ('fact:noisy', 'validated_fact', 'Dobra pitanje! Evo kako bih ja refaktorisao Live Brain.', 0.9, 'fixture', now, 'active', 1, 's', SCOPE, '{}'),
    )
    store.conn.execute(
        "INSERT INTO fix_recipes (recipe_id, scope_key, problem_pattern, tool_name, steps_json, args_template_json, success_criteria, artifact_verified, artifact_path, promotion_status, confidence, times_confirmed, status, source, scope_tags_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ('recipe:unverified-meta', SCOPE, 'review conversation above consider saving updating skill appropriate seedream', 'image_generate', json.dumps(['use image_generate']), json.dumps({'tool': 'image_generate'}), 'image file exists at absolute output path and is deliverable', 0, '', 'candidate', 0.9, 4, 'active', 'fixture', seed_tags, now, now),
    )
    store.conn.execute(
        "INSERT INTO rules (rule_id, scope, category, scope_tags_json, condition_json, action_json, confidence, source, times_confirmed, status, expires_at, specificity, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ('rule:noisy', 'user_binding', 'binding_constraint', '{}', '{}', json.dumps({'instruction': 'Ovo je gemini rekao:\n### Situacija\nStale unrelated legal advice that must never be injected.'}), 0.99, 'fixture', 3, 'active', None, 1, now, now),
    )
    store.conn.commit()
    store.close()


def _render_with_db(ctx: Any, query: str, db_path: str) -> str:
    old_home = os.environ.get('HERMES_HOME')
    temp_home = Path(tempfile.mkdtemp(prefix='live-brain-eval-home-'))
    live_dir = temp_home / 'live_brain'
    live_dir.mkdir(parents=True, exist_ok=True)
    target = live_dir / 'live_brain.db'
    target.write_bytes(Path(db_path).read_bytes())
    os.environ['HERMES_HOME'] = str(temp_home)
    try:
        return ctx._load_live_brain_context(query, '', '1280801428') or ''
    finally:
        if old_home is None:
            os.environ.pop('HERMES_HOME', None)
        else:
            os.environ['HERMES_HOME'] = old_home
        shutil.rmtree(temp_home, ignore_errors=True)


def _section_lines(rendered: str, section: str) -> list[str]:
    lines = rendered.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip() == f'{section}:':
            start = index + 1
            break
    if start is None:
        return []
    out: list[str] = []
    for line in lines[start:]:
        stripped = line.strip()
        if stripped.endswith(':') and stripped[:-1].isupper():
            break
        if stripped.startswith('- '):
            out.append(stripped[2:])
    return out


def _proven_fix_actionable(rendered: str) -> bool:
    fixes = _section_lines(rendered, 'PROVEN FIX')
    if not fixes:
        return False
    for fix in fixes:
        lowered = fix.lower()
        has_tool = lowered.startswith('use ') and ('tool=' in lowered or 'image_generate' in lowered or 'ffmpeg' in lowered)
        has_io = any(token in lowered for token in ('input=', 'output=', 'paths='))
        has_verify = 'verify=' in lowered
        if not (has_tool and has_io and has_verify):
            return False
    return True


def _assert_case(case: dict[str, Any], rendered: str) -> tuple[list[str], int]:
    errors: list[str] = []
    score = 100
    lowered = rendered.lower()
    if not rendered and not case.get('allow_empty'):
        errors.append('rendered context is empty')
        score -= 40
    lines = rendered.splitlines() if rendered else []
    if len(lines) > int(case['max_lines']):
        errors.append(f"too many lines: {len(lines)} > {case['max_lines']}")
        score -= 15
    for section in case.get('expected_sections', []):
        if f'{section}:' not in rendered:
            errors.append(f'missing section {section}')
            score -= 15
    for term in case.get('expected_terms', []):
        if term.lower() not in lowered:
            errors.append(f'missing term {term}')
            score -= 10
    forbidden_terms = list(case.get('forbidden_terms', [])) + ['session_search']
    for term in forbidden_terms:
        if term.lower() in lowered:
            errors.append(f'forbidden term present {term}')
            score -= 25
    if 'PROVEN FIX' in case.get('expected_sections', []) and not _proven_fix_actionable(rendered):
        errors.append('PROVEN FIX is not actionable')
        score -= 20
    return errors, max(score, 0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--fixtures', default=str(FIXTURES))
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args()
    ctx = _load_ctx()
    cases = json.loads(Path(args.fixtures).read_text())
    db_path = tempfile.NamedTemporaryFile(delete=False).name
    _seed_eval_db(db_path)
    failures = 0
    scores: list[int] = []
    for case in cases:
        rendered = _render_with_db(ctx, case['query'], db_path)
        errors, score = _assert_case(case, rendered)
        scores.append(score)
        if args.verbose or errors:
            print(f"\n=== {case['name']} score={score}/100 ===")
            print(rendered or '<EMPTY>')
        if errors or score < 100:
            failures += 1
            for error in errors:
                print(f"FAIL {case['name']}: {error}")
            if score < 100 and not errors:
                print(f"FAIL {case['name']}: score below 100")
    total_score = round(sum(scores) / max(len(scores), 1))
    if failures or total_score < 100:
        print(f'live_brain eval failed: score={total_score}/100 failures={failures}/{len(cases)}')
        return 1
    print(f'live_brain eval ok: score=100/100 cases={len(cases)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
