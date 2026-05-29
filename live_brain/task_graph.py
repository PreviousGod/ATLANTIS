"""Task Graph Engine — Autonomous planning and execution tracking.

Transforms ATLANTIS from reactive to proactive. The graph IS the plan —
the LLM just walks it.

Tables (in live_brain.db):
  task_graphs    — top-level task definitions + templates
  task_nodes     — individual steps with status, dependencies, results
  task_edges     — relationships between nodes (depends_on, ruled_out)
  task_executions — history of every step execution

Key concepts:
  - Templates: pre-defined workflows seeded once, executed infinitely
  - Auto-learning: successful tool sequences become graph edges
  - Constraint propagation: failed → dependent nodes blocked
  - Persistence: survives gateway restarts
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("live_brain.task_graph")


@dataclass
class TaskNode:
    node_id: str
    graph_id: str
    description: str
    status: str = "pending"  # pending | in_progress | done | failed | blocked
    dependencies: List[str] = field(default_factory=list)
    fix_recipe: str = ""
    result_json: str = ""
    tool_hint: str = ""  # suggested tool
    tool_args: str = ""  # suggested args as JSON
    order_index: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "description": self.description,
            "status": self.status,
            "dependencies": self.dependencies,
            "fix_recipe": self.fix_recipe,
            "result": self.result_json[:200],
            "tool_hint": self.tool_hint,
        }


# ── Seed templates for common task types ──────────────────────────────

_SEED_TEMPLATES: Dict[str, List[dict]] = {
    "general_task": [
        {"desc": "Understand what the user needs — ask clarifying questions if unclear", "tool": None, "args": None},
        {"desc": "Check what you already know: use brain_recall if needed", "tool": None, "args": None},
        {"desc": "Identify the first concrete step and execute it", "tool": None, "args": None},
        {"desc": "Verify the result — did it work? Prove it", "tool": None, "args": None},
        {"desc": "Report to user: what was done, what's next", "tool": None, "args": None},
    ],
    "code_implementation": [
        {"desc": "Read the existing codebase to understand the structure", "tool": "read_file", "args": None},
        {"desc": "Plan the change: what files need modification?", "tool": None, "args": None},
        {"desc": "Implement the core logic", "tool": "write_file", "args": None},
        {"desc": "Test the implementation", "tool": "terminal", "args": None},
        {"desc": "Fix any issues found during testing", "tool": "patch", "args": None},
        {"desc": "Verify all tests pass", "tool": "terminal", "args": None},
    ],
    "deploy_service": [
        {"desc": "Check current deployment state — what's running?", "tool": "terminal", "args": None},
        {"desc": "Build/prepare the deployment artifact", "tool": "terminal", "args": None},
        {"desc": "Stop old service if running", "tool": "terminal", "args": None},
        {"desc": "Deploy new version", "tool": "terminal", "args": None},
        {"desc": "Verify service is healthy — check logs and endpoints", "tool": "terminal", "args": None},
        {"desc": "Report deployment status to user", "tool": None, "args": None},
    ],
    "data_analysis": [
        {"desc": "Load and inspect the data source", "tool": "terminal", "args": None},
        {"desc": "Clean and preprocess the data", "tool": "terminal", "args": None},
        {"desc": "Perform the analysis or computation", "tool": "terminal", "args": None},
        {"desc": "Visualize or summarize the results", "tool": "terminal", "args": None},
        {"desc": "Present findings to user", "tool": None, "args": None},
    ],
    "apk_patch_feature_id": [
        {"desc": "Extract target DEX files from APK",
         "tool": "terminal", "args": '{"command": "unzip -o <apk> classes*.dex -d <workdir>"}'},
        {"desc": "Find const/16 patterns near AiFeature references",
         "tool": "terminal", "args": '{"command": "python3 -c \\"find const/16 #108 near AiFeature strings\\""}'},
        {"desc": "Patch bytecode: const/16 #108 → #105 at confirmed offsets",
         "tool": "terminal", "args": '{"command": "python3 patch_dex.py"}'},
        {"desc": "Rebuild APK preserving .so files uncompressed",
         "tool": "terminal", "args": '{"command": "python3 rebuild_apk.py"}'},
        {"desc": "Sign APK with uber-apk-signer",
         "tool": "terminal", "args": '{"command": "java -jar uber-apk-signer.jar --apks rebuilt.apk"}'},
        {"desc": "Install on device via ADB",
         "tool": "terminal", "args": '{"command": "adb install -r signed.apk"}'},
        {"desc": "Verify: check logcat for AiFeature errors",
         "tool": "terminal", "args": '{"command": "adb logcat -d | grep -i aifeature"}'},
    ],
    "debug_tool_failure": [
        {"desc": "Read the error message carefully — quote it exactly",
         "tool": None, "args": None},
        {"desc": "Diagnose root cause: missing tool? wrong args? env issue?",
         "tool": None, "args": None},
        {"desc": "If tool not installed: find alternative or install it",
         "tool": "terminal", "args": None},
        {"desc": "If wrong approach: try fundamentally different method",
         "tool": None, "args": None},
        {"desc": "Report what you learned to the user",
         "tool": None, "args": None},
    ],
    "research_topic": [
        {"desc": "Search web for authoritative sources",
         "tool": "web_search", "args": None},
        {"desc": "Extract content from top 3 sources",
         "tool": "web_extract", "args": None},
        {"desc": "Cross-reference and verify facts",
         "tool": None, "args": None},
        {"desc": "Synthesize findings into answer",
         "tool": None, "args": None},
    ],
}


# ── Schema ────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_graphs (
    graph_id TEXT PRIMARY KEY,
    scope_key TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    template_key TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    node_count INTEGER NOT NULL DEFAULT 0,
    done_count INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS task_nodes (
    node_id TEXT PRIMARY KEY,
    graph_id TEXT NOT NULL REFERENCES task_graphs(graph_id),
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    dependencies TEXT NOT NULL DEFAULT '[]',
    fix_recipe TEXT NOT NULL DEFAULT '',
    result_json TEXT NOT NULL DEFAULT '',
    tool_hint TEXT NOT NULL DEFAULT '',
    tool_args TEXT NOT NULL DEFAULT '',
    order_index INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS task_executions (
    exec_id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL REFERENCES task_nodes(node_id),
    session_id TEXT NOT NULL DEFAULT '',
    tool_name TEXT NOT NULL DEFAULT '',
    tool_args TEXT NOT NULL DEFAULT '',
    tool_result TEXT NOT NULL DEFAULT '',
    success INTEGER NOT NULL DEFAULT 0,
    duration_ms REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_task_nodes_graph ON task_nodes(graph_id, status);
CREATE INDEX IF NOT EXISTS idx_task_executions_node ON task_executions(node_id);
CREATE INDEX IF NOT EXISTS idx_task_graphs_scope ON task_graphs(scope_key, status);
"""


