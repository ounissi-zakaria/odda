---
prepend:
  - _lib/setup.md
---

# `odda request send` (HTTP/2, fix-content-length, gzip, insecure, empty)

Miscellaneous `odda request send` behavior.

## Boot the server

```scrut {detached: true, detached_kill_signal: term}
$ ( "$ODDA_BIN" server --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   >"$PWD/server.log" 2>&1 & )
```

```scrut {wait: {timeout: 10s, path: "odda.sock"}}
$ echo "server is up"
server is up
```

## `request send` negotiates HTTP/2 when the request line says `HTTP/2`

First, capture an HTTP/2 request via the proxy to get a real
H2-shaped request to clone.

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" proxy-url > "$PWD/proxy_url"
```

```scrut
$ curl -s -x "$(cat "$PWD/proxy_url")" -k --proxy-insecure \
>   'https://xs2.top/a?body=h2-capture&status=200&header=Content-Type:application/json' \
>   -o /dev/null -w "curl_status=%{http_code}\n"
curl_status=200
```

```scrut
$ sleep 1
```

```scrut
$ grep '"host": "xs2.top"' "$PWD/data/flows/flows.jsonl" | head -n 1 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' > "$PWD/flow_id"
```

```scrut
$ cat "$PWD/flow_id"
* (glob)
```

Now clone the captured H2 request and rewrite it for the H2 send test.


```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request clone "$flow_id" --name h2-test --force > /dev/null
```

```scrut
$ sed 's/$/\r/' > "$PWD/data/requests/h2-test/request" <<'REQEOF'
> GET /a?body=h2-send-test&status=200&header=Content-Type:application/json HTTP/2
> user-agent: odda-test
> accept: */*
> 
> REQEOF
```

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request send h2-test --timeout 10 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status_code"])'
200
```

The response body matches.

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request send h2-test --timeout 10 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["id"])' > "$PWD/h2_flow_id"
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
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name cl-test --host xs2.top --force > /dev/null
```

```scrut
$ sed 's/$/\r/' > "$PWD/data/requests/cl-test/request" <<'REQEOF'
> POST /a?body=cl-ok&status=200&header=Content-Type:application/json HTTP/1.1
> Host: xs2.top
> Content-Type: application/json
> Content-Length: 999
> Connection: close
> 
> {"key":"value"}
> REQEOF
```

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request send cl-test --fix-content-length --timeout 10 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["status_code"])'
200
```

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request send cl-test --fix-content-length --timeout 10 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' > "$PWD/cl_flow_id"
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

`xs2.top` returns no body when `Content-Encoding:gzip` is requested
as a response header. The send must not crash; the recorded
`response_body.*` file is empty (or absent).

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name gzip-test --host xs2.top --force > /dev/null
```

```scrut
$ sed 's/$/\r/' > "$PWD/data/requests/gzip-test/request" <<'REQEOF'
> GET /a?body=gzip-decoded-ok&status=200&header=Content-Type:application/json&header=Content-Encoding:gzip HTTP/1.1
> Host: xs2.top
> Connection: close
> 
> REQEOF
```

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request send gzip-test --timeout 10 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["status_code"])'
200
```

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request send gzip-test --timeout 10 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["id"], d["body_file"])'
* flows/*/response_body.json (glob)
```

## `--insecure` skips TLS verification against a self-signed server

Spin up a local HTTPS server with a self-signed cert.

```scrut {detached: true, detached_kill_signal: term}
$ ( openssl req -x509 -newkey rsa:2048 \
>     -keyout "$PWD/selfsigned.key" -out "$PWD/selfsigned.pem" \
>     -days 1 -nodes -subj "/CN=127.0.0.1" 2>/dev/null && \
>   nohup python3 -c "
> import http.server, ssl
> ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
> ctx.load_cert_chain(certfile='$PWD/selfsigned.pem', keyfile='$PWD/selfsigned.key')
> server = http.server.HTTPServer(('127.0.0.1', 8771), http.server.SimpleHTTPRequestHandler)
> server.socket = ctx.wrap_socket(server.socket, server_side=True)
> server.serve_forever()
> " > "$PWD/https_server.log" 2>&1 & )
```

```scrut
$ for i in $(seq 1 30); do curl -s -k -o /dev/null https://127.0.0.1:8771/ && exit 0; sleep 0.5; done; exit 1
```

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name insecure-test --host xs2.top --force > /dev/null
```

```scrut
$ sed 's/$/\r/' > "$PWD/data/requests/insecure-test/request" <<'REQEOF'
> GET / HTTP/1.1
> Host: 127.0.0.1:8771
> Connection: close
> 
> REQEOF
```

```scrut
$ cat > "$PWD/data/requests/insecure-test/meta.json" <<'METAEOF'
> {"scheme": "https", "host": "127.0.0.1", "port": 8771}
> METAEOF
```

Without `--insecure`, the send errors with a TLS error.

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request send insecure-test --timeout 5 2>&1 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print("error" in d)'
True
```

With `--insecure`, the send succeeds.

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request send insecure-test --insecure --timeout 5 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["status_code"])'
200
```

## Teardown: stop the local HTTPS server

```scrut
$ pkill -f "HTTPServer.*8771.*$PWD/selfsigned.pem" 2>/dev/null || true
```

```scrut
$ pkill -f "127.0.0.1.*8771" 2>/dev/null || true
```

```scrut
$ sleep 1
```

```scrut
$ pgrep -f "HTTPServer.*$PWD/selfsigned.pem" >/dev/null && echo "still running" || echo "stopped"
stopped
```

## Empty `request` file is rejected

`request new` makes an empty file; `request send` against an empty
file must error.

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name empty-test --host xs2.top --force > /dev/null
```

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request send empty-test --timeout 5 2>&1 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print("error" in d, "error" in d and d["error"] != "")'
True True
```

## Teardown: stop the odda server

```scrut
$ pkill -f "odda.*--data-dir $PWD/data" 2>/dev/null || true
```
