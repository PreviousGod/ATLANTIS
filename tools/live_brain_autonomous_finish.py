#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live_brain.store import LiveBrainStore


def run_command(cmd: list[str], *, cwd: Path = ROOT, timeout: int = 300) -> dict[str, Any]:
    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        return {
            'cmd': cmd,
            'returncode': proc.returncode,
            'ok': proc.returncode == 0,
            'duration_s': round(time.time() - started, 3),
            'output_tail': (proc.stdout or '')[-6000:],
        }
    except subprocess.TimeoutExpired as exc:
        return {
            'cmd': cmd,
            'returncode': 124,
            'ok': False,
            'duration_s': round(time.time() - started, 3),
            'output_tail': ((exc.stdout or '') if isinstance(exc.stdout, str) else '').splitlines()[-80:],
            'error': 'timeout',
        }


def copy_plugin_dir(src: Path, dst: Path, backup_root: Path, stamp: str) -> dict[str, Any]:
    result: dict[str, Any] = {'src': str(src), 'dst': str(dst), 'backup': '', 'ok': False}
    if not src.exists():
        result['error'] = 'source_missing'
        return result
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        backup = backup_root / f'{dst.name}_backup_{stamp}'
        shutil.copytree(dst, backup, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        current_time = time.time()
        os.utime(backup, (current_time, current_time))
        result['backup'] = str(backup)
    shutil.copytree(src, dst, dirs_exist_ok=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    result['ok'] = True
    return result


def rotate_plugin_backups(plugin_root: Path, *, max_age_hours: float = 48.0, max_keep: int = 4) -> dict[str, Any]:
    now = time.time()
    cutoff = now - max(0.1, max_age_hours) * 3600
    deleted: list[str] = []
    errors: list[dict[str, str]] = []
    for plugin_name in ('live_brain', 'live_brain_ctx'):
        backups = sorted(
            [path for path in plugin_root.glob(f'{plugin_name}_backup_*') if path.is_dir()],
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for index, backup in enumerate(backups):
            if backup.stat().st_mtime >= cutoff and index < max_keep:
                continue
            try:
                shutil.rmtree(backup)
                deleted.append(str(backup))
            except Exception as exc:
                errors.append({'path': str(backup), 'error': str(exc)[:300]})
    return {'ok': not errors, 'deleted': len(deleted), 'deleted_paths': deleted[:20], 'errors': errors}


def compile_paths() -> list[str]:
    paths: list[str] = []
    for pattern in ('live_brain/*.py', 'live_brain_ctx/*.py', 'tools/*.py', 'tests/*.py'):
        paths.extend(str(path) for path in ROOT.glob(pattern))
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(description='Autonomous finish runner for Live Brain plugin.')
    parser.add_argument('--hermes-home', default=os.environ.get('HERMES_HOME', str(Path.home() / '.hermes')))
    parser.add_argument('--report', default=str(Path.home() / 'live_brain_autonomous_finish_report.json'))
    parser.add_argument('--install', action='store_true', help='Copy package plugin dirs into HERMES_HOME/plugins with backups.')
    parser.add_argument('--restart-gateway', action='store_true', help='Restart hermes-gateway after install.')
    parser.add_argument('--telegram-e2e', action='store_true', help='Run real Telegram capability E2E.')
    parser.add_argument('--skip-research', action='store_true', help='Pass --skip-research to Telegram capability E2E.')
    parser.add_argument('--full', action='store_true', help='Install, restart gateway, and run Telegram capability E2E.')
    args = parser.parse_args()

    if args.full:
        args.install = True
        args.restart_gateway = True
        args.telegram_e2e = True

    hermes_home = Path(args.hermes_home).expanduser()
    db_path = hermes_home / 'live_brain' / 'live_brain.db'
    report: dict[str, Any] = {
        'started_at': time.time(),
        'root': str(ROOT),
        'hermes_home': str(hermes_home),
        'db_path': str(db_path),
        'steps': [],
        'install': [],
    }

    store = LiveBrainStore(str(db_path))
    try:
        store.initialize_schema()
        backup_path = ''
        if db_path.exists():
            backup_path = store.backup_database('autonomous-finish')
        dry = store.run_lifecycle_hygiene(dry_run=True)
        applied = store.run_lifecycle_hygiene(dry_run=False)
        report['db_backup'] = backup_path
        report['hygiene_dry_run'] = dry
        report['hygiene_apply'] = applied
        report['db_backup_rotation'] = store.rotate_backups(max_age_hours=48.0, max_keep=8)
        report['wal_checkpoint'] = store.checkpoint_wal(truncate=True)
    finally:
        store.close()

    compile_cmd = [sys.executable, '-m', 'py_compile', *compile_paths()]
    report['steps'].append(run_command(compile_cmd, timeout=300))
    report['steps'].append(run_command([sys.executable, 'smoke_test.py'], timeout=300))
    for test in (
        'tests/live_brain_audit_hygiene_test.py',
        'tests/live_brain_epistemic_test.py',
        'tests/live_brain_reality_test.py',
        'tests/live_brain_artifacts_test.py',
        'tests/live_brain_self_evolution_test.py',
        'tests/live_brain_capability_e2e_test.py',
        'tests/live_brain_ingest_memory_facts_test.py',
    ):
        report['steps'].append(run_command([sys.executable, test], timeout=300))

    if args.install:
        stamp = str(int(time.time()))
        backup_root = hermes_home / 'plugins'
        report['plugin_backup_rotation'] = rotate_plugin_backups(backup_root, max_age_hours=48.0, max_keep=4)
        for name in ('live_brain', 'live_brain_ctx'):
            report['install'].append(copy_plugin_dir(ROOT / name, hermes_home / 'plugins' / name, backup_root, stamp))

    if args.restart_gateway:
        report['steps'].append(run_command(['systemctl', '--user', 'restart', 'hermes-gateway'], cwd=Path.home(), timeout=120))
        report['steps'].append(run_command(['systemctl', '--user', 'status', 'hermes-gateway', '--no-pager'], cwd=Path.home(), timeout=60))

    if args.telegram_e2e:
        telegram_python = Path.home() / 'telegram_test_venv' / 'bin' / 'python'
        python_bin = str(telegram_python if telegram_python.exists() else Path(sys.executable))
        e2e_report = Path(args.report).with_name('telegram_live_brain_capability_report.json')
        cmd = [python_bin, str(ROOT / 'tools' / 'live_brain_capability_e2e.py'), '--report', str(e2e_report)]
        if args.skip_research:
            cmd.append('--skip-research')
        report['steps'].append(run_command(cmd, cwd=ROOT, timeout=1800))
        report['telegram_e2e_report'] = str(e2e_report)

    report['finished_at'] = time.time()
    report['ok'] = all(step.get('ok') for step in report['steps']) and all(item.get('ok', True) for item in report['install'])
    report_path = Path(args.report).expanduser()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(json.dumps({'ok': report['ok'], 'report': str(report_path), 'steps': len(report['steps'])}, ensure_ascii=False))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
