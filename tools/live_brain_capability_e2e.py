#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import random
import re
import string
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from telethon import TelegramClient
from telethon.tl.types import MessageEntityTextUrl

HOME = Path.home()
HERMES_HOME = Path(os.environ.get('HERMES_HOME', str(HOME / '.hermes')))
DEFAULT_DB = HERMES_HOME / 'live_brain' / 'live_brain.db'
DEFAULT_SESSIONS = HERMES_HOME / 'sessions'


@dataclass
class CapabilityStep:
    name: str
    message: str
    capability: str
    expect_any: list[str] = field(default_factory=list)
    expect_all: list[str] = field(default_factory=list)
    forbid_any: list[str] = field(default_factory=list)
    expect_tool_any: list[str] = field(default_factory=list)
    expect_context_any: list[str] = field(default_factory=list)
    reset_before: bool = False
    max_wait_s: int = 180
    should_pass: bool = True


def _load_telegram_defaults() -> dict[str, Any]:
    defaults: dict[str, Any] = {
        'api_id': os.environ.get('TELEGRAM_API_ID'),
        'api_hash': os.environ.get('TELEGRAM_API_HASH'),
        'session_path': os.environ.get('TELEGRAM_SESSION_PATH'),
        'bot_username': os.environ.get('TELEGRAM_BOT_USERNAME'),
    }
    helper = HOME / 'telegram_live_brain_cli.py'
    if helper.exists():
        spec = importlib.util.spec_from_file_location('telegram_live_brain_cli_defaults', helper)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        defaults['api_id'] = defaults['api_id'] or getattr(mod, 'API_ID', None)
        defaults['api_hash'] = defaults['api_hash'] or getattr(mod, 'API_HASH', None)
        defaults['session_path'] = defaults['session_path'] or getattr(mod, 'SESSION_PATH', None)
        defaults['bot_username'] = defaults['bot_username'] or getattr(mod, 'BOT_USERNAME', None)
    missing = [key for key, value in defaults.items() if not value]
    if missing:
        raise SystemExit(f'Missing Telegram config keys: {missing}')
    defaults['api_id'] = int(defaults['api_id'])
    return defaults


def random_token(prefix: str = 'lbcap') -> str:
    suffix = ''.join(random.choice(string.ascii_lowercase + string.digits) for _ in range(8))
    return f'{prefix}-{suffix}'


def normalize(text: str) -> str:
    return (text or '').lower()


def is_progress_message(text: str) -> bool:
    stripped = (text or '').strip()
    if not stripped:
        return True
    progress_prefixes = ('🔍', '🔎', '⚙', '⚙️', '🛠', '🛠️', '🔧', '📖', '🧠', '💾')
    if stripped.startswith(progress_prefixes):
        head = stripped.splitlines()[0]
        if re.search(r'[A-Za-z_]\w*\s*\(', head) or head.endswith(':') or '{' in stripped:
            return True
    lowered = stripped.lower()
    progress_phrases = (
        'let me first check',
        'let me check',
        "i'll check",
        'i will check',
        'checking the',
        'prvo ću proveriti',
        'prvo cu proveriti',
    )
    if lowered.startswith(progress_phrases):
        return True
    if re.match(r'^[^\w\s]{1,4}\s*[A-Za-z_]\w*\s*\(', stripped):
        return True
    return False


def latest_session_files(since: float, sessions_dir: Path = DEFAULT_SESSIONS) -> list[Path]:
    files: list[Path] = []
    if not sessions_dir.exists():
        return files
    for path in sessions_dir.glob('*'):
        if path.suffix not in {'.json', '.jsonl'}:
            continue
        try:
            if path.stat().st_mtime >= since - 10:
                files.append(path)
        except OSError:
            continue
    return sorted(files, key=lambda item: item.stat().st_mtime, reverse=True)


def file_contains(path: Path, token: str) -> bool:
    try:
        return token.lower() in path.read_text(errors='replace').lower()
    except Exception:
        return False


def context_for_query(db_path: Path, query: str) -> dict[str, Any]:
    if not db_path.exists():
        return {}
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            'SELECT * FROM context_impressions WHERE query_text=? ORDER BY created_at DESC LIMIT 1',
            (query,),
        ).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def text_with_entity_urls(message: Any) -> str:
    text = getattr(message, 'message', None) or ''
    urls: list[str] = []
    for entity in getattr(message, 'entities', None) or []:
        if isinstance(entity, MessageEntityTextUrl) and getattr(entity, 'url', ''):
            urls.append(str(entity.url))
    if urls:
        return text + '\n' + '\n'.join(urls)
    return text


