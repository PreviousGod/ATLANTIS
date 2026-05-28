"""Status and observability helpers for Nucleus."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from .config import LOCK_FILE, PID_FILE, PARGOD_DB, LIVE_BRAIN_DB, LOG_FILE
from .failure_trigger import get_research_state_snapshot


def _read_json_file(path: Path) -> dict:
    try:
        text = path.read_text().strip()
        return json.loads(text) if text else {}
    except Exception:
        return {}


def _pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except Exception:
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def _connect_existing(path: Path):
    if not path or not Path(path).exists():
        return None
    conn = sqlite3.connect(str(path), timeout=2.0)
    conn.row_factory = sqlite3.Row
    return conn


def _pargod_status(db_path: Path) -> dict:
    conn = _connect_existing(db_path)
    if not conn:
        return {"ok": False, "path": str(db_path), "reason": "missing"}
    try:
        node_counts = {
            row["type"]: row["count"]
            for row in conn.execute("SELECT type, count(*) AS count FROM nodes GROUP BY type").fetchall()
        }
        edge_count = conn.execute("SELECT count(*) FROM edges").fetchone()[0]
        episode_count = conn.execute("SELECT count(*) FROM episodes").fetchone()[0]
        last_episode = conn.execute(
            """SELECT tick, entropy, action_taken, created_at
               FROM episodes ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        return {
            "ok": True,
            "path": str(db_path),
            "nodes": node_counts,
            "edges": edge_count,
            "episodes": episode_count,
            "last_episode": dict(last_episode) if last_episode else None,
        }
    except Exception as exc:
        return {"ok": False, "path": str(db_path), "reason": str(exc)}
    finally:
        conn.close()


def _table_count(conn, table: str) -> int | None:
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            return None
        return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    except Exception:
        return None


def _live_brain_status(db_path: Path) -> dict:
    conn = _connect_existing(db_path)
    if not conn:
        return {"ok": False, "path": str(db_path), "reason": "missing"}
    try:
        return {
            "ok": True,
            "path": str(db_path),
            "facts": _table_count(conn, "epistemic_learned_facts"),
            "fix_recipes": _table_count(conn, "fix_recipes"),
            "research_jobs": _table_count(conn, "research_jobs"),
            "verified_artifacts": _table_count(conn, "verified_artifacts"),
        }
    except Exception as exc:
        return {"ok": False, "path": str(db_path), "reason": str(exc)}
    finally:
        conn.close()


def _suggestion_status(db_path: Path) -> dict:
    conn = _connect_existing(db_path)
    if not conn:
        return {"ok": False, "reason": "missing"}
    try:
        total = conn.execute("SELECT COUNT(*) FROM suggestions").fetchone()[0]
        shown = conn.execute("SELECT SUM(times_shown) FROM suggestions").fetchone()[0] or 0
        acted = conn.execute("SELECT SUM(times_acted) FROM suggestions").fetchone()[0] or 0
        ignored = conn.execute("SELECT SUM(times_ignored) FROM suggestions").fetchone()[0] or 0
        last_hour = conn.execute(
            "SELECT COUNT(*) FROM suggestion_log WHERE sent_at > ?",
            (time.time() - 3600,),
        ).fetchone()[0]
        # Get last suggestion
        last = conn.execute(
            """SELECT s.category, s.severity, s.message, sl.sent_at
               FROM suggestion_log sl
               JOIN suggestions s ON sl.suggestion_id = s.id
               ORDER BY sl.sent_at DESC LIMIT 1"""
        ).fetchone()
        return {
            "ok": True,
            "total_rules": total,
            "shown": shown,
            "acted": acted,
            "ignored": ignored,
            "last_hour": last_hour,
            "effectiveness": round(acted / max(1, shown), 3),
            "last": dict(last) if last else None,
        }
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}
    finally:
        conn.close()


