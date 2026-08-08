---
prepend:
  - _lib/boot.md
  - _lib/fixture-server.md
  - _lib/dyn-server.md
append:
  - _lib/teardown.md
---

# `odda request send` (H2 line terminator — CRLF in `:path`)

H2 request files are frame-source: odda parses the file into header
values and builds H2 frames. The parser splits header lines on the
line terminator (default `\r\n`), so a literal `\r\n` inside a
header value splits the value and breaks the parse. For H2→H1
downgrade smuggling (where an H2 `:path` containing `\r\n` splits a
smuggled header on the back-end when a gateway translates H2→H1),
the agent sets a custom line terminator (e.g. `\x00`) so the parser
splits on `\x00` and the `\r\n` inside the value is preserved into
the H2 frame. See `docs/adr/0021-h2-line-terminator-for-crlf-in-pseudo-headers.md`
and the *Line terminator* / *Frame-source* entries in `CONTEXT.md`.

## Set up the raw H2 echo-path server

A raw HTTP/2 server (no ASGI framework) that accepts an invalid
`:path` (with CRLF) — `validate_inbound_headers=False` — and echoes
the received `:path` verbatim as the body. hypercorn validates `:path`
and rejects CRLF, so the standard dyn_asgi.py fixture cannot reproduce
the H2→H1 downgrade scenario. This server uses the `h2` library
directly, mirroring `fixtures/ws_header_h2server.py`.

```scrut
$ pick_port > "$PWD/echo_port"
```

```scrut
$ openssl req -x509 -newkey rsa:2048 \
>   -keyout "$PWD/echo.key" -out "$PWD/echo.pem" \
>   -days 1 -nodes -subj "/CN=127.0.0.1" 2>/dev/null
```

```scrut {detached: true, detached_kill_signal: term}
$ port=$(cat "$PWD/echo_port"); ( python3 "$TESTDIR/fixtures/echo_path_h2server.py" "$port" "$PWD/echo.pem" "$PWD/echo.key" >"$PWD/echo_server.log" 2>&1 < /dev/null & )
```

```scrut
$ port=$(cat "$PWD/echo_port"); for i in $(seq 1 100); do curl -s -k -o /dev/null "https://127.0.0.1:$port/" && exit 0; sleep 0.05; done; echo "echo server on $port not reachable" >&2; exit 1
```

## Set up the dyn server (for the H1 test)

The raw H2 echo server only speaks HTTP/2. For the H1 line-terminator
test we need a server that speaks HTTP/1.1 — the dyn server (hypercorn)
serves both H1 and H2 with dynamic `?body=&status=` responses.

```scrut
$ setup_dyn_server
```

```scrut {detached: true, detached_kill_signal: term}
$ port=$(cat "$PWD/dyn_port"); ( hypercorn --bind "127.0.0.1:$port" --keyfile "$PWD/dyn.key" --certfile "$PWD/dyn.pem" "$PWD/dyn_asgi.py:app" >"$PWD/dyn_server.log" 2>&1 < /dev/null & )
```

```scrut
$ wait_for_dyn_server
```

## A literal CRLF in the `:path` splits the parse by default

With the default `\r\n` terminator, a `:path` containing `\r\n`
splits the request line at the first `\n`, and the parser rejects the
truncated line. Create a request with the default terminator and
attempt to send: the parse fails before the socket opens.

```scrut
$ port=$(cat "$PWD/echo_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name smuggle-default --host 127.0.0.1 --port $port --force > /dev/null
```

Write a request whose `:path` contains `\r\nX-Evil:yes`. With the
default terminator the parser sees the first line as `GET /foo\r`
and rejects it (invalid request line — missing version token).

```scrut
$ port=$(cat "$PWD/echo_port"); printf 'GET /foo\r\nX-Evil:yes HTTP/2\r\nHost: 127.0.0.1:%s\r\n\r\n' "$port" > "$PWD/data/requests/smuggle-default/request"
```

```scrut {output_stream: stderr}
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request send --name smuggle-default --insecure --timeout 10
[1]
Error: * (glob)
```

## `--line-terminator '\x00'` preserves CRLF into the H2 frame

