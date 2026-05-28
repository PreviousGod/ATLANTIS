# ATLANTIS — Memory · Cognition · Reasoning System for AI Agents

**This is not a memory plugin. This is a complete cognitive substrate that turns any LLM into a self-aware, stateful intelligence — including models so cheap nobody takes them seriously.**

ATLANTIS doesn't "help the agent remember." It fundamentally changes what the agent IS. Without it, an LLM is a stateless function call — every turn starts from zero. With ATLANTIS, the agent maintains operational truth, reasons causally, detects its own mistakes, and stops itself before spiraling. It's the difference between a goldfish and an engineer.

```
┌──────────────────────────────────────────────────────────────────────┐
│                                                                      │
│   MiniMax-M2.7 ($0.15/M tokens) + ATLANTIS                          │
│   outperforms                                                         │
│   Claude Opus ($15/M tokens) without ATLANTIS                        │
│                                                                      │
│   On multi-step operational tasks. Measured. Not hypothetical.       │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

## Why Every Other Memory System Fails

Every AI memory system on the market does one thing: **retrieves similar text.** They're search engines bolted onto an LLM. Here's why that breaks:

| Approach | What it does | Why it makes agents dumber |
|---|---|---|
| **Vector embeddings** (Mem0, MemGPT, Chroma) | Cosine similarity on chunks | Retrieves "semantically similar" garbage that sounds related but is operationally wrong. You asked about the current bug, it retrieved a conversation about butterflies because both mention "transformation." |
| **Chat summarization** (LangChain memory) | Compresses history into a paragraph | Loses causal relationships, fix recipes, artifact paths, belief states. Great for "remember my name." Useless for "finish what I was doing." |
| **RAG pipelines** (LlamaIndex, LangChain) | Dumps text chunks into context | No model of what's currently TRUE vs what was TRUE last week. The agent acts on stale information because nobody told it the situation changed. |
| **Raw history** (most agent frameworks) | Appends every message | Context window fills with noise. By turn 20 the model is drowning in its own past mistakes. 13-minute death spirals. |

**Every single one of these fails at operational continuity.** They remember text. ATLANTIS remembers TRUTH.

## What ATLANTIS Actually Is

ATLANTIS is three integrated systems that together form a **cognitive architecture**:

### 1. Memory Layer (`live_brain`) — "What do I know?"
Not text storage. **Typed, structured, lifecycle-managed knowledge:**
- **Episodes** — what happened, when, in which session
- **Beliefs** — causal claims with lifecycle: hypothesis → validated → falsified → ruled_out
- **Fix recipes** — "when you see error X, tool Y with args Z solved it"
- **Verified artifacts** — project files selected by role, not filename similarity
- **Epistemic facts** — current, high-stakes facts requiring authoritative sources
- **Entity graph** — connected knowledge with relationship traversal
- **Reality state** — what is the current objective? What open loops exist? What's blocked?

The agent doesn't search for similar text. It queries for **what is TRUE right now.**

### 2. Cognition Layer (`live_brain_ctx`) — "What matters right now?"
The **assembler pipeline** — the secret weapon:
- Classifies every turn into a **lane** (chit_chat, simple_execution, deep_execution, research, continuation, document_intake)
- Each lane has a **byte budget** — chit_chat gets 0 bytes, deep_execution gets 5000
- Only context **relevant to this specific lane** is injected
- Cross-turn **deduplication** — identical sections become 1-line pointers ("unchanged from previous turn")
- **Priority-based dropping** — corrective sections (NUCLEUS STUCK) survive; informational sections (VERIFIED ARTIFACTS) drop first when over budget

This is why a $0.15 model with 4K effective attention can outperform a $15 model with 200K context. The assembler makes every byte count.

### 3. Reasoning Layer (`nucleus`) — "Am I about to do something stupid?"
Not a prompt. **Active intervention:**
- **Mistake detection** — catches "patching without reading the file" before execution
- **Stuck detection** — 3+ consecutive same-tool failures → NUCLEUS STUCK at priority 2 (above everything)
- **Turn economy** — escalating warnings at 3/8/15 turns: "Be efficient" → "You may be stuck" → "STOP ALL TOOL CALLS IMMEDIATELY"
- **Circuit breakers** — physically blocks tool calls after 3 exact failures or 5 same-tool failures
- **Bridge architecture** — nucleus contributes context through live_brain_ctx's assembler, not through its own hook (no double-injection, no race conditions)

## The Smartness Pipeline — How It Actually Works

Here's what happens on every single turn. Compare this to "append chat history to prompt":

```
User: "ok what about the video script?"

