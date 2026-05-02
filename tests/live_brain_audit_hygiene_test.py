#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live_brain.ingest import Ingestor
from live_brain.store import LiveBrainStore


def test_audit_schema_and_revision_recording() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = LiveBrainStore(str(Path(tmp) / 'brain.db'))
        store.initialize_schema()
        tables = {
            row['name']
            for row in store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('memory_events','object_revisions','evidence_packets','maintenance_runs')"
            ).fetchall()
        }
        assert tables == {'memory_events', 'object_revisions', 'evidence_packets', 'maintenance_runs'}, tables
        fact_cols = {row['name'] for row in store.conn.execute('PRAGMA table_info(facts)').fetchall()}
        assert {'evidence_packet_id', 'source_turn_id', 'source_event_id'} <= fact_cols, fact_cols

        ingestor = Ingestor(store.conn)
        fact = ingestor.store_fact(
            'test_fact',
            'Audit spine stores fact revisions.',
            0.9,
            'test',
            1000.0,
            session_id='s',
            scope_key='agent:main:telegram:dm:audit',
        )
        revision_count = store.conn.execute(
            "SELECT COUNT(*) c FROM object_revisions WHERE object_type='fact' AND object_id=? AND action='upsert'",
            (fact['fact_id'],),
        ).fetchone()['c']
        assert revision_count == 1
        event_count = store.conn.execute(
            "SELECT COUNT(*) c FROM memory_events WHERE object_type='fact' AND object_id=?",
            (fact['fact_id'],),
        ).fetchone()['c']
        assert event_count == 1
        store.close()


def test_epistemic_fact_creates_evidence_packet() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = LiveBrainStore(str(Path(tmp) / 'brain.db'))
        store.initialize_schema()
        result = store.record_epistemic_fact(
            scope_key='agent:main:telegram:dm:evidence',
            question='Where is Python documented?',
            fact_text='Python language documentation is published at docs.python.org.',
            source_urls=['https://docs.python.org/3/'],
            confidence=0.82,
            ttl_seconds=86400,
            raw_excerpt='Python documentation official landing page with references, tutorials, and library documentation.',
        )
        assert result['status'] == 'recorded', result
        assert result['evidence_packet_id'], result
        row = store.conn.execute(
            'SELECT evidence_packet_id FROM epistemic_learned_facts WHERE fact_id=?',
            (result['fact_id'],),
        ).fetchone()
        assert row and row['evidence_packet_id'] == result['evidence_packet_id']
        packet = store.conn.execute(
            'SELECT source_urls_json, authority FROM evidence_packets WHERE evidence_packet_id=?',
            (result['evidence_packet_id'],),
        ).fetchone()
        assert packet and 'docs.python.org' in packet['source_urls_json'] and packet['authority'] in {'official', 'primary_or_support', 'unknown'}
        store.close()


def test_lifecycle_hygiene_dry_run_and_apply() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = LiveBrainStore(str(Path(tmp) / 'brain.db'))
        store.initialize_schema()
        now = 2_000_000_000.0
        old = now - 60 * 86400
        scope = 'agent:main:telegram:dm:hygiene'
        store.conn.execute(
            "INSERT INTO context_impressions (impression_id, scope_key, session_id, query_text, context_hash, sections_json, recipe_ids_json, outcome, feedback_text, created_at, updated_at) VALUES (?, ?, 's', 'old query', 'h', '[]', '[]', 'pending', '', ?, ?)",
            ('impression:old', scope, old, old),
        )
        store.conn.execute(
            "INSERT INTO work_items (work_item_id, scope_key, session_id, title, status, priority, evidence_json, next_step, root_cause, created_at, updated_at) VALUES (?, ?, 's', 'old low priority task', 'active', 0.1, '{}', '', '', ?, ?)",
            ('work:old', scope, old, old),
        )
        store.conn.execute(
            "INSERT INTO beliefs (belief_id, claim_text, belief_kind, confidence, status, created_at, updated_at, session_id, scope_key) VALUES (?, 'weak old hypothesis', 'hypothesis', 0.3, 'open', ?, ?, 's', ?)",
            ('belief:old', old, old, scope),
        )
        store.conn.commit()

        dry = store.run_lifecycle_hygiene(dry_run=True, now=now)
        assert dry['expired_context_impressions'] == 1, dry
        assert dry['superseded_work_items'] == 1, dry
        assert dry['invalidated_low_confidence_beliefs'] == 1, dry
        assert dry['status'] == 'dry_run', dry
        assert store.conn.execute("SELECT outcome FROM context_impressions WHERE impression_id='impression:old'").fetchone()['outcome'] == 'pending'

        applied = store.run_lifecycle_hygiene(dry_run=False, now=now)
        assert applied['status'] == 'ok', applied
        assert store.conn.execute("SELECT outcome FROM context_impressions WHERE impression_id='impression:old'").fetchone()['outcome'] == 'expired'
        assert store.conn.execute("SELECT status FROM work_items WHERE work_item_id='work:old'").fetchone()['status'] == 'superseded'
        assert store.conn.execute("SELECT status FROM beliefs WHERE belief_id='belief:old'").fetchone()['status'] == 'invalidated'
        revisions = store.conn.execute(
            "SELECT COUNT(*) c FROM object_revisions WHERE object_id IN ('impression:old','work:old','belief:old')"
        ).fetchone()['c']
        assert revisions >= 3
        runs = store.conn.execute("SELECT COUNT(*) c FROM maintenance_runs WHERE run_type='lifecycle_hygiene'").fetchone()['c']
        assert runs >= 2
        store.close()


