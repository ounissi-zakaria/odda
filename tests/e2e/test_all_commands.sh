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
    if [ -n "${LISTENER_HTTP_PID:-}" ]; then
        kill "$LISTENER_HTTP_PID" 2>/dev/null || true
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
    "$ODDA_BIN" --socket "$SOCKET" proxy-url
}

assert_tab_count() {
    local expected="$1"
    local output
    output=$("$ODDA_BIN" --socket "$SOCKET" tabs list)
    local count
    count=$(echo "$output" | python3 -c 'import json,sys; print(sum(len(b["tabs"]) for b in json.load(sys.stdin)))')
    if [ "$count" -eq "$expected" ]; then
        echo "[OK] tab count = $expected"
        PASSED=$((PASSED + 1))
    else
        echo "[FAIL] expected $expected tabs, got $count"
        FAILED=$((FAILED + 1))
    fi
    echo
}

assert_browser_count() {
    local expected="$1"
    local output
    output=$("$ODDA_BIN" --socket "$SOCKET" browser list)
    local count
    count=$(echo "$output" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')
    if [ "$count" -eq "$expected" ]; then
        echo "[OK] browser count = $expected"
        PASSED=$((PASSED + 1))
    else
        echo "[FAIL] expected $expected browsers, got $count"
        FAILED=$((FAILED + 1))
    fi
    echo
}

LAST_BROWSER_ID=""

open_browser() {
    local label="$1"
    local output
    output=$("$ODDA_BIN" --socket "$SOCKET" browser open 2>&1)
    echo ">>> $label"
    echo "$output"
    LAST_BROWSER_ID=$(echo "$output" | python3 -c 'import json,sys; print(json.load(sys.stdin).split()[1])')
    if [ -n "$LAST_BROWSER_ID" ]; then
        echo "[OK] browser id = $LAST_BROWSER_ID"
        PASSED=$((PASSED + 1))
    else
        echo "[FAIL] could not parse browser ID"
        FAILED=$((FAILED + 1))
    fi
    echo
}

start_listener_http_server() {
    mkdir -p "$TMPDIR/listener_http"
    cat > "$TMPDIR/listener_http/index.html" <<'EOF'
<!DOCTYPE html>
<html>
<head><title>Listener Test</title></head>
<body>
<button id="btn">Click</button>
<script>
  window.addEventListener('resize', function windowResizeHandler() {});
  document.addEventListener('scroll', function documentScrollHandler() {});
</script>
</body>
</html>
EOF
    cat > "$TMPDIR/listener_http/eval.js" <<'EOF'
// multi-line script with comments
(() => {
  const title = document.title;
  return { title, ok: true };
})()
EOF
    python3 -m http.server 8766 --bind 127.0.0.1 --directory "$TMPDIR/listener_http" > "$TMPDIR/listener_http_server.log" 2>&1 &
    LISTENER_HTTP_PID=$!
    sleep 1
}

assert_event_listeners() {
    local output
    output=$("$ODDA_BIN" --socket "$SOCKET" event-listeners 2>&1)
    echo ">>> event-listeners (with listeners)"
    echo "$output"
    local count
    count=$(echo "$output" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')
    local has_resize
    has_resize=$(echo "$output" | python3 -c 'import json,sys; print(any(l.get("type") == "resize" for l in json.load(sys.stdin)))')
    local has_scroll
    has_scroll=$(echo "$output" | python3 -c 'import json,sys; print(any(l.get("type") == "scroll" for l in json.load(sys.stdin)))')
    if [ "$count" -ge 2 ] && [ "$has_resize" = "True" ] && [ "$has_scroll" = "True" ]; then
        echo "[OK] found resize and scroll listeners"
        PASSED=$((PASSED + 1))
    else
        echo "[FAIL] expected >=2 listeners with resize and scroll, got $count"
        FAILED=$((FAILED + 1))
    fi
    echo
}

start_server

# Core commands
expect_json "version" "$ODDA_BIN" --socket "$SOCKET" version
expect_json "status" "$ODDA_BIN" --socket "$SOCKET" status
run_cmd "proxy-url" "$ODDA_BIN" --socket "$SOCKET" proxy-url
expect_json "logs" "$ODDA_BIN" --socket "$SOCKET" logs --n 5

# Flow capture (file layout)
echo "=== Flow file layout (empty) ==="
if [ ! -e "$DATA_DIR/flows/flows.jsonl" ]; then
    echo "[OK] flows.jsonl absent before any capture"
    PASSED=$((PASSED + 1))
else
    echo "[FAIL] flows.jsonl exists before any capture"
    FAILED=$((FAILED + 1))
fi
echo

# Browser commands
echo "=== Browser commands ==="
start_listener_http_server
open_browser "browser open"
BROWSER_ID=$LAST_BROWSER_ID
expect_json "browser list" "$ODDA_BIN" --socket "$SOCKET" browser list
expect_json "tabs list" "$ODDA_BIN" --socket "$SOCKET" tabs list
expect_json "navigate" "$ODDA_BIN" --socket "$SOCKET" navigate http://127.0.0.1:8766/
expect_json "eval" "$ODDA_BIN" --socket "$SOCKET" eval "document.title"
expect_json "eval --file" "$ODDA_BIN" --socket "$SOCKET" eval --file "$TMPDIR/listener_http/eval.js"
# eval --file error cases
echo ">>> eval with neither inline nor --file should fail"
if "$ODDA_BIN" --socket "$SOCKET" eval 2>&1 | grep -q '"error"'; then
    echo "[OK]"
    PASSED=$((PASSED + 1))
else
    echo "[FAIL] expected error when no JS provided"
    FAILED=$((FAILED + 1))
fi
echo
echo ">>> eval with both inline and --file should fail"
if "$ODDA_BIN" --socket "$SOCKET" eval "1" --file "$TMPDIR/listener_http/eval.js" 2>&1 | grep -q '"error"'; then
    echo "[OK]"
    PASSED=$((PASSED + 1))
else
    echo "[FAIL] expected error when both inline and --file provided"
    FAILED=$((FAILED + 1))
fi
echo
echo ">>> eval --file with missing file should fail"
if "$ODDA_BIN" --socket "$SOCKET" eval --file "$TMPDIR/nonexistent.js" 2>&1 | grep -q '"error"'; then
    echo "[OK]"
    PASSED=$((PASSED + 1))
else
    echo "[FAIL] expected error for missing file"
    FAILED=$((FAILED + 1))
fi
echo
expect_json "screenshot" "$ODDA_BIN" --socket "$SOCKET" screenshot
assert_event_listeners
expect_json "switch-tab" "$ODDA_BIN" --socket "$SOCKET" switch-tab --browser-id "$BROWSER_ID" --index 0
expect_json "browser close" "$ODDA_BIN" --socket "$SOCKET" browser close "$BROWSER_ID"

# Multiple tabs and browsers
open_browser "browser open (multi)"
BROWSER_ID_MULTI_1=$LAST_BROWSER_ID
expect_json "navigate new-tab" "$ODDA_BIN" --socket "$SOCKET" navigate https://example.org --new-tab
assert_tab_count 2
open_browser "browser open second"
BROWSER_ID_MULTI_2=$LAST_BROWSER_ID
assert_browser_count 2
expect_json "browser close second" "$ODDA_BIN" --socket "$SOCKET" browser close "$BROWSER_ID_MULTI_2"
expect_json "browser close first multi" "$ODDA_BIN" --socket "$SOCKET" browser close "$BROWSER_ID_MULTI_1"

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

# Flow file layout (with captured data)
echo "=== Flow file layout (captured) ==="
JSONL="$DATA_DIR/flows/flows.jsonl"
if [ -e "$JSONL" ] && [ -s "$JSONL" ]; then
    echo "[OK] flows.jsonl exists and is non-empty"
    PASSED=$((PASSED + 1))
else
    echo "[FAIL] flows.jsonl missing or empty"
    FAILED=$((FAILED + 1))
fi

LINE_COUNT=$(wc -l < "$JSONL" 2>/dev/null || echo 0)
if [ "$LINE_COUNT" -ge 1 ]; then
    echo "[OK] flows.jsonl has >= 1 line (got $LINE_COUNT)"
    PASSED=$((PASSED + 1))
else
    echo "[FAIL] flows.jsonl has no lines"
    FAILED=$((FAILED + 1))
fi

# Find the curl's flow (host 127.0.0.1, path /). Chrome's background traffic
# goes through the proxy too, so flow 00001 is not necessarily our curl request.
CURL_ID=$(grep '"host": "127.0.0.1"' "$JSONL" | head -n 1 | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' 2>/dev/null || echo "")
if [ -n "$CURL_ID" ] && [ -d "$DATA_DIR/flows/$CURL_ID" ]; then
    echo "[OK] curl flow dir $DATA_DIR/flows/$CURL_ID exists"
    PASSED=$((PASSED + 1))
else
    echo "[FAIL] curl flow dir for id '$CURL_ID' missing"
    FAILED=$((FAILED + 1))
fi

MISSING=0
for f in request response_headers response_body.html; do
    if [ ! -e "$DATA_DIR/flows/$CURL_ID/$f" ]; then
        echo "[FAIL] missing $DATA_DIR/flows/$CURL_ID/$f"
        MISSING=$((MISSING + 1))
    fi
done
if [ "$MISSING" -eq 0 ]; then
    echo "[OK] request, response_headers, response_body.html all present"
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + MISSING))
fi

# Per-flow files should be read-only (mode 0444)
RO_FAIL=0
for f in request response_headers response_body.html; do
    PERMS=$(stat -c '%a' "$DATA_DIR/flows/$CURL_ID/$f" 2>/dev/null || echo "000")
    if [ "$PERMS" != "444" ]; then
        echo "[FAIL] $f mode is $PERMS, expected 444"
        RO_FAIL=$((RO_FAIL + 1))
    fi
done
if [ "$RO_FAIL" -eq 0 ]; then
    echo "[OK] per-flow files are read-only (0444)"
    PASSED=$((PASSED + 1))
else
    FAILED=$((FAILED + RO_FAIL))
fi

echo

echo "=== Summary ==="
echo "Passed: $PASSED"
echo "Failed: $FAILED"

if [ "$FAILED" -ne 0 ]; then
    echo
    echo "Server log tail:"
    tail -n 30 "$DATA_DIR/server.log"
    exit 1
fi
