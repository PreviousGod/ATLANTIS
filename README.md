# ATLANTIS Memory System

**Production-grade memory plugin for long-running AI agents.**

ATLANTIS is a complete operational state management system that gives AI agents true situational awareness, causal reasoning, and the ability to maintain operational truth across sessions. It is deterministic (no embeddings, no vectors for retrieval), auditable, and self-evolving.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│ Hermes Gateway                                                   │
├──────────────────────────┬──────────────────────────────────────┤
│ live_brain (provider)    │ live_brain_ctx (context engine)       │
│ 15 brain_* tools         │ 4 hooks: pre/post LLM + tool         │
│ MemoryProvider interface │ Context injection + recording         │
├──────────────────────────┴──────────────────────────────────────┤
│ SQLite (WAL) — ~/.hermes/live_brain/live_brain.db               │
│ 8 migrations │ 60+ tables │ FTS5 search │ Audit trail           │
└─────────────────────────────────────────────────────────────────┘
```

## Core Capabilities

| Layer | What it does |
|---|---|
| **Cognitive Architecture** | Tiered reasoning (decompose → verify → synthesize → adversarial attack → final). Anti-downgrade with constraint propagation. Cross-domain synthesis. |
| **Reality Engine** | Maintains current operational state: objectives, open loops, blockers, danger zones, action constraints |
| **Epistemic Autonomy** | Current/high-stakes questions require fresh authoritative sources; blocks stale session_search |
| **Causal Reasoning** | Beliefs have lifecycle (hypothesis → validated/falsified/ruled_out); cascading invalidation |
| **Self-Evolution** | Gated proposals for code/config/schema changes; approval queue with risk scoring |
| **Verified Artifacts** | Project files selected by verified role/status, not filename similarity |
| **Entity Graph** | Connected knowledge with relationship traversal and cross-entity synthesis |
| **Dialectic Reasoning** | Cross-session synthesis tracking belief evolution and contradictions |
| **Context Fencing** | Prevents memory pollution from self-referential operations and noise |
| **User Alignment** | Automatically tracks preferences, communication patterns, feedback |
| **Deterministic Retrieval** | Keyword/rule-based scoring with domain conflict detection — no embeddings |

## Plugin Structure

### `live_brain/` — Memory Provider (39 files, ~4500 LOC)

```
live_brain/
├── __init__.py           (866 lines — provider + 15 handler methods)
├── store.py              (1325 — LiveBrainStore + LockedConnection)
├── schema_manager.py     (graceful migration runner with FAILED tracking)
├── connection_pool.py    (thread-safe pool with lifecycle tracking)
├── ingest.py             (turn ingestion + entity/fact/belief extraction)
├── retrieval.py          (token-budgeted briefing builder)
├── reality.py            (reality engine — events, state, open loops)
├── epistemic.py          (autonomous research layer)
├── causal.py             (belief marking + cascading invalidation)
├── evolution.py          (self-evolution proposals + risk scoring)
├── entity_graph.py       (relationship graph traversal)
├── dialectic.py          (cross-session synthesis)
├── user_alignment.py     (preference/pattern tracking)
├── artifacts.py          (verified artifact registry)
├── rules.py              (binding constraint engine)
├── briefing.py           (compression + canonical recaps)
├── research.py           (bounded research planning)
├── hermes_adapter.py     (standalone testing interface)
├── backup_manager.py     (online SQLite backup)
├── maintenance_manager.py (scheduled hygiene)
├── migrations/           (000-006 SQL migrations)
├── tests/                (7 test files, 47 assertions)
└── requirements.txt      (ddgs, tiktoken)
```

### `live_brain_ctx/` — Context Engine (18 files, ~3200 LOC)

```
live_brain_ctx/
├── __init__.py           (235 lines — thin facade + register)
├── modules/
│   ├── cognitive_architecture.py (tiered reasoning + ruled_out + cross-domain)
│   ├── state.py          (constants + regex patterns)
│   ├── hooks.py          (4 hook functions + orchestration)
│   ├── scoring.py        (overlap/domain/marker scoring)
│   ├── formatting.py     (section formatting)
│   ├── integrations.py   (reality + epistemic engine bridges)
│   ├── data_sources.py   (DB fetch + maintenance)
│   ├── query_filters.py  (classification + filtering)
│   ├── approval.py       (pending approval management)
│   ├── tool_context.py   (tool hints + recipe formatting)
│   ├── tag_matching.py   (scope tag matching)
│   ├── text_processing.py (redaction + noise detection)
│   └── query_classification.py
├── tests/                (3 test files, 27 assertions)
└── context_config.json
```

## Tools Registered (15)

| Tool | Purpose |
|---|---|
| `brain_state_debug` | Inspect work_state |
| `brain_reality_debug` | Reality engine state + action gate |
| `brain_recap` | Summarize recent work |
| `brain_mark_belief` | Create/update causal beliefs |
| `brain_recall` | Natural language query |
| `brain_research` | Bounded research planning |
| `brain_epistemic` | Autonomous web research |
| `brain_resolve_artifact` | Resolve verified artifact path |
| `brain_mark_artifact` | Register/deprecate artifacts |
| `brain_list_artifacts` | List project artifacts |
| `brain_self_evolution` | Propose/list/decide evolution proposals |
| `brain_entity_graph` | Traverse entity relationships |
| `brain_synthesize` | Cross-session dialectic synthesis |
| `brain_user_profile` | View/update user preferences |
| `brain_compose_query` | Algebraic queries (A + B − C) |

## Installation

```bash
git clone https://github.com/PreviousGod/ATLANTIS.git
cd ATLANTIS
python install.py
```

The installer will:
1. Detect your Hermes installation (Linux/macOS/Windows)
2. Backup existing plugins
3. Install `live_brain` + `live_brain_ctx`
4. Ask how to configure (manual instructions or auto-patch config.yaml)
5. Verify imports

Use `python install.py --auto` to skip the interactive prompt.

## Testing

```bash
# Full preflight (compile + import + migration dry-run + tests)
bash scripts/plugins_preflight.sh

