---
prepend:
  - _lib/boot.md
  - _lib/fixture-server.md
append:
  - _lib/teardown.md
---

# Proxy and flow capture

A captured HTTP flow through `odda proxy-url` lands in
`$data_dir/flows/flows.jsonl` plus a per-flow directory with
`request`, `response_headers`, `response_body.<ext>`, all
read-only (mode 0444).

## Set up the fixture server

The fixture page uses a unique body string so we can match it in
`flows.jsonl` unambiguously.

```scrut
$ mkdir -p "$PWD/site" && \
>   echo "<html><body>odda-flow-capture-marker</body></html>" > "$PWD/site/index.html"
```

```scrut
$ pick_port > "$PWD/fixture_port"
```

```scrut {detached: true, detached_kill_signal: term}
$ port=$(cat "$PWD/fixture_port"); ( python3 -m http.server "$port" --bind 127.0.0.1 --directory "$PWD/site" >"$PWD/http.log" 2>&1 < /dev/null & )
```

```scrut
$ wait_for_fixture_server
```

## `odda proxy-url` returns the proxy URL

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" proxy-url
http://127.0.0.1:* (glob)
```

## Send one request through the proxy

Use a unique query parameter so we can match this specific flow in
`flows.jsonl` unambiguously.

```scrut
$ port=$(cat "$PWD/fixture_port"); curl -s -x "$(odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" proxy-url)" \
>   "http://127.0.0.1:$port/?marker=flow-capture-test" -o /dev/null -w "curl_status=%{http_code}\n"
curl_status=200
```

Wait for mitmproxy to flush the flow to disk, matching the unique
query parameter.

```scrut
$ wait_for_flow "flow-capture-test"
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
so `flows.jsonl` may have several entries. We pick the one with our
unique query parameter to find *our* flow.

```scrut
$ grep 'flow-capture-test' "$PWD/data/flows/flows.jsonl" | head -n 1 \
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

## Teardown: stop the fixture server

```scrut
$ stop_fixture_server
```