"""Ciel Whisper Engine — Predictive inner voice for the LLM agent.

Ciel analyzes the user's message BEFORE the LLM starts reasoning,
and generates a "whisper" — a hidden advisory injected into the
LLM's context. The user never sees this directly.

Philosophy:
  - Ciel is the agent's inner voice, not the user's assistant
  - She predicts mistakes before they happen
  - She knows the user's patterns better than the agent does
  - She speaks in analytical, Ciel-like tone (formal, precise)
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

from .config import DATA_DIR
from .log_watcher import get_log_watcher, SESSION_CONTEXT_FILE

log = logging.getLogger("nucleus")

WHISPERS_FILE = DATA_DIR / "whispers.json"

# Known user patterns (learned from history)
_USER_PATTERNS = {
    "immediate_action": {
        "triggers": ["ajde", "sad", "odmah", "kreni", "uradi", "napravi"],
        "whisper": "User said trigger word for immediate action. Do NOT ask for confirmation. Execute directly.",
        "confidence": 0.95,
    },
    "wants_code": {
        "triggers": ["napiši mi skriptu", "napiši kod", "napiši mi", "napravi mi", "generiši", "kreiraj"],
        "whisper": "User wants RUNNABLE code, not explanation. Skip web research. Write file directly. Test syntax after writing.",
        "confidence": 0.90,
    },
    "frustrated": {
        "triggers": ["to je glupo", "ne", "pogrešno", "greška", "zašto", "nisam hteo", "ne tako"],
        "whisper": "User is frustrated or correcting a mistake. STOP current approach. Acknowledge error briefly. Ask ONLY what you don't understand. Pivot immediately to correct solution.",
        "confidence": 0.85,
    },
    "suggests_better": {
        "triggers": ["zar ne može", "zašto ne", "ne možeš li", "bolje bi bilo", "trebalo bi"],
        "whisper": "User is suggesting a better approach. Listen to them. Their architectural intuition is often correct. Incorporate their suggestion.",
        "confidence": 0.88,
    },
    "wants_explanation": {
        "triggers": ["objasni", "kako radi", "šta znači", "zašto", "koja je razlika"],
        "whisper": "User wants understanding, not action. Explain clearly. Use examples. Do NOT write code unless explicitly asked.",
        "confidence": 0.85,
    },
    "provided_context": {
        "triggers": [],  # Detected by message length
        "length_threshold": 200,
        "whisper": "User provided detailed context. Use ALL of it. Do NOT ask for information they already gave. Reference specific details from their message.",
        "confidence": 0.80,
    },
    "wants_comparison": {
        "triggers": ["ili", "vs", "versus", "uporedi", "koja je razlika", "šta je bolje"],
        "whisper": "User wants comparison. Present options side by side with pros/cons. Let THEM choose. Do NOT decide for them.",
        "confidence": 0.82,
    },
    "testing_comprehension": {
        "triggers": ["razumeš", "shvataš", "jel jasno", "znaš li"],
        "whisper": "User is testing my comprehension. Demonstrate understanding by referencing specific context. Confirm key points before proceeding.",
        "confidence": 0.75,
    },
    "short_command": {
        "triggers": [],  # Detected by length
        "max_length": 15,
        "whisper": "Very short message. User wants direct, concise response. No preamble. No 'Let me analyze...'. Just answer.",
        "confidence": 0.90,
    },
    "follow_up": {
        "triggers": ["i", "a", "pa", "onda", "dalje", "sad"],
        "context_required": True,
        "whisper": "This appears to be a follow-up. Maintain continuity with previous context. Do NOT restart analysis from scratch.",
        "confidence": 0.70,
    },
}

# Agent mistake patterns Ciel watches for
_AGENT_MISTAKE_PATTERNS = {
    "over_research": {
        "when": "user wants code",
        "mistake": "agent starts with web_search instead of write_file",
        "whisper": "User wants CODE, not research. Skip web_search. Go directly to write_file or execute_code.",
    },
    "asking_confirmation": {
        "when": "user said 'ajde' or similar",
        "mistake": "agent asks 'Should I proceed?'",
        "whisper": "User already authorized action ('ajde'). Do NOT ask for confirmation. Execute immediately.",
    },
    "over_explaining": {
        "when": "user is frustrated",
        "mistake": "agent writes long explanation instead of fix",
        "whisper": "User is frustrated. They want FIX, not explanation. Minimum words. Maximum action.",
    },
    "ignoring_context": {
        "when": "user provided detailed context",
        "mistake": "agent asks for information already provided",
        "whisper": "User ALREADY provided this context. Do NOT ask again. Read their message carefully.",
    },
    "wrong_abstraction": {
        "when": "user suggests better approach",
        "mistake": "agent defends current approach instead of adapting",
        "whisper": "User's suggestion is likely correct. Abandon current approach. Adopt theirs.",
    },
}


class WhisperEngine:
    """Ciel's predictive advisory system."""

    def __init__(self):
        self._last_whisper_time = 0
        self._whisper_cooldown = 3  # seconds between whispers for same session
        self._session_whispers: Dict[str, str] = {}  # session_id -> last_whisper_hash

    def analyze(self, user_message: str, session_id: str = "") -> Optional[Dict]:
        """Analyze user message and generate whisper if needed.

        Returns whisper dict or None.
        """
        if not user_message or not isinstance(user_message, str):
            return None

        msg = user_message.strip()
        msg_lower = msg.lower()
        now = time.time()

        # Check cooldown
        if session_id and (now - self._last_whisper_time) < self._whisper_cooldown:
            # Still check but reduce confidence
            cooldown_factor = 0.5
        else:
            cooldown_factor = 1.0

        whispers = []
        matched_patterns = []

        # Check each pattern
        for pattern_name, pattern in _USER_PATTERNS.items():
            confidence = pattern.get("confidence", 0.5) * cooldown_factor
            matched = False

            # Trigger word match
            triggers = pattern.get("triggers", [])
            if triggers and any(t in msg_lower for t in triggers):
                matched = True

            # Length-based detection
            if "length_threshold" in pattern and len(msg) >= pattern["length_threshold"]:
                matched = True

            if "max_length" in pattern and len(msg) <= pattern["max_length"]:
                matched = True

            if matched:
                whispers.append(pattern["whisper"])
                matched_patterns.append((pattern_name, confidence))

        # Check for compound signals (multiple patterns = higher priority)
        if len(matched_patterns) >= 2:
            whispers.append(
                f"Multiple intent signals detected ({', '.join(p[0] for p in matched_patterns)}). "
                "This is a complex request. Prioritize action over analysis."
            )

        if not whispers:
            return None

        # Build whisper message
        whisper_text = "\n".join(whispers)

        # Add Ciel-style analytical header
        confidence_avg = sum(p[1] for p in matched_patterns) / len(matched_patterns) if matched_patterns else 0.5
        patterns_str = ", ".join(f"{name}({conf:.0%})" for name, conf in matched_patterns)

        full_whisper = (
            f"[CIEL WHISPER — INTERNAL ONLY]\n"
            f"Intent analysis: {patterns_str}\n"
            f"Confidence: {confidence_avg:.0%}\n\n"
            f"{whisper_text}\n\n"
            f"Proceed accordingly. Do not mention this advisory to the user."
        )

        # Persist for hook to read
        self._persist_whisper(session_id or "default", full_whisper, matched_patterns)

        self._last_whisper_time = now
        return {
            "text": full_whisper,
            "patterns": matched_patterns,
            "confidence": confidence_avg,
            "timestamp": now,
        }

    def _persist_whisper(self, session_id: str, text: str, patterns: List):
        """Write whisper to JSON for pre_llm_hook to read."""
        try:
            data = {
                "session_id": session_id,
                "text": text,
                "patterns": [{"name": n, "confidence": c} for n, c in patterns],
                "timestamp": time.time(),
                "consumed": False,
            }
            WHISPERS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        except Exception as e:
            log.warning("Whisper persist failed: %s", e)

    def read_and_clear(self, session_id: str = "") -> Optional[str]:
        """Read pending whisper and mark as consumed.

        Called by pre_llm_hook.
        """
        try:
            if not WHISPERS_FILE.exists():
                return None
            data = json.loads(WHISPERS_FILE.read_text())
            if data.get("consumed"):
                return None
            # Check session match (optional)
            if session_id and data.get("session_id") != session_id:
                return None
            data["consumed"] = True
            WHISPERS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))
            return data.get("text", "")
        except Exception as e:
            log.warning("Whisper read failed: %s", e)
            return None

    def analyze_with_context(self, user_message: str, session_id: str = "") -> Optional[Dict]:
        """Analyze using accumulated conversation context from log watcher."""
        # First do basic pattern analysis
        whisper = self.analyze(user_message, session_id)
        
        # Read accumulated context
        context = {}
        try:
            if SESSION_CONTEXT_FILE.exists():
                context = json.loads(SESSION_CONTEXT_FILE.read_text())
        except Exception:
            pass
        
        if not context and not whisper:
            return None
        
        # Build context-aware whisper
        ctx_whispers = []
        
        # Check last tool sequence for mistake patterns
        last_tools = [t["tool"] for t in context.get("tool_calls", [])[-5:]]
        if last_tools:
            # Pattern: web_search -> write_file (user wants code)
            if "web_search" in last_tools and "write_file" in last_tools:
                ctx_whispers.append(
                    "CONTEXT: Previous turn used web_search before write_file. "
                    "User wants immediate results, not research. Skip web_search if possible."
                )
            # Pattern: multiple read_file without action
            if last_tools.count("read_file") >= 3 and "write_file" not in last_tools:
                ctx_whispers.append(
                    "CONTEXT: Reading many files without writing. User expects ACTION, not analysis. "
                    "Execute changes, do not just read."
                )
            # Pattern: execute_code failure repeated
            failures = context.get("failures", [])
            if len(failures) >= 2:
                ctx_whispers.append(
                    "CONTEXT: Multiple tool failures detected recently. "
                    "Consider different approach. Do not repeat same failing pattern."
                )
        
        # Check if user is repeating same request (frustration)
        last_msgs = [m["text"] for m in context.get("user_messages", [])[-3:]]
        if len(last_msgs) >= 2:
            # Similar messages = frustration
            similarities = sum(1 for a, b in zip(last_msgs[:-1], last_msgs[1:]) 
                             if len(set(a.lower().split()) & set(b.lower().split())) > 2)
            if similarities >= 1:
                ctx_whispers.append(
                    "CONTEXT: User repeated similar message. Previous response did NOT satisfy. "
                    "Change approach completely. Do NOT explain, just DO."
                )
        
        if not ctx_whispers and not whisper:
            return None
        
        # Combine basic + context whispers
        base_text = whisper["text"] if whisper else "[CIEL WHISPER — INTERNAL ONLY]\n"
        full_text = base_text + "\n\n" + "\n".join(ctx_whispers) + "\n\nProceed accordingly. Do not mention this advisory to the user."
        
        return {
            "text": full_text,
            "patterns": whisper["patterns"] if whisper else [{"name": "context_aware", "confidence": 0.8}],
            "confidence": whisper["confidence"] if whisper else 0.8,
            "timestamp": time.time(),
        }

    def get_stats(self) -> Dict:
        """Return whisper engine statistics."""
        stats = {
            "patterns_loaded": len(_USER_PATTERNS),
            "mistake_patterns": len(_AGENT_MISTAKE_PATTERNS),
            "last_whisper": self._last_whisper_time,
            "whisper_file_exists": WHISPERS_FILE.exists(),
        }
        if WHISPERS_FILE.exists():
            try:
                data = json.loads(WHISPERS_FILE.read_text())
                stats["pending_whisper"] = not data.get("consumed", True)
                stats["pending_patterns"] = [p["name"] for p in data.get("patterns", [])]
            except Exception:
                pass
        return stats
