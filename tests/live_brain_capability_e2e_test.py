from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path


def _load_capability_module():
    telethon = types.ModuleType('telethon')
    telethon.TelegramClient = object
    tl = types.ModuleType('telethon.tl')
    tl_types = types.ModuleType('telethon.tl.types')

    class MessageEntityTextUrl:
        pass

    tl_types.MessageEntityTextUrl = MessageEntityTextUrl
    sys.modules.setdefault('telethon', telethon)
    sys.modules.setdefault('telethon.tl', tl)
    sys.modules.setdefault('telethon.tl.types', tl_types)

    module_path = Path(__file__).resolve().parents[1] / 'tools' / 'live_brain_capability_e2e.py'
    spec = importlib.util.spec_from_file_location('live_brain_capability_e2e_under_test', module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_capability_e2e_includes_memory_inference_step() -> None:
    module = _load_capability_module()
    steps = module.build_steps('run-abcd1234', 'codename-test', include_research=False)
    names = [step.name for step in steps]

    assert names.index('seed_inference_facts') < names.index('memory_inference_conclusion')
    inference = next(step for step in steps if step.name == 'memory_inference_conclusion')
    assert inference.capability == 'inference_gain'
    assert 'svc-abcd1234' in inference.message
    assert 'adapter-abcd1234' in inference.expect_all
    assert 'unknown' in inference.forbid_any
    assert 'adapter-abcd1234' in inference.expect_context_any


def test_capability_e2e_self_review_requires_inference_verdict() -> None:
    module = _load_capability_module()
    steps = module.build_steps('run-abcd1234', 'codename-test', include_research=True)
    review = next(step for step in steps if step.name == 'agent_self_review_verdict')

    assert 'inference zaključak iz memorije' in review.message
    assert 'verdict: pass' in review.expect_any


if __name__ == '__main__':
    test_capability_e2e_includes_memory_inference_step()
    test_capability_e2e_self_review_requires_inference_verdict()
    print('live_brain_capability_e2e_test: PASS')
