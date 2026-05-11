#!/usr/bin/env bash
# plugins_preflight.sh — local guard for live_brain + live_brain_ctx
#
# Runs before a gateway reload / systemd restart. Exits 0 if all checks pass,
# 1 otherwise. Safe to run anytime — does NOT touch production DB or gateway.
#
# Usage:
#   bash ~/.hermes/scripts/plugins_preflight.sh
#
# Add to your restart flow:
#   bash ~/.hermes/scripts/plugins_preflight.sh && systemctl --user restart hermes-gateway
#
# Checks:
#   1. py_compile on all .py files in both plugins
#   2. Module-import smoke (catches SyntaxError / ImportError at load time)
#   3. Migration dry-run on a temp copy of the live DB
#   4. Run plugin tests/run_all_tests.sh with timeout

set -u

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PLUGIN_DIR="$HERMES_HOME/plugins"
VENV_PY="$HERMES_HOME/hermes-agent/venv/bin/python"
LIVE_DB="$HERMES_HOME/live_brain/live_brain.db"
TMP_DB="/tmp/lb_preflight_$$.db"

# Colors (fallback to plain if not a TTY)
if [ -t 1 ]; then
    GREEN='\033[0;32m'
    RED='\033[0;31m'
    YELLOW='\033[0;33m'
    RESET='\033[0m'
else
    GREEN=''; RED=''; YELLOW=''; RESET=''
fi

FAILED=0

section() {
    echo ""
    echo "=== $1 ==="
}

ok() {
    echo -e "  ${GREEN}✓${RESET} $1"
}

fail() {
    echo -e "  ${RED}✗${RESET} $1"
    FAILED=$((FAILED + 1))
}

warn() {
    echo -e "  ${YELLOW}!${RESET} $1"
}

cleanup() {
    rm -f "$TMP_DB" "${TMP_DB}-shm" "${TMP_DB}-wal"
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Check 1: py_compile
# ---------------------------------------------------------------------------
section "1. py_compile (syntax check)"

if [ ! -x "$VENV_PY" ]; then
    fail "Hermes venv python not found at $VENV_PY"
    exit 1
fi

for plugin in live_brain live_brain_ctx; do
    plugin_path="$PLUGIN_DIR/$plugin"
    if [ ! -d "$plugin_path" ]; then
        fail "Plugin dir missing: $plugin_path"
        continue
    fi
    # Use -q for quiet; exit status tells us the result
    if output=$("$VENV_PY" -m py_compile $(find "$plugin_path" -name "*.py" -not -path "*__pycache__*" 2>/dev/null) 2>&1); then
        count=$(find "$plugin_path" -name "*.py" -not -path "*__pycache__*" | wc -l)
        ok "$plugin: $count .py files compiled"
    else
        fail "$plugin: py_compile failed"
        echo "$output" | head -5 | sed 's/^/    /'
    fi
done

# ---------------------------------------------------------------------------
# Check 2: Module import smoke
# ---------------------------------------------------------------------------
section "2. Module import smoke"

"$VENV_PY" - <<PY 2>&1 | while read line; do echo "  $line"; done
import sys, traceback
sys.path.insert(0, "$PLUGIN_DIR")

# live_brain: can the provider class be imported?
try:
    from live_brain import LiveBrainProvider
    print(f"\033[0;32m✓\033[0m live_brain.LiveBrainProvider imports")
except Exception:
    print(f"\033[0;31m✗\033[0m live_brain import failed:")
    traceback.print_exc(limit=3)
    sys.exit(1)

# live_brain_ctx: can the module be imported + register() symbol exists?
try:
    import live_brain_ctx
    if not hasattr(live_brain_ctx, 'register'):
        print(f"\033[0;31m✗\033[0m live_brain_ctx loaded but has no register() function")
        sys.exit(1)
    print(f"\033[0;32m✓\033[0m live_brain_ctx.register imports")
except Exception:
    print(f"\033[0;31m✗\033[0m live_brain_ctx import failed:")
    traceback.print_exc(limit=3)
    sys.exit(1)
PY
if [ $? -ne 0 ]; then
    FAILED=$((FAILED + 1))
fi

# ---------------------------------------------------------------------------
# Check 3: Migration dry-run on temp DB copy
# ---------------------------------------------------------------------------
section "3. Migration dry-run (temp DB copy)"

if [ ! -f "$LIVE_DB" ]; then
    warn "Live DB not found at $LIVE_DB — skipping dry-run"
else
    # SQLite online backup is safe even if gateway is running
    if sqlite3 "$LIVE_DB" ".backup $TMP_DB" 2>/dev/null; then
        ok "Temp DB copy created ($(du -h "$TMP_DB" | cut -f1))"

        # Run LiveBrainStore.initialize_schema over the copy
        "$VENV_PY" - <<PY 2>&1 | while read line; do echo "  $line"; done
import sys
sys.path.insert(0, "$PLUGIN_DIR")
from live_brain.store import LiveBrainStore
try:
    store = LiveBrainStore("$TMP_DB")
    store.initialize_schema()
    rows = store.conn.execute(
        "SELECT migration_id FROM schema_migrations WHERE migration_id LIKE 'FAILED:%'"
    ).fetchall()
    if rows:
        print(f"\033[0;31m✗\033[0m FAILED migrations found: {[r[0] for r in rows]}")
        sys.exit(1)
    print(f"\033[0;32m✓\033[0m initialize_schema clean on temp DB")
except Exception as e:
    print(f"\033[0;31m✗\033[0m initialize_schema raised: {type(e).__name__}: {e}")
    sys.exit(1)
PY
        if [ $? -ne 0 ]; then
            FAILED=$((FAILED + 1))
        fi
    else
        fail "Could not create temp DB copy"
    fi
fi

# ---------------------------------------------------------------------------
# Check 4: Plugin test suites
# ---------------------------------------------------------------------------
section "4. Plugin test suites"

for plugin in live_brain live_brain_ctx; do
    tests_dir="$PLUGIN_DIR/$plugin/tests"
    runner="$tests_dir/run_all_tests.sh"
    if [ ! -x "$runner" ]; then
        if [ -d "$tests_dir" ] && ls "$tests_dir"/test_*.py >/dev/null 2>&1; then
            # Fallback: run each test_*.py manually with 30s timeout
            failed_tests=0
            for test_file in "$tests_dir"/test_*.py; do
                if ! timeout 30 "$VENV_PY" "$test_file" >/dev/null 2>&1; then
                    failed_tests=$((failed_tests + 1))
                fi
            done
            if [ $failed_tests -eq 0 ]; then
                ok "$plugin: all test_*.py pass (no run_all_tests.sh)"
            else
                fail "$plugin: $failed_tests test file(s) failed"
            fi
        else
            warn "$plugin: no tests/ directory or no test_*.py files"
        fi
        continue
    fi

    if timeout 30 bash "$runner" >/dev/null 2>&1; then
        ok "$plugin: tests/run_all_tests.sh passed"
    else
        fail "$plugin: tests/run_all_tests.sh FAILED or timed out"
        # Re-run visible for debugging
        echo "    (re-running for diagnostics)"
        timeout 30 bash "$runner" 2>&1 | tail -10 | sed 's/^/    /'
    fi
done

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
if [ $FAILED -eq 0 ]; then
    echo -e "${GREEN}All preflight checks passed. Safe to restart gateway.${RESET}"
    exit 0
else
    echo -e "${RED}Preflight FAILED: $FAILED check(s) failed. Do NOT restart gateway.${RESET}"
    exit 1
fi