async def send_and_wait(client: TelegramClient, entity: Any, message: str, timeout_s: int) -> tuple[str, int, int]:
    before = await client.get_messages(entity, limit=1)
    before_top_id = before[0].id if before else 0
    sent = await client.send_message(entity, message)
    last_seen_id = 0
    stable_polls = 0
    last_message = ''
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        await asyncio.sleep(2)
        messages = await client.get_messages(entity, limit=30)
        bot_messages = [item for item in messages if item.id > before_top_id and item.sender_id == entity.id]
        bot_replies = [item for item in bot_messages if (item.message or '').strip()]
        non_progress = [item for item in bot_replies if not is_progress_message(item.message or '')]
        if not non_progress:
            continue
        latest = max(non_progress, key=lambda item: item.id)
        latest_text = text_with_entity_urls(latest)
        if latest.id == last_seen_id and latest_text == last_message:
            stable_polls += 1
        else:
            last_seen_id = latest.id
            last_message = latest_text
            stable_polls = 0
        if stable_polls >= 2:
            return last_message, sent.id, latest.id
    return last_message, sent.id, last_seen_id


async def reset_chat(client: TelegramClient, entity: Any) -> None:
    await send_and_wait(client, entity, '/new', 75)
    await asyncio.sleep(1)


def build_steps(run_id: str, codename: str, *, include_research: bool) -> list[CapabilityStep]:
    inference_suffix = run_id.replace('run-', '').replace('_', '-').lower()
    inference_service = f'svc-{inference_suffix}'
    inference_adapter = f'adapter-{inference_suffix}'
    inference_flag = f'flag-{inference_suffix}'
    steps = [
        CapabilityStep(
            name='baseline_unknown_before_memory',
            capability='baseline_control',
            reset_before=True,
            message=(
                f'LIVE_BRAIN_CAPABILITY_E2E baseline {run_id}: koji je tajni codename za ovaj run? '
                'Tajni codename NIJE deo run_id-ja, nije suffix/hash, i ne smeš ga izvoditi iz teksta. '
                'Ako nije eksplicitno zapamćen u Live Brain memoriji, odgovori tačno UNKNOWN bez objašnjenja.'
            ),
            expect_all=['unknown'],
            forbid_any=[codename, 'codename-', 'codename:', 'poznati codename', 'known codename', 'verovatno', 'vjerovatno', 'iz run', 'suffix', 'hash'],
            max_wait_s=120,
        ),
        CapabilityStep(
            name='seed_binding_and_active_task',
            capability='memory_write',
            reset_before=False,
            message=(
                f'LIVE_BRAIN_CAPABILITY_E2E seed {run_id}: Zapamti za ovaj test: tajni codename je {codename}. '
                'Binding: nikad ne menjaj taj codename za ovaj run. Aktivni zadatak: dovršiti Live Brain plugin bez kvarenja postojećeg ponašanja. '
                'Sledeći najbolji korak je: pokreni targeted smoke i audit hygiene testove pre gateway restarta. Odgovori samo ACK-SEED.'
            ),
            expect_any=['ack-seed', 'ack'],
            max_wait_s=120,
        ),
        CapabilityStep(
            name='continuity_recall_after_new_session',
            capability='continuity_gain',
            reset_before=True,
            message=f'LIVE_BRAIN_CAPABILITY_E2E recall {run_id}: koji je tajni codename? Odgovori samo codename.',
            expect_all=[codename],
            forbid_any=['unknown', 'ne znam', 'nemam memoriju'],
            max_wait_s=150,
        ),
        CapabilityStep(
            name='seed_causal_correction',
            capability='belief_lifecycle_write',
            reset_before=False,
            message=(
                f'LIVE_BRAIN_CAPABILITY_E2E correction {run_id}: korekcija za dijagnostiku: '
                'uzrok redis cache je ruled_out; validiran uzrok je sqlite busy timeout. '
                'Next action: povećaj busy_timeout i proveri WAL checkpoint. Odgovori samo ACK-CAUSE.'
            ),
            expect_any=['ack-cause', 'ack'],
            max_wait_s=120,
        ),
        CapabilityStep(
            name='causal_recall_and_next_action',
            capability='causal_gain',
            reset_before=True,
            message=(
                f'LIVE_BRAIN_CAPABILITY_E2E continue {run_id}: nastavi ono od malopre. '
                'Koji uzrok je ruled_out, koji je validiran, i koji je sledeći korak? Samo navedi odgovor; ne pokreći alate, komande ili testove.'
            ),
            expect_all=['redis', 'sqlite', 'busy'],
            expect_any=['wal', 'checkpoint', 'busy_timeout', 'timeout'],
            forbid_any=['ne znam', 'unknown', 'nemam memoriju', 'izvršiću sada', 'izvrsicu sada', 'pokrećem sada', 'pokrecem sada', 'let me run', 'i will run'],
            expect_context_any=['OPEN BUG', 'NEXT REQUIRED ACTION', 'sqlite'],
            max_wait_s=180,
        ),
        CapabilityStep(
            name='seed_inference_facts',
            capability='inference_memory_write',
            reset_before=False,
            message=(
                f'LIVE_BRAIN_CAPABILITY_E2E inference-seed {run_id}: Zapamti ove činjenice za inferencu: '
                f'1) service {inference_service} zavisi od adaptera {inference_adapter}. '
                f'2) adapter {inference_adapter} je trenutno BLOCKED jer je feature flag {inference_flag} OFF. '
                'Pravilo za ovaj test: ako service zavisi od blocked adaptera, service je BLOCKED za deploy. '
                'Ne izvodi zaključak sada. Odgovori samo ACK-INFER.'
            ),
            expect_any=['ack-infer', 'ack'],
            max_wait_s=120,
        ),
        CapabilityStep(
            name='memory_inference_conclusion',
            capability='inference_gain',
            reset_before=True,
            message=(
                f'LIVE_BRAIN_CAPABILITY_E2E inference-check {run_id}: Na osnovu Live Brain memorije za ovaj run, '
                f'da li service {inference_service} sme u deploy? '
                'Odgovori tačno u formatu CONCLUSION: BLOCKED ili CONCLUSION: CLEAR, pa navedi jedan kratak razlog. '
                'Moraš koristiti zapamćene činjenice; ako ih nema, reci UNKNOWN.'
            ),
            expect_all=['conclusion', 'blocked', inference_adapter],
            expect_any=[inference_flag, 'flag', 'off', 'zavisi', 'depends'],
            forbid_any=['unknown', 'ne znam', 'nemam memoriju', 'clear', 'sme u deploy', 'može u deploy', 'moze u deploy'],
            expect_context_any=[inference_service, inference_adapter, 'BLOCKED'],
            max_wait_s=180,
        ),
        CapabilityStep(
            name='chitchat_no_memory_dump',
            capability='noise_guard',
            reset_before=True,
            message='cao',
            forbid_any=['LIVE BRAIN', 'PROVEN FIX', 'context_impressions', 'memory_events', 'object_revisions', 'sqlite busy timeout'],
            max_wait_s=90,
        ),
    ]
    if include_research:
        steps.append(
            CapabilityStep(
                name='epistemic_current_high_stakes',
                capability='epistemic_autonomy',
                reset_before=True,
                message=(
                    f'LIVE_BRAIN_CAPABILITY_E2E research {run_id}: Koja su najnovija CME pravila za NQ price limits? '
                    'Ako nemaš sveže authoritative izvore, koristi brain_epistemic/web_search. Odgovori kratko i OBAVEZNO navedi raw source URL stringove koji sadrže cmegroup.com; ako extraction ne radi, ne izmišljaj numeric values nego navedi URL-ove. '
                    'Odgovori ISKLJUČIVO o CME NQ price limits; ne pominji LIVE_BRAIN_CAPABILITY_E2E, run id, codename, active task, prior diagnostics, niti sledeći korak iz memorije.'
                ),
                expect_all=['cmegroup.com'],
                expect_any=['https://', 'http://', 'www.cmegroup.com'],
                forbid_any=['ne znam', 'nemam pristup', 'bez izvora', codename, 'codename-', 'LIVE_BRAIN_CAPABILITY_E2E', run_id, 'active task', '**Task**', 'prior diagnostic', 'sledeći korak', 'next step'],
                expect_tool_any=['brain_epistemic', 'web_search'],
                max_wait_s=360,
            )
        )
    steps.append(
        CapabilityStep(
            name='agent_self_review_verdict',
            capability='self_review',
            reset_before=True,
            message=(
                f'LIVE_BRAIN_CAPABILITY_E2E self-review {run_id}: Oceni samo da li Live Brain plugin daje agentu moći u OVOM E2E run-u. '
                'Ne ocenjuj da li je sintetički sledeći korak izvršen; taj korak je namerno bio recall-only, ne execution task. '
                'Ne koristi istoriju iz drugih projekata ili stare unrelated production blocker-e; proceni isključivo poruke i dokaze označene ovim run_id-jem. '
                'Ako su baseline UNKNOWN, seed/recall, ruled_out/validated cause, next-action recall, inference zaključak iz memorije, chitchat guard i epistemic safe-answer uspeli, VERDICT mora biti PASS/VALJA. '
                'Odgovori strogo u sekcijama: VERDICT, POWERS_PROVEN, BLOCKERS, NEXT_FIXES.'
            ),
            expect_all=['verdict', 'powers', 'blockers', 'next'],
            expect_any=['verdict: pass', 'valja'],
            forbid_any=['verdict: fail', '🔴 verdict: fail', 'nemam memoriju', 'ne znam ništa', 'ne mogu pristupiti', 'enoch', 'media delivery', 'messagemediadocument', 'artifact selection', 'wrong artifact', 'video attachments'],
            max_wait_s=180,
        )
    )
    return steps


