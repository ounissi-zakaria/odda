---
prepend:
  - _lib/boot.md
  - _lib/fixture-server.md
  - _lib/browser-fixture.md
append:
  - _lib/teardown.md
---

# Browser, tabs, navigation, eval, screenshot, event-listeners, wait-for

Covers everything you'd want to do to a single browser instance: open
it, list its tabs, navigate, run JavaScript, take a screenshot, list
event listeners, and wait for a JS condition.

## Set up the fixture server and browser

```scrut
$ setup_fixture_site index.html dialogs.html
```

```scrut {detached: true, detached_kill_signal: term}
$ port=$(cat "$PWD/fixture_port"); ( python3 -m http.server "$port" --bind 127.0.0.1 --directory "$PWD/site" >"$PWD/http.log" 2>&1 < /dev/null & )
```

```scrut
$ wait_for_fixture_server
```

```scrut
$ open_browser_fixture /
```

## `browser open` returns browser_id and initial tab_id

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" tabs list \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(len(d), d[0]["browser_id"], len(d[0]["tabs"]), d[0]["tabs"][0]["tab_id"])'
1 1 1 1
```

## `navigate` returns `{status: "Navigated to: ..."}`

```scrut
$ port=$(cat "$PWD/fixture_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   navigate --url "http://127.0.0.1:$port/" --browser-id 1 --tab-id 1 \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])'
Navigated to: http://127.0.0.1:* (glob)
```

## `eval` runs JavaScript and returns the result

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --js "document.title" --browser-id 1 --tab-id 1
"Listener Test"
```

### `eval --file` reads JavaScript from a file

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --file "$TESTDIR/fixtures/eval.js" --browser-id 1 --tab-id 1 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(sorted(d.items()))'
[('ok', True), ('title', 'Listener Test')]
```

### `eval` double-encodes when the JS itself calls `JSON.stringify`

`odda eval` returns `JSON.stringify(result)` so the CLI can print it as
JSON. When the JS *also* calls `JSON.stringify`, the result is
double-encoded and needs `json.loads(json.loads(...))` to recover the
value. Returning a plain object (no `JSON.stringify` in JS) is the
recommended form.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --js "JSON.stringify({a: 1})" --browser-id 1 --tab-id 1 \
>   | python3 -c '
> import json, sys
> raw = json.load(sys.stdin)
> print(type(raw).__name__, raw)
> print(json.loads(raw))
> '
str {"a":1}
{'a': 1}
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --js "({a: 1})" --browser-id 1 --tab-id 1 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d, type(d).__name__)'
{'a': 1} dict
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
>   eval --js "1" --file "$TESTDIR/fixtures/eval.js" --browser-id 1 --tab-id 1
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

### `screenshot --output` writes to a path the agent chooses

With `--output <path>`, the screenshot is written to that path (the
parent directory is created) and the returned string is that path.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   screenshot --browser-id 1 --tab-id 1 --output "$PWD/shot.jpeg" \
>   | python3 -c 'import json,sys; print(json.load(sys.stdin))'
*/shot.jpeg (glob)
```

```scrut
$ test -f "$PWD/shot.jpeg" && echo "file exists"
file exists
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
>   wait-for --expression "document.title" --browser-id 1 --tab-id 1 --timeout 5
"Listener Test"
```

## `wait-for` polls until a delayed value lands

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --js "setTimeout(() => { window.__waitTest__ = 'arrived'; }, 1000)" \
>   --browser-id 1 --tab-id 1 > /dev/null
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wait-for --expression "window.__waitTest__" --browser-id 1 --tab-id 1 --timeout 5
"arrived"
```

## `wait-for` times out when the condition never becomes truthy

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wait-for --expression "window.__never__" --browser-id 1 --tab-id 1 --timeout 2
[1]
{*"error": "*Timeout*"*} (glob)
```

## `wait-for` treats a thrown error as falsy and keeps polling

The fixture page has no `#root` element, so
`document.querySelector('#root').children.length` throws a `TypeError`
(null deref) on every poll. `wait-for` catches the throw and keeps
polling until the timeout, rather than crashing on the first
evaluation. The whole point of `wait-for` is "the DOM isn't ready
yet" — a null deref is the common pre-DOM-ready case, not a fatal
error. This times out cleanly like any never-truthy condition.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wait-for --expression "document.querySelector('#root').children.length > 0" \
>   --browser-id 1 --tab-id 1 --timeout 2
[1]
{*"error": "*Timeout*"*} (glob)
```

## Teardown: stop the fixture server

```scrut
$ stop_fixture_server
```