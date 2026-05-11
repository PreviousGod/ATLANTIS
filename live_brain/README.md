# live_brain — Hermes Memory Provider

Local, provider-based live brain for Hermes with typed episodic, temporal, and
causal memory. Registers a `LiveBrainProvider(MemoryProvider)` and 15
`brain_*` tools for the agent.

## Status (2026-05-11)

Production-ready after stabilization pass:

- Migration 006 FTS5 bug fixed; provider initializes cleanly.
- Single source of truth for migrations (`SchemaManager.run_migrations`),
  with graceful degradation (broken migration no longer crashes provider init;
  it is recorded as `FAILED:<id>` and skipped on subsequent restarts).
- Integration test (`tests/test_store_integration.py`) exercises migrations +
  FTS5 + Ingestor + RetrievalRouter + CausalManager + SelfEvolutionManager on
  a fresh temp DB, including a read-only sanity check against the live
  production DB.

## Architecture

```
LiveBrainProvider              (MemoryProvider entrypoint)
  └─ LiveBrainStore            (LockedConnection + ConnectionPool)
       ├─ SchemaManager        (migrations runner, FAILED tracking)
       ├─ BackupManager        (SQLite online backup)
       ├─ MaintenanceManager   (scheduled cleanup, hygiene)
       └─ Domain managers:
            Ingestor, RetrievalRouter, CausalManager, EpistemicManager,
            RealityEngine, RuleEngine, ArtifactRegistry, SelfEvolutionManager,
            DialecticEngine, EntityGraph, UserAlignmentTracker,
            CompositionEngine, CompressionManager, ResearchManager
       + SQLite DB at $HERMES_HOME/live_brain/live_brain.db
```

## Tools registered

| Tool | Purpose |
|---|---|
| `brain_state_debug` | Inspect work_state for a scope_key |
| `brain_reality_debug` | Inspect reality engine state + action gate |
| `brain_recap` | Summarize recent work items from live brain |
| `brain_mark_belief` | Create/update causal beliefs |
| `brain_recall` | Query live brain by natural language |
| `brain_research` | Plan or record bounded research |
| `brain_epistemic` | Autonomous research (status/search_web/record_source/record_fact) |
| `brain_resolve_artifact` | Resolve verified project artifact path |
| `brain_mark_artifact` | Register verified/deprecated/rejected artifacts |
| `brain_list_artifacts` | List verified project artifacts |
| `brain_self_evolution` | Propose / list / decide gated self-evolution proposals |
| `brain_entity_graph` | Traverse entity relationship graph |
| `brain_synthesize` | Cross-session dialectic synthesis |
| `brain_user_profile` | View/update user preferences and patterns |
| `brain_compose_query` | Algebraic compositional queries (A + B − C) |

## Running tests

```bash
# All plugin tests
bash ~/.hermes/plugins/live_brain/tests/run_all_tests.sh

# Integration canary (safe — read-only against prod DB)
~/.hermes/hermes-agent/venv/bin/python \
  ~/.hermes/plugins/live_brain/tests/test_store_integration.py

# Migration graceful-degradation test
~/.hermes/hermes-agent/venv/bin/python \
  ~/.hermes/plugins/live_brain/tests/test_migrations.py
```

## Preflight before gateway restart

**Run this before every `systemctl --user restart hermes-gateway`:**

```bash
bash ~/.hermes/scripts/plugins_preflight.sh && systemctl --user restart hermes-gateway
```

The preflight does:
1. `py_compile` on all plugin `.py` files (catches SyntaxError)
2. Import smoke for `LiveBrainProvider` and `live_brain_ctx.register`
3. Migration dry-run over a throw-away copy of the live DB
4. Full test suite with 30s timeout

Exit code 0 means safe; exit 1 means block the restart.

## Backup & restore

Backups are stored as:
- Online SQLite `.backup`: `~/.hermes/live_brain/live_brain.db.backup_<label>_<ts>`
- Plugin source tar: `~/.hermes/plugins_backup/live_brain_<label>_<ts>.tar.gz`

Restore:

```bash
systemctl --user stop hermes-gateway
cp ~/.hermes/live_brain/live_brain.db.backup_<label>_<ts> \
   ~/.hermes/live_brain/live_brain.db
cd ~/.hermes/plugins && rm -rf live_brain live_brain_ctx
tar xzf ~/.hermes/plugins_backup/live_brain_<label>_<ts>.tar.gz
systemctl --user start hermes-gateway
```

## Dependencies

Runtime (installed into Hermes venv):
- `duckduckgo_search>=6.0.0` — used by epistemic layer for web research
- `tiktoken>=0.5.0` — token-accurate context budgeting (char/4 fallback)

See `requirements.txt` for pin definitions.

## Schema migrations

Migrations live in `migrations/` and are applied in lexicographic order by
`SchemaManager.run_migrations()`. Each applied migration is recorded in
`schema_migrations`. If a migration fails, a `FAILED:<migration_id>` sentinel
is recorded to prevent restart-loop retries. To re-try a fixed migration,
delete its `FAILED:` marker:

```sql
DELETE FROM schema_migrations WHERE migration_id = 'FAILED:<name>';
```

Current migrations:
- `audit_spine_v1` — audit log spine (applied inline, pre-framework)
- `001_extraction_method` — manual vs automatic fact extraction
- `002_entity_relationships` — entity graph edges
- `003_dialectic_syntheses` — cross-session reasoning cache
- `004_user_profiles` — user preferences + communication patterns
- `005_concept_vectors` — compositional query vectors
- `006_fts5_search` — FTS5 virtual tables for `fix_recipes` and `causal_activations`

## Known issues / follow-ups

- **Full modularization of `live_brain_ctx/__init__.py`** — still monolithic
  (~2000 lines). Hook dispatch + register is covered by preflight + test
  suite; incremental module extraction is a follow-up.
- **Sync thread backpressure** — `sync_turn` spawns a daemon thread per turn
  with no queue cap. Replace with a bounded `ThreadPoolExecutor` in follow-up.
- **Inline DDL in `initialize_schema`** — predates the migrations framework.
  All future schema changes must go through `migrations/`; inline block
  should be gradually migrated.
- **`duckduckgo_search` renamed to `ddgs`** upstream — we still import the
  old name via compatibility alias. Migrate imports when convenient.

## Changelog

- **2026-05-11** — production-readiness pass:
  migration 006 FTS5 rewrite (rowid reserved name);
  `_run_migrations` single source of truth with graceful degradation;
  `duckduckgo_search` + `tiktoken` installed;
  new preflight guard (`~/.hermes/scripts/plugins_preflight.sh`);
  integration test added (`tests/test_store_integration.py`);
  plugins_backup cleanup (5.6 MB → 1.6 MB);
  stale `cpython-314` pyc removed (runtime is 3.11).
  See `~/.hermes/MIGRATION_NOTES_20260511.md` for full details.
