# Hermes Live Brain Plugin — Install Tutorial

This package contains the full Live Brain system for Hermes:

- `live_brain/` — memory provider, SQLite store, ingestion, rules, research, causal learning
- `live_brain_ctx/` — context injection plugin with `pre_llm_call` and `post_tool_call` hooks
- `tools/` — metrics, cleanup, backtest, manual recipe promotion, context debug
- `tests/` + `smoke_test.py` — local validation runner

## 1. Unzip the package

```bash
cd /tmp
unzip hermes_live_brain_plugin_20260425.zip
cd live_brain_plugin_package
```

## 2. Backup existing Hermes plugins

Recommended before replacing anything:

```bash
mkdir -p ~/hermes_plugin_backups
cp -a ~/.hermes/plugins/live_brain ~/hermes_plugin_backups/live_brain_$(date +%Y%m%d_%H%M%S) 2>/dev/null || true
cp -a ~/.hermes/plugins/live_brain_ctx ~/hermes_plugin_backups/live_brain_ctx_$(date +%Y%m%d_%H%M%S) 2>/dev/null || true
```

## 3. Install both plugin folders

```bash
mkdir -p ~/.hermes/plugins
rm -rf ~/.hermes/plugins/live_brain ~/.hermes/plugins/live_brain_ctx
cp -a live_brain ~/.hermes/plugins/live_brain
cp -a live_brain_ctx ~/.hermes/plugins/live_brain_ctx
```

After copying, these files should exist:

```text
~/.hermes/plugins/live_brain/__init__.py
~/.hermes/plugins/live_brain/store.py
~/.hermes/plugins/live_brain/ingest.py
~/.hermes/plugins/live_brain/scopes_config.py
~/.hermes/plugins/live_brain/plugin.yaml
~/.hermes/plugins/live_brain_ctx/__init__.py
~/.hermes/plugins/live_brain_ctx/context_config.json
~/.hermes/plugins/live_brain_ctx/plugin.yaml
```

## 4. Configure Hermes memory/context

Edit `~/.hermes/config.yaml` and set Live Brain as the only memory provider:

```yaml
memory:
  provider: live_brain
  memory_enabled: false
  user_profile_enabled: false
```

Important: `memory_enabled: false` and `user_profile_enabled: false` disable Hermes built-in/default memory layers. Keep them off if you want Live Brain to be the single source of durable memory.

`live_brain` is the long-term memory provider. `live_brain_ctx` is the runtime context hook plugin. Install and enable both.

## 5. Disable default memory/context plugins

Live Brain should not run next to another memory/context injector. Otherwise the LLM can receive duplicate or contradictory context.

If your Hermes config has a plugin allowlist, keep Live Brain enabled and remove/disable default memory or default context plugins. Example clean setup:

```yaml
plugins:
  enabled:
    - live_brain
    - live_brain_ctx
    # keep your non-memory tool plugins here
```

Remove or disable entries like these if they exist in your config:

```yaml
# examples only — exact names depend on your Hermes build
- memory
- default_memory
- user_profile
- memory_ctx
- context_memory
- long_term_memory
- default_context
- context_compressor_memory
```

Rule of thumb:

- keep `live_brain` as the memory provider
- keep `live_brain_ctx` as the context/tool-result hook
- disable built-in/default memory
- disable default memory context injection
- keep unrelated non-memory tool plugins enabled

If Hermes has separate context/compressor settings, point them to `live_brain_ctx` or disable the default memory context compressor. Do not enable two memory-context injectors at the same time.

## 6. Optional context tuning

Default context tuning lives in:

```text
~/.hermes/plugins/live_brain_ctx/context_config.json
```

You can override it without editing the plugin by creating:

```text
~/.hermes/live_brain/context_config.json
```

Important knobs:

- `chit_chat_patterns` — exact short messages that should receive no Live Brain context
- `low_signal_words` — generic words ignored during episode/fact overlap checks
- `section_limits` — max rows injected per context section

Keep these conservative. The goal is high-signal context for weak LLMs, not more context.

## 7. Restart Hermes

If you run Hermes through the user service:

```bash
systemctl --user restart hermes-gateway
systemctl --user is-active hermes-gateway
```

Expected output:

```text
active
```

If you run Hermes only from CLI, start a fresh Hermes session instead.

## 8. Verify plugin loading

Check logs:

```bash
grep -i "live_brain\|live_brain_ctx" ~/.hermes/logs/*.log | tail -50
```

The Live Brain database is created at:

```text
~/.hermes/live_brain/live_brain.db
```

## 9. Run smoke tests from the package

From the unpacked `live_brain_plugin_package/` folder:

```bash
python3 smoke_test.py
```

Expected final lines include:

```text
live_brain smoke ok
live_brain eval ok: score=100/100 cases=6
```

## 10. Useful maintenance commands

Run metrics:

```bash
python3 tools/live_brain_recipe_metrics.py --days 30
python3 tools/live_brain_attribution_report.py --days 30
```

Dry-run cleanup and recipe ageing:

```bash
python3 tools/live_brain_cleanup.py --dry-run --age-recipes --archive-stale-review
```

Promote a manually verified recipe:

```bash
python3 tools/live_brain_promote_recipe.py --help
```

Debug injected context:

```bash
python3 tools/live_brain_context_debug.py --help
```

## 11. What good installation looks like

Healthy initial state can still have zero active recipes. That is expected after strict gating.

Look for:

- `tool_results` increasing after real tool calls
- `causal_activations` increasing after successful tool calls
- `recipe_rejections` recording rejected noisy/meta candidates
- `context_impressions` recording injected context and later feedback outcomes
- `precision_ratio` as `null` when there is no feedback sample, not fake `1.0`

## 12. Rollback

To rollback:

```bash
rm -rf ~/.hermes/plugins/live_brain ~/.hermes/plugins/live_brain_ctx
cp -a ~/hermes_plugin_backups/live_brain_YYYYMMDD_HHMMSS ~/.hermes/plugins/live_brain
cp -a ~/hermes_plugin_backups/live_brain_ctx_YYYYMMDD_HHMMSS ~/.hermes/plugins/live_brain_ctx
systemctl --user restart hermes-gateway
```

The database is separate from the plugin code:

```text
~/.hermes/live_brain/live_brain.db
```

Back it up separately if needed.
