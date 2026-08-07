---
prepend:
  - _lib/boot.md
  - _lib/fixture-server.md
  - _lib/dyn-server.md
append:
  - _lib/teardown.md
---

# `odda request send --repeat N` (concurrent send for race conditions)

`request send --name <n> --repeat N` sends N concurrent copies of one
request — the race / limit-overrun path (ADR-0020). HTTP/2 uses stream
multiplexing with the last-byte single-packet technique (all N HEADERS
frames in one TLS record); HTTP/1.1 opens N parallel connections (H1
cannot multiplex on one connection, and pipelining is server-side
sequential). Multi-name + `--repeat` and `--pipelining` + `--repeat` are
rejected pre-emptively.

## Set up the dyn server

The dyn server speaks HTTP/1.1 and HTTP/2 over TLS with a self-signed
cert. The `?race=<id>` query param records arrival timestamps to
`$ODDA_RACE_DIR/<id>` so the single-packet property (all N arrive in a
tight window) is observable.

```scrut
$ export ODDA_RACE_DIR="$PWD/race" && setup_dyn_server
```

```scrut {detached: true, detached_kill_signal: term}
$ port=$(cat "$PWD/dyn_port"); ODDA_RACE_DIR="$PWD/race" hypercorn --bind "127.0.0.1:$port" --keyfile "$PWD/dyn.key" --certfile "$PWD/dyn.pem" "$PWD/dyn_asgi.py:app" >"$PWD/dyn_server.log" 2>&1 < /dev/null &
```

```scrut
$ wait_for_dyn_server
```

## `--repeat` over HTTP/2: N concurrent copies, N flow records

One HTTP/2 request, `--repeat 5`. The output is a list of 5 flow records
in `--json`; text renders 5 `key: value` blocks. Each flow has a distinct
id (one-request-one-response, ADR-0019 invariant), and all 5 succeed.

```scrut
$ port=$(cat "$PWD/dyn_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name h2-race --host 127.0.0.1 --port $port --force > /dev/null
```

```scrut
$ port=$(cat "$PWD/dyn_port"); printf 'GET /a?body=ok&status=200&header=Content-Type:application/json&race=h2 HTTP/2\r\nuser-agent: odda-test\r\naccept: */*\r\n\r\n' > "$PWD/data/requests/h2-race/request"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json \
>   request send --name h2-race --repeat 5 --insecure --timeout 10 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(isinstance(d,list) and len(d)==5 and all(r["status_code"]==200 for r in d))'
True
```

All 5 ids are distinct (one flow per copy).

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json \
>   request send --name h2-race --repeat 5 --insecure --timeout 10 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(len({r["id"] for r in d})==5)'
True
```

Text output is 5 blocks (5 `status_code: 200` lines).

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request send --name h2-race --repeat 5 --insecure --timeout 10 \
>   | grep -cE '^status_code: 200$'
5
```

## The single-packet property: all 5 arrive in a tight window

The `?race=h2` param records arrival timestamps to `$ODDA_RACE_DIR/h2`.
The single-packet technique flushes all 5 HEADERS frames in one TLS
record, so the 5 arrivals should land within a few milliseconds (local
server, no network latency). A sequential send would spread them across
5 read-response round trips. Clear the file first (earlier sends in this
document appended), then assert the max-min spread is under 50ms.

```scrut
$ rm -f "$ODDA_RACE_DIR/h2"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json \
>   request send --name h2-race --repeat 5 --insecure --timeout 10 > /dev/null
```

```scrut
$ python3 -c 'import sys; ts=[int(l) for l in open(sys.argv[1])]; \
>   spread=(max(ts)-min(ts))/1_000_000; print("spread<50ms" if spread<50 else f"spread={spread:.1f}ms")' \
>   "$ODDA_RACE_DIR/h2"
spread<50ms
```

## `--repeat` over HTTP/1.1: N parallel connections

HTTP/1.1 cannot stream-multiplex on one connection, so `--repeat` opens
N parallel TCP/TLS connections. The 5 flow records still come back, but
the arrival spread is wider (each connection does its own TLS
handshake). The mechanism is still validated by the flow count.

```scrut
$ port=$(cat "$PWD/dyn_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name h1-race --host 127.0.0.1 --port $port --force > /dev/null
```

```scrut
$ port=$(cat "$PWD/dyn_port"); printf 'GET /a?body=ok&status=200&header=Content-Type:application/json&race=h1 HTTP/1.1\r\nHost: 127.0.0.1:%s\r\nConnection: close\r\n\r\n' "$port" > "$PWD/data/requests/h1-race/request"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json \
>   request send --name h1-race --repeat 5 --insecure --timeout 10 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(isinstance(d,list) and len(d)==5 and all(r["status_code"]==200 for r in d))'
True
```

