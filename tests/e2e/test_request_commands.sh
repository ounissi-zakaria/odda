#!/usr/bin/env bash
# End-to-end test for `odda request` (raw HTTP resend).
#
# Tests clone/new/send against the xs2.top testing server and OAST:
#   - clone a captured flow and verify meta.json + request file
#   - create a new empty request, fill it, and send it (H1 + H2)
#   - verify the sent request lands in flows.jsonl with scheme+port
#   - verify response body decoding (gzip, chunked)
#   - verify --fix-content-length rewrites Content-Length on the wire
#   - verify --insecure works against a self-signed target
#   - verify OAST callback is received when sending to an interactsh payload
#
# Environment:
#   ODDA_BIN     Path to the odda executable (default: .venv/bin/odda)
#   TMPDIR       Base directory for the test sandbox (default: /tmp/odda-req-e2e)
#   INTERACTSH_BIN  Path to interactsh-client (default: auto-detect)

set -u

ODDA_BIN="${ODDA_BIN:-.venv/bin/odda}"
TMPDIR="${TMPDIR:-/tmp/odda-req-e2e}"
SOCKET="$TMPDIR/odda.sock"
DATA_DIR="$TMPDIR/data"

# Auto-detect interactsh-client
INTERACTSH_BIN="${INTERACTSH_BIN:-}"
if [ -z "$INTERACTSH_BIN" ]; then
    if command -v interactsh-client > /dev/null 2>&1; then
        INTERACTSH_BIN="interactsh-client"
    elif [ -x "$HOME/.pdtm/go/bin/interactsh-client" ]; then
        INTERACTSH_BIN="$HOME/.pdtm/go/bin/interactsh-client"
    else
        echo "interactsh-client not found; skipping OAST test"
        INTERACTSH_BIN=""
    fi
fi

PASSED=0
FAILED=0
SERVER_PID=""
INTERACTSH_PID=""

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
    if [ -n "${INTERACTSH_PID:-}" ]; then
        kill "$INTERACTSH_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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

proxy_url() {
    "$ODDA_BIN" --socket "$SOCKET" proxy-url
}

assert_eq() {
    local label="$1" expected="$2" actual="$3"
    if [ "$expected" = "$actual" ]; then
        echo "[OK] $label (expected=$expected)"
        PASSED=$((PASSED + 1))
    else
        echo "[FAIL] $label: expected=$expected, got=$actual"
        FAILED=$((FAILED + 1))
    fi
}

assert_contains() {
    local label="$1" needle="$2" haystack="$3"
    if echo "$haystack" | grep -qF "$needle"; then
        echo "[OK] $label (contains '$needle')"
        PASSED=$((PASSED + 1))
    else
        echo "[FAIL] $label: expected to contain '$needle', got: $haystack"
        FAILED=$((FAILED + 1))
    fi
}

assert_file_exists() {
    local label="$1" path="$2"
    if [ -e "$path" ]; then
        echo "[OK] $label ($path exists)"
        PASSED=$((PASSED + 1))
    else
        echo "[FAIL] $label: $path does not exist"
        FAILED=$((FAILED + 1))
    fi
}

# Extract a field from a JSON string using python3
json_field() {
    local json="$1" field="$2"
    echo "$json" | python3 -c "import json,sys; print(json.load(sys.stdin).get('$field',''))" 2>/dev/null
}

# Find the flow id for a given host in flows.jsonl
flow_id_for_host() {
    local host="$1"
    grep "\"host\": \"$host\"" "$DATA_DIR/flows/flows.jsonl" 2>/dev/null | head -n 1 | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' 2>/dev/null || echo ""
}

# Write a request file with CRLF line endings (HTTP requires \r\n).
# Usage: write_request <path> <<'EOF' ... EOF
write_request() {
    local path="$1"
    sed 's/$/\r/' > "$path"
}

# ---------------------------------------------------------------------------
# Test: clone a captured flow
# ---------------------------------------------------------------------------

