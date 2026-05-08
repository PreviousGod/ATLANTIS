#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIVE_BRAIN = ROOT / 'live_brain'
CTX = ROOT / 'live_brain_ctx' / '__init__.py'
PKG = 'live_brain_smoke_pkg'


def load_live_modules():
    package = types.ModuleType(PKG)
    package.__path__ = [str(LIVE_BRAIN)]
    sys.modules[PKG] = package
    for name in ['scopes', 'store', 'causal', 'ingest', 'rules', 'research']:
        spec = importlib.util.spec_from_file_location(f'{PKG}.{name}', LIVE_BRAIN / f'{name}.py')
        mod = importlib.util.module_from_spec(spec)
        mod.__package__ = PKG
        sys.modules[f'{PKG}.{name}'] = mod
        spec.loader.exec_module(mod)
    return {name: sys.modules[f'{PKG}.{name}'] for name in ['store', 'causal', 'ingest', 'rules', 'research']}


def load_ctx():
    spec = importlib.util.spec_from_file_location('live_brain_smoke_ctx', CTX)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> None:
    modules = load_live_modules()
    db_path = tempfile.NamedTemporaryFile(delete=False).name
    store = modules['store'].LiveBrainStore(db_path)
    store.initialize_schema()
    ingestor = modules['ingest'].Ingestor(store.conn)
    rules = modules['rules'].RuleEngine(store.conn)
    causal = modules['causal'].CausalManager(store.conn, store=store)
    research = modules['research'].ResearchManager(store.conn, ingestor=ingestor, causal=causal, session_id='s', scope_key='agent:main:telegram:dm:test')

    empty_report = store.attribution_report(scope_key='agent:main:telegram:dm:test', days=30000)
    assert empty_report['precision_ratio'] is None and empty_report['sample_size'] == 0, empty_report

    now = time.time()
    old = now - 40 * 86400
    stale_active = store.upsert_fix_recipe(
        'agent:main:telegram:dm:age',
        'stale active image recipe',
        'image_generate',
        ['Use image_generate'],
        {'tool': 'image_generate'},
        'verify=image file exists',
        0.8,
        'smoke',
        '{}',
        now,
        artifact_verified=True,
        artifact_path='/tmp/stale-active.png',
        promotion_status='active',
    )
    protected_active = store.upsert_fix_recipe(
        'agent:main:telegram:dm:age',
        'protected active image recipe',
        'image_generate',
        ['Use image_generate'],
        {'tool': 'image_generate', 'mode': 'protected'},
        'verify=image file exists',
        0.8,
        'smoke',
        '{}',
        now,
        artifact_verified=True,
        artifact_path='/tmp/protected-active.png',
        promotion_status='active',
    )
    stale_candidate = store.upsert_fix_recipe(
        'agent:main:telegram:dm:age',
        'stale candidate image recipe',
        'image_generate',
        ['Use image_generate'],
        {'tool': 'image_generate', 'mode': 'candidate'},
        'verify=image file exists',
        0.7,
        'smoke',
        '{}',
        now,
        artifact_verified=True,
        artifact_path='/tmp/stale-candidate.png',
        promotion_status='candidate',
    )
    store.conn.execute(
        "UPDATE fix_recipes SET updated_at=?, promoted_at=?, candidate_since=? WHERE recipe_id IN (?, ?, ?)",
        (old, old, old, stale_active['recipe_id'], protected_active['recipe_id'], stale_candidate['recipe_id']),
    )
    store.conn.execute(
        "INSERT INTO context_impressions (impression_id, scope_key, session_id, query_text, context_hash, sections_json, recipe_ids_json, outcome, attribution_mode, feedback_text, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'success', 'precise', 'works now', ?, ?)",
        ('impression:test:age-protected', 'agent:main:telegram:dm:age', 's', 'protected active image recipe', 'hash-age', '["PROVEN FIX"]', f'["{protected_active["recipe_id"]}"]', now, now),
    )
    dry_age = store.age_stale_recipes(active_days=30, candidate_days=30, dry_run=True)
    assert dry_age['matched_active'] == 1 and dry_age['matched_candidate'] == 1, dry_age
    age_stats = store.age_stale_recipes(active_days=30, candidate_days=30, dry_run=False)
    assert age_stats['demoted_active'] == 1 and age_stats['reviewed_candidate'] == 1, age_stats
    assert store.conn.execute("SELECT status FROM fix_recipes WHERE recipe_id=?", (stale_active['recipe_id'],)).fetchone()[0] == 'candidate'
    assert store.conn.execute("SELECT status FROM fix_recipes WHERE recipe_id=?", (protected_active['recipe_id'],)).fetchone()[0] == 'active'
    assert store.conn.execute("SELECT status FROM fix_recipes WHERE recipe_id=?", (stale_candidate['recipe_id'],)).fetchone()[0] == 'needs_review'
    assert store.conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='degrade' AND reason='stale_active_no_recent_impressions'").fetchone()[0] >= 1
    assert store.conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='review' AND reason='stale_candidate_not_promoted'").fetchone()[0] >= 1

    plan = research.plan_research('verify seedream image generation provider', scope='local')
    assert plan['research_id'].startswith('research:') and plan['scope'] == 'local'
    recorded = research.record_result(
        plan['research_id'],
        source_kind='local',
        source_ref='smoke',
        summary='image_generate should be preferred over legacy seedream model names',
        confidence=0.8,
        actionability=0.8,
        raw_excerpt='verified by smoke test',
    )
    assert recorded['status'] == 'recorded' and recorded['belief_id'] and recorded['fact_id'], recorded
    assert store.conn.execute("SELECT COUNT(*) FROM facts WHERE fact_type='research_result'").fetchone()[0] == 1

    first = rules.upsert_rule(
        scope='user_binding',
        category='tool_choice',
        condition={'tool': 'image'},
        action={'type': 'prefer', 'instruction': 'Use old image tool'},
        confidence=0.7,
        scope_tags={'domain': ['image']},
        ttl_days=1,
    )
    second = rules.upsert_rule(
        scope='user_binding',
        category='tool_choice',
        condition={'tool': 'image'},
        action={'type': 'prefer', 'instruction': 'Use precise image tool'},
        confidence=0.95,
        scope_tags={'domain': ['image'], 'tool': ['image_generate']},
        ttl_days=1,
    )
    assert second['superseded_conflicts'] == 1, (first, second)
    store.backfill_causal_activations('agent:main:telegram:dm:test', str(ROOT))
    assert not store.conn.execute("SELECT COUNT(*) FROM causal_activations WHERE tool_used LIKE '%seedream-4.5%'").fetchone()[0]

    ingestor.ingest_turn(
        's',
        'agent:main:telegram:dm:test',
        1,
        'debug /tmp/app.py seedream error',
        'I found a likely issue but it is not fixed yet.',
        1.0,
    )
    assert store.conn.execute('SELECT count(*) FROM working_set').fetchone()[0] == 1
    assert store.conn.execute('SELECT status FROM work_items').fetchone()[0] == 'active'

    assert not ingestor._recipe_worth_keeping(
        'analyze live brain precision ratio threshold',
        'implemented measurement layer precision ratio',
        'image_generate',
        {'domain': ['memory'], 'tool': ['image_generate']},
        True,
    )
    assert not ingestor._recipe_worth_keeping(
        'make me a video mp4 from clips',
        'make video mp4 from clips',
        'image_generate',
        {'domain': ['video'], 'tool': ['image_generate']},
        True,
    )
    assert ingestor._recipe_rejection_reason(
        'make me a video mp4 from clips',
        'make video mp4 from clips',
        'image_generate',
        {'domain': ['video'], 'tool': ['image_generate']},
        True,
    ) == 'domain_mismatch'
    assert ingestor._recipe_worth_keeping(
        'generate seedream image png',
        'generate seedream image png',
        'image_generate',
        {'domain': ['image'], 'tool': ['image_generate']},
        True,
    )
    store.conn.execute(
        "INSERT OR REPLACE INTO recipe_rejections (rejection_id, scope_key, trigger_pattern, tool_name, reason, artifact_verified, source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ('rej:test', 'agent:main:telegram:dm:test', 'make video mp4', 'image_generate', 'domain_mismatch', 1, 'test', 2.0),
    )
    assert store.conn.execute("SELECT COUNT(*) FROM recipe_rejections WHERE reason='domain_mismatch'").fetchone()[0] == 1
    ingestor._upsert_fix_recipe(
        'agent:main:telegram:dm:test',
        'seedream image problem',
        'image_generate',
        {'tool': 'image_generate'},
        '{}',
        2.0,
        artifact_verified=False,
        artifact_path='',
        promotion_status='candidate',
    )
    assert store.conn.execute("SELECT status FROM fix_recipes WHERE problem_pattern='seedream image problem'").fetchone()[0] == 'candidate'

    artifact = Path(tempfile.NamedTemporaryFile(delete=False, suffix='.png').name)
    artifact.write_bytes(b'image')
    ingestor._upsert_fix_recipe(
        'agent:main:telegram:dm:test',
        'verified seedream image problem',
        'image_generate',
        {'tool': 'image_generate', 'paths': [str(artifact)]},
        '{}',
        3.0,
        artifact_verified=True,
        artifact_path=str(artifact),
        promotion_status='active',
    )
    verified = store.conn.execute("SELECT recipe_id, status, artifact_verified, times_confirmed FROM fix_recipes WHERE problem_pattern='verified seedream image problem'").fetchone()
    assert verified['status'] == 'active' and verified['artifact_verified'] == 1

    other_artifact = Path(tempfile.NamedTemporaryFile(delete=False, suffix='.png').name)
    other_artifact.write_bytes(b'image')
    ingestor._upsert_fix_recipe(
        'agent:main:telegram:dm:test',
        'other verified image problem',
        'image_generate',
        {'tool': 'image_generate', 'paths': [str(other_artifact)]},
        '{}',
        3.1,
        artifact_verified=True,
        artifact_path=str(other_artifact),
        promotion_status='active',
    )
    other = store.conn.execute("SELECT recipe_id, times_confirmed FROM fix_recipes WHERE problem_pattern='other verified image problem'").fetchone()
    store.conn.execute(
        "INSERT INTO context_impressions (impression_id, scope_key, session_id, query_text, context_hash, sections_json, recipe_ids_json, outcome, attribution_mode, feedback_text, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', '', '', ?, ?)",
        ('impression:test:success', 'agent:main:telegram:dm:test', 's', 'seedream image problem', 'hash', '["PROVEN FIX"]', f'["{verified["recipe_id"]}"]', 4.0, 4.0),
    )
    ingestor._apply_user_feedback('agent:main:telegram:dm:test', 'odlično radi sada, hvala', 5.0)
    updated = store.conn.execute("SELECT times_confirmed FROM fix_recipes WHERE recipe_id=?", (verified['recipe_id'],)).fetchone()[0]
    untouched = store.conn.execute("SELECT times_confirmed FROM fix_recipes WHERE recipe_id=?", (other['recipe_id'],)).fetchone()[0]
    assert updated == verified['times_confirmed'] + 1
    assert untouched == other['times_confirmed']
    success_row = store.conn.execute("SELECT outcome, attribution_mode FROM context_impressions WHERE impression_id='impression:test:success'").fetchone()
    assert tuple(success_row) == ('success', 'precise')

    store.conn.execute(
        "INSERT INTO context_impressions (impression_id, scope_key, session_id, query_text, context_hash, sections_json, recipe_ids_json, outcome, attribution_mode, feedback_text, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', '', '', ?, ?)",
        ('impression:test:failure', 'agent:main:telegram:dm:test', 's', 'seedream image problem', 'hash2', '["PROVEN FIX"]', f'["{verified["recipe_id"]}"]', 6.0, 6.0),
    )
    ingestor._apply_user_feedback('agent:main:telegram:dm:test', 'ne radi, wrong output', 7.0)
    assert store.conn.execute("SELECT status FROM fix_recipes WHERE recipe_id=?", (verified['recipe_id'],)).fetchone()[0] == 'needs_review'
    assert store.conn.execute("SELECT status FROM fix_recipes WHERE recipe_id=?", (other['recipe_id'],)).fetchone()[0] == 'active'
    failure_row = store.conn.execute("SELECT outcome, attribution_mode FROM context_impressions WHERE impression_id='impression:test:failure'").fetchone()
    assert tuple(failure_row) == ('failure', 'precise')

    store.conn.execute(
        "INSERT INTO context_impressions (impression_id, scope_key, session_id, query_text, context_hash, sections_json, recipe_ids_json, outcome, attribution_mode, feedback_text, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', '', '', ?, ?)",
        ('impression:test:recovery', 'agent:main:telegram:dm:test', 's', 'seedream image problem', 'hash-recovery', '["PROVEN FIX"]', f'["{verified["recipe_id"]}"]', 7.5, 7.5),
    )
    ingestor._apply_user_feedback('agent:main:telegram:dm:test', 'image output radi sada', 7.6)
    recovered = store.conn.execute("SELECT status, promotion_status FROM fix_recipes WHERE recipe_id=?", (verified['recipe_id'],)).fetchone()
    assert tuple(recovered) == ('candidate', 'candidate')

    delay_artifact = Path(tempfile.NamedTemporaryFile(delete=False, suffix='.png').name)
    delay_artifact.write_bytes(b'image')
    ingestor._upsert_fix_recipe(
        'agent:main:telegram:dm:test',
        'delayed seedream image problem',
        'image_generate',
        {'tool': 'image_generate', 'paths': [str(delay_artifact)]},
        '{}',
        8.0,
        artifact_verified=True,
        artifact_path=str(delay_artifact),
        promotion_status='candidate',
    )
    assert store.conn.execute("SELECT status FROM fix_recipes WHERE problem_pattern='delayed seedream image problem'").fetchone()[0] == 'candidate'
    ingestor._upsert_fix_recipe(
        'agent:main:telegram:dm:test',
        'delayed seedream image problem',
        'image_generate',
        {'tool': 'image_generate', 'paths': [str(delay_artifact)]},
        '{}',
        9.0,
        artifact_verified=True,
        artifact_path=str(delay_artifact),
        promotion_status='candidate',
    )
    assert store.conn.execute("SELECT status FROM fix_recipes WHERE problem_pattern='delayed seedream image problem'").fetchone()[0] == 'active'

    broad_one = store.conn.execute("SELECT recipe_id, times_confirmed FROM fix_recipes WHERE problem_pattern='other verified image problem'").fetchone()
    broad_two = store.conn.execute("SELECT recipe_id, times_confirmed FROM fix_recipes WHERE problem_pattern='delayed seedream image problem'").fetchone()
    store.conn.execute(
        "INSERT INTO context_impressions (impression_id, scope_key, session_id, query_text, context_hash, sections_json, recipe_ids_json, outcome, attribution_mode, feedback_text, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', '', '', ?, ?)",
        ('impression:test:broad', 'agent:main:telegram:dm:test', 's', 'image problem', 'hash3', '["PROVEN FIX"]', f'["{broad_one["recipe_id"]}", "{broad_two["recipe_id"]}"]', 10.0, 10.0),
    )
    ingestor._apply_user_feedback('agent:main:telegram:dm:test', 'works now fixed output', 11.0)
    assert store.conn.execute("SELECT attribution_mode FROM context_impressions WHERE impression_id='impression:test:broad'").fetchone()[0] == 'broad'
    assert store.conn.execute("SELECT times_confirmed FROM fix_recipes WHERE recipe_id=?", (broad_one['recipe_id'],)).fetchone()[0] == broad_one['times_confirmed'] + 1
    assert store.conn.execute("SELECT times_confirmed FROM fix_recipes WHERE recipe_id=?", (broad_two['recipe_id'],)).fetchone()[0] == broad_two['times_confirmed'] + 1

    store.conn.execute(
        "INSERT INTO context_impressions (impression_id, scope_key, session_id, query_text, context_hash, sections_json, recipe_ids_json, outcome, attribution_mode, feedback_text, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, '[]', 'pending', '', '', ?, ?)",
        ('impression:test:fallback', 'agent:main:telegram:dm:test', 's', 'image problem', 'hash4', '["KNOWN FACTS"]', 12.0, 12.0),
    )
    ingestor._apply_user_feedback('agent:main:telegram:dm:test', 'image output ne radi', 13.0)
    assert tuple(store.conn.execute("SELECT outcome, attribution_mode FROM context_impressions WHERE impression_id='impression:test:fallback'").fetchone()) == ('pending', '')
    ingestor._apply_user_feedback('agent:main:telegram:dm:test', 'thanks', 14.0)

    store.conn.execute(
        "INSERT INTO context_impressions (impression_id, scope_key, session_id, query_text, context_hash, sections_json, recipe_ids_json, outcome, attribution_mode, feedback_text, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', '', '', ?, ?)",
        ('impression:test:meta-noise', 'agent:main:telegram:dm:test', 's', 'seedream implementation discussion', 'hash-meta', '["PROVEN FIX"]', f'["{broad_one["recipe_id"]}"]', 14.2, 14.2),
    )
    ingestor._apply_user_feedback(
        'agent:main:telegram:dm:test',
        'Gotovo — implementirao sam A end-to-end. Šta sada radi: compiler pamti recipe_id i Seedream ne radi je pomenut samo kao analiza.',
        14.3,
    )
    assert store.conn.execute("SELECT outcome FROM context_impressions WHERE impression_id='impression:test:meta-noise'").fetchone()[0] == 'pending'

    report = store.attribution_report(scope_key='agent:main:telegram:dm:test', days=30000)
    assert report['counts']['precise'] >= 3, report
    assert report['counts']['broad'] >= 1, report
    assert report['counts']['fallback'] == 0, report
    assert 0 <= report['precision_ratio'] <= 1, report

    store.conn.execute("UPDATE fix_recipes SET status='needs_review', promotion_status='needs_review', updated_at=1.0, last_reviewed_at=1.0 WHERE problem_pattern='other verified image problem'")
    archived = store.archive_stale_review_recipes(days=1, dry_run=False)
    assert archived['archived'] >= 1
    assert store.conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='archive' AND reason='stale_needs_review'").fetchone()[0] >= 1

    meta_row = store.conn.execute("SELECT recipe_id, status, problem_pattern, tool_name, scope_tags_json, artifact_verified, times_confirmed FROM fix_recipes WHERE status='archived' LIMIT 1").fetchone()
    assert meta_row is not None

    fake_secret = 'sk-or-v1-' + 'secret' * 3
    ingestor.store_fact('validated_fact', f'OpenRouter API key (active): {fake_secret}', 0.9, 'test', 2.0, scope_key='agent:main:telegram:dm:test')
    cleanup = store.cleanup_noisy_memory()
    assert cleanup['facts'] >= 1

    ctx = load_ctx()
    rendered = ctx._redact(f'OpenRouter API key: {fake_secret}')
    assert 'sk-or-v1-' not in rendered
    assert 'api key' not in rendered.lower()

    old_home = os.environ.get('HERMES_HOME')
    with tempfile.TemporaryDirectory() as hook_home:
        os.environ['HERMES_HOME'] = hook_home
        hook_db = Path(hook_home) / 'live_brain' / 'live_brain.db'
        hook_store = modules['store'].LiveBrainStore(str(hook_db))
        hook_store.initialize_schema()
        noisy_summary = '[The user sent a voice message~] client_secret hidden noise; Part 1 and Part 2 video project context exists.'
        hook_store.conn.execute(
            "INSERT INTO episodes (episode_id, kind, title, status, opened_at, updated_at, current_summary, priority_score, recency_score, scope_tags_json) VALUES (?, 'task', ?, 'dormant', ?, ?, ?, 1, 1, '{}')",
            ('episode:video-part12-a', 'Napravio si part 1 i part 2 hteli smo slike da menjamo', 20.0, 20.0, noisy_summary),
        )
        hook_store.conn.execute(
            "INSERT INTO episodes (episode_id, kind, title, status, opened_at, updated_at, current_summary, priority_score, recency_score, scope_tags_json) VALUES (?, 'task', ?, 'dormant', ?, ?, ?, 1, 1, '{}')",
            ('episode:video-part12-b', 'Imas vec part 1', 19.0, 19.0, noisy_summary),
        )
        hook_store.conn.commit()
        remembered = ctx._load_live_brain_context('posalji mi sad part 1 i part 2 video', 's', '1280801428')
        assert 'Napravio si part 1 i part 2' in remembered, remembered
        assert 'Imas vec part 1' in remembered, remembered
        assert 'client_secret' not in remembered and 'voice message' not in remembered, remembered
        hook_store.close()
        hook_artifact = Path(tempfile.NamedTemporaryFile(delete=False, suffix='.png').name)
        hook_artifact.write_bytes(b'image')
        ctx._record_context_impression('agent:main:telegram:dm:hook', 's', 'generate seedream image png', '', [], allow_empty=True)
        ctx._post_tool_call(
            tool_name='image_generate',
            args={'prompt': 'generate seedream image', 'output_path': str(hook_artifact)},
            result=json.dumps({'success': True, 'image': str(hook_artifact)}),
            session_id='s',
            tool_call_id='call-success',
            duration_ms=123,
        )
        ctx._post_tool_call(
            tool_name='image_generate',
            args={'prompt': 'generate seedream image'},
            result=json.dumps({'success': False, 'error': '401 unauthorized: model not found', 'error_type': 'api_error'}),
            session_id='s',
            tool_call_id='call-failure',
        )
        hook_conn = sqlite3.connect(hook_db)
        hook_conn.row_factory = sqlite3.Row
        ok = hook_conn.execute("SELECT success, artifact_verified, artifact_path, duration_ms FROM tool_results WHERE tool_name='image_generate' AND success=1").fetchone()
        bad = hook_conn.execute("SELECT success, error_type FROM tool_results WHERE tool_name='image_generate' AND success=0").fetchone()
        activation = hook_conn.execute("SELECT tool_used, artifact_verified, success FROM causal_activations WHERE scope_key='agent:main:telegram:dm:hook' AND success=1").fetchone()
        hook_conn.close()
        assert ok and ok['artifact_verified'] == 1 and ok['artifact_path'] == str(hook_artifact) and ok['duration_ms'] == 123
        assert bad and bad['error_type'] in {'auth', 'not_found'}
        assert activation and activation['tool_used'] == 'image_generate' and activation['artifact_verified'] == 1 and activation['success'] == 1
    if old_home is None:
        os.environ.pop('HERMES_HOME', None)
    else:
        os.environ['HERMES_HOME'] = old_home

    store.close()
    print('live_brain smoke ok')


if __name__ == '__main__':
    main()