┌─────────────────────────────────────────────────────────────┐
│ 1. Intent classification → "continuation_or_resume"        │
│    (Not chit_chat — don't ignore. Not deep_exec —           │
│     don't flood with every artifact.)                        │
├─────────────────────────────────────────────────────────────┤
│ 2. Assembler loads lane-gated context:                      │
│    • ACTIVE TASK: editing video script enoch_part_2         │
│    • RECENT EPISODES: wrote draft, user asked for revision  │
│    • VERIFIED ARTIFACTS: enoch/part_2 → script_v3.md        │
│    • RECALLED FIX: ffmpeg command for video rendering        │
├─────────────────────────────────────────────────────────────┤
│ 3. Lane-gated tools: only 6 brain tools visible             │
│    (brain_recap, brain_recall, brain_reality_debug,         │
│     brain_list_artifacts, brain_mark_artifact,               │
│     brain_resolve_artifact)                                  │
├─────────────────────────────────────────────────────────────┤
│ 4. Prefill forces: THINK: [user wants script status]        │
│    KNOW: [active task=video script, artifact=script_v3.md]  │
│    ACT: [brain_list_artifacts enoch, then respond]          │
├─────────────────────────────────────────────────────────────┤
│ 5. Tool result: [brain_list_artifacts: 2 artifacts]         │
│    (1-line summary BEFORE raw JSON — LLM sees signal first) │
├─────────────────────────────────────────────────────────────┤
│ 6. 3 API calls, 24 seconds, 417 char response.              │
│    No loops. No confusion. No "wait what were we doing?"     │
└─────────────────────────────────────────────────────────────┘
```

**Without ATLANTIS:** The same model gets "ok what about the video script?" with 40 turns of raw chat history. It has no idea which video, which script, or what state it's in. 12 tool calls later, it's still searching for files.

## Why This Architecture Beats Every Alternative

### Deterministic Retrieval > Vector Search
Embeddings find similar-sounding text. ATLANTIS finds operationally relevant truth. When you ask "what's the current bug?", a vector DB might return a conversation from last month that mentions bugs. ATLANTIS returns the **active open bug** from the reality engine, with its **fix recipe** and **causal belief state**, because it modeled those as first-class entities — not as floating text chunks.

### Lane-Gated Injection > Dump Everything
Every other system injects the same memory blob on every turn. ATLANTIS gives 0 bytes to chit-chat, 1500 bytes to simple execution, 5000 bytes to deep execution. A greeting doesn't revive a dead objective from 3 days ago. A quick command doesn't flood the context with entity graph traversals.

### Active Intervention > Hoping The Model Behaves
"Please think step by step" is a suggestion a weak model ignores. A blocked tool call is a physical constraint it can't bypass. The stuck detector at priority 2 is the last thing the model sees before responding. The circuit breaker stops the loop before it wastes 800 seconds.

### Typed Knowledge > Raw Text
A belief isn't text. It's a structured claim with a lifecycle. When evidence falsifies it, cascading invalidation updates everything that depended on it. A fix recipe isn't a memory. It's a pattern: "problem X → tool Y → args Z." The assembler injects it at exactly the right moment.

### Self-Awareness > Statelessness
The reality engine maintains current objective, open loops, blockers, danger zones, and action constraints. The agent always knows what it's doing, what's blocking it, and what needs to happen next — without asking the user "what were we doing?"

## What Users Experience

**Before ATLANTIS:**
> "fix the upload script"
> *agent runs terminal 19 times, web_searches its own error messages, times out after 300s, finally responds "we need to wait 24h" after 13 minutes*

**After ATLANTIS:**
> "fix the upload script"
> *agent recalls the script path from artifacts, checks reality engine for open loops, reads the file, identifies the bug, patches it, verifies. 4 tool calls, 45 seconds, done.*

This is not hypothetical. This is measured on the same hardware, same model (MiniMax-M2.7), same tasks.

## Installation

```bash
git clone https://github.com/PreviousGod/ATLANTIS.git
cd ATLANTIS
python install.py --auto
```

The installer:
1. Detects your Hermes installation
2. Backs up existing plugins
3. Installs live_brain + live_brain_ctx + nucleus
4. Installs prefill.json (forced THINK:/KNOW:/ACT: structure)
5. Patches config.yaml with optimal guardrail settings
6. Installs Python dependencies

```bash
livebrain update     # pull latest + reinstall
livebrain status     # check installation health
```

## Architecture

```
ATLANTIS/
├── live_brain/          Memory Provider — 15 brain_* tools, typed knowledge
├── live_brain_ctx/      Context Engine — 5 hooks, assembler, lane budgets
├── nucleus/             Reasoning Layer — intervention, stuck detection, bridge
├── prefill.json         Forced response structure (THINK:/KNOW:/ACT:)
├── install.py           Cross-platform installer + updater
├── livebrain            CLI management tool
└── README.md
```

## Design Principles

1. **Constraints over suggestions** — a blocked tool call works. A "please be careful" prompt doesn't.
2. **Less is more** — 500 bytes of right context beats 5000 bytes of noise. The assembler enforces this.
3. **Deterministic over probabilistic** — no embeddings. Every retrieval is auditable and debuggable.
4. **Typed over raw** — beliefs have lifecycles. Facts have sources. Artifacts have roles. Memory has structure.
5. **Defense in depth** — stuck detector + turn economy + circuit breakers + hard stops. Multiple layers catch what one layer misses.

## License

Private. Internal use.
