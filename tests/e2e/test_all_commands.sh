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

expect_error() {
    local label="$1"
    shift
    echo ">>> $label"
    if output=$("$@" 2>&1); then
        echo "$output"
        echo "[FAIL] expected non-zero exit, got success"
        FAILED=$((FAILED + 1))
    else
        if echo "$output" | grep -q '"error"'; then
            echo "$output"
            echo "[OK] non-zero exit with JSON error"
            PASSED=$((PASSED + 1))
        else
            echo "$output"
            echo "[FAIL] non-zero exit but no JSON error"
            FAILED=$((FAILED + 1))
        fi
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
    output=$("$ODDA_BIN" --socket "$SOCKET" tabs list)
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
LAST_TAB_ID=""

open_browser() {
    local label="$1"
    local output
    output=$("$ODDA_BIN" --socket "$SOCKET" browser open --headless 2>&1)
    echo ">>> $label"
    echo "$output"
    LAST_BROWSER_ID=$(echo "$output" | python3 -c 'import json,sys; print(json.load(sys.stdin)["browser_id"])')
    LAST_TAB_ID=$(echo "$output" | python3 -c 'import json,sys; print(json.load(sys.stdin)["tab_id"])')
    if [ -n "$LAST_BROWSER_ID" ]; then
        echo "[OK] browser id = $LAST_BROWSER_ID, initial tab id = $LAST_TAB_ID"
        PASSED=$((PASSED + 1))
    else
        echo "[FAIL] could not parse browser/tab IDs"
        FAILED=$((FAILED + 1))
    fi
    echo
}

open_tab() {
    local label="$1"
    local browser_id="$2"
    shift 2
    local output
    output=$("$ODDA_BIN" --socket "$SOCKET" tabs open --browser-id "$browser_id" "$@" 2>&1)
    echo ">>> $label"
    echo "$output"
    NEW_TAB_ID=$(echo "$output" | python3 -c 'import json,sys; print(json.load(sys.stdin)["tab_id"])')
    if [ -n "$NEW_TAB_ID" ]; then
        echo "[OK] new tab id = $NEW_TAB_ID"
        PASSED=$((PASSED + 1))
    else
        echo "[FAIL] could not parse tab_id"
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
    cat > "$TMPDIR/listener_http/dialogs.html" <<'EOF'
<!DOCTYPE html>
<html>
<head><title>Dialog Test</title></head>
<body>
<script>
  // Empty page; tests trigger dialogs via odda eval.
</script>
</body>
</html>
EOF
    python3 -m http.server 8766 --bind 127.0.0.1 --directory "$TMPDIR/listener_http" > "$TMPDIR/listener_http_server.log" 2>&1 &
    LISTENER_HTTP_PID=$!
    sleep 1
}

assert_event_listeners() {
    local browser_id="$1"
    local tab_id="$2"
    local output
    output=$("$ODDA_BIN" --socket "$SOCKET" event-listeners --browser-id "$browser_id" --tab-id "$tab_id" 2>&1)
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
TAB_ID=$LAST_TAB_ID
expect_json "tabs list" "$ODDA_BIN" --socket "$SOCKET" tabs list
expect_json "navigate" "$ODDA_BIN" --socket "$SOCKET" navigate http://127.0.0.1:8766/ --browser-id "$BROWSER_ID" --tab-id "$TAB_ID"
expect_json "eval" "$ODDA_BIN" --socket "$SOCKET" eval "document.title" --browser-id "$BROWSER_ID" --tab-id "$TAB_ID"
expect_json "eval --file" "$ODDA_BIN" --socket "$SOCKET" eval --file "$TMPDIR/listener_http/eval.js" --browser-id "$BROWSER_ID" --tab-id "$TAB_ID"
# eval --file error cases
echo ">>> eval with neither inline nor --file should fail"
if "$ODDA_BIN" --socket "$SOCKET" eval --browser-id "$BROWSER_ID" --tab-id "$TAB_ID" 2>&1 | grep -q '"error"'; then
    echo "[OK]"
    PASSED=$((PASSED + 1))
else
    echo "[FAIL] expected error when no JS provided"
    FAILED=$((FAILED + 1))
fi
echo
echo ">>> eval with both inline and --file should fail"
if "$ODDA_BIN" --socket "$SOCKET" eval "1" --file "$TMPDIR/listener_http/eval.js" --browser-id "$BROWSER_ID" --tab-id "$TAB_ID" 2>&1 | grep -q '"error"'; then
    echo "[OK]"
    PASSED=$((PASSED + 1))
else
    echo "[FAIL] expected error when both inline and --file provided"
    FAILED=$((FAILED + 1))
fi
echo
echo ">>> eval --file with missing file should fail"
if "$ODDA_BIN" --socket "$SOCKET" eval --file "$TMPDIR/nonexistent.js" --browser-id "$BROWSER_ID" --tab-id "$TAB_ID" 2>&1 | grep -q '"error"'; then
    echo "[OK]"
    PASSED=$((PASSED + 1))
else
    echo "[FAIL] expected error for missing file"
    FAILED=$((FAILED + 1))
fi
echo
expect_json "screenshot" "$ODDA_BIN" --socket "$SOCKET" screenshot --browser-id "$BROWSER_ID" --tab-id "$TAB_ID"
assert_event_listeners "$BROWSER_ID" "$TAB_ID"

# wait-for tests
echo "=== wait-for tests ==="
# Wait for a condition that's already true
WAIT_OUT=$("$ODDA_BIN" --socket "$SOCKET" wait-for "document.title" --browser-id "$BROWSER_ID" --tab-id "$TAB_ID" --timeout 5 2>&1)
echo ">>> wait-for (already true)"
echo "$WAIT_OUT"
if echo "$WAIT_OUT" | python3 -c 'import json,sys; d=json.load(sys.stdin); exit(0 if d else 1)' 2>/dev/null; then
    echo "[OK] returned truthy value"
    PASSED=$((PASSED + 1))
else
    echo "[FAIL] expected truthy value"
    FAILED=$((FAILED + 1))
fi
echo

# Wait for a condition that becomes true after a delay
# Set a timeout that sets a global after 1s
"$ODDA_BIN" --socket "$SOCKET" eval "setTimeout(() => { window.__waitTest__ = 'arrived'; }, 1000)" --browser-id "$BROWSER_ID" --tab-id "$TAB_ID" > /dev/null 2>&1
WAIT_OUT2=$("$ODDA_BIN" --socket "$SOCKET" wait-for "window.__waitTest__" --browser-id "$BROWSER_ID" --tab-id "$TAB_ID" --timeout 5 2>&1)
echo ">>> wait-for (delayed)"
echo "$WAIT_OUT2"
if echo "$WAIT_OUT2" | grep -q '"arrived"'; then
    echo "[OK] waited for delayed value"
    PASSED=$((PASSED + 1))
else
    echo "[FAIL] expected 'arrived'"
    FAILED=$((FAILED + 1))
fi
echo

# Wait-for timeout (condition never becomes true)
WAIT_OUT3=$("$ODDA_BIN" --socket "$SOCKET" wait-for "window.__never__" --browser-id "$BROWSER_ID" --tab-id "$TAB_ID" --timeout 2 2>&1)
echo ">>> wait-for (timeout)"
echo "$WAIT_OUT3"
if echo "$WAIT_OUT3" | grep -qi "timeout\|error"; then
    echo "[OK] timed out as expected"
    PASSED=$((PASSED + 1))
else
    echo "[FAIL] expected timeout error"
    FAILED=$((FAILED + 1))
fi
echo

# Userscript tests
echo "=== Userscript commands ==="
cat > "$TMPDIR/us_helper.js" <<'EOF'
if (!window.__usHelperRan__) {
  window.__usHelperRan__ = 0;
}
window.__usHelperRan__ += 1;
EOF
expect_json "userscript install" "$ODDA_BIN" --socket "$SOCKET" userscript install --name helper --browser-id "$BROWSER_ID" --file "$TMPDIR/us_helper.js"
expect_json "userscript list" "$ODDA_BIN" --socket "$SOCKET" userscript list
# Navigate and verify the helper ran at document_start
expect_json "navigate (with userscript)" "$ODDA_BIN" --socket "$SOCKET" navigate http://127.0.0.1:8766/ --browser-id "$BROWSER_ID" --tab-id "$TAB_ID"
sleep 1
US_OUT=$("$ODDA_BIN" --socket "$SOCKET" eval "String(window.__usHelperRan__)" --browser-id "$BROWSER_ID" --tab-id "$TAB_ID" 2>&1)
echo ">>> userscript ran after navigate"
echo "$US_OUT"
if [ "$US_OUT" = '"1"' ]; then
    echo "[OK] helper was injected at document_start"
    PASSED=$((PASSED + 1))
else
    echo "[FAIL] expected 1, got $US_OUT"
    FAILED=$((FAILED + 1))
fi
echo
# Navigate again and verify re-injection
expect_json "navigate again (re-inject)" "$ODDA_BIN" --socket "$SOCKET" navigate http://127.0.0.1:8766/ --browser-id "$BROWSER_ID" --tab-id "$TAB_ID"
sleep 1
US_OUT2=$("$ODDA_BIN" --socket "$SOCKET" eval "String(window.__usHelperRan__)" --browser-id "$BROWSER_ID" --tab-id "$TAB_ID" 2>&1)
echo ">>> userscript re-injected after second navigate"
echo "$US_OUT2"
if [ "$US_OUT2" = '"1"' ]; then
    echo "[OK] helper was re-injected"
    PASSED=$((PASSED + 1))
else
    echo "[FAIL] expected 1, got $US_OUT2"
    FAILED=$((FAILED + 1))
fi
echo
# Default dialog interceptor test
echo "=== Default dialog interceptor ==="
expect_json "navigate (dialogs page)" "$ODDA_BIN" --socket "$SOCKET" navigate http://127.0.0.1:8766/dialogs.html --browser-id "$BROWSER_ID" --tab-id "$TAB_ID"
sleep 1
echo ">>> eval default dialog interceptor present"
INTERCEPTOR_OUT=$("$ODDA_BIN" --socket "$SOCKET" eval "String(window.__oddaDialogInterceptorInstalled)" --browser-id "$BROWSER_ID" --tab-id "$TAB_ID" 2>&1)
echo "$INTERCEPTOR_OUT"
if [ "$INTERCEPTOR_OUT" = '"true"' ]; then
    echo "[OK] dialog interceptor installed"
    PASSED=$((PASSED + 1))
else
    echo "[FAIL] expected true, got $INTERCEPTOR_OUT"
    FAILED=$((FAILED + 1))
fi
echo
echo ">>> trigger print (should not block)"
PRINT_OUT=$("$ODDA_BIN" --socket "$SOCKET" eval "window.print(); 'print-ok'" --browser-id "$BROWSER_ID" --tab-id "$TAB_ID" 2>&1)
echo "$PRINT_OUT"
if [ "$PRINT_OUT" = '"print-ok"' ]; then
    echo "[OK] print did not block"
    PASSED=$((PASSED + 1))
else
    echo "[FAIL] expected print-ok, got $PRINT_OUT"
    FAILED=$((FAILED + 1))
fi
echo
echo ">>> trigger alert/confirm/prompt and check __oddaDialogs"
ALERT_OUT=$("$ODDA_BIN" --socket "$SOCKET" eval "window.alert('alert-msg'); 'alert-ok'" --browser-id "$BROWSER_ID" --tab-id "$TAB_ID" 2>&1)
echo "$ALERT_OUT"
CONFIRM_OUT=$("$ODDA_BIN" --socket "$SOCKET" eval "window.confirm('confirm-msg'); 'confirm-ok'" --browser-id "$BROWSER_ID" --tab-id "$TAB_ID" 2>&1)
echo "$CONFIRM_OUT"
PROMPT_OUT=$("$ODDA_BIN" --socket "$SOCKET" eval "window.prompt('prompt-msg', 'prompt-default'); 'prompt-ok'" --browser-id "$BROWSER_ID" --tab-id "$TAB_ID" 2>&1)
echo "$PROMPT_OUT"
echo ">>> check __oddaDialogs entries"
DIALOGS_COMPARE=$("$ODDA_BIN" --socket "$SOCKET" eval "
(() => {
  const actual = window.__oddaDialogs.slice(-4).map(e => ({type: e.type, message: e.message, defaultValue: e.defaultValue}));
  const expected = [
    {type: 'print'},
    {type: 'alert', message: 'alert-msg'},
    {type: 'confirm', message: 'confirm-msg'},
    {type: 'prompt', message: 'prompt-msg', defaultValue: 'prompt-default'},
  ];
  return JSON.stringify(actual) === JSON.stringify(expected) ? 'OK' : 'DIFF: ' + JSON.stringify(actual);
})()
" --browser-id "$BROWSER_ID" --tab-id "$TAB_ID" 2>&1)
echo "$DIALOGS_COMPARE"
if [ "$DIALOGS_COMPARE" = '"OK"' ]; then
    echo "[OK] captured print/alert/confirm/prompt in order"
    PASSED=$((PASSED + 1))
else
    echo "[FAIL] dialog list mismatch"
    FAILED=$((FAILED + 1))
fi
echo

# Remove and verify it's gone after navigate
expect_json "userscript remove" "$ODDA_BIN" --socket "$SOCKET" userscript remove helper --browser-id "$BROWSER_ID"
expect_json "userscript list (empty)" "$ODDA_BIN" --socket "$SOCKET" userscript list
expect_json "navigate (no userscript)" "$ODDA_BIN" --socket "$SOCKET" navigate http://127.0.0.1:8766/ --browser-id "$BROWSER_ID" --tab-id "$TAB_ID"
sleep 1
US_OUT3=$("$ODDA_BIN" --socket "$SOCKET" eval "String(typeof window.__usHelperRan__)" --browser-id "$BROWSER_ID" --tab-id "$TAB_ID" 2>&1)
echo ">>> userscript gone after remove + navigate"
echo "$US_OUT3"
if [ "$US_OUT3" = '"undefined"' ]; then
    echo "[OK] helper not injected after remove"
    PASSED=$((PASSED + 1))
else
    echo "[FAIL] expected undefined, got $US_OUT3"
    FAILED=$((FAILED + 1))
fi
echo

# === Targeting model tests ===
echo "=== Targeting model ==="

# Bad browser_id errors with non-zero exit + JSON error
expect_error "navigate bad browser_id" "$ODDA_BIN" --socket "$SOCKET" navigate http://x --browser-id 9999 --tab-id 1
# Bad tab_id errors
expect_error "navigate bad tab_id" "$ODDA_BIN" --socket "$SOCKET" navigate http://x --browser-id "$BROWSER_ID" --tab-id 9999
# eval bad tab_id errors
expect_error "eval bad tab_id" "$ODDA_BIN" --socket "$SOCKET" eval "1" --browser-id "$BROWSER_ID" --tab-id 9999
# screenshot bad browser_id errors
expect_error "screenshot bad browser_id" "$ODDA_BIN" --socket "$SOCKET" screenshot --browser-id 9999 --tab-id 1
# tabs open bad browser_id errors
expect_error "tabs open bad browser_id" "$ODDA_BIN" --socket "$SOCKET" tabs open --browser-id 9999
# tabs close bad tab_id errors
expect_error "tabs close bad tab_id" "$ODDA_BIN" --socket "$SOCKET" tabs close --browser-id "$BROWSER_ID" --tab-id 9999
# event-listeners bad browser_id errors
expect_error "event-listeners bad browser_id" "$ODDA_BIN" --socket "$SOCKET" event-listeners --browser-id 9999 --tab-id 1

# === Tab lifecycle tests ===
echo "=== Tab lifecycle ==="

# Open a new tab (with url) and get its tab_id
open_tab "tabs open (url)" "$BROWSER_ID" --url http://127.0.0.1:8766/
TAB2=$NEW_TAB_ID
assert_tab_count 2

# Open a blank tab
open_tab "tabs open (blank)" "$BROWSER_ID"
TAB3=$NEW_TAB_ID
assert_tab_count 3

# Close one tab and verify tab count drops
expect_json "tabs close" "$ODDA_BIN" --socket "$SOCKET" tabs close --browser-id "$BROWSER_ID" --tab-id "$TAB2"
assert_tab_count 2

# Closed tab_id errors on subsequent eval
expect_error "eval on closed tab" "$ODDA_BIN" --socket "$SOCKET" eval "1" --browser-id "$BROWSER_ID" --tab-id "$TAB2"

# Monotonic: new tab after closes should have a strictly higher id than TAB3
open_tab "tabs open (after closes)" "$BROWSER_ID" --url http://127.0.0.1:8766/
TAB4=$NEW_TAB_ID
echo ">>> monotonic tab_id (TAB4=$TAB4 > TAB3=$TAB3)"
if [ "$TAB4" -gt "$TAB3" ]; then
    echo "[OK] new tab_id is strictly higher than previous max"
    PASSED=$((PASSED + 1))
else
    echo "[FAIL] tab_id was reused or did not increase (TAB4=$TAB4, TAB3=$TAB3)"
    FAILED=$((FAILED + 1))
fi
echo

# Closed tab_id is not reused: TAB2 should still error
expect_error "closed tab_id not reused (TAB2)" "$ODDA_BIN" --socket "$SOCKET" eval "1" --browser-id "$BROWSER_ID" --tab-id "$TAB2"

# Close all tabs: browser stays alive with zero tabs
"$ODDA_BIN" --socket "$SOCKET" tabs close --browser-id "$BROWSER_ID" --tab-id "$TAB_ID" > /dev/null 2>&1
"$ODDA_BIN" --socket "$SOCKET" tabs close --browser-id "$BROWSER_ID" --tab-id "$TAB3" > /dev/null 2>&1
"$ODDA_BIN" --socket "$SOCKET" tabs close --browser-id "$BROWSER_ID" --tab-id "$TAB4" > /dev/null 2>&1
echo ">>> after closing all tabs, tabs list shows empty tabs array"
ZERO_OUT=$("$ODDA_BIN" --socket "$SOCKET" tabs list --browser-id "$BROWSER_ID" 2>&1)
echo "$ZERO_OUT"
ZERO_COUNT=$(echo "$ZERO_OUT" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)[0]["tabs"]))')
if [ "$ZERO_COUNT" -eq 0 ]; then
    echo "[OK] browser alive with zero tabs"
    PASSED=$((PASSED + 1))
else
    echo "[FAIL] expected 0 tabs, got $ZERO_COUNT"
    FAILED=$((FAILED + 1))
fi
echo

# Browser can still open a tab after zero tabs
open_tab "tabs open after zero tabs" "$BROWSER_ID" --url http://127.0.0.1:8766/
assert_tab_count 1

# Clean up this browser before the multi-browser section
expect_json "browser close" "$ODDA_BIN" --socket "$SOCKET" browser close "$BROWSER_ID"

# === Multiple browsers ===
echo "=== Multiple browsers ==="
open_browser "browser open (multi 1)"
BROWSER_ID_MULTI_1=$LAST_BROWSER_ID
TAB_MULTI_1=$LAST_TAB_ID
expect_json "navigate browser 1 tab" "$ODDA_BIN" --socket "$SOCKET" navigate https://example.org --browser-id "$BROWSER_ID_MULTI_1" --tab-id "$TAB_MULTI_1"
assert_tab_count 1
open_browser "browser open (multi 2)"
BROWSER_ID_MULTI_2=$LAST_BROWSER_ID
assert_browser_count 2
# Isolation: operating on browser 1 doesn't affect browser 2
expect_json "eval browser 1" "$ODDA_BIN" --socket "$SOCKET" eval "document.title" --browser-id "$BROWSER_ID_MULTI_1" --tab-id "$TAB_MULTI_1"
expect_json "browser close second" "$ODDA_BIN" --socket "$SOCKET" browser close "$BROWSER_ID_MULTI_2"
expect_json "browser close first multi" "$ODDA_BIN" --socket "$SOCKET" browser close "$BROWSER_ID_MULTI_1"

# === browser_id is monotonic / not reused ===
echo "=== browser_id not reused ==="
expect_error "closed browser_id errors (not reused)" "$ODDA_BIN" --socket "$SOCKET" navigate http://x --browser-id "$BROWSER_ID_MULTI_1" --tab-id 1

# Generate a captured flow through the proxy
echo "=== Capturing an HTTP flow through the proxy ==="
python3 -m http.server 8765 --bind 127.0.0.1 > "$TMPDIR/http_server.log" 2>&1 &
HTTP_PID=$!
sleep 1

if curl -s -x "$(proxy_url)" http://127.0.0.1:8765/ > "$TMPDIR/curl_output.html" 2>&1; then
    echo "curl through proxy succeeded"
    PASSED=$((PASSED + 1))
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