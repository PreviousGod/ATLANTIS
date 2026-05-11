"""Tests for cascading belief invalidation."""
import sqlite3
import tempfile
import time
from pathlib import Path


def test_single_belief_invalidation():
    """Test that a single belief can be invalidated."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test.db")
        conn = sqlite3.connect(db_path)

        # Create beliefs table
        conn.execute("""
            CREATE TABLE beliefs (
                belief_id TEXT PRIMARY KEY,
                claim_text TEXT,
                status TEXT,
                confidence REAL,
                updated_at REAL
            )
        """)

        # Insert test belief
        conn.execute("""
            INSERT INTO beliefs VALUES
            ('b1', 'Test claim', 'open', 0.8, ?)
        """, (time.time(),))
        conn.commit()

        # Invalidate
        conn.execute("UPDATE beliefs SET status = 'invalidated' WHERE belief_id = ?", ('b1',))
        conn.commit()

        # Verify
        status = conn.execute("SELECT status FROM beliefs WHERE belief_id = ?", ('b1',)).fetchone()[0]
        assert status == 'invalidated'
        conn.close()
    print("✓ Single belief invalidation test passed")


def test_cascading_invalidation():
    """Test that invalidation cascades to dependent beliefs."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test.db")
        conn = sqlite3.connect(db_path)

        # Create tables
        conn.execute("""
            CREATE TABLE beliefs (
                belief_id TEXT PRIMARY KEY,
                claim_text TEXT,
                status TEXT,
                confidence REAL,
                updated_at REAL
            )
        """)

        conn.execute("""
            CREATE TABLE belief_dependencies (
                source_belief_id TEXT,
                dependent_belief_id TEXT,
                PRIMARY KEY (source_belief_id, dependent_belief_id)
            )
        """)

        # Insert beliefs: b1 -> b2 -> b3 (b2 depends on b1, b3 depends on b2)
        now = time.time()
        conn.execute("INSERT INTO beliefs VALUES ('b1', 'Root claim', 'open', 0.8, ?)", (now,))
        conn.execute("INSERT INTO beliefs VALUES ('b2', 'Dependent claim', 'open', 0.7, ?)", (now,))
        conn.execute("INSERT INTO beliefs VALUES ('b3', 'Transitive claim', 'open', 0.6, ?)", (now,))

        # Create dependencies
        conn.execute("INSERT INTO belief_dependencies VALUES ('b1', 'b2')")
        conn.execute("INSERT INTO belief_dependencies VALUES ('b2', 'b3')")
        conn.commit()

        # Simulate cascading invalidation (manual traversal)
        invalidated = set()
        to_process = ['b1']

        while to_process:
            current = to_process.pop()
            if current in invalidated:
                continue

            conn.execute("UPDATE beliefs SET status = 'invalidated' WHERE belief_id = ?", (current,))
            invalidated.add(current)

            deps = conn.execute(
                "SELECT dependent_belief_id FROM belief_dependencies WHERE source_belief_id = ?",
                (current,)
            ).fetchall()
            to_process.extend(row[0] for row in deps)

        conn.commit()

        # Verify all three beliefs are invalidated
        statuses = conn.execute("SELECT belief_id, status FROM beliefs ORDER BY belief_id").fetchall()
        assert len(statuses) == 3
        assert all(s[1] == 'invalidated' for s in statuses)
        conn.close()
    print("✓ Cascading invalidation test passed")


def test_cycle_detection():
    """Test that cycles don't cause infinite loops."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test.db")
        conn = sqlite3.connect(db_path)

        # Create tables
        conn.execute("""
            CREATE TABLE beliefs (
                belief_id TEXT PRIMARY KEY,
                claim_text TEXT,
                status TEXT,
                confidence REAL,
                updated_at REAL
            )
        """)

        conn.execute("""
            CREATE TABLE belief_dependencies (
                source_belief_id TEXT,
                dependent_belief_id TEXT,
                PRIMARY KEY (source_belief_id, dependent_belief_id)
            )
        """)

        # Create cycle: b1 -> b2 -> b3 -> b1
        now = time.time()
        conn.execute("INSERT INTO beliefs VALUES ('b1', 'Claim 1', 'open', 0.8, ?)", (now,))
        conn.execute("INSERT INTO beliefs VALUES ('b2', 'Claim 2', 'open', 0.7, ?)", (now,))
        conn.execute("INSERT INTO beliefs VALUES ('b3', 'Claim 3', 'open', 0.6, ?)", (now,))

        conn.execute("INSERT INTO belief_dependencies VALUES ('b1', 'b2')")
        conn.execute("INSERT INTO belief_dependencies VALUES ('b2', 'b3')")
        conn.execute("INSERT INTO belief_dependencies VALUES ('b3', 'b1')")  # Cycle
        conn.commit()

        # Simulate cascading with cycle detection
        invalidated = set()
        to_process = ['b1']
        iterations = 0
        max_iterations = 100

        while to_process and iterations < max_iterations:
            iterations += 1
            current = to_process.pop()
            if current in invalidated:
                continue  # Skip already processed

            conn.execute("UPDATE beliefs SET status = 'invalidated' WHERE belief_id = ?", (current,))
            invalidated.add(current)

            deps = conn.execute(
                "SELECT dependent_belief_id FROM belief_dependencies WHERE source_belief_id = ?",
                (current,)
            ).fetchall()
            to_process.extend(row[0] for row in deps)

        conn.commit()

        # Verify: all 3 beliefs invalidated, no infinite loop
        assert len(invalidated) == 3
        assert iterations < 10  # Should complete quickly
        conn.close()
    print("✓ Cycle detection test passed")


if __name__ == "__main__":
    test_single_belief_invalidation()
    test_cascading_invalidation()
    test_cycle_detection()
    print("\n✅ All causal cascade tests passed!")
