"""
Entity Relationship Graph - Feature 2
Manages entity relationships with graph traversal and cross-memory synthesis.
"""
import json
import logging
import sqlite3
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def stable_id(prefix: str, *parts: str) -> str:
    """Generate stable ID from parts."""
    import hashlib
    combined = '|'.join(str(p) for p in parts)
    hash_part = hashlib.sha256(combined.encode('utf-8')).hexdigest()[:16]
    return f"{prefix}:{hash_part}"


class EntityGraph:
    """Manages entity relationships and graph traversal."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def add_relationship(
        self,
        entity_a_id: str,
        entity_b_id: str,
        relationship_type: str,
        strength: float = 1.0,
        evidence: Optional[Dict[str, Any]] = None,
        scope_key: str = '',
        scope_tags: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        Add or update entity relationship.

        Args:
            entity_a_id: Source entity ID
            entity_b_id: Target entity ID
            relationship_type: Type of relationship (uses, produces, requires, related_to, part_of)
            strength: Relationship strength (0.0-1.0)
            evidence: Evidence supporting the relationship
            scope_key: Scope key for filtering
            scope_tags: Scope tags for filtering

        Returns:
            Relationship ID
        """
        now = time.time()
        relationship_id = stable_id('rel', entity_a_id, entity_b_id, relationship_type)

        evidence_json = json.dumps(evidence or {})
        scope_tags_json = json.dumps(scope_tags or {})

        # Check if relationship exists
        existing = self.conn.execute(
            "SELECT relationship_id, strength FROM entity_relationships WHERE relationship_id = ?",
            (relationship_id,)
        ).fetchone()

        if existing:
            # Update existing relationship
            new_strength = min(1.0, existing[1] + 0.1)  # Increase strength
            self.conn.execute(
                """UPDATE entity_relationships
                   SET strength = ?, last_observed_at = ?, updated_at = ?, evidence_json = ?
                   WHERE relationship_id = ?""",
                (new_strength, now, now, evidence_json, relationship_id)
            )
        else:
            # Insert new relationship
            self.conn.execute(
                """INSERT INTO entity_relationships
                   (relationship_id, entity_a_id, entity_b_id, relationship_type, strength,
                    first_observed_at, last_observed_at, evidence_json, scope_key,
                    scope_tags_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (relationship_id, entity_a_id, entity_b_id, relationship_type, strength,
                 now, now, evidence_json, scope_key, scope_tags_json, now, now)
            )

        self.conn.commit()
        return relationship_id

    def get_related_entities(
        self,
        entity_id: str,
        relationship_type: Optional[str] = None,
        max_depth: int = 2
    ) -> List[Dict[str, Any]]:
        """
        Traverse graph to find related entities.

        Args:
            entity_id: Starting entity ID
            relationship_type: Filter by relationship type (optional)
            max_depth: Maximum traversal depth

        Returns:
            List of related entities with relationship info
        """
        visited = set()
        results = []

        def traverse(current_id: str, depth: int):
            if depth > max_depth or current_id in visited:
                return
            visited.add(current_id)

            # Find relationships where current entity is source
            query = """
                SELECT r.entity_b_id, r.relationship_type, r.strength, e.canonical_name, e.entity_type
                FROM entity_relationships r
                JOIN entities e ON r.entity_b_id = e.entity_id
                WHERE r.entity_a_id = ?
            """
            params = [current_id]

            if relationship_type:
                query += " AND r.relationship_type = ?"
                params.append(relationship_type)

            rows = self.conn.execute(query, params).fetchall()

            for row in rows:
                results.append({
                    'entity_id': row[0],
                    'relationship_type': row[1],
                    'strength': row[2],
                    'canonical_name': row[3],
                    'entity_type': row[4],
                    'depth': depth
                })
                traverse(row[0], depth + 1)

        traverse(entity_id, 1)
        return results

    def synthesize_entity_context(
        self,
        entity_id: str,
        include_facts: bool = True,
        include_beliefs: bool = True
    ) -> str:
        """
        Cross-memory synthesis for an entity.

        Args:
            entity_id: Entity ID to synthesize context for
            include_facts: Include related facts
            include_beliefs: Include related beliefs

        Returns:
            Synthesized context string
        """
        # Get entity info
        entity_row = self.conn.execute(
            "SELECT canonical_name, entity_type FROM entities WHERE entity_id = ?",
            (entity_id,)
        ).fetchone()

        if not entity_row:
            return ""

        entity_name = entity_row[0]
        lines = [f"Entity: {entity_name}"]

        # Get related entities with depth=2 for better coverage
        related = self.get_related_entities(entity_id, max_depth=2)
        if related:
            # Sort by relationship strength
            related.sort(key=lambda x: x['strength'], reverse=True)
            lines.append(f"Related entities ({len(related)}):")
            for rel in related[:5]:
                lines.append(f"  - {rel['relationship_type']}: {rel['canonical_name']} (strength: {rel['strength']:.2f})")

        # Get related facts using proper JOIN with subject_entity_id
        if include_facts:
            facts = self.conn.execute(
                """SELECT f.fact_text, f.confidence
                   FROM facts f
                   WHERE f.status='active' AND (f.subject_entity_id = ? OR f.subject_entity_id IN (
                       SELECT entity_b_id FROM entity_relationships WHERE entity_a_id = ?
                   ))
                   ORDER BY f.confidence DESC, f.valid_from DESC LIMIT 5""",
                (entity_id, entity_id)
            ).fetchall()
            if facts:
                lines.append("Related facts:")
                for fact in facts:
                    lines.append(f"  - {fact[0][:100]}")

        # Get related beliefs (keep LIKE for now as beliefs table lacks entity_id)
        if include_beliefs:
            beliefs = self.conn.execute(
                """SELECT claim_text, belief_kind, confidence FROM beliefs
                   WHERE status IN ('open', 'validated') AND claim_text LIKE ?
                   ORDER BY confidence DESC, created_at DESC LIMIT 3""",
                (f"%{entity_name}%",)
            ).fetchall()
            if beliefs:
                lines.append("Related beliefs:")
                for belief in beliefs:
                    lines.append(f"  - [{belief[1]}] {belief[0][:100]}")

        return "\n".join(lines)
