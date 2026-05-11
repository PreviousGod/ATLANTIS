"""Tests for entity graph traversal and relationship management."""
import sqlite3
import tempfile
import time
import json
from pathlib import Path


def test_add_relationship():
    """Test adding entity relationships."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test.db")
        conn = sqlite3.connect(db_path)

        # Create tables
        conn.execute("""
            CREATE TABLE entity_relationships (
                relationship_id TEXT PRIMARY KEY,
                entity_a_id TEXT,
                entity_b_id TEXT,
                relationship_type TEXT,
                strength REAL,
                first_observed_at REAL,
                last_observed_at REAL,
                evidence_json TEXT,
                scope_key TEXT,
                scope_tags_json TEXT,
                created_at REAL,
                updated_at REAL
            )
        """)

        # Add relationship
        now = time.time()
        rel_id = 'rel:test123'
        conn.execute("""
            INSERT INTO entity_relationships VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (rel_id, 'entity1', 'entity2', 'uses', 0.8, now, now,
              '{}', 'user1', '{}', now, now))
        conn.commit()

        # Verify
        row = conn.execute(
            "SELECT relationship_type, strength FROM entity_relationships WHERE relationship_id = ?",
            (rel_id,)
        ).fetchone()
        assert row[0] == 'uses'
        assert row[1] == 0.8
        conn.close()
    print("✓ Add relationship test passed")


def test_graph_traversal_depth():
    """Test graph traversal with depth limits."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test.db")
        conn = sqlite3.connect(db_path)

        # Create tables
        conn.execute("""
            CREATE TABLE entities (
                entity_id TEXT PRIMARY KEY,
                canonical_name TEXT,
                entity_type TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE entity_relationships (
                relationship_id TEXT PRIMARY KEY,
                entity_a_id TEXT,
                entity_b_id TEXT,
                relationship_type TEXT,
                strength REAL
            )
        """)

        # Create chain: e1 -> e2 -> e3 -> e4
        conn.execute("INSERT INTO entities VALUES ('e1', 'Entity 1', 'concept')")
        conn.execute("INSERT INTO entities VALUES ('e2', 'Entity 2', 'concept')")
        conn.execute("INSERT INTO entities VALUES ('e3', 'Entity 3', 'concept')")
        conn.execute("INSERT INTO entities VALUES ('e4', 'Entity 4', 'concept')")

        conn.execute("INSERT INTO entity_relationships VALUES ('r1', 'e1', 'e2', 'uses', 0.9)")
        conn.execute("INSERT INTO entity_relationships VALUES ('r2', 'e2', 'e3', 'uses', 0.8)")
        conn.execute("INSERT INTO entity_relationships VALUES ('r3', 'e3', 'e4', 'uses', 0.7)")
        conn.commit()

        # Traverse with depth=1 (should find only e2)
        results = []
        visited = set()

        def traverse(entity_id, depth, max_depth):
            if depth > max_depth or entity_id in visited:
                return
            visited.add(entity_id)

            rows = conn.execute("""
                SELECT r.entity_b_id, r.relationship_type, r.strength, e.canonical_name
                FROM entity_relationships r
                JOIN entities e ON r.entity_b_id = e.entity_id
                WHERE r.entity_a_id = ?
            """, (entity_id,)).fetchall()

            for row in rows:
                results.append({'entity_id': row[0], 'depth': depth})
                traverse(row[0], depth + 1, max_depth)

        traverse('e1', 1, 1)
        assert len(results) == 1
        assert results[0]['entity_id'] == 'e2'

        # Traverse with depth=2 (should find e2 and e3)
        results = []
        visited = set()
        traverse('e1', 1, 2)
        assert len(results) == 2
        assert 'e2' in [r['entity_id'] for r in results]
        assert 'e3' in [r['entity_id'] for r in results]

        conn.close()
    print("✓ Graph traversal depth test passed")


def test_relationship_weighting():
    """Test that relationships are weighted by strength."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test.db")
        conn = sqlite3.connect(db_path)

        # Create tables
        conn.execute("""
            CREATE TABLE entities (
                entity_id TEXT PRIMARY KEY,
                canonical_name TEXT,
                entity_type TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE entity_relationships (
                relationship_id TEXT PRIMARY KEY,
                entity_a_id TEXT,
                entity_b_id TEXT,
                relationship_type TEXT,
                strength REAL
            )
        """)

        # Create relationships with different strengths
        conn.execute("INSERT INTO entities VALUES ('e1', 'Entity 1', 'concept')")
        conn.execute("INSERT INTO entities VALUES ('e2', 'Strong relation', 'concept')")
        conn.execute("INSERT INTO entities VALUES ('e3', 'Weak relation', 'concept')")

        conn.execute("INSERT INTO entity_relationships VALUES ('r1', 'e1', 'e2', 'uses', 0.95)")
        conn.execute("INSERT INTO entity_relationships VALUES ('r2', 'e1', 'e3', 'uses', 0.3)")
        conn.commit()

        # Get relationships sorted by strength
        rows = conn.execute("""
            SELECT e.canonical_name, r.strength
            FROM entity_relationships r
            JOIN entities e ON r.entity_b_id = e.entity_id
            WHERE r.entity_a_id = ?
            ORDER BY r.strength DESC
        """, ('e1',)).fetchall()

        assert len(rows) == 2
        assert rows[0][0] == 'Strong relation'
        assert rows[0][1] == 0.95
        assert rows[1][0] == 'Weak relation'
        assert rows[1][1] == 0.3
        conn.close()
    print("✓ Relationship weighting test passed")


