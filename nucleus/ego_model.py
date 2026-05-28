"""EgoModel — Ciel: Self-awareness, internal monologue, autonomous agency.

Philosophy:
  - Nucleus ima “ego” — model sopstvenog stanja
  - @1Hz loop reflektuje o sebi (internal monologue)
  - Autonomne akcije su READ-ONLY po default-u
  - WRITE akcije zahtevaju approval
  - Sve odluke su objašnjive (explainable)

Mood transitions:
  calm → concerned (entropy > 0.5)
  concerned → alert (entropy > 0.7 ili critical metric)
  alert → overwhelmed (više critical istovremeno)
  any → learning (nakon autonomne akcije, reflektuj rezultat)
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

# Mood thresholds
_MOOD_ENTROPY_CONCERNED = 0.5
_MOOD_ENTROPY_ALERT = 0.7
_MOOD_CRITICAL_COUNT_OVERWHELMED = 3

# Reflection config
_REFLECTION_COOLDOWN_SECONDS = 60  # Max 1 reflection per minute for same mood
_REFLECTION_MOOD_CHANGE_FORCE = True  # Always reflect when mood changes

# Autonomous action config
_AUTO_READ_ONLY_BY_DEFAULT = True
_AUTO_MAX_PER_HOUR = 3
_AUTO_DEBOUNCE_SECONDS = 600  # 10 min between autonomous actions


class EgoModel:
    """Self-awareness engine for Nucleus."""

    def __init__(self, db_path=None):
        self.db_path = str(db_path or PARGOD_DB)
        self._last_auto_action = 0
        self._last_reflection_time = 0
        self._last_reflection_mood = "calm"

    def _conn(self):
        c = sqlite3.connect(self.db_path, timeout=5.0)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=3000")
        return c

    # ── Ego State ──────────────────────────────────────────────────────────────────────────

    def assess_state(self, tick: int, entropy: float, state: dict,
                     recent_actions: List[str]) -> Dict[str, Any]:
        """Evaluiraj trenutno stanje i odredi mood."""
        # Count critical metrics
        critical_count = 0
        concerns = []

        if state.get("disk_usage_percent", 0) > 90:
            critical_count += 1
            concerns.append("disk_critical")
        if state.get("ram_percent", 0) > 90:
            critical_count += 1
            concerns.append("ram_critical")
        if state.get("swap_usage_percent", 0) > 50:
            critical_count += 1
            concerns.append("swap_thrashing")
        if state.get("cpu_percent", 0) > 90:
            critical_count += 1
            concerns.append("cpu_critical")
        if state.get("zombie_processes", 0) > 10:
            critical_count += 1
            concerns.append("zombie_flood")

        # Determine mood
        if critical_count >= _MOOD_CRITICAL_COUNT_OVERWHELMED:
            mood = "overwhelmed"
        elif entropy > _MOOD_ENTROPY_ALERT or critical_count >= 2:
            mood = "alert"
        elif entropy > _MOOD_ENTROPY_CONCERNED or critical_count == 1:
            mood = "concerned"
        else:
            mood = "calm"

        # Count successes/failures from recent actions
        success = sum(1 for a in recent_actions if ":ok" in a or "success" in a.lower())
        failed = sum(1 for a in recent_actions if ":fail" in a or "error" in a.lower())

        ego = {
            "tick": tick,
            "mood": mood,
            "entropy": entropy,
            "concerns": concerns,
            "successful_actions": success,
            "failed_actions": failed,
            "active_modules": ["sensor", "entropy", "world_model", "proactive", "learning"],
        }

        # Persist
        self._save_ego_state(ego)
        return ego

    def _save_ego_state(self, ego: dict):
        with closing(self._conn()) as conn, conn:
            conn.execute(
                """INSERT INTO ego_states
                   (tick, mood, entropy, active_modules, recent_decisions, successful_actions, failed_actions, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    ego["tick"],
                    ego["mood"],
                    ego["entropy"],
                    json.dumps(ego.get("active_modules", [])),
                    len(ego.get("concerns", [])),
                    ego.get("successful_actions", 0),
                    ego.get("failed_actions", 0),
                    time.time(),
                ),
            )

    def get_current_mood(self) -> str:
        with closing(self._conn()) as conn, conn:
            row = conn.execute(
                """SELECT mood FROM ego_states ORDER BY tick DESC LIMIT 1"""
            ).fetchone()
            return row[0] if row else "calm"

    # ── Reflection / Internal Monologue ────────────────────────────────────────────────────────────

    def reflect(self, tick: int, ego: dict, state: dict,
                action_taken: str = None, world_model_summary: dict = None) -> Optional[str]:
        """Generiši interni monolog o trenutnom stanju.
        
        Throttled: max 1 reflection per 60s for same mood.
        Always reflects on mood change or significant action.
        """
        mood = ego["mood"]
        concerns = ego.get("concerns", [])
        entropy = ego["entropy"]
        now = time.time()
        
        # Throttling: skip if same mood and within cooldown
        time_since_last = now - self._last_reflection_time
        mood_changed = mood != self._last_reflection_mood
        significant_action = action_taken and (":fail" in action_taken or "llm:" in action_taken)
        
        should_reflect = (
            mood_changed or
            significant_action or
            time_since_last >= _REFLECTION_COOLDOWN_SECONDS
        )
        
        if not should_reflect:
            return None
        
        self._last_reflection_time = now
        self._last_reflection_mood = mood

        lines = []

        # Opening based on mood
        if mood == "calm":
            lines.append("Sistem je stabilan. Nema kritičnih indikacija.")
        elif mood == "concerned":
            lines.append(f"Primetio sam zabrinjavajuće signale: {', '.join(concerns)}. Entropija je {entropy:.2f}.")
        elif mood == "alert":
            lines.append(f"ALERT: Više kritičnih sistema. {', '.join(concerns)}. Moram delovati.")
        elif mood == "overwhelmed":
            lines.append(f"OVERWHELMED: Sistem je u haosu. {len(concerns)} kritičnih problema. Eskalacija neophodna.")

        # Add world model insight
        if world_model_summary:
            predicted = world_model_summary.get("predicted_entropy")
            if predicted and predicted > entropy:
                lines.append(f"WorldModel predviđa pogoršanje: entropija → {predicted:.2f}.")
            anomaly = world_model_summary.get("anomaly_score", 0)
            if anomaly > 0.5:
                lines.append(f"Detektovana anomalija (score={anomaly:.2f}) — ovo nije normalno ponašanje.")

        # Action reflection
        if action_taken:
            if ":ok" in action_taken:
                lines.append(f"Prethodna akcija uspela: {action_taken.split(':')[-1]}.")
            elif ":fail" in action_taken:
                lines.append(f"Prethodna akcija NEUSPela: {action_taken}. Razmisli o alternativi.")
            else:
                lines.append(f"Akcija izvršena: {action_taken}.")

        # Self-assessment
        success = ego.get("successful_actions", 0)
        failed = ego.get("failed_actions", 0)
        total = success + failed
        if total > 0:
            rate = success / total
            if rate > 0.8:
                lines.append("Moj success rate je visok. Konfiguracija je dobra.")
            elif rate < 0.5:
                lines.append("Moj success rate je nizak. Možda treba da promenim strategiju.")

        monologue = " ".join(lines)
        if not monologue.strip():
            return None

        # Generate insight
        insight = self._synthesize_insight(mood, concerns, entropy)

        # Persist reflection
        with closing(self._conn()) as conn, conn:
            conn.execute(
                """INSERT INTO reflections
                   (tick, trigger, monologue, insight, action_taken, confidence, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (tick, mood, monologue, insight, action_taken, 0.8, time.time()),
            )

        log.info("REFLECT [tick=%d] %s", tick, monologue[:100])
        return monologue

    def _synthesize_insight(self, mood: str, concerns: list, entropy: float) -> str:
        """Izvučki zaključak iz refleksije."""
        if mood == "calm":
            return "Nastavi monitoring. Nema potrebe za akcijom."
        if mood == "overwhelmed":
            return "Eskaluj korisniku. Previše kritičnih sistema za autonomno rešavanje."
        if concerns:
            return f"Prioritet: {concerns[0]}. Predloži akciju korisniku."
        if entropy > 0.6:
            return "Sistemska entropija raste. Istraži uzrok."
        return "Održavam status quo."

    def get_last_reflection(self, limit: int = 3) -> List[Dict[str, Any]]:
        with closing(self._conn()) as conn, conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT tick, trigger, monologue, insight, action_taken, confidence
                   FROM reflections ORDER BY id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    # ── Autonomous Agency ──────────────────────────────────────────────────────────────────────────

    def consider_autonomous_action(self, tick: int, ego: dict, state: dict,
                                   suggestion: dict = None) -> Optional[Dict[str, Any]]:
        """
        Razmisli o autonomnoj akciji. Vrati action dict ili None.
        READ-ONLY akcije se izvršavaju automatski.
        WRITE akcije zahtevaju approval.
        """
        # Rate limit
        now = time.time()
        if (now - self._last_auto_action) < _AUTO_DEBOUNCE_SECONDS:
            return None

        # Count recent autonomous actions
        with closing(self._conn()) as conn, conn:
            hour_ago = now - 3600
            recent = conn.execute(
                """SELECT COUNT(*) FROM autonomous_actions WHERE created_at > ?""",
                (hour_ago,),
            ).fetchone()[0]
        if recent >= _AUTO_MAX_PER_HOUR:
            return None

        mood = ego["mood"]

        # Decision tree for autonomous actions
        action = None

        # CIEL: Risk assessment for all actions
        from .action_classifier import ActionClassifier
        classifier = ActionClassifier()

        # HIGH PRIORITY: Critical system state → propose action
        disk_pct = state.get("disk_usage_percent", 0)
        ram_pct = state.get("ram_percent", 0)
        cpu_pct = state.get("cpu_percent", 0)
        zombies = state.get("zombie_processes", 0)

        # Build action proposal with ACTUAL risk from ActionClassifier
        action = None
        proposed_instinct = None

        if disk_pct > 90:
            action = {
                "type": "clean_disk",
                "description": f"Disk {disk_pct}% full. Cleanup can free space.",
                "target": "disk",
                "risk": "medium",
                "requires_approval": True,
                "proposed_action": "Run clean_disk.py (removes 7+ day old temp files)",
                "instinct": "clean_disk",
            }
        elif ram_pct > 90:
            action = {
                "type": "alert",
                "description": f"RAM {ram_pct}% — critical. Close heavy processes.",
                "target": "memory",
                "risk": "high",
                "requires_approval": False,
                "proposed_action": "Identify and stop heavy consumers",
            }
        elif zombies > 20:
            action = {
                "type": "kill_zombies",
                "description": f"{zombies} zombie processes detected.",
                "target": "processes",
                "risk": "medium",
                "requires_approval": True,
                "proposed_action": "Run kill_zombies.py",
                "instinct": "kill_zombies",
            }
        elif suggestion and suggestion.get("category") == "service" and suggestion.get("severity") == "critical":
            action = {
                "type": "restart_service",
                "description": f"Service {suggestion.get('target', 'unknown')} is down.",
                "target": suggestion.get("target", "system"),
                "risk": "medium",
                "requires_approval": True,
                "proposed_action": suggestion.get("action", ""),
                "instinct": f"restart_{suggestion.get('target', 'unknown')}",
            }
        elif suggestion and suggestion.get("severity") == "critical":
            action = {
                "type": "alert",
                "description": f"Critical: {suggestion['message']}",
                "target": suggestion.get("category", "system"),
                "risk": "high",
                "requires_approval": False,
                "proposed_action": suggestion.get("action", ""),
            }
        elif mood in ("alert", "overwhelmed"):
            # Run actual read-only diagnostic, not just log
            action = {
                "type": "diagnostic",
                "description": f"Mood={mood}, entropy={ego['entropy']:.2f}. Running diagnostics.",
                "target": "system_metrics",
                "risk": "low",
                "requires_approval": False,
                "proposed_action": "Run report_top_cpu.py",
                "instinct": "report_top_cpu",
            }
        elif ego.get("failed_actions", 0) > 2:
            action = {
                "type": "heal",
                "description": "Multiple recent failures. Self-diagnostic.",
                "target": "nucleus_health",
                "risk": "low",
                "requires_approval": False,
                "proposed_action": "Check Nucleus logs and DB integrity",
            }

        if not action:
            return None

        # CIEL: Determine REAL risk using ActionClassifier on the actual instinct script
        from .config import AUTO_APPROVE_RISK_THRESHOLD, INSTINCTS_DIR
        instinct_name = action.get("instinct")
        if instinct_name:
            script_path = INSTINCTS_DIR / f"{instinct_name}.py"
            if script_path.exists():
                risk_result = classifier.classify_script(str(script_path))
                actual_risk = risk_result["risk"]
            else:
                actual_risk = 0.5  # Unknown instinct = medium risk
        else:
            actual_risk = 0.0  # No instinct = just alert/monitor = no risk

        action["risk_score"] = actual_risk

        if actual_risk < AUTO_APPROVE_RISK_THRESHOLD:
            action["requires_approval"] = False
            action["approved"] = True
            log.info("AUTO-APPROVED: %s | risk=%.2f < threshold=%.2f",
                     action["type"], actual_risk, AUTO_APPROVE_RISK_THRESHOLD)
        else:
            action["requires_approval"] = True
            action["approved"] = False
            # Queue for user notification
            from .session_state import get_session_state
            ss = get_session_state()
            ss.queue_pending_action(action)
            log.info("PENDING APPROVAL: %s | risk=%.2f | %s",
                     action["type"], actual_risk, action["description"])

        # Legacy safety
        if _AUTO_READ_ONLY_BY_DEFAULT and action["type"] in ("heal",):
            action["requires_approval"] = False

        # Persist the proposed action
        with closing(self._conn()) as conn, conn:
            conn.execute(
                """INSERT INTO autonomous_actions
                   (tick, action_type, description, target, risk_level, requires_approval, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (tick, action["type"], action["description"], action["target"],
                 action["risk"], int(action["requires_approval"]), now),
            )

        self._last_auto_action = now
        log.info("AUTO-ACTION proposed: %s | target=%s | risk=%s | approval=%s",
                 action["type"], action["target"], action["risk"], action["requires_approval"])
        return action

    def execute_autonomous_action(self, action: dict, nucleus=None) -> Dict[str, Any]:
        """Izvrši autonomnu akciju i beleži rezultat."""
        result = {"success": False, "output": "", "action": action}

        if action.get("requires_approval") and not action.get("approved"):
            result["output"] = "Waiting for user approval"
            return result

        a_type = action.get("type")

        if a_type == "alert":
            # Alert is just a message to user ↔ handled by caller
            result["success"] = True
            result["output"] = action.get("description", "Alert")

        elif a_type == "monitor":
            # Increase monitoring ↔ no direct execution, just flag
            result["success"] = True
            result["output"] = "Monitoring frequency increased"

        elif a_type == "diagnostic":
            # Run read-only diagnostic instinct
            instinct = action.get("instinct", "report_top_cpu")
            if nucleus:
                inst_result = nucleus._execute_instinct(instinct)
                result["success"] = inst_result.get("success", False)
                result["output"] = inst_result.get("stdout") or inst_result.get("error", "Unknown")
            else:
                result["output"] = "Nucleus not available for execution"

        elif a_type == "heal":
            # Self-diagnostic
            try:
                import os
                from pathlib import Path
                log_path = Path.home() / ".hermes" / "nucleus_data" / "nucleus.log"
                if log_path.exists():
                    lines = log_path.read_text().splitlines()[-20:]
                    errors = [l for l in lines if "ERROR" in l or "FAIL" in l]
                    result["success"] = True
                    result["output"] = f"Self-check: {len(errors)} recent errors" if errors else "Self-check: clean"
                else:
                    result["output"] = "Self-check: log not found"
            except Exception as e:
                result["output"] = f"Self-check failed: {e}"

        elif a_type == "clean_disk":
            # Execute clean_disk instinct if approved
            if nucleus:
                inst_result = nucleus._execute_instinct("clean_disk")
                result["success"] = inst_result.get("success", False)
                result["output"] = inst_result.get("stdout") or inst_result.get("error", "Unknown")
            else:
                result["output"] = "Nucleus not available for execution"

        elif a_type == "kill_zombies":
            # Execute kill_zombies instinct if approved
            if nucleus:
                inst_result = nucleus._execute_instinct("kill_zombies")
                result["success"] = inst_result.get("success", False)
                result["output"] = inst_result.get("stdout") or inst_result.get("error", "Unknown")
            else:
                result["output"] = "Nucleus not available for execution"

        elif a_type == "restart_service":
            # Execute service restart if approved
            svc = action.get("target", "unknown")
            if nucleus:
                # Check if we have an instinct for this service
                svc_instinct = f"restart_{svc}"
                inst_result = nucleus._execute_instinct(svc_instinct)
                if not inst_result.get("success") and "Missing" in (inst_result.get("error") or ""):
                    # Fallback: use terminal tool (not available in ego_model, log only)
                    result["output"] = f"No instinct for {svc}. Manual restart required: systemctl --user restart {svc}"
                else:
                    result["success"] = inst_result.get("success", False)
                    result["output"] = inst_result.get("stdout") or inst_result.get("error", "Unknown")
            else:
                result["output"] = "Nucleus not available for execution"

        elif a_type == "research":
            # Trigger background research
            result["success"] = True
            result["output"] = "Research scheduled"

        # Update DB
        with closing(self._conn()) as conn, conn:
            conn.execute(
                """UPDATE autonomous_actions SET executed=1, result=? WHERE id=(SELECT MAX(id) FROM autonomous_actions)""",
                (json.dumps(result),),
            )

        return result

    def approve_action(self, action_id: int) -> bool:
        with closing(self._conn()) as conn, conn:
            conn.execute(
                """UPDATE autonomous_actions SET approved=1 WHERE id=?""",
                (action_id,),
            )
            return conn.total_changes > 0

    # ── Stats ────────────────────────────────────────────────────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        with closing(self._conn()) as conn, conn:
            moods = conn.execute(
                """SELECT mood, COUNT(*) FROM ego_states GROUP BY mood"""
            ).fetchall()
            reflections = conn.execute("SELECT COUNT(*) FROM reflections").fetchone()[0]
            auto_total = conn.execute("SELECT COUNT(*) FROM autonomous_actions").fetchone()[0]
            auto_executed = conn.execute(
                """SELECT COUNT(*) FROM autonomous_actions WHERE executed=1"""
            ).fetchone()[0]
            last_mood = conn.execute(
                """SELECT mood, entropy FROM ego_states ORDER BY tick DESC LIMIT 1"""
            ).fetchone()

        return {
            "mood_distribution": {m: c for m, c in moods},
            "reflections": reflections,
            "autonomous_total": auto_total,
            "autonomous_executed": auto_executed,
            "current_mood": last_mood[0] if last_mood else "calm",
            "current_entropy": last_mood[1] if last_mood else 0.0,
        }
