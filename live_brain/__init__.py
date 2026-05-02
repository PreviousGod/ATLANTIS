from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

from .briefing import CompressionManager
from .causal import CausalManager
from .ingest import Ingestor
from .research import ResearchManager
from .epistemic import EpistemicManager
from .retrieval import RetrievalRouter
from .rules import RuleEngine
from .store import LiveBrainStore
from .artifacts import ArtifactRegistry

logger = logging.getLogger(__name__)


STATE_DEBUG_SCHEMA = {
    "name": "brain_state_debug",
    "description": "Inspect the live brain state for the current scope.",
    "parameters": {
        "type": "object",
        "properties": {
            "scope_key": {"type": "string", "description": "Optional specific scope key to inspect."}
        },
        "required": []
    }
}

REALITY_DEBUG_SCHEMA = {
    "name": "brain_reality_debug",
    "description": "Inspect Live Brain Reality Engine state: current objective, open loops, danger zones, action constraints, and why a short query like 'a link?' should resolve from active situational awareness instead of semantic search.",
    "parameters": {
        "type": "object",
        "properties": {
            "scope_key": {"type": "string", "description": "Optional specific scope key to inspect."},
            "query": {"type": "string", "description": "Optional query to explain against the current reality state."},
            "action_type": {"type": "string", "description": "Optional proposed action type for action-gate evaluation, e.g. media_send, code_patch, network_exposure."},
            "action_payload": {"type": "object", "description": "Optional action payload, e.g. path or synthetic_public."}
        },
        "required": []
    }
}

RECAP_SCHEMA = {
    "name": "brain_recap",
    "description": "Summarize recent important work directly from live brain episodes, especially for prompts like 'sumarizuj sta si radio' or recap requests. Prefer this over session_search when the request is about recent work continuity.",
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "How many recent work items to include (default 3)."}
        },
        "required": []
    }
}

BELIEF_MARK_SCHEMA = {
    "name": "brain_mark_belief",
    "description": "Create or update a causal belief as hypothesis, validated, falsified, or ruled_out with optional evidence.",
    "parameters": {
        "type": "object",
        "properties": {
            "belief_id": {"type": "string", "description": "Optional existing belief id."},
            "claim_text": {"type": "string", "description": "Belief or causal claim text."},
            "action": {"type": "string", "enum": ["hypothesis", "validated", "falsified", "ruled_out"]},
            "evidence_text": {"type": "string", "description": "Optional evidence backing the action."}
        },
        "required": ["claim_text", "action"]
    }
}

BELIEF_RECALL_SCHEMA = {
    "name": "brain_recall",
    "description": "Recall facts and beliefs relevant to a query from the live brain store.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to recall."}
        },
        "required": ["query"]
    }
}

RESEARCH_SCHEMA = {
    "name": "brain_research",
    "description": "Plan or record bounded research when uncertainty remains high. Scope can be auto/local/docs/web.",
    "parameters": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "Research question."},
            "scope": {"type": "string", "enum": ["auto", "local", "docs", "web"]},
            "research_id": {"type": "string", "description": "Optional existing research job id when recording a result."},
            "source_kind": {"type": "string", "description": "Optional result source kind."},
            "source_ref": {"type": "string", "description": "Optional result source reference."},
            "summary": {"type": "string", "description": "Optional result summary to record."},
            "confidence": {"type": "number", "description": "Optional confidence for the recorded result (0-1)."},
            "actionability": {"type": "number", "description": "Optional actionability score for the recorded result (0-1)."},
            "raw_excerpt": {"type": "string", "description": "Optional raw evidence excerpt backing the result."}
        },
        "required": ["question"]
    }
}



