#!/usr/bin/env python3
from __future__ import annotations

import importlib
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HERMES_AGENT = Path.home() / '.hermes' / 'hermes-agent'
for path in (HERMES_AGENT, ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from live_brain.store import LiveBrainStore


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        hermes_home = Path(tmp) / '.hermes'
        db_path = hermes_home / 'live_brain' / 'live_brain.db'
        store = LiveBrainStore(str(db_path))
        store.initialize_schema()
        store.close()

        old_home = os.environ.get('HERMES_HOME')
        os.environ['HERMES_HOME'] = str(hermes_home)
        try:
            ctx = importlib.import_module('live_brain_ctx')
            session_id = 'epistemic-e2e-session'
            sender_id = 'telegram-user-epistemic'
            question = 'Možeš li sam da trejduješ funded account?'
            first = ctx._pre_llm_call(
                user_message=question,
                session_id=session_id,
                sender_id=sender_id,
                platform='telegram',
            )
            context = (first or {}).get('context', '')
            assert 'EPISTEMIC STATUS' in context, context
            assert 'Research required before final answer' in context, context
            assert 'web_search' in context, context
            assert 'brain_epistemic(action=record_fact)' in context, context

            web_result = {
                'success': True,
                'data': {
                    'web': [
                        {
                            'title': 'FTMO Trading Objectives',
                            'url': 'https://ftmo.com/en/trading-objectives/',
                            'description': 'Official FTMO page describing daily and total loss limits.',
                        }
                    ]
                },
            }
            ctx._post_tool_call(
                tool_name='web_search',
                args={'query': question},
                result=json.dumps(web_result),
                session_id=session_id,
                tool_call_id='web-search-1',
            )

            ctx._post_llm_call(
                user_message=question,
                assistant_response='FTMO funded-style trading requires enforcing daily and total loss limits before any autonomous execution. Sources: https://ftmo.com/en/trading-objectives/',
                session_id=session_id,
                platform='telegram',
            )

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                scope = 'agent:main:telegram:dm:telegram-user-epistemic'
                sources = conn.execute('SELECT COUNT(*) c FROM epistemic_web_sources WHERE scope_key=?', (scope,)).fetchone()['c']
                jobs = conn.execute('SELECT COUNT(*) c FROM epistemic_research_jobs WHERE scope_key=?', (scope,)).fetchone()['c']
                facts = conn.execute('SELECT COUNT(*) c FROM epistemic_learned_facts WHERE scope_key=?', (scope,)).fetchone()['c']
                assert jobs >= 1, jobs
                assert sources >= 1, sources
                assert facts >= 1, facts
            finally:
                conn.close()
        finally:
            if old_home is None:
                os.environ.pop('HERMES_HOME', None)
            else:
                os.environ['HERMES_HOME'] = old_home
    print('live_brain_epistemic_e2e: PASS')
    return 0


def test_autonomous_pre_llm_research_and_post_llm_learning() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        hermes_home = Path(tmp) / '.hermes'
        db_path = hermes_home / 'live_brain' / 'live_brain.db'
        store = LiveBrainStore(str(db_path))
        store.initialize_schema()
        store.close()

        old_home = os.environ.get('HERMES_HOME')
        os.environ['HERMES_HOME'] = str(hermes_home)
        try:
            ctx = importlib.import_module('live_brain_ctx')
            manager_cls = ctx._load_epistemic_manager_class()
            epistemic_mod = importlib.import_module(manager_cls.__module__)
            original_discover = epistemic_mod.discover_sources

            def fake_discover(question, queries=None, *, limit=8, max_queries=4, timeout=6.0):
                assert 'CME' in question or 'cme' in question.lower()
                return [
                    (
                        'https://www.cmegroup.com/trading/price-limits.html',
                        'Price Limits - CME Group',
                        'Official CME Group page for futures price limits.',
                        'official',
                        0.9,
                    )
                ]

            epistemic_mod.discover_sources = fake_discover
            try:
                session_id = 'epistemic-auto-e2e-session'
                sender_id = 'telegram-user-epistemic-auto'
                question = 'Koja su najnovija CME pravila za NQ price limits?'
                first = ctx._pre_llm_call(
                    user_message=question,
                    session_id=session_id,
                    sender_id=sender_id,
                    platform='telegram',
                )
                context = (first or {}).get('context', '')
                assert 'AUTONOMOUS WEB RESEARCH' in context, context
                assert 'cmegroup.com' in context, context
                assert 'do not answer from stale memory' in context, context
                blocked = ctx._pre_tool_call(tool_name='session_search', args={'query': question}, task_id=session_id)
                assert blocked and blocked.get('action') == 'block', blocked

                ctx._post_llm_call(
                    user_message=question,
                    assistant_response='CME price limits must be checked against the current CME Group product page before trading NQ. Source: https://www.cmegroup.com/trading/price-limits.html',
                    session_id=session_id,
                    platform='telegram',
                )

                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                try:
                    scope = 'agent:main:telegram:dm:telegram-user-epistemic-auto'
                    source = conn.execute('SELECT authority, url FROM epistemic_web_sources WHERE scope_key=?', (scope,)).fetchone()
                    fact = conn.execute('SELECT authority, fact_text FROM epistemic_learned_facts WHERE scope_key=?', (scope,)).fetchone()
                    assert source and source['authority'] == 'official', dict(source) if source else None
                    assert fact and fact['authority'] == 'official', dict(fact) if fact else None
                finally:
                    conn.close()
            finally:
                epistemic_mod.discover_sources = original_discover
        finally:
            if old_home is None:
                os.environ.pop('HERMES_HOME', None)
            else:
                os.environ['HERMES_HOME'] = old_home


if __name__ == '__main__':
    rc = main()
    if rc == 0:
        test_autonomous_pre_llm_research_and_post_llm_learning()
    raise SystemExit(rc)
