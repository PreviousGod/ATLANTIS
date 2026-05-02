# Live Brain Public Launch Kit

## Public positioning

**One-liner:** Semantic memory remembers text. Live Brain maintains operational truth.

**Short pitch:** Live Brain is an operational memory/control layer for long-running agents: verified artifacts, active work state, causal learning, context explainability, and safety-gated self-evolution.

**Not the claim:** “We invented agent memory.”

**Actual claim:** Existing pieces like vector memory, reflection, saved preferences, and workflow libraries are useful. Live Brain connects those pieces into an auditable operational loop where the agent can know what is valid, stale, blocked, risky, approved, and why it entered the prompt.

## Demo assets

- Full captioned video: `demo/live_brain_control_room_demo.mp4`
- Full voiceover video: `demo/live_brain_control_room_demo_voiceover.mp4`
- 30-second teaser: `demo/live_brain_control_room_teaser.mp4`
- 30-second teaser with voiceover: `demo/live_brain_control_room_teaser_voiceover.mp4`
- Thumbnail: `demo/live_brain_control_room_thumbnail.png`
- Full voiceover script: `demo/live_brain_voiceover_full.txt`
- Teaser voiceover script: `demo/live_brain_voiceover_teaser.txt`

All public assets are generated from the synthetic demo DB under `/tmp/live_brain_control_room_demo`, not the real Live Brain store.

## Recommended title options

1. Beyond Vector Memory: Live Brain for Long-Running Agents
2. The Missing Control Layer for Agent Memory
3. Agent Memory Should Know What Is True, Stale, Risky, and Approved

## 30-second post

Most agent memory is semantic search: it remembers similar text.

That is useful, but it is not enough for long-running agents.

Live Brain maintains operational truth: verified artifacts, stale files, active work, blocked work, causal failures, context attribution, and safety-gated self-evolution.

The agent can learn from mistakes — but high-risk changes go through an approval gate and every decision is audited.

Semantic memory remembers text. Live Brain maintains operational truth.

## Longer launch post

Vector memory is great at retrieving similar text. But long-running agents need more than similarity.

They need to know which artifact is verified, which file was rejected, which task is still active, which belief is only a hypothesis, which workflow was proven, and which self-change is too risky to apply silently.

Live Brain is an operational memory/control layer for agents. It combines scope-aware beliefs, verified artifact tracking, work-item lifecycle, causal learning from corrections, context explainability, and safety-gated self-evolution.

The key shift: memory stops being a pile of retrieved transcripts and becomes inspectable infrastructure.

The agent can learn, propose changes, and improve workflows — but high-risk mutations are held behind approval gates with evidence, risk scores, suggested tests, and audit logs.

Semantic memory remembers text. Live Brain maintains operational truth.

## Demo narration

### Full version

Use `demo/live_brain_voiceover_full.txt`.

### Teaser version

Use `demo/live_brain_voiceover_teaser.txt`.

## FAQ

### Does this already exist?

Pieces exist: vector search, reflection loops, saved preferences, workflow libraries, evals, and approval tools. Live Brain combines them into one operational loop: scope-aware truth, verified artifacts, work state, causal learning, context attribution, and gated self-evolution.

### Is it fully autonomous?

It is self-evolving, not reckless. Low-risk operational updates can be applied automatically when bounded by rules. High-risk changes become explicit proposals with evidence, tests, and human approval.

### Why is this different from semantic search?

Semantic search can retrieve both the correct file and the wrong old file if they look similar. Live Brain tracks artifact status and role explicitly, so the agent knows which result is safe to use.

### Why should developers care?

Because debugging agent memory becomes possible. You can inspect why context entered the prompt, what caused previous failures, what was learned, and what the agent wants to change next.

## Publishing checklist

- Use only synthetic demo assets.
- Do not expose the real dashboard or real database.
- Lead with the verified-versus-rejected artifact example.
- Show approval gates before saying “self-evolving.”
- Avoid claiming a replacement for vector memory; frame it as the operational layer above it.
