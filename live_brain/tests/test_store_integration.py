"""End-to-end integration test for LiveBrainStore.

Exercises the full stack: store init + all 6 migrations → ingest turns → FTS5
search → causal belief marking → self-evolution proposal/decide cycle. Runs on
a fresh temp DB so it never touches production data.

Operates without the Hermes runtime (only plugin directory on sys.path), so it
can double as a CI smoke test.
"""
from __future__ import annotations

import sys
import tempfile
import time
import traceback
from pathlib import Path

# Ensure the plugin dir is importable
PLUGINS_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PLUGINS_DIR))

from live_brain.store import LiveBrainStore  # noqa: E402
from live_brain.ingest import Ingestor  # noqa: E402
from live_brain.retrieval import RetrievalRouter  # noqa: E402
from live_brain.causal import CausalManager  # noqa: E402
from live_brain.evolution import SelfEvolutionManager  # noqa: E402


def _fresh_store(tmpdir: str) -> LiveBrainStore:
    store = LiveBrainStore(str(Path(tmpdir) / "integration.db"))
    store.initialize_schema()
    return store


def test_full_migration_apply_on_fresh_db() -> None:
    """All 6 migrations + audit_spine_v1 apply on a fresh DB with no FAILED markers."""
    with tempfile.TemporaryDirectory() as tmp:
        store = _fresh_store(tmp)
        rows = store.conn.execute(
            "SELECT migration_id FROM schema_migrations ORDER BY applied_at"
        ).fetchall()
        ids = [r[0] for r in rows]
        expected = [
            "audit_spine_v1",
            "000_base_schema",
            "001_extraction_method",
            "002_entity_relationships",
            "003_dialectic_syntheses",
            "004_user_profiles",
            "005_concept_vectors",
            "006_fts5_search",
        ]
        for name in expected:
            assert name in ids, f"Migration {name} not applied; got: {ids}"
        failed = [i for i in ids if i.startswith("FAILED:")]
        assert not failed, f"Unexpected FAILED markers: {failed}"
    print("✓ All migrations apply cleanly on fresh DB, no FAILED markers")


def test_fts5_tables_exist_and_queryable() -> None:
    """FTS5 tables created by migration 006 accept data and respond to MATCH."""
    with tempfile.TemporaryDirectory() as tmp:
        store = _fresh_store(tmp)
        # Insert a recipe so the trigger populates the FTS index
        store.conn.execute(
            "INSERT INTO fix_recipes (recipe_id, problem_pattern, scope_key, tool_name, created_at, updated_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, 'active')",
            ("r1", "how to fix memory leak in live_brain", "test", "debug_tool", time.time(), time.time()),
        )
        store.conn.commit()

        # FTS5 MATCH should find it
        rows = store.conn.execute(
            "SELECT recipe_id FROM fix_recipes_fts WHERE fix_recipes_fts MATCH 'memory'"
        ).fetchall()
        assert len(rows) >= 1, f"Expected FTS match, got {rows}"
        assert rows[0][0] == "r1"
    print("✓ fix_recipes_fts populated via trigger, MATCH query works")


def test_ingest_turns_end_to_end() -> None:
    """Ingestor can store 3 turns and they are retrievable."""
    with tempfile.TemporaryDirectory() as tmp:
        store = _fresh_store(tmp)
        # Create a session row
        store.conn.execute(
            "INSERT OR REPLACE INTO sessions (session_id, platform, started_at) VALUES (?, ?, ?)",
            ("sess-1", "test", time.time()),
        )
        store.conn.commit()

        ingestor = Ingestor(store.conn)
        now = time.time()
        for i in range(3):
            ingestor.ingest_turn(
                session_id="sess-1",
                scope_key="scope-A",
                turn_index=i,
                user_text=f"User message {i} talking about fix_recipes and memory",
                assistant_text=f"Assistant reply {i} with actionable step",
                created_at=now + i,
            )
        store.conn.commit()

        count = store.conn.execute(
            "SELECT COUNT(*) FROM turns WHERE session_id = 'sess-1'"
        ).fetchone()[0]
        assert count == 3, f"Expected 3 turns, got {count}"
    print("✓ Ingestor stored 3 turns end-to-end")


def test_retrieval_router_briefing() -> None:
    """RetrievalRouter.build_briefing produces a string without exceptions (may be empty on empty DB)."""
    with tempfile.TemporaryDirectory() as tmp:
        store = _fresh_store(tmp)
        router = RetrievalRouter(store.conn, hermes_home=tmp)
        result = router.build_briefing(scope_key="scope-A", query="what did we work on")
        assert isinstance(result, str), f"Expected str, got {type(result).__name__}"
    print("✓ RetrievalRouter.build_briefing returns string on empty DB")


