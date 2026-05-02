#!/usr/bin/env python3
from __future__ import annotations

import importlib
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HERMES_AGENT = Path.home() / '.hermes' / 'hermes-agent'
for path in (ROOT, HERMES_AGENT):
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

            first = ctx._pre_llm_call(
                user_message='hoću da mogu da vidim dashboard preko tailscale',
                session_id='reality-e2e-session',
                sender_id='telegram-user-1',
                platform='telegram',
            )
            first_context = (first or {}).get('context', '')
            assert 'LIVE REALITY' in first_context, first_context
            assert 'provide_current_dashboard_or_demo_link' in first_context, first_context

            ctx._post_tool_call(
                tool_name='browser_open',
                args={'url': 'http://100.70.190.15:8765/'},
                result='This site can’t be reached. 100.70.190.15 refused to connect. ERR_CONNECTION_REFUSED',
                session_id='reality-e2e-session',
                tool_call_id='tool-refused',
            )

            ctx._pre_llm_call(
                user_message='token neće',
                session_id='reality-e2e-session',
                sender_id='telegram-user-1',
                platform='telegram',
            )

            final = ctx._pre_llm_call(
                user_message='a link?',
                session_id='reality-e2e-session',
                sender_id='telegram-user-1',
                platform='telegram',
            )
            final_context = (final or {}).get('context', '')
            assert 'LIVE REALITY' in final_context, final_context
            assert 'current active link' in final_context, final_context
            assert 'Service refused connection' in final_context, final_context
            assert 'dashboard_auth=warn' in final_context, final_context
            assert "do not ask generic 'which link?'" in final_context, final_context

            ctx._post_tool_call(
                tool_name='health_check',
                args={'url': 'http://100.70.190.15:8765/api/health'},
                result='dashboard health returned status 200; service active (running)',
                session_id='reality-e2e-session',
                tool_call_id='tool-health',
            )

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                scope_key = 'agent:main:telegram:dm:telegram-user-1'
                counts = {
                    'events': conn.execute('SELECT COUNT(*) c FROM reality_events WHERE scope_key=?', (scope_key,)).fetchone()['c'],
                    'states': conn.execute('SELECT COUNT(*) c FROM reality_state WHERE scope_key=?', (scope_key,)).fetchone()['c'],
                    'loops': conn.execute('SELECT COUNT(*) c FROM open_loops WHERE scope_key=?', (scope_key,)).fetchone()['c'],
                    'constraints': conn.execute('SELECT COUNT(*) c FROM action_constraints WHERE scope_key=?', (scope_key,)).fetchone()['c'],
                    'impressions': conn.execute('SELECT COUNT(*) c FROM context_impressions WHERE scope_key=?', (scope_key,)).fetchone()['c'],
                }
                assert counts['events'] >= 5, counts
                assert counts['states'] >= 3, counts
                assert counts['loops'] >= 1, counts
                assert counts['constraints'] >= 1, counts
                assert counts['impressions'] >= 2, counts
                loop = conn.execute(
                    "SELECT status FROM open_loops WHERE scope_key=? AND title='Service refused connection'",
                    (scope_key,),
                ).fetchone()
                assert loop and loop['status'] == 'resolved', dict(loop) if loop else None
            finally:
                conn.close()
        finally:
            if old_home is None:
                os.environ.pop('HERMES_HOME', None)
            else:
                os.environ['HERMES_HOME'] = old_home
    print('live_brain_reality_e2e: PASS')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
