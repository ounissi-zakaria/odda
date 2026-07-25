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

## Teardown: stop the dyn server

```scrut
$ stop_dyn_server
```