# =============================================================================
# Hermes smartness layer — all changes applied 2026-05-28
# Save this file. If a Hermes update overwrites plugins, re-apply:
#   cp -r .hermes/plugins_backup_smart/* .hermes/plugins/
#   cp .hermes/config.yaml.backup_smart .hermes/config.yaml
# =============================================================================
# 
# FILES CHANGED (9 total):
#
# .hermes/config.yaml
#   - hard_stop_enabled: true (was false)
#   - max_turns: 30 (was 90)
#   - gateway_timeout: 600 (was 1800)
#   - terminal.timeout: 120 (was 180)
#   - warn thresholds tightened
#
# .hermes/prefill.json                        [NEW FILE]
#   - Forces LLM to start every response with THINK:/KNOW:/ACT:
#
# plugins/live_brain/__init__.py
#   - _LANE_TOOL_NAMES dict — lane-gated tool visibility
#   - get_tool_schemas() filters by cached lane
#   - _cached_turn_lane() reads from bridge
#   - handle_tool_call() prepends tool result summaries
#   - _summarize_tool_result() — 1-line summary per tool
#
# plugins/live_brain_ctx/modules/hooks.py
#   - _pre_llm_call_inner: user_message assigned BEFORE audit hash (line ~3774)
#   - _build_turn_economy_section() — escalating warnings at 3/8/15 turns
#   - Turn economy injected before assembler
#
# plugins/live_brain_ctx/context_config.json
#   - New section entries: NUCLEUS STUCK, TURN ECONOMY variants
#
# plugins/nucleus/__init__.py
#   - _pre_tool_hook, _post_tool_hook, _post_llm_hook wrapped in try/except
#   - _get_nucleus(): heartbeat start removed
#   - register(): _get_nucleus() eager init removed
#   - _NucleusDegraded: stripped stale attributes
#
# plugins/nucleus/nucleus_engine.py
#   - Heartbeat loop stripped (sensor, entropy, ego, world model, suggester)
#   - Kept: Pargod, Intervention, BrainSync, SessionState, _execute_instinct, _escalate_web
#
# plugins/nucleus/config.py
#   - PARGOD_DB pointed at valid ~/.hermes/nucleus/pargod.db
#
# plugins/nucleus/contributions.py
#   - _detect_stuck_loop() — NUCLEUS STUCK at priority 2
#   - compute_contributions() calls stuck detector before warnings
#
# DB CHANGES:
#   - .hermes/nucleus/pargod.db: added use_count + last_used columns
#   - .hermes/nucleus_data/pargod.db: DELETED (488MB corrupted)
#
# TO RE-APPLY AFTER UPDATE:
#   1. Copy all plugin files from backup
#   2. Restore config.yaml
#   3. Restore prefill.json
#   4. If pargod.db was replaced, run:
#      python3 -c "
#      import sqlite3
#      conn = sqlite3.connect('.hermes/nucleus/pargod.db')
#      conn.execute('ALTER TABLE nodes ADD COLUMN use_count INTEGER DEFAULT 0')
#      conn.execute('ALTER TABLE nodes ADD COLUMN last_used REAL')
#      conn.execute('ALTER TABLE edges ADD COLUMN use_count INTEGER DEFAULT 0')
#      conn.execute('ALTER TABLE edges ADD COLUMN last_used REAL')
#      conn.commit()
#      conn.close()
#      print('Schema fixed')
#      "
#   5. Restart gateway
# =============================================================================
