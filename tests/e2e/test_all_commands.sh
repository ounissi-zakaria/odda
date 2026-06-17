#!/usr/bin/env bash
# End-to-end test for the odda CLI.
#
# Launches an odda server, exercises every CLI command, and reports results.
# Requires Chrome to be installed for browser-related tests.
#
# Environment:
#   ODDA_BIN     Path to the odda executable (default: .venv/bin/odda)
#   TMPDIR       Base directory for the test sandbox (default: /tmp/odda-e2e)

set -u

ODDA_BIN="${ODDA_BIN:-.venv/bin/odda}"
TMPDIR="${TMPDIR:-/tmp/odda-e2e}"
SOCKET="$TMPDIR/odda.sock"
DATA_DIR="$TMPDIR/data"

PASSED=0
FAILED=0

if ! command -v "$ODDA_BIN" > /dev/null 2>&1; then
    echo "odda executable not found: $ODDA_BIN"
    exit 1
fi

# Clean up any previous run
rm -rf "$TMPDIR"
mkdir -p "$DATA_DIR"

cleanup() {
    if [ -n "${SERVER_PID:-}" ]; then
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
    fi
    if [ -n "${HTTP_PID:-}" ]; then
        kill "$HTTP_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

start_server() {
    echo "=== Starting odda server ==="
    "$ODDA_BIN" server --socket "$SOCKET" --data-dir "$DATA_DIR" > "$TMPDIR/server.log" 2>&1 &
    SERVER_PID=$!

    local deadline
    deadline=$(($(date +%s) + 10))
    while [ ! -e "$SOCKET" ] && [ "$(date +%s)" -lt "$deadline" ]; do
        sleep 0.1
    done

    if [ ! -e "$SOCKET" ]; then
        echo "Server failed to start"
        tail -n 30 "$TMPDIR/server.log"
        exit 1
    fi
    echo "Server listening on $SOCKET"
}

run_cmd() {
    local label="$1"
    shift
    echo ">>> $label"
    if "$@"; then
        echo "[OK]"
        PASSED=$((PASSED + 1))
    else
        echo "[FAIL] exit=$?"
        FAILED=$((FAILED + 1))
    fi
    echo
}

expect_json() {
    local label="$1"
    shift
    echo ">>> $label"
    if output=$("$@" 2>&1); then
        if echo "$output" | python3 -m json.tool > /dev/null 2>&1; then
            echo "$output"
            echo "[OK]"
            PASSED=$((PASSED + 1))
        else
            echo "$output"
            echo "[FAIL] output is not valid JSON"
            FAILED=$((FAILED + 1))
        fi
    else
        echo "$output"
        echo "[FAIL] exit=$?"
        FAILED=$((FAILED + 1))
    fi
    echo
}

proxy_url() {
    "$ODDA_BIN" --socket "$SOCKET" proxy-url | python3 -c 'import json,sys; print(json.load(sys.stdin))'
}

start_server

# Core commands
expect_json "version" "$ODDA_BIN" --socket "$SOCKET" version
expect_json "status" "$ODDA_BIN" --socket "$SOCKET" status
expect_json "proxy-url" "$ODDA_BIN" --socket "$SOCKET" proxy-url
expect_json "logs" "$ODDA_BIN" --socket "$SOCKET" logs --n 5

# Flow commands (empty database)
expect_json "flows list (empty)" "$ODDA_BIN" --socket "$SOCKET" flows list
expect_json "flows search (empty)" "$ODDA_BIN" --socket "$SOCKET" flows search "SELECT * FROM flows LIMIT 1"

# Browser commands
echo "=== Browser commands ==="
expect_json "browser open" "$ODDA_BIN" --socket "$SOCKET" browser open
expect_json "browser list" "$ODDA_BIN" --socket "$SOCKET" browser list
expect_json "tabs list" "$ODDA_BIN" --socket "$SOCKET" tabs list
expect_json "navigate" "$ODDA_BIN" --socket "$SOCKET" navigate https://example.com
expect_json "eval" "$ODDA_BIN" --socket "$SOCKET" eval "document.title"
expect_json "screenshot" "$ODDA_BIN" --socket "$SOCKET" screenshot
expect_json "console" "$ODDA_BIN" --socket "$SOCKET" console --n 5
expect_json "event-listeners" "$ODDA_BIN" --socket "$SOCKET" event-listeners
expect_json "switch-tab" "$ODDA_BIN" --socket "$SOCKET" switch-tab --browser-id 1 --index 0
expect_json "browser close" "$ODDA_BIN" --socket "$SOCKET" browser close 1

# Generate a captured flow through the proxy
echo "=== Capturing an HTTP flow through the proxy ==="
python3 -m http.server 8765 --bind 127.0.0.1 > "$TMPDIR/http_server.log" 2>&1 &
HTTP_PID=$!
sleep 1

if curl -s -x "$(proxy_url)" http://127.0.0.1:8765/ > "$TMPDIR/curl_output.html" 2>&1; then
    echo "curl through proxy succeeded"
else
    echo "curl through proxy failed"
    FAILED=$((FAILED + 1))
fi
sleep 1
kill "$HTTP_PID" 2>/dev/null || true
HTTP_PID=""

# Flow commands (with captured data)
expect_json "flows list" "$ODDA_BIN" --socket "$SOCKET" flows list
expect_json "flows inspect" "$ODDA_BIN" --socket "$SOCKET" flows inspect 1

echo "=== Summary ==="
echo "Passed: $PASSED"
echo "Failed: $FAILED"

if [ "$FAILED" -ne 0 ]; then
    echo
    echo "Server log tail:"
    tail -n 30 "$DATA_DIR/server.log"
    exit 1
fi
