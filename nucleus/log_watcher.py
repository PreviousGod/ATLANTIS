"""Ciel Log Watcher — Real-time log tailing for conversation context.

Watches gateway.log and agent.log to build a complete picture of:
- User messages
- Agent reasoning and tool calls
- Successes and failures

This accumulated context feeds the WhisperEngine for predictive advisories.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from .config import DATA_DIR

log = logging.getLogger("nucleus")

SESSION_CONTEXT_FILE = DATA_DIR / "session_context.json"

# Log files to watch
LOG_FILES = {
    "gateway": Path.home() / ".hermes" / "logs" / "gateway.log",
    "agent": Path.home() / ".hermes" / "logs" / "agent.log",
}


class LogWatcher:
    """Tails Hermes logs in background and accumulates session context."""

    def __init__(self):
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._context: Dict = {
            "started_at": time.time(),
            "user_messages": [],
            "agent_responses": [],
            "tool_calls": [],
            "failures": [],
            "last_activity": time.time(),
        }
        self._lock = threading.Lock()

    def start(self):
        """Start background log tailing thread."""
        if self._thread and self._thread.is_alive():
            log.warning("LogWatcher already running")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._watch_loop, daemon=True)
        self._thread.start()
        log.info("LogWatcher started")

    def stop(self):
        """Stop background thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        log.info("LogWatcher stopped")

    def _watch_loop(self):
        """Main loop: tail logs and parse events."""
        # Use tail -f on both log files
        cmd = ["tail", "-f", "-n", "0"] + [str(p) for p in LOG_FILES.values() if p.exists()]
        if len(cmd) <= 4:
            log.warning("No log files found to watch")
            return

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            while not self._stop_event.is_set():
                line = proc.stdout.readline()
                if not line:
                    time.sleep(0.1)
                    continue
                self._parse_line(line.strip())
        except Exception as e:
            log.error("LogWatcher error: %s", e)

    def _parse_line(self, line: str):
        """Parse a single log line and update context."""
        # User inbound message
        if "inbound message:" in line and "msg=" in line:
            match = re.search(r"msg='([^']+)'", line)
            if match:
                msg = match.group(1)
                with self._lock:
                    self._context["user_messages"].append({
                        "timestamp": time.time(),
                        "text": msg,
                    })
                    self._context["last_activity"] = time.time()
                self._persist()
                log.debug("LogWatcher: user message captured (%d chars)", len(msg))
            return

        # Agent response ready
        if "response ready:" in line:
            match = re.search(r"response=(\d+) chars", line)
            if match:
                with self._lock:
                    self._context["agent_responses"].append({
                        "timestamp": time.time(),
                        "length": int(match.group(1)),
                    })
                    self._context["last_activity"] = time.time()
                self._persist()
            return

        # Tool call completed
        if "run_agent: tool" in line and "completed" in line:
            match = re.search(r"tool (\w+) completed", line)
            if match:
                tool_name = match.group(1)
                with self._lock:
                    self._context["tool_calls"].append({
                        "timestamp": time.time(),
                        "tool": tool_name,
                    })
                    self._context["last_activity"] = time.time()
                self._persist()
                log.debug("LogWatcher: tool call captured: %s", tool_name)
            return

        # Tool failure
        if "tool" in line.lower() and ("failed" in line.lower() or "error" in line.lower()):
            with self._lock:
                self._context["failures"].append({
                    "timestamp": time.time(),
                    "line": line[:200],
                })
                self._context["last_activity"] = time.time()
            self._persist()
            return

        # LLM API call
        if "run_agent: API call" in line:
            match = re.search(r"API call #(\d+): model=(\S+)", line)
            if match:
                with self._lock:
                    self._context.setdefault("api_calls", []).append({
                        "timestamp": time.time(),
                        "number": int(match.group(1)),
                        "model": match.group(2),
                    })
                    self._context["last_activity"] = time.time()
                self._persist()
            return

    def _persist(self):
        """Write context to disk."""
        try:
            SESSION_CONTEXT_FILE.write_text(
                json.dumps(self._context, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            log.warning("LogWatcher persist failed: %s", e)

    def get_context(self) -> Dict:
        """Return current accumulated context."""
        with self._lock:
            return dict(self._context)

    def get_last_user_messages(self, n: int = 5) -> List[str]:
        """Get last N user messages."""
        with self._lock:
            msgs = self._context.get("user_messages", [])
            return [m["text"] for m in msgs[-n:]]

    def get_last_tools(self, n: int = 5) -> List[str]:
        """Get last N tool calls."""
        with self._lock:
            tools = self._context.get("tool_calls", [])
            return [t["tool"] for t in tools[-n:]]

    def get_failure_count(self, window_seconds: int = 60) -> int:
        """Count failures in last N seconds."""
        now = time.time()
        with self._lock:
            return sum(
                1 for f in self._context.get("failures", [])
                if now - f["timestamp"] <= window_seconds
            )

    def get_tool_sequence(self) -> List[str]:
        """Get full tool call sequence for pattern detection."""
        with self._lock:
            return [t["tool"] for t in self._context.get("tool_calls", [])]


# Singleton
_watcher_instance: Optional[LogWatcher] = None


def get_log_watcher() -> LogWatcher:
    """Get or create singleton LogWatcher."""
    global _watcher_instance
    if _watcher_instance is None:
        _watcher_instance = LogWatcher()
    return _watcher_instance
