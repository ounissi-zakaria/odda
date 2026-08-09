---
prepend:
  - _lib/boot.md
  - _lib/fixture-server.md
  - _lib/dyn-server.md
append:
  - _lib/teardown.md
---

# `odda request send` multi-name pipeline (HTTP/1.1 same-connection)

`request send --name a --name b` sends multiple editable requests on one
HTTP/1.1 connection (sequential keep-alive by default; `--pipelining` for
send-all-then-read-all). Single-name `send` is frozen (see `07-request-send-h1.md`
and `08-request-send-extra.md`); this document covers only the multi-name
behavior and the pre-emptive rejections.

## Set up the dyn server

```scrut
$ setup_dyn_server
```

```scrut {detached: true, detached_kill_signal: term}
$ port=$(cat "$PWD/dyn_port"); ( hypercorn --bind "127.0.0.1:$port" --keyfile "$PWD/dyn.key" --certfile "$PWD/dyn.pem" "$PWD/dyn_asgi.py:app" >"$PWD/dyn_server.log" 2>&1 < /dev/null & )
```

```scrut
$ wait_for_dyn_server
```

## Multi-name send returns a list in `--json` and two blocks in text

Two editable requests against the dyn server, sent on one connection in
sequence (default keep-alive). The `--json` output is a list of two flow
records; the text output is two `key: value` blocks separated by a blank
line.

```scrut
$ port=$(cat "$PWD/dyn_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name pipe-a --host 127.0.0.1 --port $port --force > /dev/null
```

```scrut
$ port=$(cat "$PWD/dyn_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name pipe-b --host 127.0.0.1 --port $port --force > /dev/null
```

```scrut
$ port=$(cat "$PWD/dyn_port"); printf 'GET /a?body=pipe-a-ok&status=200&header=Content-Type:application/json HTTP/1.1\r\nHost: 127.0.0.1:%s\r\nConnection: keep-alive\r\n\r\n' "$port" > "$PWD/data/requests/pipe-a/request"
```

```scrut
$ port=$(cat "$PWD/dyn_port"); printf 'GET /a?body=pipe-b-ok&status=200&header=Content-Type:application/json HTTP/1.1\r\nHost: 127.0.0.1:%s\r\nConnection: keep-alive\r\n\r\n' "$port" > "$PWD/data/requests/pipe-b/request"
```

`--json` returns a list of two records.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json \
>   request send --name pipe-a --name pipe-b --insecure --timeout 10 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(isinstance(d,list) and len(d)==2)'
True
```

Each record is a normal flow record with its own `id`, `method`, and
`status_code`. The two ids are distinct (separate flows), and the bodies
come back as the values we asked for in each query string.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json \
>   request send --name pipe-a --name pipe-b --insecure --timeout 10 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d[0]["id"]!=d[1]["id"], d[0]["status_code"], d[1]["status_code"])'
True 200 200
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json \
>   request send --name pipe-a --name pipe-b --insecure --timeout 10 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d[0]["id"])' > "$PWD/pipe_id_a"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json \
>   request send --name pipe-a --name pipe-b --insecure --timeout 10 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d[1]["id"])' > "$PWD/pipe_id_b"
```

```scrut
$ a=$(cat "$PWD/pipe_id_a"); b=$(cat "$PWD/pipe_id_b")
```

```scrut
$ cat "$PWD/data/flows/$a/response_body.json"
pipe-a-ok (no-eol)
```

```scrut
$ cat "$PWD/data/flows/$b/response_body.json"
pipe-b-ok (no-eol)
```

Text output is two `key: value` blocks separated by a blank line. We
grep for the two `status_code: 200` lines and the blank separator
between them to assert the block shape.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request send --name pipe-a --name pipe-b --insecure --timeout 10 \
>   | grep -cE '^status_code: 200$'
2
```

## Single-name `send` is frozen: one record (object), not a list

One `--name` keeps today's exact output contract. `--json` returns a
single object, not a one-element list.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json \
>   request send --name pipe-a --insecure --timeout 10 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(isinstance(d,dict))'
True
```

## `--pipelining` sends all then reads all (still two records)

