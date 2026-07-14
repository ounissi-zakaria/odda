---
prepend:
  - _lib/boot.md
append:
  - _lib/teardown.md
---

# Proxy and flow capture

A captured HTTP flow through `odda proxy-url` lands in
`$data_dir/flows/flows.jsonl` plus a per-flow directory with
`request`, `response_headers`, `response_body.<ext>`, all
read-only (mode 0444).

## Helper: local HTTP server to capture against

We start a Python HTTP server in its own session so it survives
this test case exiting. The PID is written to `$PWD/http.pid` so
the teardown case can kill it.

```scrut {detached: true, detached_kill_signal: term}
$ ( mkdir -p "$PWD/site" && \
>   echo "<html><body>hello from odda flow capture test</body></html>" \
>     > "$PWD/site/index.html" && \
>   python3 -m http.server 8765 --bind 127.0.0.1 \
>     --directory "$PWD/site" >"$PWD/http.log" 2>&1 < /dev/null & )
```

```scrut
$ pgrep -f "http.server 8765.*$PWD/site" | head -1 > "$PWD/http.pid"
```

```scrut
$ cat "$PWD/http.pid"
* (glob)
```

```scrut
$ for i in $(seq 1 30); do curl -s -o /dev/null http://127.0.0.1:8765/ && exit 0; sleep 0.5; done; exit 1
```

## `odda proxy-url` returns the proxy URL

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" proxy-url
http://127.0.0.1:* (glob)
```

## Send one request through the proxy

```scrut
$ curl -s -x "$(odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" proxy-url)" \
>   http://127.0.0.1:8765/ -o /dev/null -w "curl_status=%{http_code}\n"
curl_status=200
```

Give mitmproxy a moment to flush the flow to disk.

```scrut
$ sleep 1
```

## `flows.jsonl` is created and non-empty

```scrut
$ test -s "$PWD/data/flows/flows.jsonl" && echo "present"
present
```

```scrut
$ wc -l < "$PWD/data/flows/flows.jsonl" | awk '{ print ($1 >= 1) ? "ok" : "empty" }'
ok
```

## The captured flow is for our localhost request

The browser-foreground background traffic also goes through the proxy,
so `flows.jsonl` may have several entries. We pick the one targeting
`127.0.0.1` to find *our* flow.

```scrut
$ grep '"host": "127.0.0.1"' "$PWD/data/flows/flows.jsonl" | head -n 1 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' > "$PWD/curl_id"
```

```scrut
$ cat "$PWD/curl_id"
* (glob)
```

```scrut
$ curl_id=$(cat "$PWD/curl_id")
```

```scrut
$ test -d "$PWD/data/flows/$curl_id" && echo "dir present"
dir present
```

## The flow directory has `request`, `response_headers`, `response_body.<ext>`

```scrut
$ curl_id=$(cat "$PWD/curl_id")
```

```scrut
$ test -e "$PWD/data/flows/$curl_id/request" \
>   -a -e "$PWD/data/flows/$curl_id/response_headers" \
>   -a -e "$PWD/data/flows/$curl_id/response_body.html" \
>   && echo "files present"
files present
```

## Per-flow files are read-only (mode 0444)

```scrut
$ curl_id=$(cat "$PWD/curl_id")
```

```scrut
$ for f in request response_headers response_body.html; do
>   p=$(stat -c '%a' "$PWD/data/flows/$curl_id/$f")
>   test "$p" = "444" || { echo "FAIL: $f mode=$p"; exit 1; }
> done && echo "all read-only"
all read-only
```

## Teardown: stop the local HTTP server

The local HTTP server is started in a subshell so it survives this
test case's bash exiting. Kill it explicitly here so the test doc
finishes cleanly.

```scrut
$ pkill -f "http.server 8765.*$PWD/site" 2>/dev/null
```

```scrut
$ sleep 1
```

```scrut
$ pgrep -f "http.server 8765.*$PWD/site" >/dev/null && echo "still running" || echo "stopped"
stopped
```
