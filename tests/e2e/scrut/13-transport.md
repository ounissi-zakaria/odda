---
prepend:
  - _lib/boot.md
  - _lib/fixture-server.md
  - _lib/browser-fixture.md
append:
  - _lib/teardown.md
---

# Transport: large responses and non-serializable eval returns

The JSON-RPC transport over the Unix socket must carry arbitrarily large
responses (e.g. a wrap dump with thousands of records) and must degrade
gracefully when `eval` returns a value that cannot be JSON-serialized
(e.g. a circular `Window` object from `window.open`). This document
covers both: the readline buffer limit on the transport, and the
`rpc.encode` cycle-safety + write-inside-try fix on the server.

## Set up the fixture server and browser

```scrut
$ setup_fixture_site index.html
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

## A large eval response succeeds (transport limit raised)

A single eval that returns a string larger than the default 64KB
asyncio readline buffer must succeed, not return "Separator is found,
but chunk is longer than limit".

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --js "'x'.repeat(70000)" --browser-id 1 --tab-id 1 \
>   | python3 -c 'import json,sys; s=json.load(sys.stdin); print(len(s) >= 70000)'
True
```

## A large wrap dump succeeds

Install a wrap, re-navigate so it runs, inject 300 records directly
into `window.__oddaWrap`, and dump. With the readline limit raised,
the full 300 records come back in one call (previously failed with the
chunk-limit error at ~200 records).

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wrap calls add --browser-id 1 --tab-id 1 --expr JSON.parse --name big \
>   > /dev/null
```

```scrut
$ navigate_fixture / "window.__oddaDialogInterceptorInstalled"
```

```scrut
$ printf '%s\n' \
>   "window.__oddaWrap = [];" \
>   "for (var i = 0; i < 300; i++) {" \
>   "  window.__oddaWrap.push({wrap: 'big', type: 'call'," \
>   "    this: null, args: ['{\"k\":' + i + '}'], ret: {v: i}," \
>   "    stack: [{fn: 'f', url: 'http://x/b.js', line: i, col: 1}]});" \
>   "}" \
>   "window.__oddaWrap.length;" > "$PWD/inject.js"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --file "$PWD/inject.js" --browser-id 1 --tab-id 1
300
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wrap dump --browser-id 1 --tab-id 1 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(isinstance(d, list) and len(d) == 300)'
True
```

## `wrap dump --name` filters records server-side

With `--name big`, only records from the `big` wrap are returned.
Inject a second wrap's records alongside the first and confirm the
filter narrows to just the named wrap.

```scrut
$ printf '%s\n' \
>   "window.__oddaWrap.push({wrap: 'other', type: 'call'," \
>   "  this: null, args: [], ret: null, stack: []});" \
>   "window.__oddaWrap.length;" > "$PWD/inject2.js"
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --file "$PWD/inject2.js" --browser-id 1 --tab-id 1
301
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wrap dump --name big --browser-id 1 --tab-id 1 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(len(d), all(r["wrap"] == "big" for r in d))'
300 True
```

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wrap dump --name other --browser-id 1 --tab-id 1 \
>   | python3 -c 'import json,sys; d=json.load(sys.stdin); print(len(d), all(r["wrap"] == "other" for r in d))'
1 True
```

## `wrap dump --name` with no matching records returns an empty list

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wrap dump --name no-such-wrap --browser-id 1 --tab-id 1
[]
```

## `wrap dump --name` on a missing tab errors cleanly

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   wrap dump --name big --browser-id 1 --tab-id 9999
[1]
{"error": "Server error (-32602): Tab 9999 not found in browser 1."}
```

## eval returning a non-serializable object returns a response, not a dropped connection

An eval that returns a value with circular references cannot be
JSON-serialized by `rpc.encode`. Previously this dropped the
connection silently ("Server closed connection") because `json.dumps`
raised ValueError *outside* the server's handler try/except. With the
fix, the server returns a JSON response (either a stringified fallback
or a structured error), never a zero-byte connection drop.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --js "var a = {}; a.self = a; a" --browser-id 1 --tab-id 1 \
>   | python3 -c 'import json,sys; raw=sys.stdin.read().strip(); assert raw.startswith("{"), "expected JSON, got: " + raw[:80]; print("json-response")'
json-response
```

The response is valid JSON (the connection did not drop) and the
server is still alive afterwards — a follow-up eval succeeds.

```scrut
$ odda --socket "$PWD/odda.sock" --data-dir "$PWD/data" \
>   eval --js "1 + 1" --browser-id 1 --tab-id 1
2
```

## Teardown: stop the fixture server

```scrut
$ stop_fixture_server
```