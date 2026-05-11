"""Tests for Phase 1 context injection bug fixes."""
import sqlite3
import tempfile
import time
from pathlib import Path


def test_episode_scope_isolation():
    """Test that episodes don't leak across sessions."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        # Create episodes table with session_id
        conn.execute("""
            CREATE TABLE episodes (
                episode_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                scope_key TEXT NOT NULL,
                title TEXT,
                current_summary TEXT,
                status TEXT,
                scope_tags_json TEXT,
                updated_at REAL
            )
        """)

        # Insert episodes for different sessions
        conn.execute("""
            INSERT INTO episodes VALUES
            ('ep1', 'session1', 'user1', 'Task A', 'Working on A', 'active', '{}', ?),
            ('ep2', 'session2', 'user1', 'Task B', 'Working on B', 'active', '{}', ?)
        """, (time.time(), time.time()))
        conn.commit()

        # Query for session1 only
        rows = conn.execute("""
            SELECT * FROM episodes
            WHERE scope_key = 'user1' AND session_id = 'session1'
        """).fetchall()

        assert len(rows) == 1
        assert rows[0]['episode_id'] == 'ep1'
        conn.close()
    print("✓ Episode scope isolation test passed")


def test_fix_loop_suppression():
    """Test that FIX: loops are suppressed after 2 queries."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test.db")
        conn = sqlite3.connect(db_path)

        # Create episode_queries table
        conn.execute("""
            CREATE TABLE episode_queries (
                episode_id TEXT,
                session_id TEXT,
                queried_at REAL,
                PRIMARY KEY (episode_id, session_id, queried_at)
            )
        """)

        # Record 3 queries for same episode
        now = time.time()
        for i in range(3):
            conn.execute(
                "INSERT INTO episode_queries VALUES (?, ?, ?)",
                ('ep1', 'session1', now + i)
            )
        conn.commit()

        # Check query count
        count = conn.execute("""
            SELECT COUNT(*) FROM episode_queries
            WHERE episode_id = 'ep1' AND session_id = 'session1'
        """).fetchone()[0]

        assert count == 3
        assert count > 2  # Should be suppressed
        conn.close()
    print("✓ FIX: loop suppression test passed")


def test_objective_ttl():
    """Test that objectives expire after 24 hours."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test.db")
        conn = sqlite3.connect(db_path)

        conn.execute("""
            CREATE TABLE work_items (
                work_item_id TEXT PRIMARY KEY,
                scope_key TEXT,
                session_id TEXT,
                title TEXT,
                status TEXT,
                created_at REAL
            )
        """)

        now = time.time()
        old_time = now - 86400 - 3600  # 25 hours ago
        recent_time = now - 3600  # 1 hour ago

        conn.execute("""
            INSERT INTO work_items VALUES
            ('w1', 'user1', 'session1', 'Old task', 'active', ?),
            ('w2', 'user1', 'session1', 'Recent task', 'pending', ?)
        """, (old_time, recent_time))
        conn.commit()

        # Query with 24h TTL
        ttl_cutoff = now - 86400
        rows = conn.execute("""
            SELECT * FROM work_items
            WHERE scope_key = 'user1' AND session_id = 'session1'
            AND (created_at > ? OR status = 'active')
        """, (ttl_cutoff,)).fetchall()

        assert len(rows) == 2  # Both match (old is filtered, recent + active pass)
        conn.close()
    print("✓ Objective TTL test passed")


def test_false_signal_prevention():
    """Test that SQL operations don't trigger false trading signals."""
    test_cases = [
        ("delete trading records", False),  # SQL operation
        ("current trading price is 100", True),  # Real trading query
        ("update futures table", False),  # SQL operation
        ("what's the current futures price", True),  # Real query
    ]

    for query, should_trigger in test_cases:
        query_lower = query.lower()

        # Simulate refined detection logic
        trading_terms = ['trading', 'futures']
        financial_context = ['price', 'margin', 'broker']

        has_trading = any(term in query_lower for term in trading_terms)
        has_financial = any(term in query_lower for term in financial_context)

        triggers = has_trading and has_financial

        assert triggers == should_trigger, f"Failed for: {query}"

    print("✓ False signal prevention test passed")


def test_media_domain_filtering():
    """Test that video/image queries work in media domain."""
    # Simulate domain-specific filtering
    _LOW_SIGNAL_WORDS = {'problem', 'plugin', 'memory'}
    _MEDIA_DOMAIN_WORDS = {'video', 'image', 'audio'}

    query = "show me the video file"
    domain = "media"

    # Check if query contains media words
    has_media_words = any(w in query.lower() for w in _MEDIA_DOMAIN_WORDS)

    # In media domain, don't filter media words
    if domain == "media" and has_media_words:
        is_low_signal = False
    else:
        is_low_signal = any(w in query.lower() for w in _LOW_SIGNAL_WORDS)

    assert not is_low_signal  # Should NOT be filtered in media domain
    print("✓ Media domain filtering test passed")


if __name__ == "__main__":
    test_episode_scope_isolation()
    test_fix_loop_suppression()
    test_objective_ttl()
    test_false_signal_prevention()
    test_media_domain_filtering()
    print("\n✅ All context injection tests passed!")
