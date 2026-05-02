# Live Brain Control Room — 3-Minute Demo

## One-line hook

> Vector memory remembers similar text. Live Brain maintains operational truth: what is valid, stale, blocked, verified, risky, and approved.

## Setup

Use the synthetic demo DB so no private sessions, IDs, paths, or artifacts are exposed.

```bash
cd /home/deyaan666/live_brain_plugin_package
python3 tools/live_brain_demo.py --reset
python3 tools/live_brain_control_room.py --db /tmp/live_brain_control_room_demo/live_brain.db --port 8777
# open http://127.0.0.1:8777/
```

Or serve immediately:

```bash
python3 tools/live_brain_demo.py --reset --serve --port 8777
```

For Tailscale demo on a trusted tailnet:

```bash
python3 tools/live_brain_demo.py --reset
python3 tools/live_brain_control_room.py --db /tmp/live_brain_control_room_demo/live_brain.db --tailscale --port 8777 --auth-token 'demo-token'
```

## Demo flow

### 0:00 — Problem

Say:

> Most agent memory today is semantic search. It retrieves old text. That is useful, but it does not know whether a file is verified, a task is resolved, a belief is a hypothesis, or a self-change is risky.

Show the hero line:

> Semantic memory remembers text. Live Brain maintains operational truth.

### 0:30 — Wrong artifact vs verified artifact

Open **Verified Artifacts**.

Show:

- `enoch_part2_correct_final.mp4` is `verified`
- `enoch_part2_old_wrong_cut.mp4` is `rejected`

Say:

> Semantic search can find both. Live Brain knows which one is safe to send.

Then open **Operational Beliefs** and point at:

> The prior failure was caused by semantic search choosing an old rejected mp4.

### 1:10 — Work graph, not transcript soup

Open **Work Graph**.

Show:

- resolved: prevent sending stale files
- active: surface self-evolution approvals only when needed
- blocked: benchmark Live Brain against vector memory

Say:

> This is not a pile of memories. It is a lifecycle: active, blocked, resolved, superseded.

### 1:45 — Self-evolution, gated

Open **Approval Gates**.

Show the pending proposal:

- type: `config_change`
- target: `context`
- risk: high
- evidence and suggested tests

Say:

> The agent can learn from repeated failures, but it cannot silently mutate high-risk behavior. It proposes, scores risk, records evidence, and waits.

Click **Approve** or **Reject**.

Say:

> Every decision is audited.

### 2:20 — Why this context?

Open **Why This Context?**.

Try queries:

```text
send me Enoch part 1 and part 2
```

Expected sections:

- `VERIFIED ARTIFACTS`
- `MUST FOLLOW`

Then try:

```text
show pending approvals
```

Expected sections:

- `PENDING APPROVAL` if pending remains
- explicit routing to `brain_self_evolution`

Say:

> This is the missing debugger for agent memory: why did this context enter the prompt?

### 2:50 — Flight recorder

Open **Flight Recorder**.

Show audit events, proposal events, work changes, and context impressions.

Say:

> You can inspect how the agent learned, what changed, and why.

## Closing line

> This is a safety-gated operational memory layer for long-running agents — beyond vector memory, with provenance, artifact truth, work state, causal learning, and auditable self-evolution.

## If someone asks “doesn’t this already exist?”

Answer:

> Pieces exist: vector memory, reflection, saved preferences, workflow libraries. Live Brain combines them into a live control layer: scope-aware operational state, verified artifacts, causal learning, context explainability, and gated self-evolution in one auditable loop.

## Good 10-second clip

1. Show rejected vs verified artifact.
2. Show pending self-evolution proposal.
3. Click approve.
4. Show timeline audit entry.
5. Say: “The agent learned — but the gate stayed under human control.”