`--pipelining` writes both requests' bytes up front, then reads both
responses. The dyn server tolerates this (HTTP/1.1 pipelining); the
result is still two flow records.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json \
>   request send --name pipe-a --name pipe-b --pipelining --insecure --timeout 10 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(isinstance(d,list) and len(d)==2, d[0]["status_code"], d[1]["status_code"])'
True 200 200
```

## `--fix-content-length` + multi-name is rejected

`--fix-content-length` would overwrite the intentionally-wrong
`Content-Length` that smuggling payloads depend on. Reject pre-emptively
before the socket opens.

```scrut {output_stream: stderr}
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request send --name pipe-a --name pipe-b --fix-content-length --insecure --timeout 10
[1]
Error: * (glob)
```

## Mixed HTTP/1.1 + HTTP/2 request lines in multi-name is rejected

H1 multi-name is sequential keep-alive; H2 multi-name is concurrent
stream-multiplex (ADR-0020). Different mechanisms on one connection make
no sense, so a mix is rejected pre-emptively. (This document exercises
the H1 sequential path; the H2 concurrent path is covered in
`21-request-concurrent-multiname.md`.)

```scrut
$ port=$(cat "$PWD/dyn_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name h2-in-pipe --host 127.0.0.1 --port $port --force > /dev/null
```

```scrut
$ port=$(cat "$PWD/dyn_port"); printf 'GET /a?body=h2-no HTTP/2\r\nHost: 127.0.0.1:%s\r\n\r\n' "$port" > "$PWD/data/requests/h2-in-pipe/request"
```

```scrut {output_stream: stderr}
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request send --name pipe-a --name h2-in-pipe --insecure --timeout 10
[1]
Error: * (glob)
```

## Bare-body request (no Content-Length, no Transfer-Encoding) in multi-name is rejected

A body without framing requires `write_eof` (half-close) to terminate,
which ends the connection. Reject pre-emptively so the agent's client
stays a clean deterministic H1 sender. (Single-name `send` still allows
this via EOF; multi-name does not.)

```scrut
$ port=$(cat "$PWD/dyn_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name bare-body --host 127.0.0.1 --port $port --force > /dev/null
```

```scrut
$ port=$(cat "$PWD/dyn_port"); printf 'POST / HTTP/1.1\r\nHost: 127.0.0.1:%s\r\n\r\nhello' "$port" > "$PWD/data/requests/bare-body/request"
```

```scrut {output_stream: stderr}
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request send --name bare-body --name pipe-a --insecure --timeout 10
[1]
Error: * (glob)
```

## Mid-sequence timeout aborts the rest, recording them as aborted

Two requests on one connection, where the first request hangs (the dyn
server is told to never respond by pointing at a port with no listener
behind a tiny timeout). The first flow records the timeout error; the
second flow is recorded as "aborted" (its pre-written request file
already exists). `flows.jsonl` stays complete: every pre-written
request has a line.

We point the requests at a closed port to force a connect timeout on
the pipeline, which aborts both flows. (Connection-level failure is the
cleanest deterministic way to exercise the abort path without a flaky
hang-the-server setup.)

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name pipe-hang-a --host 127.0.0.1 --port 1 --force > /dev/null
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name pipe-hang-b --host 127.0.0.1 --port 1 --force > /dev/null
```

```scrut
$ port=$(cat "$PWD/dyn_port"); printf 'GET /a?body=x HTTP/1.1\r\nHost: 127.0.0.1:%s\r\nConnection: keep-alive\r\n\r\n' "$port" > "$PWD/data/requests/pipe-hang-a/request"
```

```scrut
$ port=$(cat "$PWD/dyn_port"); printf 'GET /a?body=y HTTP/1.1\r\nHost: 127.0.0.1:%s\r\nConnection: keep-alive\r\n\r\n' "$port" > "$PWD/data/requests/pipe-hang-b/request"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json \
>   request send --name pipe-hang-a --name pipe-hang-b --insecure --timeout 3 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(len(d), all(r.get("status_code") is None for r in d), all(r.get("error") for r in d))'
2 True True
```

## Mid-sequence connection close: step 1 succeeds, step 2 fails with a descriptive error

A true mid-sequence outcome (default sequential keep-alive): req1 with
`Connection: close` gets a 200 response; the server then closes the
connection. Req2's read hits EOF before any response headers arrive.
This must surface as a descriptive `error` on step 2 (not
`status_code: 0, error: null`, which is ambiguous — `0/null` could mean
"connection dropped" or "empty response"). Steps before the close keep
their responses; the failed step gets the underlying cause as its
`error`; any steps after the failed step are recorded as aborted with
an `"aborted: step N failed (...)"` message (ADR-0019).

```scrut
$ port=$(cat "$PWD/dyn_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name pipe-close-1 --host 127.0.0.1 --port $port --force > /dev/null
```

```scrut
$ port=$(cat "$PWD/dyn_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name pipe-close-2 --host 127.0.0.1 --port $port --force > /dev/null
```

```scrut
$ port=$(cat "$PWD/dyn_port"); printf 'GET /a?body=step1-ok&status=200&header=Content-Type:application/json HTTP/1.1\r\nHost: 127.0.0.1:%s\r\nConnection: close\r\n\r\n' "$port" > "$PWD/data/requests/pipe-close-1/request"
```

```scrut
$ port=$(cat "$PWD/dyn_port"); printf 'GET /a?body=step2 HTTP/1.1\r\nHost: 127.0.0.1:%s\r\nConnection: keep-alive\r\n\r\n' "$port" > "$PWD/data/requests/pipe-close-2/request"
```

Step 1 keeps its 200 response (`status_code: 200`, `error: null`). Step
2 is the *failed* step (the read hit EOF) — its `status_code` is `null`
(an error flow, not a `0` pseudo-response) and its `error` carries the
underlying cause. Both flow records are written.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json \
>   request send --name pipe-close-1 --name pipe-close-2 --insecure --timeout 10 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(len(d)==2, d[0]["status_code"]==200, d[0]["error"] is None, d[1]["status_code"] is None, isinstance(d[1]["error"],str) and len(d[1]["error"])>0)'
True True True True True
```

Step 2's error explains the connection closed before headers — not a
bare `null`. (There is no `"aborted: step ..."` prefix on step 2 itself;
that prefix is reserved for steps *after* the failed one. Step 2 is the
failed step, so it carries the cause directly.)

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json \
>   request send --name pipe-close-1 --name pipe-close-2 --insecure --timeout 10 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d[1]["error"])'
connection closed before response headers (glob)
```

Step 1's response body was preserved (it was read before the connection
closed).

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json \
>   request send --name pipe-close-1 --name pipe-close-2 --insecure --timeout 10 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d[0]["id"])' > "$PWD/pc1_id"
```

```scrut
$ pc1=$(cat "$PWD/pc1_id")
```

```scrut
$ cat "$PWD/data/flows/$pc1/response_body.json"
step1-ok (no-eol)
```

## `--pipelining` mid-pipeline close: step 1 succeeds, step 2 fails, step 3 aborted

`--pipelining` writes all N requests up front, then reads all N
responses in order. When the server closes after the first response
(`Connection: close` on request 1), responses 2..N hit EOF before
headers. The failed step (step 2) gets the underlying cause as its
`error`; every step after it (step 3) gets the
`"aborted: step 2 failed (...)"` prefix. No flow records `status_code:
0` with a `null` error.

The dyn server (hypercorn) rejects pipelined bytes after a
`Connection: close` with a `400` on the *first* response, which
obscures the abort attribution. A raw close-after-first fixture server
answers the first request cleanly then closes, discarding the
pipelined follow-ups — the real smuggling-target behavior
`--pipelining` is built for. Plain HTTP/1.1 (no TLS).

```scrut
$ pick_port > "$PWD/caf_port"
```

```scrut {detached: true, detached_kill_signal: term}
$ port=$(cat "$PWD/caf_port"); ( python3 "$TESTDIR/fixtures/close_after_first_server.py" "$port" >"$PWD/caf.log" 2>&1 < /dev/null & )
```

```scrut
$ for i in $(seq 1 100); do curl -s -o /dev/null "http://127.0.0.1:$(cat "$PWD/caf_port")/" && exit 0; sleep 0.05; done; echo "caf server not reachable" >&2; exit 1
```

```scrut
$ port=$(cat "$PWD/caf_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name pipe-abort-1 --host 127.0.0.1 --port $port --protocol http --force > /dev/null
```

```scrut
$ port=$(cat "$PWD/caf_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name pipe-abort-2 --host 127.0.0.1 --port $port --protocol http --force > /dev/null
```

```scrut
$ port=$(cat "$PWD/caf_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name pipe-abort-3 --host 127.0.0.1 --port $port --protocol http --force > /dev/null
```

```scrut
$ printf 'GET /a?body=pa1-ok&status=200 HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n' > "$PWD/data/requests/pipe-abort-1/request"
```

```scrut
$ printf 'GET /b HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: keep-alive\r\n\r\n' > "$PWD/data/requests/pipe-abort-2/request"
```

```scrut
$ printf 'GET /c HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: keep-alive\r\n\r\n' > "$PWD/data/requests/pipe-abort-3/request"
```

Step 1 returns 200 (`error: null`). Step 2 is the failed step —
`status_code: null`, `error` is the underlying cause. Step 3 is aborted
— `status_code: null`, `error` starts with `"aborted: step 2 failed"`
(the failed step is step 2, 1-based).

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json \
>   request send --name pipe-abort-1 --name pipe-abort-2 --name pipe-abort-3 --pipelining --timeout 10 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(len(d)==3, d[0]["status_code"]==200, d[0]["error"] is None, d[1]["status_code"] is None, isinstance(d[1]["error"],str) and "connection closed" in d[1]["error"], d[2]["status_code"] is None, isinstance(d[2]["error"],str) and d[2]["error"].startswith("aborted: step 2 failed"))'
True True True True True True True
```

Step 2's error is the underlying cause (no `"aborted: ..."` prefix —
it's the failed step, not a downstream abort).

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json \
>   request send --name pipe-abort-1 --name pipe-abort-2 --name pipe-abort-3 --pipelining --timeout 10 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d[1]["error"])'
connection closed before response headers (glob)
```

Step 3's error is the abort message naming the failed step (step 2).

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json \
>   request send --name pipe-abort-1 --name pipe-abort-2 --name pipe-abort-3 --pipelining --timeout 10 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d[2]["error"])'
aborted: step 2 failed (connection closed before response headers) (glob)
```

Stop the close-after-first fixture server.

```scrut
$ pkill -f "close_after_first_server.py $PWD" 2>/dev/null || true
```

```scrut
$ for i in $(seq 1 100); do pgrep -f "close_after_first_server.py $PWD" >/dev/null || exit 0; sleep 0.05; done; echo "caf server still running" >&2; exit 1
```

## `--pipelining` clean keep-alive pipeline: all 3 succeed (regression)

Regression guard: a clean 3-request `--pipelining` send to a
keep-alive server must still produce three `status_code: 200` records
with `error: null` — no false-positive aborts from the close-detection
fix.

```scrut
$ port=$(cat "$PWD/dyn_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name pipe-clean-1 --host 127.0.0.1 --port $port --force > /dev/null
```

```scrut
$ port=$(cat "$PWD/dyn_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name pipe-clean-2 --host 127.0.0.1 --port $port --force > /dev/null
```

```scrut
$ port=$(cat "$PWD/dyn_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name pipe-clean-3 --host 127.0.0.1 --port $port --force > /dev/null
```

```scrut
$ port=$(cat "$PWD/dyn_port"); printf 'GET /a?body=pc1-ok&status=200&header=Content-Type:application/json HTTP/1.1\r\nHost: 127.0.0.1:%s\r\nConnection: keep-alive\r\n\r\n' "$port" > "$PWD/data/requests/pipe-clean-1/request"
```

```scrut
$ port=$(cat "$PWD/dyn_port"); printf 'GET /a?body=pc2-ok&status=200&header=Content-Type:application/json HTTP/1.1\r\nHost: 127.0.0.1:%s\r\nConnection: keep-alive\r\n\r\n' "$port" > "$PWD/data/requests/pipe-clean-2/request"
```

```scrut
$ port=$(cat "$PWD/dyn_port"); printf 'GET /a?body=pc3-ok&status=200&header=Content-Type:application/json HTTP/1.1\r\nHost: 127.0.0.1:%s\r\nConnection: keep-alive\r\n\r\n' "$port" > "$PWD/data/requests/pipe-clean-3/request"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json \
>   request send --name pipe-clean-1 --name pipe-clean-2 --name pipe-clean-3 --pipelining --insecure --timeout 10 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(len(d)==3, all(r["status_code"]==200 for r in d), all(r["error"] is None for r in d))'
True True True
```

## Teardown: stop the dyn server

```scrut
$ stop_dyn_server
```