def test_entity_fact_linking():
    """Test proper JOIN between entities and facts (not LIKE)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test.db")
        conn = sqlite3.connect(db_path)

        # Create tables
        conn.execute("""
            CREATE TABLE entities (
                entity_id TEXT PRIMARY KEY,
                canonical_name TEXT,
                entity_type TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE facts (
                fact_id TEXT PRIMARY KEY,
                subject_entity_id TEXT,
                fact_text TEXT,
                confidence REAL,
                status TEXT,
                valid_from REAL
            )
        """)

        conn.execute("""
            CREATE TABLE entity_relationships (
                relationship_id TEXT PRIMARY KEY,
                entity_a_id TEXT,
                entity_b_id TEXT,
                relationship_type TEXT,
                strength REAL
            )
        """)

        # Insert entities
        conn.execute("INSERT INTO entities VALUES ('e1', 'Python', 'language')")
        conn.execute("INSERT INTO entities VALUES ('e2', 'Django', 'framework')")

        # Insert facts with proper entity_id links
        now = time.time()
        conn.execute("INSERT INTO facts VALUES ('f1', 'e1', 'Python is a programming language', 0.9, 'active', ?)", (now,))
        conn.execute("INSERT INTO facts VALUES ('f2', 'e2', 'Django is a web framework', 0.85, 'active', ?)", (now,))
        conn.execute("INSERT INTO facts VALUES ('f3', 'e1', 'Python supports multiple paradigms', 0.8, 'active', ?)", (now,))

        # Create relationship
        conn.execute("INSERT INTO entity_relationships VALUES ('r1', 'e2', 'e1', 'uses', 0.9)")
        conn.commit()

        # Query facts using proper JOIN (not LIKE)
        facts = conn.execute("""
            SELECT f.fact_text, f.confidence
            FROM facts f
            WHERE f.status='active' AND (f.subject_entity_id = ? OR f.subject_entity_id IN (
                SELECT entity_b_id FROM entity_relationships WHERE entity_a_id = ?
            ))
            ORDER BY f.confidence DESC LIMIT 5
        """, ('e2', 'e2')).fetchall()

        # Should get Django fact + Python facts (through relationship)
        assert len(facts) >= 2
        assert any('Django' in f[0] for f in facts)
        assert any('Python' in f[0] for f in facts)
        conn.close()
    print("✓ Entity-fact linking test passed")


if __name__ == "__main__":
    test_add_relationship()
    test_graph_traversal_depth()
    test_relationship_weighting()
    test_entity_fact_linking()
    print("\n✅ All entity graph tests passed!")
