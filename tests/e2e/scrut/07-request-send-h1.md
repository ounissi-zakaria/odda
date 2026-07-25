---
prepend:
  - _lib/boot.md
  - _lib/fixture-server.md
  - _lib/dyn-server.md
append:
  - _lib/teardown.md
---

# `odda request new` + `request send` (HTTP/1.1)

Create an empty editable request, fill in a raw HTTP/1.1 request,
and `send` it. The sent request is recorded as a flow with the
scheme + port in `flows.jsonl`.

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

## `request new` creates an empty `request` and a `meta.json`

`request new` text output is `name`/`path`/`scheme`/`host`/`port`
lines. We grep the three stable lines (`path` is a temp-dir-dependent
absolute path, so it's not asserted here).

```scrut
$ port=$(cat "$PWD/dyn_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name h1-test --host 127.0.0.1 --port $port \
>   | grep -E '^(name: h1-test|scheme: https|host: 127\.0\.0\.1)$' | sort
host: 127.0.0.1
name: h1-test
scheme: https
```

```scrut
$ test -e "$PWD/data/requests/h1-test/request" && stat -c '%s' "$PWD/data/requests/h1-test/request"
0
```

## Write a raw HTTP/1.1 GET request to the `request` file

The `request` file needs CRLF line endings. The Host header must carry
the dyn server's port, so the request is written with `printf`
(substituting the port) rather than a literal heredoc.

```scrut
$ port=$(cat "$PWD/dyn_port"); printf 'GET /a?body=h1-send-test&status=200&header=Content-Type:application/json HTTP/1.1\r\nHost: 127.0.0.1:%s\r\nAccept: */*\r\n\r\n' "$port" > "$PWD/data/requests/h1-test/request"
```

## `request send` records the flow and returns the `flows.jsonl` record

`request send` text output is `id`/`method`/`scheme`/`host`/`port`/
`path`/`status_code`/`total_duration_ms`/`body_file`/`error` lines. We
grep out the `status_code` and `body_file` lines to assert them
directly (the flow id inside `body_file` is run-dependent, so it's
globbed).

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request send --name h1-test --insecure --timeout 10 \
>   | grep -E '^(status_code: 200|body_file: flows/.*/response_body\.json)$'
status_code: 200
body_file: flows/*/response_body.json (glob)
```

The response body is the body we asked for in the query string. To
read it back we need the flow id, so a second `request send` captures
the id into a shell var via `--json` (the documented way to pull a
structured value out for scripting).

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json \
>   request send --name h1-test --insecure --timeout 10 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' > "$PWD/flow_id"
```

```scrut
$ flow_id=$(cat "$PWD/flow_id")
```

```scrut
$ cat "$PWD/flow_id"
* (glob)
```


```scrut
$ cat "$PWD/data/flows/$flow_id/response_body.json"
h1-send-test (no-eol)
```

The stored request file uses `HTTP/1.1`.


```scrut
$ grep -F "HTTP/1.1" "$PWD/data/flows/$flow_id/request" >/dev/null && echo "h1 stored" || echo "missing"
h1 stored
```

`flows.jsonl` records the scheme and port.


```scrut
$ port=$(cat "$PWD/dyn_port"); grep "\"id\": \"$flow_id\"" "$PWD/data/flows/flows.jsonl" | grep -F '"scheme": "https"' >/dev/null \
>   && grep "\"id\": \"$flow_id\"" "$PWD/data/flows/flows.jsonl" | grep -F "\"port\": $port" >/dev/null \
>   && echo "jsonl has scheme+port" || echo "missing"
jsonl has scheme+port
```

## Teardown: stop the dyn server

```scrut
$ stop_dyn_server
```