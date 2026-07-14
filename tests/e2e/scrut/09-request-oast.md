---
prepend:
  - _lib/setup.md
---

# `odda request send` against an OAST callback (interactsh)

`odda request send` makes a real outbound connection, so it
naturally reaches an `interactsh` callback. This test is
**skipped** when `interactsh-client` is not on `$PATH`.

If skipped, every test case in this document exits with code 80
(`skip_document_code` default) and the whole document is reported
as skipped.

## Boot the server

```scrut {detached: true, detached_kill_signal: term}
$ ( "$ODDA_BIN" server --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   >"$PWD/server.log" 2>&1 & )
```

```scrut {wait: {timeout: 10s, path: "odda.sock"}}
$ echo "server is up"
server is up
```

## Skip the whole document if `interactsh-client` is not installed

```scrut
$ command -v interactsh-client >/dev/null 2>&1 && echo "have interactsh" || echo "missing"
* (glob)
```

```scrut
$ command -v interactsh-client >/dev/null 2>&1 || exit 80
```

```scrut
$ echo "ok"
ok
```

## Start `interactsh-client` in the background and grab a payload domain

```scrut {detached: true, detached_kill_signal: term}
$ ( nohup interactsh-client -n 1 > "$PWD/interactsh.log" 2>&1 & )
```

```scrut
$ for i in $(seq 1 30); do grep -qE '[a-z0-9]+\.xs2\.top' "$PWD/interactsh.log" 2>/dev/null && exit 0; sleep 0.5; done; exit 1
```

```scrut
$ grep -oE '[a-z0-9]+\.xs2\.top' "$PWD/interactsh.log" | head -n 1 > "$PWD/payload"
```

```scrut
$ cat "$PWD/payload"
* (glob)
```

## Send a request to the payload domain

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request new --name oast-test --host xs2.top --force > /dev/null
```

```scrut
$ payload=$(cat "$PWD/payload")
```

```scrut
$ sed 's/$/\r/' > "$PWD/data/requests/oast-test/request" <<REQEOF
> GET /oast-callback-test HTTP/1.1
> Host: $payload
> Connection: close
> 
> REQEOF
```

```scrut
$ payload=$(cat "$PWD/payload")
```

```scrut
$ cat > "$PWD/data/requests/oast-test/meta.json" <<METAEOF
> {"scheme": "https", "host": "$payload", "port": 443}
> METAEOF
```

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request send oast-test --timeout 10 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print("error" in d or "status_code" in d)'
True
```

Wait for the interaction to register at the `interactsh` side, then
check the log.

```scrut
$ sleep 5
```

```scrut
$ pkill -f "interactsh-client -n 1" 2>/dev/null || true
```

```scrut
$ sleep 1
```

```scrut
$ grep -qi "oast-callback-test" "$PWD/interactsh.log" \
>   && echo "interaction received" || echo "no interaction"
interaction received
```

## Teardown: stop interactsh-client and the odda server

```scrut
$ pkill -f "interactsh-client -n 1" 2>/dev/null || true
```

```scrut
$ pkill -f "odda.*--data-dir $PWD/data" 2>/dev/null || true
```
