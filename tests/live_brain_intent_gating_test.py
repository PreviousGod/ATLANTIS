#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import tempfile
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live_brain.artifacts import ArtifactRegistry
from live_brain.evolution import SelfEvolutionManager
from live_brain.store import LiveBrainStore
from live_brain_ctx.modules.query_classification import _classify_query_intent


def _load_context_module():
    if 'agent.context_compressor' not in sys.modules:
        agent_mod = types.ModuleType('agent')
        compressor_mod = types.ModuleType('agent.context_compressor')

        class ContextCompressor:
            def compress(self, messages, current_tokens=None, focus_topic=None):
                return messages

        compressor_mod.ContextCompressor = ContextCompressor
        sys.modules.setdefault('agent', agent_mod)
        sys.modules['agent.context_compressor'] = compressor_mod
    import live_brain_ctx
    return live_brain_ctx


def test_greeting_stays_empty_even_with_active_task_memory() -> None:
    # Regression guard: short greetings must not revive ACTIVE TASK or approval routing noise.
    old_home = os.environ.get('HERMES_HOME')
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / 'home'
        live_dir = home / 'live_brain'
        live_dir.mkdir(parents=True)
        store = LiveBrainStore(str(live_dir / 'live_brain.db'))
        store.initialize_schema()
        scope = 'agent:main:telegram:dm:tester'
        now = time.time()
        store.conn.execute(
            """
            INSERT INTO work_items
            (work_item_id, scope_key, session_id, title, status, priority, evidence_json, next_step, root_cause, created_at, updated_at)
            VALUES ('work:greeting', ?, 'session-1', 'Patch greeting leak', 'active', 1.0, '{}', 'remove noisy context', 'intent gate missing', ?, ?)
            """,
            (scope, now, now),
        )
        store.conn.commit()
        store.close()
        os.environ['HERMES_HOME'] = str(home)
        try:
            ctx = _load_context_module()
            context = ctx._load_live_brain_context('E Cao', 'session-1', 'tester')
        finally:
            if old_home is None:
                os.environ.pop('HERMES_HOME', None)
            else:
                os.environ['HERMES_HOME'] = old_home
        assert context == '', context


def test_recap_prefers_recap_and_continuity_over_execution_noise() -> None:
    # Regression guard: recap intent should surface recap memory, not PROVEN FIX or NEXT REQUIRED ACTION.
    old_home = os.environ.get('HERMES_HOME')
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / 'home'
        live_dir = home / 'live_brain'
        live_dir.mkdir(parents=True)
        store = LiveBrainStore(str(live_dir / 'live_brain.db'))
        store.initialize_schema()
        scope = 'agent:main:telegram:dm:tester'
        now = time.time()
        store.conn.execute(
            """
            INSERT INTO work_items
            (work_item_id, scope_key, session_id, title, status, priority, evidence_json, next_step, root_cause, created_at, updated_at)
            VALUES ('work:recap', ?, 'session-2', 'Stabilize context gate', 'active', 1.0, '{}', 'run recap regression tests', 'default active task leakage', ?, ?)
            """,
            (scope, now, now),
        )
        store.conn.execute(
            """
            INSERT INTO canonical_recaps
            (recap_id, session_id, scope_key, task, main_problem, root_cause, what_changed, current_status, next_step, confidence, created_at, updated_at)
            VALUES ('recap:1', 'session-2', ?, 'Stabilize context gate', 'Prompt noise', 'Default surfacing was too broad', 'Intent gate patch in progress', 'Intent gate patch in progress', 'Run recap regression tests', 0.95, ?, ?)
            """,
            (scope, now, now),
        )
        store.conn.commit()
        store.close()
        os.environ['HERMES_HOME'] = str(home)
        try:
            ctx = _load_context_module()
            context = ctx._load_live_brain_context('sta si radio danas', 'session-2', 'tester')
        finally:
            if old_home is None:
                os.environ.pop('HERMES_HOME', None)
            else:
                os.environ['HERMES_HOME'] = old_home
        assert 'LATEST RECAP' in context, context
        assert 'ACTIVE TASK' not in context, context
        assert 'NEXT REQUIRED ACTION' not in context, context
        assert 'PROVEN FIX' not in context, context