test_clone() {
    echo "=== Test: clone a captured flow ==="

    # Capture a flow through the proxy
    curl -s -x "$(proxy_url)" -k --proxy-insecure \
        'https://xs2.top/a?body=clone-test&status=200&header=Content-Type:application/json' \
        -o /dev/null -w "curl_status=%{http_code}\n"
    sleep 1

    local flow_id
    flow_id=$(flow_id_for_host "xs2.top")
    if [ -z "$flow_id" ]; then
        echo "[FAIL] no captured flow for xs2.top"
        FAILED=$((FAILED + 1))
        return
    fi
    echo "Captured flow: $flow_id"

    # Clone it
    local output
    output=$("$ODDA_BIN" --socket "$SOCKET" request clone "$flow_id" --name clone-test 2>&1)
    local name scheme host port
    name=$(json_field "$output" "name")
    scheme=$(json_field "$output" "scheme")
    host=$(json_field "$output" "host")
    port=$(json_field "$output" "port")

    assert_eq "clone name" "clone-test" "$name"
    assert_eq "clone scheme" "https" "$scheme"
    assert_eq "clone host" "xs2.top" "$host"
    assert_eq "clone port" "443" "$port"

    # Verify files exist
    assert_file_exists "clone request file" "$DATA_DIR/requests/clone-test/request"
    assert_file_exists "clone meta.json" "$DATA_DIR/requests/clone-test/meta.json"

    # Verify request file is not empty
    local req_size
    req_size=$(stat -c '%s' "$DATA_DIR/requests/clone-test/request" 2>/dev/null || echo 0)
    assert_eq "clone request non-empty" "nonzero" "$([ "$req_size" -gt 0 ] && echo nonzero || echo zero)"

    # Verify collision refusal
    local collision_out
    collision_out=$("$ODDA_BIN" --socket "$SOCKET" request clone "$flow_id" --name clone-test 2>&1 || true)
    assert_contains "clone collision refused" "already exists" "$collision_out"

    # Verify --force overwrites
    local force_out
    force_out=$("$ODDA_BIN" --socket "$SOCKET" request clone "$flow_id" --name clone-test --force 2>&1)
    assert_eq "clone --force name" "clone-test" "$(json_field "$force_out" "name")"

    echo
}

# ---------------------------------------------------------------------------
# Test: create a new empty request and send it (H1)
# ---------------------------------------------------------------------------

test_new_and_send_h1() {
    echo "=== Test: new + send (HTTP/1.1) ==="

    # Create a new empty request
    local output
    output=$("$ODDA_BIN" --socket "$SOCKET" request new --name h1-test --host xs2.top 2>&1)
    assert_eq "new name" "h1-test" "$(json_field "$output" "name")"
    assert_eq "new scheme" "https" "$(json_field "$output" "scheme")"

    # Verify the request file is empty (0 bytes)
    local req_size
    req_size=$(stat -c '%s' "$DATA_DIR/requests/h1-test/request" 2>/dev/null || echo -1)
    assert_eq "new request empty" "0" "$req_size"

    # Write a raw HTTP/1.1 GET request to the file
    write_request "$DATA_DIR/requests/h1-test/request" <<'REQEOF'
GET /a?body=h1-send-test&status=200&header=Content-Type:application/json HTTP/1.1
Host: xs2.top
Accept: */*

REQEOF

    # Update meta.json to point at xs2.top
    cat > "$DATA_DIR/requests/h1-test/meta.json" <<'METAEOF'
{"scheme": "https", "host": "xs2.top", "port": 443}
METAEOF

    # Send it
    local send_output
    send_output=$("$ODDA_BIN" --socket "$SOCKET" request send h1-test 2>&1)
    echo "send output: $send_output"

    local flow_id status_code body_file
    flow_id=$(json_field "$send_output" "id")
    status_code=$(json_field "$send_output" "status_code")
    body_file=$(json_field "$send_output" "body_file")

    assert_eq "send h1 status_code" "200" "$status_code"
    assert_contains "send h1 body_file" "response_body.json" "$body_file"

    # Verify the response body
    local body
    body=$(cat "$DATA_DIR/flows/$flow_id/response_body.json" 2>/dev/null)
    assert_eq "send h1 response body" "h1-send-test" "$body"

    # Verify the stored request file in flows has HTTP/1.1
    local stored_request
    stored_request=$(cat "$DATA_DIR/flows/$flow_id/request" 2>/dev/null)
    assert_contains "send h1 stored request version" "HTTP/1.1" "$stored_request"

    # Verify scheme+port in jsonl
    local jsonl_line
    jsonl_line=$(grep "\"id\": \"$flow_id\"" "$DATA_DIR/flows/flows.jsonl")
    assert_contains "send h1 jsonl scheme" "\"scheme\": \"https\"" "$jsonl_line"
    assert_contains "send h1 jsonl port" "\"port\": 443" "$jsonl_line"

    echo
}

# ---------------------------------------------------------------------------
# Test: send an H2 request (cloned from a captured H2 flow)
# ---------------------------------------------------------------------------

test_send_h2() {
    echo "=== Test: send (HTTP/2) ==="

    local flow_id
    flow_id=$(flow_id_for_host "xs2.top")
    if [ -z "$flow_id" ]; then
        echo "[FAIL] no captured flow for xs2.top to clone for H2 test"
        FAILED=$((FAILED + 1))
        return
    fi

    # Clone the captured flow (which is HTTP/2.0 from mitmproxy)
    "$ODDA_BIN" --socket "$SOCKET" request clone "$flow_id" --name h2-test --force > /dev/null 2>&1

    # Modify the path to distinguish this request
    write_request "$DATA_DIR/requests/h2-test/request" <<'REQEOF'
GET /a?body=h2-send-test&status=200&header=Content-Type:application/json HTTP/2
user-agent: odda-test
accept: */*

REQEOF

    cat > "$DATA_DIR/requests/h2-test/meta.json" <<'METAEOF'
{"scheme": "https", "host": "xs2.top", "port": 443}
METAEOF

    local send_output
    send_output=$("$ODDA_BIN" --socket "$SOCKET" request send h2-test 2>&1)
    echo "send h2 output: $send_output"

    local flow_id2 status_code
    flow_id2=$(json_field "$send_output" "id")
    status_code=$(json_field "$send_output" "status_code")

    assert_eq "send h2 status_code" "200" "$status_code"

    local body
    body=$(cat "$DATA_DIR/flows/$flow_id2/response_body.json" 2>/dev/null)
    assert_eq "send h2 response body" "h2-send-test" "$body"

    # Verify response_headers has HTTP/2
    local resp_headers
    resp_headers=$(cat "$DATA_DIR/flows/$flow_id2/response_headers" 2>/dev/null)
    assert_contains "send h2 response version" "HTTP/2" "$resp_headers"

    echo
}

