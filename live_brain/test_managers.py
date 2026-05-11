"""Unit tests for refactored manager classes."""
import sys
import sqlite3
import tempfile
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from live_brain.schema_manager import SchemaManager
from live_brain.backup_manager import BackupManager
from live_brain.maintenance_manager import MaintenanceManager


def test_schema_manager_initialization():
    """Test SchemaManager can initialize schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    mgr = SchemaManager(conn)
    mgr.initialize_schema()

    # Verify core tables exist
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    table_names = {row[0] for row in tables}

    assert 'work_items' in table_names
    assert 'episodes' in table_names
    assert 'facts' in table_names
    assert 'beliefs' in table_names
    assert 'reality_events' in table_names

    conn.close()
    print("✓ SchemaManager initialization test passed")


def test_backup_manager_checkpoint():
    """Test BackupManager WAL checkpoint."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE test (id INTEGER)")
        conn.commit()

        mgr = BackupManager(conn, db_path)
        result = mgr.checkpoint_wal(truncate=False)

        assert result['status'] == 'ok'
        assert result['mode'] == 'passive'

        conn.close()
        print("✓ BackupManager checkpoint test passed")


def test_backup_manager_rotation():
    """Test BackupManager backup rotation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        # Create dummy backup files
        backup_dir = Path(tmpdir)
        for i in range(5):
            (backup_dir / f"test_backup_{i}.db").touch()

        mgr = BackupManager(conn, db_path)
        result = mgr.rotate_backups(max_age_hours=0.001, max_keep=2, dry_run=False)

        assert result['status'] == 'ok'
        assert result['deleted'] >= 3  # Should delete at least 3 old backups

        conn.close()
        print("✓ BackupManager rotation test passed")


if __name__ == "__main__":
    test_schema_manager_initialization()
    test_backup_manager_checkpoint()
    test_backup_manager_rotation()
    print("\n✅ All manager tests passed!")
