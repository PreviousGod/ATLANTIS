#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live_brain.ingest import Ingestor
from live_brain.store import LiveBrainStore


def test_explicit_multi_fact_memory_survives_context_retrieval() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / 'home'
        live_dir = home / 'live_brain'
        live_dir.mkdir(parents=True)
        store = LiveBrainStore(str(live_dir / 'live_brain.db'))
        store.initialize_schema()
        scope_key = 'agent:main:telegram:dm:unit'
        user_text = (
            'LIVE_BRAIN_CAPABILITY_E2E inference-seed run-unit1234: Zapamti ove činjenice za inferencu: '
            '1) service svc-unit1234 zavisi od adaptera adapter-unit1234. '
            '2) adapter adapter-unit1234 je trenutno BLOCKED jer je feature flag flag-unit1234 OFF. '
            'Pravilo za ovaj test: ako service zavisi od blocked adaptera, service je BLOCKED za deploy. '
            'Ne izvodi zaključak sada. Odgovori samo ACK-INFER.'
        )
        Ingestor(store.conn).ingest_turn(
            session_id='session:test',
            scope_key=scope_key,
            turn_index=1,
            user_text=user_text,
            assistant_text='ACK-INFER',
            created_at=time.time(),
        )
        rows = store.conn.execute(
            "SELECT fact_text FROM facts WHERE fact_type='explicit_memory_fact' ORDER BY fact_text"
        ).fetchall()
        fact_text = '\n'.join(row['fact_text'] for row in rows)
        assert 'svc-unit1234 zavisi od adaptera adapter-unit1234' in fact_text
        assert 'adapter-unit1234 je trenutno BLOCKED' in fact_text
        assert 'flag-unit1234 OFF' in fact_text
        assert 'service je BLOCKED za deploy' in fact_text
        store.close()

        old_home = os.environ.get('HERMES_HOME')
        os.environ['HERMES_HOME'] = str(home)
        try:
            from live_brain_ctx import _load_live_brain_context
            context = _load_live_brain_context(
                'LIVE_BRAIN_CAPABILITY_E2E inference-check run-unit1234: da li service svc-unit1234 sme u deploy?',
                '',
                'unit',
            )
        finally:
            if old_home is None:
                os.environ.pop('HERMES_HOME', None)
            else:
                os.environ['HERMES_HOME'] = old_home

        assert 'adapter-unit1234' in context
        assert 'flag-unit1234' in context
        assert 'BLOCKED' in context


if __name__ == '__main__':
    test_explicit_multi_fact_memory_survives_context_retrieval()
    print('live_brain_ingest_memory_facts_test: PASS')
