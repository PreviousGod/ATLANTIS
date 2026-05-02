# ATLANTIS

**Semantic memory remembers similar text. ATLANTIS maintains operational truth.**

ATLANTIS is a Hermes Live Brain plugin: a local SQLite-backed memory/context system for long-running agents that need to know what is true, stale, risky, approved, blocked, and safe to do next.

## The Problem

Most agent memory systems answer one question: **"what old text looks similar to this prompt?"**

That is useful, but it is not enough for autonomous agents. Long-running agents fail because they:

- recall stale facts as if they are current
- mix unrelated projects, runs, files, and corrections
- repeat causes that were already ruled out
- treat hypotheses as validated facts
- choose artifacts because filenames look similar, not because they are verified
- hallucinate current/high-stakes answers from old memory
- lack an approval gate for dangerous actions or self-modification
- cannot explain why a memory entered the prompt

ATLANTIS treats memory as **operational state management**, not just retrieval.

## What ATLANTIS Does Differently

| Ordinary semantic memory | ATLANTIS |
|---|---|
| Retrieves similar text | Tracks current operational reality |
| Stores memories as blobs | Separates facts, beliefs, rules, artifacts, work items, evidence, and audit events |
| Recalls stale notes | Applies TTL, authority policy, and high-stakes freshness checks |
| Can repeat bad diagnoses | Tracks belief lifecycle: `open → validated / falsified / ruled_out / superseded` |
| Finds files by fuzzy match | Uses a verified artifact registry with roles and statuses |
| Has little safety policy | Adds action gates for high-risk operations |
| Self-improvement can be unsafe | Gates self-evolution proposals with risk scoring and approval |
| Context is opaque | Records context impressions and attribution |

## Why It Matters

ATLANTIS gives an agent **situational awareness**:

- What task is active?
- What loop is still open?
- What caused the last failure?
- What cause was ruled out?
- Which file is verified and which was rejected?
- Which information must be researched again because it is current or high-stakes?
- Which action is too risky to perform without approval?
- Why did this exact context enter the prompt?

That turns memory from a pile of transcripts into an inspectable control layer.

## Best Capabilities

- **Reality Engine:** active tasks, open loops, blockers, danger zones, constraints, and safe next actions.
- **Belief lifecycle:** hypotheses are not facts; causes can be validated, falsified, ruled out, or superseded.
- **Epistemic discipline:** current/high-stakes questions require fresh authoritative sources or a safe "I cannot confirm" answer.
- **Verified artifacts:** exact project files are selected by verified role/status, not filename similarity.
- **Action gates:** dangerous actions such as trading, DB schema changes, credential exposure, public network exposure, and media sends are policy-checked.
- **Audit spine:** memory mutations write revisions, evidence packets, and maintenance records.
- **Context attribution:** the system can explain what was injected into the prompt and why.
- **Gated self-evolution:** the agent can propose improvements without silently applying high-risk code/config/schema changes.

## Why It Is Different

A normal memory layer helps an agent remember. ATLANTIS helps an agent **operate**.

Example: if the user asks "a link?", semantic memory may return every old link that looks similar. ATLANTIS can know the current active link, that the service refused connection, and that the next safe action is checking the service status.

Example: if the user asks for current CME/NQ price-limit rules, semantic memory may recall an old numeric note. ATLANTIS marks the question as current/high-stakes, prefers official sources, blocks unsupported numeric claims, and gives a safe answer if extraction fails.

## Benchmark Snapshot

The included deterministic benchmark compares ATLANTIS against a MemPalace-style semantic-memory baseline. It is not an official MemPalace runtime adapter; it models the common "store text, retrieve overlapping memories" class of systems.

```text
ATLANTIS:                 100.0 / 100
MemPalace-style baseline:   5.7 / 100
Case wins:                    7 / 7
```

Covered cases include situational awareness, action gating, autonomous research triggers, authority filtering, evidence discipline, TTL-backed learning, and stale recall prevention for high-stakes/current questions.

## Public Repo Note

ATLANTIS keeps runtime state local. Do not commit real `~/.hermes/live_brain/*.db`, Telegram sessions, credentials, generated backups, or private demo media. Large demo videos/audio should be uploaded as GitHub Release assets, not tracked in git.

## Contents

