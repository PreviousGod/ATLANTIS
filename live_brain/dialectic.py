"""
Dialectic Reasoning Layer - Feature 3
Cross-session synthesis and two-layer context building.
"""
import hashlib
import json
import logging
import sqlite3
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class DialecticEngine:
    """Manages cross-session synthesis and dialectic reasoning."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def synthesize_cross_session(
        self,
        query: str,
        scope_key: str,
        max_sessions: int = 5
    ) -> Dict[str, Any]:
        """
        Synthesize reasoning across multiple sessions.

        Args:
            query: Query to synthesize around
            scope_key: Scope key for filtering
            max_sessions: Maximum number of sessions to consider

        Returns:
            Dict with synthesis, source_sessions, and confidence
        """
        query_fingerprint = hashlib.sha256(query.lower().encode()).hexdigest()[:16]

        # Check for cached synthesis
        cached = self.conn.execute(
            """SELECT synthesis_text, source_sessions_json, confidence, last_accessed_at
               FROM dialectic_syntheses
               WHERE query_fingerprint = ? AND scope_key = ?
               ORDER BY created_at DESC LIMIT 1""",
            (query_fingerprint, scope_key)
        ).fetchone()

        if cached and (time.time() - cached[3]) < 3600:  # Cache for 1 hour
            # Update access count
            self.conn.execute(
                """UPDATE dialectic_syntheses
                   SET last_accessed_at = ?, access_count = access_count + 1
                   WHERE query_fingerprint = ? AND scope_key = ?""",
                (time.time(), query_fingerprint, scope_key)
            )
            self.conn.commit()

            return {
                'synthesis': cached[0],
                'source_sessions': json.loads(cached[1]),
                'confidence': cached[2]
            }

        # Find relevant sessions
        query_words = [w.lower() for w in query.split() if len(w) > 3]
        if not query_words:
            return {'synthesis': '', 'source_sessions': [], 'confidence': 0.0}

        # Get facts from multiple sessions
        facts = self._get_cross_session_facts(query_words, scope_key, max_sessions)

        # Get beliefs from multiple sessions
        beliefs = self._get_cross_session_beliefs(query_words, scope_key, max_sessions)

        if not facts and not beliefs:
            return {'synthesis': '', 'source_sessions': [], 'confidence': 0.0}

        # Synthesize
        synthesis_lines = []
        source_sessions = set()

        if facts:
            synthesis_lines.append(f"Cross-session facts ({len(facts)}):")
            for fact in facts[:3]:
                synthesis_lines.append(f"  - {fact['fact_text'][:100]}")
                source_sessions.add(fact['session_id'])

        if beliefs:
            # Group beliefs by status
            validated = [b for b in beliefs if b['status'] == 'validated']
            open_beliefs = [b for b in beliefs if b['status'] == 'open']

            if validated:
                synthesis_lines.append(f"Validated beliefs ({len(validated)}):")
                for belief in validated[:2]:
                    synthesis_lines.append(f"  - {belief['claim_text'][:100]}")
                    source_sessions.add(belief['session_id'])

            if open_beliefs:
                synthesis_lines.append(f"Open hypotheses ({len(open_beliefs)}):")
                for belief in open_beliefs[:2]:
                    synthesis_lines.append(f"  - {belief['claim_text'][:100]}")
                    source_sessions.add(belief['session_id'])

        synthesis_text = "\n".join(synthesis_lines)
        confidence = min(0.8, 0.5 + (len(source_sessions) * 0.1))

        # Store synthesis
        synthesis_id = f"synthesis:{query_fingerprint}:{int(time.time())}"
        now = time.time()

        self.conn.execute(
            """INSERT INTO dialectic_syntheses
               (synthesis_id, query_fingerprint, synthesis_text, source_sessions_json,
                source_facts_json, source_beliefs_json, confidence, scope_key,
                scope_tags_json, created_at, last_accessed_at, access_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, 1)""",
            (synthesis_id, query_fingerprint, synthesis_text,
             json.dumps(list(source_sessions)),
             json.dumps([f['fact_id'] for f in facts]),
             json.dumps([b['belief_id'] for b in beliefs]),
             confidence, scope_key, now, now)
        )
        self.conn.commit()

        return {
            'synthesis': synthesis_text,
            'source_sessions': list(source_sessions),
            'confidence': confidence
        }

    def _get_cross_session_facts(
        self,
        query_words: List[str],
        scope_key: str,
        max_sessions: int
    ) -> List[Dict[str, Any]]:
        """Get facts from multiple sessions matching query words."""
        like_conditions = " OR ".join(["LOWER(fact_text) LIKE ?" for _ in query_words])
        params = [f"%{w}%" for w in query_words] + [scope_key, max_sessions * 3]

        rows = self.conn.execute(
            f"""SELECT fact_id, fact_text, session_id, confidence
                FROM facts
                WHERE ({like_conditions}) AND scope_key = ? AND status = 'active'
                ORDER BY valid_from DESC
                LIMIT ?""",
            params
        ).fetchall()

        return [
            {
                'fact_id': row[0],
                'fact_text': row[1],
                'session_id': row[2],
                'confidence': row[3]
            }
            for row in rows
        ]

    def _get_cross_session_beliefs(
        self,
        query_words: List[str],
        scope_key: str,
        max_sessions: int
    ) -> List[Dict[str, Any]]:
        """Get beliefs from multiple sessions matching query words."""
        like_conditions = " OR ".join(["LOWER(claim_text) LIKE ?" for _ in query_words])
        params = [f"%{w}%" for w in query_words] + [scope_key, max_sessions * 3]

        rows = self.conn.execute(
            f"""SELECT belief_id, claim_text, belief_kind, status, session_id, confidence
                FROM beliefs
                WHERE ({like_conditions}) AND scope_key = ? AND status IN ('open', 'validated')
                ORDER BY created_at DESC
                LIMIT ?""",
            params
        ).fetchall()

        return [
            {
                'belief_id': row[0],
                'claim_text': row[1],
                'belief_kind': row[2],
                'status': row[3],
                'session_id': row[4],
                'confidence': row[5]
            }
            for row in rows
        ]

    def build_dialectic_context(
        self,
        query: str,
        scope_key: str
    ) -> str:
        """
        Build two-layer context:
        Layer 1: Current session summary
        Layer 2: Cross-session synthesized reasoning

        Args:
            query: User query
            scope_key: Scope key for filtering

        Returns:
            Formatted dialectic context string
        """
        synthesis_result = self.synthesize_cross_session(query, scope_key)

        if not synthesis_result['synthesis']:
            return ""

        lines = [
            "DIALECTIC SYNTHESIS:",
            f"Cross-session insights (confidence={synthesis_result['confidence']:.2f}):",
            synthesis_result['synthesis']
        ]

        return "\n".join(lines)