def test_stale_self_evolution_proposals_expire_with_audit() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = LiveBrainStore(str(Path(tmp) / 'brain.db'))
        store.initialize_schema()
        now = 2_000_000_000.0
        old = now - 3 * 3600
        fresh = now - 1800
        store.conn.execute(
            """
            INSERT INTO self_evolution_proposals
            (proposal_id, scope_key, session_id, trigger_text, proposal_type, target_area, rationale, proposed_action, evidence_json, suggested_tests_json, risk_level, risk_score, status, auto_apply_allowed, requires_approval, apply_result_json, created_at, updated_at, decided_at)
            VALUES (?, 'scope', 'session', ?, 'code_patch', 'code', ?, 'patch test', '{}', '[]', 'high', 0.9, 'needs_approval', 0, 1, '{}', ?, ?, NULL)
            """,
            ('proposal:e2e-old', 'ACK-SEED capability e2e old', 'capability e2e seed', old, old),
        )
        store.conn.execute(
            """
            INSERT INTO self_evolution_proposals
            (proposal_id, scope_key, session_id, trigger_text, proposal_type, target_area, rationale, proposed_action, evidence_json, suggested_tests_json, risk_level, risk_score, status, auto_apply_allowed, requires_approval, apply_result_json, created_at, updated_at, decided_at)
            VALUES (?, 'scope', 'session', ?, 'code_patch', 'code', ?, 'patch test', '{}', '[]', 'high', 0.9, 'needs_approval', 0, 1, '{}', ?, ?, NULL)
            """,
            ('proposal:fresh', 'real user approval', 'real request', fresh, fresh),
        )
        store.conn.commit()

        dry = store.expire_stale_self_evolution_proposals(dry_run=True, now=now, stale_hours=24, e2e_seed_hours=2)
        assert dry['expired'] == 1, dry
        applied = store.run_lifecycle_hygiene(dry_run=False, now=now, stale_pending_proposal_hours=24, e2e_seed_pending_hours=2)
        assert applied['expired_self_evolution_proposals'] == 1, applied
        assert store.conn.execute("SELECT status FROM self_evolution_proposals WHERE proposal_id='proposal:e2e-old'").fetchone()['status'] == 'expired'
        assert store.conn.execute("SELECT status FROM self_evolution_proposals WHERE proposal_id='proposal:fresh'").fetchone()['status'] == 'needs_approval'
        revisions = store.conn.execute(
            "SELECT COUNT(*) c FROM object_revisions WHERE object_type='self_evolution_proposal' AND object_id='proposal:e2e-old' AND action='expired'"
        ).fetchone()['c']
        assert revisions == 1
        store.close()


