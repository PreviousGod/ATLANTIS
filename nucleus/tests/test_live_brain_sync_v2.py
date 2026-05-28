from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR.parent))

from nucleus.live_brain_sync import LiveBrainSync  # noqa: E402


class FakePargod:
    def __init__(self):
        self.nodes = {}

    def get_node(self, label):
        return self.nodes.get(label)

    def add_node(self, node_type, label, text):
        self.nodes[label] = {"type": node_type, "text": text}


def _init_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE work_items (
            work_item_id TEXT PRIMARY KEY,
            title TEXT,
            status TEXT,
            priority REAL,
            updated_at REAL
        );
        CREATE TABLE memory_objects (
            object_id TEXT PRIMARY KEY,
            object_type TEXT NOT NULL,
            scope_key TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL DEFAULT '',
            source_event_ids_json TEXT NOT NULL DEFAULT '[]',
            source_session_ids_json TEXT NOT NULL DEFAULT '[]',
            title TEXT NOT NULL DEFAULT '',
            body TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            confidence REAL NOT NULL DEFAULT 0.5,
            priority REAL NOT NULL DEFAULT 0.5,
            relevance_tags_json TEXT NOT NULL DEFAULT '[]',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            expires_at REAL,
            superseded_by TEXT NOT NULL DEFAULT '',
            nucleus_eligible INTEGER NOT NULL DEFAULT 0,
            source_kind TEXT NOT NULL DEFAULT 'compiler',
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        """
    )
    now = time.time()
    conn.execute(
        "INSERT INTO work_items VALUES ('work:raw', 'raw task must not sync', 'active', 1.0, ?)",
        (now,),
    )
    conn.execute(
        """INSERT INTO memory_objects
           (object_id, object_type, scope_key, title, body, status, confidence, priority,
            created_at, updated_at, nucleus_eligible)
           VALUES ('cause:sync', 'validated_cause', 'nucleus', 'Validated timeout cause',
                   'busy_timeout must be 30000', 'active', 0.9, 0.8, ?, ?, 1)""",
        (now, now),
    )
    conn.commit()
    conn.close()


def test_sync_to_pargod_uses_compiled_objects_not_work_items() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "live_brain.db"
        _init_db(db_path)
        sync = LiveBrainSync(db_path)
        pargod = FakePargod()

        added = sync.sync_to_pargod(pargod)

        assert added == 1, pargod.nodes
        labels = set(pargod.nodes)
        assert any(label.startswith("mem_cause:sync"[:16]) or label.startswith("mem_cause") for label in labels), labels
        assert all("raw task" not in node["text"] for node in pargod.nodes.values())


def test_write_artifact_uses_stable_id() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "live_brain.db"
        _init_db(db_path)
        sync = LiveBrainSync(db_path)
        artifact_path = Path(tmp) / "artifact.txt"
        artifact_path.write_text("hello", encoding="utf-8")

        first_id = sync.write_artifact(str(artifact_path), "Artifact label", project_key="nucleus")
        second_id = sync.write_artifact(str(artifact_path), "Artifact label", project_key="nucleus")

        assert first_id == second_id
        assert first_id is not None


if __name__ == "__main__":
    test_sync_to_pargod_uses_compiled_objects_not_work_items()
    test_write_artifact_uses_stable_id()
    print("test_live_brain_sync_v2: PASS")
