"""WorldModel — vremenski, prognostički, kontekstualni model stvarnosti.

Za razliku od Pargod-a (graf problema→alata), WorldModel je:
- Vremenski: stanja se beleže kroz vreme (time-series)
- Prognostički: predviđa buduće stanje na osnovu trendova
- Kontekstualni: vezuje se za konkretne projekte, fajlove, sesije
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


class WorldModel:
    """Perzistentni time-series model sistema."""

    def __init__(self, db_path=None):
        self.db_path = str(db_path or PARGOD_DB)
        self._ensure_tables()

    def _conn(self):
        c = sqlite3.connect(self.db_path, timeout=5.0)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=3000")
        return c

    def _ensure_tables(self):
        """Schema se kreira iz schema.sql, ali proveri da li postoji."""
        with closing(self._conn()) as conn, conn:
            # Proveri da li world_snapshots postoji
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='world_snapshots'"
            ).fetchone()
            if not row:
                log.warning("world_snapshots table missing — run schema.sql first")

    # ── Snapshots ───────────────────────────────────────────────────

    def record_snapshot(self, tick: int, domain: str, state: Dict[str, Any],
                        entropy: float, anomaly_score: float = 0.0) -> int:
        """Zabeleži jedan snapshot sistema."""
        predicted = self._predict_entropy(domain, entropy)
        with closing(self._conn()) as conn, conn:
            cur = conn.execute(
                """INSERT INTO world_snapshots
                   (tick, timestamp, domain, state_json, entropy, predicted_entropy, anomaly_score)
                   VALUES (?,?,?,?,?,?,?)""",
                (tick, time.time(), domain, json.dumps(state, default=str),
                 entropy, predicted, anomaly_score),
            )
            return cur.lastrowid

    def get_recent_snapshots(self, domain: str = "system", minutes: int = 60,
                             limit: int = 100) -> List[Dict[str, Any]]:
        """Vrati poslednjih N snapshot-ova za domen."""
        cutoff = time.time() - minutes * 60
        with closing(self._conn()) as conn, conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT tick, timestamp, state_json, entropy, predicted_entropy, anomaly_score
                   FROM world_snapshots
                   WHERE domain=? AND timestamp > ?
                   ORDER BY timestamp DESC LIMIT ?""",
                (domain, cutoff, limit),
            ).fetchall()
            result = []
            for r in rows:
                try:
                    state = json.loads(r["state_json"])
                except Exception:
                    state = {}
                result.append({
                    "tick": r["tick"],
                    "timestamp": r["timestamp"],
                    "state": state,
                    "entropy": r["entropy"],
                    "predicted_entropy": r["predicted_entropy"],
                    "anomaly_score": r["anomaly_score"],
                })
            return result

    # ── Prediction ────────────────────────────────────────────────────

    def _predict_entropy(self, domain: str, current_entropy: float) -> Optional[float]:
        """Linearna regresija na poslednjih 20 snapshot-ova → predviđa entropy za sledeći tick."""
        snapshots = self.get_recent_snapshots(domain, minutes=30, limit=20)
        if len(snapshots) < 5:
            return None

        # Uzmi najstarije prvo
        snapshots = list(reversed(snapshots))
        n = len(snapshots)

        # Simple linear regression: y = a + bx
        sum_x = sum(i for i in range(n))
        sum_y = sum(s["entropy"] for s in snapshots)
        sum_xy = sum(i * s["entropy"] for i, s in enumerate(snapshots))
        sum_x2 = sum(i * i for i in range(n))

        denom = n * sum_x2 - sum_x * sum_x
        if denom == 0:
            return current_entropy

        slope = (n * sum_xy - sum_x * sum_y) / denom
        intercept = (sum_y - slope * sum_x) / n

        # Predviđaj za sledeći tick (x = n)
        predicted = intercept + slope * n
        return round(max(0.0, predicted), 2)

    def detect_anomaly(self, domain: str, current_entropy: float) -> float:
        """Vrati anomaly score (0.0-1.0) na osnovu devijacije od trenda."""
        snapshots = self.get_recent_snapshots(domain, minutes=30, limit=20)
        if len(snapshots) < 5:
            return 0.0

        entropies = [s["entropy"] for s in snapshots]
        mean = sum(entropies) / len(entropies)
        variance = sum((e - mean) ** 2 for e in entropies) / len(entropies)
        std_dev = variance ** 0.5

        if std_dev == 0:
            return 0.0

        z_score = abs(current_entropy - mean) / std_dev
        # Mapiraj z-score na 0-1 (z=3 → 1.0)
        return round(min(1.0, z_score / 3.0), 2)

    # ── Anticipated Events ─────────────────────────────────────────────

    def anticipate_event(self, domain: str, event_type: str,
                         probability: float, predicted_for: float) -> int:
        """Zabeleži predviđeni događaj."""
        with closing(self._conn()) as conn, conn:
            cur = conn.execute(
                """INSERT INTO anticipated_events
                   (domain, event_type, probability, predicted_at, predicted_for)
                   VALUES (?,?,?,?,?)""",
                (domain, event_type, probability, time.time(), predicted_for),
            )
            return cur.lastrowid

    def get_pending_anticipations(self, domain: str = "system") -> List[Dict[str, Any]]:
        """Vrati anticipacije koje još nisu triggerovane a vreme je prošlo."""
        now = time.time()
        with closing(self._conn()) as conn, conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT id, event_type, probability, predicted_for
                   FROM anticipated_events
                   WHERE domain=? AND triggered=0 AND predicted_for <= ?
                   ORDER BY probability DESC""",
                (domain, now),
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_triggered(self, event_id: int, prevented_by: str = ""):
        """Označi da se događaj desio ili sprečio."""
        with closing(self._conn()) as conn, conn:
            conn.execute(
                "UPDATE anticipated_events SET triggered=1, prevented_by=? WHERE id=?",
                (prevented_by, event_id),
            )

    # ── Domain Attachments (Ciel emotion) ───────────────────────────

    def get_attachment(self, domain: str) -> Dict[str, Any]:
        """Vrati attachment/emotional weight za domen."""
        with closing(self._conn()) as conn, conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM domain_attachments WHERE domain=?", (domain,)
            ).fetchone()
            if row:
                return dict(row)
            return {
                "domain": domain,
                "priority": 0.5,
                "health_score": 1.0,
                "failure_streak": 0,
                "concern_level": 0.3,
            }

    def record_domain_outcome(self, domain: str, success: bool):
        """Ažuriraj health_score i failure_streak nakon akcije."""
        now = time.time()
        with closing(self._conn()) as conn, conn:
            existing = conn.execute(
                "SELECT failure_streak, last_success, last_failure FROM domain_attachments WHERE domain=?",
                (domain,),
            ).fetchone()

            if not existing:
                conn.execute(
                    """INSERT INTO domain_attachments
                       (domain, priority, health_score, last_success, last_failure, failure_streak, concern_level)
                       VALUES (?,?,?,?,?,?,?)""",
                    (domain, 0.5, 1.0, now if success else None, None if success else now,
                     0 if success else 1, 0.3),
                )
                return

            streak = existing[0] if existing[0] else 0
            if success:
                new_streak = 0
                new_health = min(1.0, 0.7 + 0.3)  # recovery
            else:
                new_streak = streak + 1
                new_health = max(0.0, 1.0 - new_streak * 0.2)

            concern = 0.3
            if new_streak > 2:
                concern = 0.9
            elif new_health < 0.5:
                concern = 0.8

            conn.execute(
                """UPDATE domain_attachments SET
                   health_score=?, last_success=?, last_failure=?,
                   failure_streak=?, concern_level=?, updated_at=?
                   WHERE domain=?""",
                (new_health, now if success else existing[1],
                 None if success else now, new_streak, concern, now, domain),
            )

    # ── Cross-domain query ───────────────────────────────────────────────

    def get_summary(self, domain: str = "system") -> Dict[str, Any]:
        """Kompaktni summary za @1Hz loop logging."""
        snapshots = self.get_recent_snapshots(domain, minutes=10, limit=5)
        attachment = self.get_attachment(domain)
        pending = self.get_pending_anticipations(domain)

        avg_entropy = sum(s["entropy"] for s in snapshots) / len(snapshots) if snapshots else 0.0
        latest = snapshots[0] if snapshots else None

        return {
            "domain": domain,
            "snapshots_count": len(snapshots),
            "avg_entropy_10min": round(avg_entropy, 2),
            "predicted_entropy": latest["predicted_entropy"] if latest else None,
            "anomaly_score": latest["anomaly_score"] if latest else 0.0,
            "concern_level": attachment.get("concern_level", 0.3),
            "health_score": attachment.get("health_score", 1.0),
            "pending_anticipations": len(pending),
        }