EPISTEMIC_SCHEMA = {
    "name": "brain_epistemic",
    "description": "Autonomous learning layer. Use when the user asks something current, high-stakes, externally verifiable, or unknown. First call action=status/plan, then use web_search/web_extract or action=search_web if research is required. Use only authoritative_sources for answers. For numeric/current/high-stakes claims, extract/read the official page and include raw_excerpt when calling action=record_fact. If extraction is unavailable, answer with safe_answer and stop; never use secondary snippets or record facts from search-result titles only.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["status", "search_web", "record_source", "record_fact"]},
            "question": {"type": "string", "description": "Question or knowledge gap."},
            "job_id": {"type": "string", "description": "Research job id from EPISTEMIC STATUS or status action."},
            "url": {"type": "string", "description": "Source URL when recording one source."},
            "title": {"type": "string"},
            "summary": {"type": "string"},
            "raw_excerpt": {"type": "string"},
            "fact_text": {"type": "string", "description": "Evidence-backed learned fact to store."},
            "source_urls": {"type": "array", "items": {"type": "string"}},
            "source_ids": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number"},
            "ttl_seconds": {"type": "integer", "description": "Expiry for time-sensitive learned facts."}
        },
        "required": ["action"]
    }
}

ARTIFACT_RESOLVE_SCHEMA = {
    "name": "brain_resolve_artifact",
    "description": "Resolve a verified project artifact path by project and role before sending or editing files.",
    "parameters": {
        "type": "object",
        "properties": {
            "project_key": {"type": "string", "description": "Project key, e.g. enoch."},
            "role": {"type": "string", "description": "Artifact role, e.g. part_1, part_2, combined_or_full."}
        },
        "required": ["project_key", "role"]
    }
}

ARTIFACT_MARK_SCHEMA = {
    "name": "brain_mark_artifact",
    "description": "Create or update a verified artifact, or mark a wrong/old artifact as deprecated/rejected.",
    "parameters": {
        "type": "object",
        "properties": {
            "project_key": {"type": "string"},
            "role": {"type": "string"},
            "path": {"type": "string"},
            "label": {"type": "string"},
            "status": {"type": "string", "enum": ["verified", "candidate", "deprecated", "rejected", "missing"]},
            "source": {"type": "string"},
            "reason": {"type": "string"}
        },
        "required": ["project_key", "role", "path"]
    }
}

ARTIFACT_LIST_SCHEMA = {
    "name": "brain_list_artifacts",
    "description": "List verified project artifacts (video files, images, outputs) tracked by live brain. Use this FIRST when the user asks about project files, videos, or outputs — before using search_files or session_search.",
    "parameters": {
        "type": "object",
        "properties": {
            "project_key": {"type": "string", "description": "Project key, e.g. 'enoch'"},
            "include_inactive": {"type": "boolean"}
        },
        "required": ["project_key"]
    }
}

SELF_EVOLUTION_SCHEMA = {
    "name": "brain_self_evolution",
    "description": "Create, list, approve, or reject gated Live Brain self-evolution proposals. Use this FIRST for any pending approvals, approval queue, odobrenja, self-evolution approvals, or approve/reject latest request; do not use session_search, cronjob, or brain_state_debug for approval queue answers. Code/config/schema/file/media changes require approval; only bounded low-risk metadata cleanup may auto-apply.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "propose", "decide"]},
            "status": {"type": "string", "description": "Optional proposal status filter for list."},
            "include_applied": {"type": "boolean"},
            "limit": {"type": "integer"},
            "proposal_id": {"type": "string"},
            "decision": {"type": "string", "enum": ["approved", "rejected"], "description": "If proposal_id is omitted, decides the highest-risk/latest pending proposal."},
            "reason": {"type": "string"},
            "proposal_type": {"type": "string", "description": "Examples: code_patch, config_change, demote_fix_recipe, schema_migration."},
            "target_area": {"type": "string", "description": "Examples: code, config, db_schema, recipe, context, artifact_metadata."},
            "rationale": {"type": "string"},
            "proposed_action": {"type": "string"},
            "evidence": {"type": "object"},
            "suggested_tests": {"type": "array", "items": {"type": "string"}},
            "auto_apply": {"type": "boolean"}
        },
        "required": ["action"]
    }
}


