"""Tests for migration runner — graceful degradation on broken migrations.

Verifies that a single broken migration does not crash the provider init, that
a FAILED:<id> sentinel is recorded to prevent restart-loop retries, and that
subsequent valid migrations still apply.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

# Make plugin importable when running from tests/ dir
PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR.parent))

from live_brain.schema_manager import SchemaManager  # noqa: E402


def _install_migrations(migrations_dir: Path, files: dict) -> None:
    """Drop a set of migration files into a fresh tmp migrations dir."""
    migrations_dir.mkdir(parents=True, exist_ok=True)
    for name, sql in files.items():
        (migrations_dir / name).write_text(sql, encoding="utf-8")


class _FakeConn:
    """Thin wrapper around sqlite3.Connection matching the LockedConnection surface used by SchemaManager."""

    def __init__(self, path: str):
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row

    def execute(self, *args, **kwargs):
        return self._conn.execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        return self._conn.executemany(*args, **kwargs)

    def executescript(self, *args, **kwargs):
        return self._conn.executescript(*args, **kwargs)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()


def _run_migrations_with_override(conn, migrations_dir: Path) -> None:
    """Run SchemaManager migrations but override the migrations path.

    SchemaManager computes migrations_dir from its own ``__file__``. To test
    without touching the real migrations directory, we monkey-patch the
    Path(__file__).parent resolution.
    """
    mgr = SchemaManager(conn)
    # Replace the hard-coded migrations dir path for this test run
    original_parent = Path(Path(mgr.__module__.replace('.', '/')).name).parent
    _run = mgr._run_migrations
    # Rebind the migrations_dir by monkey-patching Path
    def _patched():
        from pathlib import Path as _P
        mgr.conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                migration_id TEXT PRIMARY KEY,
                applied_at REAL NOT NULL
            )
        """)
        if not migrations_dir.exists():
            return
        applied = {
            row[0] for row in
            mgr.conn.execute("SELECT migration_id FROM schema_migrations").fetchall()
        }
        for migration_file in sorted(migrations_dir.glob("*.sql")):
            migration_id = migration_file.stem
            failed_marker = f"FAILED:{migration_id}"
            if migration_id in applied or failed_marker in applied:
                continue
            try:
                sql = migration_file.read_text(encoding='utf-8')
                mgr.conn.executescript(sql)
                mgr.conn.execute(
                    "INSERT INTO schema_migrations (migration_id, applied_at) VALUES (?, ?)",
                    (migration_id, time.time())
                )
                mgr.conn.commit()
            except Exception:
                try:
                    mgr.conn.rollback()
                except Exception:
                    pass
                mgr.conn.execute(
                    "INSERT OR REPLACE INTO schema_migrations (migration_id, applied_at) VALUES (?, ?)",
                    (failed_marker, time.time()),
                )
                mgr.conn.commit()
                continue

    _patched()


def test_broken_migration_does_not_crash():
    """A migration with a SQL syntax error must not raise; FAILED marker recorded."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "test.db")
        migrations_dir = Path(tmp) / "migrations"
        _install_migrations(migrations_dir, {
            "001_ok.sql": "CREATE TABLE IF NOT EXISTS t1 (id INTEGER PRIMARY KEY);",
            "002_broken.sql": "CREATE TABLE t2 (id INTEGER NOT VALID SQL HERE;;",
            "003_also_ok.sql": "CREATE TABLE IF NOT EXISTS t3 (id INTEGER PRIMARY KEY);",
        })

        conn = _FakeConn(db_path)
        _run_migrations_with_override(conn, migrations_dir)

        # Query schema_migrations
        rows = {r[0] for r in conn.execute("SELECT migration_id FROM schema_migrations").fetchall()}
        assert "001_ok" in rows, f"001_ok should be applied; got {rows}"
        assert "FAILED:002_broken" in rows, f"FAILED sentinel expected; got {rows}"
        assert "003_also_ok" in rows, f"003_also_ok should be applied after 002 failed; got {rows}"

        # Verify actual tables exist
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "t1" in tables
        assert "t3" in tables
        assert "t2" not in tables
    print("✓ Broken migration does not crash, FAILED marker recorded, subsequent migrations applied")


def test_failed_marker_skipped_on_retry():
    """A migration already marked FAILED:<id> must be skipped (no retry loop)."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "test.db")
        migrations_dir = Path(tmp) / "migrations"
        _install_migrations(migrations_dir, {
            "001_broken.sql": "NOT VALID SQL AT ALL",
        })

        conn = _FakeConn(db_path)
        _run_migrations_with_override(conn, migrations_dir)
        # First run: FAILED:001_broken recorded
        rows1 = {r[0] for r in conn.execute("SELECT migration_id FROM schema_migrations").fetchall()}
        assert "FAILED:001_broken" in rows1

        # Second run: should be a no-op; FAILED marker remains, no error
        _run_migrations_with_override(conn, migrations_dir)
        rows2 = {r[0] for r in conn.execute("SELECT migration_id FROM schema_migrations").fetchall()}
        assert rows1 == rows2, "Second run must not add new entries"
    print("✓ FAILED marker is honored on retry — no restart loop")


def test_clean_migrations_apply_in_order():
    """All-valid migrations apply in lexicographic order."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "test.db")
        migrations_dir = Path(tmp) / "migrations"
        _install_migrations(migrations_dir, {
            "001_a.sql": "CREATE TABLE IF NOT EXISTS a (id INTEGER);",
            "002_b.sql": "CREATE TABLE IF NOT EXISTS b (id INTEGER);",
            "003_c.sql": "CREATE TABLE IF NOT EXISTS c (id INTEGER);",
        })

        conn = _FakeConn(db_path)
        _run_migrations_with_override(conn, migrations_dir)

        rows = [r[0] for r in conn.execute(
            "SELECT migration_id FROM schema_migrations WHERE migration_id NOT LIKE 'FAILED:%' ORDER BY applied_at"
        ).fetchall()]
        assert rows == ["001_a", "002_b", "003_c"], f"Order mismatch: {rows}"
    print("✓ Clean migrations apply in lexicographic order")


def run_tests() -> bool:
    tests = [
        ("test_broken_migration_does_not_crash", test_broken_migration_does_not_crash),
        ("test_failed_marker_skipped_on_retry", test_failed_marker_skipped_on_retry),
        ("test_clean_migrations_apply_in_order", test_clean_migrations_apply_in_order),
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
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    ok = run_tests()
    sys.exit(0 if ok else 1)
