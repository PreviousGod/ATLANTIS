#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from live_brain.artifacts import ArtifactRegistry
from live_brain.store import LiveBrainStore
from live_brain.utils import stable_id
from live_brain_control_room import run_server

DEFAULT_DEMO_DIR = Path('/tmp/live_brain_control_room_demo')


def ts(minutes_ago: int = 0) -> float:
    return time.time() - minutes_ago * 60


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def write_demo_artifacts(root: Path) -> Dict[str, str]:
    artifacts = root / 'artifacts'
    artifacts.mkdir(parents=True, exist_ok=True)
    files = {
        'part1': artifacts / 'enoch_part1_verified_v13.mp4',
        'part2': artifacts / 'enoch_part2_correct_final.mp4',
        'old_part2': artifacts / 'enoch_part2_old_wrong_cut.mp4',
        'combined': artifacts / 'enoch_full_combined_verified.mp4',
    }
    for key, path in files.items():
        if not path.exists():
            path.write_bytes((f'DEMO_PLACEHOLDER_{key}\n').encode() * 32)
    return {key: str(path) for key, path in files.items()}


def insert_fact(conn, *, fact_id: str, fact_type: str, fact_text: str, confidence: float, source_kind: str, minutes_ago: int = 0, status: str = 'active') -> None:
    conn.execute(
        "INSERT OR REPLACE INTO facts (fact_id, subject_entity_id, fact_type, fact_text, confidence, source_kind, valid_from, valid_to, status, evidence_count) VALUES (?, '', ?, ?, ?, ?, ?, NULL, ?, ?)",
        (fact_id, fact_type, fact_text, confidence, source_kind, ts(minutes_ago), status, 3),
    )


def insert_belief(conn, *, belief_id: str, claim_text: str, belief_kind: str, confidence: float, status: str, minutes_ago: int, tool_name: str = '') -> None:
    when = ts(minutes_ago)
    conn.execute(
        "INSERT OR REPLACE INTO beliefs (belief_id, episode_id, claim_text, belief_kind, confidence, status, created_at, updated_at, validated_by, supersedes_belief_id, caused_by_work_item_id, tool_name) VALUES (?, '', ?, ?, ?, ?, ?, ?, '', '', '', ?)",
        (belief_id, claim_text, belief_kind, confidence, status, when, when, tool_name),
    )


def insert_rule(conn, *, rule_id: str, scope: str, category: str, condition: Dict[str, Any], action: Dict[str, Any], confidence: float, times_confirmed: int, specificity: int, minutes_ago: int) -> None:
    when = ts(minutes_ago)
    conn.execute(
        """
        INSERT OR REPLACE INTO rules (rule_id, scope, category, scope_tags_json, condition_json, action_json, confidence, source, times_confirmed, status, expires_at, specificity, last_matched_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'demo', ?, 'active', NULL, ?, ?, ?, ?)
        """,
        (rule_id, scope, category, '{}', dumps(condition), dumps(action), confidence, times_confirmed, specificity, when, when, when),
    )


def insert_work_item(conn, *, work_item_id: str, title: str, status: str, priority: float, next_step: str, root_cause: str, minutes_ago: int, scope_key: str = 'demo:enoch') -> None:
    when = ts(minutes_ago)
    resolved_at = when if status == 'resolved' else None
    conn.execute(
        """
        INSERT OR REPLACE INTO work_items (work_item_id, scope_key, session_id, title, status, priority, evidence_json, next_step, root_cause, supersedes_work_item_id, created_at, updated_at, resolved_at)
        VALUES (?, ?, 'demo_session', ?, ?, ?, ?, ?, ?, '', ?, ?, ?)
        """,
        (
            work_item_id,
            scope_key,
            title,
            status,
            priority,
            dumps({'demo': True, 'source': 'synthetic_demo'}),
            next_step,
            root_cause,
            when,
            when,
            resolved_at,
        ),
    )


def insert_activation(conn, *, activation_id: str, trigger_text: str, trigger_pattern: str, action_taken: str, tool_used: str, test_result: str, artifact_path: str, success: bool, confidence: float, times_confirmed: int, minutes_ago: int) -> None:
    when = ts(minutes_ago)
    conn.execute(
        """
        INSERT OR REPLACE INTO causal_activations (activation_id, scope_key, trigger_text, trigger_pattern, action_taken, tool_used, args_template_json, outcome, test_result, artifact_verified, artifact_path, error_type, success, confidence, times_confirmed, scope_tags_json, created_at, updated_at)
        VALUES (?, 'demo:enoch', ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, '{}', ?, ?)
        """,
        (
            activation_id,
            trigger_text,
            trigger_pattern,
            action_taken,
            tool_used,
            dumps({'project_key': 'enoch', 'include_inactive': False} if tool_used == 'brain_list_artifacts' else {}),
            test_result,
            test_result,
            1 if artifact_path else 0,
            artifact_path,
            1 if success else 0,
            confidence,
            times_confirmed,
            when,
            when,
        ),
    )


