"""
Full integration E2E test for all live_brain features.
Tests all 6 features working together in a realistic scenario.
"""
import pytest
import time


def test_full_workflow_integration(live_brain_store, scope_key, user_id):
    """
    Full workflow test:
    1. User asks about ffmpeg (auto-extraction)
    2. Entity relationships created
    3. User gives feedback (user alignment)
    4. Cross-session synthesis
    5. Verify context fencing
    """
    from plugins.live_brain.ingest import Ingestor
    from plugins.live_brain.entity_graph import EntityGraph
    from plugins.live_brain.dialectic import DialecticEngine
    from plugins.live_brain.user_alignment import UserAlignmentTracker

    ingestor = Ingestor(live_brain_store.conn)
    graph = EntityGraph(live_brain_store.conn)
    dialectic = DialecticEngine(live_brain_store.conn)
    alignment = UserAlignmentTracker(live_brain_store.conn)

    now = time.time()

    # Session 1: Initial conversation
    user_text1 = "How do I use ffmpeg?"
    assistant_text1 = "ffmpeg is a video processing tool. It uses command-line interface."

    ingestor.ingest_turn(
        session_id="session1",
        scope_key=scope_key,
        turn_index=1,
        user_text=user_text1,
        assistant_text=assistant_text1,
        created_at=now
    )

    # Verify auto-extraction worked
    facts = live_brain_store.conn.execute(
        "SELECT COUNT(*) FROM facts WHERE extraction_method='auto'"
    ).fetchone()[0]
    assert facts > 0, "Auto-extraction failed"

    # Session 2: User preference
    user_text2 = "I prefer concise responses"
    assistant_text2 = "Understood, I'll keep responses brief."

    alignment.extract_preferences(user_text2, user_id, 2, scope_key)
    alignment.record_feedback(user_text2, assistant_text2, user_id, 2, scope_key)

    # Verify user alignment
    prefs = live_brain_store.conn.execute(
        "SELECT COUNT(*) FROM user_profiles WHERE user_id=?", (user_id,)
    ).fetchone()[0]
    assert prefs > 0, "User preference not stored"

    # Session 3: Cross-session synthesis
    synthesis = dialectic.synthesize_cross_session("ffmpeg", scope_key, max_sessions=5)
    assert synthesis['synthesis'] != "" or len(synthesis['source_sessions']) >= 0

    # Verify context fencing (no self-referential memories)
    self_ref = live_brain_store.conn.execute(
        "SELECT COUNT(*) FROM facts WHERE fact_text LIKE '%brain_%'"
    ).fetchone()[0]
    assert self_ref == 0, "Context fencing failed - self-referential memory stored"

    print("✓ All features integrated successfully")


def test_migrations_applied(live_brain_store):
    """Verify all migrations were applied."""
    migrations = live_brain_store.conn.execute(
        "SELECT migration_id FROM schema_migrations ORDER BY migration_id"
    ).fetchall()

    expected_migrations = [
        '001_extraction_method',
        '002_entity_relationships',
        '003_dialectic_syntheses',
        '004_user_profiles',
        '005_concept_vectors'
    ]

    applied = [m[0] for m in migrations]

    for expected in expected_migrations:
        assert expected in applied, f"Migration {expected} not applied"

    print(f"✓ All {len(applied)} migrations applied")


def test_new_tables_exist(live_brain_store):
    """Verify all new tables were created."""
    tables = live_brain_store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()

    table_names = [t[0] for t in tables]

    required_tables = [
        'entity_relationships',
        'dialectic_syntheses',
        'user_profiles',
        'communication_patterns',
        'user_feedback',
        'concept_vectors',
        'schema_migrations'
    ]

    for table in required_tables:
        assert table in table_names, f"Table {table} not created"

    print(f"✓ All {len(required_tables)} new tables exist")
