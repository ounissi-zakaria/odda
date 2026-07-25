---
prepend:
  - _lib/boot.md
  - _lib/fixture-server.md
append:
  - _lib/teardown.md
---

# Proxy accepts HTTP/2 responses with whitespace-padded header values

Real-world servers emit header values with leading/trailing whitespace
(e.g. `hackerone.on.symphony.com` sends ` IE=Edge`). The `h2` library
rejects those with `ProtocolError("Received header value surrounded by
whitespace ...")`, which mitmproxy surfaces as a 502 — blocking capture
of the very hosts odda targets. odda sets
`validate_inbound_headers=False` so the proxy accepts them, matching
HTTP/1.1's lenient `value.strip()`. See
docs/adr/0012-proxy-skip-inbound-header-validation.md.

## Set up the bad-header server

A raw HTTP/2 server (no ASGI framework) that emits a response header
with a leading space in its value. hypercorn sanitizes header values
(`build_and_validate_headers` calls `bytes(value).strip()`), so the
standard dyn_asgi.py fixture cannot reproduce the bug. This server
writes raw h2 frames via the `h2` library directly with
`validate_outbound_headers=False`, sending `X-Bad-Header: <space>IE=Edge`
unsanitized — exactly what a non-conformant upstream does on the wire.

```scrut
$ pick_port > "$PWD/dyn_port"
```

```scrut
$ openssl req -x509 -newkey rsa:2048 \
>   -keyout "$PWD/dyn.key" -out "$PWD/dyn.pem" \
>   -days 1 -nodes -subj "/CN=127.0.0.1" 2>/dev/null
```

```scrut {detached: true, detached_kill_signal: term}
$ port=$(cat "$PWD/dyn_port"); ( python3 "$TESTDIR/fixtures/ws_header_h2server.py" "$port" "$PWD/dyn.pem" "$PWD/dyn.key" >"$PWD/dyn_server.log" 2>&1 < /dev/null & )
```

```scrut
$ port=$(cat "$PWD/dyn_port"); for i in $(seq 1 100); do curl -s -k -o /dev/null "https://127.0.0.1:$port/" && exit 0; sleep 0.05; done; echo "dyn server on $port not reachable" >&2; exit 1
```

## A curl request through the proxy does not 502

`curl --http2` tunnels through odda's proxy; mitmproxy re-establishes
HTTP/2 with the upstream server, which responds with `X-Bad-Header:
<space>IE=Edge`. Before the fix this returned 502 with the flow error
`HTTP/2 protocol error: Received header value surrounded by whitespace
b' IE=Edge'`; now it returns 200.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" proxy-url > "$PWD/proxy_url"
```

```scrut
$ port=$(cat "$PWD/dyn_port"); curl -s -x "$(cat "$PWD/proxy_url")" -k --proxy-insecure \
>   --http2 "https://127.0.0.1:$port/?marker=ws-header-curl" \
>   -o /dev/null -w "curl_status=%{http_code}\n"
curl_status=200
```

```scrut
$ wait_for_flow "ws-header-curl"
```

## A browser navigation through the proxy does not 502

The reported symptom was a 502 when navigating via `odda browser`.
The browser takes the same proxy path as curl (CONNECT tunnel, then
mitmproxy re-establishes TLS upstream and negotiates h2), but this
drives the actual `navigate` command the user ran rather than proving
the fix by equivalence alone.

Open a browser and capture its ids into a file for the navigate step.
`browser open` is captured structurally via `--json` so the ids can be
parsed out reliably.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json \
>   browser open --headless \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["browser_id"], d["tab_id"])' > "$PWD/browser_ids"
```

```scrut
$ browser_id=$(cut -d" " -f1 "$PWD/browser_ids"); tab_id=$(cut -d" " -f2 "$PWD/browser_ids"); cat "$PWD/browser_ids"
* * (glob)
```

In text mode `navigate` prints `Navigated to: <url>`.

```scrut
$ browser_id=$(cut -d" " -f1 "$PWD/browser_ids"); tab_id=$(cut -d" " -f2 "$PWD/browser_ids"); port=$(cat "$PWD/dyn_port"); \
>   odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   navigate --url "https://127.0.0.1:$port/?marker=ws-header-browser" --browser-id "$browser_id" --tab-id "$tab_id"
Navigated to: https://127.0.0.1:* (glob)
```

```scrut
$ wait_for_flow "ws-header-browser"
```

## The captured flows have status 200, not an error

Both the curl and browser flows complete normally — `status_code` is
200 and `error` is null. Before the fix, `status_code` was null and
`error` held the protocol error message.

```scrut
$ grep 'ws-header-curl' "$PWD/data/flows/flows.jsonl" | head -n 1 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status_code"], d["error"])'
200 None
```

```scrut
$ grep 'ws-header-browser' "$PWD/data/flows/flows.jsonl" | head -n 1 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status_code"], d["error"])'
200 None
```

## Teardown: stop the bad-header server

Kill the raw h2 server by its script name (not hypercorn, so
`stop_dyn_server` would not match it) and fail loud if it survives the
bounded poll.

```scrut
$ pkill -f "ws_header_h2server.py" 2>/dev/null || true
```

```scrut
$ for i in $(seq 1 100); do pgrep -f "ws_header_h2server.py" >/dev/null || exit 0; sleep 0.05; done; echo "ws_header_h2server still running" >&2; exit 1
```