```text
live_brain/          memory provider, store, ingestion, rules, causal learning, research
live_brain_ctx/      context injection and post-tool-result hooks
tools/               metrics, cleanup, backtest, promotion, context debug
tests/               smoke/eval coverage
smoke_test.py        one-command local validation
INSTALL.md           install tutorial
```

## Core ideas

- strict scoped context injection
- atomic facts and binding rules
- candidate → active recipe promotion with artifact verification
- context impressions and attribution metrics
- recipe ageing/degradation with audit log
- append-only memory events, object revisions, and evidence packets
- lifecycle hygiene for stale impressions, weak hypotheses, recipes, low-priority work items, and stale E2E/self-evolution approvals
- gated self-evolution proposals with risk scoring and automatic expiry for stale orphaned approvals
- rate-limited init maintenance with logged summaries, WAL checkpointing, and DB/plugin backup rotation
- research results stored into research tables, beliefs, facts, and evidence packets

## Quick install

See `INSTALL.md`.

Short version:

```bash
mkdir -p ~/.hermes/plugins
cp -a live_brain ~/.hermes/plugins/live_brain
cp -a live_brain_ctx ~/.hermes/plugins/live_brain_ctx
systemctl --user restart hermes-gateway
python3 smoke_test.py
```

Configure Hermes:

```yaml
memory:
  provider: live_brain
  memory_enabled: false
  user_profile_enabled: false
```

Database path:

```text
~/.hermes/live_brain/live_brain.db
```

## Finish and E2E Validation

Local regression suite:

```bash
python3 -m py_compile live_brain/*.py live_brain_ctx/*.py tools/*.py tests/*.py
python3 smoke_test.py
python3 tests/live_brain_audit_hygiene_test.py
python3 tests/live_brain_capability_e2e_test.py
python3 tests/live_brain_ingest_memory_facts_test.py
```

Autonomous finish runner:

```bash
python3 tools/live_brain_autonomous_finish.py --full
```

The finish runner now also rotates `live_brain_backup_*.db`, checkpoints/truncates WAL, prunes old installed-plugin backup directories, expires orphaned E2E seed approvals after 1 hour, and records maintenance summaries. Runtime init maintenance is rate-limited by `LIVE_BRAIN_INIT_MAINTENANCE_INTERVAL_SECONDS` (default: 21600 seconds) so gateway restarts do not mutate memory repeatedly.

Real Telegram capability E2E:

```bash
python3 tools/live_brain_capability_e2e.py
# or skip only the live web-research leg:
python3 tools/live_brain_capability_e2e.py --skip-research
```

The Telegram E2E is intentionally not a media/video test. It seeds a fresh run, verifies baseline unknown → remembered codename, validates ruled-out vs confirmed cause recall, checks next-action continuity, proves a fresh memory inference from two stored facts, exercises the epistemic guard for current/high-stakes questions, confirms chitchat does not dump memory internals, and ends by asking the live agent for a critical VERDICT/BLOCKERS/NEXT_FIXES self-review.

## Live Brain Reality Engine

Reality Engine turns Live Brain from memory retrieval into persistent situational awareness. Instead of asking only “what old text is similar?”, it maintains what is currently going on: objective, active project, open loops, blockers, danger zones, action constraints, and safe next action.

Core flow:

```text
Hermes event → reality_events → deterministic reducers → reality_state/open_loops/danger_zones/action_constraints → LIVE REALITY brief
```

Hermes integration follows the plugin hook contract:

- `pre_llm_call` records the user event and injects an ephemeral `LIVE REALITY` block into the current user message, not the system prompt.
- `post_tool_call` records tool evidence and reducer outcomes such as service failures, missing dependencies, or delivery success.
- `post_llm_call` records assistant outcomes for continuity and completion learning.

Debug locally:

```bash
python3 tools/live_brain_reality_debug.py --query "a link?" --record
python3 tools/live_brain_reality_debug.py --scope-key demo:enoch --query "a link?" --db /tmp/live_brain_control_room_demo/live_brain.db
```

Action gate examples:

```bash
python3 tools/live_brain_reality_debug.py --action-type db_schema --action-payload-json '{"migration":"ALTER TABLE facts ADD COLUMN x TEXT"}'
python3 tools/live_brain_reality_debug.py --action-type media_send --action-payload-json '{"path":"/tmp/live_brain_control_room_demo/artifacts/enoch_part2_correct_final.mp4","synthetic_public":true}'
```

