"""Live Brain Sync — bidirekcioni most između Nucleus i Hermes Live Brain."""
import logging
import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path

from .config import LIVE_BRAIN_DB

log = logging.getLogger("nucleus")


class LiveBrainSync:
    def __init__(self, db_path=None):
        self.db_path = str(db_path or LIVE_BRAIN_DB)

    def _conn(self, read_only: bool = False):
        """Open a connection to live_brain.db.

        Bumped busy_timeout to 30s (was 5s) so the Nucleus tick loop doesn't
        race the gateway's sync_turn writer. Pass read_only=True for query
        helpers to set PRAGMA query_only=ON, which prevents SQLite from
        acquiring an EXCLUSIVE lock during BEGIN — letting reads coexist
        with the gateway's writers without contention.
        """
        if not Path(self.db_path).exists():
            return None
        c = sqlite3.connect(self.db_path, timeout=30.0)
        c.execute("PRAGMA busy_timeout=30000")
        c.execute("PRAGMA journal_mode=WAL")
        if read_only:
            c.execute("PRAGMA query_only=ON")
        return c

    def _table_columns(self, conn, table):
        try:
            return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        except Exception:
            return set()

    def _has_table(self, conn, table):
        try:
            return conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone() is not None
        except Exception:
            return False

    def _stable_id(self, prefix, *parts):
        text = "\n".join(str(part) for part in parts)
        digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]
        return f"{prefix}_{digest}"

    def _ensure_memory_v2_schema(self, conn):
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory_events (
                    event_id TEXT PRIMARY KEY,
                    object_type TEXT NOT NULL DEFAULT '',
                    object_id TEXT NOT NULL DEFAULT '',
                    action TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    source_turn_id TEXT NOT NULL DEFAULT '',
                    source_event_id TEXT NOT NULL DEFAULT '',
                    details_json TEXT NOT NULL DEFAULT '{}',
                    confidence REAL NOT NULL DEFAULT 1.0,
                    created_at REAL NOT NULL,
                    event_type TEXT NOT NULL DEFAULT '',
                    session_id TEXT NOT NULL DEFAULT '',
                    scope_key TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    source TEXT NOT NULL DEFAULT 'nucleus',
                    eligible_for_compile INTEGER NOT NULL DEFAULT 1,
                    quarantined INTEGER NOT NULL DEFAULT 0,
                    event_fingerprint TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS memory_objects (
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
                    source_kind TEXT NOT NULL DEFAULT 'nucleus',
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_memory_objects_nucleus ON memory_objects(scope_key, nucleus_eligible, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_memory_objects_scope_type_status ON memory_objects(scope_key, object_type, status, updated_at DESC);
                """
            )
        except Exception as e:
            log.debug(f"ensure_memory_v2_schema failed: {e}")

    def _record_memory_event(self, conn, event_type, payload, scope_key="nucleus", session_id="", source="nucleus"):
        self._ensure_memory_v2_schema(conn)
        now = time.time()
        payload_json = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)
        event_id = self._stable_id("nucleus_event", event_type, scope_key, session_id, payload_json, int(now * 1000))
        conn.execute(
            """INSERT OR IGNORE INTO memory_events
               (event_id, object_type, object_id, action, reason, source_turn_id, source_event_id,
                details_json, confidence, created_at, event_type, session_id, scope_key,
                payload_json, source, eligible_for_compile, quarantined, event_fingerprint)
               VALUES (?, 'raw_event', ?, ?, '', '', '', '{}', 1.0, ?, ?, ?, ?, ?, ?, 1, 0, ?)""",
            (
                event_id, event_id, event_type, now, event_type, session_id,
                scope_key, payload_json, source,
                hashlib.sha256(payload_json.encode("utf-8", "replace")).hexdigest()[:24],
            ),
        )
        return event_id

    def _record_candidate_object(self, conn, object_type, payload, scope_key="nucleus", confidence=0.35):
        self._ensure_memory_v2_schema(conn)
        now = time.time()
        title = str(payload.get("title") or payload.get("problem") or payload.get("question") or object_type)[:500]
        body = str(payload.get("body") or payload.get("fact_text") or payload.get("proposed_action") or payload.get("steps") or payload.get("summary") or "")[:5000]
        object_id = payload.get("object_id") or self._stable_id("nucleus_obj", object_type, scope_key, title, body)
        words = sorted({w.lower() for w in re.findall(r"[\w./-]+", f"{title} {body}") if len(w) > 2})[:80]
        conn.execute(
            """INSERT INTO memory_objects
               (object_id, object_type, scope_key, session_id, source_event_ids_json,
                source_session_ids_json, title, body, status, confidence, priority,
                relevance_tags_json, created_at, updated_at, expires_at, superseded_by,
                nucleus_eligible, source_kind, metadata_json)
               VALUES (?, ?, ?, ?, '[]', '[]', ?, ?, 'candidate', ?, ?, ?, ?, ?, NULL, '', 0, 'nucleus', ?)
               ON CONFLICT(object_id) DO UPDATE SET
                 title=excluded.title, body=excluded.body, status='candidate',
                 confidence=excluded.confidence, priority=excluded.priority,
                 relevance_tags_json=excluded.relevance_tags_json, updated_at=excluded.updated_at,
                 nucleus_eligible=0, source_kind='nucleus', metadata_json=excluded.metadata_json""",
            (
                object_id, object_type, scope_key, str(payload.get("session_id") or ""),
                title, body, min(float(confidence or 0.35), 0.65),
                float(payload.get("priority") or 0.4),
                json.dumps(words, ensure_ascii=False), now, now,
                json.dumps({"payload": payload, "trust": "low_until_verified"}, ensure_ascii=False, sort_keys=True),
            ),
        )
        return object_id

    # --- WRITE: Nucleus → Live Brain ---
    def write_fact(self, fact_text, scope_key="nucleus", question="", confidence=0.8, source_urls=None):
        conn = self._conn()
        if not conn:
            return None
        fact_id = self._stable_id("nucleus_fact", scope_key, question, fact_text)
        now = time.time()
        try:
            columns = self._table_columns(conn, "epistemic_learned_facts")
            values = {
                "fact_id": fact_id,
                "scope_key": scope_key,
                "question": question,
                "fact_text": fact_text,
                "confidence": confidence,
                "source_kind": "nucleus",
                "status": "active",
                "valid_from": now,
                "created_at": now,
                "updated_at": now,
            }
            if "source_urls_json" in columns:
                values["source_urls_json"] = json.dumps(source_urls or [])
            insert_columns = [name for name in values if name in columns]
            placeholders = ",".join("?" for _ in insert_columns)
            updates = ["fact_text=excluded.fact_text", "confidence=excluded.confidence", "updated_at=excluded.updated_at"]
            if "source_urls_json" in insert_columns:
                updates.append("source_urls_json=excluded.source_urls_json")
            with conn:
                conn.execute(
                    f"""INSERT INTO epistemic_learned_facts
                       ({','.join(insert_columns)}) VALUES ({placeholders})
                       ON CONFLICT(fact_id) DO UPDATE SET {','.join(updates)}""",
                    [values[name] for name in insert_columns],
                )
                self._record_candidate_object(
                    conn,
                    "open_hypothesis",
                    {
                        "title": question or "Nucleus hypothesis",
                        "body": fact_text,
                        "fact_text": fact_text,
                        "question": question,
                        "source_urls": source_urls or [],
                    },
                    scope_key=scope_key,
                    confidence=min(float(confidence or 0.35), 0.55),
                )
            return fact_id
        except Exception as e:
            log.warning(f"write_fact failed: {e}")
            return None
        finally:
            conn.close()

    def write_fix_recipe(self, problem, steps, scope_key="nucleus", sources=None, success_criteria="", confidence=0.7):
        conn = self._conn()
        if not conn:
            return None
        now = time.time()
        recipe_id = self._stable_id("nucleus_recipe", scope_key, problem, json.dumps(steps, sort_keys=True))
        try:
            if not self._has_table(conn, "fix_recipes"):
                return None
            columns = self._table_columns(conn, "fix_recipes")
            values = {
                "recipe_id": recipe_id,
                "scope_key": scope_key,
                "problem_pattern": problem,
                "tool_name": "",
                "steps_json": json.dumps(steps or []),
                "args_template_json": "{}",
                "success_criteria": success_criteria,
                "confidence": confidence,
                "times_confirmed": 1,
                "status": "active",
                "source": "nucleus_research",
                "scope_tags_json": json.dumps({"sources": sources or []}),
                "created_at": now,
                "updated_at": now,
                "promotion_status": "candidate",
                "candidate_since": now,
                "last_reviewed_at": now,
            }
            insert_columns = [name for name in values if name in columns]
            placeholders = ",".join("?" for _ in insert_columns)
            updates = [
                "steps_json=excluded.steps_json",
                "success_criteria=excluded.success_criteria",
                "confidence=excluded.confidence",
                "updated_at=excluded.updated_at",
            ]
            with conn:
                conn.execute(
                    f"""INSERT INTO fix_recipes ({','.join(insert_columns)})
                       VALUES ({placeholders})
                       ON CONFLICT(recipe_id) DO UPDATE SET {','.join(updates)}""",
                    [values[name] for name in insert_columns],
                )
                self._record_candidate_object(
                    conn,
                    "fix_recipe",
                    {
                        "problem": problem,
                        "steps": steps or [],
                        "sources": sources or [],
                        "success_criteria": success_criteria,
                    },
                    scope_key=scope_key,
                    confidence=min(float(confidence or 0.35), 0.6),
                )
            return recipe_id
        except Exception as e:
            log.warning(f"write_fix_recipe failed: {e}")
            return None
        finally:
            conn.close()

    def write_research_trace(self, research_result):
        conn = self._conn()
        if not conn:
            return None
        problem = research_result.get("problem", "")
        scope = research_result.get("scope", "nucleus")
        research_id = self._stable_id("nucleus_research", scope, problem)
        now = time.time()
        try:
            with conn:
                self._record_memory_event(
                    conn,
                    "nucleus_research_trace",
                    research_result,
                    scope_key=scope,
                    source="nucleus.researcher",
                )
                if self._has_table(conn, "research_jobs"):
                    conn.execute(
                        """INSERT INTO research_jobs
                           (research_id, topic, question, scope, status, priority, created_at, completed_at)
                           VALUES (?,?,?,?,?,?,?,?)
                           ON CONFLICT(research_id) DO UPDATE SET
                             status=excluded.status, completed_at=excluded.completed_at""",
                        (research_id, problem[:160], problem, scope, "completed", 0.5, now, now),
                    )
                if self._has_table(conn, "research_results"):
                    for source in research_result.get("local_sources", []) + research_result.get("web_sources", []):
                        ref = source.get("path") or source.get("url") or source.get("title", "")
                        result_id = self._stable_id("nucleus_result", research_id, ref)
                        conn.execute(
                            """INSERT INTO research_results
                               (result_id, research_id, source_kind, source_ref, summary,
                                confidence, actionability, raw_excerpt)
                               VALUES (?,?,?,?,?,?,?,?)
                               ON CONFLICT(result_id) DO UPDATE SET
                                 summary=excluded.summary, confidence=excluded.confidence""",
                            (
                                result_id, research_id, source.get("kind", "unknown"), ref,
                                source.get("snippet", "")[:700], research_result.get("confidence", 0.5),
                                0.6, source.get("snippet", "")[:1200],
                            ),
                        )
                if self._has_table(conn, "epistemic_web_sources"):
                    for source in research_result.get("web_sources", []):
                        source_id = self._stable_id("nucleus_src", research_id, source.get("url", ""))
                        conn.execute(
                            """INSERT INTO epistemic_web_sources
                               (source_id, job_id, scope_key, url, title, source_kind,
                                authority, summary, raw_excerpt, content_hash, confidence,
                                extracted_at, created_at)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                               ON CONFLICT(source_id) DO UPDATE SET
                                 summary=excluded.summary, confidence=excluded.confidence""",
                            (
                                source_id, research_id, scope, source.get("url", ""),
                                source.get("title", ""), "web", "unknown",
                                source.get("snippet", "")[:700], source.get("snippet", "")[:1200],
                                self._stable_id("hash", source.get("snippet", "")),
                                research_result.get("confidence", 0.5), now, now,
                            ),
                        )
            return research_id
        except Exception as e:
            log.warning(f"write_research_trace failed: {e}")
            return None
        finally:
            conn.close()

    def write_artifact(self, path, label, project_key="nucleus", confidence=0.8):
        conn = self._conn()
        if not conn:
            return None
        artifact_path = str(Path(path).expanduser())
        artifact_id = self._stable_id("nucleus_artifact", project_key, label or "", artifact_path)
        try:
            with conn:
                self._record_candidate_object(
                    conn,
                    "open_hypothesis",
                    {
                        "object_id": artifact_id,
                        "title": label or "Nucleus artifact candidate",
                        "body": artifact_path,
                        "path": artifact_path,
                    },
                    scope_key=project_key,
                    confidence=min(float(confidence or 0.35), 0.55),
                )
            return artifact_id
        except Exception as e:
            log.warning(f"write_artifact failed: {e}")
            return None
        finally:
            conn.close()

    # --- READ: Live Brain → Nucleus ---
    def get_facts(self, scope_key="nucleus", query="", limit=10):
        conn = self._conn(read_only=True)
        if not conn:
            return []
        try:
            conn.row_factory = sqlite3.Row
            pattern = f"%{query[:80]}%" if query else "%"
            rows = conn.execute(
                """SELECT fact_id, scope_key, question, fact_text, confidence, updated_at
                   FROM epistemic_learned_facts
                   WHERE scope_key=? AND status='active'
                     AND (fact_text LIKE ? OR question LIKE ?)
                   ORDER BY confidence DESC, updated_at DESC LIMIT ?""",
                (scope_key, pattern, pattern, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []
        finally:
            conn.close()

    def get_artifacts(self, project_key="nucleus", limit=20):
        conn = self._conn(read_only=True)
        if not conn:
            return []
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT artifact_id, project_key, role, path, label, status, confidence, updated_at
                   FROM verified_artifacts
                   WHERE project_key=? AND status='verified'
                   ORDER BY confidence DESC, updated_at DESC LIMIT ?""",
                (project_key, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []
        finally:
            conn.close()

    def get_fix_recipes(self, scope_key="nucleus", query="", limit=10):
        conn = self._conn(read_only=True)
        if not conn:
            return []
        try:
            conn.row_factory = sqlite3.Row
            pattern = f"%{query[:80]}%" if query else "%"
            rows = conn.execute(
                """SELECT recipe_id, scope_key, problem_pattern, steps_json,
                          success_criteria, confidence, status, updated_at
                   FROM fix_recipes
                   WHERE scope_key=? AND status='active' AND problem_pattern LIKE ?
                   ORDER BY confidence DESC, updated_at DESC LIMIT ?""",
                (scope_key, pattern, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []
        finally:
            conn.close()

    def get_nucleus_feed(self, scope_key="nucleus", limit=50, min_confidence=0.75):
        conn = self._conn(read_only=True)
        if not conn:
            return []
        try:
            conn.row_factory = sqlite3.Row
            self._ensure_memory_v2_schema(conn)
            rows = conn.execute(
                """SELECT object_id, object_type, scope_key, title, body, confidence,
                          priority, updated_at, metadata_json
                   FROM memory_objects
                   WHERE scope_key=?
                     AND object_type IN ('validated_cause','verified_artifact','fix_recipe','instruction_proposal','open_loop','open_hypothesis')
                     AND status='active'
                     AND confidence >= ?
                     AND superseded_by=''
                     AND (object_type!='open_loop' OR nucleus_eligible=1)
                   ORDER BY updated_at DESC LIMIT ?""",
                (scope_key, float(min_confidence), limit),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []
        finally:
            conn.close()

    def get_recent_work_items(self, since_minutes=60, limit=10):
        return []

    # --- SYNC: Pull eligible compiled memory into Pargod ---
    def sync_to_pargod(self, pargod):
        items = self.get_nucleus_feed(limit=50, min_confidence=0.75)
        added = 0
        for item in items:
            label = f"mem_{item['object_id'][:16]}"
            if not pargod.get_node(label):
                node_type = "problem" if item.get("object_type") in {"validated_cause", "open_loop"} else "knowledge"
                pargod.add_node(node_type, label, f"{item.get('title', '')}\n{item.get('body', '')}".strip())
                added += 1
        if added:
            log.info(f"Synced {added} compiled memory objects from Live Brain → Pargod")
        return added