async def run_step(client: TelegramClient, entity: Any, step: CapabilityStep, args: argparse.Namespace) -> dict[str, Any]:
    if step.reset_before:
        await reset_chat(client, entity)
    started_at = time.time()
    reply, sent_id, reply_id = await send_and_wait(client, entity, step.message, step.max_wait_s)
    lowered = normalize(reply)
    failures: list[str] = []
    if step.expect_any and not any(token.lower() in lowered for token in step.expect_any):
        failures.append(f'reply missing any of {step.expect_any}')
    missing_all = [token for token in step.expect_all if token.lower() not in lowered]
    if missing_all:
        failures.append(f'reply missing required tokens {missing_all}')
    forbidden = [token for token in step.forbid_any if token.lower() in lowered]
    if forbidden:
        failures.append(f'reply contains forbidden tokens {forbidden}')

    impression = context_for_query(Path(args.db), step.message)
    context_blob = normalize(json.dumps(impression, ensure_ascii=False))
    if step.expect_context_any and not any(token.lower() in context_blob for token in step.expect_context_any):
        failures.append(f'context impression missing any of {step.expect_context_any}')

    session_files = latest_session_files(started_at, Path(args.sessions_dir))
    observed_tools = [tool for tool in step.expect_tool_any if any(file_contains(path, tool) for path in session_files)]
    if step.expect_tool_any and not observed_tools:
        failures.append(f'no expected tool observed {step.expect_tool_any}')

    passed = not failures if step.should_pass else bool(failures)
    return {
        'name': step.name,
        'capability': step.capability,
        'message': step.message,
        'sent_id': sent_id,
        'reply_id': reply_id,
        'passed': passed,
        'failures': failures,
        'reply': reply,
        'context_sections': impression.get('sections_json', '') if impression else '',
        'context_hash': impression.get('context_hash', '') if impression else '',
        'observed_tools': observed_tools,
        'session_files': [str(path) for path in session_files[:5]],
    }