`db_schema`, `schema`, and `db_schema_migration` all route through the same high-risk approval policy.

Control Room panel: **What Live Brain Thinks Is Going On**.

## Verified Artifact Registry

Live Brain includes a deterministic artifact layer for production projects where the agent must choose the exact file, not infer from fuzzy search results.

Key rule:

```text
search_files finds candidates; verified_artifacts decides the winner.
```

Main commands:

```bash
tools/live_brain_artifacts.py seed-enoch
tools/live_brain_artifacts.py list --project enoch --include-inactive
tools/live_brain_artifacts.py resolve --project enoch --role part_2
tools/live_brain_artifacts.py verify --project enoch --role part_2 --path /absolute/file.mp4
tools/live_brain_artifacts.py mark --path /absolute/file.mp4 --status rejected --reason "wrong artifact"
```

When a query has artifact intent, the context compiler can inject a compact section:

```text
VERIFIED ARTIFACTS:
- project=enoch role=part_1 path=/...
- project=enoch role=part_2 path=/...
```

This prevents weak LLMs from sending a similarly named but wrong artifact, such as using a `part1` file as `part_2`.

## Gated Self-Evolution

Live Brain can now evolve its own behavior through audited proposals instead of uncontrolled self-modifying code.

Safety model:

```text
detect signal → propose change → risk score → auto-apply only low-risk metadata cleanup → require approval for code/config/schema/files/media
```

Examples:

```bash
tools/live_brain_self_evolution.py --limit 20
tools/live_brain_self_evolution.py --status needs_approval
tools/live_brain_self_evolution.py --approve self_evolution:...
tools/live_brain_self_evolution.py --approve-latest --reason "approved in chat"
tools/live_brain_self_evolution.py --reject self_evolution:... --reason "not enough evidence"
```

The agent-facing `brain_self_evolution` tool uses the same gate. High-risk proposals like code patches, config changes, DB schema migrations, file deletion, credential changes, or media sending are recorded as `needs_approval`; bounded recipe demotions from direct failure feedback may auto-apply with audit logs.

## Live Brain Control Room

The Control Room is a local, dashboard-style view over the Live Brain SQLite store. It is designed as a flight recorder and approval surface, not a public web app.

Run it locally:

```bash
python3 tools/live_brain_control_room.py
# open http://127.0.0.1:8765/
```

Useful options:

```bash
python3 tools/live_brain_control_room.py --check
python3 tools/live_brain_control_room.py --db ~/.hermes/live_brain/live_brain.db --port 8765
```

Panels:

- **Approval Gates** — pending self-evolution proposals with risk score, evidence, suggested tests, approve/reject buttons.
- **Work Graph** — active/blocked/resolved work items and next actions.
- **Operational Beliefs** — facts, hypotheses, and causal/workflow activations.
- **Verified Artifacts** — project files with role/status/confidence so wrong outputs are not selected by fuzzy memory.
- **Why This Context?** — compile and inspect the exact Live Brain context injected for a query.
- **Flight Recorder** — audit events, proposal updates, work changes, and context impressions in one timeline.

Security note: bind to `127.0.0.1` unless you intentionally want to expose private memory data. The dashboard reads local memory and can approve/reject self-evolution proposals.

Tailscale access:

```bash
python3 tools/live_brain_control_room.py --tailscale
# open the printed Access URL from any trusted device in your tailnet
```

If auto-detection cannot find the Tailscale IP, pass it manually:

```bash
python3 tools/live_brain_control_room.py --host 100.x.y.z --auth-token 'choose-a-long-token'
```

Non-loopback binds require token auth by default. Avoid `--no-auth` unless you are on a fully isolated test network.

Clean public demo data:

```bash
python3 tools/live_brain_demo.py --reset
python3 tools/live_brain_control_room.py --db /tmp/live_brain_control_room_demo/live_brain.db --port 8777
```

Walkthrough: `demo/CONTROL_ROOM_DEMO.md`.

Public launch assets:

```bash
python3 tools/build_live_brain_public_demo.py --reset-demo-db
```

This generates a full voiceover demo, a 30-second teaser, a thumbnail, and copy/paste launch text from synthetic data only. Launch kit: `demo/PUBLIC_LAUNCH_KIT.md`.
