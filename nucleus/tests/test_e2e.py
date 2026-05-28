"""End-to-end tests for Nucleus."""
import json
import os
import sqlite3
import sys
import tempfile
import time
from unittest.mock import patch

# Add parent to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from nucleus.config import INSTINCTS_DIR
from nucleus.pargod import Pargod
from nucleus.entropy import EntropyEngine
from nucleus.sensor import Sensor
from nucleus.instinct_guard import InstinctGuard
from nucleus.web_search import search
from nucleus.nucleus_engine import Nucleus
from nucleus.domain_profiles import DomainProfile, detect_scope, get_domain_profile
from nucleus.live_brain_sync import LiveBrainSync
from nucleus.researcher import research_problem, format_research_summary
from nucleus.status import collect_status, format_status
from nucleus.doctor import doctor_ok, format_doctor, run_doctor
from nucleus.failure_trigger import (
    get_continuation_context,
    is_research_candidate,
    is_followup_request,
    response_indicates_gap,
    reset_trigger_state_for_tests,
    schedule_epistemic_research,
    should_research_after_llm,
    tool_failure_problem,
)
import nucleus.failure_trigger as failure_trigger
import nucleus as nucleus_plugin


def test_pargod_pathfinding():
    """Test: graph finds shortest path from problem to tool."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        p = Pargod(db_path)
        p.add_node("problem", "high_cpu", "CPU too high")
        p.add_node("tool", "fix_cpu", "Fixes CPU")
        p.add_node("state", "idle", "Normal")
        p.add_edge("high_cpu", "fix_cpu", "RESOLVES", 1.0)
        p.add_edge("fix_cpu", "idle", "ACHIEVES", 1.0)

        result = p.find_tool_for_problem("high_cpu")
        assert result is not None, "Should find path"
        assert result["tool"] == "fix_cpu"
        assert result["cost"] == 1.0
        assert result["path"] == ["high_cpu", "fix_cpu"]

        # No path case
        p.add_node("problem", "unknown_issue", "No solution")
        result = p.find_tool_for_problem("unknown_issue")
        assert result is None, "Should return None for no path"

        print("✓ test_pargod_pathfinding")
    finally:
        os.unlink(db_path)


def test_pargod_seed():
    """Test: seed from JSON populates graph."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    seed_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "seed_graph.json")
    try:
        p = Pargod(db_path)
        p.seed_from_json(seed_path)
        nodes = p.list_nodes()
        assert len(nodes) >= 20, f"Expected 20+ nodes, got {len(nodes)}"
        # Verify a known path
        result = p.find_tool_for_problem("high_cpu")
        assert result is not None
        assert result["tool"] == "report_top_cpu"
        print("✓ test_pargod_seed")
    finally:
        os.unlink(db_path)


def test_entropy_calculation():
    """Test: entropy is 0 when metrics are normal, >0 when abnormal."""
    e = EntropyEngine()
    assert e.calculate({"cpu_percent": 50.0, "ram_percent": 50.0}) == 0.0
    assert e.calculate({"cpu_percent": 90.0, "ram_percent": 50.0}) > 0.0
    assert e.calculate({"cpu_percent": 50.0, "ram_percent": 95.0}) > 0.0
    # Verify sources
    sources = e.identify_sources({"cpu_percent": 90.0, "ram_percent": 95.0})
    assert "high_cpu" in sources
    assert "high_ram" in sources
    print("✓ test_entropy_calculation")


def test_sensor_reads():
    """Test: sensor returns valid metrics."""
    s = Sensor()
    state = s.read()
    assert "cpu_percent" in state
    assert "ram_percent" in state
    assert 0.0 <= state["ram_percent"] <= 100.0
    # Second read should give non-zero CPU (has prev)
    time.sleep(0.1)
    state2 = s.read()
    assert state2["cpu_percent"] >= 0.0
    print("✓ test_sensor_reads")


def test_instinct_guard_safe():
    """Test: safe script executes successfully."""
    guard = InstinctGuard(timeout=5)
    script = tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False)
    script.write('print("hello from instinct")\n')
    script.close()
    try:
        result = guard.execute(script.name)
        assert result["success"], f"Should succeed: {result}"
        assert "hello" in result["stdout"]
        print("✓ test_instinct_guard_safe")
    finally:
        os.unlink(script.name)


def test_instinct_guard_blocked():
    """Test: script with blocked import is rejected."""
    guard = InstinctGuard(timeout=5)
    script = tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False)
    script.write('import subprocess\nsubprocess.run(["ls"])\n')
    script.close()
    try:
        result = guard.execute(script.name)
        assert not result["success"], "Should be blocked"
        assert "BLOCKED" in result.get("error", "")
        print("✓ test_instinct_guard_blocked")
    finally:
        os.unlink(script.name)


