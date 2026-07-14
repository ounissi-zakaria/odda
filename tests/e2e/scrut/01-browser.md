---
prepend:
  - _lib/boot.md
append:
  - _lib/teardown.md
---

# Browser, tabs, navigation, eval, screenshot, event-listeners, wait-for

Covers everything you'd want to do to a single browser instance: open
it, list its tabs, navigate, run JavaScript, take a screenshot, list
event listeners, and wait for a JS condition.

## Helper: spin up a tiny local HTTP server

A few tests need a real URL to navigate to. Use Python's built-in
HTTP server bound to 127.0.0.1 on a fixed port. The `( ... & )`
keeps the process alive after the test case's bash exits.

```scrut {detached: true, detached_kill_signal: term}
$ ( mkdir -p "$PWD/site" && \
>   cp "$TESTDIR/fixtures/index.html" "$PWD/site/index.html" && \
>   cp "$TESTDIR/fixtures/dialogs.html" "$PWD/site/dialogs.html" && \
>   python3 -m http.server 8766 --bind 127.0.0.1 --directory "$PWD/site" \
>     >"$PWD/http.log" 2>&1 & )
```

Wait until the HTTP server answers on port 8766.

```scrut
$ for i in $(seq 1 30); do curl -s -o /dev/null http://127.0.0.1:8766/ && exit 0; sleep 0.5; done; exit 1
```

## `browser open` returns browser_id and initial tab_id

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" browser open --headless \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(sorted(d), d["browser_id"], d["tab_id"])'
['browser_id', 'status', 'tab_id'] 1 1
```

(We can hard-code `1 1` because the test is the only consumer of the
server — `browser_id` and the initial `tab_id` start at 1.)

## `tabs list` reports the new browser with its initial tab

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" tabs list \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(len(d), d[0]["browser_id"], len(d[0]["tabs"]), d[0]["tabs"][0]["tab_id"])'
1 1 1 1
```

## `navigate` returns `{status: "Navigated to: ..."}`

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   navigate http://127.0.0.1:8766/ --browser-id 1 --tab-id 1 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])'
Navigated to: http://127.0.0.1:8766/
```

## `eval` runs JavaScript and returns the result

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval "document.title" --browser-id 1 --tab-id 1
"Listener Test"
```

### `eval --file` reads JavaScript from a file

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --file "$TESTDIR/fixtures/eval.js" --browser-id 1 --tab-id 1 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(sorted(d.items()))'
[('ok', True), ('title', 'Listener Test')]
```

### `eval` with neither inline JS nor `--file` is rejected

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --browser-id 1 --tab-id 1
[1]
{"error": "Provide inline JS or --file <path>"}
```

### `eval` with both inline JS and `--file` is rejected

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval "1" --file "$TESTDIR/fixtures/eval.js" --browser-id 1 --tab-id 1
[1]
{"error": "Provide either inline JS or --file, not both"}
```

### `eval --file` with a missing file is rejected

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --file "$PWD/nope.js" --browser-id 1 --tab-id 1
[1]
{"error": "File not found: *"} (glob)
```

## `screenshot` writes a JPEG and returns its path

The return value is a JSON-quoted string containing the screenshot path.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   screenshot --browser-id 1 --tab-id 1
"*/screenshot_*.jpeg" (glob)
```

## `event-listeners` lists listeners on `window` and `document`

The fixture page registers a `resize` listener on `window` and a
`scroll` listener on `document`, so the response should contain at
least those two.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   event-listeners --browser-id 1 --tab-id 1 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); ts=sorted({l["type"] for l in d}); print(ts, "resize" in ts and "scroll" in ts)'
['resize', 'scroll'] True
```

## `wait-for` returns the truthy value when the condition is already true

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wait-for "document.title" --browser-id 1 --tab-id 1 --timeout 5
"Listener Test"
```

## `wait-for` polls until a delayed value lands

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval "setTimeout(() => { window.__waitTest__ = 'arrived'; }, 1000)" \
>   --browser-id 1 --tab-id 1 > /dev/null
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wait-for "window.__waitTest__" --browser-id 1 --tab-id 1 --timeout 5
"arrived"
```

## `wait-for` times out when the condition never becomes truthy

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wait-for "window.__never__" --browser-id 1 --tab-id 1 --timeout 2
[1]
{*"error": "*Timeout*"*} (glob)
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
