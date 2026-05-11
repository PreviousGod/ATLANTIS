#!/bin/bash
# Local test runner for Live Brain plugin tests

echo "Running Live Brain Test Suite..."
echo "================================"

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FAILED=0

# Run each test file
for test_file in "$TESTS_DIR"/test_*.py; do
    if [ -f "$test_file" ]; then
        echo ""
        echo "Running $(basename "$test_file")..."
        python3 "$test_file"
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
