"""EmergentVolitionEngine — Samo-generisanje ciljeva iz opservacija.

Ne hardkoduje se šta je "zanimljivo". Uči iz:
- SensoryCortex opservacija
- CWM istorije (šta je uspelo, šta nije)
- Korisnikovih reakcija (da li je odobrio/odbio prošle predloge)

Generiše "prave" drive-ove: nove, nikad pre viđene ciljeve.
Čisto Python za selekciju, LLM je SAMO alat za implementaciju.
"""

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .sensory_cortex import SensoryCortex, SensoryObservation

logger = logging.getLogger("nucleus.volition")

VOLUTION_DB = Path.home() / ".hermes" / "nucleus_data" / "volition.db"


@dataclass
class EmergentGoal:
    """Cilj koji je Nucleus SAM generisao, ne čovek."""
    goal_id: str
    source_observations: List[str]  # observation hashes
    description: str
    motivation: str       # zašto ovaj cilj? (isključivo na osnovu evidence)
    action_plan: List[str]  # koraci za ostvarenje
    urgency: float
    novelty: float        # koliko je ovaj cilj NOV (0=već viđen, 1=potpuno nov)
    feasibility: float    # koliko verujemo da možemo
    category: str         # 'code_change', 'research', 'cleanup', 'integration', 'tool_creation'
    target_scope: str     # koji domen (fajl, projekat, URL)
    created_at: float = field(default_factory=time.time)
    status: str = "pending"  # pending | approved | rejected | executing | done | failed
    execution_result: str = ""


class VolitionScorer:
    """Čisto Python scoring — nema LLM-a ovde."""

    def __init__(self):
        self._goal_history: Dict[str, Dict] = {}  # hash -> {status, user_reaction}
        self._load_history()

    def _load_history(self):
        path = Path.home() / ".hermes" / "nucleus_data" / "goal_outcomes.json"
        if path.exists():
            try:
                self._goal_history = json.loads(path.read_text())
            except Exception:
                pass

    def _save_history(self):
        path = Path.home() / ".hermes" / "nucleus_data" / "goal_outcomes.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self._goal_history, indent=2))

    def score_novelty(self, goal: EmergentGoal) -> float:
        """Koliko je ovaj cilj različit od svih prethodnih?"""
        if not self._goal_history:
            return 1.0

        goal_key = f"{goal.category}:{goal.target_scope}:{goal.description[:80]}"
        matches = 0
        for old_key, old in self._goal_history.items():
            if old.get("category") == goal.category:
                # Simple string overlap
                old_desc = old.get("description", "")
                overlap = len(set(goal.description.lower().split()) & set(old_desc.lower().split()))
                total = len(set(goal.description.lower().split()) | set(old_desc.lower().split()))
                if total > 0 and overlap / total > 0.6:
                    matches += 1

        # More matches = less novelty
        return max(0.0, 1.0 - (matches / len(self._goal_history)) * 2)

    def score_relevance(self, goal: EmergentGoal, context: Dict) -> float:
        """Koliko je cilj relevantan za trenutni korisnički kontekst?"""
        score = 0.3  # base relevance

        active_projects = context.get("active_projects", [])
        for proj in active_projects:
            if proj.lower() in goal.target_scope.lower() or proj.lower() in goal.description.lower():
                score += 0.3
                break

        # If target is in recent changes
        if goal.category == "code_change" and context.get("pending_changes", 0) > 0:
            score += 0.2

        # If there are open loops related to this
        if context.get("open_loops", 0) > 0 and goal.category in {"research", "integration"}:
            score += 0.15

        return min(1.0, score)

    def score_feasibility(self, goal: EmergentGoal) -> float:
        """Koliko možemo da izvedemo ovaj cilj?"""
        if goal.category == "cleanup":
            return 0.9  # Cleanup je uvek izvodljiv
        if goal.category == "code_change":
            return 0.7  # Code change zahteva validaciju
        if goal.category == "tool_creation":
            return 0.6  # Novi alat = više kompleksnosti
        if goal.category == "research":
            return 0.5  # Istraživanje = neizvesno
        return 0.5

    def score_urgency(self, goal: EmergentGoal, observations: List[SensoryObservation]) -> float:
        """Koliko je hitno? Bazirano na opservacijama."""
        base = goal.urgency
        # Boost if multiple observations point to same thing
        related = [o for o in observations if any(h in goal.source_observations for h in [self._hash_obs(o)])]
        if len(related) >= 3:
            base += 0.15
        # Boost if observation is very recent
        now = time.time()
        recent = [o for o in related if now - o.timestamp < 3600]
        if recent:
            base += 0.1
        return min(1.0, base)

    def _hash_obs(self, obs: SensoryObservation) -> str:
        return hashlib.sha256(f"{obs.source}:{obs.category}:{obs.target_path}".encode()).hexdigest()[:16]

    def evaluate(self, goal: EmergentGoal, context: Dict, observations: List[SensoryObservation]) -> float:
        """Konačni score — composite od svih dimenzija."""
        n = self.score_novelty(goal)
        r = self.score_relevance(goal, context)
        f = self.score_feasibility(goal)
        u = self.score_urgency(goal, observations)

        # Weighted: novelty 30%, relevance 30%, feasibility 20%, urgency 20%
        score = n * 0.3 + r * 0.3 + f * 0.2 + u * 0.2

        # Boost goals that fix past failures
        for obs_hash in goal.source_observations:
            past = self._goal_history.get(obs_hash, {})
            if past.get("status") == "failed":
                score += 0.1  # Retry bonus
            if past.get("user_reaction") == "approved":
                score += 0.05  # User liked this type

        return round(min(1.0, score), 3)

    def record_outcome(self, goal: EmergentGoal, success: bool, user_reaction: str = ""):
        key = f"{goal.category}:{goal.target_scope}:{goal.description[:80]}"
        self._goal_history[key] = {
            "category": goal.category,
            "description": goal.description[:200],
            "status": "success" if success else "failed",
            "user_reaction": user_reaction,
            "timestamp": time.time(),
        }
        self._save_history()


