#!/bin/bash
# Local test runner for live_brain_ctx plugin tests

echo "Running live_brain_ctx Test Suite..."
echo "================================"

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(dirname "$TESTS_DIR")"
VENV_PY="${HERMES_HOME:-$HOME/.hermes}/hermes-agent/venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
    VENV_PY=python3
fi

FAILED=0
for test_file in "$TESTS_DIR"/test_*.py; do
    if [ -f "$test_file" ]; then
        echo ""
        echo "Running $(basename "$test_file")..."
        "$VENV_PY" "$test_file"
        if [ $? -ne 0 ]; then
            FAILED=$((FAILED + 1))
        fi
    fi
done

echo ""
echo "================================"
if [ $FAILED -eq 0 ]; then
    echo "✅ All test suites passed!"
    exit 0
else
    echo "❌ $FAILED test suite(s) failed"
    exit 1
fi