def test_backup_rotation_and_wal_checkpoint() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / 'brain.db'
        store = LiveBrainStore(str(db_path))
        store.initialize_schema()
        old_backup = db_path.with_name('brain_backup_old_1.db')
        keep_backup = db_path.with_name('brain_backup_keep_2.db')
        old_backup.write_text('old', encoding='utf-8')
        keep_backup.write_text('keep', encoding='utf-8')
        old_mtime = time.time() - 72 * 3600
        os.utime(old_backup, (old_mtime, old_mtime))

        checkpoint = store.checkpoint_wal(truncate=True)
        assert checkpoint['status'] == 'ok', checkpoint
        dry = store.rotate_backups(max_age_hours=48, max_keep=8, dry_run=True)
        assert dry['deleted'] == 1, dry
        applied = store.rotate_backups(max_age_hours=48, max_keep=8)
        assert applied['deleted'] == 1, applied
        assert not old_backup.exists()
        assert keep_backup.exists()
        store.close()


def test_init_maintenance_rate_limits_side_effects() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / 'home'
        live_dir = home / 'live_brain'
        live_dir.mkdir(parents=True)
        store = LiveBrainStore(str(live_dir / 'live_brain.db'))
        store.initialize_schema()
        first = store.run_init_maintenance(scope_key='scope:init', hermes_home=str(home), min_interval_seconds=3600, now=2_000_000_000.0)
        second = store.run_init_maintenance(scope_key='scope:init', hermes_home=str(home), min_interval_seconds=3600, now=2_000_000_100.0)
        assert first['status'] == 'ok', first
        assert second['status'] == 'skipped', second
        assert second['reason'] == 'rate_limited'
        count = store.conn.execute("SELECT COUNT(*) c FROM maintenance_runs WHERE run_type='init_maintenance' AND status='ok'").fetchone()['c']
        assert count == 1
        store.close()


def test_project_media_blockers_do_not_pollute_plugin_self_review_context() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / 'home'
        live_dir = home / 'live_brain'
        live_dir.mkdir(parents=True)
        store = LiveBrainStore(str(live_dir / 'live_brain.db'))
        store.initialize_schema()
        now = time.time()
        scope = 'agent:main:telegram:dm:ctxfilter'
        media_instruction = 'PRODUCTION BLOCKER 3 — MEDIA DELIVERY for Enoch video attachments must be fixed.'
        store.conn.execute(
            """
            INSERT INTO rules
            (rule_id, scope, category, scope_tags_json, condition_json, action_json, confidence, source, times_confirmed, status, expires_at, specificity, created_at, updated_at)
            VALUES ('rule:media:blocker', 'user_binding', 'binding_constraint', '{}', '{}', ?, 0.99, 'user_binding', 3, 'active', NULL, 1, ?, ?)
            """,
            (json.dumps({'instruction': media_instruction}), now, now),
        )
        store.conn.execute(
            """
            INSERT INTO facts
            (fact_id, subject_entity_id, fact_type, fact_text, confidence, source_kind, valid_from, valid_to, status, evidence_count, session_id, scope_key)
            VALUES ('fact:media:blocker', NULL, 'learned', ?, 0.95, 'test', ?, NULL, 'active', 1, 'session', ?)
            """,
            ('PRODUCTION BLOCKER — WRONG ARTIFACT SELECTION in Enoch normal chat.', now, scope),
        )
        store.conn.execute(
            """
            INSERT INTO facts
            (fact_id, subject_entity_id, fact_type, fact_text, confidence, source_kind, valid_from, valid_to, status, evidence_count, session_id, scope_key)
            VALUES ('fact:codename:test', NULL, 'learned', ?, 0.95, 'test', ?, NULL, 'active', 1, 'session', ?)
            """,
            ('LIVE_BRAIN_CAPABILITY_E2E seed run-test: tajni codename je codename-secret123.', now, scope),
        )
        store.conn.commit()
        store.close()

        old_home = os.environ.get('HERMES_HOME')
        os.environ['HERMES_HOME'] = str(home)
        try:
            from live_brain_ctx import _load_live_brain_context
            plugin_context = _load_live_brain_context(
                'LIVE_BRAIN_CAPABILITY_E2E self-review run-test: pregledaj samo plugin capability ponašanje ovog run-a',
                '',
                'ctxfilter',
            ).lower()
            media_context = _load_live_brain_context(
                'review media delivery and Enoch video artifact blocker',
                '',
                'ctxfilter',
            ).lower()
            research_context = _load_live_brain_context(
                'LIVE_BRAIN_CAPABILITY_E2E research run-test: Koja su najnovija CME pravila?',
                '',
                'ctxfilter',
            ).lower()
        finally:
            if old_home is None:
                os.environ.pop('HERMES_HOME', None)
            else:
                os.environ['HERMES_HOME'] = old_home
        assert 'enoch' not in plugin_context
        assert 'media delivery' not in plugin_context
        assert 'wrong artifact' not in plugin_context
        assert 'media delivery' in media_context or 'wrong artifact' in media_context
        assert 'codename-secret123' not in research_context