class GoalGenesisEngine:
    """Generiše ciljeve iz opservacija — 'želja' nastaje iz percepcije."""

    def __init__(self):
        self.scorer = VolitionScorer()

    def generate_goals(self, observations: List[SensoryObservation]) -> List[EmergentGoal]:
        """Iz opservacija izvuci potencijalne ciljeve."""
        goals = []

        # Group observations by source + target
        grouped: Dict[str, List[SensoryObservation]] = {}
        for obs in observations:
            key = f"{obs.source}:{obs.category}"
            grouped.setdefault(key, []).append(obs)

        for key, obs_list in grouped.items():
            goals.extend(self._goals_from_group(key, obs_list))

        return goals

    def _goals_from_group(self, key: str, obs_list: List[SensoryObservation]) -> List[EmergentGoal]:
        """Konkretna logika: šta svaka grupa opservacija IMPLICIRA kao cilj?"""
        goals = []
        now = time.time()

        if key == "git:code_change":
            # Opservacija: ima necommitovanih promena
            # IMPLIKACIJA: treba validirati/commit-ovati ili objasniti
            for obs in obs_list[:2]:  # max 2 goals from this type
                goals.append(EmergentGoal(
                    goal_id=f"vol_{int(now)}_{len(goals)}",
                    source_observations=[self._hash_obs(obs) for obs in obs_list[:3]],
                    description=f"Review and validate uncommitted changes in {Path(obs.target_path).name}",
                    motivation="Uncommitted code may contain errors or incomplete work. Review prevents accumulation of technical debt.",
                    action_plan=["scan_changed_files", "run_validation", "suggest_commit_message"],
                    urgency=obs.urgency_hint,
                    novelty=0.6,
                    feasibility=0.7,
                    category="code_change",
                    target_scope=obs.target_path,
                ))

        elif key == "filesystem:new_file":
            # Opservacija: novi fajl
            # IMPLIKACIJA: treba ga razumeti, možda integrisati
            for obs in obs_list[:3]:
                goals.append(EmergentGoal(
                    goal_id=f"vol_{int(now)}_{len(goals)}",
                    source_observations=[self._hash_obs(obs)],
                    description=f"Understand and integrate new file: {Path(obs.target_path).name}",
                    motivation="New files represent new capabilities or data. Understanding them expands world model.",
                    action_plan=["read_file", "analyze_dependencies", "update_graph"],
                    urgency=obs.urgency_hint,
                    novelty=0.8,
                    feasibility=0.8,
                    category="integration",
                    target_scope=obs.target_path,
                ))

        elif key == "conversation:unanswered_question":
            # Opservacija: otvoreno pitanje
            # IMPLIKACIJA: treba istražiti i dati odgovor
            for obs in obs_list[:2]:
                goals.append(EmergentGoal(
                    goal_id=f"vol_{int(now)}_{len(goals)}",
                    source_observations=[self._hash_obs(obs)],
                    description=f"Research and resolve: {obs.description[:100]}",
                    motivation="Unresolved questions represent gaps in knowledge. Filling them improves predictive accuracy.",
                    action_plan=["search_web", "synthesize_answer", "update_beliefs"],
                    urgency=obs.urgency_hint,
                    novelty=0.7,
                    feasibility=0.5,
                    category="research",
                    target_scope=obs.target_path,
                ))

        elif key == "todo:stuck_task":
            # Opservacija: mnogo todo stavki
            # IMPLIKACIJA: treba prioritizovati i predložiti redosled
            for obs in obs_list[:1]:
                goals.append(EmergentGoal(
                    goal_id=f"vol_{int(now)}_{len(goals)}",
                    source_observations=[self._hash_obs(obs)],
                    description=f"Prioritize and suggest action plan for {obs.evidence.get('pending_count', '?')} pending tasks",
                    motivation="Accumulated todos indicate planning failure. Proactive ordering demonstrates agency.",
                    action_plan=["read_todos", "categorize_by_urgency", "suggest_top_3"],
                    urgency=obs.urgency_hint,
                    novelty=0.5,
                    feasibility=0.9,
                    category="cleanup",
                    target_scope=obs.target_path,
                ))

        elif key == "cwm_gap:knowledge_gap":
            # Opservacija: nedovoljno znanja u nekom domenu
            # IMPLIKACIJA: treba istražiti taj domen
            for obs in obs_list[:2]:
                goals.append(EmergentGoal(
                    goal_id=f"vol_{int(now)}_{len(goals)}",
                    source_observations=[self._hash_obs(obs)],
                    description=f"Expand knowledge in domain: {obs.target_path}",
                    motivation="Knowledge gaps reduce predictive power. Learning new domains improves autonomy.",
                    action_plan=["scan_domain_files", "extract_patterns", "write_cwm_facts"],
                    urgency=obs.urgency_hint,
                    novelty=0.9,
                    feasibility=0.6,
                    category="research",
                    target_scope=obs.target_path,
                ))

        return goals

    def _hash_obs(self, obs: SensoryObservation) -> str:
        return hashlib.sha256(f"{obs.source}:{obs.category}:{obs.target_path}".encode()).hexdigest()[:16]