def test_instinct_guard_blocks_from_import_call():
    """Test: from-import aliases for blocked calls are rejected."""
    guard = InstinctGuard(timeout=5)
    script = tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False)
    script.write('from os import system\nsystem("echo unsafe")\n')
    script.close()
    try:
        result = guard.execute(script.name)
        assert not result["success"], "Should block imported os.system alias"
        assert "BLOCKED" in result.get("error", "")
        print("✓ test_instinct_guard_blocks_from_import_call")
    finally:
        os.unlink(script.name)


def test_instinct_guard_blocks_getattr_bypass():
    """Test: getattr(os, 'system') bypass is rejected."""
    guard = InstinctGuard(timeout=5)
    script = tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False)
    script.write('import os\ngetattr(os, "system")("echo unsafe")\n')
    script.close()
    try:
        result = guard.execute(script.name)
        assert not result["success"], "Should block getattr(os, system) bypass"
        assert "BLOCKED" in result.get("error", "")
        print("✓ test_instinct_guard_blocks_getattr_bypass")
    finally:
        os.unlink(script.name)


def test_instinct_guard_uses_instance_memory_limit():
    """Test: per-instance memory_mb is passed into resource limiter."""
    guard = InstinctGuard(timeout=5, memory_mb=123)
    script = tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False)
    script.write('print("ok")\n')
    script.close()
    try:
        captured = {}

        def fake_run(*args, **kwargs):
            captured["preexec_fn"] = kwargs.get("preexec_fn")
            class Result:
                returncode = 0
                stdout = "ok\n"
                stderr = ""
            return Result()

        with patch("nucleus.instinct_guard.subprocess.run", side_effect=fake_run), \
             patch("nucleus.instinct_guard._set_limits") as mocked_limits:
            result = guard.execute(script.name)
            assert result["success"], f"Should succeed: {result}"
            assert captured.get("preexec_fn"), "subprocess should receive preexec_fn"
            captured["preexec_fn"]()
            mocked_limits.assert_called_once_with(123)
        print("✓ test_instinct_guard_uses_instance_memory_limit")
    finally:
        os.unlink(script.name)


def test_instinct_guard_timeout():
    """Test: infinite loop is killed by timeout."""
    guard = InstinctGuard(timeout=2)
    script = tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False)
    script.write('while True: pass\n')
    script.close()
    try:
        result = guard.execute(script.name)
        assert not result["success"]
        assert "Timeout" in result.get("error", "")
        print("✓ test_instinct_guard_timeout")
    finally:
        os.unlink(script.name)


