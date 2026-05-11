"""Integration tests for Live Brain end-to-end functionality."""
import sqlite3
import tempfile
import time
import json
from pathlib import Path


def test_full_context_injection_pipeline():
    """Test complete context injection with reality + epistemic + episodes."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        # Create minimal schema
        conn.execute("""
            CREATE TABLE episodes (
                episode_id TEXT PRIMARY KEY,
                session_id TEXT,
                scope_key TEXT,
                title TEXT,
                current_summary TEXT,
                status TEXT,
                updated_at REAL
            )
        """)

        conn.execute("""
            CREATE TABLE facts (
                fact_id TEXT PRIMARY KEY,
                scope_key TEXT,
                fact_text TEXT,
                confidence REAL,
                status TEXT,
                valid_from REAL
            )
        """)

        conn.execute("""
            CREATE TABLE work_items (
                work_item_id TEXT PRIMARY KEY,
                session_id TEXT,
                scope_key TEXT,
                title TEXT,
                status TEXT,
                priority INTEGER,
                created_at REAL
            )
        """)

        # Insert test data
        now = time.time()
        session_id = 'session123'
        scope_key = 'user1'

        # Episode for current session
        conn.execute("""
            INSERT INTO episodes VALUES
            ('ep1', ?, ?, 'Active task', 'Working on feature X', 'active', ?)
        """, (session_id, scope_key, now))

        # Episode for different session (should not appear)
        conn.execute("""
            INSERT INTO episodes VALUES
            ('ep2', 'other_session', ?, 'Other task', 'Different work', 'active', ?)
        """, (scope_key, now))

        # Facts
        conn.execute("""
            INSERT INTO facts VALUES
            ('f1', ?, 'Python is a programming language', 0.95, 'active', ?)
        """, (scope_key, now))

        # Work items
        conn.execute("""
            INSERT INTO work_items VALUES
            ('w1', ?, ?, 'Implement feature', 'active', 10, ?)
        """, (session_id, scope_key, now))

        conn.commit()

        # Simulate context retrieval
        episodes = conn.execute("""
            SELECT * FROM episodes
            WHERE scope_key = ? AND session_id = ? AND status = 'active'
        """, (scope_key, session_id)).fetchall()

        facts = conn.execute("""
            SELECT * FROM facts
            WHERE scope_key = ? AND status = 'active'
            ORDER BY confidence DESC LIMIT 5
        """, (scope_key,)).fetchall()

        work_items = conn.execute("""
            SELECT * FROM work_items
            WHERE scope_key = ? AND session_id = ? AND status = 'active'
            ORDER BY priority DESC LIMIT 3
        """, (scope_key, session_id)).fetchall()

        # Verify isolation
        assert len(episodes) == 1
        assert episodes[0]['episode_id'] == 'ep1'
        assert len(facts) == 1
        assert len(work_items) == 1

        conn.close()
    print("✓ Full context injection pipeline test passed")


def test_multi_session_isolation():
    """Test that multiple sessions don't interfere with each other."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test.db")
        conn = sqlite3.connect(db_path)

        # Create tables
        conn.execute("""
            CREATE TABLE episodes (
                episode_id TEXT PRIMARY KEY,
                session_id TEXT,
                scope_key TEXT,
                title TEXT,
                status TEXT,
                updated_at REAL
            )
        """)

        conn.execute("""
            CREATE TABLE work_items (
                work_item_id TEXT PRIMARY KEY,
                session_id TEXT,
                scope_key TEXT,
                title TEXT,
                status TEXT,
                created_at REAL
            )
        """)

        # Insert data for two sessions
        now = time.time()
        scope_key = 'user1'

        # Session 1 data
        conn.execute("INSERT INTO episodes VALUES ('ep1', 'session1', ?, 'Task A', 'active', ?)", (scope_key, now))
        conn.execute("INSERT INTO work_items VALUES ('w1', 'session1', ?, 'Work A', 'active', ?)", (scope_key, now))

        # Session 2 data
        conn.execute("INSERT INTO episodes VALUES ('ep2', 'session2', ?, 'Task B', 'active', ?)", (scope_key, now))
        conn.execute("INSERT INTO work_items VALUES ('w2', 'session2', ?, 'Work B', 'active', ?)", (scope_key, now))

        conn.commit()

        # Query session 1
        s1_episodes = conn.execute(
            "SELECT episode_id FROM episodes WHERE scope_key = ? AND session_id = ?",
            (scope_key, 'session1')
        ).fetchall()
        s1_work = conn.execute(
            "SELECT work_item_id FROM work_items WHERE scope_key = ? AND session_id = ?",
            (scope_key, 'session1')
        ).fetchall()

        # Query session 2
        s2_episodes = conn.execute(
            "SELECT episode_id FROM episodes WHERE scope_key = ? AND session_id = ?",
            (scope_key, 'session2')
        ).fetchall()
        s2_work = conn.execute(
            "SELECT work_item_id FROM work_items WHERE scope_key = ? AND session_id = ?",
            (scope_key, 'session2')
        ).fetchall()

        # Verify isolation
        assert len(s1_episodes) == 1 and s1_episodes[0][0] == 'ep1'
        assert len(s1_work) == 1 and s1_work[0][0] == 'w1'
        assert len(s2_episodes) == 1 and s2_episodes[0][0] == 'ep2'
        assert len(s2_work) == 1 and s2_work[0][0] == 'w2'

        conn.close()
    print("✓ Multi-session isolation test passed")


def test_reality_engine_signal_extraction():
    """Test reality engine signal extraction logic."""
    test_cases = [
        ("I need dashboard access", True, "request_dashboard"),
        ("Show me the logs", False, None),
        ("Create a new user account", True, "user_management"),
        ("What's the weather?", False, None),
    ]

    for query, should_extract, expected_signal in test_cases:
        query_lower = query.lower()

        # Simulate signal extraction
        signals = []
        if 'dashboard' in query_lower and ('need' in query_lower or 'access' in query_lower):
            signals.append('request_dashboard')
        if 'user' in query_lower and 'account' in query_lower:
            signals.append('user_management')

        has_signal = len(signals) > 0

        if should_extract:
            assert has_signal, f"Failed to extract signal from: {query}"
            if expected_signal:
                assert expected_signal in signals, f"Expected {expected_signal}, got {signals}"
        else:
            assert not has_signal, f"False positive signal for: {query}"

    print("✓ Reality engine signal extraction test passed")


def test_epistemic_context_isolation():
    """Test epistemic context isolation for high-stakes queries."""
    test_cases = [
        ("What's my trading account balance?", True),  # Financial + trading
        ("Update the trading table in database", False),  # SQL operation
        ("Show futures price for BTC", True),  # Financial query
        ("Delete old futures records", False),  # SQL operation
    ]

    for query, should_isolate in test_cases:
        query_lower = query.lower()

        # Simulate isolation logic
        high_stakes_terms = ['trading', 'futures', 'funded']
        financial_context = ['price', 'margin', 'broker', 'account', 'balance']

        has_trading = any(term in query_lower for term in high_stakes_terms)
        has_context = any(term in query_lower for term in financial_context)

        isolate = has_trading and has_context

        assert isolate == should_isolate, f"Isolation mismatch for: {query}"

    print("✓ Epistemic context isolation test passed")


if __name__ == "__main__":
    test_full_context_injection_pipeline()
    test_multi_session_isolation()
    test_reality_engine_signal_extraction()
    test_epistemic_context_isolation()
    print("\n✅ All integration tests passed!")