class TaskGraph:
    """Autonomous task graph engine."""

    def __init__(self, db_conn):
        self.conn = db_conn
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # ── Planning ──────────────────────────────────────────────────────

    def _detect_template(self, task_description: str) -> str:
        """Auto-detect the best template from the task description."""
        desc = task_description.lower()
        if any(w in desc for w in ("apk", "patch", "dex", "smali", "aifeature", "bytecode", "cosmo")):
            return "apk_patch_feature_id"
        if any(w in desc for w in ("deploy", "service", "restart", "docker", "server")):
            return "deploy_service"
        if any(w in desc for w in ("code", "implement", "refactor", "bug", "fix bug", "feature", "write")):
            return "code_implementation"
        if any(w in desc for w in ("data", "analyze", "analysis", "csv", "json", "chart", "statistics")):
            return "data_analysis"
        if any(w in desc for w in ("debug", "error", "failing", "broken", "fix", "traceback")):
            return "debug_tool_failure"
        if any(w in desc for w in ("research", "find", "search", "learn about", "what is")):
            return "research_topic"
        return "general_task"

    def plan(self, task_description: str, scope_key: str = "",
             graph_id: str = "", template_key: str = "") -> str:
        """Create a task graph and return the graph_id.

        If template_key is provided, use that template. Otherwise auto-detect
        the best template from the task description. Falls back to empty graph.
        """
        if not graph_id:
            graph_id = f"task:{scope_key}:{int(time.time())}"

        if not template_key:
            template_key = self._detect_template(task_description)

        now = time.time()
        self.conn.execute(
            "INSERT OR REPLACE INTO task_graphs (graph_id, scope_key, name, description, template_key, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (graph_id, scope_key, task_description[:80], task_description, template_key, now, now),
        )

        # Pre-populate from seed template
        template = _SEED_TEMPLATES.get(template_key, [])
        for i, step in enumerate(template):
            node_id = f"{graph_id}:step{i+1}"
            deps = []
            if i > 0:
                deps = [f"{graph_id}:step{i}"]
            tool_hint = step.get("tool") or ""
            tool_args = step.get("args") or ""
            self.conn.execute(
                "INSERT INTO task_nodes (node_id, graph_id, description, status, dependencies, fix_recipe, tool_hint, tool_args, order_index, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (node_id, graph_id, step["desc"], "pending", json.dumps(deps), "", tool_hint, tool_args, i+1, now, now),
            )
        if template:
            self.conn.execute(
                "UPDATE task_graphs SET node_count=? WHERE graph_id=?",
                (len(template), graph_id),
            )

        self.conn.commit()
        logger.info("TASK GRAPH created: %s template=%s nodes=%d", graph_id, template_key, len(template))
        return graph_id

    # ── Navigation ────────────────────────────────────────────────────

    def next_node(self, graph_id: str) -> Optional[dict]:
        """Return the next node the agent should work on (first pending with all deps done)."""
        # Activate first pending node (set to in_progress if it was pending)
        row = self.conn.execute(
            "SELECT node_id FROM task_nodes WHERE graph_id=? AND status='in_progress' LIMIT 1",
            (graph_id,),
        ).fetchone()
        if row:
            return self.get_node(row[0])

        # Find first pending node whose dependencies are all done
        pending = self.conn.execute(
            "SELECT node_id, dependencies FROM task_nodes WHERE graph_id=? AND status='pending' ORDER BY order_index, created_at",
            (graph_id,),
        ).fetchall()
        for node_id, deps_json in pending:
            deps = json.loads(deps_json) if deps_json else []
            if not deps:
                self._set_status(node_id, "in_progress")
                return self.get_node(node_id)
            # Check all deps done
            dep_rows = self.conn.execute(
                "SELECT status FROM task_nodes WHERE node_id IN ({})".format(
                    ",".join("?" * len(deps))), deps,
            ).fetchall()
            if all(r[0] == "done" for r in dep_rows):
                self._set_status(node_id, "in_progress")
                return self.get_node(node_id)

        return None  # all done or all blocked

    def get_node(self, node_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT node_id, graph_id, description, status, dependencies, fix_recipe, result_json, tool_hint, tool_args, order_index FROM task_nodes WHERE node_id=?",
            (node_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "node_id": row[0], "graph_id": row[1], "description": row[2],
            "status": row[3], "dependencies": json.loads(row[4]) if row[4] else [],
            "fix_recipe": row[5], "result": row[6][:200],
            "tool_hint": row[7], "tool_args": row[8], "order": row[9],
        }

    def get_graph(self, graph_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT graph_id, name, description, status, node_count, done_count, template_key FROM task_graphs WHERE graph_id=?",
            (graph_id,),
        ).fetchone()
        if not row:
            return None
        nodes = self.conn.execute(
            "SELECT node_id, description, status, result_json FROM task_nodes WHERE graph_id=? ORDER BY order_index",
            (graph_id,),
        ).fetchall()
        return {
            "graph_id": row[0], "name": row[1], "description": row[2],
            "status": row[3], "node_count": row[4], "done_count": row[5],
            "template": row[6],
            "nodes": [{"id": n[0], "desc": n[1], "status": n[2], "result": (n[3] or "")[:100]} for n in nodes],
        }

    def active_graphs(self, scope_key: str = "") -> List[dict]:
        rows = self.conn.execute(
            "SELECT graph_id, name, status, done_count, node_count FROM task_graphs WHERE (scope_key=? OR ?='') AND status='active' ORDER BY updated_at DESC LIMIT 5",
            (scope_key, scope_key),
        ).fetchall()
        return [{"graph_id": r[0], "name": r[1], "status": r[2],
                 "progress": f"{r[3]}/{r[4]}"} for r in rows]

    # ── Actions ────────────────────────────────────────────────────────

    def complete_node(self, node_id: str, result: str = "",
                      session_id: str = "", tool_name: str = "",
                      tool_args: str = "", duration_ms: float = 0) -> bool:
        self._set_status(node_id, "done")
        now = time.time()
        self.conn.execute(
            "UPDATE task_nodes SET result_json=?, updated_at=? WHERE node_id=?",
            (result[:5000], now, node_id),
        )
        # Record execution
        exec_id = f"exec:{node_id}:{int(now)}"
        self.conn.execute(
            "INSERT INTO task_executions (exec_id, node_id, session_id, tool_name, tool_args, tool_result, success, duration_ms, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (exec_id, node_id, session_id, tool_name, tool_args, result[:2000], 1, duration_ms, now),
        )
        # Update graph progress
        graph_id = node_id.rsplit(":", 1)[0]
        self.conn.execute(
            "UPDATE task_graphs SET done_count=(SELECT COUNT(*) FROM task_nodes WHERE graph_id=? AND status='done'), updated_at=? WHERE graph_id=?",
            (graph_id, now, graph_id),
        )
        # Check if all done
        total = self.conn.execute("SELECT COUNT(*) FROM task_nodes WHERE graph_id=?", (graph_id,)).fetchone()[0]
        done = self.conn.execute("SELECT COUNT(*) FROM task_nodes WHERE graph_id=? AND status='done'", (graph_id,)).fetchone()[0]
        if total > 0 and done >= total:
            self.conn.execute("UPDATE task_graphs SET status='completed', updated_at=? WHERE graph_id=?", (now, graph_id))
            logger.info("TASK GRAPH completed: %s (%d/%d nodes)", graph_id, done, total)

        # Auto-learn: successful node completion creates fix recipe for this step
        if result and tool_name:
            # Attach fix recipe to any sibling nodes with same tool_hint
            self.conn.execute(
                "UPDATE task_nodes SET fix_recipe=? WHERE graph_id=? AND tool_hint=? AND status='pending'",
                (result[:500], graph_id, tool_name),
            )

        self.conn.commit()
        return True

    def fail_node(self, node_id: str, reason: str = "",
                  session_id: str = "", tool_name: str = "") -> bool:
        """Mark a node as failed. Propagates: dependent nodes become blocked."""
        self._set_status(node_id, "failed")
        now = time.time()
        self.conn.execute(
            "UPDATE task_nodes SET result_json=?, updated_at=? WHERE node_id=?",
            (reason[:2000], now, node_id),
        )
        # Block dependent nodes
        graph_id = node_id.rsplit(":", 1)[0]
        deps_json = json.dumps([node_id])
        self.conn.execute(
            "UPDATE task_nodes SET status='blocked' WHERE graph_id=? AND dependencies LIKE ? AND status='pending'",
            (graph_id, f"%{node_id}%"),
        )
        # Record failure
        exec_id = f"exec:{node_id}:{int(now)}"
        self.conn.execute(
            "INSERT INTO task_executions (exec_id, node_id, session_id, tool_name, tool_result, success, created_at) VALUES (?,?,?,?,?,?,?)",
            (exec_id, node_id, session_id, tool_name, reason[:2000], 0, now),
        )
        self.conn.commit()
        logger.info("TASK NODE failed: %s — %s", node_id, reason[:100])
        return True

    def skip_node(self, node_id: str, reason: str = "") -> bool:
        """Skip a node (e.g. dependency already handled)."""
        self._set_status(node_id, "done")
        self.conn.execute(
            "UPDATE task_nodes SET result_json=? WHERE node_id=?",
            (f"Skipped: {reason}"[:500], node_id),
        )
        self.conn.commit()
        return True

    def _set_status(self, node_id: str, status: str):
        self.conn.execute(
            "UPDATE task_nodes SET status=?, updated_at=? WHERE node_id=?",
            (status, time.time(), node_id),
        )

    # ── Context injection ──────────────────────────────────────────────

    def current_task_context(self, scope_key: str = "") -> str:
        """Build CURRENT TASK context section for pre-LLM injection."""
        graphs = self.active_graphs(scope_key)
        if not graphs:
            return ""

        lines = ["CURRENT TASK:"]
        for g in graphs[:3]:
            graph = self.get_graph(g["graph_id"])
            if not graph:
                continue
            lines.append(f"- {graph['name']} ({graph['done_count']}/{graph['node_count']} done)")
            # Show what failed so the agent doesn't retry
            failed = [n for n in graph["nodes"] if n["status"] == "failed"]
            if failed:
                lines.append("  ALREADY TRIED AND FAILED:")
                for f in failed:
                    lines.append(f"    ✗ {f['desc']} — {f['result'][:120]}")
                lines.append("  DO NOT retry these approaches. Find a DIFFERENT way.")
            # Show what's blocked
            blocked = [n for n in graph["nodes"] if n["status"] == "blocked"]
            if blocked:
                lines.append(f"  {len(blocked)} steps blocked by failures above.")
            # Show next step
            node = self.next_node(g["graph_id"])
            if node:
                lines.append(f"  Next step: {node['description']}")
                if node.get("tool_hint"):
                    lines.append(f"  Use: {node['tool_hint']}")
        if lines:
            lines.append("Take ONE step, then respond to the user. Do NOT chain multiple steps.")
        return "\n".join(lines)

    # ── Seed / Template management ────────────────────────────────────

    def seed_template(self, template_key: str, description: str,
                      scope_key: str = "") -> str:
        """Create a graph from a seed template."""
        graph_id = f"task:{scope_key}:{int(time.time())}"
        return self.plan(description, scope_key, graph_id, template_key)

    def list_templates(self) -> List[str]:
        return sorted(_SEED_TEMPLATES.keys())

    # ── Auto-learning from past executions ─────────────────────────────

    def learn_from_session(self, session_id: str, scope_key: str = "") -> str | None:
        """Analyze a completed session and create a template from its tool sequence."""
        rows = self.conn.execute(
            "SELECT tool_name, tool_args, tool_result, success, created_at FROM task_executions WHERE session_id=? ORDER BY created_at",
            (session_id,),
        ).fetchall()
        if len(rows) < 2:
            return None
        # Build a mini-graph from the tool sequence
        graph_id = f"learned:{scope_key}:{int(time.time())}"
        now = time.time()
        desc = f"Learned from session {session_id}: {len(rows)} steps"
        self.conn.execute(
            "INSERT INTO task_graphs (graph_id, scope_key, name, description, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (graph_id, scope_key, desc[:80], desc, now, now),
        )
        for i, (tool, args, result, success, ts) in enumerate(rows):
            node_id = f"{graph_id}:step{i+1}"
            deps = [f"{graph_id}:step{i}"] if i > 0 else []
            self.conn.execute(
                "INSERT INTO task_nodes (node_id, graph_id, description, status, dependencies, tool_hint, tool_args, order_index, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (node_id, graph_id, f"{tool}: success={success}"[:120], "pending" if i > 0 else "done", json.dumps(deps), "", tool, args, i+1, now, now),
            )
        self.conn.commit()
        logger.info("TASK GRAPH learned from session %s: %d nodes", session_id, len(rows))
        return graph_id
