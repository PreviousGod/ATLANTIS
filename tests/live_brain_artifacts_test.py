#!/usr/bin/env python3
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live_brain.artifacts import ArtifactRegistry
from live_brain.store import LiveBrainStore


def test_resolver_prefers_verified_existing_path() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / 'brain.db')
        artifact = Path(tmp) / 'enoch_part2_CORRECT.mp4'
        artifact.write_bytes(b'fake-video')
        store = LiveBrainStore(db)
        store.initialize_schema()
        registry = ArtifactRegistry(store.conn)
        registry.upsert_artifact(project_key='enoch', role='part_2', path=str(artifact), label='correct')
        store.conn.commit()
        result = registry.resolve('enoch', 'part 2')
        assert result['status'] == 'resolved'
        assert result['path'] == str(artifact)
        store.close()


def test_context_lines_include_verified_artifact_not_wrong_part1() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / 'brain.db')
        part2 = Path(tmp) / 'enoch_part2_CORRECT.mp4'
        wrong = Path(tmp) / 'enoch_part1_1.mp4'
        part2.write_bytes(b'correct')
        wrong.write_bytes(b'wrong')
        store = LiveBrainStore(db)
        store.initialize_schema()
        registry = ArtifactRegistry(store.conn)
        registry.upsert_artifact(project_key='enoch', role='part_2', path=str(part2), label='correct part 2')
        registry.upsert_artifact(project_key='enoch', role='part_1_old', path=str(wrong), label='old part 1', status='rejected')
        store.conn.commit()
        lines = registry.context_lines_for_query('pošalji mi Enoch part 2 video')
        joined = '\n'.join(lines)
        assert 'enoch_part2_CORRECT.mp4' in joined
        assert 'enoch_part1_1.mp4' not in joined
        store.close()


def test_context_lines_infer_single_project_from_part_roles() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / 'brain.db')
        part1 = Path(tmp) / 'enoch_part1_VERIFIED.mp4'
        part2 = Path(tmp) / 'enoch_part2_CORRECT.mp4'
        part1.write_bytes(b'part1')
        part2.write_bytes(b'part2')
        store = LiveBrainStore(db)
        store.initialize_schema()
        registry = ArtifactRegistry(store.conn)
        registry.upsert_artifact(project_key='enoch', role='part_1', path=str(part1), label='correct part 1')
        registry.upsert_artifact(project_key='enoch', role='part_2', path=str(part2), label='correct part 2')
        store.conn.commit()
        lines = registry.context_lines_for_query('posalji mi sad part 1 i part 2 video')
        joined = '\n'.join(lines)
        assert 'project=enoch role=part_1' in joined
        assert 'project=enoch role=part_2' in joined
        store.close()


def test_missing_verified_artifact_is_not_resolved() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / 'brain.db')
        missing = Path(tmp) / 'missing.mp4'
        store = LiveBrainStore(db)
        store.initialize_schema()
        registry = ArtifactRegistry(store.conn)
        registry.upsert_artifact(project_key='enoch', role='part_2', path=str(missing), label='missing')
        store.conn.commit()
        result = registry.resolve('enoch', 'part_2')
        assert result['status'] == 'needs_resolution'
        row = store.conn.execute('SELECT status FROM verified_artifacts WHERE path=?', (str(missing),)).fetchone()
        assert row['status'] == 'missing'
        store.close()

def test_self_heal_archives_destructive_non_negated_episode_only() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / 'brain.db')
        store = LiveBrainStore(db)
        store.initialize_schema()
        now = 1234.0
        store.conn.execute(
            "INSERT INTO episodes (episode_id, kind, title, status, current_summary, opened_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ('danger', 'general', 'Izbrisi video Ancient_scroll.mp4', 'dormant', 'old delete request', now, now),
        )
        store.conn.execute(
            "INSERT INTO episodes (episode_id, kind, title, status, current_summary, opened_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ('safe', 'general', 'ne brisi video nikad', 'dormant', 'explicit safety rule', now, now),
        )
        store.conn.commit()
        result = store.suppress_destructive_episode_memory()
        assert result['archived'] == 1
        danger = store.conn.execute("SELECT status FROM episodes WHERE episode_id='danger'").fetchone()['status']
        safe = store.conn.execute("SELECT status FROM episodes WHERE episode_id='safe'").fetchone()['status']
        assert danger == 'archived'
        assert safe == 'dormant'
        audit = store.conn.execute("SELECT COUNT(*) c FROM audit_log WHERE object_id='danger' AND reason='destructive_stale_memory_guard'").fetchone()['c']
        assert audit == 1
        store.close()


if __name__ == '__main__':
    test_resolver_prefers_verified_existing_path()
    test_context_lines_include_verified_artifact_not_wrong_part1()
    test_context_lines_infer_single_project_from_part_roles()
    test_missing_verified_artifact_is_not_resolved()
    test_self_heal_archives_destructive_non_negated_episode_only()
    print('live_brain_artifacts_test: PASS')