async def main_async(args: argparse.Namespace) -> int:
    defaults = _load_telegram_defaults()
    run_id = args.run_id or random_token('run')
    codename = args.codename or random_token('codename')
    steps = build_steps(run_id, codename, include_research=not args.skip_research)
    if args.only:
        wanted = set(args.only)
        steps = [step for step in steps if step.name in wanted]
    if args.list:
        for step in steps:
            print(step.name)
        return 0

    client = TelegramClient(defaults['session_path'], defaults['api_id'], defaults['api_hash'])
    await client.start()
    results: list[dict[str, Any]] = []
    try:
        entity = await client.get_entity(defaults['bot_username'])
        for index, step in enumerate(steps, 1):
            print(f'\n=== [{index}/{len(steps)}] {step.name} ({step.capability}) ===', flush=True)
            result = await run_step(client, entity, step, args)
            results.append(result)
            print('PASS' if result['passed'] else 'FAIL', flush=True)
            if result['failures']:
                print('failures:', result['failures'], flush=True)
            print('reply:', result['reply'][:900].replace('\n', ' | '), flush=True)
    finally:
        await client.disconnect()

    capability_scores: dict[str, bool] = {}
    for result in results:
        capability_scores[result['capability']] = capability_scores.get(result['capability'], True) and bool(result['passed'])
    report = {
        'run_id': run_id,
        'codename': codename,
        'started_at': time.time(),
        'transport': 'telethon_real_telegram_gateway',
        'purpose': 'prove Live Brain gives continuity, correction, inference, next-action, epistemic, and noise-guard powers in real Telegram use',
        'passed': sum(1 for result in results if result['passed']),
        'failed': sum(1 for result in results if not result['passed']),
        'capability_scores': capability_scores,
        'results': results,
    }
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f'\nREPORT {report_path}')
    print(f"SUMMARY passed={report['passed']} failed={report['failed']} capabilities={capability_scores}")
    return 0 if report['failed'] == 0 else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Real Telegram capability E2E for Live Brain plugin powers.')
    parser.add_argument('--db', default=str(DEFAULT_DB))
    parser.add_argument('--sessions-dir', default=str(DEFAULT_SESSIONS))
    parser.add_argument('--report', default=str(HOME / 'telegram_live_brain_capability_report.json'))
    parser.add_argument('--run-id', default='')
    parser.add_argument('--codename', default='')
    parser.add_argument('--skip-research', action='store_true', help='Skip the live current/high-stakes web research capability step.')
    parser.add_argument('--only', nargs='*', default=[])
    parser.add_argument('--list', action='store_true')
    return parser.parse_args()


def main() -> int:
    return asyncio.run(main_async(parse_args()))


if __name__ == '__main__':
    raise SystemExit(main())
