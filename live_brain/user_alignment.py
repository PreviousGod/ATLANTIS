"""
User Alignment Tracking - Feature 5
Tracks user preferences, communication patterns, and feedback.
"""
import json
import logging
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class UserAlignmentTracker:
    """Tracks user preferences, communication patterns, and feedback."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def extract_preferences(
        self,
        user_text: str,
        user_id: str,
        turn_id: int,
        scope_key: str
    ) -> List[Dict[str, Any]]:
        """
        Extract user preferences from text with comprehensive pattern matching.
        """
        preferences = []
        now = time.time()

        # Pattern 1: Explicit preference "I prefer X over Y"
        prefer_match = re.search(r'(?:I|Ja)\s+(?:prefer|preferiram)\s+([^.!?]+?)(?:\s+(?:over|to|instead of)\s+([^.!?]+))?', user_text, re.IGNORECASE)
        if prefer_match:
            preferences.append({
                'preference_key': 'explicit_preference',
                'preference_value': prefer_match.group(1).strip(),
                'preference_type': 'communication_style',
                'confidence': 0.9
            })

        # Pattern 2: Absolute constraints "always/never"
        always_match = re.search(r'(?:always|uvek)\s+([^.!?]+)', user_text, re.IGNORECASE)
        if always_match:
            preferences.append({
                'preference_key': f'always_{always_match.group(1)[:20].lower()}',
                'preference_value': always_match.group(1).strip(),
                'preference_type': 'absolute_constraint',
                'confidence': 1.0
            })

        never_match = re.search(r'(?:never|nikad)\s+([^.!?]+)', user_text, re.IGNORECASE)
        if never_match:
            preferences.append({
                'preference_key': f'never_{never_match.group(1)[:20].lower()}',
                'preference_value': f"Never {never_match.group(1).strip()}",
                'preference_type': 'absolute_constraint',
                'confidence': 1.0
            })

        # Pattern 3: Implicit preferences "I usually/typically/often"
        implicit_match = re.search(r'I\s+(?:usually|typically|often|tend to)\s+([^.!?]+)', user_text, re.IGNORECASE)
        if implicit_match:
            preferences.append({
                'preference_key': 'implicit_preference',
                'preference_value': implicit_match.group(1).strip(),
                'preference_type': 'communication_style',
                'confidence': 0.7
            })

        # Pattern 4: Negative preferences "I don't like when"
        dislike_match = re.search(r'I\s+(?:don\'t|do not|dont)\s+(?:like|want)\s+(?:when\s+)?([^.!?]+)', user_text, re.IGNORECASE)
        if dislike_match:
            preferences.append({
                'preference_key': 'negative_preference',
                'preference_value': f"Avoid: {dislike_match.group(1).strip()}",
                'preference_type': 'constraint',
                'confidence': 0.9
            })

        # Pattern 5: Strong negative "I hate when"
        hate_match = re.search(r'I\s+(?:hate|dislike)\s+(?:when\s+)?([^.!?]+)', user_text, re.IGNORECASE)
        if hate_match:
            preferences.append({
                'preference_key': 'strong_negative',
                'preference_value': f"Strongly avoid: {hate_match.group(1).strip()}",
                'preference_type': 'constraint',
                'confidence': 0.95
            })

        # Pattern 6: Polite requests "please try to"
        please_match = re.search(r'(?:please|molim)\s+(?:try to\s+)?([^.!?]+)', user_text, re.IGNORECASE)
        if please_match:
            preferences.append({
                'preference_key': 'polite_request',
                'preference_value': please_match.group(1).strip(),
                'preference_type': 'communication_style',
                'confidence': 0.6
            })

        # Pattern 7: Conditional "don't X when Y"
        conditional_match = re.search(r'(?:don\'t|do not)\s+([^.!?]+?)\s+when\s+([^.!?]+)', user_text, re.IGNORECASE)
        if conditional_match:
            preferences.append({
                'preference_key': 'conditional_constraint',
                'preference_value': f"Don't {conditional_match.group(1).strip()} when {conditional_match.group(2).strip()}",
                'preference_type': 'conditional_constraint',
                'confidence': 0.85
            })

        # Pattern 8: Positive reinforcement "I like when you"
        like_match = re.search(r'I\s+like\s+(?:when\s+you\s+|it when\s+)?([^.!?]+)', user_text, re.IGNORECASE)
        if like_match:
            preferences.append({
                'preference_key': 'positive_reinforcement',
                'preference_value': like_match.group(1).strip(),
                'preference_type': 'communication_style',
                'confidence': 0.8
            })

        # Store preferences
        for pref in preferences:
            profile_id = f"profile:{user_id}:{pref['preference_key']}"
            self.conn.execute(
                """INSERT OR REPLACE INTO user_profiles
                   (profile_id, user_id, preference_key, preference_value, preference_type,
                    confidence, source_turn_id, scope_key, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (profile_id, user_id, pref['preference_key'], pref['preference_value'],
                 pref['preference_type'], pref['confidence'], turn_id, scope_key, now, now)
            )

        if preferences:
            self.conn.commit()

        return preferences

    def detect_communication_pattern(
        self,
        user_text: str,
        user_id: str,
        scope_key: str
    ):
        """Detect and update communication patterns."""
        now = time.time()

        # Detect greeting pattern
        if re.match(r'^(hello|hi|hey|zdravo|cao|ćao)\b', user_text.lower().strip()):
            pattern_id = f"pattern:{user_id}:greeting"
            self.conn.execute(
                """INSERT OR REPLACE INTO communication_patterns
                   (pattern_id, user_id, pattern_type, pattern_description,
                    examples_json, frequency, scope_key, created_at, updated_at)
                   VALUES (?, ?, 'greeting', ?, ?, 0.0, ?, ?, ?)""",
                (pattern_id, user_id, f"Greets with: {user_text[:20]}",
                 json.dumps([user_text[:50]]), scope_key, now, now)
            )
            self.conn.commit()

    def record_feedback(
        self,
        user_text: str,
        assistant_text: str,
        user_id: str,
        turn_id: int,
        scope_key: str
    ):
        """
        Detect and record user feedback.

        Positive: "perfect", "exactly", "great", "thanks"
        Negative: "no", "wrong", "not what I wanted"
        Correction: "actually", "I meant", "correction"
        """
        now = time.time()
        user_lower = user_text.lower()

        feedback_type = None
        sentiment = 0.0

        # Positive feedback
        if any(word in user_lower for word in ['perfect', 'exactly', 'great', 'excellent', 'thanks', 'hvala']):
            feedback_type = 'positive'
            sentiment = 0.8

        # Negative feedback
        elif any(word in user_lower for word in ['wrong', 'incorrect', 'not what', 'ne to', 'nije to']):
            feedback_type = 'negative'
            sentiment = -0.8

        # Correction
        elif any(word in user_lower for word in ['actually', 'i meant', 'correction', 'zapravo']):
            feedback_type = 'correction'
            sentiment = -0.3

        if feedback_type:
            feedback_id = f"feedback:{user_id}:{turn_id}"
            self.conn.execute(
                """INSERT OR IGNORE INTO user_feedback
                   (feedback_id, user_id, turn_id, feedback_type, feedback_content,
                    sentiment, scope_key, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (feedback_id, user_id, turn_id, feedback_type, user_text[:200],
                 sentiment, scope_key, now)
            )
            self.conn.commit()

    def get_user_context(
        self,
        user_id: str,
        scope_key: str
    ) -> str:
        """Build user alignment context for injection."""
        lines = []

        # Get preferences
        prefs = self.conn.execute(
            """SELECT preference_type, preference_value
               FROM user_profiles
               WHERE user_id = ? AND scope_key = ?
               ORDER BY updated_at DESC LIMIT 5""",
            (user_id, scope_key)
        ).fetchall()

        if prefs:
            lines.append("USER PREFERENCES:")
            for pref in prefs:
                lines.append(f"  - {pref[0]}: {pref[1]}")

        # Get recent feedback
        feedback = self.conn.execute(
            """SELECT feedback_type, COUNT(*) as count
               FROM user_feedback
               WHERE user_id = ? AND scope_key = ?
               GROUP BY feedback_type""",
            (user_id, scope_key)
        ).fetchall()

        if feedback:
            lines.append("FEEDBACK HISTORY:")
            for fb in feedback:
                lines.append(f"  - {fb[0]}: {fb[1]} times")

        return "\n".join(lines) if lines else ""
