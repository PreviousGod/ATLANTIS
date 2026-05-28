# ATLANTIS — Complete Agent Intelligence System for Hermes

**Makes weak LLMs deliver expert results. No embeddings. No vectors. Pure operational truth.**

ATLANTIS turns any LLM — including cheap models like MiniMax-M2.7 — into a
stateful, self-aware agent that remembers what it's doing, retrieves only
relevant context, and stops itself before looping. It's the difference between
"what were we doing?" and "I already fixed that, here's the next step."

```
┌──────────────────────────────────────────────────────────────────────┐
│                        ATLANTIS Stack                                │
├───────────────┬──────────────────┬───────────────────────────────────┤
│  live_brain   │  live_brain_ctx  │  nucleus                          │
│  (provider)   │  (context engine)│  (guard + stuck detector)         │
│  15 brain_*   │  5 hooks         │  3 hooks + bridge                 │
│  tools        │  assembler       │  contributions                    │
├───────────────┴──────────────────┴───────────────────────────────────┤
│  SQLite (WAL) — ~/.hermes/live_brain/live_brain.db                  │
│  8 migrations │ 60+ tables │ FTS5 │ Causal beliefs │ Fix recipes     │
├──────────────────────────────────────────────────────────────────────┤
│  prefill.json — forces THINK:/KNOW:/ACT: response structure          │
└──────────────────────────────────────────────────────────────────────┘
```

## What Makes ATLANTIS Different

| Every other memory system | ATLANTIS |
|---|---|
| Dumps raw chat history into context | **Lane-gated injection** — chit_chat gets 0 bytes, deep_execution gets 5KB of relevant state |
| Vector DB with noisy semantic matches | **Deterministic scoring** — keyword, domain, marker, causal — no embeddings |
| LLM loops forever on failures | **Stuck detector** — 3+ same-tool failures forces escape prompt at priority 2 |
| Shows all 100 tools to a weak model | **Lane-gated tools** — 3-6 tools per turn lane, not all 14 |
| Raw JSON tool results drown the LLM | **1-line summaries** — `[brain_recall: 3 matches. Top: project enoch config...]` |
| "Please think step by step" (ignored) | **Forced prefill** — LLM output starts with `THINK:` in the buffer, can't skip it |
| Chit-chat revives dead objectives | **Intent classification** — greetings get zero context, no stale state leakage |

## Plugin Architecture

### `live_brain/` — Memory Provider
15 brain tools the LLM can actively query. Episodic, causal, factual, and
artifact memory with typed retrieval and belief lifecycle management.

### `live_brain_ctx/` — Context Engine
The central pipeline. Classifies intent, loads relevant state, assembles
context with byte budgets, cross-turn dedup, and lane-gated priority dropping.
Runs as `pre_llm_call` hook — injects context BEFORE the LLM sees the prompt.

### `nucleus/` — Guard & Stuck Detector
Intervention engine catches mistake patterns before tool execution (patch
without reading, write without backup). Stuck detector forces escape when
3+ same-tool failures. Contributions injected via bridge at priority 2-5.

### `prefill.json` — Forced Response Structure
The LLM's output buffer starts with `THINK: [analyze]\nKNOW: [context]\nACT:`.
Weak LLMs can't ignore their own output stream.

## Quick Install

```bash
git clone https://github.com/PreviousGod/ATLANTIS.git
cd ATLANTIS
python install.py --auto
```

The installer detects your Hermes installation, backs up existing plugins,
installs all three plugins + prefill, patches config.yaml, and installs deps.

After install, restart your Hermes gateway:
```bash
systemctl --user restart hermes-gateway
# or just restart hermes from CLI
```

## Update

```bash
python install.py --update     # pull latest from repo + reinstall
```

Or after install, you can also run:
```bash
livebrain update               # if the CLI tool is on PATH
```

## The Smartness Layer

### Forced Prefill
```json
[{"role": "assistant", "content": "THINK: [Analyze request against context]\nKNOW: [What's relevant]\nACT:"}]
```
The LLM's first token is already written. It MUST complete the thought.

### Lane-Gated Tools
| Turn lane | Tools visible | Why |
|---|---|---|
| `chit_chat` | 0 brain tools | No memory pollution on "hello" |
| `simple_execution` | 6 tools | recap, recall, reality, artifacts |
| `deep_execution` | 10 tools | full reasoning + entity graph |
| `research_or_epistemic` | 6 tools | epistemic, research, compose, entity |
| `continuation_or_resume` | 5 tools | recap, recall, reality, beliefs |
| `document_intake` | 3 tools | artifacts only |

### Tool Result Compression
Every brain tool result gets a 1-line summary before raw JSON:
```
[brain_recap: 3 recent work items]
{"recap": [...]}
```

### Turn Economy Warnings
- 3 turns: "Be efficient"
- 8 turns: "You may be stuck — STOP and explain"
- 15 turns: "CRITICAL — STOP ALL TOOL CALLS IMMEDIATELY"

### Stuck Detector
When 3+ consecutive same-tool failures detected, NUCLEUS STUCK injects at priority 2:
```
NUCLEUS STUCK DETECTED:
- 5 consecutive failures of tool 'terminal'
- STOP all tool calls IMMEDIATELY
- Do NOT retry 'terminal' — it will fail again
- Tell the user what went wrong
```

### Circuit Breakers
After 3 exact same failures or 5 same-tool failures, tool calls are **physically blocked**.

## Full Tool List

