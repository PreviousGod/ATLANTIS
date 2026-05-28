"""Nucleus Engine — lightweight core for Pargod lookups and research escalation.

Heartbeat removed — the 1Hz tick loop (sensor, entropy, ego, world model,
proactive suggester, autonomous actions) was pure infrastructure overhead.
Hooks + bridge contributions deliver all value without the CPU cost.

Kept:
  - Pargod graph (node/edge lookup, seeding)
  - Intervention engine (mistake detection in pre_tool hooks)
  - LiveBrainSync (fact/artifact/recipe writes from hooks)
  - SessionState (runtime context shared across hooks)
  - _execute_instinct (used by monkey-patch bypass)
  - _escalate_web (used by monkey-patch research bypass)
"""
import json
import logging
import os
import time
from pathlib import Path

from .config import DATA_DIR, INSTINCTS_DIR, LOG_FILE
from .pargod import Pargod
from .instinct_guard import InstinctGuard
from .researcher import research_problem
from .intervention import InterventionEngine
from .live_brain_sync import LiveBrainSync
from .session_state import get_session_state

log = logging.getLogger("nucleus")


class Nucleus:
    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        INSTINCTS_DIR.mkdir(parents=True, exist_ok=True)
        self.pargod = Pargod()
        self.guard = InstinctGuard()
        self.intervention = InterventionEngine()
        self.brain_sync = LiveBrainSync()
        self.session_state = get_session_state()
        self._setup_logging()
        self._auto_seed()

    def _setup_logging(self):
        for handler in log.handlers:
            if isinstance(handler, logging.FileHandler) and Path(handler.baseFilename) == LOG_FILE:
                log.setLevel(logging.INFO)
                return
        handler = logging.FileHandler(LOG_FILE)
        handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", "%H:%M:%S"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)

    def _auto_seed(self):
        if not self.pargod.list_nodes():
            seed = Path(__file__).parent / "seed_graph.json"
            if seed.exists():
                self.pargod.seed_from_json(str(seed))
                log.info(f"Auto-seeded graph: {len(self.pargod.list_nodes())} nodes")

    def _execute_instinct(self, tool_label):
        script = INSTINCTS_DIR / f"{tool_label}.py"
        if not script.exists():
            return {"success": False, "error": f"Missing: {script}"}
        result = self.guard.execute(str(script))
        if result["success"]:
            self.pargod.record_use(tool_label)
        return result

    def _escalate_web(self, problem):
        """Middle layer: structured local/web research before waking LLM."""
        result = research_problem(
            problem,
            brain_sync=self.brain_sync,
            pargod=self.pargod,
            include_web=True,
        )
        if not result:
            return None
        log.info(
            "RESEARCH: learned problem=%r scope=%s local=%d web=%d",
            problem, result.get("scope"), len(result.get("local_sources", [])),
            len(result.get("web_sources", [])),
        )
        return result
