#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live_brain import LiveBrainProvider


def test_provider_handles_latest_session_switch_and_metadata_hooks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        old_home = os.environ.get('HERMES_HOME')
        os.environ['HERMES_HOME'] = tmp
        try:
            provider = LiveBrainProvider()
            provider.initialize('session:old', hermes_home=tmp, platform='compat', user_id='user:old')
            assert provider._session_id == 'session:old'
            assert provider._scope_key == 'user:old'

            provider.on_session_switch('session:new', parent_session_id='session:old', reset=True, user_id='user:new')
            assert provider._session_id == 'session:new'
            assert provider._scope_key == 'user:new'
            assert provider._turn_count == 0

            provider.on_memory_write(
                'add',
                'memory',
                'ATLANTIS provider contract metadata smoke',
                metadata={'session_id': 'session:new', 'platform': 'compat'},
            )
            provider.on_delegation('contract task', 'contract result', child_session_id='child:1', extra='ignored')
            provider.shutdown()
        finally:
            if old_home is None:
                os.environ.pop('HERMES_HOME', None)
            else:
                os.environ['HERMES_HOME'] = old_home


if __name__ == '__main__':
    test_provider_handles_latest_session_switch_and_metadata_hooks()
    print('live_brain_provider_contract_test: PASS')
