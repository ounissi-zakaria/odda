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
>   request send h2-test --insecure --timeout 10 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status_code"])'
200
```

The response body matches.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request send h2-test --insecure --timeout 10 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["id"])' > "$PWD/h2_flow_id"
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
>   request send cl-test --fix-content-length --insecure --timeout 10 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["status_code"])'
200
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request send cl-test --fix-content-length --insecure --timeout 10 \
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
>   request send gzip-test --insecure --timeout 10 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["status_code"])'
200
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request send gzip-test --insecure --timeout 10 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["id"], d["body_file"])'
* flows/*/response_body.json (glob)
```


```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request send gzip-test --insecure --timeout 10 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["id"])' > "$PWD/gzip_flow_id"
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
Without `--insecure`, the send errors with a TLS error.

```scrut
$ port=$(cat "$PWD/dyn_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name insecure-test --host 127.0.0.1 --port $port --force > /dev/null
```

```scrut
$ port=$(cat "$PWD/dyn_port"); printf 'GET / HTTP/1.1\r\nHost: 127.0.0.1:%s\r\nConnection: close\r\n\r\n' "$port" > "$PWD/data/requests/insecure-test/request"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request send insecure-test --timeout 5 2>&1 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print("error" in d)'
True
```

With `--insecure`, the send succeeds.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request send insecure-test --insecure --timeout 5 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["status_code"])'
200
```

## Empty `request` file is rejected

`request new` makes an empty file; `request send` against an empty
file must error.

```scrut
$ port=$(cat "$PWD/dyn_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name empty-test --host 127.0.0.1 --port $port --force > /dev/null
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request send empty-test --timeout 5 2>&1 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print("error" in d, "error" in d and d["error"] != "")'
True True
```

## Teardown: stop the dyn server

```scrut
$ stop_dyn_server
```