## Last-byte single-packet works with a POST body

The tested race pattern is a POST (e.g. coupon-apply). The last-byte
technique holds back the final body byte and flushes it for all N
streams at once, so the requests complete simultaneously. A POST with a
small body over HTTP/2 with `--repeat 4` should produce 4 successful
flows.

```scrut
$ port=$(cat "$PWD/dyn_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name h2-post --host 127.0.0.1 --port $port --force > /dev/null
```

```scrut
$ port=$(cat "$PWD/dyn_port"); printf 'POST /a?body=posted&status=200&header=Content-Type:application/json&race=h2post HTTP/2\r\nHost: 127.0.0.1:%s\r\nContent-Type: application/x-www-form-urlencoded\r\nContent-Length: 10\r\n\r\ncoupon=X20' "$port" > "$PWD/data/requests/h2-post/request"
```

```scrut
$ rm -f "$ODDA_RACE_DIR/h2post"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json \
>   request send --name h2-post --repeat 4 --insecure --timeout 10 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(isinstance(d,list) and len(d)==4 and all(r["status_code"]==200 for r in d))'
True
```

```scrut
$ python3 -c 'import sys; ts=[int(l) for l in open(sys.argv[1])]; \
>   spread=(max(ts)-min(ts))/1_000_000; print("spread<50ms" if spread<50 else f"spread={spread:.1f}ms")' \
>   "$ODDA_RACE_DIR/h2post"
spread<50ms
```

## `--repeat` with a large body still completes (last-byte correctness)

The last-byte technique must not break when the body is large enough to
span multiple DATA frames. A 4KB body over HTTP/2 with `--repeat 3`
should still produce 3 successful flows — proving the body-split logic
(send all-but-last byte, then the last byte) handles multi-frame bodies.

```scrut
$ port=$(cat "$PWD/dyn_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name h2-big --host 127.0.0.1 --port $port --force > /dev/null
```

```scrut
$ port=$(cat "$PWD/dyn_port"); python3 -c '
> import sys
> body = b"x" * 4096
> cl = len(body)
> req = f"POST /a?body=big-ok&status=200&header=Content-Type:text/plain&race=h2big HTTP/2\r\nHost: 127.0.0.1:{sys.argv[1]}\r\nContent-Type: application/octet-stream\r\nContent-Length: {cl}\r\n\r\n".encode() + body
> sys.stdout.buffer.write(req)
> ' "$port" > "$PWD/data/requests/h2-big/request"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json \
>   request send --name h2-big --repeat 3 --insecure --timeout 10 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(isinstance(d,list) and len(d)==3 and all(r["status_code"]==200 for r in d))'
True
```

## `--fix-content-length` + `--repeat` is allowed

The multi-name rejection of `--fix-content-length` (protecting
intentionally-wrong CLs for smuggling) does not transfer to
same-request-N-times — a race is never a CL-differential attack. So
`--fix-content-length --repeat N` recomputes CL once and fires N copies.

```scrut
$ port=$(cat "$PWD/dyn_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name cl-repeat --host 127.0.0.1 --port $port --force > /dev/null
```

```scrut
$ port=$(cat "$PWD/dyn_port"); printf 'POST /a?body=cl-rep-ok&status=200&header=Content-Type:application/json HTTP/2\r\nHost: 127.0.0.1:%s\r\nContent-Type: application/json\r\nContent-Length: 999\r\n\r\n{"k":"v"}' "$port" > "$PWD/data/requests/cl-repeat/request"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json \
>   request send --name cl-repeat --repeat 3 --fix-content-length --insecure --timeout 10 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(isinstance(d,list) and len(d)==3 and all(r["status_code"]==200 for r in d))'
True
```

## `--repeat` + multiple `--name` is rejected

The combo is ambiguous (N-per-name vs N-total). Single-name only.

```scrut {output_stream: stderr}
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request send --name h2-race --name h1-race --repeat 3 --insecure --timeout 10
[1]
Error: * (glob)
```

## `--repeat` + `--pipelining` is rejected

`--repeat` is concurrent; `--pipelining` is H1 multi-name sequential.

```scrut {output_stream: stderr}
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request send --name h2-race --repeat 3 --pipelining --insecure --timeout 10
[1]
Error: * (glob)
```

## `--repeat 1` is single-shot (no concurrent send)

`--repeat 1` (the default) keeps the frozen single-name contract: one
flow record, one `--json` object (not a list).

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json \
>   request send --name h2-race --repeat 1 --insecure --timeout 10 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(isinstance(d,dict))'
True
```

## Teardown: stop the dyn server

```scrut
$ stop_dyn_server
```