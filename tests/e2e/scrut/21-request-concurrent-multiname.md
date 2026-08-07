---
prepend:
  - _lib/boot.md
  - _lib/fixture-server.md
  - _lib/dyn-server.md
append:
  - _lib/teardown.md
---

# `odda request send` multi-name over HTTP/2 (concurrent stream-multiplex)

ADR-0020 lifts ADR-0019's rejection of H2 in the multi-name pipeline:
`--name a --name b ...` where every request line says `HTTP/2` now opens
one H2 connection and sends all N as concurrent streams (stream
multiplexing with the last-byte single-packet technique) — the
multi-endpoint race path (N *different* requests fired concurrently to
exploit a state-machine race). H1 multi-name stays sequential
keep-alive (frozen, smuggling).

## Set up the dyn server

```scrut
$ export ODDA_RACE_DIR="$PWD/race" && setup_dyn_server
```

```scrut {detached: true, detached_kill_signal: term}
$ port=$(cat "$PWD/dyn_port"); ODDA_RACE_DIR="$PWD/race" hypercorn --bind "127.0.0.1:$port" --keyfile "$PWD/dyn.key" --certfile "$PWD/dyn.pem" "$PWD/dyn_asgi.py:app" >"$PWD/dyn_server.log" 2>&1 < /dev/null &
```

```scrut
$ wait_for_dyn_server
```

## Multi-name over H2 fires concurrently (N different paths)

Three HTTP/2 requests to different paths, sent on one connection as
concurrent streams. Each flow has its own body (the path-specific query
string controls the response body). The `--json` output is a list of 3
records, all 200, distinct ids.

```scrut
$ port=$(cat "$PWD/dyn_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name h2-a --host 127.0.0.1 --port $port --force > /dev/null
```

```scrut
$ port=$(cat "$PWD/dyn_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name h2-b --host 127.0.0.1 --port $port --force > /dev/null
```

```scrut
$ port=$(cat "$PWD/dyn_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name h2-c --host 127.0.0.1 --port $port --force > /dev/null
```

```scrut
$ port=$(cat "$PWD/dyn_port"); printf 'GET /a?body=aaa&status=200&header=Content-Type:application/json&race=h2mn HTTP/2\r\nuser-agent: odda-test\r\n\r\n' > "$PWD/data/requests/h2-a/request"
```

```scrut
$ port=$(cat "$PWD/dyn_port"); printf 'GET /b?body=bbb&status=200&header=Content-Type:application/json&race=h2mn HTTP/2\r\nuser-agent: odda-test\r\n\r\n' > "$PWD/data/requests/h2-b/request"
```

```scrut
$ port=$(cat "$PWD/dyn_port"); printf 'GET /c?body=ccc&status=200&header=Content-Type:application/json&race=h2mn HTTP/2\r\nuser-agent: odda-test\r\n\r\n' > "$PWD/data/requests/h2-c/request"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json \
>   request send --name h2-a --name h2-b --name h2-c --insecure --timeout 10 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(isinstance(d,list) and len(d)==3 and all(r["status_code"]==200 for r in d) and len({r["id"] for r in d})==3)'
True
```

The 3 bodies come back in send order (a, b, c).

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json \
>   request send --name h2-a --name h2-b --name h2-c --insecure --timeout 10 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); ids=[r["id"] for r in d]; print(ids[0])' > "$PWD/mn_a"
```

```scrut
$ mn_a=$(cat "$PWD/mn_a")
```

```scrut
$ cat "$PWD/data/flows/$mn_a/response_body.json"
aaa (no-eol)
```

## Single-packet: the 3 arrivals land in a tight window

The last-byte single-packet technique flushes all 3 HEADERS in one TLS
record, so the 3 arrivals should land within a few ms (local server).
Clear the file first (the preceding send appended), then assert spread
< 50ms.

```scrut
$ rm -f "$ODDA_RACE_DIR/h2mn"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json \
>   request send --name h2-a --name h2-b --name h2-c --insecure --timeout 10 > /dev/null
```

```scrut
$ python3 -c 'import sys; ts=[int(l) for l in open(sys.argv[1])]; \
>   spread=(max(ts)-min(ts))/1_000_000; print("spread<50ms" if spread<50 else f"spread={spread:.1f}ms")' \
>   "$ODDA_RACE_DIR/h2mn"
spread<50ms
```

## Multi-name over H1 stays sequential (frozen, ADR-0019)

Two HTTP/1.1 requests on one connection (sequential keep-alive). The
H1 path is unchanged — this is the smuggling pipeline, not the race
path. The arrival spread is wider (one connection, sequential).

```scrut
$ port=$(cat "$PWD/dyn_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name h1-a --host 127.0.0.1 --port $port --force > /dev/null
```

```scrut
$ port=$(cat "$PWD/dyn_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name h1-b --host 127.0.0.1 --port $port --force > /dev/null
```

```scrut
$ port=$(cat "$PWD/dyn_port"); printf 'GET /a?body=h1a&status=200&header=Content-Type:application/json HTTP/1.1\r\nHost: 127.0.0.1:%s\r\nConnection: keep-alive\r\n\r\n' "$port" > "$PWD/data/requests/h1-a/request"
```

```scrut
$ port=$(cat "$PWD/dyn_port"); printf 'GET /a?body=h1b&status=200&header=Content-Type:application/json HTTP/1.1\r\nHost: 127.0.0.1:%s\r\nConnection: keep-alive\r\n\r\n' "$port" > "$PWD/data/requests/h1-b/request"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json \
>   request send --name h1-a --name h1-b --insecure --timeout 10 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(isinstance(d,list) and len(d)==2 and all(r["status_code"]==200 for r in d))'
True
```

## Mixed H1 + H2 request lines in multi-name is rejected

Multi-name send cannot mix HTTP/1.1 and HTTP/2 request lines: H1 is
sequential keep-alive, H2 is concurrent stream-multiplex — different
mechanisms on one connection make no sense. Reject pre-emptively.

```scrut {output_stream: stderr}
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request send --name h1-a --name h2-a --insecure --timeout 10
[1]
Error: * (glob)
```

## `--pipelining` with H2 multi-name is rejected

`--pipelining` is an H1-only concept (send-all-then-read-all sequential).
With H2 multi-name (concurrent), it's meaningless.

```scrut {output_stream: stderr}
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request send --name h2-a --name h2-b --pipelining --insecure --timeout 10
[1]
Error: * (glob)
```

## Teardown: stop the dyn server

```scrut
$ stop_dyn_server
```