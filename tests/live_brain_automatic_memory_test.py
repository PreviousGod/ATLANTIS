#!/usr/bin/env python3
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live_brain.ingest import Ingestor
from live_brain.store import LiveBrainStore


def _facts(store: LiveBrainStore, like: str) -> list[str]:
    rows = store.conn.execute(
        "SELECT fact_text FROM facts WHERE status='active' AND fact_text LIKE ? ORDER BY valid_from DESC",
        (like,),
    ).fetchall()
    return [str(row[0]) for row in rows]


def test_implicit_workflow_instruction_is_persisted_without_upamti() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = LiveBrainStore(str(Path(tmp) / 'brain.db'))
        store.initialize_schema()
        ingestor = Ingestor(store.conn)
        ingestor.ingest_turn(
            session_id='natural-session',
            scope_key='agent:main:telegram:dm:test',
            turn_index=1,
            user_text='Za Suno login koristimo postojeći Brave profil preko remote debugging porta 9222 i njegove cookies.',
            assistant_text='Razumem, nastavljam tim workflow-om.',
            created_at=time.time(),
        )
        facts = _facts(store, '%Suno login%')
        store.close()
        assert any('9222' in fact and 'cookies' in fact for fact in facts), facts


def test_successful_tool_call_becomes_action_memory() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = LiveBrainStore(str(Path(tmp) / 'brain.db'))
        store.initialize_schema()
        ingestor = Ingestor(store.conn)
        ingestor.store_tool_result_event(
            'browser_open',
            {'url': 'https://suno.com/create', 'port': 9222},
            {'success': True, 'url': 'https://suno.com/create', 'title': 'Suno Create'},
            session_id='tool-session',
            scope_key='agent:main:telegram:dm:test',
            user_text='Proveri Suno login koristeći postojeći Brave na remote debugging portu 9222.',
            created_at=time.time(),
        )
        facts = _facts(store, '%browser_open%')
        store.close()
        assert any('Suno' in fact and '9222' in fact and 'browser_open' in fact for fact in facts), facts


if __name__ == '__main__':
    test_implicit_workflow_instruction_is_persisted_without_upamti()
    test_successful_tool_call_becomes_action_memory()
    print('live_brain_automatic_memory_test: PASS')
