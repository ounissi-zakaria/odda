---
prepend:
  - _lib/setup.md
---

# Tab lifecycle, multi-browser, and ID monotonicity

Tab and browser IDs are integers, monotonic, and never reused. A
closed tab's id is retired forever. The browser stays alive with
zero tabs after its last tab is closed. Multiple browsers are
isolated.

## Boot the server

```scrut {detached: true, detached_kill_signal: term}
$ ( "$ODDA_BIN" server --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   >"$PWD/server.log" 2>&1 & )
```

```scrut {wait: {timeout: 10s, path: "odda.sock"}}
$ echo "server is up"
server is up
```

## Helper: spin up a tiny local HTTP server

```scrut {detached: true, detached_kill_signal: term}
$ ( mkdir -p "$PWD/site" && \
>   python3 -m http.server 8766 --bind 127.0.0.1 --directory "$PWD/site" \
>     >"$PWD/http.log" 2>&1 & )
```

```scrut
$ for i in $(seq 1 30); do curl -s -o /dev/null http://127.0.0.1:8766/ && exit 0; sleep 0.5; done; exit 1
```

## Open a browser with one initial tab

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" browser open --headless \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["browser_id"], d["tab_id"])'
1 1
```

## `tabs list` returns the browser with its initial tab

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" tabs list \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(sum(len(b["tabs"]) for b in d))'
1
```

## `tabs open --url` opens a new tab and returns its id

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   tabs open --browser-id 1 --url http://127.0.0.1:8766/ \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["tab_id"], d["status"])'
2 opened
```

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" tabs list \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(sum(len(b["tabs"]) for b in d))'
2
```

## `tabs open` without `--url` opens a blank tab

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   tabs open --browser-id 1 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["tab_id"])'
3
```

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" tabs list \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(sum(len(b["tabs"]) for b in d))'
3
```

## `tabs close` drops the tab count

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   tabs close --browser-id 1 --tab-id 2 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["tab_id"], d["status"])'
2 closed
```

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" tabs list \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(sum(len(b["tabs"]) for b in d))'
2
```

## A closed tab's id stays dead — `eval` on it errors

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval "1" --browser-id 1 --tab-id 2
[1]
{"error": "Server error (-32602): Tab 2 not found in browser 1."}
```

## New tab IDs are strictly higher than any previous id

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   tabs open --browser-id 1 --url http://127.0.0.1:8766/ \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["tab_id"])'
4
```

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval "1" --browser-id 1 --tab-id 2
[1]
{"error": "Server error (-32602): Tab 2 not found in browser 1."}
```

(`Tab 2` is still dead — IDs are not reused.)

## Closing all tabs leaves the browser alive with zero tabs

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" tabs close --browser-id 1 --tab-id 1 > /dev/null
```

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" tabs close --browser-id 1 --tab-id 3 > /dev/null
```

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" tabs close --browser-id 1 --tab-id 4 > /dev/null
```

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" tabs list --browser-id 1 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(len(d[0]["tabs"]))'
0
```

## A browser with zero tabs can still open new tabs

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   tabs open --browser-id 1 --url http://127.0.0.1:8766/ \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["tab_id"])'
5
```

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" tabs list --browser-id 1 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(len(d[0]["tabs"]))'
1
```

## Multiple browsers are isolated

Close the first browser, then open two more.

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" browser close 1 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["browser_id"], d["status"])'
1 closed
```

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" browser open --headless \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["browser_id"], d["tab_id"])'
2 1
```

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   navigate https://example.org --browser-id 2 --tab-id 1 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])'
Navigated to: https://example.org (glob)
```

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" tabs list \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(sum(len(b["tabs"]) for b in d))'
1
```

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" browser open --headless \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["browser_id"], d["tab_id"])'
3 1
```

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" tabs list \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(len(d))'
2
```

Operating on browser 2 doesn't affect browser 3.

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval "document.title" --browser-id 2 --tab-id 1
"Example Domain"
```

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" browser close 3 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["browser_id"], d["status"])'
3 closed
```

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" browser close 2 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["browser_id"], d["status"])'
2 closed
```

## A closed browser_id is not reused

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   navigate http://x --browser-id 2 --tab-id 1
[1]
{"error": "Server error (-32602): Browser 2 not found."}
```

## Teardown: stop the local HTTP server

The local HTTP server was started in a subshell earlier; kill it
explicitly here so the test doc finishes cleanly.

```scrut
$ pkill -f "http.server 8766.*$PWD/site" 2>/dev/null
```

```scrut
$ sleep 1
```

```scrut
$ pgrep -f "http.server 8766.*$PWD/site" >/dev/null && echo "still running" || echo "stopped"
stopped
```

## Teardown: stop the odda server

```scrut
$ pkill -f "odda.*--data-dir $PWD/data" 2>/dev/null || true
```
