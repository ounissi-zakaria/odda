---
prepend:
  - _lib/setup.md
---

# Targeting model: errors on bad browser/tab IDs

Every browser/tab command takes an explicit target. Unknown or stale
IDs must error cleanly with a JSON `{"error": ...}` body and a
non-zero exit code, never silently hit a different tab/browser.

## Boot the server

```scrut {detached: true, detached_kill_signal: term}
$ ( "$ODDA_BIN" server --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   >"$PWD/server.log" 2>&1 & )
```

```scrut {wait: {timeout: 10s, path: "odda.sock"}}
$ echo "server is up"
server is up
```

## Set up a browser

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" browser open --headless \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["browser_id"], d["tab_id"])'
1 1
```

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   navigate http://127.0.0.1:8766/ --browser-id 1 --tab-id 1 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])'
Navigated to: http://127.0.0.1:8766/
```

## `navigate` errors on an unknown browser_id

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   navigate http://x --browser-id 9999 --tab-id 1
[1]
{"error": "Server error (-32602): Browser 9999 not found."}
```

## `navigate` errors on an unknown tab_id

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   navigate http://x --browser-id 1 --tab-id 9999
[1]
{"error": "Server error (-32602): Tab 9999 not found in browser 1."}
```

## `eval` errors on an unknown tab_id

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval "1" --browser-id 1 --tab-id 9999
[1]
{"error": "Server error (-32602): Tab 9999 not found in browser 1."}
```

## `screenshot` errors on an unknown browser_id

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   screenshot --browser-id 9999 --tab-id 1
[1]
{"error": "Server error (-32602): Browser 9999 not found."}
```

## `tabs open` errors on an unknown browser_id

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   tabs open --browser-id 9999
[1]
{"error": "Server error (-32602): Browser 9999 not found."}
```

## `tabs close` errors on an unknown tab_id

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   tabs close --browser-id 1 --tab-id 9999
[1]
{"error": "Server error (-32602): Tab 9999 not found in browser 1."}
```

## `event-listeners` errors on an unknown browser_id

```scrut
$ "$ODDA_BIN" --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   event-listeners --browser-id 9999 --tab-id 1
[1]
{"error": "Server error (-32602): Browser 9999 not found."}
```

## Teardown: stop the odda server

```scrut
$ pkill -f "odda.*--data-dir $PWD/data" 2>/dev/null || true
```
