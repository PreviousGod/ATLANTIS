#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live_brain.evolution import SelfEvolutionManager
from live_brain.ingest import Ingestor
from live_brain.store import LiveBrainStore


def _store(tmp: str) -> LiveBrainStore:
    store = LiveBrainStore(str(Path(tmp) / 'brain.db'))
    store.initialize_schema()
    return store


def test_low_risk_recipe_demotion_auto_applies() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        now = time.time()
        recipe_id = 'recipe:test-auto-demote'
        store.conn.execute(
            "INSERT INTO fix_recipes (recipe_id, scope_key, problem_pattern, tool_name, steps_json, args_template_json, success_criteria, artifact_verified, artifact_path, promotion_status, confidence, times_confirmed, status, source, scope_tags_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (recipe_id, 'scope:test', 'image output wrong', 'image_generate', '[]', '{}', 'image exists', 1, '/tmp/out.png', 'active', 0.9, 3, 'active', 'fixture', '{}', now, now),
        )
        result = SelfEvolutionManager(store.conn).propose(
            scope_key='scope:test',
            trigger_text='output wrong',
            proposal_type='demote_fix_recipe',
            target_area='recipe',
            rationale='failed context feedback',
            proposed_action='Move bounded recipe IDs to needs_review only.',
            evidence={'recipe_ids': [recipe_id]},
            suggested_tests=['context eval'],
            auto_apply=True,
        )
        assert result['status'] == 'auto_applied'
        assert result['risk_level'] == 'low'
        row = store.conn.execute('SELECT status, promotion_status FROM fix_recipes WHERE recipe_id=?', (recipe_id,)).fetchone()
        assert row['status'] == 'needs_review'
        assert row['promotion_status'] == 'needs_review'
        audit_count = store.conn.execute("SELECT COUNT(*) c FROM audit_log WHERE object_type='self_evolution_proposal'").fetchone()['c']
        assert audit_count >= 1
        revision_count = store.conn.execute("SELECT COUNT(*) c FROM object_revisions WHERE object_type='self_evolution_proposal' AND object_id=?", (result['proposal_id'],)).fetchone()['c']
        assert revision_count >= 1
        recipe_revision_count = store.conn.execute("SELECT COUNT(*) c FROM object_revisions WHERE object_type='fix_recipe' AND object_id=? AND action='self_evolution_demote'", (recipe_id,)).fetchone()['c']
        assert recipe_revision_count == 1
        store.close()


def test_high_risk_code_patch_requires_approval() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        result = SelfEvolutionManager(store.conn).propose(
            scope_key='scope:test',
            session_id='session:test',
            trigger_text='change live brain code',
            proposal_type='code_patch',
            target_area='code',
            rationale='need behavior change',
            proposed_action='Patch plugin code and run migration.',
            evidence={'requires_code_change': True},
            suggested_tests=['py_compile', 'smoke_test'],
            auto_apply=True,
        )
        assert result['status'] == 'needs_approval'
        assert result['risk_level'] == 'high'
        assert result['requires_approval'] == 1
        assert result['auto_apply_allowed'] == 0
        proposals = store.list_self_evolution_proposals(limit=5)
        assert proposals and proposals[0]['proposal_id'] == result['proposal_id']
        decided = store.decide_self_evolution_proposal(result['proposal_id'], 'rejected', 'test rejection')
        assert decided['status'] == 'rejected'
        store.close()


def test_pending_approval_is_visible_in_context() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / 'home'
        live_dir = home / 'live_brain'
        live_dir.mkdir(parents=True)
        store = LiveBrainStore(str(live_dir / 'live_brain.db'))
        store.initialize_schema()
        result = SelfEvolutionManager(store.conn).propose(
            scope_key='agent:main:telegram:dm:1280801428',
            session_id='session:test',
            trigger_text='make live brain fully autonomous',
            proposal_type='code_patch',
            target_area='code',
            rationale='approval visibility test',
            proposed_action='Patch Live Brain code after explicit approval only.',
            evidence={'requires_code_change': True},
            suggested_tests=['py_compile'],
            auto_apply=False,
        )
        store.close()
        old_home = os.environ.get('HERMES_HOME')
        os.environ['HERMES_HOME'] = str(home)
        try:
            from live_brain_ctx import _load_live_brain_context
            automatic_context = _load_live_brain_context('napravi jednu kratku recenicu', '', '1280801428')
            repeated_chitchat_context = _load_live_brain_context('cao', '', '1280801428')
            relevant_context = _load_live_brain_context('nastavi live brain patch', '', '1280801428')
            explicit_context = _load_live_brain_context('show pending self-evolution approval', '', '1280801428')
        finally:
            if old_home is None:
                os.environ.pop('HERMES_HOME', None)
            else:
                os.environ['HERMES_HOME'] = old_home
        assert automatic_context == ''
        assert repeated_chitchat_context == ''
        assert 'PENDING APPROVAL' in relevant_context
        assert result['proposal_id'] in relevant_context
        assert 'PENDING APPROVAL' in explicit_context
        assert result['proposal_id'] in explicit_context
        assert 'brain_self_evolution action=decide' in explicit_context