| Tool | Purpose |
|---|---|
| `brain_recap` | Summarize recent work |
| `brain_recall` | Natural language memory query |
| `brain_reality_debug` | Active objectives, open loops, constraints |
| `brain_mark_belief` | Create/update causal beliefs |
| `brain_list_artifacts` | List verified project files |
| `brain_mark_artifact` | Register/deprecate project artifacts |
| `brain_resolve_artifact` | Resolve artifact path by project+role |
| `brain_epistemic` | Autonomous web research (current facts) |
| `brain_research` | Bounded research planning |
| `brain_entity_graph` | Traverse entity relationships |
| `brain_synthesize` | Cross-session dialectic synthesis |
| `brain_user_profile` | View/update user preferences |
| `brain_compose_query` | Algebraic queries (A + B - C) |
| `brain_self_evolution` | Propose/approve/reject code changes |
| `brain_state_debug` | Inspect work_state (debug, gated) |

## Directory Structure

```
ATLANTIS/
├── live_brain/               Memory provider (15 brain_* tools)
│   ├── __init__.py            Provider + lane-gating + tool summaries
│   ├── store.py               SQLite store with connection pooling
│   ├── ingest.py              Turn ingestion + entity extraction
│   ├── retrieval.py           Token-budgeted briefing builder
│   ├── reality.py             Reality engine (objectives, loops, gates)
│   ├── epistemic.py           Autonomous research layer
│   ├── causal.py              Belief lifecycle management
│   ├── evolution.py           Self-evolution proposals
│   ├── entity_graph.py        Relationship graph traversal
│   ├── dialectic.py           Cross-session synthesis
│   ├── artifacts.py           Verified artifact registry
│   ├── rules.py               Binding constraint engine
│   ├── briefing.py            Compression + canonical recaps
│   ├── research.py            Bounded research planning
│   ├── schema_manager.py      Migration runner
│   ├── connection_pool.py     Thread-safe DB pool
│   ├── backup_manager.py      Online SQLite backup
│   ├── maintenance_manager.py Scheduled hygiene
│   ├── migrations/            000-008 SQL migrations
│   └── tests/                 10 test files
│
├── live_brain_ctx/            Context engine (5 hooks)
│   ├── __init__.py            Thin facade + register
│   ├── modules/
│   │   ├── hooks.py           4 hook functions + turn economy
│   │   ├── assembler.py       Byte-budget assembler + dedup
│   │   ├── bridge.py          Cross-plugin shared state
│   │   ├── cognitive_architecture.py  Tiered reasoning
│   │   ├── scoring.py         Overlap/domain/marker scoring
│   │   ├── formatting.py      Section formatting
│   │   ├── integrations.py    Reality + epistemic bridges
│   │   ├── query_filters.py   Intent classification
│   │   └── ...
│   ├── tests/                 6 test files + golden snapshots
│   └── context_config.json    Lane budgets + section priorities
│
├── nucleus/                   Guard + stuck detector
│   ├── __init__.py            Hooks + monkey-patch + bridge registration
│   ├── nucleus_engine.py      Core (heartbeat removed, pargod kept)
│   ├── contributions.py       Bridge contributions + stuck detector
│   ├── intervention.py        Mistake pattern detection
│   ├── session_state.py       Thread-safe runtime state
│   ├── pargod.py              Graph-based resolution
│   ├── config.py              All thresholds + paths
│   └── ...
│
├── install.py                 Cross-platform installer
├── livebrain                  CLI tool (install/update/status)
├── prefill.json               Forced THINK:/KNOW:/ACT: structure
├── requirements.txt           Python dependencies
└── scripts/
    └── plugins_preflight.sh   Compile + import + migration check
```

## Config Reference

Add to `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - live_brain
    - live_brain_ctx
    - nucleus

memory:
  provider: live_brain

context:
  engine: live_brain_ctx

agent:
  prefill_messages_file: prefill.json

tool_loop_guardrails:
  hard_stop_enabled: true
  hard_stop_after:
    exact_failure: 3
    same_tool_failure: 5
```

## Design Philosophy

1. **Constraints over suggestions** — weak LLMs ignore "please think carefully" but can't ignore their own output stream (prefill) or a blocked tool call (circuit breaker)
2. **Less is more** — 3 tools visible, not 14. 500 bytes of right context beats 5000 bytes of noise
3. **Deterministic retrieval** — keyword/rule scoring, not embeddings. Auditable, debuggable.
4. **Graceful degradation** — broken migration doesn't crash provider; missing JSON doesn't crash import
5. **Defense in depth** — stuck detector + turn economy + circuit breakers + hard stops. Multiple layers catch what one misses.

## Changelog

### 2026-05-28 — Smartness Layer
- Lane-gated tool visibility (3-6 tools per turn lane)
- Tool result compression (1-line summaries before raw JSON)
- Turn economy section (escalating warnings at 3/8/15 turns)
- Nucleus stuck detector (3+ same-tool failures → priority 2 escape prompt)
- Nucleus heartbeat removed (hooks + bridge provide all value)
- Pargod schema fix (use_count + last_used columns)
- Hook exception handling (try/except on all hooks)
- Forced prefill (THINK:/KNOW:/ACT:)
- Circuit breakers enabled (hard_stop_enabled: true)

### 2026-05-17 — Intent-Gated Context
- Intent classification for chit_chat, continuity_recap, task_execution
- Centralized section allowlists and budgets
- Greetings and vague follow-ups no longer revive stale state

### 2026-05-11 — Cognitive Architecture
- Tiered reasoning (Tier 1/2/3) with zero overhead for trivial queries
- Adversarial self-attack before delivering answers
- Anti-downgrade: ruled_out state with constraint propagation

## License

Private / Internal use.
