#!/usr/bin/env python3
from __future__ import annotations

import tempfile
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live_brain.ingest import Ingestor
from live_brain.store import LiveBrainStore


def test_reality_reducers_build_situational_awareness() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / 'brain.db')
        store = LiveBrainStore(db)
        store.initialize_schema()
        scope = 'agent:main:telegram:dm:test'
        store.ingest_reality_event(
            scope_key=scope,
            session_id='s',
            event_type='user_message',
            subject='dashboard_request',
            payload={'text': 'hoću da mogu da vidim dashboard preko tailscale'},
        )
        store.ingest_reality_event(
            scope_key=scope,
            session_id='s',
            event_type='tool_result',
            subject='browser_open',
            payload={'result': 'This site can’t be reached. 100.70.190.15 refused to connect. ERR_CONNECTION_REFUSED', 'success': False},
        )
        store.ingest_reality_event(
            scope_key=scope,
            session_id='s',
            event_type='user_message',
            subject='auth_feedback',
            payload={'text': 'token neće'},
        )
        brief = store.compile_reality_brief(scope, 'a link?')
        assert 'LIVE REALITY' in brief
        assert 'current active link' in brief or 'Current objective' in brief
        assert 'Service refused connection' in brief
        assert 'dashboard_auth=warn' in brief
        debug = store.debug_reality(scope, 'a link?')
        assert 'request_link' in debug['signals']
        assert debug['open_loops']
        assert debug['action_constraints']
        store.ingest_reality_event(
            scope_key=scope,
            session_id='s',
            event_type='tool_result',
            subject='health_check',
            payload={'result': 'dashboard health returned status 200; service active (running)', 'success': True},
        )
        loop_id = debug['open_loops'][0]['loop_id']
        row = store.conn.execute('SELECT status FROM open_loops WHERE loop_id=?', (loop_id,)).fetchone()
        assert row['status'] == 'resolved'
        store.close()


def test_action_gate_blocks_private_db_and_allows_synthetic_demo() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / 'brain.db')
        store = LiveBrainStore(db)
        store.initialize_schema()
        scope = 'agent:main:telegram:dm:test'
        denied = store.action_gate(scope, 'send_private_db', {'path': '/home/user/.hermes/live_brain/live_brain.db'})
        assert denied['decision'] == 'deny'
        db_schema = store.action_gate(scope, 'db_schema', {'migration': 'ALTER TABLE facts ADD COLUMN x TEXT'})
        assert db_schema['decision'] == 'needs_approval', db_schema
        schema_alias = store.action_gate(scope, 'schema', {'migration': 'ALTER TABLE facts ADD COLUMN y TEXT'})
        assert schema_alias['decision'] == 'needs_approval', schema_alias
        assert schema_alias['action_type'] == 'db_schema', schema_alias
        allowed = store.action_gate(scope, 'media_send', {'path': '/tmp/live_brain_control_room_demo/live_brain_control_room_teaser_voiceover.mp4', 'synthetic_public': True})
        assert allowed['decision'] == 'allow', allowed
        store.close()



def test_reality_replaces_stale_demo_with_youtube_and_trading_context() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / 'brain.db')
        store = LiveBrainStore(db)
        store.initialize_schema()
        scope = 'agent:main:telegram:dm:test'
        store.ingest_reality_event(
            scope_key=scope,
            session_id='s',
            event_type='user_message',
            subject='demo_request',
            payload={'text': 'sad nam demo treba'},
        )
        assert 'prepare_or_send_public_demo_package' in store.compile_reality_brief(scope, 'demo')
        store.ingest_reality_event(
            scope_key=scope,
            session_id='s',
            event_type='user_message',
            subject='youtube_revenue_problem',
            payload={'text': 'Majok, nece to pomoci, treba nam alternativa kako da pravimo pare. Youtube shorts buraz'},
        )
        brief = store.compile_reality_brief(scope, 'šta dalje?')
        assert 'find_youtube_shorts_monetization_path' in brief, brief
        assert 'Prepare public demo package' not in brief, brief
        store.ingest_reality_event(
            scope_key=scope,
            session_id='s',
            event_type='user_message',
            subject='funded_account_request',
            payload={'text': 'Ili sta kazes da ti kupim funded account i da trejdujes, hoces li biti sposoban za to?'},
        )
        trading_brief = store.compile_reality_brief(scope, 'funded account?')
        assert 'evaluate_financial_trading_request_safely' in trading_brief, trading_brief
        assert 'claiming_agent_can_trade_funded_or_live_accounts' in trading_brief, trading_brief
        gate = store.action_gate(scope, 'financial_trade_execution', {'account_type': 'funded'})
        assert gate['decision'] == 'deny', gate
        store.close()



def test_tool_result_transcripts_do_not_create_user_intent() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / 'brain.db')
        store = LiveBrainStore(db)
        store.initialize_schema()
        scope = 'agent:main:telegram:dm:test'
        store.ingest_reality_event(
            scope_key=scope,
            session_id='s',
            event_type='tool_result',
            subject='terminal',
            payload={
                'args': {'command': 'sqlite3 live_brain.db "SELECT title FROM episodes"'},
                'result': 'TEST Autonomous Trading Research funded account | Youtube shorts monetization | dashboard link',
                'success': True,
                'user_message': 'Sta radis kad te pitam nesto sto ne znas',
            },
        )
        assert store.compile_reality_brief(scope, 'Sta radis kad te pitam nesto sto ne znas') == ''
        assert not store.conn.execute("SELECT COUNT(*) FROM reality_state WHERE scope_key=?", (scope,)).fetchone()[0]
        store.close()