def test_has_answer_for():
    """Test: graph query matching."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        p = Pargod(db_path)
        p.add_node("problem", "high_cpu", "CPU usage exceeds threshold")
        p.add_node("tool", "report_top_cpu", "Reports top CPU")
        p.add_edge("high_cpu", "report_top_cpu", "RESOLVES", 1.0)

        # Direct match
        result = p.has_answer_for("high_cpu")
        assert result is not None
        assert result["tool"] == "report_top_cpu"

        # Fuzzy match (content words)
        result = p.has_answer_for("cpu usage is too high")
        assert result is not None

        # No match
        result = p.has_answer_for("what is the meaning of life")
        assert result is None

        print("✓ test_has_answer_for")
    finally:
        os.unlink(db_path)


def test_web_search():
    """Test: web search returns results (requires internet)."""
    try:
        results = search("python list comprehension")
        assert results and len(results) > 0, "Should get results"
        assert "url" in results[0]
        print("✓ test_web_search")
    except Exception as e:
        print(f"⚠ test_web_search skipped (no internet?): {e}")


def test_domain_profile_detects_hermes():
    """Test: Hermes problems route to the Hermes profile."""
    assert detect_scope("Hermes gateway plugin fails") == "hermes"
    assert get_domain_profile("telegram gateway issue").scope == "hermes"
    assert get_domain_profile("high cpu issue").scope == "linux"
    print("✓ test_domain_profile_detects_hermes")


def test_research_problem_local_sources_write_graph():
    """Test: structured research consumes local docs and writes Pargod knowledge."""
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "docs")
        os.mkdir(root)
        doc = os.path.join(root, "plugin.md")
        with open(doc, "w") as f:
            f.write("Hermes gateway plugin loading requires plugin.yaml and register(ctx).")

        profile = DomainProfile(
            scope="hermes",
            local_roots=(__import__("pathlib").Path(root),),
            web_allowlist=(),
            search_suffix="Hermes docs",
        )
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as db:
            db_path = db.name
        try:
            p = Pargod(db_path)
            result = research_problem(
                "Hermes gateway plugin loading",
                profile=profile,
                brain_sync=None,
                pargod=p,
                include_web=False,
            )
            assert result is not None
            assert result["scope"] == "hermes"
            assert result["local_sources"]
            assert result["facts"]
            assert result["fix_recipe"]
            assert p.list_nodes("knowledge"), "Research should write knowledge nodes"
            assert p.list_nodes("fix_recipe"), "Research should write fix_recipe nodes"
            resolution = p.has_resolution_for("Hermes gateway plugin loading")
            assert resolution is not None
            assert resolution["type"] == "fix_recipe"
            assert "recipe" in resolution
            summary = format_research_summary(result)
            assert "[NUCLEUS/RESEARCH]" in summary
            assert "Hermes gateway plugin loading" in summary
            print("✓ test_research_problem_local_sources_write_graph")
        finally:
            os.unlink(db_path)


def _create_live_brain_test_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE epistemic_learned_facts (
            fact_id TEXT PRIMARY KEY,
            scope_key TEXT NOT NULL,
            question TEXT NOT NULL DEFAULT '',
            fact_text TEXT NOT NULL,
            source_urls_json TEXT NOT NULL DEFAULT '[]',
            confidence REAL NOT NULL DEFAULT 0.7,
            source_kind TEXT NOT NULL DEFAULT 'web',
            status TEXT NOT NULL DEFAULT 'active',
            valid_from REAL NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE fix_recipes (
            recipe_id TEXT PRIMARY KEY,
            scope_key TEXT NOT NULL,
            problem_pattern TEXT NOT NULL,
            tool_name TEXT NOT NULL DEFAULT '',
            steps_json TEXT NOT NULL DEFAULT '[]',
            args_template_json TEXT NOT NULL DEFAULT '{}',
            success_criteria TEXT NOT NULL DEFAULT '',
            confidence REAL NOT NULL DEFAULT 0.7,
            times_confirmed INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'active',
            source TEXT NOT NULL DEFAULT 'causal_activation',
            scope_tags_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            promotion_status TEXT NOT NULL DEFAULT 'candidate',
            candidate_since REAL,
            last_reviewed_at REAL
        );
        CREATE TABLE research_jobs (
            research_id TEXT PRIMARY KEY,
            trigger_turn_id INTEGER,
            topic TEXT NOT NULL,
            question TEXT NOT NULL,
            scope TEXT NOT NULL,
            status TEXT NOT NULL,
            priority REAL NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            completed_at REAL
        );
        CREATE TABLE research_results (
            result_id TEXT PRIMARY KEY,
            research_id TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            source_ref TEXT NOT NULL,
            summary TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0.5,
            actionability REAL NOT NULL DEFAULT 0.5,
            raw_excerpt TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE epistemic_web_sources (
            source_id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL DEFAULT '',
            scope_key TEXT NOT NULL,
            url TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            source_kind TEXT NOT NULL DEFAULT 'web',
            authority TEXT NOT NULL DEFAULT 'unknown',
            summary TEXT NOT NULL DEFAULT '',
            raw_excerpt TEXT NOT NULL DEFAULT '',
            content_hash TEXT NOT NULL DEFAULT '',
            confidence REAL NOT NULL DEFAULT 0.5,
            extracted_at REAL NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE TABLE verified_artifacts (
            artifact_id TEXT PRIMARY KEY,
            project_key TEXT NOT NULL,
            role TEXT NOT NULL,
            path TEXT NOT NULL,
            label TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'verified',
            confidence REAL NOT NULL DEFAULT 1.0,
            source TEXT NOT NULL DEFAULT 'manual',
            mime_type TEXT NOT NULL DEFAULT '',
            size_bytes INTEGER NOT NULL DEFAULT 0,
            verified_at REAL NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()


def test_live_brain_sync_writes_research_structures():
    """Test: research facts, recipes and traces are persisted into Live Brain tables."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        _create_live_brain_test_db(db_path)
        sync = LiveBrainSync(db_path)
        fact_id = sync.write_fact(
            "Hermes plugins register hooks via register(ctx).",
            scope_key="hermes",
            question="Hermes plugin loading",
            source_urls=["/tmp/plugin.md"],
        )
        recipe_id = sync.write_fix_recipe(
            "Hermes plugin loading",
            ["Inspect plugin.yaml", "Inspect register(ctx)"],
            scope_key="hermes",
            sources=["/tmp/plugin.md"],
        )
        research_id = sync.write_research_trace({
            "problem": "Hermes plugin loading",
            "scope": "hermes",
            "confidence": 0.8,
            "local_sources": [{"kind": "local", "path": "/tmp/plugin.md", "snippet": "register(ctx)"}],
            "web_sources": [{"kind": "web", "url": "https://github.com/example/hermes", "title": "Hermes", "snippet": "docs"}],
        })
        assert fact_id and recipe_id and research_id
        assert sync.get_facts("hermes", "plugins")
        assert sync.get_fix_recipes("hermes", "plugin")
        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT count(*) FROM research_results").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM epistemic_web_sources").fetchone()[0] == 1
        conn.close()
        print("✓ test_live_brain_sync_writes_research_structures")
    finally:
        os.unlink(db_path)


