---
prepend:
  - _lib/setup.md
---

# `odda request clone`

Clones a captured flow into an editable request. The captured flow
must come from a previous `curl` (or browser navigation) through
the proxy, which is set up at the start of this document.

## Boot the server

```scrut {detached: true, detached_kill_signal: term}
$ ( "$ODDA_BIN" server --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   >"$PWD/server.log" 2>&1 & )
```

```scrut {wait: {timeout: 10s, path: "odda.sock"}}
$ echo "server is up"
server is up
```

## Capture a flow through the proxy

Use `curl` through `proxy-url` against the local `xs2.top` testing
server. The body, status, and a header are all controlled by the
query string so we can verify them later.

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" proxy-url > "$PWD/proxy_url"
```

```scrut
$ curl -s -x "$(cat "$PWD/proxy_url")" -k --proxy-insecure \
>   'https://xs2.top/a?body=clone-test&status=200&header=Content-Type:application/json' \
>   -o /dev/null -w "curl_status=%{http_code}\n"
curl_status=200
```

```scrut
$ sleep 1
```

## Find the flow id for `xs2.top`

```scrut
$ grep '"host": "xs2.top"' "$PWD/data/flows/flows.jsonl" | head -n 1 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' > "$PWD/flow_id"
```

```scrut
$ cat "$PWD/flow_id"
* (glob)
```

## `request clone` writes a `request` and `meta.json`


```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request clone "$flow_id" --name clone-test \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["name"], d["scheme"], d["host"], d["port"])'
clone-test https xs2.top 443
```

The cloned request files exist and are non-empty.

```scrut
$ test -s "$PWD/data/requests/clone-test/request" && echo "request non-empty"
request non-empty
```

```scrut
$ test -s "$PWD/data/requests/clone-test/meta.json" && echo "meta.json non-empty"
meta.json non-empty
```

## A second `request clone` on the same name refuses without `--force`


```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request clone "$flow_id" --name clone-test 2>&1 \
>   | grep -F "already exists" >/dev/null && echo "refused" || echo "ERROR: did not refuse"
refused
```

## `--force` overwrites the existing request


```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   request clone "$flow_id" --name clone-test --force \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["name"], d["scheme"], d["host"], d["port"])'
clone-test https xs2.top 443
```

## Teardown: stop the odda server

```scrut
$ pkill -f "odda.*--data-dir $PWD/data" 2>/dev/null || true
```
