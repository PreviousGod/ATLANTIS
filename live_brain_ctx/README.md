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

- **2026-05-11** — stabilization pass:
  added `tests/test_hook_dispatch.py` (6 tests);
  added `tests/run_all_tests.sh`;
  removed `__init__.py.backup`;
  removed legacy `test_refactoring.py` (incompatible with current
  `QueryContext`);
  wired into global `plugins_preflight.sh` guard;
  documented known refactor state.
  See `~/.hermes/MIGRATION_NOTES_20260511.md` for full details.