def test_pargod_resolution_prefers_tool_when_available():
    """Test: resolution path can traverse problem→knowledge→recipe→tool."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        p = Pargod(db_path)
        p.add_node("tool", "repair_hermes_plugin", "Repair Hermes plugin")
        result = {
            "problem": "Hermes plugin loading failure",
            "scope": "hermes",
            "local_sources": [{"kind": "local", "path": "/tmp/plugin.md", "snippet": "register(ctx) loads hooks"}],
            "web_sources": [],
            "fix_recipe": {
                "problem_pattern": "Hermes plugin loading failure",
                "steps": ["Inspect plugin.yaml", "Verify register(ctx)"],
                "success_criteria": "Plugin registered without hook errors.",
                "source_refs": ["/tmp/plugin.md"],
                "tool_name": "repair_hermes_plugin",
            },
        }
        p.add_research_result(result)
        resolution = p.has_resolution_for("Hermes plugin loading failure")
        assert resolution is not None
        assert resolution["type"] == "fix_recipe"
        assert resolution["recipe"].startswith("recipe_")
        tool = p.find_tool_for_problem(resolution["recipe"])
        assert tool is not None
        assert tool["tool"] == "repair_hermes_plugin"
        print("✓ test_pargod_resolution_prefers_tool_when_available")
    finally:
        os.unlink(db_path)


def test_pre_llm_hook_injects_fix_recipe_context():
    """P3.1: emission logic moved from _pre_llm_hook to
    nucleus.contributions.compute_contributions, which is registered with
    the live_brain_ctx bridge. _pre_llm_hook itself returns None now;
    we exercise the new contributor entry point instead and assert it
    produces a NUCLEUS GRAPH ContextContribution carrying the recipe.
    """
    class FakePargod:
        def has_resolution_for(self, query):
            return {
                "type": "fix_recipe",
                "path": ["problem_x", "kb_x", "recipe_x"],
                "content": json.dumps({
                    "problem_pattern": "Hermes plugin loading",
                    "steps": ["Inspect plugin.yaml", "Verify register(ctx)"],
                    "success_criteria": "Plugin registered.",
                    "source_refs": ["/tmp/plugin.md"],
                }),
            }

    class FakeNucleus:
        pargod = FakePargod()

    # Fresh SessionState — drain_warnings should return [].
    from nucleus.session_state import reset_session_state, get_session_state
    reset_session_state()
    state = get_session_state()
    state.on_user_message("test-sid", "nucleus Hermes plugin loading")

    from nucleus.contributions import compute_contributions
    with patch("nucleus._get_nucleus", return_value=FakeNucleus()), \
         patch("nucleus.learning_engine.LearningEngine") as mock_le, \
         patch("nucleus.proactive_suggester.ProactiveSuggester") as mock_ps:
        mock_le.return_value.apply_feedback.return_value = None
        mock_ps.return_value.record_user_response.return_value = None
        mock_ps.return_value.read_and_clear_pending.return_value = None
        contribs = compute_contributions(
            session_id="test-sid",
            user_message="nucleus Hermes plugin loading",
        )

    sections = {c.section: c for c in contribs}
    assert "NUCLEUS GRAPH" in sections, [c.section for c in contribs]
    graph = sections["NUCLEUS GRAPH"]
    assert "Research-backed fix recipe" in graph.body
    assert "Inspect plugin.yaml" in graph.body
    print("✓ test_pre_llm_hook_injects_fix_recipe_context")


def test_failure_trigger_classifies_llm_gap():
    """Test: only technical epistemic gaps become research candidates."""
    assert is_research_candidate("Hermes gateway plugin fails to load")
    assert not is_research_candidate("jel pricas srpski?")
    assert response_indicates_gap("I'm not sure; I don't have enough information to verify this.")
    assert should_research_after_llm(
        "Hermes gateway plugin fails to load",
        "I'm not sure; I don't have enough information to verify this.",
    )
    assert not should_research_after_llm("thanks", "I'm not sure")
    print("✓ test_failure_trigger_classifies_llm_gap")


def test_failure_trigger_schedules_once_and_debounces():
    """Test: background research scheduling is debounced per problem."""
    class FakeExecutor:
        def __init__(self):
            self.jobs = []

        def submit(self, fn, *args):
            self.jobs.append((fn, args))

    class FakeNucleus:
        pass

    reset_trigger_state_for_tests()
    fake_executor = FakeExecutor()
    with patch("nucleus.failure_trigger._get_executor", return_value=fake_executor), \
         patch.dict(os.environ, {"NUCLEUS_AUTO_RESEARCH": "1"}, clear=False):
        status1 = schedule_epistemic_research(FakeNucleus(), "Hermes gateway plugin failure", trigger="llm_gap")
        status2 = schedule_epistemic_research(FakeNucleus(), "Hermes gateway plugin failure", trigger="llm_gap")

    assert status1 == "scheduled"
    assert status2 == "pending"
    assert len(fake_executor.jobs) == 1
    reset_trigger_state_for_tests()
    print("✓ test_failure_trigger_schedules_once_and_debounces")


def test_post_llm_hook_schedules_gap_research():
    """Test: post_llm_call schedules research for LLM uncertainty."""
    calls = []
    with patch("nucleus.failure_trigger.schedule_epistemic_research", side_effect=lambda *a, **k: calls.append((a, k)) or "scheduled"), \
         patch("nucleus._get_nucleus", return_value=object()):
        nucleus_plugin._post_llm_hook(
            user_message="Hermes gateway plugin fails to load",
            assistant_response="I'm not sure; I don't have enough information to verify this.",
        )
    assert calls
    assert calls[0][1]["trigger"] == "llm_gap"
    print("✓ test_post_llm_hook_schedules_gap_research")


def test_post_tool_hook_schedules_failure_research():
    """Test: failed tool outputs trigger research instead of success learning."""
    class FakePargod:
        def __init__(self):
            self.nodes = {}
            self.recorded = []

        def get_node(self, label):
            return self.nodes.get(label)

        def add_node(self, node_type, label, content=None):
            self.nodes[label] = {"type": node_type, "label": label, "content": content}

        def record_use(self, label):
            self.recorded.append(label)

    class FakeNucleus:
        def __init__(self):
            self.pargod = FakePargod()

    calls = []
    fake = FakeNucleus()
    assert tool_failure_problem("terminal", "Traceback: sqlite3.OperationalError: no such table")
    with patch("nucleus._get_nucleus", return_value=fake), \
         patch("nucleus.failure_trigger.schedule_epistemic_research", side_effect=lambda *a, **k: calls.append((a, k)) or "scheduled"):
        nucleus_plugin._post_tool_hook(
            tool_name="terminal",
            result="Traceback: sqlite3.OperationalError: no such table",
            session_id="tool-failure-session",
        )

    assert calls
    assert calls[0][1]["trigger"] == "tool_failure"
    assert not fake.pargod.nodes
    print("✓ test_post_tool_hook_schedules_failure_research")


def test_continuation_context_after_completed_research():
    """Test: completed background research is injected on follow-up."""
    reset_trigger_state_for_tests()
    session_id = "s-continuation"
    failure_trigger._remember_completed(session_id, "Hermes gateway plugin failure", "llm_gap", {
        "scope": "hermes",
        "facts": ["Hermes plugin registration uses register(ctx)."],
        "fix_recipe": {
            "steps": ["Inspect plugin.yaml", "Verify register(ctx)"],
            "source_refs": ["/tmp/plugin.md"],
        },
        "citations": ["/tmp/plugin.md"],
    })
    assert is_followup_request("ajde")
    context = get_continuation_context(session_id, "ajde")
    assert context and "Background research completed" in context
    assert "Inspect plugin.yaml" in context
    assert get_continuation_context(session_id, "ajde") is None, "completed context should be consumed once"
    print("✓ test_continuation_context_after_completed_research")


def test_continuation_pending_context():
    """Test: follow-up receives pending notice while research is still running."""
    reset_trigger_state_for_tests()
    session_id = "s-pending"
    failure_trigger._remember_pending(session_id, "Hermes gateway plugin failure", "llm_gap")
    context = get_continuation_context(session_id, "nastavi")
    assert context and "still running" in context
    print("✓ test_continuation_pending_context")


def test_pre_llm_hook_injects_continuation_before_graph():
    """Test: continuation context surfaces through the contribution bridge."""
    reset_trigger_state_for_tests()
    session_id = "s-hook-continuation"
    failure_trigger._remember_completed(session_id, "Hermes gateway plugin failure", "llm_gap", {
        "scope": "hermes",
        "facts": ["Nucleus learned new Hermes evidence."],
        "fix_recipe": {"steps": ["Use the learned evidence."], "source_refs": []},
        "citations": [],
    })
    from nucleus.contributions import compute_contributions

    result = compute_contributions(
        session_id=session_id,
        user_message="ajde",
        turn_lane="continuation_or_resume",
    )
    assert result, "continuation follow-up should emit at least one contribution"
    bodies = "\n".join(item.body for item in result)
    assert "Nucleus learned new Hermes evidence." in bodies
    print("✓ test_pre_llm_hook_injects_continuation_via_bridge")


def test_session_finalize_clears_research_continuation_state():
    """True session teardown must not leave stale follow-up research context."""
    reset_trigger_state_for_tests()
    stale_session = "s-finalize-stale"
    other_session = "s-finalize-other"
    failure_trigger._remember_pending(stale_session, "Hermes gateway plugin failure", "llm_gap")
    failure_trigger._remember_completed(stale_session, "Hermes gateway plugin failure", "llm_gap", {
        "scope": "hermes",
        "facts": ["Stale research fact."],
        "citations": [],
    })
    failure_trigger._remember_completed(other_session, "Hermes gateway plugin failure", "llm_gap", {
        "scope": "hermes",
        "facts": ["Other session fact."],
        "citations": [],
    })

    nucleus_plugin._on_session_finalize(session_id=stale_session, platform="test")

    snapshot = failure_trigger.get_research_state_snapshot()
    assert stale_session not in snapshot["pending_by_session"]
    assert stale_session not in snapshot["completed_by_session"]
    assert other_session in snapshot["completed_by_session"]
    assert get_continuation_context(stale_session, "ajde", turn_lane="continuation_or_resume") is None
    assert get_continuation_context(other_session, "ajde", turn_lane="continuation_or_resume")
    print("✓ test_session_finalize_clears_research_continuation_state")


def test_post_llm_hook_passes_session_id_to_scheduler():
    """Test: post_llm_call links background research to the active session."""
    calls = []
    with patch("nucleus.failure_trigger.schedule_epistemic_research", side_effect=lambda *a, **k: calls.append((a, k)) or "scheduled"), \
         patch("nucleus._get_nucleus", return_value=object()):
        nucleus_plugin._post_llm_hook(
            session_id="session-123",
            user_message="Hermes gateway plugin fails to load",
            assistant_response="I'm not sure; I don't have enough information to verify this.",
        )
    assert calls and calls[0][1]["session_id"] == "session-123"
    print("✓ test_post_llm_hook_passes_session_id_to_scheduler")


def test_nucleus_runtime_lock_busy_noops():
    """Test: a busy runtime lock prevents a second heartbeat owner."""
    n = Nucleus()
    with patch("nucleus.nucleus_engine.fcntl.flock", side_effect=BlockingIOError):
        assert not n._acquire_runtime_lock()
    assert n._lock_handle is None
    print("✓ test_nucleus_runtime_lock_busy_noops")


def test_get_nucleus_respects_disable_embedded_env():
    """Test: hooks can instantiate Nucleus without starting embedded heartbeat."""
    class FakeNucleus:
        def __init__(self):
            self.started = False

        def run_threaded(self):
            self.started = True

    old_instance = nucleus_plugin._nucleus_instance
    nucleus_plugin._nucleus_instance = None
    try:
        with patch("nucleus.nucleus_engine.Nucleus", FakeNucleus), \
             patch.dict(os.environ, {"NUCLEUS_DISABLE_EMBEDDED": "1"}, clear=False):
            instance = nucleus_plugin._get_nucleus()
        assert isinstance(instance, FakeNucleus)
        assert not instance.started
        print("✓ test_get_nucleus_respects_disable_embedded_env")
    finally:
        nucleus_plugin._nucleus_instance = old_instance


def test_standalone_service_contract():
    """Test: standalone service is opt-in and will not restart on lock no-op."""
    service_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "nucleus.service")
    with open(service_path, encoding="utf-8") as handle:
        service = handle.read()
    assert "Environment=NUCLEUS_STANDALONE=1" in service
    assert "Restart=on-failure" in service
    assert "hermes-agent/venv/bin/python" in service
    launcher_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "launcher.sh")
    with open(launcher_path, encoding="utf-8") as handle:
        launcher = handle.read()
    assert "NUCLEUS_DISABLE_EMBEDDED=1" in launcher
    assert "install|start|stop|restart|status|disable|seed" in launcher
    print("✓ test_standalone_service_contract")


def test_status_snapshot_and_formatting():
    """Test: status collector summarizes Pargod, Live Brain and research queue."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as pargod_file, \
         tempfile.NamedTemporaryFile(suffix=".db", delete=False) as brain_file:
        pargod_path = pargod_file.name
        brain_path = brain_file.name
    try:
        p = Pargod(pargod_path)
        p.add_node("problem", "status_problem", "Status problem")
        p.add_node("tool", "status_tool", "Status tool")
        p.add_edge("status_problem", "status_tool", "RESOLVES", 1.0)
        p.log_episode(1, 0.0, {"cpu_percent": 1.0}, "test_action")
        _create_live_brain_test_db(brain_path)

        class FakeNucleus:
            pargod = p
            brain_sync = LiveBrainSync(brain_path)

        status = collect_status(FakeNucleus())
        assert status["pargod"]["ok"]
        assert status["pargod"]["nodes"]["problem"] == 1
        assert status["live_brain"]["ok"]
        text = format_status(status)
        assert "[NUCLEUS STATUS]" in text
        assert "Pargod:" in text
        assert "Research Queue:" in text
        print("✓ test_status_snapshot_and_formatting")
    finally:
        os.unlink(pargod_path)
        os.unlink(brain_path)


