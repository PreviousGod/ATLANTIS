"""Regression tests for SensoryCortex resource ownership."""
import gc
import sqlite3
import sys
import warnings
from pathlib import Path

PLUGIN_PARENT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PLUGIN_PARENT))

from nucleus.sensory_cortex import SensoryCortex


def test_conversation_scan_closes_connection_on_query_failure(tmp_path):
    db_path = tmp_path / "brain.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE entity_meta (entity_id TEXT)")
        conn.commit()
    finally:
        conn.close()

    cortex = SensoryCortex(str(db_path))
    with warnings.catch_warnings():
        warnings.simplefilter("error", ResourceWarning)
        assert cortex._scan_conversation_gaps() == []
        gc.collect()

    print("✓ test_conversation_scan_closes_connection_on_query_failure")


def test_cwm_gap_scan_closes_connection_on_query_failure(tmp_path):
    db_path = tmp_path / "brain.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE causal_facts (scope_key TEXT)")
        conn.commit()
    finally:
        conn.close()

    cortex = SensoryCortex(str(db_path))
    with warnings.catch_warnings():
        warnings.simplefilter("error", ResourceWarning)
        assert cortex._scan_cwm_knowledge_gaps() == []
        gc.collect()

    print("✓ test_cwm_gap_scan_closes_connection_on_query_failure")


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_a:
        test_conversation_scan_closes_connection_on_query_failure(Path(tmp_a))
    with tempfile.TemporaryDirectory() as tmp_b:
        test_cwm_gap_scan_closes_connection_on_query_failure(Path(tmp_b))
    print("\n✅ SENSORY CORTEX TESTS PASSED")
