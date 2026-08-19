---
prepend:
  - _lib/boot.md
  - _lib/fixture-server.md
  - _lib/dyn-server.md
append:
  - _lib/teardown.md
---

# `odda request send` (HTTP/2, fix-content-length, gzip, insecure, empty)

Miscellaneous `odda request send` behavior.

## Set up the dyn server

The dyn server speaks HTTP/1.1 and HTTP/2 over TLS with a self-signed
cert, serving dynamic `?body=&status=&header=&gzip=1` responses. It
replaces the remote `xs2.top` server so the test is deterministic and
runs offline.

```scrut
$ setup_dyn_server
```

```scrut {detached: true, detached_kill_signal: term}
$ port=$(cat "$PWD/dyn_port"); ( hypercorn --bind "127.0.0.1:$port" --keyfile "$PWD/dyn.key" --certfile "$PWD/dyn.pem" "$PWD/dyn_asgi.py:app" >"$PWD/dyn_server.log" 2>&1 < /dev/null & )
```

```scrut
$ wait_for_dyn_server
```

## `request send` negotiates HTTP/2 when the request line says `HTTP/2`

Create an editable request pointed at the dyn server and write a raw
HTTP/2 GET request. The dyn server negotiates `h2` via ALPN.

```scrut
$ port=$(cat "$PWD/dyn_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name h2-test --host 127.0.0.1 --port $port --force > /dev/null
```

```scrut
$ port=$(cat "$PWD/dyn_port"); printf 'GET /a?body=h2-send-test&status=200&header=Content-Type:application/json HTTP/2\r\nuser-agent: odda-test\r\naccept: */*\r\n\r\n' > "$PWD/data/requests/h2-test/request"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request send --name h2-test --insecure --timeout 10 \
>   | grep -E '^status_code: 200$'
status_code: 200
```