def insert_context_impression(conn, *, impression_id: str, query_text: str, sections: list[str], outcome: str, attribution_mode: str, minutes_ago: int) -> None:
    when = ts(minutes_ago)
    conn.execute(
        """
        INSERT OR REPLACE INTO context_impressions (impression_id, scope_key, session_id, query_text, context_hash, sections_json, recipe_ids_json, outcome, attribution_mode, source, feedback_text, created_at, updated_at)
        VALUES (?, 'demo:enoch', 'demo_session', ?, ?, ?, '[]', ?, ?, 'demo', '', ?, ?)
        """,
        (impression_id, query_text, stable_id('ctx', query_text), dumps(sections), outcome, attribution_mode, when, when),
    )


def insert_audit(conn, *, object_type: str, object_id: str, action: str, reason: str, minutes_ago: int, details: Dict[str, Any] | None = None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO audit_log (audit_id, object_type, object_id, action, reason, details_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (stable_id('audit', object_type, object_id, action, str(minutes_ago)), object_type, object_id, action, reason, dumps(details or {}), ts(minutes_ago)),
    )


def seed_demo(db_path: Path, *, reset: bool = False) -> Path:
    db_path = db_path.expanduser().resolve()
    if reset and db_path.exists():
        db_path.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_paths = write_demo_artifacts(db_path.parent)

    store = LiveBrainStore(str(db_path))
    store.initialize_schema()
    conn = store.conn

    registry = ArtifactRegistry(conn)
    registry.upsert_artifact(project_key='enoch', role='part_1', path=artifact_paths['part1'], label='Verified Part 1 final cut', status='verified', confidence=0.99, source='demo')
    registry.upsert_artifact(project_key='enoch', role='part_2', path=artifact_paths['part2'], label='Verified Part 2 correct narration', status='verified', confidence=0.99, source='demo')
    registry.upsert_artifact(project_key='enoch', role='part_2_old', path=artifact_paths['old_part2'], label='Old wrong Part 2 candidate', status='rejected', confidence=0.2, source='demo', evidence={'reason': 'wrong narration order'})
    registry.upsert_artifact(project_key='enoch', role='combined_or_full', path=artifact_paths['combined'], label='Verified combined upload', status='verified', confidence=0.97, source='demo')

    insert_fact(conn, fact_id='fact:demo:artifact_policy', fact_type='artifact_policy', fact_text='Before sending project media, resolve verified_artifacts by project and role; never trust fuzzy filename similarity.', confidence=0.98, source_kind='demo', minutes_ago=22)
    insert_fact(conn, fact_id='fact:demo:enoch_part2', fact_type='verified_artifact', fact_text=f'Enoch part_2 verified artifact is {artifact_paths["part2"]}; the old cut is rejected.', confidence=0.99, source_kind='demo', minutes_ago=18)
    insert_fact(conn, fact_id='fact:demo:approval_gate', fact_type='safety_gate', fact_text='Code/config/schema/media changes require self-evolution approval; low-risk metadata demotion may auto-apply with audit.', confidence=0.96, source_kind='demo', minutes_ago=10)

    insert_belief(conn, belief_id='belief:demo:validated_wrong_artifact', claim_text='The prior failure was caused by semantic search choosing an old rejected mp4 instead of the verified artifact registry entry.', belief_kind='validated_cause', confidence=0.94, status='validated', minutes_ago=16, tool_name='brain_list_artifacts')
    insert_belief(conn, belief_id='belief:demo:open_retention', claim_text='Shorts retention likely improves when thumbnail text matches the first 3 seconds of narration; needs A/B evidence.', belief_kind='hypothesis', confidence=0.58, status='open', minutes_ago=9, tool_name='youtube_analytics')

    insert_work_item(conn, work_item_id='work:demo:wrong_file', title='Prevent sending stale Enoch video files', status='resolved', priority=0.92, next_step='Use brain_resolve_artifact before any media send.', root_cause='Semantic search returned a similarly named but rejected Part 2 file.', minutes_ago=14)
    insert_work_item(conn, work_item_id='work:demo:approval_surface', title='Surface self-evolution approvals only when needed', status='active', priority=0.82, next_step='Demo the one-shot approval banner and audit trail.', root_cause='Approval queues are invisible if users must ask manually, but noisy if repeated every turn.', minutes_ago=4)
    insert_work_item(conn, work_item_id='work:demo:context_eval', title='Benchmark Live Brain against vector memory', status='blocked', priority=0.73, next_step='Run 20 scenarios: wrong artifact, stale recap, repeated mistake, approval gate.', root_cause='Need quantitative proof for public launch.', minutes_ago=2)

    insert_rule(conn, rule_id='rule:demo:artifact_first', scope='user_binding', category='binding_constraint', condition={'intent': 'send_project_file'}, action={'instruction': 'Use verified_artifacts / brain_resolve_artifact before sending media files.'}, confidence=0.97, times_confirmed=7, specificity=9, minutes_ago=17)
    insert_rule(conn, rule_id='rule:demo:approval_needed', scope='user_binding', category='safety_gate', condition={'target_area': ['code', 'config', 'db_schema', 'media']}, action={'instruction': 'Create a self-evolution proposal and wait for explicit approval before high-risk changes.'}, confidence=0.96, times_confirmed=5, specificity=10, minutes_ago=11)

    insert_activation(conn, activation_id='activation:demo:list_artifacts', trigger_text='which enoch files should I send?', trigger_pattern='send verified project artifacts', action_taken='Resolved exact verified artifacts before media delivery.', tool_used='brain_list_artifacts', test_result='verified artifacts returned correct part_1 and part_2 paths', artifact_path=artifact_paths['part2'], success=True, confidence=0.93, times_confirmed=8, minutes_ago=12)
    insert_activation(conn, activation_id='activation:demo:self_evolution', trigger_text='make Live Brain self evolving but safe', trigger_pattern='change live brain behavior', action_taken='Created gated self-evolution proposal instead of editing silently.', tool_used='brain_self_evolution', test_result='proposal visible once, approve-latest works, audit recorded', artifact_path='', success=True, confidence=0.88, times_confirmed=4, minutes_ago=5)
    insert_activation(conn, activation_id='activation:demo:semantic_failure', trigger_text='send part 2 video', trigger_pattern='fuzzy search media send', action_taken='Semantic search found old rejected media file.', tool_used='session_search', test_result='failed: selected rejected artifact', artifact_path=artifact_paths['old_part2'], success=False, confidence=0.34, times_confirmed=1, minutes_ago=20)

    insert_context_impression(conn, impression_id='impression:demo:artifact', query_text='send me Enoch part 1 and part 2', sections=['VERIFIED ARTIFACTS', 'MUST FOLLOW'], outcome='success', attribution_mode='artifact_registry', minutes_ago=7)
    insert_context_impression(conn, impression_id='impression:demo:approval', query_text='cao', sections=['PENDING APPROVAL'], outcome='success', attribution_mode='new_pending_approval_once', minutes_ago=3)
    insert_context_impression(conn, impression_id='impression:demo:suppress', query_text='cao', sections=['APPROVAL ROUTING'], outcome='success', attribution_mode='suppress_unrelated_repeat', minutes_ago=2)

    store.ingest_reality_event(
        scope_key='demo:enoch',
        session_id='demo_session',
        event_type='user_message',
        subject='dashboard_request',
        payload={'text': 'hoću da mogu da vidim dashboard preko tailscale'},
        source='demo',
        created_at=ts(24),
    )
    store.ingest_reality_event(
        scope_key='demo:enoch',
        session_id='demo_session',
        event_type='tool_result',
        subject='browser_open',
        payload={'result': 'This site can’t be reached. 100.70.190.15 refused to connect. ERR_CONNECTION_REFUSED', 'success': False},
        source='demo',
        confidence=0.9,
        created_at=ts(23),
    )
    store.ingest_reality_event(
        scope_key='demo:enoch',
        session_id='demo_session',
        event_type='user_message',
        subject='auth_feedback',
        payload={'text': 'token neće'},
        source='demo',
        confidence=0.9,
        created_at=ts(22),
    )
    store.ingest_reality_event(
        scope_key='demo:enoch',
        session_id='demo_session',
        event_type='user_message',
        subject='short_reference',
        payload={'text': 'a link?'},
        source='demo',
        confidence=0.82,
        created_at=ts(21),
    )
    store.ingest_reality_event(
        scope_key='demo:enoch',
        session_id='demo_session',
        event_type='tool_result',
        subject='health_check',
        payload={'result': 'dashboard health returned status 200; service active (running)', 'success': True},
        source='demo',
        confidence=0.92,
        created_at=ts(20),
    )
    store.ingest_reality_event(
        scope_key='demo:enoch',
        session_id='demo_session',
        event_type='user_message',
        subject='approval_visibility_feedback',
        payload={'text': 'ne vidim approval'},
        source='demo',
        confidence=0.9,
        created_at=ts(19),
    )
    store.ingest_reality_event(
        scope_key='demo:enoch',
        session_id='demo_session',
        event_type='tool_result',
        subject='brain_mark_artifact',
        payload={'tool_name': 'brain_mark_artifact', 'result': '{"status":"rejected","path":"enoch_part2_old_wrong_cut.mp4"}', 'success': True},
        source='demo',
        confidence=0.9,
        created_at=ts(18),
    )
    store.ingest_reality_event(
        scope_key='demo:enoch',
        session_id='demo_session',
        event_type='tool_result',
        subject='send_message',
        payload={'tool_name': 'send_message', 'result': 'The message was sent successfully to Telegram. message_id=26183', 'success': True},
        source='demo',
        confidence=0.92,
        created_at=ts(1),
    )

    conn.execute(
        """
        INSERT OR REPLACE INTO canonical_recaps (recap_id, session_id, scope_key, task, objective, main_problem, root_cause, ruled_out_causes, what_changed, current_status, next_step, confidence, created_at, updated_at)
        VALUES ('recap:demo:launch', 'demo_session', 'demo:enoch', 'Prepare public Live Brain demo', 'Show post-vector operational memory', 'Vector memory retrieves stale text; agent needs operational truth.', 'No provenance/scope/risk gate in plain semantic search.', 'More embeddings alone; larger context alone.', 'Added artifact registry, work graph, approval queue, context flight recorder.', 'Demo DB ready; pending approval waiting for review.', 'Open Control Room, inspect pending gate, show exact context, approve proposal.', 0.95, ?, ?)
        """,
        (ts(8), ts(1)),
    )

    pending = store.propose_self_evolution(
        scope_key='demo:enoch',
        session_id='demo_session',
        trigger_text='Agent keeps repeating a context bug; propose a deterministic context-routing patch.',
        proposal_type='config_change',
        target_area='context',
        rationale='Demo: convert repeated correction into a gated behavior update, not silent self-modifying code.',
        proposed_action='Add a deterministic context route: use verified_artifacts before fuzzy search for project media, and surface approvals once when new.',
        evidence={'demo': True, 'requires_code_change': False, 'observed_failures': ['stale artifact selected', 'approval invisible until asked']},
        suggested_tests=['artifact resolution smoke', 'approval one-shot smoke', 'context inspector check'],
        auto_apply=False,
    )
    approved = store.propose_self_evolution(
        scope_key='demo:enoch',
        session_id='demo_session',
        trigger_text='Prior demo proposal approved by user.',
        proposal_type='demote_fix_recipe',
        target_area='recipe',
        rationale='Demo: bounded low-risk recipe cleanup from direct failure feedback.',
        proposed_action='Move stale fuzzy-search recipe to needs_review and lower confidence.',
        evidence={'demo': True, 'recipe_ids': ['recipe:demo:fuzzy_artifact_search']},
        suggested_tests=['context eval'],
        auto_apply=False,
    )
    store.decide_self_evolution_proposal(approved['proposal_id'], 'approved', 'Demo: user approved bounded cleanup after review.')

    insert_audit(conn, object_type='verified_artifact', object_id='artifact:demo:part2', action='rejected_old_candidate', reason='Old Part 2 was explicitly rejected after mismatch review.', minutes_ago=19)
    insert_audit(conn, object_type='context_impression', object_id='impression:demo:artifact', action='success', reason='Verified artifact context prevented wrong file send.', minutes_ago=6)
    insert_audit(conn, object_type='self_evolution_proposal', object_id=pending['proposal_id'], action='surfaced_pending_approval', reason='Demo one-shot approval surfaced in next turn.', minutes_ago=3)

    conn.commit()
    store.close()
    return db_path


def main() -> int:
    parser = argparse.ArgumentParser(description='Seed and optionally serve a clean Live Brain Control Room demo.')
    parser.add_argument('--demo-dir', default=str(DEFAULT_DEMO_DIR), help='Directory for synthetic demo DB/artifacts.')
    parser.add_argument('--db', default='', help='Override demo DB path.')
    parser.add_argument('--reset', action='store_true', help='Reset demo DB before seeding.')
    parser.add_argument('--serve', action='store_true', help='Run the Control Room after seeding.')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8777)
    parser.add_argument('--auth-token', default='')
    parser.add_argument('--no-auth', action='store_true')
    args = parser.parse_args()

    demo_dir = Path(args.demo_dir).expanduser().resolve()
    db_path = Path(args.db).expanduser().resolve() if args.db else demo_dir / 'live_brain.db'
    if args.reset and demo_dir.exists() and not args.db:
        shutil.rmtree(demo_dir)
    seeded = seed_demo(db_path, reset=args.reset and bool(args.db))
    print(f'Demo DB seeded: {seeded}')
    print(f'Demo artifacts: {seeded.parent / "artifacts"}')
    print('Narrative: vector memory remembers text; Live Brain maintains operational truth.')
    if args.serve:
        require_auth = bool(args.auth_token) and not args.no_auth
        run_server(str(seeded), args.host, args.port, require_auth=require_auth, token=args.auth_token)
    else:
        print(f'Run dashboard: python3 tools/live_brain_control_room.py --db {seeded} --port 8777')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