def _ego_status(db_path: Path) -> dict:
    conn = _connect_existing(db_path)
    if not conn:
        return {"ok": False, "reason": "missing"}
    try:
        moods = conn.execute("SELECT mood, COUNT(*) FROM ego_states GROUP BY mood").fetchall()
        reflections = conn.execute("SELECT COUNT(*) FROM reflections").fetchone()[0]
        auto_total = conn.execute("SELECT COUNT(*) FROM autonomous_actions").fetchone()[0]
        auto_exec = conn.execute("SELECT COUNT(*) FROM autonomous_actions WHERE executed=1").fetchone()[0]
        last_mood = conn.execute(
            "SELECT mood, entropy FROM ego_states ORDER BY tick DESC LIMIT 1"
        ).fetchone()
        last_reflect = conn.execute(
            """SELECT trigger, monologue, insight FROM reflections ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        return {
            "ok": True,
            "moods": {m: c for m, c in moods},
            "reflections": reflections,
            "autonomous_total": auto_total,
            "autonomous_executed": auto_exec,
            "current_mood": last_mood[0] if last_mood else "calm",
            "current_entropy": last_mood[1] if last_mood else 0.0,
            "last_reflection": dict(last_reflect) if last_reflect else None,
        }
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}
    finally:
        conn.close()


def _last_log_lines(path: Path, limit: int = 8) -> list[str]:
    try:
        lines = path.read_text(errors="replace").splitlines()
    except Exception:
        return []
    interesting = [line for line in lines if any(token in line for token in ("AUTO-RESEARCH", "RESEARCH:", "GRAPH PATH", "Runtime lock", "heartbeat"))]
    return interesting[-limit:]


def collect_status(nucleus=None) -> dict:
    lock_owner = _read_json_file(LOCK_FILE)
    pid_info = _read_json_file(PID_FILE)
    pargod_path = Path(getattr(getattr(nucleus, "pargod", None), "db_path", PARGOD_DB))
    brain_path = Path(getattr(getattr(nucleus, "brain_sync", None), "db_path", LIVE_BRAIN_DB))
    active_pid = lock_owner.get("pid") or pid_info.get("pid")
    return {
        "time": time.time(),
        "mode": lock_owner.get("mode") or pid_info.get("mode") or "unknown",
        "lock_file": str(LOCK_FILE),
        "lock_owner": lock_owner,
        "pid_file": str(PID_FILE),
        "pid_info": pid_info,
        "heartbeat_alive": _pid_alive(active_pid),
        "embedded_disabled": os.getenv("NUCLEUS_DISABLE_EMBEDDED", "").strip().lower() in {"1", "true", "yes", "on"},
        "standalone_env": os.getenv("NUCLEUS_STANDALONE", "").strip().lower() in {"1", "true", "yes", "on"},
        "pargod": _pargod_status(pargod_path),
        "live_brain": _live_brain_status(brain_path),
        "research": get_research_state_snapshot(),
        "suggestions": _suggestion_status(pargod_path),
        "ego": _ego_status(pargod_path),
        "recent_log": _last_log_lines(LOG_FILE),
    }


def _fmt_count_map(values: dict) -> str:
    if not values:
        return "none"
    return ", ".join(f"{key}={value}" for key, value in sorted(values.items()))


def format_status(status: dict) -> str:
    pargod = status.get("pargod") or {}
    live_brain = status.get("live_brain") or {}
    research = status.get("research") or {}
    lock_owner = status.get("lock_owner") or {}
    last_episode = pargod.get("last_episode") or {}
    lines = [
        "[NUCLEUS STATUS]",
        f"Mode: {status.get('mode')} | heartbeat_alive={status.get('heartbeat_alive')}",
        f"Lock owner: pid={lock_owner.get('pid', 'n/a')} mode={lock_owner.get('mode', 'n/a')}",
        f"Embedded disabled: {status.get('embedded_disabled')} | standalone env: {status.get('standalone_env')}",
        "",
        "Pargod:",
        f"- ok={pargod.get('ok')} db={pargod.get('path')}",
        f"- nodes: {_fmt_count_map(pargod.get('nodes') or {})}",
        f"- edges={pargod.get('edges', 0)} episodes={pargod.get('episodes', 0)}",
    ]
    if last_episode:
        lines.append(f"- last_action={last_episode.get('action_taken')} entropy={last_episode.get('entropy')} tick={last_episode.get('tick')}")
    lines.extend([
        "",
        "Live Brain:",
        f"- ok={live_brain.get('ok')} db={live_brain.get('path')}",
        f"- facts={live_brain.get('facts')} fix_recipes={live_brain.get('fix_recipes')} research_jobs={live_brain.get('research_jobs')} artifacts={live_brain.get('verified_artifacts')}",
        "",
        "Research Queue:",
        f"- auto_enabled={research.get('auto_enabled')} pending={research.get('pending_count')} limit={research.get('queue_limit')} debounce={research.get('debounce_seconds')}s",
        f"- pending_sessions={len(research.get('pending_by_session') or {})} completed_sessions={len(research.get('completed_by_session') or {})}",
    ])
    recent_log = status.get("recent_log") or []
    if recent_log:
        lines.append("")
        lines.append("Recent Nucleus log:")
        for line in recent_log[-5:]:
            lines.append(f"- {line}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(format_status(collect_status()))
