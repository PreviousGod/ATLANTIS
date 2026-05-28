"""SensoryCortex — Percepcija KORISNIČKOG konteksta (ne sistemskih metrika).

Osim CPU/RAM, gleda:
- Aktivne projekte (git status, nedavni fajlovi)
- Razgovore sa korisnikom (Live Brain sessions)
- Browser aktivnost (Brave CDP — ako dostupan)
- Nedovršene zadatke (TODO, pendinzi)
- CWM patterns (šta Nucleus ne zna)

Čisto Python. Nema LLM poziva."""

import json
import logging
import os
import sqlite3
import subprocess
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("nucleus.sensory")

# Dirs to watch for user activity (not system dirs)
WATCH_ROOTS = [
    Path.home() / ".hermes",
    Path.home() / "projects",
    Path.home() / "workspace",
    Path.home() / "repos",
]
CODE_EXTENSIONS = {".py", ".js", ".ts", ".md", ".json", ".yaml", ".yml", ".sh", ".sql", ".rs", ".go"}


@dataclass
class SensoryObservation:
    """Jedna opservacija iz korisničkog konteksta."""
    source: str              # 'git', 'filesystem', 'conversation', 'browser', 'todo', 'cwm_gap'
    category: str            # 'new_project', 'code_change', 'user_request', 'unanswered_question', 'stuck_task'
    description: str
    target_path: str = ""    # fajl, dir, ili URL
    urgency_hint: float = 0.5  # 0-1, inicijalna procena
    evidence: Dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


