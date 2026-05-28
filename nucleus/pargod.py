"""Pargod: Topološko Pamćenje — SQLite graf sa Dijkstra pathfinding-om."""
import heapq
import hashlib
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from .config import PARGOD_DB, DATA_DIR, EDGE_DECAY_RATE, EDGE_MIN_WEIGHT

_SCHEMA = Path(__file__).parent / "schema.sql"


class Pargod:
    def __init__(self, db_path=None):
        self.db_path = str(db_path or PARGOD_DB)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self):
        c = sqlite3.connect(self.db_path, timeout=5.0)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=3000")
        c.execute("PRAGMA foreign_keys=ON")
        return c

    def _init_db(self):
        with closing(self._conn()) as conn, conn:
            conn.executescript(_SCHEMA.read_text())

    def _stable_label(self, prefix, *parts):
        text = "\n".join(str(part) for part in parts)
        digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]
        return f"{prefix}_{digest}"

    # --- Nodes ---
    def add_node(self, node_type, label, content=None):
        with closing(self._conn()) as conn, conn:
            conn.execute(
                "INSERT OR IGNORE INTO nodes (type, label, content) VALUES (?,?,?)",
                (node_type, label, content),
            )

    def upsert_node(self, node_type, label, content=None):
        with closing(self._conn()) as conn, conn:
            conn.execute(
                """INSERT INTO nodes (type, label, content) VALUES (?,?,?)
                   ON CONFLICT(label) DO UPDATE SET
                     type=excluded.type,
                     content=COALESCE(excluded.content, nodes.content)""",
                (node_type, label, content),
            )

    def get_node(self, label):
        with closing(self._conn()) as conn, conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM nodes WHERE label=?", (label,)).fetchone()
            return dict(row) if row else None

    def list_nodes(self, node_type=None):
        with closing(self._conn()) as conn, conn:
            conn.row_factory = sqlite3.Row
            if node_type:
                rows = conn.execute("SELECT * FROM nodes WHERE type=?", (node_type,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM nodes").fetchall()
            return [dict(r) for r in rows]

    # --- Edges ---
    def add_edge(self, source_label, target_label, relation, weight=1.0):
        with closing(self._conn()) as conn, conn:
            src = conn.execute("SELECT id FROM nodes WHERE label=?", (source_label,)).fetchone()
            tgt = conn.execute("SELECT id FROM nodes WHERE label=?", (target_label,)).fetchone()
            if not src or not tgt:
                return
            conn.execute(
                "INSERT OR IGNORE INTO edges (source_id, target_id, relation, weight) VALUES (?,?,?,?)",
                (src[0], tgt[0], relation, weight),
            )

    # --- Dijkstra: find nearest target node from a problem ---
    def _find_nearest_for_problem(self, problem_label, target_types):
        target_types = set(target_types)
        with closing(self._conn()) as conn, conn:
            src = conn.execute("SELECT id FROM nodes WHERE label=?", (problem_label,)).fetchone()
            if not src:
                return None
            src_id = src[0]
            heap = [(0.0, src_id, [problem_label])]
            visited = set()
            while heap:
                cost, nid, path = heapq.heappop(heap)
                if nid in visited:
                    continue
                visited.add(nid)
                row = conn.execute("SELECT type, label, content FROM nodes WHERE id=?", (nid,)).fetchone()
                if row and row[0] in target_types and nid != src_id:
                    result = {
                        "cost": cost,
                        "path": path,
                        "type": row[0],
                        "label": row[1],
                        "content": row[2],
                    }
                    if row[0] == "tool":
                        result["tool"] = row[1]
                    if row[0] == "fix_recipe":
                        result["recipe"] = row[1]
                    return result
                for edge in conn.execute(
                    "SELECT target_id, weight, id FROM edges WHERE source_id=?", (nid,)
                ).fetchall():
                    tid, w, _ = edge
                    if tid not in visited:
                        tlabel = conn.execute("SELECT label FROM nodes WHERE id=?", (tid,)).fetchone()
                        heapq.heappush(heap, (cost + w, tid, path + [tlabel[0] if tlabel else "?"]))
            return None

    # --- Dijkstra: find nearest tool node from a problem ---
    def find_tool_for_problem(self, problem_label):
        result = self._find_nearest_for_problem(problem_label, {"tool"})
        if result and "tool" not in result:
            result["tool"] = result["label"]
        return result

    def find_resolution_for_problem(self, problem_label):
        """Find nearest executable tool or research-backed fix recipe."""
        return self._find_nearest_for_problem(problem_label, {"tool", "fix_recipe"})

    def add_research_result(self, research_result):
        """Persist problem → knowledge → fix_recipe paths from structured research."""
        problem = (research_result or {}).get("problem", "")
        scope = (research_result or {}).get("scope", "nucleus")
        if not problem:
            return []
        problem_label = self._stable_label("problem", scope, problem)
        created = []
        if not self.get_node(problem_label):
            created.append(problem_label)
        self.upsert_node("problem", problem_label, problem)

        knowledge_labels = []
        sources = research_result.get("local_sources", []) + research_result.get("web_sources", [])
        for source in sources:
            ref = source.get("path") or source.get("url") or source.get("title", "")
            snippet = source.get("snippet", "")
            label = self._stable_label("kb", ref, snippet)
            if not self.get_node(label):
                created.append(label)
            self.upsert_node("knowledge", label, snippet)
            self.add_edge(problem_label, label, "INFORMED_BY", 0.5)
            knowledge_labels.append(label)

        recipe = research_result.get("fix_recipe")
        if recipe:
            recipe_content = json.dumps(recipe, ensure_ascii=False, sort_keys=True)
            recipe_label = self._stable_label("recipe", problem, recipe_content)
            if not self.get_node(recipe_label):
                created.append(recipe_label)
            self.upsert_node("fix_recipe", recipe_label, recipe_content)
            if knowledge_labels:
                for knowledge_label in knowledge_labels:
                    self.add_edge(knowledge_label, recipe_label, "SUPPORTS", 0.7)
            else:
                self.add_edge(problem_label, recipe_label, "SUPPORTS", 0.7)
            tool_name = recipe.get("tool_name") or recipe.get("tool")
            if tool_name and self.get_node(tool_name):
                self.add_edge(recipe_label, tool_name, "CAN_EXECUTE", 0.8)
        return created

    def _matching_problem_labels(self, query):
        q = query.lower().strip()
        with closing(self._conn()) as conn, conn:
            labels = []
            row = conn.execute(
                "SELECT label FROM nodes WHERE type='problem' AND label=?", (q,)
            ).fetchone()
            if row:
                labels.append(row[0])
            rows = conn.execute(
                "SELECT label, content FROM nodes WHERE type='problem'"
            ).fetchall()
            for label, content in rows:
                if label in labels:
                    continue
                if label in q or (content and any(w in q for w in content.lower().split()[:3])):
                    labels.append(label)
            return labels

    # --- Record usage (strengthens edge) ---
    def record_use(self, tool_label):
        now = time.time()
        with closing(self._conn()) as conn, conn:
            conn.execute(
                "UPDATE nodes SET use_count=use_count+1, last_used=? WHERE label=?",
                (now, tool_label),
            )
            node = conn.execute("SELECT id FROM nodes WHERE label=?", (tool_label,)).fetchone()
            if node:
                conn.execute(
                    "UPDATE edges SET use_count=use_count+1, last_used=? WHERE target_id=?",
                    (now, node[0]),
                )

    # --- Edge decay (call periodically) ---
    def decay_edges(self):
        now = time.time()
        with closing(self._conn()) as conn, conn:
            edges = conn.execute("SELECT id, weight, last_used FROM edges WHERE last_used IS NOT NULL").fetchall()
            for eid, w, last in edges:
                hours = (now - last) / 3600.0
                new_w = max(EDGE_MIN_WEIGHT, w - EDGE_DECAY_RATE * hours)
                if new_w != w:
                    conn.execute("UPDATE edges SET weight=? WHERE id=?", (new_w, eid))

    # --- Episodes ---
    def log_episode(self, tick, entropy, state, action=None):
        with closing(self._conn()) as conn, conn:
            conn.execute(
                "INSERT INTO episodes (tick, entropy, sensor_state, action_taken) VALUES (?,?,?,?)",
                (tick, entropy, json.dumps(state), action),
            )

    # --- Seed from JSON ---
    def seed_from_json(self, json_path):
        data = json.loads(Path(json_path).read_text())
        for node in data.get("nodes", []):
            self.add_node(node["type"], node["label"], node.get("content"))
        for edge in data.get("edges", []):
            self.add_edge(edge["source"], edge["target"], edge["relation"], edge.get("weight", 1.0))

    # --- Query: does graph know about this problem? ---
    def has_answer_for(self, query):
        """Check if graph can resolve a natural-language query to a tool."""
        for label in self._matching_problem_labels(query):
            result = self.find_tool_for_problem(label)
            if result:
                return result
        return None

    def has_resolution_for(self, query):
        """Check if graph has either an executable tool or fix recipe for a query."""
        for label in self._matching_problem_labels(query):
            result = self.find_resolution_for_problem(label)
            if result:
                return result
        return None