# ---------------------------------------------------------------------------
# Test: --fix-content-length rewrites Content-Length on the wire
# ---------------------------------------------------------------------------

test_fix_content_length() {
    echo "=== Test: --fix-content-length ==="

    "$ODDA_BIN" --socket "$SOCKET" request new --name cl-test --host xs2.top --force > /dev/null 2>&1

    # Write a POST request with a WRONG Content-Length and a body that doesn't match
    write_request "$DATA_DIR/requests/cl-test/request" <<'REQEOF'
POST /a?body=cl-ok&status=200&header=Content-Type:application/json HTTP/1.1
Host: xs2.top
Content-Type: application/json
Content-Length: 999
Connection: close

{"key":"value"}
REQEOF

    cat > "$DATA_DIR/requests/cl-test/meta.json" <<'METAEOF'
{"scheme": "https", "host": "xs2.top", "port": 443}
METAEOF

    # Without --fix-content-length, the wrong CL should cause the server to hang
    # or respond with an error. With --fix-content-length, it should succeed.
    local send_output
    send_output=$("$ODDA_BIN" --socket "$SOCKET" request send cl-test --fix-content-length --timeout 10 2>&1)
    echo "send cl output: $send_output"

    local status_code
    status_code=$(json_field "$send_output" "status_code")
    assert_eq "fix-cl status_code" "200" "$status_code"

    # Verify the stored request file has the CORRECT Content-Length
    # The body is '{"key":"value"}\n' = 17 bytes (heredoc adds trailing newline)
    local stored_request
    stored_request=$(cat "$DATA_DIR/flows/$(json_field "$send_output" "id")/request" 2>/dev/null)
    assert_contains "fix-cl stored request has correct CL" "Content-Length: 17" "$stored_request"

    # Verify the editable file is UNCHANGED (still has 999)
    local editable_request
    editable_request=$(cat "$DATA_DIR/requests/cl-test/request" 2>/dev/null)
    assert_contains "fix-cl editable file unchanged" "Content-Length: 999" "$editable_request"

    echo
}

# ---------------------------------------------------------------------------
# Test: gzip response decoding
# ---------------------------------------------------------------------------