class LiveBrainProvider(MemoryProvider):
    def __init__(self):
        self._store = None
        self._ingestor = None
        self._router = None
        self._causal = None
        self._compression = None
        self._research = None
        self._epistemic = None
        self._artifacts = None
        self._rules = None
        self._session_id = ""
        self._platform = "cli"
        self._agent_identity = ""
        self._agent_context = "primary"
        self._user_id = ""
        self._gateway_session_key = ""
        self._scope_key = ""
        self._turn_count = 0
        self._hermes_home = ""

    @property
    def name(self) -> str:
        return "live_brain"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        hermes_home = kwargs.get("hermes_home") or os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
        self._hermes_home = hermes_home
        db_path = str(Path(hermes_home) / "live_brain" / "live_brain.db")
        self._session_id = session_id
        self._platform = kwargs.get("platform", "cli")
        self._agent_identity = kwargs.get("agent_identity", "")
        self._agent_context = kwargs.get("agent_context", "primary")
        self._user_id = kwargs.get("user_id", "")
        self._gateway_session_key = kwargs.get("gateway_session_key", "")
        self._scope_key = self._gateway_session_key or self._user_id or self._session_id

        self._store = LiveBrainStore(db_path)
        self._store.initialize_schema()
        try:
            interval = float(os.environ.get("LIVE_BRAIN_INIT_MAINTENANCE_INTERVAL_SECONDS", "21600") or "21600")
        except ValueError:
            interval = 21600.0
        try:
            maintenance = self._store.run_init_maintenance(
                scope_key=self._scope_key,
                hermes_home=hermes_home,
                min_interval_seconds=interval,
            )
            logger.info("[live_brain] init maintenance %s", json.dumps(maintenance, ensure_ascii=False, sort_keys=True)[:1200])
        except Exception:
            logger.exception("[live_brain] init maintenance failed")

        self._ingestor = Ingestor(self._store.conn)
        self._router = RetrievalRouter(self._store.conn, hermes_home=self._hermes_home)
        self._causal = CausalManager(self._store.conn, store=self._store)
        self._compression = CompressionManager(self._store.conn)
        self._rules = RuleEngine(self._store.conn)
        self._research = ResearchManager(self._store.conn, ingestor=self._ingestor, causal=self._causal, session_id=self._session_id, scope_key=self._scope_key)
        self._epistemic = EpistemicManager(self._store.conn, ingestor=self._ingestor, session_id=self._session_id, scope_key=self._scope_key)
        self._artifacts = ArtifactRegistry(self._store.conn)

        self._store.conn.execute(
            "INSERT OR REPLACE INTO sessions (session_id, platform, agent_identity, agent_context, user_id, gateway_session_key, started_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                self._session_id,
                self._platform,
                self._agent_identity,
                self._agent_context,
                self._user_id,
                self._gateway_session_key,
                time.time(),
            ),
        )
        self._store.conn.commit()
        logger.info("[live_brain] initialized db=%s session=%s", db_path, self._session_id)

    def system_prompt_block(self) -> str:
        return (
            "# Live Brain\n"
            "Active. Recalled state distinguishes VALIDATED FACTS, OPEN HYPOTHESES, NEXT BEST ACTIONS, and LIVE REALITY situational awareness. "
            "Use LIVE REALITY to resolve short references like 'a link?' or 'uradi to' from current objective/open loops before asking generic clarification. "
            "Do not treat hypotheses as validated causes without evidence. "
            "Never infer hidden codenames, secrets, or remembered values from run IDs, suffixes, hashes, filenames, or the current prompt; answer UNKNOWN unless Live Brain context explicitly contains the value. "
            "Approval queue routing is deterministic: if the user asks for pending approvals/approvals/approval queue/odobrenja, or asks to approve/reject latest, call brain_self_evolution(action='list', status='needs_approval') before answering; never use session_search, cronjob, or brain_state_debug for approval queue answers. "
            "Before changing Live Brain code, config, DB schema, files, credentials, or media behavior, create a brain_self_evolution proposal and ask for approval; do not auto-apply high-risk changes. "
            "Epistemic autonomy is active: when EPISTEMIC STATUS says research is required, do not answer from memory; use web_search/web_extract or brain_epistemic(action='search_web'), use only authoritative_sources, and for numeric/current/high-stakes claims extract/read official pages before recording facts. If extraction fails or browser/web_extract is unavailable, use brain_epistemic safe_answer, cite official URLs, and say exact values require the official page/bulletin; do not invent numbers, use secondary snippets, session_search, search_files, or record facts from search-result titles only."
        )

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        self._turn_count = turn_number

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if os.environ.get("LIVE_BRAIN_PROVIDER_PREFETCH", "0") != "1":
            return ""
        if not self._router:
            return ""
        briefing = self._router.build_briefing(self._scope_key, query)
        return briefing

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        return

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        if not self._store or not self._ingestor:
            return

        def _sync() -> None:
            try:
                created_at = time.time()
                self._ingestor.ingest_turn(
                    session_id=self._session_id,
                    scope_key=self._scope_key,
                    turn_index=self._turn_count,
                    user_text=user_content,
                    assistant_text=assistant_content,
                    created_at=created_at,
                )
                # Derive scoped rules from repeated or explicit corrections.
                if self._rules:
                    self._rules.derive_binding_constraint_from_turn(user_content, self._session_id, self._scope_key)
                    self._rules.derive_correction_constraint_from_turn(user_content, self._session_id, self._scope_key)
                # Keep canonical recap updated incrementally, not only at session end.
                if self._compression:
                    row = self._store.conn.execute(
                        "SELECT state_json FROM work_state WHERE scope_key = ?",
                        (self._scope_key,),
                    ).fetchone()
                    state = json.loads(row[0]) if row and row[0] else {}
                    self._compression.update_canonical_recap(self._session_id, self._scope_key, state, created_at)
                    self._compression.crystallise_from_work_item(self._scope_key, created_at)
                self._store.conn.commit()
            except Exception:
                logger.exception("[live_brain] sync_turn failed")
                try:
                    self._store.conn.rollback()
                except Exception:
                    pass

        threading.Thread(target=_sync, daemon=True, name="live-brain-sync").start()

    def on_pre_compress(self, messages: list) -> str:
        if not self._compression:
            return ""
        return self._compression.preserve_from_messages(self._scope_key, messages)

    def on_session_end(self, messages: list) -> None:
        if not self._compression:
            return
        self._compression.finalize_session(self._session_id, self._scope_key, time.time())

    def on_memory_write(self, action: str, target: str, content: str) -> None:
        if not self._store or not self._ingestor or action not in ("add", "replace") or not content:
            return
        self._ingestor.mirror_memory_write(target=target, content=content, created_at=time.time(), session_id=self._session_id, scope_key=self._scope_key)

    def on_delegation(self, task: str, result: str, *, child_session_id: str) -> None:
        if not self._store or not self._ingestor:
            return
        self._ingestor.ingest_delegation(task=task, result=result, child_session_id=child_session_id, created_at=time.time(), session_id=self._session_id, scope_key=self._scope_key)

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            STATE_DEBUG_SCHEMA,
            REALITY_DEBUG_SCHEMA,
            RECAP_SCHEMA,
            BELIEF_MARK_SCHEMA,
            BELIEF_RECALL_SCHEMA,
            RESEARCH_SCHEMA,
            EPISTEMIC_SCHEMA,
            ARTIFACT_RESOLVE_SCHEMA,
            ARTIFACT_MARK_SCHEMA,
            ARTIFACT_LIST_SCHEMA,
            SELF_EVOLUTION_SCHEMA,
        ]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if not self._store:
            return tool_error("Live brain store is not initialized")
        if tool_name == "brain_state_debug":
            scope_key = args.get("scope_key") or self._scope_key
            row = self._store.conn.execute(
                "SELECT state_json, updated_at FROM work_state WHERE scope_key = ?",
                (scope_key,),
            ).fetchone()
            if not row:
                return json.dumps({"scope_key": scope_key, "state": None})
            return json.dumps({
                "scope_key": scope_key,
                "updated_at": row["updated_at"],
                "state": json.loads(row["state_json"]),
            })
        if tool_name == "brain_reality_debug":
            scope_key = args.get("scope_key") or self._scope_key
            query = args.get("query", "")
            result = self._store.debug_reality(scope_key, query)
            action_type = args.get("action_type") or ""
            if action_type:
                payload = args.get("action_payload") if isinstance(args.get("action_payload"), dict) else {}
                result["action_gate"] = self._store.action_gate(scope_key, action_type, payload)
            return json.dumps(result, ensure_ascii=False)
        if tool_name == "brain_recap":
            if not self._router:
                return tool_error("Retrieval router is not initialized")
            limit = int(args.get('limit', 3))
            result = self._router.recap_recent_work(limit=limit)
            return json.dumps({"result": result})
        if tool_name == "brain_mark_belief":
            if not self._causal:
                return tool_error("Causal manager is not initialized")
            result = self._causal.mark_belief(
                belief_id=args.get("belief_id"),
                claim_text=args.get("claim_text", ""),
                action=args.get("action", "hypothesis"),
                evidence_text=args.get("evidence_text"),
                session_id=self._session_id,
                scope_key=self._scope_key,
            )
            return json.dumps(result)
        if tool_name == "brain_recall":
            if not self._router:
                return tool_error("Retrieval router is not initialized")
            q = args.get("query", "")
            return json.dumps({"result": self._router.build_briefing(self._scope_key, q)})
        if tool_name == "brain_resolve_artifact":
            if not self._artifacts:
                return tool_error("Artifact registry is not initialized")
            return json.dumps(self._artifacts.resolve(args.get("project_key", ""), args.get("role", "")), ensure_ascii=False)
        if tool_name == "brain_mark_artifact":
            if not self._artifacts:
                return tool_error("Artifact registry is not initialized")
            status = args.get("status", "verified")
            if status in ("deprecated", "rejected", "missing") and not args.get("label"):
                result = self._artifacts.mark_status(path=args.get("path", ""), status=status, reason=args.get("reason", ""))
            else:
                result = self._artifacts.upsert_artifact(
                    project_key=args.get("project_key", ""),
                    role=args.get("role", ""),
                    path=args.get("path", ""),
                    label=args.get("label", ""),
                    status=status,
                    source=args.get("source", "tool"),
                    evidence={"reason": args.get("reason", "")},
                )
            self._store.conn.commit()
            return json.dumps(result, ensure_ascii=False)
        if tool_name == "brain_list_artifacts":
            if not self._artifacts:
                return tool_error("Artifact registry is not initialized")
            return json.dumps({"artifacts": self._artifacts.list_project(args.get("project_key", ""), include_inactive=bool(args.get("include_inactive", False)))}, ensure_ascii=False)
        if tool_name == "brain_self_evolution":
            action = args.get("action", "list")
            if action == "list":
                result = self._store.list_self_evolution_proposals(
                    status=args.get("status", ""),
                    include_applied=bool(args.get("include_applied", False)),
                    limit=int(args.get("limit", 10)),
                )
                return json.dumps({"proposals": result}, ensure_ascii=False)
            if action == "decide":
                result = self._store.decide_self_evolution_proposal(
                    proposal_id=args.get("proposal_id", ""),
                    decision=args.get("decision", ""),
                    reason=args.get("reason", ""),
                )
                return json.dumps(result, ensure_ascii=False)
            if action == "propose":
                result = self._store.propose_self_evolution(
                    scope_key=self._scope_key,
                    session_id=self._session_id,
                    trigger_text=args.get("reason", "") or args.get("rationale", "") or args.get("proposed_action", ""),
                    proposal_type=args.get("proposal_type", "code_patch"),
                    target_area=args.get("target_area", "code"),
                    rationale=args.get("rationale", ""),
                    proposed_action=args.get("proposed_action", ""),
                    evidence=args.get("evidence", {}) if isinstance(args.get("evidence"), dict) else {},
                    suggested_tests=args.get("suggested_tests", []) if isinstance(args.get("suggested_tests"), list) else [],
                    auto_apply=bool(args.get("auto_apply", False)),
                )
                return json.dumps(result, ensure_ascii=False)
            return tool_error("Unknown brain_self_evolution action")
        if tool_name == "brain_epistemic":
            if not self._epistemic:
                return tool_error("Epistemic manager is not initialized")
            action = args.get("action", "status")
            question = args.get("question", "")
            if action == "status":
                return json.dumps(self._epistemic.debug(self._scope_key, question), ensure_ascii=False)
            if action == "search_web":
                result = self._epistemic.search_web(
                    scope_key=self._scope_key,
                    question=question,
                    job_id=args.get("job_id", ""),
                )
                return json.dumps(result, ensure_ascii=False)
            if action == "record_source":
                result = self._epistemic.record_source(
                    scope_key=self._scope_key,
                    job_id=args.get("job_id", ""),
                    url=args.get("url", ""),
                    title=args.get("title", ""),
                    summary=args.get("summary", ""),
                    raw_excerpt=args.get("raw_excerpt", ""),
                    confidence=float(args.get("confidence", 0.6)),
                )
                return json.dumps(result, ensure_ascii=False)
            if action == "record_fact":
                result = self._epistemic.record_fact(
                    scope_key=self._scope_key,
                    question=question,
                    job_id=args.get("job_id", ""),
                    fact_text=args.get("fact_text", "") or args.get("summary", ""),
                    source_urls=args.get("source_urls", []) if isinstance(args.get("source_urls"), list) else [],
                    source_ids=args.get("source_ids", []) if isinstance(args.get("source_ids"), list) else [],
                    confidence=float(args.get("confidence", 0.75)),
                    ttl_seconds=args.get("ttl_seconds") if isinstance(args.get("ttl_seconds"), int) else None,
                    raw_excerpt=args.get("raw_excerpt", ""),
                )
                return json.dumps(result, ensure_ascii=False)
            return tool_error("Unknown brain_epistemic action")
        if tool_name == "brain_research":
            if not self._research:
                return tool_error("Research manager is not initialized")
            question = args.get("question", "")
            research_id = args.get("research_id")
            summary = args.get("summary")
            if research_id and summary:
                result = self._research.record_result(
                    research_id=research_id,
                    source_kind=args.get("source_kind", "manual"),
                    source_ref=args.get("source_ref", "manual"),
                    summary=summary,
                    confidence=float(args.get("confidence", 0.6)),
                    actionability=float(args.get("actionability", 0.6)),
                    raw_excerpt=args.get("raw_excerpt", ""),
                )
                return json.dumps(result)
            return json.dumps(self._research.plan_research(question, args.get("scope", "auto")))
        return tool_error(f"Unknown tool: {tool_name}")

    def shutdown(self) -> None:
        if self._store:
            self._store.close()
            self._store = None
            self._ingestor = None
            self._artifacts = None
            self._epistemic = None


def register(ctx) -> None:
    register_memory_provider = getattr(ctx, 'register_memory_provider', None)
    if callable(register_memory_provider):
        register_memory_provider(LiveBrainProvider())
