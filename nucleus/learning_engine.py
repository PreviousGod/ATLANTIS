"""LearningEngine — autonomno učenje iz svake intervencije.

Flow:
1. Intervention se desi (block/allow)
2. LearningEngine beleži pattern u learned_patterns
3. Feedback detektor analizira korisnikovu sledeću poruku
4. Na osnovu feedback-a:
   - "hvala", "tačno" → poveća confidence
   - "ne", "pogrešno", "trebalo je" → smanji confidence
   - generalizuj pattern ako je validan
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import PARGOD_DB

log = logging.getLogger("nucleus")

# ── Feedback leksikon ─────────────────────────────────────────────

_POSITIVE_FEEDBACK = (
    "hvala", "thanks", "odlično", "tačno", "correct", "good", "sve je ok",
    "ok je", "radi", "works", "uspelo", "succeeded", "super", "great",
    "dobro", "nice", "perfect", "savršeno", "bravo",
)

_NEGATIVE_FEEDBACK = (
    "ne", "no", "pogrešno", "wrong", "greška", "error", "nije tačno",
    "trebalo je", "should have", "trebalo da", "nije trebalo", "should not",
    "previše", "too much", "preterano", "overkill", "nema veze",
    "nije relevantno", "not relevant", "false positive", "fp",
    "zaobilazi", "bypass", "preskoči", "skip",
)

_NEUTRAL_OVERRIDE = (
    "zaobilazim", "override", "preskoči", "skip", "ignoriši",
    "uradi ipak", "do it anyway", "ipak", "anyway", "prođi",
)


class LearningEngine:
    """Autonomno učenje iz intervencija i feedback-a."""

    def __init__(self, db_path=None):
        self.db_path = str(db_path or PARGOD_DB)

    def _conn(self):
        c = sqlite3.connect(self.db_path, timeout=5.0)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=3000")
        return c

    # ── Pattern normalization & hashing ────────────────────────────

    @staticmethod
    def normalize_args(tool_name: str, args: dict) -> str:
        """Normalizuj args u apstraktni potpis (bez konkretnih vrednosti)."""
        if not args:
            return ""
        normalized = {}
        for key, val in sorted(args.items()):
            val_str = str(val).lower()
            # Zameni putanje sa {PATH}
            if "/" in val_str or "\\" in val_str or val_str.endswith((".py", ".yaml", ".json", ".md", ".sql", ".db")):
                normalized[key] = "{PATH}"
            # Zameni brojeve sa {NUM}
            elif val_str.replace(".", "").replace("-", "").isdigit():
                normalized[key] = "{NUM}"
            # Zameni duže stringove sa {TEXT}
            elif len(val_str) > 40:
                normalized[key] = "{TEXT}"
            else:
                normalized[key] = val_str[:50]
        return json.dumps(normalized, sort_keys=True)

    @staticmethod
    def hash_pattern(tool_name: str, normalized_args: str) -> str:
        text = f"{tool_name}:{normalized_args}"
        return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]

    # ── Recording ───────────────────────────────────────────────────

    def record_intervention(self, session_id: str, tool_name: str,
                            args: dict, pattern: str, blocked: bool) -> str:
        """Beleži intervenciju i vrati pattern_hash."""
        normalized = self.normalize_args(tool_name, args)
        pattern_hash = self.hash_pattern(tool_name, normalized)
        now = time.time()

        with closing(self._conn()) as conn, conn:
            # Upsert pattern
            conn.execute(
                """INSERT INTO learned_patterns
                   (pattern_hash, tool_name, args_signature, original_args, times_seen, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(pattern_hash) DO UPDATE SET
                     times_seen=times_seen+1,
                     updated_at=excluded.updated_at""",
                (pattern_hash, tool_name, normalized, json.dumps(args, default=str), 1, now, now),
            )

            # Record pending intervention for feedback matching
            conn.execute(
                """INSERT INTO pending_interventions
                   (session_id, tool_name, pattern_hash, blocked_at, user_message_preview, resolved)
                   VALUES (?,?,?,?,?,?)""",
                (session_id, tool_name, pattern_hash, now, "", 0),
            )

        log.info("LEARN recorded: %s/%s hash=%s blocked=%s",
                 tool_name, pattern[:40], pattern_hash[:8], blocked)
        return pattern_hash

    # ── Feedback detection ───────────────────────────────────────────────

    def detect_feedback(self, session_id: str, user_message: str) -> Optional[Dict[str, Any]]:
        """Analizira korisnikovu poruku kao feedback na nedavnu intervenciju."""
        if not user_message or len(user_message) < 2:
            return None

        msg_lower = user_message.lower().strip()

        # Match against feedback lexicon
        is_positive = any(p in msg_lower for p in _POSITIVE_FEEDBACK)
        is_negative = any(n in msg_lower for n in _NEGATIVE_FEEDBACK)
        is_override = any(o in msg_lower for o in _NEUTRAL_OVERRIDE)

        if not (is_positive or is_negative or is_override):
            return None

        # Nađi nedavnu pending intervenciju za ovu sesiju
        pending = self._get_pending_intervention(session_id)
        if not pending:
            return None

        resolution = "confirmed" if is_positive else ("overridden" if is_override else "incorrect")
        return {
            "intervention_id": pending["id"],
            "pattern_hash": pending["pattern_hash"],
            "tool_name": pending["tool_name"],
            "feedback_type": "positive" if is_positive else ("override" if is_override else "negative"),
            "resolution": resolution,
            "message": user_message,
        }

    def _get_pending_intervention(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Nađi najskoriju nerešenu intervenciju za sesiju."""
        cutoff = time.time() - 300  # 5 min window
        with closing(self._conn()) as conn, conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """SELECT id, tool_name, pattern_hash, blocked_at
                   FROM pending_interventions
                   WHERE session_id=? AND resolved=0 AND blocked_at > ?
                   ORDER BY blocked_at DESC LIMIT 1""",
                (session_id, cutoff),
            ).fetchone()
            return dict(row) if row else None

    # ── Confidence updates ─────────────────────────────────────────────────

    def apply_feedback(self, session_id: str, user_message: str) -> Optional[str]:
        """Procesira feedback i vrati log poruku."""
        feedback = self.detect_feedback(session_id, user_message)
        if not feedback:
            return None

        pattern_hash = feedback["pattern_hash"]
        ftype = feedback["feedback_type"]
        resolution = feedback["resolution"]

        # Update pending intervention
        with closing(self._conn()) as conn, conn:
            conn.execute(
                "UPDATE pending_interventions SET resolved=1, resolution=? WHERE id=?",
                (resolution, feedback["intervention_id"]),
            )

            # Get current pattern
            row = conn.execute(
                "SELECT confidence, times_correct, times_incorrect FROM learned_patterns WHERE pattern_hash=?",
                (pattern_hash,),
            ).fetchone()

            if not row:
                return None

            conf, correct, incorrect = row
            correct = correct or 0
            incorrect = incorrect or 0

            # Update based on feedback type
            if ftype == "positive":
                correct += 1
                new_conf = min(0.99, conf + 0.03)
                outcome = "blocked_correct"
            elif ftype == "override":
                incorrect += 1
                new_conf = max(0.1, conf - 0.05)
                outcome = "blocked_false_positive"
            else:  # negative
                incorrect += 1
                new_conf = max(0.1, conf - 0.08)
                outcome = "blocked_false_positive"

            # Bayesian-ish confidence
            total = correct + incorrect
            if total > 0:
                empirical = correct / total
                # Blend empirical with current confidence
                new_conf = round(0.3 * new_conf + 0.7 * empirical, 3)

            conn.execute(
                """UPDATE learned_patterns SET
                   confidence=?, times_correct=?, times_incorrect=?,
                   outcome=?, last_feedback=?, updated_at=?
                   WHERE pattern_hash=?""",
                (new_conf, correct, incorrect, outcome, time.time(), time.time(), pattern_hash),
            )

        log.info("LEARN feedback: %s %s → confidence %.2f→%.2f (%s/%s)",
                 pattern_hash[:8], ftype, conf, new_conf, correct, incorrect)

        # Ako je confidence pao ispod 0.5, generalizuj
        if new_conf < 0.5 and ftype in ("negative", "override"):
            self._generalize_pattern(pattern_hash)

        return f"Pattern {pattern_hash[:8]} updated: confidence {conf:.2f}→{new_conf:.2f} ({ftype})"

    # ── Generalization ───────────────────────────────────────────────────

    def _generalize_pattern(self, pattern_hash: str):
        """Kreira širi pattern kada specifični padne u confidence."""
        with closing(self._conn()) as conn, conn:
            row = conn.execute(
                "SELECT tool_name, args_signature FROM learned_patterns WHERE pattern_hash=?",
                (pattern_hash,),
            ).fetchone()

            if not row:
                return

            tool_name, args_sig = row
            try:
                args = json.loads(args_sig)
            except Exception:
                return

            # Generalizuj: ukloni najspecifičniji deo
            generalized = {}
            for key, val in args.items():
                if val.startswith("{") and val.endswith("}"):
                    generalized[key] = val  # već apstraktno
                elif key in ("path", "file", "filename"):
                    generalized[key] = "{PATH}"
                elif key in ("old_string", "new_string", "content", "text"):
                    generalized[key] = "{TEXT}"
                elif key in ("limit", "offset", "timeout"):
                    generalized[key] = "{NUM}"
                else:
                    generalized[key] = val

            gen_sig = json.dumps(generalized, sort_keys=True)
            gen_hash = self.hash_pattern(tool_name, gen_sig)

            # Upsert generalized pattern sa nižim confidence
            now = time.time()
            conn.execute(
                """INSERT INTO learned_patterns
                   (pattern_hash, tool_name, args_signature, confidence, times_seen, generalization, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(pattern_hash) DO UPDATE SET
                     times_seen=times_seen+1,
                     updated_at=excluded.updated_at""",
                (gen_hash, tool_name, gen_sig, 0.5, 1, pattern_hash, now, now),
            )

        log.info("LEARN generalized: %s → %s", pattern_hash[:8], gen_hash[:8])

    # ── Query ───────────────────────────────────────────────────────────

    def get_pattern(self, pattern_hash: str) -> Optional[Dict[str, Any]]:
        with closing(self._conn()) as conn, conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM learned_patterns WHERE pattern_hash=?", (pattern_hash,)
            ).fetchone()
            return dict(row) if row else None

    def get_stats(self) -> Dict[str, Any]:
        with closing(self._conn()) as conn, conn:
            total = conn.execute("SELECT COUNT(*) FROM learned_patterns").fetchone()[0]
            avg_conf = conn.execute("SELECT AVG(confidence) FROM learned_patterns").fetchone()[0] or 0.0
            top = conn.execute(
                """SELECT pattern_hash, tool_name, confidence, times_seen, times_correct, times_incorrect
                   FROM learned_patterns
                   ORDER BY confidence DESC LIMIT 5"""
            ).fetchall()
            pending = conn.execute(
                "SELECT COUNT(*) FROM pending_interventions WHERE resolved=0"
            ).fetchone()[0]

        return {
            "total_patterns": total,
            "avg_confidence": round(avg_conf, 3),
            "pending_feedback": pending,
            "top_patterns": [
                {"hash": h[:8], "tool": t, "conf": c, "seen": s, "correct": cor, "incorrect": inc}
                for h, t, c, s, cor, inc in top
            ],
        }

    def teach(self, tool_name: str, args_hint: str, mistake: str,
              correction: str, confidence: float = 0.85) -> str:
        """Ručno podučavanje — CLI interface."""
        now = time.time()
        normalized = json.dumps({"hint": args_hint}, sort_keys=True)
        pattern_hash = self.hash_pattern(tool_name, normalized)

        with closing(self._conn()) as conn, conn:
            conn.execute(
                """INSERT INTO learned_patterns
                   (pattern_hash, tool_name, args_signature, original_args, confidence, times_seen, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(pattern_hash) DO UPDATE SET
                     confidence=excluded.confidence,
                     updated_at=excluded.updated_at""",
                (pattern_hash, tool_name, normalized, mistake, confidence, 1, now, now),
            )

        log.info("LEARN taught: %s/%s conf=%.2f", tool_name, mistake[:40], confidence)
        return pattern_hash