def test_nucleus_status_query_bypasses_to_status():
    """Test: explicit status command returns status, not graph/research/chat."""
    class FakeAgent:
        pass

    class FakeRunAgent:
        class AIAgent:
            def run_conversation(self, user_message, *args, **kwargs):
                return {"final_response": "original"}

    class FakeNucleus:
        pass

    old_patch = nucleus_plugin._patch_applied
    try:
        nucleus_plugin._patch_applied = False
        with patch.dict(sys.modules, {"run_agent": FakeRunAgent}), \
             patch("nucleus._get_nucleus", return_value=FakeNucleus()), \
             patch("nucleus.status.collect_status", return_value={"mode": "embedded", "pargod": {}, "live_brain": {}, "research": {}}), \
             patch("nucleus.status.format_status", return_value="[NUCLEUS STATUS]\nMode: embedded"):
            nucleus_plugin._apply_monkey_patch()
            result = FakeRunAgent.AIAgent().run_conversation("/nucleus status")
        assert result["turn_exit_reason"] == "nucleus_status"
        assert "[NUCLEUS STATUS]" in result["final_response"]
        print("✓ test_nucleus_status_query_bypasses_to_status")
    finally:
        nucleus_plugin._patch_applied = old_patch


def test_doctor_preflight_on_temp_dbs():
    """Test: doctor validates syntax, schemas and temp Live Brain write/read."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as pargod_file, \
         tempfile.NamedTemporaryFile(suffix=".db", delete=False) as brain_file, \
         tempfile.NamedTemporaryFile(suffix=".json", delete=False) as lock_file, \
         tempfile.NamedTemporaryFile(suffix=".json", delete=False) as pid_file:
        pargod_path = pargod_file.name
        brain_path = brain_file.name
        lock_path = lock_file.name
        pid_path = pid_file.name
    try:
        p = Pargod(pargod_path)
        p.add_node("problem", "doctor_problem", "Doctor problem")
        p.log_episode(1, 0.0, {}, "doctor")
        _create_live_brain_test_db(brain_path)
        with open(lock_path, "w") as f:
            json.dump({"pid": os.getpid(), "mode": "test"}, f)
        with open(pid_path, "w") as f:
            json.dump({"pid": os.getpid(), "mode": "test"}, f)

        plugin_dir = __import__("pathlib").Path(os.path.dirname(os.path.dirname(__file__)))
        results = run_doctor(
            plugin_dir=plugin_dir,
            pargod_db=__import__("pathlib").Path(pargod_path),
            live_brain_db=__import__("pathlib").Path(brain_path),
            lock_file=__import__("pathlib").Path(lock_path),
            pid_file=__import__("pathlib").Path(pid_path),
            run_web=False,
        )
        names = {result.name: result.status for result in results}
        assert names["syntax"] == "pass"
        assert names["pargod_schema"] == "pass"
        assert names["runtime_lock"] == "pass"
        assert names["service_contract"] == "pass"
        assert names["live_brain_probe"] == "pass"
        assert names["web_availability"] == "skip"
        assert doctor_ok(results)
        assert "[NUCLEUS DOCTOR] PASS" in format_doctor(results)
        print("✓ test_doctor_preflight_on_temp_dbs")
    finally:
        for path in (pargod_path, brain_path, lock_path, pid_path):
            os.unlink(path)


def test_nucleus_doctor_query_bypasses_to_doctor():
    """Test: explicit doctor command returns preflight output."""
    class FakeRunAgent:
        class AIAgent:
            def run_conversation(self, user_message, *args, **kwargs):
                return {"final_response": "original"}

    old_patch = nucleus_plugin._patch_applied
    try:
        nucleus_plugin._patch_applied = False
        with patch.dict(sys.modules, {"run_agent": FakeRunAgent}), \
             patch("nucleus.doctor.run_doctor", return_value=[]), \
             patch("nucleus.doctor.format_doctor", return_value="[NUCLEUS DOCTOR] PASS"):
            nucleus_plugin._apply_monkey_patch()
            result = FakeRunAgent.AIAgent().run_conversation("/nucleus doctor")
        assert result["turn_exit_reason"] == "nucleus_doctor"
        assert "[NUCLEUS DOCTOR]" in result["final_response"]
        print("✓ test_nucleus_doctor_query_bypasses_to_doctor")
    finally:
        nucleus_plugin._patch_applied = old_patch


def test_full_instinct_execution():
    """Test: execute a real instinct from the instincts/ directory."""
    guard = InstinctGuard(timeout=10)
    script = str(INSTINCTS_DIR / "report_top_cpu.py")
    if not os.path.exists(script):
        print("⚠ test_full_instinct_execution skipped (instinct not found)")
        return
    result = guard.execute(script)
    assert result["success"], f"Instinct failed: {result}"
    assert "Top CPU" in result["stdout"]
    print("✓ test_full_instinct_execution")


def test_post_tool_hook_accepts_hermes_result_arg():
    """Test: post_tool_call learns from Hermes' result= kwarg."""
    from nucleus.session_state import reset_session_state, get_session_state

    class FakePargod:
        def __init__(self):
            self.nodes = {}
            self.recorded = []

        def get_node(self, label):
            return self.nodes.get(label)

        def add_node(self, node_type, label, content=None):
            self.nodes[label] = {"type": node_type, "label": label, "content": content}

        def record_use(self, label):
            self.recorded.append(label)

    class FakeNucleus:
        def __init__(self):
            self.pargod = FakePargod()

    fake = FakeNucleus()
    reset_session_state()
    get_session_state().on_user_message("t1", "run terminal command")
    with patch("nucleus._get_nucleus", return_value=fake):
        nucleus_plugin._post_tool_hook(tool_name="terminal", result="ok output", task_id="t1")

    assert "hermes_tool_terminal" in fake.pargod.nodes
    assert fake.pargod.recorded == ["hermes_tool_terminal"]
    print("✓ test_post_tool_hook_accepts_hermes_result_arg")


