"""ProactiveSuggester — anticipatorne sugestije bazirane na WorldModel-u.

Flow:
1. @1Hz loop poziva check_and_suggest(snapshot)
2. Upori metrike iz snapshot-a sa suggestion rules
3. Rate-limit: max 1 sugestija / 5 min, ne ponavljaj istu u 30 min
4. Generiši prirodnojezičku poruku
5. Beleži u suggestion_log
6. Uči iz korisnikove reakcije (ignorisanje → smanji confidence)
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import PARGOD_DB

log = logging.getLogger("nucleus")

# Rate limits
_MIN_SECONDS_BETWEEN_SUGGESTIONS = 300   # 5 min
_MIN_SECONDS_REPEAT_SAME = 1800          # 30 min
_MAX_SUGGESTIONS_PER_HOUR = 6
_PENDING_SUGGESTIONS_FILE = Path.home() / ".hermes" / "nucleus_data" / "pending_suggestions.json"

# Default suggestion rules — seeded on first run
_DEFAULT_RULES = [
    {
        "category": "disk",
        "severity": "warning",
        "condition": {"metric": "disk_usage_percent", "operator": ">=", "threshold": 85},
        "message": "Disk je {value}% pun. Preporučujem čišćenje logova ili tmp fajlova.",
        "action": " journalctl --vacuum-time=3d && rm -rf /tmp/* 2>/dev/null; df -h",
        "confidence": 0.85,
    },
    {
        "category": "disk",
        "severity": "critical",
        "condition": {"metric": "disk_usage_percent", "operator": ">=", "threshold": 95},
        "message": "KRITIČNO: Disk je {value}% pun! Odmah oslobodi prostor ili servisi mogu pasti.",
        "action": "du -sh /var/log/* /tmp/* 2>/dev/null | sort -rh | head -10",
        "confidence": 0.95,
    },
    {
        "category": "memory",
        "severity": "warning",
        "condition": {"metric": "ram_usage_percent", "operator": ">=", "threshold": 85},
        "message": "RAM je {value}% zauzet. Moguć OOM kill uskoro ako nastavi da raste.",
        "action": "ps aux --sort=-%mem | head -10",
        "confidence": 0.80,
    },
    {
        "category": "memory",
        "severity": "critical",
        "condition": {"metric": "swap_usage_percent", "operator": ">=", "threshold": 50},
        "message": "Swap je {value}% pun — sistem thrashuje. Ubij najveće procese ili dodaj RAM.",
        "action": "ps aux --sort=-%mem | head -5",
        "confidence": 0.90,
    },
    {
        "category": "cpu",
        "severity": "warning",
        "condition": {"metric": "cpu_percent", "operator": ">=", "threshold": 80},
        "message": "CPU je {value}% zauzet duže vreme. Proveri da li neki proces loop-uje.",
        "action": "ps aux --sort=-%cpu | head -10",
        "confidence": 0.75,
    },
    {
        "category": "maintenance",
        "severity": "info",
        "condition": {"metric": "entropy", "operator": ">=", "threshold": 0.7},
        "message": "Sistemska entropija je {value:.2f} — haotično stanje. Preporučujem kratak pregled logova.",
        "action": "tail -n 20 ~/.hermes/logs/hermes.log",
        "confidence": 0.70,
    },
    {
        "category": "network",
        "severity": "warning",
        "condition": {"metric": "connections_established", "operator": ">=", "threshold": 500},
        "message": "{value} aktivnih konekcija — moguć port exhaustion ako raste.",
        "action": "ss -s",
        "confidence": 0.75,
    },
    {
        "category": "security",
        "severity": "warning",
        "condition": {"metric": "zombie_processes", "operator": ">=", "threshold": 5},
        "message": "{value} zombie procesa — roditeljski procesi ne čistaju PID-ove.",
        "action": "ps aux | awk '$8==\"Z\"' | head -10",
        "confidence": 0.80,
    },
    {
        "category": "maintenance",
        "severity": "info",
        "condition": {"metric": "days_since_reboot", "operator": ">=", "threshold": 30},
        "message": "Sistem je uptime {value} dana. Razmisli o restartu za kernel/security update.",
        "action": "needs-restart -r 2>/dev/null || echo 'Proveri rucno: pacman -Qu | grep linux'",
        "confidence": 0.65,
    },
]


class ProactiveSuggester:
    """Generiše anticipatorne sugestije bazirane na sistemskim metrikama."""

    def __init__(self, db_path=None):
        self.db_path = str(db_path or PARGOD_DB)
        self._seed_rules()

    def _conn(self):
        c = sqlite3.connect(self.db_path, timeout=5.0)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=3000")
        return c

    def _seed_rules(self):
        """Insert default rules if table empty."""
        with closing(self._conn()) as conn, conn:
            count = conn.execute("SELECT COUNT(*) FROM suggestions").fetchone()[0]
            if count > 0:
                return
            now = time.time()
            for rule in _DEFAULT_RULES:
                conn.execute(
                    """INSERT INTO suggestions
                       (category, severity, condition_trigger, message, suggested_action, confidence, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        rule["category"],
                        rule["severity"],
                        json.dumps(rule["condition"]),
                        rule["message"],
                        rule["action"],
                        rule["confidence"],
                        now,
                        now,
                    ),
                )
            log.info("Seeded %d proactive suggestion rules", len(_DEFAULT_RULES))

    # ── Core logic ────────────────────────────────────────────────────────────────

    def check_and_suggest(self, snapshot: dict) -> Optional[Dict[str, Any]]:
        """
        Proveri snapshot metrike i vrati sugestiju ako je potrebno.
        Vrati None ako nema sugestije (rate limited, nema match, ili nije važno).
        """
        if not snapshot:
            return None

        # Rate limit: proveri kad je poslednja sugestija poslata
        if self._is_rate_limited():
            return None

        # Učitaj sve active rules
        rules = self._get_active_rules()

        for rule in rules:
            match = self._evaluate_condition(rule["condition"], snapshot)
            if match is not None:
                # Proveri da li je ista sugestija već poslata skoro
                if self._recently_shown(rule["id"]):
                    continue

                # Format message
                msg = rule["message"].format(value=match)
                suggestion = {
                    "id": rule["id"],
                    "category": rule["category"],
                    "severity": rule["severity"],
                    "message": msg,
                    "action": rule["suggested_action"],
                    "confidence": rule["confidence"],
                    "raw_value": match,
                }

                # Log that we’re about to show it
                self._log_suggestion(rule["id"], msg)
                self._bump_shown(rule["id"])

                log.info("PROACTIVE [%s] %s (conf=%.2f)", rule["category"], msg[:60], rule["confidence"])
                return suggestion

        return None

    def _evaluate_condition(self, condition: dict, snapshot: dict) -> Any:
        """
        Evaluiraj jednu uslovnu pravilo protiv snapshot-a.
        Vrati metriku vrednost ako je uslov zadovoljen, inače None.
        """
        metric = condition.get("metric")
        op = condition.get("operator", ">=")
        threshold = condition.get("threshold")

        if metric not in snapshot:
            return None

        value = snapshot[metric]
        try:
            value = float(value)
            threshold = float(threshold)
        except (TypeError, ValueError):
            return None

        ops = {
            ">": lambda a, b: a > b,
            ">=": lambda a, b: a >= b,
            "<": lambda a, b: a < b,
            "<=": lambda a, b: a <= b,
            "==": lambda a, b: a == b,
            "!=": lambda a, b: a != b,
        }

        if op not in ops:
            return None

        if ops[op](value, threshold):
            return value
        return None

    # ── Rate limiting ──────────────────────────────────────────────────────────────

    def _is_rate_limited(self) -> bool:
        """Proveri global rate limit (poslednjih 5 min i 6/h)."""
        now = time.time()
        with closing(self._conn()) as conn, conn:
            # Poslednja sugestija
            row = conn.execute(
                """SELECT MAX(sent_at) FROM suggestion_log"""
            ).fetchone()
            if row and row[0] and (now - row[0]) < _MIN_SECONDS_BETWEEN_SUGGESTIONS:
                return True

            # Count u poslednjih sat vremena
            hour_ago = now - 3600
            cnt = conn.execute(
                """SELECT COUNT(*) FROM suggestion_log WHERE sent_at > ?""", (hour_ago,)
            ).fetchone()[0]
            return cnt >= _MAX_SUGGESTIONS_PER_HOUR

    def _recently_shown(self, suggestion_id: int) -> bool:
        """Proveri da li je ista sugestija poslata u poslednjih 30 min."""
        cutoff = time.time() - _MIN_SECONDS_REPEAT_SAME
        with closing(self._conn()) as conn, conn:
            row = conn.execute(
                """SELECT MAX(sent_at) FROM suggestion_log WHERE suggestion_id=?""",
                (suggestion_id,),
            ).fetchone()
            return row and row[0] and row[0] > cutoff

    def _get_active_rules(self) -> List[Dict[str, Any]]:
        """Vrati aktivna pravila sortirana po severity/confidence."""
        with closing(self._conn()) as conn, conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT id, category, severity, condition_trigger, message, suggested_action, confidence
                   FROM suggestions
                   WHERE auto_resolve=0 OR (auto_resolve=1 AND user_reaction IS NULL)
                   ORDER BY
                     CASE severity
                       WHEN 'critical' THEN 1
                       WHEN 'warning' THEN 2
                       WHEN 'info' THEN 3
                       ELSE 4
                     END,
                     confidence DESC"""
            ).fetchall()
            results = []
            for r in rows:
                d = dict(r)
                d["condition"] = json.loads(d.pop("condition_trigger", "{}"))
                results.append(d)
            return results

    def _log_suggestion(self, suggestion_id: int, message: str):
        with closing(self._conn()) as conn, conn:
            conn.execute(
                """INSERT INTO suggestion_log (suggestion_id, message, sent_at)
                   VALUES (?,?,?)""",
                (suggestion_id, message, time.time()),
            )

    def _bump_shown(self, suggestion_id: int):
        with closing(self._conn()) as conn, conn:
            conn.execute(
                """UPDATE suggestions SET times_shown=times_shown+1, last_shown=? WHERE id=?""",
                (time.time(), suggestion_id),
            )

    # ── Learning from user reactions ──────────────────────────────────────────────────────────────

    def record_user_response(self, session_id: str, user_message: str) -> Optional[str]:
        """
        Analiziraj korisnikovu poruku kao reakciju na poslednju sugestiju.
        Vrati log string ako je matchovan.
        """
        msg_lower = user_message.lower().strip()

        # Match na osnovu ključnih reči
        acted = any(w in msg_lower for w in ("ok", "uradi", "radi", "do it", "go", "sve je ok", "hvala", "tačno"))
        dismissed = any(w in msg_lower for w in ("ne", "ignoriši", "dismiss", "ne treba", "nema veze", "preskoči"))

        if not (acted or dismissed):
            return None

        # Nađi poslednju sugestiju za ovu sesiju
        with closing(self._conn()) as conn, conn:
            row = conn.execute(
                """SELECT id, suggestion_id, message FROM suggestion_log
                   WHERE session_id=? OR session_id IS NULL
                   ORDER BY sent_at DESC LIMIT 1""",
                (session_id,),
            ).fetchone()

            if not row:
                return None

            log_id, suggestion_id, sugg_msg = row
            reaction = "acted" if acted else "dismissed"

            # Update suggestion_log
            conn.execute(
                """UPDATE suggestion_log SET user_response=?, response_type=? WHERE id=?""",
                (user_message, reaction, log_id),
            )

            # Update suggestion stats
            if reaction == "acted":
                conn.execute(
                    """UPDATE suggestions SET times_acted=times_acted+1, user_reaction=? WHERE id=?""",
                    (reaction, suggestion_id),
                )
            else:
                conn.execute(
                    """UPDATE suggestions SET times_ignored=times_ignored+1, user_reaction=?, confidence=max(0.1, confidence-0.05) WHERE id=?""",
                    (reaction, suggestion_id),
                )

        log.info("PROACTIVE feedback: suggestion %d → %s", suggestion_id, reaction)
        return f"Suggestion {suggestion_id} marked as {reaction}"

    # ── Stats & admin ─────────────────────────────────────────────────────────────────────────

    def _write_pending_suggestion(self, suggestion: dict):
        try:
            data = {
                "timestamp": time.time(),
                "suggestion": suggestion,
                "delivered": False,
            }
            _PENDING_SUGGESTIONS_FILE.write_text(json.dumps(data, default=str), encoding="utf-8")
        except Exception:
            pass

    def read_and_clear_pending(self) -> Optional[dict]:
        try:
            if not _PENDING_SUGGESTIONS_FILE.exists():
                return None
            data = json.loads(_PENDING_SUGGESTIONS_FILE.read_text(encoding="utf-8"))
            if data.get("delivered"):
                return None
            if time.time() - data.get("timestamp", 0) > 300:
                return None
            data["delivered"] = True
            _PENDING_SUGGESTIONS_FILE.write_text(json.dumps(data, default=str), encoding="utf-8")
            return data.get("suggestion")
        except Exception:
            return None

    def get_stats(self) -> Dict[str, Any]:
        with closing(self._conn()) as conn, conn:
            total = conn.execute("SELECT COUNT(*) FROM suggestions").fetchone()[0]
            shown = conn.execute("SELECT SUM(times_shown) FROM suggestions").fetchone()[0] or 0
            acted = conn.execute("SELECT SUM(times_acted) FROM suggestions").fetchone()[0] or 0
            ignored = conn.execute("SELECT SUM(times_ignored) FROM suggestions").fetchone()[0] or 0
            last_hour = conn.execute(
                """SELECT COUNT(*) FROM suggestion_log WHERE sent_at > ?""",
                (time.time() - 3600,),
            ).fetchone()[0]
        return {
            "total_rules": total,
            "total_shown": shown,
            "times_acted": acted,
            "times_ignored": ignored,
            "last_hour": last_hour,
            "effectiveness": round(acted / max(1, shown), 3),
        }

    def add_rule(self, category: str, severity: str, condition: dict,
                 message: str, action: str = "", confidence: float = 0.8) -> int:
        """Dinamičko dodavanje pravila (iz koda ili CLI)."""
        now = time.time()
        with closing(self._conn()) as conn, conn:
            cur = conn.execute(
                """INSERT INTO suggestions
                   (category, severity, condition_trigger, message, suggested_action, confidence, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (category, severity, json.dumps(condition), message, action, confidence, now, now),
            )
            return cur.lastrowid
