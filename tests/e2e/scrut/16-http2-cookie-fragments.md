---
prepend:
  - _lib/boot.md
  - _lib/fixture-server.md
  - _lib/dyn-server.md
append:
  - _lib/teardown.md
---

# Captured flows emit duplicate headers per-line, not `, `-folded

`_build_request_bytes` and `_build_response_headers_bytes`
(flowstore.py) serialize headers via `headers.items(multi=True)`. The
old `headers.items()` fold joined same-name headers with `", "`
(RFC 7230 §3.2.2) regardless of HTTP version — a version-agnostic
defect firing whenever mitmproxy's `Headers` held more than one entry
for a name. Both builders now emit each duplicate header on its own
line, faithful to what the client/server sent: two `cookie` fragments
(from a non-conformant H1 UA, a hand-built request, or an HTTP/2
client splitting cookie pairs per RFC 7540 §8.1.2.5) appear as two
`cookie:` lines; two `Set-Cookie` headers (a routine H1 login
response, which RFC 6265 §5.3 forbids from folding because attribute
values contain commas) appear as two `set-cookie:` lines. See
docs/adr/0013-cookie-header-serialization.md.

The request side is reproduced with a raw h2 client because curl and
Chrome pre-join cookies before encoding and so cannot produce
duplicate `cookie` headers. The fixture
(`fixtures/h2_cookie_client.py`) writes the split fragments directly.
The response side is reproduced with curl against a dyn-server
response carrying two `Set-Cookie` headers — a routine H1 response
shape (every login response) that the old fold corrupted.

## Set up the dyn server

The dyn server speaks HTTP/1.1 and HTTP/2 over TLS with a self-signed
cert, serving dynamic `?body=&status=&header=` responses. We use it
for the response-side `Set-Cookie` assertion (two `?header=set-cookie:`
params produce two `Set-Cookie` response headers).

```scrut
$ setup_dyn_server
```

```scrut {detached: true, detached_kill_signal: term}
$ port=$(cat "$PWD/dyn_port"); ( hypercorn --bind "127.0.0.1:$port" --keyfile "$PWD/dyn.key" --certfile "$PWD/dyn.pem" "$PWD/dyn_asgi.py:app" >"$PWD/dyn_server.log" 2>&1 < /dev/null & )
```

```scrut
$ wait_for_dyn_server
```

## Request side: duplicate Cookie headers stay on separate lines

A raw h2 client tunnels through odda's proxy and sends a GET with two
`cookie` fragments (`session=abc` and `_lab=val`). curl cannot do this
(it joins cookies before encoding), so the fixture writes raw h2
frames via the `h2` library. The captured `request` file must show
two `cookie:` lines, not the pre-fix `cookie: session=abc, _lab=val`
fold.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" proxy-url > "$PWD/proxy_url"
```

```scrut
$ port=$(cat "$PWD/dyn_port"); \
>   python3 "$TESTDIR/fixtures/h2_cookie_client.py" "$(cat "$PWD/proxy_url")" 127.0.0.1 "$port" "/?marker=h2-cookie-join"
h2client=ok
```

```scrut
$ wait_for_flow "h2-cookie-join"
```

Find the flow id for the h2-cookie-join request.

```scrut
$ grep '"host": "127.0.0.1"' "$PWD/data/flows/flows.jsonl" | grep 'h2-cookie-join' | head -n 1 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' > "$PWD/req_flow_id"
```

The captured `request` file records the two `cookie` fragments as two
separate `cookie:` lines — faithful to what the client sent. Before the
fix this was folded into `cookie: session=abc, _lab=val`.

```scrut
$ flow_id=$(cat "$PWD/req_flow_id"); count=$(grep -c -i "^cookie:" "$PWD/data/flows/$flow_id/request"); \
>   test "$count" -eq 2 && echo "split-ok" || echo "ERROR: expected 2 cookie lines, got $count"
split-ok
```

The captured `request` file must NOT contain the `, `-folded form.

```scrut
$ flow_id=$(cat "$PWD/req_flow_id"); grep -F "session=abc, _lab" "$PWD/data/flows/$flow_id/request" \
>   && echo "ERROR: still folded with ', '" || echo "no-fold-ok"
no-fold-ok
```

## Response side: duplicated Set-Cookie headers stay on separate lines

The dyn server serves a response with two `Set-Cookie` headers via two
`?header=set-cookie:` query params. The captured `response_headers`
file must show two separate `set-cookie:` lines, not the pre-fix
`set-cookie: a=1; Path=/, b=2; Path=/` fold (unparseable per RFC 6265
§5.3).

```scrut
$ port=$(cat "$PWD/dyn_port"); curl -s -x "$(cat "$PWD/proxy_url")" -k --proxy-insecure \
>   --http2 "https://127.0.0.1:$port/?marker=h2-setcookie-split&header=set-cookie:a%3D1%3B%20Path%3D/&header=set-cookie:b%3D2%3B%20Path%3D/" \
>   -o /dev/null -w "curl_status=%{http_code}\n"
curl_status=200
```

```scrut
$ wait_for_flow "h2-setcookie-split"
```

```scrut
$ grep '"host": "127.0.0.1"' "$PWD/data/flows/flows.jsonl" | grep 'h2-setcookie-split' | head -n 1 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' > "$PWD/resp_flow_id"
```

The captured `response_headers` must contain two `set-cookie:` lines —
one per Set-Cookie — not a single `, `-folded line.

```scrut
$ flow_id=$(cat "$PWD/resp_flow_id"); count=$(grep -c -i "^set-cookie:" "$PWD/data/flows/$flow_id/response_headers"); \
>   test "$count" -eq 2 && echo "split-ok" || echo "ERROR: expected 2 set-cookie lines, got $count"
split-ok
```

The captured `response_headers` must NOT contain the unparseable fold
(`set-cookie: a=1; Path=/, b=2; Path=/`).

```scrut
$ flow_id=$(cat "$PWD/resp_flow_id"); grep -F "a=1; Path=/, b=2" "$PWD/data/flows/$flow_id/response_headers" \
>   && echo "ERROR: set-cookie still folded" || echo "no-fold-ok"
no-fold-ok
```

## Teardown: stop the dyn server

```scrut
$ stop_dyn_server
```