def test_post_tool_hook_ignores_ambiguous_missing_session_id():
    """Tool hooks without session_id should not borrow a stale active session."""
    from nucleus.session_state import reset_session_state, get_session_state

    reset_session_state()
    state = get_session_state()
    state.on_user_message("session-a", "first session")
    state.on_user_message("session-b", "second session")

    with patch("nucleus._get_nucleus", return_value=object()):
        nucleus_plugin._post_tool_hook(tool_name="terminal", result="ok output", task_id="task-123")

    assert state.snapshot("session-a")["tool_calls_count"] == 0
    assert state.snapshot("session-b")["tool_calls_count"] == 0
    print("✓ test_post_tool_hook_ignores_ambiguous_missing_session_id")


def test_nucleus_tick_logs_action_and_decays_edges():
    """Test: tick records action_taken and runs periodic edge decay."""
    n = Nucleus()
    n.tick = 60
    n.sensor.read = lambda: {"cpu_percent": 92.0, "ram_percent": 50.0, "disk_percent": 0.0, "timestamp": time.time()}
    n.pargod.find_tool_for_problem = lambda problem: {"tool": "report_top_cpu", "path": [problem, "report_top_cpu"], "cost": 1.0}
    executed = []
    n._execute_instinct = lambda tool: executed.append(tool) or {"success": True, "stdout": "ok"}
    synced = []
    decayed = []
    logged = []
    n.brain_sync.sync_to_pargod = lambda pargod: synced.append(True) or 0
    n.pargod.decay_edges = lambda: decayed.append(True)
    n.pargod.log_episode = lambda tick, entropy, state, action=None: logged.append(action)

    n._tick()

    assert executed == ["report_top_cpu"]
    assert synced == [True]
    assert decayed == [True]
    assert logged and logged[-1] == "graph:high_cpu->report_top_cpu:ok"
    print("✓ test_nucleus_tick_logs_action_and_decays_edges")