def test_causal_mark_belief() -> None:
    """CausalManager.mark_belief creates and updates beliefs."""
    with tempfile.TemporaryDirectory() as tmp:
        store = _fresh_store(tmp)
        causal = CausalManager(store.conn, store=store)
        result = causal.mark_belief(
            belief_id=None,
            claim_text="FTS5 rowid is reserved",
            action="hypothesis",
            evidence_text="sqlite docs confirm",
            session_id="sess-1",
            scope_key="scope-A",
        )
        assert isinstance(result, dict), f"Expected dict, got {type(result).__name__}"
        # Subsequent validate
        bid = result.get("belief_id") or ""
        if bid:
            r2 = causal.mark_belief(
                belief_id=bid,
                claim_text="FTS5 rowid is reserved",
                action="validated",
                session_id="sess-1",
                scope_key="scope-A",
            )
            assert isinstance(r2, dict)
        row = store.conn.execute(
            "SELECT COUNT(*) FROM beliefs WHERE claim_text = ?",
            ("FTS5 rowid is reserved",),
        ).fetchone()
        assert row[0] >= 1, "Belief should be persisted"
    print("✓ CausalManager.mark_belief creates and updates beliefs")


def test_self_evolution_propose_and_decide() -> None:
    """SelfEvolutionManager.propose + .decide full cycle persists to DB."""
    with tempfile.TemporaryDirectory() as tmp:
        store = _fresh_store(tmp)
        evo = SelfEvolutionManager(store.conn)
        result = evo.propose(
            scope_key="scope-A",
            session_id="sess-1",
            trigger_text="integration test trigger",
            proposal_type="code_patch",
            target_area="code",
            rationale="verify propose path works",
            proposed_action="no-op test proposal",
            evidence={"test": True},
            suggested_tests=["run test_store_integration"],
            auto_apply=False,
        )
        assert isinstance(result, dict), f"Expected dict, got {type(result).__name__}"
        pid = result.get("proposal_id") or ""
        assert pid, f"Proposal id missing: {result}"

        # Decide: reject
        decided = evo.decide(pid, decision="rejected", reason="integration test cleanup")
        assert isinstance(decided, dict)
        row = store.conn.execute(
            "SELECT status FROM self_evolution_proposals WHERE proposal_id = ?",
            (pid,),
        ).fetchone()
        assert row is not None, "Proposal should be persisted"
        assert row[0] == "rejected", f"Expected status=rejected, got {row[0]}"
    print("✓ SelfEvolutionManager propose+decide cycle works end-to-end")


def test_production_db_readable_readonly() -> None:
    """Sanity check: production DB (if present) opens clean with our schema validator.

    This test is read-only and skipped automatically if the production DB is missing.
    """
    prod_db = Path.home() / ".hermes" / "live_brain" / "live_brain.db"
    if not prod_db.exists():
        print("  (skipped — production DB not present)")
        return
    import sqlite3
    conn = sqlite3.connect(f"file:{prod_db}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT migration_id FROM schema_migrations").fetchall()
        ids = {r[0] for r in rows}
        failed = [i for i in ids if i.startswith("FAILED:")]
        assert not failed, f"Production DB has FAILED migrations: {failed}"
        assert "006_fts5_search" in ids, f"Production DB missing 006_fts5_search; has {ids}"
    finally:
        conn.close()
    print("✓ Production DB schema_migrations clean (no FAILED markers, 006 applied)")


def run_tests() -> bool:
    tests = [
        ("test_full_migration_apply_on_fresh_db", test_full_migration_apply_on_fresh_db),
        ("test_fts5_tables_exist_and_queryable", test_fts5_tables_exist_and_queryable),
        ("test_ingest_turns_end_to_end", test_ingest_turns_end_to_end),
        ("test_retrieval_router_briefing", test_retrieval_router_briefing),
        ("test_causal_mark_belief", test_causal_mark_belief),
        ("test_self_evolution_propose_and_decide", test_self_evolution_propose_and_decide),
        ("test_production_db_readable_readonly", test_production_db_readable_readonly),
    ]
    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
        except AssertionError as e:
            print(f"✗ {name}: {e}")
            failed += 1
        except Exception as e:
            print(f"✗ {name}: ERROR — {type(e).__name__}: {e}")
            traceback.print_exc(limit=4)
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    ok = run_tests()
    sys.exit(0 if ok else 1)
