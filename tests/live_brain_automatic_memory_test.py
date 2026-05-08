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

from live_brain.ingest import Ingestor
from live_brain.store import LiveBrainStore


def _load_context_module():
    if 'agent.context_compressor' not in sys.modules:
        agent_mod = types.ModuleType('agent')
        compressor_mod = types.ModuleType('agent.context_compressor')

        class ContextCompressor:  # minimal test stub
            def compress(self, messages, current_tokens=None, focus_topic=None):
                return messages

        compressor_mod.ContextCompressor = ContextCompressor
        sys.modules.setdefault('agent', agent_mod)
        sys.modules['agent.context_compressor'] = compressor_mod
    import live_brain_ctx
    return live_brain_ctx


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



def test_question_like_workflow_prompt_is_not_stored_as_fact() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = LiveBrainStore(str(Path(tmp) / 'brain.db'))
        store.initialize_schema()
        ingestor = Ingestor(store.conn)
        ingestor.ingest_turn(
            session_id='question-session',
            scope_key='agent:main:telegram:dm:test',
            turn_index=1,
            user_text='Kako radimo Suno login workflow i koja mu je oznaka? Odgovori kratko.',
            assistant_text='Koristi postojeći Brave profil na portu 9222.',
            created_at=time.time(),
        )
        rows = store.conn.execute(
            "SELECT source_kind, fact_text FROM facts WHERE fact_text LIKE 'Kako radimo Suno%'"
        ).fetchall()
        store.close()
        assert rows == [], rows


def test_natural_music_work_is_recalled_without_explicit_memory_seed() -> None:
    old_home = os.environ.get('HERMES_HOME')
    with tempfile.TemporaryDirectory() as tmp:
        hermes_home = Path(tmp) / 'hermes'
        db_path = hermes_home / 'live_brain' / 'live_brain.db'
        store = LiveBrainStore(str(db_path))
        store.initialize_schema()
        ingestor = Ingestor(store.conn)
        scope_key = 'agent:main:telegram:dm:test'
        now = time.time()
        turns = [
            ('Koje su najpoznatije Muharem Serbezovski i Ajnur Serbezovski pesme?', 'Pregledao sam opcije.'),
            ('Totalno isti text kao original pravimo cover sa flamenco muzikom i trilerima kao ovde https://youtu.be/9oultCXItik', 'Razumem pravac.'),
            ('Hocu drugi glas, i isti tekst kao pevac sto peva samo da muzika ima flamenco style i pevac koristi tu vrstu trilera', 'To je smer za cover.'),
            ('To je romska pesma', 'Zabeležen je žanr.'),
            ('Probaj esmeralda', 'Nastavljam od Esmeralde.'),
        ]
        for index, (user_text, assistant_text) in enumerate(turns, start=1):
            ingestor.ingest_turn(
                session_id='music-session',
                scope_key=scope_key,
                turn_index=index,
                user_text=user_text,
                assistant_text=assistant_text,
                created_at=now + index,
            )
        stored = _facts(store, '%flamenco%')
        assert any('cover' in fact.lower() and 'triler' in fact.lower() for fact in stored), stored
        os.environ['HERMES_HOME'] = str(hermes_home)
        ctx = _load_context_module()
        context = ctx._load_live_brain_context('Gde smo stali sa pesmama sta sam ti rekao?', 'fresh-session', 'test')
        store.close()
        if old_home is None:
            os.environ.pop('HERMES_HOME', None)
        else:
            os.environ['HERMES_HOME'] = old_home
        lowered = context.lower()
        assert 'probaj esmeralda' in lowered, context
        assert 'flamenco' in lowered and 'triler' in lowered, context
        assert 'serbezovski' in lowered, context
        assert 'upamti' not in lowered and 'ack-seed' not in lowered, context

if __name__ == '__main__':
    test_implicit_workflow_instruction_is_persisted_without_upamti()
    test_successful_tool_call_becomes_action_memory()
    test_question_like_workflow_prompt_is_not_stored_as_fact()
    test_natural_music_work_is_recalled_without_explicit_memory_seed()
    print('live_brain_automatic_memory_test: PASS')