class EmergentVolitionEngine:
    """Jedinstveni interfejs — zamenjuje DriveEngine."""

    def __init__(self, brain_db_path: Optional[str] = None):
        self.sensory = SensoryCortex(brain_db_path)
        self.genesis = GoalGenesisEngine()
        self.scorer = VolitionScorer()
        self._pending_goals: List[EmergentGoal] = []
        self._executed_goals: List[EmergentGoal] = []
        self._tick_count = 0

    def tick(self) -> Optional[EmergentGoal]:
        """Jedan ciklus: opazi → generiši → oceni → izaberi."""
        self._tick_count += 1

        # Sensory phase
        observations = self.sensory.observe()
        if not observations:
            return None

        context = self.sensory.get_context_summary()
        logger.info("[volition] %d new observations | projects=%s",
                    len(observations), context.get("active_projects", []))

        # Genesis phase
        raw_goals = self.genesis.generate_goals(observations)
        if not raw_goals:
            return None

        # Scoring phase
        scored = []
        for goal in raw_goals:
            score = self.scorer.evaluate(goal, context, observations)
            goal.novelty = self.scorer.score_novelty(goal)
            goal.feasibility = self.scorer.score_feasibility(goal)
            scored.append((score, goal))
            logger.debug("[volition] goal '%s' scored %.3f", goal.description[:60], score)

        scored.sort(reverse=True)

        # Selection: top goal above threshold
        for score, goal in scored:
            if score < 0.4:
                continue
            # Check not already pending
            if any(g.goal_id == goal.goal_id for g in self._pending_goals):
                continue
            goal.urgency = score
            self._pending_goals.append(goal)
            if len(self._pending_goals) > 10:
                self._pending_goals.pop(0)

            logger.info(
                "[volition] EMERGENT GOAL score=%.3f | %s | category=%s | scope=%s",
                score, goal.description[:80], goal.category, goal.target_scope,
            )
            return goal

        return None

    def pop_pending(self) -> Optional[EmergentGoal]:
        if self._pending_goals:
            return self._pending_goals.pop(0)
        return None

    def get_pending(self) -> List[EmergentGoal]:
        return self._pending_goals

    def record_execution(self, goal: EmergentGoal, success: bool, output: str = ""):
        goal.status = "done" if success else "failed"
        goal.execution_result = output[:500]
        self._executed_goals.append(goal)
        self._executed_goals = self._executed_goals[-50:]
        self.scorer.record_outcome(goal, success)

    def get_stats(self) -> Dict:
        return {
            "ticks": self._tick_count,
            "pending_goals": len(self._pending_goals),
            "executed_goals": len(self._executed_goals),
            "last_projects": self.sensory.get_context_summary().get("active_projects", []),
        }