The response body matches. A second `request send` captures the flow id
into a shell var via `--json` (the documented way to pull a structured
value out for scripting).

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json \
>   request send --name h2-test --insecure --timeout 10 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' > "$PWD/h2_flow_id"
```

```scrut
$ h2_flow_id=$(cat "$PWD/h2_flow_id")
```

```scrut
$ cat "$PWD/h2_flow_id"
* (glob)
```


```scrut
$ cat "$PWD/data/flows/$h2_flow_id/response_body.json"
h2-send-test (no-eol)
```

The stored `response_headers` shows HTTP/2.


```scrut
$ grep -F "HTTP/2" "$PWD/data/flows/$h2_flow_id/response_headers" >/dev/null \
>   && echo "h2 response" || echo "missing"
h2 response
```

## `--fix-content-length` recomputes Content-Length on the wire

The body in the test is `{"key":"value"}\n` (17 bytes including the
trailing newline from the heredoc). The `request` file declares
`Content-Length: 999` (wrong). Without `--fix-content-length` the
server would hang; with the flag the send succeeds and the stored
request shows the corrected `Content-Length: 17`.

```scrut
$ port=$(cat "$PWD/dyn_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name cl-test --host 127.0.0.1 --port $port --force > /dev/null
```

```scrut
$ port=$(cat "$PWD/dyn_port"); printf 'POST /a?body=cl-ok&status=200&header=Content-Type:application/json HTTP/1.1\r\nHost: 127.0.0.1:%s\r\nContent-Type: application/json\r\nContent-Length: 999\r\nConnection: close\r\n\r\n{"key":"value"}\r\n' "$port" > "$PWD/data/requests/cl-test/request"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request send --name cl-test --fix-content-length --insecure --timeout 10 \
>   | grep -E '^status_code: 200$'
status_code: 200
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json \
>   request send --name cl-test --fix-content-length --insecure --timeout 10 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["id"])' > "$PWD/cl_flow_id"
```

```scrut
$ cl_flow_id=$(cat "$PWD/cl_flow_id")
```

```scrut
$ grep -F "Content-Length: 17" "$PWD/data/flows/$cl_flow_id/request" >/dev/null \
>   && echo "stored has correct CL" || echo "missing"
stored has correct CL
```

The editable `request` file on disk is untouched.

```scrut
$ grep -F "Content-Length: 999" "$PWD/data/requests/cl-test/request" >/dev/null \
>   && echo "editable unchanged" || echo "editable modified"
editable unchanged
```

## `--fix-content-length` preserves CRLF when CL is followed by another header

Regression: when `Content-Length` is not the last header before the blank
line, the regex used to rewrite the value consumed the trailing `\r`
(via `\s*`) and emitted a bare `\n`, gluing the next header onto the
same line and producing a malformed-on-the-wire request. The body
(`postbody` = 8 bytes) differs from the declared `Content-Length: 999`
so the flag has work to do, and `X-Order: trailing` is placed after the
`Content-Length` header to expose the corruption.

```scrut
$ port=$(cat "$PWD/dyn_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name cl-crlf --host 127.0.0.1 --port $port --force > /dev/null
```

```scrut
$ port=$(cat "$PWD/dyn_port"); printf 'POST /a?body=cl-crlf-ok&status=200&header=Content-Type:application/json HTTP/1.1\r\nHost: 127.0.0.1:%s\r\nContent-Type: application/json\r\nContent-Length: 999\r\nX-Order: trailing\r\nConnection: close\r\n\r\npostbody' "$port" > "$PWD/data/requests/cl-crlf/request"
```

A bare-LF after `Content-Length` would glue `X-Order: trailing` onto the
CL line, and hypercorn would reject the request (400 / connection drop)
instead of returning the requested 200.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request send --name cl-crlf --fix-content-length --insecure --timeout 10 \
>   | grep -E '^status_code: 200$'
status_code: 200
```

The stored on-the-wire request must keep CRLF after the rewritten
`Content-Length` (the bug dropped the `\r`). Asserted in Python on the
raw bytes so the CRLF is actually inspected, not just a substring.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json \
>   request send --name cl-crlf --fix-content-length --insecure --timeout 10 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' > "$PWD/cl_crlf_flow_id"
```

```scrut
$ cl_crlf_flow_id=$(cat "$PWD/cl_crlf_flow_id")
```

```scrut
$ python3 -c 'import sys; b=open(sys.argv[1],"rb").read(); \
>   ok=b"Content-Length: 8\r\nX-Order: trailing\r\n" in b; \
>   print("crlf preserved" if ok else "BARE LF: "+repr(b[b.find(b"Content-Length"):b.find(b"Content-Length")+40]))' \
>   "$PWD/data/flows/$cl_crlf_flow_id/request"
crlf preserved
```

## `--fix-content-length` preserves CRLF on the HTTP/2 send path

The same corruption affects the HTTP/2 send path: `fix_content_length_bytes`
runs on the raw request bytes before the H2/H1 branch, so a `Content-Length`
header followed by another header loses its `\r` there too. This is the
path the bug report flags as the affected one. Build an `HTTP/2` request
with a wrong `Content-Length: 999` followed by a trailing header and a
shortened body (`h2body` = 6 bytes), send it, and assert the stored
on-the-wire bytes keep CRLF after the rewritten `Content-Length`.

```scrut
$ port=$(cat "$PWD/dyn_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name cl-h2 --host 127.0.0.1 --port $port --force > /dev/null
```

```scrut
$ port=$(cat "$PWD/dyn_port"); printf 'POST /a?body=cl-h2-ok&status=200&header=Content-Type:application/json HTTP/2\r\nHost: 127.0.0.1:%s\r\nContent-Type: application/json\r\nContent-Length: 999\r\nX-Order: trailing\r\n\r\nh2body' "$port" > "$PWD/data/requests/cl-h2/request"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request send --name cl-h2 --fix-content-length --insecure --timeout 10 \
>   | grep -E '^status_code: 200$'
status_code: 200
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json \
>   request send --name cl-h2 --fix-content-length --insecure --timeout 10 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' > "$PWD/cl_h2_flow_id"
```

```scrut
$ cl_h2_flow_id=$(cat "$PWD/cl_h2_flow_id")
```

The stored request keeps CRLF after the rewritten `Content-Length: 6`
on the H2 path (the bug dropped the `\r`, gluing `X-Order` onto the CL
line). The response must not be a 400 "Newlines in headers" — a 200
proves the on-the-wire framing was accepted.

```scrut
$ python3 -c 'import sys; b=open(sys.argv[1],"rb").read(); \
>   ok=b"Content-Length: 6\r\nX-Order: trailing\r\n" in b; \
>   print("crlf preserved" if ok else "BARE LF: "+repr(b[b.find(b"Content-Length"):b.find(b"Content-Length")+40]))' \
>   "$PWD/data/flows/$cl_h2_flow_id/request"
crlf preserved
```

## gzip response: empty body is handled gracefully

The dyn server compresses the body and sets `Content-Encoding: gzip`
when `gzip=1` is in the query string. The send decodes it and records
the decoded body.

```scrut
$ port=$(cat "$PWD/dyn_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name gzip-test --host 127.0.0.1 --port $port --force > /dev/null
```

```scrut
$ port=$(cat "$PWD/dyn_port"); printf 'GET /a?body=gzip-decoded-ok&status=200&header=Content-Type:application/json&gzip=1 HTTP/1.1\r\nHost: 127.0.0.1:%s\r\nConnection: close\r\n\r\n' "$port" > "$PWD/data/requests/gzip-test/request"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request send --name gzip-test --insecure --timeout 10 \
>   | grep -E '^(status_code: 200|body_file: flows/.*/response_body\.json)$'
status_code: 200
body_file: flows/*/response_body.json (glob)
```


```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json \
>   request send --name gzip-test --insecure --timeout 10 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' > "$PWD/gzip_flow_id"
```

```scrut
$ gzip_flow_id=$(cat "$PWD/gzip_flow_id")
```

```scrut
$ cat "$PWD/data/flows/$gzip_flow_id/response_body.json"
gzip-decoded-ok (no-eol)
```

## `--insecure` skips TLS verification against a self-signed server

The dyn server uses a self-signed cert (generated at setup time).
Without `--insecure`, the send fails with a TLS error — but `request
send` captures transport failures as error flow records (exit 0, the
failure recorded as a flow), so the text output is the normal
`request send` block with `status_code: null`, `body_file: null`, and
`error: <TLS message>`.

```scrut
$ port=$(cat "$PWD/dyn_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name insecure-test --host 127.0.0.1 --port $port --force > /dev/null
```

```scrut
$ port=$(cat "$PWD/dyn_port"); printf 'GET / HTTP/1.1\r\nHost: 127.0.0.1:%s\r\nConnection: close\r\n\r\n' "$port" > "$PWD/data/requests/insecure-test/request"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request send --name insecure-test --timeout 5 \
>   | grep -E '^(status_code: null|body_file: null|error: .+)$'
status_code: null
body_file: null
error: * (glob)
```

With `--insecure`, the send succeeds.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request send --name insecure-test --insecure --timeout 5 \
>   | grep -E '^status_code: 200$'
status_code: 200
```

## A connection that closes without a response is an error flow (not `status_code: 0`)

Single-shot `request send` shares the response-header reader with the
multi-name pipeline. A server that accepts the connection then closes
without sending any response headers must produce a descriptive error
flow (`status_code: null` + `error`), not the ambiguous
`status_code: 0, error: null` pseudo-response that masked dropped
connections. A raw TCP fixture server accepts then closes immediately,
so the read hits EOF before any headers.

```scrut
$ pick_port > "$PWD/close_port"
```

```scrut {detached: true, detached_kill_signal: term}
$ port=$(cat "$PWD/close_port"); ( python3 "$TESTDIR/fixtures/close_without_response_server.py" "$port" >"$PWD/close.log" 2>&1 < /dev/null & )
```

```scrut
$ for i in $(seq 1 300); do ( python3 -c "import socket; s=socket.socket(); s.connect((\"127.0.0.1\",$(cat "$PWD/close_port"))); s.close()" 2>/dev/null ) && exit 0; sleep 0.05; done; echo "close server not reachable" >&2; exit 1
```

```scrut
$ port=$(cat "$PWD/close_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name close-no-resp --host 127.0.0.1 --port $port --protocol http --force > /dev/null
```

```scrut
$ printf 'GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n' > "$PWD/data/requests/close-no-resp/request"
```

The flow record carries `status_code: null` and a descriptive `error`
naming the connection close — not `status_code: 0` with `error: null`.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json \
>   request send --name close-no-resp --timeout 5 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status_code"] is None, isinstance(d["error"],str) and "connection closed" in d["error"])'
True True
```

Stop the close-without-response fixture server.

```scrut
$ pkill -f "close_without_response_server.py $PWD" 2>/dev/null || true
```

```scrut
$ for i in $(seq 1 300); do pgrep -f "close_without_response_server.py $PWD" >/dev/null || exit 0; sleep 0.05; done; echo "close server still running" >&2; exit 1
```

## Empty `request` file is rejected

`request new` makes an empty file; `request send` against an empty
file must error.

```scrut
$ port=$(cat "$PWD/dyn_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name empty-test --host 127.0.0.1 --port $port --force > /dev/null
```

```scrut {output_stream: stderr}
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request send --name empty-test --timeout 5
[1]
Error: * (glob)
```

## `request send` stores the body even for excluded Content-Types

Browser capture drops response bodies whose `Content-Type` is in the
excluded set (images/video/audio/fonts), but `request send` does not —
a hand-built request exists to see its response body (e.g. a
path-traversal file mislabeled `image/jpeg`). The dyn server serves an
arbitrary body with a caller-chosen `Content-Type`, so we ask for
`image/jpeg` carrying a text payload and assert the body is stored as
`response_body.jpeg` and is readable, with no special flag.

```scrut
$ port=$(cat "$PWD/dyn_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name img-test --host 127.0.0.1 --port $port --force > /dev/null
```

```scrut
$ port=$(cat "$PWD/dyn_port"); printf 'GET /a?body=secret-file-contents&status=200&header=Content-Type:image/jpeg HTTP/1.1\r\nHost: 127.0.0.1:%s\r\nConnection: close\r\n\r\n' "$port" > "$PWD/data/requests/img-test/request"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request send --name img-test --insecure --timeout 10 \
>   | grep -E '^(status_code: 200|body_file: flows/.*/response_body\.jpeg)$'
status_code: 200
body_file: flows/*/response_body.jpeg (glob)
```

The body file is readable and holds the payload, not dropped.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json \
>   request send --name img-test --insecure --timeout 10 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' > "$PWD/img_flow_id"
```

```scrut
$ img_flow_id=$(cat "$PWD/img_flow_id")
```

```scrut
$ cat "$PWD/data/flows/$img_flow_id/response_body.jpeg"
secret-file-contents (no-eol)
```

## Teardown: stop the dyn server

```scrut
$ stop_dyn_server
```