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

`tabs list` prints a table; one browser (id 1) with one tab (id 1).
Column widths depend on content (port, title), so match with regex
that ignores spacing.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" tabs list
browser_id.* (regex)
.* (regex)
1\s+1\s+http://127\.0\.0\.1:\d+/\s+Listener Test (regex)
```

## `navigate` prints the status line

```scrut
$ port=$(cat "$PWD/fixture_port"); odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   navigate --url "http://127.0.0.1:$port/" --browser-id 1 --tab-id 1
Navigated to: http://127.0.0.1:* (glob)
```

## `eval` runs JavaScript and prints the result readably

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --js "document.title" --browser-id 1 --tab-id 1
Listener Test
```

### `eval --file` reads JavaScript from a file

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --file "$TESTDIR/fixtures/eval.js" --browser-id 1 --tab-id 1
{"title": "Listener Test", "ok": true}
```

### `eval` renders objects as compact JSON (no double-encoding in text mode)

In text mode, `eval` renders strings without quotes and objects as
compact JSON. Calling `JSON.stringify` inside the JS no longer
double-encodes — `JSON.stringify({a: 1})` returns a JS string, which
text mode prints raw as `{"a":1}`. Returning a plain object (no
`JSON.stringify` in JS) prints as `{"a": 1}` (with the space after the
colon, matching `json.dumps` defaults). Use `--json` when you need the
value as a JSON-typed value in a pipeline.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --js "JSON.stringify({a: 1})" --browser-id 1 --tab-id 1
{"a":1}
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --js "({a: 1})" --browser-id 1 --tab-id 1
{"a": 1}
```

### `eval` with neither inline JS nor `--file` is rejected

```scrut {output_stream: stderr}
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --browser-id 1 --tab-id 1
[1]
Error: Provide inline JS or --file <path>
```

### `eval` with both inline JS and `--file` is rejected

```scrut {output_stream: stderr}
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --js "1" --file "$TESTDIR/fixtures/eval.js" --browser-id 1 --tab-id 1
[1]
Error: Provide either inline JS or --file, not both
```

### `eval --file` with a missing file is rejected

```scrut {output_stream: stderr}
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --file "$PWD/nope.js" --browser-id 1 --tab-id 1
[1]
Error: File not found: * (glob)
```

## `screenshot` writes a JPEG and prints its path

The text output is the bare path (no JSON quotes).

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   screenshot --browser-id 1 --tab-id 1
*/screenshot_*.jpeg (glob)
```

### `screenshot --output` writes to a path the agent chooses

With `--output <path>`, the screenshot is written to that path (the
parent directory is created) and the printed line is that path.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   screenshot --browser-id 1 --tab-id 1 --output "$PWD/shot.jpeg"
*/shot.jpeg (glob)
```

```scrut
$ test -f "$PWD/shot.jpeg" && echo "file exists"
file exists
```

## `event-listeners` lists listeners on `window` and `document`

The fixture page registers a `resize` listener on `window` and a
`scroll` listener on `document`, so the table should contain at least
those two types. The table is matched with `--json` here so the test
can sort and assert the types structurally.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" --json \
>   event-listeners --browser-id 1 --tab-id 1 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); ts=sorted({l["type"] for l in d}); print(ts, "resize" in ts and "scroll" in ts)'
['resize', 'scroll'] True
```

## `wait-for` prints the truthy value when the condition is already true

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wait-for --expression "document.title" --browser-id 1 --tab-id 1 --timeout 5
Listener Test
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
arrived
```

## `wait-for` times out when the condition never becomes truthy

```scrut {output_stream: stderr}
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wait-for --expression "window.__never__" --browser-id 1 --tab-id 1 --timeout 2
[1]
Error: *Timeout* (glob)
```

## `wait-for` treats a thrown error as falsy and keeps polling

The fixture page has no `#root` element, so
`document.querySelector('#root').children.length` throws a `TypeError`
(null deref) on every poll. `wait-for` catches the throw and keeps
polling until the timeout, rather than crashing on the first
evaluation. The whole point of `wait-for` is "the DOM isn't ready
yet" — a null deref is the common pre-DOM-ready case, not a fatal
error. This times out cleanly like any never-truthy condition.

```scrut {output_stream: stderr}
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wait-for --expression "document.querySelector('#root').children.length > 0" \
>   --browser-id 1 --tab-id 1 --timeout 2
[1]
Error: *Timeout* (glob)
```

## Teardown: stop the fixture server

```scrut
$ stop_fixture_server
```