def test_decide_latest_pending_without_id() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        result = SelfEvolutionManager(store.conn).propose(
            scope_key='scope:test',
            session_id='session:test',
            trigger_text='change live brain code latest fallback',
            proposal_type='code_patch',
            target_area='code',
            rationale='latest fallback test',
            proposed_action='Patch code only after approval.',
            evidence={'requires_code_change': True},
            suggested_tests=['py_compile'],
            auto_apply=False,
        )
        decided = store.decide_self_evolution_proposal('', 'approved', 'latest fallback approved')
        assert decided['proposal_id'] == result['proposal_id']
        assert decided['status'] == 'approved'
        store.close()


def test_expired_proposals_are_hidden_from_default_list() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        result = SelfEvolutionManager(store.conn).propose(
            scope_key='scope:test',
            session_id='session:test',
            trigger_text='ACK-SEED capability e2e stale',
            proposal_type='code_patch',
            target_area='code',
            rationale='capability e2e seed',
            proposed_action='Patch only in test.',
            evidence={'source': 'capability_e2e'},
            suggested_tests=['py_compile'],
            auto_apply=False,
        )
        old = time.time() - 3 * 3600
        store.conn.execute("UPDATE self_evolution_proposals SET created_at=?, updated_at=? WHERE proposal_id=?", (old, old, result['proposal_id']))
        store.conn.commit()
        expired = store.expire_stale_self_evolution_proposals(now=time.time(), stale_hours=24, e2e_seed_hours=2)
        assert expired['expired'] == 1, expired
        assert store.list_self_evolution_proposals(limit=10) == []
        explicit = store.list_self_evolution_proposals(status='expired', include_applied=True, limit=10)
        assert explicit and explicit[0]['proposal_id'] == result['proposal_id']
        store.close()


def test_review_only_prompt_does_not_create_self_evolution_noise() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        now = time.time()
        Ingestor(store.conn).ingest_turn(
            session_id='session:test',
            scope_key='agent:main:telegram:dm:1280801428',
            turn_index=1,
            user_text='Napravi mi full review o Live Brain pluginu. Daj VERDICT, AGREE, DISAGREE, MUST_FIX_NEXT.',
            assistant_text='VERDICT: conditional review only.',
            created_at=now,
        )
        count = store.conn.execute("SELECT COUNT(*) c FROM self_evolution_proposals WHERE status='needs_approval'").fetchone()['c']
        assert count == 0
        store.close()


def test_capability_e2e_seed_does_not_create_self_evolution_noise() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        now = time.time()
        Ingestor(store.conn).ingest_turn(
            session_id='session:test',
            scope_key='agent:main:telegram:dm:1280801428',
            turn_index=1,
            user_text='LIVE_BRAIN_CAPABILITY_E2E seed run-test: dovršiti Live Brain plugin, patch code, busy_timeout, config, schema. Odgovori ACK.',
            assistant_text='ACK',
            created_at=now,
        )
        count = store.conn.execute("SELECT COUNT(*) c FROM self_evolution_proposals WHERE status='needs_approval'").fetchone()['c']
        assert count == 0
        store.close()


def test_missing_blocker_inquiry_does_not_create_self_evolution_noise() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        now = time.time()
        Ingestor(store.conn).ingest_turn(
            session_id='session:test',
            scope_key='agent:main:telegram:dm:1280801428',
            turn_index=1,
            user_text='Nastavi posle brain_self_evolution tool-call-a i odgovori tekstom: šta TAČNO fali kao hard blocker za Live Brain plugin? Ako nema hard blocker-a, napiši NEMA HARD BLOCKER i samo nice-to-have.',
            assistant_text='NEMA HARD BLOCKER. Nice-to-have only.',
            created_at=now,
        )
        count = store.conn.execute("SELECT COUNT(*) c FROM self_evolution_proposals WHERE status='needs_approval'").fetchone()['c']
        assert count == 0
        store.close()


def test_auto_approval_banner_does_not_create_false_proposal() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        now = time.time()
        Ingestor(store.conn).ingest_turn(
            session_id='session:test',
            scope_key='agent:main:telegram:dm:1280801428',
            turn_index=1,
            user_text='cao',
            assistant_text='Pending self-evolution approval: config_change context patch. Say approve latest pending self-evolution.',
            created_at=now,
        )
        count = store.conn.execute("SELECT COUNT(*) c FROM self_evolution_proposals WHERE status='needs_approval'").fetchone()['c']
        assert count == 0
        store.close()


if __name__ == '__main__':
    test_low_risk_recipe_demotion_auto_applies()
    test_high_risk_code_patch_requires_approval()
    test_pending_approval_is_visible_in_context()
    test_decide_latest_pending_without_id()
    test_expired_proposals_are_hidden_from_default_list()
    test_review_only_prompt_does_not_create_self_evolution_noise()
    test_capability_e2e_seed_does_not_create_self_evolution_noise()
    test_missing_blocker_inquiry_does_not_create_self_evolution_noise()
    test_auto_approval_banner_does_not_create_false_proposal()
    print('live_brain_self_evolution_test: PASS')
