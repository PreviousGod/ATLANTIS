# live_brain_ctx — Context Engine for live_brain

Context engine and pre-LLM continuity injection powered by Live Brain.
Registers a `LiveBrainContextEngine` plus four hooks (`pre_llm_call`,
`pre_tool_call`, `post_tool_call`, `post_llm_call`) that inject a
scope-aware "LIVE BRAIN" context block before each LLM call and record
reality/epistemic events back into the DB after responses.

## Status (2026-05-11)

Operational and guard-rail protected:

- `register()` entrypoint validated by dedicated test suite
  (`tests/test_hook_dispatch.py`) — 6 tests covering import, all 4 hooks,
  context engine registration, kwargs contract, and idempotency.
- Preflight script (`~/.hermes/scripts/plugins_preflight.sh`) runs
  `py_compile` + import smoke on every `.py` file before any gateway
  restart, catching SyntaxError regressions in < 2 s.
- Stale `__init__.py.backup` removed; legacy pre-refactor
  `test_refactoring.py` removed (was incompatible with current
  `QueryContext` shape).

## Plugin contract

| Hook | Called when | Returns |
|---|---|---|
| `pre_llm_call` | Before each LLM request | `{"context": "..."}` to inject, or `None` |
| `pre_tool_call` | Before each tool dispatch | `{"action": "block", ...}` to block, or `None` |
| `post_tool_call` | After tool result | `None` (records result only) |
| `post_llm_call` | After LLM response | `None` (records reality events + impressions) |

Context engine: `LiveBrainContextEngine` (subclass of
`agent.context_compressor.ContextCompressor`) registered via
`ctx.register_context_engine()`.

## What gets injected into "LIVE BRAIN" block

The exact sections vary by query type; typical contents:

- `MUST FOLLOW` — binding constraints (e.g. "never delete artifact X")
- `ACTIVE TASK` — currently active work item
- `VERIFIED ARTIFACTS` — paths confirmed by `brain_mark_artifact`
- `KNOWN FACTS` — high-confidence facts with scope match
- `OPEN BUG` — unresolved diagnostic episodes
- `PROVEN FIX` — fix_recipes matching the query (FTS5-backed)
- `NEXT REQUIRED ACTION` — from reality engine open loops
- `RECENT EPISODES` — canonical recaps for continuity queries
- `PENDING APPROVAL` — auto-surfaced self-evolution proposals
- `EPISTEMIC STATUS` — research gaps / authoritative sources
- `CONTINUITY MEMORY` — relevant past-session syntheses

Section budgets configured in `context_config.json`.

## Intent-gated context policy

As of 2026-05-17, context surfacing is intent-gated before sections are added
to the prompt. This exists to prevent a common failure mode in long-running
agents: a short greeting, recap question, or file lookup accidentally revives
stale operational objectives.

The policy buckets are:

| Intent | Purpose | Allowed section families |
|---|---|---|
| `chit_chat` | Short greetings / acknowledgements | none |
| `continuity_recap` | "šta si radio", "where were we" style recap | recap + continuity + facts |
| `task_execution` | active debugging / coding / fixing | full operational context |
| `local_repo_lookup` | file/path/repo lookup | artifacts + facts + proven fixes |
| `approval_flow` | approval / pending proposal queries | approval sections only |

This means:

- greetings no longer reopen `ACTIVE TASK` or `NEXT REQUIRED ACTION`
- recap prompts do not inherit execution noise unless they explicitly become operational
- repo/file lookups stay factual instead of reopening stale objectives
- approval prompts stay narrow and do not drag code/task context into the prompt

## Why this is better than generic memory injection

Many memory systems are good at recall but weak at prompt selection. They can
store the right information and still inject the wrong context.

`live_brain_ctx` is stronger for operational agents because it:

- gates sections by user intent instead of dumping "top relevant" snippets
- prefers verified artifacts and active truth over broad semantic similarity
- keeps casual chat and recap turns cheap, short, and clean
- logs section acceptance/rejection decisions for debugging and regression tests

That does not make it universally better than every memory product. It makes it
better for scoped agent execution where the wrong context is worse than missing
context.

## Running tests

```bash
bash ~/.hermes/plugins/live_brain_ctx/tests/run_all_tests.sh
```

Or an individual test:

```bash
~/.hermes/hermes-agent/venv/bin/python \
  ~/.hermes/plugins/live_brain_ctx/tests/test_hook_dispatch.py
```

## Preflight

The global preflight (`~/.hermes/scripts/plugins_preflight.sh`) covers this
plugin:

```bash
bash ~/.hermes/scripts/plugins_preflight.sh && \
  systemctl --user restart hermes-gateway
```

Always run preflight before any restart if you have edited `__init__.py` or
any file under `modules/`.

## Configuration

`context_config.json` (merged from `$HERMES_HOME/live_brain/context_config.json`
if present) controls:

- `chit_chat_patterns` — short messages that receive no context injection
- `low_signal_words` — generic words dropped from query matching
- `section_limits` — max rows per injected section
- `auto_surface_pending_approvals` — whether to inject pending approvals
  even when user did not explicitly ask

## Known issues / follow-ups

- **`__init__.py` is still ~2000 lines** — partial refactor in
  `modules/` directory is not wired in yet. Hook dispatch + load
  reliability is covered by preflight and `test_hook_dispatch.py`. Full
  split into ≤ 200 line facade + module breakdown is deferred to a
  follow-up iteration.
- **`modules/` contains duplicated dependency-injection-style
  implementations** of helpers that also live in `__init__.py` — only
  `query_classification` and `text_processing` are actually imported from
  `modules/`. The rest is dead until the full refactor is completed.

## Changelog

- **2026-05-17** — intent-gated context surfacing:
  added centralized query intent classification, per-intent section allowlists,
  per-intent section budgets, and regression coverage for greeting suppression,
  recap routing, approval-only prompts, repo lookups, and intent conflicts.

- **2026-05-11** — stabilization pass:
  added `tests/test_hook_dispatch.py` (6 tests);
  added `tests/run_all_tests.sh`;
  removed `__init__.py.backup`;
  removed legacy `test_refactoring.py` (incompatible with current
  `QueryContext`);
  wired into global `plugins_preflight.sh` guard;
  documented known refactor state.
  See `~/.hermes/MIGRATION_NOTES_20260511.md` for full details.