Set the line terminator to `\x00` so the parser splits on null bytes,
never on `\r\n`. The request line `GET /foo\r\nX-Evil:yes HTTP/2` is
one line (no `\x00` in it), and the `:path` value is `/foo\r\nX-Evil:yes`
— the CRLF is preserved. The H2 frame carries this path verbatim; the
echo server receives it and echoes it back.

```scrut
$ port=$(cat "$PWD/echo_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name h2smuggle --host 127.0.0.1 --port $port --line-terminator '\x00' --force > /dev/null
```

The `meta.json` carries the line terminator as a list of byte ints
(`[0]` = one null byte).

```scrut
$ python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("line_terminator"))' \
>   "$PWD/data/requests/h2smuggle/meta.json" | tr -d '[]'
0
```

Write the request file using `\x00` as the line terminator. The path
contains `\r\nX-Evil:yes` (no spaces in the injected part, since the
request line is split on spaces to find method/path/version).

```scrut
$ port=$(cat "$PWD/echo_port"); printf 'GET /foo\r\nX-Evil:yes HTTP/2\x00Host: 127.0.0.1:%s\x00\x00' "$port" > "$PWD/data/requests/h2smuggle/request"
```

Send it. The H2 frame carries `:path = /foo\r\nX-Evil:yes`; the echo
server returns that path as the body.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request send --name h2smuggle --insecure --timeout 10 \
>   | grep -E '^status_code: 200$'
status_code: 200
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json \
>   request send --name h2smuggle --insecure --timeout 10 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' > "$PWD/h2smuggle_flow_id"
```

```scrut
$ h2smuggle_flow_id=$(cat "$PWD/h2smuggle_flow_id")
```

The response body is the `:path` the server received. It must contain
the injected `\r\nX-Evil:yes` — proving the CRLF survived the parse and
landed in the H2 frame on the wire.

```scrut
$ python3 -c 'import sys; b=open(sys.argv[1],"rb").read(); \
>   ok=b"/foo\r\nX-Evil:yes" in b; \
>   print("crlf preserved in :path" if ok else "MISSING: "+repr(b))' \
>   "$PWD/data/flows/$h2smuggle_flow_id/response_body.txt"
crlf preserved in :path
```

## `--line-terminator` is ignored for HTTP/1.1 (wire-faithful)

H1 request files go on the socket verbatim; re-framing them is
meaningless. Setting `--line-terminator` on an H1 request has no
effect — the parser always uses `\r\n` for H1. An H1 `Host` value
already goes on the wire as-is (the file is the wire bytes), so no
framing knob is needed.

```scrut
$ port=$(cat "$PWD/dyn_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name h1-lt --host 127.0.0.1 --port $port --line-terminator '\x00' --force > /dev/null
```

Write a normal CRLF-terminated H1 request.

```scrut
$ port=$(cat "$PWD/dyn_port"); printf 'GET /a?body=h1-lt-ok&status=200 HTTP/1.1\r\nHost: 127.0.0.1:%s\r\nConnection: close\r\n\r\n' "$port" > "$PWD/data/requests/h1-lt/request"
```

The send succeeds — the `\x00` line terminator in `meta.json` is
ignored because the request line says `HTTP/1.1`. The parser used
`\r\n` (the default for H1) and the request went on the wire verbatim.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request send --name h1-lt --insecure --timeout 10 \
>   | grep -E '^status_code: 200$'
status_code: 200
```

The response body confirms the request reached the dyn server.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json \
>   request send --name h1-lt --insecure --timeout 10 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' > "$PWD/h1_lt_flow_id"
```

```scrut
$ h1_lt_flow_id=$(cat "$PWD/h1_lt_flow_id")
```

```scrut
$ cat "$PWD/data/flows/$h1_lt_flow_id/response_body.txt"
h1-lt-ok (no-eol)
```

## Teardown: stop the servers

Stop the dyn server (hypercorn) and the raw H2 echo server.

```scrut
$ stop_dyn_server
```

Kill the raw h2 server by its script name (not hypercorn) and fail
loud if it survives the bounded poll.

```scrut
$ pkill -f "echo_path_h2server.py" 2>/dev/null || true
```

```scrut
$ for i in $(seq 1 100); do pgrep -f "echo_path_h2server.py" >/dev/null || exit 0; sleep 0.05; done; echo "echo_path_h2server still running" >&2; exit 1
```