class SensoryCortex:
    """Gleda šta KORISNIK radi, ne šta SISTEM radi."""

    def __init__(self, brain_db_path: Optional[str] = None):
        self._known_files: Dict[str, float] = {}   # path -> mtime
        self._known_branches: set = set()
        self._last_git_check = 0.0
        self._last_conversation_check = 0.0
        self._brain_db = brain_db_path or str(Path.home() / ".hermes" / "live_brain" / "live_brain.db")
        self._observation_history: List[SensoryObservation] = []
        self._load_history()

    def _load_history(self):
        path = Path.home() / ".hermes" / "nucleus_data" / "sensory_history.jsonl"
        if path.exists():
            try:
                lines = path.read_text().strip().split("\n")
                self._observation_history = [
                    SensoryObservation(**json.loads(l)) for l in lines[-200:]
                ]
            except Exception:
                pass

    def _save_history(self):
        path = Path.home() / ".hermes" / "nucleus_data" / "sensory_history.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            for obs in self._observation_history[-200:]:
                d = {
                    "source": obs.source,
                    "category": obs.category,
                    "description": obs.description,
                    "target_path": obs.target_path,
                    "urgency_hint": obs.urgency_hint,
                    "evidence": obs.evidence,
                    "timestamp": obs.timestamp,
                }
                f.write(json.dumps(d) + "\n")

    def observe(self) -> List[SensoryObservation]:
        """Glavna metoda — prikuplja sve opservacije iz korisničkog konteksta."""
        observations = []

        observations.extend(self._scan_git_activity())
        observations.extend(self._scan_filesystem_activity())
        observations.extend(self._scan_conversation_gaps())
        observations.extend(self._scan_pending_todos())
        observations.extend(self._scan_cwm_knowledge_gaps())

        # Dedup and store
        new_obs = [o for o in observations if not self._is_known(o)]
        self._observation_history.extend(new_obs)
        self._observation_history = self._observation_history[-500:]
        self._save_history()

        return new_obs

    def _is_known(self, obs: SensoryObservation) -> bool:
        """Proveri da li smo već videli ovu opservaciju."""
        key = f"{obs.source}:{obs.category}:{obs.target_path}"
        for old in self._observation_history[-50:]:
            old_key = f"{old.source}:{old.category}:{old.target_path}"
            if old_key == key:
                # Ako je ista stvar, ali opis se promenio, to je update
                if abs(old.timestamp - obs.timestamp) < 300:
                    return True
        return False

    def _scan_git_activity(self) -> List[SensoryObservation]:
        """Detektuje git promene u projektima."""
        observations = []
        now = time.time()
        if now - self._last_git_check < 60:
            return observations
        self._last_git_check = now

        for root in WATCH_ROOTS:
            if not root.exists():
                continue
            for subdir in root.iterdir():
                if not subdir.is_dir():
                    continue
                git_dir = subdir / ".git"
                if not git_dir.exists():
                    continue
                try:
                    # Check for uncommitted changes
                    result = subprocess.run(
                        ["git", "-C", str(subdir), "status", "--short"],
                        capture_output=True, text=True, timeout=10,
                    )
                    if result.stdout.strip():
                        lines = result.stdout.strip().split("\n")
                        modified = len(lines)
                        branch = subprocess.run(
                            ["git", "-C", str(subdir), "branch", "--show-current"],
                            capture_output=True, text=True, timeout=5,
                        ).stdout.strip()

                        if branch not in self._known_branches:
                            self._known_branches.add(branch)
                            observations.append(SensoryObservation(
                                source="git",
                                category="new_branch",
                                description=f"New branch '{branch}' in {subdir.name}",
                                target_path=str(subdir),
                                urgency_hint=0.4,
                                evidence={"branch": branch, "modified_files": modified},
                            ))

                        if modified > 0:
                            observations.append(SensoryObservation(
                                source="git",
                                category="code_change",
                                description=f"{modified} uncommitted changes in {subdir.name}",
                                target_path=str(subdir),
                                urgency_hint=min(0.8, 0.3 + modified * 0.05),
                                evidence={"modified": modified, "branch": branch},
                            ))
                except Exception:
                    continue
        return observations

    def _scan_filesystem_activity(self) -> List[SensoryObservation]:
        """Detektuje nove ili nedavno promenjene fajlove."""
        observations = []
        now = time.time()

        for root in WATCH_ROOTS:
            if not root.exists():
                continue
            try:
                for entry in root.rglob("*"):
                    if not entry.is_file():
                        continue
                    if entry.suffix not in CODE_EXTENSIONS:
                        continue
                    # Skip venv, node_modules, etc.
                    if any(part.startswith(".") for part in entry.parts):
                        continue

                    mtime = entry.stat().st_mtime
                    known_mtime = self._known_files.get(str(entry), 0)

                    if str(entry) not in self._known_files:
                        # Brand new file
                        self._known_files[str(entry)] = mtime
                        observations.append(SensoryObservation(
                            source="filesystem",
                            category="new_file",
                            description=f"New file: {entry.name}",
                            target_path=str(entry),
                            urgency_hint=0.5,
                            evidence={"size": entry.stat().st_size},
                        ))
                    elif mtime > known_mtime + 1:
                        # Modified file
                        self._known_files[str(entry)] = mtime
                        age_hours = (now - mtime) / 3600
                        if age_hours < 24:
                            observations.append(SensoryObservation(
                                source="filesystem",
                                category="code_change",
                                description=f"Modified: {entry.name} ({age_hours:.1f}h ago)",
                                target_path=str(entry),
                                urgency_hint=min(0.7, 0.4 + (1 - age_hours/24) * 0.3),
                                evidence={"mtime": mtime, "age_hours": age_hours},
                            ))
            except PermissionError:
                continue
        return observations

    def _scan_conversation_gaps(self) -> List[SensoryObservation]:
        """Čita Live Brain da nađe nerazrešene teme."""
        observations = []
        now = time.time()
        if now - self._last_conversation_check < 120:
            return observations
        self._last_conversation_check = now

        try:
            with closing(sqlite3.connect(self._brain_db, timeout=30)) as conn:
                conn.execute("PRAGMA busy_timeout=30000")
                conn.execute("PRAGMA query_only=ON")
                conn.row_factory = sqlite3.Row

                # Find recent open loops that haven't been resolved
                rows = conn.execute(
                    """SELECT entity_id, title, description, status, created_at
                       FROM entity_meta
                       WHERE entity_type = 'open_loop'
                       AND status = 'open'
                       AND created_at > ?
                       ORDER BY created_at DESC LIMIT 10""",
                    (now - 86400 * 7,),
                ).fetchall()

                for row in rows:
                    age_days = (now - row["created_at"]) / 86400
                    observations.append(SensoryObservation(
                        source="conversation",
                        category="unanswered_question",
                        description=f"Open loop: {row['title'] or row['description'][:100]}",
                        target_path=f"loop:{row['entity_id']}",
                        urgency_hint=min(0.9, 0.4 + age_days * 0.1),
                        evidence={"age_days": age_days, "loop_id": row["entity_id"]},
                    ))

                # Find recent high-confidence hypotheses that were never validated
                rows = conn.execute(
                    """SELECT belief_id, claim_text, confidence, created_at
                       FROM causal_beliefs
                       WHERE status = 'hypothesis'
                       AND confidence > 0.7
                       AND created_at > ?
                       ORDER BY created_at DESC LIMIT 5""",
                    (now - 86400 * 3,),
                ).fetchall()

                for row in rows:
                    observations.append(SensoryObservation(
                        source="conversation",
                        category="unanswered_question",
                        description=f"Unverified hypothesis: {row['claim_text'][:120]}",
                        target_path=f"belief:{row['belief_id']}",
                        urgency_hint=row["confidence"],
                        evidence={"confidence": row["confidence"]},
                    ))
        except Exception as e:
            logger.debug("[sensory] conversation scan: %s", e)

        return observations

    def _scan_pending_todos(self) -> List[SensoryObservation]:
        """Proveri todo fajlove ili todo.txt."""
        observations = []
        todo_paths = [
            Path.home() / "TODO.md",
            Path.home() / "todo.txt",
            Path.home() / ".hermes" / "todo.md",
        ]
        for tp in todo_paths:
            if not tp.exists():
                continue
            try:
                text = tp.read_text()
                pending = [l for l in text.split("\n") if l.strip().startswith("- [ ]") or l.strip().startswith("[ ]")]
                if len(pending) >= 3:
                    observations.append(SensoryObservation(
                        source="todo",
                        category="stuck_task",
                        description=f"{len(pending)} pending todos in {tp.name}",
                        target_path=str(tp),
                        urgency_hint=min(0.8, 0.3 + len(pending) * 0.05),
                        evidence={"pending_count": len(pending)},
                    ))
            except Exception:
                continue
        return observations

    def _scan_cwm_knowledge_gaps(self) -> List[SensoryObservation]:
        """Detektuje gde CWM ima malo evidence = šta Nucleus ne zna."""
        observations = []
        try:
            with closing(sqlite3.connect(self._brain_db, timeout=30)) as conn:
                conn.execute("PRAGMA busy_timeout=30000")
                conn.execute("PRAGMA query_only=ON")
                conn.row_factory = sqlite3.Row

                # Domains with very few facts = gaps
                rows = conn.execute(
                    """SELECT scope_key, COUNT(*) as c FROM causal_facts
                       GROUP BY scope_key HAVING c < 3
                       ORDER BY c LIMIT 5""",
                ).fetchall()

                for row in rows:
                    scope = row["scope_key"]
                    if scope in {"global", "system"}:
                        continue
                    observations.append(SensoryObservation(
                        source="cwm_gap",
                        category="knowledge_gap",
                        description=f"Low knowledge in domain '{scope}' ({row['c']} facts)",
                        target_path=scope,
                        urgency_hint=0.5,
                        evidence={"fact_count": row["c"]},
                    ))
        except Exception:
            pass

        return observations

    def get_context_summary(self) -> Dict:
        """Vraća sažetak trenutnog korisničkog konteksta za LLM/grounding."""
        recent = self._observation_history[-20:]
        by_source = {}
        for obs in recent:
            by_source.setdefault(obs.source, []).append(obs.category)

        active_projects = set()
        for obs in recent:
            if obs.source in {"git", "filesystem"} and obs.target_path:
                p = Path(obs.target_path)
                if p.is_dir():
                    active_projects.add(p.name)
                elif p.parent:
                    active_projects.add(p.parent.name)

        return {
            "active_projects": list(active_projects)[:10],
            "recent_activity": {s: len(c) for s, c in by_source.items()},
            "open_loops": len([o for o in recent if o.category == "unanswered_question"]),
            "pending_changes": len([o for o in recent if o.category == "code_change"]),
            "last_observation_time": max((o.timestamp for o in recent), default=0),
        }