# Individual test suites
python tests/live_brain_smoke.py
python live_brain/tests/test_store_integration.py
python live_brain_ctx/tests/test_hook_dispatch.py
python live_brain_ctx/tests/test_scoring.py
python live_brain_ctx/tests/test_cross_platform.py
```

## Production Stats (2026-05-11)

- **74 automated tests**, all passing
- **8 schema migrations** applied (audit_spine_v1, 000-006)
- **481 sessions**, 1340 turns, 889 episodes, 641 facts, 236 beliefs
- **49 self-evolution proposals** (12 approved, 14 rejected, 22 expired, 1 pending)
- **Preflight guard** catches SyntaxError/import/migration failures in <2s

## Design Philosophy

1. **Deterministic over probabilistic** — keyword/rule scoring, not embeddings
2. **Auditable** — every mutation has before/after revision + audit_log entry
3. **Graceful degradation** — broken migration doesn't crash provider; missing JSON doesn't crash import
4. **Bounded concurrency** — ThreadPoolExecutor(max_workers=2) for sync, 1 for maintenance
5. **Platform-agnostic** — scope keys parameterized by platform (telegram/discord/slack/cli/web)

## Changelog

- **2026-05-11** — ATLANTIS Cognitive Architecture:
  - Tiered reasoning (Tier 1/2/3) with zero overhead for trivial queries
  - Multi-perspective decomposition (structural/causal/temporal/analogical)
  - Adversarial self-attack before delivering answers
  - Confidence gate (forces research when <2 verified facts)
  - Anti-downgrade: ruled_out state with constraint propagation across turns
  - Cross-domain synthesis via SQLite (0 extra LLM calls)
  - Cross-platform install script (`python install.py`)

- **2026-05-11** — Production-readiness pass + external review remediation:
  - Migration 006 FTS5 fix (reserved `rowid` column)
  - Graceful migration runner (FAILED tracking, no restart loops)
  - Full ctx modularization (2221 → 235 line facade + 12 modules)
  - Handler map dispatch (replaced 300-line if-elif)
  - ConnectionPool lifecycle fix (thread-local + close_all)
  - Background DB maintenance (non-blocking)
  - Batched self-evolution expiry (~66% fewer DB hits)
  - Cross-platform scope keys (telegram/discord/slack/cli/web)
  - Import caching for dynamic class loading
  - `ddgs` package migration (from deprecated `duckduckgo_search`)
  - 74 automated tests across 10 test files
  - Preflight guard script

## License

Private / Internal use.