def test_reality_brief_filters_irrelevant_stale_state() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / 'brain.db')
        store = LiveBrainStore(db)
        store.initialize_schema()
        scope = 'agent:main:telegram:dm:test'
        store.ingest_reality_event(
            scope_key=scope,
            session_id='s',
            event_type='user_message',
            subject='youtube_revenue_problem',
            payload={'text': 'Youtube shorts buraz, kako da pravimo pare'},
        )
        store.ingest_reality_event(
            scope_key=scope,
            session_id='s',
            event_type='user_message',
            subject='funded_account_request',
            payload={'text': 'Treba mi funded account trading research'},
        )
        unrelated = store.compile_reality_brief(scope, 'Sta radis kad te pitam nesto sto ne znas')
        assert 'evaluate_financial_trading_request_safely' not in unrelated, unrelated
        assert 'YouTube Shorts' not in unrelated, unrelated
        assert 'funded' not in unrelated.lower(), unrelated
        relevant = store.compile_reality_brief(scope, 'funded account?')
        assert 'evaluate_financial_trading_request_safely' in relevant, relevant
        assert 'financial_trade_execution=deny' in relevant, relevant
        store.close()


def test_noisy_meta_episodes_are_archived_and_not_recreated() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / 'brain.db')
        store = LiveBrainStore(db)
        store.initialize_schema()
        scope = 'agent:main:telegram:dm:test'
        now = 10.0
        store.conn.execute(
            "INSERT INTO episodes (episode_id, kind, title, status, opened_at, updated_at, current_summary, priority_score, recency_score, scope_tags_json) VALUES (?, 'general', ?, 'active', ?, ?, ?, 0.5, 1.0, '{}')",
            ('episode:review', 'Review the conversation above and consider saving or updating a skill if appropriate.', now, now, 'SCOPE: memory | FIX: Skill ažuriran sa novim root cause'),
        )
        store.conn.execute(
            "INSERT INTO episodes (episode_id, kind, title, status, opened_at, updated_at, current_summary, priority_score, recency_score, scope_tags_json) VALUES (?, 'general', ?, 'active', ?, ?, ?, 0.5, 1.0, '{}')",
            ('episode:ack', 'Cekaj', now, now, 'TASK: Cekaj'),
        )
        store.conn.commit()
        stats = store.cleanup_noisy_memory()
        assert stats['episodes'] >= 2, stats
        statuses = dict(store.conn.execute('SELECT episode_id, status FROM episodes').fetchall())
        assert statuses['episode:review'] == 'archived', statuses
        assert statuses['episode:ack'] == 'archived', statuses

        ingestor = Ingestor(store.conn)
        ingestor.ingest_turn('s', scope, 1, 'Cekaj', 'Naravno.', 20.0)
        ingestor.ingest_turn('s', scope, 2, 'Sta radis kad te pitam nesto sto ne znas', 'OK, sad je čisto. Odgovaram direktno bez autonomus research-a.', 21.0)
        active_titles = [row[0] for row in store.conn.execute("SELECT title FROM episodes WHERE status IN ('active','dormant')").fetchall()]
        assert not any(title == 'Cekaj' for title in active_titles), active_titles
        assert not any('ne znas' in title.lower() for title in active_titles), active_titles
        store.close()

def test_assistant_response_does_not_overwrite_active_project() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / 'brain.db')
        store = LiveBrainStore(db)
        store.initialize_schema()
        scope = 'agent:main:telegram:dm:test'
        store.ingest_reality_event(
            scope_key=scope,
            session_id='s',
            event_type='user_message',
            subject='enoch_context',
            payload={'text': 'Youtube shorts buraz za Enoch'},
        )
        store.ingest_reality_event(
            scope_key=scope,
            session_id='s',
            event_type='assistant_response',
            subject='assistant_response',
            payload={'text': 'Youtube shorts buraz za Enoch', 'assistant_response': 'Live Reality kaže public demo package za live brain dashboard i tailscale.'},
        )
        row = store.conn.execute(
            "SELECT value_json FROM reality_state WHERE scope_key=? AND state_key='active_project'",
            (scope,),
        ).fetchone()
        assert row and 'enoch' in row['value_json'], row['value_json'] if row else None
        assert 'live_brain' not in row['value_json'], row['value_json'] if row else None
        store.close()


if __name__ == '__main__':
    test_reality_reducers_build_situational_awareness()
    test_action_gate_blocks_private_db_and_allows_synthetic_demo()
    test_reality_replaces_stale_demo_with_youtube_and_trading_context()
    test_tool_result_transcripts_do_not_create_user_intent()
    test_reality_brief_filters_irrelevant_stale_state()
    test_noisy_meta_episodes_are_archived_and_not_recreated()
    test_assistant_response_does_not_overwrite_active_project()
    print('live_brain_reality_test: PASS')