def test_repo_lookup_prefers_verified_artifacts_and_skips_active_task() -> None:
    # Regression guard: local file lookup should stay factual and file-oriented.
    old_home = os.environ.get('HERMES_HOME')
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / 'home'
        live_dir = home / 'live_brain'
        live_dir.mkdir(parents=True)
        artifact_path = Path(tmp) / 'plugin.yaml'
        artifact_path.write_text('name: hermes-plugin\n', encoding='utf-8')
        store = LiveBrainStore(str(live_dir / 'live_brain.db'))
        store.initialize_schema()
        scope = 'agent:main:telegram:dm:tester'
        now = time.time()
        registry = ArtifactRegistry(store.conn)
        registry.upsert_artifact(
            project_key='hermes',
            role='plugin_yaml',
            path=str(artifact_path),
            label='verified plugin manifest',
            scope_tags={'repo': ['hermes']},
        )
        store.conn.execute(
            """
            INSERT INTO work_items
            (work_item_id, scope_key, session_id, title, status, priority, evidence_json, next_step, root_cause, created_at, updated_at)
            VALUES ('work:repo', ?, 'session-3', 'Do not leak active task into repo lookup', 'active', 1.0, '{}', 'should stay hidden', 'wrong intent routing', ?, ?)
            """,
            (scope, now, now),
        )
        store.conn.commit()
        store.close()
        os.environ['HERMES_HOME'] = str(home)
        try:
            ctx = _load_context_module()
            context = ctx._load_live_brain_context('Koji fajlovi imaju plugin.yaml u hermesu', 'session-3', 'tester')
        finally:
            if old_home is None:
                os.environ.pop('HERMES_HOME', None)
            else:
                os.environ['HERMES_HOME'] = old_home
        assert 'VERIFIED ARTIFACTS' in context, context
        assert 'plugin.yaml' in context, context
        assert 'ACTIVE TASK' not in context, context
        assert 'NEXT REQUIRED ACTION' not in context, context


def test_approval_prompt_stays_in_approval_sections_only() -> None:
    # Regression guard: approval queries should not drag generic task or repo sections into the prompt.
    old_home = os.environ.get('HERMES_HOME')
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / 'home'
        live_dir = home / 'live_brain'
        live_dir.mkdir(parents=True)
        store = LiveBrainStore(str(live_dir / 'live_brain.db'))
        store.initialize_schema()
        result = SelfEvolutionManager(store.conn).propose(
            scope_key='agent:main:telegram:dm:tester',
            session_id='session-4',
            trigger_text='approval test',
            proposal_type='code_patch',
            target_area='code',
            rationale='approval routing regression guard',
            proposed_action='Patch only after explicit approval.',
            evidence={'requires_code_change': True},
            suggested_tests=['pytest'],
            auto_apply=False,
        )
        store.close()
        os.environ['HERMES_HOME'] = str(home)
        try:
            ctx = _load_context_module()
            context = ctx._load_live_brain_context('approval prompt', 'session-4', 'tester')
        finally:
            if old_home is None:
                os.environ.pop('HERMES_HOME', None)
            else:
                os.environ['HERMES_HOME'] = old_home
        assert 'PENDING APPROVAL' in context, context
        assert result['proposal_id'] in context, context
        assert 'ACTIVE TASK' not in context, context
        assert 'VERIFIED ARTIFACTS' not in context, context


def test_diagnostic_plugin_query_stays_task_execution() -> None:
    # Regression guard: plugin/gateway/memory terms alone must not demote a real bug report into repo lookup.
    intent = _classify_query_intent(
        'plugin gateway memory error ne radi posle patcha',
        chit_chat_patterns={'cao'},
    )
    assert intent == 'task_execution', intent


def test_recap_with_file_term_stays_recap() -> None:
    # Regression guard: recap phrasing wins even if the prompt also mentions files.
    intent = _classify_query_intent(
        'sta si radio danas oko plugin file path-a',
        chit_chat_patterns={'cao'},
    )
    assert intent == 'continuity_recap', intent


def test_approval_with_patch_terms_stays_approval_flow() -> None:
    # Regression guard: approval prompts win over patch/fix wording.
    intent = _classify_query_intent(
        'approve latest patch for plugin fix',
        chit_chat_patterns={'cao'},
    )
    assert intent == 'approval_flow', intent


if __name__ == '__main__':
    test_greeting_stays_empty_even_with_active_task_memory()
    test_recap_prefers_recap_and_continuity_over_execution_noise()
    test_repo_lookup_prefers_verified_artifacts_and_skips_active_task()
    test_approval_prompt_stays_in_approval_sections_only()
    test_diagnostic_plugin_query_stays_task_execution()
    test_recap_with_file_term_stays_recap()
    test_approval_with_patch_terms_stays_approval_flow()
    print('live_brain_intent_gating_test: PASS')