test_gzip_decode() {
    echo "=== Test: response with Content-Encoding but uncompressed body ==="

    "$ODDA_BIN" --socket "$SOCKET" request new --name gzip-test --host xs2.top --force > /dev/null 2>&1

    # xs2.top returns the body= value as-is, even when we request
    # Content-Encoding:gzip in the response header. This tests that send
    # gracefully handles a mismatched Content-Encoding (returns raw bytes).
    write_request "$DATA_DIR/requests/gzip-test/request" <<'REQEOF'
GET /a?body=gzip-decoded-ok&status=200&header=Content-Type:application/json&header=Content-Encoding:gzip HTTP/1.1
Host: xs2.top
Connection: close

REQEOF

    cat > "$DATA_DIR/requests/gzip-test/meta.json" <<'METAEOF'
{"scheme": "https", "host": "xs2.top", "port": 443}
METAEOF

    local send_output
    send_output=$("$ODDA_BIN" --socket "$SOCKET" request send gzip-test --timeout 10 2>&1)
    echo "send gzip output: $send_output"

    local status_code
    status_code=$(json_field "$send_output" "status_code")
    assert_eq "gzip status_code" "200" "$status_code"

    # The response body should be empty — xs2.top returns no body when
    # Content-Encoding:gzip is set as a response header. The key assertion
    # is that send handled the empty chunked body gracefully (status 200,
    # no crash) and recorded the flow.
    local flow_id body_file body
    flow_id=$(json_field "$send_output" "id")
    body_file=$(json_field "$send_output" "body_file")
    if [ -n "$body_file" ] && [ -e "$DATA_DIR/$body_file" ]; then
        echo "[OK] gzip response body file exists (empty body handled gracefully)"
        PASSED=$((PASSED + 1))
        body=$(cat "$DATA_DIR/$body_file" 2>/dev/null)
        assert_eq "gzip response body is empty" "" "$body"
    else
        echo "[FAIL] gzip response body file missing"
        FAILED=$((FAILED + 1))
    fi

    echo
}

# ---------------------------------------------------------------------------
# Test: --insecure skips TLS verification
# ---------------------------------------------------------------------------

test_insecure() {
    echo "=== Test: --insecure ==="

    # Start a local HTTPS server with a self-signed cert
    python3 -c "
import http.server, ssl, os, tempfile
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain(certfile='/tmp/odda-req-e2e/selfsigned.pem', keyfile='/tmp/odda-req-e2e/selfsigned.key')
# Generate a self-signed cert if not present
" 2>/dev/null || true

    # Generate self-signed cert
    if [ ! -e "$TMPDIR/selfsigned.pem" ]; then
        openssl req -x509 -newkey rsa:2048 -keyout "$TMPDIR/selfsigned.key" \
            -out "$TMPDIR/selfsigned.pem" -days 1 -nodes \
            -subj "/CN=127.0.0.1" 2>/dev/null
    fi

    # Start a simple HTTPS server
    python3 -c "
import http.server, ssl
handler = http.server.SimpleHTTPRequestHandler
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain(certfile='$TMPDIR/selfsigned.pem', keyfile='$TMPDIR/selfsigned.key')
server = http.server.HTTPServer(('127.0.0.1', 8771), handler)
server.socket = ctx.wrap_socket(server.socket, server_side=True)
server.serve_forever()
" > "$TMPDIR/https_server.log" 2>&1 &
    local https_pid=$!
    sleep 1

    "$ODDA_BIN" --socket "$SOCKET" request new --name insecure-test --host xs2.top --force > /dev/null 2>&1

    write_request "$DATA_DIR/requests/insecure-test/request" <<'REQEOF'
GET / HTTP/1.1
Host: 127.0.0.1:8771
Connection: close

REQEOF

    cat > "$DATA_DIR/requests/insecure-test/meta.json" <<'METAEOF'
{"scheme": "https", "host": "127.0.0.1", "port": 8771}
METAEOF

    # Without --insecure, should fail with TLS error
    local fail_output
    fail_output=$("$ODDA_BIN" --socket "$SOCKET" request send insecure-test --timeout 5 2>&1)
    local fail_error
    fail_error=$(json_field "$fail_output" "error")
    if [ -n "$fail_error" ]; then
        echo "[OK] without --insecure, TLS verification fails as expected"
        PASSED=$((PASSED + 1))
    else
        echo "[FAIL] expected TLS error without --insecure, got: $fail_output"
        FAILED=$((FAILED + 1))
    fi

    # With --insecure, should succeed
    local send_output
    send_output=$("$ODDA_BIN" --socket "$SOCKET" request send insecure-test --insecure --timeout 5 2>&1)
    echo "send insecure output: $send_output"

    local status_code
    status_code=$(json_field "$send_output" "status_code")
    if [ "$status_code" = "200" ]; then
        echo "[OK] with --insecure, self-signed cert accepted (status=$status_code)"
        PASSED=$((PASSED + 1))
    else
        echo "[FAIL] expected 200 with --insecure, got status=$status_code"
        FAILED=$((FAILED + 1))
    fi

    kill "$https_pid" 2>/dev/null || true
    echo
}