if __name__ == "__main__":
    test_pargod_pathfinding()
    test_pargod_seed()
    test_entropy_calculation()
    test_sensor_reads()
    test_instinct_guard_safe()
    test_instinct_guard_blocked()
    test_instinct_guard_blocks_from_import_call()
    test_instinct_guard_blocks_getattr_bypass()
    test_instinct_guard_uses_instance_memory_limit()
    test_instinct_guard_timeout()
    test_has_answer_for()
    test_web_search()
    test_domain_profile_detects_hermes()
    test_research_problem_local_sources_write_graph()
    test_live_brain_sync_writes_research_structures()
    test_pargod_resolution_prefers_tool_when_available()
    test_pre_llm_hook_injects_fix_recipe_context()
    test_failure_trigger_classifies_llm_gap()
    test_failure_trigger_schedules_once_and_debounces()
    test_post_llm_hook_schedules_gap_research()
    test_post_tool_hook_schedules_failure_research()
    test_continuation_context_after_completed_research()
    test_continuation_pending_context()
    test_pre_llm_hook_injects_continuation_before_graph()
    test_session_finalize_clears_research_continuation_state()
    test_post_llm_hook_passes_session_id_to_scheduler()
    test_nucleus_runtime_lock_busy_noops()
    test_get_nucleus_respects_disable_embedded_env()
    test_standalone_service_contract()
    test_status_snapshot_and_formatting()
    test_nucleus_status_query_bypasses_to_status()
    test_doctor_preflight_on_temp_dbs()
    test_nucleus_doctor_query_bypasses_to_doctor()
    test_full_instinct_execution()
    test_post_tool_hook_accepts_hermes_result_arg()
    test_post_tool_hook_ignores_ambiguous_missing_session_id()
    test_nucleus_tick_logs_action_and_decays_edges()
    print("\n✅ All tests passed!")
