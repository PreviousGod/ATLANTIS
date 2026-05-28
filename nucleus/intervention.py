"""Intervention engine — pre-emptive mistake detection and correction.

Nucleus as "god in the background":
  1. Sees intent before execution (pre_tool_call)
  2. Detects patterns that historically fail
  3. Blocks with explanation + correct approach
  4. Points to exact files/lines when possible
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from .config import DATA_DIR
from .session_state import get_session_state
from .learning_engine import LearningEngine

log = logging.getLogger("nucleus")

_INTERVENTION_DB = DATA_DIR / "interventions.db"

# Confidence threshold for blocking — must be high to avoid false positives
_BLOCK_CONFIDENCE = 0.85

# P1.2: severity tiers. Only CRITICAL patterns return action=block (which the
# Hermes core honors); everything else becomes action=warn and is surfaced as
# a one-shot system note on the next turn (see _pre_tool_hook in __init__.py).
# Critical = destructive/irreversible at scale OR secret exfiltration.
_CRITICAL_PATTERNS = frozenset({
    "Using 777 permissions",
    "Writing API keys or secrets into tracked files",
    "rm -rf /",
})

# Known mistake patterns mapped to corrections
# These seed patterns; the graph learns more over time.
_SEED_MISTAKES = [
    {
        "task_pattern": "refactor",
        "tool": "patch",
        "args_hint": "replace",
        "mistake": "Editing code without reading the full file context first",
        "correction": "Always read the target file with read_file before patching. Use ATLANTIS: decompose→verify→patch→verify.",
        "confidence": 0.90,
        "file_locations": [],
    },
    {
        "task_pattern": "refactor",
        "tool": "patch",
        "args_hint": "old_string",
        "mistake": "Using ambiguous old_string that matches multiple locations",
        "correction": "Include 3+ lines of unique context around old_string. If uncertain, read first.",
        "confidence": 0.88,
        "file_locations": [],
    },
    {
        "task_pattern": "config",
        "tool": "write_file",
        "args_hint": "yaml",
        "mistake": "Overwriting entire config without preserving existing keys",
        "correction": "Use patch for targeted edits. Only write_file for new files.",
        "confidence": 0.85,
        "file_locations": [],
    },
    {
        "task_pattern": "database",
        "tool": "terminal",
        "args_hint": "sqlite",
        "mistake": "Running raw SQL on live DB without backup or transaction",
        "correction": "Always backup first: cp db.db db.db.bak. Use transactions: BEGIN; ... COMMIT;",
        "confidence": 0.92,
        "file_locations": [],
    },
    {
        "task_pattern": "test",
        "tool": "terminal",
        "args_hint": "pytest",
        "mistake": "Running full test suite when only one test changed",
        "correction": "Run the narrowest test first: pytest path/to/test_file.py::test_name -xvs",
        "confidence": 0.82,
        "file_locations": [],
    },
    {
        "task_pattern": "deploy",
        "tool": "terminal",
        "args_hint": "systemctl",
        "mistake": "Restarting production service without checking logs first",
        "correction": "Check logs before restart: journalctl -u service -n 50 --no-pager. Then restart.",
        "confidence": 0.90,
        "file_locations": [],
    },
    {
        "task_pattern": "git",
        "tool": "terminal",
        "args_hint": "git push",
        "mistake": "Pushing without running tests or checking diff",
        "correction": "Run tests, then git diff --stat, then git push.",
        "confidence": 0.85,
        "file_locations": [],
    },
    {
        "task_pattern": "permission",
        "tool": "terminal",
        "args_hint": "chmod 777",
        "mistake": "Using 777 permissions",
        "correction": "Use least privilege: chmod 644 for files, 755 for dirs. Never 777.",
        "confidence": 0.95,
        "file_locations": [],
    },
    {
        "task_pattern": "secret",
        "tool": "write_file",
        "args_hint": "api_key",
        "mistake": "Writing API keys or secrets into tracked files",
        "correction": "Use .env files (gitignored) or secret managers. Never commit secrets.",
        "confidence": 0.95,
        "file_locations": [],
    },
    {
        "task_pattern": "nucleus",
        "tool": "patch",
        "args_hint": "__init__.py",
        "mistake": "Editing Nucleus plugin without running tests after",
        "correction": "After any Nucleus change: cd ~/.hermes/plugins && python3 -c \"from tests.test_e2e import *; [t() for t in [test_pargod_pathfinding, test_pargod_seed, test_entropy_calculation, test_sensor_reads, test_instinct_guard_safe, test_instinct_guard_blocked, test_live_brain_sync_writes, test_research_problem_local_sources_write_graph, test_pargod_resolution_prefers_tool_when_available, test_pre_llm_hook_injects_fix_recipe_context, test_failure_trigger_classifies_llm_gap, test_post_llm_hook_schedules_gap_research, test_post_tool_hook_schedules_failure_research]]\"",
        "confidence": 0.88,
        "file_locations": [
            "~/.hermes/plugins/nucleus/__init__.py",
            "~/.hermes/plugins/nucleus/tests/test_e2e.py",
        ],
    },
]


class InterventionEngine:
    """Detects and blocks known mistakes before tool execution."""

    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._seed_if_empty()
        self.learning = LearningEngine()

    def _conn(self):
        c = sqlite3.connect(str(_INTERVENTION_DB), timeout=5.0)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=3000")
        return c

    def _init_db(self):
        with closing(self._conn()) as conn, conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS mistakes (
                    id INTEGER PRIMARY KEY,
                    task_pattern TEXT,
                    tool_name TEXT,
                    args_hint TEXT,
                    mistake TEXT NOT NULL,
                    correction TEXT NOT NULL,
                    confidence REAL DEFAULT 0.8,
                    file_locations_json TEXT DEFAULT '[]',
                    hit_count INTEGER DEFAULT 0,
                    last_hit REAL,
                    created_at REAL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY,
                    session_id TEXT,
                    tool_name TEXT,
                    args_preview TEXT,
                    blocked INTEGER DEFAULT 0,
                    reason TEXT,
                    timestamp REAL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_mistakes_pattern ON mistakes(task_pattern, tool_name);
                CREATE INDEX IF NOT EXISTS idx_history_session ON history(session_id);
            """)

    def _seed_if_empty(self):
        with closing(self._conn()) as conn, conn:
            count = conn.execute("SELECT COUNT(*) FROM mistakes").fetchone()[0]
            if count > 0:
                return
            now = time.time()
            for m in _SEED_MISTAKES:
                conn.execute(
                    """INSERT INTO mistakes
                       (task_pattern, tool_name, args_hint, mistake, correction,
                        confidence, file_locations_json, created_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        m.get("task_pattern"),
                        m.get("tool"),
                        m.get("args_hint"),
                        m["mistake"],
                        m["correction"],
                        m["confidence"],
                        json.dumps(m.get("file_locations", [])),
                        now,
                    ),
                )
            log.info(f"Seeded {len(_SEED_MISTAKES)} intervention patterns")

    def check(self, tool_name: str | None, args: dict | None, session_id: str = "") -> dict | None:
        """Return intervention dict if this tool call should be blocked."""
        if not tool_name:
            return None
        args = args or {}
        args_text = json.dumps(args, ensure_ascii=False, default=str).lower()
        tool_lower = tool_name.lower()

        # ── LearningEngine: check if we learned this is a false positive ──
        normalized = self.learning.normalize_args(tool_lower, args)
        pattern_hash = self.learning.hash_pattern(tool_lower, normalized)
        learned = self.learning.get_pattern(pattern_hash)
        if learned and learned["confidence"] < 0.4:
            # This pattern was marked as false positive; allow it
            self.learning.record_intervention(session_id, tool_lower, args, pattern_hash, blocked=False)
            return None
        if learned and learned["confidence"] > 0.85 and learned["outcome"] == "blocked_correct":
            # High confidence known bad pattern; boost block
            pass  # fall through to normal blocking

        # ── SessionState-based dynamic rules ──
        session_state = get_session_state()
        ss = session_state.snapshot(session_id) if session_id else {}

        # Rule: patch without reading file first
        if session_id and tool_lower == "patch":
            path = args.get("path", "")
            if path and not session_state.has_file_been_read(str(path), session_id=session_id):
                self.learning.record_intervention(session_id, tool_lower, args, "patch_without_read", blocked=True)
                return {
                    "action": "warn",
                    "severity": "warn",
                    "message": (
                        "[NUCLEUS WARN] confidence=88%\n"
                        "Detected risk: patching a file that was not read first\n\n"
                        "Recommended approach:\n"
                        f"Read {path} with read_file before patching. "
                        "Use ATLANTIS: decompose→verify→patch→verify."
                    ),
                    "confidence": 0.88,
                    "pattern": "patch_without_read",
                    "auto_resolve_on": {"signal": "file_was_read", "target": str(path)},
                }

        # Rule: high-risk pending changes without backup
        if session_id and tool_lower in ("patch", "write_file") and session_state.has_unbackedup_changes(session_id):
            risk = session_state.pending_risk_score(session_id)
            if risk > 0.5:
                self.learning.record_intervention(session_id, tool_lower, args, "no_backup_high_risk", blocked=True)
                return {
                    "action": "warn",
                    "severity": "warn",
                    "message": (
                        "[NUCLEUS WARN] confidence=85%\n"
                        "Detected risk: making changes without backup\n\n"
                        "Recommended approach:\n"
                        f"You have {session_state.pending_change_count(session_id)} pending changes with risk={risk:.1f}. "
                        "Run a backup first: cp file file.bak"
                    ),
                    "confidence": 0.85,
                    "pattern": "no_backup_high_risk",
                    "auto_resolve_on": {"signal": "backup_taken"},
                }

        # Rule: repeating known error pattern
        last_err = ss.get("last_error", "")
        if session_id and last_err and tool_lower == "terminal":
            cmd = args.get("command", "").lower()
            if "sqlite" in last_err.lower() and "sqlite" in cmd:
                self.learning.record_intervention(session_id, tool_lower, args, "repeat_sqlite_error", blocked=True)
                return {
                    "action": "warn",
                    "severity": "warn",
                    "message": (
                        "[NUCLEUS WARN] confidence=82%\n"
                        "Detected risk: repeating a known failing pattern\n\n"
                        "Recommended approach:\n"
                        f"Last error was SQLite-related: {last_err[:100]}. "
                        "Wait 2s, check locks, or use a different approach."
                    ),
                    "confidence": 0.82,
                    "pattern": "repeat_sqlite_error",
                }

        # Query matching patterns (existing DB rules)
        with closing(self._conn()) as conn, conn:
            rows = conn.execute(
                """SELECT task_pattern, tool_name, args_hint, mistake, correction,
                          confidence, file_locations_json
                   FROM mistakes
                   WHERE (tool_name = ? OR tool_name = '*')
                   ORDER BY confidence DESC""",
                (tool_lower,),
            ).fetchall()

        for row in rows:
            task_pattern, db_tool, args_hint, mistake, correction, confidence, files_json = row
            # Match args hint against actual args
            if args_hint and args_hint not in args_text:
                continue
            # Check confidence threshold
            if confidence < _BLOCK_CONFIDENCE:
                continue
            # Log the hit
            self._log(session_id, tool_name, args_text[:200], blocked=True, reason=mistake)
            self._bump_hit(db_tool, args_hint)
            self.learning.record_intervention(session_id, tool_lower, args, mistake, blocked=True)

            file_locations = json.loads(files_json or "[]")
            severity = "block" if mistake in _CRITICAL_PATTERNS else "warn"
            explanation = self._format_explanation(
                mistake, correction, file_locations, confidence, severity=severity
            )
            return {
                "action": severity,
                "severity": severity,
                "message": explanation,
                "confidence": confidence,
                "pattern": mistake,
            }

        # No block — just log for learning
        self._log(session_id, tool_name, args_text[:200], blocked=False, reason="")
        self.learning.record_intervention(session_id, tool_lower, args, "no_match", blocked=False)
        return None

    def _format_explanation(
        self,
        mistake: str,
        correction: str,
        file_locations: list,
        confidence: float,
        severity: str = "block",
    ) -> str:
        header = "[NUCLEUS INTERVENTION]" if severity == "block" else "[NUCLEUS WARN]"
        verb = "Detected mistake" if severity == "block" else "Detected risk"
        approach = "Correct approach:" if severity == "block" else "Recommended approach:"
        lines = [
            f"{header} confidence={confidence:.0%}",
            f"{verb}: {mistake}",
            "",
            approach,
            correction,
        ]
        if file_locations:
            lines.append("")
            lines.append("Relevant files:")
            for loc in file_locations:
                lines.append(f"  - {loc}")
        if severity == "block":
            lines.append("")
            lines.append("Override: If you're sure, rephrase your request more specifically.")
        return "\n".join(lines)

    def _log(self, session_id: str, tool_name: str, args_preview: str, blocked: bool, reason: str):
        try:
            with closing(self._conn()) as conn, conn:
                conn.execute(
                    """INSERT INTO history
                       (session_id, tool_name, args_preview, blocked, reason, timestamp)
                       VALUES (?,?,?,?,?,?)""",
                    (session_id, tool_name, args_preview, int(blocked), reason, time.time()),
                )
        except Exception:
            pass

    def _bump_hit(self, tool_name: str, args_hint: str):
        try:
            with closing(self._conn()) as conn, conn:
                conn.execute(
                    """UPDATE mistakes
                       SET hit_count = hit_count + 1, last_hit = ?
                       WHERE tool_name = ? AND (args_hint = ? OR args_hint IS NULL)""",
                    (time.time(), tool_name, args_hint),
                )
        except Exception:
            pass

    def record_custom_pattern(
        self,
        task_pattern: str,
        tool_name: str,
        args_hint: str,
        mistake: str,
        correction: str,
        confidence: float = 0.85,
        file_locations: list | None = None,
    ) -> bool:
        """Manually teach Nucleus a new mistake pattern."""
        try:
            with closing(self._conn()) as conn, conn:
                conn.execute(
                    """INSERT INTO mistakes
                       (task_pattern, tool_name, args_hint, mistake, correction,
                        confidence, file_locations_json, created_at)
                       VALUES (?,?,?,?,?,?,?,?)
                       ON CONFLICT DO NOTHING""",
                    (
                        task_pattern, tool_name, args_hint, mistake, correction,
                        confidence, json.dumps(file_locations or []), time.time(),
                    ),
                )
            log.info("Recorded intervention pattern: %s / %s", task_pattern, tool_name)
            return True
        except Exception as e:
            log.warning("Failed to record pattern: %s", e)
            return False

    def get_stats(self) -> dict:
        with closing(self._conn()) as conn, conn:
            total = conn.execute("SELECT COUNT(*) FROM mistakes").fetchone()[0]
            blocked = conn.execute(
                "SELECT COUNT(*) FROM history WHERE blocked = 1"
            ).fetchone()[0]
            observed = conn.execute(
                "SELECT COUNT(*) FROM history"
            ).fetchone()[0]
            top = conn.execute(
                """SELECT mistake, hit_count FROM mistakes
                   ORDER BY hit_count DESC LIMIT 5"""
            ).fetchall()
        return {
            "patterns": total,
            "blocked": blocked,
            "observed": observed,
            "top_mistakes": [{"mistake": m, "hits": h} for m, h in top],
        }