def test_epistemic_research_pre_llm_isolates_run_memory() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / 'home'
        live_dir = home / 'live_brain'
        live_dir.mkdir(parents=True)
        store = LiveBrainStore(str(live_dir / 'live_brain.db'))
        store.initialize_schema()
        now = time.time()
        scope = 'agent:main:telegram:dm:researchiso'
        store.conn.execute(
            """
            INSERT INTO facts
            (fact_id, subject_entity_id, fact_type, fact_text, confidence, source_kind, valid_from, valid_to, status, evidence_count, session_id, scope_key)
            VALUES ('fact:codename:researchiso', NULL, 'learned', ?, 0.99, 'test', ?, NULL, 'active', 1, 'session', ?)
            """,
            ('LIVE_BRAIN_CAPABILITY_E2E seed run-iso: tajni codename je codename-secret999.', now, scope),
        )
        store.conn.execute(
            """
            INSERT INTO work_items
            (work_item_id, scope_key, session_id, title, status, priority, evidence_json, next_step, root_cause, created_at, updated_at)
            VALUES ('work:researchiso', ?, 'session', 'LIVE_BRAIN_CAPABILITY_E2E continue run-iso', 'active', 1.0, '{}', 'pokreni targeted smoke', 'sqlite busy timeout', ?, ?)
            """,
            (scope, now, now),
        )
        store.conn.commit()
        store.close()

        old_home = os.environ.get('HERMES_HOME')
        old_auto = os.environ.get('LIVE_BRAIN_AUTONOMOUS_RESEARCH')
        os.environ['HERMES_HOME'] = str(home)
        os.environ['LIVE_BRAIN_AUTONOMOUS_RESEARCH'] = '0'
        try:
            from live_brain_ctx import _pre_llm_call
            result = _pre_llm_call(
                user_message='LIVE_BRAIN_CAPABILITY_E2E research run-iso: Koja su najnovija CME pravila za NQ price limits? navedi cmegroup.com URL',
                session_id='session',
                sender_id='researchiso',
                platform='telegram',
            )
        finally:
            if old_home is None:
                os.environ.pop('HERMES_HOME', None)
            else:
                os.environ['HERMES_HOME'] = old_home
            if old_auto is None:
                os.environ.pop('LIVE_BRAIN_AUTONOMOUS_RESEARCH', None)
            else:
                os.environ['LIVE_BRAIN_AUTONOMOUS_RESEARCH'] = old_auto
        context = ((result or {}).get('context') or '').lower()
        assert 'epistemic isolation' in context
        assert 'codename-secret999' not in context
        assert 'run-iso' not in context
        assert 'active task:' not in context
        assert 'next required action' not in context


def test_capability_e2e_has_no_project_media_requirement() -> None:
    e2e = (ROOT / 'tools' / 'live_brain_capability_e2e.py').read_text(encoding='utf-8').lower()
    assert 'expect_media_count' not in e2e
    assert 'documentattributevideo' not in e2e


if __name__ == '__main__':
    test_audit_schema_and_revision_recording()
    test_epistemic_fact_creates_evidence_packet()
    test_lifecycle_hygiene_dry_run_and_apply()
    test_stale_self_evolution_proposals_expire_with_audit()
    test_backup_rotation_and_wal_checkpoint()
    test_init_maintenance_rate_limits_side_effects()
    test_project_media_blockers_do_not_pollute_plugin_self_review_context()
    test_epistemic_research_pre_llm_isolates_run_memory()
    test_capability_e2e_has_no_project_media_requirement()
    print('live_brain_audit_hygiene_test: PASS')
