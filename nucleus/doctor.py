"""Nucleus doctor/preflight checks.

Safe by default: production Live Brain write/read is tested against a temporary
SQLite backup, not the live DB. Network web probe is opt-in via --web.
"""
from __future__ import annotations

import argparse
import json
import os
import py_compile
import sqlite3
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import LIVE_BRAIN_DB, LOCK_FILE, NUCLEUS_HOME, PARGOD_DB, PID_FILE
from .live_brain_sync import LiveBrainSync
from .status import _pid_alive, _read_json_file


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str = ""
    data: dict | None = None

    @property
    def ok(self) -> bool:
        return self.status in {"pass", "warn", "skip"}


def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _has_table(conn, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def check_syntax(plugin_dir: Path = NUCLEUS_HOME) -> CheckResult:
    files = [
        path for path in Path(plugin_dir).rglob("*.py")
        if "__pycache__" not in path.parts
    ]
    failures = []
    for path in files:
        try:
            py_compile.compile(str(path), doraise=True)
        except py_compile.PyCompileError as exc:
            failures.append({"path": str(path), "error": str(exc)[:500]})
    if failures:
        return CheckResult("syntax", "fail", f"{len(failures)} Python file(s) failed py_compile", {"failures": failures})
    return CheckResult("syntax", "pass", f"compiled {len(files)} Python file(s)", {"files": len(files)})


def check_pargod_schema(db_path: Path = PARGOD_DB) -> CheckResult:
    if not Path(db_path).exists():
        return CheckResult("pargod_schema", "fail", f"missing DB: {db_path}")
    required = {
        "nodes": {"id", "type", "label", "content", "use_count", "last_used", "created_at"},
        "edges": {"id", "source_id", "target_id", "relation", "weight", "use_count", "last_used", "created_at"},
        "episodes": {"id", "tick", "entropy", "sensor_state", "action_taken", "created_at"},
    }
    conn = sqlite3.connect(str(db_path), timeout=2.0)
    try:
        missing = {}
        for table, columns in required.items():
            if not _has_table(conn, table):
                missing[table] = ["<table missing>"]
                continue
            table_columns = _columns(conn, table)
            absent = sorted(columns - table_columns)
            if absent:
                missing[table] = absent
        if missing:
            return CheckResult("pargod_schema", "fail", "Pargod schema missing required tables/columns", {"missing": missing})
        counts = {
            "nodes": conn.execute("SELECT count(*) FROM nodes").fetchone()[0],
            "edges": conn.execute("SELECT count(*) FROM edges").fetchone()[0],
            "episodes": conn.execute("SELECT count(*) FROM episodes").fetchone()[0],
        }
        return CheckResult("pargod_schema", "pass", "Pargod schema OK", counts)
    except Exception as exc:
        return CheckResult("pargod_schema", "fail", str(exc))
    finally:
        conn.close()


def check_runtime_lock(lock_file: Path = LOCK_FILE, pid_file: Path = PID_FILE) -> CheckResult:
    lock_owner = _read_json_file(lock_file)
    pid_info = _read_json_file(pid_file)
    active_pid = lock_owner.get("pid") or pid_info.get("pid")
    alive = _pid_alive(active_pid)
    if active_pid and alive:
        return CheckResult("runtime_lock", "pass", "heartbeat owner is alive", {"lock_owner": lock_owner, "pid_info": pid_info})
    if active_pid and not alive:
        return CheckResult("runtime_lock", "warn", "stale pid/lock owner detected", {"lock_owner": lock_owner, "pid_info": pid_info})
    return CheckResult("runtime_lock", "warn", "no active heartbeat lock owner", {"lock_owner": lock_owner, "pid_info": pid_info})


def check_service_contract(plugin_dir: Path = NUCLEUS_HOME) -> CheckResult:
    service = Path(plugin_dir) / "nucleus.service"
    launcher = Path(plugin_dir) / "launcher.sh"
    if not service.exists() or not launcher.exists():
        return CheckResult("service_contract", "fail", "missing nucleus.service or launcher.sh")
    service_text = service.read_text(errors="replace")
    launcher_text = launcher.read_text(errors="replace")
    required = {
        "service_standalone_env": "Environment=NUCLEUS_STANDALONE=1" in service_text,
        "service_on_failure": "Restart=on-failure" in service_text,
        "service_venv": "hermes-agent/venv/bin/python" in service_text,
        "launcher_disable_hint": "NUCLEUS_DISABLE_EMBEDDED=1" in launcher_text,
        "launcher_doctor_cmd": "doctor)" in launcher_text,
    }
    failed = [name for name, ok in required.items() if not ok]
    if failed:
        return CheckResult("service_contract", "fail", "service/launcher contract failed", {"failed": failed})
    return CheckResult("service_contract", "pass", "service/launcher contract OK", required)


def _copy_sqlite_db(src: Path, dst: Path) -> None:
    src_conn = sqlite3.connect(str(src), timeout=10.0)
    dst_conn = sqlite3.connect(str(dst), timeout=10.0)
    try:
        src_conn.backup(dst_conn)
    finally:
        src_conn.close()
        dst_conn.close()


def check_live_brain_probe(db_path: Path = LIVE_BRAIN_DB) -> CheckResult:
    if not Path(db_path).exists():
        return CheckResult("live_brain_probe", "fail", f"missing DB: {db_path}")
    with tempfile.NamedTemporaryFile(prefix="nucleus_doctor_live_brain_", suffix=".db", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        _copy_sqlite_db(Path(db_path), tmp_path)
        sync = LiveBrainSync(str(tmp_path))
        fact_text = f"Nucleus doctor probe {int(time.time())}"
        fact_id = sync.write_fact(
            fact_text,
            scope_key="nucleus_doctor",
            question="doctor write/read probe",
            confidence=0.51,
            source_urls=["doctor://local-copy"],
        )
        facts = sync.get_facts("nucleus_doctor", "doctor write", limit=5)
        if not fact_id or not facts:
            return CheckResult("live_brain_probe", "fail", "Live Brain write/read probe failed on temp backup", {"fact_id": fact_id})
        conn = sqlite3.connect(str(tmp_path), timeout=2.0)
        try:
            required_tables = ["epistemic_learned_facts", "fix_recipes", "research_jobs", "verified_artifacts"]
            missing_tables = [table for table in required_tables if not _has_table(conn, table)]
        finally:
            conn.close()
        if missing_tables:
            return CheckResult("live_brain_probe", "fail", "Live Brain missing required tables", {"missing_tables": missing_tables})
        return CheckResult("live_brain_probe", "pass", "Live Brain schema and write/read probe OK on temp backup", {"fact_id": fact_id})
    except Exception as exc:
        return CheckResult("live_brain_probe", "fail", str(exc))
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


def check_web_availability(run_web: bool = False) -> CheckResult:
    if not run_web:
        return CheckResult("web_availability", "skip", "network probe skipped; pass --web to test DuckDuckGo")
    try:
        from .web_search import search
        results = search("python list comprehension", limit=1, timeout=5.0)
    except Exception as exc:
        return CheckResult("web_availability", "warn", f"web probe failed: {exc}")
    if results:
        return CheckResult("web_availability", "pass", "web search returned results", {"url": results[0].get("url", "")})
    return CheckResult("web_availability", "warn", "web search returned no results")


def run_doctor(
    *,
    plugin_dir: Path = NUCLEUS_HOME,
    pargod_db: Path = PARGOD_DB,
    live_brain_db: Path = LIVE_BRAIN_DB,
    lock_file: Path = LOCK_FILE,
    pid_file: Path = PID_FILE,
    run_web: bool = False,
) -> list[CheckResult]:
    return [
        check_syntax(Path(plugin_dir)),
        check_pargod_schema(Path(pargod_db)),
        check_runtime_lock(Path(lock_file), Path(pid_file)),
        check_service_contract(Path(plugin_dir)),
        check_live_brain_probe(Path(live_brain_db)),
        check_web_availability(run_web),
    ]


def doctor_ok(results: list[CheckResult]) -> bool:
    return all(result.ok for result in results)


def format_doctor(results: list[CheckResult]) -> str:
    failed = sum(1 for result in results if result.status == "fail")
    warned = sum(1 for result in results if result.status == "warn")
    skipped = sum(1 for result in results if result.status == "skip")
    lines = [f"[NUCLEUS DOCTOR] {'PASS' if failed == 0 else 'FAIL'} | failures={failed} warnings={warned} skipped={skipped}"]
    for result in results:
        marker = {"pass": "✅", "warn": "⚠️", "fail": "❌", "skip": "⏭️"}.get(result.status, "•")
        lines.append(f"{marker} {result.name}: {result.status} — {result.detail}")
    return "\n".join(lines)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Nucleus production preflight checks.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    parser.add_argument("--web", action="store_true", help="Run live web search availability probe")
    args = parser.parse_args(argv)
    results = run_doctor(run_web=args.web)
    if args.json:
        print(json.dumps([asdict(result) for result in results], indent=2, sort_keys=True))
    else:
        print(format_doctor(results))
    return 0 if doctor_ok(results) else 1


if __name__ == "__main__":
    raise SystemExit(_main())