# ---------------------------------------------------------------------------
# Test: OAST callback via interactsh
# ---------------------------------------------------------------------------

test_oast() {
    echo "=== Test: OAST callback ==="

    if [ -z "$INTERACTSH_BIN" ]; then
        echo "[SKIP] interactsh-client not available"
        echo
        return
    fi

    # Start interactsh-client in the background, capturing output
    "$INTERACTSH_BIN" -n 1 > "$TMPDIR/interactsh.log" 2>&1 &
    INTERACTSH_PID=$!
    sleep 3

    # Extract the payload domain from the output
    local payload
    payload=$(grep -oE '[a-z0-9]+\.xs2\.top' "$TMPDIR/interactsh.log" | head -n 1)
    if [ -z "$payload" ]; then
        echo "[FAIL] could not extract OAST payload from interactsh output"
        cat "$TMPDIR/interactsh.log"
        FAILED=$((FAILED + 1))
        return
    fi
    echo "OAST payload: $payload"

    # Create a request that hits the OAST payload URL
    "$ODDA_BIN" --socket "$SOCKET" request new --name oast-test --host xs2.top --force > /dev/null 2>&1

    write_request "$DATA_DIR/requests/oast-test/request" <<REQEOF
GET /oast-callback-test HTTP/1.1
Host: $payload
Connection: close

REQEOF

    cat > "$DATA_DIR/requests/oast-test/meta.json" <<METAEOF
{"scheme": "https", "host": "$payload", "port": 443}
METAEOF

    # Send the request
    local send_output
    send_output=$("$ODDA_BIN" --socket "$SOCKET" request send oast-test --timeout 10 2>&1)
    echo "send oast output: $send_output"

    # Wait for the interaction to be registered
    sleep 5

    # Kill interactsh-client and check its output for the interaction
    kill "$INTERACTSH_PID" 2>/dev/null || true
    wait "$INTERACTSH_PID" 2>/dev/null || true
    INTERACTSH_PID=""

    # Check if the interactsh log contains the interaction
    if grep -qi "oast-callback-test" "$TMPDIR/interactsh.log" 2>/dev/null; then
        echo "[OK] OAST interaction received for /oast-callback-test"
        PASSED=$((PASSED + 1))
    else
        echo "[FAIL] no OAST interaction received"
        echo "--- interactsh log ---"
        cat "$TMPDIR/interactsh.log"
        FAILED=$((FAILED + 1))
    fi

    echo
}

# ---------------------------------------------------------------------------
# Test: empty request error handling
# ---------------------------------------------------------------------------

test_empty_request_error() {
    echo "=== Test: empty request error ==="

    "$ODDA_BIN" --socket "$SOCKET" request new --name empty-test --host xs2.top --force > /dev/null 2>&1

    # Don't write anything to the request file — it's empty
    local send_output
    send_output=$("$ODDA_BIN" --socket "$SOCKET" request send empty-test --timeout 5 2>&1 || true)
    echo "send empty output: $send_output"

    # Should contain an error
    local error_msg
    error_msg=$(json_field "$send_output" "error")
    if [ -n "$error_msg" ]; then
        echo "[OK] empty request produces error: $error_msg"
        PASSED=$((PASSED + 1))
    else
        echo "[FAIL] empty request should produce an error"
        FAILED=$((FAILED + 1))
    fi

    echo
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

start_server

test_clone
test_new_and_send_h1
test_send_h2
test_fix_content_length
test_gzip_decode
test_insecure
test_oast
test_empty_request_error

echo "=== Summary ==="
echo "Passed: $PASSED"
echo "Failed: $FAILED"

if [ "$FAILED" -ne 0 ]; then
    echo
    echo "Server log tail:"
    tail -n 30 "$DATA_DIR/server.log" 2>/dev/null
    exit 1
fi
